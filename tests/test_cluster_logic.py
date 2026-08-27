"""Tests for ltvm_pkg/vm_cluster.py: spec parsing, local.sh generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from ltvm_pkg import vm_cluster
from ltvm_pkg.vm_state import ClusterInfo

# ── parse_node_spec ──────────────────────────────────────


class TestParseNodeSpec:
    """parse_node_spec accepts roles:name[:disks] and rejects garbage."""

    def test_mgs_mds_combined_defaults_to_one_mdt(self) -> None:
        """mgs+mds with no disk count gets the minimum 1 MDT disk."""
        n = vm_cluster.parse_node_spec("mgs+mds:co1-mds")
        assert n.roles == ["mgs", "mds"]
        assert n.is_mgs and n.is_mds
        assert n.mdt_disks == 1
        assert n.ost_disks == 0

    def test_mds_with_explicit_disk_count(self) -> None:
        """Explicit disk count overrides the minimum."""
        n = vm_cluster.parse_node_spec("mds:co1-mds:3")
        assert n.mdt_disks == 3
        assert n.ost_disks == 0

    def test_oss_disk_count_goes_to_ost(self) -> None:
        n = vm_cluster.parse_node_spec("oss:co1-oss:4")
        assert n.mdt_disks == 0
        assert n.ost_disks == 4
        assert n.is_oss

    def test_client_no_disks(self) -> None:
        """Client role gets no MDT or OST disks."""
        n = vm_cluster.parse_node_spec("client:co1-client")
        assert n.is_client
        assert n.mdt_disks == 0
        assert n.ost_disks == 0

    def test_mgs_alone_no_disks(self) -> None:
        """mgs without mds gets no MDT disks from parse_node_spec itself;
        the extra MGS disk is added later at create time."""
        n = vm_cluster.parse_node_spec("mgs:co1-mgs")
        assert n.is_mgs and not n.is_mds
        assert n.mdt_disks == 0

    def test_oss_default_to_one(self) -> None:
        """oss with no count still gets a minimum of 1 OST."""
        n = vm_cluster.parse_node_spec("oss:co1-oss")
        assert n.ost_disks == 1

    def test_unknown_role_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_cluster.parse_node_spec("junk:co1-x")

    def test_missing_name_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_cluster.parse_node_spec("mds")

    def test_invalid_vm_name_dies(self) -> None:
        """Names with spaces or leading hyphen are rejected by validator."""
        with pytest.raises(SystemExit):
            vm_cluster.parse_node_spec("mds:-bad-name:1")
        with pytest.raises(SystemExit):
            vm_cluster.parse_node_spec("mds:bad name:1")

    def test_non_integer_disk_count_dies_cleanly(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A typo like 'mds:foo:abc' must produce a clean error
        message via die(), not a raw ValueError traceback."""
        with pytest.raises(SystemExit):
            vm_cluster.parse_node_spec("mds:co1-mds:abc")
        err = capsys.readouterr().err
        assert "abc" in err
        assert "integer" in err.lower() or "disk" in err.lower()

    def test_role_case_insensitive(self) -> None:
        """Roles are lowercased before comparison."""
        n = vm_cluster.parse_node_spec("MDS:co1-mds:2")
        assert n.roles == ["mds"]
        assert n.mdt_disks == 2


# ── generate_local_sh ────────────────────────────────────


def _cluster(*nodes) -> ClusterInfo:
    """Build a ClusterInfo from (name, roles, mdt, ost, ip) tuples."""
    return ClusterInfo(
        name="testc",
        nodes=[
            {
                "name": n[0],
                "roles": list(n[1]),
                "mdt_disks": n[2],
                "ost_disks": n[3],
                "ip": n[4],
            }
            for n in nodes
        ],
    )


