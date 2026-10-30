"""Bounded, persisted network profiles for explicit Lustre rails."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import asdict, dataclass
from typing import Any

MAX_RAILS = 8
TOPOLOGY_VERSION = 1
MAX_TOPOLOGY_BYTES = 8192
MODES = ("shared", "isolated")
_LNET_NETWORK = re.compile(r"(tcp|o2ib)([0-9]{1,5})\Z")


@dataclass(frozen=True)
class Rail:
    """One data NIC's network attachment, without its allocated addresses."""

    bridge: str
    subnet4: str
    prefix4: int
    subnet6: str
    prefix6: int
    lnet_network: str

    def ipv6_for(self, ipv4: str) -> str:
        """Embed an allocated IPv4 address in this rail's /64 prefix."""
        address = ipaddress.IPv4Address(ipv4)
        network4 = ipaddress.IPv4Network((self.subnet4, self.prefix4))
        if address not in network4:
            # A mismatched allocation must not produce a plausible wrong NID.
            raise ValueError(f"rail address {ipv4} is outside {network4}")
        prefix = ipaddress.IPv6Network((self.subnet6, self.prefix6))
        # Preserve long IPv6 NIDs used to exercise Lustre's large-NID path.
        hextets = prefix.network_address.exploded.split(":")[:4]
        return ":".join(hextets + [f"f{octet:03d}" for octet in address.packed])

    def to_dict(self) -> dict[str, Any]:
        """Return only the fixed schema fields for JSON state and diagnostics."""
        return asdict(self)


def profile_rail(mode: str, index: int, lnet_network: str = "tcp0") -> Rail:
    """Resolve a profile NIC, indexed from one, to its fixed data fabric."""
    if mode not in MODES or not 1 <= index <= MAX_RAILS:
        # Reject invalid indices before deriving bridge names or addresses.
        raise ValueError("rail profile needs shared/isolated and index 1..8")
    fabric = 0 if mode == "shared" else index
    return Rail(
        bridge="fcbr-data" if fabric == 0 else f"fcbr-rail{fabric}",
        subnet4=f"172.18.{100 + fabric}.0",
        prefix4=24,
        subnet6=f"fd17:2016:2000:{0x1000 + fabric:x}::",
        prefix6=64,
        lnet_network=lnet_network,
    )


def configured_rails() -> tuple[Rail, ...]:
    """List the nine fixed host fabrics that installation must prepare."""
    return (profile_rail("shared", 1),) + tuple(
        profile_rail("isolated", index) for index in range(1, MAX_RAILS + 1)
    )


def _network_type(nic: str) -> str:
    """Map an allowed virtual NIC transport to its LNet transport."""
    if nic == "tcp":
        return "tcp"
    if nic == "softroce":
        return "o2ib"
    # Physical passthrough does not belong to a managed virtual fabric.
    raise ValueError("network profiles support only tcp and softroce NICs")


def _validate_network(network: str, transport: str) -> None:
    """Check an exact LNet name, its transport, and its numeric limit."""
    match = _LNET_NETWORK.fullmatch(network)
    if match is None or match.group(1) != transport:
        # Net IDs cannot inject commas, parentheses, or command-line tokens.
        raise ValueError(
            f"LNet network {network!r} must be {transport}<0..65535>"
        )
    if int(match.group(2)) > 65535:
        raise ValueError(f"LNet network {network!r} exceeds 65535")
    if str(int(match.group(2))) != match.group(2):
        # One spelling per ID makes grouping and equality checks exact.
        raise ValueError(f"LNet network {network!r} has a leading zero")


