"""End-to-end tests for arch propagation across the build/deploy chain.

These tests are inspired by the round 18 audit, which found that arch
state didn't propagate cleanly through every layer:
  - staging_path was arch-blind (cross-arch builds clobbered each other)
  - reconfigure stamps weren't arch-qualified (autogen+configure skipped
    on arch switch, producing corrupt cross-arch artifacts)
  - cmd_deploy/cmd_cluster_deploy built TargetConfig() without vm.arch
  - cmd_deploy used Path(__file__).parent.parent instead of ARTIFACTS_DIR
  - ccache volume wasn't arch-qualified

The tests below pin those properties so future refactors can't quietly
re-introduce the same bugs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ltvm_pkg import lustre_build
from ltvm_pkg.lustre_build import _stamp_suffix, staging_path

# ── staging_path is arch-aware ───────────────────────────


class TestStagingPathArch:
    """staging_path must isolate cross-arch builds for the same target,
    AND must place the staging dir inside the user's lustre tree so two
    users on the same host cannot collide."""

    TREE_A = Path("/home/alice/lustre-release")
    TREE_B = Path("/home/bob/lustre-release")
    KERNEL = "5.14-rhel9.7"

    def test_lives_inside_lustre_tree(self) -> None:
        """The whole point of the in-tree refactor: staging is under the
        user's tree, not under the shared output dir."""
        p = staging_path(self.TREE_A, "rocky9", kernel=self.KERNEL)
        assert str(p).startswith(str(self.TREE_A) + "/")
        assert ".ltvm-staging" in p.parts

    def test_x86_64_default_includes_arch(self) -> None:
        """x86_64 (default) gets its own arch dir under the target.
        Unlike the previous flat layout, the arch is always present so
        a `rm -rf` of one arch's staging cannot nuke the other's."""
        p = staging_path(self.TREE_A, "rocky9", kernel=self.KERNEL)
        assert p.parts[-4:] == (
            ".ltvm-staging",
            "rocky9",
            "x86_64",
            self.KERNEL,
        )

    def test_x86_64_explicit_matches_default(self) -> None:
        """Explicit x86_64 is the same path as the default."""
        assert staging_path(
            self.TREE_A, "rocky9", arch="x86_64", kernel=self.KERNEL
        ) == staging_path(self.TREE_A, "rocky9", kernel=self.KERNEL)

    def test_aarch64_is_arch_qualified(self) -> None:
        p = staging_path(
            self.TREE_A, "rocky9", arch="aarch64", kernel=self.KERNEL
        )
        assert p.parts[-4:] == (
            ".ltvm-staging",
            "rocky9",
            "aarch64",
            self.KERNEL,
        )

    def test_two_arches_isolated(self) -> None:
        """Two arches must not share a staging path."""
        x86 = staging_path(
            self.TREE_A, "rocky9", arch="x86_64", kernel=self.KERNEL
        )
        arm = staging_path(
            self.TREE_A, "rocky9", arch="aarch64", kernel=self.KERNEL
        )
        assert x86 != arm
        assert not str(arm).startswith(str(x86) + "/")
        assert not str(x86).startswith(str(arm) + "/")

    def test_distinct_targets_distinct_paths(self) -> None:
        """Different targets in the same tree get different paths."""
        assert staging_path(
            self.TREE_A, "rocky9", kernel=self.KERNEL
        ) != staging_path(self.TREE_A, "ubuntu2404", kernel=self.KERNEL)

    def test_distinct_trees_distinct_paths(self) -> None:
        """The multi-user invariant: alice and bob building the same
        target against their own trees get fully disjoint staging."""
        a = staging_path(self.TREE_A, "rocky9", kernel=self.KERNEL)
        b = staging_path(self.TREE_B, "rocky9", kernel=self.KERNEL)
        assert a != b
        assert not str(a).startswith(str(b) + "/")
        assert not str(b).startswith(str(a) + "/")

    def test_same_target_same_arch_idempotent(self) -> None:
        """Two calls with the same args return equal paths."""
        assert staging_path(
            self.TREE_A, "rocky9", arch="aarch64", kernel=self.KERNEL
        ) == staging_path(
            self.TREE_A, "rocky9", arch="aarch64", kernel=self.KERNEL
        )


# ── reconfigure stamp suffix isolates target+arch ────────


