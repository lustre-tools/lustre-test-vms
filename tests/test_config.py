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

    def test_server(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.server is True

    def test_arch(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.arch == "x86_64"

    def test_container_image(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.container_image == "rockylinux:9.7"

    def test_lustre_target(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.lustre_target == "5.14-rhel9.7"

    def test_default_kernel(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.default_kernel == tc.lustre_target

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
            "defaults": {"arch": "x86_64", "os_family": "rhel", "server": True},
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
        kernels = tmp_targets / "output" / "rocky9" / "kernels"
        full = "5.14-rhel9.7-5.14.0-611.13.1.el9_7_lustre"
        (kernels / full).mkdir(parents=True)
        assert tc.resolve_kernel("5.14-rhel9.7") == full


class TestKernelOutputDir:
    def test_default_path(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        expected = (
            tmp_targets / "output" / "rocky9" / "kernels" / "5.14-rhel9.7"
        )
        assert tc.kernel_output_dir() == expected

    def test_custom_kernel_path(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        expected = tmp_targets / "output" / "rocky9" / "kernels" / "custom"
        assert tc.kernel_output_dir("custom") == expected


class TestAvailableKernels:
    def test_no_kernels_dir(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.available_kernels() == []

    def test_with_kernels(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        kernels = tmp_targets / "output" / "rocky9" / "kernels"
        (kernels / "5.14-rhel9.7").mkdir(parents=True)
        (kernels / "5.14-rhel9.6").mkdir(parents=True)
        result = tc.available_kernels()
        assert result == ["5.14-rhel9.6", "5.14-rhel9.7"]

    def test_ignores_files(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        kernels = tmp_targets / "output" / "rocky9" / "kernels"
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
            == tmp_targets / "output" / "rocky9" / "images" / "5.14-rhel9.7"
        )

    def test_image_output_dir_explicit_kernel(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert (
            tc.image_output_dir("5.14-rhel9.5")
            == tmp_targets / "output" / "rocky9" / "images" / "5.14-rhel9.5"
        )

    def test_image_output_dir_distinct_per_kernel(
        self, tmp_targets: Path
    ) -> None:
        tc = _make_config(tmp_targets)
        assert tc.image_output_dir("5.14-rhel9.7") != tc.image_output_dir(
            "5.14-rhel9.5"
        )

    def test_container_output_dir(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        assert tc.container_output_dir() == (
            tmp_targets / "output" / "rocky9" / "container"
        )


class TestInputHash:
    def test_container_hash_deterministic(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        h1 = tc.input_hash("container")
        h2 = tc.input_hash("container")
        assert h1 == h2
        assert len(h1) == 16

    def test_kernel_hash_deterministic(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        h1 = tc.input_hash("kernel")
        h2 = tc.input_hash("kernel")
        assert h1 == h2

    def test_image_hash_deterministic(self, tmp_targets: Path) -> None:
        tc = _make_config(tmp_targets)
        h1 = tc.input_hash("image")
        h2 = tc.input_hash("image")
        assert h1 == h2

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

    def test_image_hash_includes_server_packages_even_for_non_server(
        self, tmp_targets: Path
    ) -> None:
        """Image hash includes server packages regardless of `server`.

        The image Dockerfile COPYs packages-server.txt unconditionally,
        so a change to that file invalidates the image hash via the
        Dockerfile COPY scan even when targets.yaml says server=false.
        The `server` field only controls Lustre build's --enable-server.

        (Older behavior was 'exclude server pkgs from non-server image',
        but that was never actually implemented in any Dockerfile and
        the test that asserted it was passing vacuously due to a
        _make_config patch-scope bug.)
        """
        import ltvm_pkg.target_config as cfg

        data = yaml.safe_load(
            (tmp_targets / "targets" / "targets.yaml").read_text()
        )
        data["targets"]["rocky9"]["server"] = False
        _write_targets_yaml(tmp_targets / "targets", data)
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
            tmp_targets / "output" / "rocky9" / "container" / "meta.json"
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
            tmp_targets / "output" / "rocky9" / "container" / "meta.json"
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
                    "server": False,
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