@dataclass(frozen=True)
class RailTopology:
    """An opt-in profile whose resolved facts survive later environment changes."""

    mode: str
    rails: tuple[Rail, ...]

    def validate_for_nics(self, nics: list[str]) -> None:
        """Reject corrupt state and mismatches before any host mutation."""
        if self.mode not in MODES or not 2 <= len(self.rails) <= MAX_RAILS:
            raise ValueError("network profiles require 2..8 extra NICs")
        if len(nics) != len(self.rails):
            raise ValueError("rail topology and extra NIC counts differ")
        seen: set[str] = set()
        for index, (rail, nic) in enumerate(zip(self.rails, nics), start=1):
            _validate_network(rail.lnet_network, _network_type(nic))
            expected = profile_rail(self.mode, index, rail.lnet_network)
            if rail != expected:
                # Only the fixed profiles are supported; reject modified state.
                raise ValueError(f"invalid fabric for rail {index}")
            if self.mode == "isolated" and rail.lnet_network in seen:
                raise ValueError(
                    "isolated rails need distinct LNet network IDs"
                )
            seen.add(rail.lnet_network)
        if self.mode == "shared" and len(seen) != 1:
            # This profile tests several NIs of one LNet net on one subnet.
            raise ValueError(
                "shared rails must use one transport and LNet network ID"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize a small, versioned topology without runtime resources."""
        return {
            "version": TOPOLOGY_VERSION,
            "mode": self.mode,
            "rails": [rail.to_dict() for rail in self.rails],
        }

    def validate_addresses(self, ipv4s: list[str], ipv6s: list[str]) -> None:
        """Require one valid paired address per rail in both supported families."""
        if len(ipv4s) != len(self.rails) or len(ipv6s) != len(self.rails):
            raise ValueError("rail address counts must match the topology")
        if len(set(ipv4s)) != len(ipv4s) or len(set(ipv6s)) != len(ipv6s):
            # Two local NIs may not claim the same source address.
            raise ValueError("rail source addresses must be unique")
        for rail, ipv4, ipv6 in zip(self.rails, ipv4s, ipv6s):
            network = ipaddress.IPv4Network((rail.subnet4, rail.prefix4))
            address = ipaddress.IPv4Address(ipv4)
            if address in (network.network_address, network.broadcast_address):
                raise ValueError("rail address must be a usable host address")
            expected = ipaddress.IPv6Address(rail.ipv6_for(ipv4))
            if ipaddress.IPv6Address(ipv6) != expected:
                raise ValueError(
                    "rail IPv6 address does not match its fabric and IPv4 address"
                )

    @classmethod
    def from_dict(cls, value: Any) -> RailTopology:
        """Read only the supported bounded schema; never coerce malformed state."""
        if not isinstance(value, dict) or set(value) != {
            "version",
            "mode",
            "rails",
        }:
            raise ValueError("invalid rail topology object")
        if (
            type(value["version"]) is not int
            or value["version"] != TOPOLOGY_VERSION
        ):
            raise ValueError("unsupported rail topology version")
        if value["mode"] not in MODES:
            raise ValueError("invalid rail topology mode")
        entries = value["rails"]
        if not isinstance(entries, list) or not 2 <= len(entries) <= MAX_RAILS:
            raise ValueError("rail topology needs 2..8 rail entries")
        rails = tuple(_read_rail(entry) for entry in entries)
        topology = cls(value["mode"], rails)
        # Saved transport names supply enough information to validate the schema.
        nics = [
            "tcp" if rail.lnet_network.startswith("tcp") else "softroce"
            for rail in rails
        ]
        topology.validate_for_nics(nics)
        return topology


def _read_rail(value: Any) -> Rail:
    """Validate scalar types before constructing one persisted rail."""
    names = {
        "bridge",
        "subnet4",
        "prefix4",
        "subnet6",
        "prefix6",
        "lnet_network",
    }
    if not isinstance(value, dict) or set(value) != names:
        raise ValueError("invalid rail fields")
    for key in names:
        expected = int if key in ("prefix4", "prefix6") else str
        if type(value[key]) is not expected:
            # Booleans must not pass as prefix lengths through Python's int subtype.
            raise ValueError(f"invalid rail field {key}")
    return Rail(**value)


def resolve_topology(
    nics: list[str], mode: str | None, lnet_networks: list[str] | None = None
) -> RailTopology | None:
    """Validate requested options and return fixed rails, or the legacy mode."""
    if mode is None:
        if lnet_networks:
            # Explicit grouping requires an explicit profile, leaving old VMs intact.
            raise ValueError("--lnet-network requires --network-topology")
        return None
    if mode not in MODES:
        raise ValueError("network topology must be shared or isolated")
    if not 2 <= len(nics) <= MAX_RAILS:
        raise ValueError("network profiles require 2..8 extra NICs")
    if lnet_networks is not None and len(lnet_networks) != len(nics):
        raise ValueError("give one --lnet-network per extra NIC")
    rails: list[Rail] = []
    for index, nic in enumerate(nics, start=1):
        transport = _network_type(nic)
        net_index = 0 if mode == "shared" else index - 1
        network = (
            lnet_networks[index - 1]
            if lnet_networks is not None
            else f"{transport}{net_index}"
        )
        rails.append(profile_rail(mode, index, network))
    topology = RailTopology(mode, tuple(rails))
    topology.validate_for_nics(nics)
    return topology
