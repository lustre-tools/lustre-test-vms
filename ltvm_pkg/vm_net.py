"""Networking, DNS, and SSH registry management."""

from __future__ import annotations

import fcntl
import hashlib
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .priv import atomic_write as _priv_atomic_write
from .priv import sudo_run
from .qemu_run import die, run
from .vm_state import MARKER, ROOT_PASSWORD, SUBNET, VM_DIR, VMInfo, VMNotFound

_IP_LOCK_PATH = VM_DIR / ".ip-alloc.lock"
_HOSTS_LOCK_PATH = VM_DIR / ".hosts.lock"

# Host /etc/hosts path -- module-level so tests can monkey-patch a
# tmp file instead of the real system file.  All readers/writers in
# this package import HOSTS_FILE rather than hard-coding the literal.
HOSTS_FILE = Path("/etc/hosts")

# Shared SSH client options for all sshpass-driven ssh/scp calls.
# Kept here so call sites can't diverge (one was omitting
# UserKnownHostsFile=/dev/null, silently polluting known_hosts on
# every deploy).
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
]


def sshpass_ssh_argv(
    ip: str,
    command: str,
    *,
    extra_opts: list[str] | None = None,
) -> list[str]:
    """argv for `sshpass ssh [opts] root@<ip> <command>`."""
    opts = SSH_OPTS + (extra_opts or [])
    return [
        "sshpass", "-p", ROOT_PASSWORD, "ssh",
        *opts,
        f"root@{ip}", command,
    ]


def sshpass_scp_argv(
    src: str,
    dst: str,
    *,
    extra_opts: list[str] | None = None,
) -> list[str]:
    """argv for `sshpass scp [opts] <src> <dst>`.

    Either src or dst may be a `root@host:/path` remote spec; scp
    figures out the direction itself.
    """
    opts = SSH_OPTS + (extra_opts or [])
    return [
        "sshpass", "-p", ROOT_PASSWORD, "scp",
        *opts,
        src, dst,
    ]


@contextmanager
def _ip_alloc_lock() -> Iterator[None]:
    """Exclusive file lock serialising IP allocation across concurrent creates."""
    _IP_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_IP_LOCK_PATH, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def _hosts_lock() -> Iterator[None]:
    """Exclusive file lock serialising /etc/hosts and ~/.ssh/config edits.

    `cmd_cluster_create` spawns N parallel `sudo ltvm create` subprocesses,
    each of which calls register_ssh_name(); without this lock the unsynchronised
    read-modify-write on /etc/hosts silently drops entries.
    """
    _HOSTS_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_HOSTS_LOCK_PATH, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically.

    Thin wrapper over ``priv.atomic_write`` that preserves the
    target's existing mode when the file already exists, falling
    back to 0o644.  Sudo-elevates the write when the parent dir
    isn't user-writable (e.g. /etc/hosts).
    """
    mode = 0o644
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        pass
    _priv_atomic_write(path, content, mode=mode)


def _chown_to_real_user(path: Path) -> None:
    """Chown *path* to ``SUDO_USER`` when running under sudo.

    Every write into ``~/.ssh/`` from ltvm goes through tempfile +
    rename as root, so without this the user's own dotfiles end up
    root-owned and unreadable to their normal-shell ssh -- which then
    falls through to password auth and "Permission denied".  Callers
    in register/unregister and any future writer should run this
    after each atomic write.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_user or sudo_user == "root":
        return
    import pwd as _pwd
    try:
        pw = _pwd.getpwnam(sudo_user)
    except KeyError:
        return
    try:
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except OSError:
        pass


def tap_for_name(name: str) -> str:
    suffix = name
    if len(suffix) > 11:
        suffix = hashlib.md5(name.encode()).hexdigest()[:11]
    return f"tap-{suffix}"


def extra_tap_for_name(name: str, idx: int) -> str:
    """Return the TAP device name for extra NIC ``idx`` (1-based).

    The mgmt NIC (idx 0, eth0) lives at ``tap_for_name(name)``; extra
    NICs use ``tap_for_name(name) + "-<idx>"`` so that prefix-matching
    against ``tap-<vm>`` finds all of a VM's TAPs (used in cmd_doctor
    for orphan detection).  The Linux interface-name limit is 15 chars
    including the NUL, so we hash-compress the name portion if the
    combined name would overflow -- same fallback as ``tap_for_name``
    uses for long VM names on its own.
    """
    if idx < 1:
        raise ValueError(f"extra NIC index must be >= 1, got {idx}")
    base = tap_for_name(name)
    suffix = f"-{idx}"
    if len(base) + len(suffix) <= 15:
        return base + suffix
    # base is already at its 15-char limit; re-hash with the index
    # folded in so the combined ifname fits.
    h = hashlib.md5(f"{name}|{idx}".encode()).hexdigest()
    return f"tap-{h[:11 - len(suffix)]}{suffix}"


