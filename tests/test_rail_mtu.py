"""Host MTU behavior at the network API and direct sysfs boundaries."""

from __future__ import annotations

import errno
from typing import Any

import pytest

from ltvm_pkg import rail_host
from ltvm_pkg.rail_topology import resolve_topology
from ltvm_pkg.vm_state import VMInfo
from tests.test_rail_host import FakeIPRoute, _profile_vm
from tests.test_rail_host import network_api as network_api

_NETWORK_CONTEXT = rail_host._iproute


def _network(
    api: FakeIPRoute,
    mode: str = "isolated",
    nics: int = 2,
    transport: str = "tcp",
) -> VMInfo:
    """Prepare supported data fabrics while leaving management at its own MTU."""
    vm = _profile_vm(mode)
    vm.nics = [transport] * nics
    vm.rail_topology = resolve_topology(vm.nics, mode)
    assert vm.rail_topology is not None
    vm.nic_ips = [
        rail.subnet4.rsplit(".", 1)[0] + f".{30 + index}"
        for index, rail in enumerate(vm.rail_topology.rails)
    ]
    vm.nic_ip6s = [
        rail.ipv6_for(ip)
        for rail, ip in zip(vm.rail_topology.rails, vm.nic_ips)
    ]
    api.add_existing("fcbr0", alias="management", mtu=1500)
    for name in dict.fromkeys(rail.bridge for rail in vm.rail_topology.rails):
        api.add_existing(name, mtu=4200)
    return vm


def test_install_updates_existing_default_mtu_without_adding_addresses(
    network_api: FakeIPRoute,
) -> None:
    """A repeated install upgrades existing owned 1500-byte fabrics too."""
    for name in rail_host.bridge_names():
        network_api.add_existing(name)
    rail_host.ensure_bridges()
    assert network_api.calls == [
        ("set", {"index": index, "mtu": 4200, "state": "up"})
        for index in range(10, 19)
    ]
    assert all(link["IFLA_MTU"] == 4200 for link in network_api.links.values())
    assert network_api.addresses == {}


@pytest.mark.parametrize(
    "contents",
    [
        None,
        "",
        "bad\n",
        "-1\n",
        "0\n",
        "4199\n",
        "4294967296\n",
        "４２００\n",
        "00000000000004200bad",
    ],
)
def test_start_rejects_unreadable_or_small_sysfs_mtu_without_writes(
    network_api: FakeIPRoute, contents: str | None
) -> None:
    """Missing or corrupt state must not become a healthy default MTU."""
    vm = _network(network_api)
    assert vm.rail_topology is not None
    path = rail_host.SYS_NET / "fcbr-rail2/mtu"
    # Missing state and present corrupt state must both fail closed.
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents)
    with pytest.raises(RuntimeError, match="MTU|unavailable"):
        rail_host.require_bridges(vm.rail_topology)
    assert network_api.calls == []


@pytest.mark.parametrize("mtu", [None, True, "4200", 0, -1, 4199, 0x100000000])
def test_start_rechecks_netlink_mtu_after_sysfs_passes(
    network_api: FakeIPRoute, mtu: object
) -> None:
    """A later kernel read must reject a missing or changed MTU independently."""
    vm = _network(network_api)
    assert vm.rail_topology is not None
    network_api.links["fcbr-rail2"]["IFLA_MTU"] = mtu
    with pytest.raises(RuntimeError, match="MTU"):
        rail_host.require_bridges(vm.rail_topology)
    assert network_api.calls == []