class TestStampSuffix:
    """Reconfigure stamps must distinguish (target, arch) so a switch
    forces a fresh autogen+configure pass instead of reusing the other
    arch's config.status."""

    def test_default_arch_suffix_includes_arch(self) -> None:
        """Every arch is suffixed -- no x86_64 special case anymore."""
        assert _stamp_suffix("rocky9", "x86_64") == "rocky9-x86_64"

    def test_aarch64_suffix_includes_arch(self) -> None:
        assert _stamp_suffix("rocky9", "aarch64") == "rocky9-aarch64"

    def test_default_and_aarch64_distinct(self) -> None:
        """The whole point: switching arches must produce a different
        stamp filename so _needs_reconfigure sees it as missing."""
        assert _stamp_suffix("rocky9", "x86_64") != _stamp_suffix(
            "rocky9", "aarch64"
        )

    def test_distinct_targets_distinct_suffixes(self) -> None:
        assert _stamp_suffix("rocky9", "aarch64") != _stamp_suffix(
            "ubuntu2404", "aarch64"
        )


# ── _needs_reconfigure honors arch ───────────────────────


class TestNeedsReconfigureArch:
    """A previously-built x86_64 tree should still need reconfigure when
    we ask for an aarch64 build, and vice versa."""

    def _make_lustre_tree(self, tmp_path: Path) -> Path:
        """Build a fake Lustre tree with configure + config.status."""
        tree = tmp_path / "lustre-release"
        tree.mkdir()
        (tree / "configure").write_text("#!/bin/sh\n")
        (tree / "configure").chmod(0o755)
        (tree / "config.status").write_text("# fake config.status\n")
        return tree

    def _make_build_tree(
        self, tmp_path: Path, kver: str = "5.14.0-test"
    ) -> Path:
        """Build a fake kernel build tree with kernel.release."""
        bt = tmp_path / "build-tree"
        (bt / "include" / "config").mkdir(parents=True)
        (bt / "include" / "config" / "kernel.release").write_text(kver + "\n")
        return bt

    def test_x86_stamps_dont_satisfy_aarch64_check(
        self, tmp_path: Path
    ) -> None:
        """If only x86_64 stamps exist, an aarch64 build must reconfigure.

        This is the bug round 18 caught: shared stamp filename meant
        switching arches on the same source tree silently reused
        config.status from the other arch and produced corrupt artifacts.
        """
        tree = self._make_lustre_tree(tmp_path)
        build_tree = self._make_build_tree(tmp_path)
        kver = "5.14.0-test"

        # Pretend we just finished an x86_64 build by writing the
        # x86_64-suffixed stamps.
        (tree / f".ltvm-kernel-{_stamp_suffix('rocky9', 'x86_64')}").write_text(
            kver + "\n"
        )
        (tree / f".ltvm-server-{_stamp_suffix('rocky9', 'x86_64')}").write_text(
            "True\n"
        )

        # Now ask: do we need to reconfigure for an aarch64 build?
        need = lustre_build._needs_reconfigure(
            tree,
            build_tree,
            force=False,
            target="rocky9",
            enable_server=True,
            arch="aarch64",
        )
        assert need is True, (
            "aarch64 build should not skip reconfigure because the "
            "x86_64 stamps exist -- this is exactly the bug round 18 "
            "caught"
        )

    def test_x86_stamps_satisfy_x86_check(self, tmp_path: Path) -> None:
        """The corollary: an x86_64 rebuild with matching stamps does
        NOT need to reconfigure (otherwise we'd rebuild on every run)."""
        tree = self._make_lustre_tree(tmp_path)
        build_tree = self._make_build_tree(tmp_path)
        kver = "5.14.0-test"

        (tree / f".ltvm-kernel-{_stamp_suffix('rocky9', 'x86_64')}").write_text(
            kver + "\n"
        )
        (tree / f".ltvm-server-{_stamp_suffix('rocky9', 'x86_64')}").write_text(
            "True\n"
        )

        need = lustre_build._needs_reconfigure(
            tree,
            build_tree,
            force=False,
            target="rocky9",
            enable_server=True,
            arch="x86_64",
        )
        assert need is False

    def test_aarch64_stamps_dont_satisfy_x86_check(
        self, tmp_path: Path
    ) -> None:
        """Symmetric: aarch64 stamps don't satisfy an x86_64 build."""
        tree = self._make_lustre_tree(tmp_path)
        build_tree = self._make_build_tree(tmp_path)
        kver = "5.14.0-test"

        (
            tree / f".ltvm-kernel-{_stamp_suffix('rocky9', 'aarch64')}"
        ).write_text(kver + "\n")
        (
            tree / f".ltvm-server-{_stamp_suffix('rocky9', 'aarch64')}"
        ).write_text("True\n")

        need = lustre_build._needs_reconfigure(
            tree,
            build_tree,
            force=False,
            target="rocky9",
            enable_server=True,
            arch="x86_64",
        )
        assert need is True


# ── extra_configure quoting end-to-end ───────────────────


