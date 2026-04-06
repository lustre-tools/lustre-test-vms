#!/usr/bin/env python3
"""QEMU/KVM microVM manager for Lustre test environments.

Manages ephemeral and persistent QEMU microVMs with KVM acceleration,
virtio-mmio devices, and kdump support. Designed for agent consumption
(structured JSON output, standardized exit codes, timeouts).
"""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ── constants ────────────────────────────────────────────

VM_DIR = Path("/opt/qemu-vms")
QEMU = "/opt/qemu/bin/qemu-system-x86_64"
DISK_SIZE_BYTES = 8 * 1024 * 1024 * 1024  # 8 GiB
BASE_IMAGE = Path("/opt/qemu-vms/images/rocky9-base.ext4")
KERNEL = VM_DIR / "kernel" / "vmlinux"
OVERLAYS = VM_DIR / "overlays"
SOCKETS = VM_DIR / "sockets"
BRIDGE = "fcbr0"
SUBNET = "192.168.100"
GATEWAY = f"{SUBNET}.1"
MARKER = "# qemu-vm"

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_FOUND = 2
EXIT_TIMEOUT = 3
EXIT_UNREACHABLE = 4


# ── VM data ──────────────────────────────────────────────


@dataclass
class VMInfo:
    name: str
    ip: str
    pid: int = 0
    tap: str = ""
    mac: str = ""
    vcpus: int = 2
    mem: int = 2048
    mdt_disks: int = 0
    ost_disks: int = 0
    image: str = ""  # base image path; empty = default (rocky9)
    kernel: str = ""  # kernel path; empty = default (vmlinux)

    @property
    def info_path(self) -> Path:
        return SOCKETS / f"{self.name}.info"

    @property
    def pid_path(self) -> Path:
        return SOCKETS / f"{self.name}.pid"

    @property
    def log_path(self) -> Path:
        return SOCKETS / f"{self.name}.log"

    @property
    def overlay_path(self) -> Path:
        return OVERLAYS / f"{self.name}.qcow2"

    def disk_path(self, n: int) -> Path:
        return OVERLAYS / f"{self.name}-disk{n}.img"

    def save(self):
        self.info_path.write_text(
            f"NAME={self.name}\n"
            f"IP={self.ip}\n"
            f"PID={self.pid}\n"
            f"TAP={self.tap}\n"
            f"MAC={self.mac}\n"
            f"VCPUS={self.vcpus}\n"
            f"MEM={self.mem}\n"
            f"MDT_DISKS={self.mdt_disks}\n"
            f"OST_DISKS={self.ost_disks}\n"
            f"IMAGE={self.image}\n"
            f"KERNEL={self.kernel}\n"
        )

    def update_pid(self, pid: int):
        self.pid = pid
        if self.info_path.exists():
            text = self.info_path.read_text()
            text = re.sub(r"^PID=.*$", f"PID={pid}", text, flags=re.MULTILINE)
            self.info_path.write_text(text)

    @staticmethod
    def load(name: str) -> "VMInfo":
        path = SOCKETS / f"{name}.info"
        if not path.exists():
            raise VMNotFound(name)
        vals = {}
        for line in path.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                vals[k] = v
        return VMInfo(
            name=vals.get("NAME", name),
            ip=vals.get("IP", ""),
            pid=int(vals.get("PID", 0)),
            tap=vals.get("TAP", ""),
            mac=vals.get("MAC", ""),
            vcpus=int(vals.get("VCPUS", 2)),
            mem=int(vals.get("MEM", 2048)),
            mdt_disks=int(vals.get("MDT_DISKS", 0)),
            ost_disks=int(vals.get("OST_DISKS", 0)),
            image=vals.get("IMAGE", ""),
            kernel=vals.get("KERNEL", ""),
        )

    @staticmethod
    def all_names() -> list:
        names = []
        for f in sorted(SOCKETS.glob("*.info")):
            names.append(f.stem)
        return names


class VMNotFound(Exception):
    def __init__(self, name):
        self.name = name
        super().__init__(f"VM '{name}' not found")


# ── helpers ──────────────────────────────────────────────


def ip_for_name(name: str) -> str:
    h = hashlib.md5(name.encode()).hexdigest()[:4]
    octet = (int(h, 16) % 244) + 10
    return f"{SUBNET}.{octet}"


def tap_for_name(name: str) -> str:
    suffix = name
    if len(suffix) > 11:
        suffix = hashlib.md5(name.encode()).hexdigest()[:11]
    return f"tap-{suffix}"


def mac_for_name(name: str) -> str:
    h = hashlib.md5(name.encode()).hexdigest()
    return f"AA:FC:00:{h[0:2]}:{h[2:4]}:{h[4:6]}"


def is_running(vm: VMInfo) -> bool:
    if vm.pid <= 0:
        return False
    try:
        os.kill(vm.pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def run(cmd, **kwargs):
    """Run a command, return CompletedProcess."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


def run_ssh(
    ip: str, command: str, timeout: int = 120
) -> subprocess.CompletedProcess:
    """Run a command on a VM via SSH with timeout."""
    ssh_cmd = [
        "sshpass",
        "-p",
        "initial0",
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=1",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "LogLevel=ERROR",
        f"root@{ip}",
        command,
    ]
    return run(ssh_cmd, timeout=timeout)


def reload_dns():
    """SIGHUP dnsmasq to re-read /etc/hosts."""
    pid_path = Path("/run/dnsmasq.pid")
    pid = None
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
        except (ValueError, OSError):
            pass
    if pid is None:
        r = run(["pgrep", "-x", "dnsmasq"])
        if r.returncode == 0 and r.stdout.strip():
            pid = int(r.stdout.strip().splitlines()[0])
    if pid:
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass


def check_ip_collision(name: str, ip: str):
    """Check that no other VM uses this IP."""
    for other_name in VMInfo.all_names():
        if other_name == name:
            continue
        try:
            other = VMInfo.load(other_name)
            if other.ip == ip:
                die(f"IP {ip} already used by VM '{other_name}'")
        except VMNotFound:
            pass


def die(msg: str, code: int = EXIT_ERROR):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ── SSH name / key management ────────────────────────────


def _real_user_ssh_dir() -> tuple:
    real_user = os.environ.get("SUDO_USER", "root")
    if real_user == "root":
        ssh_dir = Path("/root/.ssh")
    else:
        ssh_dir = Path(f"~{real_user}").expanduser() / ".ssh"
    return real_user, ssh_dir


def register_ssh_name(name: str, ip: str):
    """Add /etc/hosts and ~/.ssh/config entries for a VM."""
    hosts = Path("/etc/hosts")
    marker_line = f"{MARKER}:{name}"

    # /etc/hosts
    hosts_text = hosts.read_text() if hosts.exists() else ""
    if marker_line not in hosts_text:
        with open(hosts, "a") as f:
            f.write(f"{ip}\t{name} {marker_line}\n")
        reload_dns()

    # ~/.ssh/config
    real_user, ssh_dir = _real_user_ssh_dir()
    ssh_dir.mkdir(parents=True, exist_ok=True)
    ssh_cfg = ssh_dir / "config"
    cfg_text = ssh_cfg.read_text() if ssh_cfg.exists() else ""

    host_line = f"Host {name} {marker_line}"
    if host_line not in cfg_text:
        block = (
            f"\n{host_line}\n"
            f"\tHostName {ip}\n"
            f"\tUser root\n"
            f"\tStrictHostKeyChecking no\n"
            f"\tUserKnownHostsFile /dev/null\n"
            f"\tLogLevel ERROR\n"
            f"\tServerAliveInterval 1\n"
            f"\tServerAliveCountMax 2\n"
            f"\tConnectTimeout 5\n"
        )
        with open(ssh_cfg, "a") as f:
            f.write(block)
        ssh_cfg.chmod(0o600)
        import pwd

        try:
            pw = pwd.getpwnam(real_user)
            os.chown(ssh_cfg, pw.pw_uid, pw.pw_gid)
        except KeyError:
            pass


def unregister_ssh_name(name: str):
    """Remove /etc/hosts and ~/.ssh/config entries for a VM."""
    marker = f"{MARKER}:{name}"

    # /etc/hosts
    hosts = Path("/etc/hosts")
    if hosts.exists():
        lines = [
            line
            for line in hosts.read_text().splitlines()
            if marker not in line
        ]
        hosts.write_text("\n".join(lines) + "\n")
        reload_dns()

    # ~/.ssh/config — remove block
    _, ssh_dir = _real_user_ssh_dir()
    ssh_cfg = ssh_dir / "config"
    if not ssh_cfg.exists():
        return
    lines = ssh_cfg.read_text().splitlines()
    out = []
    skip = False
    for line in lines:
        if f"Host {name} {marker}" in line:
            skip = True
            # Also remove blank line before the block
            if out and out[-1] == "":
                out.pop()
            continue
        if skip:
            if line.startswith("\t") or line == "":
                continue
            skip = False
        out.append(line)
    ssh_cfg.write_text("\n".join(out) + "\n")


def deploy_ssh_key(ip: str):
    """Copy the invoking user's SSH public key to the VM."""
    _, ssh_dir = _real_user_ssh_dir()
    pubkey = None
    for f in sorted(ssh_dir.glob("id_*.pub")):
        pubkey = f
        break
    if not pubkey:
        return
    key_data = pubkey.read_text().strip()
    run_ssh(
        ip,
        f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        f"echo '{key_data}' >> ~/.ssh/authorized_keys && "
        f"chmod 600 ~/.ssh/authorized_keys",
        timeout=10,
    )


def wait_for_ssh(ip: str, max_wait: int = 30) -> bool:
    """Wait for SSH to become available on a VM."""
    for _ in range(max_wait):
        try:
            r = run_ssh(ip, "true", timeout=5)
            if r.returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)
    print(f"warning: SSH not ready after {max_wait}s", file=sys.stderr)
    return False


