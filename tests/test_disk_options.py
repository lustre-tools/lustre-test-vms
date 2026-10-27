"""Data disk options: --disk-dir, --disk-cache and --disk-prealloc.

A VM may keep its MDT/OST disks outside the overlays directory, give
them a QEMU cache mode, and have them preallocated.  None of it may
change a VM that does not ask for it: its .info loads as before and its
QEMU command line is the same, byte for byte.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import qemu_run, vm_cluster, vm_commands
from ltvm_pkg.cli import clean as _clean_mod
from ltvm_pkg.vm_state import ClusterNode, VMInfo
from tests.test_qemu_run import (  # noqa: F401
    _force_linux_host,
    _LaunchHarness,
    _run_launch,
)

# The autouse _neutralize_host_clean_state stubs this out; collection
# runs before it, so the real one is kept here.
_REAL_IDLE_VMS = _clean_mod._idle_vms


@pytest.fixture
def vm_dirs(tmp_path: Path) -> Any:
    sockets = tmp_path / "sockets"
    overlays = tmp_path / "overlays"
    disks = tmp_path / "hdd"
    for d in (sockets, overlays, disks):
        d.mkdir()
    with (
        patch("ltvm_pkg.vm_state.VM_DIR", tmp_path),
        patch("ltvm_pkg.vm_state.SOCKETS", sockets),
        patch("ltvm_pkg.vm_state.OVERLAYS", overlays),
        patch("ltvm_pkg.vm_commands.SOCKETS", sockets),
        patch("ltvm_pkg.vm_commands.OVERLAYS", overlays),
        patch("ltvm_pkg.vm_cluster.SOCKETS", sockets),
    ):
        yield sockets, overlays, disks


def _vm(name: str, mdt: int = 0, ost: int = 0, **kw: Any) -> VMInfo:
    vm = VMInfo(name=name, ip="192.0.2.10", mdt_disks=mdt, ost_disks=ost, **kw)
    vm.save()
    for disk in vm.disk_paths():
        disk.write_text("")
    return vm


# ── VMInfo ───────────────────────────────────────────────


class TestVMInfoFields:
    def test_round_trip(self, vm_dirs: Any) -> None:
        _, _, disks = vm_dirs
        _vm(
            "d-a",
            mdt=1,
            disk_dir=str(disks),
            disk_cache="none",
            disk_prealloc="full",
        )
        vm = VMInfo.load("d-a")
        assert vm.disk_dir == str(disks)
        assert vm.disk_cache == "none"
        assert vm.disk_prealloc == "full"
        text = vm.info_path.read_text()
        assert f"DISK_DIR={disks}\n" in text
        assert "DISK_CACHE=none\n" in text
        assert "DISK_PREALLOC=full\n" in text

    def test_old_info_file_loads_as_before(self, vm_dirs: Any) -> None:
        sockets, overlays, _ = vm_dirs
        (sockets / "d-old.info").write_text(
            "NAME=d-old\nIP=192.0.2.9\nMDT_DISKS=1\nOST_DISKS=1\n"
        )
        vm = VMInfo.load("d-old")
        assert (vm.disk_dir, vm.disk_cache, vm.disk_prealloc) == ("", "", "")
        assert vm.disk_paths() == [
            overlays / "d-old-disk1.img",
            overlays / "d-old-disk2.img",
        ]

    def test_disks_follow_the_disk_dir(self, vm_dirs: Any) -> None:
        _, overlays, disks = vm_dirs
        vm = VMInfo(name="d-a", ip="", ost_disks=2, disk_dir=str(disks))
        assert vm.disk_paths() == [
            disks / "d-a-disk1.img",
            disks / "d-a-disk2.img",
        ]
        # The root overlay stays where it is.
        assert vm.overlay_path == overlays / "d-a.qcow2"


# ── QEMU disk arguments ──────────────────────────────────


def _drives(vm: VMInfo) -> list[str]:
    return qemu_run.data_disk_args(vm)[3::4]


class TestDataDiskArgs:
    def test_plain_vm_has_no_cache(self, vm_dirs: Any) -> None:
        vm = _vm("d-a", ost=1)
        assert _drives(vm) == [
            f"id=disk1,file={vm.disk_path(1)},format=raw,if=none,"
            "discard=unmap,detect-zeroes=unmap"
        ]

    @pytest.mark.parametrize(
        "mode", ["none", "writeback", "writethrough", "directsync", "unsafe"]
    )
    def test_cache_mode_is_passed(self, vm_dirs: Any, mode: str) -> None:
        vm = _vm("d-a", mdt=1, ost=1, disk_cache=mode)
        for drive in _drives(vm):
            assert drive.endswith(
                f",discard=unmap,detect-zeroes=unmap,cache={mode}"
            )

    def test_disk_dir_is_the_file(self, vm_dirs: Any) -> None:
        _, _, disks = vm_dirs
        vm = _vm("d-a", ost=2, disk_dir=str(disks))
        assert [d.split(",")[1] for d in _drives(vm)] == [
            f"file={disks}/d-a-disk1.img",
            f"file={disks}/d-a-disk2.img",
        ]

    def test_preallocated_disks_ignore_discards(self, vm_dirs: Any) -> None:
        vm = _vm("d-a", ost=1, disk_prealloc="falloc")
        (drive,) = _drives(vm)
        assert "discard=ignore,detect-zeroes=off" in drive
        assert "unmap" not in drive

    def test_shared_disk_says_cache_once(self, vm_dirs: Any) -> None:
        vm = _vm("d-a", mdt=1, share_disks=True, disk_cache="none")
        (drive,) = _drives(vm)
        assert drive.count("cache=") == 1
        assert drive.endswith(",cache=none")

    def test_partner_follows_the_primary_disks(self, vm_dirs: Any) -> None:
        _, _, disks = vm_dirs
        a = _vm(
            "d-a",
            mdt=1,
            share_disks=True,
            disk_dir=str(disks),
            disk_prealloc="full",
        )
        b = _vm("d-b", disks_from="d-a")
        assert _drives(b) == _drives(a)
        assert f"file={disks}/d-a-disk1.img" in _drives(b)[0]
        assert "discard=ignore" in _drives(b)[0]

    def test_missing_disk_dir_is_named(
        self, vm_dirs: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vm = _vm("d-a", ost=1, disk_dir=str(tmp_path / "hdd"))
        vm.disk_dir = str(tmp_path / "unmounted")
        with pytest.raises(SystemExit):
            qemu_run.data_disk_args(vm)
        assert "not mounted" in capsys.readouterr().err

    def test_launch_command_line(self, vm_dirs: Any) -> None:
        _, _, disks = vm_dirs
        kernel = disks.parent / "vmlinuz"
        kernel.write_text("")
        vm = _vm(
            "d-a",
            ost=1,
            disk_dir=str(disks),
            disk_cache="none",
            kernel=str(kernel),
            tap="tap-d-a",
            mac="AA:FC:00:01:02:03",
        )
        vm.overlay_path.write_text("")
        h = _LaunchHarness()
        _run_launch(vm, h)
        argv = h.qemu_argv or []
        assert (
            f"id=disk1,file={disks}/d-a-disk1.img,format=raw,if=none,"
            "discard=unmap,detect-zeroes=unmap,cache=none"
        ) in argv


# ── create validation ────────────────────────────────────


def _args(**kw: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "name": "d-a",
        "disk_dir": None,
        "disk_cache": None,
        "disk_prealloc": None,
        "share_disks": False,
        "disks_from": None,
    }
    values.update(kw)
    return argparse.Namespace(**values)


class TestValidateDiskOptions:
    def test_nothing_given_is_empty(self) -> None:
        args = _args()
        vm_commands._validate_disk_options(args)
        assert (args.disk_dir, args.disk_cache, args.disk_prealloc) == (
            "",
            "",
            "",
        )

    def test_dir_is_normalized(self, tmp_path: Path) -> None:
        args = _args(disk_dir=f"{tmp_path}/", disk_cache="none")
        vm_commands._validate_disk_options(args)
        assert args.disk_dir == str(tmp_path)
        assert args.disk_cache == "none"

    @pytest.mark.parametrize(
        "bad", ["relative/dir", "/no/such/ltvm-dir", "/tmp/a,b", "/tmp/a\nb"]
    )
    def test_bad_dirs_are_refused(self, bad: str) -> None:
        with pytest.raises(SystemExit):
            vm_commands._validate_disk_options(_args(disk_dir=bad))

    def test_a_file_is_not_a_dir(self, tmp_path: Path) -> None:
        f = tmp_path / "f"
        f.write_text("")
        with pytest.raises(SystemExit):
            vm_commands._validate_disk_options(_args(disk_dir=str(f)))

    @pytest.mark.parametrize(
        "kw", [{"disk_cache": "bogus"}, {"disk_prealloc": "sparse"}]
    )
    def test_bad_modes_are_refused(self, kw: dict[str, str]) -> None:
        with pytest.raises(SystemExit):
            vm_commands._validate_disk_options(_args(**kw))

    @pytest.mark.parametrize(
        "kw", [{"disk_dir": "/"}, {"disk_prealloc": "falloc"}]
    )
    def test_partner_has_no_disks_to_place(self, kw: dict[str, str]) -> None:
        with pytest.raises(SystemExit):
            vm_commands._validate_disk_options(_args(disks_from="d-x", **kw))

    @pytest.mark.parametrize(
        "kw", [{"share_disks": True}, {"disks_from": "d-x"}]
    )
    def test_shared_disks_take_only_none(self, kw: dict[str, Any]) -> None:
        with pytest.raises(SystemExit):
            vm_commands._validate_disk_options(
                _args(disk_cache="writeback", **kw)
            )
        vm_commands._validate_disk_options(_args(disk_cache="none", **kw))

    def test_create_persists_the_options(self, vm_dirs: Any) -> None:
        _, _, disks = vm_dirs
        args = argparse.Namespace(
            name="d-new",
            ip=None,
            vcpus=2,
            mem=2048,
            mdt_disks=1,
            ost_disks=1,
            owner_id="test",
            disk_dir=str(disks),
            disk_cache="directsync",
            disk_prealloc="falloc",
        )
        with (
            patch.object(vm_commands, "_create_disks", return_value=False),
            patch.object(vm_commands, "alloc_ip") as alloc,
        ):
            alloc.return_value.__enter__.return_value = ["192.0.2.20"]
            vm_commands._allocate_and_persist_vm(
                args,
                VMInfo(name="d-new", ip="").info_path,
                "tap-d-new",
                "AA:FC:00:00:00:01",
                MagicMock(arch="x86_64"),
                "/nonexistent/base.ext4",
                "/nonexistent/vmlinuz",
                "6.0",
                "rocky9",
                "base.ext4",
                "base",
                [],
                500 << 20,
                8 << 30,
            )
        vm = VMInfo.load("d-new")
        assert vm.disk_dir == str(disks)
        assert vm.disk_cache == "directsync"
        assert vm.disk_prealloc == "falloc"


class TestIgnoredFlagWarning:
    def test_same_dir_spelled_differently_is_not_ignored(
        self, vm_dirs: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, _, disks = vm_dirs
        vm = _vm("d-a", ost=1, disk_dir=str(disks))
        argv = ["ltvm", "create", "d-a", "--disk-dir", f"{disks}/"]
        vm_commands._warn_ignored_resource_flags(
            vm, _args(disk_dir=f"{disks}/"), argv
        )
        assert capsys.readouterr().err == ""

    def test_a_new_cache_mode_is_reported(
        self, vm_dirs: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vm = _vm("d-a", ost=1)
        argv = ["ltvm", "create", "d-a", "--disk-cache", "none"]
        vm_commands._warn_ignored_resource_flags(
            vm, _args(disk_cache="none"), argv
        )
        assert "--disk-cache: requested none" in capsys.readouterr().err


# ── creating the disks ───────────────────────────────────


class _Recorder:
    """Stands in for _checked: records each command and makes its file."""

    def __init__(self, fail_on: str = "") -> None:
        self.cmds: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, cmd: list[str]) -> None:
        self.cmds.append(cmd)
        if self.fail_on and any(self.fail_on in c for c in cmd):
            raise RuntimeError(f"qemu-img failed (rc=1): {self.fail_message}")
        target = None
        if cmd[0] == "truncate":
            target = cmd[-1]
        elif "create" in cmd:
            target = cmd[-2] if "-o" in cmd else cmd[-1]
        if target:
            Path(target).write_text("")

    fail_message = "Could not preallocate data: Operation not supported"


def _create(vm: VMInfo, rec: _Recorder) -> bool:
    with (
        patch.object(vm_commands, "_checked", side_effect=rec),
        patch.object(vm_commands, "_user_can_write", return_value=True),
    ):
        return vm_commands._create_disks(vm, "/nonexistent/base.ext4")


class TestCreateDisks:
    def test_plain_disks_are_sparse_in_overlays(self, vm_dirs: Any) -> None:
        _, overlays, _ = vm_dirs
        vm = VMInfo(name="d-a", ip="", mdt_disks=1, ost_disks=1)
        rec = _Recorder()
        assert _create(vm, rec) is False
        truncs = [c for c in rec.cmds if c[0] == "truncate"]
        assert [c[-1] for c in truncs] == [
            str(overlays / "d-a-disk1.img"),
            str(overlays / "d-a-disk2.img"),
        ]

    def test_disks_go_to_the_disk_dir(self, vm_dirs: Any) -> None:
        _, overlays, disks = vm_dirs
        vm = VMInfo(name="d-a", ip="", ost_disks=2, disk_dir=str(disks))
        _create(vm, _Recorder())
        assert sorted(p.name for p in disks.iterdir()) == [
            "d-a-disk1.img",
            "d-a-disk2.img",
        ]
        assert not list(overlays.glob("*-disk*.img"))

    def test_unwritable_disk_dir_goes_through_sudo(self, vm_dirs: Any) -> None:
        _, overlays, disks = vm_dirs
        vm = VMInfo(name="d-a", ip="", ost_disks=1, disk_dir=str(disks))
        rec, sudo = _Recorder(), _Recorder()
        with (
            patch.object(vm_commands, "_checked", side_effect=rec),
            patch.object(vm_commands, "_sudo_checked", side_effect=sudo),
            patch.object(
                vm_commands,
                "_user_can_write",
                side_effect=lambda d: d == overlays,
            ),
        ):
            assert vm_commands._create_disks(vm, "/x") is True
        assert [c[0] for c in sudo.cmds] == ["truncate"]
        assert all(c[0] != "truncate" for c in rec.cmds)

    def test_prealloc_uses_qemu_img(self, vm_dirs: Any) -> None:
        vm = VMInfo(name="d-a", ip="", ost_disks=1, disk_prealloc="falloc")
        rec = _Recorder()
        _create(vm, rec)
        (cmd,) = [c for c in rec.cmds if "-o" in c]
        assert cmd[cmd.index("-f") + 1] == "raw"
        assert "preallocation=falloc" in cmd
        assert cmd[-2:] == [str(vm.disk_path(1)), str(vm.disk_size)]
        assert not [c for c in rec.cmds if c[0] == "truncate"]

    def test_falloc_falls_back_to_full(
        self, vm_dirs: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vm = VMInfo(name="d-a", ip="", ost_disks=2, disk_prealloc="falloc")
        rec = _Recorder(fail_on="preallocation=falloc")
        _create(vm, rec)
        modes = [c[c.index("-o") + 1] for c in rec.cmds if "-o" in c]
        assert modes == [
            "preallocation=falloc",
            "preallocation=full",
            "preallocation=full",
        ]
        assert vm.disk_prealloc == "full"
        assert "writing the disks out in full" in capsys.readouterr().err

    def test_no_space_is_not_retried(self, vm_dirs: Any) -> None:
        vm = VMInfo(name="d-a", ip="", ost_disks=1, disk_prealloc="falloc")
        rec = _Recorder(fail_on="preallocation=falloc")
        rec.fail_message = "No space left on device"
        with pytest.raises(RuntimeError):
            _create(vm, rec)
        assert not vm.overlay_path.exists()
        assert not vm.disk_path(1).exists()

    def test_failure_cleans_the_disk_dir(self, vm_dirs: Any) -> None:
        _, _, disks = vm_dirs
        vm = VMInfo(
            name="d-a",
            ip="",
            ost_disks=2,
            disk_dir=str(disks),
            disk_prealloc="full",
        )
        rec = _Recorder(fail_on=str(disks / "d-a-disk2.img"))
        with pytest.raises(RuntimeError):
            _create(vm, rec)
        assert not list(disks.iterdir())


# ── destroy, doctor, clean ───────────────────────────────


class TestDestroy:
    def test_removes_disks_in_the_disk_dir_only(self, vm_dirs: Any) -> None:
        _, _, disks = vm_dirs
        vm = _vm("d-a", mdt=1, ost=1, disk_dir=str(disks))
        vm.overlay_path.write_text("")
        keep = [
            disks / "notes.txt",
            disks / "d-ab-disk1.img",
            disks / "d-a-disk1.img.bak",
        ]
        for f in keep:
            f.write_text("")
        vm_commands._destroy_vm_artifacts("d-a")
        assert not vm.info_path.exists()
        assert not vm.overlay_path.exists()
        assert sorted(disks.iterdir()) == sorted(keep)


class TestDoctor:
    def test_clean_host_has_no_issues(self, vm_dirs: Any) -> None:
        _, _, disks = vm_dirs
        _vm("d-a", ost=1)
        _vm("d-b", ost=1, disk_dir=str(disks))
        assert vm_commands._check_disk_dirs(fix=True) == (0, 0)

    def test_missing_disk_is_reported(
        self, vm_dirs: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, _, disks = vm_dirs
        vm = _vm("d-b", ost=2, disk_dir=str(disks))
        vm.disk_path(2).unlink()
        assert vm_commands._check_disk_dirs(fix=False) == (1, 0)
        assert "missing data disk: d-b disk2" in capsys.readouterr().out

    def test_missing_dir_is_reported_once(
        self, vm_dirs: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        VMInfo(
            name="d-b", ip="", ost_disks=3, disk_dir=str(tmp_path / "gone")
        ).save()
        assert vm_commands._check_disk_dirs(fix=False) == (1, 0)
        assert "missing disk dir: d-b" in capsys.readouterr().out

    def test_orphan_in_a_disk_dir_is_reported_not_removed(
        self, vm_dirs: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, _, disks = vm_dirs
        _vm("d-b", ost=1, disk_dir=str(disks))
        orphan = disks / "d-gone-disk1.img"
        orphan.write_text("")
        (disks / "unrelated.img").write_text("")
        assert vm_commands._check_disk_dirs(fix=True) == (1, 1)
        assert f"orphan disk: {orphan}" in capsys.readouterr().out
        assert orphan.exists()


def test_clean_counts_disks_in_the_disk_dir(vm_dirs: Any) -> None:
    _, _, disks = vm_dirs
    vm = _vm("d-a", ost=1, disk_dir=str(disks))
    vm.overlay_path.write_text("")
    vm.disk_path(1).write_bytes(b"x" * 8192)
    with patch("ltvm_pkg.qemu_run.is_running", return_value=False):
        (idle,) = _REAL_IDLE_VMS(0)
    assert idle["bytes"] >= 8192


# ── vm set --disk-cache ──────────────────────────────────


class TestVmSet:
    def _set(self, cache: str, **vm_kw: Any) -> VMInfo:
        vm = _vm("d-a", ost=1, **vm_kw)
        ns = argparse.Namespace(
            name=vm.name,
            vcpus=None,
            mem=None,
            numa_nodes=None,
            disk_cache=cache,
        )
        with patch.object(vm_commands, "is_running", return_value=False):
            vm_commands.cmd_set(ns)
        return VMInfo.load(vm.name)

    def test_sets_the_mode(self, vm_dirs: Any) -> None:
        assert self._set("none").disk_cache == "none"

    def test_default_clears_it(self, vm_dirs: Any) -> None:
        assert self._set("default", disk_cache="unsafe").disk_cache == ""

    def test_shared_disks_refuse_another_mode(self, vm_dirs: Any) -> None:
        with pytest.raises(SystemExit):
            self._set("writeback", share_disks=True)

    def test_keeps_the_disk_dir(self, vm_dirs: Any) -> None:
        _, _, disks = vm_dirs
        vm = self._set("none", disk_dir=str(disks), disk_prealloc="falloc")
        assert vm.disk_dir == str(disks)
        assert vm.disk_prealloc == "falloc"


# ── cluster create ───────────────────────────────────────


class TestCluster:
    def _argv(self, node: ClusterNode, **kw: Any) -> list[str]:
        with patch.object(vm_cluster, "_run_prefixed") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._create_one_node(node, 2, None, **kw)
        return run.call_args.args[0]

    def test_options_reach_each_node(self) -> None:
        argv = self._argv(
            vm_cluster.parse_node_spec("oss:d-oss:2"),
            disk_dir="/hdd",
            disk_cache="none",
            disk_prealloc="full",
        )
        assert argv[argv.index("--disk-dir") + 1] == "/hdd"
        assert argv[argv.index("--disk-cache") + 1] == "none"
        assert "--disk-prealloc=full" in argv

    def test_none_given_adds_nothing(self) -> None:
        argv = self._argv(vm_cluster.parse_node_spec("oss:d-oss:2"))
        assert not [a for a in argv if a.startswith("--disk-")]

    def test_partner_gets_none_of_them(self) -> None:
        partner = ClusterNode(
            name="d-p", roles=["failover"], failover_for="d-m"
        )
        argv = self._argv(
            partner, disk_dir="/hdd", disk_cache="none", disk_prealloc="falloc"
        )
        assert not [a for a in argv if a.startswith("--disk-")]

    def test_failover_refuses_a_cached_mode(self, vm_dirs: Any) -> None:
        args = argparse.Namespace(
            name="d",
            nodes=["mgs+mds:d-mds1:1", "client:d-c"],
            failover={"d-mds2": "d-mds1"},
            vcpus=2,
            mem=None,
            os=None,
            arch=None,
            disk_size=None,
            nic=None,
            disk_cache="writeback",
        )
        with (
            patch.object(vm_cluster.subprocess, "run") as run,
            pytest.raises(SystemExit),
        ):
            vm_cluster.cmd_cluster_create(args)
        assert not run.called

    def test_dry_run_names_them(
        self, vm_dirs: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, _, disks = vm_dirs
        args = argparse.Namespace(
            name="d",
            nodes=["mgs+mds:d-mds1:1", "client:d-c"],
            vcpus=2,
            mem=None,
            os=None,
            arch=None,
            disk_size=None,
            nic=None,
            disk_dir=str(disks),
            disk_cache="none",
            disk_prealloc="falloc",
            dry_run=True,
            owner_id="test",
        )
        vm_cluster.cmd_cluster_create(args)
        out = capsys.readouterr().out
        assert f"disks:   in {disks}" in out
        assert "cache:   none" in out
        assert "prealloc: falloc" in out


# ── parser ───────────────────────────────────────────────


@pytest.fixture(scope="module")
def parser() -> argparse.ArgumentParser:
    loader = importlib.machinery.SourceFileLoader(
        "ltvm_cli_disk_options",
        str(Path(__file__).resolve().parent.parent / "ltvm"),
    )
    spec = importlib.util.spec_from_loader("ltvm_cli_disk_options", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod.build_parser()


class TestParser:
    def test_defaults_are_unset(self, parser: argparse.ArgumentParser) -> None:
        a = parser.parse_args(["create", "co1-x"])
        assert (a.disk_dir, a.disk_cache, a.disk_prealloc) == (None, None, None)

    def test_create_flags(self, parser: argparse.ArgumentParser) -> None:
        a = parser.parse_args(
            [
                "create",
                "co1-x",
                "--disk-dir",
                "/hdd",
                "--disk-cache",
                "none",
                "--disk-prealloc",
            ]
        )
        assert (a.disk_dir, a.disk_cache, a.disk_prealloc) == (
            "/hdd",
            "none",
            "falloc",
        )
        a = parser.parse_args(["vm", "create", "co1-x", "--disk-prealloc=full"])
        assert a.disk_prealloc == "full"

    def test_bad_cache_mode_is_refused(
        self, parser: argparse.ArgumentParser
    ) -> None:
        with pytest.raises(SystemExit):
            parser.parse_args(["create", "co1-x", "--disk-cache", "fast"])

    def test_cluster_and_set(self, parser: argparse.ArgumentParser) -> None:
        a = parser.parse_args(
            [
                "cluster",
                "create",
                "co1",
                "mgs+mds:co1-m:1",
                "--disk-dir",
                "/hdd",
                "--disk-cache",
                "directsync",
                "--disk-prealloc",
            ]
        )
        assert (a.disk_dir, a.disk_cache, a.disk_prealloc) == (
            "/hdd",
            "directsync",
            "falloc",
        )
        a = parser.parse_args(["vm", "set", "co1-x", "--disk-cache", "default"])
        assert a.disk_cache == "default"
