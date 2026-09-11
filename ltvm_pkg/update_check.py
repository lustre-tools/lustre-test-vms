"""ltvm self-update check.

Compares the local ``ltvm_pkg.__version__`` (which embeds the short
git sha written to ``_build_info.py`` at install time) against the
tip of ``master`` on the upstream GitHub repo, and -- if the local
tree is behind -- tells the user.

The work is split three ways because each part has different rules:

  * **refresh** -- hit the network, cache the verdict.  Weekly.
  * **notify** -- tell somebody.  Interactive callers get the prompt;
    everyone else gets one line on stderr, at most daily while an
    update is pending.
  * **act** -- ``git pull`` + reinstall.  Only ever from an
    interactive prompt, or an explicit ``sudo ltvm update``.

Splitting them is what makes the check useful to non-TTY callers --
scripts, CI, and agents driving ltvm through a subprocess.  Those
callers are a large share of real usage and previously saw nothing
at all, because one TTY gate suppressed the network check, the
notice and the action together.

**Acting without a TTY is forbidden, not merely skipped.** ``auto``
mode runs ``sudo ltvm install`` and then exits 0; with no TTY the
sudo either fails or blocks on a password, and a "successful" run
exits 0 *without having run the user's command* -- which a caller
reads as that command having succeeded.  Non-interactive auto mode
degrades to a notice.

Preferences live in ``~/.config/ltvm/config.json``::

    {"update_check": {"mode": "prompt" | "auto" | "never"}}

The cached verdict lives apart from them, in
``~/.local/state/ltvm/update_cache.json``::

    {
      "last_check_iso": "2026-04-16T16:00:00+00:00",
      "last_notice_iso": "2026-04-16T16:00:00+00:00",
      "pending_update": {"local": "abc1234", "remote": "def5678"}
    }

Two files, because they have different writers.  The config records
what the human chose and is written only when they answer a prompt;
the cache is regenerable machine state written by any invocation.
Keeping the cache separate also means an agent's non-TTY run no
longer consumes the weekly check that the human's next interactive
run was going to prompt from -- the verdict persists, so both see it.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)


REPO_SLUG = "lustre-tools/lustre-test-vms"

# How often we ask GitHub anything.
CHECK_INTERVAL = timedelta(days=7)
# How often we mention a pending update to a non-interactive caller.
# Shorter than CHECK_INTERVAL on purpose: a weekly network check that
# also notified weekly would leave an available update unmentioned for
# six days out of seven.
NOTICE_INTERVAL = timedelta(hours=24)

_CONFIG_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ltvm"
)
_CONFIG_FILE = _CONFIG_DIR / "config.json"

_STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    / "ltvm"
)
_STATE_FILE = _STATE_DIR / "update_cache.json"


Mode = Literal["prompt", "auto", "never"]
_DEFAULT_CONFIG: dict[str, Any] = {
    "update_check": {"mode": "prompt"},
}
_DEFAULT_STATE: dict[str, Any] = {
    "last_check_iso": None,
    "last_notice_iso": None,
    "pending_update": None,
}


# ---------------------------------------------------------------------------
# Config IO
# ---------------------------------------------------------------------------


def _default_config() -> dict[str, Any]:
    """A fresh deep copy of the default config."""
    return copy.deepcopy(_DEFAULT_CONFIG)


def _load_config() -> dict[str, Any]:
    if not _CONFIG_FILE.is_file():
        return _default_config()
    try:
        data = json.loads(_CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning(
            "ltvm config at %s is unreadable; using defaults", _CONFIG_FILE
        )
        return _default_config()
    # Merge defaults so a partial config still works.
    out = _default_config()
    uc = data.get("update_check")
    if isinstance(uc, dict):
        out["update_check"].update(uc)
    return out


def _save_config(cfg: dict[str, Any]) -> None:
    """Persist the config, keeping it owned by the human.

    maybe_check_for_updates() runs on invocations that need root
    (`sudo ltvm cluster create`).  On a distro that preserves HOME
    under sudo, the first such call wrote the user's config.json as
    root; every later unprivileged ltvm could still read it but not
    write it, and the resulting PermissionError was swallowed by
    ltvm's blanket `except Exception`.  The schedule stamp then never
    advanced, so the check was always due -- a git ls-remote on every
    single command, and an update prompt with it.  _save_state carries
    the same hazard and the same chown.
    """
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")
    except PermissionError:
        log.debug("cannot write %s (owned by another user?)", _CONFIG_FILE)
        return
    from .priv import chown_to_invoking_user

    chown_to_invoking_user(_CONFIG_DIR)
    chown_to_invoking_user(_CONFIG_FILE)


# ---------------------------------------------------------------------------
# State IO (the cached verdict)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state() -> dict[str, Any]:
    """A fresh deep copy of the default state."""
    return copy.deepcopy(_DEFAULT_STATE)


def _load_state() -> dict[str, Any]:
    if not _STATE_FILE.is_file():
        return _default_state()
    try:
        data = json.loads(_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()
    out = _default_state()
    out.update(data)
    return out


def _save_state(state: dict[str, Any]) -> None:
    """Persist the cached verdict, atomically and without nagging.

    Written by every invocation, including the root ones (`sudo ltvm
    cluster create`), so it hits the same ownership trap documented on
    _save_config -- hence the chown.  tempfile + replace because two
    ltvm processes can be running at once and a half-written cache
    would be read back as corrupt.

    Every failure here is swallowed: a lost cache costs one extra
    network check, which is not worth failing a user's command over.
    """
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_STATE_DIR), prefix=".update_cache.")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(state, indent=2) + "\n")
            os.replace(tmp, _STATE_FILE)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as e:
        log.debug("cannot write %s: %s", _STATE_FILE, e)
        return

    from .priv import chown_to_invoking_user

    chown_to_invoking_user(_STATE_DIR)
    chown_to_invoking_user(_STATE_FILE)


def _mark_noticed(state: dict[str, Any]) -> None:
    state["last_notice_iso"] = _now_iso()
    _save_state(state)


def _clear_pending(state: dict[str, Any]) -> None:
    state["pending_update"] = None
    _save_state(state)


# ---------------------------------------------------------------------------
# Schedule gate
# ---------------------------------------------------------------------------


def _is_interactive() -> bool:
    """True if stdin+stdout are TTYs and we're not in --json mode.

    The --json check is done by the caller (we don't reach into argv
    from here), but stdin/stdout TTYs we can check directly.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def _elapsed_since(stamp: Any, interval: timedelta) -> bool:
    """True if *stamp* is missing, unparseable, or older than *interval*."""
    if not isinstance(stamp, str) or not stamp:
        return True
    try:
        stamp_dt = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - stamp_dt >= interval


