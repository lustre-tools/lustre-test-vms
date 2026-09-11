"""ZFS's seams with the Lustre build, deploy, cluster and publish paths.

Separate from test_zfs.py, which covers the artifact itself.  What is
pinned down here is the plumbing: that --with-zfs actually reaches
configure, that the ZFS a VM receives is the one its Lustre was linked
against, and that ZFS never leaks into a build or a release that did
not ask for it.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import deploy as dp
from ltvm_pkg import lustre_build as lb
from ltvm_pkg import vm_cluster as vc

# ── the configure line ───────────────────────────────────


_CAPTURE_SEQ = itertools.count()


def _capture_container_build(tmp_path: Path, **kwargs):
    """Run _build_in_container far enough to capture the podman argv.

    Each call gets its own tree so a test can build twice (e.g. with
    and without --zfs) and compare.
    """
    root = tmp_path / f"run{next(_CAPTURE_SEQ)}"
    lustre_tree = root / "lustre"
    (lustre_tree / "lustre" / "kernel_patches").mkdir(parents=True)
    (lustre_tree / "lnet").mkdir()
    (lustre_tree / "configure.ac").write_text("")
    build_tree = root / "kernel"
    (build_tree / "include" / "config").mkdir(parents=True)
    (build_tree / "include" / "config" / "kernel.release").write_text("1.2.3")
    (build_tree / "Module.symvers").write_text("")

    captured: dict = {}

    def _fake_podman(cmd, **kw):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=1)  # stop before the meta write

    with (
        patch.object(lb, "run_podman_with_cleanup", side_effect=_fake_podman),
        patch.object(lb, "_show_configure_log"),
    ):
        with pytest.raises(RuntimeError):
            lb._build_in_container(
                lustre_tree,
                build_tree,
                "ltvm-build-rocky9",
                "1.2.3",
                kwargs.pop("enable_server", True),
                kwargs.pop("extra_configure", None),
                4,
                kwargs.pop("force", False),
                **kwargs,
            )
    return captured["cmd"]


def _configure_line(script: str) -> str:
    """The ./configure invocation the generated build script runs."""
    for line in script.splitlines():
        if line.strip().startswith("./configure"):
            return line.strip()
    raise AssertionError("no ./configure line in build script")


class TestConfigureLine:
    def test_with_zfs_absent_by_default(self, tmp_path: Path) -> None:
        """The premise of ZFS being opt-in: no flag, no mention."""
        cmd = _capture_container_build(tmp_path)
        script = cmd[-1]
        assert "--with-zfs" not in script
        assert not any(":/zfs:ro" in a for a in cmd)

    def test_with_zfs_reaches_configure_and_mounts(
        self, tmp_path: Path
    ) -> None:
        zfs_src = tmp_path / "zfsart" / "src"
        zfs_src.mkdir(parents=True)
        cmd = _capture_container_build(tmp_path, zfs_src=zfs_src)
        script = cmd[-1]
        assert "--with-zfs=/zfs" in script
        assert f"{zfs_src}:/zfs:ro" in cmd

    def test_zfs_mount_precedes_the_image_tag(self, tmp_path: Path) -> None:
        """podman run's argv order is load-bearing: everything after
        the image tag is the container's own command line, so a -v
        inserted there would be passed to bash instead of podman."""
        zfs_src = tmp_path / "zfsart" / "src"
        zfs_src.mkdir(parents=True)
        cmd = _capture_container_build(tmp_path, zfs_src=zfs_src)
        assert cmd[-3] == "ltvm-build-rocky9"
        assert cmd[-2] == "-c"
        assert cmd.index(f"{zfs_src}:/zfs:ro") < len(cmd) - 3

    def test_extra_configure_can_still_override(self, tmp_path: Path) -> None:
        """autoconf takes the last spelling, and extra_configure is
        appended after --with-zfs -- same contract as --with-o2ib."""
        zfs_src = tmp_path / "zfsart" / "src"
        zfs_src.mkdir(parents=True)
        cmd = _capture_container_build(
            tmp_path, zfs_src=zfs_src, extra_configure=["--with-zfs=no"]
        )
        script = cmd[-1]
        assert script.index("--with-zfs=/zfs") < script.index("--with-zfs=no")

    def test_zfs_version_is_in_the_configure_stamp(
        self, tmp_path: Path
    ) -> None:
        """Regression: --with-zfs=/zfs is spelled identically for every
        version, because /zfs is a mount point.  Hashing only the
        configure line therefore let `--zfs-version 2.3.4` on a tree
        last built against 2.4.0 skip the reconfigure and compile
        osd-zfs against 2.3.4 headers with a config.h probed against
        2.4.0 -- "too many arguments to function 'dmu_write_by_dnode'".
        """
        zfs_src = tmp_path / "zfsart" / "src"
        zfs_src.mkdir(parents=True)
        a = _capture_container_build(
            tmp_path, zfs_src=zfs_src, zfs_version="2.4.0"
        )[-1]
        b = _capture_container_build(
            tmp_path, zfs_src=zfs_src, zfs_version="2.3.4"
        )[-1]
        # Same configure line...
        assert _configure_line(a) == _configure_line(b)
        # ...but a different stamp, so the reconfigure still happens.
        line = _configure_line(a)
        assert lb.configure_stamp_hash(line, "2.4.0") != (
            lb.configure_stamp_hash(line, "2.3.4")
        )

    def test_stamp_hash_still_tracks_the_flags(self) -> None:
        assert lb.configure_stamp_hash("./configure --a") != (
            lb.configure_stamp_hash("./configure --b")
        )

    def test_stamp_hash_stable_without_zfs(self) -> None:
        """A non-ZFS build's stamp must still be the bare hash of the
        command line -- otherwise adding the ZFS parameter invalidates
        the stamp in every Lustre tree already on disk and costs
        everyone a full reconfigure + rebuild per target for nothing."""
        line = "./configure --with-linux=/kernel --enable-server"
        assert (
            lb.configure_stamp_hash(line)
            == hashlib.sha256(line.encode()).hexdigest()
        )
        assert lb.configure_stamp_hash(line, None) == (
            lb.configure_stamp_hash(line)
        )
        assert lb.configure_stamp_hash(line, "") == (
            lb.configure_stamp_hash(line)
        )

    def test_toggling_zfs_forces_a_reconfigure(self, tmp_path: Path) -> None:
        """--with-zfs is in the configure-flags hash, so turning it on
        cannot silently reuse a config.status that lacks it."""
        zfs_src = tmp_path / "zfsart" / "src"
        zfs_src.mkdir(parents=True)
        plain = _configure_line(_capture_container_build(tmp_path)[-1])
        withz = _configure_line(
            _capture_container_build(tmp_path, zfs_src=zfs_src)[-1]
        )
        assert plain != withz
        assert hashlib.sha256(plain.encode()).hexdigest() != (
            hashlib.sha256(withz.encode()).hexdigest()
        )


def _ko_sweep(script: str) -> str:
    """The stale-.ko cleanup line the generated build script runs."""
    for line in script.splitlines():
        if line.startswith("find .") and "'*.ko'" in line:
            return line
    raise AssertionError("no .ko sweep in build script")


class TestStagingNotClobbered:
    """Regression (pre-existing, surfaced by switching kernels for ZFS):
    the stale-.ko sweep runs from the Lustre tree root, and staging
    lives inside that tree at .ltvm-staging/<target>/<arch>/<kernel>/.
    An unqualified `find . -name '*.ko' -delete` therefore deleted the
    staged modules of every other kernel and target in the tree.  They
    are never rebuilt -- the next build writes only its own staging dir
    -- so it surfaced much later as a deploy or publish finding a
    staging tree with directories and no modules."""

    def test_sweep_excludes_staging(self, tmp_path: Path) -> None:
        cmd = _capture_container_build(tmp_path, force=True)
        sweep = _ko_sweep(cmd[-1])
        assert "-not -path './.ltvm-staging/*'" in sweep

    def test_sweep_does_not_use_prune(self, tmp_path: Path) -> None:
        """-delete turns on -depth, which makes -prune a silent no-op."""
        cmd = _capture_container_build(tmp_path, force=True)
        sweep = _ko_sweep(cmd[-1])
        assert "-prune" not in sweep


class TestBuildLustreGuards:
    def _tree(self, tmp_path: Path) -> tuple[Path, Path]:
        lustre_tree = tmp_path / "lustre"
        (lustre_tree / "lustre" / "kernel_patches").mkdir(parents=True)
        build_tree = tmp_path / "kernel"
        build_tree.mkdir()
        (build_tree / "Module.symvers").write_text("")
        return lustre_tree, build_tree

    def test_unconfigured_zfs_tree_rejected(self, tmp_path: Path) -> None:
        """An unpacked-but-unbuilt tree has no zfs_config.h; Lustre's
        LB_ZFS would silently set enable_zfs=no and build no OSD."""
        lustre_tree, build_tree = self._tree(tmp_path)
        zfs_src = tmp_path / "zfs"
        zfs_src.mkdir()
        with pytest.raises(ValueError, match="not a configured ZFS tree"):
            lb.build_lustre(lustre_tree, build_tree, zfs_src=zfs_src)

    def test_zfs_with_client_only_rejected(self, tmp_path: Path) -> None:
        lustre_tree, build_tree = self._tree(tmp_path)
        zfs_src = tmp_path / "zfs"
        zfs_src.mkdir()
        (zfs_src / "zfs_config.h").write_text("")
        with pytest.raises(ValueError, match="server backend"):
            lb.build_lustre(
                lustre_tree, build_tree, zfs_src=zfs_src, enable_server=False
            )


# ── deploy ───────────────────────────────────────────────


def _vm() -> MagicMock:
    vm = MagicMock()
    vm.ip = "192.168.100.99"
    vm.name = "co7-zfs"
    vm.mdt_disks = 0
    vm.ost_disks = 0
    vm.disk_size = 0
    return vm


class TestDeployStreaming:
    def _staging(self, tmp_path: Path, name: str) -> Path:
        d = tmp_path / name
        (d / "lib" / "modules").mkdir(parents=True)
        return d

    def test_zfs_streams_before_lustre(self, tmp_path: Path) -> None:
        """One depmod covers both trees, and osd_zfs.ko depends on
        zfs.ko -- so ZFS has to already be on disk when it runs."""
        lustre = self._staging(tmp_path, "lustre")
        zfs = self._staging(tmp_path, "zfs")
        order: list[str] = []
        with (
            patch.object(
                dp,
                "_stream_tree",
                side_effect=lambda vm, tree, **kw: order.append(tree.name),
            ),
            patch.object(dp, "run_ssh") as ssh,
            patch.object(dp, "verify_deployed_modules"),
        ):
            ssh.return_value = SimpleNamespace(
                returncode=0, stdout="", stderr=""
            )
            dp.deploy_to_vm(_vm(), lustre, zfs_staging=zfs)
        assert order == ["zfs", "lustre"]

    def test_no_zfs_stream_by_default(self, tmp_path: Path) -> None:
        lustre = self._staging(tmp_path, "lustre")
        order: list[str] = []
        with (
            patch.object(
                dp,
                "_stream_tree",
                side_effect=lambda vm, tree, **kw: order.append(tree.name),
            ),
            patch.object(dp, "run_ssh") as ssh,
            patch.object(dp, "verify_deployed_modules"),
        ):
            ssh.return_value = SimpleNamespace(
                returncode=0, stdout="", stderr=""
            )
            dp.deploy_to_vm(_vm(), lustre)
        assert order == ["lustre"]

    def test_userspace_only_skips_zfs(self, tmp_path: Path) -> None:
        """--userspace-only ships no modules; the ZFS already on the VM
        is the one these tools were built against."""
        lustre = self._staging(tmp_path, "lustre")
        zfs = self._staging(tmp_path, "zfs")
        order: list[str] = []
        with (
            patch.object(
                dp,
                "_stream_tree",
                side_effect=lambda vm, tree, **kw: order.append(tree.name),
            ),
            patch.object(dp, "run_ssh") as ssh,
        ):
            ssh.return_value = SimpleNamespace(
                returncode=0, stdout="", stderr=""
            )
            dp.deploy_to_vm(_vm(), lustre, zfs_staging=zfs, userspace_only=True)
        assert order == ["lustre"]

    def test_missing_zfs_staging_is_an_error(self, tmp_path: Path) -> None:
        lustre = self._staging(tmp_path, "lustre")
        with (
            patch.object(dp, "_stream_tree"),
            patch.object(dp, "run_ssh") as ssh,
            patch.object(dp, "verify_deployed_modules"),
        ):
            ssh.return_value = SimpleNamespace(
                returncode=0, stdout="", stderr=""
            )
            with pytest.raises(RuntimeError, match="ZFS staging"):
                dp.deploy_to_vm(_vm(), lustre, zfs_staging=tmp_path / "nope")


class TestRetireStaleZfs:
    """Regression: a running zfs.ko is matched to osd_zfs.ko by symbol
    version, and installing a new zfs.ko on disk does not touch the one
    already in the kernel.  Deploying ZFS 2.3.4 onto a VM running 2.4.0
    therefore produced a wall of "osd_zfs: disagrees about version of
    symbol ..." at mount time, several steps from the deploy that caused
    it."""

    def _script(self) -> str:
        with patch.object(dp, "run_ssh") as ssh:
            ssh.return_value = SimpleNamespace(
                returncode=0, stdout="", stderr=""
            )
            dp.retire_stale_zfs(_vm())
        return ssh.call_args[0][1]

    def test_compares_loaded_against_on_disk(self) -> None:
        script = self._script()
        assert "/sys/module/zfs/version" in script
        assert "modinfo -F version zfs" in script

    def test_noop_when_versions_agree(self) -> None:
        """Unloading ZFS on every deploy would be gratuitous -- and on a
        VM with Lustre up, destructive."""
        script = self._script()
        assert '[ "$loaded" != "$ondisk" ] || exit 0' in script

    def test_noop_when_zfs_is_not_loaded(self) -> None:
        script = self._script()
        assert '[ -n "$loaded" ] || exit 0' in script

    def test_unloads_through_the_dependency_chain(self) -> None:
        """zfs.ko cannot go while a pool holds it or osd_zfs sits on
        top, so both have to be cleared first."""
        script = self._script()
        assert script.index("zpool export") < script.index("rmmod osd_zfs")
        assert script.index("rmmod osd_zfs") < script.index("modprobe -r zfs")

    def test_warns_when_it_cannot_unload(self, capsys) -> None:
        """The deploy itself succeeded; stopping Lustre is the caller's
        call, so this warns rather than raising."""
        with patch.object(dp, "run_ssh") as ssh:
            ssh.return_value = SimpleNamespace(
                returncode=0,
                stdout="retiring loaded ZFS 2.4.0 for 2.3.4\n2.4.0-1\n",
                stderr="",
            )
            dp.retire_stale_zfs(_vm())
        err = capsys.readouterr().err
        assert "still has ZFS 2.4.0-1 loaded" in err
        assert "--cleanup" in err

    def test_silent_when_it_worked(self, capsys) -> None:
        with patch.object(dp, "run_ssh") as ssh:
            ssh.return_value = SimpleNamespace(
                returncode=0,
                stdout="retiring loaded ZFS 2.4.0 for 2.3.4\n",
                stderr="",
            )
            dp.retire_stale_zfs(_vm())
        assert capsys.readouterr().err == ""

    def test_called_on_a_zfs_deploy(self, tmp_path: Path) -> None:
        lustre = tmp_path / "lustre"
        (lustre / "lib" / "modules").mkdir(parents=True)
        zfs = tmp_path / "zfs"
        (zfs / "lib" / "modules").mkdir(parents=True)
        with (
            patch.object(dp, "_stream_tree"),
            patch.object(dp, "verify_deployed_modules"),
            patch.object(dp, "retire_stale_zfs") as retire,
            patch.object(dp, "run_ssh") as ssh,
        ):
            ssh.return_value = SimpleNamespace(
                returncode=0, stdout="", stderr=""
            )
            dp.deploy_to_vm(_vm(), lustre, zfs_staging=zfs)
        retire.assert_called_once()

    def test_not_called_without_zfs(self, tmp_path: Path) -> None:
        lustre = tmp_path / "lustre"
        (lustre / "lib" / "modules").mkdir(parents=True)
        with (
            patch.object(dp, "_stream_tree"),
            patch.object(dp, "verify_deployed_modules"),
            patch.object(dp, "retire_stale_zfs") as retire,
            patch.object(dp, "run_ssh") as ssh,
        ):
            ssh.return_value = SimpleNamespace(
                returncode=0, stdout="", stderr=""
            )
            dp.deploy_to_vm(_vm(), lustre)
        retire.assert_not_called()


class TestConfigureFstype:
    def _run(self, fstype: str) -> str:
        with patch.object(dp, "run_ssh") as ssh:
            ssh.return_value = SimpleNamespace(
                returncode=0, stdout="", stderr=""
            )
            dp.configure_fstype("10.0.0.1", fstype)
        return ssh.call_args[0][1]

    def test_writes_fstype_block(self) -> None:
        script = self._run("zfs")
        assert "FSTYPE=zfs" in script
        assert "cfg/local.sh" in script

    def test_replaces_its_own_block(self) -> None:
        """Re-deploying, or switching backends, must not stack blocks --
        cfg/local.sh is sourced, so the file would keep growing and the
        last one silently wins."""
        script = self._run("ldiskfs")
        assert script.startswith("sed -i '/^# --- FSTYPE (generated by ltvm")
        assert "END FSTYPE" in script

    def test_rejects_unknown_fstype(self) -> None:
        with pytest.raises(ValueError, match="unsupported fstype"):
            dp.configure_fstype("10.0.0.1", "btrfs")

    def test_deploy_writes_fstype_last(self, tmp_path: Path) -> None:
        """After the disk block, so a reader sees both and the later
        assignment is the effective one."""
        lustre = tmp_path / "lustre"
        (lustre / "lib" / "modules").mkdir(parents=True)
        vm = _vm()
        vm.mdt_disks = 1
        vm.ost_disks = 1
        calls: list[str] = []
        with (
            patch.object(dp, "_stream_tree"),
            patch.object(dp, "verify_deployed_modules"),
            patch.object(dp, "run_ssh") as ssh,
        ):
            ssh.side_effect = lambda ip, script, **kw: (
                calls.append(script),
                SimpleNamespace(returncode=0, stdout="", stderr=""),
            )[1]
            dp.deploy_to_vm(vm, lustre, fstype="zfs")
        disk_idx = next(i for i, c in enumerate(calls) if "OSTDEV1" in c)
        fst_idx = next(i for i, c in enumerate(calls) if "FSTYPE=zfs" in c)
        assert disk_idx < fst_idx


class TestMountCleanup:
    def _cleanup_script(self) -> str:
        vm = _vm()
        with (
            patch.object(dp.VMInfo, "load", return_value=vm),
            patch.object(dp, "run_ssh") as ssh,
        ):
            ssh.return_value = SimpleNamespace(
                returncode=0, stdout="", stderr=""
            )
            dp.lustre_mount_vm("co7-zfs", "rhel")
        return ssh.call_args_list[0][0][1]

    def test_zpools_are_exported(self) -> None:
        """An imported pool holds its vdev open, so the next format
        fails with "apparently in use by the system"."""
        script = self._cleanup_script()
        assert "zpool export" in script
        assert "zpool list" in script

    def test_sweep_is_unconditional(self) -> None:
        """Regression: gating this on fstype == "zfs" broke the
        direction that matters most.  Switching a VM from ZFS back to
        ldiskfs is exactly when the leftover pools are still imported
        and exactly when a zfs-only guard would skip them -- mkfs.lustre
        then fails on /dev/vdb.  It runs as unconditionally as the
        dmsetup sweep beside it."""
        script = self._cleanup_script()
        assert "command -v zpool" in script
        assert "dmsetup remove_all" in script
        # No fstype branch left to get wrong.
        import inspect

        sig = inspect.signature(dp.lustre_mount_vm)
        assert "fstype" not in sig.parameters

    def test_zfs_module_is_dropped_after_lustre_rmmod(self) -> None:
        """llmount.sh reloads zfs.ko, and this is what makes a
        newly-deployed ZFS of a different version actually take
        effect.  Order matters: osd_zfs sits on top of it."""
        script = self._cleanup_script()
        assert script.index("lustre_rmmod") < script.index("modprobe -r zfs")

    def test_export_not_destroy(self) -> None:
        """formatall reformats with --reformat anyway; destroying would
        discard a pool someone deliberately left in place."""
        assert "zpool destroy" not in self._cleanup_script()


# ── cluster ──────────────────────────────────────────────


def _cluster() -> MagicMock:
    node = SimpleNamespace(
        name="co2-mds",
        ip="192.168.100.10",
        roles=["mgs", "mds"],
        mdt_disks=1,
        ost_disks=0,
        is_mgs=True,
        is_mds=True,
    )
    oss = SimpleNamespace(
        name="co2-oss",
        ip="192.168.100.11",
        roles=["oss"],
        mdt_disks=0,
        ost_disks=2,
        is_mgs=False,
        is_mds=False,
    )
    c = MagicMock()
    c.name = "co2"
    c.mgs_node.return_value = node
    c.mds_nodes.return_value = [node]
    c.oss_nodes.return_value = [oss]
    c.client_nodes.return_value = []
    return c


class TestClusterLocalSh:
    def test_defaults_to_ldiskfs(self) -> None:
        assert "FSTYPE=ldiskfs" in vc.generate_local_sh(_cluster())

    def test_zfs_selected(self) -> None:
        out = vc.generate_local_sh(_cluster(), fstype="zfs")
        assert "FSTYPE=zfs" in out
        assert "FSTYPE=ldiskfs" not in out

    def test_vdev_mapping_is_unchanged_for_zfs(self) -> None:
        """The /dev/vd* devices stay: for ZFS the framework reads them
        as the vdevs to build pools on, and derives the dataset names
        itself.  Rewriting them to dataset names would break that."""
        out = vc.generate_local_sh(_cluster(), fstype="zfs")
        assert "MDSDEV1=/dev/vdb" in out
        assert "OSTDEV1=/dev/vdb" in out

    def test_rejects_unknown_fstype(self) -> None:
        with pytest.raises(ValueError, match="unsupported fstype"):
            vc.generate_local_sh(_cluster(), fstype="btrfs")


# ── publish ──────────────────────────────────────────────


class TestPublish:
    def test_snapshot_zfs_version_prefers_snapshot_meta(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg.release_package import _snapshot_zfs_version

        d = tmp_path / "lustre-artifacts"
        d.mkdir()
        (d / ".ltvm-snapshot.json").write_text(
            json.dumps({"zfs_version": "2.4.0"})
        )
        (d / ".ltvm-staging-meta.json").write_text(
            json.dumps({"zfs_version": "2.2.7"})
        )
        assert _snapshot_zfs_version(d) == "2.4.0"

    def test_falls_back_to_staging_meta(self, tmp_path: Path) -> None:
        """A snapshot taken before the snapshot-meta field existed still
        carries the staging meta the rsync copied in."""
        from ltvm_pkg.release_package import _snapshot_zfs_version

        d = tmp_path / "lustre-artifacts"
        d.mkdir()
        (d / ".ltvm-snapshot.json").write_text(json.dumps({"ko_count": 3}))
        (d / ".ltvm-staging-meta.json").write_text(
            json.dumps({"zfs_version": "2.2.7"})
        )
        assert _snapshot_zfs_version(d) == "2.2.7"

    def test_none_when_built_without_zfs(self, tmp_path: Path) -> None:
        from ltvm_pkg.release_package import _snapshot_zfs_version

        d = tmp_path / "lustre-artifacts"
        d.mkdir()
        (d / ".ltvm-staging-meta.json").write_text(
            json.dumps({"zfs_version": None})
        )
        assert _snapshot_zfs_version(d) is None

    def test_asset_name_carries_kernel_and_version(self) -> None:
        """Two ZFS versions can coexist under one kernel, and a fetcher
        must land on the one its Lustre was linked against."""
        from ltvm_pkg.release_package import _zfs_asset_name

        n = _zfs_asset_name("rocky9", "x86_64", "5.14.0-611.el9", "2.4.0")
        assert n == "zfs-rocky9-x86_64-5.14.0-611.el9-2.4.0.tar.zst"
        other = _zfs_asset_name("rocky9", "x86_64", "5.14.0-611.el9", "2.2.7")
        assert n != other

    def test_kernel_asset_excludes_zfs(self) -> None:
        """Without this every base fetcher pays for a ZFS they never
        asked for -- the same bug mofed-kmods hit."""
        src = Path("ltvm_pkg/release_package.py").read_text()
        kern_block = src[
            src.index("# ---- kernel asset") : src.index("# ---- image asset")
        ]
        assert '"--exclude",\n            f"{kernel_rel}/zfs",' in kern_block


# ── the CLI's --zfs / --fstype interaction ───────────────


class TestCliFlagSemantics:
    """`--zfs` on a *deploy* means "run this VM on ZFS"; on a *build* it
    only means "build the OSD".  The asymmetry is deliberate, so pin it.
    """

    def _resolve(self, **kw) -> tuple[bool, str | None]:
        """Mirror of the resolution in cmd_deploy / cmd_cluster_deploy."""
        args = SimpleNamespace(
            **{"zfs": False, "zfs_version": None, "fstype": None, **kw}
        )
        fstype = args.fstype
        want = bool(args.zfs) or bool(args.zfs_version) or fstype == "zfs"
        if want and fstype is None:
            fstype = "zfs"
        return want, fstype

    def test_plain_deploy_untouched(self) -> None:
        assert self._resolve() == (False, None)

    def test_zfs_implies_fstype_zfs(self) -> None:
        assert self._resolve(zfs=True) == (True, "zfs")

    def test_zfs_version_implies_zfs(self) -> None:
        assert self._resolve(zfs_version="2.2.7") == (True, "zfs")

    def test_fstype_zfs_implies_zfs(self) -> None:
        assert self._resolve(fstype="zfs") == (True, "zfs")

    def test_explicit_fstype_wins(self) -> None:
        """Staging ZFS on a VM you want to keep running ldiskfs."""
        assert self._resolve(zfs=True, fstype="ldiskfs") == (True, "ldiskfs")

    def test_build_lustre_zfs_does_not_imply_an_fstype(self) -> None:
        from ltvm_pkg.cli.build import _resolve_zfs

        tc = MagicMock()
        args = SimpleNamespace(zfs=False, zfs_version=None)
        assert _resolve_zfs(tc, args, None, True) == (None, None, None)

    def test_resolve_zfs_refuses_client_targets(self) -> None:
        """--with-zfs needs --enable-server to have an OSD to build."""
        from ltvm_pkg.cli.build import _resolve_zfs
        from ltvm_pkg.target_config import LustreMode

        tc = MagicMock()
        tc.name = "ubuntu2404"
        tc.lustre_mode = LustreMode.CLIENT
        args = SimpleNamespace(zfs=True, zfs_version=None, force=False)
        _src, _ver, err = _resolve_zfs(tc, args, None, True)
        assert err is not None and "client target" in err
        # ...and it points at the thing that does work.
        assert "ltvm build zfs" in err

    def test_build_zfs_allows_client_targets(self) -> None:
        """Building ZFS against a kernel is not server-specific.  Only
        `--with-zfs` on a Lustre build is, and that gate is separate --
        conflating them blocked `ltvm build zfs ubuntu2404` for no
        reason."""
        src = Path("ltvm_pkg/cli/build.py").read_text()
        body = src[src.index("def cmd_build_zfs") :]
        body = body[: body.index("def cmd_build_mofed_kmods")]
        assert "LustreMode.CLIENT" not in body


class TestUserspaceOnlyGuard:
    def test_zfs_with_userspace_only_is_refused(self) -> None:
        """Honouring only the --fstype half would pin the VM to a
        backend whose modules the deploy never shipped."""
        src = Path("ltvm_pkg/cli/deploy.py").read_text()
        assert "if want_zfs and userspace_only:" in src
        assert "incompatible" in src


class TestDeployStalenessOnZfsRequest:
    """A staging tree built without ZFS has the same configure hash as
    the source tree's stamp, so --zfs would otherwise be a silent no-op.
    """

    def test_zfs_version_is_recorded_in_staging_meta(self) -> None:
        src = Path("ltvm_pkg/lustre_build.py").read_text()
        assert '"zfs_version": zfs_version,' in src

    def test_deploy_rebuilds_when_staging_lacks_zfs(self) -> None:
        src = Path("ltvm_pkg/cli/deploy.py").read_text()
        block = src[src.index("staged_zfs = meta.get") :][:600]
        assert "if want_zfs:" in block
        assert "if not staged_zfs:" in block

    def test_deploy_rebuilds_on_version_mismatch(self) -> None:
        src = Path("ltvm_pkg/cli/deploy.py").read_text()
        assert "staged_zfs != zfs_version_arg" in src
