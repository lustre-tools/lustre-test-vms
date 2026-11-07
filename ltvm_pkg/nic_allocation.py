"""Explicit data address allocation under the existing VM lock."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

from .nic_network import (
    data_networks,
    network_problem,
    overlap_problem,
    select_address,
)
from .nic_spec import NicSpec
from .qemu_run import die
from .vm_state import EXTRA_SUBNET6, VMInfo, VMNotFound, nic_ip6, subnet_for_nic


def _data_defaults() -> tuple[str, str, str]:
    """Resolve the current host subnet settings at the allocation boundary."""
    return (
        f"{subnet_for_nic(0)}.0/24",
        f"{subnet_for_nic(1)}.0/24",
        f"{EXTRA_SUBNET6}::/64",
    )


def nic_network_problem(nics: list[NicSpec]) -> str | None:
    """Report a layout conflict before create or cluster create writes state."""
    if not nics:
        return None
    management, default4, default6 = _data_defaults()
    return network_problem(nics, management, default4, default6)


def _stored_address_state(
    name: str,
) -> tuple[set[str], set[str], list[str], list[str]]:
    """Read allocations while locked; unreadable peer state fails safely."""
    import ipaddress

    used4: set[str] = set()
    used6: set[str] = set()
    subnets4: list[str] = []
    subnets6: list[str] = []
    for peer in VMInfo.all_names():
        if peer == name:
            continue
        try:
            vm = VMInfo.load(peer)
        except VMNotFound:
            # A concurrent destroy can remove an entry after all_names().
            continue
        used4.update(ip for ip in [vm.ip, *vm.nic_ips] if ip)
        # Stored full-width and compressed IPv6 spellings name the same host.
        used6.update(str(ipaddress.IPv6Address(ip)) for ip in vm.nic_ip6s if ip)
        subnets4.extend(n.subnet for n in vm.nics if n.subnet)
        subnets6.extend(n.subnet6 for n in vm.nics if n.subnet6)
    return used4, used6, subnets4, subnets6


def _default_address(name: str, prefix: str, used: set[str]) -> str:
    """Keep the existing deterministic .10 through .253 default pool."""
    base = int(hashlib.md5(name.encode()).hexdigest()[:4], 16) % 244
    for delta in range(244):
        address = f"{prefix}.{(base + delta) % 244 + 10}"
        if address not in used:
            return address
    raise ValueError(f"No free IP addresses available in {prefix}.0/24")


@contextmanager
def alloc_vm_addresses(
    name: str, nics: list[NicSpec], explicit_ip: str | None = None
) -> Iterator[tuple[list[str], list[str]]]:
    """Allocate both IP families under the existing lock through .info save.

    Default pools and IPv6 derivation keep their old spelling. Explicit
    pools scan at most MAX_POOL_ADDRESSES hosts, even for a large subnet.
    """
    import ipaddress

    # Import locally: vm_net exports this API and owns the existing lock.
    from .vm_net import _ip_alloc_lock, _validate_explicit_ip

    problem = nic_network_problem(nics)
    if problem:
        raise ValueError(problem)
    _management, default4, default6 = _data_defaults()
    nets4, nets6 = data_networks(nics, default4, default6)
    with _ip_alloc_lock():
        used4, used6, stored4, stored6 = _stored_address_state(name)
        # Reserving defaults also protects future implicit creates.
        overlap = overlap_problem(
            [ipaddress.ip_network(s) for s in [default4, *stored4]] + nets4
        ) or overlap_problem(
            [ipaddress.ip_network(s) for s in [default6, *stored6]] + nets6
        )
        if overlap:
            raise ValueError(overlap)
        if explicit_ip:
            _validate_explicit_ip(explicit_ip)
            if explicit_ip in used4:
                die(f"IP {explicit_ip} already used by another VM")
            primary = explicit_ip
        else:
            primary = _default_address(name, subnet_for_nic(0), used4)
        ips, ip6s = [primary], []
        used4.add(primary)
        for idx, (nic, net4, net6) in enumerate(zip(nics, nets4, nets6)):
            # Preserve the existing pool for an implicit data subnet.
            address = (
                select_address(name, net4, used4)
                if nic.subnet
                else _default_address(name, subnet_for_nic(idx + 1), used4)
            )
            used4.add(address)
            address6 = (
                select_address(name, net6, used6)
                if nic.subnet6
                else nic_ip6(address)
            )
            canonical6 = str(ipaddress.IPv6Address(address6))
            if canonical6 in used6:
                raise ValueError(f"IPv6 address {address6} is already used")
            used6.add(canonical6)
            ips.append(address)
            ip6s.append(address6)
        yield ips, ip6s
