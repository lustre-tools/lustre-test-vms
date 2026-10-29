"""Validate explicit guest rail configuration before cluster operations."""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence

from .vm_state import VMInfo

PROFILE_FAILOVER_ERROR = (
    "server failover is not supported with network profiles"
)
MAX_PROBE_BYTES = 65536
MAX_PROBE_LINES = 256


def require_no_profile_failover(
    vms: Sequence[VMInfo], has_failover: bool
) -> None:
    """Reject server HA before framework hostname resolution can invent wrong NIDs."""
    if has_failover and any(vm.rail_topology is not None for vm in vms):
        # The framework has no supported per-host map for several LNet networks.
        raise ValueError(PROFILE_FAILOVER_ERROR)


def _expected_networks(vm: VMInfo) -> str:
    """Group the saved rail interfaces exactly as the guest emitter does."""
    topology = vm.rail_topology
    if topology is None:
        raise ValueError("guest rail validation needs a network profile")
    topology.validate_for_nics(vm.nics)
    topology.validate_addresses(vm.nic_ips, vm.nic_ip6s)
    groups: dict[str, list[str]] = {}
    for index, rail in enumerate(topology.rails, start=1):
        groups.setdefault(rail.lnet_network, []).append(f"eth{index}")
    return ",".join(
        f"{network}({','.join(ifaces)})" for network, ifaces in groups.items()
    )


def _probe_addresses(
    output: str,
) -> dict[tuple[str, str], set[tuple[str, int]]]:
    """Read bounded iproute2 probe output, retaining only global IP addresses."""
    lines = output.splitlines()
    if len(lines) > MAX_PROBE_LINES:
        raise ValueError("guest address probe has too many lines")
    addresses: dict[tuple[str, str], set[tuple[str, int]]] = {}
    for line in lines:
        fields = line.split()
        if len(fields) < 4 or fields[2] not in ("inet", "inet6"):
            continue
        if "global" not in fields:
            # IPv6 link-local addresses support ND and are not LNet candidates.
            continue
        try:
            address = ipaddress.ip_interface(fields[3])
        except ValueError as error:
            raise ValueError(
                f"invalid guest address in probe: {fields[3]!r}"
            ) from error
        family = "inet6" if address.version == 6 else "inet"
        if fields[2] != family:
            raise ValueError(
                "guest address probe has an inconsistent IP family"
            )
        interface = fields[1].split("@", 1)[0]
        addresses.setdefault((interface, family), set()).add(
            (str(address.ip), address.network.prefixlen)
        )
    return addresses


def validate_profile_probe(vm: VMInfo, output: str, ip_family: str) -> None:
    """Require the saved profile instead of falling back to management networking."""
    if vm.rail_topology is None:
        return
    if ip_family not in ("ipv4", "ipv6"):
        raise ValueError(f"unsupported cluster IP family: {ip_family!r}")
    if len(output.encode()) > MAX_PROBE_BYTES:
        raise ValueError("guest rail probe is too large")
    networks, separator, address_output = output.partition("--\n")
    expected = _expected_networks(vm)
    if not separator or networks.strip() != expected:
        # A missing config must never turn a profile into tcp on eth0.
        raise ValueError(f"{vm.name}: guest LNet config must match {expected}")
    addresses = _probe_addresses(address_output)
    for index, rail in enumerate(vm.rail_topology.rails):
        interface = f"eth{index + 1}"
        expected4 = (
            str(ipaddress.IPv4Address(vm.nic_ips[index])),
            rail.prefix4,
        )
        expected6 = (
            str(ipaddress.IPv6Address(vm.nic_ip6s[index])),
            rail.prefix6,
        )
        for family, wanted in (("inet", expected4), ("inet6", expected6)):
            if addresses.get((interface, family)) != {wanted}:
                # Extra global addresses can make LNet select an unintended NID.
                raise ValueError(
                    f"{vm.name}: {interface} must have only the saved {family} "
                    f"address {wanted[0]}/{wanted[1]}"
                )
            for (other, other_family), values in addresses.items():
                if (
                    other != interface
                    and other_family == family
                    and any(address == wanted[0] for address, _prefix in values)
                ):
                    raise ValueError(
                        f"{vm.name}: saved source {wanted[0]} is also configured on {other}"
                    )