@pytest.mark.parametrize("mode", ["shared", "isolated"])
def test_taps_inherit_each_bridge_mtu_before_attachment(
    network_api: FakeIPRoute, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Both profiles support full SoftRoCE frames without changing management."""
    vm = _network(network_api, mode)
    assert vm.rail_topology is not None
    bridge = vm.rail_topology.rails[-1].bridge
    network_api.link("set", index=network_api.links[bridge]["index"], mtu=9000)
    network_api.calls.clear()
    real_link = network_api.link

    def inspect_attachment(action: str, **values: Any) -> None:
        """Model the port MTU check at the first master attachment request."""
        if action == "set" and "master" in values:
            port = network_api.get_links(values["index"])[0]
            master = network_api.get_links(values["master"])[0]
            assert port["IFLA_MTU"] == master["IFLA_MTU"]
        real_link(action, **values)

    monkeypatch.setattr(network_api, "link", inspect_attachment)
    rail_host.require_bridges(vm.rail_topology)
    rail_host.prepare_taps(vm)
    taps = [vm.tap, *(entry[2] for entry in vm.extra_nics())]
    assert [network_api.links[tap]["IFLA_MTU"] for tap in taps] == (
        [1500, 9000, 9000] if mode == "shared" else [1500, 4200, 9000]
    )
    assert network_api.links["fcbr0"]["IFLA_MTU"] == 1500


def test_invalid_last_fabric_mtu_precedes_all_tap_mutations(
    network_api: FakeIPRoute,
) -> None:
    """An incomplete data path cannot leave even the management TAP behind."""
    vm = _network(network_api)
    network_api.links["fcbr-rail2"]["IFLA_MTU"] = 1500
    before = set(network_api.links)
    with pytest.raises(RuntimeError, match="1500.*4200"):
        rail_host.prepare_taps(vm)
    assert set(network_api.links) == before
    assert network_api.calls == []


@pytest.mark.parametrize("mode", ["shared", "isolated"])
@pytest.mark.parametrize("transport", ["tcp", "softroce"])
def test_all_eight_rail_taps_keep_full_frame_mtu(
    network_api: FakeIPRoute, mode: str, transport: str
) -> None:
    """TCP and SoftRoCE take the same bounded nine-port host setup path."""
    vm = _network(network_api, mode, nics=8, transport=transport)
    assert vm.rail_topology is not None
    rail_host.require_bridges(vm.rail_topology)
    rail_host.prepare_taps(vm)
    taps = [entry[2] for entry in vm.extra_nics()]
    assert len(taps) == 8
    assert all(network_api.links[tap]["IFLA_MTU"] == 4200 for tap in taps)
    assert len(network_api.calls) == 27
    assert [action for action, _values in network_api.calls] == [
        "add",
        "set",
        "set",
    ] * 9


@pytest.mark.parametrize("operation", ["bridge", "tap"])
def test_acknowledged_but_unchanged_mtu_is_checked_and_rolled_back(
    network_api: FakeIPRoute, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """An API acknowledgement alone does not prove full-size frame support."""
    vm = _network(network_api) if operation == "tap" else None
    before = set(network_api.links)
    real_link = network_api.link

    def ignore_mtu(action: str, **values: Any) -> None:
        """Simulate an acknowledged request that failed to change kernel state."""
        real_link(action, **values)
        if action == "set" and "mtu" in values:
            network_api.get_links(values["index"])[0]["IFLA_MTU"] = 1500

    monkeypatch.setattr(network_api, "link", ignore_mtu)
    with pytest.raises(RuntimeError, match="1500.*4200"):
        # Exercise each public lifecycle operation with the same ignored write.
        if vm is None:
            rail_host.ensure_bridges()
        else:
            rail_host.prepare_taps(vm)
    assert set(network_api.links) == before
    assert any(action == "del" for action, _values in network_api.calls)


def test_kernel_mtu_failure_rolls_back_all_new_taps_with_checked_error(
    network_api: FakeIPRoute, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real NetlinkError must not escape or leave partial guest interfaces."""
    pyroute2 = pytest.importorskip(
        "pyroute2", reason="Linux network API unavailable"
    )
    from pyroute2.netlink.exceptions import NetlinkError

    vm = _network(network_api)
    before = set(network_api.links)
    last_index = 10 + len(before) + 2
    real_link = network_api.link

    def fail_last_mtu(action: str, **values: Any) -> None:
        """Fail after two configured TAPs and the third newly created device."""
        if (
            action == "set"
            and "mtu" in values
            and values["index"] == last_index
        ):
            raise NetlinkError(errno.EINVAL)
        real_link(action, **values)

    monkeypatch.setattr(network_api, "link", fail_last_mtu)
    monkeypatch.setattr(pyroute2, "IPRoute", lambda: network_api)
    monkeypatch.setattr(rail_host, "_iproute", _NETWORK_CONTEXT)
    with pytest.raises(RuntimeError, match="rail network API failed"):
        rail_host.prepare_taps(vm)
    assert set(network_api.links) == before
    assert [
        values["index"]
        for action, values in network_api.calls
        if action == "del"
    ] == [
        last_index,
        last_index - 1,
        last_index - 2,
    ]
