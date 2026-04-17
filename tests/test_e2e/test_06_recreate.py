"""End-to-end: destroy + recreate same name returns the same IP.

Covers the VM-lifecycle cleanliness contract:

  * All per-VM artefacts (TAP, /etc/hosts entry, .info, overlay)
    exist while the VM is up.
  * `ltvm destroy` removes every one of them.
  * `ltvm create` with the same name afterwards deterministically
    returns the same IP (vm_net.alloc_ip seeds its hunt at
    `md5(name) % 244 + 10`, so recreating a name into an otherwise
    unchanged pool must land on the same octet).

Keeping this test Lustre-free so it stays under 90s even on cold
hosts -- it's purely VM plumbing.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .conftest import SOCKETS_DIR, run_ltvm, wait_ssh

OVERLAYS_DIR = Path("/opt/qemu-vms/overlays")
HOSTS_FILE = Path("/etc/hosts")


def _tap_exists(tap: str) -> bool:
    """True if a TAP named `tap` exists on the host.

    `ip link show dev <tap>` exits 0 when the link exists, non-zero
    otherwise; we don't care about the full output.
    """
    r = subprocess.run(
        ["ip", "link", "show", "dev", tap],
        check=False, capture_output=True, text=True, timeout=5,
    )
    return r.returncode == 0


def _hosts_entry_for(name: str) -> str | None:
    """Return the IP from /etc/hosts for `name`, or None if not present.

    Tolerates both exact-name-only and `# qemu-vm:<name>` marker
    forms so a future refactor of the hosts writer doesn't break
    this check.
    """
    try:
        text = HOSTS_FILE.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Match either: "<ip>\t<name>" or "<ip>\t<name> # qemu-vm:<name>"
        m = re.match(r"^(\S+)\s+(\S+)", stripped)
        if not m:
            continue
        ip, hname = m.group(1), m.group(2)
        if hname == name:
            return ip
    return None


def _tap_for(name: str) -> str:
    """Mirror ltvm_pkg.vm_net.tap_for_name for assertions.

    We deliberately don't import from ltvm_pkg because these tests
    exercise the CLI from the outside -- keeping the public-surface
    boundary clean means an internal rename can't silently make the
    assertion pass against a stale TAP.
    """
    import hashlib
    suffix = name if len(name) <= 11 else hashlib.md5(
        name.encode()
    ).hexdigest()[:11]
    return f"tap-{suffix}"


def test_destroy_then_recreate_restores_artifacts(vm_name) -> None:  # type: ignore[no-untyped-def]
    """Destroy yanks all artifacts; recreate restores them with the same IP."""
    name = vm_name()
    tap = _tap_for(name)
    info = SOCKETS_DIR / f"{name}.info"
    overlay = OVERLAYS_DIR / f"{name}.qcow2"

    # ---- create #1 ----
    proc = run_ltvm(
        "create", name,
        "--mem", "1024",
        "--vcpus", "1",
        "--mdt-disks", "0",
        "--ost-disks", "0",
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"first create failed rc={proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    wait_ssh(name)

    # All per-VM artifacts should now exist.
    assert info.exists(), f"missing {info} after first create"
    assert overlay.exists(), f"missing {overlay} after first create"
    assert _tap_exists(tap), f"TAP {tap!r} missing after first create"

    ip_before = _hosts_entry_for(name)
    assert ip_before is not None, (
        f"no /etc/hosts entry for {name!r} after first create"
    )

    # ---- destroy ----
    proc = run_ltvm("destroy", name, timeout=60)
    assert proc.returncode == 0, (
        f"destroy failed rc={proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )

    assert not info.exists(), f"leak: {info} still present after destroy"
    assert not overlay.exists(), (
        f"leak: {overlay} still present after destroy"
    )
    assert not _tap_exists(tap), (
        f"leak: TAP {tap!r} still present after destroy"
    )
    assert _hosts_entry_for(name) is None, (
        f"leak: /etc/hosts still has {name!r} after destroy"
    )

    # ---- create #2 (same name) ----
    proc = run_ltvm(
        "create", name,
        "--mem", "1024",
        "--vcpus", "1",
        "--mdt-disks", "0",
        "--ost-disks", "0",
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"second create failed rc={proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    wait_ssh(name)

    assert info.exists(), f"missing {info} after second create"
    assert overlay.exists(), f"missing {overlay} after second create"
    assert _tap_exists(tap), f"TAP {tap!r} missing after second create"

    ip_after = _hosts_entry_for(name)
    assert ip_after is not None, (
        f"no /etc/hosts entry for {name!r} after second create"
    )
    # alloc_ip's hash-seeded scan must return the same octet as long
    # as the rest of the pool hasn't shifted.  If the host was
    # contended enough to push the allocation forward, we'd still
    # want to notice -- so assert the strong invariant.
    assert ip_after == ip_before, (
        f"IP changed across recreate (alloc_ip is not deterministic?):\n"
        f"  before: {ip_before}\n"
        f"  after:  {ip_after}"
    )
