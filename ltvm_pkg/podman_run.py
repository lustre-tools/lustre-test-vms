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
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# podman machine on macOS occasionally drops its socket while tearing
# down a container that `podman run --rm` was waiting on.  The inner
# workload exits 0, artifacts are on disk, and then podman's own
# removal / wait API call hits EOF and the outer command returns
# non-zero (126).  We detect that shape post-hoc and let the caller
# decide whether to treat it as success.
_CLEANUP_EOF_MARKERS = (
    re.compile(r"Removing container.*EOF", re.IGNORECASE),
    re.compile(r"wait for container.*EOF", re.IGNORECASE),
)


def _stderr_matches_cleanup_eof(stderr: str) -> bool:
    """True when *stderr* looks like a podman cleanup EOF symptom.

    Matches when the text contains 'EOF' alongside either
    'Removing container' or 'wait for container'.  Robust to minor
    message drift across podman versions.
    """
    if not stderr or "EOF" not in stderr:
        return False
    return any(m.search(stderr) for m in _CLEANUP_EOF_MARKERS)

# Grace period between `podman kill --signal TERM` and the KILL
# escalation.  2s is plenty for podman to forward TERM to the
# container's init and for the container runtime to tear down.
_GRACE_SECONDS = 2.0


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
    if is_podman_run:
        injected: list[str] = []
        if "--cidfile" not in cmd:
            fd, tmp = tempfile.mkstemp(prefix="ltvm-cidfile-")
            os.close(fd)
            # podman refuses to start when --cidfile already exists.
            os.unlink(tmp)
            cidfile_path = Path(tmp)
            injected += ["--cidfile", str(cidfile_path)]
        if not any(c == "--ulimit" or c.startswith("--ulimit=") for c in cmd):
            # Kernel kbuild opens thousands of file descriptors walking
            # Kconfig / generated headers; podman machine on macOS
            # defaults to ~1024 and the build aborts with "Too many
            # open files.  Stop.".  Keep the bump modest so it stays
            # within rootless podman's RLIMIT_NOFILE hard cap -- raising
            # beyond that fails with "OCI permission denied" (crun
            # can't setrlimit above the container's inherited hard
            # limit).  64k is ~16x the worst observed kbuild usage and
            # comfortably below the rootless cap on macOS machines.
            injected += ["--ulimit", "nofile=65536:65536"]
        if injected:
            final_cmd = [cmd[0], cmd[1], *injected, *cmd[2:]]

    # Child runs in its own session so the terminal's pgrp SIGINT
    # doesn't reach it before we've had a chance to ask podman to
    # tear the container down gracefully.
    popen_kwargs = dict(kwargs)
    popen_kwargs.setdefault("start_new_session", True)

    # Tee stderr into a bounded ring buffer so we can inspect it
    # post-hoc for the podman-machine cleanup-EOF symptom (exit 126
    # after the inner workload succeeded).  Only enabled when the
    # caller hasn't asked for their own stderr handling.
    tee_thread: threading.Thread | None = None
    tee_buffer: deque[str] | None = None
    if "stderr" not in popen_kwargs and "capture_output" not in popen_kwargs:
        popen_kwargs["stderr"] = subprocess.PIPE
        tee_buffer = deque(maxlen=200)

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
        proc_stderr = getattr(proc, "stderr", None)
        if tee_buffer is not None and proc_stderr is not None:
            tee_thread = threading.Thread(
                target=_tee_stderr,
                args=(proc_stderr, sys.stderr, tee_buffer),
                daemon=True,
            )
            tee_thread.start()
        try:
            returncode = proc.wait()
        except KeyboardInterrupt:
            # Shouldn't happen -- our handler catches SIGINT -- but
            # cover the race where the handler hasn't been installed
            # yet.  Same cleanup path.
            _handler(signal.SIGINT, None)
            returncode = proc.wait()
        if tee_thread is not None:
            tee_thread.join(timeout=1.0)
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

    tail_stderr = "".join(tee_buffer) if tee_buffer is not None else ""
    cleanup_eof = (
        returncode != 0 and _stderr_matches_cleanup_eof(tail_stderr)
    )

    # cleanup_eof suppresses check=True's CalledProcessError -- callers
    # must inspect `result.cleanup_eof` and verify expected artifacts
    # exist before treating it as success.
    if check and returncode != 0 and not cleanup_eof:
        err = subprocess.CalledProcessError(returncode, final_cmd)
        err.stderr = tail_stderr
        raise err

    result = subprocess.CompletedProcess(
        args=final_cmd,
        returncode=returncode,
        stdout=None,
        stderr=tail_stderr if tee_buffer is not None else None,
    )
    result.cleanup_eof = cleanup_eof  # type: ignore[attr-defined]
    return result


def _tee_stderr(
    src: Any,
    dst: Any,
    ring: deque[str],
) -> None:
    """Copy *src* to *dst* while recording lines into *ring*."""
    try:
        for raw in iter(src.readline, b""):
            try:
                line = raw.decode("utf-8", errors="replace")
            except AttributeError:
                line = raw
            ring.append(line)
            try:
                dst.write(line)
                dst.flush()
            except (OSError, ValueError):
                pass
    except (OSError, ValueError):
        pass
    finally:
        try:
            src.close()
        except OSError:
            pass
