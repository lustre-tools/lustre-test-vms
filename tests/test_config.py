"""Tests for ltvm_pkg/target_config.py -- TargetConfig and helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from tests.conftest import _ROCKY9_YAML, _make_config, _write_targets_yaml


class TestTargetConfigProperties:
    def test_os_family(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.os_family == "rhel"

    def test_os_name(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.os_name == "rocky"

    def test_os_version(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.os_version == "9.7"

    def test_arch(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.arch == "x86_64"

    def test_container_image(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.container_image == "rockylinux:9.7"

    def test_default_kernel(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.default_kernel == "5.14-rhel9.7"

    def test_kernel_config_overrides(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.kernel_config_overrides == {"CONFIG_XEN_PVH": "y"}

    def test_status(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.status == "working"


class TestTargetConfigUnknown:
    def test_unknown_target_raises(self, tmp_targets: Path) -> None:
        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
            pytest.raises(ValueError, match="Unknown target"),
        ):
            cfg.TargetConfig("nonexistent")

    def test_planned_target_raises(self, tmp_targets: Path) -> None:
        import ltvm_pkg.target_config as cfg

        data = {
            "defaults": {"arch": "x86_64", "os_family": "rhel"},
            "targets": {
                "rocky8": {
                    "os_name": "rocky",
                    "os_version": "8",
                    "container_image": "rockylinux:8",
                    "status": "planned",
                    "kernels": {"default": "4.18-rhel8"},
                }
            },
        }
        _write_targets_yaml(tmp_targets / "targets", data)
        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
            pytest.raises(ValueError, match="status='planned'"),
        ):
            cfg.TargetConfig("rocky8")


class TestLustreMode:
    def test_valid_mode_parsed(self, tmp_targets: Path) -> None:
        from ltvm_pkg.target_config import LustreMode

        tc = _make_config(tmp_targets)
        assert tc.lustre_mode is LustreMode.SERVER_LDISKFS

    def test_server_zfs_mode(self, tmp_targets: Path) -> None:
        import ltvm_pkg.target_config as cfg

        data = yaml.safe_load(
            (tmp_targets / "targets" / "targets.yaml").read_text()
        )
        data["targets"]["rocky9"]["lustre"] = {"mode": "server_zfs"}
        _write_targets_yaml(tmp_targets / "targets", data)
        tc = _make_config(tmp_targets)
        assert tc.lustre_mode is cfg.LustreMode.SERVER_ZFS

    def test_missing_lustre_section_raises(self, tmp_targets: Path) -> None:
        data = yaml.safe_load(
            (tmp_targets / "targets" / "targets.yaml").read_text()
        )
        del data["targets"]["rocky9"]["lustre"]
        _write_targets_yaml(tmp_targets / "targets", data)
        with pytest.raises(ValueError, match="missing required 'lustre.mode'"):
            _make_config(tmp_targets)

    def test_missing_mode_key_raises(self, tmp_targets: Path) -> None:
        data = yaml.safe_load(
            (tmp_targets / "targets" / "targets.yaml").read_text()
        )
        data["targets"]["rocky9"]["lustre"] = {}
        _write_targets_yaml(tmp_targets / "targets", data)
        with pytest.raises(ValueError, match="missing required 'lustre.mode'"):
            _make_config(tmp_targets)

    def test_unknown_mode_raises(self, tmp_targets: Path) -> None:
        data = yaml.safe_load(
            (tmp_targets / "targets" / "targets.yaml").read_text()
        )
        data["targets"]["rocky9"]["lustre"] = {"mode": "client_only"}
        _write_targets_yaml(tmp_targets / "targets", data)
        with pytest.raises(
            ValueError, match="unknown lustre.mode 'client_only'"
        ):
            _make_config(tmp_targets)


class TestResolveKernel:
    def test_explicit_kernel(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.resolve_kernel("custom-kernel") == "custom-kernel"

    def test_default_kernel(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.resolve_kernel(None) == "5.14-rhel9.7"

    def test_prefix_scan_finds_full_version_dir(
        self, tmp_targets: Path
    ) -> None:
        tc = _make_config(tmp_targets)
        kernels = tmp_targets / "output" / "rocky9" / "x86_64" / "kernels"
        full = "5.14-rhel9.7-5.14.0-611.13.1.el9_7_lustre"
        (kernels / full).mkdir(parents=True)
        assert tc.resolve_kernel("5.14-rhel9.7") == full


class TestKernelOutputDir:
    def test_default_path(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        expected = (
            tmp_targets / "output" / "rocky9" / "x86_64" / "kernels" / "5.14-rhel9.7"
        )
        assert tc.kernel_output_dir() == expected

    def test_custom_kernel_path(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        expected = tmp_targets / "output" / "rocky9" / "x86_64" / "kernels" / "custom"
        assert tc.kernel_output_dir("custom") == expected


class TestAvailableKernels:
    def test_no_kernels_dir(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.available_kernels() == []

    def test_with_kernels(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        kernels = tmp_targets / "output" / "rocky9" / "x86_64" / "kernels"
        (kernels / "5.14-rhel9.7").mkdir(parents=True)
        (kernels / "5.14-rhel9.6").mkdir(parents=True)
        result = tc.available_kernels()
        assert result == ["5.14-rhel9.6", "5.14-rhel9.7"]

    def test_ignores_files(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        kernels = tmp_targets / "output" / "rocky9" / "x86_64" / "kernels"
        kernels.mkdir(parents=True)
        (kernels / "stray-file.txt").write_text("ignore me")
        (kernels / "real-kernel").mkdir()
        assert tc.available_kernels() == ["real-kernel"]


class TestOutputDirs:
    def test_image_output_dir(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        # Default: paired with the target's default kernel.
        assert (
            tc.image_output_dir()
            == tmp_targets / "output" / "rocky9" / "x86_64" / "images" / "5.14-rhel9.7"
        )

    def test_image_output_dir_explicit_kernel(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert (
            tc.image_output_dir("5.14-rhel9.5")
            == tmp_targets / "output" / "rocky9" / "x86_64" / "images" / "5.14-rhel9.5"
        )

    def test_image_output_dir_distinct_per_kernel(
        self, tmp_targets: Path
    ) -> None:
        tc = _make_config(tmp_targets)
        assert tc.image_output_dir("5.14-rhel9.7") != tc.image_output_dir(
            "5.14-rhel9.5"
        )

    def test_image_output_dir_under_arch_subdir(
        self, tmp_targets: Path
    ) -> None:
        """Cross-arch build routes per-kernel images under the arch dir."""
        tc = _make_config(tmp_targets, arch="aarch64")
        assert tc.image_output_dir("5.14-rhel9.7") == (
            tmp_targets
            / "output"
            / "rocky9"
            / "aarch64"
            / "images"
            / "5.14-rhel9.7"
        )
        # Still distinct per-kernel under the arch subdir.
        assert tc.image_output_dir("5.14-rhel9.7") != tc.image_output_dir(
            "5.14-rhel9.5"
        )

    def test_container_output_dir(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.container_output_dir() == (
            tmp_targets / "output" / "rocky9" / "x86_64" / "container"
        )


class TestInputHash:
    def test_different_artifacts_different_hash(
        self, tmp_targets: Path
    ) -> None:
        tc = _make_config(tmp_targets)
        hashes = {
            tc.input_hash("container"),
            tc.input_hash("kernel"),
            tc.input_hash("image"),
        }
        assert len(hashes) == 3

    def test_container_hash_changes_with_dockerfile(
        self, tmp_targets: Path
    ) -> None:
        tc = _make_config(tmp_targets)
        h1 = tc.input_hash("container")
        df = tmp_targets / "targets" / "rocky9" / "container.Dockerfile"
        df.write_text("FROM rockylinux:9.7\nRUN echo changed\n")
        h2 = tc.input_hash("container")
        assert h1 != h2

    def test_kernel_hash_changes_with_override(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        h1 = tc.input_hash("kernel")
        # Add a new kernel config override via targets.yaml
        data = yaml.safe_load(
            (tmp_targets / "targets" / "targets.yaml").read_text()
        )
        data["targets"]["rocky9"]["kernels"]["config"]["CONFIG_NEW"] = "m"
        _write_targets_yaml(tmp_targets / "targets", data)
        tc2 = _make_config(tmp_targets)
        h2 = tc2.input_hash("kernel")
        assert h1 != h2

    def test_image_hash_changes_with_packages(self, tmp_targets: Path) -> None:
        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            tc = cfg.TargetConfig("rocky9")
            h1 = tc.input_hash("image")
            pkg = tmp_targets / "targets" / "common" / "packages-base.txt"
            pkg.write_text("bash\ncoreutils\nnew-package\n")
            h2 = tc.input_hash("image")
        assert h1 != h2

    def test_image_hash_includes_server_packages(
        self, tmp_targets: Path
    ) -> None:
        """Image hash for a server target includes server packages."""
        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            tc = cfg.TargetConfig("rocky9")
            h1 = tc.input_hash("image")
            pkg = tmp_targets / "targets" / "common" / "packages-server.txt"
            pkg.write_text("nfs-utils\nextra-server-pkg\n")
            h2 = tc.input_hash("image")
        assert h1 != h2

class TestStaleness:
    def test_stale_when_no_meta(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.is_stale("container") is True

    def test_not_stale_after_write_meta(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        tc.write_meta("container")
        assert tc.is_stale("container") is False

    def test_stale_after_input_change(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        tc.write_meta("container")
        assert tc.is_stale("container") is False
        df = tmp_targets / "targets" / "rocky9" / "container.Dockerfile"
        df.write_text("FROM rockylinux:9.7\nRUN echo changed\n")
        assert tc.is_stale("container") is True

    def test_image_staleness_per_kernel(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        tc.write_meta("image", kernel="5.14-rhel9.7")
        assert tc.is_stale("image", kernel="5.14-rhel9.7") is False
        # Other kernel's image still stale (no meta written for it).
        assert tc.is_stale("image", kernel="5.14-rhel9.5") is True

    def test_image_hash_per_kernel_distinct(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        h1 = tc.input_hash("image", kernel="5.14-rhel9.7")
        h2 = tc.input_hash("image", kernel="5.14-rhel9.5")
        assert h1 != h2

    def test_kernel_staleness(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.is_stale("kernel") is True
        tc.write_meta("kernel")
        assert tc.is_stale("kernel") is False

    def test_kernel_staleness_with_explicit_name(
        self, tmp_targets: Path
    ) -> None:
        tc = _make_config(tmp_targets)
        tc.write_meta("kernel", kernel="custom-kernel")
        assert tc.is_stale("kernel", kernel="custom-kernel") is False
        assert tc.is_stale("kernel", kernel="other-kernel") is True


class TestWriteMeta:
    def test_writes_json(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        tc.write_meta("container", build_date="2024-01-01")
        meta_path = (
            tmp_targets / "output" / "rocky9" / "x86_64" / "container" / "meta.json"
        )
        assert meta_path.exists()
        data = json.loads(meta_path.read_text())
        assert data["target"] == "rocky9"
        assert data["build_date"] == "2024-01-01"
        assert "input_hash" in data

    def test_kernel_meta_under_kernel_dir(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        tc.write_meta("kernel", kernel="5.14-rhel9.7", version="5.14.0")
        meta_path = (
            tmp_targets
            / "output"
            / "rocky9"
            / "x86_64"
            / "kernels"
            / "5.14-rhel9.7"
            / "meta.json"
        )
        assert meta_path.exists()
        data = json.loads(meta_path.read_text())
        assert data["version"] == "5.14.0"

    def test_creates_dirs(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        tc.write_meta("image")
        meta_path = (
            tmp_targets
            / "output"
            / "rocky9"
            / "x86_64"
            / "images"
            / "5.14-rhel9.7"
            / "meta.json"
        )
        assert meta_path.exists()


class TestDeclaredKernels:
    def test_returns_available_list(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        result = tc.declared_kernels()
        assert result == ["5.14-rhel9.7", "5.14-rhel9.5"]

    def test_default_always_included(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.default_kernel in tc.declared_kernels()

    def test_default_not_duplicated(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        result = tc.declared_kernels()
        assert result.count(tc.default_kernel) == 1

    def test_custom_available_list(self, tmp_targets: Path) -> None:
        data = yaml.safe_load(
            (tmp_targets / "targets" / "targets.yaml").read_text()
        )
        data["targets"]["rocky9"]["kernels"]["available"] = [
            "5.14-rhel9.7",
            "6.1-rhel9.7",
        ]
        _write_targets_yaml(tmp_targets / "targets", data)
        tc = _make_config(tmp_targets)
        result = tc.declared_kernels()
        assert "5.14-rhel9.7" in result
        assert "6.1-rhel9.7" in result


class TestKernelOverrides:
    """Mixed string + mapping entries in kernels.available."""

    def _yaml_with_override(
        self, tmp_targets: Path, override_entry: dict
    ) -> None:
        data = yaml.safe_load(
            (tmp_targets / "targets" / "targets.yaml").read_text()
        )
        data["targets"]["rocky9"]["kernels"]["available"] = [
            "5.14-rhel9.7",
            override_entry,
        ]
        _write_targets_yaml(tmp_targets / "targets", data)

    def test_mapping_entry_declared(self, tmp_targets: Path) -> None:
        self._yaml_with_override(
            tmp_targets,
            {"name": "5.14-rhel9.5", "srpm_version": "5.14.0-503.11.1.el9_5"},
        )
        tc = _make_config(tmp_targets)
        assert "5.14-rhel9.5" in tc.declared_kernels()
        assert "5.14-rhel9.7" in tc.declared_kernels()

    def test_overrides_returned_for_mapping(self, tmp_targets: Path) -> None:
        self._yaml_with_override(
            tmp_targets,
            {"name": "5.14-rhel9.5", "srpm_version": "5.14.0-503.11.1.el9_5"},
        )
        tc = _make_config(tmp_targets)
        assert tc.kernel_overrides("5.14-rhel9.5") == {
            "srpm_version": "5.14.0-503.11.1.el9_5"
        }

    def test_overrides_empty_for_bare_string(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.kernel_overrides("5.14-rhel9.7") == {}

    def test_overrides_empty_for_unknown(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.kernel_overrides("nonexistent") == {}

    def test_invalid_entry_raises(self, tmp_targets: Path) -> None:
        data = yaml.safe_load(
            (tmp_targets / "targets" / "targets.yaml").read_text()
        )
        data["targets"]["rocky9"]["kernels"]["available"] = [
            {"srpm_version": "5.14.0-503.11.1.el9_5"},
        ]
        _write_targets_yaml(tmp_targets / "targets", data)
        tc = _make_config(tmp_targets)
        with pytest.raises(ValueError, match="Invalid kernel entry"):
            tc.declared_kernels()

    def test_short_kernel_name_matches_mapping_entry(
        self, tmp_targets: Path
    ) -> None:
        self._yaml_with_override(
            tmp_targets,
            {"name": "5.14-rhel9.5", "srpm_version": "5.14.0-503.11.1.el9_5"},
        )
        tc = _make_config(tmp_targets)
        full = "5.14-rhel9.5-5.14.0-503.11.1.el9_5"
        assert tc._short_kernel_name(full) == "5.14-rhel9.5"


class TestLoadMetaSafe:
    """paths.load_meta_safe tolerates corrupt/missing meta.json so a
    crashed build can't brick every subsequent status/build command."""

    def test_missing_returns_none(self, tmp_path: Path) -> None:
        from ltvm_pkg.paths import load_meta_safe

        assert load_meta_safe(tmp_path / "nope.json") is None

    def test_corrupt_returns_none(self, tmp_path: Path) -> None:
        from ltvm_pkg.paths import load_meta_safe

        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json")
        assert load_meta_safe(bad) is None

    def test_truncated_returns_none(self, tmp_path: Path) -> None:
        """A common failure mode is a build that died mid-write,
        leaving an empty file."""
        from ltvm_pkg.paths import load_meta_safe

        empty = tmp_path / "empty.json"
        empty.write_text("")
        assert load_meta_safe(empty) is None

    def test_valid_returns_dict(self, tmp_path: Path) -> None:
        from ltvm_pkg.paths import load_meta_safe

        good = tmp_path / "good.json"
        good.write_text('{"input_hash": "abc", "version": "1.0"}')
        result = load_meta_safe(good)
        assert result == {"input_hash": "abc", "version": "1.0"}


