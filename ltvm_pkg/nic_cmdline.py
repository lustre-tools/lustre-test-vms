"""Kernel command-line values for explicit extra-NIC networks."""

from __future__ import annotations

from ipaddress import ip_network

from .vm_state import PREFIX_LEN, PREFIX_LEN6, VMInfo


def prefixes(vm: VMInfo, *, ipv6: bool = False) -> str:
    """Keep each saved address aligned with its NIC's requested prefix."""
    addresses = vm.nic_ip6s if ipv6 else vm.nic_ips
    default = PREFIX_LEN6 if ipv6 else PREFIX_LEN
    result = []
    for index in range(len(addresses)):
        # Older records may have incomplete NIC metadata; keep their default.
        subnet = ""
        if index < len(vm.nics):
            nic = vm.nics[index]
            subnet = nic.subnet6 if ipv6 else nic.subnet
        result.append(str(ip_network(subnet).prefixlen if subnet else default))
    return ",".join(result)


def network_names(vm: VMInfo) -> str:
    """Return the parallel name parameter only for explicitly named NICs."""
    if not any(nic.net for nic in vm.nics):
        # Omit the new parameter so existing VM command lines stay unchanged.
        return ""
    return " fc_nic_nets=" + ",".join(nic.net for nic in vm.nics)
