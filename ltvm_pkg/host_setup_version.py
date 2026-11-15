"""Which revision of the host setup ``ltvm install`` last applied.

``ltvm install`` makes host state -- groups, udev rules, services,
packages -- and a release that adds a step to it reaches no host that
is already installed: ``git pull`` runs nothing, and neither did ``sudo
ltvm update`` until it learned to rerun install.  So the code carries
``HOST_SETUP_VERSION``, a full install records the value it applied in
a root-owned file, and every ltvm command compares the two and says
once a day when the host is behind.

**Bump HOST_SETUP_VERSION whenever an install step is added or
changed** in a way an installed host needs to rerun.  A host with no
record was installed before this existed and counts as 0.

History:
  1: /dev/vhost-net for the ltvm group (PR #63), and the first record.
  2: the performance cpufreq governor, now and at boot (PR #66).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

HOST_SETUP_VERSION = 2

NOTICE_INTERVAL = timedelta(hours=24)

_LINUX_RECORD = Path("/var/lib/ltvm/host-setup.json")
# macOS has no /var/lib; ltvm's root-owned files live here already.
_MACOS_RECORD = Path("/Library/Application Support/ltvm/host-setup.json")

# Key in the per-user update-check state that throttles the notice.
_STATE_KEY = "last_host_setup_notice_iso"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def record_path() -> Path:
    env = os.environ.get("LTVM_HOST_SETUP_RECORD")
    if env:
        return Path(env)
    return _MACOS_RECORD if _is_macos() else _LINUX_RECORD


def install_command() -> str:
    """What to tell the user to run: install refuses root on macOS."""
    return "ltvm install" if _is_macos() else "sudo ltvm install"


def recorded_version() -> int:
    """The version the last full install applied; 0 if none is recorded."""
    try:
        data = json.loads(record_path().read_text())
    except (OSError, ValueError):
        return 0
    v = data.get("version") if isinstance(data, dict) else None
    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
        return v
    return 0


def is_stale(recorded: int | None = None) -> bool:
    if recorded is None:
        recorded = recorded_version()
    return recorded < HOST_SETUP_VERSION


def write_record() -> None:
    """Record HOST_SETUP_VERSION as applied, after a full install.

    On Linux install runs as root and writes directly; on macOS it runs
    as the user, and priv.atomic_write sudos for the write alone.  The
    file is outside the VM directory, so it stays root's either way.
    """
    from ltvm_pkg import __version__
    from ltvm_pkg.priv import atomic_write

    text = (
        json.dumps(
            {
                "version": HOST_SETUP_VERSION,
                "ltvm_version": __version__,
                "installed_iso": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n"
    )
    atomic_write(record_path(), text, mode=0o644)


def notice_text(recorded: int) -> str:
    return (
        f"ltvm: host setup is older than this ltvm "
        f"({recorded} < {HOST_SETUP_VERSION}). Run: {install_command()}"
    )


def maybe_notify() -> None:
    """Print the stale-host line on stderr, at most once a day per user.

    stderr, so ``--json`` output stays parseable, and regardless of a
    TTY: scripts and agents are the callers least likely to rerun
    install on their own.  The stamp lives in the update check's state
    file, which is per user and already written by every invocation.
    """
    recorded = recorded_version()
    if not is_stale(recorded):
        return
    from ltvm_pkg import update_check

    state = update_check._load_state()
    stamp = state.get(_STATE_KEY)
    if not update_check._elapsed_since(stamp, NOTICE_INTERVAL):
        return
    print(notice_text(recorded), file=sys.stderr)
    state[_STATE_KEY] = update_check._now_iso()
    with contextlib.suppress(Exception):
        update_check._save_state(state)


def doctor_report() -> dict[str, Any]:
    recorded = recorded_version()
    return {
        "recorded": recorded,
        "current": HOST_SETUP_VERSION,
        "stale": is_stale(recorded),
        "path": str(record_path()),
    }
