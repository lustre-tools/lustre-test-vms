"""Tests for ltvm_pkg/image_build.py -- image building helpers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_target_config(tmp_path: Path, name: str = "rocky9") -> MagicMock:
    """Return a MagicMock TargetConfig with sensible defaults."""
    tc = MagicMock()
    tc.name = name
    tc.output_dir = tmp_path / "output" / name
    tc.image_output_dir.return_value = tc.output_dir / "image"
    tc.is_stale.return_value = False
    return tc


class TestContainerImageTag:
    def test_returns_expected_format(self) -> None:
        import ltvm_pkg.image_build as image

        tc = MagicMock()
        tc.name = "rocky9"
        tc.arch = "x86_64"
        assert image._container_image_tag(tc) == "ltvm-image-rocky9"

    def test_uses_target_name(self) -> None:
        import ltvm_pkg.image_build as image

        tc = MagicMock()
        tc.name = "fedora40"
        tc.arch = "x86_64"
        assert image._container_image_tag(tc) == "ltvm-image-fedora40"

    def test_name_embedded_in_tag(self) -> None:
        import ltvm_pkg.image_build as image

        tc = MagicMock()
        tc.name = "ubuntu22"
        tag = image._container_image_tag(tc)
        assert tag.startswith("ltvm-image-")
        assert "ubuntu22" in tag


class TestImageStatus:
    def test_no_image_returns_not_built(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        tc = _make_target_config(tmp_path)
        status = image.image_status(tc)

        assert status["built"] is False
        assert status["stale"] is True
        assert status["path"] is None

    def test_no_image_returns_none_fields(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        tc = _make_target_config(tmp_path)
        status = image.image_status(tc)

        assert status["build_date"] is None
        assert status["size_mb"] is None

    def test_image_exists_returns_built_true(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        tc = _make_target_config(tmp_path)
        out_dir = tc.image_output_dir()
        out_dir.mkdir(parents=True)
        (out_dir / "base.ext4").write_bytes(b"\x00" * 1024 * 1024)

        status = image.image_status(tc)

        assert status["built"] is True

    def test_image_exists_stale_false_when_is_stale_false(
        self, tmp_path: Path
    ) -> None:
        import ltvm_pkg.image_build as image

        tc = _make_target_config(tmp_path)
        tc.is_stale.return_value = False
        out_dir = tc.image_output_dir()
        out_dir.mkdir(parents=True)
        (out_dir / "base.ext4").write_bytes(b"\x00" * 1024 * 1024)

        status = image.image_status(tc)

        assert status["stale"] is False

    def test_image_exists_stale_true_when_is_stale_true(
        self, tmp_path: Path
    ) -> None:
        import ltvm_pkg.image_build as image

        tc = _make_target_config(tmp_path)
        tc.is_stale.return_value = True
        out_dir = tc.image_output_dir()
        out_dir.mkdir(parents=True)
        (out_dir / "base.ext4").write_bytes(b"\x00" * 1024 * 1024)

        status = image.image_status(tc)

        assert status["stale"] is True

    def test_size_mb_rounded_to_1dp(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        tc = _make_target_config(tmp_path)
        out_dir = tc.image_output_dir()
        out_dir.mkdir(parents=True)
        # Write exactly 1.5 MiB (1.5 * 1024 * 1024 bytes)
        size_bytes = int(1.5 * 1024 * 1024)
        (out_dir / "base.ext4").write_bytes(b"\x00" * size_bytes)

        status = image.image_status(tc)

        assert status["size_mb"] == 1.5

    def test_size_mb_is_float(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        tc = _make_target_config(tmp_path)
        out_dir = tc.image_output_dir()
        out_dir.mkdir(parents=True)
        (out_dir / "base.ext4").write_bytes(b"\x00" * (2 * 1024 * 1024))

        status = image.image_status(tc)

        assert isinstance(status["size_mb"], float)

    def test_build_date_from_meta_json(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        tc = _make_target_config(tmp_path)
        out_dir = tc.image_output_dir()
        out_dir.mkdir(parents=True)
        (out_dir / "base.ext4").write_bytes(b"\x00" * 1024)

        build_date = "2025-01-15T12:34:56+00:00"
        meta = {"build_date": build_date, "image_size_mb": 1.0}
        (out_dir / "meta.json").write_text(json.dumps(meta))

        status = image.image_status(tc)

        assert status["build_date"] == build_date

    def test_build_date_none_when_no_meta(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        tc = _make_target_config(tmp_path)
        out_dir = tc.image_output_dir()
        out_dir.mkdir(parents=True)
        (out_dir / "base.ext4").write_bytes(b"\x00" * 1024)
        # No meta.json written

        status = image.image_status(tc)

        assert status["build_date"] is None

    def test_path_is_string(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        tc = _make_target_config(tmp_path)
        out_dir = tc.image_output_dir()
        out_dir.mkdir(parents=True)
        (out_dir / "base.ext4").write_bytes(b"\x00" * 1024)

        status = image.image_status(tc)

        assert isinstance(status["path"], str)

    def test_path_points_to_base_ext4(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        tc = _make_target_config(tmp_path)
        out_dir = tc.image_output_dir()
        out_dir.mkdir(parents=True)
        (out_dir / "base.ext4").write_bytes(b"\x00" * 1024)

        status = image.image_status(tc)

        assert status["path"].endswith("base.ext4")

    def test_is_stale_called_with_image_artifact(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        tc = _make_target_config(tmp_path)
        out_dir = tc.image_output_dir()
        out_dir.mkdir(parents=True)
        (out_dir / "base.ext4").write_bytes(b"\x00" * 1024)

        image.image_status(tc)

        tc.is_stale.assert_called_once_with("image", kernel=None)

    def test_is_stale_not_called_when_no_image(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        tc = _make_target_config(tmp_path)
        image.image_status(tc)

        tc.is_stale.assert_not_called()


class TestImageOutputDirPerKernel:
    """build-image --kernel produces distinct paths for different kernels."""

    def test_distinct_paths_for_distinct_kernels(
        self, tmp_targets: Path
    ) -> None:
        from tests.conftest import _make_config

        tc = _make_config(tmp_targets)
        p1 = tc.image_output_dir("5.14-rhel9.7")
        p2 = tc.image_output_dir("5.14-rhel9.5")
        assert p1 != p2
        assert p1.name == "5.14-rhel9.7"
        assert p2.name == "5.14-rhel9.5"
        assert p1.parent == p2.parent
        assert p1.parent.name == "images"


class TestCheckMke2fs:
    def test_does_not_raise_when_mke2fs_present(self) -> None:
        import ltvm_pkg.image_build as image

        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = "mke2fs 1.47.0 (5-Feb-2023)\n"

        with patch("subprocess.run", return_value=mock_result):
            # Should not raise
            image._check_mke2fs()

    def test_does_not_raise_when_mke2fs_in_stdout(self) -> None:
        import ltvm_pkg.image_build as image

        mock_result = MagicMock()
        mock_result.stdout = "mke2fs 1.47.0"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            image._check_mke2fs()

    def test_raises_runtime_error_when_not_found(self) -> None:
        import ltvm_pkg.image_build as image

        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = "some other tool output"

        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="mke2fs not found"):
                image._check_mke2fs()

    def test_raises_runtime_error_on_empty_output(self) -> None:
        import ltvm_pkg.image_build as image

        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError):
                image._check_mke2fs()

    def test_calls_mke2fs_with_version_flag(self) -> None:
        import ltvm_pkg.image_build as image

        mock_result = MagicMock()
        mock_result.stdout = "mke2fs 1.47.0"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            image._check_mke2fs()
            args, kwargs = mock_run.call_args
            assert args[0] == ["mke2fs", "-V"]


class TestGetPackageManifest:
    def test_returns_sorted_package_list(self) -> None:
        import ltvm_pkg.image_build as image

        rpm_output = "zlib-1.2.11-40.el9.x86_64\nbash-5.1.8-6.el9.x86_64\nattr-2.5.1-3.el9.x86_64\n"
        mock_result = MagicMock()
        mock_result.stdout = rpm_output

        with patch("ltvm_pkg.image_build._run", return_value=mock_result):
            packages = image._get_package_manifest("ltvm-image-rocky9")

        assert packages == [
            "attr-2.5.1-3.el9.x86_64",
            "bash-5.1.8-6.el9.x86_64",
            "zlib-1.2.11-40.el9.x86_64",
        ]

    def test_returns_sorted_list(self) -> None:
        import ltvm_pkg.image_build as image

        mock_result = MagicMock()
        mock_result.stdout = "zzz-1.0\naaa-1.0\nmmm-1.0\n"

        with patch("ltvm_pkg.image_build._run", return_value=mock_result):
            packages = image._get_package_manifest("ltvm-image-rocky9")

        assert packages == sorted(packages)

    def test_raises_on_called_process_error(self) -> None:
        import ltvm_pkg.image_build as image

        with patch(
            "ltvm_pkg.image_build._run",
            side_effect=subprocess.CalledProcessError(1, "podman"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                image._get_package_manifest("ltvm-image-rocky9")

    def test_strips_trailing_newline(self) -> None:
        import ltvm_pkg.image_build as image

        mock_result = MagicMock()
        mock_result.stdout = "pkg-1.0\n"

        with patch("ltvm_pkg.image_build._run", return_value=mock_result):
            packages = image._get_package_manifest("ltvm-image-rocky9")

        assert packages == ["pkg-1.0"]

    def test_passes_container_tag_to_run(self) -> None:
        import ltvm_pkg.image_build as image

        mock_result = MagicMock()
        mock_result.stdout = ""

        with patch(
            "ltvm_pkg.image_build._run", return_value=mock_result
        ) as mock_run:
            image._get_package_manifest("ltvm-image-rocky9")

        cmd = mock_run.call_args[0][0]
        assert "ltvm-image-rocky9" in cmd

    def test_empty_output_returns_empty_list(self) -> None:
        import ltvm_pkg.image_build as image

        mock_result = MagicMock()
        mock_result.stdout = ""

        with patch("ltvm_pkg.image_build._run", return_value=mock_result):
            packages = image._get_package_manifest("ltvm-image-rocky9")

        assert packages == []
