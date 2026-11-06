"""The NUMA distance table (--numa-dist, --numa-dist-file)."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ltvm_pkg import numa_dist, qemu_run, vm_cluster, vm_commands
from ltvm_pkg.numa_dist import NumaDistError
from ltvm_pkg.vm_cluster import ClusterNode
from ltvm_pkg.vm_state import VMInfo
from tests.test_failover_pair import vm_dirs  # noqa: F401
from tests.test_qemu_run import (  # noqa: F401
    _force_linux_host,
    _LaunchHarness,
    _make_vm,
    _run_launch,
    tmp_vmdir,
)

FIXTURE = Path(__file__).parent / "fixtures" / "numa" / "numactl-6node.txt"

# A real two-socket, six-node table; asymmetric (0->3 is 21, 3->0 is 21,
# but 0->4 is 28 while 4->0 is 23).
SIX = [
    [10, 15, 17, 21, 28, 26],
    [15, 10, 15, 23, 26, 23],
    [17, 15, 10, 26, 23, 21],
    [21, 28, 26, 10, 15, 17],
    [23, 26, 23, 15, 10, 15],
    [26, 23, 21, 17, 15, 10],
]
SIX_TEXT = numa_dist.format_matrix(SIX)


class TestParse:
    def test_inline(self) -> None:
        assert numa_dist.parse_matrix("10,15;15,10") == [[10, 15], [15, 10]]

    def test_inline_spaces(self) -> None:
        assert numa_dist.parse_matrix(" 10 15 ; 15, 10 ") == [
            [10, 15],
            [15, 10],
        ]

    def test_numactl_full_output(self) -> None:
        assert numa_dist.parse_numactl(FIXTURE.read_text()) == SIX

    def test_numactl_block_only(self) -> None:
        block = FIXTURE.read_text().split("node distances:\n")[1]
        assert numa_dist.parse_numactl(block) == SIX

    @pytest.mark.parametrize(
        "text, message",
        [
            ("", "empty"),
            ("10,x;15,10", "not a number"),
            ("10,15;", "row 1 is empty"),
        ],
    )
    def test_inline_rejects(self, text: str, message: str) -> None:
        with pytest.raises(NumaDistError, match=message):
            numa_dist.parse_matrix(text)

    def test_numactl_without_table(self) -> None:
        with pytest.raises(NumaDistError, match="no 'node distances:'"):
            numa_dist.parse_numactl("available: 1 nodes (0)\n")

    def test_numactl_with_a_gap(self) -> None:
        text = "node distances:\nnode 0 2\n  0: 10 20\n  2: 20 10\n"
        with pytest.raises(NumaDistError):
            numa_dist.parse_numactl(text)

    def test_round_trip(self) -> None:
        assert numa_dist.parse_matrix(SIX_TEXT) == SIX


class TestProblem:
    def test_asymmetric_is_fine(self) -> None:
        assert numa_dist.problem(SIX, 6) is None

    def test_needs_numa(self) -> None:
        assert "--numa-nodes 2" in (numa_dist.problem([[10]], 0) or "")

    def test_wrong_size(self) -> None:
        assert "must be 2x2" in (numa_dist.problem(SIX, 2) or "")

    def test_ragged(self) -> None:
        assert "must be 2x2" in (numa_dist.problem([[10, 20], [20]], 2) or "")

    def test_diagonal(self) -> None:
        assert "to itself is 11" in (
            numa_dist.problem([[11, 20], [20, 10]], 2) or ""
        )

    @pytest.mark.parametrize("value", [10, 9, 256])
    def test_off_diagonal_range(self, value: int) -> None:
        assert "11-255" in (numa_dist.problem([[10, value], [20, 10]], 2) or "")


class TestQemuArgs:
    def test_every_off_diagonal_pair(self) -> None:
        args = numa_dist.qemu_args(SIX)
        assert len(args) == 2 * 30
        pairs = args[1::2]
        assert "dist,src=0,dst=4,val=28" in pairs
        assert "dist,src=4,dst=0,val=23" in pairs
        assert not any(
            f"src={i},dst={i}," in p for p in pairs for i in range(6)
        )

    def test_smp_args_appends_them(self) -> None:
        args = qemu_run.smp_args(12, 6144, 6, SIX_TEXT)
        joined = " ".join(args)
        assert joined.count("-numa node,") == 6
        assert joined.count("-numa dist,") == 30

    def test_smp_args_default_has_none(self) -> None:
        assert "dist," not in " ".join(qemu_run.smp_args(4, 4096, 2))


def test_launch_passes_the_table(tmp_vmdir: Path) -> None:  # noqa: F811
    vm = _make_vm(tmp_vmdir)
    vm.vcpus = 4
    vm.numa_nodes = 2
    vm.numa_dist = "10,15;17,10"
    h = _LaunchHarness()
    _run_launch(vm, h)
    joined = " ".join(h.qemu_argv or [])
    assert "-numa dist,src=0,dst=1,val=15" in joined
    assert "-numa dist,src=1,dst=0,val=17" in joined


def test_info_round_trip(tmp_vmdir: Path) -> None:  # noqa: F811
    vm = _make_vm(tmp_vmdir)
    vm.numa_nodes = 6
    vm.numa_dist = SIX_TEXT
    vm.save()
    assert VMInfo.load(vm.name).numa_dist == SIX_TEXT


def test_old_info_has_none(tmp_vmdir: Path) -> None:  # noqa: F811
    vm = _make_vm(tmp_vmdir)
    vm.save()
    text = vm.info_path.read_text()
    vm.info_path.write_text(
        "".join(ln for ln in text.splitlines(True) if "NUMA_DIST" not in ln)
    )
    assert VMInfo.load(vm.name).numa_dist == ""


class TestResolve:
    def test_neither(self) -> None:
        assert numa_dist.resolve(None, None, 2) == ""

    def test_file(self) -> None:
        assert numa_dist.resolve(None, str(FIXTURE), 6) == SIX_TEXT

    def test_both(self) -> None:
        with pytest.raises(NumaDistError, match="not both"):
            numa_dist.resolve("10,20;20,10", str(FIXTURE), 2)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(NumaDistError, match="--numa-dist-file"):
            numa_dist.resolve(None, str(tmp_path / "nope"), 2)

    def test_does_not_fit(self) -> None:
        with pytest.raises(NumaDistError, match="must be 2x2"):
            numa_dist.resolve(None, str(FIXTURE), 2)


class TestVmSet:
    def _vm(self, tmp_vmdir: Path, **kw: Any) -> VMInfo:  # noqa: F811
        vm = _make_vm(tmp_vmdir)
        vm.vcpus = 12
        vm.mem = 6144
        for k, v in kw.items():
            setattr(vm, k, v)
        vm.save()
        return vm

    def _set(self, vm: VMInfo, **kw: Any) -> VMInfo:
        ns = argparse.Namespace(name=vm.name, vcpus=None, mem=None, **kw)
        with patch.object(vm_commands, "is_running", return_value=False):
            vm_commands.cmd_set(ns)
        return VMInfo.load(vm.name)

    def test_sets_from_file(self, tmp_vmdir: Path) -> None:  # noqa: F811
        vm = self._vm(tmp_vmdir, numa_nodes=6)
        got = self._set(vm, numa_dist_file=str(FIXTURE))
        assert got.numa_dist == SIX_TEXT

    def test_default_clears(self, tmp_vmdir: Path) -> None:  # noqa: F811
        vm = self._vm(tmp_vmdir, numa_nodes=6, numa_dist=SIX_TEXT)
        assert self._set(vm, numa_dist="default").numa_dist == ""

    def test_new_node_count_must_fit_the_table(
        self,
        tmp_vmdir: Path,  # noqa: F811
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        vm = self._vm(tmp_vmdir, numa_nodes=6, numa_dist=SIX_TEXT)
        with pytest.raises(SystemExit):
            self._set(vm, numa_nodes=2)
        assert "--numa-dist default" in capsys.readouterr().err
        assert VMInfo.load(vm.name).numa_nodes == 6

    def test_new_count_and_table_together(
        self,
        tmp_vmdir: Path,  # noqa: F811
    ) -> None:
        vm = self._vm(tmp_vmdir, numa_nodes=6, numa_dist=SIX_TEXT)
        got = self._set(vm, numa_nodes=2, numa_dist="10,30;30,10")
        assert (got.numa_nodes, got.numa_dist) == (2, "10,30;30,10")

    def test_refuses_a_bad_table(self, tmp_vmdir: Path) -> None:  # noqa: F811
        vm = self._vm(tmp_vmdir, numa_nodes=2)
        with pytest.raises(SystemExit):
            self._set(vm, numa_dist="10,20;20,11")


class TestCluster:
    def _args(self, **kw: Any) -> argparse.Namespace:
        return argparse.Namespace(
            name="g",
            nodes=["mgs+mds:g-mds:1", "client:g-c"],
            vcpus=12,
            mem=6144,
            os=None,
            arch=None,
            disk_size=None,
            nic=[],
            owner_id=None,
            **kw,
        )

    def test_file_read_once_and_passed_inline(
        self,
        vm_dirs: Any,  # noqa: F811
    ) -> None:
        seen: list[str] = []

        def fake_create(node: ClusterNode, *a: Any, **kw: Any) -> Any:
            seen.append(kw["numa_dist"])
            VMInfo(name=node.name, ip="192.0.2.30").save()
            return node.name, 0, ""

        with (
            patch.object(
                vm_cluster, "_create_one_node", side_effect=fake_create
            ),
            patch.object(
                vm_cluster, "_distribute_cluster_hosts", return_value=[]
            ),
        ):
            vm_cluster.cmd_cluster_create(
                self._args(numa_nodes=6, numa_dist_file=str(FIXTURE))
            )
        assert seen == [SIX_TEXT, SIX_TEXT]

    def test_node_argv(self) -> None:
        node = ClusterNode(name="g-c", roles=["client"])
        with patch.object(
            vm_cluster,
            "_run_prefixed",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            vm_cluster._create_one_node(
                node, 12, None, numa_nodes=6, numa_dist=SIX_TEXT
            )
        argv = run.call_args.args[0]
        assert f"--numa-dist={SIX_TEXT}" in argv

    def test_bad_table_fails_before_any_node(
        self,
        vm_dirs: Any,  # noqa: F811
    ) -> None:
        with (
            patch.object(vm_cluster, "_create_one_node") as create,
            pytest.raises(SystemExit),
        ):
            vm_cluster.cmd_cluster_create(
                self._args(numa_nodes=2, numa_dist_file=str(FIXTURE))
            )
        create.assert_not_called()


def test_cli_flags_parse() -> None:
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader(
        "ltvm_cli_numa_dist",
        str(Path(__file__).resolve().parent.parent / "ltvm"),
    )
    spec = importlib.util.spec_from_loader("ltvm_cli_numa_dist", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    p = mod.build_parser()
    a = p.parse_args(["create", "co1-x", "--numa-dist", "10,20;20,10"])
    assert a.numa_dist == "10,20;20,10"
    a = p.parse_args(["vm", "set", "co1-x", "--numa-dist-file", "f"])
    assert a.numa_dist_file == "f"
    with pytest.raises(SystemExit):
        p.parse_args(
            ["create", "co1-x", "--numa-dist", "x", "--numa-dist-file", "f"]
        )
