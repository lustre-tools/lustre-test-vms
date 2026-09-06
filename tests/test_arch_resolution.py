"""targets.yaml's `arch` key means two different things.

For rocky9-64k ("arch: aarch64 -- must not inherit the x86_64
default") it is a constraint.  For rocky9, which never states one and
is published for both arches, the inherited x86_64 is only a default
and the host should win.  Commands disagreed about this: the build
path substituted host_arch() unconditionally (so `build all
rocky9-64k` produced an x86_64 kernel and applied
CONFIG_ARM64_64K_PAGES as a no-op), while `target clean`/`delete`
passed None and got the declared arch -- cleaning a directory the
builds never wrote to.
"""

from __future__ import annotations

import argparse

import yaml

from ltvm_pkg.cli.util import host_arch, resolve_arch
from ltvm_pkg.target_config import TargetConfig


class TestArchIsDeclared:
    def test_declared_when_yaml_states_it(self) -> None:
        assert TargetConfig("rocky9-64k").arch_is_declared is True
        assert TargetConfig("rocky9-64k").arch == "aarch64"

    def test_not_declared_when_inherited(self) -> None:
        tc = TargetConfig("rocky9")
        assert tc.arch_is_declared is False
        assert tc.arch == "x86_64"  # from _DEFAULTS


class TestResolveArch:
    def test_explicit_flag_wins(self) -> None:
        ns = argparse.Namespace(arch="ppc64le")
        assert resolve_arch(ns, "rocky9-64k") == "ppc64le"
        assert resolve_arch(ns, "rocky9") == "ppc64le"

    def test_declared_arch_beats_host(self) -> None:
        """The whole point of rocky9-64k."""
        ns = argparse.Namespace(arch=None)
        assert resolve_arch(ns, "rocky9-64k") == "aarch64"

    def test_undeclared_target_follows_host(self) -> None:
        ns = argparse.Namespace(arch=None)
        assert resolve_arch(ns, "rocky9") == host_arch()

    def test_unknown_target_falls_back_to_host(self) -> None:
        ns = argparse.Namespace(arch=None)
        assert resolve_arch(ns, "no-such-target") == host_arch()

    def test_no_target_falls_back_to_host(self) -> None:
        ns = argparse.Namespace(arch=None)
        assert resolve_arch(ns, None) == host_arch()


class TestDeclaredArchDrivesOutputDir:
    def test_output_dir_uses_declared_arch(self) -> None:
        """Artifacts must not land under the host arch for a
        cross-only target -- that is what made `build all` and
        `target clean` operate on different directories."""
        ns = argparse.Namespace(arch=None)
        arch = resolve_arch(ns, "rocky9-64k")
        tc = TargetConfig("rocky9-64k", arch=arch)
        assert tc.output_dir.name == "aarch64"


class TestFixtureArchOverrideStillWorks:
    def test_yaml_declared_arch_round_trips(self, tmp_targets) -> None:
        """A target that declares an arch keeps it through a reload."""
        yaml_path = tmp_targets / "targets" / "targets.yaml"
        data = yaml.safe_load(yaml_path.read_text())
        data["targets"]["rocky9"]["arch"] = "aarch64"
        yaml_path.write_text(yaml.dump(data, default_flow_style=False))

        from unittest.mock import patch

        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"),
            patch.object(cfg, "TARGETS_YAML", yaml_path),
        ):
            tc = cfg.TargetConfig("rocky9")
        assert tc.arch_is_declared is True
        assert tc.arch == "aarch64"