# ── QEMU launch / kill ───────────────────────────────────


def launch_qemu(vm: VMInfo):
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
    run(["ip", "tuntap", "add", "dev", vm.tap, "mode", "tap"], check=True)
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


def kill_qemu(vm: VMInfo):
    """Kill the QEMU process and tear down the TAP device."""
    if vm.pid > 0:
        try:
            os.kill(vm.pid, signal.SIGTERM)
            time.sleep(0.2)
            os.kill(vm.pid, signal.SIGKILL)
        except OSError:
            pass
    run(["ip", "link", "del", vm.tap], capture_output=True)


# ── commands: lifecycle ──────────────────────────────────


def cmd_create(args):
    name = args.name
    if not name:
        name = f"qemu-{int(time.time()) % 100000000}"

    if (SOCKETS / f"{name}.info").exists():
        die(f"VM '{name}' already exists")

    ip = args.ip or ip_for_name(name)
    check_ip_collision(name, ip)

    tap = tap_for_name(name)
    mac = mac_for_name(name)

    image = getattr(args, "image", "") or args.rootfs or str(BASE_IMAGE)
    kernel = getattr(args, "kernel", "") or ""

    vm = VMInfo(
        name=name,
        ip=ip,
        tap=tap,
        mac=mac,
        vcpus=args.vcpus,
        mem=args.mem,
        mdt_disks=args.mdt_disks,
        ost_disks=args.ost_disks,
        image=image if image != str(BASE_IMAGE) else "",
        kernel=kernel,
    )

    # Create overlay
    rootfs = image
    run(
        [
            "qemu-img",
            "create",
            "-f",
            "qcow2",
            "-b",
            rootfs,
            "-F",
            "raw",
            str(vm.overlay_path),
        ],
        capture_output=True,
        check=True,
    )

    # Create backing disks
    total = vm.mdt_disks + vm.ost_disks
    for n in range(1, total + 1):
        run(
            ["truncate", "-s", str(DISK_SIZE_BYTES), str(vm.disk_path(n))],
            check=True,
        )

    vm.save()
    launch_qemu(vm)
    wait_for_ssh(vm.ip, 30)
    register_ssh_name(vm.name, vm.ip)
    deploy_ssh_key(vm.ip)

    print(
        f"name={vm.name} ip={vm.ip} pid={vm.pid} "
        f"mdt_disks={vm.mdt_disks} ost_disks={vm.ost_disks}"
    )


def cmd_start(args):
    for name in args.names:
        vm = VMInfo.load(name)
        launch_qemu(vm)
        wait_for_ssh(vm.ip, 30)
        register_ssh_name(vm.name, vm.ip)
        print(f"started {name}")


def cmd_start_all(args):
    started = 0
    for name in VMInfo.all_names():
        vm = VMInfo.load(name)
        if not is_running(vm):
            launch_qemu(vm)
            wait_for_ssh(vm.ip, 30)
            register_ssh_name(vm.name, vm.ip)
            started += 1
            print(f"started {name}")
    print(f"started {started} VM(s)")


def cmd_stop(args):
    for name in args.names:
        vm = VMInfo.load(name)
        kill_qemu(vm)
        print(f"stopped {name}")


def cmd_stop_all(args):
    stopped = 0
    for name in VMInfo.all_names():
        vm = VMInfo.load(name)
        if is_running(vm):
            kill_qemu(vm)
            stopped += 1
            print(f"stopped {name}")
    print(f"stopped {stopped} VM(s)")


def cmd_restart(args):
    for name in args.names:
        vm = VMInfo.load(name)
        kill_qemu(vm)
        launch_qemu(vm)
        wait_for_ssh(vm.ip, 30)
        register_ssh_name(vm.name, vm.ip)
        print(f"restarted {name}")


def cmd_destroy(args):
    for name in args.names:
        try:
            vm = VMInfo.load(name)
            kill_qemu(vm)
        except VMNotFound:
            pass

        # Clean up files even if .info is missing
        overlay = OVERLAYS / f"{name}.qcow2"
        for f in [overlay] + list(OVERLAYS.glob(f"{name}-disk*.img")):
            f.unlink(missing_ok=True)
        for ext in ("sock", "pid", "info", "log"):
            (SOCKETS / f"{name}.{ext}").unlink(missing_ok=True)

        unregister_ssh_name(name)
        print(f"destroyed {name}")


def cmd_ensure(args):
    name = args.name
    info_path = SOCKETS / f"{name}.info"

    if info_path.exists():
        vm = VMInfo.load(name)
        if is_running(vm):
            if args.json:
                print(
                    json.dumps(
                        {
                            "action": "none",
                            "name": name,
                            "status": "already running",
                        }
                    )
                )
            else:
                print(f"{name}: already running")
            return
        launch_qemu(vm)
        wait_for_ssh(vm.ip, 30)
        register_ssh_name(vm.name, vm.ip)
        if args.json:
            print(
                json.dumps(
                    {"action": "started", "name": name, "status": "running"}
                )
            )
        else:
            print(f"{name}: started")
        return

    # Doesn't exist — create it
    # Build a fake args namespace for cmd_create
    create_args = argparse.Namespace(
        name=name,
        vcpus=args.vcpus,
        mem=args.mem,
        ip=None,
        rootfs=None,
        image=getattr(args, "image", ""),
        kernel=getattr(args, "kernel", ""),
        mdt_disks=args.mdt_disks,
        ost_disks=args.ost_disks,
    )
    cmd_create(create_args)
    if args.json:
        print(
            json.dumps({"action": "created", "name": name, "status": "running"})
        )


