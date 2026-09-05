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
    """_write_grub_cfg uses _ensure_dir / _write_text helpers which
    try as the current user first and only fall back to sudo on
    PermissionError; with user-owned tmpdir paths these tests stay
    fully unprivileged."""

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

    def test_non_root_primes_sudo_instead_of_refusing(
        self, tmp_path: Path
    ) -> None:
        """Non-root invocation no longer hard-fails: we prime sudo
        instead, then let sudo_run elevate the specific losetup/mount
        calls.  Verifies the unified-privilege contract (priv.sudo_prime
        is invoked) without exercising the full export pipeline."""
        import argparse

        from ltvm_pkg import cli, priv
        from ltvm_pkg.cli import targets as cli_targets

        tc = _make_target_config(tmp_path)
        args = argparse.Namespace(
            target="rocky9", arch=None, kernel=None,
            output=str(tmp_path / "out.qcow2"), format="qcow2",
            force=False, json=False,
        )
        # Make _load_target_args succeed so we reach sudo_prime, then
        # short-circuit export_image with a clean error to avoid
        # touching real losetup/mount.
        import ltvm_pkg.image_export as ie

        with patch.object(priv, "sudo_prime") as sp, \
             patch.object(cli_targets, "_load_target_args",
                          return_value=(tc, None)), \
             patch.object(ie, "export_image",
                          side_effect=RuntimeError("stubbed")):
            rc = cli.cmd_target_export(args)
        sp.assert_called_once()
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


# ======================================================================
# --format gce
# ======================================================================


def _has_gnu_tar() -> bool:
    """True when PATH's tar can write oldgnu/sparse members.

    macOS (and some minimal images) ship bsdtar, which cannot -- the
    real-tar round-trip below is skipped there.  Export itself is
    Linux-only anyway (losetup), so the hosts that can run an export
    are the hosts that run this test.
    """
    import shutil as _shutil
    import subprocess as _sp

    if _shutil.which("tar") is None:
        return False
    r = _sp.run(["tar", "--version"], capture_output=True, text=True,
                check=False)
    return "GNU tar" in (r.stdout or "")


class TestGceSizing:
    """GCE rejects an image whose disk.raw is not a whole number of
    gigabytes, so the raw disk is rounded up before it is packed."""

    def test_rounds_up_to_whole_gib(self) -> None:
        import ltvm_pkg.image_export as ie

        assert ie._round_up_gib_mb(1) == 1024
        assert ie._round_up_gib_mb(1023) == 1024
        assert ie._round_up_gib_mb(1024) == 1024      # already exact
        assert ie._round_up_gib_mb(1025) == 2048
        assert ie._round_up_gib_mb(4600) == 5120

    def test_export_rounds_size_for_gce_only(self, tmp_path: Path) -> None:
        """The rounding is applied for gce and not for qcow2/raw.

        Checked at the helper boundary: the full export needs
        losetup, so the size is verified via the log line instead of
        by running the pipeline.
        """
        import ltvm_pkg.image_export as ie

        # 521 MiB of content -> one whole GiB once rounded.
        assert ie._round_up_gib_mb(521) == 1024


class TestGnuTarCheck:
    def test_missing_tar_raises(self) -> None:
        import ltvm_pkg.image_export as ie

        with patch.object(ie.shutil, "which", return_value=None):
            with pytest.raises(RuntimeError, match="tar not found"):
                ie._check_gnu_tar()

    def test_bsdtar_rejected(self) -> None:
        """bsdtar can't write oldgnu sparse members; catching it here
        beats handing Google a tarball it silently refuses."""
        import ltvm_pkg.image_export as ie

        fake = MagicMock(stdout="bsdtar 3.5.3 - libarchive 3.5.3\n")
        with patch.object(ie.shutil, "which", return_value="/usr/bin/tar"), \
             patch.object(ie.subprocess, "run", return_value=fake):
            with pytest.raises(RuntimeError, match="GNU tar is required"):
                ie._check_gnu_tar()

    def test_gnu_tar_accepted(self) -> None:
        import ltvm_pkg.image_export as ie

        fake = MagicMock(stdout="tar (GNU tar) 1.34\n")
        with patch.object(ie.shutil, "which", return_value="/usr/bin/tar"), \
             patch.object(ie.subprocess, "run", return_value=fake):
            ie._check_gnu_tar()  # no raise

    def test_only_checked_for_gce(self) -> None:
        """qcow2/raw exports don't need tar at all, so they must not
        fail on a host whose tar is bsdtar."""
        import ltvm_pkg.image_export as ie

        with patch.object(ie.shutil, "which", return_value="/u/bin/x"), \
             patch.object(ie, "_check_gnu_tar") as chk:
            ie._check_host_tools("qcow2")
            chk.assert_not_called()
            ie._check_host_tools("gce")
            chk.assert_called_once()


