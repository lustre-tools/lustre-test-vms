"""QEMU process management and subprocess helpers."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

from .vm_state import (
    BRIDGE,
    EXIT_ERROR,
    GATEWAY,
    VMInfo,
    VMNotFound,
    qemu_binary_for_arch,
    qemu_machine_for_arch,
)


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
    """Return /proc/meminfo's <key> value in MiB, or 0 if unreadable."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith(key + ":"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


# Reserve for the host kernel + userspace.  1 GiB or 10% of total RAM,
# whichever is larger.  Host bookkeeping (page cache, sshd, the QEMU
# monitor processes themselves) needs slack -- without it, the OOM
# killer fires on the host instead of refusing the launch up front.
_HOST_MEM_RESERVE_FLOOR_MB = 1024


def _check_memory_for_launch(vm: VMInfo) -> None:
    """Refuse to launch ``vm`` if the host can't accommodate its RAM.

    /proc/meminfo's MemAvailable alone is not a safe signal: QEMU
    allocates guest RAM lazily, so a 4 GiB VM that just booted may
    only show as using ~500 MiB.  A naive "free memory" check would
    happily green-light a second 4 GiB VM and then OOM the host
    minutes later when the first VM faulted in the rest of its pages.

    Instead we sum the *committed* memory of all running VMs (each
    VM's ``-m`` value) and add the new VM, comparing against the
    host's physical RAM minus a reserve.  Conservative but predictable.
    """
    needed_mb = vm.mem
    host_total_mb = _read_meminfo_mb("MemTotal")
    if host_total_mb <= 0:
        # Can't read /proc/meminfo (non-Linux test host?); skip the
        # check rather than block legitimate launches.
        return

    reserve_mb = max(_HOST_MEM_RESERVE_FLOOR_MB, host_total_mb // 10)
    budget_mb = host_total_mb - reserve_mb

    running: list[tuple[str, int]] = []
    for name in VMInfo.all_names():
        if name == vm.name:
            continue
        try:
            other = VMInfo.load(name)
        except VMNotFound:
            continue
        if is_running(other):
            running.append((other.name, other.mem))

    committed_mb = sum(m for _, m in running)
    if committed_mb + needed_mb <= budget_mb:
        return

    lines = [
        f"not enough host memory to start VM '{vm.name}'",
        f"  requested:    {needed_mb} MiB",
        f"  already used: {committed_mb} MiB across "
        f"{len(running)} running VM(s)",
        f"  host budget:  {budget_mb} MiB "
        f"(MemTotal {host_total_mb} MiB - {reserve_mb} MiB reserve)",
        f"  shortfall:    {committed_mb + needed_mb - budget_mb} MiB",
    ]
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
    die("\n".join(lines))


def is_running(vm: VMInfo) -> bool:
    """True iff vm.pid is alive AND points at a qemu process for this VM.

    A bare `os.kill(pid, 0)` check is unsafe across host reboots: PIDs
    get reused, and a long-stopped VM's PID might now be a shell, an
    editor, or another VM.  Without this validation, cmd_doctor sees
    the alien process as "still running", refuses to clean up, and
    cmd_ensure takes the "already running" branch instead of relaunching.
    Read /proc/<pid>/comm to confirm it's a qemu binary.
    """
    if vm.pid <= 0:
        return False
    try:
        os.kill(vm.pid, 0)
    except (OSError, ProcessLookupError):
        return False
    try:
        comm = Path(f"/proc/{vm.pid}/comm").read_text().strip()
    except (OSError, FileNotFoundError):
        return False
    # qemu reports as "qemu-system-x86" / "qemu-system-aarch64" / etc.
    # The Linux comm field is truncated to 15 chars (TASK_COMM_LEN-1),
    # so we substring-match rather than equality-test.
    return comm.startswith("qemu-system")


def launch_qemu(vm: VMInfo) -> None:
    """Launch QEMU for an existing VM. Recreates TAP device."""
    if is_running(vm):
        print(f"VM '{vm.name}' is already running", file=sys.stderr)
        return

    if not vm.overlay_path.exists():
        die(f"overlay missing for '{vm.name}'")

    _check_memory_for_launch(vm)

    # aarch64 virt uses PL011 UART (ttyAMA0); x86 uses 8250 (ttyS0)
    console = "ttyAMA0" if vm.arch == "aarch64" else "ttyS0"

    crashkernel = "512M" if vm.mem >= 2048 else "256M"
    boot_args = (
        f"console={console} reboot=k panic=1 crashkernel={crashkernel} "
        f"net.ifnames=0 biosdevname=0 "
        f"root=/dev/vda rw fc_ip={vm.ip} fc_gw={GATEWAY} "
        f"fc_name={vm.name}"
    )

    # Recreate TAP and flush any stale ARP entry for this IP.
    run(["ip", "link", "del", vm.tap], capture_output=True)
    run(["ip", "neigh", "flush", vm.ip, "dev", BRIDGE], capture_output=True)
    run(
        ["ip", "tuntap", "add", "dev", vm.tap, "mode", "tap"],
        check=True,
    )
    run(["ip", "link", "set", vm.tap, "master", BRIDGE], check=True)
    run(["ip", "link", "set", vm.tap, "up"], check=True)

    if not vm.kernel:
        die(f"VM '{vm.name}' has no kernel path set — recreate with --os")
    kernel = Path(vm.kernel)

    import platform as _platform

    arch = vm.arch
    qemu_bin = qemu_binary_for_arch(arch)
    machine = qemu_machine_for_arch(arch)

    # Device model suffix: microvm uses virtio-*-device (MMIO),
    # virt machine uses virtio-*-pci.
    if arch == "aarch64":
        blk_driver = "virtio-blk-pci"
        net_driver = "virtio-net-pci"
        rng_driver = "virtio-rng-pci"
    else:
        blk_driver = "virtio-blk-device"
        net_driver = "virtio-net-device"
        rng_driver = "virtio-rng-device"

    # KVM allows -cpu host; TCG (cross-arch emulation) needs a real model.
    host_arch = _platform.machine()
    if (arch == "x86_64" and host_arch in ("x86_64", "amd64")) or (
        arch == "aarch64" and host_arch in ("aarch64", "arm64")
    ):
        cpu_model = "host"
    elif arch == "aarch64":
        cpu_model = "cortex-a57"
    else:
        cpu_model = "qemu64"

    qemu_args = [
        qemu_bin,
        "-name",
        vm.name,
        "-machine",
        machine,
        "-cpu",
        cpu_model,
        "-smp",
        str(vm.vcpus),
        "-m",
        str(vm.mem),
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
        f"tap,id=net0,ifname={vm.tap},script=no,downscript=no",
        "-device",
        f"{net_driver},netdev=net0,mac={vm.mac}",
        "-daemonize",
        "-pidfile",
        str(vm.pid_path),
        "-qmp",
        f"unix:{vm.socket_path},server,nowait",
    ]

    total_disks = vm.mdt_disks + vm.ost_disks
    for n in range(1, total_disks + 1):
        disk = vm.disk_path(n)
        if not disk.exists():
            die(f"disk{n} missing for '{vm.name}'")
        qemu_args += [
            "-device",
            f"{blk_driver},drive=disk{n}",
            "-drive",
            f"id=disk{n},file={disk},format=raw,if=none",
        ]

    try:
        with open(vm.log_path, "a") as log:
            r = subprocess.run(qemu_args, stdout=log, stderr=log)
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
        pid = int(vm.pid_path.read_text().strip())
    except BaseException:
        # TAP was created above; tear it down so we don't leak the device.
        # cmd_create has its own broader rollback, but cmd_start, cmd_ensure
        # and cmd_cluster_* call launch_qemu directly with no rollback path,
        # so the TAP would otherwise leak until the next restart of this VM.
        # BaseException catches SystemExit raised by die() so cleanup runs
        # before the process exits.
        run(["ip", "link", "del", vm.tap], capture_output=True)
        raise

    vm.update_pid(pid)
    vm.update_last_boot(int(time.time()))


def kill_qemu(vm: VMInfo) -> None:
    """Kill the QEMU process and tear down the TAP device.

    Validates that vm.pid actually points at a qemu process before
    sending any signals.  Without this guard, after a host reboot or
    PID wraparound vm.pid can refer to an unrelated process (a shell,
    editor, another VM's qemu) and a SIGTERM/SIGKILL would happily
    take it down.  is_running() does the /proc/<pid>/comm check.
    """
    if vm.pid > 0 and is_running(vm):
        try:
            os.kill(vm.pid, signal.SIGTERM)
        except OSError:
            pass
        else:
            # Wait up to 5s for clean shutdown (qcow2 flush)
            for _ in range(50):
                try:
                    os.kill(vm.pid, 0)
                except OSError:
                    break
                time.sleep(0.1)
            else:
                # Still alive after 5s, force kill.  Re-check is_running
                # so we don't SIGKILL a PID that QEMU released to another
                # process during the 5-second wait.
                if is_running(vm):
                    try:
                        os.kill(vm.pid, signal.SIGKILL)
                    except OSError:
                        pass
    try:
        vm.update_pid(0)
    except VMNotFound:
        # Race with cmd_destroy: the .info file was removed between
        # VMInfo.load and now.  We're tearing the VM down anyway, so
        # this is benign.
        pass
    run(["ip", "link", "del", vm.tap], capture_output=True)
    # Flush stale ARP entry so the bridge doesn't poison new VMs or
    # re-creations of this VM that may get a different MAC.
    run(["ip", "neigh", "flush", vm.ip, "dev", BRIDGE], capture_output=True)