# ── commands: execution ──────────────────────────────────


def cmd_exec(args):
    name = args.name
    try:
        vm = VMInfo.load(name)
    except VMNotFound:
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "exit_code": EXIT_NOT_FOUND,
                        "error": "VM not found",
                    }
                )
            )
        else:
            print(f"error: VM '{name}' not found", file=sys.stderr)
        sys.exit(EXIT_NOT_FOUND)

    if not is_running(vm):
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "exit_code": EXIT_UNREACHABLE,
                        "error": "VM not running",
                    }
                )
            )
        else:
            print(f"error: VM '{name}' not running", file=sys.stderr)
        sys.exit(EXIT_UNREACHABLE)

    command = " ".join(args.command)
    if not command:
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "exit_code": EXIT_ERROR,
                        "error": "no command given",
                    }
                )
            )
        else:
            print("error: no command given", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    try:
        r = run_ssh(vm.ip, command, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "exit_code": EXIT_TIMEOUT,
                        "error": f"timeout after {args.timeout}s",
                    }
                )
            )
        else:
            print(f"error: timeout after {args.timeout}s", file=sys.stderr)
        sys.exit(EXIT_TIMEOUT)

    output = r.stdout or ""
    if r.stderr:
        output += r.stderr

    # ssh returns 255 on connection failure
    if r.returncode == 255:
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "exit_code": EXIT_UNREACHABLE,
                        "error": "unreachable",
                        "output": output,
                    }
                )
            )
        else:
            print("error: unreachable", file=sys.stderr)
            if output:
                print(output)
        sys.exit(EXIT_UNREACHABLE)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": r.returncode == 0,
                    "exit_code": r.returncode,
                    "output": output,
                }
            )
        )
    else:
        if output:
            print(output, end="")
    sys.exit(r.returncode)


def cmd_ssh(args):
    name = args.name
    vm = VMInfo.load(name)
    cmd = args.command
    ssh_args = [
        "sshpass",
        "-p",
        "initial0",
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "LogLevel=ERROR",
        f"root@{vm.ip}",
    ] + cmd
    os.execvp("sshpass", ssh_args)


def cmd_cp_to(args):
    vm = VMInfo.load(args.name)
    r = run(
        [
            "sshpass",
            "-p",
            "initial0",
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-r",
            args.src,
            f"root@{vm.ip}:{args.dest}",
        ],
        capture_output=False,
    )
    sys.exit(r.returncode)


def cmd_cp_from(args):
    vm = VMInfo.load(args.name)
    r = run(
        [
            "sshpass",
            "-p",
            "initial0",
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-r",
            f"root@{vm.ip}:{args.src}",
            args.dest,
        ],
        capture_output=False,
    )
    sys.exit(r.returncode)


# ── commands: info + observability ───────────────────────


def cmd_list(args):
    total_vcpus = 0
    total_mem = 0
    running_count = 0
    stopped_count = 0
    entries = []

    for name in VMInfo.all_names():
        vm = VMInfo.load(name)
        status = "running" if is_running(vm) else "stopped"

        if status == "running":
            running_count += 1
            total_vcpus += vm.vcpus
            total_mem += vm.mem
        else:
            stopped_count += 1

        disk_mb = "-"
        if vm.overlay_path.exists():
            disk_mb = f"{vm.overlay_path.stat().st_size // 1048576}M"

        entry = {
            "name": vm.name,
            "ip": vm.ip,
            "status": status,
            "pid": vm.pid,
            "vcpus": vm.vcpus,
            "mem": vm.mem,
            "mdt_disks": vm.mdt_disks,
            "ost_disks": vm.ost_disks,
            "disk": disk_mb,
        }
        entries.append(entry)

    host_cpus = os.cpu_count() or 1
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                host_mem_mb = int(line.split()[1]) // 1024
                break
        else:
            host_mem_mb = 0

    if args.json:
        print(
            json.dumps(
                {
                    "vms": entries,
                    "totals": {
                        "running": running_count,
                        "stopped": stopped_count,
                        "vcpus_used": total_vcpus,
                        "vcpus_available": host_cpus,
                        "mem_used_mb": total_mem,
                        "mem_available_mb": host_mem_mb,
                    },
                }
            )
        )
    else:
        if not entries:
            print("(no VMs)")
            return
        for e in entries:
            disks = ""
            if e["mdt_disks"] + e["ost_disks"] > 0:
                disks = f" mdt={e['mdt_disks']} ost={e['ost_disks']}"
            print(
                f"{e['name']:<20} {e['ip']:<18} {e['status']:<8} "
                f"pid={e['pid']:<8} vcpus={e['vcpus']} "
                f"mem={e['mem']:<6} disk={e['disk']:<8}{disks}"
            )
        print("---")
        print(
            f"{running_count} running, {stopped_count} stopped | "
            f"vcpus: {total_vcpus}/{host_cpus} | "
            f"mem: {total_mem}M/{host_mem_mb}M"
        )


def cmd_status(args):
    vm = VMInfo.load(args.name)
    qemu_status = "running" if is_running(vm) else "dead"

    ssh_status = "unreachable"
    if qemu_status == "running":
        try:
            r = run_ssh(vm.ip, "true", timeout=5)
            if r.returncode == 0:
                ssh_status = "ok"
        except subprocess.TimeoutExpired:
            pass

    lustre_status = "not loaded"
    mount_status = "not mounted"
    if ssh_status == "ok":
        try:
            r = run_ssh(vm.ip, "lsmod 2>/dev/null | grep -c lustre", timeout=5)
            if r.returncode == 0 and r.stdout.strip() not in ("", "0"):
                lustre_status = "loaded"
        except subprocess.TimeoutExpired:
            pass
        try:
            r = run_ssh(vm.ip, "mount 2>/dev/null | grep -c lustre", timeout=5)
            if r.returncode == 0 and r.stdout.strip() not in ("", "0"):
                mount_status = "mounted"
        except subprocess.TimeoutExpired:
            pass

    if args.json:
        print(
            json.dumps(
                {
                    "name": vm.name,
                    "ip": vm.ip,
                    "qemu": qemu_status,
                    "pid": vm.pid,
                    "ssh": ssh_status,
                    "lustre": lustre_status,
                    "mount": mount_status,
                    "vcpus": vm.vcpus,
                    "mem": vm.mem,
                    "mdt_disks": vm.mdt_disks,
                    "ost_disks": vm.ost_disks,
                }
            )
        )
    else:
        print(f"{'name:':<12} {vm.name}")
        print(f"{'ip:':<12} {vm.ip}")
        print(f"{'qemu:':<12} {qemu_status} (pid {vm.pid})")
        print(f"{'ssh:':<12} {ssh_status}")
        print(f"{'lustre:':<12} {lustre_status}")
        print(f"{'mount:':<12} {mount_status}")
        print(
            f"{'resources:':<12} vcpus={vm.vcpus} mem={vm.mem} "
            f"mdt={vm.mdt_disks} ost={vm.ost_disks}"
        )


def cmd_log(args):
    vm = VMInfo.load(args.name)
    if not vm.log_path.exists():
        die(f"no log for VM '{args.name}'")
    lines = vm.log_path.read_text().splitlines()
    for line in lines[-args.lines :]:
        print(line)


def cmd_dmesg(args):
    vm = VMInfo.load(args.name)
    if not is_running(vm):
        die(f"VM '{args.name}' not running", EXIT_UNREACHABLE)
    try:
        r = run_ssh(vm.ip, f"dmesg | tail -n {args.tail}", timeout=10)
        if r.stdout:
            print(r.stdout, end="")
        if r.returncode != 0 and r.stderr:
            print(r.stderr, end="", file=sys.stderr)
        sys.exit(r.returncode)
    except subprocess.TimeoutExpired:
        die(f"timeout reading dmesg from '{args.name}'", EXIT_TIMEOUT)


