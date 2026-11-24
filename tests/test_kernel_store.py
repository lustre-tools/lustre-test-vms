"""Versioned kernels: a rebuild never changes the kernel a VM boots.

A kernel artifact dir is keyed on kernel.release, so a rebuild whose
inputs moved without moving the release (a config overlay change) lands
in the same dir.  The container's cp wrote the new vmlinuz over the old
one in place, and every VM created earlier booted it with the modules
of the old one in its root image: "disagrees about version of symbol
module_layout", `modprobe lnet`: Exec format error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import kernel_store
from ltvm_pkg.kernel_store import (
    CURRENT,
    check_kernel,
    current_kernel,
    detach_current,
    kernel_identity,
    kernel_problem,
    replacing,
    versioned_files,
)
from ltvm_pkg.vm_state import VMInfo
from tests.test_package import _make_fake_output, needs_host_tools


def _cp(kdir: Path, data: bytes) -> None:
    """What the inner build script's `cp ... /output/vmlinuz` does: open
    the name for writing, through to any existing inode."""
    with open(kdir / CURRENT, "wb") as f:
        f.write(data)


def _build(kdir: Path, data: bytes) -> Path:
    """One kernel build into *kdir*; returns the versioned file."""
    kdir.mkdir(parents=True, exist_ok=True)
    with replacing(kdir):
        _cp(kdir, data)
    cur = current_kernel(kdir)
    assert cur is not None
    return cur


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


def _vm(name: str, kernel: Path, **kw: Any) -> VMInfo:
    vm = VMInfo(
        name=name,
        ip="192.168.100.9",
        kernel=str(kernel),
        created=kw.pop("created", 0),
        **kw,
    )
    vm.save()
    return vm


class TestBuild:
    def test_build_lands_under_a_versioned_name(self, tmp_path: Path) -> None:
        k = _build(tmp_path / "k", b"one")
        assert kernel_store._VERSIONED_RE.match(k.name)
        assert k.read_bytes() == b"one"
        # vmlinuz stays a real file: every reader and publish use it
        cur = tmp_path / "k" / CURRENT
        assert cur.is_file() and not cur.is_symlink()
        assert os.path.samefile(cur, k)

    def test_rebuild_leaves_the_old_file_alone(self, tmp_path: Path) -> None:
        d = tmp_path / "k"
        first = _build(d, b"one")
        ino = first.stat().st_ino
        ident = kernel_identity(first)
        with patch.object(
            kernel_store,
            "referenced_kernels",
            return_value=({str(first)}, set()),
        ):
            second = _build(d, b"two, rebuilt")
        assert second != first
        assert first.read_bytes() == b"one"
        assert first.stat().st_ino == ino
        assert kernel_identity(first) == ident
        assert current_kernel(d) == second
        assert (d / CURRENT).read_bytes() == b"two, rebuilt"

    def test_rebuild_prunes_what_no_vm_uses(self, tmp_path: Path) -> None:
        d = tmp_path / "k"
        first = _build(d, b"one")
        with patch.object(
            kernel_store, "referenced_kernels", return_value=(set(), set())
        ):
            second = _build(d, b"two")
        assert not first.exists()
        assert versioned_files(d) == [second]

    def test_legacy_vmlinuz_is_kept_by_identity(self, tmp_path: Path) -> None:
        d = tmp_path / "k"
        d.mkdir()
        (d / CURRENT).write_bytes(b"legacy")
        ident = kernel_identity(d / CURRENT)
        # A legacy kernel has no versioned name, so it is its own current
        assert current_kernel(d) == d / CURRENT
        with patch.object(
            kernel_store, "referenced_kernels", return_value=(set(), {ident})
        ):
            new = _build(d, b"rebuilt")
        (old,) = [f for f in versioned_files(d) if f != new]
        assert old.read_bytes() == b"legacy"
        assert kernel_identity(old) == ident

    def test_failed_build_restores_the_previous_kernel(
        self, tmp_path: Path
    ) -> None:
        d = tmp_path / "k"
        first = _build(d, b"one")
        with pytest.raises(RuntimeError), replacing(d):
            raise RuntimeError("build died")
        assert current_kernel(d) == first
        assert (d / CURRENT).read_bytes() == b"one"

    def test_a_symlinked_vmlinuz_is_the_users(self, tmp_path: Path) -> None:
        d = tmp_path / "k"
        d.mkdir()
        mine = tmp_path / "my-bzImage"
        mine.write_bytes(b"mine")
        (d / CURRENT).symlink_to(mine)
        assert detach_current(d) is None
        assert current_kernel(d) == d / CURRENT
        assert versioned_files(d) == []

    def test_no_hard_links_falls_back_to_a_copy(self, tmp_path: Path) -> None:
        d = tmp_path / "k"
        with patch.object(
            kernel_store.os, "link", side_effect=OSError(1, "no")
        ):
            first = _build(d, b"one")
            assert not os.path.samefile(first, d / CURRENT)
            assert current_kernel(d) == first
            with patch.object(
                kernel_store,
                "referenced_kernels",
                return_value=({str(first)}, set()),
            ):
                _build(d, b"two")
        assert first.read_bytes() == b"one"

    def test_vmlinux_only_dir(self, tmp_path: Path) -> None:
        (tmp_path / "vmlinux").write_bytes(b"elf")
        assert current_kernel(tmp_path) == tmp_path / "vmlinux"
        assert current_kernel(tmp_path / "nothing") is None


class TestKernelBuild:
    """The real build path: _build_kernel_upstream with podman stubbed."""

    def test_rebuild_does_not_touch_the_kernel_a_vm_boots(
        self, tmp_path: Path, vmdir: Path
    ) -> None:
        from ltvm_pkg import kernel_build

        out = tmp_path / "out"
        tc = MagicMock(
            arch="x86_64",
            output_dir=out,
            kernel_upstream={},
            default_kernel="latest",
        )
        tc._short_kernel_name.side_effect = lambda k: k
        tc.is_stale.return_value = True
        rel = MagicMock(version="7.0.1", source="s", moniker="stable")
        payloads = iter([b"first kernel", b"second kernel, rebuilt"])

        def fake_podman(cmd: list[str], kernel_out: Path) -> None:
            _cp(kernel_out, next(payloads))

        with (
            patch("ltvm_pkg.upstream_kernel.resolve", return_value=rel),
            patch("ltvm_pkg.upstream_kernel.fetch_releases"),
            patch(
                "ltvm_pkg.upstream_kernel.download_tarball",
                return_value=tmp_path / "linux.tar.xz",
            ),
            patch.object(kernel_build, "_ensure_kernel_builder"),
            patch.object(kernel_build, "_stage_config_inputs"),
            patch.object(kernel_build, "_ccache_volume", return_value="cc"),
            patch.object(kernel_build, "_finalize_kernel_build"),
            patch.object(kernel_build.shutil, "copy2"),
            patch.object(kernel_build.os, "chmod"),
            patch.object(
                kernel_build, "_run_kernel_podman", side_effect=fake_podman
            ),
        ):
            kernel_build._build_kernel_upstream(tc, kernel="latest")
            kdir = out / "kernels" / "latest-7.0.1"
            first = current_kernel(kdir)
            assert first is not None
            _vm("co1-a", first, kernel_id=kernel_identity(first))
            kernel_build._build_kernel_upstream(tc, kernel="latest")
        assert first.read_bytes() == b"first kernel"
        assert (kdir / CURRENT).read_bytes() == b"second kernel, rebuilt"
        assert current_kernel(kdir) != first
        assert check_kernel(VMInfo.load("co1-a")) is None


class TestCreateRecordsIt:
    def test_resolve_os_artifacts_returns_the_versioned_file(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg import target_config as tc_mod
        from ltvm_pkg import vm_state
        from tests.test_image_store import TestCreateUsesTheResolvedFile

        TestCreateUsesTheResolvedFile._setup(None, tmp_path)  # type: ignore[arg-type]
        kdir = tmp_path / "artifacts/rocky9/x86_64/kernels/5.14-rhel9.7"
        (kdir / CURRENT).unlink()
        k = _build(kdir, b"kernel")
        idir = tmp_path / "artifacts/rocky9/x86_64/images/5.14-rhel9.7"
        idir.mkdir(parents=True)
        (idir / "base.ext4").write_bytes(b"img")
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
        assert arts.kernel == k

    def test_identity_recorded(self, tmp_path: Path, vmdir: Path) -> None:
        from ltvm_pkg import vm_commands

        k = _build(tmp_path / "k", b"kernel")
        img = tmp_path / "base.ext4"
        img.write_bytes(b"img")
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

        @contextmanager
        def fake_alloc(
            name: str, count: int = 1, explicit_ip: Any = None
        ) -> Iterator[list[str]]:
            yield ["192.168.100.5"]

        with (
            patch.object(vm_commands, "alloc_ip", fake_alloc),
            patch.object(vm_commands, "_checked"),
            patch.object(vm_commands, "_user_can_write", return_value=True),
        ):
            vm_commands._allocate_and_persist_vm(
                args,
                vmdir.parent / "sockets" / "co1-x.info",
                "tap-x",
                "52:54:00:00:00:01",
                MagicMock(arch="x86_64"),
                str(img),
                str(k),
                "5.14",
                "rocky9",
                "",
                "base",
                [],
                1 << 20,
                1 << 30,
            )
        vm = VMInfo.load("co1-x")
        assert vm.kernel == str(k)
        assert vm.kernel_id == kernel_identity(k)


class TestCheckKernel:
    def test_matching_kernel_boots(self, tmp_path: Path, vmdir: Path) -> None:
        k = _build(tmp_path / "k", b"one")
        vm = _vm("co1-a", k, kernel_id=kernel_identity(k))
        assert check_kernel(vm) is None
        assert kernel_problem(vm) is None

    def test_changed_kernel_is_refused(
        self, tmp_path: Path, vmdir: Path
    ) -> None:
        d = tmp_path / "k"
        d.mkdir()
        (d / CURRENT).write_bytes(b"old")
        vm = _vm("co1-b", d / CURRENT, kernel_id=kernel_identity(d / CURRENT))
        # What the old build did: write the new kernel in place.
        _cp(d, b"newer kernel")
        msg = check_kernel(vm)
        assert msg is not None
        assert "has changed since the VM was created" in msg
        assert "ltvm destroy co1-b" in msg
        assert "ltvm vm set co1-b --current-kernel" in msg
        assert kernel_problem(vm) is not None

    def test_missing_kernel_is_refused(
        self, tmp_path: Path, vmdir: Path
    ) -> None:
        vm = _vm("co1-c", tmp_path / "vmlinuz-gone", kernel_id="1:2")
        msg = check_kernel(vm)
        assert msg is not None and "is gone" in msg
        assert kernel_problem(vm) == f"kernel file gone: {vm.kernel}"

    def test_a_vm_on_the_pointer_follows_its_identity(
        self, tmp_path: Path, vmdir: Path
    ) -> None:
        """A VM that adopted the record while naming `vmlinuz` is moved
        to the versioned file holding the kernel it recorded."""
        d = tmp_path / "k"
        d.mkdir()
        (d / CURRENT).write_bytes(b"legacy")
        ident = kernel_identity(d / CURRENT)
        vm = _vm("co1-d", d / CURRENT, kernel_id=ident)
        with patch.object(
            kernel_store, "referenced_kernels", return_value=(set(), {ident})
        ):
            _build(d, b"rebuilt")
        assert kernel_problem(vm) is None
        assert check_kernel(vm) is None
        saved = VMInfo.load("co1-d")
        assert saved.kernel != str(d / CURRENT)
        assert Path(saved.kernel).read_bytes() == b"legacy"
        assert vm.kernel == saved.kernel

    def test_legacy_vm_warned_when_kernel_is_newer(
        self, tmp_path: Path, vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        k = _build(tmp_path / "k", b"one")
        vm = _vm("co1-e", k, created=int(k.stat().st_mtime) - 100)
        assert check_kernel(vm) is None
        assert "changed after the VM was created" in capsys.readouterr().err
        assert VMInfo.load("co1-e").kernel_id == ""
        assert kernel_problem(vm) is not None

    def test_legacy_vm_adopts_the_record_and_the_versioned_name(
        self, tmp_path: Path, vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        d = tmp_path / "k"
        k = _build(d, b"one")
        vm = _vm("co1-f", d / CURRENT, created=int(k.stat().st_ctime) + 100)
        assert check_kernel(vm) is None
        assert capsys.readouterr().err == ""
        saved = VMInfo.load("co1-f")
        assert saved.kernel_id == kernel_identity(k)
        assert saved.kernel == str(k)

    def test_launch_qemu_refuses(self, tmp_path: Path, vmdir: Path) -> None:
        from ltvm_pkg import qemu_run

        k = _build(tmp_path / "k", b"one")
        vm = _vm("co1-g", k, kernel_id="0:0")
        vm.overlay_path.write_bytes(b"")
        with (
            patch.object(qemu_run, "is_running", return_value=False),
            patch("ltvm_pkg.image_store.check_backing", return_value=None),
            patch.object(qemu_run, "_start_qemu") as start,
            pytest.raises(SystemExit),
        ):
            qemu_run.launch_qemu(vm)
        start.assert_not_called()


class TestDoctor:
    def test_reports_each_vm_whose_kernel_moved(
        self, tmp_path: Path, vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from ltvm_pkg import vm_commands

        k = _build(tmp_path / "k", b"one")
        _vm("co1-ok", k, kernel_id=kernel_identity(k))
        _vm("co1-bad", k, kernel_id="0:0")
        _vm("co1-gone", tmp_path / "k" / "vmlinuz-gone", kernel_id="1:1")
        assert vm_commands._check_vm_kernels(fix=False) == (2, 0)
        out = capsys.readouterr().out
        assert "co1-bad: kernel changed since create" in out
        assert "co1-gone: kernel file gone" in out
        assert "co1-ok" not in out
        assert vm_commands._check_vm_kernels(fix=True) == (2, 2)


class TestVmSetCurrentKernel:
    def test_moves_the_vm_to_the_current_kernel(
        self, tmp_path: Path, vmdir: Path
    ) -> None:
        from ltvm_pkg import vm_commands

        d = tmp_path / "k"
        old = _build(d, b"one")
        _vm("co1-s", old, kernel_id=kernel_identity(old))
        with patch.object(
            kernel_store, "referenced_kernels", return_value=({str(old)}, set())
        ):
            new = _build(d, b"two")
        args = argparse.Namespace(
            name="co1-s", vcpus=None, mem=None, current_kernel=True
        )
        with (
            patch.object(vm_commands.vm_claim, "require"),
            patch.object(vm_commands, "is_running", return_value=False),
        ):
            vm_commands.cmd_set(args)
        vm = VMInfo.load("co1-s")
        assert vm.kernel == str(new)
        assert vm.kernel_id == kernel_identity(new)
        assert check_kernel(vm) is None

    def test_parser(self) -> None:
        from tests.test_ltvm_cli import _load_ltvm

        ltvm = _load_ltvm()
        args = ltvm.build_parser().parse_args(
            ["vm", "set", "co1-s", "--current-kernel"]
        )
        assert args.current_kernel is True


class TestClean:
    def _setup(self, tmp_targets: Path) -> Path:
        from tests.test_clean_command import _make_kernel_dir

        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        d = _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-611.49.1.el9_7")
        (d / CURRENT).unlink()
        return d

    def test_superseded_unreferenced_files_go(
        self, tmp_targets: Path, vmdir: Path
    ) -> None:
        from tests.test_clean_command import _run_prune

        d = self._setup(tmp_targets)
        with patch.object(kernel_store, "in_use", return_value=True):
            unused = _build(d, b"unused")
            by_path = _build(d, b"by path")
            by_id = _build(d, b"by id")
            cur = _build(d, b"current")
        assert len(versioned_files(d)) == 4
        _vm("co1-p", by_path)
        _vm("co1-i", d / CURRENT, kernel_id=kernel_identity(by_id))

        assert _run_prune(tmp_targets, target="rocky9") == 0
        assert unused.exists()  # dry run
        assert _run_prune(tmp_targets, target="rocky9", apply=True) == 0
        assert not unused.exists()
        assert by_path.exists()
        assert by_id.exists()
        assert cur.exists()
        assert current_kernel(d) == cur

    def test_an_unreadable_vm_still_keeps_its_kernel(
        self, tmp_path: Path, vmdir: Path
    ) -> None:
        d = tmp_path / "k"
        with patch.object(kernel_store, "in_use", return_value=True):
            old = _build(d, b"old")
            _build(d, b"new")
        info = vmdir.parent / "sockets" / "co1-u.info"
        info.write_text(f"NAME=co1-u\nKERNEL={old}\nNICS=bogus:x\n")
        with pytest.raises(ValueError):
            VMInfo.load("co1-u")
        assert kernel_store.superseded(d) == []

    def test_legacy_vmlinuz_is_never_offered(
        self, tmp_targets: Path, vmdir: Path
    ) -> None:
        from tests.test_clean_command import _run_prune

        d = self._setup(tmp_targets)
        (d / CURRENT).write_bytes(b"legacy")
        _run_prune(tmp_targets, target="rocky9", apply=True)
        assert (d / CURRENT).read_bytes() == b"legacy"


class TestPublish:
    def test_kernel_asset_excludes_versioned_files(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg import release_package

        out = _make_fake_output(tmp_path)
        cmds: list[list[str]] = []

        class Done(Exception):
            pass

        def fake_run(cmd: list[str], **kw: Any) -> Any:
            cmds.append(cmd)
            if any("kernel-" in a for a in cmd):
                raise Done
            return MagicMock(returncode=0)

        with (
            patch.object(release_package, "export_build_container"),
            patch.object(release_package, "_check_zstd"),
            patch.object(release_package.subprocess, "run", fake_run),
            pytest.raises(Done),
        ):
            release_package.package_target(
                "rocky9",
                out,
                kernel="5.14-rhel9.7",
                dest_dir=tmp_path / "rel",
                include_lustre=False,
            )
        (kern,) = [c for c in cmds if any("kernel-" in a for a in c)]
        assert "rocky9/x86_64/kernels/5.14-rhel9.7/vmlinuz-*" in kern


@needs_host_tools
class TestPublishFetchRoundTrip:
    def test_real_tar(self, tmp_path: Path) -> None:
        from ltvm_pkg import release_package as rp

        out = _make_fake_output(tmp_path)
        kdir = out / "kernels" / "5.14-rhel9.7"
        (kdir / CURRENT).unlink()
        with patch.object(kernel_store, "in_use", return_value=True):
            _build(kdir, b"older kernel")
            _build(kdir, b"current kernel")
        with patch.object(rp, "export_build_container"):
            assets = rp.package_target(
                "rocky9",
                out,
                kernel="5.14-rhel9.7",
                dest_dir=tmp_path / "rel",
                include_lustre=False,
            )
        kasset = assets["kernel"]

        # An existing fetch of the same kernel, which a VM boots.
        fetched = tmp_path / "fetched"
        fkdir = fetched / "rocky9" / "x86_64" / "kernels" / "5.14-rhel9.7"
        booted = _build(fkdir, b"booted kernel")
        (fetched / "rocky9/x86_64/container").mkdir(parents=True)
        (fetched / "rocky9/x86_64/container/image.tar").write_bytes(b"x")

        manifest = {
            "schema": rp._schema_id(),
            "target": "rocky9",
            "kernel": "5.14-rhel9.7",
            "variant": "base",
            "assets": [
                {
                    "kind": "kernel",
                    "name": kasset.name,
                    "size": kasset.stat().st_size,
                    "sha256": hashlib.sha256(kasset.read_bytes()).hexdigest(),
                }
            ],
        }

        def fake_download(url: str, dest: Path, **kw: Any) -> None:
            if url.endswith("m.json"):
                dest.write_text(json.dumps(manifest))
            else:
                shutil.copy(kasset, dest)

        with (
            patch.object(rp, "_download", side_effect=fake_download),
            patch.object(rp, "_ensure_podman"),
            patch.object(rp, "load_container_archive", return_value="abc"),
            patch.object(
                kernel_store,
                "referenced_kernels",
                return_value=({str(booted)}, set()),
            ),
        ):
            rp.fetch_target("rocky9", "https://ex/m.json", fetched)

        # The asset carried exactly one kernel, as vmlinuz.
        assert (fkdir / CURRENT).read_bytes() == b"current kernel"
        assert not (fkdir / CURRENT).is_symlink()
        assert booted.read_bytes() == b"booted kernel"
        cur = current_kernel(fkdir)
        assert cur is not None and cur != booted
        assert cur.read_bytes() == b"current kernel"
        assert sorted(f.read_bytes() for f in versioned_files(fkdir)) == [
            b"booted kernel",
            b"current kernel",
        ]
