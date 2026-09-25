"""Claims: which session is using a VM right now.

Several agent sessions (or people) on one host see the same VMs, and
nothing in ``ltvm list`` says which of them is in the middle of a deploy
or a test run on one.  A claim records that.  Commands that change what
runs on a VM (deploy, llmount, start/stop/destroy, snapshot, cluster
deploy/exec) refuse a VM that someone else holds a live claim on, and
``deploy-lustre`` claims an unclaimed VM for an agent session.

A claim is ``VM_DIR/claims/<vm>``: a 0666 file in a 1777 directory, so
every user can claim and release every VM without sudo.  It is updated
in place under ``flock`` and never renamed or unlinked, because the
sticky bit stops one user from replacing a file another created.  An
empty file means unclaimed.

The claimant is identified by ``owner`` (``--owner``, ``$LTVM_OWNER_ID``,
a Claude Code session, else ``user:<name>``) and, when known, a ``pid``
whose exit ends the claim (``--pid``, ``$LTVM_OWNER_PID``, the Claude Code
process).  Each command an agent runs is a fresh shell, so ltvm's own
parent is no use for that.  ``expires`` ends a claim after a ``--ttl``.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .vm_owner import OWNER_ID_ENV, validate_owner_id
from .vm_state import VM_DIR

CLAIMS_DIR = VM_DIR / "claims"
CLAIMS_DIR_MODE = 0o1777
OWNER_PID_ENV = "LTVM_OWNER_PID"

# Claude Code exports these into every command it runs, so a session
# is recognised without the agent remembering to export anything.
CLAUDE_SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
CLAUDE_PID_ENV = "CLAUDE_PID"

_TTL_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


class ClaimError(RuntimeError):
    """A VM is claimed by someone else, or the claim store is unusable."""


class ClaimHeld(ClaimError):
    """A VM is claimed by another live owner."""


@dataclass
class Claim:
    vm: str
    owner: str
    user: str
    pid: int | None = None
    pid_start: str | None = None
    since: float = 0.0
    expires: float | None = None
    tree: str | None = None

    def live(self, now: float | None = None) -> bool:
        """Is this claim still in force?"""
        now = time.time() if now is None else now
        if self.expires is not None and now >= self.expires:
            return False
        if self.pid is None:
            return True
        return _pid_alive(self.pid, self.pid_start)

    def describe(self) -> str:
        parts = [f"owner {self.owner}", f"user {self.user}"]
        if self.pid is not None:
            parts.append(f"pid {self.pid}")
        if self.tree:
            parts.append(f"tree {self.tree}")
        parts.append(
            "since " + time.strftime("%F %T", time.localtime(self.since))
        )
        if self.expires is not None:
            parts.append(
                "until " + time.strftime("%F %T", time.localtime(self.expires))
            )
        return ", ".join(parts)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["live"] = self.live()
        return d


# ── identity ─────────────────────────────────────────────


def _user_name() -> str:
    import pwd

    name = os.environ.get("SUDO_USER")
    if name and name != "root":
        return name
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())


def current_owner(
    explicit: str | None = None, *, environ: Mapping[str, str] | None = None
) -> str:
    """Who is asking: --owner, $LTVM_OWNER_ID, a Claude Code session, or
    the user."""
    env = os.environ if environ is None else environ
    if explicit:
        return validate_owner_id(explicit)
    if env.get(OWNER_ID_ENV):
        return validate_owner_id(env[OWNER_ID_ENV])
    if env.get(CLAUDE_SESSION_ENV):
        return validate_owner_id(f"claude:{env[CLAUDE_SESSION_ENV]}")
    return f"user:{_user_name()}"


def is_session_owner(owner: str) -> bool:
    """True for an agent session, False for the per-user fallback."""
    return not owner.startswith("user:")


def current_pid(
    explicit: int | None = None, *, environ: Mapping[str, str] | None = None
) -> int | None:
    """The process whose exit ends a claim, if one is known."""
    env = os.environ if environ is None else environ
    if explicit is not None:
        return explicit
    for key in (OWNER_PID_ENV, CLAUDE_PID_ENV):
        if key == CLAUDE_PID_ENV and not env.get(CLAUDE_SESSION_ENV):
            continue
        val = env.get(key, "")
        if val.isdigit() and int(val) > 0:
            return int(val)
    return None


def _pid_start(pid: int) -> str | None:
    """Start time of *pid* as ps reports it, to tell a reused pid apart."""
    try:
        r = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            # Compared across users and sudo, so pin the locale and zone.
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC0"},
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out or None


def _pid_alive(pid: int, start: str | None) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    if start is None:
        return True
    now_start = _pid_start(pid)
    return now_start is None or now_start == start


def parse_ttl(text: str) -> float:
    """``90``, ``30m``, ``4h`` or ``2d`` as seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", text or "")
    if not m or int(m.group(1)) == 0:
        raise ValueError(f"bad TTL '{text}': want e.g. 90, 30m, 4h or 2d")
    return int(m.group(1)) * _TTL_UNITS[m.group(2)]


