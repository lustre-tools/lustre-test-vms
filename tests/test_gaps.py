"""High-level gap-filling tests for lustre-test-vms-v2.

Covers behaviours that were absent from existing tests:

1. ClusterInfo save/load round-trip (JSON on-disk format)
2. ClusterInfo.load on corrupt state raises RuntimeError with hint
3. lustre_build._needs_reconfigure stamp-driven logic
4. lustre_build._kernel_changed distinguishes "never built" from "changed"
5. lustre_build.lustre_status integrates stamp + ko discovery correctly
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ltvm_pkg.vm_state import ClusterInfo, ClusterNotFound, ClusterNode


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_sockets(tmp_path: Path) -> Path:
    with patch("ltvm_pkg.vm_state.SOCKETS", tmp_path):
        yield tmp_path


def _make_cluster(tmp_sockets: Path) -> ClusterInfo:
    """Return a ClusterInfo whose SOCKETS path is tmp_sockets."""
    return ClusterInfo(
        name="co2",
        nodes=[
            {
                "name": "co2-mds",
                "roles": ["mgs", "mds"],
                "mdt_disks": 1,
                "ost_disks": 0,
                "ip": "10.0.0.10",
            },
            {
                "name": "co2-oss",
                "roles": ["oss"],
                "mdt_disks": 0,
                "ost_disks": 3,
                "ip": "10.0.0.11",
            },
            {
                "name": "co2-client",
                "roles": ["client"],
                "mdt_disks": 0,
                "ost_disks": 0,
                "ip": "10.0.0.12",
            },
        ],
    )


# ── ClusterInfo save/load round-trip ─────────────────────────────────────────


class TestClusterInfoRoundTrip:
    """ClusterInfo survives a save/load cycle with all fields intact."""

    def test_full_roundtrip(self, tmp_sockets: Path) -> None:
        c = _make_cluster(tmp_sockets)
        c.save()

        loaded = ClusterInfo.load("co2")
        assert loaded.name == "co2"
        assert len(loaded.nodes) == 3

        nodes = loaded.get_nodes()
        mds = next(n for n in nodes if n.is_mds)
        oss = next(n for n in nodes if n.is_oss)
        client = next(n for n in nodes if n.is_client)

        assert mds.name == "co2-mds"
        assert mds.is_mgs
        assert mds.mdt_disks == 1
        assert oss.ost_disks == 3
        assert client.name == "co2-client"

    def test_save_produces_valid_json(self, tmp_sockets: Path) -> None:
        c = _make_cluster(tmp_sockets)
        c.save()
        raw = (tmp_sockets / "co2.cluster").read_text()
        data = json.loads(raw)  # must not raise
        assert data["name"] == "co2"
        assert isinstance(data["nodes"], list)

    def test_load_nonexistent_raises_cluster_not_found(
        self, tmp_sockets: Path
    ) -> None:
        with pytest.raises(ClusterNotFound):
            ClusterInfo.load("phantom")

    def test_all_names_lists_saved_clusters(self, tmp_sockets: Path) -> None:
        c = _make_cluster(tmp_sockets)
        c.save()
        assert "co2" in ClusterInfo.all_names()

    def test_overwrite_updates_nodes(self, tmp_sockets: Path) -> None:
        """Saving a second time with different nodes replaces the first write."""
        c = _make_cluster(tmp_sockets)
        c.save()

        c2 = ClusterInfo(
            name="co2",
            nodes=[
                {
                    "name": "co2-mds",
                    "roles": ["mgs", "mds"],
                    "mdt_disks": 2,
                    "ost_disks": 0,
                    "ip": "10.0.0.10",
                },
            ],
        )
        c2.save()

        loaded = ClusterInfo.load("co2")
        assert len(loaded.nodes) == 1
        assert loaded.get_nodes()[0].mdt_disks == 2


class TestClusterInfoCorruptLoad:
    """ClusterInfo.load raises RuntimeError with an actionable message on corrupt state."""

    def test_corrupt_json_raises_runtime_error(
        self, tmp_sockets: Path
    ) -> None:
        (tmp_sockets / "bad.cluster").write_text("{not valid json")
        with pytest.raises(RuntimeError, match="cluster create"):
            ClusterInfo.load("bad")

    def test_truncated_file_raises_runtime_error(
        self, tmp_sockets: Path
    ) -> None:
        (tmp_sockets / "trunc.cluster").write_text("")
        with pytest.raises(RuntimeError, match="corrupt cluster state"):
            ClusterInfo.load("trunc")

    def test_error_names_the_file(self, tmp_sockets: Path) -> None:
        (tmp_sockets / "broken.cluster").write_text("{")
        with pytest.raises(RuntimeError) as exc_info:
            ClusterInfo.load("broken")
        assert "broken" in str(exc_info.value)


# ── lustre_build._needs_reconfigure ──────────────────────────────────────────


class TestNeedsReconfigure:
    """_needs_reconfigure drives when autogen+configure must re-run."""

    from ltvm_pkg import lustre_build

    def _write_kernel_release(self, build_tree: Path, kver: str) -> None:
        release = build_tree / "include" / "config" / "kernel.release"
        release.parent.mkdir(parents=True, exist_ok=True)
        release.write_text(kver + "\n")

    def test_no_configure_script_triggers_reconf(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import _needs_reconfigure

        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        build_tree.mkdir()
        # configure script missing
        assert _needs_reconfigure(lustre_tree, build_tree, force=False)

    def test_no_config_status_triggers_reconf(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import _needs_reconfigure

        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        (lustre_tree / "configure").write_text("#!/bin/sh\n")
        # config.status missing
        assert _needs_reconfigure(lustre_tree, build_tree, force=False)

    def test_no_stamp_triggers_reconf(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import _needs_reconfigure

        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        build_tree.mkdir()
        (lustre_tree / "configure").write_text("#!/bin/sh\n")
        (lustre_tree / "config.status").write_text("")
        self._write_kernel_release(build_tree, "5.14.0-611.el9.x86_64")
        # stamp missing for this target+arch
        assert _needs_reconfigure(lustre_tree, build_tree, force=False)

    def test_same_kernel_no_reconf(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import _needs_reconfigure

        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        build_tree.mkdir()
        (lustre_tree / "configure").write_text("#!/bin/sh\n")
        (lustre_tree / "config.status").write_text("")
        kver = "5.14.0-611.el9.x86_64"
        self._write_kernel_release(build_tree, kver)
        (lustre_tree / ".ltvm-kernel-rocky9-x86_64").write_text(kver)
        (lustre_tree / ".ltvm-server-rocky9-x86_64").write_text("True")
        assert not _needs_reconfigure(
            lustre_tree, build_tree, force=False, target="rocky9"
        )

    def test_kernel_change_triggers_reconf(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import _needs_reconfigure

        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        build_tree.mkdir()
        (lustre_tree / "configure").write_text("#!/bin/sh\n")
        (lustre_tree / "config.status").write_text("")
        self._write_kernel_release(build_tree, "5.14.0-999.el9.x86_64")
        # stamp has an older kernel
        (lustre_tree / ".ltvm-kernel-rocky9-x86_64").write_text(
            "5.14.0-611.el9.x86_64"
        )
        (lustre_tree / ".ltvm-server-rocky9-x86_64").write_text("True")
        assert _needs_reconfigure(
            lustre_tree, build_tree, force=False, target="rocky9"
        )

    def test_server_flag_change_triggers_reconf(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import _needs_reconfigure

        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        build_tree.mkdir()
        (lustre_tree / "configure").write_text("#!/bin/sh\n")
        (lustre_tree / "config.status").write_text("")
        kver = "5.14.0-611.el9.x86_64"
        self._write_kernel_release(build_tree, kver)
        (lustre_tree / ".ltvm-kernel-rocky9-x86_64").write_text(kver)
        # stamp says server=False, but caller passes enable_server=True
        (lustre_tree / ".ltvm-server-rocky9-x86_64").write_text("False")
        assert _needs_reconfigure(
            lustre_tree,
            build_tree,
            force=False,
            target="rocky9",
            enable_server=True,
        )

    def test_force_always_triggers_reconf(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import _needs_reconfigure

        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        build_tree.mkdir()
        (lustre_tree / "configure").write_text("#!/bin/sh\n")
        (lustre_tree / "config.status").write_text("")
        kver = "5.14.0-611.el9.x86_64"
        self._write_kernel_release(build_tree, kver)
        (lustre_tree / ".ltvm-kernel-rocky9-x86_64").write_text(kver)
        (lustre_tree / ".ltvm-server-rocky9-x86_64").write_text("True")
        # force=True overrides everything
        assert _needs_reconfigure(
            lustre_tree, build_tree, force=True, target="rocky9"
        )

    def test_stamp_suffix_separates_targets(self, tmp_path: Path) -> None:
        """Two different targets sharing a lustre_tree don't clobber each other's stamps."""
        from ltvm_pkg.lustre_build import _stamp_suffix

        assert _stamp_suffix("rocky9", "x86_64") != _stamp_suffix(
            "rocky10", "x86_64"
        )
        assert _stamp_suffix("rocky9", "x86_64") != _stamp_suffix(
            "rocky9", "aarch64"
        )


# ── lustre_build._kernel_changed ─────────────────────────────────────────────


class TestKernelChanged:
    def _write_kernel_release(self, build_tree: Path, kver: str) -> None:
        release = build_tree / "include" / "config" / "kernel.release"
        release.parent.mkdir(parents=True, exist_ok=True)
        release.write_text(kver + "\n")

    def test_no_stamp_returns_false(self, tmp_path: Path) -> None:
        """No previous stamp means 'never built', not 'changed'."""
        from ltvm_pkg.lustre_build import _kernel_changed

        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        build_tree.mkdir()
        assert not _kernel_changed(lustre_tree, build_tree, target="rocky9")

    def test_same_kernel_returns_false(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import _kernel_changed

        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        build_tree.mkdir()
        kver = "5.14.0-611.el9.x86_64"
        self._write_kernel_release(build_tree, kver)
        (lustre_tree / ".ltvm-kernel-rocky9-x86_64").write_text(kver)
        assert not _kernel_changed(lustre_tree, build_tree, target="rocky9")

    def test_different_kernel_returns_true(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import _kernel_changed

        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        build_tree.mkdir()
        self._write_kernel_release(build_tree, "5.14.0-999.el9.x86_64")
        (lustre_tree / ".ltvm-kernel-rocky9-x86_64").write_text(
            "5.14.0-611.el9.x86_64"
        )
        assert _kernel_changed(lustre_tree, build_tree, target="rocky9")


# ── lustre_build.lustre_status ────────────────────────────────────────────────


class TestLustreStatus:
    """lustre_status integrates stamp + staging ko discovery + stale flag."""

    def _write_kernel_release(self, build_tree: Path, kver: str) -> None:
        release = build_tree / "include" / "config" / "kernel.release"
        release.parent.mkdir(parents=True, exist_ok=True)
        release.write_text(kver + "\n")

    def _write_kos(self, staging: Path, count: int) -> None:
        staging.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (staging / f"mod{i}.ko").write_text("")

    def test_fresh_tree_reports_not_configured_and_stale(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg.lustre_build import lustre_status

        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        build_tree.mkdir()
        self._write_kernel_release(build_tree, "5.14.0-611.el9")

        status = lustre_status(lustre_tree, build_tree, target="rocky9")
        assert status["configured"] is False
        assert status["ko_count"] == 0
        assert status["built_against"] is None
        assert status["stale"] is True

    def test_ko_count_from_staging_dir(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import lustre_status, staging_path

        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        build_tree.mkdir()
        kver = "5.14.0-611.el9"
        self._write_kernel_release(build_tree, kver)

        staging = staging_path(lustre_tree, "rocky9", kernel=kver)
        self._write_kos(staging, 5)

        status = lustre_status(
            lustre_tree, build_tree, target="rocky9", kernel=kver
        )
        assert status["ko_count"] == 5

    def test_matching_stamp_marks_not_stale(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import lustre_status

        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        build_tree.mkdir()
        kver = "5.14.0-611.el9"
        self._write_kernel_release(build_tree, kver)
        (lustre_tree / "config.status").write_text("")
        (lustre_tree / ".ltvm-kernel-rocky9-x86_64").write_text(kver)

        status = lustre_status(lustre_tree, build_tree, target="rocky9")
        assert status["configured"] is True
        assert status["stale"] is False
        assert status["built_against"] == kver

    def test_stale_when_kernel_differs(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import lustre_status

        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        build_tree.mkdir()
        self._write_kernel_release(build_tree, "5.14.0-999.el9")
        (lustre_tree / "config.status").write_text("")
        # stamp says it was built against an older kernel
        (lustre_tree / ".ltvm-kernel-rocky9-x86_64").write_text(
            "5.14.0-611.el9"
        )

        status = lustre_status(lustre_tree, build_tree, target="rocky9")
        assert status["stale"] is True

    def test_ko_count_summed_across_kernels_when_no_kernel_given(
        self, tmp_path: Path
    ) -> None:
        """When kernel=None, ko_count sums all per-kernel staging dirs."""
        from ltvm_pkg.lustre_build import lustre_status, staging_path

        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        build_tree.mkdir()

        for k, n in [("5.14-rhel9.7", 3), ("5.14-rhel9.5", 2)]:
            s = staging_path(lustre_tree, "rocky9", kernel=k)
            self._write_kos(s, n)

        status = lustre_status(lustre_tree, build_tree, target="rocky9")
        assert status["ko_count"] == 5


# ── lustre_build.staging_path layout ─────────────────────────────────────────


class TestStagingPath:
    """staging_path encodes target/arch/kernel to prevent cross-arch clobber."""

    def test_path_under_lustre_tree(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import staging_path

        p = staging_path(tmp_path / "lustre", "rocky9", kernel="5.14-rhel9.7")
        assert str(p).startswith(str(tmp_path / "lustre"))

    def test_different_kernels_different_paths(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import staging_path

        p1 = staging_path(tmp_path, "rocky9", kernel="5.14-rhel9.7")
        p2 = staging_path(tmp_path, "rocky9", kernel="5.14-rhel9.5")
        assert p1 != p2

    def test_different_arches_different_paths(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import staging_path

        p1 = staging_path(tmp_path, "rocky9", "x86_64", kernel="5.14-rhel9.7")
        p2 = staging_path(
            tmp_path, "rocky9", "aarch64", kernel="5.14-rhel9.7"
        )
        assert p1 != p2

    def test_different_targets_different_paths(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import staging_path

        p1 = staging_path(tmp_path, "rocky9", kernel="5.14-rhel9.7")
        p2 = staging_path(tmp_path, "rocky10", kernel="5.14-rhel9.7")
        assert p1 != p2

    def test_path_structure(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import staging_path

        p = staging_path(
            tmp_path / "lustre", "rocky9", "x86_64", kernel="5.14-rhel9.7"
        )
        parts = p.parts
        assert ".ltvm-staging" in parts
        assert "rocky9" in parts
        assert "x86_64" in parts
        assert "5.14-rhel9.7" in parts


# ── lustre_build.read_staging_meta ───────────────────────────────────────────


class TestReadStagingMeta:
    """read_staging_meta tolerates corrupt/missing files."""

    def test_missing_returns_none(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import read_staging_meta

        assert read_staging_meta(tmp_path) is None

    def test_corrupt_returns_none(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import read_staging_meta

        (tmp_path / ".ltvm-staging-meta.json").write_text("{bad json")
        assert read_staging_meta(tmp_path) is None

    def test_non_dict_returns_none(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import read_staging_meta

        (tmp_path / ".ltvm-staging-meta.json").write_text("[1, 2, 3]")
        assert read_staging_meta(tmp_path) is None

    def test_valid_dict_returned(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import read_staging_meta

        data = {"kernel_version": "5.14.0-611.el9", "ko_count": 12}
        (tmp_path / ".ltvm-staging-meta.json").write_text(json.dumps(data))
        result = read_staging_meta(tmp_path)
        assert result == data
