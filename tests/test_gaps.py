"""High-level gap-filling tests for lustre-test-vms-v2.

Covers behaviours that were absent from existing tests:

1. ClusterInfo save/load round-trip (JSON on-disk format)
2. ClusterInfo.load on corrupt state raises RuntimeError with hint
3. lustre_build._needs_reconfigure stamp-driven logic
4. lustre_build._kernel_changed distinguishes "never built" from "changed"
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ltvm_pkg.vm_state import ClusterInfo, ClusterNotFound

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

    def test_corrupt_json_raises_runtime_error(self, tmp_sockets: Path) -> None:
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


# ── lustre_build._needs_reconfigure: kernel config ──────────────────────────


class TestReconfigureOnKernelConfig:
    """A kernel rebuilt with another config keeps its release string, but
    configure probes the config (FS_VERITY decides whether ldiskfs builds
    verity.c), so the tree must reconfigure."""

    def _configured_tree(self, tmp_path: Path, config: str) -> tuple:
        lustre_tree = tmp_path / "lustre"
        build_tree = tmp_path / "build"
        lustre_tree.mkdir()
        release = build_tree / "include" / "config" / "kernel.release"
        release.parent.mkdir(parents=True)
        release.write_text("6.12.107-lustre\n")
        (build_tree / ".config").write_text(config)
        for name in ("configure", "config.status"):
            (lustre_tree / name).write_text("")
        suffix = "debian13-x86_64"
        (lustre_tree / f".ltvm-kernel-{suffix}").write_text("6.12.107-lustre\n")
        (lustre_tree / f".ltvm-server-{suffix}").write_text("True\n")
        return lustre_tree, build_tree

    def _stamp_config(self, lustre_tree: Path, build_tree: Path) -> None:
        from ltvm_pkg.lustre_build import _hash_file

        (lustre_tree / ".ltvm-kconfig-debian13-x86_64").write_text(
            _hash_file(build_tree / ".config") + "\n"
        )

    def test_same_config_does_not_reconfigure(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import _needs_reconfigure

        lustre_tree, build_tree = self._configured_tree(
            tmp_path, "CONFIG_FS_VERITY=y\n"
        )
        self._stamp_config(lustre_tree, build_tree)
        assert not _needs_reconfigure(
            lustre_tree, build_tree, False, target="debian13"
        )

    def test_changed_config_reconfigures(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import _needs_reconfigure

        lustre_tree, build_tree = self._configured_tree(
            tmp_path, "# CONFIG_FS_VERITY is not set\n"
        )
        self._stamp_config(lustre_tree, build_tree)
        (build_tree / ".config").write_text("CONFIG_FS_VERITY=y\n")
        assert _needs_reconfigure(
            lustre_tree, build_tree, False, target="debian13"
        )

    def test_no_config_stamp_reconfigures(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import _needs_reconfigure

        lustre_tree, build_tree = self._configured_tree(
            tmp_path, "CONFIG_FS_VERITY=y\n"
        )
        assert _needs_reconfigure(
            lustre_tree, build_tree, False, target="debian13"
        )


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
        p2 = staging_path(tmp_path, "rocky9", "aarch64", kernel="5.14-rhel9.7")
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

    def test_base_variant_unchanged(self, tmp_path: Path) -> None:
        """Default variant keeps the pre-variant layout so existing
        staging dirs on disk stay addressable."""
        from ltvm_pkg.lustre_build import staging_path

        p_default = staging_path(tmp_path, "rocky9", kernel="5.14-rhel9.7")
        p_base = staging_path(
            tmp_path, "rocky9", kernel="5.14-rhel9.7", variant="base"
        )
        assert p_default == p_base
        assert "mofed" not in p_base.parts

    def test_variant_is_a_sibling_of_the_base_kernel_dir(
        self, tmp_path: Path
    ) -> None:
        """Non-base variants sit beside the base dir, not inside it.

        Nesting defeated the coexistence it existed for: the build
        clears its DESTDIR with `rm -rf /staging/*`, which for a base
        build is the parent of a nested variant dir -- so building base
        deleted the variant's staging outright.
        """
        from ltvm_pkg.lustre_build import staging_path

        p_base = staging_path(
            tmp_path, "rocky9", kernel="5.14-rhel9.7", variant="base"
        )
        p_mofed = staging_path(
            tmp_path, "rocky9", kernel="5.14-rhel9.7", variant="mofed"
        )
        assert p_mofed.parent == p_base.parent
        assert p_mofed == p_base.parent / "5.14-rhel9.7__mofed"
        # The base wipe must not be able to reach the variant.
        assert p_base not in p_mofed.parents


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


# ── lustre_build: what the container build records and mounts ──────────────


def _container_build(tmp_path: Path, on_podman=None) -> tuple:
    """Run _build_in_container with a podman that installs a staging."""
    from types import SimpleNamespace

    from ltvm_pkg import lustre_build as lb

    lustre_tree = tmp_path / "lustre"
    (lustre_tree / "lustre" / "kernel_patches").mkdir(parents=True)
    (lustre_tree / "lnet").mkdir()
    (lustre_tree / "configure.ac").write_text("")
    build_tree = tmp_path / "kernel" / "build-tree"
    (build_tree / "include" / "config").mkdir(parents=True)
    (build_tree / "include" / "config" / "kernel.release").write_text("1.2.3")
    (build_tree / "Module.symvers").write_text("")
    (build_tree / ".config").write_text(
        "CONFIG_INFINIBAND=m\nCONFIG_INFINIBAND_ADDR_TRANS=y\n"
        "CONFIG_DEBUG_INFO_BTF_MODULES=y\n"
    )
    captured: dict = {}

    def fake_podman(cmd, **kw):
        captured["cmd"] = cmd
        staging = Path(
            next(a for a in cmd if a.endswith(":/staging")).rsplit(":", 1)[0]
        )
        ko = staging / "lib" / "modules" / "1.2.3" / "extra" / "lnet.ko"
        ko.parent.mkdir(parents=True)
        ko.write_text("")
        (staging / "sbin").mkdir(parents=True)
        (staging / "sbin" / "mount.lustre").write_text("")
        if on_podman:
            on_podman(build_tree)
        return SimpleNamespace(returncode=0)

    with (
        patch.object(lb, "run_podman_with_cleanup", side_effect=fake_podman),
        patch.object(lb, "_show_configure_log"),
    ):
        lb._build_in_container(
            lustre_tree,
            build_tree,
            "ltvm-build-rocky9",
            "1.2.3",
            True,
            None,
            4,
            False,
            target="rocky9",
        )
    return lustre_tree, build_tree, captured["cmd"]


class TestContainerBuildStamps:
    def test_config_stamped(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import _hash_file

        lustre_tree, build_tree, _ = _container_build(tmp_path)
        stamp = lustre_tree / ".ltvm-kconfig-rocky9-x86_64"
        assert stamp.read_text().strip() == _hash_file(build_tree / ".config")

    def test_config_changed_during_build_not_stamped(
        self, tmp_path: Path
    ) -> None:
        def rebuild_kernel(build_tree: Path) -> None:
            (build_tree / ".config").write_text("CONFIG_INFINIBAND=n\n")

        with pytest.raises(RuntimeError, match="rebuilt during this build"):
            _container_build(tmp_path, rebuild_kernel)
        assert not (
            tmp_path / "lustre" / ".ltvm-kconfig-rocky9-x86_64"
        ).exists()


def fake_vmlinux(path: Path, build_id: bytes) -> None:
    """A little-endian ELF64 file whose one section is a GNU build ID note."""
    import struct

    note = struct.pack("<III", 4, len(build_id), 3) + b"GNU\0" + build_id
    note += b"\0" * (-len(note) % 4)
    shoff = 64 + len(note)
    hdr = bytearray(64)
    hdr[:6] = b"\x7fELF\x02\x01"
    hdr[40:48] = struct.pack("<Q", shoff)
    hdr[58:62] = struct.pack("<HH", 64, 2)
    null = bytes(64)
    sh = bytearray(64)
    sh[4:8] = struct.pack("<I", 7)
    sh[24:40] = struct.pack("<QQ", 64, len(note))
    path.write_bytes(bytes(hdr) + note + null + bytes(sh))


class TestVmlinuxForBtf:
    def test_vmlinux_mounted_and_linked(self, tmp_path: Path) -> None:
        (tmp_path / "kernel").mkdir()
        vmlinux = tmp_path / "kernel" / "vmlinux"
        fake_vmlinux(vmlinux, b"\x01" * 20)
        _, build_tree, cmd = _container_build(tmp_path)
        assert f"{vmlinux}:/vmlinux:ro" in cmd
        assert cmd.index(f"{vmlinux}:/vmlinux:ro") < len(cmd) - 3
        assert os.readlink(build_tree / "vmlinux") == "/vmlinux"

    def test_new_vmlinux_relinks_modules(self, tmp_path: Path) -> None:
        (tmp_path / "kernel").mkdir()
        fake_vmlinux(tmp_path / "kernel" / "vmlinux", b"\x01" * 20)
        lustre_tree, build_tree, _ = _container_build(tmp_path)
        stamp = lustre_tree / ".ltvm-vmlinux-rocky9-x86_64"
        from ltvm_pkg.lustre_build import vmlinux_id

        assert stamp.read_text().strip() == vmlinux_id(build_tree)
        ko = lustre_tree / "lnet" / "lnet.ko"
        ko.write_text("")
        staged = lustre_tree / ".ltvm-staging" / "x" / "lnet.ko"
        staged.parent.mkdir(parents=True)
        staged.write_text("")
        fake_vmlinux(tmp_path / "kernel" / "vmlinux", b"\x02" * 20)
        from ltvm_pkg import lustre_build as lb

        with (
            patch.object(lb, "run_podman_with_cleanup") as podman,
            patch.object(lb, "_show_configure_log"),
        ):
            podman.return_value = SimpleNamespace(returncode=1)
            with pytest.raises(RuntimeError):
                lb._build_in_container(
                    lustre_tree,
                    build_tree,
                    "ltvm-build-rocky9",
                    "1.2.3",
                    True,
                    None,
                    4,
                    False,
                    target="rocky9",
                )
        assert not ko.exists()
        assert staged.exists()

    def test_unwritable_build_tree_no_mount(self, tmp_path: Path) -> None:
        from ltvm_pkg import lustre_build as lb

        kdir = tmp_path / "kernel"
        (kdir / "build-tree").mkdir(parents=True)
        (kdir / "build-tree" / ".config").write_text(
            "CONFIG_DEBUG_INFO_BTF_MODULES=y\n"
        )
        fake_vmlinux(kdir / "vmlinux", b"\x01" * 20)
        with patch.object(
            Path, "symlink_to", side_effect=PermissionError
        ) as link:
            assert lb._vmlinux_for_btf(kdir / "build-tree") is None
        link.assert_called_once()

    def test_vmlinux_without_build_id_no_mount(self, tmp_path: Path) -> None:
        (tmp_path / "kernel").mkdir()
        (tmp_path / "kernel" / "vmlinux").write_text("no ELF notes")
        _, build_tree, cmd = _container_build(tmp_path)
        assert not any(a.endswith(":/vmlinux:ro") for a in cmd)

    def test_no_vmlinux_no_mount(self, tmp_path: Path) -> None:
        _, build_tree, cmd = _container_build(tmp_path)
        assert not any(a.endswith(":/vmlinux:ro") for a in cmd)
        assert not (build_tree / "vmlinux").is_symlink()


class TestElfBuildId:
    def test_matches_readelf(self) -> None:
        import shutil
        import subprocess

        from ltvm_pkg.lustre_build import elf_build_id

        exe = Path(shutil.which("ls") or "/bin/ls").resolve()
        if not shutil.which("readelf"):
            pytest.skip("no readelf to compare with")
        out = subprocess.run(
            ["readelf", "-n", str(exe)], capture_output=True, text=True
        ).stdout
        ids = [
            x.split(":")[-1].strip()
            for x in out.splitlines()
            if "Build ID" in x
        ]
        if not ids:
            pytest.skip(f"{exe} has no build ID")
        assert elf_build_id(exe) == ids[0]

    def test_not_elf(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import elf_build_id

        (tmp_path / "x").write_text("not an ELF file")
        assert elf_build_id(tmp_path / "x") == ""
        (tmp_path / "t").write_bytes(b"\x7fELF")
        assert elf_build_id(tmp_path / "t") == ""
        fake_vmlinux(tmp_path / "v", b"\xab" * 20)
        assert elf_build_id(tmp_path / "v") == "ab" * 20
        assert elf_build_id(tmp_path / "missing") == ""


class TestStagingFitsVmlinux:
    def test_release_check_refuses_another_vmlinux(
        self, tmp_path: Path
    ) -> None:
        import json

        from ltvm_pkg.lustre_build import _hash_file, vmlinux_id
        from ltvm_pkg.release_build import lustre_fits_kernel

        kdir = tmp_path / "k"
        (kdir / "build-tree").mkdir(parents=True)
        (kdir / "build-tree" / "Module.symvers").write_text("syms\n")
        (kdir / "vmlinux").write_text("v1")
        staged = tmp_path / "staged"
        staged.mkdir()
        meta = {
            "module_symvers_sha256": _hash_file(
                kdir / "build-tree" / "Module.symvers"
            ),
            "kernel_vmlinux_id": vmlinux_id(kdir / "build-tree"),
        }
        (staged / ".ltvm-staging-meta.json").write_text(json.dumps(meta))
        assert lustre_fits_kernel(kdir, staged)
        meta["kernel_vmlinux_id"] = "1:2"
        (staged / ".ltvm-staging-meta.json").write_text(json.dumps(meta))
        assert not lustre_fits_kernel(kdir, staged)
        meta["kernel_vmlinux_id"] = ""
        (staged / ".ltvm-staging-meta.json").write_text(json.dumps(meta))
        assert lustre_fits_kernel(kdir, staged)