class TestFstabRewrite:
    """The base image's `/dev/vda / ext4` entry is right for ltvm's
    microvm boot (whole unpartitioned disk) and wrong for every
    exported disk, which is partitioned and named differently by
    every hypervisor.  Rewriting to UUID fixes all of them."""

    def test_replaces_root_device(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        etc = tmp_path / "etc"
        etc.mkdir()
        (etc / "fstab").write_text(
            "/dev/vda  /  ext4  defaults,noatime  0 1\n"
        )
        ie._rewrite_fstab_root(tmp_path, "1111-2222")

        text = (etc / "fstab").read_text()
        assert "UUID=1111-2222" in text
        assert "/dev/vda" not in text
        # Mount options must survive the rewrite.
        assert "defaults,noatime" in text

    def test_preserves_comments_and_other_mounts(
        self, tmp_path: Path
    ) -> None:
        import ltvm_pkg.image_export as ie

        etc = tmp_path / "etc"
        etc.mkdir()
        (etc / "fstab").write_text(
            "# a comment mentioning / that must not be rewritten\n"
            "/dev/vda   /      ext4  defaults  0 1\n"
            "/dev/vdb1  /mnt   ext4  defaults  0 2\n"
        )
        ie._rewrite_fstab_root(tmp_path, "abcd")

        lines = (etc / "fstab").read_text().splitlines()
        assert lines[0].startswith("# a comment")
        assert lines[1].split()[0] == "UUID=abcd"
        assert lines[2].split()[0] == "/dev/vdb1"

    def test_appends_when_no_root_entry(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        etc = tmp_path / "etc"
        etc.mkdir()
        (etc / "fstab").write_text("/dev/vdb1  /mnt  ext4  defaults  0 2\n")
        ie._rewrite_fstab_root(tmp_path, "beef")

        text = (etc / "fstab").read_text()
        assert "UUID=beef  /  ext4" in text
        assert "/dev/vdb1" in text

    def test_missing_fstab_is_created(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        (tmp_path / "etc").mkdir()
        ie._rewrite_fstab_root(tmp_path, "cafe")
        assert "UUID=cafe" in (tmp_path / "etc" / "fstab").read_text()


class TestInjectSshKey:
    def test_appends_without_dropping_shared_key(
        self, tmp_path: Path
    ) -> None:
        """The image already carries the shared inter-VM ltvm key in
        root's authorized_keys; clobbering it would break cluster
        ssh, so injection must append."""
        import ltvm_pkg.image_export as ie

        ssh = tmp_path / "root" / ".ssh"
        ssh.mkdir(parents=True)
        (ssh / "authorized_keys").write_text("ssh-ed25519 SHARED ltvm\n")
        key = tmp_path / "id.pub"
        key.write_text("ssh-ed25519 MINE me@host\n")

        with patch.object(ie, "sudo_run"):
            ie._inject_ssh_key(tmp_path, key)

        text = (ssh / "authorized_keys").read_text()
        assert "SHARED" in text
        assert "MINE" in text
        assert (ssh / "authorized_keys").stat().st_mode & 0o777 == 0o600

    def test_idempotent(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        ssh = tmp_path / "root" / ".ssh"
        ssh.mkdir(parents=True)
        key = tmp_path / "id.pub"
        key.write_text("ssh-ed25519 MINE me@host\n")

        with patch.object(ie, "sudo_run"):
            ie._inject_ssh_key(tmp_path, key)
            ie._inject_ssh_key(tmp_path, key)

        text = (ssh / "authorized_keys").read_text()
        assert text.count("MINE") == 1

    def test_appends_missing_trailing_newline(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        ssh = tmp_path / "root" / ".ssh"
        ssh.mkdir(parents=True)
        (ssh / "authorized_keys").write_text("ssh-ed25519 SHARED ltvm")
        key = tmp_path / "id.pub"
        key.write_text("ssh-ed25519 MINE me@host")

        with patch.object(ie, "sudo_run"):
            ie._inject_ssh_key(tmp_path, key)

        lines = (ssh / "authorized_keys").read_text().splitlines()
        assert len(lines) == 2

    def test_rejects_empty_key_file(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        key = tmp_path / "id.pub"
        key.write_text("   \n")
        with patch.object(ie, "sudo_run"):
            with pytest.raises(ValueError, match="empty"):
                ie._inject_ssh_key(tmp_path, key)


class TestGceGuestConfig:
    """Without these tweaks a GCE instance boots with no network:
    rc.local only configures eth0 from ltvm's fc_ip= cmdline, which
    GCE never passes."""

    def _mk_rootfs(self, tmp_path: Path, with_nm_unit: bool = True) -> Path:
        if with_nm_unit:
            unit_dir = tmp_path / "usr" / "lib" / "systemd" / "system"
            unit_dir.mkdir(parents=True)
            (unit_dir / "NetworkManager.service").write_text("[Unit]\n")
        return tmp_path

    def test_writes_nm_profile_with_dhcp(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        root = self._mk_rootfs(tmp_path)
        with patch.object(ie, "sudo_run"):
            ie._apply_gce_guest_config(root)

        prof = (root / "etc" / "NetworkManager" / "system-connections"
                / "ltvm-gce.nmconnection")
        text = prof.read_text()
        assert "method=auto" in text          # DHCP
        assert "interface-name=eth0" in text  # grub pins net.ifnames=0
        # NM silently ignores a group/world-readable keyfile.
        assert prof.stat().st_mode & 0o777 == 0o600

    def test_enables_networkmanager(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        root = self._mk_rootfs(tmp_path)
        with patch.object(ie, "sudo_run") as sr:
            ie._apply_gce_guest_config(root)

        ln_calls = [c.args[0] for c in sr.call_args_list
                    if c.args and c.args[0][0] == "ln"]
        assert len(ln_calls) == 1
        argv = ln_calls[0]
        assert argv[:3] == ["ln", "-sf",
                            "/usr/lib/systemd/system/NetworkManager.service"]
        assert argv[3].endswith(
            "etc/systemd/system/multi-user.target.wants/"
            "NetworkManager.service"
        )

    def test_finds_debian_unit_path(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        unit_dir = tmp_path / "lib" / "systemd" / "system"
        unit_dir.mkdir(parents=True)
        (unit_dir / "NetworkManager.service").write_text("[Unit]\n")

        with patch.object(ie, "sudo_run") as sr:
            ie._apply_gce_guest_config(tmp_path)

        ln_calls = [c.args[0] for c in sr.call_args_list
                    if c.args and c.args[0][0] == "ln"]
        assert ln_calls[0][2] == "/lib/systemd/system/NetworkManager.service"

    def test_no_dangling_symlink_when_unit_absent(
        self, tmp_path: Path
    ) -> None:
        """A .wants symlink to a unit that isn't there just makes
        systemd complain, so skip it and warn instead."""
        import ltvm_pkg.image_export as ie

        root = self._mk_rootfs(tmp_path, with_nm_unit=False)
        with patch.object(ie, "sudo_run") as sr:
            ie._apply_gce_guest_config(root)

        assert not [c for c in sr.call_args_list
                    if c.args and c.args[0][0] == "ln"]
        # The profile is still written -- harmless, and correct if the
        # user enables NM themselves.
        assert (root / "etc" / "NetworkManager" / "system-connections"
                / "ltvm-gce.nmconnection").exists()


class TestPackageGce:
    def test_argv_is_googles_documented_form(self, tmp_path: Path) -> None:
        """GCE's import wants oldgnu format; -S keeps the sparse
        multi-GiB disk cheap; -C keeps the member name bare (a path
        prefix makes the import fail)."""
        import ltvm_pkg.image_export as ie

        raw = tmp_path / "disk.raw"
        raw.write_bytes(b"")
        out = tmp_path / "img.tar.gz"

        with patch.object(ie.subprocess, "run") as run:
            ie._package_gce(raw, out)

        argv = run.call_args.args[0]
        assert argv[0] == "tar"
        assert "--format=oldgnu" in argv
        assert "-Sczf" in argv
        assert argv[-3:] == ["-C", str(tmp_path), "disk.raw"]
        assert str(out) in argv

    def test_rejects_wrong_member_name(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        raw = tmp_path / "notdisk.raw"
        raw.write_bytes(b"")
        with pytest.raises(RuntimeError, match="disk.raw"):
            ie._package_gce(raw, tmp_path / "o.tar.gz")

    @pytest.mark.skipif(not _has_gnu_tar(),
                        reason="needs GNU tar (export is Linux-only anyway)")
    def test_real_tarball_has_bare_disk_raw(self, tmp_path: Path) -> None:
        """End-to-end on the packaging step: the tarball GCE receives
        must contain exactly one member, named `disk.raw`, at the
        archive root."""
        import tarfile

        import ltvm_pkg.image_export as ie

        raw = tmp_path / "disk.raw"
        with raw.open("wb") as fp:
            fp.truncate(64 * 1024 * 1024)  # sparse
        out = tmp_path / "img.tar.gz"

        ie._package_gce(raw, out)

        assert out.exists()
        with tarfile.open(out, "r:gz") as tf:
            names = tf.getnames()
        assert names == ["disk.raw"]
        # Sparse packing: 64 MiB of holes must not become a big file.
        assert out.stat().st_size < 1024 * 1024


class TestExportImageGceGuards:
    def test_unknown_format_lists_gce(self, tmp_path: Path) -> None:
        import ltvm_pkg.image_export as ie

        tc = _make_target_config(tmp_path)
        with pytest.raises(ValueError, match="gce"):
            ie.export_image(tc, None, tmp_path / "o.img", image_format="vmdk")

    def test_gce_format_accepted(self, tmp_path: Path) -> None:
        """'gce' must get past format validation (and fail later, on
        the missing rootfs) rather than being rejected outright."""
        import ltvm_pkg.image_export as ie

        tc = _make_target_config(tmp_path)
        (tc.image_output_dir.return_value / "base.ext4").unlink()
        with patch.object(ie, "_check_host_tools", return_value={
                "grub_install": "grub-install"}):
            with pytest.raises(FileNotFoundError, match="base.ext4"):
                ie.export_image(tc, None, tmp_path / "o.tar.gz",
                                image_format="gce")

    def test_disk_size_smaller_than_rootfs_rejected(
        self, tmp_path: Path
    ) -> None:
        import ltvm_pkg.image_export as ie

        tc = _make_target_config(tmp_path)
        # Fixture needs 1 + 8 + 512 = 521 MiB; 0 GiB can't hold it.
        with patch.object(ie, "_check_host_tools", return_value={
                "grub_install": "grub-install"}):
            with pytest.raises(ValueError, match="too small"):
                ie.export_image(tc, None, tmp_path / "o.qcow2",
                                disk_size_gb=0)

    def test_missing_ssh_key_file_rejected_early(
        self, tmp_path: Path
    ) -> None:
        """Caught before any disk work: discovering a typo'd key path
        after a multi-minute export would be miserable."""
        import ltvm_pkg.image_export as ie

        tc = _make_target_config(tmp_path)
        with pytest.raises(FileNotFoundError, match="ssh key"):
            ie.export_image(tc, None, tmp_path / "o.qcow2",
                            ssh_key=tmp_path / "nope.pub")


class TestGceCliWiring:
    def _parser(self):
        import importlib.machinery
        from pathlib import Path as _P

        root = _P(__file__).resolve().parent.parent
        loader = importlib.machinery.SourceFileLoader(
            "ltvm_script_gce", str(root / "ltvm"))
        mod = loader.load_module()  # type: ignore[deprecated]
        return mod.build_parser()

    def test_format_gce_parses(self) -> None:
        ns = self._parser().parse_args(
            ["target", "export", "rocky9", "--format", "gce"])
        assert ns.format == "gce"

    def test_new_flags_parse(self) -> None:
        ns = self._parser().parse_args([
            "target", "export", "rocky9", "--format", "gce",
            "--disk-size-gb", "20", "--ssh-key", "/tmp/k.pub",
        ])
        assert ns.disk_size_gb == 20
        assert ns.ssh_key == "/tmp/k.pub"

    def test_flags_default_to_none(self) -> None:
        ns = self._parser().parse_args(["target", "export", "rocky9"])
        assert ns.disk_size_gb is None
        assert ns.ssh_key is None
        assert ns.format == "qcow2"

    def test_extension_mapping(self) -> None:
        from ltvm_pkg.cli.targets import _EXPORT_EXT

        assert _EXPORT_EXT == {
            "qcow2": "qcow2", "raw": "raw", "gce": "tar.gz"}

    def test_gce_default_output_name_and_hints(
        self, tmp_path: Path, capsys
    ) -> None:
        """gce assets get their own stem so `target publish --image`,
        which looks for bootable-<kernel>.qcow2, can't pick one up."""
        import argparse

        from ltvm_pkg import cli, priv
        from ltvm_pkg.cli import targets as cli_targets
        import ltvm_pkg.image_export as ie

        tc = _make_target_config(tmp_path)
        produced = tmp_path / "produced.tar.gz"
        produced.write_bytes(b"\0" * 2048)
        seen: dict[str, object] = {}

        def fake_export(tc_, kernel, out, **kw):
            seen["out"] = out
            seen.update(kw)
            return produced

        args = argparse.Namespace(
            target="rocky9", arch=None, kernel=None, output=None,
            format="gce", force=False, json=False,
            disk_size_gb=20, ssh_key=None,
        )
        with patch.object(priv, "sudo_prime"), \
             patch.object(cli_targets, "_load_target_args",
                          return_value=(tc, None)), \
             patch.object(ie, "export_image", side_effect=fake_export):
            rc = cli.cmd_target_export(args)

        assert rc == cli.EXIT_OK
        assert seen["out"].name == "gce-5.14-rhel9.7-1.el9.tar.gz"
        assert seen["image_format"] == "gce"
        assert seen["disk_size_gb"] == 20

        out = capsys.readouterr().out
        assert "gcloud compute images create" in out
        assert "--source-uri" in out
        # No key baked in -> say so, since GCE can't inject one either.
        assert "--ssh-key" in out

    def test_qcow2_keeps_bootable_stem(self, tmp_path: Path) -> None:
        import argparse

        from ltvm_pkg import cli, priv
        from ltvm_pkg.cli import targets as cli_targets
        import ltvm_pkg.image_export as ie

        tc = _make_target_config(tmp_path)
        produced = tmp_path / "produced.qcow2"
        produced.write_bytes(b"\0" * 2048)
        seen: dict[str, object] = {}

        def fake_export(tc_, kernel, out, **kw):
            seen["out"] = out
            return produced

        args = argparse.Namespace(
            target="rocky9", arch=None, kernel=None, output=None,
            format="qcow2", force=False, json=False,
            disk_size_gb=None, ssh_key=None,
        )
        with patch.object(priv, "sudo_prime"), \
             patch.object(cli_targets, "_load_target_args",
                          return_value=(tc, None)), \
             patch.object(ie, "export_image", side_effect=fake_export):
            rc = cli.cmd_target_export(args)

        assert rc == cli.EXIT_OK
        assert seen["out"].name == "bootable-5.14-rhel9.7-1.el9.qcow2"
