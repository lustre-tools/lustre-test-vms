"""Path helpers for Lustre source tree layout.

Centralises the handful of fixed sub-paths under a Lustre checkout
so kernel_build, lustre_compat, and CLI helpers can't drift on the
spelling.
"""

from __future__ import annotations

from pathlib import Path


def kp_root(tree: str | Path) -> Path:
    """lustre/kernel_patches/ under a Lustre source tree."""
    return Path(tree) / "lustre" / "kernel_patches"


def kp_targets(tree: str | Path) -> Path:
    """lustre/kernel_patches/targets/ (.target and .target.in files)."""
    return kp_root(tree) / "targets"


def kp_configs(tree: str | Path) -> Path:
    """lustre/kernel_patches/kernel_configs/ (per-target kernel .config)."""
    return kp_root(tree) / "kernel_configs"


def kp_series(tree: str | Path) -> Path:
    """lustre/kernel_patches/series/ (patch series files)."""
    return kp_root(tree) / "series"


def kp_patches(tree: str | Path) -> Path:
    """lustre/kernel_patches/patches/ (individual .patch files)."""
    return kp_root(tree) / "patches"


def ldiskfs_series(tree: str | Path) -> Path:
    """ldiskfs/kernel_patches/series/ (ldiskfs series files)."""
    return Path(tree) / "ldiskfs" / "kernel_patches" / "series"


def ldiskfs_patches(tree: str | Path) -> Path:
    """ldiskfs/kernel_patches/patches/ (ldiskfs patch files)."""
    return Path(tree) / "ldiskfs" / "kernel_patches" / "patches"
