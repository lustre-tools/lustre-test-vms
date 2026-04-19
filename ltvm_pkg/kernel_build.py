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

from .cross_compile import host_podman_platform
from .lustre_tree import kp_configs, kp_patches, kp_series, kp_targets
from .paths import load_meta_safe
from .podman_run import run_podman_with_cleanup
from .target_config import TARGETS_DIR

if TYPE_CHECKING:
    from .target_config import TargetConfig

log = logging.getLogger(__name__)

INNER_SCRIPT = Path(__file__).parent / "kernel-build-inner.sh"
INNER_SCRIPT_DEB = Path(__file__).parent / "kernel-build-inner-deb.sh"


def _kernel_outputs_complete(kernel_out: Path) -> bool:
    """Return True when a kernel build's expected outputs are on disk.

    Checked: vmlinux, vmlinuz, build-tree/.config, and at least one
    .ko under modules/.
    """
    if not (kernel_out / "vmlinux").exists():
        return False
    if not (kernel_out / "vmlinuz").exists():
        return False
    if not (kernel_out / "build-tree" / ".config").exists():
        return False
    modules_dir = kernel_out / "modules"
    return modules_dir.is_dir() and next(modules_dir.rglob("*.ko"), None) is not None


def _run_kernel_podman(
    container_cmd: list[str], kernel_out: Path
) -> None:
    """Run a kernel-build podman container, tolerating cleanup EOF.

    When podman exits non-zero after the inner build succeeded (macOS
    podman-machine socket drops during container removal), we treat
    the build as a success if the expected kernel outputs are on disk.
    Genuine failures still propagate as ``CalledProcessError``.
    """
    r = run_podman_with_cleanup(container_cmd, check=True)
    if r.returncode == 0:
        return
    if getattr(r, "cleanup_eof", False) and _kernel_outputs_complete(kernel_out):
        log.warning(
            "Kernel build finished but podman cleanup exited %d with an "
            "EOF (macOS podman-machine socket drop).  Artifacts are on "
            "disk; treating as success.",
            r.returncode,
        )
        return
    raise subprocess.CalledProcessError(r.returncode, container_cmd)


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
    targets_dir = kp_targets(lustre_tree)
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


def apply_srpm_override(
    target_info: dict[str, str],
    srpm_version: str | None,
    lustre_target: str,
) -> dict[str, str]:
    """Apply a per-kernel srpm_version override to parsed .target info.

    ``srpm_version`` format: ``"<lnxmaj>-<lnxrel>"`` (e.g. ``"6.12.0-55.41.1.el10_0"``).
    Returns a new dict with lnxmaj/lnxrel/srpm replaced.  When
    ``srpm_version`` is None/empty, returns ``target_info`` unchanged.
    """
    if not srpm_version:
        return target_info
    if "-" not in srpm_version:
        raise ValueError(
            f"kernel {lustre_target!r}: srpm_version override "
            f"{srpm_version!r} must be '<lnxmaj>-<lnxrel>'"
        )
    new_lnxmaj, new_lnxrel = srpm_version.split("-", 1)
    declared = f"{target_info['lnxmaj']}-{target_info['lnxrel']}"
    log.info(
        "Kernel SRPM override: using %s instead of %s",
        srpm_version,
        declared,
    )
    result = dict(target_info)
    result["lnxmaj"] = new_lnxmaj
    result["lnxrel"] = new_lnxrel
    result["srpm"] = f"kernel-{new_lnxmaj}-{new_lnxrel}.src.rpm"
    return result


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
    # Kernel config -- arch-specific name (Lustre uses x86_64 / aarch64).
    config_glob = (
        f"kernel-{target_info['lnxmaj']}-{lustre_target}-{arch}.config"
    )
    _config = kp_configs(lustre_tree) / config_glob
    # No Lustre-provided config -- will extract from SRPM at build time
    config_path: Path | None = _config if _config.exists() else None

    # Series file
    series_file = kp_series(lustre_tree) / target_info["series"]
    patches = []
    if series_file.exists():
        for line in series_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patch_path = kp_patches(lustre_tree) / line
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

    targets_dir = kp_targets(lustre_tree)
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
# SRPM-not-found diagnostics
# ------------------------------------------------------------------