class TestIsStaleCorruption:
    """is_stale must treat corrupt meta.json as stale so the next
    build can overwrite it instead of crashing every command."""

    def test_corrupt_meta_treated_as_stale(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        tc.write_meta("container")
        assert tc.is_stale("container") is False
        # Corrupt the file mid-flight
        meta_file = (
            tmp_targets / "output" / "rocky9" / "x86_64" / "container" / "meta.json"
        )
        meta_file.write_text("{garbage")
        # Must not raise
        assert tc.is_stale("container") is True


class TestOutputDirEnvOverride:
    def test_env_var_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import importlib

        import ltvm_pkg.target_config as cfg

        monkeypatch.setenv("LTVM_OUTPUT_DIR", str(tmp_path))
        importlib.reload(cfg)
        try:
            assert cfg.OUTPUT_DIR == tmp_path
        finally:
            monkeypatch.delenv("LTVM_OUTPUT_DIR", raising=False)
            importlib.reload(cfg)

    def test_default_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        import ltvm_pkg.target_config as cfg

        monkeypatch.delenv("LTVM_OUTPUT_DIR", raising=False)
        importlib.reload(cfg)
        try:
            assert cfg.OUTPUT_DIR == cfg.REPO_ROOT / "output"
        finally:
            importlib.reload(cfg)

    def test_env_var_resolves_to_path_object(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import importlib

        import ltvm_pkg.target_config as cfg

        monkeypatch.setenv("LTVM_OUTPUT_DIR", str(tmp_path))
        importlib.reload(cfg)
        try:
            assert isinstance(cfg.OUTPUT_DIR, Path)
        finally:
            monkeypatch.delenv("LTVM_OUTPUT_DIR", raising=False)
            importlib.reload(cfg)


class TestVariants:
    """Optional per-target variants (e.g. MOFED overlay).

    Design invariants asserted here:
      - base variant exists implicitly; its paths match the pre-variant
        layout so on-disk caches don't get orphaned.
      - changing a variant's inputs does NOT invalidate the base hash.
      - changing base inputs DOES invalidate the variant hash.
      - kernel artifact ignores variant (kernel is shared).
    """

    def _yaml_with_variant(
        self, tmp_targets: Path, variants: dict
    ) -> None:
        data = yaml.safe_load(
            (tmp_targets / "targets" / "targets.yaml").read_text()
        )
        data["targets"]["rocky9"]["variants"] = variants
        _write_targets_yaml(tmp_targets / "targets", data)

    def _write_overlay(
        self, tmp_targets: Path, rel: str, contents: str
    ) -> Path:
        path = tmp_targets / "targets" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        return path

    def test_base_variant_always_present(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert "base" in tc.variants()
        assert tc.variant("base").is_base

    def test_no_declared_variants_by_default(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.declared_variants() == []

    def test_declared_variants(self, tmp_targets: Path) -> None:
        self._yaml_with_variant(
            tmp_targets,
            {"mofed": {"params": {"mofed_version": "24.10-1.1.4.0"}}},
        )
        tc = _make_config(tmp_targets)
        assert tc.declared_variants() == ["mofed"]
        v = tc.variant("mofed")
        assert v.params == {"mofed_version": "24.10-1.1.4.0"}

    def test_unknown_variant_raises(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        with pytest.raises(ValueError, match="unknown variant 'mofed'"):
            tc.variant("mofed")

    def test_reserved_base_name_rejected(self, tmp_targets: Path) -> None:
        self._yaml_with_variant(tmp_targets, {"base": {}})
        with pytest.raises(ValueError, match="reserved variant name"):
            _make_config(tmp_targets)

    def test_non_dict_variant_rejected(self, tmp_targets: Path) -> None:
        self._yaml_with_variant(tmp_targets, {"mofed": "not a dict"})  # type: ignore[dict-item]
        with pytest.raises(ValueError, match="must be a mapping"):
            _make_config(tmp_targets)

    def test_container_output_dir_variant(self, tmp_targets: Path) -> None:
        self._yaml_with_variant(tmp_targets, {"mofed": {}})
        tc = _make_config(tmp_targets)
        base_dir = tmp_targets / "output" / "rocky9" / "x86_64" / "container"
        assert tc.container_output_dir() == base_dir
        assert tc.container_output_dir("base") == base_dir
        assert tc.container_output_dir("mofed") == base_dir / "mofed"

    def test_image_output_dir_variant(self, tmp_targets: Path) -> None:
        self._yaml_with_variant(tmp_targets, {"mofed": {}})
        tc = _make_config(tmp_targets)
        base_dir = (
            tmp_targets
            / "output"
            / "rocky9"
            / "x86_64"
            / "images"
            / "5.14-rhel9.7"
        )
        assert tc.image_output_dir() == base_dir
        assert tc.image_output_dir("5.14-rhel9.7", variant="base") == base_dir
        assert (
            tc.image_output_dir("5.14-rhel9.7", variant="mofed")
            == base_dir / "mofed"
        )

    def test_meta_path_variant(self, tmp_targets: Path) -> None:
        self._yaml_with_variant(tmp_targets, {"mofed": {}})
        tc = _make_config(tmp_targets)
        base_meta = (
            tmp_targets / "output" / "rocky9" / "x86_64" / "container" / "meta.json"
        )
        assert tc.meta_path("container") == base_meta
        assert tc.meta_path("container", variant="mofed") == (
            base_meta.parent / "mofed" / "meta.json"
        )

    def test_base_hash_unchanged_by_variant_declaration(
        self, tmp_targets: Path
    ) -> None:
        """Introducing a variant must not invalidate the base cache."""
        tc = _make_config(tmp_targets)
        h_before = tc.input_hash("container")
        self._yaml_with_variant(
            tmp_targets,
            {"mofed": {"params": {"mofed_version": "24.10-1.1.4.0"}}},
        )
        tc2 = _make_config(tmp_targets)
        h_after = tc2.input_hash("container")
        assert h_before == h_after, (
            "declaring a variant must not change the base input hash"
        )

    def test_variant_hash_differs_from_base(self, tmp_targets: Path) -> None:
        self._yaml_with_variant(
            tmp_targets,
            {"mofed": {"params": {"mofed_version": "24.10-1.1.4.0"}}},
        )
        tc = _make_config(tmp_targets)
        assert tc.input_hash("container") != tc.input_hash(
            "container", variant="mofed"
        )
        assert tc.input_hash("image") != tc.input_hash(
            "image", variant="mofed"
        )

    def test_variant_params_change_invalidates_variant_only(
        self, tmp_targets: Path
    ) -> None:
        self._yaml_with_variant(
            tmp_targets,
            {"mofed": {"params": {"mofed_version": "24.10-1.1.4.0"}}},
        )
        tc = _make_config(tmp_targets)
        h_variant_before = tc.input_hash("container", variant="mofed")
        h_base = tc.input_hash("container")

        self._yaml_with_variant(
            tmp_targets,
            {"mofed": {"params": {"mofed_version": "23.10-2.1.3.0"}}},
        )
        tc2 = _make_config(tmp_targets)
        assert tc2.input_hash("container") == h_base
        assert tc2.input_hash("container", variant="mofed") != h_variant_before

    def test_base_change_invalidates_variant(self, tmp_targets: Path) -> None:
        """Bumping a base input must cascade to the variant."""
        self._yaml_with_variant(tmp_targets, {"mofed": {}})
        tc = _make_config(tmp_targets)
        h_before = tc.input_hash("container", variant="mofed")
        df = tmp_targets / "targets" / "rocky9" / "container.Dockerfile"
        df.write_text("FROM rockylinux:9.7\nRUN echo changed\n")
        tc2 = _make_config(tmp_targets)
        h_after = tc2.input_hash("container", variant="mofed")
        assert h_before != h_after

    def test_variant_overlay_file_folded_in(self, tmp_targets: Path) -> None:
        overlay = self._write_overlay(
            tmp_targets,
            "rocky9/variants/mofed.container.Dockerfile",
            "RUN echo v1\n",
        )
        self._yaml_with_variant(
            tmp_targets,
            {
                "mofed": {
                    "container_overlay": "rocky9/variants/mofed.container.Dockerfile"
                }
            },
        )
        tc = _make_config(tmp_targets)
        h1 = tc.input_hash("container", variant="mofed")
        overlay.write_text("RUN echo v2\n")
        h2 = tc.input_hash("container", variant="mofed")
        assert h1 != h2

    def test_container_overlay_does_not_affect_image_hash(
        self, tmp_targets: Path
    ) -> None:
        """Image hash must only mix in the image overlay, not container."""
        overlay_c = self._write_overlay(
            tmp_targets,
            "rocky9/variants/mofed.container.Dockerfile",
            "RUN echo container\n",
        )
        self._yaml_with_variant(
            tmp_targets,
            {
                "mofed": {
                    "container_overlay": "rocky9/variants/mofed.container.Dockerfile"
                }
            },
        )
        tc = _make_config(tmp_targets)
        h_image_before = tc.input_hash("image", variant="mofed")
        overlay_c.write_text("RUN echo changed\n")
        h_image_after = tc.input_hash("image", variant="mofed")
        assert h_image_before == h_image_after

    def test_kernel_hash_ignores_variant(self, tmp_targets: Path) -> None:
        self._yaml_with_variant(
            tmp_targets,
            {"mofed": {"params": {"mofed_version": "24.10-1.1.4.0"}}},
        )
        tc = _make_config(tmp_targets)
        assert tc.input_hash("kernel") == tc.input_hash(
            "kernel", variant="mofed"
        )

    def test_staleness_per_variant(self, tmp_targets: Path) -> None:
        self._yaml_with_variant(tmp_targets, {"mofed": {}})
        tc = _make_config(tmp_targets)
        tc.write_meta("container", variant="base")
        assert tc.is_stale("container", variant="base") is False
        assert tc.is_stale("container", variant="mofed") is True
        tc.write_meta("container", variant="mofed")
        assert tc.is_stale("container", variant="mofed") is False

    def test_write_meta_records_variant(self, tmp_targets: Path) -> None:
        self._yaml_with_variant(tmp_targets, {"mofed": {}})
        tc = _make_config(tmp_targets)
        tc.write_meta("container", variant="mofed", build_date="2026-04-15")
        meta = json.loads(
            (
                tmp_targets
                / "output"
                / "rocky9"
                / "x86_64"
                / "container"
                / "mofed"
                / "meta.json"
            ).read_text()
        )
        assert meta["variant"] == "mofed"
        assert meta["build_date"] == "2026-04-15"

    def test_write_meta_base_omits_variant_key(self, tmp_targets: Path) -> None:
        """Base meta must not carry a variant field, so old readers that
        don't know about variants keep working."""
        tc = _make_config(tmp_targets)
        tc.write_meta("container")
        meta = json.loads(
            (
                tmp_targets
                / "output"
                / "rocky9"
                / "x86_64"
                / "container"
                / "meta.json"
            ).read_text()
        )
        assert "variant" not in meta

    def test_param_override_affects_hash(self, tmp_targets: Path) -> None:
        self._yaml_with_variant(
            tmp_targets,
            {"mofed": {"params": {"mofed_version": "24.10-1.1.4.0"}}},
        )
        tc = _make_config(tmp_targets)
        v = tc.variant("mofed")
        h_default = v.hash_bytes("container")
        v_over = v.with_param_overrides({"mofed_version": "23.10-2.1.3.0"})
        assert v_over.params["mofed_version"] == "23.10-2.1.3.0"
        assert v.params["mofed_version"] == "24.10-1.1.4.0"  # unchanged
        assert h_default != v_over.hash_bytes("container")


class TestListTargets:
    def test_finds_targets(self, tmp_targets: Path) -> None:
        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            targets = cfg.list_targets()
        assert "rocky9" in targets

    def test_returns_all_declared(self, tmp_targets: Path) -> None:
        import ltvm_pkg.target_config as cfg

        data = {
            "targets": {
                "rocky9": _ROCKY9_YAML["targets"]["rocky9"],
                "ubuntu2404": {
                    "os_name": "ubuntu",
                    "os_version": "24.04",
                    "container_image": "ubuntu:24.04",
                    "lustre": {"mode": "client"},
                    "kernels": {"default": "5.15-ubuntu24", "available": []},
                },
            }
        }
        _write_targets_yaml(tmp_targets / "targets", data)

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            targets = cfg.list_targets()
        assert set(targets) == {"rocky9", "ubuntu2404"}
