"""Kernel build system for ltvm.

Downloads a kernel SRPM, applies Lustre patches and microvm config,
and builds inside a podman container.  Outputs vmlinux, vmlinuz,
a full build tree (for Lustre module builds), and meta.json.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from .config import TargetConfig

log = logging.getLogger(__name__)

INNER_SCRIPT = Path(__file__).parent / "kernel-build-inner.sh"


# ------------------------------------------------------------------
# Lustre target file parsing
# ------------------------------------------------------------------


def parse_lustre_target(
    lustre_tree: str | Path, lustre_target: str
) -> dict[str, str]:
    """Parse a Lustre .target file for SRPM version info.

    Returns dict with keys: lnxmaj, lnxrel, srpm, series.
    """
    target_file = (
        Path(lustre_tree)
        / "lustre/kernel_patches/targets"
        / f"{lustre_target}.target"
    )
    if not target_file.exists():
        raise FileNotFoundError(f"Lustre target file not found: {target_file}")

    text = target_file.read_text()

    lnxmaj = _shell_var(text, "lnxmaj")
    lnxrel = _shell_var(text, "lnxrel")
    series = _shell_var(text, "SERIES")

    if not lnxmaj or not lnxrel:
        raise ValueError(f"Cannot parse lnxmaj/lnxrel from {target_file}")

    srpm = f"kernel-{lnxmaj}-{lnxrel}.src.rpm"

    return {
        "lnxmaj": lnxmaj,
        "lnxrel": lnxrel,
        "srpm": srpm,
        "series": series or f"{lustre_target}.series",
    }


def _shell_var(text: str, name: str) -> str | None:
    """Extract a simple VAR=value or VAR="value" assignment."""
    # Match: VAR=value, VAR="value", or VAR='value'
    # Also handle shell expansions like ${lnxmaj}-${lnxrel}
    # by doing a second pass.
    m = re.search(rf'^{name}=["\']?([^"\'$\n]+)["\']?\s*$', text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return None


# ------------------------------------------------------------------
# Lustre patch/config resolution
# ------------------------------------------------------------------


class LustreFiles(TypedDict):
    config: Path
    series_file: Path
    patches: list[Path]


def resolve_lustre_files(
    lustre_tree: str | Path,
    lustre_target: str,
    target_info: dict[str, str],
) -> LustreFiles:
    """Locate kernel config, series file, and patch files.

    Returns dict with keys: config, series_file, patches (list).
    """
    lt = Path(lustre_tree)
    kp = lt / "lustre/kernel_patches"

    # Kernel config
    config_glob = (
        f"kernel-{target_info['lnxmaj']}-{lustre_target}-x86_64.config"
    )
    config_path = kp / "kernel_configs" / config_glob
    if not config_path.exists():
        raise FileNotFoundError(f"Kernel config not found: {config_path}")

    # Series file
    series_file = kp / "series" / target_info["series"]
    patches = []
    if series_file.exists():
        for line in series_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patch_path = kp / "patches" / line
            if not patch_path.exists():
                raise FileNotFoundError(f"Patch not found: {patch_path}")
            patches.append(patch_path)

    return {
        "config": config_path,
        "series_file": series_file,
        "patches": patches,
    }


# ------------------------------------------------------------------
# SRPM download
# ------------------------------------------------------------------


def _find_srpm_url(srpm_name: str, base_url: str) -> str:
    """Construct the download URL for a kernel SRPM."""
    return f"{base_url}/{srpm_name}"


def download_srpm(srpm_name: str, cache_dir: str | Path, base_url: str) -> Path:
    """Download a kernel SRPM if not already cached.

    base_url: per-target SRPM base URL from targets.yaml (srpm_url field).
    Returns Path to the downloaded file.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / srpm_name

    if cached.exists():
        log.info("Using cached SRPM: %s", cached)
        return cached

    url = _find_srpm_url(srpm_name, base_url)
    log.info("Downloading SRPM: %s", url)

    subprocess.run(
        ["curl", "-fSL", "--progress-bar", "-o", str(cached), url], check=True
    )

    return cached