class SrpmNotFoundError(RuntimeError):
    """Raised when every candidate SRPM URL returns 404.

    Carries enough context for a user-friendly multi-line error:
    the requested SRPM, a guess at the latest matching SRPM published
    by the repo (None if offline/unprobeable), the Lustre target
    family name, and the list of siblings we found in the tree.
    """

    def __init__(
        self,
        srpm_name: str,
        latest_available: str | None,
        lustre_target: str,
        family: str,
        siblings: list[str],
        target_name: str,
        lustre_tree: str | Path | None,
    ) -> None:
        self.srpm_name = srpm_name
        self.latest_available = latest_available
        self.lustre_target = lustre_target
        self.family = family
        self.siblings = siblings
        self.target_name = target_name
        self.lustre_tree = lustre_tree
        super().__init__(self._format())

    def _format(self) -> str:
        lines = [f"kernel SRPM {self.srpm_name} not found in Rocky repos", ""]
        if self.latest_available:
            lines += [
                "  Rocky may lag behind RHEL on kernel rebuilds. The latest",
                f"  matching SRPM currently published by Rocky appears to be:",
                f"    {self.latest_available}",
            ]
        else:
            lines += [
                "  Rocky may lag behind RHEL on kernel rebuilds, or the",
                "  repo directory could not be probed (offline?).",
            ]
        suggestion = self._suggestion()
        if suggestion:
            tree = (
                str(self.lustre_tree) if self.lustre_tree else "/path/to/lustre-release"
            )
            lines += [
                "",
                "hint: Try an older Lustre target that Rocky has caught up on:",
                f"  ltvm build kernel {self.target_name} "
                f"--kernel {suggestion} --lustre-tree {tree}",
            ]
        if self.siblings:
            lines += [
                "",
                f"Available Lustre {self.family} targets:",
                "  " + "  ".join(self.siblings),
            ]
        return "\n".join(lines)

    def _suggestion(self) -> str | None:
        """Pick a sibling target to suggest in the hint line."""
        if not self.siblings:
            return None
        if self.lustre_target in self.siblings:
            i = self.siblings.index(self.lustre_target)
            if i > 0:
                return self.siblings[i - 1]
        return self.siblings[0]