def mac_for_name(name: str) -> str:
    h = hashlib.md5(name.encode()).hexdigest()
    return f"AA:FC:00:{h[0:2]}:{h[2:4]}:{h[4:6]}"


def extra_mac_for_name(name: str, idx: int) -> str:
    """Return a deterministic MAC for extra NIC ``idx`` (1-based).

    Keys on ``(name, idx)`` so each extra NIC has a distinct MAC that
    doesn't collide with the mgmt NIC (idx 0, ``mac_for_name``) or
    with any other VM's NICs.  Uses the same AA:FC:00 locally-administered
    prefix as the mgmt NIC.
    """
    if idx < 1:
        raise ValueError(f"extra NIC index must be >= 1, got {idx}")
    h = hashlib.md5(f"{name}|nic{idx}".encode()).hexdigest()
    return f"AA:FC:00:{h[0:2]}:{h[2:4]}:{h[4:6]}"


def _used_ips(exclude_name: str) -> set[str]:
    """Return every IP currently in use by any VM on this host.

    Scans both the primary mgmt IP (``VMInfo.ip``) and any per-extra-NIC
    IPs (``VMInfo.nic_ips``) so a multi-NIC VM's addresses don't collide
    with another VM's mgmt or extras.
    """
    used: set[str] = set()
    for n in VMInfo.all_names():
        if n == exclude_name:
            continue
        try:
            vm = VMInfo.load(n)
        except VMNotFound:
            continue
        if vm.ip:
            used.add(vm.ip)
        used.update(ip for ip in vm.nic_ips if ip)
    return used


@contextmanager
def alloc_ip(
    name: str,
    count: int = 1,
    explicit_ip: str | None = None,
) -> Iterator[list[str]]:
    """Context manager that allocates *count* unique IPs for *name* under
    an exclusive lock held until the ``with`` block exits.

    Returns a list of IPs of length *count*.  Element 0 is the mgmt IP
    (eth0); remaining elements are for extra NICs (eth1, eth2, ...).

    ``explicit_ip`` (optional) pins the mgmt IP; extras are still
    auto-allocated.  Pass None to auto-allocate all IPs.

    Usage::

        with alloc_ip(name, count=1 + len(extras)) as ips:
            vm = VMInfo(..., ip=ips[0], nic_ips=ips[1:], ...)
            vm.save()   # lock released after this block
    """
    if count < 1:
        raise ValueError(f"alloc_ip count must be >= 1, got {count}")
    with _ip_alloc_lock():
        used = _used_ips(name)
        ips: list[str] = []
        if explicit_ip:
            if explicit_ip in used:
                die(f"IP {explicit_ip} already used by another VM")
            ips.append(explicit_ip)
            used.add(explicit_ip)

        # Deterministic starting octet so ltvm create returns a stable
        # IP for the same name across re-creates (matches pre-multi-IP
        # behaviour).  Scan forward until we have `count` free octets.
        base_octet = (
            int(hashlib.md5(name.encode()).hexdigest()[:4], 16) % 244
        ) + 10
        need = count - len(ips)
        if need > 0:
            # Wider scan window than 244 deltas is pointless -- the /24
            # only has 244 usable host addresses -- but we walk the full
            # range so a heavily-populated host still finds free slots
            # (including wrap-around past the hash seed).
            delta = 0
            while need > 0 and delta < 244:
                octet = ((base_octet - 10 + delta) % 244) + 10
                ip = f"{SUBNET}.{octet}"
                if ip not in used:
                    ips.append(ip)
                    used.add(ip)
                    need -= 1
                delta += 1
            if need > 0:
                die(
                    f"No free IP addresses available in {SUBNET}.0/24 "
                    f"(need {count}, short by {need})"
                )
        yield ips


