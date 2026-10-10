"""How long a build container may run before podman stops it.

The Lustre, ZFS and MOFED-kmod builds run under ``podman run
--timeout`` so that a wedged configure or lock loop fails instead of
blocking the command forever.  The limit has to allow for a slow
host, not a typical one: Lustre's was 600s, sized from a ~5 minute
clean rocky9 build, and a first build on a macOS podman machine
shared by several builds ran past it and died as ``Container build
failed (rc=255)`` with nothing saying why.

Precedence, highest first:

- ``LTVM_BUILD_TIMEOUT=<seconds>`` -- one user or one command;
- ``[build] timeout = <seconds>`` in /etc/ltvm.conf -- the host;
- the build's own default (``LUSTRE``, ``ZFS``, ``MOFED_KMODS``).

``0`` means no limit.  The kernel and image builds have none; they
legitimately run for an hour or more.
"""

from __future__ import annotations

import configparser
import os
import sys

from . import site_config

ENV = "LTVM_BUILD_TIMEOUT"

# Clean builds take ~5 min (Lustre), ~3-5 min (ZFS) and 10-15 min
# (MOFED kmods) on an idle host; these are several times that.
LUSTRE = 3600
ZFS = 1800
MOFED_KMODS = 1800

# Say what is wrong with a setting once per process, not once per build.
_warned: set[str] = set()


def _warn(problem: str) -> None:
    if problem not in _warned:
        _warned.add(problem)
        print(f"warning: {problem}", file=sys.stderr)


def _parse(raw: str) -> int | None:
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def seconds(default: int) -> int:
    """The limit, in seconds, for a build whose own default is *default*.

    A malformed setting warns once and falls through to the next one.
    """
    raw = os.environ.get(ENV, "").strip()
    if raw:
        value = _parse(raw)
        if value is not None:
            return value
        _warn(f"ignoring {ENV}={raw!r}: not a whole number of seconds >= 0")

    site = site_config.path()
    parser = configparser.ConfigParser()
    try:
        parser.read(site)
        raw = parser.get("build", "timeout", fallback="").strip()
    except configparser.Error as e:
        _warn(f"ignoring [build] in {site}: {e}")
        return default
    if raw:
        value = _parse(raw)
        if value is not None:
            return value
        _warn(
            f"ignoring [build] timeout in {site}: {raw!r} is not a whole "
            f"number of seconds >= 0"
        )
    return default


def podman_args(limit: int) -> list[str]:
    """``podman run`` arguments that apply *limit* (none for 0)."""
    return ["--timeout", str(limit)] if limit > 0 else []


def explain(limit: int, elapsed: float) -> str:
    """Why a failed build failed, when the limit is the likely reason.

    podman reports a container it stopped at ``--timeout`` only through
    its exit status (255 here), so go by the run time instead: a build
    that failed at or past the limit was stopped.  Empty otherwise.
    """
    if limit <= 0 or elapsed < limit:
        return ""
    return (
        f"the build container was stopped at its {limit}s limit; "
        f"raise it with {ENV}=<seconds> or [build] timeout in "
        f"{site_config.path()} (0 for no limit)"
    )
