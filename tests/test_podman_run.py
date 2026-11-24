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
import resource
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ltvm_pkg import podman_run
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

        with patch(
            "ltvm_pkg.podman_run.subprocess.Popen", side_effect=fake_popen
        ):
            run_podman_with_cleanup(
                ["podman", "run", "--rm", "busybox", "true"]
            )

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
        # 64k, clamped to this process's own RLIMIT_NOFILE hard cap:
        # crun cannot setrlimit above the limit it inherits, so asking
        # for more fails the container with "OCI permission denied".
        ulimit = cmd[cmd.index("--ulimit") + 1]
        soft, _, hard = ulimit.removeprefix("nofile=").partition(":")
        assert soft == hard
        assert 0 < int(soft) <= 65536
        assert int(soft) <= resource.getrlimit(resource.RLIMIT_NOFILE)[1]

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

        with patch(
            "ltvm_pkg.podman_run.subprocess.Popen", side_effect=fake_popen
        ):
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

        with patch(
            "ltvm_pkg.podman_run.subprocess.Popen", side_effect=fake_popen
        ):
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
        script.write_text(f"#!/bin/sh\nprintf '%s' {repr(eof)} >&2\nexit 126\n")
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
            "#!/bin/sh\necho 'make: *** [vmlinux] Error 1' >&2\nexit 2\n"
        )
        script.chmod(0o755)

        with pytest.raises(subprocess.CalledProcessError):
            run_podman_with_cleanup([str(script)], check=True)

    def test_success_has_no_cleanup_eof(self) -> None:
        r = run_podman_with_cleanup(["true"])
        assert r.returncode == 0
        assert getattr(r, "cleanup_eof", False) is False


class TestReclaimOwnership:
    """Subuid-owned entries a rootless container left go back to the user."""

    def _tree(self, tmp_path: Path) -> Path:
        root = tmp_path / "build-tree"
        (root / "init").mkdir(parents=True)
        (root / "init" / "utsversion-tmp.h").write_text("x")
        (root / "link").symlink_to("init")
        return root

    def test_any_foreign_owned(self, tmp_path: Path) -> None:
        root = self._tree(tmp_path)
        assert not podman_run._any_foreign_owned(root)
        assert not podman_run._any_foreign_owned(tmp_path / "gone")
        real = os.lstat
        leaf = str(root / "init" / "utsversion-tmp.h")

        def subuid_leaf(p: str | Path) -> os.stat_result:
            st = real(p)
            if str(p) != leaf:
                return st
            fields = list(st)
            fields[4] = 102000
            return os.stat_result(fields)

        with patch.object(podman_run.os, "lstat", side_effect=subuid_leaf):
            assert podman_run._any_foreign_owned(root)

    def test_nothing_foreign_runs_nothing(self, tmp_path: Path) -> None:
        root = self._tree(tmp_path)
        with (
            patch.object(podman_run.sys, "platform", "linux"),
            patch.object(podman_run.subprocess, "run") as run,
        ):
            podman_run.reclaim_ownership(root)
        run.assert_not_called()

    def test_chowns_subuid_entries_under_unshare(self, tmp_path: Path) -> None:
        ok = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch.object(podman_run.sys, "platform", "linux"),
            patch.object(podman_run.os, "geteuid", return_value=1000),
            patch.object(podman_run, "_any_foreign_owned", return_value=True),
            patch.object(podman_run.subprocess, "run", return_value=ok) as run,
        ):
            podman_run.reclaim_ownership(tmp_path)
        cmd = run.call_args.args[0]
        assert cmd[:4] == ["podman", "unshare", "find", str(tmp_path)]
        assert cmd[4:] == [
            "!", "-uid", "0", "!", "-uid", "65534",
            "-exec", "chown", "-h", "0:0", "{}", "+",
        ]  # fmt: skip

    def test_failure_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad = subprocess.CompletedProcess([], 1, "", "no subuid mapping")
        with (
            patch.object(podman_run.sys, "platform", "linux"),
            patch.object(podman_run.os, "geteuid", return_value=1000),
            patch.object(podman_run, "_any_foreign_owned", return_value=True),
            patch.object(podman_run.subprocess, "run", return_value=bad),
        ):
            podman_run.reclaim_ownership(tmp_path)
        assert "no subuid mapping" in caplog.text

    @pytest.mark.parametrize(
        ("platform", "euid"), [("darwin", 501), ("linux", 0)]
    )
    def test_skipped_off_linux_and_as_root(
        self, tmp_path: Path, platform: str, euid: int
    ) -> None:
        with (
            patch.object(podman_run.sys, "platform", platform),
            patch.object(podman_run.os, "geteuid", return_value=euid),
            patch.object(podman_run, "_any_foreign_owned") as walk,
            patch.object(podman_run.subprocess, "run") as run,
        ):
            podman_run.reclaim_ownership(tmp_path)
        walk.assert_not_called()
        run.assert_not_called()


