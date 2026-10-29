"""Cluster framework settings retain explicit LNet IDs and address families."""

from __future__ import annotations

import subprocess

import pytest

from ltvm_pkg import vm_cluster
from ltvm_pkg.cluster_rail_config import (
    PROFILE_FAILOVER_ERROR,
    require_no_profile_failover,
    validate_profile_probe,
)
from ltvm_pkg.rail_topology import resolve_topology
from ltvm_pkg.vm_state import ClusterInfo, VMInfo


@pytest.mark.parametrize("network", ["tcp7", "o2ib9"])
def test_custom_network_id_reaches_framework(network: str) -> None:
    """NETTYPE and the MGS seed must name the same configured LNet network."""
    output = (
        f"{network}(eth1),{network[:-1]}8(eth2)\n--\n"
        "2: eth0 inet 192.0.2.10/24 scope global eth0\n"
        "3: eth1 inet 172.18.101.30/24 scope global eth1\n"
        "4: eth2 inet 172.18.102.30/24 scope global eth2\n"
    )
    lnet = vm_cluster.lnet_from_probe(output)
    cluster = ClusterInfo(
        name="rail-cluster",
        nodes=[
            {
                "name": "rail-mgs",
                "roles": ["mgs", "mds"],
                "mdt_disks": 1,
                "ost_disks": 0,
                "ip": "192.0.2.10",
            }
        ],
    )
    generated = vm_cluster.generate_local_sh(cluster, lnet=lnet)
    # Exercise the generated shell values as the framework consumes them.
    result = subprocess.run(
        [
            "bash",
            "-eu",
            "-c",
            generated + '\nprintf "%s\\n%s\\n" "$NETTYPE" "$MGSNID"',
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [network, f"172.18.101.30@{network}"]
    assert "192.0.2.10@" not in result.stdout


def test_custom_ipv6_network_id_is_not_changed_to_tcp() -> None:
    """Large NID selection changes the address family while preserving its net ID."""
    lnet = vm_cluster.lnet_from_probe(
        "tcp7(eth1),tcp8(eth2)\n--\n"
        "3: eth1 inet 172.18.101.30/24 scope global eth1\n"
        "3: eth1 inet6 fd17:2016:2000:1001:f172:f018:f101:f030/64 scope global\n",
        "ipv6",
    )
    assert lnet.nettype == "tcp7"
    assert lnet.mgsnid == "fd17:2016:2000:1001:f172:f018:f101:f030@tcp7"
    assert lnet.force_large_nid


def _profile_vm(mode: str = "isolated", nic: str = "tcp") -> VMInfo:
    """Build saved dual-stack data addresses for a fixed two-rail profile."""
    transport = "tcp" if nic == "tcp" else "o2ib"
    ids = [f"{transport}7", f"{transport}{7 if mode == 'shared' else 8}"]
    topology = resolve_topology([nic, nic], mode, ids)
    assert topology is not None
    ips = [
        rail.subnet4.rsplit(".", 1)[0] + f".{30 + index}"
        for index, rail in enumerate(topology.rails)
    ]
    return VMInfo(
        name="profile-node",
        ip="192.0.2.10",
        nics=[nic, nic],
        nic_ips=ips,
        nic_ip6s=[
            rail.ipv6_for(address) for rail, address in zip(topology.rails, ips)
        ],
        rail_topology=topology,
    )


def _probe_profile(vm: VMInfo) -> str:
    """Use the shipped guest emitter so the test reaches the configuration boundary."""
    import re
    from pathlib import Path

    assert vm.rail_topology is not None
    emitter = (
        Path(__file__).resolve().parents[1]
        / "targets/common/setup-lnet-config.sh"
    )
    command_line = (
        f"fc_nics={','.join(vm.nics)} fc_rail_topology={vm.rail_topology.mode} "
        f"fc_lnet_nets={','.join(rail.lnet_network for rail in vm.rail_topology.rails)}"
    )
    emitted = subprocess.run(
        ["bash", str(emitter), "--stdin"],
        input=command_line + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert emitted.returncode == 0, emitted.stderr
    match = re.fullmatch('options lnet networks="([^"\n]+)"\n', emitted.stdout)
    assert match is not None
    lines = [
        match.group(1),
        "--",
        "2: eth0 inet 192.0.2.10/24 scope global eth0",
    ]
    for index, (ipv4, ipv6) in enumerate(zip(vm.nic_ips, vm.nic_ip6s), start=1):
        lines.append(
            f"{index + 2}: eth{index} inet {ipv4}/24 scope global eth{index}"
        )
        lines.append(
            f"{index + 2}: eth{index} inet6 {ipv6}/64 scope global eth{index}"
        )
        lines.append(
            f"{index + 2}: eth{index} inet6 fe80::{index}/64 scope link eth{index}"
        )
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("mode", ["shared", "isolated"])
@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
def test_guest_emitter_and_saved_profile_agree(mode: str, family: str) -> None:
    """Both address families validate the exact grouped or isolated guest topology."""
    vm = _profile_vm(mode)
    output = _probe_profile(vm)
    validate_profile_probe(vm, output, family)
    lnet = vm_cluster.lnet_from_probe(output, family)
    assert lnet.nettype == "tcp7"
    expected = vm.nic_ips[0] if family == "ipv4" else vm.nic_ip6s[0]
    assert lnet.mgsnid == f"{expected}@tcp7"


@pytest.mark.parametrize("mode", ["shared", "isolated"])
def test_softroce_guest_mapping_agrees(mode: str) -> None:
    """SoftRoCE uses the same saved data interfaces without management addresses."""
    vm = _profile_vm(mode, "softroce")
    validate_profile_probe(vm, _probe_profile(vm), "ipv4")
    lnet = vm_cluster.lnet_from_probe(_probe_profile(vm))
    assert lnet.nettype == "o2ib7"
    assert lnet.mgsnid == f"{vm.nic_ips[0]}@o2ib7"


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "management",
        "wrong_id",
        "wrong_rail",
        "missing_address",
        "extra_address",
        "duplicate_source",
    ],
)
def test_profile_probe_never_falls_back_to_legacy(change: str) -> None:
    """A mismatched guest must fail before the cluster build or framework setup."""
    vm = _profile_vm()
    output = _probe_profile(vm)
    if change == "missing":
        output = output[output.index("--\n") :]
    elif change == "management":
        output = output.replace("tcp7(eth1),tcp8(eth2)", "tcp0(eth0)")
    elif change == "wrong_id":
        output = output.replace("tcp8(eth2)", "tcp7(eth2)")
    elif change == "wrong_rail":
        output = output.replace(
            "tcp7(eth1),tcp8(eth2)", "tcp7(eth2),tcp8(eth1)"
        )
    elif change == "missing_address":
        output = output.replace(vm.nic_ips[1], "172.18.102.99")
    elif change == "extra_address":
        output += "3: eth1 inet 172.18.101.99/24 scope global eth1\n"
    else:
        output += f"2: eth0 inet {vm.nic_ips[0]}/24 scope global eth0\n"
    with pytest.raises(ValueError):
        validate_profile_probe(vm, output, "ipv4")


def test_profile_probe_requires_valid_ip_family() -> None:
    """Invalid family names cannot silently select IPv4."""
    vm = _profile_vm()
    with pytest.raises(ValueError, match="unsupported cluster IP family"):
        validate_profile_probe(vm, _probe_profile(vm), "ipv5")


def test_legacy_probe_remains_unchanged() -> None:
    """Old VMs keep the existing guest probe behavior without new requirements."""
    validate_profile_probe(VMInfo(name="legacy", ip="192.0.2.1"), "", "ipv4")


def test_profile_server_failover_is_checked() -> None:
    """Server HA remains separate from supported data-rail failure recovery."""
    vm = _profile_vm()
    with pytest.raises(ValueError, match=PROFILE_FAILOVER_ERROR):
        require_no_profile_failover([vm], True)
    require_no_profile_failover([vm], False)
    require_no_profile_failover([VMInfo(name="legacy", ip="192.0.2.1")], True)