# ── store ────────────────────────────────────────────────


def _path(vm: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", vm):
        raise ClaimError(f"invalid VM name '{vm}'")
    return CLAIMS_DIR / vm


def claims_dir_ok() -> bool:
    try:
        st = CLAIMS_DIR.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and (st.st_mode & 0o7777) == CLAIMS_DIR_MODE


def ensure_claims_dir(*, noninteractive: bool = False) -> None:
    """Create ``VM_DIR/claims`` as 1777, with sudo if VM_DIR needs it."""
    if claims_dir_ok():
        return
    from .priv import sudo_ready, sudo_run

    if CLAIMS_DIR.is_symlink():
        raise ClaimError(f"{CLAIMS_DIR} is a symlink; refusing to use it")
    try:
        CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
        CLAIMS_DIR.chmod(CLAIMS_DIR_MODE)
        return
    except PermissionError:
        pass
    if noninteractive and not sudo_ready():
        raise ClaimError(
            f"{CLAIMS_DIR} is missing and needs root to create; "
            "run `ltvm doctor --fix` once"
        )
    sudo_run(
        ["mkdir", "-p", str(CLAIMS_DIR)],
        quiet=True,
        noninteractive=noninteractive,
    )
    sudo_run(
        ["chmod", f"{CLAIMS_DIR_MODE:o}", str(CLAIMS_DIR)],
        quiet=True,
        noninteractive=noninteractive,
    )


def _open(path: Path, create: bool) -> int:
    """Open the claim file, creating it only when it is not there.

    Never O_CREAT an existing file: with fs.protected_regular (the
    default on current Linux distributions) that fails in a sticky
    world-writable directory for a file another user created.
    """
    while True:
        try:
            return os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            if not create or not path.parent.is_dir():
                raise
        try:
            return os.open(
                path, os.O_RDWR | os.O_NOFOLLOW | os.O_CREAT | os.O_EXCL, 0o666
            )
        except FileExistsError:
            continue


@contextlib.contextmanager
def _locked(vm: str, *, create: bool) -> Iterator[int | None]:
    """Open and flock the claim file; yield None when there is none."""
    path = _path(vm)
    try:
        fd = _open(path, create)
    except FileNotFoundError:
        if create:
            raise ClaimError(f"{CLAIMS_DIR} is missing") from None
        yield None
        return
    except PermissionError as e:
        raise ClaimError(f"cannot open {path}: {e.strerror}") from None
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ClaimError(
                f"{path} is a symlink; refusing to use it"
            ) from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ClaimError(f"{path} is not a regular file")
        # The other users must be able to take over a stale claim.
        with contextlib.suppress(OSError):
            if os.fstat(fd).st_uid == os.geteuid():
                os.fchmod(fd, 0o666)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def _read(fd: int, vm: str) -> Claim | None:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        b = os.read(fd, 65536)
        if not b:
            break
        chunks.append(b)
    text = b"".join(chunks).decode(errors="replace").strip()
    if not text:
        return None
    try:
        d = json.loads(text)
        return Claim(
            vm=vm,
            owner=str(d["owner"]),
            user=str(d.get("user", "?")),
            pid=int(d["pid"]) if d.get("pid") is not None else None,
            pid_start=d.get("pid_start"),
            since=float(d.get("since", 0)),
            expires=float(d["expires"])
            if d.get("expires") is not None
            else None,
            tree=d.get("tree"),
        )
    except (ValueError, KeyError, TypeError):
        return None


def _write(fd: int, claim: Claim | None) -> None:
    body = b""
    if claim is not None:
        d = asdict(claim)
        d.pop("vm")
        body = (json.dumps(d, sort_keys=True) + "\n").encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    if body:
        os.write(fd, body)


def load(vm: str) -> Claim | None:
    """The claim on *vm* (live or stale), or None."""
    try:
        with _locked(vm, create=False) as fd:
            return None if fd is None else _read(fd, vm)
    except ClaimError:
        return None


def all_claims() -> dict[str, Claim]:
    out: dict[str, Claim] = {}
    try:
        names = sorted(p.name for p in CLAIMS_DIR.iterdir())
    except OSError:
        return out
    for name in names:
        with contextlib.suppress(ClaimError):
            c = load(name)
            if c is not None:
                out[name] = c
    return out


def _new_claim(
    vm: str,
    owner: str,
    pid: int | None,
    ttl: float | None,
    tree: str | None,
    prev: Claim | None,
) -> Claim:
    now = time.time()
    keep = prev is not None and prev.owner == owner and prev.live(now)
    return Claim(
        vm=vm,
        owner=owner,
        user=_user_name(),
        pid=pid,
        pid_start=_pid_start(pid) if pid is not None else None,
        since=prev.since if keep and prev is not None else now,
        expires=now + ttl if ttl else (prev.expires if keep and prev else None),
        tree=tree or (prev.tree if keep and prev else None),
    )


def claim(
    vm: str,
    *,
    owner: str | None = None,
    pid: int | None = None,
    ttl: float | None = None,
    tree: str | None = None,
    force: bool = False,
    noninteractive: bool = False,
) -> tuple[Claim, Claim | None]:
    """Claim *vm*; return (new claim, claim it replaced if not ours).

    Re-claiming refreshes our own claim.  A live claim by another owner
    raises ClaimError unless *force*.
    """
    owner = current_owner(owner)
    pid = current_pid(pid)
    ensure_claims_dir(noninteractive=noninteractive)
    with _locked(vm, create=True) as fd:
        assert fd is not None
        prev = _read(fd, vm)
        if (
            prev is not None
            and prev.owner != owner
            and prev.live()
            and not force
        ):
            raise ClaimHeld(_held_message(prev, "claim"))
        new = _new_claim(vm, owner, pid, ttl, tree, prev)
        _write(fd, new)
    replaced = prev if prev is not None and prev.owner != owner else None
    return new, replaced


def release(
    vm: str, *, owner: str | None = None, force: bool = False
) -> Claim | None:
    """Drop the claim on *vm*; return the claim dropped, if any."""
    owner = current_owner(owner)
    with _locked(vm, create=False) as fd:
        if fd is None:
            return None
        prev = _read(fd, vm)
        if prev is None:
            return None
        if prev.owner != owner and prev.live() and not force:
            raise ClaimHeld(
                f"{vm} is claimed by {prev.describe()}; "
                f"`ltvm release --force {vm}` to break the claim"
            )
        _write(fd, None)
        return prev


def forget(vm: str) -> None:
    """Clear any claim on *vm*, e.g. once it is destroyed."""
    with contextlib.suppress(ClaimError, OSError):
        with _locked(vm, create=False) as fd:
            if fd is not None:
                _write(fd, None)


def _held_message(c: Claim, verb: str) -> str:
    msg = (
        f"{c.vm} is claimed by {c.describe()}; refusing to {verb} it.  "
        f"Pick another VM, or `ltvm release --force {c.vm}` to break "
        "the claim"
    )
    if os.geteuid() == 0 and c.user == os.environ.get("SUDO_USER"):
        msg += (
            ".  If the claim is your own session's, sudo dropped its "
            f"identity: keep {OWNER_ID_ENV} and {CLAUDE_SESSION_ENV} "
            "(`ltvm install` sets that up)"
        )
    return msg


def holder(vm: str, owner: str | None = None) -> Claim | None:
    """The live claim on *vm* by someone other than *owner*, if any."""
    c = load(vm)
    if c is None or c.owner == current_owner(owner) or not c.live():
        return None
    return c


def check(vm: str, verb: str) -> None:
    """Raise ClaimError when another live owner holds *vm*."""
    c = holder(vm)
    if c is not None:
        raise ClaimHeld(_held_message(c, verb))


def require(vm: str, verb: str) -> None:
    """check() for CLI paths: print the refusal and exit 1."""
    try:
        check(vm, verb)
    except ClaimError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


def require_all(vms: list[str], verb: str) -> None:
    """require() every VM before touching any, so a refusal never
    leaves a cluster half done."""
    for vm in vms:
        require(vm, verb)


def auto_claim(vm: str, tree: str | None = None) -> Claim | None:
    """Claim *vm* for an agent session before changing what runs on it.

    People without a session ID are not claimed for, so a deploy by
    hand never locks a VM away from anyone.  A host without the claims
    directory only gets a warning, but a VM another session claimed
    since the caller's check() raises ClaimHeld.
    """
    owner = current_owner()
    if not is_session_owner(owner):
        return None
    try:
        new, replaced = claim(vm, owner=owner, tree=tree, noninteractive=True)
    except ClaimHeld:
        raise
    except ClaimError as e:
        print(f"warning: not claiming {vm}: {e}", file=sys.stderr)
        return None
    if replaced is not None:
        print(
            f"{vm}: took over stale claim from {replaced.owner}",
            file=sys.stderr,
        )
    return new