def _other_group() -> int:
    others = [g for g in os.getgroups() if g != os.getegid()]
    if not others:
        pytest.skip("needs a supplementary group")
    return others[0]


class TestMappedGroup:
    """A setgid tree shared with an unmapped group, around a container."""

    def _tree(self, tmp_path: Path, shared: int) -> Path:
        root = tmp_path / "kernel"
        (root / "build-tree").mkdir(parents=True)
        (root / "build-tree" / "Makefile").write_text("x")
        (root / "keep").write_text("x")
        for p in (root, root / "build-tree", root / "build-tree" / "Makefile",
                  root / "keep"):  # fmt: skip
            os.chown(p, -1, shared)
        for d in (root, root / "build-tree"):
            os.chmod(d, 0o2775)
        return root

    def test_user_group_during_run_shared_group_after(
        self, tmp_path: Path
    ) -> None:
        shared = _other_group()
        mine = os.getegid()
        root = self._tree(tmp_path, shared)
        with patch.object(podman_run.sys, "platform", "linux"):
            with podman_run.mapped_group(root):
                for p in (root, root / "build-tree", root / "keep"):
                    assert os.lstat(p).st_gid == mine
                for d in (root, root / "build-tree"):
                    assert not os.lstat(d).st_mode & stat.S_ISGID
                # What the container makes, and a file it replaces.
                (root / "build-tree" / "include").mkdir()
                (root / "build-tree" / "include" / "a.h").write_text("x")
                (root / "build-tree" / "Makefile").unlink()
                (root / "build-tree" / "Makefile").write_text("y")
        for p in (
            root,
            root / "keep",
            root / "build-tree",
            root / "build-tree" / "Makefile",
            root / "build-tree" / "include",
            root / "build-tree" / "include" / "a.h",
        ):
            assert os.lstat(p).st_gid == shared, p
        for d in (root, root / "build-tree", root / "build-tree" / "include"):
            assert os.lstat(d).st_mode & stat.S_ISGID, d
        assert not os.lstat(root / "keep").st_mode & stat.S_ISGID

    def test_restores_when_the_run_fails(self, tmp_path: Path) -> None:
        shared = _other_group()
        root = self._tree(tmp_path, shared)
        with patch.object(podman_run.sys, "platform", "linux"):
            with pytest.raises(RuntimeError):
                with podman_run.mapped_group(root):
                    raise RuntimeError("build failed")
        assert os.lstat(root).st_gid == shared
        assert os.lstat(root).st_mode & stat.S_ISGID

    def test_own_group_tree_is_left_alone(self, tmp_path: Path) -> None:
        root = tmp_path / "kernel"
        root.mkdir()
        os.chown(root, -1, os.getegid())
        os.chmod(root, 0o755)
        (root / "f").write_text("x")
        with (
            patch.object(podman_run.sys, "platform", "linux"),
            patch.object(podman_run.os, "lchown") as lchown,
            patch.object(podman_run.os, "chmod") as chmod,
        ):
            with podman_run.mapped_group(root):
                (root / "g").write_text("x")
        lchown.assert_not_called()
        chmod.assert_not_called()

    @pytest.mark.parametrize(
        ("platform", "euid"), [("darwin", 501), ("linux", 0)]
    )
    def test_skipped_off_linux_and_as_root(
        self, tmp_path: Path, platform: str, euid: int
    ) -> None:
        with (
            patch.object(podman_run.sys, "platform", platform),
            patch.object(podman_run.os, "geteuid", return_value=euid),
            patch.object(podman_run.os, "walk") as walk,
        ):
            with podman_run.mapped_group(tmp_path):
                pass
        walk.assert_not_called()


class TestRmtree:
    def test_reclaims_and_retries_when_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "t"
        target.mkdir()
        real = podman_run.shutil.rmtree
        calls: list[Path] = []

        def refuse_once(p: Path) -> None:
            calls.append(p)
            if len(calls) == 1:
                raise PermissionError(13, "Permission denied")
            real(p)

        with (
            patch.object(podman_run.shutil, "rmtree", side_effect=refuse_once),
            patch.object(podman_run, "reclaim_ownership") as reclaim,
        ):
            podman_run.rmtree(target)
        reclaim.assert_called_once_with(target)
        assert len(calls) == 2
        assert not target.exists()

    def test_no_reclaim_when_rmtree_succeeds(self, tmp_path: Path) -> None:
        target = tmp_path / "t"
        target.mkdir()
        with patch.object(podman_run, "reclaim_ownership") as reclaim:
            podman_run.rmtree(target)
        reclaim.assert_not_called()
        assert not target.exists()
