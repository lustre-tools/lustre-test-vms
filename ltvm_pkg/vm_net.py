"""Networking, DNS, and SSH registry management."""

from __future__ import annotations

import fcntl
import hashlib
import os
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .vm_state import MARKER, ROOT_PASSWORD, SUBNET, VM_DIR, VMInfo, VMNotFound
from .qemu_run import die, run

_IP_LOCK_PATH = VM_DIR / ".ip-alloc.lock"


@contextmanager
def _ip_alloc_lock():
    """Exclusive file lock serialising IP allocation across concurrent creates."""
    _IP_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_IP_LOCK_PATH, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via rename."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        try:
            os.chmod(tmp, path.stat().st_mode)
        except FileNotFoundError:
            pass  # target doesn't exist yet; keep mkstemp default mode
        os.rename(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def tap_for_name(name: str) -> str:
    suffix = name
    if len(suffix) > 11:
        suffix = hashlib.md5(name.encode()).hexdigest()[:11]
    return f"tap-{suffix}"


def mac_for_name(name: str) -> str:
    h = hashlib.md5(name.encode()).hexdigest()
    return f"AA:FC:00:{h[0:2]}:{h[2:4]}:{h[4:6]}"


def _used_ips(exclude_name: str) -> set[str]:
    used = set()
    for n in VMInfo.all_names():
        if n == exclude_name:
            continue
        try:
            used.add(VMInfo.load(n).ip)
        except VMNotFound:
            pass
    return used


@contextmanager
def alloc_ip(name: str, explicit_ip: str | None = None):
    """Context manager that allocates a unique IP for *name* under an exclusive
    lock held until the ``with`` block exits (i.e. until VMInfo.save() returns).

    Usage::

        with alloc_ip(name) as ip:
            vm = VMInfo(..., ip=ip, ...)
            vm.save()   # lock released after this block
    """
    with _ip_alloc_lock():
        if explicit_ip:
            used = _used_ips(name)
            if explicit_ip in used:
                die(f"IP {explicit_ip} already used by another VM")
            yield explicit_ip
            return

        base_octet = (int(hashlib.md5(name.encode()).hexdigest()[:4], 16) % 244) + 10
        used = _used_ips(name)
        for delta in range(244):
            octet = ((base_octet - 10 + delta) % 244) + 10
            ip = f"{SUBNET}.{octet}"
            if ip not in used:
                yield ip
                return
        die(f"No free IP addresses available in {SUBNET}.0/24")


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

    # /etc/hosts — always replace any existing entry for this name so the
    # write is idempotent.  Reading the current content and writing a new
    # version via _atomic_write is still a read-modify-write, but making it
    # idempotent means a duplicate race at worst writes the same content
    # twice; the final rename wins and /etc/hosts stays correct.
    hosts_text = hosts.read_text() if hosts.exists() else ""
    new_entry = f"{ip}\t{name} {marker_line}\n"
    # Strip any existing lines that reference this hostname marker.
    filtered = [
        ln
        for ln in hosts_text.splitlines(keepends=True)
        if marker_line not in ln
    ]
    _atomic_write(hosts, "".join(filtered) + new_entry)
    reload_dns()

    # ~/.ssh/config — read existing content, strip any old block for this
    # host, then append the (possibly updated) block atomically.
    real_user, ssh_dir = _real_user_ssh_dir()
    ssh_dir.mkdir(parents=True, exist_ok=True)
    ssh_cfg = ssh_dir / "config"
    cfg_text = ssh_cfg.read_text() if ssh_cfg.exists() else ""

    host_line = f"Host {name} {marker_line}"
    block = (
        f"\n{host_line}\n"
        f"\tHostName {ip}\n"
        f"\tUser root\n"
        f"\tStrictHostKeyChecking no\n"
        f"\tUserKnownHostsFile /dev/null\n"
        f"\tLogLevel ERROR\n"
        f"\tServerAliveInterval 5\n"
        f"\tServerAliveCountMax 3\n"
        f"\tConnectTimeout 5\n"
    )
    # Always strip any old block for this host before appending, so that
    # IP changes (e.g. destroy + recreate with --ip) are picked up.
    stripped_lines: list[str] = []
    skip = False
    for line in cfg_text.splitlines():
        if host_line in line:
            skip = True
            if stripped_lines and stripped_lines[-1] == "":
                stripped_lines.pop()
            continue
        if skip:
            if line.startswith("\t") or line == "":
                continue
            skip = False
        stripped_lines.append(line)
    cfg_text = "\n".join(stripped_lines) + ("\n" if stripped_lines else "")
    _atomic_write(ssh_cfg, cfg_text + block)
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
    _atomic_write(ssh_cfg, "\n".join(out) + "\n")


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
        die(f"SSH key deployment timed out for {ip}")


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
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "LogLevel=ERROR",
        f"root@{ip}",
        command,
    ]
    return run(ssh_cmd, timeout=timeout)


def wait_for_ssh(ip: str, max_wait: int = 30) -> None:
    """Wait for SSH to become available on a VM.

    `max_wait` is wall-clock seconds.  Each probe can take up to its
    own 5s connect timeout, so we track elapsed time explicitly rather
    than counting iterations -- the old "for _ in range(max_wait)"
    loop could actually wait up to 6*max_wait seconds while the error
    message claimed it gave up after `max_wait`.

    A FileNotFoundError here means sshpass/ssh aren't on PATH, which is
    a host-setup bug we want to surface immediately rather than masquerade
    as "SSH not ready".
    """
    start = time.monotonic()
    deadline = start + max_wait
    while time.monotonic() < deadline:
        try:
            r = run_ssh(ip, "true", timeout=5)
            if r.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError as e:
            die(f"required command missing on host ({e}); "
                f"is sshpass installed?")
        time.sleep(1)
    elapsed = int(time.monotonic() - start)
    die(f"SSH not ready after {elapsed}s on {ip}")
