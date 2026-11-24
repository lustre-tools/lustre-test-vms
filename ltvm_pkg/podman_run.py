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

import contextlib
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Iterator
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


# Grace period between initial cleanup attempt and the nuclear
# `podman rm -f` fallback.  Kept short because the container's PID 1
# (bash waiting on make children) typically ignores SIGTERM -- there's
# no graceful shutdown to wait for, and a long grace just extends the
# user-visible hang after Ctrl+C.
_GRACE_SECONDS = 0.5


# Kernel kbuild's worst observed fd usage is ~4k; 64k is a generous
# ceiling that stays under the rootless RLIMIT_NOFILE hard cap.
_NOFILE_WANT = 65536


def _nofile_limit() -> str:
    """Return the "soft:hard" --ulimit nofile value to pass to podman.

    Clamped to this process's own hard limit: crun cannot setrlimit
    above the limit it inherits, and asking for more fails the whole
    container with "OCI permission denied".
    """
    want = _NOFILE_WANT
    try:
        import resource

        hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
        if hard != resource.RLIM_INFINITY:
            want = min(want, hard)
    except (ImportError, OSError, ValueError):
        pass
    return f"{want}:{want}"


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
    is_podman_run = len(cmd) >= 2 and cmd[0] == "podman" and cmd[1] == "run"

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
            # comfortably below the rootless cap on macOS machines --
            # but the code asked for 524288, which is exactly the hard
            # cap systemd commonly sets, leaving no margin, and more
            # than it on hosts that set less.  Ask for 64k, and never
            # for more than this process can actually pass on.
            injected += ["--ulimit", f"nofile={_nofile_limit()}"]
        # A container writing into a group-shared artifacts tree must
        # leave what it makes group-writable (storage.py); podman's
        # own umask would not.
        try:
            from .storage import podman_share_args

            injected += podman_share_args(cmd)
        except Exception as e:  # noqa: BLE001
            log.debug("storage sharing check skipped: %s", e)
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

    def _force_remove_container() -> None:
        """Tear the container down through podman's own database.

        ``podman rm -f`` sends SIGKILL to the container's PID 1 and
        waits for the runtime to tear down the namespace, so it works
        even when the podman-run process is already gone and our killpg
        reached no one.  The signal handler below explains why this is
        the only step rather than the last of several.
        """
        cid = _read_cid()
        if not cid:
            return
        try:
            # --time 0 skips the default 10s stop-timeout: rm -f
            # SIGKILLs PID 1 itself, so there is no graceful shutdown
            # to wait on.
            subprocess.run(
                ["podman", "rm", "-f", "--time", "0", cid],
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
            "Received signal %s, tearing down container...",
            signum,
        )
        # `podman rm -f --time 0` is the only reliable cleanup:
        # conmon has typically daemonized into its own session by the
        # time we run, so `killpg` on our podman-run child doesn't
        # reach it; `podman kill --signal KILL` merely asks conmon to
        # SIGKILL PID 1, which can race with an in-flight runc action
        # and leave the container alive.  `rm -f` talks to podman's
        # own DB and forcibly tears everything down.
        _force_remove_container()
        if proc is not None:
            deadline = time.monotonic() + _GRACE_SECONDS
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if proc.poll() is None:
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
    cleanup_eof = returncode != 0 and _stderr_matches_cleanup_eof(tail_stderr)

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


def _any_foreign_owned(root: Path) -> bool:
    """True when anything under *root*, itself included, is not ours."""
    uid = os.geteuid()
    try:
        if os.lstat(root).st_uid != uid:
            return True
    except OSError:
        return False
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            try:
                if os.lstat(os.path.join(dirpath, name)).st_uid != uid:
                    return True
            except OSError:
                pass
    return False