class TestGenerateLocalSh:
    """generate_local_sh produces a valid cfg/local.sh for Lustre tests."""

    def test_combined_mgs_mds_plus_oss(self) -> None:
        """Classic MGS+MDS on one node, OSS on another."""
        c = _cluster(
            ("co2-mds", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("co2-oss", ["oss"], 0, 3, "10.0.0.11"),
        )
        text = vm_cluster.generate_local_sh(c)
        assert "mgs_HOST=co2-mds" in text
        assert "MGSNID=10.0.0.10@tcp" in text
        # combined=True -> no separate MGSDEV
        assert "MGSDEV" not in text
        assert "mds_HOST=co2-mds" in text
        assert "MDSCOUNT=1" in text
        assert "MDSDEV1=/dev/vdb" in text
        assert "ost_HOST=co2-oss" in text
        assert "OSTCOUNT=3" in text
        # OSS is not MDS/MGS, so ost disks start at vdb
        assert "OSTDEV1=/dev/vdb" in text
        assert "OSTDEV2=/dev/vdc" in text
        assert "OSTDEV3=/dev/vdd" in text

    def test_split_mgs_mds_oss(self) -> None:
        """Three dedicated nodes: MGS with its own disk, separate MDS."""
        c = _cluster(
            ("co3-mgs", ["mgs"], 0, 0, "10.0.0.1"),
            ("co3-mds", ["mds"], 1, 0, "10.0.0.2"),
            ("co3-oss", ["oss"], 0, 2, "10.0.0.3"),
        )
        text = vm_cluster.generate_local_sh(c)
        assert "mgs_HOST=co3-mgs" in text
        # standalone MGS -> MGSDEV is set
        assert "MGSDEV=/dev/vdb" in text
        assert "mds_HOST=co3-mds" in text
        assert "MDSDEV1=/dev/vdb" in text
        # OSS doesn't host MGS, starts at vdb
        assert "OSTDEV1=/dev/vdb" in text
        assert "OSTDEV2=/dev/vdc" in text

    def test_multi_mds_numbers_hosts(self) -> None:
        """Two MDS nodes get per-index MDSDEV + mdsN_HOST entries."""
        c = _cluster(
            ("co-mgs", ["mgs"], 0, 0, "10.0.0.1"),
            ("co-mds1", ["mds"], 1, 0, "10.0.0.2"),
            ("co-mds2", ["mds"], 1, 0, "10.0.0.3"),
            ("co-oss", ["oss"], 0, 1, "10.0.0.4"),
        )
        text = vm_cluster.generate_local_sh(c)
        assert "MDSCOUNT=2" in text
        assert "MDSDEV1=/dev/vdb" in text
        assert "MDSDEV2=/dev/vdb" in text  # each on its own node
        assert "mds1_HOST=co-mds1" in text
        assert "mds2_HOST=co-mds2" in text

    def test_multi_oss_numbers_hosts(self) -> None:
        """Multiple OSS nodes get per-index OSTDEV + ostN_HOST entries."""
        c = _cluster(
            ("co-mds", ["mgs", "mds"], 1, 0, "10.0.0.1"),
            ("co-oss1", ["oss"], 0, 2, "10.0.0.2"),
            ("co-oss2", ["oss"], 0, 1, "10.0.0.3"),
        )
        text = vm_cluster.generate_local_sh(c)
        assert "OSTCOUNT=3" in text
        # oss1: OST 1+2, vdb+vdc on co-oss1; oss2: OST 3, vdb on co-oss2
        assert "OSTDEV1=/dev/vdb" in text
        assert "OSTDEV2=/dev/vdc" in text
        assert "OSTDEV3=/dev/vdb" in text  # new node, reset to vdb
        assert "ost1_HOST=co-oss1" in text
        assert "ost2_HOST=co-oss1" in text
        assert "ost3_HOST=co-oss2" in text

    def test_combined_mds_oss_disk_offset(self) -> None:
        """A single node hosting MDS+OSS: OST disks start after MDT disks."""
        c = _cluster(
            ("co-all", ["mgs", "mds", "oss"], 2, 2, "10.0.0.1"),
        )
        text = vm_cluster.generate_local_sh(c)
        # MDT: vdb, vdc; OST: vdd, vde
        assert "MDSDEV1=/dev/vdb" in text
        assert "MDSDEV2=/dev/vdc" in text
        assert "OSTDEV1=/dev/vdd" in text
        assert "OSTDEV2=/dev/vde" in text

    def test_clients_listed(self) -> None:
        """Client nodes are available to mounting and test-framework setup."""
        c = _cluster(
            ("co-mds", ["mgs", "mds"], 1, 0, "10.0.0.1"),
            ("co-oss", ["oss"], 0, 1, "10.0.0.2"),
            ("co-c1", ["client"], 0, 0, "10.0.0.3"),
            ("co-c2", ["client"], 0, 0, "10.0.0.4"),
        )
        text = vm_cluster.generate_local_sh(c)
        assert "CLIENTS=co-c1,co-c2" in text
        assert 'RCLIENTS="co-c2"' in text

    def test_rclients_excludes_test_runner(self) -> None:
        """RCLIENTS= lists the clients other than the first.

        init_clients_lists() rebuilds CLIENTS from RCLIENTS, so without
        this the remote clients drop out of every multi-client suite.
        """
        c = _cluster(
            ("co-mds", ["mgs", "mds"], 1, 0, "10.0.0.1"),
            ("co-c1", ["client"], 0, 0, "10.0.0.3"),
            ("co-c2", ["client"], 0, 0, "10.0.0.4"),
            ("co-c3", ["client"], 0, 0, "10.0.0.5"),
        )
        text = vm_cluster.generate_local_sh(c)
        assert 'RCLIENTS="co-c2 co-c3"' in text

    def test_rclients_omitted_for_single_client(self) -> None:
        """One client means no remote clients -- don't emit RCLIENTS."""
        c = _cluster(
            ("co-mds", ["mgs", "mds"], 1, 0, "10.0.0.1"),
            ("co-c1", ["client"], 0, 0, "10.0.0.3"),
        )
        text = vm_cluster.generate_local_sh(c)
        assert "CLIENTS=co-c1" in text
        assert "RCLIENTS" not in text

    def test_rhel_libdir_default(self) -> None:
        c = _cluster(("n", ["mgs", "mds"], 1, 0, "10.0.0.1"))
        text = vm_cluster.generate_local_sh(c, os_family="rhel")
        assert "LUSTRE=/usr/lib64/lustre" in text
        assert "RLUSTRE=/usr/lib64/lustre" in text
        assert "RPWD=/usr/lib64/lustre/tests" in text

    def test_debian_libdir(self) -> None:
        c = _cluster(("n", ["mgs", "mds"], 1, 0, "10.0.0.1"))
        text = vm_cluster.generate_local_sh(c, os_family="debian")
        assert "LUSTRE=/usr/lib/lustre" in text
        assert "RPWD=/usr/lib/lustre/tests" in text

    def test_common_invariants(self) -> None:
        """Every cluster config gets the standard fsname/net/ldiskfs block."""
        c = _cluster(("n", ["mgs", "mds"], 1, 0, "10.0.0.1"))
        text = vm_cluster.generate_local_sh(c)
        assert "FSNAME=lustre" in text
        assert "NETTYPE=tcp" in text
        assert "FSTYPE=ldiskfs" in text
        assert "MOUNT=/mnt/lustre" in text
        assert "MOUNT2=/mnt/lustre2" in text
        assert "DIR=${DIR:-$MOUNT}" in text
        assert "DIR1=${DIR1:-$MOUNT1}" in text
        assert "DIR2=${DIR2:-$MOUNT2}" in text
        assert "LOAD_MODULES_REMOTE=true" in text


# ── _validate_lustre_source ──────────────────────────────


class TestValidateLustreSource:
    """_validate_lustre_source catches obvious non-Lustre-tree inputs."""

    def test_rejects_non_directory(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            vm_cluster._validate_lustre_source(tmp_path / "nope")

    def test_rejects_missing_files(self, tmp_path: Path) -> None:
        """Empty dir is missing configure.ac, lustre/, lnet/."""
        with pytest.raises(SystemExit):
            vm_cluster._validate_lustre_source(tmp_path)

    def test_accepts_minimal_tree(self, tmp_path: Path) -> None:
        """A tree with the three sentinel entries passes."""
        (tmp_path / "configure.ac").write_text("")
        (tmp_path / "lustre").mkdir()
        (tmp_path / "lnet").mkdir()
        # Should not raise
        vm_cluster._validate_lustre_source(tmp_path)


# ── ClusterInfo helpers used by generate_local_sh ────────


class TestClusterInfoRoleQueries:
    """Role-query helpers on ClusterInfo feed generate_local_sh correctly."""

    def test_mgs_node_raises_when_missing(self) -> None:
        c = ClusterInfo(
            name="no-mgs",
            nodes=[
                {
                    "name": "lonely",
                    "roles": ["client"],
                    "mdt_disks": 0,
                    "ost_disks": 0,
                    "ip": "1.2.3.4",
                }
            ],
        )
        with pytest.raises(RuntimeError, match="no MGS"):
            c.mgs_node()

    def test_role_filters_split_correctly(self) -> None:
        """mds_nodes / oss_nodes / client_nodes isolate their roles."""
        c = _cluster(
            ("a", ["mgs", "mds"], 1, 0, "1.1.1.1"),
            ("b", ["oss"], 0, 1, "1.1.1.2"),
            ("c", ["client"], 0, 0, "1.1.1.3"),
            ("d", ["client"], 0, 0, "1.1.1.4"),
        )
        assert [n.name for n in c.mds_nodes()] == ["a"]
        assert [n.name for n in c.oss_nodes()] == ["b"]
        assert [n.name for n in c.client_nodes()] == ["c", "d"]
        assert c.mgs_node().name == "a"