def cmd_lustre_log(args):
    vm = VMInfo.load(args.name)
    if not is_running(vm):
        die(f"VM '{args.name}' not running", EXIT_UNREACHABLE)
    try:
        r = run_ssh(
            vm.ip,
            'lctl dk 2>/dev/null || echo "lctl not available"',
            timeout=10,
        )
        if r.stdout:
            print(r.stdout, end="")
        sys.exit(r.returncode)
    except subprocess.TimeoutExpired:
        die(f"timeout reading lustre log from '{args.name}'", EXIT_TIMEOUT)


# ── commands: crash-collect ──────────────────────────────


def cmd_crash_collect(args):
    """Collect vmcore from a crashed VM and run lustre triage.

    Two modes:
    - Default: VM already crashed and rebooted. Find latest
      vmcore, copy it out, run triage.
    - --trigger: Send sysrq-trigger to crash the VM, wait
      for reboot, then collect.
    """
    vm = VMInfo.load(args.name)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if args.trigger:
        if not is_running(vm):
            die(f"VM '{args.name}' not running, can't trigger crash")
        print(f"triggering crash on {args.name}...")
        try:
            run_ssh(vm.ip, "echo c > /proc/sysrq-trigger", timeout=5)
        except subprocess.TimeoutExpired:
            pass  # expected -- VM crashes, SSH dies

        # Wait for VM to reboot via kdump
        print("waiting for kdump + reboot...", end="", flush=True)
        time.sleep(5)
        for i in range(args.wait):
            try:
                r = run_ssh(vm.ip, "true", timeout=3)
                if r.returncode == 0:
                    print(f" up after {i + 5}s")
                    break
            except subprocess.TimeoutExpired:
                pass
            print(".", end="", flush=True)
            time.sleep(1)
        else:
            die(
                f"\nVM '{args.name}' did not come back after {args.wait + 5}s",
                EXIT_TIMEOUT,
            )

    # VM should be up -- find the latest vmcore
    if not is_running(vm):
        die(f"VM '{args.name}' not running")

    print("finding vmcore...")
    r = run_ssh(
        vm.ip, "ls -td /var/crash/*/vmcore 2>/dev/null | head -1", timeout=10
    )
    vmcore_path = r.stdout.strip()
    if not vmcore_path:
        die("no vmcore found in /var/crash/")

    r = run_ssh(vm.ip, f"ls -lh {vmcore_path}", timeout=5)
    print(f"found: {r.stdout.strip()}")

    # Copy vmcore out
    ts = time.strftime("%Y%m%d-%H%M%S")
    local_dir = outdir / f"crash-{args.name}-{ts}"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_vmcore = local_dir / "vmcore"

    print(f"copying vmcore to {local_vmcore}...")
    r = run(
        [
            "sshpass",
            "-p",
            "initial0",
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            f"root@{vm.ip}:{vmcore_path}",
            str(local_vmcore),
        ],
        capture_output=False,
        timeout=300,
    )
    if r.returncode != 0:
        die("failed to copy vmcore")

    print(f"vmcore: {local_vmcore}")

    # Run lustre triage if mod-dir provided
    if args.mod_dir:
        vmlinux = str(KERNEL)
        print("running lustre triage...")
        triage_script = Path(
            "/home/admin/llm_code_and_review_tools/"
            "lustre-drgn-tools/lustre_triage.py"
        )
        if not triage_script.exists():
            print(f"triage script not found: {triage_script}")
            print(f"vmcore dir: {local_dir}")
            return
        triage_cmd = [
            "python3",
            str(triage_script),
            "--vmcore",
            str(local_vmcore),
            "--vmlinux",
            vmlinux,
            "--mod-dir",
            args.mod_dir,
            "--pretty",
        ]
        run(triage_cmd, capture_output=False, timeout=120)
        print(f"\nvmcore dir: {local_dir}")
    else:
        print(f"vmcore dir: {local_dir}")
        print("run triage:")
        print(
            f"  crash-tool recipes lustre "
            f"--vmcore {local_vmcore} "
            f"--vmlinux {KERNEL} "
            f"--mod-dir <build-tree>"
        )


# ── commands: snapshots ──────────────────────────────────


def cmd_snapshot(args):
    vm = VMInfo.load(args.name)
    tag = args.tag or f"snap-{time.strftime('%Y%m%d-%H%M%S')}"

    was_running = is_running(vm)
    if was_running:
        print(f"stopping {vm.name} for snapshot...")
        kill_qemu(vm)

    r = run(["qemu-img", "snapshot", "-c", tag, str(vm.overlay_path)])
    if r.returncode != 0:
        die(f"snapshot failed: {r.stderr}")

    print(f"snapshot '{tag}' created for {vm.name}")

    if was_running:
        print(f"restarting {vm.name}...")
        launch_qemu(vm)
        wait_for_ssh(vm.ip, 30)
        register_ssh_name(vm.name, vm.ip)
        print(f"started {vm.name}")


def cmd_restore(args):
    vm = VMInfo.load(args.name)

    if not args.tag:
        # List snapshots
        print(f"snapshots for {vm.name}:")
        r = run(
            ["qemu-img", "snapshot", "-l", "-U", str(vm.overlay_path)],
            capture_output=False,
        )
        return

    if is_running(vm):
        print(f"stopping {vm.name} before restore...")
        kill_qemu(vm)

    r = run(["qemu-img", "snapshot", "-a", args.tag, str(vm.overlay_path)])
    if r.returncode != 0:
        die(f"restore failed: {r.stderr}")
    print(f"restored {vm.name} to '{args.tag}'")


# ── commands: doctor ─────────────────────────────────────


def cmd_doctor(args):
    issues = 0

    # Stale PIDs
    for name in VMInfo.all_names():
        vm = VMInfo.load(name)
        if vm.pid > 0 and not is_running(vm):
            print(f"stale PID: {name} (pid {vm.pid} dead)")
            issues += 1
            if args.fix:
                vm.update_pid(0)
                print("  fixed: reset PID to 0")

    # Orphan overlays
    for overlay in sorted(OVERLAYS.glob("*.qcow2")):
        oname = overlay.stem
        if not (SOCKETS / f"{oname}.info").exists():
            size = overlay.stat().st_size // 1048576
            print(f"orphan overlay: {oname} ({size}M)")
            issues += 1
            if args.fix:
                overlay.unlink()
                for disk in OVERLAYS.glob(f"{oname}-disk*.img"):
                    disk.unlink()
                print("  fixed: removed")

    # Stale /etc/hosts entries
    hosts = Path("/etc/hosts")
    if hosts.exists():
        for line in hosts.read_text().splitlines():
            m = re.search(rf"{re.escape(MARKER)}:(\S+)$", line)
            if m:
                hname = m.group(1)
                if not (SOCKETS / f"{hname}.info").exists():
                    print(f"stale hosts entry: {hname}")
                    issues += 1
                    if args.fix:
                        lines = [
                            line
                            for line in hosts.read_text().splitlines()
                            if f"{MARKER}:{hname}" not in line
                        ]
                        hosts.write_text("\n".join(lines) + "\n")
                        reload_dns()
                        print("  fixed: removed from /etc/hosts")

    # Orphan TAP devices
    r = run(["ip", "-o", "link", "show", "type", "tun"])
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            m = re.search(r":\s*(tap-\S+?)[@:]", line)
            if not m:
                continue
            tap = m.group(1)
            found = False
            for name in VMInfo.all_names():
                vm = VMInfo.load(name)
                if vm.tap == tap and is_running(vm):
                    found = True
                    break
            if not found:
                print(f"orphan TAP: {tap}")
                issues += 1
                if args.fix:
                    run(["ip", "link", "del", tap])
                    print("  fixed: removed")

    # Stale SSH config entries
    _, ssh_dir = _real_user_ssh_dir()
    ssh_cfg = ssh_dir / "config"
    if ssh_cfg.exists():
        for line in ssh_cfg.read_text().splitlines():
            m = re.search(rf"{re.escape(MARKER)}:(\S+)$", line)
            if m:
                sname = m.group(1)
                if not (SOCKETS / f"{sname}.info").exists():
                    print(f"stale ssh config: {sname}")
                    issues += 1
                    if args.fix:
                        unregister_ssh_name(sname)
                        print("  fixed: removed ssh config")

    if issues == 0:
        print("no issues found")
    else:
        print("---")
        print(f"{issues} issue(s) found")
        if not args.fix:
            print("run with --fix to clean up")


