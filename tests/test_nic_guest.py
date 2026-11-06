"""Checks that a guest applied explicit NIC options at the SSH boundary."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import call, patch

import pytest

from ltvm_pkg import nic_guest
from ltvm_pkg.nic_spec import NicSpec
from ltvm_pkg.vm_state import VMInfo


def _vm(*specs: str) -> VMInfo:
    """Build saved NIC data without touching the host's VM directories."""
    return VMInfo(
        name="co1-client",
        ip="203.0.113.50",
        tap="tap-client",
        mac="aa:fc:00:01:02:03",
        nics=[NicSpec.parse(spec) for spec in specs],
        nic_ips=["192.0.2.10", "192.0.2.11"],
        nic_ip6s=["2001:db8:1::10", "2001:db8:1::11"],
    )


def _reply(body: str, status: int = 0) -> subprocess.CompletedProcess[str]:
    """Keep the fake SSH result's stdout and exit status independent."""
    return subprocess.CompletedProcess([], status, body, "read failed")


def _addresses(
    address4: str, prefix4: int, address6: str, prefix6: int, *, index: int = 1
) -> str:
    """Match the iproute2 address JSON fields the real guest returns."""
    return json.dumps(
        [
            {
                "ifname": f"eth{index}",
                "addr_info": [
                    {"family": "inet", "local": address4, "prefixlen": prefix4},
                    {
                        "family": "inet6",
                        "local": address6,
                        "prefixlen": prefix6,
                    },
                ],
            }
        ]
    )


def test_default_layout_does_not_read_guest_state() -> None:
    """Existing VMs add no SSH request when no NIC option needs checking."""
    for vm in (_vm(), _vm("tcp", "softroce")):
        with patch.object(nic_guest, "run_ssh") as ssh:
            nic_guest.check_layout(vm)
        ssh.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        'options lnet networks="tcp0(eth1,eth2)" lnet_retry_count=2\n',
        'options lnet networks="tcp(eth1[0],eth2[1])[0]" # operator setting\n',
        'options lnet networks="tcp0(eth1[0,1], eth2[2])[0-2]"\n',
        'options lnet networks=" tcp ( eth1[0,1] , eth2[2] ) [0-2] "\n',
        'options lnet networks="tcp0(eth1, eth2), tcp1(eth3)"\n',
        'options other networks="tcp9(eth9)"\n'
        'options lnet "networks=tcp0(eth1,eth2)" lnet_health_sensitivity=100\n',
        'options lnet networks="tcp9(eth9)"\n'
        'options lnet networks="tcp0(eth1,eth2)"\n',
        'options lnet lnet_retry_count=2 \\\n networks="tcp0(eth1,eth2)"\n',
    ],
)
def test_grouped_network_allows_operator_options(body: str) -> None:
    """Only requested network members matter; other options stay valid."""
    vm = _vm("tcp:net=tcp0", "tcp:net=tcp0")
    with patch.object(nic_guest, "run_ssh", return_value=_reply(body)) as ssh:
        nic_guest.check_layout(vm)
    ssh.assert_called_once_with(
        "203.0.113.50",
        "cat /etc/modprobe.d/lnet.conf",
        timeout=30,
        retry_connect=False,
    )


@pytest.mark.parametrize(
    "body",
    [
        'options lnet networks="tcp0(eth1),tcp1(eth2)"\n',
        'options lnet networks="tcp0(eth1,eth3)"\n',
        'options lnet networks="tcp0(eth1),tcp0(eth2)"\n',
        'options lnet networks="tcp0(eth1,eth2),"\n',
        'options lnet networks="tcp0(eth1,eth2) junk"\n',
        'options lnet networks="tcp0(eth1[0,1,eth2)"\n',
        'options lnet networks="tcp0(eth1[[0]],eth2)"\n',
        'options lnet networks="tcp0(eth1,,eth2)"\n',
        'options lnet networks="tcp0(eth1,eth2)\n',
        "options lnet lnet_retry_count=2\n",
        "",
    ],
)
def test_missing_or_ignored_network_names_fail(body: str) -> None:
    """An old image's per-interface defaults must not satisfy grouping."""
    with (
        patch.object(nic_guest, "run_ssh", return_value=_reply(body)),
        pytest.raises(ValueError),
    ):
        nic_guest.check_layout(_vm("tcp:net=tcp0", "tcp:net=tcp0"))