# ------------------------------------------------------------------
# Container build
# ------------------------------------------------------------------


def _ensure_container_image(target_config: TargetConfig) -> str:
    """Build the container image if needed.

    Returns the image tag.
    """
    tag = f"ltvm-build-{target_config.name}"
    dockerfile = target_config.target_dir / "container.Dockerfile"

    log.info("Building container image: %s", tag)
    subprocess.run(
        [
            "podman",
            "build",
            "-t",
            tag,
            "-f",
            str(dockerfile),
            str(target_config.target_dir),
        ],
        check=True,
    )

    return tag


# ------------------------------------------------------------------
# Config fragment assembly
# ------------------------------------------------------------------


def _build_config_fragment(target_config: TargetConfig) -> str:
    """Assemble the merged config fragment (common + target).

    Returns the fragment text.
    """
    lines = []

    # Common fragment -- targets_dir is target_dir's parent (targets/)
    targets_dir = target_config.target_dir.parent
    common = targets_dir / "common" / "kernel-config.fragment"
    if common.exists():
        lines.append(common.read_text())

    # Per-target overrides from kernel.conf [config]
    for key, val in target_config.kernel_config_overrides.items():
        lines.append(f"{key}={val}")

    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------
# Main build entry point
# ------------------------------------------------------------------


