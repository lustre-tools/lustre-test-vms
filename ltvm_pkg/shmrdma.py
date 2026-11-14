"""shmrdma: RDMA and a virtio-net switch between VMs through host memory.

Two halves, both built from the sources under ``shmrdma/``:

* ``shmrdmad``, the host daemon.  It maps the RAM of every VM attached
  to it (QEMU hands over the memfd behind guest RAM through vhost-user)
  and moves data between guests with one memcpy: as an RDMA device
  (``--shmrdma``) and as a switch for virtio-net NICs (``--nic
  shmnet``).  ltvm builds it with the host compiler, cached by source
  hash, and runs one per user, started when a VM that needs it starts.
  It is never stopped or restarted behind a VM's back: QEMU's
  vhost-user devices do not reconnect, so a daemon restart breaks every
  attached guest's device until that VM restarts.

* ``shmrdma.ko``, the guest driver.  It is built per kernel in the
  target's build container and baked into the image (image_build), so a
  VM gets the device at boot with no deploy step.
"""

from __future__ import annotations

import fcntl
import getpass
import hashlib
import json
import logging
import os
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import vm_state
from .paths import load_meta_safe

if TYPE_CHECKING:
    from .target_config import TargetConfig
    from .vm_state import VMInfo

log = logging.getLogger(__name__)

SRC_DIR = Path(__file__).resolve().parent.parent / "shmrdma"

# Guest RAM QEMU can hand to shmrdmad: a memfd on Linux, POSIX shm
# (shm_open) elsewhere.
# (Through a variable: mypy evaluates sys.platform tests.)
_LINUX = sys.platform.startswith("linux")
SHARED_RAM_BACKEND = "memory-backend-memfd" if _LINUX else "memory-backend-shm"

_DAEMON_FILES = (
    "daemon/shmrdmad.c",
    "daemon/plat.h",
    "daemon/plat_linux.h",
    "daemon/plat_macos.h",
    "daemon/shmnet.c",
    "include/shmrdma_abi.h",
)
KMOD_FILES = ("kmod/shmrdma.c", "kmod/Makefile", "include/shmrdma_abi.h")
# Baked into the image next to the module, into /usr/local/sbin.
GUEST_FILES = ("guest/shmrdma-boot.sh", "tools/smc_ueid.py")

# Guest modprobe config baked next to the module.  The device is a
# vhost-user-fs to QEMU, so virtiofs matches it too; loading shmrdma
# first lets it take its device (it refuses any whose tag is not
# "shmrdma") before virtiofs sees it.
MODPROBE_CONF = (
    "# ltvm: shmrdma rides on a vhost-user-fs device tagged shmrdma\n"
    "softdep virtiofs pre: shmrdma\n"
)

DAEMON_CFLAGS = (
    "-O2",
    "-g",
    "-Wall",
    "-Wno-unused-parameter",
    "-Wno-unused-result",
    "-pthread",
    "-std=gnu11",
)


def _source_hash(files: tuple[str, ...]) -> str:
    h = hashlib.sha256()
    for rel in files:
        h.update(rel.encode())
        h.update(b"\0")
        h.update((SRC_DIR / rel).read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def kmod_source_hash() -> bytes:
    """The guest module's sources and boot files, as image inputs."""
    return _source_hash(KMOD_FILES + GUEST_FILES).encode()


# --- the guest module -------------------------------------------------


def kmod_dir(tc: TargetConfig, kernel: str | None = None) -> Path:
    """Per-kernel cache of the built module."""
    return tc.kernel_output_dir(kernel) / "shmrdma"


def _kmod_input_hash(kver: str, kernel_hash: str) -> str:
    h = hashlib.sha256()
    for part in (kver, kernel_hash, _source_hash(KMOD_FILES)):
        h.update(part.encode())
        h.update(b"\0")
    return h.hexdigest()


def build_kmod(tc: TargetConfig, kernel: str | None, kver: str) -> Path | None:
    """shmrdma.ko for *kernel*, built if missing or stale.

    Returns None, with a warning, when it cannot be built: the image is
    then built without it, and ``create --shmrdma`` refuses that image.
    """
    out = kmod_dir(tc, kernel)
    ko = out / "shmrdma.ko"
    build_tree = tc.kernel_output_dir(kernel) / "build-tree"
    kmeta = load_meta_safe(tc.meta_path("kernel", kernel)) or {}
    kh = kmeta.get("input_hash")
    want = _kmod_input_hash(kver, kh if isinstance(kh, str) else "")
    meta = load_meta_safe(out / "meta.json") or {}
    if meta.get("input_hash") == want and ko.is_file():
        return ko
    if not build_tree.is_dir():
        log.warning("shmrdma: no kernel build tree at %s", build_tree)
        return None
    out.mkdir(parents=True, exist_ok=True)
    ko.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="shmrdma-kmod-") as tmp:
        src = Path(tmp)
        for rel in KMOD_FILES:
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SRC_DIR / rel, src / rel)
        cmd = [
            "podman",
            "run",
            "--rm",
            "--security-opt",
            "label=disable",
            "-v",
            f"{build_tree}:/kbuild:ro",
            "-v",
            f"{src}:/src",
            "--entrypoint",
            "bash",
            tc.container_tag,
            "-c",
            "make -C /kbuild M=/src/kmod modules",
        ]
        log.info("Building shmrdma.ko for %s ...", kver)
        r = subprocess.run(cmd, capture_output=True, text=True)
        built = src / "kmod" / "shmrdma.ko"
        if r.returncode != 0 or not built.is_file():
            tail = "\n".join((r.stdout + r.stderr).splitlines()[-15:])
            log.warning(
                "shmrdma.ko did not build for %s; the image will not "
                "have it, and create --shmrdma will refuse it:\n%s",
                kver,
                tail,
            )
            return None
        shutil.copy2(built, ko)
    (out / "meta.json").write_text(
        json.dumps({"kernel": kver, "input_hash": want}, indent=2) + "\n"
    )
    return ko


