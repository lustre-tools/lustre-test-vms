"""Persisted topology and concurrent allocation without host networking."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ltvm_pkg import vm_commands, vm_net, vm_state
from ltvm_pkg.rail_topology import MAX_TOPOLOGY_BYTES, resolve_topology
from ltvm_pkg.vm_state import VMInfo


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep all VM state and allocation locks inside this test's directory."""
    sockets = tmp_path / "sockets"
    sockets.mkdir()
    monkeypatch.setattr(vm_state, "SOCKETS", sockets)
    monkeypatch.setattr(vm_state, "SUBNET", "192.168.100")
    monkeypatch.setattr(vm_net, "_IP_LOCK_PATH", tmp_path / "alloc.lock")
    return sockets


def _allocate_vm(name: str, mode: str) -> VMInfo:
    """Allocate and save while holding the real file lock used by create."""
    topology = resolve_topology(["tcp", "tcp"], mode)
    assert topology is not None
    subnets = ["192.168.100"] + [
        rail.subnet4.rsplit(".", 1)[0] for rail in topology.rails
    ]
    with vm_net.alloc_ip(name, count=3, subnets=subnets) as addresses:
        vm = VMInfo(
            name=name,
            ip=addresses[0],
            nics=["tcp", "tcp"],
            nic_ips=addresses[1:],
            nic_ip6s=[
                rail.ipv6_for(ip)
                for rail, ip in zip(topology.rails, addresses[1:])
            ],
            rail_topology=topology,
        )
        vm.save()
    return vm


