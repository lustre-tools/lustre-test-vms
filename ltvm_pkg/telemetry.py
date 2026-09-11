"""ltvm usage telemetry -- an anonymous weekly check-in.

Sends a random install ID, the ltvm version, a description of the
host, and counts of which commands and targets were used to
https://ltvm.mulberrytree.cc once a week, so the people maintaining
ltvm can tell whether anyone is using it and which parts they use.
Opt-out, with a notice on first run.

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
names, Lustre tree identity, git branches and IP addresses are not on
it and must not be added -- the rule of thumb is that a week of the
server's table should be safe to paste into a public ticket.  (The
server records a *hash* of the source IP so distinct networks can be
counted; the address itself is never stored.)

Two specific things that stay off it.  Failure *reasons*: commands
carry an ok/fail count and nothing more, because a reason is a string
built at an error site and error sites are where paths live.  And
names from outside the shipped target and variant lists, which are
replaced with "other" -- a target someone added themselves identifies
their site far better than an install ID does.

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
SCHEMA_VERSION = 2
# Every counter key is drawn from a closed set already (our own command
# function names, the shipped target list, a fixed option list), so this
# is a backstop against a bug rather than against a user.
MAX_COUNTER_KEYS = 64
# Variants ship with the targets; anything else is somebody's local
# experiment and its name is not ours to send.
KNOWN_VARIANTS = frozenset({"base", "mofed"})
SEND_INTERVAL = timedelta(days=7)
# Long enough that a stalled network never delays the child past the
# point of being noticed; short enough to cross an ocean.
SEND_TIMEOUT = 5

# Resolved per call rather than at import.  As module constants these
# were fixed by whatever the environment happened to be when the module
# was first imported, which made them untestable without reloading the
# module -- and a reload silently undoes any patching a test has done.
# That is not a theoretical tidiness point: it let the test suite write
# and very nearly send the developer's real counters as usage data.


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "ltvm"


def _config_file() -> Path:
    return _config_dir() / "config.json"


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "ltvm"


def _state_file() -> Path:
    return _state_dir() / "telemetry.json"


def _counters_file() -> Path:
    return _state_dir() / "counters.json"


def _site_config() -> Path:
    return Path(os.environ.get("LTVM_SITE_CONFIG") or "/etc/ltvm.conf")


NOTICE = """\
ltvm sends anonymous usage telemetry once per week, so developers can
know what usage is and where to focus improvements.
  ltvm telemetry show   what it sends      ltvm telemetry off   stop it
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
        data = json.loads(_config_file().read_text())
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
        loaded = json.loads(_config_file().read_text())
        if isinstance(loaded, dict):
            data = loaded
    data["telemetry"] = block
    try:
        _config_dir().mkdir(parents=True, exist_ok=True)
        _config_file().write_text(json.dumps(data, indent=2) + "\n")
    except OSError as e:
        log.debug("cannot write %s: %s", _config_file(), e)
        return
    from .priv import chown_to_invoking_user

    chown_to_invoking_user(_config_dir())
    chown_to_invoking_user(_config_file())


def _load_state() -> dict[str, Any]:
    out: dict[str, Any] = {"last_send_iso": None}
    try:
        data = json.loads(_state_file().read_text())
    except (OSError, json.JSONDecodeError):
        return out
    if isinstance(data, dict):
        out.update(data)
    return out


def _write_json_state(path: Path, data: dict[str, Any]) -> None:
    """Write one state file atomically, and never raise.

    Both state files are written by root invocations too (`sudo ltvm
    cluster create`), so both carry the chown against the
    root-owned-file trap update_check._save_config documents.  The
    counters file gets written after *every* command, which is what
    makes that trap a certainty here rather than a possibility.

    tempfile + replace because two ltvm processes can be running at
    once and a half-written file would read back as corrupt.
    """
    try:
        _state_dir().mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(_state_dir()), prefix=f".{path.name}."
        )
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(data, indent=2) + "\n")
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as e:
        log.debug("cannot write %s: %s", path, e)
        return
    from .priv import chown_to_invoking_user

    chown_to_invoking_user(_state_dir())
    chown_to_invoking_user(path)


def _save_state(state: dict[str, Any]) -> None:
    _write_json_state(_state_file(), state)


def _site_disabled() -> bool:
    """True if /etc/ltvm.conf turns telemetry off for the whole host.

    The site file can only ever disable.  It exists so an admin can
    opt a shared lab out in one place, and an opt-out a user could
    silently undo would not be one.
    """
    site = _site_config()
    if not site.is_file():
        return False
    parser = configparser.ConfigParser()
    try:
        parser.read(site)
        return not parser.getboolean("telemetry", "enabled", fallback=True)
    except (configparser.Error, ValueError, OSError) as e:
        log.debug("ignoring unreadable %s: %s", site, e)
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
# Host facts (sampled at send time)
# ---------------------------------------------------------------------------


