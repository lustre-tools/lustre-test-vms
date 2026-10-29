"""Tests for ltvm_pkg/vm_cluster.py: spec parsing, local.sh generation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import deploy, vm_cluster
from ltvm_pkg.deploy import ENV_SAVE_BEGIN
from ltvm_pkg.vm_state import ClusterInfo, ClusterNode
from tests.host_marks import needs_gnu_sed

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

    def test_disk_sizes_reach_the_cluster_block(self) -> None:
        """The client formats every target, so it needs the sizes too."""
        c = _cluster(
            ("co9-mds", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("co9-oss", ["oss"], 0, 4, "10.0.0.11"),
            ("co9-client", ["client"], 0, 0, "10.0.0.12"),
        )
        sizes = {"co9-mds": 4 << 30, "co9-oss": 8 << 30, "co9-client": 0}
        text = vm_cluster.generate_local_sh(c, disk_sizes=sizes)
        assert "MDSSIZE=4194304" in text
        assert "OSTSIZE=8388608" in text

    def test_disk_sizes_use_smallest_oss(self) -> None:
        c = _cluster(
            ("co2-mds", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("co2-oss1", ["oss"], 0, 2, "10.0.0.11"),
            ("co2-oss2", ["oss"], 0, 2, "10.0.0.12"),
        )
        sizes = {"co2-mds": 1 << 30, "co2-oss1": 8 << 30, "co2-oss2": 2 << 30}
        text = vm_cluster.generate_local_sh(c, disk_sizes=sizes)
        assert "OSTSIZE=2097152" in text

    def test_no_disk_sizes_leaves_framework_default(self) -> None:
        c = _cluster(
            ("co2-mds", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("co2-oss", ["oss"], 0, 3, "10.0.0.11"),
        )
        text = vm_cluster.generate_local_sh(c)
        assert "OSTSIZE" not in text
        assert "MDSSIZE" not in text

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
        assert "MDSCOUNT=${_ltvm_env_MDSCOUNT:-1}" in text
        assert "MDSDEV1=/dev/vdb" in text
        assert "ost_HOST=co2-oss" in text
        assert "OSTCOUNT=${_ltvm_env_OSTCOUNT:-3}" in text
        # OSS is not MDS/MGS, so ost disks start at vdb
        assert "OSTDEV1=/dev/vdb" in text
        assert "OSTDEV2=/dev/vdc" in text
        assert "OSTDEV3=/dev/vdd" in text
        # Per-target hosts even with one node of each: the framework's
        # all_osts_nodes() (SSK key distribution) reads only these.
        assert "mds1_HOST=co2-mds" in text
        for i in (1, 2, 3):
            assert f"ost{i}_HOST=co2-oss" in text

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
        assert "MDSCOUNT=${_ltvm_env_MDSCOUNT:-2}" in text
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
        assert "OSTCOUNT=${_ltvm_env_OSTCOUNT:-3}" in text
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
        assert "FSTYPE=${_ltvm_env_FSTYPE:-ldiskfs}" in text
        assert "MOUNT=/mnt/lustre" in text
        assert "MOUNT2=/mnt/lustre2" in text
        assert "DIR=${DIR:-$MOUNT}" in text
        assert "DIR1=${DIR1:-$MOUNT1}" in text
        assert "DIR2=${DIR2:-$MOUNT2}" in text
        assert "LOAD_MODULES_REMOTE=true" in text


# ── _validate_lustre_source ──────────────────────────────


# ── the MGS's LNet net, as the node reports it ──────────


# `ip -o addr show` on a node with one extra NIC.
_ADDRS = (
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
    "1: lo    inet6 ::1/128 scope host \\       valid_lft forever\n"
    "2: eth0    inet 192.168.122.10/24 brd 192.168.122.255 scope global "
    "eth0\\       valid_lft forever\n"
    "3: eth1    inet 172.16.100.23/24 brd 172.16.100.255 scope global "
    "eth1\\       valid_lft forever\n"
    "3: eth1    inet6 fd17:2016:1000:f100:f172:f016:f100:f023/64 scope "
    "global \\       valid_lft forever\n"
)


def _probe(networks: str) -> str:
    return f"{networks}\n--\n{_ADDRS}"


class TestOstCount:
    """--ost-count on a cluster: files on the OSS nodes, in turn."""

    def _two_oss(self) -> ClusterInfo:
        return _cluster(
            ("co2-mds", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("co2-oss1", ["oss"], 0, 2, "10.0.0.11"),
            ("co2-oss2", ["oss"], 0, 1, "10.0.0.12"),
            ("co2-client", ["client"], 0, 0, "10.0.0.13"),
        )

    def _plan(
        self, c: ClusterInfo, count: int, space: dict[str, int], **kw
    ) -> tuple[vm_cluster.OstFilePlan | None, list]:
        ips = {n.name: n.ip for n in c.get_nodes()}
        by_ip = {ips[name]: kb for name, kb in space.items()}
        calls: list = []

        def fake_prepare(ip, keep, *, prune):
            calls.append((ip, keep, prune))
            return by_ip.get(ip, 0)

        sizes = {n.name: 500 << 20 for n in c.get_nodes()}
        with patch("ltvm_pkg.deploy.prepare_ost_files", fake_prepare):
            plan = vm_cluster.plan_ost_files(
                c, count, ips, sizes, prune=kw.get("prune", True)
            )
        return plan, calls

    def test_files_go_round_the_oss_nodes(self) -> None:
        big = 16 << 20
        plan, calls = self._plan(
            self._two_oss(), 8, {"co2-oss1": big, "co2-oss2": big}
        )
        assert plan is not None
        # Three disks, so the files are OSTs 4 to 8.
        assert plan.hosts == {
            4: "co2-oss1",
            5: "co2-oss2",
            6: "co2-oss1",
            7: "co2-oss2",
            8: "co2-oss1",
        }
        assert plan.size_kb == 512000
        assert ("10.0.0.11", [4, 6, 8], True) in calls
        assert ("10.0.0.12", [5, 7], True) in calls
        # Only OSS nodes keep OST files.
        assert {c[0] for c in calls} == {"10.0.0.11", "10.0.0.12"}

    def test_the_tightest_node_sets_the_size(self) -> None:
        reserve = deploy.OST_FILE_RESERVE_KB
        plan, _ = self._plan(
            self._two_oss(),
            5,
            {"co2-oss1": 16 << 20, "co2-oss2": reserve + 300 * 1024},
        )
        assert plan is not None
        assert plan.size_kb == 300 * 1024

    def test_too_little_space_dies(self) -> None:
        with pytest.raises(SystemExit):
            self._plan(self._two_oss(), 8, {"co2-oss1": 16 << 20})

    def test_within_the_disks_only_prunes(self) -> None:
        plan, calls = self._plan(self._two_oss(), 0, {})
        assert plan is None
        assert all(keep == [] and prune for _, keep, prune in calls)
        assert len(calls) == 2

    def test_no_oss_dies(self) -> None:
        c = _cluster(("co2-mds", ["mgs", "mds"], 1, 0, "10.0.0.10"))
        with pytest.raises(SystemExit):
            self._plan(c, 4, {})

    def test_local_sh_names_the_files_and_their_hosts(self) -> None:
        plan = vm_cluster.OstFilePlan(
            hosts={4: "co2-oss1", 5: "co2-oss2"}, size_kb=300 * 1024
        )
        text = vm_cluster.generate_local_sh(
            self._two_oss(),
            disk_sizes={"co2-oss1": 500 << 20, "co2-oss2": 500 << 20},
            ost_count=5,
            ost_files=plan,
        )
        assert "OSTCOUNT=${_ltvm_env_OSTCOUNT:-5}" in text
        assert f"OSTSIZE={300 * 1024}" in text
        assert "OSTSIZE=512000" not in text
        assert "OSTDEV3=/dev/vdb" in text
        assert f"OSTDEV4={deploy.OST_FILE_DIR}/ost4" in text
        assert "ost4_HOST=co2-oss1" in text
        assert f"OSTDEV5={deploy.OST_FILE_DIR}/ost5" in text
        assert "ost5_HOST=co2-oss2" in text

    def test_fewer_than_the_disks(self) -> None:
        text = vm_cluster.generate_local_sh(self._two_oss(), ost_count=1)
        assert "OSTCOUNT=${_ltvm_env_OSTCOUNT:-1}" in text

    def test_cluster_records_its_count(self, tmp_path: Path) -> None:
        with patch("ltvm_pkg.vm_state.SOCKETS", tmp_path):
            ClusterInfo(name="c8", nodes=[], ost_count=8).save()
            assert ClusterInfo.load("c8").ost_count == 8
            ClusterInfo(name="c0", nodes=[]).save()
            assert ClusterInfo.load("c0").ost_count == 0


class TestLnetFromProbe:
    """lnet_from_probe names the net and NID the MGS actually runs."""

    def test_no_extra_nic_runs_tcp_on_mgmt(self) -> None:
        lnet = vm_cluster.lnet_from_probe(_probe("tcp0(eth0)"))
        assert lnet.nettype == "tcp"
        assert lnet.mgsnid == "192.168.122.10@tcp"

    def test_extra_tcp_nic_carries_the_nid(self) -> None:
        # With any --nic, the boot emitter takes eth0 out of LNet, so
        # the mgmt address is not a NID of the MGS.
        lnet = vm_cluster.lnet_from_probe(_probe("tcp0(eth1)"))
        assert lnet.mgsnid == "172.16.100.23@tcp"

    def test_softroce_runs_o2ib(self) -> None:
        lnet = vm_cluster.lnet_from_probe(_probe("o2ib0(eth1)"))
        assert lnet.nettype == "o2ib"
        assert lnet.mgsnid == "172.16.100.23@o2ib"

    def test_first_net_and_first_interface_win(self) -> None:
        lnet = vm_cluster.lnet_from_probe(_probe("tcp0(eth1,eth0),o2ib0(eth0)"))
        assert lnet.mgsnid == "172.16.100.23@tcp"

    def test_nonzero_net_index_is_kept(self) -> None:
        lnet = vm_cluster.lnet_from_probe(_probe("tcp1(eth1)"))
        assert lnet.nettype == "tcp1"
        assert lnet.mgsnid == "172.16.100.23@tcp1"

    def test_no_lnet_conf_is_tcp_on_eth0(self) -> None:
        lnet = vm_cluster.lnet_from_probe(f"--\n{_ADDRS}")
        assert lnet.mgsnid == "192.168.122.10@tcp"

    def test_unresolved_passthrough_is_refused(self) -> None:
        with pytest.raises(ValueError, match="@ib-of-eth1"):
            vm_cluster.lnet_from_probe(_probe("o2ib0(@ib-of-eth1))"))

    def test_ipv6_takes_the_global_address(self) -> None:
        lnet = vm_cluster.lnet_from_probe(_probe("tcp0(eth1)"), "ipv6")
        assert lnet.mgsnid == "fd17:2016:1000:f100:f172:f016:f100:f023@tcp"
        assert lnet.force_large_nid

    def test_ipv4_does_not_force_large_nids(self) -> None:
        lnet = vm_cluster.lnet_from_probe(_probe("tcp0(eth1)"), "ipv4")
        assert not lnet.force_large_nid

    def test_ipv6_on_o2ib_is_refused(self) -> None:
        with pytest.raises(ValueError, match="only supported by tcp"):
            vm_cluster.lnet_from_probe(_probe("o2ib0(eth1)"), "ipv6")

    def test_ipv6_on_mgmt_is_refused(self) -> None:
        # eth0 is IPv4 only; its link-local address is not a NID.
        probe = _probe("tcp0(eth0)") + (
            "2: eth0    inet6 fe80::1/64 scope link \\  valid_lft forever\n"
        )
        with pytest.raises(ValueError, match="no global IPv6 address on eth0"):
            vm_cluster.lnet_from_probe(probe, "ipv6")

    def test_local_sh_forces_large_nids_for_ipv6_only(self) -> None:
        c = _cluster(("n-mds", ["mgs", "mds"], 1, 0, "192.168.122.10"))
        v6 = vm_cluster.lnet_from_probe(_probe("tcp0(eth1)"), "ipv6")
        assert "FORCE_LARGE_NID=true" in vm_cluster.generate_local_sh(
            c, lnet=v6
        )
        # An explicit false, so a true from an earlier deploy cannot stay.
        assert "FORCE_LARGE_NID=false" in vm_cluster.generate_local_sh(c)

    def test_cluster_records_its_family(self, tmp_path: Path) -> None:
        with patch("ltvm_pkg.vm_state.SOCKETS", tmp_path):
            ClusterInfo(name="c6", nodes=[], ip_family="ipv6").save()
            assert ClusterInfo.load("c6").ip_family == "ipv6"
            ClusterInfo(name="c4", nodes=[]).save()
            assert ClusterInfo.load("c4").ip_family == ""

    def test_generate_local_sh_writes_the_probed_net(self) -> None:
        c = _cluster(("n-mds", ["mgs", "mds"], 1, 0, "192.168.122.10"))
        lnet = vm_cluster.lnet_from_probe(_probe("o2ib0(eth1)"))
        text = vm_cluster.generate_local_sh(c, lnet=lnet)
        assert "NETTYPE=o2ib" in text
        assert "MGSNID=172.16.100.23@o2ib" in text


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


class TestClusterBlockKeepsTheStockLocalSh:
    """The cluster settings go into the tree's own cfg/local.sh, after each
    node's disk block, instead of replacing the file.  The replacement
    carried only what someone had noticed missing: without TSTUSR,
    sanity-quota died at load in reset_quota_settings() ("clear quota for
    [type:-u name:] failed"), and before that RUNAS was missing too."""

    STOCK = (
        "FSNAME=${FSNAME:-lustre}\n"
        "mds_HOST=${mds_HOST:-$(hostname)}\n"
        "MDSCOUNT=${MDSCOUNT:-1}\n"
        'TSTUSR=${TSTUSR:-"quota_usr"}\n'
        'TSTUSR2=${TSTUSR2:-"quota_2usr"}\n'
        "if [ $UID -ne 0 ]; then\n"
        '\tRUNAS_ID="$UID"\n'
        "else\n"
        "\tRUNAS_ID=${RUNAS_ID:-500}\n"
        "fi\n"
    )
    DISK_BLOCK = (
        "\n# --- VM disk configuration (generated by ltvm deploy) ---\n"
        "OSTCOUNT=3\n"
        "OSTDEV1=/dev/vdb\n"
        "# --- END VM disk configuration ---\n"
    )

    def _cluster(self) -> ClusterInfo:
        return _cluster(
            ("co2-mds", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("co2-oss", ["oss"], 0, 3, "10.0.0.11"),
            ("co2-client", ["client"], 0, 0, "10.0.0.12"),
        )

    def _write(self, cfg: Path, local_sh: str) -> None:
        """Run the script _write_cluster_local_sh sends, against ``cfg``."""
        sent: dict[str, str] = {}

        def argv(ip: str, script: str, **kw: object) -> list[str]:
            sent["script"] = script
            return ["true"]

        with (
            patch.object(vm_cluster, "sshpass_ssh_argv", side_effect=argv),
            patch.object(vm_cluster.subprocess, "run") as run,
        ):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._write_cluster_local_sh(
                "co2-oss", "10.0.0.11", local_sh, []
            )
        script = (
            sent["script"]
            .replace("/usr/lib64/lustre/tests/cfg/local.sh", str(cfg))
            .replace("/usr/lib64/lustre/tests/cfg", str(cfg.parent))
        )
        subprocess.run(
            ["bash", "-c", script], input=local_sh, text=True, check=True
        )

    def _source(
        self, cfg: Path, *names: str, env: dict[str, str] | None = None
    ) -> list[str]:
        echo = "; ".join(f'echo "${name}"' for name in names)
        out = subprocess.run(
            ["bash", "-c", f". {cfg}; {echo}"],
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": "/usr/bin:/bin", **(env or {})},
        )
        return out.stdout.splitlines()

    def test_the_stock_settings_stay_and_the_cluster_wins(
        self, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "cfg" / "local.sh"
        cfg.parent.mkdir()
        cfg.write_text(self.STOCK + self.DISK_BLOCK)

        self._write(cfg, vm_cluster.generate_local_sh(self._cluster()))

        tstusr, tstusr2, mds_host, ostcount = self._source(
            cfg, "TSTUSR", "TSTUSR2", "mds_HOST", "OSTCOUNT"
        )
        assert (tstusr, tstusr2) == ("quota_usr", "quota_2usr")
        assert mds_host == "co2-mds"
        assert ostcount == "3"
        generated = vm_cluster.generate_local_sh(self._cluster())
        assert "CLEANUP_DM_DEV" not in generated
        assert "CLIENTCOUNT=${CLIENTCOUNT:-" in generated
        body = cfg.read_text()
        assert body.index("VM disk configuration") < body.index(
            "Cluster configuration"
        )

    def test_a_second_deploy_rewrites_the_block_in_place(
        self, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "cfg" / "local.sh"
        cfg.parent.mkdir()
        cfg.write_text(self.STOCK + self.DISK_BLOCK)
        local_sh = vm_cluster.generate_local_sh(self._cluster())

        self._write(cfg, local_sh)
        self._write(cfg, local_sh)

        body = cfg.read_text()
        assert body.count("# --- Cluster configuration") == 1
        assert body.count("# --- END cluster configuration") == 1
        assert body.count(ENV_SAVE_BEGIN) == 1
        assert body.startswith(ENV_SAVE_BEGIN)
        assert self.STOCK in body

    def test_the_block_is_valid_shell(self) -> None:
        r = subprocess.run(
            ["bash", "-n"],
            input=vm_cluster.generate_local_sh(self._cluster()),
            text=True,
            capture_output=True,
        )
        assert r.returncode == 0, r.stderr

    def test_a_node_with_no_local_sh_gets_the_block(
        self, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "cfg" / "local.sh"
        cfg.parent.mkdir()
        self._write(cfg, vm_cluster.generate_local_sh(self._cluster()))
        assert "mds_HOST=co2-mds" in cfg.read_text()

    def test_the_environment_sets_the_counts(self, tmp_path: Path) -> None:
        """MDSCOUNT=1 sanity.sh runs one MDT, as on a stock local.sh,
        though the cluster's block comes after the stock defaults."""
        cfg = tmp_path / "cfg" / "local.sh"
        cfg.parent.mkdir()
        cfg.write_text(self.STOCK + self.DISK_BLOCK)
        self._write(cfg, vm_cluster.generate_local_sh(self._cluster()))

        assert self._source(cfg, "OSTCOUNT") == ["3"]
        assert self._source(cfg, "OSTCOUNT", env={"OSTCOUNT": "1"}) == ["1"]
        # The framework sources the file again; the count stays put.
        out = subprocess.run(
            ["bash", "-c", f'. {cfg}; . {cfg}; echo "$OSTCOUNT"'],
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": "/usr/bin:/bin", "OSTCOUNT": "1"},
        )
        assert out.stdout.split() == ["1"]


@needs_gnu_sed
class TestGuestHostsBlock:
    """Every member's name goes into each member's /etc/hosts."""

    MEMBERS = [("co2-mds", "10.0.0.10"), ("co2-client", "10.0.0.12")]

    def _write(self, hosts: Path, block: str) -> None:
        """Run the script _write_cluster_hosts sends, against ``hosts``."""
        sent: dict[str, str] = {}

        def argv(ip: str, script: str, **kw: object) -> list[str]:
            sent["script"] = script
            return ["true"]

        with (
            patch.object(vm_cluster, "sshpass_ssh_argv", side_effect=argv),
            patch.object(vm_cluster.subprocess, "run") as run,
        ):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._write_cluster_hosts("co2-mds", "10.0.0.10", block)
        script = sent["script"].replace("/etc/hosts", str(hosts))
        subprocess.run(
            ["bash", "-c", script], input=block, text=True, check=True
        )

    def test_block_names_every_member(self) -> None:
        block = vm_cluster.cluster_hosts_block("co2", self.MEMBERS)
        assert "10.0.0.10\tco2-mds\n" in block
        assert "10.0.0.12\tco2-client\n" in block

    def test_written_once_and_rewritten_in_place(self, tmp_path: Path) -> None:
        hosts = tmp_path / "hosts"
        stock = "127.0.0.1   localhost\n"
        hosts.write_text(stock + "10.9.9.9 other\n")
        self._write(hosts, vm_cluster.cluster_hosts_block("co2", self.MEMBERS))
        moved = [("co2-mds", "10.0.0.20"), ("co2-client", "10.0.0.12")]
        self._write(hosts, vm_cluster.cluster_hosts_block("co2", moved))
        body = hosts.read_text()
        assert body.startswith(stock + "10.9.9.9 other\n")
        assert body.count(vm_cluster.GUEST_HOSTS_BEGIN) == 1
        assert body.count(vm_cluster.GUEST_HOSTS_END) == 1
        assert "10.0.0.20\tco2-mds" in body
        assert "10.0.0.10" not in body

    def test_no_trailing_newline_is_not_joined(self, tmp_path: Path) -> None:
        hosts = tmp_path / "hosts"
        hosts.write_text("127.0.0.1 localhost")
        self._write(hosts, vm_cluster.cluster_hosts_block("co2", self.MEMBERS))
        lines = hosts.read_text().splitlines()
        assert lines[0] == "127.0.0.1 localhost"
        assert lines[1].startswith(vm_cluster.GUEST_HOSTS_BEGIN)


class TestClusterJsonOutput:
    """`--json` was accepted on every `cluster` subcommand and read by
    none of them, so a machine consumer got a human table and exit 0.

    `cluster status` and `cluster list` are what an agent polls, and
    `cluster exec` is what it collects results from; those three emit
    documents now.  `create`/`destroy`/`deploy` still stream human
    progress under --json, and `ssh` execs an interactive session where
    the flag has no meaning.
    """

    def _args(self, **over):
        import argparse

        base = dict(name="testc", json=True)
        base.update(over)
        return argparse.Namespace(**base)

    def test_status_emits_a_document(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        c = _cluster(
            ("testc-mds", ["mgs", "mds"], 1, 0, "192.168.100.11"),
            ("testc-oss1", ["oss"], 0, 2, "192.168.100.12"),
        )
        monkeypatch.setattr(ClusterInfo, "load", staticmethod(lambda n: c))
        monkeypatch.setattr(vm_cluster, "_node_state", lambda n: "up")

        vm_cluster.cmd_cluster_status(self._args())
        doc = json.loads(capsys.readouterr().out)

        assert doc["cluster"] == "testc"
        assert [n["name"] for n in doc["nodes"]] == [
            "testc-mds",
            "testc-oss1",
        ]
        mds = doc["nodes"][0]
        assert mds["roles"] == ["mgs", "mds"]
        assert mds["state"] == "up"
        assert mds["ip"] == "192.168.100.11"
        assert doc["nodes"][1]["ost_disks"] == 2

    def test_status_human_output_is_unchanged(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = _cluster(("testc-mds", ["mgs", "mds"], 1, 0, "192.168.100.11"))
        monkeypatch.setattr(ClusterInfo, "load", staticmethod(lambda n: c))
        monkeypatch.setattr(vm_cluster, "_node_state", lambda n: "down")

        vm_cluster.cmd_cluster_status(self._args(json=False))
        out = capsys.readouterr().out

        assert "cluster: testc" in out
        assert "testc-mds" in out
        assert "stopped" in out
        assert "mgs+mds" in out
        assert "mdt=1" in out

    def test_list_emits_a_document(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        c = _cluster(("testc-mds", ["mgs", "mds"], 1, 0, "192.168.100.11"))
        monkeypatch.setattr(
            ClusterInfo, "all_names", staticmethod(lambda: ["testc"])
        )
        monkeypatch.setattr(ClusterInfo, "load", staticmethod(lambda n: c))
        monkeypatch.setattr(vm_cluster, "_node_state", lambda n: "up")

        vm_cluster.cmd_cluster_list(self._args())
        doc = json.loads(capsys.readouterr().out)

        assert doc["clusters"][0]["cluster"] == "testc"
        assert doc["clusters"][0]["nodes"][0]["state"] == "up"

    def test_list_with_no_clusters_is_still_a_document(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The empty case printed "(no clusters)" regardless of --json."""
        import json

        monkeypatch.setattr(ClusterInfo, "all_names", staticmethod(lambda: []))
        vm_cluster.cmd_cluster_list(self._args())
        assert json.loads(capsys.readouterr().out) == {"clusters": []}

    def test_list_reports_a_broken_cluster_file(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        def boom(n):
            raise ValueError("bad node list")

        monkeypatch.setattr(
            ClusterInfo, "all_names", staticmethod(lambda: ["broken"])
        )
        monkeypatch.setattr(ClusterInfo, "load", staticmethod(boom))

        vm_cluster.cmd_cluster_list(self._args())
        doc = json.loads(capsys.readouterr().out)
        assert doc["clusters"][0]["error"] == "bad node list"

    def test_node_state_survives_a_corrupt_info(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """VMInfo.load raises ValueError by design on a truncated .info;
        one casualty must not take out a whole listing."""
        from ltvm_pkg.vm_state import VMInfo

        def boom(name):
            raise ValueError("invalid literal for int()")

        monkeypatch.setattr(VMInfo, "load", staticmethod(boom))
        assert vm_cluster._node_state("whatever") == "corrupt"


# Taken before conftest's autouse patch replaces it for every test.
_DEPLOYED_FSTYPE = vm_cluster._deployed_fstype


class TestDeployedFstype:
    """The backend of a cluster deployed before its record kept one."""

    def _ask(self, local_sh: str) -> str | None:
        cluster = ClusterInfo(
            name="co3", nodes=[{"name": "co3-c1", "roles": ["client"]}]
        )
        with (
            patch.object(
                vm_cluster.VMInfo, "load", return_value=MagicMock(ip="10.0.0.9")
            ),
            patch(
                "ltvm_pkg.deploy.run_ssh",
                return_value=MagicMock(returncode=0, stdout=local_sh),
            ) as ssh,
        ):
            got = _DEPLOYED_FSTYPE(cluster, "rhel")
        assert ssh.call_args.args[0] == "10.0.0.9"
        return got

    def _block(self, line: str) -> str:
        return (
            f"FSTYPE=${{FSTYPE:-ldiskfs}}\n{vm_cluster.CLUSTER_BLOCK_BEGIN} "
            f"---\n{line}\n{vm_cluster.CLUSTER_BLOCK_END} ---\n"
        )

    def test_old_pinned_form(self) -> None:
        assert self._ask(self._block("FSTYPE=zfs")) == "zfs"

    def test_environment_form(self) -> None:
        line = "FSTYPE=${_ltvm_env_FSTYPE:-ldiskfs}"
        assert self._ask(self._block(line)) == "ldiskfs"

    def test_no_block_is_unknown(self) -> None:
        assert self._ask("FSTYPE=${FSTYPE:-zfs}\n") is None


_ENSURE_PDSH = vm_cluster._ensure_pdsh


class TestEnsurePdsh:
    """cluster deploy installs pdsh on nodes whose image lacks it."""

    def _run(self, os_family: str, rc: int = 0) -> tuple[tuple, str]:
        with patch.object(
            vm_cluster,
            "run_ssh",
            return_value=MagicMock(returncode=rc, stdout="", stderr="no"),
        ) as ssh:
            got = _ENSURE_PDSH("co2-mds", "10.0.0.2", os_family)
        return got, ssh.call_args.args[1]

    def test_rhel_falls_back_to_epel10(self) -> None:
        """Regression: rocky10 images had no pdsh, and cluster deploy
        --mount failed in llmount.sh with rc 127."""
        got, script = self._run("rhel")
        assert got == ("co2-mds", 0, "no")
        assert script.startswith("rpm -q pdsh pdsh-rcmd-ssh")
        assert "repo=epel-10&arch=$basearch" in script

    def test_debian(self) -> None:
        _, script = self._run("debian")
        assert "apt-get install -y -q pdsh" in script

    def test_failure_is_reported(self) -> None:
        got, _ = self._run("rhel", rc=1)
        assert got[1] == 1

    def test_dockerfiles_install_it(self) -> None:
        for target in ("rocky10", "mainline", "rocky10-16k"):
            text = Path(f"targets/{target}/image.Dockerfile").read_text()
            assert "repo=epel-10&arch=$basearch" in text, target
            assert "pdsh pdsh-rcmd-ssh" in text, target


class TestMixedFamilies:
    """Nodes that install Lustre in different places share one cluster
    block: rocky or SLES servers with Debian or Ubuntu clients."""

    def _link(
        self, families: dict[str, str]
    ) -> tuple[list[str], dict[str, str]]:
        nodes = [ClusterNode(name=n, roles=["client"]) for n in families]
        ips = {n: f"10.0.0.{i}" for i, n in enumerate(families, 1)}
        by_ip: dict[str, str] = {}

        def _ssh(ip, cmd, **kw):
            by_ip[ip] = cmd
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(vm_cluster, "run_ssh", side_effect=_ssh):
            failed = vm_cluster._link_lustre_libdirs(nodes, ips, families)
        return failed, {n: by_ip.get(ips[n], "") for n in families}

    def test_one_place_needs_no_links(self) -> None:
        failed, cmds = self._link({"co3-mds": "rhel", "co3-c1": "suse"})
        assert failed == []
        assert cmds == {"co3-mds": "", "co3-c1": ""}

    def test_each_node_links_the_other_place_to_its_own(self) -> None:
        failed, cmds = self._link({"co3-mds": "rhel", "co3-c1": "debian"})
        assert failed == []
        assert "ln -sfn /usr/lib64/lustre /usr/lib/lustre" in cmds["co3-mds"]
        assert "mkdir -p /usr/lib64 && " in cmds["co3-c1"]
        assert "ln -sfn /usr/lib/lustre /usr/lib64/lustre" in cmds["co3-c1"]
        # A real directory there is another install, not replaced.
        assert "[ ! -L /usr/lib64/lustre ]" in cmds["co3-c1"]

    def test_cluster_block_family_is_stable(self) -> None:
        families = {"co3-mds": "rhel", "co3-c1": "debian", "co3-c2": "suse"}
        assert vm_cluster._cluster_os_family(families) == "debian"
        assert vm_cluster._cluster_os_family({}) == "rhel"


class TestNodeKernels:
    def _nodes(self) -> list[ClusterNode]:
        return [
            ClusterNode(name="co8-srv", roles=["mgs", "mds", "oss"]),
            ClusterNode(name="co8-c", roles=["client"]),
        ]

    def test_a_kernel_of_the_nodes_own_target(self) -> None:
        got = vm_cluster._node_kernels(
            self._nodes(),
            [None, "6.4-sles15sp6"],
            {"co8-srv": None, "co8-c": "opensuse156"},
            "rocky9",
        )
        assert got == {"co8-srv": None, "co8-c": "6.4-sles15sp6"}

    def test_another_targets_kernel_is_refused_up_front(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            vm_cluster._node_kernels(
                self._nodes(),
                [None, "5.14-rhel9.5"],
                {"co8-srv": None, "co8-c": "opensuse156"},
                "rocky9",
            )
        err = capsys.readouterr().err
        assert "opensuse156 has no kernel '5.14-rhel9.5'" in err
