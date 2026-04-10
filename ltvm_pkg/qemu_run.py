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


def is_running(vm: VMInfo) -> bool:
    if vm.pid <= 0:
        return False
    try:
        os.kill(vm.pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def launch_qemu(vm: VMInfo) -> None:
    """Launch QEMU for an existing VM. Recreates TAP device."""
    if is_running(vm):
        print(f"VM '{vm.name}' is already running", file=sys.stderr)
        return

    if not vm.overlay_path.exists():
        die(f"overlay missing for '{vm.name}'")

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
    if (arch == "x86_64" and host_arch in ("x86_64", "amd64")) or \
       (arch == "aarch64" and host_arch in ("aarch64", "arm64")):
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
        "-object", "rng-random,id=rng0,filename=/dev/urandom",
        "-device", f"{rng_driver},rng=rng0",
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
    """Kill the QEMU process and tear down the TAP device."""
    if vm.pid > 0:
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
                # Still alive after 5s, force kill
                try:
                    os.kill(vm.pid, signal.SIGKILL)
                except OSError:
                    pass
    vm.update_pid(0)
    run(["ip", "link", "del", vm.tap], capture_output=True)
    # Flush stale ARP entry so the bridge doesn't poison new VMs or
    # re-creations of this VM that may get a different MAC.
    run(["ip", "neigh", "flush", vm.ip, "dev", BRIDGE], capture_output=True)
