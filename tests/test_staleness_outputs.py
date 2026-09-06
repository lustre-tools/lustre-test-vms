"""Staleness must consider the artifact on disk, not just its hash.

A meta.json is written when a build succeeds, but nothing removes it
when a *later* rebuild of the same inputs fails partway -- and a
failed kernel build actively destroys its outputs (vmlinux is renamed
away by archive_outgoing_vmlinux, modules/ is rm -rf'd by the inner
script) while leaving meta.json, whose inputs did not change, in
place.  Hash-only staleness then answered "up to date" forever.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import (
    _make_config,
    _make_image_outputs,
    _make_kernel_outputs,
)


class TestKernelOutputsGateStaleness:
    def test_fresh_when_meta_and_outputs_present(
        self, tmp_targets: Path
    ) -> None:
        tc = _make_config(tmp_targets)
        tc.write_meta("kernel")
        _make_kernel_outputs(tc)
        assert tc.is_stale("kernel") is False

    def test_stale_when_vmlinux_missing(self, tmp_targets: Path) -> None:
        """The exact state a failed rebuild leaves behind."""
        tc = _make_config(tmp_targets)
        tc.write_meta("kernel")
        out = _make_kernel_outputs(tc)
        assert tc.is_stale("kernel") is False
        (out / "vmlinux").unlink()
        assert tc.is_stale("kernel") is True

    def test_stale_when_modules_dir_emptied(self, tmp_targets: Path) -> None:
        """kernel-build-inner.sh rm -rf's modules/ before it rebuilds."""
        tc = _make_config(tmp_targets)
        tc.write_meta("kernel")
        out = _make_kernel_outputs(tc)
        for ko in (out / "modules").rglob("*.ko"):
            ko.unlink()
        assert tc.is_stale("kernel") is True

    def test_stale_when_build_tree_config_missing(
        self, tmp_targets: Path
    ) -> None:
        tc = _make_config(tmp_targets)
        tc.write_meta("kernel")
        out = _make_kernel_outputs(tc)
        (out / "build-tree" / ".config").unlink()
        assert tc.is_stale("kernel") is True

    def test_outputs_complete_reports_directly(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.outputs_complete("kernel") is False
        _make_kernel_outputs(tc)
        assert tc.outputs_complete("kernel") is True


class TestImageOutputsGateStaleness:
    def test_stale_when_base_ext4_missing(self, tmp_targets: Path) -> None:
        """An interrupted `target fetch` can leave meta.json with no image.

        tar writes members in archive order, so meta.json can land
        before base.ext4; no release-tag file is recorded, so the next
        fetch proceeds -- but in the meantime is_stale said "fresh"
        and `build all` skipped straight past the missing image.
        """
        tc = _make_config(tmp_targets)
        tc.write_meta("image", kernel="5.14-rhel9.7")
        img = _make_image_outputs(tc, kernel="5.14-rhel9.7")
        assert tc.is_stale("image", kernel="5.14-rhel9.7") is False
        img.unlink()
        assert tc.is_stale("image", kernel="5.14-rhel9.7") is True

    def test_container_not_output_checked(self, tmp_targets: Path) -> None:
        """Container images live in podman's store, not the filesystem."""
        tc = _make_config(tmp_targets)
        tc.write_meta("container")
        assert tc.outputs_complete("container") is True
        assert tc.is_stale("container") is False


class TestWriteMetaHashKernel:
    def test_hash_kernel_keeps_full_and_short_names_agreeing(
        self, tmp_targets: Path
    ) -> None:
        """meta lands in the full-name dir but hashes the short name.

        _short_kernel_name() can only normalise the two forms for
        kernels declared in targets.yaml.  For anything else the
        builder's write_meta hash and is_stale's hash disagreed, so
        the kernel rebuilt from scratch on every invocation and showed
        permanently stale in `build status`.
        """
        tc = _make_config(tmp_targets)
        full = "5.14-rhel9.7-5.14.0-611.42.1.el9_7"
        short = "5.14-rhel9.7"
        tc.write_meta("kernel", kernel=full, hash_kernel=short)
        _make_kernel_outputs(tc, kernel=full)
        assert tc.is_stale("kernel", kernel=short) is False

    def test_undeclared_kernel_would_diverge_without_hash_kernel(
        self, tmp_targets: Path
    ) -> None:
        tc = _make_config(tmp_targets)
        undeclared_full = "6.17-6.17.9"
        undeclared_short = "6.17"
        # Without hash_kernel the two hashes differ for a kernel that
        # targets.yaml does not declare -- that divergence is the bug.
        assert tc.input_hash("kernel", kernel=undeclared_full) != tc.input_hash(
            "kernel", kernel=undeclared_short
        )
        # With it, the persisted hash matches what is_stale computes.
        tc.write_meta(
            "kernel",
            kernel=undeclared_full,
            hash_kernel=undeclared_short,
        )
        _make_kernel_outputs(tc, kernel=undeclared_full)
        assert tc.is_stale("kernel", kernel=undeclared_short) is False
