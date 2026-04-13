from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from ltvm_pkg.lustre_build import (
    _container_exists,
    _kernel_release,
    _needs_reconfigure,
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
        (lt / f".ltvm-kernel-{self.TARGET}-x86_64").write_text("5.14.0-427.el9\n")
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
        (sa / "lib" / "modules" / "5.14.0-A" / "extra" / "lustre.ko").write_text(
            "A"
        )
        (sb / "usr" / "sbin").mkdir(parents=True)
        (sb / "usr" / "sbin" / "mount.lustre").write_text("B")
        (sb / "lib" / "modules" / "5.14.0-B" / "extra").mkdir(parents=True)
        (sb / "lib" / "modules" / "5.14.0-B" / "extra" / "lustre.ko").write_text(
            "B"
        )
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
        lustre, kernel = self._full_tree(
            tmp_path, "5.14.0-old", "5.14.0-new"
        )
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
                "ltvm_pkg.lustre_build._container_exists", return_value=True
            ),
            patch(
                "ltvm_pkg.target_config.TargetConfig"
            ) as mock_tc,
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
