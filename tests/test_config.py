"""Tests for lib/config.py -- TargetConfig and helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from lib.config import _infer_os, add_target
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
        import lib.config as cfg

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
        import lib.config as cfg

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
        assert (
            tc.image_output_dir() == tmp_targets / "output" / "rocky9" / "image"
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
        import lib.config as cfg

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
        import lib.config as cfg

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

    def test_image_hash_excludes_server_for_non_server(
        self, tmp_targets: Path
    ) -> None:
        """Image hash for non-server target ignores server packages."""
        data = yaml.safe_load(
            (tmp_targets / "targets" / "targets.yaml").read_text()
        )
        data["targets"]["rocky9"]["server"] = False
        _write_targets_yaml(tmp_targets / "targets", data)
        tc = _make_config(tmp_targets)
        h1 = tc.input_hash("image")
        pkg = tmp_targets / "targets" / "common" / "packages-server.txt"
        pkg.write_text("nfs-utils\nextra-server-pkg\n")
        h2 = tc.input_hash("image")
        assert h1 == h2


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
        meta_path = tmp_targets / "output" / "rocky9" / "image" / "meta.json"
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


class TestListTargets:
    def test_finds_targets(self, tmp_targets: Path) -> None:
        import lib.config as cfg

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
        import lib.config as cfg

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


# ------------------------------------------------------------------
# _infer_os
# ------------------------------------------------------------------


class TestInferOs:
    def test_rockylinux(self) -> None:
        assert _infer_os("rockylinux:9.7") == ("rocky", "rhel", "9.7")

    def test_almalinux(self) -> None:
        assert _infer_os("almalinux:9.4") == ("alma", "rhel", "9.4")

    def test_ubuntu(self) -> None:
        assert _infer_os("ubuntu:24.04") == ("ubuntu", "debian", "24.04")

    def test_debian(self) -> None:
        assert _infer_os("debian:12") == ("debian", "debian", "12")

    def test_centos(self) -> None:
        assert _infer_os("centos:7") == ("centos", "rhel", "7")

    def test_with_registry_prefix(self) -> None:
        assert _infer_os("docker.io/library/ubuntu:22.04") == (
            "ubuntu",
            "debian",
            "22.04",
        )

    def test_unknown_image(self) -> None:
        name, family, ver = _infer_os("suse:15")
        assert family == "unknown"

    def test_no_tag(self) -> None:
        name, family, ver = _infer_os("rockylinux")
        assert name == "rocky"
        assert ver == "unknown"


# ------------------------------------------------------------------
# add_target
# ------------------------------------------------------------------


class TestAddTarget:
    def test_creates_directory_and_files(self, tmp_targets: Path) -> None:
        import lib.config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            result = add_target("alma9", "almalinux:9.4", kernel="5.14-rhel9.4")

        assert result["name"] == "alma9"
        target_dir = Path(result["target_dir"])
        assert target_dir.is_dir()
        assert (target_dir / "container.Dockerfile").exists()
        assert (target_dir / "image.Dockerfile").exists()
        assert (target_dir / "packages-os.txt").exists()

    def test_from_line_substituted(self, tmp_targets: Path) -> None:
        import lib.config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            add_target("alma9", "almalinux:9.4")

        df = tmp_targets / "targets" / "alma9" / "container.Dockerfile"
        content = df.read_text()
        assert content.startswith("FROM almalinux:9.4\n")
        assert "FROM rockylinux" not in content

    def test_yaml_entry_added(self, tmp_targets: Path) -> None:
        import lib.config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            add_target(
                "alma9",
                "almalinux:9.4",
                kernel="5.14-rhel9.4",
                srpm_url="https://example.com/srpms",
            )

        data = yaml.safe_load(
            (tmp_targets / "targets" / "targets.yaml").read_text()
        )
        assert "alma9" in data["targets"]
        entry = data["targets"]["alma9"]
        assert entry["os_name"] == "alma"
        assert entry["container_image"] == "almalinux:9.4"
        assert entry["status"] == "planned"
        assert entry["srpm_url"] == "https://example.com/srpms"
        assert entry["kernels"]["default"] == "5.14-rhel9.4"

    def test_duplicate_target_raises(self, tmp_targets: Path) -> None:
        import lib.config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
            pytest.raises(ValueError, match="already exists"),
        ):
            add_target("rocky9", "rockylinux:9.7")

    def test_debian_target_generates_apt_dockerfile(
        self, tmp_targets: Path
    ) -> None:
        import lib.config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            add_target("debian12", "debian:12", server=False)

        df = tmp_targets / "targets" / "debian12" / "container.Dockerfile"
        content = df.read_text()
        assert "apt-get" in content
        assert "dnf" not in content

    def test_no_server_flag(self, tmp_targets: Path) -> None:
        import lib.config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            add_target("debian12", "debian:12", server=False)

        data = yaml.safe_load(
            (tmp_targets / "targets" / "targets.yaml").read_text()
        )
        assert data["targets"]["debian12"]["server"] is False

    def test_os_family_set_for_non_rhel(self, tmp_targets: Path) -> None:
        import lib.config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            add_target("debian12", "debian:12")

        data = yaml.safe_load(
            (tmp_targets / "targets" / "targets.yaml").read_text()
        )
        assert data["targets"]["debian12"]["os_family"] == "debian"

    def test_rhel_target_omits_os_family(self, tmp_targets: Path) -> None:
        """RHEL is the default, so os_family should not be in the YAML."""
        import lib.config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            add_target("alma9", "almalinux:9.4")

        data = yaml.safe_load(
            (tmp_targets / "targets" / "targets.yaml").read_text()
        )
        assert "os_family" not in data["targets"]["alma9"]
