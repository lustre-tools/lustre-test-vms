"""Shared fixtures for ltvm tests."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import yaml

if TYPE_CHECKING:
    from ltvm_pkg.target_config import TargetConfig

_ROCKY9_YAML: dict = {
    "defaults": {"arch": "x86_64", "os_family": "rhel", "server": True},
    "targets": {
        "rocky9": {
            "os_name": "rocky",
            "os_version": "9.7",
            "container_image": "rockylinux:9.7",
            "status": "working",
            "kernels": {
                "default": "5.14-rhel9.7",
                "available": ["5.14-rhel9.7", "5.14-rhel9.5"],
                "config": {"CONFIG_XEN_PVH": "y"},
            },
            "lustre": {"mode": "server_ldiskfs"},
        }
    },
}


def _write_targets_yaml(targets_dir: Path, data: dict | None = None) -> None:
    """Write targets.yaml into targets_dir."""
    (targets_dir / "targets.yaml").write_text(
        yaml.dump(data or _ROCKY9_YAML, default_flow_style=False)
    )


def _make_config(tmp_targets: Path) -> TargetConfig:
    """Instantiate a TargetConfig with patched paths."""
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
        return cfg.TargetConfig("rocky9")


@pytest.fixture
def tmp_targets(tmp_path: Path) -> Path:
    """Create a minimal targets/ tree for TargetConfig tests."""
    common = tmp_path / "targets" / "common"
    common.mkdir(parents=True)
    (common / "packages-base.txt").write_text("bash\ncoreutils\n")
    (common / "packages-dev.txt").write_text("gcc\nmake\n")
    (common / "packages-test.txt").write_text("fio\nattr\n")
    (common / "packages-debug.txt").write_text("gdb\nstrace\n")
    (common / "packages-server.txt").write_text("nfs-utils\n")
    (common / "kernel-config.fragment").write_text(
        "CONFIG_VIRTIO=y\nCONFIG_9P_FS=y\n"
    )

    rocky9 = tmp_path / "targets" / "rocky9"
    rocky9.mkdir(parents=True)

    # Populate rocky9 with real Dockerfiles for tests that read them.
    # Fall back to stubs if the real files aren't present (e.g. CI).
    _real_targets = Path(__file__).parent.parent / "targets"
    for df in ("container.Dockerfile", "image.Dockerfile"):
        real = _real_targets / "rocky9" / df
        if real.exists():
            (rocky9 / df).write_text(real.read_text())
        else:
            (rocky9 / df).write_text("FROM rockylinux:9.7\n# stub\n")

    _write_targets_yaml(tmp_path / "targets")

    # Also create output dir
    (tmp_path / "output" / "rocky9").mkdir(parents=True)

    return tmp_path


@pytest.fixture
def lustre_tree(tmp_path: Path) -> Path:
    """Create a minimal mock Lustre source tree."""
    lt = tmp_path / "lustre-release"
    targets_dir = lt / "lustre" / "kernel_patches" / "targets"
    targets_dir.mkdir(parents=True)

    configs_dir = lt / "lustre" / "kernel_patches" / "kernel_configs"
    configs_dir.mkdir(parents=True)

    series_dir = lt / "lustre" / "kernel_patches" / "series"
    series_dir.mkdir(parents=True)

    patches_dir = lt / "lustre" / "kernel_patches" / "patches"
    patches_dir.mkdir(parents=True)

    # Write a .target file
    (targets_dir / "5.14-rhel9.7.target").write_text(
        textwrap.dedent("""\
            lnxmaj=5.14.0
            lnxrel=503.26.1.el9_7
            SERIES=5.14-rhel9.7.series
        """)
    )

    # Kernel config
    (configs_dir / "kernel-5.14.0-5.14-rhel9.7-x86_64.config").write_text(
        "# kernel config\nCONFIG_X86=y\n"
    )

    # Series file with patches
    (series_dir / "5.14-rhel9.7.series").write_text(
        "patch1.patch\npatch2.patch\n"
    )

    # Patch files
    (patches_dir / "patch1.patch").write_text("--- a/foo\n+++ b/foo\n")
    (patches_dir / "patch2.patch").write_text("--- a/bar\n+++ b/bar\n")

    return lt
