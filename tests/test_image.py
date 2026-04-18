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
    tc.output_dir = tmp_path / "artifacts" / name
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

        # image_status now threads the bound TargetConfig's variant into
        # is_stale so variant-scoped images report staleness correctly.
        tc.is_stale.assert_called_once_with(
            "image", kernel=None, variant=tc.variant_name
        )

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
            with pytest.raises(RuntimeError, match="mke2fs not functional"):
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


class TestKdumpInjectLines:
    """_kdump_inject_lines bakes vmlinuz + initramfs into the image."""

    def _setup_kdir(
        self, tmp_path: Path, with_vmlinuz: bool = True,
        with_kconfig: bool = False, with_vmlinux: bool = False,
    ) -> tuple[Path, Path]:
        kdir = tmp_path / "kernel"
        kdir.mkdir()
        if with_vmlinuz:
            (kdir / "vmlinuz").write_bytes(b"bzImage")
        if with_vmlinux:
            (kdir / "vmlinux").write_bytes(b"ELF")
        if with_kconfig:
            (kdir / "build-tree").mkdir()
            (kdir / "build-tree" / ".config").write_text("CONFIG_X=y\n")
        inject = tmp_path / "inject"
        inject.mkdir()
        return kdir, inject

    def test_rhel_emits_copy_and_dracut(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        kdir, inject = self._setup_kdir(tmp_path)
        lines = image._kdump_inject_lines(
            kdir, inject, "5.14.0-foo", "rhel"
        )

        text = "\n".join(lines)
        assert "COPY vmlinuz /boot/vmlinuz-5.14.0-foo" in text
        assert "dracut --kver 5.14.0-foo" in text
        assert "/boot/initramfs-5.14.0-foo.img" in text
        assert (inject / "vmlinuz").exists()

    def test_debian_emits_update_initramfs(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        kdir, inject = self._setup_kdir(
            tmp_path, with_kconfig=True
        )
        lines = image._kdump_inject_lines(
            kdir, inject, "5.14.0-foo", "debian"
        )

        text = "\n".join(lines)
        assert "COPY vmlinuz /boot/vmlinuz-5.14.0-foo" in text
        assert "COPY kconfig /boot/config-5.14.0-foo" in text
        assert "update-initramfs -c -k 5.14.0-foo" in text
        assert "/var/lib/kdump/initrd.img-5.14.0-foo" in text
        assert "dracut" not in text
        assert (inject / "kconfig").exists()

    def test_debian_without_kconfig_skips_config_copy(
        self, tmp_path: Path
    ) -> None:
        import ltvm_pkg.image_build as image

        kdir, inject = self._setup_kdir(tmp_path)
        lines = image._kdump_inject_lines(
            kdir, inject, "5.14.0-foo", "debian"
        )
        text = "\n".join(lines)
        assert "COPY kconfig" not in text
        assert "update-initramfs" in text

    def test_no_kver_returns_empty(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        kdir, inject = self._setup_kdir(tmp_path)
        assert image._kdump_inject_lines(kdir, inject, None, "rhel") == []
        assert image._kdump_inject_lines(kdir, inject, "", "rhel") == []

    def test_vmlinux_fallback_no_dracut(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        kdir, inject = self._setup_kdir(
            tmp_path, with_vmlinuz=False, with_vmlinux=True
        )
        lines = image._kdump_inject_lines(
            kdir, inject, "5.14.0-foo", "rhel"
        )
        text = "\n".join(lines)
        assert "COPY vmlinuz /boot/vmlinuz-5.14.0-foo" in text
        assert "dracut" not in text
        assert "update-initramfs" not in text
        assert (inject / "vmlinuz").exists()

    def test_no_kernel_images_returns_empty(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        kdir, inject = self._setup_kdir(tmp_path, with_vmlinuz=False)
        assert (
            image._kdump_inject_lines(kdir, inject, "5.14.0-foo", "rhel")
            == []
        )


class TestLustreInjectLines:
    """_lustre_inject_lines stages Lustre modules+userland into the
    image build context."""

    def _make_staging(self, tmp_path: Path, kver: str) -> Path:
        staging = tmp_path / "staging"
        (staging / "lib" / "modules" / kver / "extra").mkdir(parents=True)
        (staging / "lib" / "modules" / kver / "extra" / "lustre.ko").write_text(
            "M"
        )
        (staging / "usr" / "sbin").mkdir(parents=True)
        (staging / "usr" / "sbin" / "mount.lustre").write_text("X")
        (staging / "etc").mkdir()
        (staging / "etc" / "ldev.conf").write_text("")
        return staging

    def test_emits_module_and_userland_copies(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        staging = self._make_staging(tmp_path, "5.14.0-foo")
        inject = tmp_path / "inject"
        inject.mkdir()

        lines = image._lustre_inject_lines(
            staging, inject, "5.14.0-foo", "rhel"
        )
        text = "\n".join(lines)
        assert "COPY lustre-extra/ /lib/modules/5.14.0-foo/extra/" in text
        assert "COPY lustre-userland-usr/ /usr/" in text
        assert "COPY lustre-userland-etc/ /etc/" in text
        assert "depmod -a 5.14.0-foo" in text
        assert (inject / "lustre-extra" / "lustre.ko").read_text() == "M"
        assert (
            inject / "lustre-userland-usr" / "sbin" / "mount.lustre"
        ).read_text() == "X"

    def test_debian_same_layout(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        staging = self._make_staging(tmp_path, "6.1.0-deb")
        inject = tmp_path / "inject"
        inject.mkdir()

        lines = image._lustre_inject_lines(
            staging, inject, "6.1.0-deb", "debian"
        )
        text = "\n".join(lines)
        assert "COPY lustre-extra/ /lib/modules/6.1.0-deb/extra/" in text
        assert "depmod -a 6.1.0-deb" in text

    def test_no_shell_interpolation_in_copy_sources(
        self, tmp_path: Path
    ) -> None:
        import ltvm_pkg.image_build as image

        staging = self._make_staging(tmp_path, "5.14.0-foo")
        inject = tmp_path / "inject"
        inject.mkdir()
        lines = image._lustre_inject_lines(
            staging, inject, "5.14.0-foo", "rhel"
        )
        for line in lines:
            # Tar-in-tar-out via copytree means every COPY is a fixed
            # literal path, not a shell glob/expansion.
            assert "$(" not in line
            assert "*" not in line


class TestBuildImageWithLustre:
    """--with-lustre flips the input hash and requires staging."""

    def _lustre_tree_with_staging(
        self,
        tmp_targets: Path,
        tmp_path: Path,
        kernel_dir: str,
    ) -> Path:
        from ltvm_pkg.lustre_build import staging_path

        lt = tmp_path / "tree"
        lt.mkdir()
        staging = staging_path(
            lt, "rocky9", arch="x86_64", kernel=kernel_dir
        )
        (staging / "lib" / "modules" / "5.14.0-foo" / "extra").mkdir(
            parents=True
        )
        (
            staging
            / "lib"
            / "modules"
            / "5.14.0-foo"
            / "extra"
            / "lustre.ko"
        ).write_text("M")
        (staging / "usr" / "sbin").mkdir(parents=True)
        (staging / "usr" / "sbin" / "mount.lustre").write_text("X")
        (staging / ".ltvm-staging-meta.json").write_text(
            '{"module_symvers_sha256": "deadbeef"}'
        )
        return lt

    def test_missing_staging_raises(
        self, tmp_targets: Path, tmp_path: Path
    ) -> None:
        import ltvm_pkg.image_build as image
        from tests.conftest import _make_config

        tc = _make_config(tmp_targets)
        lt = tmp_path / "empty-tree"
        lt.mkdir()
        with pytest.raises(FileNotFoundError, match="ltvm build lustre"):
            image.build_image(
                tc, force=True, kernel="5.14-rhel9.7", with_lustre=lt
            )

    def test_presence_flips_input_hash(
        self, tmp_targets: Path, tmp_path: Path
    ) -> None:
        from tests.conftest import _make_config

        tc = _make_config(tmp_targets)
        lt = self._lustre_tree_with_staging(
            tmp_targets, tmp_path, "5.14-rhel9.7"
        )
        # Hash without --with-lustre
        h0 = tc.input_hash("image", kernel="5.14-rhel9.7")

        from ltvm_pkg.image_build import _lustre_staging_hash_input
        from ltvm_pkg.lustre_build import staging_path

        staging = staging_path(
            lt, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        extra = _lustre_staging_hash_input(staging)
        h1 = tc.input_hash("image", kernel="5.14-rhel9.7", extra=extra)
        assert h0 != h1

    def test_staging_hash_changes_when_symvers_changes(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg.image_build import _lustre_staging_hash_input

        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / ".ltvm-staging-meta.json").write_text(
            '{"module_symvers_sha256": "aaaa"}'
        )
        h0 = _lustre_staging_hash_input(staging)
        (staging / ".ltvm-staging-meta.json").write_text(
            '{"module_symvers_sha256": "bbbb"}'
        )
        h1 = _lustre_staging_hash_input(staging)
        assert h0 != h1


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


class TestComputeImageSizeFromTar:
    def test_small_tar_hits_floor(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        tarball = tmp_path / "r.tar"
        tarball.write_bytes(b"\0" * 1024)
        assert image._compute_image_size_mb_from_tar(tarball) == image._IMAGE_SIZE_FLOOR_MB

    def test_large_tar_scales_with_fudge(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_build as image

        tarball = tmp_path / "r.tar"
        # 2 GiB tar -> at least 2 GiB * 1.2 + headroom, safely above floor
        size = 2 * 1024 * 1024 * 1024
        with tarball.open("wb") as fp:
            fp.truncate(size)
        mb = image._compute_image_size_mb_from_tar(tarball)
        expected = int(size * image._IMAGE_SIZE_FUDGE / (1024 * 1024)) + image._IMAGE_SIZE_HEADROOM_MB
        assert mb == expected
        assert mb > image._IMAGE_SIZE_FLOOR_MB


class TestBuildImageNotRootGated:
    def test_cli_cmd_build_image_has_no_require_root_call(self) -> None:
        """build-image and build-all must not be gated by _require_root."""
        import inspect
        from ltvm_pkg import cli

        src_image = inspect.getsource(cli.cmd_build_image)
        src_all = inspect.getsource(cli.cmd_build_all)
        assert "_require_root" not in src_image
        assert "_require_root" not in src_all
