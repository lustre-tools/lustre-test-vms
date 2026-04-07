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
INNER_SCRIPT_DEB = Path(__file__).parent / "kernel-build-inner-deb.sh"


# ------------------------------------------------------------------
# Lustre target file parsing
# ------------------------------------------------------------------


def parse_lustre_target(
    lustre_tree: str | Path, lustre_target: str
) -> dict[str, str]:
    """Parse a Lustre .target file for SRPM version info.

    Returns dict with keys: lnxmaj, lnxrel, srpm, series.
    """
    targets_dir = Path(lustre_tree) / "lustre/kernel_patches/targets"
    target_file = targets_dir / f"{lustre_target}.target"
    if not target_file.exists():
        target_file = targets_dir / f"{lustre_target}.target.in"
    if not target_file.exists():
        raise FileNotFoundError(
            f"Lustre target file not found: {targets_dir}/{lustre_target}.target[.in]"
        )

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
    """Extract a simple VAR=value or VAR="value" assignment.

    Handles literal values and simple ${var} expansions by
    substituting previously parsed variables from the same file.
    """
    # Match the value (allow $ for shell expansions)
    m = re.search(rf'^{name}=["\']?([^"\'\n]+?)["\']?\s*$', text, re.MULTILINE)
    if not m:
        return None
    val = m.group(1).strip()
    # If value contains ${...}, substitute from other vars in the file
    if "${" in val:

        def _sub(match: re.Match[str]) -> str:
            ref = match.group(1)
            resolved = _shell_var(text, ref)
            return resolved if resolved is not None else match.group(0)

        val = re.sub(r"\$\{(\w+)\}", _sub, val)
    return val


# ------------------------------------------------------------------
# Lustre patch/config resolution
# ------------------------------------------------------------------


class LustreFiles(TypedDict):
    config: Path | None
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
    _config = kp / "kernel_configs" / config_glob
    # No Lustre-provided config -- will extract from SRPM at build time
    config_path: Path | None = _config if _config.exists() else None

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
    # Build context must be targets/ (parent of target_dir) so that
    # COPY common/... directives in the Dockerfile resolve correctly.
    subprocess.run(
        [
            "podman",
            "build",
            "-t",
            tag,
            "-f",
            str(dockerfile),
            str(target_config.target_dir.parent),
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
    lustre_tree: str | Path | None,
    force: bool = False,
    kernel: str | None = None,
) -> dict[str, object]:
    """Build a kernel for the given target.

    Dispatches to the deb-based build path for debian-family targets
    (kernel_deb_source set), or the SRPM-based path for RHEL-family.

    Args:
        target_config: TargetConfig instance
        lustre_tree: Path to a Lustre source tree (not needed for deb targets)
        force: Build even if inputs haven't changed
        kernel: Lustre target name to build (defaults to target_config.lustre_target)

    Returns:
        dict with build metadata
    """
    if target_config.kernel_deb_source:
        return _build_kernel_deb(target_config, force=force, kernel=kernel)

    if lustre_tree is None:
        raise ValueError("lustre_tree is required for SRPM-based kernel builds")

    return _build_kernel_srpm(
        target_config, lustre_tree, force=force, kernel=kernel
    )


def _build_kernel_deb(
    target_config: TargetConfig,
    force: bool = False,
    kernel: str | None = None,
) -> dict[str, object]:
    """Build a kernel from a deb linux-source package.

    The build container already has the kernel source installed via apt.
    We extract it, apply the microvm config fragment, and build.
    No Lustre kernel patches or .target file needed -- this is for
    client-only targets where we build against the stock distro kernel.
    """
    lustre_target = kernel or target_config.lustre_target
    deb_source = target_config.kernel_deb_source
    assert deb_source is not None

    # Staleness check
    if not force and not target_config.is_stale("kernel", kernel=lustre_target):
        log.info("Kernel is up to date (use force=True to rebuild)")
        return kernel_status(target_config, kernel=kernel)

    # For deb targets, output dir is just the target name (no SRPM version)
    full_name = lustre_target
    log.info("Kernel output directory: kernels/%s", full_name)

    # Ensure container image
    image_tag = _ensure_container_image(target_config)

    # Prepare output directory
    kernel_out = target_config.output_dir / "kernels" / full_name
    kernel_out.mkdir(parents=True, exist_ok=True)
    build_tree = kernel_out / "build-tree"

    # Prepare staging area with config fragment (no patches/SRPM)
    with tempfile.TemporaryDirectory(prefix="ltvm-kbuild-") as staging_str:
        staging = Path(staging_str)

        # Empty patches dir and series (no patches for stock kernel)
        (staging / "patches").mkdir()
        (staging / "series").write_text("")

        # Write config fragment
        frag = _build_config_fragment(target_config)
        (staging / "config.fragment").write_text(frag)

        # Copy inner build script
        shutil.copy2(INNER_SCRIPT_DEB, staging / "kernel-build-inner-deb.sh")
        os.chmod(staging / "kernel-build-inner-deb.sh", 0o755)

        # Run build in container
        jobs = os.cpu_count() or 4
        container_cmd = [
            "podman",
            "run",
            "--rm",
            "-v",
            f"{staging}:/input/staging:ro,Z",
            "-v",
            f"{kernel_out}:/output:Z",
            "-v",
            f"ltvm-ccache-{target_config.name}:/ccache:Z",
            "-e",
            f"JOBS={jobs}",
            "-e",
            f"KERNEL_DEB_SOURCE={deb_source}",
            image_tag,
            "-c",
            "/input/staging/kernel-build-inner-deb.sh",
        ]

        log.info(
            "Starting deb kernel build in container (j%d, %s)...",
            jobs,
            deb_source,
        )
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
    meta: dict[str, object] = {
        "kernel_version": krelease,
        "deb_source": deb_source,
        "lustre_target": lustre_target,
        "patches_applied": 0,
        "vmlinux_bytes": vmlinux_size,
        "vmlinuz_bytes": vmlinuz_size,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    target_config.write_meta("kernel", kernel=full_name, **meta)

    log.info("Kernel build complete")
    return meta


def _build_kernel_srpm(
    target_config: TargetConfig,
    lustre_tree: str | Path,
    force: bool = False,
    kernel: str | None = None,
) -> dict[str, object]:
    """Build a kernel from a distro SRPM with Lustre patches.

    This is the original RHEL/SLES build path.
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
    lustre_patches = lustre_files["patches"]
    assert isinstance(lustre_patches, list)
    if lustre_config is not None:
        log.info("Kernel config: %s", lustre_config)
    else:
        log.info("No Lustre kernel config -- will extract from SRPM")
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

        # Copy kernel config (empty sentinel if extracting from SRPM)
        if lustre_config is not None:
            shutil.copy2(lustre_config, staging / "kernel.config")
        else:
            (staging / "kernel.config").write_text("")

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
            f"ltvm-ccache-{target_config.name}:/ccache:Z",
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
    meta: dict[str, object] = {
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
