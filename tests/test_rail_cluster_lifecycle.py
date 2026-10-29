"""Cluster lifecycle refuses invalid guest profiles before changing a filesystem."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from ltvm_pkg import (
    cluster_failover,
    vm_claim,
    vm_cluster,
    vm_commands,
    vm_state,
)
from ltvm_pkg.cli import cluster as cluster_cli
from ltvm_pkg.rail_topology import resolve_topology
from ltvm_pkg.vm_state import ClusterInfo, VMInfo
from tests.test_rail_cli import _args


def _profile_vm(name: str, host: int) -> VMInfo:
    """Construct complete persisted facts for a dual-stack shared profile."""
    topology = resolve_topology(["tcp", "tcp"], "shared", ["tcp7", "tcp7"])
    assert topology is not None
    ips = [f"172.18.100.{host}", f"172.18.100.{host + 1}"]
    return VMInfo(
        name,
        f"192.168.100.{host}",
        nics=["tcp", "tcp"],
        nic_ips=ips,
        nic_ip6s=[rail.ipv6_for(ip) for rail, ip in zip(topology.rails, ips)],
        rail_topology=topology,
    )


def _probe(vm: VMInfo) -> str:
    """Supply the complete guest config and iproute2 address response."""
    return "tcp7(eth1,eth2)\n--\n" + "\n".join(
        line
        for index, (ipv4, ipv6) in enumerate(
            zip(vm.nic_ips, vm.nic_ip6s), start=1
        )
        for line in (
            f"{index + 2}: eth{index} inet {ipv4}/24 scope global eth{index}",
            f"{index + 2}: eth{index} inet6 {ipv6}/64 scope global",
        )
    )


@pytest.fixture
def saved_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ClusterInfo, dict[str, VMInfo]]:
    """Keep real cluster roles while replacing all VM files and process checks."""
    vms = {
        vm.name: vm
        for vm in (_profile_vm("co1-srv", 30), _profile_vm("co1-cli", 40))
    }
    cluster = ClusterInfo(
        "co1-lab",
        nodes=[
            {
                "name": "co1-srv",
                "roles": ["mgs", "mds", "oss"],
                "mdt_disks": 1,
                "ost_disks": 1,
                "ip": vms["co1-srv"].ip,
            },
            {"name": "co1-cli", "roles": ["client"], "ip": vms["co1-cli"].ip},
        ],
    )
    monkeypatch.setattr(ClusterInfo, "load", MagicMock(return_value=cluster))
    monkeypatch.setattr(VMInfo, "load", MagicMock(side_effect=vms.__getitem__))
    monkeypatch.setattr(vm_cluster, "_node_state", MagicMock(return_value="up"))
    monkeypatch.setattr(vm_claim, "require_all", MagicMock())
    monkeypatch.setattr(vm_claim, "auto_claim", MagicMock())
    monkeypatch.setattr(vm_cluster, "_validate_lustre_source", MagicMock())
    return cluster, vms


def test_profile_server_ha_create_fails_before_privileges_or_record_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unsupported combination cannot prompt for root or erase stale state."""
    monkeypatch.setattr(vm_commands.sys, "platform", "linux")
    monkeypatch.setattr(vm_state, "SUBNET", "192.168.100")
    args = _args("-T", "shared", "--failover", "co1-alt=co1-srv", cluster=True)
    privilege = MagicMock()
    monkeypatch.setattr(cluster_cli, "_cluster_privileges", privilege)
    assert cluster_cli.cmd_cluster_create(args) == 1
    privilege.assert_not_called()
    record = tmp_path / "co1-lab.cluster"
    record.write_text("keep this record\n")
    monkeypatch.setattr(vm_cluster, "SOCKETS", tmp_path)
    args.nodes = []
    with pytest.raises(SystemExit):
        vm_cluster.cmd_cluster_create(args)
    assert record.read_text() == "keep this record\n"