# ── cluster ──────────────────────────────────────────────

DEPLOY_SCRIPT = VM_DIR / "deploy-lustre.sh"


@dataclass
class ClusterNode:
    name: str
    roles: list  # ["mgs"], ["mds"], ["oss"], ["mgs", "mds"], etc.
    mdt_disks: int = 0
    ost_disks: int = 0
    ip: str = ""

    @property
    def is_mgs(self):
        return "mgs" in self.roles

    @property
    def is_mds(self):
        return "mds" in self.roles

    @property
    def is_oss(self):
        return "oss" in self.roles

    @property
    def is_client(self):
        return "client" in self.roles

    @property
    def total_disks(self):
        """MGS gets 1 disk if standalone, MDS gets mdt_disks,
        OSS gets ost_disks."""
        n = self.mdt_disks + self.ost_disks
        # Standalone MGS needs 1 small disk
        if self.is_mgs and not self.is_mds:
            n += 1
        return n


@dataclass
class ClusterInfo:
    name: str
    nodes: list  # list of dicts (serialized ClusterNode)

    @property
    def path(self) -> Path:
        return SOCKETS / f"{self.name}.cluster"

    def save(self):
        data = {"name": self.name, "nodes": self.nodes}
        self.path.write_text(json.dumps(data, indent=2) + "\n")

    @staticmethod
    def load(name: str) -> "ClusterInfo":
        path = SOCKETS / f"{name}.cluster"
        if not path.exists():
            die(f"cluster '{name}' not found", EXIT_NOT_FOUND)
        data = json.loads(path.read_text())
        return ClusterInfo(name=data["name"], nodes=data["nodes"])

    @staticmethod
    def all_names() -> list:
        return [f.stem for f in sorted(SOCKETS.glob("*.cluster"))]

    def get_nodes(self) -> list:
        return [ClusterNode(**n) for n in self.nodes]

    def mgs_node(self) -> ClusterNode:
        for n in self.get_nodes():
            if n.is_mgs:
                return n
        die("cluster has no MGS node")

    def mds_nodes(self) -> list:
        return [n for n in self.get_nodes() if n.is_mds]

    def oss_nodes(self) -> list:
        return [n for n in self.get_nodes() if n.is_oss]

    def client_nodes(self) -> list:
        return [n for n in self.get_nodes() if n.is_client]


def parse_node_spec(spec: str) -> ClusterNode:
    """Parse a node spec like 'mgs+mds:myvm:1' or 'oss:myoss:3'.

    Format: roles:vmname[:diskcount]
    - roles: mgs, mds, oss, client, joined with +
    - vmname: VM name to create
    - diskcount: number of disks (MDT for mds, OST for oss)
    """
    parts = spec.split(":")
    if len(parts) < 2:
        die(f"invalid node spec '{spec}': need roles:name[:disks]")

    roles_str = parts[0]
    name = parts[1]
    disk_count = int(parts[2]) if len(parts) > 2 else 0

    roles = roles_str.lower().split("+")
    for r in roles:
        if r not in ("mgs", "mds", "oss", "client"):
            die(f"unknown role '{r}' in spec '{spec}'")

    mdt_disks = 0
    ost_disks = 0
    if "mds" in roles:
        mdt_disks = max(disk_count, 1)
    if "oss" in roles:
        ost_disks = max(disk_count, 1)

    return ClusterNode(
        name=name,
        roles=roles,
        mdt_disks=mdt_disks,
        ost_disks=ost_disks,
    )


def generate_local_sh(cluster: ClusterInfo, build_path: str = "") -> str:
    """Generate cfg/local.sh content for a multi-node cluster."""
    mgs = cluster.mgs_node()
    mds_list = cluster.mds_nodes()
    oss_list = cluster.oss_nodes()
    client_list = cluster.client_nodes()

    # Build tree is rsynced to same absolute path on all VMs
    lustre_dir = f"{build_path}/lustre" if build_path else ""

    lines = [
        f"# Cluster: {cluster.name}",
        "# Generated by vm.py cluster deploy",
        "",
        "FSNAME=lustre",
        "NETTYPE=tcp",
        "",
    ]

    # Paths -- build tree rsynced to same location on all nodes
    if lustre_dir:
        lines.append(f"LUSTRE={lustre_dir}")
        lines.append(f"RLUSTRE={lustre_dir}")
        lines.append(f"RPWD={lustre_dir}/tests")
        lines.append("")

    # MGS
    lines.append(f"mgs_HOST={mgs.name}")
    lines.append(f"MGSNID={mgs.ip}@tcp")

    # If MGS is combined with MDS, they share the device.
    # If MGS is standalone, it gets its own device (first disk).
    combined = mgs.is_mds
    if not combined:
        # Standalone MGS: /dev/vdb
        lines.append("MGSDEV=/dev/vdb")
    lines.append("")

    # MDS nodes
    if mds_list:
        # All MDS facets default to mds_HOST
        lines.append(f"mds_HOST={mds_list[0].name}")
        total_mdts = sum(n.mdt_disks for n in mds_list)
        lines.append(f"MDSCOUNT={total_mdts}")

        mdt_idx = 1
        for mds_node in mds_list:
            # Disk letters: vdb, vdc, ...
            # If combined MGS+MDS, MDT starts at vdb
            # If this node is also standalone MGS, MDT starts at vdc
            # (MGS took vdb)
            disk_offset = 1  # vdb = disk offset 1
            if mds_node.is_mgs and not combined:
                # standalone MGS on same node took vdb
                disk_offset = 2

            for d in range(mds_node.mdt_disks):
                letter = chr(ord("a") + disk_offset + d)
                lines.append(f"MDSDEV{mdt_idx}=/dev/vd{letter}")
                if len(mds_list) > 1:
                    lines.append(f"mds{mdt_idx}_HOST={mds_node.name}")
                mdt_idx += 1
        lines.append("")

    # OSS nodes
    if oss_list:
        lines.append(f"ost_HOST={oss_list[0].name}")
        total_osts = sum(n.ost_disks for n in oss_list)
        lines.append(f"OSTCOUNT={total_osts}")

        ost_idx = 1
        for oss_node in oss_list:
            # OSS node disks start at vdb (OSS-only nodes)
            # unless this node also has other roles
            disk_offset = 1
            if oss_node.is_mgs and not oss_node.is_mds:
                disk_offset = 2  # MGS took vdb
            if oss_node.is_mds:
                disk_offset = 1 + oss_node.mdt_disks
                if oss_node.is_mgs and not combined:
                    disk_offset += 1

            for d in range(oss_node.ost_disks):
                letter = chr(ord("a") + disk_offset + d)
                lines.append(f"OSTDEV{ost_idx}=/dev/vd{letter}")
                if len(oss_list) > 1:
                    lines.append(f"ost{ost_idx}_HOST={oss_node.name}")
                ost_idx += 1
        lines.append("")

    # Clients
    if client_list:
        client_names = ",".join(n.name for n in client_list)
        lines.append(f"CLIENTS={client_names}")
    lines.append("")

    # Filesystem and sequencing
    lines.append("FSTYPE=ldiskfs")
    lines.append("OSTSEQWIDTH=${OSTSEQWIDTH:-0x20000}")
    lines.append("")

    # Use installed binaries, not libtool wrappers
    lines.append("LCTL=/usr/sbin/lctl")
    lines.append("LFS=/usr/bin/lfs")
    lines.append("")

    # Remote execution
    lines.append('PDSH="pdsh -S -Rssh -w"')
    lines.append("LOAD_MODULES_REMOTE=true")
    lines.append("MOUNT=/mnt/lustre")
    lines.append("")

    return "\n".join(lines) + "\n"


