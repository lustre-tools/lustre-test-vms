"""ltvm usage telemetry -- an anonymous weekly check-in.

Sends a random install ID and the ltvm version to
https://ltvm.mulberrytree.cc once a week, so the people maintaining
ltvm can tell whether anyone is using it.  Opt-out, with a notice on
first run.

**Notice and send are gated separately**, which is the whole reason
this is not modelled on update_check's single TTY gate:

  * the notice needs a human, so it prints once on first run;
  * the send needs no human, so it is gated on `enabled` and the
    seven-day interval alone.

Gating the send on a TTY would have excluded CI and agent-driven
installs entirely -- which is a large share of how ltvm is actually
run, and the share least likely to ever see a prompt.  Installation
is the consent point: the notice is in the first run's output, and
opting out is one command.

What is sent is a closed list, built in _payload() and printed
verbatim by `ltvm telemetry show`.  Hostnames, usernames, paths, VM
names, Lustre tree identity and IP addresses are not on it and must
not be added -- the rule of thumb is that a week of the server's
table should be safe to paste into a public ticket.  (The server
records a *hash* of the source IP so distinct networks can be
counted; the address itself is never stored.)

Nothing here may ever fail a user's command.  Every entry point
swallows its own exceptions, and the ltvm hook wraps the lot again.
"""

from __future__ import annotations

import configparser
import contextlib
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("ltvm.telemetry")

ENDPOINT = os.environ.get(
    "LTVM_TELEMETRY_URL", "https://ltvm.mulberrytree.cc/v1/checkin"
)
SCHEMA_VERSION = 1
SEND_INTERVAL = timedelta(days=7)
# Long enough that a stalled network never delays the child past the
# point of being noticed; short enough to cross an ocean.
SEND_TIMEOUT = 5

_CONFIG_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ltvm"
)
_CONFIG_FILE = _CONFIG_DIR / "config.json"
_STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    / "ltvm"
)
_STATE_FILE = _STATE_DIR / "telemetry.json"
_SITE_CONFIG = Path(os.environ.get("LTVM_SITE_CONFIG", "/etc/ltvm.conf"))

NOTICE = """\
ltvm sends an anonymous weekly check-in -- a random install ID and the
ltvm version -- so we can see how many people use it.  No hostnames,
paths, or IP addresses are sent.

  ltvm telemetry show     print exactly what would be sent
  ltvm telemetry off      turn it off
"""


# ---------------------------------------------------------------------------
# Config and state
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_config() -> dict[str, Any]:
    """The telemetry block of ltvm's config, with defaults filled in."""
    out: dict[str, Any] = {
        "enabled": True,
        "install_id": None,
        "notice_shown": False,
    }
    try:
        data = json.loads(_CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return out
    block = data.get("telemetry") if isinstance(data, dict) else None
    if isinstance(block, dict):
        out.update(block)
    return out


def _save_config(block: dict[str, Any]) -> None:
    """Merge *block* into config.json without disturbing update_check.

    Read-modify-write rather than a whole-file rewrite: this file is
    shared with update_check, and clobbering someone's `mode` to record
    an install ID would be a memorable bug.
    """
    data: dict[str, Any] = {}
    with contextlib.suppress(OSError, json.JSONDecodeError):
        loaded = json.loads(_CONFIG_FILE.read_text())
        if isinstance(loaded, dict):
            data = loaded
    data["telemetry"] = block
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as e:
        log.debug("cannot write %s: %s", _CONFIG_FILE, e)
        return
    from .priv import chown_to_invoking_user

    chown_to_invoking_user(_CONFIG_DIR)
    chown_to_invoking_user(_CONFIG_FILE)


def _load_state() -> dict[str, Any]:
    out: dict[str, Any] = {"last_send_iso": None}
    try:
        data = json.loads(_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return out
    if isinstance(data, dict):
        out.update(data)
    return out


def _save_state(state: dict[str, Any]) -> None:
    """Persist send state atomically, and never raise.

    Written by root invocations too (`sudo ltvm cluster create`), so it
    carries the same chown as the other two state files against the
    root-owned-file trap update_check._save_config documents.
    """
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_STATE_DIR), prefix=".telemetry.")
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


def _site_disabled() -> bool:
    """True if /etc/ltvm.conf turns telemetry off for the whole host.

    The site file can only ever disable.  It exists so an admin can
    opt a shared lab out in one place, and an opt-out a user could
    silently undo would not be one.
    """
    if not _SITE_CONFIG.is_file():
        return False
    parser = configparser.ConfigParser()
    try:
        parser.read(_SITE_CONFIG)
        return not parser.getboolean("telemetry", "enabled", fallback=True)
    except (configparser.Error, ValueError, OSError) as e:
        log.debug("ignoring unreadable %s: %s", _SITE_CONFIG, e)
        return False


def is_enabled() -> bool:
    """Whether telemetry may run at all, cheapest check first."""
    if os.environ.get("LTVM_TELEMETRY", "").strip() in ("0", "no", "false"):
        return False
    if _site_disabled():
        return False
    return bool(_load_config().get("enabled", True))


