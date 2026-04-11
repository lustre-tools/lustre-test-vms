from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from ltvm_pkg.lustre_build import (
    _container_exists,
    _kernel_release,
    _needs_reconfigure,
    lustre_status,
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
        (lustre / f".ltvm-kernel-{t}").write_text(kver + "\n")
        (lustre / f".ltvm-kernel-path-{t}").write_text("/kernel\n")
        (lustre / f".ltvm-server-{t}").write_text("True\n")
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

    def _staging(self, lt: Path) -> Path:
        """Per-tree staging dir, mirroring lustre_build.staging_path."""
        d = lt / ".ltvm-staging" / self.TARGET / "x86_64"
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
        (lt / f".ltvm-kernel-{self.TARGET}").write_text("5.14.0-old\n")
        status = lustre_status(lt, kt, target=self.TARGET)
        assert status["stale"] is True

    def test_stale_false_when_versions_match(self, tmp_path: Path) -> None:
        lt = self._make_lustre(tmp_path)
        kt = self._make_kernel(tmp_path, "5.14.0")
        (lt / f".ltvm-kernel-{self.TARGET}").write_text("5.14.0\n")
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
        (lt / f".ltvm-kernel-{self.TARGET}").write_text("5.14.0-427.el9\n")
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