def reload_dns() -> None:
    """SIGHUP dnsmasq to re-read /etc/hosts.

    Linux runs dnsmasq on the qemu-bridge interface; macOS runs its
    own dnsmasq under launchd bound to the socket_vmnet gateway IP
    so guests can resolve each other by name (their resolv.conf
    points at fc_gw, where nothing else is listening).  Both daemons
    pick up new /etc/hosts entries on SIGHUP -- only the pidfile
    location differs.
    """
    from .host_setup import is_macos, DNSMASQ_PID_PATH
    pid_path: Path
    if is_macos():
        pid_path = DNSMASQ_PID_PATH
    else:
        pid_path = Path("/run/dnsmasq.pid")
    pid: int | None = None
    pid_err: str | None = None
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
        except (ValueError, OSError) as e:
            pid_err = f"read {pid_path}: {e}"
    if pid is None:
        r = run(["pgrep", "-x", "dnsmasq"])
        if r.returncode == 0 and r.stdout.strip():
            pid = int(r.stdout.strip().splitlines()[0])
        else:
            raise RuntimeError(
                "failed to reload dnsmasq: "
                f"pidfile {pid_path} unusable ({pid_err or 'missing'}) "
                f"and pgrep -x dnsmasq returned rc={r.returncode}"
            )
    # dnsmasq runs as root, so signalling it from an unprivileged user
    # gets EPERM.  Try the cheap path first; fall back to ``sudo kill``.
    try:
        os.kill(pid, signal.SIGHUP)
        return
    except PermissionError:
        pass
    except OSError as e:
        raise RuntimeError(
            f"failed to reload dnsmasq: kill SIGHUP {pid}: {e}"
        ) from e
    r = sudo_run(
        ["kill", "-HUP", str(pid)], check=False, quiet=True
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"failed to reload dnsmasq: sudo kill SIGHUP {pid} "
            f"rc={r.returncode}: {(r.stderr or '').strip()}"
        )


# ── SSH name / key management ────────────────────────────


def _real_user_ssh_dir() -> tuple[str, Path]:
    """Return (username, ~/.ssh) for the human invoking ltvm.

    Three cases:
      * Plain ``ltvm`` as user X -> (X, ~X/.ssh).  The common path
        once the privilege model lets every command run as the user.
      * ``sudo ltvm`` from user X -> (X, ~X/.ssh) via SUDO_USER, so
        the user's own ssh config / known_hosts get the entries.
      * Truly running as root with no SUDO_USER -> ('root', /root/.ssh).
    """
    real_user = os.environ.get("SUDO_USER")
    if not real_user:
        import getpass
        real_user = getpass.getuser()
    if real_user == "root":
        return real_user, Path("/root/.ssh")
    return real_user, Path(f"~{real_user}").expanduser() / ".ssh"


def register_ssh_name(name: str, ip: str) -> None:
    """Add /etc/hosts and ~/.ssh/config entries for a VM."""
    with _hosts_lock():
        _register_ssh_name_locked(name, ip)