def cmd_cluster(args):
    """Dispatch cluster subcommands."""
    subcmd = args.cluster_cmd

    if subcmd == "create":
        cmd_cluster_create(args)
    elif subcmd == "deploy":
        cmd_cluster_deploy(args)
    elif subcmd == "destroy":
        cmd_cluster_destroy(args)
    elif subcmd == "list":
        cmd_cluster_list(args)
    elif subcmd == "status":
        cmd_cluster_status(args)
    elif subcmd == "ssh":
        cmd_cluster_ssh(args)
    elif subcmd == "exec":
        cmd_cluster_exec(args)
    else:
        die(f"unknown cluster command: {subcmd}")


def cmd_cluster_create(args):
    cluster_name = args.name
    if (SOCKETS / f"{cluster_name}.cluster").exists():
        die(f"cluster '{cluster_name}' already exists")

    node_specs = [parse_node_spec(s) for s in args.nodes]

    # Validate: exactly one MGS
    mgs_count = sum(1 for n in node_specs if n.is_mgs)
    if mgs_count == 0:
        die("cluster needs at least one node with mgs role")
    if mgs_count > 1:
        die("cluster can only have one MGS node")

    # Validate: at least one MDS
    if not any(n.is_mds for n in node_specs):
        die("cluster needs at least one node with mds role")

    vcpus = args.vcpus
    mem = args.mem

    # Create VMs
    print(f"=== Creating cluster '{cluster_name}' ===")
    for node in node_specs:
        # Calculate total disks for this node
        mdt = node.mdt_disks
        ost = node.ost_disks
        # Standalone MGS gets 1 disk
        mgs_disk = 1 if (node.is_mgs and not node.is_mds) else 0

        print(f"--- Creating {node.name} ({'+'.join(node.roles)})...")

        create_args = argparse.Namespace(
            name=node.name,
            vcpus=vcpus,
            mem=mem,
            ip=None,
            rootfs=None,
            mdt_disks=mdt + mgs_disk,
            ost_disks=ost,
        )
        cmd_create(create_args)

        # Store the IP
        vm = VMInfo.load(node.name)
        node.ip = vm.ip

    # Save cluster metadata
    cluster = ClusterInfo(
        name=cluster_name,
        nodes=[
            {
                "name": n.name,
                "roles": n.roles,
                "mdt_disks": n.mdt_disks,
                "ost_disks": n.ost_disks,
                "ip": n.ip,
            }
            for n in node_specs
        ],
    )
    cluster.save()

    print(f"\n=== Cluster '{cluster_name}' created ===")
    for n in node_specs:
        print(f"  {n.name:<20} {n.ip:<18} {'+'.join(n.roles)}")
    print(
        f"\nNext: vm.sh cluster deploy {cluster_name} "
        f"--build /path/to/lustre-release [--mount]"
    )


def cmd_cluster_deploy(args):
    cluster = ClusterInfo.load(args.name)
    nodes = cluster.get_nodes()
    build = str(Path(args.build).resolve())

    if not Path(build).is_dir():
        die(f"build directory '{build}' not found")

    print(f"=== Deploying to cluster '{cluster.name}' ===")
    print(f"    Build: {build}")

    ssh_opts = [
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
    ]

    # Rsync the full build tree to the same path on each VM.
    # Also deploy modules via depmod so modprobe works.
    for node in nodes:
        vm = VMInfo.load(node.name)
        ip = vm.ip
        print(f"\n--- Deploying to {node.name} ({'+'.join(node.roles)})...")

        # Ensure parent dir exists on remote
        parent = str(Path(build).parent)
        run(
            ["sshpass", "-p", "initial0", "ssh"]
            + ssh_opts
            + [f"root@{ip}", f"mkdir -p {parent}"],
            capture_output=True,
            timeout=10,
        )

        # Rsync the build tree
        print(f"  rsync {build} -> {node.name}:{build}")
        r = run(
            [
                "sshpass",
                "-p",
                "initial0",
                "rsync",
                "-a",
                "--delete",
                "-e",
                "ssh " + " ".join(ssh_opts),
                "--exclude=.git",
                "--exclude=*.o",
                "--exclude=*.cmd",
                "--exclude=*.mod",
                f"{build}/",
                f"root@{ip}:{build}/",
            ],
            capture_output=False,
            timeout=300,
        )
        if r.returncode != 0:
            die(f"rsync failed for {node.name}")

        # Find and install kernel modules + depmod
        kver_r = run(
            ["sshpass", "-p", "initial0", "ssh"]
            + ssh_opts
            + [f"root@{ip}", "uname -r"],
            capture_output=True,
            timeout=10,
        )
        kver = kver_r.stdout.strip()

        run(
            ["sshpass", "-p", "initial0", "ssh"]
            + ssh_opts
            + [
                f"root@{ip}",
                f"mkdir -p /lib/modules/{kver}/extra/lustre && "
                f"find {build} -name '*.ko' "
                f"-not -path '*/kconftest*' "
                f"-exec cp {{}} /lib/modules/{kver}/extra/lustre/ \\; && "
                f"depmod -a {kver}",
            ],
            capture_output=True,
            timeout=30,
        )

        # Install shared libraries + ldconfig
        run(
            ["sshpass", "-p", "initial0", "ssh"]
            + ssh_opts
            + [
                f"root@{ip}",
                f"cp -f {build}/lustre/utils/.libs/liblustreapi.so* "
                f"/usr/lib64/ 2>/dev/null; "
                f"cp -f {build}/lnet/utils/lnetconfig/.libs/"
                f"liblnetconfig.so* /usr/lib64/ 2>/dev/null; "
                f"ldconfig",
            ],
            capture_output=True,
            timeout=10,
        )

        # Install key binaries to /usr/sbin so they're in PATH
        run(
            ["sshpass", "-p", "initial0", "ssh"]
            + ssh_opts
            + [
                f"root@{ip}",
                f"for b in lctl mkfs.lustre mount.lustre "
                f"tunefs.lustre lnetctl; do "
                f"src={build}/lustre/utils/.libs/$b; "
                f"[ -f $src ] && cp -f $src /usr/sbin/; done; "
                f"src={build}/lnet/utils/.libs/lnetctl; "
                f"[ -f $src ] && cp -f $src /usr/sbin/; "
                f"for b in lfs lustre_rmmod; do "
                f"src={build}/lustre/utils/.libs/$b; "
                f"[ -f $src ] && cp -f $src /usr/bin/; done; "
                f"src={build}/lustre/utils/lustre_rmmod; "
                f"[ -f $src ] && cp -f $src /usr/sbin/; "
                f"cp -f {build}/lustre/utils/.libs/mount_osd_*.so "
                f"/usr/lib64/ 2>/dev/null; true",
            ],
            capture_output=True,
            timeout=10,
        )

        # Replace libtool wrapper scripts with real binaries
        # so the test framework doesn't trigger relinks
        run(
            ["sshpass", "-p", "initial0", "ssh"]
            + ssh_opts
            + [
                f"root@{ip}",
                f"for d in {build}/lustre/utils {build}/lnet/utils "
                f"{build}/lnet/utils/lnetconfig; do "
                f"[ -d $d/.libs ] || continue; "
                f"for f in $d/.libs/*; do "
                f"[ -f $f ] && [ ! -h $f ] && "
                f"b=$(basename $f) && "
                f"[ -f $d/$b ] && head -1 $d/$b 2>/dev/null | "
                f"grep -q libtool && "
                f"cp -f $f $d/$b; "
                f"done; done; true",
            ],
            capture_output=True,
            timeout=30,
        )

        # Remove ptlrpc_gss.ko -- if the build includes it but
        # the kernel lacks crypto support, insmod fails fatally.
        # Without the .ko, load_module falls through to modprobe
        # which handles the failure gracefully.
        run(
            ["sshpass", "-p", "initial0", "ssh"]
            + ssh_opts
            + [f"root@{ip}", f"rm -f {build}/lustre/ptlrpc/gss/ptlrpc_gss.ko"],
            capture_output=True,
            timeout=10,
        )

        print(f"  {node.name}: deployed")

    # Generate and push local.sh to all nodes
    local_sh = generate_local_sh(cluster, build_path=build)
    print("\n--- Distributing cluster config (local.sh)...")

    cfg_path = f"{build}/lustre/tests/cfg/local.sh"
    for node in nodes:
        vm = VMInfo.load(node.name)
        run(
            ["sshpass", "-p", "initial0", "ssh"]
            + ssh_opts
            + [
                f"root@{vm.ip}",
                f"cat > {cfg_path} << 'LOCALEOF'\n{local_sh}LOCALEOF",
            ],
            capture_output=True,
            timeout=10,
        )
        print(f"  {node.name}: local.sh deployed")

    print("\n--- Cluster config (local.sh):")
    print(local_sh)

    # Optionally mount
    if args.mount:
        print("=== Mounting Lustre filesystem ===")
        # Run llmount.sh from the MGS/MDS node
        mgs = cluster.mgs_node()
        mgs_vm = VMInfo.load(mgs.name)

        mount_cmd = f"cd {build}/lustre/tests && bash llmount.sh"
        if args.server_only:
            mount_cmd += " --server-only"

        print(f"Running llmount.sh from {mgs.name}...")
        r = run(
            [
                "sshpass",
                "-p",
                "initial0",
                "ssh",
            ]
            + ssh_opts
            + [
                f"root@{mgs_vm.ip}",
                mount_cmd,
            ],
            capture_output=False,
            timeout=300,
        )
        if r.returncode != 0:
            die("llmount.sh failed")
        print("=== Lustre mounted ===")

    print(f"\n=== Cluster '{cluster.name}' deployed ===")