def _due_for_check(state: dict[str, Any]) -> bool:
    return _elapsed_since(state.get("last_check_iso"), CHECK_INTERVAL)


def _due_for_notice(state: dict[str, Any]) -> bool:
    return _elapsed_since(state.get("last_notice_iso"), NOTICE_INTERVAL)


# ---------------------------------------------------------------------------
# Remote comparison
# ---------------------------------------------------------------------------


def _local_hash() -> str | None:
    """Short sha of the local ltvm tree.

    Prefers the baked BUILD_HASH so we work even when the install
    isn't a git checkout.  Falls back to `git rev-parse` if the
    baked file is missing.
    """
    try:
        from . import _build_info

        h = getattr(_build_info, "BUILD_HASH", None)
        if isinstance(h, str) and h:
            return h
    except ImportError:
        pass
    repo = Path(__file__).resolve().parent.parent
    if not (repo / ".git").exists():
        return None
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
        return r.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


def _remote_hash() -> str | None:
    """Short sha of origin/master on the upstream repo.

    Uses `git ls-remote` so we don't require `gh` to be authenticated
    for a read that's already public.  Network failures are silent --
    the caller interprets ``None`` as "skip the check this time".
    """
    try:
        r = subprocess.run(
            [
                "git",
                "ls-remote",
                f"https://github.com/{REPO_SLUG}.git",
                "refs/heads/master",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    out = r.stdout.strip().split()
    if not out:
        return None
    return out[0][:7]


def _is_newer(local: str, remote: str) -> bool:
    """True if remote's sha is reachable but different from local.

    When we have the git tree we use `merge-base --is-ancestor` so a
    user sitting on a private branch AHEAD of master doesn't get a
    spurious "update available" prompt.  Without git, fall back to
    string equality (any mismatch implies newer).
    """
    if local == remote:
        return False
    repo = Path(__file__).resolve().parent.parent
    if not (repo / ".git").exists():
        return True  # can't check ancestry; assume remote is newer
    try:
        # If remote is an ancestor of local, we're ahead (or equal): no update.
        r = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "merge-base",
                "--is-ancestor",
                remote,
                local,
            ],
            capture_output=True,
            timeout=3,
        )
        if r.returncode == 0:
            return False
        # returncode == 1 means "not an ancestor" -> remote is newer
        # (or a divergent branch, which we conservatively treat as newer).
        return True
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return True


