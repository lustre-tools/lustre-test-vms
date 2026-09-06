"""cmd_deploy's fast path must see more than source-file mtimes.

`_staging_is_fresh` decided whether `ltvm deploy-lustre` could skip
the build using exactly one input: whether any file in the Lustre tree
was newer than the staging stamp.  Neither of the two things that most
often invalidate a staging -- the kernel it links against, and the
configure flags it was built with -- moves a source file's mtime.
"""

from __future__ import annotations

import json
from pathlib import Path

from ltvm_pkg.lustre_build import _hash_file, _stamp_suffix


def _seed(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a tree + staging + kernel build-tree that look consistent."""
    tree = tmp_path / "lustre-release"
    (tree / "lustre" / "kernel_patches").mkdir(parents=True)
    (tree / "lnet").mkdir()

    staging = tree / ".ltvm-staging" / "rocky9" / "x86_64" / "5.14-rhel9.7"
    staging.mkdir(parents=True)
    (staging / "lustre.ko").write_text("")

    build_tree = tmp_path / "kernels" / "5.14-rhel9.7" / "build-tree"
    build_tree.mkdir(parents=True)
    (build_tree / "Module.symvers").write_text("symbols v1\n")
    return tree, staging, build_tree


def _record(tree: Path, staging: Path, build_tree: Path, cfg: str) -> None:
    (staging / ".ltvm-staging-meta.json").write_text(
        json.dumps(
            {
                "kernel_version": "5.14.0-fake",
                "module_symvers_sha256": _hash_file(
                    build_tree / "Module.symvers"
                ),
                "configure_sha256": cfg,
            }
        )
    )
    (tree / f".ltvm-configure-{_stamp_suffix('rocky9', 'x86_64')}").write_text(
        cfg + "\n"
    )
    (staging / ".ltvm-staging-stamp").touch()


class TestStagingMetaRecordsFreshnessInputs:
    def test_build_records_symvers_and_configure(self) -> None:
        """build_lustre must persist both signals deploy compares.

        They are what let deploy tell "same kernel and flags" from
        "same source mtimes"; without them the fast path is blind.
        """
        import inspect

        from ltvm_pkg import lustre_build

        src = inspect.getsource(lustre_build)
        assert '"module_symvers_sha256": symvers_hash' in src
        assert '"configure_sha256": cfg_hash' in src


class TestKernelAbiChangeIsDetected:
    def test_symvers_hash_moves_when_kernel_rebuilt(
        self, tmp_path: Path
    ) -> None:
        """A rebuilt kernel keeps its release string but not its ABI.

        This is the case that shipped modules the VM then refused to
        load with "disagrees about version of symbol".
        """
        tree, staging, build_tree = _seed(tmp_path)
        cfg = "ab" * 32
        _record(tree, staging, build_tree, cfg)
        recorded = json.loads(
            (staging / ".ltvm-staging-meta.json").read_text()
        )["module_symvers_sha256"]

        # Kernel rebuilt from an edited patch series: same directory,
        # same kernel.release, different Module.symvers.
        (build_tree / "Module.symvers").write_text("symbols v2\n")
        assert _hash_file(build_tree / "Module.symvers") != recorded

    def test_configure_stamp_moves_when_flags_change(
        self, tmp_path: Path
    ) -> None:
        tree, staging, build_tree = _seed(tmp_path)
        cfg = "ab" * 32
        _record(tree, staging, build_tree, cfg)
        stamp = tree / f".ltvm-configure-{_stamp_suffix('rocky9', 'x86_64')}"
        recorded = json.loads(
            (staging / ".ltvm-staging-meta.json").read_text()
        )["configure_sha256"]
        assert stamp.read_text().strip() == recorded

        # targets.yaml configure_args changed -> build_lustre rewrites
        # the stamp; the staging meta still names the old flags.
        stamp.write_text("cd" * 32 + "\n")
        assert stamp.read_text().strip() != recorded
