"""Tests for ltvm_pkg/image_export.py -- bootable-disk packaging."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_target_config(
    tmp_path: Path,
    name: str = "rocky9",
    kernel_name: str = "5.14-rhel9.7-1.el9",
    kver: str = "5.14.0-1.el9.x86_64",
) -> MagicMock:
    """Return a MagicMock TargetConfig with a populated on-disk layout."""
    image_dir = tmp_path / "artifacts" / name / "x86_64" / "images" / kernel_name
    kernel_dir = tmp_path / "artifacts" / name / "x86_64" / "kernels" / kernel_name
    image_dir.mkdir(parents=True)
    (kernel_dir / "build-tree" / "include" / "config").mkdir(parents=True)

    # Fake artifacts.  Sizes are what _image_size_mb rounds on.
    (image_dir / "base.ext4").write_bytes(b"\0" * (1024 * 1024))  # 1 MiB
    (kernel_dir / "vmlinuz").write_bytes(b"\0" * (8 * 1024 * 1024))  # 8 MiB
    (kernel_dir / "build-tree" / "include" / "config" /
     "kernel.release").write_text(kver + "\n")

    tc = MagicMock()
    tc.name = name
    tc.resolve_kernel.return_value = kernel_name
    tc.image_output_dir.return_value = image_dir
    tc.kernel_output_dir.return_value = kernel_dir
    return tc


class TestCheckHostTools:
    def test_missing_core_tool_raises(self) -> None:
        import ltvm_pkg.image_export as ie

        with patch.object(ie.shutil, "which",
                          side_effect=lambda x: None if x == "parted" else "/u/bin/x"):
            with pytest.raises(RuntimeError, match="parted"):
                ie._check_host_tools()

    def test_no_grub_raises(self) -> None:
        import ltvm_pkg.image_export as ie

        def which(name: str) -> str | None:
            if name in ("grub2-install", "grub-install"):
                return None
            return "/u/bin/" + name

        with patch.object(ie.shutil, "which", side_effect=which):
            with pytest.raises(RuntimeError, match="grub"):
                ie._check_host_tools()

    def test_prefers_grub2_install(self) -> None:
        import ltvm_pkg.image_export as ie

        def which(name: str) -> str:
            # Both present; grub2-install should win.
            return f"/u/bin/{name}"

        with patch.object(ie.shutil, "which", side_effect=which):
            tools = ie._check_host_tools()
        assert tools["grub_install"] == "grub2-install"

    def test_falls_back_to_grub_install(self) -> None:
        import ltvm_pkg.image_export as ie

        def which(name: str) -> str | None:
            if name == "grub2-install":
                return None
            return f"/u/bin/{name}"

        with patch.object(ie.shutil, "which", side_effect=which):
            tools = ie._check_host_tools()
        assert tools["grub_install"] == "grub-install"


class TestImageSize:
    def test_includes_headroom(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        rootfs = tmp_path / "root.ext4"
        rootfs.write_bytes(b"\0" * (100 * 1024 * 1024))  # 100 MiB
        kdir = tmp_path / "kdir"
        kdir.mkdir()
        (kdir / "vmlinuz").write_bytes(b"\0" * (10 * 1024 * 1024))

        size = ie._image_size_mb(rootfs, kdir)
        # 100 (rootfs) + 10 (vmlinuz) + 512 (headroom) = 622
        assert size == 622

    def test_missing_vmlinuz_ok(self, tmp_path: Path) -> None:
        """_image_size_mb should not blow up if vmlinuz is absent;
        vmlinux alone counts, and callers will catch the missing
        vmlinuz separately."""
        import ltvm_pkg.image_export as ie

        rootfs = tmp_path / "root.ext4"
        rootfs.write_bytes(b"\0" * (50 * 1024 * 1024))
        kdir = tmp_path / "kdir"
        kdir.mkdir()

        size = ie._image_size_mb(rootfs, kdir)
        assert size == 50 + ie._HEADROOM_MB


class TestWriteGrubCfg:
    def test_menuentry_points_at_fs_uuid(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        boot = tmp_path / "boot"
        boot.mkdir()
        ie._write_grub_cfg(boot, "5.14.0-test", "abc-uuid-1234",
                           grub_install="grub2-install")

        cfg = (boot / "grub2" / "grub.cfg").read_text()
        assert "abc-uuid-1234" in cfg
        assert "/boot/vmlinuz-5.14.0-test" in cfg
        assert "/boot/initramfs-5.14.0-test.img" in cfg
        # Both consoles, so headless-serial AND graphical qemu work.
        assert "console=tty0" in cfg
        assert "console=ttyS0" in cfg
        # root= pinned to UUID, not /dev/something (portability).
        assert "root=UUID=abc-uuid-1234" in cfg

    def test_picks_grub_vs_grub2_dir(self, tmp_path: Path) -> None:
        """grub-install (Debian) writes to /boot/grub; grub2-install
        (RHEL) writes to /boot/grub2.  _write_grub_cfg picks the
        subdir to match, so the BIOS bootloader finds its config at
        the compiled-in path."""
        import ltvm_pkg.image_export as ie

        boot = tmp_path / "boot"
        boot.mkdir()
        ie._write_grub_cfg(boot, "kv", "uuid", grub_install="/u/bin/grub-install")
        assert (boot / "grub" / "grub.cfg").exists()
        assert not (boot / "grub2" / "grub.cfg").exists()

        boot2 = tmp_path / "boot2"
        boot2.mkdir()
        ie._write_grub_cfg(boot2, "kv", "uuid",
                           grub_install="/u/bin/grub2-install")
        assert (boot2 / "grub2" / "grub.cfg").exists()
        assert not (boot2 / "grub" / "grub.cfg").exists()


class TestExportImageGuards:
    """Smoke tests for the public entry point's input-validation path.

    These never reach the subprocess/loop section: every case is
    rejected before any disk work happens.  That keeps them fast and
    free of host-tool dependencies.
    """

    def test_rejects_unknown_format(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        tc = _make_target_config(tmp_path)
        with pytest.raises(ValueError, match="unknown format"):
            ie.export_image(tc, None, tmp_path / "o.img", image_format="vmdk")

    def test_rejects_existing_output(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        tc = _make_target_config(tmp_path)
        out = tmp_path / "exists.qcow2"
        out.write_text("x")
        with pytest.raises(FileExistsError):
            ie.export_image(tc, None, out)

    def test_missing_base_ext4(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        tc = _make_target_config(tmp_path)
        # Delete base.ext4 after fixture put it there.
        (tc.image_output_dir.return_value / "base.ext4").unlink()

        with patch.object(ie, "_check_host_tools", return_value={
                "grub_install": "grub-install"}):
            with pytest.raises(FileNotFoundError, match="base.ext4"):
                ie.export_image(tc, None, tmp_path / "o.qcow2")

    def test_missing_vmlinuz(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        tc = _make_target_config(tmp_path)
        (tc.kernel_output_dir.return_value / "vmlinuz").unlink()

        with patch.object(ie, "_check_host_tools", return_value={
                "grub_install": "grub-install"}):
            with pytest.raises(FileNotFoundError, match="vmlinuz"):
                ie.export_image(tc, None, tmp_path / "o.qcow2")

    def test_missing_kernel_release(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        tc = _make_target_config(tmp_path)
        kdir = tc.kernel_output_dir.return_value
        (kdir / "build-tree" / "include" / "config" /
         "kernel.release").unlink()

        with patch.object(ie, "_check_host_tools", return_value={
                "grub_install": "grub-install"}):
            with pytest.raises(FileNotFoundError, match="kernel release"):
                ie.export_image(tc, None, tmp_path / "o.qcow2")


class TestCliWiring:
    """The `ltvm target export` subcommand is visible and routes to
    cmd_target_export."""

    def test_subcommand_registered(self) -> None:
        # The ltvm CLI script has no .py extension; load it via
        # SourceFileLoader, the same trick test_ltvm_cli uses.
        import importlib.machinery
        from pathlib import Path as _P

        root = _P(__file__).resolve().parent.parent
        loader = importlib.machinery.SourceFileLoader(
            "ltvm_script_export", str(root / "ltvm"))
        mod = loader.load_module()  # type: ignore[deprecated]
        parser = mod.build_parser()

        # Parse a nonsense invocation that nevertheless must succeed
        # at the argparse level -- failure would mean the subcommand
        # didn't register.
        ns = parser.parse_args(
            ["target", "export", "rocky9", "--format", "raw",
             "--output", "/tmp/x.raw"]
        )
        assert ns.func.__name__ == "cmd_target_export"
        assert ns.target == "rocky9"
        assert ns.format == "raw"
        assert ns.output == "/tmp/x.raw"

    def test_requires_root(self, tmp_path: Path) -> None:
        import argparse

        from ltvm_pkg import cli

        args = argparse.Namespace(
            target="rocky9", arch=None, kernel=None,
            output=None, format="qcow2", force=False, json=True,
        )
        with patch.object(cli.os, "getuid", return_value=1000):
            rc = cli.cmd_target_export(args)
        assert rc == cli.EXIT_ERROR


class TestDoctorFlagsMissingExportTools:
    """`ltvm doctor` surfaces missing export deps so users aren't
    surprised at export time."""

    def test_flags_missing_parted(self) -> None:
        import shutil as _shutil

        from ltvm_pkg.vm_commands import _check_export_tools

        def which(name: str) -> str | None:
            return None if name == "parted" else f"/u/bin/{name}"

        with patch.object(_shutil, "which", side_effect=which):
            warnings = _check_export_tools()
        assert any("parted" in w and "target export" in w for w in warnings)

    def test_flags_missing_grub(self) -> None:
        import shutil as _shutil

        from ltvm_pkg.vm_commands import _check_export_tools

        def which(name: str) -> str | None:
            if name in ("grub-install", "grub2-install"):
                return None
            return f"/u/bin/{name}"

        with patch.object(_shutil, "which", side_effect=which):
            warnings = _check_export_tools()
        assert any("grub" in w for w in warnings)

    def test_silent_when_all_present(self) -> None:
        import shutil as _shutil

        from ltvm_pkg.vm_commands import _check_export_tools

        with patch.object(_shutil, "which", return_value="/u/bin/x"):
            warnings = _check_export_tools()
        assert warnings == []


class TestHostSetupDeps:
    """check_prerequisites now declares parted + grub as deps."""

    def test_parted_in_needed(self) -> None:
        from ltvm_pkg import host_setup

        host = MagicMock()
        host.pkg_mgr = "apt"
        with patch.object(host_setup.shutil, "which", return_value="/u/bin/x"), \
             patch.object(host_setup, "_pkg_install") as install:
            host_setup.check_prerequisites(host)

        # When everything is installed, _pkg_install is only called for
        # podman/pyyaml.  What we care about: the dict *would* list parted.
        # So rerun with parted missing and assert it shows up as a pkg.
        def which(name: str) -> str | None:
            return None if name == "parted" else "/u/bin/" + name

        with patch.object(host_setup.shutil, "which", side_effect=which), \
             patch.object(host_setup, "_pkg_install") as install:
            host_setup.check_prerequisites(host)
        pkgs = [a for call in install.call_args_list for a in call.args]
        assert "parted" in pkgs

    def test_grub_in_needed_apt(self) -> None:
        from ltvm_pkg import host_setup

        host = MagicMock()
        host.pkg_mgr = "apt"

        def which(name: str) -> str | None:
            return None if name == "grub-install" else "/u/bin/" + name

        with patch.object(host_setup.shutil, "which", side_effect=which), \
             patch.object(host_setup, "_pkg_install") as install:
            host_setup.check_prerequisites(host)
        pkgs = [a for call in install.call_args_list for a in call.args]
        assert "grub-pc-bin" in pkgs

    def test_grub_in_needed_dnf(self) -> None:
        from ltvm_pkg import host_setup

        host = MagicMock()
        host.pkg_mgr = "dnf"

        def which(name: str) -> str | None:
            return None if name == "grub2-install" else "/u/bin/" + name

        with patch.object(host_setup.shutil, "which", side_effect=which), \
             patch.object(host_setup, "_pkg_install") as install:
            host_setup.check_prerequisites(host)
        pkgs = [a for call in install.call_args_list for a in call.args]
        assert "grub2-pc" in pkgs
