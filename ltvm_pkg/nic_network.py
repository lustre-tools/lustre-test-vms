"""Validated data subnets and bounded address pools for extra NICs."""

from __future__ import annotations

import hashlib
import ipaddress
from collections.abc import Sequence

from .nic_spec import NicSpec

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
MAX_POOL_ADDRESSES = 65534


def data_networks(
    nics: Sequence[NicSpec], default4: str, default6: str
) -> tuple[list[IPNetwork], list[IPNetwork]]:
    """Resolve empty per-NIC subnets to the host's existing defaults."""
    return (
        [ipaddress.ip_network(n.subnet or default4) for n in nics],
        [ipaddress.ip_network(n.subnet6 or default6) for n in nics],
    )


def overlap_problem(networks: Sequence[IPNetwork]) -> str | None:
    """Allow one shared subnet, but reject different overlapping subnets."""
    # Repeated shared/default subnets must not multiply pairwise work.
    unique = list(dict.fromkeys(networks))
    for idx, network in enumerate(unique):
        for other in unique[:idx]:
            # Equal subnets are the requested multi-homed layout.
            if (
                network.version == other.version
                and network != other
                and network.overlaps(other)
            ):
                return f"data subnets {network} and {other} overlap"
    return None


def network_problem(
    nics: Sequence[NicSpec], management: str, default4: str, default6: str
) -> str | None:
    """Validate the whole VM layout before allocation or other side effects."""
    # Only explicit layouts gain a new limit; old NIC lists keep working.
    explicit = any(n.net or n.subnet or n.subnet6 for n in nics)
    if explicit and len(nics) > 8:
        return "explicit NIC layouts support at most 8 extra NICs"
    nets4, nets6 = data_networks(nics, default4, default6)
    mgmt = ipaddress.ip_network(management)
    for network in nets4:
        # Management must stay reachable without a competing data route.
        if network.overlaps(mgmt):
            return f"data subnet {network} overlaps management subnet {mgmt}"
    # Keep default networks reserved so a later ordinary create stays safe.
    return overlap_problem(
        [ipaddress.ip_network(default4), *nets4]
    ) or overlap_problem([ipaddress.ip_network(default6), *nets6])


def select_address(name: str, network: IPNetwork, used: set[str]) -> str:
    """Select one host from the first bounded pool of a configured subnet.

    IPv4 excludes network and broadcast; IPv6 excludes subnet-router
    anycast. The limit applies to large networks without enumerating them.
    """
    excluded = 2 if network.version == 4 else 1
    count = min(network.num_addresses - excluded, MAX_POOL_ADDRESSES)
    if count < 1:
        raise ValueError(f"no host addresses in data subnet {network}")
    start = int(hashlib.md5(name.encode()).hexdigest()[:8], 16) % count
    for offset in range(count):
        address = str(network.network_address + 1 + (start + offset) % count)
        if address not in used:
            return address
    raise ValueError(
        f"no free IP addresses in the {count}-address pool of {network}"
    )
