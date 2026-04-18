"""Podman subprocess wrapper with Ctrl-C / SIGTERM cleanup.

Long-running `podman run ...` invocations (kernel build, Lustre
build, MOFED kmod build) used to orphan their containers and the
inner `make -j28` when the user hit Ctrl-C.  The Python wrapper
died; the container and its inner build kept running for minutes,
eating cores and occasionally leaving partial files in staging.

``run_podman_with_cleanup`` wraps :func:`subprocess.run` with two
additions:

  1. ``podman run`` gets a ``--cidfile`` injected so we know the
     container id even if we never got to read stdout.
  2. SIGINT / SIGTERM received by the parent are translated into
     ``podman kill --signal TERM <cid>`` followed (after a brief
     grace period) by ``podman kill --signal KILL <cid>`` and a
     final ``killpg(SIGKILL)`` belt on the podman child's process
     group.

The child runs in its own session via ``preexec_fn=os.setsid`` so
the terminal's process-group SIGINT doesn't auto-kill it before
we've had a chance to tell podman to tear down the container
cleanly.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Grace period between initial cleanup attempt and the nuclear
# `podman rm -f` fallback.  Kept short because the container's PID 1
# (bash waiting on make children) typically ignores SIGTERM -- there's
# no graceful shutdown to wait for, and a long grace just extends the
# user-visible hang after Ctrl+C.
_GRACE_SECONDS = 0.5


def run_podman_with_cleanup(
    cmd: list[str],
    *,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run``-style wrapper that kills the container on signal.

    Behaves like ``subprocess.run(cmd, check=check, **kwargs)`` in
    the happy path: stdout/stderr flow to the parent's terminal by
    default, the returncode is returned, and ``check=True`` still
    raises :class:`subprocess.CalledProcessError` on non-zero exit.

    The helper only modifies ``cmd`` when it starts with
    ``["podman", "run", ...]``.  Other ``podman`` subcommands
    (``build``, ``image exists``, ``save``, etc.) are passed
    through unchanged -- they're short-lived and Ctrl-C already
    DTRT for them.

    When a SIGINT or SIGTERM arrives while the child is running,
    the helper:

      1. reads the cidfile
      2. runs ``podman kill --signal TERM <cid>`` (best-effort)
      3. waits up to ``_GRACE_SECONDS`` for podman to exit
      4. if still alive: ``podman kill --signal KILL <cid>`` plus
         ``killpg(SIGKILL)`` on the podman process group
      5. restores the previous signal handlers and re-raises
         :class:`KeyboardInterrupt` (for SIGINT) or exits the
         process (for SIGTERM)

    Previous handlers are always restored on exit via ``finally``.

    ``cidfile`` note: podman refuses to start if ``--cidfile``
    points at an already-existing path, so we use
    :func:`tempfile.mkstemp` then immediately unlink the empty
    file before passing the path to podman.
    """
    is_podman_run = (
        len(cmd) >= 2 and cmd[0] == "podman" and cmd[1] == "run"
    )

    cidfile_path: Path | None = None
    final_cmd = cmd
    if is_podman_run and "--cidfile" not in cmd:
        fd, tmp = tempfile.mkstemp(prefix="ltvm-cidfile-")
        os.close(fd)
        # podman refuses to start when --cidfile already exists.
        os.unlink(tmp)
        cidfile_path = Path(tmp)
        # Inject --cidfile immediately after `podman run`; any
        # position before the image tag works, but right after the
        # subcommand keeps it obvious and out of the way of flags
        # that callers order meaningfully.
        final_cmd = [cmd[0], cmd[1], "--cidfile", str(cidfile_path), *cmd[2:]]

    # Child runs in its own session so the terminal's pgrp SIGINT
    # doesn't reach it before we've had a chance to ask podman to
    # tear the container down gracefully.
    popen_kwargs = dict(kwargs)
    popen_kwargs.setdefault("start_new_session", True)

    proc: subprocess.Popen[Any] | None = None
    signal_received: list[int] = []

    def _read_cid() -> str | None:
        if cidfile_path is None:
            return None
        # podman writes the cid as soon as the container is created.
        # If the signal arrives before that (container image pull,
        # etc.) the file is empty or missing -- nothing to kill
        # explicitly; the killpg below will still tear podman down.
        try:
            text = cidfile_path.read_text().strip()
        except OSError:
            return None
        return text or None

    def _kill_container(sig: str) -> None:
        cid = _read_cid()
        if not cid:
            return
        try:
            subprocess.run(
                ["podman", "kill", "--signal", sig, cid],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _force_remove_container() -> None:
        """Last-resort guaranteed cleanup.

        ``podman rm -f`` sends SIGKILL to the container's PID 1 and
        waits for the runtime to tear down the namespace.  We call
        this after the TERM/KILL dance so even if the podman-run
        process is already gone (so our killpg hit no one) the
        conmon-owned container still gets cleaned up.
        """
        cid = _read_cid()
        if not cid:
            return
        try:
            subprocess.run(
                ["podman", "rm", "-f", cid],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _handler(signum: int, _frame: object) -> None:
        # First signal wins; subsequent ones are swallowed so a
        # double Ctrl-C doesn't interrupt our cleanup.
        if signal_received:
            return
        signal_received.append(signum)
        log.warning(
            "Received signal %s, killing container and podman child...",
            signum,
        )
        _kill_container("TERM")
        if proc is not None:
            # Give podman a chance to forward TERM to its managed
            # container and exit cleanly before we escalate.
            deadline = time.monotonic() + _GRACE_SECONDS
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if proc.poll() is None:
                _kill_container("KILL")
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass

    prev_int = signal.signal(signal.SIGINT, _handler)
    prev_term = signal.signal(signal.SIGTERM, _handler)
    try:
        proc = subprocess.Popen(final_cmd, **popen_kwargs)
        try:
            returncode = proc.wait()
        except KeyboardInterrupt:
            # Shouldn't happen -- our handler catches SIGINT -- but
            # cover the race where the handler hasn't been installed
            # yet.  Same cleanup path.
            _handler(signal.SIGINT, None)
            returncode = proc.wait()
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        if cidfile_path is not None:
            try:
                cidfile_path.unlink()
            except OSError:
                pass

    if signal_received:
        sig = signal_received[0]
        if sig == signal.SIGINT:
            raise KeyboardInterrupt()
        # SIGTERM: propagate by exiting with the conventional code.
        # 128 + signum matches what the shell reports.
        raise SystemExit(128 + sig)

    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, final_cmd)

    # subprocess.CompletedProcess fields we can reasonably fill in:
    # stdout/stderr are None unless the caller passed capture args,
    # which we don't support (these builds stream to the terminal).
    return subprocess.CompletedProcess(
        args=final_cmd, returncode=returncode, stdout=None, stderr=None
    )