class TestExtraConfigureQuoting:
    """`--extra-configure` flows into a bash heredoc inside the build
    container.  Round 18 found a naive space-join that broke on values
    with spaces and was a shell-injection vector for adversarial flags.
    """

    def test_simple_flags_pass_through(self, tmp_path: Path) -> None:
        """Plain flags like --with-foo=bar are unchanged after quoting."""
        import shlex

        args = ["--with-o2ib=no", "--disable-server"]
        joined = " ".join(shlex.quote(a) for a in args)
        # shlex.quote leaves alnum + safe chars alone
        assert "--with-o2ib=no" in joined
        assert "--disable-server" in joined

    def test_value_with_spaces_is_quoted(self, tmp_path: Path) -> None:
        """A configure flag with embedded spaces survives shell parsing."""
        import shlex

        args = ["--with-linux=/tmp/build dir/linux"]
        joined = " ".join(shlex.quote(a) for a in args)
        # shlex.quote wraps the whole thing in single quotes
        assert "'" in joined
        # And tokenising the result gets the original arg back as one token
        assert shlex.split(joined) == args

    def test_metachar_does_not_escape_quoting(self, tmp_path: Path) -> None:
        """A semicolon in a value does not turn into a separator."""
        import shlex

        args = ["--with-foo=bar; rm -rf /"]
        joined = " ".join(shlex.quote(a) for a in args)
        # The injection vector: `rm -rf /` must be inside the quoted arg,
        # not a separate command.
        tokens = shlex.split(joined)
        assert len(tokens) == 1
        assert tokens[0] == "--with-foo=bar; rm -rf /"


# ── cmd_deploy uses tc.output_dir not __file__.parent ────


class TestDeployBundledSnapshotPath:
    """cmd_deploy's bundled-snapshot lookup must honor LTVM_ROOT and
    arch.  The previous Path(__file__).parent.parent / "artifacts" / target
    hardcoded the source-tree layout and ignored arch."""

    def test_aarch64_lookup_uses_arch_subdir(self, tmp_path: Path) -> None:
        """A TargetConfig built with arch=aarch64 has output_dir under
        the arch subdir; cmd_deploy must use tc.output_dir to find
        bundled snapshots."""
        from ltvm_pkg.target_config import TargetConfig

        # Use a real target from the repo so the test doesn't depend on
        # fixture scaffolding.  We just need to verify the path shape.
        try:
            tc = TargetConfig("rocky9", arch="aarch64")
        except (ValueError, FileNotFoundError):
            pytest.skip("rocky9 target not available in this checkout")
        # The bundled snapshot lookup is
        # `tc.output_dir / "kernels" / k / "lustre-artifacts"`.
        # The key property: tc.output_dir is arch-qualified for aarch64.
        assert "aarch64" in tc.output_dir.parts

    def test_x86_64_lookup_includes_arch(self) -> None:
        """Default arch is now arch-qualified just like every other arch
        so callers never have to special-case x86_64 vs the rest."""
        from ltvm_pkg.target_config import TargetConfig

        try:
            tc = TargetConfig("rocky9")  # default arch
        except (ValueError, FileNotFoundError):
            pytest.skip("rocky9 target not available in this checkout")
        assert "x86_64" in tc.output_dir.parts
        assert "aarch64" not in tc.output_dir.parts


# ── ccache volume name distinguishes arches ──────────────


class TestCcacheVolumeArch:
    """The ccache podman volume name must include arch for non-default
    arch builds, otherwise an aarch64 cross-build serves stale x86_64
    object files (and vice versa)."""

    def test_default_arch_volume_name(self) -> None:
        from ltvm_pkg.kernel_build import _ccache_volume
        from ltvm_pkg.target_config import TargetConfig

        try:
            tc = TargetConfig("rocky9")
        except (ValueError, FileNotFoundError):
            pytest.skip("rocky9 target not available in this checkout")
        assert _ccache_volume(tc) == "ltvm-ccache-rocky9"

    def test_aarch64_volume_name(self) -> None:
        from ltvm_pkg.kernel_build import _ccache_volume
        from ltvm_pkg.target_config import TargetConfig

        try:
            tc = TargetConfig("rocky9", arch="aarch64")
        except (ValueError, FileNotFoundError):
            pytest.skip("rocky9 target not available in this checkout")
        assert _ccache_volume(tc) == "ltvm-ccache-rocky9-aarch64"

    def test_native_and_cross_volumes_differ(self) -> None:
        """The whole point of arch-qualifying the volume name."""
        from ltvm_pkg.kernel_build import _ccache_volume
        from ltvm_pkg.target_config import TargetConfig

        try:
            native = _ccache_volume(TargetConfig("rocky9"))
            cross = _ccache_volume(TargetConfig("rocky9", arch="aarch64"))
        except (ValueError, FileNotFoundError):
            pytest.skip("rocky9 target not available in this checkout")
        assert native != cross
