"""Kernel build system for ltvm.

Downloads a kernel SRPM, applies Lustre patches and microvm config,
and builds inside a podman container.  Outputs vmlinux, vmlinuz,
a full build tree (for Lustre module builds), and meta.json.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from .paths import load_meta_safe
from .target_config import TARGETS_DIR

if TYPE_CHECKING:
    from .target_config import TargetConfig

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

    Thin wrapper around :func:`lustre_compat.parse_target_in` --
    that module owns the authoritative parser; this function
    preserves the dict shape the rest of kernel_build.py expects.
    Historically this function always constructed the SRPM name
    as ``kernel-<lnxmaj>-<lnxrel>.src.rpm`` regardless of what the
    .target file said, so we preserve that behavior here.
    """
    from .lustre_compat import parse_target_in

    # parse_target_in raises FileNotFoundError / ValueError itself.
    # Prefer .target over .target.in (matches historical behavior).
    targets_dir = Path(lustre_tree) / "lustre/kernel_patches/targets"
    plain = targets_dir / f"{lustre_target}.target"
    dotin = targets_dir / f"{lustre_target}.target.in"
    if not plain.exists() and not dotin.exists():
        raise FileNotFoundError(
            f"Lustre target file not found: {targets_dir}/"
            f"{lustre_target}.target[.in]"
        )

    ti = parse_target_in(Path(lustre_tree), lustre_target)
    return {
        "lnxmaj": ti.lnxmaj,
        "lnxrel": ti.lnxrel,
        "srpm": f"kernel-{ti.lnxmaj}-{ti.lnxrel}.src.rpm",
        "series": ti.SERIES,
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
    arch: str = "x86_64",
) -> LustreFiles:
    """Locate kernel config, series file, and patch files.

    Returns dict with keys: config, series_file, patches (list).
    """
    lt = Path(lustre_tree)
    kp = lt / "lustre/kernel_patches"

    # Kernel config -- arch-specific name (Lustre uses x86_64 / aarch64).
    config_glob = (
        f"kernel-{target_info['lnxmaj']}-{lustre_target}-{arch}.config"
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


def lustre_inputs_hash(
    lustre_tree: str | Path,
    lustre_target: str,
    lustre_files: LustreFiles,
) -> bytes:
    """Hash the Lustre-side inputs that affect a kernel build.

    target_config.input_hash() doesn't have access to the Lustre tree,
    so it can't fold these in itself.  Instead, callers compute this
    digest and pass it through ``input_hash(extra=...)`` /
    ``is_stale(extra_hash=...)`` / ``write_meta(extra_hash=...)``.

    Inputs hashed:
      - the .target file (lnxmaj/lnxrel/series/SRPM URL)
      - the series file
      - every patch in the series, in series order
      - the Lustre kernel config (when one exists for this target/arch)

    Editing any of these in place must invalidate the cached vmlinux,
    or `is_stale` returns False and the rebuild the user is iterating
    on gets silently skipped -- exactly the workflow this tool exists
    for.
    """
    h = hashlib.sha256()

    targets_dir = Path(lustre_tree) / "lustre/kernel_patches/targets"
    for name in (f"{lustre_target}.target", f"{lustre_target}.target.in"):
        tf = targets_dir / name
        if tf.exists():
            h.update(tf.read_bytes())
            break

    series_file = lustre_files["series_file"]
    if series_file.exists():
        h.update(series_file.read_bytes())

    # Patches are already in series order from resolve_lustre_files
    for patch in lustre_files["patches"]:
        if patch.is_file():
            h.update(patch.read_bytes())

    config = lustre_files["config"]
    if config is not None and config.is_file():
        h.update(config.read_bytes())

    return h.digest()


# ------------------------------------------------------------------
# SRPM download
# ------------------------------------------------------------------


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

    urls = [f"{base_url}/{srpm_name}"] + _srpm_fallback_urls(base_url, srpm_name)

    # Download to a per-pid temp file in the same directory and rename on
    # success.  A previous interrupted curl could otherwise leave a
    # truncated `cached` file that the next run silently re-uses,
    # causing rpm2cpio to fail with an opaque error inside the
    # container build.  Using a unique tempfile name (not f".{srpm_name}.partial")
    # also keeps two concurrent build-kernel runs from clobbering each other's
    # download.
    import tempfile

    fd, tmp_str = tempfile.mkstemp(
        dir=str(cache_dir), prefix=f".{srpm_name}.", suffix=".partial"
    )
    os.close(fd)
    tmp = Path(tmp_str)
    try:
        last_err: Exception | None = None
        for url in urls:
            log.info("Downloading SRPM: %s", url)
            try:
                subprocess.run(
                    ["curl", "-fSL", "--progress-bar", "-o", str(tmp), url],
                    check=True,
                )
                break
            except subprocess.CalledProcessError as e:
                last_err = e
                log.info("  not found at %s", url)
        else:
            assert last_err is not None
            raise last_err
        tmp.rename(cached)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

    return cached


_ROCKY_PUB_RE = re.compile(r"^(https?://[^/]+)/pub/rocky/(\d+)/(.*)$")
_EL_MINOR_RE = re.compile(r"\.el(\d+)_(\d+)\.src\.rpm$")


def _srpm_fallback_urls(base_url: str, srpm_name: str) -> list[str]:
    """Derive vault fallback URLs for an SRPM when the primary 404s.

    Rocky only keeps the current minor at /pub/rocky/<major>/; older
    minors move to /vault/rocky/<major>.<minor>/.  We detect this from
    the SRPM's ``.elN_M.src.rpm`` suffix and the base URL's shape.
    """
    pub = _ROCKY_PUB_RE.match(base_url)
    minor = _EL_MINOR_RE.search(srpm_name)
    if not pub or not minor:
        return []
    host, major, rest = pub.group(1), pub.group(2), pub.group(3)
    if minor.group(1) != major:
        return []
    return [f"{host}/vault/rocky/{major}.{minor.group(2)}/{rest}/{srpm_name}"]


# ------------------------------------------------------------------
# Container build
# ------------------------------------------------------------------


def _ccache_volume(target_config: TargetConfig) -> str:
    """Return the ccache volume name, arch-qualified for non-default arch.

    The kernel build mounts the volume so a same-target/different-arch
    cross build (e.g. rocky9 x86_64 then rocky9 aarch64) doesn't share
    object files with the native build.  lustre_build derives the
    volume name from the (already arch-qualified) container tag, so
    keep this consistent with that convention.
    """
    arch = target_config.arch
    if arch and arch != "x86_64":
        return f"ltvm-ccache-{target_config.name}-{arch}"
    return f"ltvm-ccache-{target_config.name}"


def _ensure_container_image(target_config: TargetConfig) -> str:
    """Build the container image if needed.

    Returns the image tag.
    """
    # Arch-qualify the tag so cross-compile containers coexist with native
    arch = target_config.arch
    default_arch = "x86_64"
    if arch != default_arch:
        tag = f"ltvm-build-{target_config.name}-{arch}"
    else:
        tag = f"ltvm-build-{target_config.name}"
    dockerfile = target_config.target_dir / "container.Dockerfile"

    log.info("Building container image: %s", tag)
    # Map target arch to podman platform string so cross-arch builds
    # pull the correct base image (e.g. arm64 instead of amd64).
    _arch_to_platform = {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}
    platform = _arch_to_platform.get(arch, "linux/amd64")
    # Build context must be targets/ (parent of target_dir) so that
    # COPY common/... directives in the Dockerfile resolve correctly.
    cmd = [
        "podman",
        "build",
        "--platform",
        platform,
        "-t",
        tag,
        "--build-arg",
        f"BASE_IMAGE={target_config.container_image}",
    ]
    if target_config.kernel_deb_source:
        cmd += [
            "--build-arg",
            f"KERNEL_DEB_SOURCE={target_config.kernel_deb_source}",
        ]
    cmd += [
        "-f",
        str(dockerfile),
        str(TARGETS_DIR),
    ]
    subprocess.run(cmd, check=True)

    return tag


# ------------------------------------------------------------------
# Config fragment assembly
# ------------------------------------------------------------------


def _build_config_fragment(target_config: TargetConfig) -> str:
    """Assemble the merged config fragment (common + arch + target).

    Returns the fragment text.
    """
    lines = []

    # Common fragment -- targets_dir is target_dir's parent (targets/)
    targets_dir = target_config.target_dir.parent
    common = targets_dir / "common" / "kernel-config.fragment"
    if common.exists():
        lines.append(common.read_text())

    # Arch-specific fragment (e.g. kernel-config-aarch64.fragment)
    arch_frag = (
        targets_dir / "common" / f"kernel-config-{target_config.arch}.fragment"
    )
    if arch_frag.exists():
        lines.append(arch_frag.read_text())

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
            f"{_ccache_volume(target_config)}:/ccache:Z",
            "-e",
            f"JOBS={jobs}",
            "-e",
            f"KERNEL_DEB_SOURCE={deb_source}",
            "-e",
            f"TARGET_ARCH={target_config.arch}",
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
    target_config.write_meta(
        "kernel",
        kernel=full_name,
        kernel_version=krelease,
        deb_source=deb_source,
        lustre_target=lustre_target,
        patches_applied=0,
        vmlinux_bytes=vmlinux_size,
        vmlinuz_bytes=vmlinuz_size,
        built_at=meta["built_at"],
    )

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

    # Resolve Lustre patches/config FIRST so we can fold them into the
    # staleness check.  Without this, editing a patch in place doesn't
    # invalidate the cached vmlinuz and `is_stale` returns False --
    # silently skipping the rebuild the user is iterating on (the
    # primary use case this tool exists for).
    target_info = parse_lustre_target(lustre_tree, lustre_target)
    log.info("Kernel SRPM: %s", target_info["srpm"])

    lustre_files = resolve_lustre_files(
        lustre_tree, lustre_target, target_info, arch=target_config.arch
    )
    lustre_config = lustre_files["config"]
    lustre_patches = lustre_files["patches"]
    assert isinstance(lustre_patches, list)
    if lustre_config is not None:
        log.info("Kernel config: %s", lustre_config)
    else:
        log.info("No Lustre kernel config -- will extract from SRPM")
    log.info("Patches to apply: %d", len(lustre_patches))

    extra_hash = lustre_inputs_hash(lustre_tree, lustre_target, lustre_files)

    # Staleness check (now folds in the resolved Lustre inputs)
    if not force and not target_config.is_stale(
        "kernel", kernel=lustre_target, extra_hash=extra_hash
    ):
        log.info("Kernel is up to date (use force=True to rebuild)")
        return kernel_status(
            target_config, kernel=kernel, extra_hash=extra_hash
        )

    # Compute the full output directory name: <lustre_target>-<lnxmaj>-<lnxrel>
    # e.g. 5.14-rhel9.7-5.14.0-611.13.1.el9_7_lustre
    full_name = (
        f"{lustre_target}-{target_info['lnxmaj']}-{target_info['lnxrel']}"
    )
    log.info("Kernel output directory: kernels/%s", full_name)

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
            f"{_ccache_volume(target_config)}:/ccache:Z",
            "-e",
            f"JOBS={jobs}",
            "-e",
            f"LNXMAJ={target_info['lnxmaj']}",
            "-e",
            f"LNXREL={target_info['lnxrel']}",
            "-e",
            f"TARGET_ARCH={target_config.arch}",
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

    # Write metadata.  extra_hash MUST match what was used in the
    # is_stale check above, otherwise the persisted hash and the
    # next-run input_hash diverge and rebuild loops forever.
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
    target_config.write_meta(
        "kernel", kernel=full_name, extra_hash=extra_hash, **meta
    )

    log.info("Kernel build complete")
    return meta


# ------------------------------------------------------------------
# Status query
# ------------------------------------------------------------------


def kernel_status(
    target_config: TargetConfig,
    kernel: str | None = None,
    extra_hash: bytes = b"",
) -> dict[str, object]:
    """Return kernel build status for a target.

    Args:
        target_config: TargetConfig instance
        kernel: Lustre target name to query (defaults to target_config.lustre_target)
        extra_hash: Lustre-inputs hash from kernel_build's caller, when
            available.  ``cmd_status`` doesn't have a Lustre tree on hand
            so it can't recompute the Lustre-inputs portion of the
            staleness hash; in that case we omit the staleness check
            entirely (meta.json existing == build was successful) rather
            than always reporting stale, which would defeat the round 17
            extra_hash plumbing the moment status is run on a non-stale
            kernel.

    Returns dict with version, build date, staleness, etc.
    """
    resolved = kernel or target_config.lustre_target
    meta_file = target_config.kernel_output_dir(kernel=resolved) / "meta.json"
    meta = load_meta_safe(meta_file)
    if meta is None:
        return {
            "built": False,
            "stale": True,
        }

    # When the caller has the Lustre-inputs hash, use it -- this is the
    # accurate staleness check from kernel_build's early-return paths.
    # Otherwise (cmd_status), we cannot honestly compute staleness
    # without the Lustre tree on hand: the round 17 input_hash now
    # depends on per-patch bytes that target_config can't see.  Return
    # a tristate (built=True, stale=None) so callers can show "?"
    # instead of either always-stale (round 17 regression) or
    # always-not-stale (round 18 over-correction).
    stale: bool | None
    if extra_hash:
        stale = target_config.is_stale(
            "kernel", kernel=resolved, extra_hash=extra_hash
        )
    else:
        stale = None
    return {
        "built": True,
        "stale": stale,
        **meta,
    }
