"""Versioned base images: a rebuild never changes an overlay's backing file.

A VM's root disk is a qcow2 overlay backed by an image file by path.
Rebuilding the image by renaming a new file over that path left every
stopped VM reading its own blocks over a different filesystem -- a
corrupt root fs at the next boot ("No working init found").
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import image_store
from ltvm_pkg.image_store import (
    CURRENT,
    LEGACY,
    check_backing,
    current_image,
    image_files,
    image_identity,
    install_image,
    overlay_backing_file,
    referenced_images,
)
from ltvm_pkg.vm_state import VMInfo
from tests.test_package import _make_fake_output, needs_host_tools


def _qcow2(path: Path, backing: str | None) -> Path:
    """A qcow2 header naming *backing*, as `qemu-img create -b` writes it."""
    hdr = bytearray(512)
    hdr[0:4] = b"QFI\xfb"
    hdr[4:8] = (3).to_bytes(4, "big")
    if backing is not None:
        raw = backing.encode()
        hdr[8:16] = (112).to_bytes(8, "big")
        hdr[16:20] = len(raw).to_bytes(4, "big")
        hdr[112 : 112 + len(raw)] = raw
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(hdr))
    return path


def _new_image(image_dir: Path, data: bytes) -> Path:
    image_dir.mkdir(parents=True, exist_ok=True)
    tmp = image_dir / "ltvm-image-tmp.ext4"
    tmp.write_bytes(data)
    return install_image(tmp, image_dir)


@pytest.fixture
def vmdir(tmp_path: Path) -> Any:
    sockets = tmp_path / "sockets"
    overlays = tmp_path / "overlays"
    sockets.mkdir()
    overlays.mkdir()
    with (
        patch("ltvm_pkg.vm_state.SOCKETS", sockets),
        patch("ltvm_pkg.vm_state.OVERLAYS", overlays),
    ):
        yield overlays


def _vm(name: str, image: Path, **kw: Any) -> VMInfo:
    vm = VMInfo(
        name=name,
        ip="192.168.100.9",
        image=str(image),
        created=kw.pop("created", 0),
        **kw,
    )
    vm.save()
    return vm


class TestInstallImage:
    def test_build_lands_under_a_versioned_name(self, tmp_path: Path) -> None:
        d = tmp_path / "img"
        img = _new_image(d, b"one")
        assert image_store._VERSIONED_RE.match(img.name)
        assert img.read_bytes() == b"one"
        assert not list(d.glob("ltvm-image-*"))

    def test_pointer_is_a_relative_symlink_to_it(self, tmp_path: Path) -> None:
        d = tmp_path / "img"
        img = _new_image(d, b"one")
        assert os.readlink(d / CURRENT) == img.name
        assert current_image(d) == img

    def test_rebuild_leaves_the_old_file_alone(self, tmp_path: Path) -> None:
        d = tmp_path / "img"
        first = _new_image(d, b"one")
        ino = first.stat().st_ino
        ident = image_identity(first)
        second = _new_image(d, b"two")
        assert second != first
        assert first.read_bytes() == b"one"
        assert first.stat().st_ino == ino
        assert image_identity(first) == ident
        assert current_image(d) == second

    def test_legacy_file_is_never_written(self, tmp_path: Path) -> None:
        d = tmp_path / "img"
        d.mkdir()
        legacy = d / LEGACY
        legacy.write_bytes(b"legacy")
        ino = legacy.stat().st_ino
        assert current_image(d) == legacy

        new = _new_image(d, b"new")
        assert legacy.read_bytes() == b"legacy"
        assert legacy.stat().st_ino == ino
        assert not legacy.is_symlink()
        assert current_image(d) == new
        assert image_files(d) == sorted([legacy, new])

    def test_dangling_pointer_is_no_image(self, tmp_path: Path) -> None:
        d = tmp_path / "img"
        d.mkdir()
        (d / LEGACY).write_bytes(b"legacy")
        (d / CURRENT).symlink_to("base-20260101T000000-abcdef.ext4")
        assert current_image(d) is None

    def test_empty_dir_is_no_image(self, tmp_path: Path) -> None:
        assert current_image(tmp_path) is None


class TestExportInstallsVersioned:
    """The real build path: _export_to_ext4 with every tool stubbed."""

    def _export(self, out_dir: Path, payload: bytes) -> Path:
        from ltvm_pkg import image_build

        def fake_run(cmd: list[str], *a: Any, **kw: Any) -> Any:
            if cmd[0] == "fakeroot":
                (tmp,) = out_dir.glob("ltvm-image-*.ext4")
                tmp.write_bytes(payload)
            out = "cid\n" if kw.get("text") else b""
            return subprocess.CompletedProcess(cmd, 0, out, out)

        with (
            patch.object(image_build.subprocess, "run", side_effect=fake_run),
            patch.object(
                image_build, "_is_macos_build_host", return_value=False
            ),
        ):
            return image_build._export_to_ext4("tag", out_dir)

    def test_rebuild_does_not_touch_the_image_an_overlay_uses(
        self, tmp_path: Path
    ) -> None:
        out = tmp_path / "images" / "5.14"
        out.mkdir(parents=True)
        legacy = out / LEGACY
        legacy.write_bytes(b"legacy")
        first = self._export(out, b"first")
        overlay_backing = current_image(out)
        assert overlay_backing == first
        second = self._export(out, b"second")

        assert legacy.read_bytes() == b"legacy"
        assert first.read_bytes() == b"first"
        assert second.read_bytes() == b"second"
        assert current_image(out) == second
        assert oct(second.stat().st_mode & 0o777) == oct(0o644)


class TestOverlayBackingFile:
    def test_absolute(self, tmp_path: Path) -> None:
        ov = _qcow2(tmp_path / "o.qcow2", "/a/b/base.ext4")
        assert overlay_backing_file(ov) == Path("/a/b/base.ext4")

    def test_relative_is_against_the_overlay_dir(self, tmp_path: Path) -> None:
        ov = _qcow2(tmp_path / "o.qcow2", "base.ext4")
        assert overlay_backing_file(ov) == tmp_path / "base.ext4"

    def test_no_backing(self, tmp_path: Path) -> None:
        assert overlay_backing_file(_qcow2(tmp_path / "o.qcow2", None)) is None

    def test_not_qcow2(self, tmp_path: Path) -> None:
        p = tmp_path / "o.qcow2"
        p.write_bytes(b"")
        assert overlay_backing_file(p) is None


class TestCreateUsesTheResolvedFile:
    def _setup(self, tmp_path: Path) -> Path:
        out = tmp_path / "artifacts" / "rocky9" / "x86_64"
        k = out / "kernels" / "5.14-rhel9.7"
        k.mkdir(parents=True)
        (k / "vmlinuz").write_bytes(b"k")
        yml = tmp_path / "targets" / "targets.yaml"
        yml.parent.mkdir(parents=True)
        yml.write_text(
            "defaults: {}\n"
            "targets:\n"
            "  rocky9:\n"
            "    os_name: rocky\n"
            "    os_version: '9'\n"
            "    container_image: rockylinux:9\n"
            "    status: working\n"
            "    kernels:\n"
            "      default: 5.14-rhel9.7\n"
            "    lustre: {mode: server_ldiskfs}\n"
        )
        return out / "images" / "5.14-rhel9.7"

    def test_resolve_os_artifacts_returns_the_versioned_file(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg import target_config as tc_mod
        from ltvm_pkg import vm_state

        image_dir = self._setup(tmp_path)
        image_dir.mkdir(parents=True)
        (image_dir / LEGACY).write_bytes(b"legacy")
        new = _new_image(image_dir, b"new")
        with (
            patch.object(vm_state, "_LTVM_ROOT", tmp_path),
            patch.object(
                vm_state, "TARGETS_YAML", tmp_path / "targets" / "targets.yaml"
            ),
            patch.object(
                tc_mod, "TARGETS_YAML", tmp_path / "targets" / "targets.yaml"
            ),
            patch.object(tc_mod, "TARGETS_DIR", tmp_path / "targets"),
            patch.object(tc_mod, "ARTIFACTS_DIR", tmp_path / "artifacts"),
        ):
            arts = vm_state.resolve_os_artifacts("rocky9")
        assert arts.image == new

    def test_overlay_is_created_against_it_and_identity_recorded(
        self, tmp_path: Path, vmdir: Path
    ) -> None:
        from ltvm_pkg import vm_commands

        image_dir = tmp_path / "images"
        img = _new_image(image_dir, b"new")
        calls: list[list[str]] = []
        args = argparse.Namespace(
            name="co1-x",
            vcpus=1,
            mem=512,
            mdt_disks=0,
            ost_disks=0,
            owner_id=None,
            ip=None,
            kernel_args="",
        )
        arts = MagicMock(arch="x86_64")

        def fake_checked(cmd: list[str]) -> None:
            calls.append(cmd)

        from contextlib import contextmanager

        @contextmanager
        def fake_alloc(
            name: str, count: int = 1, explicit_ip: Any = None, nics: Any = ()
        ) -> Any:
            yield ["192.168.100.5"]

        with (
            patch.object(vm_commands, "alloc_ip", fake_alloc),
            patch.object(vm_commands, "_checked", side_effect=fake_checked),
            patch.object(vm_commands, "_user_can_write", return_value=True),
        ):
            vm = vm_commands._allocate_and_persist_vm(
                args,
                vmdir.parent / "sockets" / "co1-x.info",
                "tap-x",
                "52:54:00:00:00:01",
                arts,
                str(img),
                "/k/vmlinuz",
                "5.14",
                "rocky9",
                "",
                "base",
                [],
                1 << 20,
                1 << 30,
            )
        (create,) = [c for c in calls if c[1] == "create"]
        assert create[create.index("-b") + 1] == str(img)
        assert vm.image_id == image_identity(img)
        assert VMInfo.load("co1-x").image_id == image_identity(img)

    def test_explicit_image_symlink_is_resolved(self, tmp_path: Path) -> None:
        from ltvm_pkg import vm_commands

        d = tmp_path / "img"
        img = _new_image(d, b"new")
        args = argparse.Namespace(
            target="rocky9",
            image=str(d / CURRENT),
            kernel="",
            arch="x86_64",
            variant=None,
            mem=512,
            json=True,
        )
        arts = MagicMock(kernel=tmp_path / "vmlinuz")
        with (
            patch.object(
                vm_commands, "resolve_os_artifacts", return_value=arts
            ),
            patch.object(
                vm_commands,
                "load_meta_safe",
                return_value={"kernel_version": "5.14"},
            ),
        ):
            _, image, *_ = vm_commands._resolve_os_and_kernel(args)
        assert image == os.path.realpath(img)


class TestCheckBacking:
    def test_matching_image_boots(self, tmp_path: Path, vmdir: Path) -> None:
        img = _new_image(tmp_path / "img", b"one")
        vm = _vm("co1-a", img, image_id=image_identity(img))
        _qcow2(vm.overlay_path, str(img))
        assert check_backing(vm) is None

    def test_changed_image_is_refused(
        self, tmp_path: Path, vmdir: Path
    ) -> None:
        d = tmp_path / "img"
        d.mkdir()
        legacy = d / LEGACY
        legacy.write_bytes(b"old")
        vm = _vm("co1-b", legacy, image_id=image_identity(legacy))
        _qcow2(vm.overlay_path, str(legacy))
        # What the old build did: rename a new file over the path.
        tmp = d / "new.tmp"
        tmp.write_bytes(b"newer image")
        os.rename(tmp, legacy)
        msg = check_backing(vm)
        assert msg is not None
        assert "has changed since the VM was created" in msg
        assert "ltvm destroy co1-b" in msg

    def test_missing_image_is_refused(
        self, tmp_path: Path, vmdir: Path
    ) -> None:
        img = tmp_path / "gone.ext4"
        vm = _vm("co1-c", img, image_id="1:2")
        _qcow2(vm.overlay_path, str(img))
        msg = check_backing(vm)
        assert msg is not None and "is gone" in msg

    def test_the_header_wins_over_the_info(
        self, tmp_path: Path, vmdir: Path
    ) -> None:
        good = _new_image(tmp_path / "a", b"one")
        other = _new_image(tmp_path / "b", b"different")
        vm = _vm("co1-d", other, image_id=image_identity(other))
        _qcow2(vm.overlay_path, str(good))
        assert check_backing(vm) is not None

    def test_legacy_vm_warned_when_image_is_newer(
        self, tmp_path: Path, vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        img = _new_image(tmp_path / "img", b"one")
        vm = _vm("co1-e", img, created=int(img.stat().st_mtime) - 100)
        _qcow2(vm.overlay_path, str(img))
        assert check_backing(vm) is None
        assert "changed after the VM was created" in capsys.readouterr().err
        assert VMInfo.load("co1-e").image_id == ""

    def test_legacy_vm_adopts_the_record(
        self, tmp_path: Path, vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        img = _new_image(tmp_path / "img", b"one")
        vm = _vm("co1-f", img, created=int(img.stat().st_ctime) + 100)
        _qcow2(vm.overlay_path, str(img))
        assert check_backing(vm) is None
        assert capsys.readouterr().err == ""
        assert VMInfo.load("co1-f").image_id == image_identity(img)

    def test_launch_qemu_refuses(self, tmp_path: Path, vmdir: Path) -> None:
        from ltvm_pkg import qemu_run

        img = _new_image(tmp_path / "img", b"one")
        vm = _vm("co1-g", img, image_id="0:0")
        _qcow2(vm.overlay_path, str(img))
        with (
            patch.object(qemu_run, "is_running", return_value=False),
            patch.object(qemu_run, "_start_qemu") as start,
            pytest.raises(SystemExit),
        ):
            qemu_run.launch_qemu(vm)
        start.assert_not_called()

    def test_legacy_overlay_keeps_booting_after_a_rebuild(
        self, tmp_path: Path, vmdir: Path
    ) -> None:
        d = tmp_path / "img"
        d.mkdir()
        legacy = d / LEGACY
        legacy.write_bytes(b"legacy")
        vm = _vm("co1-h", legacy, image_id=image_identity(legacy))
        _qcow2(vm.overlay_path, str(legacy))
        _new_image(d, b"rebuilt")
        assert legacy.read_bytes() == b"legacy"
        assert check_backing(vm) is None


class TestReferencedImages:
    def test_overlays_and_info_both_count(
        self, tmp_path: Path, vmdir: Path
    ) -> None:
        a = _new_image(tmp_path / "a", b"a")
        b = _new_image(tmp_path / "b", b"b")
        _qcow2(vmdir / "co1-a.qcow2", str(a))
        _vm("co1-b", b)
        refs, unknown = referenced_images()
        assert {os.path.realpath(a), os.path.realpath(b)} <= refs
        assert unknown == []

    def test_unreadable_overlay_without_info_is_unknown(
        self, tmp_path: Path, vmdir: Path
    ) -> None:
        (vmdir / "co1-z.qcow2").write_bytes(b"garbage")
        _, unknown = referenced_images()
        assert unknown == ["co1-z"]


class TestPruneSuperseded:
    """An image build removes the files it superseded that no VM uses."""

    def test_keeps_current_and_backing_files(
        self, tmp_path: Path, vmdir: Path
    ) -> None:
        d = tmp_path / "img"
        used = _new_image(d, b"used")
        unused = _new_image(d, b"unused-" * 10)
        legacy = d / LEGACY
        legacy.write_bytes(b"legacy")
        current = _new_image(d, b"current")
        _qcow2(vmdir / "co1-a.qcow2", str(used))
        freed = image_store.prune_superseded(d)
        assert freed == len(b"unused-" * 10) + len(b"legacy")
        assert set(image_files(d)) == {used, current}
        assert not unused.exists() and not legacy.exists()
        assert current_image(d) == current

    def test_unknown_backing_file_keeps_everything(
        self, tmp_path: Path, vmdir: Path
    ) -> None:
        d = tmp_path / "img"
        old = _new_image(d, b"old")
        _new_image(d, b"new")
        (vmdir / "co1-z.qcow2").write_bytes(b"garbage")
        assert image_store.prune_superseded(d) == 0
        assert old.exists()

    def test_no_current_image_removes_nothing(self, tmp_path: Path) -> None:
        d = tmp_path / "img"
        d.mkdir()
        assert image_store.prune_superseded(d) == 0


class TestFetchInstallsVersioned:
    def test_fetched_image_does_not_replace_the_legacy_file(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg.release_package import _install_fetched_images

        out = tmp_path / "artifacts"
        image_dir = out / "rocky9" / "x86_64" / "images" / "5.14"
        image_dir.mkdir(parents=True)
        legacy = image_dir / LEGACY
        legacy.write_bytes(b"legacy")
        (image_dir / "meta.json").write_text("{}")

        staging = out / ".ltvm-fetch-image-x"
        sd = staging / "rocky9" / "x86_64" / "images" / "5.14"
        sd.mkdir(parents=True)
        (sd / LEGACY).write_bytes(b"fetched")
        (sd / "meta.json").write_text('{"new": 1}')
        _install_fetched_images(staging, out)

        assert legacy.read_bytes() == b"legacy"
        cur = current_image(image_dir)
        assert cur is not None and cur != legacy
        assert cur.read_bytes() == b"fetched"
        assert json.loads((image_dir / "meta.json").read_text()) == {"new": 1}


class TestPublishCarriesTheCurrentImage:
    def test_member_is_base_ext4_with_current_bytes(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg import release_package

        out = _make_fake_output(tmp_path)
        idir = out / "images" / "5.14-rhel9.7"
        new = _new_image(idir, b"current image")
        seen: dict[str, bytes] = {}

        def fake_tar(
            base: Path, entries: list[str], dest: Path, **kw: Any
        ) -> None:
            for e in entries:
                if e.endswith(".ext4"):
                    assert kw.get("dereference")
                    seen[e] = (base / e).read_bytes()
            dest.write_bytes(b"")

        def fake_run(cmd: list[str], *a: Any, **kw: Any) -> Any:
            for arg in cmd:
                if str(arg).endswith(".tar.zst"):
                    Path(arg).write_bytes(b"")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with (
            patch.object(release_package, "_check_zstd"),
            patch.object(release_package, "_tar_zstd", side_effect=fake_tar),
            patch.object(release_package, "export_build_container"),
            patch.object(
                release_package.subprocess, "run", side_effect=fake_run
            ),
            patch.object(release_package, "_asset_entry", return_value={}),
        ):
            release_package.package_target(
                "rocky9",
                out,
                kernel="5.14-rhel9.7",
                dest_dir=tmp_path / "rel",
                include_lustre=False,
            )
        assert new.name not in "".join(seen)
        ((member, data),) = seen.items()
        assert member.endswith("images/5.14-rhel9.7/base.ext4")
        assert data == b"current image"


@needs_host_tools
class TestPublishFetchRoundTrip:
    def test_real_tar(self, tmp_path: Path) -> None:
        from ltvm_pkg import release_package

        out = _make_fake_output(tmp_path)
        idir = out / "images" / "5.14-rhel9.7"
        _new_image(idir, b"current image")
        with patch.object(release_package, "export_build_container"):
            assets = release_package.package_target(
                "rocky9",
                out,
                kernel="5.14-rhel9.7",
                dest_dir=tmp_path / "rel",
                include_lustre=False,
            )
        fetched = tmp_path / "fetched"
        staging = fetched / ".staging"
        staging.mkdir(parents=True)
        release_package._untar_zstd(assets["image"], staging)
        rel = idir.relative_to(out.parent.parent)
        member = staging / rel / LEGACY
        assert not member.is_symlink()
        assert member.read_bytes() == b"current image"
        release_package._install_fetched_images(staging, fetched)
        cur = current_image(fetched / rel)
        assert cur is not None and cur.read_bytes() == b"current image"


class TestCleanPrunesOnlyUnreferenced:
    def _setup(self, tmp_targets: Path) -> Path:
        from tests.test_clean_command import _make_kernel_dir

        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-611.49.1.el9_7")
        d = arch_dir / "images" / "5.14-rhel9.7-5.14.0-611.49.1.el9_7"
        d.mkdir(parents=True)
        (d / "meta.json").write_text("{}")
        return d

    def test_superseded_unreferenced_files_go(
        self, tmp_targets: Path, vmdir: Path
    ) -> None:
        from tests.test_clean_command import _run_prune

        d = self._setup(tmp_targets)
        legacy = d / LEGACY
        legacy.write_bytes(b"legacy")
        used = _new_image(d, b"used")
        unused = _new_image(d, b"unused")
        cur = _new_image(d, b"current")
        _qcow2(vmdir / "co1-a.qcow2", str(used))

        assert _run_prune(tmp_targets, target="rocky9", apply=True) == 0
        assert not legacy.exists()
        assert not unused.exists()
        assert used.exists()
        assert cur.exists()
        assert current_image(d) == cur

    def test_legacy_referenced_by_info_is_kept(
        self, tmp_targets: Path, vmdir: Path
    ) -> None:
        from tests.test_clean_command import _run_prune

        d = self._setup(tmp_targets)
        legacy = d / LEGACY
        legacy.write_bytes(b"legacy")
        _new_image(d, b"current")
        _vm("co1-old", legacy)
        _run_prune(tmp_targets, target="rocky9", apply=True)
        assert legacy.exists()

    def test_current_legacy_without_pointer_is_kept(
        self, tmp_targets: Path, vmdir: Path
    ) -> None:
        from tests.test_clean_command import _run_prune

        d = self._setup(tmp_targets)
        legacy = d / LEGACY
        legacy.write_bytes(b"legacy")
        _run_prune(tmp_targets, target="rocky9", apply=True)
        assert legacy.exists()

    def test_unknown_overlay_keeps_everything(
        self, tmp_targets: Path, vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from tests.test_clean_command import _run_prune

        d = self._setup(tmp_targets)
        old = _new_image(d, b"old")
        _new_image(d, b"current")
        (vmdir / "co1-z.qcow2").write_bytes(b"unreadable")
        _run_prune(tmp_targets, target="rocky9", apply=True)
        assert old.exists()
        assert "keeping every image" in capsys.readouterr().err

    def test_orphan_image_dir_in_use_is_kept(
        self, tmp_targets: Path, vmdir: Path
    ) -> None:
        from tests.test_clean_command import _make_image_dir, _run_prune

        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        orphan = _make_image_dir(arch_dir, "5.14-rhel9.5-5.14.0-503.40.1.el9_5")
        _qcow2(vmdir / "co1-a.qcow2", str(orphan / LEGACY))
        _run_prune(tmp_targets, target="rocky9", apply=True)
        assert (orphan / LEGACY).exists()
