from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg.lustre_build import (
    CLAIM_STAMP,
    GIT_EXCLUDE_MARKER,
    _container_exists,
    _kernel_release,
    _needs_reconfigure,
    _register_git_exclude,
    _show_configure_log,
    _tree_claim,
    build_lustre,
    kernel_has_o2ib,
    o2ib_configure_flag,
    read_staging_meta,
    staging_path,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completed(returncode: int, stdout: str = "") -> MagicMock:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    return r


# ---------------------------------------------------------------------------
# _kernel_release
# ---------------------------------------------------------------------------


class TestKernelRelease:
    def _release_file(self, build_tree: Path) -> Path:
        p = build_tree / "include" / "config"
        p.mkdir(parents=True, exist_ok=True)
        return p / "kernel.release"

    def test_reads_release_file(self, tmp_path: Path) -> None:
        self._release_file(tmp_path).write_text("5.14.0-427.el9.x86_64\n")
        assert _kernel_release(tmp_path) == "5.14.0-427.el9.x86_64"

    def test_strips_whitespace(self, tmp_path: Path) -> None:
        self._release_file(tmp_path).write_text("  5.14.0-1.el9  \n")
        assert _kernel_release(tmp_path) == "5.14.0-1.el9"

    def test_returns_unknown_when_file_missing(self, tmp_path: Path) -> None:
        assert _kernel_release(tmp_path) == "unknown"


# ---------------------------------------------------------------------------
# _container_exists
# ---------------------------------------------------------------------------


class TestContainerExists:
    def test_returns_true_when_podman_exits_zero(self) -> None:
        with patch("ltvm_pkg.lustre_build.subprocess.run") as mock_run:
            mock_run.return_value = _make_completed(0)
            assert _container_exists("ltvm-build-rocky9") is True
        mock_run.assert_called_once_with(
            ["podman", "image", "exists", "ltvm-build-rocky9"],
            capture_output=True,
        )

    def test_returns_false_when_podman_exits_nonzero(self) -> None:
        with patch("ltvm_pkg.lustre_build.subprocess.run") as mock_run:
            mock_run.return_value = _make_completed(1)
            assert _container_exists("no-such-image") is False

    def test_returns_false_on_exit_125(self) -> None:
        with patch("ltvm_pkg.lustre_build.subprocess.run") as mock_run:
            mock_run.return_value = _make_completed(125)
            assert _container_exists("ltvm-build-rocky9") is False


# ---------------------------------------------------------------------------
# _needs_reconfigure
# ---------------------------------------------------------------------------


class TestNeedsReconfigure:
    def _tree(self, tmp_path: Path) -> tuple[Path, Path]:
        lustre = tmp_path / "lustre"
        kernel = tmp_path / "kernel"
        lustre.mkdir()
        kernel.mkdir()
        return lustre, kernel

    TARGET = "rocky9"

    def _full_tree(
        self, tmp_path: Path, kver: str = "5.14.0"
    ) -> tuple[Path, Path]:
        lustre, kernel = self._tree(tmp_path)
        (lustre / "configure").write_text("#!/bin/sh\n")
        (lustre / "config.status").write_text("# status\n")
        # kernel.release is the canonical version file written by build_kernel
        release_dir = kernel / "include" / "config"
        release_dir.mkdir(parents=True)
        (release_dir / "kernel.release").write_text(kver + "\n")
        t = self.TARGET
        (lustre / f".ltvm-kernel-{t}-x86_64").write_text(kver + "\n")
        (lustre / f".ltvm-server-{t}-x86_64").write_text("True\n")
        return lustre, kernel

    def test_force_returns_true(self, tmp_path: Path) -> None:
        lustre, kernel = self._full_tree(tmp_path)
        assert (
            _needs_reconfigure(lustre, kernel, force=True, target=self.TARGET)
            is True
        )

    def test_missing_configure_script_returns_true(
        self, tmp_path: Path
    ) -> None:
        lustre, kernel = self._tree(tmp_path)
        (lustre / "config.status").write_text("# status\n")
        assert (
            _needs_reconfigure(
                lustre,
                kernel,
                force=False,
                target=self.TARGET,
            )
            is True
        )

    def test_missing_config_status_returns_true(self, tmp_path: Path) -> None:
        lustre, kernel = self._tree(tmp_path)
        (lustre / "configure").write_text("#!/bin/sh\n")
        assert (
            _needs_reconfigure(
                lustre,
                kernel,
                force=False,
                target=self.TARGET,
            )
            is True
        )

    def test_different_kernel_version_returns_true(
        self, tmp_path: Path
    ) -> None:
        lustre, kernel = self._full_tree(tmp_path, kver="5.14.0-old")
        # Stamp records old version; kernel.release records new version
        release_dir = kernel / "include" / "config"
        release_dir.mkdir(parents=True, exist_ok=True)
        (release_dir / "kernel.release").write_text("5.14.0-new\n")
        result = _needs_reconfigure(
            lustre,
            kernel,
            force=False,
            target=self.TARGET,
        )
        assert result is True

    def test_everything_matches_returns_false(self, tmp_path: Path) -> None:
        lustre, kernel = self._full_tree(tmp_path, kver="5.14.0")
        result = _needs_reconfigure(
            lustre,
            kernel,
            force=False,
            target=self.TARGET,
        )
        assert result is False

    def test_no_stamp_files_returns_true(self, tmp_path: Path) -> None:
        """No per-target stamps means never built for this target."""
        lustre, kernel = self._tree(tmp_path)
        (lustre / "configure").write_text("#!/bin/sh\n")
        (lustre / "config.status").write_text("# status\n")
        result = _needs_reconfigure(
            lustre,
            kernel,
            force=False,
            target=self.TARGET,
        )
        assert result is True

    def test_server_flag_change_triggers_reconf(self, tmp_path: Path) -> None:
        lustre, kernel = self._tree(tmp_path)
        (lustre / "configure").write_text("#!/bin/sh\n")
        (lustre / "config.status").write_text("")
        kver = "5.14.0-611.el9.x86_64"
        release = kernel / "include" / "config" / "kernel.release"
        release.parent.mkdir(parents=True, exist_ok=True)
        release.write_text(kver + "\n")
        (lustre / f".ltvm-kernel-{self.TARGET}-x86_64").write_text(kver)
        # stamp says server=False, but caller passes enable_server=True
        (lustre / f".ltvm-server-{self.TARGET}-x86_64").write_text("False")
        assert _needs_reconfigure(
            lustre,
            kernel,
            force=False,
            target=self.TARGET,
            enable_server=True,
        )

    def test_stamp_suffix_separates_targets(self) -> None:
        """Two different targets sharing a lustre_tree don't clobber stamps."""
        from ltvm_pkg.lustre_build import _stamp_suffix

        assert _stamp_suffix("rocky9", "x86_64") != _stamp_suffix(
            "rocky10", "x86_64"
        )
        assert _stamp_suffix("rocky9", "x86_64") != _stamp_suffix(
            "rocky9", "aarch64"
        )


# ---------------------------------------------------------------------------
# Per-kernel staging path (lustre_test_vms_v2-eh9)
# ---------------------------------------------------------------------------


class TestStagingPathPerKernel:
    def test_kernel_key_appends_kernel_dir(self, tmp_path: Path) -> None:
        p = staging_path(
            tmp_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        assert p == (
            tmp_path / ".ltvm-staging" / "rocky9" / "x86_64" / "5.14-rhel9.7"
        )

    def test_two_kernels_do_not_share_path(self, tmp_path: Path) -> None:
        a = staging_path(tmp_path, "rocky9", arch="x86_64", kernel="k-a")
        b = staging_path(tmp_path, "rocky9", arch="x86_64", kernel="k-b")
        assert a != b
        assert a.parent == b.parent


class TestStagingCoexistence:
    """Two kernels' staging dirs coexist and userland doesn't overlap."""

    def test_two_kernels_coexist(self, tmp_path: Path) -> None:
        sa = staging_path(
            tmp_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        sb = staging_path(
            tmp_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.5"
        )
        (sa / "usr" / "sbin").mkdir(parents=True)
        (sa / "usr" / "sbin" / "mount.lustre").write_text("A")
        (sa / "lib" / "modules" / "5.14.0-A" / "extra").mkdir(parents=True)
        (
            sa / "lib" / "modules" / "5.14.0-A" / "extra" / "lustre.ko"
        ).write_text("A")
        (sb / "usr" / "sbin").mkdir(parents=True)
        (sb / "usr" / "sbin" / "mount.lustre").write_text("B")
        (sb / "lib" / "modules" / "5.14.0-B" / "extra").mkdir(parents=True)
        (
            sb / "lib" / "modules" / "5.14.0-B" / "extra" / "lustre.ko"
        ).write_text("B")
        assert sa.is_dir() and sb.is_dir()
        assert (sa / "usr" / "sbin" / "mount.lustre").read_text() == "A"
        assert (sb / "usr" / "sbin" / "mount.lustre").read_text() == "B"
        # The two staging roots are disjoint leaf dirs -- neither is a
        # parent of the other.
        assert sa not in sb.parents and sb not in sa.parents


class TestKernelChangeDistclean:
    """Kernel change triggers distclean, not just reconfigure."""

    TARGET = "rocky9"

    def _full_tree(self, tmp_path: Path, old_kver: str, new_kver: str):
        lustre = tmp_path / "lustre"
        kernel = tmp_path / "kernel"
        lustre.mkdir()
        kernel.mkdir()
        (lustre / "lustre" / "kernel_patches").mkdir(parents=True)
        (lustre / "configure").write_text("#!/bin/sh\n")
        (lustre / "config.status").write_text("# status\n")
        (lustre / "Makefile").write_text("# stub\n")
        release_dir = kernel / "include" / "config"
        release_dir.mkdir(parents=True)
        (release_dir / "kernel.release").write_text(new_kver + "\n")
        (kernel / "Module.symvers").write_text("")
        t = self.TARGET
        (lustre / f".ltvm-kernel-{t}-x86_64").write_text(old_kver + "\n")
        (lustre / f".ltvm-server-{t}-x86_64").write_text("True\n")
        return lustre, kernel

    def test_kernel_change_invokes_distclean(self, tmp_path: Path) -> None:
        lustre, kernel = self._full_tree(tmp_path, "5.14.0-old", "5.14.0-new")
        captured_scripts = []

        def mock_run(cmd, *args, **kwargs):
            if "podman" in cmd[0]:
                captured_scripts.append(cmd[-1])
                r = MagicMock()
                r.returncode = 0
                return r
            r = MagicMock()
            r.returncode = 0
            return r

        with (
            patch("ltvm_pkg.lustre_build.subprocess.run", side_effect=mock_run),
            patch(
                "ltvm_pkg.lustre_build.run_podman_with_cleanup",
                side_effect=mock_run,
            ),
            patch("ltvm_pkg.lustre_build._container_exists", return_value=True),
            patch("ltvm_pkg.target_config.TargetConfig") as mock_tc,
        ):
            mock_tc.return_value.resolve_kernel.return_value = "5.14-rhel9.7"
            try:
                build_lustre(
                    lustre,
                    kernel,
                    container_tag="ltvm-build-rocky9",
                    target=self.TARGET,
                    force=False,
                )
            except Exception:
                pass

        assert captured_scripts, "podman run was not called"
        script = captured_scripts[0]
        assert "distclean" in script


class TestTreeClaim:
    """The claim stamp records which build owns the tree's shared
    autoconf state, so a repeat build of the SAME target/kernel skips
    distclean even in a tree that has served other targets."""

    TARGET = "rocky9"
    KVER = "5.14.0-611.el9_lustre"

    def _tree(self, tmp_path: Path):
        lustre = tmp_path / "lustre"
        kernel = tmp_path / "kernel"
        lustre.mkdir()
        kernel.mkdir()
        (lustre / "lustre" / "kernel_patches").mkdir(parents=True)
        (lustre / "configure").write_text("#!/bin/sh\n")
        (lustre / "config.status").write_text("# status\n")
        (lustre / "Makefile").write_text("# stub\n")
        release_dir = kernel / "include" / "config"
        release_dir.mkdir(parents=True)
        (release_dir / "kernel.release").write_text(self.KVER + "\n")
        (kernel / "Module.symvers").write_text("")
        t = self.TARGET
        (lustre / f".ltvm-kernel-{t}-x86_64").write_text(self.KVER + "\n")
        (lustre / f".ltvm-server-{t}-x86_64").write_text("True\n")
        return lustre, kernel

    def _claim(self, lustre: Path, text: str) -> None:
        (lustre / CLAIM_STAMP).write_text(text + "\n")

    def _run(self, lustre: Path, kernel: Path) -> str:
        captured: list[str] = []

        def mock_run(cmd, *args, **kwargs):
            if "podman" in cmd[0]:
                captured.append(cmd[-1])
            r = MagicMock()
            r.returncode = 0
            return r

        with (
            patch("ltvm_pkg.lustre_build.subprocess.run", side_effect=mock_run),
            patch(
                "ltvm_pkg.lustre_build.run_podman_with_cleanup",
                side_effect=mock_run,
            ),
            patch("ltvm_pkg.lustre_build._container_exists", return_value=True),
            patch("ltvm_pkg.target_config.TargetConfig") as mock_tc,
        ):
            mock_tc.return_value.resolve_kernel.return_value = "5.14-rhel9.7"
            try:
                build_lustre(
                    lustre,
                    kernel,
                    container_tag="ltvm-build-rocky9",
                    target=self.TARGET,
                    force=False,
                )
            except Exception:
                pass
        assert captured, "podman run was not called"
        return captured[0]

    def test_matching_claim_skips_distclean(self, tmp_path: Path) -> None:
        lustre, kernel = self._tree(tmp_path)
        self._claim(
            lustre, _tree_claim(self.TARGET, "x86_64", "base", self.KVER)
        )
        assert "distclean" not in self._run(lustre, kernel)

    def test_other_target_claim_forces_distclean(self, tmp_path: Path) -> None:
        lustre, kernel = self._tree(tmp_path)
        self._claim(
            lustre,
            _tree_claim("rocky10", "x86_64", "base", "6.12.0-el10_lustre"),
        )
        assert "distclean" in self._run(lustre, kernel)

    def test_stale_sibling_stamp_does_not_force_distclean(
        self, tmp_path: Path
    ) -> None:
        """The regression: another target's leftover `.ltvm-kernel-*`
        stamp used to distclean every build of THIS target forever."""
        lustre, kernel = self._tree(tmp_path)
        (lustre / ".ltvm-kernel-rocky10-x86_64").write_text(
            "6.12.0-el10_lustre\n"
        )
        self._claim(
            lustre, _tree_claim(self.TARGET, "x86_64", "base", self.KVER)
        )
        assert "distclean" not in self._run(lustre, kernel)

    def test_no_claim_falls_back_to_sibling_sweep(self, tmp_path: Path) -> None:
        """A tree from an ltvm predating the stamp distcleans once."""
        lustre, kernel = self._tree(tmp_path)
        (lustre / ".ltvm-kernel-rocky10-x86_64").write_text(
            "6.12.0-el10_lustre\n"
        )
        assert "distclean" in self._run(lustre, kernel)

    def test_script_writes_claim(self, tmp_path: Path) -> None:
        lustre, kernel = self._tree(tmp_path)
        self._claim(
            lustre, _tree_claim(self.TARGET, "x86_64", "base", self.KVER)
        )
        script = self._run(lustre, kernel)
        expected = _tree_claim(self.TARGET, "x86_64", "base", self.KVER)
        assert f"> {CLAIM_STAMP}" in script
        assert expected in script

    def test_claim_written_before_make(self, tmp_path: Path) -> None:
        """Ordering matters: the claim must land after the cleanup and
        before the build, so a failed `make` leaves it standing."""
        lustre, kernel = self._tree(tmp_path)
        self._claim(
            lustre,
            _tree_claim("rocky10", "x86_64", "base", "6.12.0-el10_lustre"),
        )
        script = self._run(lustre, kernel)
        claim_at = script.index(f"> {CLAIM_STAMP}")
        assert claim_at < script.index("\nmake ")
        assert claim_at > script.index("distclean")


class TestBuildTimeout:
    """The container's --timeout comes from ltvm_pkg.build_timeout, and
    a build stopped there says so instead of a bare rc=255."""

    def _build(
        self,
        tmp_path: Path,
        returncode: int = 0,
        clock_step: float = 0.0,
        configure_failed: bool = True,
    ) -> tuple[list[str], Exception | None]:
        lustre, kernel = TestTreeClaim()._tree(tmp_path)
        cmds: list[list[str]] = []

        def fake_podman(cmd, *args, **kwargs):
            cmds.append(list(cmd))
            return _make_completed(returncode)

        now = [1_000_000.0]

        def fake_time() -> float:
            now[0] += clock_step
            return now[0]

        err: Exception | None = None
        with (
            patch(
                "ltvm_pkg.lustre_build.subprocess.run",
                return_value=_make_completed(0),
            ),
            patch(
                "ltvm_pkg.lustre_build.run_podman_with_cleanup",
                side_effect=fake_podman,
            ),
            patch("ltvm_pkg.lustre_build._container_exists", return_value=True),
            patch(
                "ltvm_pkg.lustre_build._show_configure_log",
                return_value=configure_failed,
            ),
            patch("ltvm_pkg.lustre_build._hit_clock_skew", return_value=False),
            patch("ltvm_pkg.lustre_build.time.time", side_effect=fake_time),
            patch("ltvm_pkg.target_config.TargetConfig") as mock_tc,
        ):
            mock_tc.return_value.resolve_kernel.return_value = "5.14-rhel9.7"
            try:
                build_lustre(
                    lustre,
                    kernel,
                    container_tag="ltvm-build-rocky9",
                    target=TestTreeClaim.TARGET,
                    force=False,
                )
            except Exception as e:
                err = e
        assert cmds, "podman run was not called"
        return cmds[0], err

    @staticmethod
    def _timeout_arg(cmd: list[str]) -> str | None:
        if "--timeout" not in cmd:
            return None
        return cmd[cmd.index("--timeout") + 1]

    def test_default_is_an_hour(self, tmp_path: Path) -> None:
        cmd, _ = self._build(tmp_path)
        assert self._timeout_arg(cmd) == "3600"

    def test_env_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_BUILD_TIMEOUT", "5400")
        cmd, _ = self._build(tmp_path)
        assert self._timeout_arg(cmd) == "5400"

    def test_site_file_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        site = tmp_path / "ltvm.conf"
        site.write_text("[build]\ntimeout = 7200\n")
        monkeypatch.setenv("LTVM_SITE_CONFIG", str(site))
        cmd, _ = self._build(tmp_path)
        assert self._timeout_arg(cmd) == "7200"

    def test_zero_drops_the_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_BUILD_TIMEOUT", "0")
        cmd, _ = self._build(tmp_path)
        assert "--timeout" not in cmd
        # The image tag still directly precedes the script.
        assert cmd[-3] == "ltvm-build-rocky9"

    def test_stopped_at_the_limit_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_BUILD_TIMEOUT", "600")
        # Each clock read is 700s after the last, so the run outlasts
        # the 600s limit.
        _, err = self._build(tmp_path, returncode=255, clock_step=700.0)
        assert isinstance(err, RuntimeError)
        assert "rc=255" in str(err)
        assert "stopped at its 600s limit" in str(err)
        assert "LTVM_BUILD_TIMEOUT" in str(err)

    def test_quick_failure_is_not_blamed_on_the_limit(
        self, tmp_path: Path
    ) -> None:
        _, err = self._build(tmp_path, returncode=2, clock_step=1.0)
        assert isinstance(err, RuntimeError)
        assert str(err) == "Container build failed (rc=2)"

    def test_podman_255_is_not_left_unexplained(self, tmp_path: Path) -> None:
        """Four parallel builds died "rc=255" with nothing else said."""
        _, err = self._build(
            tmp_path, returncode=255, clock_step=1.0, configure_failed=False
        )
        assert isinstance(err, RuntimeError)
        assert "rc=255" in str(err)
        assert "podman's own status" in str(err)

    def test_make_failure_after_configure_passed(self, tmp_path: Path) -> None:
        _, err = self._build(
            tmp_path, returncode=2, clock_step=1.0, configure_failed=False
        )
        assert str(err) == "Container build failed (rc=2)"


class TestIncrementalRebuildGuard:
    """When per-kernel staging exists for this kernel, treat it as
    incremental.  When it doesn't exist, build fresh."""

    def test_missing_staging_means_build_fresh(self, tmp_path: Path) -> None:
        s = staging_path(
            tmp_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        assert not s.exists()

    def test_meta_roundtrip(self, tmp_path: Path) -> None:
        s = staging_path(
            tmp_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        s.mkdir(parents=True)
        (s / ".ltvm-staging-meta.json").write_text(
            '{"kernel_version": "5.14.0-foo", '
            '"module_symvers_sha256": "deadbeef"}'
        )
        meta = read_staging_meta(s)
        assert meta is not None
        assert meta["kernel_version"] == "5.14.0-foo"
        assert meta["module_symvers_sha256"] == "deadbeef"

    def test_meta_missing_returns_none(self, tmp_path: Path) -> None:
        s = tmp_path / "empty"
        s.mkdir()
        assert read_staging_meta(s) is None


# ---------------------------------------------------------------------------
# _show_configure_log -- autoconf's "check config.log" made actionable
# ---------------------------------------------------------------------------


class TestShowConfigureLog:
    """Autoconf dies with 'check config.log for details' and the
    container is torn down before the user can inspect it.  We read
    the log from the bind-mounted tree and dump the tail to stderr so
    the real error is visible.
    """

    def test_prints_tail_when_log_exists(
        self, tmp_path: Path, capsys: object
    ) -> None:

        cap: pytest.CaptureFixture[str] = capsys  # type: ignore[assignment]
        log = tmp_path / "config.log"
        log.write_text("\n".join(f"line {i}" for i in range(1, 101)) + "\n")
        _show_configure_log(tmp_path, tail_lines=10)
        err = cap.readouterr().err
        assert "config.log" in err
        assert "line 91" in err
        assert "line 100" in err
        # Earlier lines are NOT in the tail.
        assert "line 1\n" not in err

    def test_silent_when_log_missing(
        self, tmp_path: Path, capsys: object
    ) -> None:

        cap: pytest.CaptureFixture[str] = capsys  # type: ignore[assignment]
        _show_configure_log(tmp_path)
        assert cap.readouterr().err == ""

    def test_silent_when_configure_passed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A make failure is not explained by a configure that passed."""
        (tmp_path / "config.log").write_text(
            "#define X 1\n\nconfigure: exit 0\n"
        )
        assert _show_configure_log(tmp_path) is False
        assert capsys.readouterr().err == ""

    def test_shown_when_configure_failed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "config.log").write_text("no foo.h\nconfigure: exit 1\n")
        assert _show_configure_log(tmp_path) is True
        assert "no foo.h" in capsys.readouterr().err

    def test_silent_when_log_predates_the_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log = tmp_path / "config.log"
        log.write_text("no foo.h\nconfigure: exit 1\n")
        os.utime(log, (1000, 1000))
        assert _show_configure_log(tmp_path, since=2000) is False
        assert capsys.readouterr().err == ""

    def test_tail_shorter_than_file_length(
        self, tmp_path: Path, capsys: object
    ) -> None:
        """A log shorter than tail_lines emits the whole file."""

        cap: pytest.CaptureFixture[str] = capsys  # type: ignore[assignment]
        log = tmp_path / "config.log"
        log.write_text("only line\n")
        _show_configure_log(tmp_path, tail_lines=50)
        err = cap.readouterr().err
        assert "only line" in err


# ---------------------------------------------------------------------------
# _register_git_exclude
# ---------------------------------------------------------------------------


class TestRegisterGitExclude:
    """ltvm's in-tree state must not show up as untracked files.

    Patch-watcher reports the files a patch changed as the git diff plus
    every untracked file, so a build that leaves .ltvm-* behind makes
    every consumer of that report hand-filter a set only ltvm knows.
    """

    def _exclude(self, tree: Path) -> Path:
        return tree / ".git" / "info" / "exclude"

    def test_creates_exclude_in_plain_checkout(self, tmp_path: Path) -> None:
        (tmp_path / ".git" / "info").mkdir(parents=True)
        _register_git_exclude(tmp_path)
        assert "/.ltvm-*" in self._exclude(tmp_path).read_text()

    def test_creates_info_dir_when_absent(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        _register_git_exclude(tmp_path)
        assert "/.ltvm-*" in self._exclude(tmp_path).read_text()

    def test_is_idempotent(self, tmp_path: Path) -> None:
        (tmp_path / ".git" / "info").mkdir(parents=True)
        for _ in range(3):
            _register_git_exclude(tmp_path)
        text = self._exclude(tmp_path).read_text()
        assert text.count(GIT_EXCLUDE_MARKER) == 1

    def test_preserves_existing_content(self, tmp_path: Path) -> None:
        (tmp_path / ".git" / "info").mkdir(parents=True)
        exclude = self._exclude(tmp_path)
        exclude.write_text("*.swp\n")
        _register_git_exclude(tmp_path)
        text = exclude.read_text()
        assert text.startswith("*.swp\n")
        assert "/.ltvm-*" in text

    def test_appends_newline_to_unterminated_file(self, tmp_path: Path) -> None:
        (tmp_path / ".git" / "info").mkdir(parents=True)
        exclude = self._exclude(tmp_path)
        exclude.write_text("*.swp")
        _register_git_exclude(tmp_path)
        assert exclude.read_text().splitlines()[0] == "*.swp"

    def test_follows_gitdir_file_of_a_submodule(self, tmp_path: Path) -> None:
        real = tmp_path / "realgit"
        (real / "info").mkdir(parents=True)
        tree = tmp_path / "sub"
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {real}\n")
        _register_git_exclude(tree)
        assert "/.ltvm-*" in (real / "info" / "exclude").read_text()

    def test_worktree_writes_the_main_repository_exclude(
        self, tmp_path: Path
    ) -> None:
        main_git = tmp_path / "main" / ".git"
        wt_gitdir = main_git / "worktrees" / "wt"
        wt_gitdir.mkdir(parents=True)
        (wt_gitdir / "commondir").write_text("../..\n")
        tree = tmp_path / "wt"
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {wt_gitdir}\n")
        _register_git_exclude(tree)
        assert "/.ltvm-*" in (main_git / "info" / "exclude").read_text()
        assert not (wt_gitdir / "info").exists()

    def test_non_git_tree_is_a_no_op(self, tmp_path: Path) -> None:
        _register_git_exclude(tmp_path)
        assert not (tmp_path / ".git").exists()

    @pytest.mark.parametrize("worktree", [False, True])
    def test_git_actually_ignores_every_state_file(
        self, tmp_path: Path, worktree: bool
    ) -> None:
        """End to end: the real git, on the real set of names."""
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                capture_output=True,
                text=True,
                check=True,
                env=env,
            ).stdout

        tree = tmp_path / "main"
        git("init", "-q", str(tree))
        if worktree:
            git("-C", str(tree), "commit", "-q", "--allow-empty", "-m", "x")
            tree = tmp_path / "wt"
            git(
                "-C", str(tmp_path / "main"), "worktree", "add", "-q", str(tree)
            )
        _register_git_exclude(tree)
        for name in (
            ".ltvm-build-lock",
            ".ltvm-last-build",
            ".ltvm-configure-rocky9-x86_64",
            ".ltvm-container-libtool",
            ".ltvm-kernel-rocky9-x86_64",
            ".ltvm-server-rocky9-x86_64",
        ):
            (tree / name).write_text("x\n")
        (tree / ".ltvm-staging" / "rocky9").mkdir(parents=True)
        (tree / ".ltvm-staging" / "rocky9" / "a.ko").write_text("x\n")
        assert git("-C", str(tree), "status", "--porcelain") == ""


class TestO2ibDetection:
    """--with-o2ib=yes is the default, and configure fails outright on a
    kernel built without InfiniBand.  Such a kernel gets =no only when
    its meta.json records that its target turned IB off on purpose;
    otherwise the build is refused.  An o2ib setting a target or the
    user named always wins."""

    IB_SYMVERS = (
        "0x1\trdma_destroy_id\tdrivers/infiniband/core/rdma_cm\t"
        "EXPORT_SYMBOL\t\n"
    )
    NO_IB_SYMVERS = "0x2\tjbd2_journal_start\tvmlinux\tEXPORT_SYMBOL\t\n"

    def test_symvers_with_rdma_cm(self, tmp_path: Path) -> None:
        (tmp_path / "Module.symvers").write_text(self.IB_SYMVERS)
        assert kernel_has_o2ib(tmp_path)[0] is True

    def test_symvers_without_rdma_cm(self, tmp_path: Path) -> None:
        """Module.symvers decides, even over a .config that has IB."""
        (tmp_path / "Module.symvers").write_text(self.NO_IB_SYMVERS)
        (tmp_path / ".config").write_text(
            "CONFIG_INFINIBAND=m\nCONFIG_INFINIBAND_ADDR_TRANS=y\n"
        )
        has, why = kernel_has_o2ib(tmp_path)
        assert has is False
        assert "rdma_destroy_id" in why

    @pytest.mark.parametrize(
        "config,has",
        [
            ("CONFIG_INFINIBAND=m\nCONFIG_INFINIBAND_ADDR_TRANS=y\n", True),
            ("CONFIG_INFINIBAND=y\nCONFIG_INFINIBAND_ADDR_TRANS=y\n", True),
            ("CONFIG_INFINIBAND=m\n", False),
            ("# CONFIG_INFINIBAND is not set\n", False),
        ],
    )
    def test_config_without_symvers(
        self, tmp_path: Path, config: str, has: bool
    ) -> None:
        (tmp_path / ".config").write_text(config)
        assert kernel_has_o2ib(tmp_path)[0] is has

    def test_autoconf_h(self, tmp_path: Path) -> None:
        gen = tmp_path / "include" / "generated"
        gen.mkdir(parents=True)
        (gen / "autoconf.h").write_text(
            "#define CONFIG_INFINIBAND_MODULE 1\n"
            "#define CONFIG_INFINIBAND_ADDR_TRANS 1\n"
        )
        assert kernel_has_o2ib(tmp_path)[0] is True

    def test_empty_tree_cannot_tell(self, tmp_path: Path) -> None:
        assert kernel_has_o2ib(tmp_path)[0] is None
        # ... and keeps the default rather than guessing.
        assert o2ib_configure_flag(tmp_path, None) == "--with-o2ib=yes"

    def _no_ib_tree(self, tmp_path: Path, meta: dict | None) -> Path:
        tree = tmp_path / "build-tree"
        tree.mkdir()
        (tree / "Module.symvers").write_text(self.NO_IB_SYMVERS)
        if meta is not None:
            (tmp_path / "meta.json").write_text(json.dumps(meta))
        return tree

    def test_opt_out_configures_no_with_a_note(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tree = self._no_ib_tree(
            tmp_path, {"config_overlay_missed": [], "infiniband_opt_out": True}
        )
        assert o2ib_configure_flag(tree, ["--enable-gss"]) == "--with-o2ib=no"
        out = capsys.readouterr().out
        assert out.count("\n") == 1
        assert "configuring --with-o2ib=no" in out

    @pytest.mark.parametrize(
        "meta,cause",
        [
            (None, "nothing in its target turns InfiniBand off"),
            ({"kernel_version": "x"}, "built before the shared config overlay"),
            (
                {
                    "config_overlay_missed": [
                        "CONFIG_INFINIBAND=m (got n)",
                        "CONFIG_DRM=n (got m)",
                    ]
                },
                "could not turn on CONFIG_INFINIBAND=m (got n)",
            ),
            (
                {"config_overlay_missed": [], "infiniband_opt_out": False},
                "nothing in its target turns InfiniBand off",
            ),
        ],
    )
    def test_no_ib_without_opt_out_is_refused(
        self, tmp_path: Path, meta: dict | None, cause: str
    ) -> None:
        tree = self._no_ib_tree(tmp_path, meta)
        with pytest.raises(RuntimeError) as exc:
            o2ib_configure_flag(tree, None, "rocky8", "aarch64")
        msg = str(exc.value)
        assert cause in msg
        assert "CONFIG_DRM" not in msg
        assert "ltvm build kernel rocky8 --arch aarch64" in msg
        assert "--configure=--with-o2ib=no" in msg

    def test_empty_symvers_cannot_tell(self, tmp_path: Path) -> None:
        (tmp_path / "Module.symvers").write_text("")
        assert kernel_has_o2ib(tmp_path)[0] is None

    @pytest.mark.parametrize(
        "explicit",
        [
            ["--with-o2ib=yes"],
            ["--with-o2ib=/usr/src/ofa_kernel/default"],
            ["--with-o2ib"],
            ["--without-o2ib"],
        ],
    )
    def test_explicit_setting_is_never_overridden(
        self,
        tmp_path: Path,
        explicit: list[str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An explicit setting is appended after the default and wins,
        so the default line is left alone and nothing is printed."""
        (tmp_path / "Module.symvers").write_text(self.NO_IB_SYMVERS)
        assert o2ib_configure_flag(tmp_path, explicit) == "--with-o2ib=yes"
        assert capsys.readouterr().out == ""

    def _build(
        self, tmp_path: Path, symvers: str, extra=None, opt_out=False
    ) -> tuple[list[str], Exception | None]:
        """The podman scripts build_lustre ran, and what it raised."""
        lustre, kernel = TestTreeClaim()._tree(tmp_path)
        (kernel / "Module.symvers").write_text(symvers)
        (kernel.parent / "meta.json").write_text(
            json.dumps(
                {"config_overlay_missed": [], "infiniband_opt_out": opt_out}
            )
        )
        captured: list[str] = []

        def mock_run(cmd, *args, **kwargs):
            if "podman" in cmd[0]:
                captured.append(cmd[-1])
            return _make_completed(0)

        raised = None
        with (
            patch("ltvm_pkg.lustre_build.subprocess.run", side_effect=mock_run),
            patch(
                "ltvm_pkg.lustre_build.run_podman_with_cleanup",
                side_effect=mock_run,
            ),
            patch("ltvm_pkg.lustre_build._container_exists", return_value=True),
            patch("ltvm_pkg.target_config.TargetConfig") as mock_tc,
        ):
            mock_tc.return_value.resolve_kernel.return_value = "5.14-rhel9.7"
            try:
                build_lustre(
                    lustre,
                    kernel,
                    container_tag="ltvm-build-rocky9",
                    target=TestTreeClaim.TARGET,
                    extra_configure=extra,
                    force=False,
                )
            except Exception as e:
                raised = e
        return captured, raised

    def _script(self, tmp_path: Path, symvers: str, **kw) -> str:
        captured, _ = self._build(tmp_path, symvers, **kw)
        assert captured, "podman run was not called"
        return captured[0]

    def test_build_configures_no_for_an_opted_out_kernel(
        self, tmp_path: Path
    ) -> None:
        script = self._script(tmp_path, self.NO_IB_SYMVERS, opt_out=True)
        assert "--with-linux=/kernel --with-o2ib=no" in script

    def test_build_refuses_a_kernel_without_ib_by_accident(
        self, tmp_path: Path
    ) -> None:
        captured, raised = self._build(tmp_path, self.NO_IB_SYMVERS)
        assert captured == []
        assert isinstance(raised, RuntimeError)
        assert "Kernel has no InfiniBand" in str(raised)

    def test_build_keeps_yes_for_a_kernel_with_ib(self, tmp_path: Path) -> None:
        script = self._script(tmp_path, self.IB_SYMVERS)
        assert "--with-linux=/kernel --with-o2ib=yes" in script

    def test_build_keeps_the_users_choice(self, tmp_path: Path) -> None:
        script = self._script(
            tmp_path, self.NO_IB_SYMVERS, extra=["--with-o2ib=yes"]
        )
        assert "--with-o2ib=no" not in script
        assert script.rstrip().count("--with-o2ib=yes") >= 1
