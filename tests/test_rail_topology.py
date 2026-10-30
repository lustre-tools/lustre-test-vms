"""Profile limits, transport rules, and persisted schema validation."""

from __future__ import annotations

import ipaddress

import pytest

from ltvm_pkg.rail_topology import (
    MAX_RAILS,
    RailTopology,
    configured_rails,
    profile_rail,
    resolve_topology,
)


@pytest.mark.parametrize("mode", ["shared", "isolated"])
@pytest.mark.parametrize("kind", ["tcp", "softroce"])
def test_two_rail_profiles(mode: str, kind: str) -> None:
    """Both supported transports produce the selected network separation."""
    topology = resolve_topology([kind, kind], mode)
    assert topology is not None
    first, second = topology.rails
    transport = "tcp" if kind == "tcp" else "o2ib"
    assert first.lnet_network == f"{transport}0"
    if mode == "shared":
        # Shared rails are two NIs on one bridge, subnet, and LNet network.
        assert first == second
        assert first.bridge == "fcbr-data"
    else:
        # Isolated rails have distinct fabrics in both address families.
        assert first.bridge == "fcbr-rail1"
        assert second.bridge == "fcbr-rail2"
        assert first.subnet4 != second.subnet4
        assert first.subnet6 != second.subnet6
        assert second.lnet_network == f"{transport}1"


def test_lnet_ids_are_independent_of_the_fabric() -> None:
    """Explicit IDs can change without changing any bridge or IP prefix."""
    topology = resolve_topology(["tcp", "tcp"], "isolated", ["tcp7", "tcp42"])
    assert topology is not None
    assert topology.rails[0] == profile_rail("isolated", 1, "tcp7")
    assert topology.rails[1] == profile_rail("isolated", 2, "tcp42")
    shared = resolve_topology(["tcp", "tcp"], "shared", ["tcp42", "tcp42"])
    assert shared is not None
    assert shared.rails[0] == profile_rail("shared", 1, "tcp42")


def test_profile_limits_and_host_fabrics() -> None:
    """The maximum profile and installation fabric list stay small and fixed."""
    topology = resolve_topology(["tcp"] * MAX_RAILS, "isolated")
    assert topology is not None
    assert len(topology.rails) == MAX_RAILS
    fabrics = configured_rails()
    assert len(fabrics) == MAX_RAILS + 1
    assert len({rail.bridge for rail in fabrics}) == len(fabrics)
    assert len({rail.subnet4 for rail in fabrics}) == len(fabrics)
    assert len({rail.subnet6 for rail in fabrics}) == len(fabrics)


@pytest.mark.parametrize(
    "nics,mode,networks",
    [
        (["tcp"], "shared", None),
        (["tcp"] * 9, "isolated", None),
        (["tcp", "passthrough:0000:01:00.0"], "isolated", None),
        (["tcp", "tcp"], "other", None),
        (["tcp", "tcp"], None, ["tcp0", "tcp0"]),
        (["tcp", "tcp"], "shared", ["tcp0"]),
        (["tcp", "tcp"], "shared", ["tcp0", "tcp1"]),
        (["tcp", "softroce"], "shared", None),
        (["tcp", "tcp"], "isolated", ["tcp0", "tcp0"]),
        (["tcp", "tcp"], "isolated", ["tcp0", "o2ib1"]),
        (["tcp", "tcp"], "isolated", ["tcp0", "tcp65536"]),
        (["tcp", "tcp"], "isolated", ["tcp0", "tcp01"]),
        (["tcp", "tcp"], "isolated", ["tcp0", "tcp1(eth2)"]),
        (["tcp", "tcp"], "isolated", ["tcp0", "tcp1\nother"]),
    ],
)
def test_invalid_profile_rejected(
    nics: list[str], mode: str | None, networks: list[str] | None
) -> None:
    """Invalid bounds, grouping, or transport fail without producing a topology."""
    with pytest.raises(ValueError):
        resolve_topology(nics, mode, networks)


def test_legacy_request_has_no_topology() -> None:
    """An omitted profile preserves legacy virtual and physical NIC behavior."""
    assert resolve_topology([], None) is None
    assert resolve_topology(["passthrough:0000:01:00.0"], None) is None


def test_schema_round_trip_and_rejects_modified_fabrics() -> None:
    """Persist exact facts and reject a hand-edited bridge attachment."""
    topology = resolve_topology(["softroce", "softroce"], "shared")
    assert topology is not None
    value = topology.to_dict()
    assert RailTopology.from_dict(value) == topology
    value["rails"][0]["bridge"] = "eth0"
    with pytest.raises(ValueError, match="invalid fabric"):
        RailTopology.from_dict(value)


@pytest.mark.parametrize("version", [0, 2, True, "1"])
def test_schema_version_is_exact(version: object) -> None:
    """Unknown versions and integer coercions do not alter network meaning."""
    topology = resolve_topology(["tcp", "tcp"], "shared")
    assert topology is not None
    value = topology.to_dict()
    value["version"] = version
    with pytest.raises(ValueError, match="version"):
        RailTopology.from_dict(value)


def test_ipv6_addresses_use_their_own_rail_prefix() -> None:
    """Each IPv6 address stays full length and belongs to its own rail's /64."""
    topology = resolve_topology(["tcp", "tcp"], "isolated")
    assert topology is not None
    ipv4s = ["172.18.101.50", "172.18.102.50"]
    ipv6s = [rail.ipv6_for(ip) for rail, ip in zip(topology.rails, ipv4s)]
    topology.validate_addresses(ipv4s, ipv6s)
    for rail, address in zip(topology.rails, ipv6s):
        assert len(address) == 39
        assert ipaddress.IPv6Address(address) in ipaddress.IPv6Network(
            (rail.subnet6, rail.prefix6)
        )
    with pytest.raises(ValueError, match="outside"):
        topology.rails[0].ipv6_for(ipv4s[1])
    with pytest.raises(ValueError, match="counts"):
        topology.validate_addresses(ipv4s, ipv6s[:1])
    with pytest.raises(ValueError, match="does not match"):
        topology.validate_addresses(ipv4s, list(reversed(ipv6s)))


def test_shared_source_addresses_must_be_distinct() -> None:
    """One subnet never permits two NIs with the same source address."""
    topology = resolve_topology(["tcp", "tcp"], "shared")
    assert topology is not None
    address = topology.rails[0].ipv6_for("172.18.100.50")
    with pytest.raises(ValueError, match="unique"):
        topology.validate_addresses(["172.18.100.50"] * 2, [address] * 2)