def image_has_kmod(image: str | Path) -> bool:
    """Whether the image at *image* was built with shmrdma.ko."""
    meta = load_meta_safe(Path(image).parent / "meta.json") or {}
    return bool(meta.get("shmrdma_kmod"))


# --- the host daemon --------------------------------------------------


class UnsafeDir(RuntimeError):
    """A daemon directory someone else owns or has redirected."""


def _owner() -> tuple[str, int]:
    """(user, uid) the daemon belongs to: under sudo, the one who typed it."""
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        name = os.environ["SUDO_USER"]
        return name, pwd.getpwnam(name).pw_uid
    return getpass.getuser(), os.geteuid()


def _check_private(d: Path, uid: int) -> None:
    """Refuse *d* unless it is a real directory of *uid* that only it can use."""
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
        raise UnsafeDir(f"{d} is not a directory owned by uid {uid}")
    if stat.S_IMODE(st.st_mode) & 0o077:
        if os.geteuid() != uid and os.geteuid() != 0:
            raise UnsafeDir(f"{d} is open to other users")
        os.chmod(d, 0o700, follow_symlinks=False)


def _candidate_dirs(uid: int) -> list[Path]:
    home = Path(pwd.getpwuid(uid).pw_dir)
    state = (
        Path(os.environ["XDG_STATE_HOME"])
        if os.environ.get("XDG_STATE_HOME") and os.geteuid() == uid
        else home / ".local" / "state"
    )
    return [
        vm_state.VM_DIR / f"shmrdma-{uid}",
        state / "ltvm" / "shmrdma",
    ]


def run_dir(uid: int, create: bool = True) -> Path:
    """The private directory of *uid*'s daemon: sockets, pid, log, lock.

    ``/opt/qemu-vms/shmrdma-<uid>`` (mode 0700, owned by the user), which
    outlives a login and which both a QEMU running as the user and one
    running as root reach; the user's state directory when that one
    cannot be used, or someone else made it first.  With *create*
    false, the first that exists, or the first candidate.
    """
    cands = _candidate_dirs(uid)
    for d in cands:
        if not create:
            if os.path.lexists(d):
                return d
            continue
        try:
            if d is cands[-1]:
                d.parent.mkdir(parents=True, exist_ok=True)
            try:
                d.mkdir(mode=0o700)
                if os.geteuid() != uid:
                    os.chown(d, uid, pwd.getpwuid(uid).pw_gid)
            except FileExistsError:
                pass
            _check_private(d, uid)
            return d
        except (OSError, UnsafeDir) as e:
            log.debug("shmrdmad: not using %s: %s", d, e)
    if not create:
        return cands[0]
    raise RuntimeError(
        f"no private directory for shmrdmad ({', '.join(map(str, cands))})"
    )


def _open_private(path: Path, flags: int) -> int:
    """os.open *path* in a checked directory, refusing symlinks."""
    return os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