@pytest.mark.parametrize("mode", ["shared", "isolated"])
def test_saved_topology_does_not_follow_environment(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Changing defaults does not move a persisted bridge, subnet, or NID."""
    original = _allocate_vm("co1-saved", mode)
    monkeypatch.setattr(vm_state, "EXTRA_SUBNET", "10.99.88")
    monkeypatch.setattr(vm_state, "EXTRA_SUBNET6", "fd99:9999:9999:9999")
    restored = VMInfo.load(original.name)
    assert restored.rail_topology == original.rail_topology
    assert restored.nic_ips == original.nic_ips
    assert restored.nic_ip6s == original.nic_ip6s
    assert restored.extra_nics() == original.extra_nics()
    assert len(restored.extra_nics()[0]) == 4


def test_legacy_state_needs_no_new_fields(state_dir: Path) -> None:
    """Older VM records retain their original NIC and topology defaults."""
    (state_dir / "co1-old.info").write_text("NAME=co1-old\nIP=192.168.100.30\n")
    vm = VMInfo.load("co1-old")
    assert vm.rail_topology is None
    assert vm.nics == []
    vm.save()
    assert "RAIL_TOPOLOGY=" not in vm.info_path.read_text()


@pytest.mark.parametrize("mode", ["shared", "isolated"])
def test_concurrent_allocations_keep_sources_unique(
    state_dir: Path, mode: str
) -> None:
    """Concurrent creates never claim the same data source or management IP."""
    names = [f"co1-parallel-{index}" for index in range(8)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        vms = list(executor.map(_allocate_vm, names, [mode] * len(names)))
    assert len({vm.ip for vm in vms}) == len(names)
    all_ipv4s = [ip for vm in vms for ip in vm.nic_ips]
    all_ipv6s = [ip for vm in vms for ip in vm.nic_ip6s]
    assert len(set(all_ipv4s)) == len(all_ipv4s)
    assert len(set(all_ipv6s)) == len(all_ipv6s)


def test_corrupt_or_oversized_topology_is_not_legacy(state_dir: Path) -> None:
    """Malformed opt-in metadata fails instead of using the legacy bridge."""
    vm = _allocate_vm("co1-corrupt", "shared")
    original = vm.info_path.read_text()
    without = (
        "\n".join(
            line
            for line in original.splitlines()
            if not line.startswith("RAIL_TOPOLOGY=")
        )
        + "\n"
    )
    for raw in (
        "{",
        "null",
        "{}",
        "x" * (MAX_TOPOLOGY_BYTES + 1),
        "[" * 4000 + "]" * 4000,
    ):
        vm.info_path.write_text(without + f"RAIL_TOPOLOGY={raw}\n")
        with pytest.raises(ValueError, match="rail topology"):
            VMInfo.load(vm.name)


def test_missing_ipv6_state_is_refused(state_dir: Path) -> None:
    """Explicit Linux profiles never silently become IPv4-only after corruption."""
    vm = _allocate_vm("co1-no-ip6", "isolated")
    text = (
        "\n".join(
            line
            for line in vm.info_path.read_text().splitlines()
            if not line.startswith("NIC_IP6S=")
        )
        + "\n"
    )
    vm.info_path.write_text(text)
    with pytest.raises(ValueError, match="address counts"):
        VMInfo.load(vm.name)


def test_topology_cannot_rebind_to_a_different_nic_type(
    state_dir: Path,
) -> None:
    """Metadata edits cannot send a TCP fabric through another transport."""
    vm = _allocate_vm("co1-wrong-nic", "shared")
    vm.info_path.write_text(
        vm.info_path.read_text().replace("NICS=tcp|tcp", "NICS=tcp|softroce")
    )
    with pytest.raises(ValueError, match="o2ib"):
        VMInfo.load(vm.name)


def test_address_pool_exhaustion_is_a_checked_failure(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full data subnet fails before yielding an address reservation."""
    occupied = {
        "172.18.100": {f"172.18.100.{index}" for index in range(10, 254)}
    }

    def used_ips(name: str, *, strict: bool = False) -> dict[str, set[str]]:
        """Supply a deterministic full data subnet, without creating 244 VMs."""
        assert strict
        return occupied

    monkeypatch.setattr(vm_net, "_used_ips_by_net", used_ips)
    with pytest.raises(SystemExit):
        with vm_net.alloc_ip(
            "co1-full",
            count=3,
            subnets=["192.168.100", "172.18.100", "172.18.100"],
        ):
            pytest.fail("exhausted pool yielded a reservation")
    assert not list(state_dir.iterdir())


def test_saved_topology_json_contains_only_facts(state_dir: Path) -> None:
    """The profile state records resolved network facts, not runtime handles."""
    vm = _allocate_vm("co1-facts", "shared")
    raw = next(
        line.split("=", 1)[1]
        for line in vm.info_path.read_text().splitlines()
        if line.startswith("RAIL_TOPOLOGY=")
    )
    value = json.loads(raw)
    assert set(value) == {"version", "mode", "rails"}
    assert set(value["rails"][0]) == {
        "bridge",
        "subnet4",
        "prefix4",
        "subnet6",
        "prefix6",
        "lnet_network",
    }


def test_corrupt_profile_cannot_make_its_sources_free(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing IPv6 metadata must not free a running VM's valid IPv4 sources."""
    original = _allocate_vm("co1-existing", "shared")
    text = (
        "\n".join(
            line
            for line in original.info_path.read_text().splitlines()
            if not line.startswith("NIC_IP6S=")
        )
        + "\n"
    )
    original.info_path.write_text(text)
    with pytest.raises(SystemExit):
        _allocate_vm("co1-new", "shared")
    assert "corrupt state" in capsys.readouterr().err
    assert not (state_dir / "co1-new.info").exists()
    # Omitted profiles retain the repository's existing damaged-state behavior.
    with vm_net.alloc_ip("co1-legacy") as ips:
        assert len(ips) == 1


@pytest.mark.parametrize("mode", ["shared", "isolated"])
def test_create_allocates_and_persists_the_complete_profile(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Exercise create's actual allocation and save boundary with disk I/O stubbed."""
    topology = resolve_topology(["tcp", "tcp"], mode)
    assert topology is not None
    args = argparse.Namespace(
        name="co1-created",
        vcpus=2,
        mem=2048,
        mdt_disks=0,
        ost_disks=0,
        owner_id="test:rail-create",
        rail_topology=topology,
    )
    disk_setup = MagicMock(return_value=False)
    monkeypatch.setattr(vm_commands, "_create_disks", disk_setup)
    vm = vm_commands._allocate_and_persist_vm(
        args,
        state_dir / "co1-created.info",
        "tap-co1-created",
        "aa:fc:00:01:02:03",
        SimpleNamespace(arch="x86_64"),
        "base.ext4",
        "vmlinuz",
        "test",
        "rocky9",
        "base.ext4",
        "base",
        ["tcp", "tcp"],
        1024,
        4096,
    )
    saved = VMInfo.load(vm.name)
    assert saved.rail_topology == topology
    assert len(saved.nic_ips) == len(saved.nic_ip6s) == 2
    topology.validate_addresses(saved.nic_ips, saved.nic_ip6s)
    assert [ip.rsplit(".", 1)[0] for ip in saved.nic_ips] == [
        rail.subnet4.rsplit(".", 1)[0] for rail in topology.rails
    ]
    disk_setup.assert_called_once_with(vm, "base.ext4")
