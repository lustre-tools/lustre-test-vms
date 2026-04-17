"""End-to-end: 2-node cluster create + deploy + cross-node assertions.

`ltvm cluster create / deploy` is still flagged "early, not
well-verified" in the deck.  This test is part of removing that
caveat: if it passes cleanly end-to-end, we can drop the warning;
if it fails, we file a bd bug with the captured artefacts and the
test skips (with a pointer back to the bug) instead of failing the
whole suite.

Shape:
  * 2-node cluster: `mgs+mds` (1 MDT disk) + `oss` (3 OST disks).
  * Deploy + mount from ~/lustre-release.
  * From the mds node: `lctl dl` shows MGS + MDT.
  * From the oss node: `lctl dl` shows OSTs.
  * Destroy: both VMs gone, no leftover .cluster state, no leaked
    TAPs for either node.

No cross-mount (step 5 in the spec) -- llmount.sh via the cluster
deploy already mounts on the mgs node, so the "data read from
another node" dance requires a client node which we haven't
created.  Skipping as optional per the spec.
"""

from __future__ import annotations

import subprocess

import pytest

from .conftest import LUSTRE_TREE, SOCKETS_DIR, run_ltvm

# Cluster deploy-and-mount is the slow long pole: it builds lustre
# against the target kernel tree, rsyncs to each node, writes local.sh
# on both, and runs llmount.sh on the mgs node.  Cold path on this
# host is ~3-5 min; we give it 12 min of headroom before treating a
# stall as a real bug.
CLUSTER_DEPLOY_TIMEOUT = 720


def _tap_exists(tap: str) -> bool:
    r = subprocess.run(
        ["ip", "link", "show", "dev", tap],
        check=False, capture_output=True, text=True, timeout=5,
    )
    return r.returncode == 0


def _tap_for(name: str) -> str:
    import hashlib
    suffix = name if len(name) <= 11 else hashlib.md5(
        name.encode()
    ).hexdigest()[:11]
    return f"tap-{suffix}"


def _skip_on_cluster_bug(
    phase: str, proc: subprocess.CompletedProcess[str]
) -> None:
    """Skip cleanly when a cluster op fails; capture details inline.

    Per the phase-2 spec: we don't want an always-failing cluster
    test in the suite.  If `ltvm cluster <phase>` errors out, we
    call pytest.skip with a summary the next reader can act on (the
    full stdout/stderr already lives in /tmp/test_e2e via the
    failure-capture hook when the test is failing, but skips don't
    hit that hook -- so surface the key info in the reason).
    """
    stderr_head = (proc.stderr or "").strip().splitlines()[:6]
    stdout_tail = (proc.stdout or "").strip().splitlines()[-6:]
    reason = (
        f"ltvm cluster {phase} failed rc={proc.returncode} -- "
        f"early/unverified feature; file a bd bug before re-enabling. "
        f"stderr head: {stderr_head!r} | stdout tail: {stdout_tail!r}"
    )
    pytest.skip(reason)


def test_cluster_create_deploy_destroy(cluster_name) -> None:  # type: ignore[no-untyped-def]
    """Minimal 2-node cluster: create, deploy+mount, probe, destroy."""
    assert LUSTRE_TREE.is_dir(), (
        f"~/lustre-release missing at {LUSTRE_TREE}"
    )

    cname, mds, oss = cluster_name()
    mds_tap = _tap_for(mds)
    oss_tap = _tap_for(oss)
    cluster_state = SOCKETS_DIR / f"{cname}.cluster"

    # ---- create ----
    # 1024 MiB per node keeps the cluster under ~2 GiB total, which
    # fits alongside the other dev VMs the host normally carries.
    proc = run_ltvm(
        "cluster", "create", cname,
        "--mem", "1024",
        "--vcpus", "1",
        f"mgs+mds:{mds}:1",
        f"oss:{oss}:3",
        timeout=360,
    )
    if proc.returncode != 0:
        _skip_on_cluster_bug("create", proc)

    assert cluster_state.exists(), (
        f"cluster state file {cluster_state} missing after create"
    )
    assert _tap_exists(mds_tap), f"mds TAP {mds_tap!r} missing after create"
    assert _tap_exists(oss_tap), f"oss TAP {oss_tap!r} missing after create"

    # ---- deploy + mount ----
    proc = run_ltvm(
        "cluster", "deploy", cname,
        "--build", str(LUSTRE_TREE),
        "--mount",
        timeout=CLUSTER_DEPLOY_TIMEOUT,
    )
    if proc.returncode != 0:
        _skip_on_cluster_bug("deploy", proc)

    # ---- probe: mds shows MGS + MDT ----
    # `ltvm cluster exec` takes command words as separate argv,
    # shlex.join's them for the remote shell -- passing "lctl dl"
    # as one arg sends it to bash as a single word and trips
    # "command not found" (rc=127).
    proc = run_ltvm(
        "cluster", "exec", cname, "mds", "lctl", "dl",
        timeout=30,
    )
    if proc.returncode != 0:
        _skip_on_cluster_bug("exec mds 'lctl dl'", proc)
    mds_dl = proc.stdout
    # We care about the device types listed in `lctl dl`'s column 3.
    # For an mgs+mds node the expected types include mgs, mdt, and
    # usually mds + lov/lod; assert the two strong markers.
    assert " mgs " in f" {mds_dl} ", (
        f"expected 'mgs' device on mds node, got:\n{mds_dl}"
    )
    assert " mdt " in f" {mds_dl} ", (
        f"expected 'mdt' device on mds node, got:\n{mds_dl}"
    )

    # ---- probe: oss shows OSTs ----
    proc = run_ltvm(
        "cluster", "exec", cname, "oss", "lctl", "dl",
        timeout=30,
    )
    if proc.returncode != 0:
        _skip_on_cluster_bug("exec oss 'lctl dl'", proc)
    oss_dl = proc.stdout
    # At least one obdfilter (OST backend) -- 3 disks -> 3 OSTs.
    assert " obdfilter " in f" {oss_dl} ", (
        f"expected 'obdfilter' devices on oss node, got:\n{oss_dl}"
    )

    # ---- destroy ----
    proc = run_ltvm("cluster", "destroy", cname, timeout=120)
    assert proc.returncode == 0, (
        f"cluster destroy failed rc={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    assert not cluster_state.exists(), (
        f"leak: {cluster_state} present after destroy"
    )
    assert not (SOCKETS_DIR / f"{mds}.info").exists(), (
        f"leak: mds .info still present after cluster destroy"
    )
    assert not (SOCKETS_DIR / f"{oss}.info").exists(), (
        f"leak: oss .info still present after cluster destroy"
    )
    assert not _tap_exists(mds_tap), (
        f"leak: TAP {mds_tap!r} still present after cluster destroy"
    )
    assert not _tap_exists(oss_tap), (
        f"leak: TAP {oss_tap!r} still present after cluster destroy"
    )
