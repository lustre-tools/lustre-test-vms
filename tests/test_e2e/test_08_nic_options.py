"""Smoke tests for LNet NIC names, subnets and source routes.

Two guests check the result of create on each rail.  These tests do not
change LNet tunables or run a load workload.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from ltvm_pkg import vm_state
from ltvm_pkg.vm_state import VMInfo

from .conftest import SOCKETS_DIR, run_ltvm, ssh_run, wait_ssh


def _guest_run(name: str, command: str) -> str:
    """Return checked SSH output; guest commands have a short deadline."""
    rc, out, err = ssh_run(name, command, timeout=30)
    assert rc == 0, f"{name}: {command}: rc={rc}\n{out}\n{err}"
    return out


def _guest_json(name: str, command: str) -> list[dict[str, Any]]:
    """Read ip's JSON output through the existing guest SSH interface."""
    value = json.loads(_guest_run(name, command))
    assert isinstance(value, list), value
    assert all(isinstance(entry, dict) for entry in value), value
    return value


def _address(name: str, index: int, subnet: str) -> str:
    """Require one global address on this NIC in the requested network."""
    network = ipaddress.ip_network(subnet)
    family = "-4" if network.version == 4 else "-6"
    links = _guest_json(name, f"ip -j {family} addr show dev eth{index}")
    assert len(links) == 1, links
    addresses = [
        address
        for address in links[0]["addr_info"]
        if address["scope"] == "global"
    ]
    assert len(addresses) == 1, addresses
    address = addresses[0]
    interface = ipaddress.ip_interface(
        f"{address['local']}/{address['prefixlen']}"
    )
    assert interface.network == network, (interface, network)
    return str(interface.ip)


def _check_route(name: str, index: int, source: str, destination: str) -> None:
    """A lookup from an NI address must use that NIC's policy table."""
    family = "-4" if ipaddress.ip_address(source).version == 4 else "-6"
    routes = _guest_json(
        name,
        f"ip -j {family} route get {shlex.quote(destination)} "
        f"from {shlex.quote(source)}",
    )
    assert len(routes) == 1, routes
    route = routes[0]
    assert route.get("dev") == f"eth{index}", route
    # iproute2 versions return numeric table IDs as strings or integers.
    assert str(route.get("table")) == str(100 + index), route
    # IPv6 route replies report the selected source as prefsrc.
    assert route.get("from", route.get("prefsrc")) == source, route


def _create_pair(vm_name: Callable[[str], str], specs: list[str]) -> list[str]:
    """Create two guests; the existing fixture owns teardown on failure."""
    names = [vm_name("a"), vm_name("b")]
    # A prepared host can select a built non-default kernel for this smoke.
    kernel = os.environ.get("LTVM_E2E_KERNEL", "")
    kernel_args = ["--kernel", kernel] if kernel else []
    nic_args = []
    for index, spec in enumerate(specs):
        # Exercise both spellings at the real CLI boundary.
        nic_args.extend(["-N" if index == 0 else "--nic", spec])
    for name in names:
        proc = run_ltvm(
            "create",
            name,
            "--mem",
            "2048",
            "--vcpus",
            "1",
            "--mdt-disks",
            "0",
            "--ost-disks",
            "0",
            *kernel_args,
            *nic_args,
            timeout=240,
        )
        assert proc.returncode == 0, (
            f"create {name}: rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
        )
        wait_ssh(name)
    return names


def _restart(name: str) -> None:
    """Restart one guest so checks also cover its saved NIC values."""
    for action in ("stop", "start"):
        proc = run_ltvm(action, name, timeout=180)
        assert proc.returncode == 0, (
            f"{action} {name}: rc={proc.returncode}\n"
            f"{proc.stdout}\n{proc.stderr}"
        )
    wait_ssh(name)


