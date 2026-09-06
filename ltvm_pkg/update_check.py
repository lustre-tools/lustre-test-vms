"""ltvm self-update check.

Compares the local ``ltvm_pkg.__version__`` (which embeds the short
git sha written to ``_build_info.py`` at install time) against the
tip of ``master`` on the upstream GitHub repo, and -- if the local
tree is behind -- prompts the user to update.

Triggers:
  * Once every 24h on any interactive ltvm invocation
  * Immediately on a schema-version mismatch raised by release
    fetching

Config lives in ``~/.config/ltvm/config.json``::

    {
      "update_check": {
        "mode": "prompt" | "auto" | "never",
        "last_check_iso": "2026-04-16T16:00:00+00:00"
      }
    }

Non-interactive callers (``--json`` or no stdin tty) are silently
skipped -- we never want to block a script on an interactive prompt.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)


REPO_SLUG = "lustre-tools/lustre-test-vms"
CHECK_INTERVAL = timedelta(hours=24)

_CONFIG_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ltvm"
)
_CONFIG_FILE = _CONFIG_DIR / "config.json"


Mode = Literal["prompt", "auto", "never"]
_DEFAULT_CONFIG: dict[str, Any] = {
    "update_check": {"mode": "prompt", "last_check_iso": None},
}


# ---------------------------------------------------------------------------
# Config IO
# ---------------------------------------------------------------------------


def _load_config() -> dict[str, Any]:
    if not _CONFIG_FILE.is_file():
        return json.loads(json.dumps(_DEFAULT_CONFIG))  # deep copy
    try:
        data = json.loads(_CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning(
            "ltvm config at %s is unreadable; using defaults", _CONFIG_FILE
        )
        return json.loads(json.dumps(_DEFAULT_CONFIG))
    # Merge defaults so a partial config still works.
    out = json.loads(json.dumps(_DEFAULT_CONFIG))
    uc = data.get("update_check")
    if isinstance(uc, dict):
        out["update_check"].update(uc)
    return out


def _save_config(cfg: dict[str, Any]) -> None:
    """Persist the config, keeping it owned by the human.

    maybe_check_for_updates() runs on every interactive invocation,
    including the ones that need root (`sudo ltvm cluster create`).  On
    a distro that preserves HOME under sudo, the first such call wrote
    the user's config.json as root; every later unprivileged ltvm could
    still read it but _bump_last_check raised PermissionError, which
    ltvm's blanket `except Exception` swallowed.  last_check_iso then
    never advanced, so _due_for_check was always true -- a git
    ls-remote on every single command, and an update prompt with it.
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


def _bump_last_check(cfg: dict[str, Any]) -> None:
    cfg["update_check"]["last_check_iso"] = datetime.now(
        timezone.utc
    ).isoformat()
    _save_config(cfg)


# ---------------------------------------------------------------------------
# Schedule gate
# ---------------------------------------------------------------------------


def _is_interactive() -> bool:
    """True if stdin+stdout are TTYs and we're not in --json mode.

    The --json check is done by the caller (we don't reach into argv
    from here), but stdin/stdout TTYs we can check directly.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def _due_for_check(cfg: dict[str, Any]) -> bool:
    last = cfg["update_check"].get("last_check_iso")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last_dt >= CHECK_INTERVAL


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
        from . import _build_info  # type: ignore[attr-defined]

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
# Public entry points
# ---------------------------------------------------------------------------


def maybe_check_for_updates(
    *, force: bool = False, use_json: bool = False
) -> None:
    """Top-level hook called once per ltvm invocation.

    ``force=True`` bypasses the 24h schedule (used when a caller has
    already seen a schema mismatch).  ``use_json=True`` suppresses
    the interactive prompt entirely -- JSON callers are scripts, not
    humans, and we never want to block them.
    """
    if use_json or not _is_interactive():
        return
    cfg = _load_config()
    mode = cfg["update_check"].get("mode", "prompt")
    # "never" means never: not even a schema-mismatch force bypasses
    # the user's explicit opt-out.  The raw fetch error still surfaces.
    if mode == "never":
        return
    if not force and not _due_for_check(cfg):
        return

    local = _local_hash()
    remote = _remote_hash()
    if local is None or remote is None:
        # Can't tell; don't nag.  Still mark the attempt so we don't
        # pound the network every invocation.
        _bump_last_check(cfg)
        return

    if not _is_newer(local, remote):
        _bump_last_check(cfg)
        return

    if mode == "auto":
        _bump_last_check(cfg)
        print(
            f"ltvm: auto-updating ({local} -> {remote})...",
            file=sys.stderr,
        )
        if _apply_update():
            _exit_after_update()
        return

    # mode == "prompt"
    choice = _prompt_choice(local, remote)
    _bump_last_check(cfg)  # always: we DID check, regardless of answer
    if choice == "y":
        if _apply_update():
            _exit_after_update()
    elif choice == "a":
        cfg["update_check"]["mode"] = "auto"
        _save_config(cfg)
        if _apply_update():
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
