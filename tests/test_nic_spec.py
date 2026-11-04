"""The --nic TYPE[:key=value,...] parser (ltvm_pkg/nic_spec.py)."""

from __future__ import annotations

import argparse
from typing import Any
from unittest.mock import patch

import pytest

from ltvm_pkg import vm_cluster
from ltvm_pkg.nic_spec import NicSpec, NicSpecError, load_stored, numa_problem
from ltvm_pkg.vm_cluster import ClusterNode
from ltvm_pkg.vm_state import VMInfo
from tests.test_failover_pair import vm_dirs  # noqa: F401


class TestParse:
    def test_bare_types(self) -> None:
        assert NicSpec.parse("tcp") == NicSpec("tcp")
        assert NicSpec.parse("softroce") == NicSpec("softroce")

    def test_type_is_case_insensitive(self) -> None:
        assert NicSpec.parse("TCP") == NicSpec("tcp")

    def test_passthrough_positional_bdf(self) -> None:
        """The spelling ltvm always took: the BDF's colons survive."""
        spec = NicSpec.parse("passthrough:0000:85:00.1")
        assert spec == NicSpec("passthrough", bdf="0000:85:00.1")

    def test_passthrough_keyed_bdf(self) -> None:
        assert NicSpec.parse("passthrough:bdf=0000:85:00.1") == NicSpec(
            "passthrough", bdf="0000:85:00.1"
        )

    @pytest.mark.parametrize(
        "raw, message",
        [
            ("", "empty"),
            ("infiniband", "unknown --nic type"),
            ("tcp:", "nothing after ':'"),
            ("tcp:foo", "not key=value"),
            ("tcp:color=red", "unknown key 'color'"),
            ("tcp:node=-1", "expected a NUMA node number"),
            ("tcp:node=x", "expected a NUMA node number"),
            ("tcp:node=1,node=2", "node given twice"),
            ("passthrough:0000:85:00.1,node=1", "unknown key 'node'"),
            ("tcp:bdf=0000:85:00.1", "unknown key 'bdf' for type 'tcp'"),
            ("passthrough", "requires a PCIe BDF"),
            ("passthrough:not-a-bdf", "not a valid PCIe BDF"),
            (
                "passthrough:0000:85:00.1,bdf=0000:85:00.2",
                "bdf given twice",
            ),
        ],
    )
    def test_rejects(self, raw: str, message: str) -> None:
        with pytest.raises(NicSpecError, match=message):
            NicSpec.parse(raw)

    def test_error_lists_the_types_keys(self) -> None:
        with pytest.raises(NicSpecError, match=r"keys for 'passthrough': bdf"):
            NicSpec.parse("passthrough:0000:85:00.1,x=1")

    def test_node(self) -> None:
        assert NicSpec.parse("tcp:node=2") == NicSpec("tcp", node=2)
        assert NicSpec.parse("softroce:NODE=0") == NicSpec("softroce", node=0)


class TestCanonical:
    @pytest.mark.parametrize(
        "raw",
        ["tcp", "softroce", "passthrough:0000:85:00.1", "tcp:node=0"],
    )
    def test_round_trip(self, raw: str) -> None:
        """str() is what .info stores, and parse() reads it back."""
        assert str(NicSpec.parse(raw)) == raw
        assert NicSpec.parse(str(NicSpec.parse(raw))) == NicSpec.parse(raw)

    def test_keyed_bdf_stores_positional(self) -> None:
        """Older ltvm reads the stored spelling of a passthrough NIC."""
        assert (
            str(NicSpec.parse("passthrough:bdf=0000:85:00.1"))
            == "passthrough:0000:85:00.1"
        )

    def test_cmdline_entry(self) -> None:
        """fc_nics= keeps the TYPE[;ARG] form the guest has always read."""
        assert NicSpec("tcp").cmdline_entry() == "tcp"
        assert NicSpec("tcp", node=1).cmdline_entry() == "tcp"
        assert (
            NicSpec("passthrough", bdf="0000:85:00.1").cmdline_entry()
            == "passthrough;0000;85;00.1"
        )