def _check_config(
    name: str, net: str, subnets4: list[str], subnets6: list[str]
) -> None:
    """Check saved specs, shared bridge attachment and guest LNet mapping."""
    expected = f'options lnet networks="{net}(eth1,eth2)"'
    assert _guest_run(name, "cat /etc/modprobe.d/lnet.conf").strip() == expected
    # Unit-test isolation redirects this constant. Read the real record
    # written by this test's CLI process, without replacing file access.
    with patch.object(vm_state, "SOCKETS", SOCKETS_DIR):
        vm = VMInfo.load(name)
    assert len(vm.nics) == 2, vm.nics
    for index, spec, tap, _mac in vm.extra_nics():
        assert spec.net == net, spec
        assert spec.subnet == subnets4[index - 1], spec
        assert spec.subnet6 == subnets6[index - 1], spec
        bridge = Path("/sys/class/net") / tap / "master"
        assert bridge.is_symlink(), bridge
        assert bridge.resolve().name == "fcbr0", bridge.resolve()
    # Management remains available while LNet uses the two data NICs.
    management = _guest_json(name, "ip -j -4 addr show dev eth0")
    assert any(item["local"] == vm.ip for item in management[0]["addr_info"]), (
        management
    )


@pytest.mark.parametrize("layout", ["shared", "distinct"])
@pytest.mark.parametrize(
    "transport,family", [("tcp", "ipv4"), ("tcp", "ipv6"), ("softroce", "ipv4")]
)
def test_nic_layout_smoke(
    vm_name: Callable[[str], str], layout: str, transport: str, family: str
) -> None:
    """Check each configured rail and send one LNet ping to each peer NI."""
    net = "tcp0" if transport == "tcp" else "o2ib0"
    subnets4 = ["192.0.2.0/24", "192.0.2.0/24"]
    subnets6 = ["2001:db8:1::/64", "2001:db8:1::/64"]
    if layout == "distinct":
        # Different rails share the bridge and use distinct IP networks.
        subnets4[1] = "198.51.100.0/24"
        subnets6[1] = "2001:db8:2::/64"
    specs = [
        f"{transport}:net={net},subnet={v4},subnet6={v6}"
        for v4, v6 in zip(subnets4, subnets6, strict=True)
    ]
    names = _create_pair(vm_name, specs)
    if (transport, family, layout) == ("tcp", "ipv4", "shared"):
        # One case verifies persistence without repeating the full boot matrix.
        _restart(names[1])
    addresses4: list[list[str]] = []
    addresses6: list[list[str]] = []
    for name in names:
        _check_config(name, net, subnets4, subnets6)
        addresses4.append(
            [
                _address(name, index, subnet)
                for index, subnet in enumerate(subnets4, 1)
            ]
        )
        addresses6.append(
            [
                _address(name, index, subnet)
                for index, subnet in enumerate(subnets6, 1)
            ]
        )

    for addresses in (addresses4, addresses6):
        for guest in range(2):
            for rail in range(2):
                _check_route(
                    names[guest],
                    rail + 1,
                    addresses[guest][rail],
                    addresses[1 - guest][rail],
                )

    module = "ksocklnd" if transport == "tcp" else "ko2iblnd"
    large = " --large" if family == "ipv6" else ""
    for name in names:
        _guest_run(name, f"modprobe lnet && modprobe {module}")
        _guest_run(name, f"lnetctl lnet configure --all{large}")

    # One request to each peer NI is enough to check this VM setup.
    peer_addresses = addresses6[1] if family == "ipv6" else addresses4[1]
    nid_net = "tcp" if transport == "tcp" else "o2ib"
    for address in peer_addresses:
        nid = f"{address}@{nid_net}"
        reply = _guest_run(
            names[0], f"lnetctl ping --timeout 5 {shlex.quote(nid)}"
        )
        print(reply)
        # A zero exit status alone does not prove the requested NI replied.
        result = yaml.safe_load(reply)
        assert isinstance(result, dict) and "ping" in result, result
        assert any(
            interface.get("nid") == nid
            for peer in result["ping"]
            for interface in peer.get("peer_ni", [])
        ), result
