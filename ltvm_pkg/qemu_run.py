"""QEMU process management and subprocess helpers."""

from __future__ import annotations

import configparser
import fcntl
import math
import os
import pwd
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from . import rootless, site_config, vm_state
from .host_setup import is_macos, socket_vmnet_socket_path
from .priv import ensure_dir, ensure_lock_file, invoking_user, sudo_run
from .vm_state import (
    BRIDGE,
    EXIT_ERROR,
    GATEWAY,
    PREFIX_LEN,
    PREFIX_LEN6,
    VMInfo,
    VMNotFound,
    qemu_binary_for_arch,
    qemu_machine_for_arch,
)

# x86_64 and arm64 Linux both define COMMAND_LINE_SIZE as 2048.
# Reserve the terminating NUL; the x86 boot header advertises SIZE-1.
# https://github.com/torvalds/linux/blob/v6.12/arch/x86/include/asm/setup.h
# https://github.com/torvalds/linux/blob/v6.12/arch/arm64/include/uapi/asm/setup.h
PROFILE_CMDLINE_MAX_BYTES = 2047


def run(
    cmd: list[str] | str, **kwargs: Any
) -> subprocess.CompletedProcess[Any]:
    """Run a command, return CompletedProcess."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


def die(msg: str, code: int = EXIT_ERROR) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _read_meminfo_mb(key: str) -> int:
    """Return /proc/meminfo's <key> value in MiB, or 0 if unreadable.

    On macOS only MemTotal is supported, resolved via
    ``sysctl -n hw.memsize``.  Other keys return 0.
    """
    if is_macos():
        if key != "MemTotal":
            return 0
        try:
            r = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                check=True,
            )
            return int(r.stdout.strip()) // (1024 * 1024)
        except (OSError, subprocess.CalledProcessError, ValueError):
            return 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith(key + ":"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


_CGROUP_ROOT = Path("/sys/fs/cgroup")


def _cgroup_memory_limit_mb(
    proc_cgroup: Path = Path("/proc/self/cgroup"),
    root: Path = _CGROUP_ROOT,
    relative: str | None = None,
) -> int:
    """The tightest memory.max over a cgroup and its parents, in MiB, or 0
    for none.  The cgroup is ``relative`` to ``root`` when given, else this
    process's own.

    In a container MemTotal is the host's, not what the container's QEMU
    may use: past its limit the kernel kills a guest instead.
    """
    if relative is None:
        try:
            text = proc_cgroup.read_text()
        except OSError:
            return 0
        relative = next(
            (line[3:] for line in text.splitlines() if line.startswith("0::")),
            None,
        )
    if relative is None:
        return 0
    directory = root / relative.strip().lstrip("/")
    limits = []
    while True:
        try:
            value = (directory / "memory.max").read_text().strip()
        except OSError:
            value = "max"
        if value.isdigit():
            limits.append(int(value))
        if directory == root or root not in directory.parents:
            break
        directory = directory.parent
    return min(limits) // (1024 * 1024) if limits else 0


# Reserve for the host kernel + userspace.  1 GiB or 10% of total RAM,
# whichever is larger.  Host bookkeeping (page cache, sshd, the QEMU
# monitor processes themselves) needs slack -- without it, the OOM
# killer fires on the host instead of refusing the launch up front.
_HOST_MEM_RESERVE_FLOOR_MB = 1024

# How often a launch waiting for host memory looks again.
_MEMORY_POLL_SECONDS = 5


@dataclass(frozen=True)
class _Shortfall:
    message: str
    # Whether stopping the running VMs would make room at all.
    fits_when_idle: bool


# A --wait re-reads the site file every few seconds; say what is wrong once.
_site_config_warned: set[str] = set()


def _memory_overcommit() -> float:
    """/etc/ltvm.conf's ``[memory] overcommit``, or 1.0 if unset or bad."""
    site = site_config.path()
    parser = configparser.ConfigParser()
    try:
        parser.read(site)
        ratio = parser.getfloat("memory", "overcommit", fallback=1.0)
    except (configparser.Error, ValueError) as e:
        problem = str(e)
    else:
        if math.isfinite(ratio) and ratio >= 1.0:
            return ratio
        problem = f"overcommit must be a number >= 1.0, not {ratio}"
    if problem not in _site_config_warned:
        _site_config_warned.add(problem)
        print(
            f"warning: ignoring [memory] in {site}: {problem}",
            file=sys.stderr,
        )
    return 1.0


@dataclass(frozen=True)
class MemoryBudget:
    """What the VMs on this host may commit, as create and start count it."""

    total_mb: int  # MemTotal, or the cgroup's memory.max when smaller
    source: str  # which of the two
    reserve_mb: int
    overcommit: float

    @property
    def budget_mb(self) -> int:
        """The most one VM may have."""
        return self.total_mb - self.reserve_mb

    @property
    def limit_mb(self) -> int:
        """The most the running VMs may have together."""
        return int(self.budget_mb * self.overcommit)

    def describe(self) -> str:
        return (
            f"{self.source} {self.total_mb} MiB - {self.reserve_mb} MiB reserve"
        )


