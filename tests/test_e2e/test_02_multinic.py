"""End-to-end: --nic combinations produce the right lnet.conf + IPs.

Encodes the table from commit 2b3dfda (`lnet: drop mgmt from LNet
when --nic is specified`) and commit 0a708a3 (multi-IP allocation
for each --nic):

    --nic args              expected_lnet_body              expected_ipv4_ifaces
    (none)                  tcp0(eth0)                      1   (eth0)
    --nic tcp               tcp0(eth1)                      2   (eth0+eth1)
    --nic tcp --nic tcp     tcp0(eth1),tcp1(eth2)           3
    --nic softroce          o2ib0(eth1)                     2
    --nic tcp --nic softroce
                            tcp0(eth1),o2ib0(eth2)          3
    --nic softroce --nic softroce
                            o2ib0(eth1),o2ib1(eth2)         3

Softroce LNet entries reference the BACKING NETDEV (ethI), not the
rxe link.  ko2iblnd takes a netdev name and finds the rxe ibdev via
rdma_cm; rxe links aren't netdevs and can't appear in lnet.conf.
See targets/common/setup-lnet-config.sh comment.

eth0 always gets a mgmt IP regardless of LNet membership, so the
IPv4-address count is `1 + len(--nic)` for every case.
"""

from __future__ import annotations

import pytest

from .conftest import run_ltvm, ssh_run, wait_ssh

# Each row: test-id, list of --nic values (may be empty), expected
# lnet.conf body (the string between quotes in `networks="..."`),
# expected count of interfaces with an IPv4 address.
_CASES: list[tuple[str, list[str], str, int]] = [
    ("default", [], "tcp0(eth0)", 1),
    ("one-tcp", ["tcp"], "tcp0(eth1)", 2),
    ("two-tcp", ["tcp", "tcp"], "tcp0(eth1),tcp1(eth2)", 3),
    ("one-softroce", ["softroce"], "o2ib0(eth1)", 2),
    ("tcp-plus-softroce", ["tcp", "softroce"], "tcp0(eth1),o2ib0(eth2)", 3),
    ("two-softroce", ["softroce", "softroce"], "o2ib0(eth1),o2ib1(eth2)", 3),
]


@pytest.mark.parametrize(
    "nic_args,expected_lnet_body,expected_ipv4_count",
    [(c[1], c[2], c[3]) for c in _CASES],
    ids=[c[0] for c in _CASES],
)
def test_nic_combination(  # type: ignore[no-untyped-def]
    vm_name,
    nic_args: list[str],
    expected_lnet_body: str,
    expected_ipv4_count: int,
) -> None:
    """Boot a VM with the given --nic combination and verify the spec."""
    name = vm_name()
    extra = []
    for n in nic_args:
        extra += ["--nic", n]

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
        *extra,
        timeout=240,
    )
    assert proc.returncode == 0, (
        f"ltvm create failed rc={proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    wait_ssh(name)

    # 1. lnet.conf body.
    rc, out, err = ssh_run(name, "cat /etc/modprobe.d/lnet.conf")
    assert rc == 0, f"ssh cat lnet.conf failed rc={rc}: {err}"
    # rc.local writes exactly:
    #   options lnet networks="<body>"
    expected_line = f'options lnet networks="{expected_lnet_body}"'
    actual = out.strip()
    assert actual == expected_line, (
        f"lnet.conf mismatch for {nic_args!r}:\n"
        f"  expected: {expected_line!r}\n"
        f"  got:      {actual!r}"
    )

    # 2. IPv4 interface count.  We count lines from `ip -4 -br addr`
    # that actually have an address, excluding lo.  This checks that
    # fc_nic_ips= allocated + rc.local applied IPs to every ethN.
    rc, out, err = ssh_run(
        name,
        'ip -4 -br addr | awk \'$1 != "lo" && $3 != "" {print}\'',
    )
    assert rc == 0, f"ssh ip -br addr failed rc={rc}: {err}"
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == expected_ipv4_count, (
        f"expected {expected_ipv4_count} IPv4 ifaces for {nic_args!r}, "
        f"got {len(lines)}:\n{out}"
    )
