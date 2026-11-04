"""Parallel NIC kernel parameters preserve explicit and older defaults."""

from __future__ import annotations

import pytest

from ltvm_pkg.nic_cmdline import network_names, prefixes
from ltvm_pkg.nic_spec import NicSpec
from ltvm_pkg.vm_state import VMInfo


def _vm(*specs: str) -> VMInfo:
    """Make metadata with two address slots and no external state."""
    return VMInfo(
        name="co1-client",
        ip="203.0.113.50",
        tap="tap-client",
        mac="aa:fc:00:01:02:03",
        nics=[NicSpec.parse(spec) for spec in specs],
        nic_ips=["192.0.2.10", "192.0.2.11"],
        nic_ip6s=["2001:db8:1::10", "2001:db8:1::11"],
    )


@pytest.mark.parametrize("distinct", [False, True])
def test_explicit_prefixes_follow_each_nic(distinct: bool) -> None:
    """IPv4 and IPv6 prefix arrays keep the saved NIC order."""
    second4 = "198.51.100.0/28" if distinct else "192.0.2.0/27"
    second6 = "2001:db8:2::/112" if distinct else "2001:db8:1::/120"
    vm = _vm(
        "tcp:subnet=192.0.2.0/27,subnet6=2001:db8:1::/120",
        f"tcp:subnet={second4},subnet6={second6}",
    )
    assert prefixes(vm) == ("27,28" if distinct else "27,27")
    assert prefixes(vm, ipv6=True) == ("120,112" if distinct else "120,120")


def test_old_records_keep_default_prefixes_and_no_network_parameter() -> None:
    """Address slots older than complete metadata retain /24 and /64."""
    for vm in (_vm(), _vm("tcp"), _vm("tcp", "softroce")):
        assert prefixes(vm) == "24,24"
        assert prefixes(vm, ipv6=True) == "64,64"
        assert network_names(vm) == ""


def test_one_family_override_leaves_other_default() -> None:
    """Each subnet option applies only to its own address family."""
    vm = _vm("tcp:subnet=192.0.2.0/27", "tcp:subnet6=2001:db8:1::/120")
    assert prefixes(vm) == "27,24"
    assert prefixes(vm, ipv6=True) == "64,120"


def test_old_record_without_addresses_emits_no_prefix_values() -> None:
    """A missing old address array must not invent addresses or slots."""
    vm = _vm("tcp", "softroce")
    vm.nic_ips = []
    vm.nic_ip6s = []
    assert prefixes(vm) == ""
    assert prefixes(vm, ipv6=True) == ""


@pytest.mark.parametrize(
    "specs,expected",
    [
        (["tcp:net=tcp0", "tcp:net=tcp0"], " fc_nic_nets=tcp0,tcp0"),
        (["tcp", "tcp:net=tcp7", "softroce"], " fc_nic_nets=,tcp7,"),
        (["tcp:net=tcp7", "softroce:net=o2ib4"], " fc_nic_nets=tcp7,o2ib4"),
    ],
)
def test_explicit_names_keep_empty_automatic_slots(
    specs: list[str], expected: str
) -> None:
    """The guest must see the same index for every explicit name."""
    assert network_names(_vm(*specs)) == expected
