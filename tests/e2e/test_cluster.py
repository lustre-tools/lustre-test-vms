"""End-to-end tests for multi-node cluster create, deploy, and mount.

Three dependency levels:

* ``TestClusterLifecycle`` -- create/status/exec/destroy.  Requires
  only the base VM image; no built Lustre tree needed.

* ``TestClusterDeploy`` -- create + deploy Lustre to all nodes, verify
  binaries and config on each.  Requires LTVM_LUSTRE_TREE.

* ``TestClusterMount`` -- create + deploy --mount, verify Lustre is
  mounted and functional across the cluster.  Requires LTVM_LUSTRE_TREE.

Run with::

    make test-e2e

or directly::

    LTVM_LUSTRE_TREE=~/code_shared/master_checkouts/1 \\
        uv run pytest tests/e2e/test_cluster.py -v --no-cov -m e2e
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lib.vmctl import (
    cluster_create,
    cluster_deploy,
    cluster_destroy,
    cluster_exec,
    cluster_status,
)

# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------

BASE_IMAGE = Path("/opt/firecracker/images/rocky9-base.ext4")

skip_no_image = pytest.mark.skipif(
    not BASE_IMAGE.exists(),
    reason=(
        f"Base VM image not found: {BASE_IMAGE}. "
        "Build or provision the base image first."
    ),
)


def _find_lustre_tree() -> Path | None:
    override = os.environ.get("LTVM_LUSTRE_TREE")
    if not override:
        return None
    p = Path(override).expanduser().resolve()
    if not p.is_dir():
        return None
    if not any(f for f in p.rglob("*.ko") if "kconftest" not in str(f)):
        return None
    return p


LUSTRE_TREE = _find_lustre_tree()

skip_no_tree = pytest.mark.skipif(
    LUSTRE_TREE is None,
    reason=(
        "LTVM_LUSTRE_TREE not set or tree has no .ko files. "
        "Set it to a built Lustre tree matching the VM kernel."
    ),
)

# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

CLUSTER_BASE = f"ltvm-e2e-cl-{os.getpid()}"


def _cluster_name(suffix: str) -> str:
    safe = suffix.replace("[", "").replace("]", "").replace(" ", "_")
    return f"{CLUSTER_BASE}-{safe[:12]}"


def _node(cluster: str, role: str) -> str:
    """Predictable node name: <cluster>-<role>."""
    return f"{cluster}-{role}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def two_node_cluster(request: pytest.FixtureRequest) -> str:  # type: ignore[return]
    """Create a minimal mgs+mds / oss cluster; destroy on teardown."""
    name = _cluster_name(request.node.name)
    mds = _node(name, "mds")
    oss = _node(name, "oss")
    result = cluster_create(
        name,
        f"mgs+mds:{mds}:1",
        f"oss:{oss}:2",
    )
    if not result["ok"]:
        pytest.fail(
            f"cluster_create failed (rc={result['returncode']}):\n"
            f"{result['output']}"
        )
    yield name
    cluster_destroy(name)


@pytest.fixture
def deployed_cluster(request: pytest.FixtureRequest) -> str:  # type: ignore[return]
    """Cluster with Lustre deployed (not yet mounted). Destroys on teardown."""
    assert LUSTRE_TREE is not None
    name = _cluster_name(request.node.name)
    mds = _node(name, "mds")
    oss = _node(name, "oss")
    result = cluster_create(
        name,
        f"mgs+mds:{mds}:1",
        f"oss:{oss}:3",
    )
    if not result["ok"]:
        pytest.fail(
            f"cluster_create failed (rc={result['returncode']}):\n"
            f"{result['output']}"
        )
    result = cluster_deploy(name, build_path=LUSTRE_TREE, mount=False)
    if not result["ok"]:
        cluster_destroy(name)
        pytest.fail(
            f"cluster_deploy failed (rc={result['returncode']}):\n"
            f"{result['output']}"
        )
    yield name
    cluster_destroy(name)


@pytest.fixture
def mounted_cluster(request: pytest.FixtureRequest) -> str:  # type: ignore[return]
    """Cluster with Lustre deployed and mounted. Destroys on teardown."""
    assert LUSTRE_TREE is not None
    name = _cluster_name(request.node.name)
    mds = _node(name, "mds")
    oss = _node(name, "oss")
    result = cluster_create(
        name,
        f"mgs+mds:{mds}:1",
        f"oss:{oss}:3",
    )
    if not result["ok"]:
        pytest.fail(
            f"cluster_create failed (rc={result['returncode']}):\n"
            f"{result['output']}"
        )
    result = cluster_deploy(name, build_path=LUSTRE_TREE, mount=True)
    if not result["ok"]:
        cluster_destroy(name)
        pytest.fail(
            f"cluster_deploy --mount failed (rc={result['returncode']}):\n"
            f"{result['output']}"
        )
    yield name
    cluster_destroy(name)


# ---------------------------------------------------------------------------
# TestClusterLifecycle
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@skip_no_image
class TestClusterLifecycle:
    """Create/status/exec/destroy without any Lustre deployment."""

    def test_create_and_destroy(self, request: pytest.FixtureRequest) -> None:
        """cluster_create and cluster_destroy both succeed."""
        name = _cluster_name(request.node.name)
        mds = _node(name, "mds")
        oss = _node(name, "oss")
        result = cluster_create(name, f"mgs+mds:{mds}:1", f"oss:{oss}:2")
        assert result["ok"], (
            f"cluster_create failed (rc={result['returncode']}):\n"
            f"{result['output']}"
        )
        destroy = cluster_destroy(name)
        assert destroy["ok"], (
            f"cluster_destroy failed (rc={destroy['returncode']}):\n"
            f"{destroy['output']}"
        )

    def test_create_duplicate_fails(self, two_node_cluster: str) -> None:
        """Creating a cluster that already exists returns non-ok."""
        name = two_node_cluster
        mds = _node(name, "mds2")
        oss = _node(name, "oss2")
        result = cluster_create(name, f"mgs+mds:{mds}:1", f"oss:{oss}:2")
        assert not result["ok"], (
            "Expected second cluster_create to fail for an existing "
            "cluster name, but got ok=True"
        )

    def test_status_shows_nodes(self, two_node_cluster: str) -> None:
        """cluster_status output includes both node names."""
        name = two_node_cluster
        result = cluster_status(name)
        assert result["ok"], (
            f"cluster_status failed (rc={result['returncode']}):\n"
            f"{result['output']}"
        )
        out = result["output"]
        assert _node(name, "mds") in out, (
            f"MDS node not in status output:\n{out}"
        )
        assert _node(name, "oss") in out, (
            f"OSS node not in status output:\n{out}"
        )

    def test_status_shows_roles(self, two_node_cluster: str) -> None:
        """cluster_status output shows role assignments."""
        result = cluster_status(two_node_cluster)
        assert result["ok"], f"cluster_status failed: {result['output']}"
        out = result["output"]
        assert "mgs" in out, f"mgs role not in status:\n{out}"
        assert "mds" in out, f"mds role not in status:\n{out}"
        assert "oss" in out, f"oss role not in status:\n{out}"

    def test_exec_by_role(self, two_node_cluster: str) -> None:
        """cluster_exec targeting a role returns command output."""
        result = cluster_exec(two_node_cluster, "mds", "hostname")
        assert result["ok"], (
            f"cluster_exec by role failed (rc={result['returncode']}):\n"
            f"{result['output']}"
        )
        assert result["output"].strip(), (
            "Expected non-empty hostname from cluster_exec"
        )

    def test_exec_by_node_name(self, two_node_cluster: str) -> None:
        """cluster_exec targeting by exact node name returns output."""
        name = two_node_cluster
        oss_node = _node(name, "oss")
        result = cluster_exec(name, oss_node, "hostname")
        assert result["ok"], (
            f"cluster_exec by node name failed "
            f"(rc={result['returncode']}):\n{result['output']}"
        )
        assert result["output"].strip(), (
            "Expected non-empty hostname from OSS node exec"
        )

    def test_exec_nonexistent_role_fails(self, two_node_cluster: str) -> None:
        """cluster_exec for an unknown role returns non-ok."""
        result = cluster_exec(two_node_cluster, "client", "hostname")
        assert not result["ok"], (
            "Expected cluster_exec for non-existent role to fail"
        )

    def test_status_nonexistent_fails(self) -> None:
        """cluster_status for an unknown cluster name returns non-ok."""
        result = cluster_status(f"{CLUSTER_BASE}-no-such-cluster")
        assert not result["ok"], (
            "Expected cluster_status for nonexistent cluster to fail"
        )


# ---------------------------------------------------------------------------
# TestClusterDeploy
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@skip_no_image
@skip_no_tree
class TestClusterDeploy:
    """Verify cluster deploy syncs binaries and config to all nodes."""

    def test_lctl_on_mds(self, deployed_cluster: str) -> None:
        """lctl is present on the MDS node after deploy."""
        result = cluster_exec(deployed_cluster, "mds", "lctl --version")
        assert result["ok"], (
            f"lctl missing on MDS after deploy "
            f"(rc={result['returncode']}): {result['output']}"
        )

    def test_lctl_on_oss(self, deployed_cluster: str) -> None:
        """lctl is present on the OSS node after deploy."""
        result = cluster_exec(deployed_cluster, "oss", "lctl --version")
        assert result["ok"], (
            f"lctl missing on OSS after deploy "
            f"(rc={result['returncode']}): {result['output']}"
        )

    def test_modules_on_mds(self, deployed_cluster: str) -> None:
        """Lustre .ko files are present on the MDS node."""
        result = cluster_exec(
            deployed_cluster,
            "mds",
            "find /lib/modules -name '*.ko' -path '*/lustre/*' | head -3",
        )
        assert result["ok"] and result["output"].strip(), (
            f"No Lustre .ko files on MDS: {result['output']}"
        )

    def test_modules_on_oss(self, deployed_cluster: str) -> None:
        """Lustre .ko files are present on the OSS node."""
        result = cluster_exec(
            deployed_cluster,
            "oss",
            "find /lib/modules -name '*.ko' -path '*/lustre/*' | head -3",
        )
        assert result["ok"] and result["output"].strip(), (
            f"No Lustre .ko files on OSS: {result['output']}"
        )

    def test_local_sh_on_mds(self, deployed_cluster: str) -> None:
        """Cluster local.sh is present on the MDS node with topology info."""
        result = cluster_exec(
            deployed_cluster,
            "mds",
            "cat /usr/lib64/lustre/tests/cfg/local.sh",
        )
        assert result["ok"], f"local.sh missing on MDS: {result['output']}"
        out = result["output"]
        assert "MGSNID" in out, f"MGSNID not in local.sh:\n{out}"
        assert "OSTCOUNT" in out, f"OSTCOUNT not in local.sh:\n{out}"
        assert "MDSCOUNT" in out, f"MDSCOUNT not in local.sh:\n{out}"

    def test_local_sh_on_oss(self, deployed_cluster: str) -> None:
        """Cluster local.sh is present on the OSS node with topology info."""
        result = cluster_exec(
            deployed_cluster,
            "oss",
            "cat /usr/lib64/lustre/tests/cfg/local.sh",
        )
        assert result["ok"], f"local.sh missing on OSS: {result['output']}"
        out = result["output"]
        assert "MGSNID" in out, f"MGSNID not in local.sh:\n{out}"
        assert "OSTCOUNT" in out, f"OSTCOUNT not in local.sh:\n{out}"

    def test_local_sh_ost_count(self, deployed_cluster: str) -> None:
        """local.sh OSTCOUNT matches the number of OST disks configured."""
        result = cluster_exec(
            deployed_cluster,
            "mds",
            "grep '^OSTCOUNT=' /usr/lib64/lustre/tests/cfg/local.sh",
        )
        assert result["ok"], (
            f"OSTCOUNT not found in local.sh: {result['output']}"
        )
        # deployed_cluster fixture creates oss with 3 disks
        assert "OSTCOUNT=3" in result["output"], (
            f"Expected OSTCOUNT=3, got: {result['output']}"
        )

    def test_local_sh_consistent_across_nodes(
        self, deployed_cluster: str
    ) -> None:
        """local.sh content is identical on MDS and OSS nodes."""
        mds_result = cluster_exec(
            deployed_cluster,
            "mds",
            "cat /usr/lib64/lustre/tests/cfg/local.sh",
        )
        oss_result = cluster_exec(
            deployed_cluster,
            "oss",
            "cat /usr/lib64/lustre/tests/cfg/local.sh",
        )
        assert mds_result["ok"] and oss_result["ok"], (
            "Failed to read local.sh from one or both nodes"
        )
        assert mds_result["output"] == oss_result["output"], (
            "local.sh differs between MDS and OSS nodes"
        )


# ---------------------------------------------------------------------------
# TestClusterMount
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@skip_no_image
@skip_no_tree
class TestClusterMount:
    """Verify Lustre is mounted and functional on the cluster."""

    def test_modules_loaded_on_mds(self, mounted_cluster: str) -> None:
        """lctl dl on MDS shows MDT and LDLM devices."""
        result = cluster_exec(mounted_cluster, "mds", "lctl dl")
        assert result["ok"], f"lctl dl failed on MDS: {result['output']}"
        out = result["output"]
        assert "mdt" in out, f"MDT device not in lctl dl:\n{out}"
        assert "ldlm" in out, f"LDLM not in lctl dl:\n{out}"

    def test_modules_loaded_on_oss(self, mounted_cluster: str) -> None:
        """lctl dl on OSS shows obdfilter (OST) devices."""
        result = cluster_exec(mounted_cluster, "oss", "lctl dl")
        assert result["ok"], f"lctl dl failed on OSS: {result['output']}"
        assert "obdfilter" in result["output"], (
            f"No obdfilter in lctl dl on OSS:\n{result['output']}"
        )

    def test_ost_count_on_oss(self, mounted_cluster: str) -> None:
        """OSS has exactly 3 obdfilter devices (matching ost_disks=3)."""
        result = cluster_exec(
            mounted_cluster, "oss", "lctl dl | grep -c obdfilter"
        )
        assert result["ok"], f"lctl dl failed: {result['output']}"
        assert int(result["output"].strip()) == 3, (
            f"Expected 3 obdfilter devices, got: {result['output']}"
        )

    def test_lustre_mounted_on_mds(self, mounted_cluster: str) -> None:
        """/mnt/lustre is a mountpoint on the MDS node."""
        result = cluster_exec(
            mounted_cluster, "mds", "mountpoint -q /mnt/lustre"
        )
        assert result["ok"], (
            f"/mnt/lustre not mounted on MDS "
            f"(rc={result['returncode']}): {result['output']}"
        )

    def test_lfs_df(self, mounted_cluster: str) -> None:
        """lfs df on MDS shows OST and MDT entries."""
        result = cluster_exec(mounted_cluster, "mds", "lfs df /mnt/lustre")
        assert result["ok"], f"lfs df failed: {result['output']}"
        out = result["output"]
        assert "OST" in out, f"No OST in lfs df:\n{out}"
        assert "MDT" in out, f"No MDT in lfs df:\n{out}"

    def test_lfs_df_ost_count(self, mounted_cluster: str) -> None:
        """lfs df reports exactly 3 OST entries."""
        result = cluster_exec(
            mounted_cluster, "mds", "lfs df /mnt/lustre | grep -c OST"
        )
        assert result["ok"], f"lfs df failed: {result['output']}"
        assert int(result["output"].strip()) == 3, (
            f"Expected 3 OST entries in lfs df, got: {result['output']}"
        )

    def test_basic_io_on_mds(self, mounted_cluster: str) -> None:
        """Write and read back a file on /mnt/lustre from the MDS."""
        payload = "ltvm-cluster-e2e-io-12345"
        result = cluster_exec(
            mounted_cluster,
            "mds",
            f"echo '{payload}' > /mnt/lustre/e2e_cluster_test && "
            f"cat /mnt/lustre/e2e_cluster_test",
        )
        assert result["ok"], f"write/read failed: {result['output']}"
        assert payload in result["output"], (
            f"Readback mismatch: {result['output']!r}"
        )
        cluster_exec(
            mounted_cluster, "mds", "rm -f /mnt/lustre/e2e_cluster_test"
        )

    def test_lustre_version_consistent(self, mounted_cluster: str) -> None:
        """lctl get_param version returns same Lustre version on both nodes."""
        mds_r = cluster_exec(mounted_cluster, "mds", "lctl get_param version")
        oss_r = cluster_exec(mounted_cluster, "oss", "lctl get_param version")
        assert mds_r["ok"] and oss_r["ok"], (
            "lctl get_param version failed on one or both nodes"
        )
        assert "lustre" in mds_r["output"].lower(), (
            f"Unexpected MDS version: {mds_r['output']}"
        )
        assert "lustre" in oss_r["output"].lower(), (
            f"Unexpected OSS version: {oss_r['output']}"
        )

        # Major.minor should match between nodes
        def _version(out: str) -> str:
            for line in out.splitlines():
                if "lustre:" in line.lower():
                    return line.split()[-1]
            return out.strip()

        assert _version(mds_r["output"]) == _version(oss_r["output"]), (
            f"Version mismatch: MDS={mds_r['output']!r} OSS={oss_r['output']!r}"
        )