def cmd_cluster_destroy(args):
    cluster = ClusterInfo.load(args.name)
    nodes = cluster.get_nodes()

    print(f"=== Destroying cluster '{cluster.name}' ===")
    for node in nodes:
        try:
            vm = VMInfo.load(node.name)
            kill_qemu(vm)
        except VMNotFound:
            pass

        # Clean up VM files
        overlay = OVERLAYS / f"{node.name}.qcow2"
        for f in [overlay] + list(OVERLAYS.glob(f"{node.name}-disk*.img")):
            f.unlink(missing_ok=True)
        for ext in ("sock", "pid", "info", "log"):
            (SOCKETS / f"{node.name}.{ext}").unlink(missing_ok=True)
        unregister_ssh_name(node.name)
        print(f"  destroyed {node.name}")

    cluster.path.unlink(missing_ok=True)
    print(f"=== Cluster '{cluster.name}' destroyed ===")


def cmd_cluster_list(args):
    names = ClusterInfo.all_names()
    if not names:
        print("(no clusters)")
        return
    for cname in names:
        cluster = ClusterInfo.load(cname)
        nodes = cluster.get_nodes()
        node_summary = []
        for n in nodes:
            roles = "+".join(n.roles)
            running = "up" if is_running(VMInfo.load(n.name)) else "down"
            node_summary.append(f"{n.name}({roles},{running})")
        print(f"{cname}: {' '.join(node_summary)}")


def cmd_cluster_status(args):
    cluster = ClusterInfo.load(args.name)
    nodes = cluster.get_nodes()

    print(f"cluster: {cluster.name}")
    print(f"nodes:   {len(nodes)}")
    print()

    for node in nodes:
        try:
            vm = VMInfo.load(node.name)
            running = is_running(vm)
        except VMNotFound:
            running = False

        status = "running" if running else "stopped"
        roles = "+".join(node.roles)
        disks = ""
        if node.mdt_disks:
            disks += f" mdt={node.mdt_disks}"
        if node.ost_disks:
            disks += f" ost={node.ost_disks}"
        if node.is_mgs and not node.is_mds:
            disks += " mgs=1"

        print(f"  {node.name:<20} {node.ip:<18} {status:<8} {roles:<12}{disks}")


def cmd_cluster_ssh(args):
    cluster = ClusterInfo.load(args.name)
    target = args.target
    nodes = cluster.get_nodes()

    # Find node by name or role
    found = None
    for n in nodes:
        if n.name == target or target in n.roles:
            found = n
            break
    if not found:
        die(f"no node matching '{target}' in cluster '{args.name}'")

    vm = VMInfo.load(found.name)
    ssh_args = [
        "sshpass",
        "-p",
        "initial0",
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "LogLevel=ERROR",
        f"root@{vm.ip}",
    ] + args.command
    os.execvp("sshpass", ssh_args)


def cmd_cluster_exec(args):
    cluster = ClusterInfo.load(args.name)
    target = args.target
    nodes = cluster.get_nodes()

    found = None
    for n in nodes:
        if n.name == target or target in n.roles:
            found = n
            break
    if not found:
        die(f"no node matching '{target}' in cluster '{args.name}'")

    command = " ".join(args.command)
    vm = VMInfo.load(found.name)
    try:
        r = run_ssh(vm.ip, command, timeout=args.timeout)
        if r.stdout:
            print(r.stdout, end="")
        sys.exit(r.returncode)
    except subprocess.TimeoutExpired:
        die(f"timeout after {args.timeout}s", EXIT_TIMEOUT)


# ── CLI ──────────────────────────────────────────────────