def _register_ssh_name_locked(name: str, ip: str) -> None:
    hosts = HOSTS_FILE
    marker_line = f"{MARKER}:{name}"

    # /etc/hosts — always replace any existing entry for this name so the
    # write is idempotent.  The _hosts_lock above serialises read-modify-write
    # against concurrent register/unregister calls (notably from parallel
    # `sudo ltvm create` subprocesses spawned by `ltvm cluster create`).
    hosts_text = hosts.read_text() if hosts.exists() else ""
    new_entry = f"{ip}\t{name} {marker_line}\n"
    # Strip only lines whose marker matches EXACTLY (anchored to
    # end-of-line).  A plain substring check would strip lines for
    # sibling-named VMs: "# qemu-vm:co1" is a substring of
    # "# qemu-vm:co1-single", so registering/unregistering co1 would
    # silently corrupt the co1-single entry.
    filtered = [
        ln
        for ln in hosts_text.splitlines(keepends=True)
        if not ln.rstrip("\n").endswith(marker_line)
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
    _chown_to_real_user(ssh_cfg)


def unregister_ssh_name(name: str) -> None:
    """Remove /etc/hosts and ~/.ssh/config entries for a VM."""
    with _hosts_lock():
        _unregister_ssh_name_locked(name)


def _unregister_ssh_name_locked(name: str) -> None:
    marker = f"{MARKER}:{name}"

    # Capture the IP from /etc/hosts BEFORE we strip the entry, so we
    # can also nuke the matching ~/.ssh/known_hosts records.  Without
    # that, the next `ltvm create <samename>` boots a VM with a fresh
    # SSH host key and the user's ssh hits "WARNING: REMOTE HOST
    # IDENTIFICATION HAS CHANGED" instead of just connecting.
    hosts = HOSTS_FILE
    ip: str | None = None
    if hosts.exists():
        for line in hosts.read_text().splitlines():
            stripped = line.rstrip("\r\n")
            if stripped.endswith(marker):
                parts = stripped.split()
                if parts:
                    ip = parts[0]
                break

    # /etc/hosts (atomic write to avoid races with parallel destroys).
    # Anchor the marker match to end-of-line: see the prefix-collision
    # comment in _register_ssh_name_locked above.
    if hosts.exists():
        lines = [
            line
            for line in hosts.read_text().splitlines(keepends=True)
            if not line.rstrip("\r\n").endswith(marker)
        ]
        _atomic_write(hosts, "".join(lines))
        reload_dns()

    # ~/.ssh/config -- remove block.  Same root-owns-the-file footgun
    # as register, so we chown back to the real user after writing.
    _, ssh_dir = _real_user_ssh_dir()
    ssh_cfg = ssh_dir / "config"
    if ssh_cfg.exists():
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
        _chown_to_real_user(ssh_cfg)

    # ~/.ssh/known_hosts -- drop entries by name AND by ip so a
    # recreate doesn't trip the "host key changed" guard.  ssh-keygen
    # handles the file format (hashed-host entries, comments, etc.)
    # and is a no-op if the file doesn't exist.
    known = ssh_dir / "known_hosts"
    if known.exists():
        for target in (name, ip):
            if not target:
                continue
            try:
                subprocess.run(
                    ["ssh-keygen", "-q", "-R", target, "-f", str(known)],
                    check=False,
                    capture_output=True,
                )
            except OSError:
                pass
        _chown_to_real_user(known)
        backup = known.with_name(known.name + ".old")
        if backup.exists():
            _chown_to_real_user(backup)


def deploy_ssh_key(ip: str) -> None:
    """Copy the invoking user's SSH public key to the VM."""
    _, ssh_dir = _real_user_ssh_dir()
    pubkey = None
    for f in sorted(ssh_dir.glob("id_*.pub")):
        pubkey = f
        break
    if not pubkey:
        return
    # Pipe the key in via stdin so weird key-file content (newlines in
    # comment field, embedded quotes, etc.) can't break shell quoting.
    key_data = pubkey.read_text().rstrip("\n") + "\n"
    ssh_cmd = sshpass_ssh_argv(
        ip,
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "cat >> ~/.ssh/authorized_keys && "
        "chmod 600 ~/.ssh/authorized_keys",
        extra_opts=["-o", "ConnectTimeout=5"],
    )
    try:
        r = subprocess.run(
            ssh_cmd,
            input=key_data,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        die(f"SSH key deployment timed out for {ip}")
    # Check the rc instead of silently swallowing errors -- previously
    # an authorized_keys write failure (full disk, RO remount, perms)
    # left the VM looking provisioned but with no key.
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        die(f"SSH key deployment failed on {ip} (rc={r.returncode}): {err}")


def run_ssh(
    ip: str,
    command: str,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Run a command on a VM via SSH with timeout."""
    ssh_cmd = sshpass_ssh_argv(
        ip,
        command,
        extra_opts=[
            "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3",
        ],
    )
    return run(ssh_cmd, timeout=timeout)


def provision_vm_ssh(
    vm: VMInfo,
    timeout: int = 30,
    *,
    register_before_wait: bool = False,
) -> None:
    """Run the standard post-launch SSH provisioning sequence.

    1. wait_for_ssh
    2. register_ssh_name (/etc/hosts + ~/.ssh/config)
    3. deploy_ssh_key (invoking user's pubkey)

    `register_before_wait` flips the first two steps: cmd_start wants
    /etc/hosts populated even if wait_for_ssh times out, so the user
    can `ssh root@<name>` to diagnose a stalled boot.
    """
    if register_before_wait:
        register_ssh_name(vm.name, vm.ip)
        wait_for_ssh(vm.ip, timeout)
    else:
        wait_for_ssh(vm.ip, timeout)
        register_ssh_name(vm.name, vm.ip)
    deploy_ssh_key(vm.ip)


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
            die(
                f"required command missing on host ({e}); is sshpass installed?"
            )
        time.sleep(1)
    elapsed = int(time.monotonic() - start)
    die(f"SSH not ready after {elapsed}s on {ip}")
