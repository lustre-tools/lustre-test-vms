"""Tests for ltvm clean (top-level smart prune; cmd_prune).

Distinct from cmd_clean (the `target clean` per-target wipe) which is
covered by test_build_commands.py and test_ltvm_cli.py.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from ltvm_pkg.cli import EXIT_OK


def _make_kernel_dir(
    arch_dir: Path,
    full_name: str,
    *,
    built_at: datetime | None = None,
    size_bytes: int = 1024,
) -> Path:
    """Create a fake kernel dir with a meta.json + a payload file."""
    d = arch_dir / "kernels" / full_name
    d.mkdir(parents=True, exist_ok=True)
    meta = {"target": "rocky9", "kernel_version": full_name}
    if built_at is not None:
        meta["built_at"] = built_at.isoformat()
    (d / "meta.json").write_text(json.dumps(meta))
    # Pad with a payload so size accounting is non-trivial.
    (d / "vmlinuz").write_bytes(b"x" * size_bytes)
    return d


def _make_image_dir(
    arch_dir: Path,
    kernel_full_name: str,
    *,
    variant: str | None = None,
    build_date: datetime | None = None,
    size_bytes: int = 2048,
) -> Path:
    base = arch_dir / "images" / kernel_full_name
    d = base / variant if variant else base
    d.mkdir(parents=True, exist_ok=True)
    meta = {"target": "rocky9", "kernel_name": kernel_full_name}
    if build_date is not None:
        meta["build_date"] = build_date.isoformat()
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "base.ext4").write_bytes(b"x" * size_bytes)
    return d


def _patch_paths(tmp_targets: Path):
    """Patch ARTIFACTS_DIR/TARGETS_DIR/TARGETS_YAML to the tmp tree."""
    import ltvm_pkg.target_config as cfg

    return [
        patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
        patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"),
        patch.object(
            cfg,
            "TARGETS_YAML",
            tmp_targets / "targets" / "targets.yaml",
        ),
    ]


def _run_prune(
    tmp_targets: Path,
    *,
    target: str | None = None,
    arch: str | None = None,
    apply: bool = False,
    keep: int = 1,
    older_than: str | None = None,
    force: bool = False,
    use_json: bool = False,
    user_zfs: bool = False,
) -> int:
    from ltvm_pkg.cli import cmd_prune

    args = argparse.Namespace(
        target=target,
        arch=arch,
        apply=apply,
        keep=keep,
        older_than=older_than,
        force=force,
        json=use_json,
        user_zfs=user_zfs,
    )
    with (
        _patch_paths(tmp_targets)[0],
        _patch_paths(tmp_targets)[1],
        _patch_paths(tmp_targets)[2],
    ):
        return cmd_prune(args)


class TestCmdPrune:
    def test_empty_artifacts_reports_nothing(
        self,
        tmp_targets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = _run_prune(tmp_targets)
        assert rc == EXIT_OK
        assert "Nothing to prune" in capsys.readouterr().out

    def test_supersession_within_group(
        self,
        tmp_targets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Two builds of the same short prefix: the older one is
        flagged superseded; the latest stays."""
        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        old = _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-503.40.1.el9_7")
        new = _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-611.49.1.el9_7")

        rc = _run_prune(tmp_targets, target="rocky9")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert str(old) in out
        assert str(new) not in out
        assert "superseded" in out
        assert "Re-run with --apply" in out
        # Dry-run -- nothing actually deleted.
        assert old.exists()
        assert new.exists()

    def test_supersession_goes_by_version_not_string(
        self,
        tmp_targets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """As strings 553.155.1 sorts below 553.89.1, which marked the
        newer, published kernel superseded."""
        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        old = _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-553.89.1.el9_7")
        new = _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-553.155.1.el9_7")

        rc = _run_prune(tmp_targets, target="rocky9")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert str(old) in out
        assert str(new) not in out

    def test_apply_actually_deletes(
        self,
        tmp_targets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        old = _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-503.40.1.el9_7")
        new = _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-611.49.1.el9_7")

        rc = _run_prune(tmp_targets, target="rocky9", apply=True)
        assert rc == EXIT_OK
        assert not old.exists()
        assert new.exists()
        assert "Removed 1 entries" in capsys.readouterr().out

    def test_default_kernel_group_protected_with_one_entry(
        self,
        tmp_targets: Path,
    ) -> None:
        """A single build of the default kernel must never be pruned
        (the protected-group guard).  Without it, --keep 0 would orphan
        the user's only working setup."""
        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        only = _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-611.49.1.el9_7")

        rc = _run_prune(tmp_targets, target="rocky9", keep=0)
        assert rc == EXIT_OK
        assert only.exists()

    def test_off_list_group_swept(
        self,
        tmp_targets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A kernel whose short prefix is no longer in
        kernels.available is fully removed regardless of --keep."""
        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        # 5.14-rhel9.4 is not in the rocky9 fixture's available list.
        offlist = _make_kernel_dir(
            arch_dir, "5.14-rhel9.4-5.14.0-427.18.1.el9_4"
        )

        rc = _run_prune(tmp_targets, target="rocky9", apply=True)
        assert rc == EXIT_OK
        assert not offlist.exists()
        assert "off-list" in capsys.readouterr().out

    def test_orphan_image_removed(
        self,
        tmp_targets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An image dir with no matching kernel dir is an orphan and
        gets pruned even when no kernels need pruning."""
        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        # Kernel that survives:
        _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-611.49.1.el9_7")
        orphan = _make_image_dir(arch_dir, "5.14-rhel9.5-5.14.0-503.40.1.el9_5")

        rc = _run_prune(tmp_targets, target="rocky9", apply=True)
        assert rc == EXIT_OK
        assert not orphan.exists()
        assert "orphan image" in capsys.readouterr().out

    def test_image_cascade_with_pruned_kernel(
        self,
        tmp_targets: Path,
    ) -> None:
        """When a kernel is pruned, its corresponding image (and
        variant subdirs) cascade-prune with it."""
        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        old_kver = "5.14-rhel9.7-5.14.0-503.40.1.el9_7"
        _make_kernel_dir(arch_dir, old_kver)
        _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-611.49.1.el9_7")
        old_image = _make_image_dir(arch_dir, old_kver)
        old_variant_image = _make_image_dir(
            arch_dir, old_kver, variant="mofed-24"
        )

        rc = _run_prune(tmp_targets, target="rocky9", apply=True)
        assert rc == EXIT_OK
        assert not old_image.exists()
        assert not old_variant_image.exists()

    def test_older_than_filter(
        self,
        tmp_targets: Path,
    ) -> None:
        """--older-than DAYS keeps recently-built superseded entries."""
        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        recent = datetime.now(tz=timezone.utc) - timedelta(days=2)
        old_built = datetime.now(tz=timezone.utc) - timedelta(days=200)
        new_dir = _make_kernel_dir(
            arch_dir,
            "5.14-rhel9.7-5.14.0-611.49.1.el9_7",
            built_at=recent,
        )
        old_dir = _make_kernel_dir(
            arch_dir,
            "5.14-rhel9.7-5.14.0-503.40.1.el9_7",
            built_at=old_built,
        )

        # 30 days threshold: old_dir qualifies, but a 7-day-old
        # superseded entry would not.
        rc = _run_prune(
            tmp_targets, target="rocky9", apply=True, older_than="30"
        )
        assert rc == EXIT_OK
        assert not old_dir.exists()
        assert new_dir.exists()

        # Reset and re-run with a high threshold: old_dir survives.
        old_dir = _make_kernel_dir(
            arch_dir,
            "5.14-rhel9.7-5.14.0-503.40.1.el9_7",
            built_at=old_built,
        )
        rc = _run_prune(
            tmp_targets, target="rocky9", apply=True, older_than="365"
        )
        assert rc == EXIT_OK
        assert old_dir.exists()

    def test_json_output_shape(
        self,
        tmp_targets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-503.40.1.el9_7")
        _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-611.49.1.el9_7")

        rc = _run_prune(tmp_targets, target="rocky9", use_json=True)
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["applied"] is False
        assert payload["removed"] == []
        assert payload["total_bytes"] > 0
        kinds = {c["kind"] for c in payload["candidates"]}
        assert kinds == {"kernel"}

    def test_force_can_prune_protected_group(
        self,
        tmp_targets: Path,
    ) -> None:
        """--force lifts the protected-group guard so even the only
        entry in the default-kernel group can be removed."""
        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        only = _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-611.49.1.el9_7")

        rc = _run_prune(
            tmp_targets, target="rocky9", apply=True, keep=0, force=True
        )
        assert rc == EXIT_OK
        assert not only.exists()

    def test_invalid_target_yaml_skipped_not_swept(
        self,
        tmp_targets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A target whose targets.yaml entry fails validation must be
        SKIPPED with a warning, not treated as gone-from-yaml: the
        gone-path strips default-kernel protection and would offer
        protected artifacts for deletion."""
        import yaml as _yaml

        ypath = tmp_targets / "targets" / "targets.yaml"
        data = _yaml.safe_load(ypath.read_text())
        data["targets"]["rocky9"]["configure_arg"] = ["--typo"]
        ypath.write_text(_yaml.dump(data))

        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-611.13.1.el9_7")

        rc = _run_prune(tmp_targets, target="rocky9", use_json=True)
        assert rc == EXIT_OK
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["candidates"] == []
        assert "failed to load" in captured.err


class TestUserZfs:
    """--user-zfs prunes this user's fallback ZFS builds, and only them."""

    def _seed(self, tmp_targets: Path) -> tuple[Path, Path, Path]:
        from ltvm_pkg.zfs_build import user_cache_root

        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        live = _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-611.55.1.el9_7")
        root = user_cache_root() / "zfs" / "rocky9" / "x86_64"
        kept = root / live.name / "2.4.0"
        gone = root / "5.14-rhel9.5-5.14.0-503.40.1.el9_5" / "2.4.0"
        for d in (kept, gone):
            (d / "staging").mkdir(parents=True)
            (d / "staging" / "zfs.ko").write_bytes(b"x" * 100)
        return live, kept, gone

    def test_preview_names_only_orphans(
        self, tmp_targets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        live, kept, gone = self._seed(tmp_targets)
        rc = _run_prune(tmp_targets, user_zfs=True, use_json=True)
        assert rc == EXIT_OK
        out = json.loads(capsys.readouterr().out)
        paths = [c["path"] for c in out["candidates"]]
        assert paths == [str(gone.parent)]
        assert out["candidates"][0]["kind"] == "user-zfs"
        assert gone.exists() and kept.exists() and live.exists()

    def test_apply_removes_orphans_and_leaves_artifacts(
        self, tmp_targets: Path
    ) -> None:
        live, kept, gone = self._seed(tmp_targets)
        # A superseded shared kernel would be a candidate without
        # --user-zfs; with it, shared artifacts are not touched.
        _make_kernel_dir(
            tmp_targets / "artifacts" / "rocky9" / "x86_64",
            "5.14-rhel9.7-5.14.0-611.13.1.el9_7",
            built_at=datetime.now(timezone.utc) - timedelta(days=90),
        )
        rc = _run_prune(tmp_targets, user_zfs=True, apply=True)
        assert rc == EXIT_OK
        assert not gone.parent.exists()
        assert kept.exists()
        assert (
            tmp_targets
            / "artifacts/rocky9/x86_64/kernels/5.14-rhel9.7-5.14.0-611.13.1.el9_7"
        ).exists()


# The autouse _neutralize_host_clean_state stubs these out; collection
# runs before it, so the real ones are kept here.
from ltvm_pkg.cli import clean as _clean_mod  # noqa: E402

_REAL_IDLE_VMS = _clean_mod._idle_vms
_REAL_VM_KERNEL_DIRS = _clean_mod._vm_kernel_dirs


class TestVmKernels:
    """A kernel a VM boots is never offered, --force or not."""

    @pytest.mark.parametrize("force", [False, True])
    def test_kernel_a_vm_boots_is_kept(
        self,
        tmp_targets: Path,
        capsys: pytest.CaptureFixture[str],
        force: bool,
    ) -> None:
        import os

        arch_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        old = _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-503.40.1.el9_7")
        _make_kernel_dir(arch_dir, "5.14-rhel9.7-5.14.0-611.49.1.el9_7")
        with patch(
            "ltvm_pkg.cli.clean._vm_kernel_dirs",
            return_value={os.path.realpath(old)},
        ):
            rc = _run_prune(
                tmp_targets, target="rocky9", apply=True, force=force
            )
        assert rc == EXIT_OK
        assert old.exists()
        assert "Nothing to prune" in capsys.readouterr().out

    def test_vm_kernel_dirs_reads_each_vm(self, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from ltvm_pkg import vm_state

        k = tmp_path / "kernels" / "k1"
        k.mkdir(parents=True)
        vms = {
            "co1-a": SimpleNamespace(kernel=str(k / "vmlinuz")),
            "co1-b": SimpleNamespace(kernel=""),
        }

        def load(name: str) -> SimpleNamespace:
            if name == "co1-bad":
                raise ValueError("corrupt")
            return vms[name]

        with (
            patch.object(
                vm_state.VMInfo,
                "all_names",
                return_value=["co1-a", "co1-b", "co1-bad"],
            ),
            patch.object(vm_state.VMInfo, "load", side_effect=load),
        ):
            assert _REAL_VM_KERNEL_DIRS() == {str(k.resolve())}


class TestPodmanImages:
    def _dead(self) -> list:
        from ltvm_pkg.podman_gc import TagInfo

        now = datetime.now(tz=timezone.utc).timestamp()
        return [
            (
                TagInfo("ltvm-image-rocky9-final", 3 * 1024**3, now - 86400),
                "image build's export stage; nothing reads it after",
            ),
            (
                TagInfo(
                    "ltvm-build-rocky9-mofed", 2 * 1024**3, now - 90 * 86400
                ),
                "no target, arch or variant in targets.yaml builds it",
            ),
        ]

    def test_full_scan_previews_dead_images(
        self, tmp_targets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("ltvm_pkg.podman_gc.dead_tags", return_value=self._dead()),
            patch(
                "ltvm_pkg.cli.clean.shutil.which", return_value="/bin/podman"
            ),
            patch("ltvm_pkg.podman_gc.remove_tag") as rmi,
        ):
            rc = _run_prune(tmp_targets)
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "ltvm-image-rocky9-final" in out
        assert "ltvm-build-rocky9-mofed" in out
        assert "--apply" in out
        rmi.assert_not_called()

    def test_apply_removes_and_older_than_filters(
        self, tmp_targets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("ltvm_pkg.podman_gc.dead_tags", return_value=self._dead()),
            patch(
                "ltvm_pkg.cli.clean.shutil.which", return_value="/bin/podman"
            ),
            patch("ltvm_pkg.podman_gc.remove_tag", return_value=True) as rmi,
        ):
            rc = _run_prune(
                tmp_targets, apply=True, older_than="30", use_json=True
            )
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert [p["tag"] for p in payload["podman_images"]] == [
            "ltvm-build-rocky9-mofed"
        ]
        assert payload["removed_podman"] == ["ltvm-build-rocky9-mofed"]
        rmi.assert_called_once_with("ltvm-build-rocky9-mofed")

    def test_refused_rmi_is_an_error(
        self, tmp_targets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("ltvm_pkg.podman_gc.dead_tags", return_value=self._dead()),
            patch(
                "ltvm_pkg.cli.clean.shutil.which", return_value="/bin/podman"
            ),
            patch("ltvm_pkg.podman_gc.remove_tag", return_value=False),
        ):
            rc = _run_prune(tmp_targets, apply=True, use_json=True)
        assert rc != EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["removed_podman"] == []
        assert len(payload["errors"]) == 2

    def test_one_target_leaves_podman_alone(
        self, tmp_targets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("ltvm_pkg.podman_gc.dead_tags") as dead:
            _run_prune(tmp_targets, target="rocky9")
        dead.assert_not_called()


class TestIdleVms:
    def _seed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import os
        from types import SimpleNamespace

        from ltvm_pkg import qemu_run, vm_state

        overlays = tmp_path / "overlays"
        overlays.mkdir()
        monkeypatch.setattr(vm_state, "OVERLAYS", overlays)
        now = int(datetime.now(tz=timezone.utc).timestamp())
        day = 86400

        def vm(name: str, last: int, running: bool = False):
            ov = overlays / f"{name}.qcow2"
            ov.write_bytes(b"x" * 8192)
            os.utime(ov, (last, last))
            disk = overlays / f"{name}-disk0.img"
            disk.write_bytes(b"y" * 4096)
            os.utime(disk, (last, last))
            return SimpleNamespace(
                name=name,
                created=last - day,
                last_boot=last,
                last_deploy=0,
                overlay_path=ov,
                disk_dir="",
                running=running,
            )

        vms = {
            v.name: v
            for v in (
                vm("co1-old", now - 40 * day),
                vm("co2-mds", now - 30 * day),
                vm("co2-oss", now - 30 * day),
                vm("co3-new", now - 2 * day),
                vm("co4-up", now - 40 * day, running=True),
            )
        }
        monkeypatch.setattr(
            vm_state.VMInfo, "all_names", staticmethod(lambda: sorted(vms))
        )
        monkeypatch.setattr(
            vm_state.VMInfo, "load", staticmethod(lambda n: vms[n])
        )
        monkeypatch.setattr(qemu_run, "is_running", lambda v: v.running)
        cluster = SimpleNamespace(
            get_nodes=lambda: [
                SimpleNamespace(name="co2-mds"),
                SimpleNamespace(name="co2-oss"),
            ]
        )
        monkeypatch.setattr(
            vm_state.ClusterInfo, "all_names", staticmethod(lambda: ["co2"])
        )
        monkeypatch.setattr(
            vm_state.ClusterInfo, "load", staticmethod(lambda n: cluster)
        )

    def test_lists_stopped_vms_idle_past_the_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(tmp_path, monkeypatch)
        idle = {v["name"]: v for v in _REAL_IDLE_VMS(14)}
        assert set(idle) == {"co1-old", "co2-mds", "co2-oss"}
        assert idle["co1-old"]["cluster"] is None
        assert idle["co2-mds"]["cluster"] == "co2"
        assert 39 < idle["co1-old"]["idle_days"] < 41
        assert idle["co1-old"]["bytes"] > 0

    def test_report_names_whole_clusters_and_lone_vms(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._seed(tmp_path, monkeypatch)
        _clean_mod._print_idle_vms(_REAL_IDLE_VMS(14), 14)
        out = capsys.readouterr().out
        assert "clean never removes a VM" in out
        assert "remove: ltvm cluster destroy co2" in out
        assert "remove: ltvm destroy co1-old\n" in out

    def test_cluster_with_a_busy_member_is_not_whole(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._seed(tmp_path, monkeypatch)
        idle = [v for v in _REAL_IDLE_VMS(14) if v["name"] != "co2-oss"]
        _clean_mod._print_idle_vms(idle, 14)
        out = capsys.readouterr().out
        assert "cluster destroy" not in out
        assert "remove: ltvm destroy co1-old co2-mds" in out

    def test_prune_prints_them_and_removes_nothing(
        self,
        tmp_targets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        idle = [
            {"name": "co1-old", "idle_days": 40.0, "bytes": 1, "cluster": None}
        ]
        with patch("ltvm_pkg.cli.clean._idle_vms", return_value=idle):
            rc = _run_prune(tmp_targets, apply=True)
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "co1-old" in out
        assert "Nothing to prune" in out


class TestBuildVolumes:
    """macOS build-tree volumes go with their kernels, scoped like the
    rest of clean; ccache is shown and never pruned."""

    def _setup(
        self, tmp_targets: Path, monkeypatch: pytest.MonkeyPatch
    ) -> dict:
        from ltvm_pkg import build_volumes as bv

        arts = tmp_targets / "artifacts"
        r9 = arts / "rocky9" / "x86_64"
        old = _make_kernel_dir(r9, "5.14-rhel9.7-5.14.0-503.40.1.el9_7")
        new = _make_kernel_dir(r9, "5.14-rhel9.7-5.14.0-611.49.1.el9_7")
        (old / "build-tree").mkdir()
        (new / "build-tree").mkdir()
        gone = arts / "rocky10" / "x86_64" / "kernels" / "k" / "build-tree"

        def kvol(name: str, tree: Path) -> bv.Volume:
            labels = {
                bv.LABEL_KIND: "kernel",
                bv.LABEL_TREE: str(tree.resolve()),
            }
            return bv.Volume(name, labels)

        vols = [
            # The superseded kernel's own current volume: live until the
            # kernel is removed.
            kvol(bv.kernel_volume_name(old / "build-tree"), old / "build-tree"),
            kvol(bv.kernel_volume_name(new / "build-tree"), new / "build-tree"),
            kvol("v-r10-gone", gone),
            bv.Volume(
                "ltvm-build-ccache-rocky9-x86_64", {bv.LABEL_KIND: "ccache"}
            ),
        ]
        monkeypatch.setattr(bv, "is_macos", lambda: True)
        monkeypatch.setattr(bv, "list_volumes", lambda: vols)
        monkeypatch.setattr(
            bv, "volume_sizes", lambda: {v.name: 1_800_000_000 for v in vols}
        )
        removed: list[str] = []
        monkeypatch.setattr(
            bv, "remove_volume", lambda n: removed.append(n) is None
        )
        return {"vols": vols, "removed": removed, "old": old, "new": new}

    def test_a_removed_kernel_takes_its_volume_along(
        self,
        tmp_targets: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        s = self._setup(tmp_targets, monkeypatch)
        old_vol = s["vols"][0].name
        rc = _run_prune(tmp_targets, target="rocky9", apply=True)
        assert rc == EXIT_OK
        assert not s["old"].exists()
        # The superseded kernel's volume goes with it; rocky10's dead
        # volume is outside `clean rocky9`; the live kernel's stays.
        assert s["removed"] == [old_vol]
        out = capsys.readouterr().out
        assert "its kernel is removed in this run" in out
        assert "ccache, capped at 5G" in out
        assert "and 1 podman volume." in out

    def test_full_scan_dry_run_lists_dead_ones_and_removes_nothing(
        self,
        tmp_targets: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        s = self._setup(tmp_targets, monkeypatch)
        rc = _run_prune(tmp_targets, use_json=True)
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        names = {v["name"] for v in payload["podman_volumes"]}
        assert names == {"v-r10-gone", s["vols"][0].name}
        assert [v["name"] for v in payload["podman_volumes_kept"]] == [
            "ltvm-build-ccache-rocky9-x86_64"
        ]
        assert payload["podman_volumes_checked"] is True
        assert s["removed"] == []

    def test_says_when_podman_could_not_be_asked(
        self,
        tmp_targets: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ltvm_pkg import build_volumes as bv

        monkeypatch.setattr(bv, "is_macos", lambda: True)
        monkeypatch.setattr(bv, "list_volumes", lambda: None)
        rc = _run_prune(tmp_targets)
        assert rc == EXIT_OK
        assert "Build volumes: not checked" in capsys.readouterr().out

    def test_nothing_about_volumes_off_a_mac(
        self, tmp_targets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = _run_prune(tmp_targets)
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "volume" not in out.lower()
