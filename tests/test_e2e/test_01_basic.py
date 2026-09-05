"""End-to-end smoke: create a VM, ssh in, destroy, verify cleanup.

One scenario per test function.  Teardown is handled by the
`vm_name` fixture in conftest.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import (
    SOCKETS_DIR,
    run_ltvm,
    ssh_run,
    wait_ssh,
)


def test_basic_create_ssh_destroy(vm_name, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """`ltvm create` -> ssh -> hostname / eth0 / destroy.

    Asserts:
      * `ltvm create ... --mem 2048` exits 0
      * ssh becomes reachable
      * `hostname` inside the VM == the vm name
      * `ip -br addr` shows eth0 UP with an IPv4 address
      * `ltvm destroy` exits 0
      * post-destroy: .info file is gone and `ssh <name>` fails
    """
    name = vm_name()

    # Create with no extra NICs and no disks.  --mem 2048 to shave
    # some fat off the default.  --ost-disks 0 / --mdt-disks 0 keeps
    # the overlay footprint minimal.
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
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"ltvm create failed rc={proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )

    wait_ssh(name)

    rc, out, err = ssh_run(name, "hostname")
    assert rc == 0, f"ssh hostname failed rc={rc}: {err}"
    assert out.strip() == name, (
        f"hostname inside VM is {out.strip()!r}, expected {name!r}"
    )

    rc, out, err = ssh_run(name, "ip -br -4 addr show dev eth0")
    assert rc == 0, f"ssh ip addr failed rc={rc}: {err}"
    # Expected: "eth0  UP  192.168.100.xx/24"
    tokens = out.split()
    assert "UP" in tokens, f"eth0 not UP in `ip -br` output: {out!r}"
    has_ipv4 = any("/" in t and t.split("/")[0].count(".") == 3 for t in tokens)
    assert has_ipv4, f"no IPv4 on eth0: {out!r}"

    info = SOCKETS_DIR / f"{name}.info"
    assert info.exists(), f"expected {info} to exist while VM is running"

    proc = run_ltvm("destroy", name, timeout=60)
    assert proc.returncode == 0, (
        f"ltvm destroy failed rc={proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )

    assert not info.exists(), f"after destroy, {info} still exists -- leak"
    # ssh must now fail (host removed from /etc/hosts + VM gone).
    rc, _, _ = ssh_run(name, "true", timeout=5)
    assert rc != 0, f"ssh to destroyed VM {name!r} still succeeded"
