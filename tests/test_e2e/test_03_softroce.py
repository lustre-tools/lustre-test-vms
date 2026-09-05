"""End-to-end: deeper assertions on `--nic softroce`.

test_02 covers the table shape; this test drills into the softroce
hook (setup-nic-softroce.sh, commit 12dace6) and verifies the image
actually brings up rxe0 in state ACTIVE on eth1 with the tuned MTU.
"""

from __future__ import annotations

import re

from .conftest import run_ltvm, ssh_run, wait_ssh


def test_softroce_rxe0_active(vm_name) -> None:  # type: ignore[no-untyped-def]
    """Single `--nic softroce` boots with an ACTIVE rxe0 on eth1 at MTU 4200.

    Assertions:
      * `rdma link show` lists a link named `rxe0` in state ACTIVE
        with `netdev eth1`
      * `modinfo rdma_rxe` exits 0 (module preinstalled)
      * `ip -d link show eth1` reports mtu >= 4200 (set by the hook)
    """
    name = vm_name()
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
        "--nic",
        "softroce",
        timeout=240,
    )
    assert proc.returncode == 0, (
        f"ltvm create failed rc={proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    wait_ssh(name)

    # 1. rdma link show -- expect a single line:
    #   link rxe0/1 state ACTIVE physical_state LINK_UP netdev eth1
    rc, out, err = ssh_run(name, "rdma link show")
    assert rc == 0, f"ssh rdma link show failed rc={rc}: {err}"
    # Tolerate leading/trailing whitespace + minor wording variants.
    m = re.search(
        r"^link\s+(rxe\d+)(?:/\d+)?\s+state\s+(\S+).*?netdev\s+(\S+)",
        out,
        re.MULTILINE,
    )
    assert m is not None, f"no rxe link parsed from `rdma link show`:\n{out}"
    rxe_name, state, netdev = m.group(1), m.group(2), m.group(3)
    assert rxe_name == "rxe0", (
        f"expected rxe0 (first softroce), got {rxe_name!r}"
    )
    assert state == "ACTIVE", (
        f"rxe0 state is {state!r}, expected ACTIVE:\n{out}"
    )
    assert netdev == "eth1", f"rxe0 netdev is {netdev!r}, expected eth1:\n{out}"

    # 2. modinfo rdma_rxe: the module must be in the image.
    rc, out, err = ssh_run(name, "modinfo rdma_rxe")
    assert rc == 0, (
        f"modinfo rdma_rxe failed rc={rc}; stderr: {err}\nstdout: {out}"
    )

    # 3. MTU tuned on eth1.  `ip -d link show eth1` output has the
    # token `mtu <N>` on the first line.
    rc, out, err = ssh_run(name, "ip -d link show eth1")
    assert rc == 0, f"ssh ip -d link show eth1 failed rc={rc}: {err}"
    m = re.search(r"\bmtu\s+(\d+)\b", out)
    assert m is not None, f"mtu not found in ip -d link:\n{out}"
    mtu = int(m.group(1))
    assert mtu >= 4200, f"eth1 mtu is {mtu}, expected >= 4200 (softroce hook)"