def install_id() -> str:
    """This install's random ID, minted on first use.

    uuid4 rather than anything derived from the host: an ID that could
    be recomputed from a hostname or MAC would identify the machine,
    which is the property we are specifically trying not to have.
    Deleting it yields a new install, which is also the user's escape
    hatch.
    """
    cfg = _load_config()
    existing = cfg.get("install_id")
    if isinstance(existing, str) and existing:
        return existing
    new = str(uuid.uuid4())
    cfg["install_id"] = new
    _save_config(cfg)
    return new


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def _jitter(uid: str) -> timedelta:
    """A stable per-install offset of +/-12h on the send interval.

    Derived from the install ID rather than random so it does not move
    between runs.  Without it every install that updated on the same
    day would check in within the same hour a week later, which turns
    a trickle into a spike for no reason.
    """
    digest = hashlib.sha256(uid.encode()).digest()
    seconds = int.from_bytes(digest[:4], "big") % (24 * 3600) - 12 * 3600
    return timedelta(seconds=seconds)


def _due_for_send(state: dict[str, Any], uid: str) -> bool:
    last = state.get("last_send_iso")
    if not isinstance(last, str) or not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    due = last_dt + SEND_INTERVAL + _jitter(uid)
    return datetime.now(timezone.utc) >= due


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def _payload() -> dict[str, Any]:
    """Everything that leaves the machine.

    If you are adding a field here, it belongs on the list in this
    module's docstring first.
    """
    from . import __version__

    return {
        "schema": SCHEMA_VERSION,
        "install_id": install_id(),
        "sent_at": _now_iso(),
        "ltvm_version": __version__,
    }


def _post(payload: dict[str, Any]) -> bool:
    """POST the payload.  True if the server took it."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"ltvm/{payload.get('ltvm_version', '?')}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT) as resp:
            return bool(200 <= resp.status < 300)
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.debug("telemetry send failed: %s", e)
        return False


def send_now() -> bool:
    """Send one check-in and record the attempt.

    The stamp moves on *attempt*, not success.  Retrying a failed send
    is how a telemetry client turns into something that hammers a
    server through an outage; a missed week is just a missed week.
    """
    state = _load_state()
    ok = _post(_payload())
    state["last_send_iso"] = _now_iso()
    _save_state(state)
    return ok


# ---------------------------------------------------------------------------
# Notice
# ---------------------------------------------------------------------------


def _maybe_show_notice(cfg: dict[str, Any], state: dict[str, Any]) -> bool:
    """Print the first-run notice.  True if this run printed it.

    Printed regardless of whether anyone is watching: stderr survives
    redirection into a log, and a machine that only ever runs ltvm
    from CI has still been told.
    """
    if cfg.get("notice_shown"):
        return False
    print(NOTICE, file=sys.stderr)
    cfg["notice_shown"] = True
    _save_config(cfg)
    # Start the clock now, so the first real check-in is a week out.
    # That is the window in which `ltvm telemetry off` still means
    # nothing was ever sent.
    state["last_send_iso"] = _now_iso()
    _save_state(state)
    return True


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _spawn_detached_send() -> None:
    """Run the send in a detached child and return immediately.

    In-band would put a network round trip in front of the user's
    command.  update_check can afford that because it only ever runs
    with a human waiting; this runs on scripted invocations too, where
    even a couple of hundred milliseconds on every `ltvm list` is not
    ours to spend.

    `telemetry` is skipped by the hook in ltvm, so the child does not
    recurse into spawning another child.
    """
    script = Path(__file__).resolve().parent.parent / "ltvm"
    if not script.exists():
        return
    with contextlib.suppress(OSError, ValueError):
        subprocess.Popen(
            [sys.executable, str(script), "telemetry", "send", "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def maybe_send() -> None:
    """Top-level hook, called once per ltvm invocation."""
    if not is_enabled():
        return
    cfg = _load_config()
    state = _load_state()
    if _maybe_show_notice(cfg, state):
        # Told them this run; first send is a week from now.
        return
    uid = cfg.get("install_id") or install_id()
    if not _due_for_send(state, uid):
        return
    _spawn_detached_send()


def status() -> dict[str, Any]:
    """What `ltvm telemetry status` reports."""
    cfg = _load_config()
    state = _load_state()
    reason = None
    if os.environ.get("LTVM_TELEMETRY", "").strip() in ("0", "no", "false"):
        reason = "LTVM_TELEMETRY in the environment"
    elif _site_disabled():
        reason = f"{_SITE_CONFIG} (site-wide)"
    elif not cfg.get("enabled", True):
        reason = str(_CONFIG_FILE)
    return {
        "enabled": is_enabled(),
        "disabled_by": reason,
        "install_id": cfg.get("install_id"),
        "last_send": state.get("last_send_iso"),
        "endpoint": ENDPOINT,
        "interval_days": SEND_INTERVAL.days,
    }


def set_enabled(value: bool) -> None:
    cfg = _load_config()
    cfg["enabled"] = value
    # Someone turning it on explicitly has seen the notice by
    # definition; don't print it at them afterwards.
    cfg["notice_shown"] = True
    _save_config(cfg)


def preview() -> dict[str, Any]:
    """The exact payload a send would post.

    The point of this being a command is that "look at what leaves
    your machine" is checkable rather than promised.
    """
    return _payload()