def _url_returns_404(url: str, timeout: float = 5.0) -> bool | None:
    """True if HEAD returns 404, False if any other HTTP status, None on
    network/transport error (treat as 'unknown -- probably offline')."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            _ = resp.status
        return False
    except urllib.error.HTTPError as e:
        return e.code == 404
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def _probe_latest_rocky_srpm(
    parent_url: str, srpm_name: str, timeout: float = 5.0
) -> str | None:
    """Scrape the parent directory's HTML index for the highest version
    of kernel-<major>-*.el<N>_<M>.src.rpm.  Returns None on any error."""
    import urllib.error
    import urllib.request

    m = re.match(r"^(kernel-[\d.]+)-.*\.(el\d+_\d+)\.src\.rpm$", srpm_name)
    if not m:
        return None
    prefix, elver = m.group(1), m.group(2)
    pattern = re.compile(
        rf'href="({re.escape(prefix)}-[^"]*?\.{re.escape(elver)}\.src\.rpm)"'
    )
    try:
        with urllib.request.urlopen(parent_url + "/", timeout=timeout) as resp:  # noqa: S310
            html = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    names = sorted(set(pattern.findall(html)), key=_srpm_version_key)
    return names[-1] if names else None


def _srpm_version_key(name: str) -> tuple:
    """Sort key for kernel-<maj>-<rel>.elN_M.src.rpm: numeric tuples."""
    m = re.match(r"^kernel-([\d.]+)-([\d.]+)\.el\d+_\d+\.src\.rpm$", name)
    if not m:
        return ((), ())
    maj = tuple(int(x) for x in m.group(1).split(".") if x.isdigit())
    rel = tuple(int(x) for x in m.group(2).split(".") if x.isdigit())
    return (maj, rel)


def _list_lustre_kernel_targets(
    lustre_tree: str | Path, family: str
) -> list[str]:
    """List Lustre kernel targets matching the given family (e.g. 'rhel9').

    Returns sorted short names like '5.14-rhel9.0' ... '5.14-rhel9.7'.
    """
    targets_dir = kp_targets(lustre_tree)
    if not targets_dir.is_dir():
        return []
    found: set[str] = set()
    for p in targets_dir.iterdir():
        name = p.name
        for suffix in (".target", ".target.in"):
            if name.endswith(suffix):
                short = name[: -len(suffix)]
                # Only include entries shaped like <ver>-<family>.<minor>
                m = re.match(rf"^[\d.]+-{re.escape(family)}\.[\d.]+$", short)
                if m:
                    found.add(short)
                break
    return sorted(found, key=_lustre_target_key)


def _lustre_target_key(name: str) -> tuple:
    """Sort key that orders '5.14-rhel9.2' before '5.14-rhel9.10'."""
    m = re.match(r"^([\d.]+)-([a-z]+\d+)\.([\d.]+)$", name)
    if not m:
        return ((), (), ())
    ver = tuple(int(x) for x in m.group(1).split(".") if x.isdigit())
    minor = tuple(int(x) for x in m.group(3).split(".") if x.isdigit())
    return (ver, m.group(2), minor)


def _lustre_target_family(lustre_target: str) -> str | None:
    """Extract the family from a short target name: '5.14-rhel9.7' -> 'rhel9'."""
    m = re.match(r"^[\d.]+-([a-z]+\d+)\.[\d.]+$", lustre_target)
    return m.group(1) if m else None


def diagnose_srpm_not_found(
    srpm_name: str,
    base_url: str,
    target_name: str,
    lustre_target: str,
    lustre_tree: str | Path | None,
) -> SrpmNotFoundError | None:
    """Decide whether a download_srpm failure is really a 404 across all
    candidates, and if so build a SrpmNotFoundError with helpful context.

    Returns None when at least one candidate URL is reachable but not
    404 (something else went wrong) or when every candidate looks like
    a network/transport failure (likely offline) -- in the offline case
    we still want to produce the friendly error, so we return one with
    ``latest_available=None``.
    """
    urls = [f"{base_url}/{srpm_name}"] + _srpm_fallback_urls(base_url, srpm_name)
    statuses = [_url_returns_404(u) for u in urls]
    any_not_404 = any(s is False for s in statuses)
    if any_not_404:
        return None
    all_offline = all(s is None for s in statuses)

    latest: str | None = None
    if not all_offline:
        for url, status in zip(urls, statuses):
            if status is True:
                parent = url.rsplit("/", 1)[0]
                latest = _probe_latest_rocky_srpm(parent, srpm_name)
                if latest:
                    break

    family = _lustre_target_family(lustre_target) or ""
    siblings = (
        _list_lustre_kernel_targets(lustre_tree, family)
        if lustre_tree and family
        else []
    )
    return SrpmNotFoundError(
        srpm_name=srpm_name,
        latest_available=latest,
        lustre_target=lustre_target,
        family=family,
        siblings=siblings,
        target_name=target_name,
        lustre_tree=lustre_tree,
    )


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

    For non-base variants, first ensures the base container exists
    and then builds a variant container ``FROM`` it with the variant's
    container_overlay Dockerfile applied.  Base and variant containers
    have distinct tags (see build_container_tag) so both can coexist
    on the host.  Returns the (possibly variant-suffixed) tag.
    """
    from .target_config import DEFAULT_VARIANT, build_container_tag

    arch = target_config.arch
    variant = target_config.variant_name

    # Base container -- always built first, even when the caller asked
    # for a variant, since the variant Dockerfile does `FROM <base>`.
    base_tag = build_container_tag(target_config.name, arch, DEFAULT_VARIANT)
    base_dockerfile = target_config.target_dir / "container.Dockerfile"

    # Build the container as the HOST arch, not the target.  The kernel
    # build uses a cross toolchain installed in the image; running the
    # container emulated (target arch on a cross host) would be ~10x
    # slower and bypass the cross-compile path entirely, because
    # uname -m inside qemu-user reports the target and the shared
    # cross-compile-env.sh sees CROSSING=0.
    platform = host_podman_platform()

    log.info("Building container image: %s", base_tag)
    cmd = [
        "podman",
        "build",
        "--platform",
        platform,
        "-t",
        base_tag,
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
        str(base_dockerfile),
        str(TARGETS_DIR),
    ]
    subprocess.run(cmd, check=True)

    if variant == DEFAULT_VARIANT:
        return base_tag

    v = target_config.variant(variant)
    overlay = v.container_overlay
    if overlay is None or not overlay.exists():
        raise RuntimeError(
            f"variant {variant!r}: container_overlay is required but "
            f"missing (checked {overlay})"
        )
    variant_tag = target_config.container_tag  # variant-suffixed

    log.info(
        "Building variant container %s (overlay=%s)", variant_tag, overlay
    )
    # Build arg BASE_TAG lets the overlay Dockerfile say
    # `ARG BASE_TAG` + `FROM ${BASE_TAG}` so the parent image is wired
    # at build time instead of hardcoded.  Variant params are also
    # surfaced as --build-arg VARIANT_<KEY>=value for overlay use
    # (e.g. VARIANT_MOFED_VERSION).
    v_cmd = [
        "podman",
        "build",
        "--platform",
        platform,
        "-t",
        variant_tag,
        "--build-arg",
        f"BASE_TAG={base_tag}",
    ]
    for key, val in sorted(v.params.items()):
        v_cmd += ["--build-arg", f"VARIANT_{key.upper()}={val}"]
    v_cmd += ["-f", str(overlay), str(TARGETS_DIR)]
    subprocess.run(v_cmd, check=True)

    return variant_tag


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
        kernel: Lustre target name to build (defaults to target_config.default_kernel)

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