# ---------------------------------------------------------------------------
# Prompt + apply
# ---------------------------------------------------------------------------


_PROMPT = """
A newer ltvm is available (local={local}, remote={remote}).

  [y] yes, update now
  [a] auto-update from now on (still checks daily)
  [n] not right now
  [x] don't ask again (ltvm will stop checking for updates)

Your choice [y/a/N/x]: """


def _apply_update() -> bool:
    """Attempt `git pull` + `sudo ./ltvm install` in the checkout
    this package was loaded from.  Returns True on success.
    """
    repo = Path(__file__).resolve().parent.parent
    if not (repo / ".git").exists():
        print(
            "  ltvm is not installed from a git checkout -- "
            "can't self-update.  Re-clone to update.",
            file=sys.stderr,
        )
        return False
    print(f"  Updating {repo}...")
    try:
        subprocess.run(
            ["git", "-C", str(repo), "pull", "--ff-only"],
            check=True,
        )
    except subprocess.CalledProcessError:
        print("  git pull failed; aborting update.", file=sys.stderr)
        return False
    installer = repo / "ltvm"
    if not installer.exists():
        return False
    # Pin the interpreter to whatever python is currently running ltvm
    # (already known to meet the floor) instead of going through the
    # script's shebang.  Without this, on a host whose
    # /usr/bin/env python3 resolves to a sub-floor system Python (e.g.
    # rocky9's 3.9), the install step bombs at the floor check and the
    # update aborts mid-flight -- working tree pulled but
    # _build_info.py / wrappers never refreshed.
    py = sys.executable
    # macOS install runs as the invoking user (Homebrew refuses root)
    # and sudos selectively for the operations that need it; Linux
    # install needs root throughout.
    import platform

    if platform.system() == "Darwin":
        print(f"  Running {installer} install...")
        cmd = [py, str(installer), "install"]
    else:
        print(f"  Running {installer} install (will sudo)...")
        cmd = ["sudo", py, str(installer), "install"]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        print("  `ltvm install` failed; update incomplete.", file=sys.stderr)
        return False
    print("  ltvm updated.  Re-run your command.")
    return True


def _exit_after_update() -> None:
    """Stop after a successful self-update instead of continuing.

    _apply_update() does `git pull --ff-only` + `ltvm install` and then
    returned, so main() went on to run the user's command with the
    already-imported *old* ltvm_pkg modules against the freshly-pulled
    tree.  Anything read from disk after the pull -- targets.yaml,
    SCHEMA_VERSION, host-config/ templates, kernel-build-inner*.sh --
    is then the new file interpreted by old code: a new targets.yaml
    key, for instance, fails validation against the old in-memory
    _KNOWN_TARGET_KEYS immediately after a "successful" update.  The
    message already says "Re-run your command"; make that true.
    """
    raise SystemExit(0)