def _bucket(value: int | None, edges: tuple[int, ...]) -> str | None:
    """Put a count in a bucket rather than reporting it exactly.

    With a population this small, an exact number next to a country and
    a version starts to identify a machine, and "can the default VM
    size go up" is a bucket-shaped question anyway.
    """
    if value is None or value < 0:
        return None
    lo = 0
    for edge in edges:
        if value < edge:
            return f"{lo}-{edge - 1}" if lo else f"<{edge}"
        lo = edge
    return f"{lo}+"


def _host_os() -> tuple[str | None, str | None]:
    """(id, version) for the host OS."""
    import platform

    if platform.system() == "Darwin":
        return "macos", (platform.mac_ver()[0] or None)
    fields = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k] = v.strip().strip('"')
    except OSError:
        return None, None
    return fields.get("ID"), fields.get("VERSION_ID")


def _ram_gb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // (1024 * 1024)
    except (OSError, ValueError, IndexError):
        pass
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return int(out.stdout.strip()) // (1024**3)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _qemu_version() -> str | None:
    import platform
    import shutil

    arch = "aarch64" if platform.machine() in ("aarch64", "arm64") else "x86_64"
    binary = shutil.which(f"qemu-system-{arch}")
    if not binary:
        return None
    try:
        out = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=5
        )
    except (subprocess.SubprocessError, OSError):
        return None
    # "QEMU emulator version 8.2.2 (...)" -- take the bare version.
    for word in out.stdout.split():
        if word[:1].isdigit():
            return word[:MAX_STR_LEN]
    return None


def _host_info() -> dict[str, Any]:
    """What the machine is -- sampled now, not accumulated.

    Answers the questions that decide what ltvm has to keep working:
    whether anyone is on macOS or aarch64, how much of the population
    is WSL (which changes networking, clocks and filesystem behaviour
    enough to be a different product), and whether the Python floor
    can move.

    Deliberately absent: the container runtime, because ltvm only ever
    drives podman, so the answer is known before asking; and the CPU
    count, which nothing was going to be decided on.  RAM stays --
    default VM sizing is a real question.
    """
    import platform

    os_id, os_version = _host_os()
    try:
        from .host_setup import is_wsl2

        wsl = bool(is_wsl2())
    except Exception:  # noqa: BLE001
        wsl = False
    machine = platform.machine()
    return {
        "os": os_id,
        "os_version": os_version,
        "arch": "aarch64" if machine in ("aarch64", "arm64") else machine,
        "wsl": wsl,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "qemu": _qemu_version(),
        "ram_gb": _bucket(_ram_gb(), (8, 16, 32, 64, 128)),
    }


# ---------------------------------------------------------------------------
# Counters (accumulated between sends)
# ---------------------------------------------------------------------------

MAX_STR_LEN = 64
_TARGETS_CACHE: frozenset[str] | None = None


def _known_targets() -> frozenset[str]:
    """The targets this checkout ships, as an allowlist.

    A target someone added themselves is named something we have no
    business transmitting -- `acme-internal` identifies a site far
    better than an install ID does.  Unknown names become "other".
    """
    global _TARGETS_CACHE
    if _TARGETS_CACHE is None:
        try:
            root = Path(__file__).resolve().parent.parent / "targets"
            _TARGETS_CACHE = frozenset(
                d.name
                for d in root.iterdir()
                if d.is_dir() and d.name != "common"
            )
        except OSError:
            _TARGETS_CACHE = frozenset()
    return _TARGETS_CACHE


def _empty_counters() -> dict[str, Any]:
    return {
        "since": _now_iso(),
        "targets": {},
        "commands": {},
        "options": {},
    }


def _load_counters() -> dict[str, Any]:
    out = _empty_counters()
    try:
        data = json.loads(_counters_file().read_text())
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(data, dict):
        return out
    for key in ("targets", "commands", "options"):
        if isinstance(data.get(key), dict):
            out[key] = data[key]
    if isinstance(data.get("since"), str):
        out["since"] = data["since"]
    return out


def _save_counters(counters: dict[str, Any]) -> None:
    _write_json_state(_counters_file(), counters)


def _command_label(args: Any) -> str | None:
    """A stable name for the command that just ran.

    Taken from the handler's own function name rather than reassembled
    from argparse dests: every subparser sets `func`, the names are
    ours rather than the user's, and `cmd_build_lustre` already
    distinguishes itself from `cmd_build_kernel` without us
    maintaining a mapping.
    """
    fn = getattr(args, "func", None)
    name = getattr(fn, "__name__", "")
    if not isinstance(name, str) or not name.startswith("cmd_"):
        return None
    label = name[4:]
    if not label:
        return None
    # Most handlers are named for their full command path already
    # (cmd_build_lustre), but a few shared ones are not: `build status`
    # is cmd_status, which on a dashboard reads as nothing in
    # particular.  Prefix the group when the name does not carry it.
    group = str(getattr(args, "command", "") or "").replace("-", "_")
    if group and not label.startswith(group):
        label = f"{group}.{label}"
    return label[:MAX_STR_LEN]