@dataclass(frozen=True)
class Daemon:
    """One user's shmrdmad: its files, whether or not it runs."""

    user: str
    uid: int
    dir: Path

    @property
    def rdma_socket(self) -> Path:
        return self.dir / "rdma.sock"

    @property
    def net_socket(self) -> Path:
        return self.dir / "net.sock"

    @property
    def pid_path(self) -> Path:
        return self.dir / "shmrdmad.pid"

    @property
    def log_path(self) -> Path:
        return self.dir / "shmrdmad.log"

    @property
    def lock_path(self) -> Path:
        return self.dir / "shmrdmad.lock"

    def _record(self) -> list[str]:
        try:
            fd = _open_private(self.pid_path, os.O_RDONLY)
        except OSError:
            return []
        with os.fdopen(fd) as f:
            return f.read(256).split()

    def pid(self) -> int:
        """The running daemon's pid, or 0.

        Only a shmrdmad process of this user, running the binary the pid
        file names, counts: a pid file is no proof of who listens.
        """
        rec = self._record()
        try:
            pid, exe_want = int(rec[0]), rec[2]
        except (ValueError, IndexError):
            return 0
        uid, exe = _proc_owner_exe(pid)
        if Path(exe).name != "shmrdmad" or uid != self.uid:
            return 0
        return pid if exe == exe_want else 0

    def binary_hash(self) -> str:
        """Source hash the running daemon was built from, or ""."""
        rec = self._record()
        return rec[1] if len(rec) > 1 else ""


def _proc_owner_exe(pid: int) -> tuple[int, str]:
    """(uid, executable path) of *pid*, or (-1, "") if it is gone."""
    if _LINUX:
        try:
            uid = os.stat(f"/proc/{pid}").st_uid
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return -1, ""
        # comm, not the path, names a process that exec'd elsewhere
        return (uid, exe) if comm == Path(exe).name else (-1, "")
    # macOS: no /proc; ps prints the full executable path as comm
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "uid=", "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        uid_s, exe = r.stdout.strip().split(None, 1)
        return int(uid_s), exe
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return -1, ""


def daemon(create: bool = True) -> Daemon:
    """This user's daemon; *create* false never makes its directory."""
    user, uid = _owner()
    return Daemon(user, uid, run_dir(uid, create))


def _cache_dir(uid: int | None = None) -> Path:
    uid = os.geteuid() if uid is None else uid
    if uid == os.geteuid() and os.environ.get("XDG_CACHE_HOME"):
        base = Path(os.environ["XDG_CACHE_HOME"])
    else:
        base = Path(pwd.getpwuid(uid).pw_dir) / ".cache"
    return base / "ltvm" / "shmrdmad"


def build_daemon() -> tuple[Path, str]:
    """The shmrdmad binary for the current sources, building it if needed."""
    digest = _source_hash(_DAEMON_FILES)[:16]
    out = _cache_dir() / digest / "shmrdmad"
    if out.is_file():
        return out, digest
    cc = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc")
    if not cc:
        raise RuntimeError(
            "shmrdmad is built on the host and needs a C compiler: install gcc"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".shmrdmad.{os.getpid()}")
    r = subprocess.run(
        [cc, *DAEMON_CFLAGS, "-o", str(tmp), "shmrdmad.c"],
        cwd=SRC_DIR / "daemon",
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"building shmrdmad failed:\n{r.stderr.strip()}")
    tmp.replace(out)
    return out, digest


def vm_uses(vm: VMInfo, d: Daemon) -> bool:
    """Whether *vm*, as last started, is attached to daemon *d*."""
    socks = {str(d.rdma_socket), str(d.net_socket)}
    return any(s in socks for s in vm.shm_sockets)


def vm_sockets(vm: VMInfo, d: Daemon) -> list[str]:
    """The vhost-user sockets *vm* connects to, with *d*'s for its own."""
    socks = []
    if vm.shmrdma:
        socks.append(str(d.rdma_socket) if vm.shmrdma == "auto" else vm.shmrdma)
    for nic in vm.nics:
        if nic.type == "shmnet":
            socks.append(nic.socket or str(d.net_socket))
    return socks


def attached_vms(d: Daemon) -> list[str]:
    """Running VMs that use daemon *d*."""
    from .qemu_run import is_running
    from .vm_state import VMInfo, VMNotFound

    names = []
    for name in VMInfo.all_names():
        try:
            vm = VMInfo.load(name)
        except (VMNotFound, ValueError):
            continue
        if vm.shared_ram and vm_uses(vm, d) and is_running(vm):
            names.append(name)
    return names


