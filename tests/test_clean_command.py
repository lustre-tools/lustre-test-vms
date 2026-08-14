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
        orphan = _make_image_dir(
            arch_dir, "5.14-rhel9.5-5.14.0-503.40.1.el9_5"
        )

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
        only = _make_kernel_dir(
            arch_dir, "5.14-rhel9.7-5.14.0-611.49.1.el9_7"
        )

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
