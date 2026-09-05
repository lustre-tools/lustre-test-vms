"""Tests for ltvm_pkg/local_install.py and the make-* subcommands.

These cover ltvm running *inside* a machine it built -- an ltvm VM or
a cloud node from `ltvm target export --format gce` -- installing
Lustre onto that machine's own root filesystem.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _has_gnu_tar() -> bool:
    import shutil as _shutil

    if _shutil.which("tar") is None:
        return False
    r = subprocess.run(["tar", "--version"], capture_output=True, text=True,
                       check=False)
    return "GNU tar" in (r.stdout or "")


def _stamp(**over) -> dict:
    base = {
        "schema": "ltvm-image/1",
        "target": "rocky9",
        "arch": "x86_64",
        "variant": "base",
        "kernel": "5.14-rhel9.7-1.el9",
        "kernel_version": "5.14.0-503.ltvm.el9.x86_64",
        "os_family": "rhel",
        "built": 1234567890,
    }
    base.update(over)
    return base


def _lustre_tree(tmp_path: Path, name: str = "lustre-release") -> Path:
    """Minimal tree that satisfies check_in_lustre_tree."""
    tree = tmp_path / name
    (tree / "lustre" / "kernel_patches").mkdir(parents=True)
    (tree / "lnet").mkdir(parents=True)
    (tree / "configure.ac").write_text("AC_INIT([lustre],[2.17])\n")
    return tree


def _no_evidence(tmp_path: Path):
    """Patch out every form of ltvm-machine evidence.

    Needed because two of the three are absolute host paths: a Linux
    dev box running these tests could genuinely have /proc/cmdline and
    /usr/local/sbin, and the "refuses" tests must not depend on it.
    """
    import ltvm_pkg.local_install as li

    return [
        patch.object(li.platform, "system", return_value="Linux"),
        patch.object(li, "IMAGE_STAMP_PATH", tmp_path / "absent.json"),
        patch.object(li, "_LTVM_IMAGE_MARKERS", (tmp_path / "absent.sh",)),
        patch.object(li, "_read_text", return_value=""),
    ]


def _write_stamp(tmp_path: Path, **over) -> Path:
    p = tmp_path / "ltvm-image.json"
    p.write_text(json.dumps(_stamp(**over)))
    return p


# ======================================================================
# Reading the image stamp
# ======================================================================


class TestReadImageStamp:
    def test_reads_a_valid_stamp(self, tmp_path: Path) -> None:
        from ltvm_pkg.local_install import read_image_stamp

        img = read_image_stamp(_write_stamp(tmp_path))
        assert img is not None
        assert img.target == "rocky9"
        assert img.arch == "x86_64"
        assert img.kernel == "5.14-rhel9.7-1.el9"
        assert img.kernel_version == "5.14.0-503.ltvm.el9.x86_64"
        assert img.source == "stamp"

    def test_missing_file_is_none_not_error(self, tmp_path: Path) -> None:
        """A machine with no stamp is the normal legacy case, not a
        failure -- the caller falls back to sniffing."""
        from ltvm_pkg.local_install import read_image_stamp

        assert read_image_stamp(tmp_path / "nope.json") is None

    def test_malformed_json_is_none(self, tmp_path: Path) -> None:
        from ltvm_pkg.local_install import read_image_stamp

        p = tmp_path / "s.json"
        p.write_text("{not json")
        assert read_image_stamp(p) is None

    def test_unknown_schema_is_none(self, tmp_path: Path) -> None:
        """Refuse to guess at a stamp written by a newer ltvm rather
        than misread its fields."""
        from ltvm_pkg.local_install import read_image_stamp

        assert read_image_stamp(
            _write_stamp(tmp_path, schema="ltvm-image/99")) is None

    def test_missing_required_field_is_none(self, tmp_path: Path) -> None:
        from ltvm_pkg.local_install import read_image_stamp

        p = tmp_path / "s.json"
        raw = _stamp()
        del raw["kernel"]
        p.write_text(json.dumps(raw))
        assert read_image_stamp(p) is None

    def test_non_dict_json_is_none(self, tmp_path: Path) -> None:
        from ltvm_pkg.local_install import read_image_stamp

        p = tmp_path / "s.json"
        p.write_text("[1, 2, 3]")
        assert read_image_stamp(p) is None

    def test_variant_defaults_to_base(self, tmp_path: Path) -> None:
        from ltvm_pkg.local_install import read_image_stamp

        img = read_image_stamp(_write_stamp(tmp_path, variant=""))
        assert img is not None and img.variant == "base"


# ======================================================================
# os-release fallback
# ======================================================================


class TestOsReleaseFallback:
    def _osr(self, tmp_path: Path, text: str) -> Path:
        p = tmp_path / "os-release"
        p.write_text(text)
        return p

    def test_parses_quoted_values(self, tmp_path: Path) -> None:
        from ltvm_pkg.local_install import _parse_os_release

        p = self._osr(tmp_path,
                      '# comment\nID="rocky"\nVERSION_ID="9.7"\n\nX=y\n')
        got = _parse_os_release(p)
        assert got["ID"] == "rocky"
        assert got["VERSION_ID"] == "9.7"
        assert got["X"] == "y"

    def test_matches_on_major_version(self, tmp_path: Path) -> None:
        """The node's minor version drifts from targets.yaml as it
        takes updates, so match on ID + major only."""
        from ltvm_pkg.local_install import detect_targets_from_os_release

        p = self._osr(tmp_path, 'ID=ubuntu\nVERSION_ID="24.04"\n')
        assert detect_targets_from_os_release(p) == ["ubuntu2404"]

    def test_rocky9_is_ambiguous(self, tmp_path: Path) -> None:
        """rocky9 and rocky9-64k are the same OS; os-release cannot
        tell them apart, and the caller must ask for --target."""
        from ltvm_pkg.local_install import detect_targets_from_os_release

        p = self._osr(tmp_path, 'ID=rocky\nVERSION_ID="9.7"\n')
        assert sorted(detect_targets_from_os_release(p)) == [
            "rocky9", "rocky9-64k"]

    def test_unknown_os_gives_no_candidates(self, tmp_path: Path) -> None:
        from ltvm_pkg.local_install import detect_targets_from_os_release

        p = self._osr(tmp_path, 'ID=arch\nVERSION_ID="rolling"\n')
        assert detect_targets_from_os_release(p) == []

    def test_missing_file_gives_no_candidates(self, tmp_path: Path) -> None:
        from ltvm_pkg.local_install import detect_targets_from_os_release

        assert detect_targets_from_os_release(tmp_path / "nope") == []


# ======================================================================
# The "am I allowed to write to /" guard
# ======================================================================


class TestMachineGuard:
    def test_refuses_on_non_linux(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        with patch.object(li.platform, "system", return_value="Darwin"):
            with pytest.raises(li.LocalInstallError, match="only runs on Linux"):
                li.check_is_ltvm_machine()

    def test_refuses_without_any_evidence(self, tmp_path: Path) -> None:
        """The whole point: typing make-install on a build host must
        not scatter Lustre across the user's workstation."""
        import ltvm_pkg.local_install as li
        from contextlib import ExitStack

        with ExitStack() as st:
            for cm in _no_evidence(tmp_path):
                st.enter_context(cm)
            with pytest.raises(li.LocalInstallError,
                               match="does not look like one ltvm built"):
                li.check_is_ltvm_machine()

    def test_error_points_at_deploy_lustre(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li
        from contextlib import ExitStack

        with ExitStack() as st:
            for cm in _no_evidence(tmp_path):
                st.enter_context(cm)
            with pytest.raises(li.LocalInstallError) as e:
                li.check_is_ltvm_machine()
        assert "deploy-lustre" in str(e.value)

    def test_force_overrides(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li
        from contextlib import ExitStack

        with ExitStack() as st:
            for cm in _no_evidence(tmp_path):
                st.enter_context(cm)
            assert li.check_is_ltvm_machine(force=True) == "forced"

    def test_stamp_is_evidence(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        with patch.object(li.platform, "system", return_value="Linux"), \
             patch.object(li, "IMAGE_STAMP_PATH", _write_stamp(tmp_path)):
            assert "image stamp" in li.check_is_ltvm_machine()

    def test_ltvm_kernel_cmdline_is_evidence(self, tmp_path: Path) -> None:
        """Every VM built before the stamp existed still proves itself
        this way: qemu_run hands the guest its address on the cmdline.
        Taken from a real ltvm VM's /proc/cmdline."""
        import ltvm_pkg.local_install as li

        cmdline = (
            "console=ttyAMA0 reboot=k panic=1 crashkernel=512M "
            "net.ifnames=0 biosdevname=0 root=/dev/vda rw "
            "fc_ip=192.168.105.182 fc_gw=192.168.105.1 fc_name=co1-mkinst"
        )
        with patch.object(li.platform, "system", return_value="Linux"), \
             patch.object(li, "IMAGE_STAMP_PATH", tmp_path / "absent.json"), \
             patch.object(li, "_LTVM_IMAGE_MARKERS", (tmp_path / "absent.sh",)), \
             patch.object(li, "_read_text", return_value=cmdline):
            assert "kernel cmdline" in li.check_is_ltvm_machine()

    def test_image_markers_are_evidence(self, tmp_path: Path) -> None:
        """A cloud node from `target export` gets no ltvm cmdline, so
        the rootfs markers are what identify it."""
        import ltvm_pkg.local_install as li

        marker = tmp_path / "setup-lnet-config.sh"
        marker.write_text("#!/bin/sh\n")
        with patch.object(li.platform, "system", return_value="Linux"), \
             patch.object(li, "IMAGE_STAMP_PATH", tmp_path / "absent.json"), \
             patch.object(li, "_LTVM_IMAGE_MARKERS", (marker,)), \
             patch.object(li, "_read_text", return_value=""):
            assert "image marker" in li.check_is_ltvm_machine()

    def test_plain_rhel_box_is_not_evidence(self, tmp_path: Path) -> None:
        """A normal RHEL workstation has /proc/cmdline and may have
        /usr/local/sbin -- neither must count."""
        import ltvm_pkg.local_install as li

        with patch.object(li.platform, "system", return_value="Linux"), \
             patch.object(li, "IMAGE_STAMP_PATH", tmp_path / "absent.json"), \
             patch.object(li, "_LTVM_IMAGE_MARKERS", (tmp_path / "absent.sh",)), \
             patch.object(li, "_read_text",
                          return_value="BOOT_IMAGE=/vmlinuz root=/dev/sda1 ro"):
            assert li.ltvm_machine_evidence() is None


class TestLustreTreeGuard:
    """make-install builds what the tree holds and make-uninstall is
    the other half of that pair, so both are source-tree commands."""

    def test_accepts_a_lustre_tree(self, tmp_path: Path) -> None:
        from ltvm_pkg.local_install import check_in_lustre_tree

        tree = _lustre_tree(tmp_path)
        assert check_in_lustre_tree(tree) == tree.resolve()

    def test_rejects_a_non_lustre_dir(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        with pytest.raises(li.LocalInstallError, match="not a Lustre source tree"):
            li.check_in_lustre_tree(tmp_path)

    def test_names_what_is_missing(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        tree = _lustre_tree(tmp_path)
        (tree / "configure.ac").unlink()
        with pytest.raises(li.LocalInstallError, match="configure.ac"):
            li.check_in_lustre_tree(tree)

    def test_defaults_to_cwd(self, tmp_path: Path, monkeypatch) -> None:
        from ltvm_pkg.local_install import check_in_lustre_tree

        tree = _lustre_tree(tmp_path)
        monkeypatch.chdir(tree)
        assert check_in_lustre_tree(None) == tree.resolve()

    def test_missing_dir_is_a_clean_error(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        with pytest.raises(li.LocalInstallError, match="Not a directory"):
            li.check_in_lustre_tree(tmp_path / "nope")


# ======================================================================
# Resolving which target this machine is
# ======================================================================


class TestResolveLocalImage:
    def test_uses_the_stamp(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        with patch.object(li, "IMAGE_STAMP_PATH", _write_stamp(tmp_path)):
            img = li.resolve_local_image()
        assert img.target == "rocky9"
        assert img.variant == "base"
        assert img.source == "stamp"
        assert img.os_family == "rhel"

    def test_explicit_target_overrides_stamp(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        with patch.object(li, "IMAGE_STAMP_PATH", _write_stamp(tmp_path)):
            img = li.resolve_local_image(explicit_target="rocky8")
        assert img.target == "rocky8"
        assert img.source == "explicit"

    def test_falls_back_to_os_release(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        with patch.object(li, "IMAGE_STAMP_PATH", tmp_path / "absent.json"), \
             patch.object(li, "detect_targets_from_os_release",
                          return_value=["ubuntu2404"]):
            img = li.resolve_local_image()
        assert img.target == "ubuntu2404"
        assert img.source == "os-release"

    def test_ambiguous_fallback_demands_explicit_target(
        self, tmp_path: Path
    ) -> None:
        import ltvm_pkg.local_install as li

        with patch.object(li, "IMAGE_STAMP_PATH", tmp_path / "absent.json"), \
             patch.object(li, "detect_targets_from_os_release",
                          return_value=["rocky9", "rocky9-64k"]):
            with pytest.raises(li.LocalInstallError, match="more than one"):
                li.resolve_local_image()

    def test_no_candidates_demands_explicit_target(
        self, tmp_path: Path
    ) -> None:
        import ltvm_pkg.local_install as li

        with patch.object(li, "IMAGE_STAMP_PATH", tmp_path / "absent.json"), \
             patch.object(li, "detect_targets_from_os_release",
                          return_value=[]):
            with pytest.raises(li.LocalInstallError, match="--target"):
                li.resolve_local_image()

    def test_unknown_target_is_a_clean_error(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        with patch.object(li, "IMAGE_STAMP_PATH", tmp_path / "absent.json"):
            with pytest.raises(li.LocalInstallError, match="Unknown target"):
                li.resolve_local_image(explicit_target="nosuchtarget")


class TestKernelMatch:
    def test_silent_when_kernels_agree(self) -> None:
        import ltvm_pkg.local_install as li

        img = li.LocalImage("rocky9", "x86_64", "base", "k", "", "rhel", "stamp")
        with patch.object(li.platform, "release", return_value="5.14.0-x"):
            assert li.check_kernel_match(img, "5.14.0-x") is None

    def test_warns_on_mismatch(self) -> None:
        """Modules built for another kernel install fine and then fail
        to load much later; say it at install time instead."""
        import ltvm_pkg.local_install as li

        img = li.LocalImage("rocky9", "x86_64", "base", "k", "", "rhel", "stamp")
        with patch.object(li.platform, "release", return_value="5.14.0-run"):
            msg = li.check_kernel_match(img, "5.14.0-built")
        assert msg is not None
        assert "5.14.0-built" in msg and "5.14.0-run" in msg


# ======================================================================
# Staging inventory
# ======================================================================


def _make_staging(tmp_path: Path) -> Path:
    staging = tmp_path / "staging"
    (staging / "usr" / "sbin").mkdir(parents=True)
    (staging / "lib" / "modules" / "5.14.0" / "extra" / "lustre").mkdir(
        parents=True)
    (staging / "usr" / "sbin" / "mount.lustre").write_text("#!/bin/sh\n")
    (staging / "lib" / "modules" / "5.14.0" / "extra" / "lustre"
     / "lustre.ko").write_bytes(b"\x7fELF")
    (staging / ".ltvm-staging-stamp").write_text("stamp")
    return staging


class TestStagingContents:
    def test_lists_files_relative_and_sorted(self, tmp_path: Path) -> None:
        from ltvm_pkg.local_install import staging_contents

        files, _ = staging_contents(_make_staging(tmp_path))
        assert "usr/sbin/mount.lustre" in files
        assert "lib/modules/5.14.0/extra/lustre/lustre.ko" in files
        assert files == sorted(files)

    def test_skips_build_bookkeeping(self, tmp_path: Path) -> None:
        """.ltvm-staging-stamp is the build's own marker, not part of
        the install -- copying it to / then deleting it is noise."""
        from ltvm_pkg.local_install import staging_contents

        files, _ = staging_contents(_make_staging(tmp_path))
        assert not any(f.startswith(".ltvm-") for f in files)

    def test_dirs_are_deepest_first(self, tmp_path: Path) -> None:
        """Uninstall rmdirs bottom-up, so the order matters."""
        from ltvm_pkg.local_install import staging_contents

        _, dirs = staging_contents(_make_staging(tmp_path))
        depths = [d.count("/") for d in dirs]
        assert depths == sorted(depths, reverse=True)
        assert "lib/modules/5.14.0/extra/lustre" in dirs
        assert dirs.index("lib/modules/5.14.0/extra/lustre") < dirs.index("lib")

    def test_includes_symlinks_as_files(self, tmp_path: Path) -> None:
        from ltvm_pkg.local_install import staging_contents

        staging = _make_staging(tmp_path)
        (staging / "usr" / "sbin" / "mount.lustre_tgt").symlink_to(
            "mount.lustre")
        files, _ = staging_contents(staging)
        assert "usr/sbin/mount.lustre_tgt" in files


# ======================================================================
# Installing into a root
# ======================================================================


class TestInstallIntoRoot:
    def test_missing_staging_is_a_clean_error(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        with pytest.raises(li.LocalInstallError, match="Staging directory"):
            li.install_staging_into_root(tmp_path / "nope", tmp_path)

    @pytest.mark.skipif(
        not _has_gnu_tar(),
        reason="needs GNU tar (--keep-directory-symlink); "
               "make-install is Linux-only anyway")
    def test_unpacks_tree_into_root(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        staging = _make_staging(tmp_path)
        root = tmp_path / "root"
        root.mkdir()

        li.install_staging_into_root(staging, root)

        assert (root / "usr" / "sbin" / "mount.lustre").is_file()
        assert (root / "lib" / "modules" / "5.14.0" / "extra" / "lustre"
                / "lustre.ko").is_file()

    @pytest.mark.skipif(
        not _has_gnu_tar(),
        reason="needs GNU tar (--keep-directory-symlink)")
    def test_keeps_lib_as_a_symlink(self, tmp_path: Path) -> None:
        """On RHEL /lib is a symlink to /usr/lib.  Replacing it with a
        real directory would strand every library on the system, so
        the extraction must follow it instead."""
        import ltvm_pkg.local_install as li

        staging = _make_staging(tmp_path)
        root = tmp_path / "root"
        (root / "usr" / "lib").mkdir(parents=True)
        (root / "lib").symlink_to("usr/lib")

        li.install_staging_into_root(staging, root)

        assert (root / "lib").is_symlink()
        assert (root / "usr" / "lib" / "modules" / "5.14.0" / "extra"
                / "lustre" / "lustre.ko").is_file()


# ======================================================================
# Manifest
# ======================================================================


class TestManifest:
    def test_round_trip(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        img = li.LocalImage("rocky9", "x86_64", "base", "k",
                            "5.14.0-x", "rhel", "stamp")
        path = tmp_path / "manifest.json"
        li.write_manifest(img, tmp_path / "staging", tmp_path / "tree",
                          "5.14.0-x", ["usr/sbin/a"], ["usr/sbin"], path)

        got = li.read_manifest(path)
        assert got is not None
        assert got["files"] == ["usr/sbin/a"]
        assert got["kernel_version"] == "5.14.0-x"
        assert got["image"]["target"] == "rocky9"

    def test_missing_manifest_is_none(self, tmp_path: Path) -> None:
        from ltvm_pkg.local_install import read_manifest

        assert read_manifest(tmp_path / "nope.json") is None

    def test_wrong_schema_raises(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        p = tmp_path / "m.json"
        p.write_text(json.dumps({"schema": "other/1"}))
        with pytest.raises(li.LocalInstallError, match="schema"):
            li.read_manifest(p)


# ======================================================================
# Uninstall
# ======================================================================


def _nosudo(cmd, check=True, quiet=False):
    """Stand-in for priv.sudo_run that runs without elevating, so the
    removal tests exercise the real rm/rmdir against a temp root."""
    return subprocess.run(cmd, capture_output=True, text=True)


class TestSafeRelative:
    @pytest.mark.parametrize("bad", [
        "", "/etc/passwd", "../../etc/passwd", "usr/../../etc/passwd",
        "-rf", ".",
    ])
    def test_rejects_unsafe(self, bad: str) -> None:
        from ltvm_pkg.local_install import _safe_relative

        assert not _safe_relative(bad)

    @pytest.mark.parametrize("good", [
        "usr/sbin/mount.lustre", "lib/modules/5.14.0/extra/lustre/lustre.ko",
    ])
    def test_accepts_normal_paths(self, good: str) -> None:
        from ltvm_pkg.local_install import _safe_relative

        assert _safe_relative(good)


class TestRemoveInstalledFiles:
    def test_removes_listed_files_only(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        (tmp_path / "usr" / "sbin").mkdir(parents=True)
        (tmp_path / "usr" / "sbin" / "mine").write_text("x")
        (tmp_path / "usr" / "sbin" / "theirs").write_text("x")

        with patch.object(li, "sudo_run", _nosudo):
            n = li.remove_installed_files(["usr/sbin/mine"], tmp_path)

        assert n == 1
        assert not (tmp_path / "usr" / "sbin" / "mine").exists()
        assert (tmp_path / "usr" / "sbin" / "theirs").exists()

    def test_skips_unsafe_entries(self, tmp_path: Path) -> None:
        """The manifest is a writable file on a machine people poke
        at, and every entry becomes an rm argument."""
        import ltvm_pkg.local_install as li

        victim = tmp_path / "victim"
        victim.write_text("x")

        with patch.object(li, "sudo_run", _nosudo):
            n = li.remove_installed_files(
                ["../victim", "/etc/passwd", "-rf"], tmp_path / "root")

        assert n == 0
        assert victim.exists()

    def test_tolerates_already_missing(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        with patch.object(li, "sudo_run", _nosudo):
            assert li.remove_installed_files(["usr/gone"], tmp_path) == 0

    def test_removes_symlinks(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        (tmp_path / "usr").mkdir()
        (tmp_path / "usr" / "target").write_text("x")
        (tmp_path / "usr" / "link").symlink_to("target")

        with patch.object(li, "sudo_run", _nosudo):
            n = li.remove_installed_files(["usr/link"], tmp_path)

        assert n == 1
        assert not (tmp_path / "usr" / "link").exists()
        assert (tmp_path / "usr" / "target").exists()

    def test_batches_large_lists(self, tmp_path: Path) -> None:
        """Long file lists must not blow past ARG_MAX in one rm."""
        import ltvm_pkg.local_install as li

        d = tmp_path / "usr"
        d.mkdir()
        names = []
        for i in range(li._RM_CHUNK + 5):
            (d / f"f{i}").write_text("x")
            names.append(f"usr/f{i}")

        calls = []

        def rec(cmd, check=True, quiet=False):
            calls.append(cmd)
            return _nosudo(cmd, check=check, quiet=quiet)

        with patch.object(li, "sudo_run", rec):
            n = li.remove_installed_files(names, tmp_path)

        assert n == li._RM_CHUNK + 5
        assert len(calls) == 2
        assert not any(d.iterdir())


class TestPruneEmptyDirs:
    def test_prunes_empty_and_keeps_populated(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        (tmp_path / "opt" / "empty").mkdir(parents=True)
        (tmp_path / "opt" / "full").mkdir(parents=True)
        (tmp_path / "opt" / "full" / "other").write_text("x")

        with patch.object(li, "sudo_run", _nosudo):
            n = li.prune_empty_dirs(["opt/empty", "opt/full"], tmp_path)

        assert n == 1
        assert not (tmp_path / "opt" / "empty").exists()
        assert (tmp_path / "opt" / "full").exists()

    def test_never_prunes_system_dirs(self, tmp_path: Path) -> None:
        """rmdir refuses non-empty dirs anyway; this is the second
        lock on the door for the paths where being wrong is fatal."""
        import ltvm_pkg.local_install as li

        (tmp_path / "usr" / "sbin").mkdir(parents=True)

        with patch.object(li, "sudo_run", _nosudo):
            n = li.prune_empty_dirs(["usr/sbin", "usr", "lib/modules"],
                                    tmp_path)

        assert n == 0
        assert (tmp_path / "usr" / "sbin").is_dir()

    def test_ignores_symlinked_dirs(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        (tmp_path / "opt" / "real").mkdir(parents=True)
        (tmp_path / "opt" / "link").symlink_to("real")

        with patch.object(li, "sudo_run", _nosudo):
            n = li.prune_empty_dirs(["opt/link"], tmp_path)

        assert n == 0
        assert (tmp_path / "opt" / "link").is_symlink()


class TestModuleUnload:
    def _procmods(self, tmp_path: Path, text: str) -> Path:
        p = tmp_path / "modules"
        p.write_text(text)
        return p

    def test_lists_loaded_lustre_modules(self, tmp_path: Path) -> None:
        from ltvm_pkg.local_install import loaded_lustre_modules

        p = self._procmods(
            tmp_path,
            "lustre 1 0 - Live 0x0\nlnet 2 0 - Live 0x0\next4 3 0 - Live 0x0\n"
        )
        assert loaded_lustre_modules(p) == ["lustre", "lnet"]

    def test_missing_proc_modules_is_empty(self, tmp_path: Path) -> None:
        from ltvm_pkg.local_install import loaded_lustre_modules

        assert loaded_lustre_modules(tmp_path / "nope") == []

    def test_clean_when_nothing_loaded(self) -> None:
        import ltvm_pkg.local_install as li

        with patch.object(li, "loaded_lustre_modules", return_value=[]):
            clean, msg = li.unload_lustre_modules()
        assert clean and "no Lustre modules" in msg

    def test_uses_lustre_rmmod_when_available(self) -> None:
        import ltvm_pkg.local_install as li

        with patch.object(li, "loaded_lustre_modules",
                          side_effect=[["lustre"], []]), \
             patch.object(li.shutil, "which", return_value="/usr/sbin/lustre_rmmod"), \
             patch.object(li, "sudo_run") as sr:
            clean, msg = li.unload_lustre_modules()

        assert clean
        assert sr.call_args_list[0].args[0] == ["lustre_rmmod"]

    def test_reports_modules_that_survive(self) -> None:
        """Deleting a loaded module's file leaves a machine whose
        running Lustre matches nothing on disk -- say so."""
        import ltvm_pkg.local_install as li

        with patch.object(li, "loaded_lustre_modules",
                          side_effect=[["lustre"], ["lustre"]]), \
             patch.object(li.shutil, "which", return_value=None), \
             patch.object(li, "sudo_run"):
            clean, msg = li.unload_lustre_modules()

        assert not clean
        assert "still loaded" in msg and "lustre" in msg


# ======================================================================
# CLI
# ======================================================================


def _parser():
    import importlib.machinery

    root = Path(__file__).resolve().parent.parent
    loader = importlib.machinery.SourceFileLoader(
        "ltvm_script_make", str(root / "ltvm"))
    mod = loader.load_module()  # type: ignore[deprecated]
    return mod.build_parser()


class TestMakeCliWiring:
    @pytest.mark.parametrize("cmd,func", [
        ("make-install", "cmd_make_install"),
        ("make-uninstall", "cmd_make_uninstall"),
        ("make-reinstall", "cmd_make_reinstall"),
    ])
    def test_subcommands_registered(self, cmd: str, func: str) -> None:
        ns = _parser().parse_args([cmd])
        assert ns.func.__name__ == func

    def test_install_flags(self) -> None:
        ns = _parser().parse_args([
            "make-install", "--lustre-tree", "/src/lustre", "--target",
            "rocky9", "--variant", "mofed-24", "--jobs", "8", "--rebuild",
            "--force",
        ])
        assert ns.lustre_tree == "/src/lustre"
        assert ns.target == "rocky9"
        assert ns.variant == "mofed-24"
        assert ns.jobs == 8
        assert ns.rebuild is True
        assert ns.force is True

    def test_uninstall_flags(self) -> None:
        ns = _parser().parse_args(
            ["make-uninstall", "--no-unload", "--lustre-tree", "/src"])
        assert ns.no_unload is True
        assert ns.lustre_tree == "/src"

    def test_reinstall_takes_both_sides_flags(self) -> None:
        ns = _parser().parse_args([
            "make-reinstall", "--no-unload", "--lustre-tree", "/src"])
        assert ns.no_unload is True
        assert ns.lustre_tree == "/src"

    def test_defaults(self) -> None:
        ns = _parser().parse_args(["make-install"])
        assert ns.target is None
        assert ns.variant is None
        assert ns.force is False
        assert ns.lustre_tree is None


class TestMakeCommandBehaviour:
    def _args(self, tmp_path: Path | None = None, **over):
        import argparse

        base = dict(
            json=False, target=None, variant=None, kernel=None, arch=None,
            force=False, force_compat=False, jobs=None,
            rebuild=False, no_unload=False,
            lustre_tree=str(_lustre_tree(tmp_path)) if tmp_path else None,
        )
        base.update(over)
        return argparse.Namespace(**base)

    def _on_ltvm(self, tmp_path: Path):
        import ltvm_pkg.local_install as li

        return [
            patch.object(li.platform, "system", return_value="Linux"),
            patch.object(li, "IMAGE_STAMP_PATH", _write_stamp(tmp_path)),
        ]

    def test_install_refuses_off_an_ltvm_machine(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li
        from contextlib import ExitStack

        from ltvm_pkg.cli.make import cmd_make_install

        with ExitStack() as st:
            for cm in _no_evidence(tmp_path):
                st.enter_context(cm)
            rc = cmd_make_install(self._args(tmp_path))
        assert rc != 0

    def test_install_refuses_outside_a_lustre_tree(
        self, tmp_path: Path, capsys
    ) -> None:
        """On an ltvm machine, but cwd isn't a checkout."""
        import ltvm_pkg.cli.make as mk
        from contextlib import ExitStack

        notatree = tmp_path / "somewhere"
        notatree.mkdir()
        with ExitStack() as st:
            for cm in self._on_ltvm(tmp_path):
                st.enter_context(cm)
            rc = mk.cmd_make_install(
                self._args(lustre_tree=str(notatree)))
        assert rc != 0
        assert "not a Lustre source tree" in capsys.readouterr().err

    def test_uninstall_refuses_outside_a_lustre_tree(
        self, tmp_path: Path
    ) -> None:
        import ltvm_pkg.cli.make as mk
        from contextlib import ExitStack

        notatree = tmp_path / "somewhere"
        notatree.mkdir()
        with ExitStack() as st:
            for cm in self._on_ltvm(tmp_path):
                st.enter_context(cm)
            rc = mk.cmd_make_uninstall(
                self._args(lustre_tree=str(notatree)))
        assert rc != 0

    def test_uninstall_without_manifest_errors(self, tmp_path: Path) -> None:
        import ltvm_pkg.cli.make as mk
        from contextlib import ExitStack

        with ExitStack() as st:
            for cm in self._on_ltvm(tmp_path):
                st.enter_context(cm)
            st.enter_context(
                patch.object(mk, "read_manifest", return_value=None))
            rc = mk.cmd_make_uninstall(self._args(tmp_path))
        assert rc != 0

    def test_reinstall_tolerates_nothing_installed(
        self, tmp_path: Path, capsys
    ) -> None:
        """Reinstall is what people reach for after editing source;
        refusing because there was no prior ltvm install would just be
        in the way."""
        import ltvm_pkg.local_install as li
        import ltvm_pkg.cli.make as mk

        with patch.object(li.platform, "system", return_value="Linux"), \
             patch.object(li, "IMAGE_STAMP_PATH", _write_stamp(tmp_path)), \
             patch.object(mk, "read_manifest", return_value=None), \
             patch.object(mk, "_do_install", return_value=0) as inst:
            rc = mk.cmd_make_reinstall(self._args(tmp_path))

        assert rc == 0
        inst.assert_called_once()
        assert "Nothing to uninstall" in capsys.readouterr().out

    def test_uninstall_refuses_while_modules_loaded(
        self, tmp_path: Path
    ) -> None:
        import ltvm_pkg.local_install as li
        import ltvm_pkg.cli.make as mk

        manifest = {"schema": li.MANIFEST_SCHEMA, "files": [], "dirs": [],
                    "kernel_version": "5.14.0"}
        with patch.object(li.platform, "system", return_value="Linux"), \
             patch.object(li, "IMAGE_STAMP_PATH", _write_stamp(tmp_path)), \
             patch.object(mk, "read_manifest", return_value=manifest), \
             patch.object(mk, "unload_lustre_modules",
                          return_value=(False, "still loaded: lustre")):
            rc = mk.cmd_make_uninstall(self._args())
        assert rc != 0

    def test_force_lets_uninstall_proceed_anyway(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li
        import ltvm_pkg.cli.make as mk

        manifest = {"schema": li.MANIFEST_SCHEMA,
                    "files": ["usr/sbin/mount.lustre"], "dirs": [],
                    "kernel_version": "5.14.0"}
        with patch.object(li.platform, "system", return_value="Linux"), \
             patch.object(li, "IMAGE_STAMP_PATH", _write_stamp(tmp_path)), \
             patch.object(mk, "read_manifest", return_value=manifest), \
             patch.object(mk, "unload_lustre_modules",
                          return_value=(False, "still loaded: lustre")), \
             patch.object(mk, "remove_installed_files", return_value=1) as rm, \
             patch.object(mk, "prune_empty_dirs", return_value=0), \
             patch.object(mk, "run_depmod_ldconfig"), \
             patch.object(mk, "loaded_lustre_modules", return_value=[]), \
             patch("ltvm_pkg.priv.sudo_run"):
            rc = mk.cmd_make_uninstall(self._args(tmp_path, force=True))

        assert rc == 0
        rm.assert_called_once()

    def test_no_unload_skips_the_unload(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li
        import ltvm_pkg.cli.make as mk

        manifest = {"schema": li.MANIFEST_SCHEMA, "files": [], "dirs": [],
                    "kernel_version": "5.14.0"}
        with patch.object(li.platform, "system", return_value="Linux"), \
             patch.object(li, "IMAGE_STAMP_PATH", _write_stamp(tmp_path)), \
             patch.object(mk, "read_manifest", return_value=manifest), \
             patch.object(mk, "unload_lustre_modules") as unload, \
             patch.object(mk, "remove_installed_files", return_value=0), \
             patch.object(mk, "prune_empty_dirs", return_value=0), \
             patch.object(mk, "run_depmod_ldconfig"), \
             patch.object(mk, "loaded_lustre_modules", return_value=[]), \
             patch("ltvm_pkg.priv.sudo_run"):
            rc = mk.cmd_make_uninstall(self._args(tmp_path, no_unload=True))

        assert rc == 0
        unload.assert_not_called()

    def test_install_missing_build_tree_points_at_fetch(
        self, tmp_path: Path
    ) -> None:
        """A fresh cloud node has no artifacts yet; the error has to
        say how to get them rather than just naming a missing path."""
        import ltvm_pkg.local_install as li
        import ltvm_pkg.cli.make as mk

        with patch.object(li.platform, "system", return_value="Linux"), \
             patch.object(li, "IMAGE_STAMP_PATH", _write_stamp(tmp_path)):
            rc = mk.cmd_make_install(self._args(tmp_path))
        assert rc != 0


class TestImageStampIsBakedIn:
    """image_build writes the stamp make-install reads back."""

    def test_writes_stamp_and_copy_line(self, tmp_path: Path) -> None:
        from ltvm_pkg.image_build import _image_stamp_lines
        from ltvm_pkg.local_install import IMAGE_STAMP_SCHEMA

        tc = MagicMock()
        tc.name = "rocky9"
        tc.arch = "x86_64"
        tc.variant_name = "base"
        tc.os_family = "rhel"

        lines = _image_stamp_lines(
            tc, tmp_path, "5.14-rhel9.7-1.el9", "5.14.0-503.el9.x86_64")

        assert lines == ["COPY ltvm-image.json /etc/ltvm-image.json"]
        stamp = json.loads((tmp_path / "ltvm-image.json").read_text())
        assert stamp["schema"] == IMAGE_STAMP_SCHEMA
        assert stamp["target"] == "rocky9"
        assert stamp["kernel"] == "5.14-rhel9.7-1.el9"
        assert stamp["kernel_version"] == "5.14.0-503.el9.x86_64"

    def test_stamp_round_trips_through_read_image_stamp(
        self, tmp_path: Path
    ) -> None:
        """The writer and the reader must agree -- this is the whole
        contract between image build and make-install."""
        from ltvm_pkg.image_build import _image_stamp_lines
        from ltvm_pkg.local_install import read_image_stamp

        tc = MagicMock()
        tc.name = "rocky9-64k"
        tc.arch = "aarch64"
        tc.variant_name = "base"
        tc.os_family = "rhel"
        _image_stamp_lines(tc, tmp_path, "5.14-rhel9.7-1.el9", "5.14.0-x")

        img = read_image_stamp(tmp_path / "ltvm-image.json")
        assert img is not None
        assert img.target == "rocky9-64k"
        assert img.arch == "aarch64"
        assert img.kernel_version == "5.14.0-x"

    def test_handles_unknown_kver(self, tmp_path: Path) -> None:
        from ltvm_pkg.image_build import _image_stamp_lines

        tc = MagicMock()
        tc.name = "rocky9"
        tc.arch = "x86_64"
        tc.variant_name = "base"
        tc.os_family = "rhel"
        _image_stamp_lines(tc, tmp_path, "k", None)
        stamp = json.loads((tmp_path / "ltvm-image.json").read_text())
        assert stamp["kernel_version"] == ""


class TestKernelDirMatchingRunning:
    """Without a stamp, falling back to the target's *default* kernel
    is wrong on any machine not booted on that default -- the modules
    build fine and then won't load.  Prefer the running kernel."""

    def _tc(self, tmp_path: Path) -> MagicMock:
        tc = MagicMock()
        tc.output_dir = tmp_path
        return tc

    def _kernel(self, tmp_path: Path, name: str, release: str) -> None:
        d = tmp_path / "kernels" / name / "build-tree" / "include" / "config"
        d.mkdir(parents=True)
        (d / "kernel.release").write_text(release + "\n")

    def test_matches_on_kernel_release_not_dir_name(
        self, tmp_path: Path
    ) -> None:
        """The running release carries suffixes the artifact name does
        not: an ltvm VM reports 5.14.0-503.40.1.el9_5_lustre from a
        directory named 5.14-rhel9.5-5.14.0-503.40.1.el9_5."""
        import ltvm_pkg.local_install as li

        running = "5.14.0-503.40.1.el9_5_lustre"
        self._kernel(tmp_path, "5.14-rhel9.5-5.14.0-503.40.1.el9_5", running)
        self._kernel(tmp_path, "5.14-rhel9.7-5.14.0-611.49.1.el9_7",
                     "5.14.0-611.49.1.el9_7_lustre")

        with patch.object(li.platform, "release", return_value=running):
            got = li.kernel_dir_matching_running(self._tc(tmp_path))
        assert got == "5.14-rhel9.5-5.14.0-503.40.1.el9_5"

    def test_none_when_nothing_matches(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        self._kernel(tmp_path, "5.14-rhel9.7-x", "5.14.0-611.el9_7")
        with patch.object(li.platform, "release", return_value="6.1.0-other"):
            assert li.kernel_dir_matching_running(self._tc(tmp_path)) is None

    def test_none_on_a_fresh_node_with_no_artifacts(
        self, tmp_path: Path
    ) -> None:
        import ltvm_pkg.local_install as li

        assert li.kernel_dir_matching_running(self._tc(tmp_path)) is None

    def test_incomplete_build_tree_is_skipped(self, tmp_path: Path) -> None:
        import ltvm_pkg.local_install as li

        (tmp_path / "kernels" / "half-built").mkdir(parents=True)
        self._kernel(tmp_path, "good", "5.14.0-x")
        with patch.object(li.platform, "release", return_value="5.14.0-x"):
            assert li.kernel_dir_matching_running(self._tc(tmp_path)) == "good"

    def test_stamp_kernel_wins_over_running_match(
        self, tmp_path: Path
    ) -> None:
        """A stamped image already names its kernel authoritatively."""
        import ltvm_pkg.local_install as li

        with patch.object(li, "IMAGE_STAMP_PATH", _write_stamp(tmp_path)), \
             patch.object(li, "kernel_dir_matching_running") as m:
            img = li.resolve_local_image()
        m.assert_not_called()
        assert img.kernel == "5.14-rhel9.7-1.el9"