def _bump(table: dict[str, Any], key: str, amount: int = 1) -> None:
    if key not in table and len(table) >= MAX_COUNTER_KEYS:
        return
    table[key] = int(table.get(key, 0)) + amount


def record(args: Any, rc: int) -> None:
    """Note one finished command.  Called once, from ltvm's main().

    Counts are usage plus a bare ok/fail split.  The *reason* a command
    failed is deliberately not collected: a reason is a string built at
    an error site, which is where paths and tree names live.  A rate
    tells us where to look; people can file bugs for the rest.

    A single instrumentation point: all of this is already on `args`
    and `rc` by the time the command returns, so nothing has to be
    threaded through the rest of the codebase.
    """
    if not is_enabled():
        return
    counters = _load_counters()

    label = _command_label(args)
    if label:
        commands = counters["commands"]
        if label in commands or len(commands) < MAX_COUNTER_KEYS:
            entry = commands.get(label)
            if not isinstance(entry, dict):
                # Repair rather than skip.  A counters file written by
                # another ltvm version can hold a different shape here,
                # and silently declining to count it would mean this
                # command never being counted again.
                entry = {"ok": 0, "fail": 0}
                commands[label] = entry
            key = "ok" if rc == 0 else "fail"
            entry[key] = int(entry.get(key, 0)) + 1

    target = getattr(args, "target", None)
    if isinstance(target, str) and target:
        known = target if target in _known_targets() else "other"
        _bump(counters["targets"], known)

    if getattr(args, "zfs", False):
        _bump(counters["options"], "zfs")
    variant = getattr(args, "variant", None)
    if isinstance(variant, str) and variant and variant != "base":
        name = variant if variant in KNOWN_VARIANTS else "other"
        _bump(counters["options"], f"variant:{name}")

    _save_counters(counters)


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def _payload() -> dict[str, Any]:
    """Everything that leaves the machine.

    If you are adding a field here, it belongs on the list in this
    module's docstring first.
    """
    from . import __version__

    counters = _load_counters()
    return {
        "schema": SCHEMA_VERSION,
        "install_id": install_id(),
        "sent_at": _now_iso(),
        "ltvm_version": __version__,
        "host": _host_info(),
        "since": counters["since"],
        "targets": counters["targets"],
        "commands": counters["commands"],
        "options": counters["options"],
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
    if ok:
        # Only on success: a failed send keeps the counters, so the
        # next one carries two weeks rather than losing one.  `since`
        # says which, so the server is never guessing at the span.
        _save_counters(_empty_counters())
    return ok


# ---------------------------------------------------------------------------
# Notice
# ---------------------------------------------------------------------------


def _maybe_show_notice(cfg: dict[str, Any]) -> None:
    """Print the first-run notice, once.

    Printed regardless of whether anyone is watching: stderr survives
    redirection into a log, and a machine that only ever runs ltvm from
    CI has still been told.

    It does not gate the send.  This is opt-out: the first check-in
    goes out on the first run, alongside the notice.  Holding it back
    for a week would be the behaviour of an opt-in scheme, and would
    lose every install that gets used for a few days and dropped --
    which is exactly the population "how many people use this" is
    asking about.
    """
    if cfg.get("notice_shown"):
        return
    print(NOTICE, file=sys.stderr)
    cfg["notice_shown"] = True
    _save_config(cfg)


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
    _maybe_show_notice(cfg)
    uid = cfg.get("install_id") or install_id()
    if not _due_for_send(state, uid):
        return
    # Mark the attempt here rather than leaving it to the child.  The
    # child is what actually POSTs, and it records the attempt too --
    # but it takes a moment to start, and until it does the clock has
    # not moved.  A script running ltvm in a loop would spawn a sender
    # per invocation in that window.
    state["last_send_iso"] = _now_iso()
    _save_state(state)
    _spawn_detached_send()


def status() -> dict[str, Any]:
    """What `ltvm telemetry status` reports."""
    cfg = _load_config()
    state = _load_state()
    reason = None
    if os.environ.get("LTVM_TELEMETRY", "").strip() in ("0", "no", "false"):
        reason = "LTVM_TELEMETRY in the environment"
    elif _site_disabled():
        reason = f"{_site_config()} (site-wide)"
    elif not cfg.get("enabled", True):
        reason = str(_config_file())
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
