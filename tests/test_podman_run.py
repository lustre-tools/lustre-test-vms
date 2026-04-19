"""Tests for run_podman_with_cleanup.

The helper is exercised end-to-end by all three build-site tests
(kernel/lustre/mofed), but those mock podman out entirely.  These
tests drive the signal-handling path directly using a plain
command in place of `podman run`, confirming:

  - happy path returns a CompletedProcess with the right returncode
  - SIGINT while the child runs kills the child and raises
    KeyboardInterrupt in the caller
  - cidfile is injected exactly once, only for ``podman run``
    invocations
  - other podman subcommands are passed through unchanged
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ltvm_pkg.podman_run import (
    _stderr_matches_cleanup_eof,
    run_podman_with_cleanup,
)


class TestHappyPath:
    def test_returncode_propagates(self) -> None:
        r = run_podman_with_cleanup(["true"])
        assert r.returncode == 0

    def test_nonzero_returncode_without_check(self) -> None:
        r = run_podman_with_cleanup(["false"])
        assert r.returncode != 0

    def test_check_true_raises_on_nonzero(self) -> None:
        with pytest.raises(subprocess.CalledProcessError):
            run_podman_with_cleanup(["false"], check=True)


class TestCidfileInjection:
    def test_podman_run_gets_cidfile(self) -> None:
        captured: list[list[str]] = []

        class FakeProc:
            pid = 12345

            def wait(self) -> int:  # pragma: no cover - trivial
                return 0

            def poll(self) -> int | None:  # pragma: no cover
                return 0

        def fake_popen(cmd, **_kwargs):
            captured.append(list(cmd))
            return FakeProc()

        with patch("ltvm_pkg.podman_run.subprocess.Popen", side_effect=fake_popen):
            run_podman_with_cleanup(["podman", "run", "--rm", "busybox", "true"])

        assert len(captured) == 1
        cmd = captured[0]
        assert "--cidfile" in cmd
        idx = cmd.index("--cidfile")
        # --cidfile lands right after `podman run`
        assert cmd[:2] == ["podman", "run"]
        assert idx == 2
        # downstream args are preserved in order; --ulimit is injected
        # between --cidfile and the original tail
        assert cmd[-3:] == ["--rm", "busybox", "true"]
        assert "--ulimit" in cmd
        assert cmd[cmd.index("--ulimit") + 1] == "nofile=65536:65536"

    def test_non_run_subcommand_passthrough(self) -> None:
        captured: list[list[str]] = []

        class FakeProc:
            pid = 12345

            def wait(self) -> int:
                return 0

            def poll(self) -> int | None:
                return 0

        def fake_popen(cmd, **_kwargs):
            captured.append(list(cmd))
            return FakeProc()

        with patch("ltvm_pkg.podman_run.subprocess.Popen", side_effect=fake_popen):
            run_podman_with_cleanup(["podman", "build", "-t", "foo", "."])

        assert captured[0] == ["podman", "build", "-t", "foo", "."]

    def test_preexisting_cidfile_not_added(self) -> None:
        captured: list[list[str]] = []

        class FakeProc:
            pid = 12345

            def wait(self) -> int:
                return 0

            def poll(self) -> int | None:
                return 0

        def fake_popen(cmd, **_kwargs):
            captured.append(list(cmd))
            return FakeProc()

        with patch("ltvm_pkg.podman_run.subprocess.Popen", side_effect=fake_popen):
            run_podman_with_cleanup(
                ["podman", "run", "--cidfile", "/tmp/explicit", "busybox"]
            )

        # Exactly one --cidfile (the caller's), not two.
        assert captured[0].count("--cidfile") == 1


class TestSignalHandling:
    """Drive the real signal path with a plain sleep command.

    `podman run` isn't available in CI so we treat `sleep` as the
    child.  The helper still installs SIGINT/SIGTERM handlers and
    will try to run `podman kill ...` -- that subprocess call
    fails fast because the cidfile will be empty (we never ran
    podman), which is the same code path as "signal arrived
    before the container was created".  The escalation path
    (killpg SIGKILL) is what actually kills the sleep child.
    """

    def test_sigint_kills_child_and_raises(self) -> None:
        def send_sigint() -> None:
            time.sleep(0.2)
            os.kill(os.getpid(), signal.SIGINT)

        t = threading.Thread(target=send_sigint)
        t.start()
        try:
            with pytest.raises(KeyboardInterrupt):
                run_podman_with_cleanup(["sleep", "30"])
        finally:
            t.join()


class TestCleanupEofDetection:
    """Pattern matcher for podman-machine cleanup-EOF symptom.

    The tee/ring-buffer machinery in run_podman_with_cleanup is
    exercised indirectly by the build-site tests (kernel/lustre/
    mofed); here we just verify the standalone pattern matcher and
    the post-hoc flagging on CompletedProcess.
    """

    def test_removing_container_eof_matches(self) -> None:
        stderr = (
            "=== Kernel build complete ===\n"
            "ERRO[1569] Removing container 428f5: "
            "Delete 'http://d/v5.8.2/libpod/containers/428f5?"
            "force=false&volumes=true': EOF\n"
        )
        assert _stderr_matches_cleanup_eof(stderr)

    def test_wait_for_container_eof_matches(self) -> None:
        stderr = (
            "Error: wait for container: "
            "Post 'http://d/v5.8.2/libpod/containers/x/wait': EOF\n"
        )
        assert _stderr_matches_cleanup_eof(stderr)

    def test_plain_error_does_not_match(self) -> None:
        assert not _stderr_matches_cleanup_eof("make: *** [vmlinux] Error 1")

    def test_eof_without_container_context_does_not_match(self) -> None:
        assert not _stderr_matches_cleanup_eof("unexpected EOF in stream")

    def test_empty_stderr_does_not_match(self) -> None:
        assert not _stderr_matches_cleanup_eof("")

    def test_nonzero_with_cleanup_eof_suppresses_check(
        self, tmp_path: Path
    ) -> None:
        eof = (
            "ERRO[1569] Removing container 428f5: Delete "
            "'http://d/v5.8.2/libpod/containers/428f5?"
            "force=false&volumes=true': EOF\n"
            "Error: wait for container: Post "
            "'http://d/.../wait': EOF\n"
        )
        script = tmp_path / "run.sh"
        script.write_text(
            "#!/bin/sh\n"
            f"printf '%s' {repr(eof)} >&2\n"
            "exit 126\n"
        )
        script.chmod(0o755)

        # check=True should NOT raise -- cleanup_eof suppresses it.
        r = run_podman_with_cleanup([str(script)], check=True)
        assert r.returncode == 126
        assert getattr(r, "cleanup_eof", False) is True
        assert "EOF" in (r.stderr or "")

    def test_nonzero_without_cleanup_eof_still_raises(
        self, tmp_path: Path
    ) -> None:
        script = tmp_path / "run.sh"
        script.write_text(
            "#!/bin/sh\n"
            "echo 'make: *** [vmlinux] Error 1' >&2\n"
            "exit 2\n"
        )
        script.chmod(0o755)

        with pytest.raises(subprocess.CalledProcessError):
            run_podman_with_cleanup([str(script)], check=True)

    def test_success_has_no_cleanup_eof(self) -> None:
        r = run_podman_with_cleanup(["true"])
        assert r.returncode == 0
        assert getattr(r, "cleanup_eof", False) is False