def build_kernel(
    target_config: TargetConfig,
    lustre_tree: str | Path,
    force: bool = False,
    kernel: str | None = None,
) -> dict[str, object]:
    """Build a kernel for the given target.

    Args:
        target_config: TargetConfig instance
        lustre_tree: Path to a Lustre source tree
        force: Build even if inputs haven't changed
        kernel: Lustre target name to build (defaults to target_config.lustre_target)

    Returns:
        dict with build metadata
    """
    lustre_tree = Path(lustre_tree)
    lustre_target = kernel or target_config.lustre_target

    # Staleness check
    if not force and not target_config.is_stale("kernel", kernel=lustre_target):
        log.info("Kernel is up to date (use force=True to rebuild)")
        return kernel_status(target_config, kernel=kernel)

    # Parse Lustre target file
    target_info = parse_lustre_target(lustre_tree, lustre_target)
    log.info("Kernel SRPM: %s", target_info["srpm"])

    # Resolve Lustre kernel config and patches
    lustre_files = resolve_lustre_files(lustre_tree, lustre_target, target_info)
    lustre_config = lustre_files["config"]
    assert isinstance(lustre_config, Path)
    lustre_patches = lustre_files["patches"]
    assert isinstance(lustre_patches, list)
    log.info("Kernel config: %s", lustre_config)
    log.info("Patches to apply: %d", len(lustre_patches))

    # Compute the full output directory name: <lustre_target>-<lnxmaj>-<lnxrel>
    # e.g. 5.14-rhel9.7-5.14.0-611.13.1.el9_7_lustre
    full_name = (
        f"{lustre_target}-{target_info['lnxmaj']}-{target_info['lnxrel']}"
    )
    log.info("Kernel output directory: kernels/%s", full_name)

    # Migrate old short-name directory if it exists (one-time migration)
    old_out = target_config.output_dir / "kernels" / lustre_target
    new_out = target_config.output_dir / "kernels" / full_name
    if old_out.exists() and not new_out.exists():
        log.info("Migrating kernel dir: %s -> %s", old_out.name, new_out.name)
        old_out.rename(new_out)

    # Download SRPM
    srpm_url = target_config.srpm_url
    if not srpm_url:
        raise ValueError(
            f"Target {target_config.name!r} has no srpm_url configured "
            f"in targets.yaml -- cannot download kernel SRPM"
        )
    cache_dir = target_config.output_dir / "cache"
    srpm_path = download_srpm(target_info["srpm"], cache_dir, srpm_url)

    # Ensure container image
    image_tag = _ensure_container_image(target_config)

    # Prepare output directory (use full name)
    kernel_out = target_config.output_dir / "kernels" / full_name
    kernel_out.mkdir(parents=True, exist_ok=True)
    build_tree = kernel_out / "build-tree"

    # Prepare staging area with patches and config
    with tempfile.TemporaryDirectory(prefix="ltvm-kbuild-") as staging_str:
        staging = Path(staging_str)

        # Copy patches
        patches_dir = staging / "patches"
        patches_dir.mkdir()
        for p in lustre_patches:
            shutil.copy2(p, patches_dir / p.name)

        # Write series file (just filenames)
        series_list = staging / "series"
        series_list.write_text("\n".join(p.name for p in lustre_patches) + "\n")

        # Copy kernel config
        shutil.copy2(lustre_config, staging / "kernel.config")

        # Write config fragment
        frag = _build_config_fragment(target_config)
        (staging / "config.fragment").write_text(frag)

        # Copy inner build script
        shutil.copy2(INNER_SCRIPT, staging / "kernel-build-inner.sh")
        os.chmod(staging / "kernel-build-inner.sh", 0o755)

        # Run build in container
        jobs = os.cpu_count() or 4
        container_cmd = [
            "podman",
            "run",
            "--rm",
            "-v",
            f"{srpm_path}:/input/kernel.src.rpm:ro,Z",
            "-v",
            f"{staging}:/input/staging:ro,Z",
            "-v",
            f"{kernel_out}:/output:Z",
            "-v",
            "ltvm-ccache:/ccache:Z",
            "-e",
            f"JOBS={jobs}",
            "-e",
            f"LNXMAJ={target_info['lnxmaj']}",
            "-e",
            f"LNXREL={target_info['lnxrel']}",
            image_tag,
            "-c",
            "/input/staging/kernel-build-inner.sh",
        ]

        log.info("Starting kernel build in container (j%d)...", jobs)
        subprocess.run(container_cmd, check=True)

    # Verify outputs
    vmlinux = kernel_out / "vmlinux"
    vmlinuz = kernel_out / "vmlinuz"
    if not vmlinux.exists():
        raise RuntimeError("Build failed: vmlinux not found in output")
    if not vmlinuz.exists():
        raise RuntimeError("Build failed: vmlinuz not found in output")

    # Get kernel version from build tree
    krelease = "unknown"
    kr_file = build_tree / "include/config/kernel.release"
    if kr_file.exists():
        krelease = kr_file.read_text().strip()

    vmlinux_size = vmlinux.stat().st_size
    vmlinuz_size = vmlinuz.stat().st_size
    log.info("vmlinux: %.1f MB", vmlinux_size / 1e6)
    log.info("vmlinuz: %.1f MB", vmlinuz_size / 1e6)
    log.info("Kernel version: %s", krelease)

    # Write metadata
    meta = {
        "kernel_version": krelease,
        "srpm": target_info["srpm"],
        "lnxmaj": target_info["lnxmaj"],
        "lnxrel": target_info["lnxrel"],
        "lustre_target": lustre_target,
        "patches_applied": len(lustre_patches),
        "vmlinux_bytes": vmlinux_size,
        "vmlinuz_bytes": vmlinuz_size,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    target_config.write_meta("kernel", kernel=full_name, **meta)

    log.info("Kernel build complete")
    return meta


# ------------------------------------------------------------------
# Status query
# ------------------------------------------------------------------


def kernel_status(
    target_config: TargetConfig, kernel: str | None = None
) -> dict[str, object]:
    """Return kernel build status for a target.

    Args:
        target_config: TargetConfig instance
        kernel: Lustre target name to query (defaults to target_config.lustre_target)

    Returns dict with version, build date, staleness, etc.
    """
    resolved = kernel or target_config.lustre_target
    meta_file = target_config.kernel_output_dir(kernel=resolved) / "meta.json"
    if not meta_file.exists():
        return {
            "built": False,
            "stale": True,
        }

    meta = json.loads(meta_file.read_text())
    return {
        "built": True,
        "stale": target_config.is_stale("kernel", kernel=resolved),
        **meta,
    }