def memory_budget(cgroup: str | None = None) -> MemoryBudget | None:
    """The budget for guests in ``cgroup`` (None: this process's), or
    None when the host's memory cannot be read."""
    total_mb = _read_meminfo_mb("MemTotal")
    if total_mb <= 0:
        return None
    source = "MemTotal"
    cgroup_mb = _cgroup_memory_limit_mb(relative=cgroup)
    if 0 < cgroup_mb < total_mb:
        total_mb, source = cgroup_mb, "cgroup memory.max"
    reserve_mb = max(_HOST_MEM_RESERVE_FLOOR_MB, total_mb // 10)
    return MemoryBudget(total_mb, source, reserve_mb, _memory_overcommit())


def guest_memory_budget() -> MemoryBudget | None:
    """The budget a plain VM started now from this process is held to."""
    return memory_budget(_guest_cgroup(VMInfo(name="ltvm-budget", ip="")))


def running_vm_memory(skip: str = "") -> list[tuple[str, int]]:
    """(name, MiB) of every running VM but ``skip``."""
    running: list[tuple[str, int]] = []
    for name in VMInfo.all_names():
        if name == skip:
            continue
        try:
            other = VMInfo.load(name)
        except VMNotFound:
            continue
        except ValueError as e:
            # VMInfo.load fails loud on a corrupt int field by design
            # (TestVMInfoLoadCorruption), and that must stay scoped to
            # the damaged VM.  This walk runs before every launch, so
            # letting it out meant one truncated .info made `create`
            # and `start` fail for every *other* VM on the host -- and
            # inside cmd_create the raise lands in `except
            # BaseException`, which rolls back the healthy VM being
            # built.  vm_net._used_ips guards the same hazard for the
            # same reason; this call site was missed.
            print(
                f"warning: ignoring corrupt VM state for {name!r} "
                f"while checking host memory: {e}",
                file=sys.stderr,
            )
            continue
        if is_running(other):
            running.append((other.name, other.mem))
    return running


def _memory_shortfall(vm: VMInfo) -> _Shortfall | None:
    """Why the host can't accommodate ``vm``'s RAM now, or None if it can.

    /proc/meminfo's MemAvailable alone is not a safe signal: QEMU
    allocates guest RAM lazily, so a 4 GiB VM that just booted may
    only show as using ~500 MiB.  A naive "free memory" check would
    happily green-light a second 4 GiB VM and then OOM the host
    minutes later when the first VM faulted in the rest of its pages.

    Instead we sum the *committed* memory of all running VMs (each
    VM's ``-m`` value) and add the new VM, comparing against the
    host's physical RAM minus a reserve.  Conservative but predictable.

    A host that merges identical guest pages (KSM) holds far less than
    that.  ``[memory] overcommit`` in /etc/ltvm.conf lets the running
    VMs together commit that multiple of the budget; a single VM must
    still fit the budget itself.
    """
    needed_mb = vm.mem
    budget = memory_budget(_guest_cgroup(vm))
    if budget is None:
        # Can't read /proc/meminfo (non-Linux test host?); skip the
        # check rather than block legitimate launches.
        return None
    host_total_mb, total_source = budget.total_mb, budget.source
    reserve_mb, budget_mb = budget.reserve_mb, budget.budget_mb
    overcommit, limit_mb = budget.overcommit, budget.limit_mb

    running = running_vm_memory(skip=vm.name)
    committed_mb = sum(m for _, m in running)
    if needed_mb <= budget_mb and committed_mb + needed_mb <= limit_mb:
        return None

    shortfall_mb = max(
        committed_mb + needed_mb - limit_mb, needed_mb - budget_mb
    )
    lines = [
        f"not enough host memory to start VM '{vm.name}'",
        f"  requested:    {needed_mb} MiB",
        f"  already used: {committed_mb} MiB across "
        f"{len(running)} running VM(s)",
        f"  host budget:  {budget_mb} MiB "
        f"({total_source} {host_total_mb} MiB - {reserve_mb} MiB reserve)",
    ]
    if overcommit != 1.0:
        lines.append(
            f"  overcommit:   {overcommit:g} ({site_config.path()}), so "
            f"running VMs may commit {limit_mb} MiB, but no single VM "
            f"more than the budget"
        )
    lines.append(f"  shortfall:    {shortfall_mb} MiB")
    if running:
        lines.append("")
        lines.append("running VMs (largest first):")
        for name, mb in sorted(running, key=lambda x: (-x[1], x[0])):
            lines.append(f"  {name:<24} {mb} MiB")
        lines.append("")
        lines.append("free memory by stopping one or more:")
        lines.append("  ltvm stop <name> [<name>...]")
    else:
        lines.append("")
        lines.append(
            "no other VMs are running -- try a smaller --mem value, "
            "or free host memory."
        )
    return _Shortfall("\n".join(lines), needed_mb <= budget_mb)


@contextmanager
def _launch_lock() -> Iterator[None]:
    """Serialise the memory check with the launch it lets through.

    Two launches that each find room in the same free memory would
    otherwise both start and overcommit the host, and memory freed by a
    stopping VM wakes every waiting ``--wait`` at once.  Held until the
    new QEMU's pid is recorded, which is when ``is_running`` counts it.
    """
    path = vm_state.VM_DIR / ".launch.lock"
    ensure_dir(path.parent)
    try:
        ensure_lock_file(path)
    except (OSError, RuntimeError):
        pass
    try:
        fh = open(path, "a")
    except PermissionError:
        fh = open(path)
    with fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def is_running(vm: VMInfo) -> bool:
    """True iff vm.pid is alive AND points at a qemu process for this VM.

    We check /proc/<pid>/comm directly rather than `os.kill(pid, 0)`:

      * /proc/<pid>/comm is world-readable on standard Linux so an
        unprivileged `ltvm list` correctly sees a root-owned qemu
        as running.  `os.kill(pid, 0)` against another user's pid
        returns EPERM (OSError), which previously made `ltvm list`
        claim "stopped" for every VM to a non-root caller.
      * The /proc read doubles as the PID-reuse guard the old
        implementation added os.kill for: if pid was reused by an
        unrelated process, comm won't start with "qemu-system" and
        we correctly return False.  cmd_doctor / cmd_ensure keep
        working across host reboots.

    The Linux comm field is truncated to 15 chars (TASK_COMM_LEN-1)
    so we substring-match "qemu-system" rather than equality-test.

    comm alone only rules out reuse by a *non-QEMU* process, which is
    not enough: after a host reboot without `ltvm stop`, PIDs restart
    low and so do the stale PIDs left in .info files, so VM A's stale
    pid can land on VM B's live QEMU.  `ltvm list` then shows A
    running, `ltvm start A` says "already running" and never boots it,
    and `ltvm stop A` / `ltvm destroy A` SIGTERMs -- then SIGKILLs --
    B's QEMU while tearing down A's artifacts.  QEMU is launched with
    ``-name <vm.name>``, so the command line carries the identity;
    check it.  If the command line can't be read we fall back to the
    comm check rather than regress `ltvm list` for an unprivileged
    caller.

    A VM whose .info says PID=0 is checked against its pidfile too: QEMU
    holds that file while it runs, so a live QEMU named there was
    recorded as stopped by mistake (a stop whose signal never landed).
    Its pid is adopted into *vm*, in memory only, so that list shows it,
    stop kills it and start refuses rather than tripping on the lock.
    """
    if vm.pid > 0:
        return _qemu_pid_is(vm.pid, vm.name)
    pid = _pidfile_pid(vm)
    if pid > 0 and _qemu_pid_is(pid, vm.name):
        vm.pid = pid
        return True
    return False


def numa_problem(vcpus: int, mem: int, nodes: int) -> str | None:
    """Why *nodes* NUMA nodes cannot split this VM, or None if they can."""
    if nodes < 0:
        return f"--numa-nodes must be >= 0 (got {nodes})"
    if nodes <= 1:
        return None
    if vcpus % nodes:
        return f"--numa-nodes {nodes} must divide the {vcpus} vCPUs evenly"
    if mem // nodes < 128:
        return f"{mem} MB is under 128 MB for each of {nodes} NUMA nodes"
    return None


def smp_args(vcpus: int, mem: int, nodes: int) -> list[str]:
    """-smp, -m and, for a simulated NUMA layout, -numa arguments.

    Each node is one socket with an equal share of the vCPUs and its own
    memory backend; memory is split in whole 2 MB units, the last node
    taking the remainder, so the backends add up to -m exactly.
    """
    if nodes <= 1:
        return ["-smp", str(vcpus), "-m", str(mem)]
    per_cpu = vcpus // nodes
    per_mem = (mem // nodes) // 2 * 2
    args = [
        "-smp",
        f"{vcpus},sockets={nodes},cores={per_cpu},threads=1",
        "-m",
        str(mem),
    ]
    for i in range(nodes):
        size = per_mem if i < nodes - 1 else mem - per_mem * (nodes - 1)
        first = i * per_cpu
        cpus = f"{first}-{first + per_cpu - 1}" if per_cpu > 1 else f"{first}"
        args += [
            "-object",
            f"memory-backend-ram,id=numa{i},size={size}M",
            "-numa",
            f"node,nodeid={i},cpus={cpus},memdev=numa{i}",
        ]
    return args


# The cache mode of a disk that two QEMUs open at once.  Both must use
# the same one: a write one process holds in the host page cache is
# invisible to the other's O_DIRECT reads, and the MMP block is exactly
# such a write.  "none" bypasses the host cache on both sides, as a
# shared SAN LUN would.
SHARED_DISK_CACHE = "none"


def data_disk_args(vm: VMInfo, blk_driver: str = "virtio-blk-pci") -> list[str]:
    """The -device/-drive pairs for *vm*'s data disks (vdb, vdc, ...).

    A failover partner (``disks_from``) has no disks of its own and
    attaches its primary's, in the primary's order, so /dev/vdb is the
    same target on both guests.  QEMU locks an image against a second
    writer unless every QEMU that opens it says share-rw=on, so the
    primary (``share_disks``) and the partner both do.  A VM with
    neither gets its own disks exactly as before.

    ``disk_cache`` adds cache=MODE.  Preallocated disks ignore the
    guest's discards and zero writes, which would otherwise punch holes
    in them (mkfs discards the whole device).
    """
    if vm.disks_from:
        try:
            owner = VMInfo.load(vm.disks_from)
        except VMNotFound:
            die(
                f"'{vm.name}' attaches the disks of '{vm.disks_from}', "
                f"which does not exist"
            )
        if not owner.share_disks:
            die(
                f"'{vm.name}' attaches the disks of '{owner.name}', which "
                f"was not created with --share-disks"
            )
        source = owner
        shared = True
    else:
        source = vm
        shared = vm.share_disks
    zeroes = (
        "discard=ignore,detect-zeroes=off"
        if source.disk_prealloc
        else "discard=unmap,detect-zeroes=unmap"
    )
    cache = SHARED_DISK_CACHE if shared else vm.disk_cache
    disks = source.disk_paths()
    if disks and source.disk_dir and not source.disk_dir_path.is_dir():
        die(
            f"the data disk directory of '{source.name}', "
            f"{source.disk_dir}, is not there (not mounted?)"
        )
    args: list[str] = []
    for n, disk in enumerate(disks, start=1):
        if not disk.exists():
            die(f"disk{n} missing for '{vm.name}': {disk}")
        device = f"{blk_driver},drive=disk{n}"
        drive = f"id=disk{n},file={disk},format=raw,if=none,{zeroes}"
        if shared:
            device += ",share-rw=on"
        if cache:
            drive += f",cache={cache}"
        args += ["-device", device, "-drive", drive]
    return args


def _pidfile_pid(vm: VMInfo) -> int:
    try:
        return int(vm.pid_path.read_text().strip())
    except (OSError, ValueError):
        return 0


def _qemu_pid_is(pid: int, name: str) -> bool:
    """Is *pid* a live QEMU launched for the VM called *name*?"""
    if is_macos():
        # Only args=, not comm=: macOS truncates comm to 16 characters
        # *including the directory*, so /opt/qemu/bin/qemu-system-aarch64
        # came back as "/opt/qemu/bin/qe" and every running VM -- whose
        # root-owned QEMU the caller cannot signal to check otherwise --
        # was listed as stopped.  argv[0] carries the whole path.
        try:
            r = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return False
        if r.returncode != 0:
            return False
        ps_args = r.stdout.split()
        if not ps_args:
            return False
        if not Path(ps_args[0]).name.startswith("qemu-system"):
            return False
        return _cmdline_names_vm(ps_args, name, strict=False)
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return False
    if not comm.startswith("qemu-system"):
        return False
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        # Unreadable: keep the pre-identity behavior rather than
        # reporting a live VM as stopped.
        return True
    parts = [a.decode(errors="replace") for a in argv.split(b"\0") if a]
    if not parts:
        # Zombie or otherwise empty cmdline: no identity information,
        # so don't call a live VM stopped.
        return True
    return _cmdline_names_vm(parts, name, strict=True)


def _cmdline_names_vm(argv: list[str], name: str, *, strict: bool) -> bool:
    """True if *argv* is a QEMU invocation for the VM called *name*.

    Matches the ``-name <name>`` pair launch_qemu passes.  When the
    argv carries no ``-name`` at all we cannot tell (``strict=False``
    for the macOS ``ps`` path, whose output may be truncated), so we
    accept rather than declare a running VM stopped.
    """
    for i, arg in enumerate(argv):
        if arg == "-name":
            return i + 1 < len(argv) and argv[i + 1] == name
        if arg.startswith("-name="):
            return arg.split("=", 1)[1] == name
    return not strict


def _as_root(cmd: list[str]) -> list[str]:
    """Prefix sudo unless we already are root."""
    if os.geteuid() == 0:
        return cmd
    return ["sudo", *cmd]


def _prepare_log(vm: Any) -> None:
    """Make the QEMU log writable by whoever is running ltvm.

    SOCKETS is root-owned 0755 on purpose -- `doctor` asserts that mode
    -- so an unprivileged process cannot create a file in it, though it
    can write one it already owns.  Create the log as root once and hand
    it over, the same bargain the .info files get.  Without this,
    `ltvm create` and `ltvm start` fail for a non-root user with a
    PermissionError naming the log, which reads like a bug in the
    logging rather than a privilege boundary.
    """
    if os.access(vm.log_path, os.W_OK):
        return
    if os.geteuid() != 0:
        try:
            fd = os.open(
                vm.log_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                0o644,
            )
            os.close(fd)
            return
        except PermissionError:
            pass
    sudo_run(["touch", str(vm.log_path)], quiet=True)
    owner = invoking_user()
    if owner:
        sudo_run(
            ["chown", f"{owner[0]}:{owner[1]}", str(vm.log_path)], quiet=True
        )
    sudo_run(["chmod", "644", str(vm.log_path)], quiet=True)


GUEST_SLICE = "ltvm-guests.slice"
_PROC_SELF_CGROUP = Path("/proc/self/cgroup")
_LINGER_DIR = Path("/var/lib/systemd/linger")
_SCOPE_REFUSALS = (
    "Failed to connect to bus",
    "Failed to connect to user scope bus",
    "Failed to start transient scope unit",
)


def _manager_outlives_login(uid: int) -> bool:
    """Will ``user@UID.service`` outlive the caller's login?

    It stops at logout unless the user lingers, while a login's session
    scope survives it.  A caller already running under it is tied to it
    anyway.
    """
    try:
        cgroup = _PROC_SELF_CGROUP.read_text()
    except OSError:
        cgroup = ""
    for line in cgroup.splitlines():
        if line.startswith("0::"):
            if f"user@{uid}.service" in line[3:].split("/"):
                return True
            break
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = None
    if user is not None:
        return (_LINGER_DIR / user).exists()
    if not shutil.which("loginctl"):
        return False
    try:
        r = subprocess.run(
            ["loginctl", "show-user", str(uid), "-p", "Linger", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and r.stdout.strip() == "yes"


def _guest_cgroup(vm: VMInfo) -> str | None:
    """The cgroup a launched guest's QEMU will live in, when not ltvm's own.

    A guest that gets a user scope of its own (``_guest_scope``) leaves the
    caller's cgroup, so a memory.max on that -- a tool's transient scope, a
    service -- does not bound it, and the user manager's app.slice does.
    ``None`` means the caller's cgroup, as for a root QEMU.
    """
    if os.geteuid() == 0 or any(
        n.split(":", 1)[0] == "passthrough" for n in vm.nics
    ):
        return None
    if not _guest_scope(vm) or not rootless.readiness().ok:
        return None
    uid = os.geteuid()
    return f"/user.slice/user-{uid}.slice/user@{uid}.service/app.slice"


def _guest_scope(vm: VMInfo) -> list[str]:
    """A ``systemd-run`` prefix that gives an unprivileged QEMU a scope of its own.

    QEMU daemonizes but keeps its caller's cgroup, so a guest started from a
    service, or from a tool in a transient scope, dies when that unit is
    stopped or killed.  In a user scope of its own it belongs to no caller.
    ``[]`` where there is no user manager to ask, where the manager stops at
    logout, or ``LTVM_GUEST_SCOPE=0``.
    """
    if os.environ.get("LTVM_GUEST_SCOPE", "1") == "0" or os.geteuid() == 0:
        return []
    # Through a variable: mypy evaluates a sys.platform test, so on a Mac
    # the rest of the function would be "unreachable".
    linux = sys.platform.startswith("linux")
    if not linux or not shutil.which("systemd-run"):
        return []
    if not (
        os.environ.get("XDG_RUNTIME_DIR")
        or os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    ):
        return []
    if not _manager_outlives_login(os.geteuid()):
        return []
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", vm.name)
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit=ltvm-{name}-{int(time.time())}-{secrets.token_hex(4)}.scope",
        f"--slice={GUEST_SLICE}",
        f"--description=ltvm guest {vm.name}",
        "--",
    ]


def _scope_refused(log_path: Path, offset: int) -> bool:
    """Did systemd-run fail to make the scope, rather than QEMU fail to start?"""
    try:
        with open(log_path, "rb") as log:
            log.seek(offset)
            text = log.read().decode(errors="replace")
    except OSError:
        return False
    return any(refusal in text for refusal in _SCOPE_REFUSALS)


def launch_qemu(vm: VMInfo, *, wait_seconds: int = 0) -> None:
    """Launch QEMU for an existing VM. Recreates TAP device.

    A host whose memory budget cannot take the VM is refused at once, or
    with ``wait_seconds`` waited on for up to that long.
    """
    if is_running(vm):
        print(f"VM '{vm.name}' is already running", file=sys.stderr)
        return

    if not vm.overlay_path.exists():
        die(f"overlay missing for '{vm.name}'")

    from .image_store import check_backing

    refusal = check_backing(vm)
    if refusal:
        die(refusal)

    if is_macos():
        from .host_setup import ensure_socket_vmnet_running

        try:
            ensure_socket_vmnet_running()
        except RuntimeError as e:
            die(str(e))

    deadline = time.monotonic() + wait_seconds
    waited = False
    while True:
        with _launch_lock():
            # Another start can finish while this process waits for the lock.
            started_pid = _pidfile_pid(vm)
            if is_running(vm) or (
                started_pid > 0 and _qemu_pid_is(started_pid, vm.name)
            ):
                print(f"VM '{vm.name}' is already running", file=sys.stderr)
                return
            shortfall = _memory_shortfall(vm)
            if shortfall is None:
                _start_qemu(vm)
                return
        if not shortfall.fits_when_idle:
            die(shortfall.message)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if waited:
                die(
                    f"{shortfall.message}\n\ngave up after waiting {wait_seconds}s"
                )
            die(f"{shortfall.message}\nor wait for them: --wait SECONDS")
        if not waited:
            print(
                f"{shortfall.message.splitlines()[0]}; waiting up to "
                f"{wait_seconds}s for running VMs to free it",
                file=sys.stderr,
            )
            waited = True
        time.sleep(min(_MEMORY_POLL_SECONDS, remaining))


# An NMI injected from the host (`ltvm vm nmi`) panics into kdump with
# no ssh needed, which is when it is wanted: the guest has stopped
# answering.  QEMU delivers it as an ISA SERR NMI, which panics only
# with panic_on_unrecovered_nmi; the other two cover other kernels.
# sysctl.* on the cmdline needs 5.8 or later; the guest's sysctl.d
# drop-in (vm_commands.NMI_SYSCTL_CONF) covers rocky8's 4.18.  x86 only:
# the knobs are CONFIG_X86.
NMI_PANIC_ARGS = (
    " unknown_nmi_panic sysctl.kernel.panic_on_unrecovered_nmi=1"
    " sysctl.kernel.panic_on_io_nmi=1"
)


def _start_qemu(vm: VMInfo) -> None:
    """Launch QEMU for ``vm``, under the launch lock, its memory admitted."""
    # aarch64 virt uses PL011 UART (ttyAMA0); x86 uses 8250 (ttyS0)
    console = "ttyAMA0" if vm.arch == "aarch64" else "ttyS0"

    crashkernel = "512M" if vm.mem >= 2048 else "256M"
    # Thread the list of extra NIC *types* onto the kernel cmdline as
    # fc_nics=<csv>.  rc.local uses this to drive per-type setup for
    # each extra interface (eth1, eth2, ...).  The mgmt NIC (eth0) is
    # still configured from fc_ip / fc_gw / fc_name as before.
    # Empty list -> omit the parameter entirely so existing single-NIC
    # VMs' cmdline is byte-identical to the pre-feature path.
    extra_nics = vm.extra_nics()
    topology = vm.rail_topology
    if topology is not None:
        # Profile facts are persisted; a restart must not derive a new fabric.
        from .rail_host import require_bridges
        from .vm_commands import _require_profile_image

        topology.validate_for_nics(vm.nics)
        _require_profile_image(vm.image, topology)
        if any(token.startswith("fc_") for token in vm.kernel_args.split()):
            die(
                "rail profiles reserve fc_* kernel parameters for their saved topology"
            )
        require_bridges(topology)
    fc_nics_fragment = ""
    fc_nic_ips_fragment = ""
    fc_nic_prefixes_fragment = ""
    fc_nic_ip6s_fragment = ""
    fc_nic_ip6_prefixes_fragment = ""
    if extra_nics:
        # Replace the ':' in 'passthrough:0000:00:02.0' with ';' on the
        # cmdline so the CSV separator stays unambiguous.  rc.local
        # reverses this when parsing.  'tcp' has no arg, so it's
        # unaffected.
        cmdline_types = [
            nic_type.replace(":", ";")
            for (_idx, nic_type, _tap, _mac) in extra_nics
        ]
        fc_nics_fragment = f" fc_nics={','.join(cmdline_types)}"
        # Extra-NIC IPs.  Same index order as fc_nics; i.e. the Nth
        # entry in fc_nic_ips is the IP that rc.local should assign
        # to eth{N+1}.  Empty IPs (shouldn't happen on a freshly
        # created VM but may on an old .info file) become bare commas
        # so rc.local can count positions.
        #
        # fc_nic_ips carries bare dotted quads, never a /prefix: an
        # image older than fc_nic_prefixes appends its own "/24" and
        # would silently assign nothing if the address already had one.
        # The prefix travels in its own parallel array, which such an
        # image simply ignores -- and 24 is what it assumes anyway.
        if vm.nic_ips:
            fc_nic_ips_fragment = f" fc_nic_ips={','.join(vm.nic_ips)}"
            fc_nic_prefixes_fragment = (
                " fc_nic_prefixes="
                + ",".join(str(rail.prefix4) for rail in topology.rails)
                if topology
                else " fc_nic_prefixes="
                + ",".join(str(PREFIX_LEN) for _ in vm.nic_ips)
            )
        # The extras' IPv6 addresses travel in their own pair of
        # parallel arrays, omitted entirely when there are none: a VM
        # created before extras carried IPv6 keeps a byte-identical
        # cmdline, and an image built before them ignores two unknown
        # parameters.
        if vm.nic_ip6s:
            fc_nic_ip6s_fragment = f" fc_nic_ip6s={','.join(vm.nic_ip6s)}"
            fc_nic_ip6_prefixes_fragment = (
                " fc_nic_ip6_prefixes="
                + ",".join(str(rail.prefix6) for rail in topology.rails)
                if topology
                else " fc_nic_ip6_prefixes="
                + ",".join(str(PREFIX_LEN6) for _ in vm.nic_ip6s)
            )
    boot_args = (
        f"console={console} reboot=k panic=1 crashkernel={crashkernel} "
        f"net.ifnames=0 biosdevname=0 "
        f"systemd.journald.forward_to_console=1 systemd.log_target=console "
        f"root=/dev/vda rw fc_ip={vm.ip} fc_gw={GATEWAY} "
        f"fc_name={vm.name}"
        f"{fc_nics_fragment}"
        f"{fc_nic_ips_fragment}"
        f"{fc_nic_prefixes_fragment}"
        f"{fc_nic_ip6s_fragment}"
        f"{fc_nic_ip6_prefixes_fragment}"
    )
    if vm.arch in ("", "x86_64"):
        boot_args += NMI_PANIC_ARGS
    if topology is not None:
        boot_args += (
            f" fc_rail_topology={topology.mode} fc_lnet_nets="
            + ",".join(rail.lnet_network for rail in topology.rails)
        )
    # Last, so that a parameter whose last occurrence wins
    # (crashkernel=, panic=) takes the user's value.
    if vm.kernel_args:
        boot_args += f" {vm.kernel_args}"
    if (
        topology is not None
        and len(boot_args.encode()) > PROFILE_CMDLINE_MAX_BYTES
    ):
        # A longer string would be silently truncated before guest rail setup reads it.
        die(
            f"rail kernel command line is {len(boot_args.encode())} bytes; "
            f"limit is {PROFILE_CMDLINE_MAX_BYTES}; shorten --kernel-args"
        )

    # Recreate TAP and flush any stale ARP entry for this IP.  Also
    # tear down any extra-NIC TAPs from a previous launch so ``ltvm
    # start`` on a VM created with ``--nic tcp`` doesn't leak TAPs
    # across restarts.  macOS has no per-VM host device: socket_vmnet
    # multiplexes every guest onto one Unix socket managed by launchd.
    all_taps = [vm.tap] + [t for (_i, _n, t, _m) in extra_nics]
    macos = is_macos()
    has_passthrough = any(n.split(":", 1)[0] == "passthrough" for n in vm.nics)
    # On a shared host QEMU runs as the user.  On Linux each NIC's tap
    # is created by the bridge helper and goes away with QEMU; on macOS
    # QEMU connects to socket_vmnet's socket itself.  Passthrough still
    # needs a root QEMU for the vfio device.
    helper: Path | None = None
    as_user = False
    if not has_passthrough and os.geteuid() != 0:
        ready = rootless.readiness()
        if ready.ok:
            helper = ready.helper
            as_user = True
    if topology is not None and helper is not None:
        # The helper must authorize every selected data bridge, not only fcbr0.
        for rail in topology.rails:
            if not rootless.bridge_allowed(rail.bridge):
                die(
                    f"bridge helper does not allow {rail.bridge}; run ltvm install --install"
                )
    vmnet_socket: str | None = None
    if macos:
        vmnet_socket = str(socket_vmnet_socket_path())
    elif topology is not None and helper is None:
        if os.geteuid() != 0:
            die(
                "rail profiles need the configured bridge helper; run ltvm install --install"
            )
        # Profile TAPs are created inside the launch rollback block below.
    elif helper is None:
        # On Linux the TAP is created with ``user $LOGNAME`` so QEMU can
        # attach to it without root via TUNSETIFF; the ``ip`` calls
        # themselves still need sudo for CAP_NET_ADMIN.
        import getpass as _getpass

        _user = _getpass.getuser()
        for _tap in all_taps:
            sudo_run(["ip", "link", "del", _tap], check=False, quiet=True)
        sudo_run(
            ["ip", "neigh", "flush", vm.ip, "dev", BRIDGE],
            check=False,
            quiet=True,
        )
        sudo_run(
            [
                "ip",
                "tuntap",
                "add",
                "dev",
                vm.tap,
                "mode",
                "tap",
                "user",
                _user,
            ],
            check=True,
            quiet=True,
        )
        sudo_run(
            ["ip", "link", "set", vm.tap, "master", BRIDGE],
            check=True,
            quiet=True,
        )
        sudo_run(
            ["ip", "link", "set", vm.tap, "up"],
            check=True,
            quiet=True,
        )

    # Extra NICs: create one TAP per declared nic.  They all join the
    # same bridge as the mgmt NIC for now (tcp only); softroce (-r55)
    # and passthrough (-5a0) will override this dispatch and may take
    # a different path entirely (rxe on top of the bridge for softroce,
    # vfio-pci with no TAP for passthrough).  Keep the construction
    # shape as a per-type dispatch so those follow-ups slot in without
    # reworking this loop.
    for _idx, _nic_type, _tap, _mac in extra_nics:
        # Strip the ':arg' suffix for dispatch -- 'passthrough:BDF'
        # still needs a TAP-less path; the BDF is only read in the
        # later qemu-args loop.
        _base_type = _nic_type.split(":", 1)[0]
        if _base_type in ("tcp", "softroce"):
            # softroce presents to QEMU exactly like tcp (a virtio-net
            # on the bridge); the rxe layer is built inside the guest
            # at boot via setup-nic-softroce.sh.
            if macos or helper is not None or topology is not None:
                continue
            sudo_run(
                [
                    "ip",
                    "tuntap",
                    "add",
                    "dev",
                    _tap,
                    "mode",
                    "tap",
                    "user",
                    _user,
                ],
                check=True,
                quiet=True,
            )
            sudo_run(
                ["ip", "link", "set", _tap, "master", BRIDGE],
                check=True,
                quiet=True,
            )
            sudo_run(
                ["ip", "link", "set", _tap, "up"],
                check=True,
                quiet=True,
            )
        elif _base_type == "passthrough":
            # No host TAP: the VF is attached directly to the guest
            # via vfio-pci.  The host-side bind-to-vfio happened in
            # cmd_create; launch_qemu only emits the QEMU flag below.
            pass
        else:
            die(
                f"internal error: launch_qemu saw unknown NIC type "
                f"{_nic_type!r} on VM {vm.name!r}"
            )

    if not vm.kernel:
        die(f"VM '{vm.name}' has no kernel path set — recreate with --target")
    kernel = Path(vm.kernel)

    import platform as _platform

    arch = vm.arch
    qemu_bin = qemu_binary_for_arch(arch)
    machine = qemu_machine_for_arch(arch)

    # q35 (x86) and virt (aarch64) both have a PCI bus, so virtio
    # devices attach as virtio-*-pci.  Previously x86 used microvm and
    # virtio-*-device (MMIO); q35 replaced microvm after benchmarking
    # showed only ~300 ms of boot overhead.
    blk_driver = "virtio-blk-pci"
    net_driver = "virtio-net-pci"
    rng_driver = "virtio-rng-pci"

    # KVM allows -cpu host; TCG (cross-arch emulation) needs a real model.
    #
    # For x86_64 TCG we use Nehalem rather than the default qemu64.  Rocky
    # 9 (and any EL9-derived userspace) ships glibc compiled for the
    # x86-64-v2 microarchitecture level, which requires CMPXCHG16B, LAHF
    # /SAHF, POPCNT, SSE3/SSSE3/SSE4.1/SSE4.2.  qemu64 exposes none of
    # those, so /sbin/init aborts with "Fatal glibc error: CPU does not
    # support x86-64-v2" and the kernel panics.  Nehalem (Intel 2008) is
    # the baseline CPU model that satisfies v2 in full.
    host_arch = _platform.machine()
    import os as _os

    _force_tcg = _os.environ.get("LTVM_FORCE_TCG") == "1"
    if not _force_tcg and (
        (arch == "x86_64" and host_arch in ("x86_64", "amd64"))
        or (arch == "aarch64" and host_arch in ("aarch64", "arm64"))
    ):
        # "-cpu host" is only valid with a hardware accelerator; under
        # TCG it must be a concrete model.
        cpu_model = "host"
    elif arch == "aarch64":
        cpu_model = "cortex-a57"
    else:
        cpu_model = "Nehalem"

    qemu_args = [
        qemu_bin,
        "-name",
        vm.name,
        "-machine",
        machine,
        "-cpu",
        cpu_model,
        *smp_args(vm.vcpus, vm.mem, vm.numa_nodes),
        "-kernel",
        str(kernel),
        "-append",
        boot_args,
        "-nodefaults",
        "-no-user-config",
        "-nographic",
        "-object",
        "rng-random,id=rng0,filename=/dev/urandom",
        "-device",
        f"{rng_driver},rng=rng0",
        "-serial",
        "chardev:serial0",
        "-chardev",
        f"file,id=serial0,path={vm.log_path}",
        "-device",
        f"{blk_driver},drive=rootfs",
        "-drive",
        f"id=rootfs,file={vm.overlay_path},format=qcow2,if=none",
        "-netdev",
        (
            f"stream,id=net0,addr.type=unix,addr.path={vmnet_socket},server=off"
            if macos
            else f"bridge,id=net0,br={BRIDGE},helper={helper}"
            if helper is not None
            else f"tap,id=net0,ifname={vm.tap},script=no,downscript=no"
        ),
        "-device",
        f"{net_driver},netdev=net0,mac={vm.mac}",
        "-daemonize",
        "-pidfile",
        str(vm.pid_path),
        "-qmp",
        f"unix:{vm.socket_path},server,nowait",
    ]

    # Extra NICs: per-type dispatch.  The mgmt NIC (net0 / vm.tap /
    # vm.mac) was already emitted above; here we append one netdev +
    # one device for each entry in vm.nics.  IDs are net1, net2, ...
    # so they correspond 1:1 to the guest's eth1, eth2, ... (QEMU
    # assigns PCI slots in args order on q35 / aarch64 virt).
    # The per-type dispatch shape is deliberate: softroce (-r55) will
    # add a `softroce` branch that looks a lot like this tcp branch
    # but with extra rxe-related guest-side setup; passthrough (-5a0)
    # will add a `passthrough` branch that emits `-device vfio-pci,
    # host=<BDF>` with no -netdev / no TAP.  The current CLI parser
    # rejects both, so those branches aren't emitted today -- but the
    # loop shape is what lets them slot in without reworking.
    if has_passthrough:
        # vfio-pci pins guest memory; QEMU needs -mem-prealloc up-front
        # so DMA translations are stable at launch time.  Harmless for
        # VMs without passthrough but we scope it to avoid the RAM
        # commit cost on the common case.
        qemu_args += ["-mem-prealloc"]

    for _idx, _nic_type, _tap, _mac in extra_nics:
        _base_type = _nic_type.split(":", 1)[0]
        _netdev_id = f"net{_idx}"
        if _base_type in ("tcp", "softroce"):
            # softroce's QEMU surface is identical to tcp; see the
            # TAP-create loop above.
            if macos:
                _netdev_arg = (
                    f"stream,id={_netdev_id},addr.type=unix,"
                    f"addr.path={vmnet_socket},server=off"
                )
            elif helper is not None:
                bridge = topology.rails[_idx - 1].bridge if topology else BRIDGE
                _netdev_arg = (
                    f"bridge,id={_netdev_id},br={bridge},helper={helper}"
                )
            else:
                _netdev_arg = (
                    f"tap,id={_netdev_id},ifname={_tap},script=no,downscript=no"
                )
            qemu_args += [
                "-netdev",
                _netdev_arg,
                "-device",
                f"{net_driver},netdev={_netdev_id},mac={_mac}",
            ]
        elif _base_type == "passthrough":
            # Parse the BDF out of 'passthrough:<BDF>'.
            _bdf = _nic_type.split(":", 1)[1]
            # pcie-root-port gives the vfio'd device a dedicated slot;
            # q35 and aarch64 virt both expose a PCIe root complex.
            # Chassis numbers must be unique per root port.
            qemu_args += [
                "-device",
                f"pcie-root-port,id=rp{_idx},chassis={_idx}",
                "-device",
                f"vfio-pci,host={_bdf},bus=rp{_idx}",
            ]
        else:
            die(
                f"internal error: launch_qemu saw unknown NIC type "
                f"{_nic_type!r} on VM {vm.name!r}"
            )

    qemu_args += data_disk_args(vm, blk_driver)

    profile_taps_cleanup = topology is not None and helper is not None
    try:
        if topology is not None and helper is None:
            from .rail_host import prepare_taps, remove_taps

            # A crashed root-run QEMU can leave its owned persistent TAPs behind.
            remove_taps(vm)
            profile_taps_cleanup = True
            prepare_taps(vm)
        _prepare_log(vm)
        with open(vm.log_path, "a") as log:
            # QEMU runs as root: it creates its pidfile and QMP socket in
            # the root-owned SOCKETS dir, and attaches the TAP.  The log
            # is opened here, unprivileged, and inherited as fd 1/2 --
            # sudo preserves those.
            argv = qemu_args if as_user else _as_root(qemu_args)
            scope = _guest_scope(vm) if as_user else []
            log.flush()
            logged = os.fstat(log.fileno()).st_size
            r = subprocess.run([*scope, *argv], stdout=log, stderr=log)
            if (
                r.returncode != 0
                and scope
                and _scope_refused(vm.log_path, logged)
            ):
                r = subprocess.run(argv, stdout=log, stderr=log)
        if r.returncode != 0:
            die(
                f"QEMU failed to start for '{vm.name}' "
                f"(rc={r.returncode}); see {vm.log_path}"
            )
        # -daemonize returns once the parent exits, but the child may not
        # have written its pidfile yet.  Poll briefly so we don't false-
        # positive a successful launch into a rollback.
        for _ in range(20):
            if vm.pid_path.exists():
                break
            time.sleep(0.1)
        if not vm.pid_path.exists():
            die(
                f"QEMU pidfile not written within 2s: {vm.pid_path}; "
                f"QEMU likely failed to start"
            )
        # QEMU wrote its pidfile as root, 0600, in a directory the
        # invoking user cannot write.  Hand it over before reading it,
        # the same bargain the QMP socket gets below -- a pid is not a
        # secret, and without this an unprivileged ltvm cannot read the
        # pid of the VM it just started.
        owner = invoking_user() if not as_user else None
        if owner is not None:
            sudo_run(
                ["chown", f"{owner[0]}:{owner[1]}", str(vm.pid_path)],
                check=False,
                quiet=True,
            )
        pid = int(vm.pid_path.read_text().strip())
        # QMP socket is created by QEMU (running as root) as 0600.  The
        # invoking human should be able to send NMI and other QMP
        # commands without sudo, so hand them the socket -- but keep it
        # 0600.  QMP exposes human-monitor-command and `migrate exec:`,
        # both of which spawn a shell as the QEMU process owner (root
        # for a VM created under sudo), so a world-writable socket in
        # the 0755 SOCKETS dir is local code execution as that owner.
        try:
            if owner is not None:
                sudo_run(
                    ["chown", f"{owner[0]}:{owner[1]}", str(vm.socket_path)],
                    check=False,
                    quiet=True,
                )
            os.chmod(vm.socket_path, 0o600)
        except OSError:
            pass
    except BaseException:
        # TAPs were created above; tear them down so we don't leak
        # devices.  cmd_create has its own broader rollback, but
        # cmd_start, cmd_ensure and cmd_cluster_* call launch_qemu
        # directly with no rollback path, so the TAPs would otherwise
        # leak until the next restart of this VM.  BaseException
        # catches SystemExit raised by die() so cleanup runs before
        # the process exits.  macOS has no TAPs -- socket_vmnet owns
        # the L2 fabric and no per-VM host state was created here.
        if profile_taps_cleanup:
            from .rail_host import remove_taps

            remove_taps(vm)
        elif topology is None and not macos and helper is None:
            for _tap in all_taps:
                sudo_run(
                    ["ip", "link", "del", _tap],
                    check=False,
                    quiet=True,
                )
        raise

    vm.update_pid(pid)
    vm.update_last_boot(int(time.time()))


def _pid_alive(pid: int) -> bool:
    """Is *pid* still around?

    EPERM means the process exists but belongs to another uid -- the
    normal case for an unprivileged `ltvm stop` against a QEMU started
    by `sudo ltvm create`.  Treating it as "gone" (which a bare
    `except OSError: break` does) made kill_qemu report a clean
    shutdown while QEMU kept running, then mark the VM stopped and
    delete its TAP, leaving an unreachable orphan that `ltvm destroy`
    would not kill and whose overlay it would unlink underneath.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _signal_qemu(vm: VMInfo, sig: int) -> bool:
    """Signal a VM's QEMU, escalating to sudo when it isn't ours.

    A VM created with `sudo ltvm create` runs QEMU as root, but stop
    and destroy are documented as unprivileged commands, so the direct
    os.kill() gets EPERM.  Fall back to `sudo kill` rather than
    silently doing nothing.  Returns False when the signal could not
    be sent: sudo refused, or had no terminal to ask for a password.
    """
    try:
        os.kill(vm.pid, sig)
        return True
    except ProcessLookupError:
        return True
    except PermissionError:
        pass
    except OSError:
        return False
    r = sudo_run(
        ["kill", f"-{int(sig)}", str(vm.pid)],
        check=False,
        quiet=True,
    )
    return r.returncode == 0


def kill_qemu(vm: VMInfo) -> None:
    """Kill the QEMU process and tear down the TAP device.

    Validates that vm.pid actually points at a qemu process before
    sending any signals.  Without this guard, after a host reboot or
    PID wraparound vm.pid can refer to an unrelated process (a shell,
    editor, another VM's qemu) and a SIGTERM/SIGKILL would happily
    take it down.  is_running() does the /proc/<pid>/comm check.

    Dies, leaving PID in .info, when QEMU outlives both signals -- most
    often a root QEMU whose `sudo kill` had no password.  Recording it
    as stopped hid a live guest from list, stop and destroy, and the
    next start failed on QEMU's locked pidfile.
    """
    if is_running(vm):
        sent = _signal_qemu(vm, signal.SIGTERM)
        # Wait up to 5s for clean shutdown (qcow2 flush)
        for _ in range(50 if sent else 0):
            if not _pid_alive(vm.pid):
                break
            time.sleep(0.1)
        else:
            # Still alive after 5s, force kill.  Re-check is_running
            # so we don't SIGKILL a PID that QEMU released to another
            # process during the 5-second wait.
            if is_running(vm) and _signal_qemu(vm, signal.SIGKILL):
                for _ in range(20):
                    if not _pid_alive(vm.pid):
                        break
                    time.sleep(0.1)
            if _pid_alive(vm.pid) and is_running(vm):
                die(
                    f"{vm.name}: QEMU pid {vm.pid} is still running and "
                    f"could not be signalled; it belongs to another "
                    f"user. Stop it from a terminal: sudo ltvm stop "
                    f"{vm.name}"
                )
    try:
        vm.update_pid(0)
    except VMNotFound:
        # Race with cmd_destroy: the .info file was removed between
        # VMInfo.load and now.  We're tearing the VM down anyway, so
        # this is benign.
        pass
    # Delete the mgmt TAP plus every extra NIC TAP.  Missing TAPs are
    # ignored (capture_output swallows the ip-link error), so this is
    # safe even when launch_qemu never created them (e.g. a kill on a
    # VM that failed partway through its own launch).  On macOS there
    # are no per-VM TAPs -- socket_vmnet multiplexes every guest onto
    # one Unix socket -- so teardown is a no-op there.
    if is_macos():
        return
    if vm.rail_topology is not None:
        from .rail_host import remove_taps

        remove_taps(vm)
        return
    taps = [vm.tap] + [t for (_i, _n, t, _m) in vm.extra_nics()]
    if os.geteuid() != 0 and rootless.readiness().ok:
        # A helper-attached tap closes with QEMU.  Only one left by an
        # earlier privileged start can still be here, and removing it
        # is worth a try but not a password prompt.
        for tap in taps:
            if Path("/sys/class/net", tap).exists():
                sudo_run(
                    ["ip", "link", "del", tap],
                    check=False,
                    quiet=True,
                    noninteractive=True,
                )
        return
    for tap in taps:
        sudo_run(["ip", "link", "del", tap], check=False, quiet=True)
    # Flush stale ARP entry so the bridge doesn't poison new VMs or
    # re-creations of this VM that may get a different MAC.
    sudo_run(
        ["ip", "neigh", "flush", vm.ip, "dev", BRIDGE],
        check=False,
        quiet=True,
    )
