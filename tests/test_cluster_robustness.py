"""Cluster commands must survive one bad node / one bad file."""

from __future__ import annotations

import argparse
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import vm_cluster
from ltvm_pkg.vm_state import ClusterInfo, ClusterNode


class TestParallelClusterOpErrorHandling:
    def test_one_node_raising_does_not_abort_the_rest(
        self, capsys: Any
    ) -> None:
        """_deploy_one_node can raise past its own except RuntimeError.

        VMInfo.load raises VMNotFound, which is not a RuntimeError, and
        an unguarded future.result() aborted the whole fan-out with a
        traceback: no per-node report, no failure summary, local.sh
        never distributed.
        """
        nodes = [
            ClusterNode(name="co2-mds", roles=["mgs", "mds"]),
            ClusterNode(name="co2-oss", roles=["oss"]),
        ]

        def submit(node):
            if node.name == "co2-oss":
                raise RuntimeError("VM co2-oss not found")
            return (node.name, 0, "")

        failed = vm_cluster._parallel_cluster_op(
            nodes, submit, "deployed", "deploy failed"
        )
        assert failed == ["co2-oss"]
        out = capsys.readouterr().out
        assert "co2-mds: deployed" in out
        assert "co2-oss" in out

    def test_non_runtime_exception_also_collected(self, capsys: Any) -> None:
        nodes = [ClusterNode(name="co2-a", roles=["oss"])]

        def submit(node):
            raise KeyError("some other failure")

        failed = vm_cluster._parallel_cluster_op(
            nodes, submit, "deployed", "deploy failed"
        )
        assert failed == ["co2-a"]


class TestClusterInfoResilience:
    def test_unknown_node_field_is_ignored_not_fatal(self) -> None:
        """A .cluster written by a newer ltvm must still list.

        get_nodes() did ClusterNode(**n), so any added field made every
        older ltvm's `cluster list` traceback -- and cmd_cluster_list
        calls get_nodes() outside its try block, so one such file
        aborted the listing of all clusters.
        """
        ci = ClusterInfo(
            name="co2",
            nodes=[
                {
                    "name": "co2-mds",
                    "roles": ["mgs", "mds"],
                    "some_future_field": 1,
                }
            ],
        )
        nodes = ci.get_nodes()
        assert len(nodes) == 1
        assert nodes[0].name == "co2-mds"

    def test_node_entry_of_wrong_type_gives_clean_error(self) -> None:
        ci = ClusterInfo(name="co2", nodes=["not-a-dict"])
        with pytest.raises(RuntimeError, match="corrupt cluster state"):
            ci.get_nodes()


class TestClusterExecFansOutAcrossRole:
    def _cluster(self) -> MagicMock:
        ci = MagicMock()
        ci.get_nodes.return_value = [
            ClusterNode(name="co2-mds", roles=["mgs", "mds"]),
            ClusterNode(name="co2-oss1", roles=["oss"]),
            ClusterNode(name="co2-oss2", roles=["oss"]),
        ]
        return ci

    def test_role_runs_on_every_matching_node(self, capsys: Any) -> None:
        """`cluster exec co2 oss` covered one OSS of three and exited 0,
        which reads as "the whole role is healthy"."""
        seen: list[str] = []

        def fake_run_ssh(ip, cmd, timeout=None):
            seen.append(ip)
            return MagicMock(returncode=0, stdout="ok\n", stderr="")

        args = argparse.Namespace(
            name="co2", target="oss", command=["lctl dl"], timeout=30
        )
        with (
            patch.object(
                vm_cluster.ClusterInfo, "load", return_value=self._cluster()
            ),
            patch.object(
                vm_cluster.VMInfo,
                "load",
                side_effect=lambda n: MagicMock(ip=f"ip-{n}"),
            ),
            patch.object(vm_cluster, "run_ssh", side_effect=fake_run_ssh),
            pytest.raises(SystemExit) as exc,
        ):
            vm_cluster.cmd_cluster_exec(args)

        assert exc.value.code == 0
        assert seen == ["ip-co2-oss1", "ip-co2-oss2"]

    def test_failure_on_any_node_is_reported(self) -> None:
        def fake_run_ssh(ip, cmd, timeout=None):
            rc = 1 if ip.endswith("oss2") else 0
            return MagicMock(returncode=rc, stdout="", stderr="")

        args = argparse.Namespace(
            name="co2", target="oss", command=["false"], timeout=30
        )
        with (
            patch.object(
                vm_cluster.ClusterInfo, "load", return_value=self._cluster()
            ),
            patch.object(
                vm_cluster.VMInfo,
                "load",
                side_effect=lambda n: MagicMock(ip=f"ip-{n}"),
            ),
            patch.object(vm_cluster, "run_ssh", side_effect=fake_run_ssh),
            pytest.raises(SystemExit) as exc,
        ):
            vm_cluster.cmd_cluster_exec(args)
        assert exc.value.code == 1

    def test_exact_node_name_selects_only_that_node(self) -> None:
        seen: list[str] = []

        def fake_run_ssh(ip, cmd, timeout=None):
            seen.append(ip)
            return MagicMock(returncode=0, stdout="", stderr="")

        args = argparse.Namespace(
            name="co2", target="co2-oss2", command=["hostname"], timeout=30
        )
        with (
            patch.object(
                vm_cluster.ClusterInfo, "load", return_value=self._cluster()
            ),
            patch.object(
                vm_cluster.VMInfo,
                "load",
                side_effect=lambda n: MagicMock(ip=f"ip-{n}"),
            ),
            patch.object(vm_cluster, "run_ssh", side_effect=fake_run_ssh),
            pytest.raises(SystemExit),
        ):
            vm_cluster.cmd_cluster_exec(args)
        assert seen == ["ip-co2-oss2"]