@pytest.mark.parametrize("distinct", [False, True])
def test_requested_dual_stack_addresses_and_prefixes(distinct: bool) -> None:
    """Read each requested NIC once for shared and distinct subnets."""
    subnet4 = "198.51.100.0/28" if distinct else "192.0.2.0/27"
    subnet6 = "2001:db8:2::/112" if distinct else "2001:db8:1::/120"
    vm = _vm(
        "tcp:subnet=192.0.2.0/27,subnet6=2001:db8:1::/120",
        f"tcp:subnet={subnet4},subnet6={subnet6}",
    )
    if distinct:
        # Addresses on a different rail must follow its explicit prefixes.
        vm.nic_ips[1] = "198.51.100.11"
        vm.nic_ip6s[1] = "2001:db8:2::11"
    results = [
        _reply(_addresses(vm.nic_ips[0], 27, vm.nic_ip6s[0], 120)),
        _reply(
            _addresses(
                vm.nic_ips[1],
                28 if distinct else 27,
                vm.nic_ip6s[1],
                112 if distinct else 120,
                index=2,
            )
        ),
    ]
    with patch.object(nic_guest, "run_ssh", side_effect=results) as ssh:
        nic_guest.check_layout(vm)
    assert ssh.call_args_list == [
        call(
            vm.ip,
            f"ip -j address show dev eth{index}",
            timeout=30,
            retry_connect=False,
        )
        for index in (1, 2)
    ]


@pytest.mark.parametrize("family", [4, 6])
def test_old_image_ignoring_explicit_prefix_fails(family: int) -> None:
    """A correct address with the old default prefix is still incorrect."""
    vm = _vm("tcp:subnet=192.0.2.0/27,subnet6=2001:db8:1::/120")
    prefix4, prefix6 = (24, 120) if family == 4 else (27, 64)
    body = _addresses(vm.nic_ips[0], prefix4, vm.nic_ip6s[0], prefix6)
    with (
        patch.object(nic_guest, "run_ssh", return_value=_reply(body)),
        pytest.raises(ValueError, match="did not apply"),
    ):
        nic_guest.check_layout(vm)


@pytest.mark.parametrize(
    "body",
    [
        "not JSON",
        "{}",
        "[]",
        "[{}, {}]",
        "[null]",
        "[[]]",
        '[{"addr_info":null}]',
        '[{"addr_info":[null]}]',
        '[{"addr_info":[{"local":[],"prefixlen":27}]}]',
    ],
)
def test_bad_address_reads_fail_with_checked_error(body: str) -> None:
    """Malformed remote state returns a layout error without a traceback."""
    with (
        patch.object(nic_guest, "run_ssh", return_value=_reply(body)),
        pytest.raises(ValueError),
    ):
        nic_guest.check_layout(_vm("tcp:subnet=192.0.2.0/27"))


@pytest.mark.parametrize("family", [4, 6])
def test_missing_saved_address_fails_with_checked_error(family: int) -> None:
    """An incomplete saved record must not index beyond its address list."""
    vm = _vm("tcp:subnet=192.0.2.0/27,subnet6=2001:db8:1::/120")
    if family == 4:
        # Either address family can be missing from a damaged record.
        vm.nic_ips = []
    else:
        vm.nic_ip6s = []
    body = _addresses("192.0.2.10", 27, "2001:db8:1::10", 120)
    with (
        patch.object(nic_guest, "run_ssh", return_value=_reply(body)),
        pytest.raises(ValueError),
    ):
        nic_guest.check_layout(vm)


@pytest.mark.parametrize("spec", ["tcp:net=tcp0", "tcp:subnet=192.0.2.0/27"])
@pytest.mark.parametrize("status,body", [(255, ""), (0, "x")])
def test_unreadable_guest_state_fails(
    spec: str, status: int, body: str
) -> None:
    """Transport failure and garbage output cannot pass checks."""
    with (
        patch.object(nic_guest, "run_ssh", return_value=_reply(body, status)),
        pytest.raises(ValueError),
    ):
        nic_guest.check_layout(_vm(spec))