def test_partial_deploy_validates_undeployed_profile_before_any_build(
    saved_cluster: tuple[ClusterInfo, dict[str, VMInfo]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing client rail config blocks server-only deploy before build or config writes."""
    _cluster, vms = saved_cluster
    ssh = MagicMock(
        side_effect=[
            MagicMock(returncode=0, stdout=_probe(vms["co1-srv"])),
            MagicMock(returncode=0, stdout="\n--\n"),
        ]
    )
    build = MagicMock(side_effect=AssertionError("unexpected build or write"))
    mgs_probe = MagicMock(
        side_effect=AssertionError("unexpected legacy fallback")
    )
    monkeypatch.setattr(vm_cluster, "run_ssh", ssh)
    monkeypatch.setattr(vm_cluster.subprocess, "run", build)
    monkeypatch.setattr(vm_cluster, "probe_mgs_lnet", mgs_probe)
    args = argparse.Namespace(
        name="co1-lab", lustre_source=str(tmp_path), server_only=True
    )
    with pytest.raises(SystemExit):
        vm_cluster.cmd_cluster_deploy(args)
    assert ssh.call_args_list == [
        call(vms[name].ip, vm_cluster.LNET_PROBE, timeout=30)
        for name in ("co1-srv", "co1-cli")
    ]
    build.assert_not_called()
    mgs_probe.assert_not_called()


@pytest.mark.parametrize("ip_family", ["ipv4", "ipv6"])
@pytest.mark.parametrize("cleanup", [False, True])
def test_llmount_checks_every_profile_before_mount_or_cleanup(
    saved_cluster: tuple[ClusterInfo, dict[str, VMInfo]],
    monkeypatch: pytest.MonkeyPatch,
    ip_family: str,
    cleanup: bool,
) -> None:
    """Mount validates each guest; cleanup remains possible after a data-rail fault."""
    cluster, vms = saved_cluster
    cluster.ip_family = ip_family
    if cleanup:
        # Missing data addresses must not prevent management-driven unmount.
        vms["co1-cli"].nic_ip6s = []
    ssh = MagicMock(
        side_effect=[
            MagicMock(returncode=0, stdout=_probe(vms[name]))
            for name in ("co1-srv", "co1-cli")
        ]
    )
    mount = MagicMock()
    monkeypatch.setattr(vm_cluster, "run_ssh", ssh)
    monkeypatch.setattr(
        vm_commands, "_os_family_for_vm", MagicMock(return_value="rhel")
    )
    monkeypatch.setattr(vm_cluster, "_require_cluster_block", MagicMock())
    monkeypatch.setattr(vm_cluster, "_run_llmount", mount)
    vm_cluster.cmd_cluster_llmount(
        argparse.Namespace(
            name=cluster.name,
            cleanup=cleanup,
            server_only=False,
            timeout=17,
        )
    )
    if cleanup:
        # Fault recovery does not require a healthy live LNet configuration.
        ssh.assert_not_called()
    else:
        # A normal mount must check the whole cluster before it can change it.
        assert ssh.call_args_list == [
            call(vms[name].ip, vm_cluster.LNET_PROBE, timeout=30)
            for name in ("co1-srv", "co1-cli")
        ]
    mount.assert_called_once_with(
        cluster,
        "rhel",
        cleanup=cleanup,
        server_only=False,
        timeout=17,
    )


@pytest.mark.parametrize(
    "operation", ["deploy", "llmount", "failover", "failback"]
)
def test_saved_profile_server_ha_is_refused_before_network_or_faults(
    saved_cluster: tuple[ClusterInfo, dict[str, VMInfo]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Hand-edited or older HA records cannot bypass the creation restriction."""
    cluster, vms = saved_cluster
    cluster.nodes.append(
        {"name": "co1-alt", "roles": ["failover"], "failover_for": "co1-srv"}
    )
    vms["co1-alt"] = _profile_vm("co1-alt", 50)
    network = MagicMock(
        side_effect=AssertionError("unexpected network operation")
    )
    fault = MagicMock(
        side_effect=AssertionError("unexpected failover operation")
    )
    monkeypatch.setattr(vm_cluster, "run_ssh", network)
    monkeypatch.setattr(cluster_failover, "cmd_cluster_failover", fault)
    monkeypatch.setattr(cluster_failover, "cmd_cluster_failback", fault)
    args = argparse.Namespace(
        name=cluster.name, lustre_source=str(tmp_path), cleanup=False
    )
    with pytest.raises(SystemExit):
        getattr(vm_cluster, f"cmd_cluster_{operation}")(args)
    network.assert_not_called()
    fault.assert_not_called()


@pytest.mark.parametrize("profile_probe", [False, True])
def test_lnet_probe_timeout_has_a_checked_node_error(
    saved_cluster: tuple[ClusterInfo, dict[str, VMInfo]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile_probe: bool,
) -> None:
    """A stalled guest reports its node and time limit instead of a traceback."""
    cluster, vms = saved_cluster
    ssh = MagicMock(side_effect=subprocess.TimeoutExpired("ssh", 30))
    monkeypatch.setattr(vm_cluster, "run_ssh", ssh)
    with pytest.raises(SystemExit):
        if profile_probe:
            # Profile validation checks the complete persisted rail contract.
            vm_cluster._validate_profile_nodes([vms["co1-srv"]], "ipv4")
        else:
            # The MGS seed probe uses the same checked failure contract.
            vm_cluster._probe_node_lnet(cluster.mgs_node(), "ipv4")
    assert "co1-srv: timed out after 30s" in capsys.readouterr().err
    ssh.assert_called_once_with(
        vms["co1-srv"].ip, vm_cluster.LNET_PROBE, timeout=30
    )