class TestNumaProblem:
    def test_fits(self) -> None:
        nics = [NicSpec("tcp", node=0), NicSpec("tcp"), NicSpec("tcp", node=1)]
        assert numa_problem(nics, 2) is None
        assert numa_problem([NicSpec("tcp")], 0) is None

    def test_needs_numa(self) -> None:
        assert "needs --numa-nodes 2" in (
            numa_problem([NicSpec("tcp", node=0)], 0) or ""
        )

    def test_node_out_of_range(self) -> None:
        msg = numa_problem([NicSpec("tcp"), NicSpec("tcp", node=2)], 2) or ""
        assert "eth2" in msg and "0-1" in msg


class TestLoadStored:
    def test_valid(self) -> None:
        assert load_stored("passthrough:0000:85:00.1").bdf == "0000:85:00.1"

    def test_old_junk_arg_keeps_the_type(self) -> None:
        """ltvm used to store tcp:<anything>; the VM must still load."""
        assert load_stored("tcp:whatever") == NicSpec("tcp")

    def test_bad_passthrough_still_fails(self) -> None:
        with pytest.raises(NicSpecError):
            load_stored("passthrough:nope")


class TestClusterCreate:
    def _args(self, nic: list[str]) -> argparse.Namespace:
        return argparse.Namespace(
            name="g",
            nodes=["mgs+mds:g-mds:1", "client:g-c"],
            vcpus=2,
            mem=2048,
            os=None,
            arch=None,
            disk_size=None,
            nic=nic,
            owner_id=None,
        )

    def test_bad_spec_fails_before_any_node(
        self,
        vm_dirs: Any,  # noqa: F811
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with (
            patch.object(vm_cluster, "_create_one_node") as create,
            pytest.raises(SystemExit),
        ):
            vm_cluster.cmd_cluster_create(self._args(["tcp", "tcp:bogus=1"]))
        create.assert_not_called()
        assert "unknown key 'bogus'" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (
                ["TCP", "passthrough:bdf=0000:85:00.1"],
                ["tcp", "passthrough:0000:85:00.1"],
            ),
            (
                [
                    "TCP:net=tcp0,subnet=192.0.2.0/24,subnet6=2001:db8:1::/64",
                    "tcp:net=tcp0,subnet=198.51.100.0/24,subnet6=2001:db8:2::/64",
                ],
                [
                    "tcp:net=tcp0,subnet=192.0.2.0/24,subnet6=2001:db8:1::/64",
                    "tcp:net=tcp0,subnet=198.51.100.0/24,subnet6=2001:db8:2::/64",
                ],
            ),
        ],
    )
    def test_nodes_get_the_canonical_spelling(
        self,
        vm_dirs: Any,  # noqa: F811
        raw: list[str],
        expected: list[str],
    ) -> None:
        """Every node receives all NIC options in their canonical spelling."""
        seen: list[list[str]] = []

        def fake_create(node: ClusterNode, *a: Any, **kw: Any) -> Any:
            seen.append(a[6])
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
            vm_cluster.cmd_cluster_create(self._args(raw))
        assert seen == [expected] * 2

    @pytest.mark.parametrize(
        "subnets",
        [
            ("203.0.113.0/24", "192.0.2.0/24"),
            ("192.0.2.0/24", "192.0.2.0/25"),
        ],
    )
    @pytest.mark.usefixtures("vm_dirs")
    def test_subnet_conflict_fails_before_any_node(
        self,
        capsys: pytest.CaptureFixture[str],
        subnets: tuple[str, str],
    ) -> None:
        """A layout error must not create a partial cluster."""
        with (
            patch(
                "ltvm_pkg.nic_allocation._data_defaults",
                return_value=(
                    "203.0.113.0/24",
                    "192.0.2.0/24",
                    "2001:db8::/64",
                ),
            ),
            patch.object(vm_cluster, "_create_one_node") as create,
            pytest.raises(SystemExit),
        ):
            vm_cluster.cmd_cluster_create(
                self._args(
                    [f"tcp:net=tcp0,subnet={network}" for network in subnets]
                )
            )
        create.assert_not_called()
        assert "overlap" in capsys.readouterr().err

    def test_node_beyond_numa_nodes_fails_before_any_node(
        self,
        vm_dirs: Any,  # noqa: F811
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        args = self._args(["tcp:node=2"])
        args.numa_nodes = 2
        with (
            patch.object(vm_cluster, "_create_one_node") as create,
            pytest.raises(SystemExit),
        ):
            vm_cluster.cmd_cluster_create(args)
        create.assert_not_called()
        assert "node 2 does not exist" in capsys.readouterr().err