def _finalize_kernel_build(
    target_config: TargetConfig,
    kernel_out: Path,
    full_name: str,
    lustre_target: str,
    patches_applied: int,
    extra_meta: dict[str, object],
    extra_hash: bytes = b"",
) -> dict[str, object]:
    """Shared tail of the kernel build: verify outputs, read kernel.release,
    log sizes, write meta.json.  Returns the meta dict."""
    vmlinux = kernel_out / "vmlinux"
    vmlinuz = kernel_out / "vmlinuz"
    if not vmlinux.exists():
        raise RuntimeError("Build failed: vmlinux not found in output")
    if not vmlinuz.exists():
        raise RuntimeError("Build failed: vmlinuz not found in output")

    krelease = "unknown"
    kr_file = kernel_out / "build-tree" / "include/config/kernel.release"
    if kr_file.exists():
        krelease = kr_file.read_text().strip()

    vmlinux_size = vmlinux.stat().st_size
    vmlinuz_size = vmlinuz.stat().st_size
    log.info("vmlinux: %.1f MB", vmlinux_size / 1e6)
    log.info("vmlinuz: %.1f MB", vmlinuz_size / 1e6)
    log.info("Kernel version: %s", krelease)

    # Schema: see ltvm_pkg.meta_schema.KernelMeta.
    # target/input_hash are written by TargetConfig.write_meta.
    meta: dict[str, object] = {
        "kernel_version": krelease,
        "lustre_target": lustre_target,
        "patches_applied": patches_applied,
        "vmlinux_bytes": vmlinux_size,
        "vmlinuz_bytes": vmlinuz_size,
        "built_at": datetime.now(timezone.utc).isoformat(),
        **extra_meta,
    }
    target_config.write_meta(
        "kernel", kernel=full_name, extra_hash=extra_hash, **meta
    )
    log.info("Kernel build complete")
    return meta


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
    # Normalise full -> short form (see _build_kernel_srpm for rationale).
    lustre_target = target_config._short_kernel_name(
        kernel or target_config.default_kernel
    )
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

    # Prepare staging area with config fragment (no patches/SRPM)
    with tempfile.TemporaryDirectory(prefix="ltvm-kbuild-") as staging_str:
        staging = Path(staging_str)

        # Empty patches dir and series (no patches for stock kernel)
        (staging / "patches").mkdir()
        (staging / "series").write_text("")

        # Write config fragment
        frag = _build_config_fragment(target_config)
        (staging / "config.fragment").write_text(frag)

        # Copy inner build script + shared cross-compile helper
        shutil.copy2(INNER_SCRIPT_DEB, staging / "kernel-build-inner-deb.sh")
        os.chmod(staging / "kernel-build-inner-deb.sh", 0o755)
        shutil.copy2(
            TARGETS_DIR / "common" / "cross-compile-env.sh",
            staging / "cross-compile-env.sh",
        )

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
        _run_kernel_podman(container_cmd, kernel_out)

    return _finalize_kernel_build(
        target_config,
        kernel_out,
        full_name,
        lustre_target,
        patches_applied=0,
        extra_meta={"deb_source": deb_source},
    )


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
    # Normalise to the short form ("5.14-rhel9.7") since
    # parse_lustre_target + the series/config lookups below are all
    # keyed on the short name.  Callers may legitimately pass either
    # form (e.g. resolve_kernel returns the full cached dir name once
    # a build exists).
    lustre_target = target_config._short_kernel_name(
        kernel or target_config.default_kernel
    )

    # Resolve Lustre patches/config FIRST so we can fold them into the
    # staleness check.  Without this, editing a patch in place doesn't
    # invalidate the cached vmlinuz and `is_stale` returns False --
    # silently skipping the rebuild the user is iterating on (the
    # primary use case this tool exists for).
    target_info = parse_lustre_target(lustre_tree, lustre_target)
    overrides = target_config.kernel_overrides(lustre_target)
    target_info = apply_srpm_override(
        target_info, overrides.get("srpm_version"), lustre_target
    )
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
    try:
        srpm_path = download_srpm(target_info["srpm"], cache_dir, srpm_url)
    except subprocess.CalledProcessError:
        diag = diagnose_srpm_not_found(
            target_info["srpm"],
            srpm_url,
            target_config.name,
            lustre_target,
            lustre_tree,
        )
        if diag is not None:
            raise diag from None
        raise

    # Ensure container image
    image_tag = _ensure_container_image(target_config)

    # Prepare output directory (use full name)
    kernel_out = target_config.output_dir / "kernels" / full_name
    kernel_out.mkdir(parents=True, exist_ok=True)

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

        # Copy inner build script + shared cross-compile helper
        shutil.copy2(INNER_SCRIPT, staging / "kernel-build-inner.sh")
        os.chmod(staging / "kernel-build-inner.sh", 0o755)
        shutil.copy2(
            TARGETS_DIR / "common" / "cross-compile-env.sh",
            staging / "cross-compile-env.sh",
        )

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
        _run_kernel_podman(container_cmd, kernel_out)

    # extra_hash MUST match what was used in the is_stale check above,
    # otherwise the persisted hash and the next-run input_hash diverge
    # and rebuild loops forever.
    return _finalize_kernel_build(
        target_config,
        kernel_out,
        full_name,
        lustre_target,
        patches_applied=len(lustre_patches),
        extra_meta={
            "srpm": target_info["srpm"],
            "lnxmaj": target_info["lnxmaj"],
            "lnxrel": target_info["lnxrel"],
        },
        extra_hash=extra_hash,
    )


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
        kernel: Lustre target name to query (defaults to target_config.default_kernel)
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
    resolved = kernel or target_config.default_kernel
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
