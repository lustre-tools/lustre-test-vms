"""Networking, DNS, and SSH registry management."""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .models import MARKER, ROOT_PASSWORD, SUBNET, VMInfo, VMNotFound
from .process import die, run


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via rename."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, path.stat().st_mode)
        os.rename(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def check_ip_collision(name: str, ip: str) -> None:
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


def reload_dns() -> None:
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


# ── SSH name / key management ────────────────────────────


def _real_user_ssh_dir() -> tuple[str, Path]:
    real_user = os.environ.get("SUDO_USER", "root")
    if real_user == "root":
        ssh_dir = Path("/root/.ssh")
    else:
        ssh_dir = Path(f"~{real_user}").expanduser() / ".ssh"
    return real_user, ssh_dir


def register_ssh_name(name: str, ip: str) -> None:
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


def unregister_ssh_name(name: str) -> None:
    """Remove /etc/hosts and ~/.ssh/config entries for a VM."""
    marker = f"{MARKER}:{name}"

    # /etc/hosts (atomic write to avoid races with parallel destroys)
    hosts = Path("/etc/hosts")
    if hosts.exists():
        lines = [
            line
            for line in hosts.read_text().splitlines()
            if marker not in line
        ]
        _atomic_write(hosts, "\n".join(lines) + "\n")
        reload_dns()

    # ~/.ssh/config -- remove block
    _, ssh_dir = _real_user_ssh_dir()
    ssh_cfg = ssh_dir / "config"
    if not ssh_cfg.exists():
        return
    lines = ssh_cfg.read_text().splitlines()
    out: list[str] = []
    skip = False
    for line in lines:
        if f"Host {name} {marker}" in line:
            skip = True
            if out and out[-1] == "":
                out.pop()
            continue
        if skip:
            if line.startswith("\t") or line == "":
                continue
            skip = False
        out.append(line)
    ssh_cfg.write_text("\n".join(out) + "\n")


def deploy_ssh_key(ip: str) -> None:
    """Copy the invoking user's SSH public key to the VM."""
    _, ssh_dir = _real_user_ssh_dir()
    pubkey = None
    for f in sorted(ssh_dir.glob("id_*.pub")):
        pubkey = f
        break
    if not pubkey:
        return
    key_data = pubkey.read_text().strip().replace("'", "'\\''")
    try:
        run_ssh(
            ip,
            f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"echo '{key_data}' >> ~/.ssh/authorized_keys && "
            f"chmod 600 ~/.ssh/authorized_keys",
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        print(
            f"warning: SSH key deployment timed out for {ip}",
            file=__import__("sys").stderr,
        )


def run_ssh(
    ip: str,
    command: str,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Run a command on a VM via SSH with timeout."""
    ssh_cmd = [
        "sshpass",
        "-p",
        ROOT_PASSWORD,
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
    print(
        f"warning: SSH not ready after {max_wait}s",
        file=__import__("sys").stderr,
    )
    return False