def _prompt_choice(local: str, remote: str) -> str:
    """Ask the user which of y/a/n/x they want; default is n."""
    try:
        ans = input(_PROMPT.format(local=local, remote=remote)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "n"
    if not ans:
        return "n"
    return ans[0]


# ---------------------------------------------------------------------------
# Refresh / notify
# ---------------------------------------------------------------------------


_NOTICE = "ltvm: update available ({local} -> {remote}).  Run: sudo ltvm update"


def _refresh_cache(state: dict[str, Any]) -> None:
    """Ask GitHub, and cache the verdict in *state*.

    A network or git failure leaves any previous verdict alone rather
    than clearing it -- an offline week should not make a known-pending
    update disappear.  last_check_iso is bumped either way so a host
    that cannot reach GitHub does not retry on every invocation.
    """
    local = _local_hash()
    remote = _remote_hash()
    state["last_check_iso"] = _now_iso()
    if local is not None and remote is not None:
        state["pending_update"] = (
            {"local": local, "remote": remote}
            if _is_newer(local, remote)
            else None
        )
    _save_state(state)


def _pending_if_current(state: dict[str, Any]) -> dict[str, Any] | None:
    """The cached verdict, or None if the local tree has moved since.

    Someone who has just run `sudo ltvm update` should not be told
    about the update they already applied for the rest of the week.
    _local_hash() prefers the baked BUILD_HASH, so this costs a dict
    lookup rather than a subprocess.
    """
    pending = state.get("pending_update")
    if not isinstance(pending, dict):
        return None
    if _local_hash() != pending.get("local"):
        _clear_pending(state)
        return None
    return pending


def _emit_notice(state: dict[str, Any], pending: dict[str, Any]) -> None:
    """Tell a non-interactive caller, on stderr.

    stderr rather than stdout because stdout is the data channel --
    `ltvm list --json` is parsed by callers and must stay clean.  We
    emit under --json for that reason: the JSON itself is untouched,
    and --json is exactly what a scripted or agent caller reaches for,
    so suppressing there would hide the notice from the audience that
    most needs it.
    """
    print(
        _NOTICE.format(
            local=pending.get("local", "?"), remote=pending.get("remote", "?")
        ),
        file=sys.stderr,
    )
    _mark_noticed(state)


def _handle_interactive(
    cfg: dict[str, Any],
    state: dict[str, Any],
    mode: str,
    pending: dict[str, Any],
) -> None:
    """Prompt (or auto-apply) for a caller that has a human attached."""
    local = pending.get("local", "?")
    remote = pending.get("remote", "?")

    if mode == "auto":
        _mark_noticed(state)
        print(f"ltvm: auto-updating ({local} -> {remote})...", file=sys.stderr)
        if _apply_update():
            _clear_pending(state)
            _exit_after_update()
        return

    choice = _prompt_choice(local, remote)
    _mark_noticed(state)  # always: we DID tell them, regardless of answer
    if choice == "y":
        if _apply_update():
            _clear_pending(state)
            _exit_after_update()
    elif choice == "a":
        cfg["update_check"]["mode"] = "auto"
        _save_config(cfg)
        if _apply_update():
            _clear_pending(state)
            _exit_after_update()
    elif choice == "x":
        cfg["update_check"]["mode"] = "never"
        _save_config(cfg)
        print(
            "  ltvm will not check for updates again.  "
            "Re-enable with: rm ~/.config/ltvm/config.json",
            file=sys.stderr,
        )
    # choice == "n" (or unrecognized): nothing further.


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def maybe_check_for_updates(
    *, force: bool = False, use_json: bool = False
) -> None:
    """Top-level hook called once per ltvm invocation.

    ``force=True`` bypasses both schedules (used when a caller has
    already seen a schema mismatch and wants the user told now).
    ``use_json=True`` suppresses the interactive prompt -- JSON
    callers are scripts, and we never block a script on a prompt --
    but they still get the stderr notice.
    """
    cfg = _load_config()
    mode = cfg["update_check"].get("mode", "prompt")
    # "never" means never: not even a schema-mismatch force bypasses
    # the user's explicit opt-out.  The raw fetch error still surfaces.
    if mode == "never":
        return

    state = _load_state()
    if force or _due_for_check(state):
        _refresh_cache(state)

    pending = _pending_if_current(state)
    if pending is None:
        return

    if _is_interactive() and not use_json:
        _handle_interactive(cfg, state, mode, pending)
    elif force or _due_for_notice(state):
        # No human on the far end: say it once and act on nothing.
        _emit_notice(state, pending)