@contextmanager
def _daemon_lock(d: Daemon) -> Iterator[None]:
    fd = _open_private(d.lock_path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def ensure_daemon() -> Daemon:
    """Start this user's shmrdmad unless it already runs; return it."""
    d = daemon()
    if os.geteuid() != d.uid:
        # Under sudo: use the user's own daemon, never start one as root.
        if d.pid():
            return d
        raise RuntimeError(
            f"shmrdmad is not running for {d.user}, and ltvm will not start "
            f"it as root: run `ltvm vm shmrdmad start` as {d.user} first"
        )
    with _daemon_lock(d):
        if d.pid():
            if d.binary_hash() != _source_hash(_DAEMON_FILES)[:16]:
                log.info(
                    "shmrdmad (pid %d) predates the current sources; it "
                    "keeps running until `ltvm vm shmrdmad stop`",
                    d.pid(),
                )
            return d
        binary, digest = build_daemon()
        for s in (d.rdma_socket, d.net_socket):
            s.unlink(missing_ok=True)
        from .qemu_run import _guest_scope
        from .vm_state import VMInfo

        scope = _guest_scope(VMInfo(name=f"shmrdmad-{d.user}", ip=""))
        cpus = daemon_cpus()
        pin = ["taskset", "-c", cpus] if cpus else []
        extra = os.environ.get("LTVM_SHMRDMAD_ARGS", "").split()
        logfd = _open_private(
            d.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND
        )
        with os.fdopen(logfd, "a") as logf:
            logf.write(
                f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting {binary}\n"
            )
            logf.flush()
            proc = subprocess.Popen(
                [
                    *scope,
                    *pin,
                    str(binary),
                    "-s",
                    str(d.rdma_socket),
                    "-n",
                    str(d.net_socket),
                    *extra,
                ],
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        pidfd = _open_private(d.pid_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        with os.fdopen(pidfd, "w") as f:
            f.write(f"{proc.pid} {digest} {binary}\n")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            if d.rdma_socket.is_socket() and d.net_socket.is_socket():
                return d
            time.sleep(0.05)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        d.pid_path.unlink(missing_ok=True)
        raise RuntimeError(f"shmrdmad did not come up; see {d.log_path}")


def daemon_nodes() -> list[int]:
    """Host NUMA nodes the daemon runs on: ``LTVM_SHMRDMAD_NODES`` or
    /etc/ltvm.conf's ``[shmrdma] nodes``, comma-separated; empty is
    unpinned."""
    raw = os.environ.get("LTVM_SHMRDMAD_NODES")
    if raw is None:
        import configparser

        from . import site_config

        parser = configparser.ConfigParser()
        try:
            parser.read(site_config.path())
            raw = parser.get("shmrdma", "nodes", fallback="")
        except configparser.Error:
            raw = ""
    try:
        return [int(n) for n in raw.replace(" ", "").split(",") if n]
    except ValueError:
        log.warning("shmrdmad nodes %r are not node numbers; unpinned", raw)
        return []


def daemon_cpus() -> str:
    """The cpulist the daemon is pinned to, or "" for none."""
    from .qemu_run import host_node_cpus

    lists = [host_node_cpus(n) for n in daemon_nodes()]
    if not all(lists):
        log.warning(
            "shmrdmad nodes %s: not all exist; unpinned", daemon_nodes()
        )
        return ""
    return ",".join(lists)


def stop_daemon(force: bool = False) -> str:
    """Stop this user's shmrdmad; refuse while VMs use it unless *force*."""
    from .qemu_run import _launch_lock

    d = daemon(create=False)
    # Under the launch lock: a VM whose launch got past ensure_daemon
    # has no QEMU pid recorded until that lock is released.
    with _launch_lock():
        pid = d.pid()
        if not pid:
            return "shmrdmad is not running"
        users = attached_vms(d)
        if users and not force:
            raise RuntimeError(
                f"shmrdmad is in use by {', '.join(users)}: their shmrdma "
                f"devices and shmnet NICs stop working until they restart.  "
                f"Stop them first, or pass --force"
            )
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not d.pid():
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
        for p in (d.pid_path, d.rdma_socket, d.net_socket):
            p.unlink(missing_ok=True)
    return f"stopped shmrdmad (pid {pid})"


def status() -> dict[str, object]:
    d = daemon(create=False)
    pid = d.pid()
    current = _source_hash(_DAEMON_FILES)[:16]
    return {
        "running": bool(pid),
        "cpus": daemon_cpus(),
        "pid": pid,
        "user": d.user,
        "rdma_socket": str(d.rdma_socket),
        "net_socket": str(d.net_socket),
        "log": str(d.log_path),
        "current": (d.binary_hash() == current) if pid else None,
        "vms": attached_vms(d) if pid else [],
    }


def prepare_launch(vm: VMInfo) -> list[str]:
    """Make sure every socket *vm* needs is served; return them.

    ltvm's own daemon is started on demand.  A socket given by hand
    belongs to a daemon run by hand, so it must already be there.
    """
    from .qemu_run import die

    needs_own = vm.shmrdma == "auto" or any(
        n.type == "shmnet" and not n.socket for n in vm.nics
    )
    try:
        d = ensure_daemon() if needs_own else daemon(create=False)
    except (RuntimeError, OSError) as e:
        die(f"{vm.name}: {e}")
    socks = vm_sockets(vm, d)
    for s in socks:
        if not Path(s).is_socket():
            die(f"{vm.name} needs shmrdmad, but nothing listens on {s}")
    return socks