def reclaim_ownership(root: Path) -> None:
    """Hand back what a rootless container left owned by a subuid.

    Container root is the invoking user, but a file it writes with
    another owner, as tar and ``rsync -a`` do when they keep a source
    tarball's uid, lands owned by one of the user's subordinate uids,
    and the user can then not delete entries in its directories.
    Inside ``podman unshare`` the user is uid 0, those files have their
    container uids, and other users' files are the overflow uid 65534.
    """
    # Read through a local: mypy treats a bare sys.platform test as
    # fixed for the platform it checks on, and on macOS calls the rest
    # of this function unreachable.
    host = sys.platform
    if host != "linux" or os.geteuid() == 0:
        return
    if not _any_foreign_owned(root):
        return
    cmd = [
        "podman",
        "unshare",
        "find",
        os.path.abspath(root),
        "!",
        "-uid",
        "0",
        "!",
        "-uid",
        "65534",
        "-exec",
        "chown",
        "-h",
        "0:0",
        "{}",
        "+",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        log.warning("Could not reclaim ownership under %s: %s", root, e)
        return
    if r.returncode != 0:
        log.warning(
            "Could not reclaim ownership under %s: %s", root, r.stderr.strip()
        )


def _walk_all(root: Path) -> Iterator[tuple[str, str]]:
    """(parent, path) for *root* and everything under it, top down."""
    top = os.path.abspath(root)
    yield os.path.dirname(top), top
    for dirpath, dirnames, filenames in os.walk(top):
        for name in dirnames + filenames:
            yield dirpath, os.path.join(dirpath, name)


@contextlib.contextmanager
def mapped_group(root: Path) -> Iterator[None]:
    """Give *root* the user's own group for a rootless container's run.

    Rootless podman maps only the user's primary gid (to 0) and the
    subgids, so in a setgid tree shared with another group (sookie's
    ltvm, gid 1002) every file a container creates takes a gid the
    container sees as 65534.  The kernel refuses chown of an inode whose
    gid is unmapped, so ``rsync -a`` keeping a tarball's owners failed
    with EPERM on each of ~89000 files.  The shared group cannot be
    mapped in: newgidmap allows only the subgid ranges and the user's
    own gid.

    So for the run, every entry under *root* gets the user's gid and
    the directories lose setgid; afterwards each entry still the same
    inode gets back its group and setgid bit, and one created meanwhile
    gets what a setgid parent would have given it.  Call
    ``reclaim_ownership`` inside, so that the restore owns what it
    changes.  Nothing is done unless something under *root* needs it.
    """
    host = sys.platform
    if host != "linux" or os.geteuid() == 0:
        yield
        return
    gid = os.getegid()
    saved: dict[str, tuple[int, int, bool]] = {}
    needed = False
    for _, path in _walk_all(root):
        try:
            st = os.lstat(path)
        except OSError:
            continue
        setgid = stat.S_ISDIR(st.st_mode) and bool(st.st_mode & stat.S_ISGID)
        saved[path] = (st.st_ino, st.st_gid, setgid)
        needed = needed or setgid or st.st_gid != gid
    if not needed:
        yield
        return
    log.info("Giving %s group %d for the container's run", root, gid)
    failed = 0
    for path, (_, old_gid, setgid) in saved.items():
        try:
            if old_gid != gid:
                os.lchown(path, -1, gid)
            if setgid:
                os.chmod(path, os.lstat(path).st_mode & ~stat.S_ISGID & 0o7777)
        except OSError:
            failed += 1
    if failed:
        log.warning(
            "Could not change the group of %d entries under %s; a chown "
            "in the container may fail on them",
            failed,
            root,
        )
    try:
        yield
    finally:
        _restore_group(root, saved)


def _restore_group(root: Path, saved: dict[str, tuple[int, int, bool]]) -> None:
    """Undo ``mapped_group``, as a setgid tree would have it."""
    # Directory -> (gid, setgid) as restored, for its new children.
    state: dict[str, tuple[int, bool]] = {}
    failed = 0
    for parent, path in _walk_all(root):
        try:
            st = os.lstat(path)
        except OSError:
            continue
        rec = saved.get(path)
        if rec is not None and rec[0] == st.st_ino:
            want_gid, setgid = rec[1], rec[2]
        elif parent in state and state[parent][1]:
            want_gid = state[parent][0]
            setgid = stat.S_ISDIR(st.st_mode)
        else:
            want_gid, setgid = st.st_gid, False
        if stat.S_ISDIR(st.st_mode):
            state[path] = (want_gid, setgid)
        try:
            if st.st_gid != want_gid:
                os.lchown(path, -1, want_gid)
            if setgid:
                os.chmod(path, (os.lstat(path).st_mode | stat.S_ISGID) & 0o7777)
        except OSError:
            failed += 1
    if failed:
        log.warning(
            "Could not give back the shared group of %d entries under %s",
            failed,
            root,
        )


def rmtree(path: Path) -> None:
    """shutil.rmtree, reclaiming subuid-owned entries when refused."""
    try:
        shutil.rmtree(path)
    except PermissionError:
        reclaim_ownership(path)
        shutil.rmtree(path)