def build_parser():
    p = argparse.ArgumentParser(
        prog="vm.sh",
        description="QEMU microVM manager for Lustre testing",
    )
    sub = p.add_subparsers(dest="subcmd", metavar="COMMAND")

    # create
    c = sub.add_parser("create", help="Create and start a new VM")
    c.add_argument("name", nargs="?", default="")
    c.add_argument("--name", dest="name_flag", default="")
    c.add_argument("--vcpus", type=int, default=2)
    c.add_argument("--mem", type=int, default=2048)
    c.add_argument("--ip", default="")
    c.add_argument("--rootfs", default="")
    c.add_argument(
        "--image",
        default="",
        help="Base image path (default: rocky9-base.ext4)",
    )
    c.add_argument(
        "--kernel", default="", help="Kernel path (default: vmlinux)"
    )
    c.add_argument("--mdt-disks", type=int, default=0)
    c.add_argument("--ost-disks", type=int, default=0)

    # ensure
    c = sub.add_parser("ensure", help="Idempotent: create/start as needed")
    c.add_argument("name")
    c.add_argument("--vcpus", type=int, default=2)
    c.add_argument("--mem", type=int, default=2048)
    c.add_argument("--mdt-disks", type=int, default=0)
    c.add_argument("--ost-disks", type=int, default=0)
    c.add_argument(
        "--image",
        default="",
        help="Base image path (default: rocky9-base.ext4)",
    )
    c.add_argument(
        "--kernel", default="", help="Kernel path (default: vmlinux)"
    )
    c.add_argument("--json", action="store_true")

    # start
    c = sub.add_parser("start", help="Start stopped VM(s)")
    c.add_argument("names", nargs="+", metavar="NAME")

    # start-all
    sub.add_parser("start-all", help="Start all stopped VMs")

    # stop
    c = sub.add_parser("stop", help="Stop VM(s), keep disks")
    c.add_argument("names", nargs="+", metavar="NAME")

    # stop-all
    sub.add_parser("stop-all", help="Stop all running VMs")

    # restart
    c = sub.add_parser("restart", help="Stop + start VM(s)")
    c.add_argument("names", nargs="+", metavar="NAME")

    # destroy
    c = sub.add_parser("destroy", help="Stop + delete everything")
    c.add_argument("names", nargs="+", metavar="NAME")
    sub.add_parser("rm", help=argparse.SUPPRESS)  # alias

    # exec
    c = sub.add_parser("exec", help="Run command with timeout + exit codes")
    c.add_argument("--timeout", type=int, default=120)
    c.add_argument("--json", action="store_true")
    c.add_argument("name")
    c.add_argument("command", nargs=argparse.REMAINDER)

    # ssh
    c = sub.add_parser("ssh", help="Interactive SSH (no timeout)")
    c.add_argument("name")
    c.add_argument("command", nargs=argparse.REMAINDER)

    # cp-to
    c = sub.add_parser("cp-to", help="Copy file(s) to VM")
    c.add_argument("name")
    c.add_argument("src")
    c.add_argument("dest")
    sub.add_parser("push", help=argparse.SUPPRESS)  # alias

    # cp-from
    c = sub.add_parser("cp-from", help="Copy file(s) from VM")
    c.add_argument("name")
    c.add_argument("src")
    c.add_argument("dest")
    sub.add_parser("pull", help=argparse.SUPPRESS)  # alias

    # list
    c = sub.add_parser("list", help="Show all VMs + resource totals")
    c.add_argument("--json", action="store_true")
    for alias in ("ls", "ps"):
        sub.add_parser(alias, help=argparse.SUPPRESS)

    # status
    c = sub.add_parser("status", help="Health check")
    c.add_argument("--json", action="store_true")
    c.add_argument("name")

    # log
    c = sub.add_parser("log", help="Tail serial console log")
    c.add_argument("name")
    c.add_argument("lines", type=int, nargs="?", default=50)

    # dmesg
    c = sub.add_parser("dmesg", help="Kernel log from VM")
    c.add_argument("--tail", type=int, default=200)
    c.add_argument("name")

    # lustre-log
    c = sub.add_parser("lustre-log", help="lctl dk from VM")
    c.add_argument("name")

    # snapshot
    c = sub.add_parser("snapshot", help="Create qcow2 snapshot")
    c.add_argument("name")
    c.add_argument("tag", nargs="?", default="")
    sub.add_parser("snap", help=argparse.SUPPRESS)  # alias

    # restore
    c = sub.add_parser("restore", help="Restore snapshot (no tag = list)")
    c.add_argument("name")
    c.add_argument("tag", nargs="?", default="")

    # crash-collect
    c = sub.add_parser("crash-collect", help="Collect vmcore + run triage")
    c.add_argument("name")
    c.add_argument(
        "--trigger",
        action="store_true",
        help="Crash the VM first via sysrq-trigger",
    )
    c.add_argument("--mod-dir", help="Lustre build tree for triage symbols")
    c.add_argument(
        "--outdir", default="/tmp", help="Output directory (default: /tmp)"
    )
    c.add_argument(
        "--wait",
        type=int,
        default=60,
        help="Seconds to wait for reboot (default: 60)",
    )

    # doctor
    c = sub.add_parser("doctor", help="Find/fix stale state")
    c.add_argument("--fix", action="store_true")

    # cluster
    c = sub.add_parser("cluster", help="Multi-node cluster management")
    csub = c.add_subparsers(dest="cluster_cmd", metavar="CMD")

    cc = csub.add_parser("create", help="Create a cluster")
    cc.add_argument("name", help="Cluster name")
    cc.add_argument(
        "nodes",
        nargs="+",
        metavar="SPEC",
        help="Node specs: roles:vmname[:disks] "
        "(roles: mgs,mds,oss,client joined with +)",
    )
    cc.add_argument("--vcpus", type=int, default=2)
    cc.add_argument("--mem", type=int, default=4096)

    cc = csub.add_parser("deploy", help="Deploy Lustre to cluster")
    cc.add_argument("name", help="Cluster name")
    cc.add_argument(
        "--build", required=True, help="Path to built lustre-release tree"
    )
    cc.add_argument(
        "--mount", action="store_true", help="Run llmount.sh after deploy"
    )
    cc.add_argument(
        "--server-only",
        action="store_true",
        help="With --mount, skip client mount",
    )

    cc = csub.add_parser("destroy", help="Destroy cluster and VMs")
    cc.add_argument("name", help="Cluster name")

    cc = csub.add_parser("list", help="List clusters")

    cc = csub.add_parser("status", help="Cluster health")
    cc.add_argument("name", help="Cluster name")

    cc = csub.add_parser("ssh", help="SSH to cluster node")
    cc.add_argument("name", help="Cluster name")
    cc.add_argument("target", help="Node name or role")
    cc.add_argument("command", nargs=argparse.REMAINDER)

    cc = csub.add_parser("exec", help="Exec on cluster node")
    cc.add_argument("--timeout", type=int, default=120)
    cc.add_argument("name", help="Cluster name")
    cc.add_argument("target", help="Node name or role")
    cc.add_argument("command", nargs=argparse.REMAINDER)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.subcmd:
        parser.print_help()
        sys.exit(0)

    # Handle name from --name flag vs positional for create
    if args.subcmd == "create":
        args.name = args.name_flag or args.name

    # Alias handling
    cmd_map = {
        "rm": "destroy",
        "push": "cp-to",
        "pull": "cp-from",
        "ls": "list",
        "ps": "list",
        "snap": "snapshot",
    }
    cmd = cmd_map.get(args.subcmd, args.subcmd)

    # For aliases that don't carry args, re-parse
    if cmd != args.subcmd and cmd in ("list",):
        args.json = False

    try:
        dispatch = {
            "create": cmd_create,
            "ensure": cmd_ensure,
            "start": cmd_start,
            "start-all": cmd_start_all,
            "stop": cmd_stop,
            "stop-all": cmd_stop_all,
            "restart": cmd_restart,
            "destroy": cmd_destroy,
            "exec": cmd_exec,
            "ssh": cmd_ssh,
            "cp-to": cmd_cp_to,
            "cp-from": cmd_cp_from,
            "list": cmd_list,
            "status": cmd_status,
            "log": cmd_log,
            "dmesg": cmd_dmesg,
            "lustre-log": cmd_lustre_log,
            "snapshot": cmd_snapshot,
            "restore": cmd_restore,
            "crash-collect": cmd_crash_collect,
            "doctor": cmd_doctor,
            "cluster": cmd_cluster,
        }
        fn = dispatch.get(cmd)
        if fn:
            fn(args)
        else:
            parser.print_help()
    except VMNotFound as e:
        die(str(e), EXIT_NOT_FOUND)


if __name__ == "__main__":
    main()
