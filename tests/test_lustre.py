from __future__ import annotations

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
    lustre_status,
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
# lustre_status
# ---------------------------------------------------------------------------


class TestLustreStatus:
    TARGET = "rocky9"

    def _make_lustre(self, tmp_path: Path) -> Path:
        lt = tmp_path / "lustre"
        lt.mkdir()
        return lt

    def _make_kernel(self, tmp_path: Path, kver: str = "5.14.0") -> Path:
        kt = tmp_path / "kernel"
        kt.mkdir()
        release_dir = kt / "include" / "config"
        release_dir.mkdir(parents=True)
        (release_dir / "kernel.release").write_text(kver + "\n")
        return kt

    def _staging(self, lt: Path, kernel: str = "test-kernel") -> Path:
        """Per-tree, per-kernel staging dir mirroring staging_path."""
        d = lt / ".ltvm-staging" / self.TARGET / "x86_64" / kernel
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_stale_true_when_no_stamp(self, tmp_path: Path) -> None:
        lt = self._make_lustre(tmp_path)
        kt = self._make_kernel(tmp_path)
        status = lustre_status(lt, kt, target=self.TARGET)
        assert status["stale"] is True

    def test_stale_true_when_versions_differ(self, tmp_path: Path) -> None:
        lt = self._make_lustre(tmp_path)
        kt = self._make_kernel(tmp_path, "5.14.0-new")
        (lt / f".ltvm-kernel-{self.TARGET}-x86_64").write_text("5.14.0-old\n")
        status = lustre_status(lt, kt, target=self.TARGET)
        assert status["stale"] is True

    def test_stale_false_when_versions_match(self, tmp_path: Path) -> None:
        lt = self._make_lustre(tmp_path)
        kt = self._make_kernel(tmp_path, "5.14.0")
        (lt / f".ltvm-kernel-{self.TARGET}-x86_64").write_text("5.14.0\n")
        status = lustre_status(lt, kt, target=self.TARGET)
        assert status["stale"] is False

    def test_ko_count_counts_ko_files(self, tmp_path: Path) -> None:
        lt = self._make_lustre(tmp_path)
        kt = self._make_kernel(tmp_path)
        # ko files now live in <lustre_tree>/.ltvm-staging/<target>/
        staging = self._staging(lt)
        (staging / "foo.ko").write_text("")
        (staging / "bar.ko").write_text("")
        status = lustre_status(lt, kt, target=self.TARGET)
        assert status["ko_count"] == 2

    def test_ko_count_excludes_kconftest(self, tmp_path: Path) -> None:
        lt = self._make_lustre(tmp_path)
        kt = self._make_kernel(tmp_path)
        staging = self._staging(lt)
        (staging / "real.ko").write_text("")
        status = lustre_status(lt, kt, target=self.TARGET)
        assert status["ko_count"] == 1

    def test_configured_true_when_config_status_exists(
        self, tmp_path: Path
    ) -> None:
        lt = self._make_lustre(tmp_path)
        kt = self._make_kernel(tmp_path)
        (lt / "config.status").write_text("# generated\n")
        status = lustre_status(lt, kt, target=self.TARGET)
        assert status["configured"] is True

    def test_configured_false_when_config_status_missing(
        self, tmp_path: Path
    ) -> None:
        lt = self._make_lustre(tmp_path)
        kt = self._make_kernel(tmp_path)
        status = lustre_status(lt, kt, target=self.TARGET)
        assert status["configured"] is False

    def test_built_against_from_stamp(self, tmp_path: Path) -> None:
        lt = self._make_lustre(tmp_path)
        kt = self._make_kernel(tmp_path, "5.14.0-427.el9")
        (lt / f".ltvm-kernel-{self.TARGET}-x86_64").write_text(
            "5.14.0-427.el9\n"
        )
        status = lustre_status(lt, kt, target=self.TARGET)
        assert status["built_against"] == "5.14.0-427.el9"

    def test_built_against_none_when_no_stamp(self, tmp_path: Path) -> None:
        lt = self._make_lustre(tmp_path)
        kt = self._make_kernel(tmp_path)
        status = lustre_status(lt, kt, target=self.TARGET)
        assert status["built_against"] is None

    def test_current_kernel_from_build_tree(self, tmp_path: Path) -> None:
        lt = self._make_lustre(tmp_path)
        kt = self._make_kernel(tmp_path, "5.14.0-test")
        status = lustre_status(lt, kt)
        assert status["current_kernel"] == "5.14.0-test"

    def test_current_kernel_none_when_build_tree_missing(
        self, tmp_path: Path
    ) -> None:
        lt = self._make_lustre(tmp_path)
        (lt / ".ltvm-kernel").write_text("5.14.0\n")
        status = lustre_status(lt, tmp_path / "nonexistent")
        assert status["current_kernel"] is None

    def test_stale_true_when_built_against_none(self, tmp_path: Path) -> None:
        lt = self._make_lustre(tmp_path)
        kt = self._make_kernel(tmp_path, "5.14.0")
        # no stamp -> built_against is None -> stale
        status = lustre_status(lt, kt)
        assert status["stale"] is True

    def test_stale_true_when_current_kernel_none(self, tmp_path: Path) -> None:
        lt = self._make_lustre(tmp_path)
        (lt / ".ltvm-kernel").write_text("5.14.0\n")
        # build_tree does not exist -> current_kernel None -> stale
        status = lustre_status(lt, tmp_path / "nonexistent")
        assert status["stale"] is True

    def test_ko_count_summed_across_kernels_when_no_kernel_given(
        self, tmp_path: Path
    ) -> None:
        """When kernel=None, ko_count sums all per-kernel staging dirs."""
        lt = self._make_lustre(tmp_path)
        kt = self._make_kernel(tmp_path)
        for k, n in [("5.14-rhel9.7", 3), ("5.14-rhel9.5", 2)]:
            s = staging_path(lt, self.TARGET, kernel=k)
            s.mkdir(parents=True, exist_ok=True)
            for i in range(n):
                (s / f"mod{i}.ko").write_text("")
        status = lustre_status(lt, kt, target=self.TARGET)
        assert status["ko_count"] == 5


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
        self, tmp_path: Path, returncode: int = 0, clock_step: float = 0.0
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
            patch("ltvm_pkg.lustre_build._show_configure_log"),
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
