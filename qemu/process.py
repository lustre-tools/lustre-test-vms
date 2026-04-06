"""QEMU process management and subprocess helpers."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .models import (
    BRIDGE,
    EXIT_ERROR,
    GATEWAY,
    KERNEL,
    QEMU,
    VMInfo,
)


def run(
    cmd: list[str] | str, **kwargs: Any
) -> subprocess.CompletedProcess[Any]:
    """Run a command, return CompletedProcess."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


def die(msg: str, code: int = EXIT_ERROR) -> None:
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

    boot_args = (
        f"console=ttyS0 reboot=k panic=1 crashkernel=512M "
        f"root=/dev/vda rw fc_ip={vm.ip} fc_gw={GATEWAY} "
        f"fc_name={vm.name}"
    )

    # Recreate TAP
    run(["ip", "link", "del", vm.tap], capture_output=True)
    run(
        ["ip", "tuntap", "add", "dev", vm.tap, "mode", "tap"],
        check=True,
    )
    run(["ip", "link", "set", vm.tap, "master", BRIDGE], check=True)
    run(["ip", "link", "set", vm.tap, "up"], check=True)

    kernel = Path(vm.kernel) if vm.kernel else KERNEL

    qemu_args = [
        QEMU,
        "-name",
        vm.name,
        "-machine",
        "microvm,accel=kvm,pit=off,pic=off,rtc=on",
        "-cpu",
        "host",
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
        "-serial",
        "chardev:serial0",
        "-chardev",
        f"file,id=serial0,path={vm.log_path}",
        "-device",
        "virtio-blk-device,drive=rootfs",
        "-drive",
        f"id=rootfs,file={vm.overlay_path},format=qcow2,if=none",
        "-netdev",
        f"tap,id=net0,ifname={vm.tap},script=no,downscript=no",
        "-device",
        f"virtio-net-device,netdev=net0,mac={vm.mac}",
        "-daemonize",
        "-pidfile",
        str(vm.pid_path),
    ]

    total_disks = vm.mdt_disks + vm.ost_disks
    for n in range(1, total_disks + 1):
        disk = vm.disk_path(n)
        if not disk.exists():
            die(f"disk{n} missing for '{vm.name}'")
        qemu_args += [
            "-device",
            f"virtio-blk-device,drive=disk{n}",
            "-drive",
            f"id=disk{n},file={disk},format=raw,if=none",
        ]

    with open(vm.log_path, "a") as log:
        r = subprocess.run(qemu_args, stderr=log)
    if r.returncode != 0:
        die(f"QEMU failed to start for '{vm.name}'")

    pid = int(vm.pid_path.read_text().strip())
    vm.update_pid(pid)


def kill_qemu(vm: VMInfo) -> None:
    """Kill the QEMU process and tear down the TAP device."""
    if vm.pid > 0:
        try:
            os.kill(vm.pid, signal.SIGTERM)
            time.sleep(0.2)
            os.kill(vm.pid, signal.SIGKILL)
        except OSError:
            pass
    run(["ip", "link", "del", vm.tap], capture_output=True)
