"""Package, fetch, and install target artifacts.

Each (target, arch, kernel, variant) is published as a *set of zstd
tarballs* plus a JSON manifest that ties them together.  The manifest
lists every asset with its sha256 and byte-size so ``ltvm target fetch``
can verify downloads.  GitHub caps release assets at 2 GiB each, so
splitting by artifact keeps us safely under the limit -- a monolithic
tarball would exceed that for any variant carrying MOFED.

Assets per (target, arch, kernel, variant):

  manifest-<key>.json          # always published, ~1 KB
  container-<target>-<arch>[-<variant>].tar.zst  # podman save of builder
  kernel-<target>-<arch>-<kver>.tar.zst          # variant-independent
  image-<target>-<arch>-<kver>[-<variant>].tar.zst  # base.ext4 + meta
  lustre-<target>-<arch>-<kver>[-<variant>].tar.zst # optional (may be absent)

Plus, optionally and via a *separate* command (``ltvm target publish
--image``), a self-contained bootable qcow2:

  bootable-<target>-<arch>-<kver>[-<variant>].qcow2.zst

The bootable asset is not referenced by the manifest: it's a
self-service artifact for users who just want to boot the thing
without ltvm's VM runtime.

``key`` composition -- variant suffix is ``-<variant>`` for non-base,
empty string for base so pre-variant tags read naturally.  Kernel
assets deliberately omit the variant suffix because the kernel is
shared across a target's variants (see lustre_test_vms_v2-9vi).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .paths import load_meta_safe

# Compression level.  zstd -10 with --long=27 hits a sweet spot:
# about 90-95% of -19's ratio on ext4 rootfs content, at ~10x the
# throughput -- the difference between "finishes in under a minute"
# and "burns an hour on a 3 GiB image".  --long=27 unlocks a 128
# MiB window which matters a lot for the duplicate-pattern slack
# inside an ext4.
ZSTD_LEVEL = "10"
ZSTD_LONG = "27"
ZSTD_THREADS = "0"  # "0" = all cores


DEFAULT_VARIANT = "base"


# ---------------------------------------------------------------------------
# Key composition
# ---------------------------------------------------------------------------


def _variant_suffix(variant: str) -> str:
    """Return ``-<variant>`` or empty string when variant is base.

    Keeping the base tag free of ``-base`` makes the common case
    read naturally (e.g. ``image-rocky9-x86_64-5.14.0-611.....tar.zst``)
    and avoids invalidating the mental model of every existing user."""
    return "" if variant == DEFAULT_VARIANT else f"-{variant}"


def _container_asset_name(target: str, arch: str, variant: str) -> str:
    return f"container-{target}-{arch}{_variant_suffix(variant)}.tar.zst"


def _kernel_asset_name(target: str, arch: str, kver: str) -> str:
    # Kernel is variant-independent: same artifact serves all variants
    # of a target, so no variant suffix here.
    return f"kernel-{target}-{arch}-{kver}.tar.zst"


def _image_asset_name(
    target: str, arch: str, kver: str, variant: str
) -> str:
    return f"image-{target}-{arch}-{kver}{_variant_suffix(variant)}.tar.zst"


def _lustre_asset_name(
    target: str, arch: str, kver: str, variant: str
) -> str:
    return f"lustre-{target}-{arch}-{kver}{_variant_suffix(variant)}.tar.zst"


def _bootable_asset_name(
    target: str, arch: str, kver: str, variant: str, ext: str = "qcow2"
) -> str:
    return (
        f"bootable-{target}-{arch}-{kver}{_variant_suffix(variant)}"
        f".{ext}.zst"
    )


def _manifest_name(
    target: str, arch: str, kver: str, variant: str
) -> str:
    return f"manifest-{target}-{arch}-{kver}{_variant_suffix(variant)}.json"


# ---------------------------------------------------------------------------
# Compression + hashing primitives
# ---------------------------------------------------------------------------


def _sha256(path: Path, bufsize: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(bufsize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _check_zstd() -> None:
    """Fail fast if zstd isn't installed.  ltvm install / doctor add
    it as a prerequisite; we re-check here so the publisher gets a
    clear error instead of a subprocess FileNotFoundError."""
    import shutil

    if shutil.which("zstd") is None:
        raise RuntimeError(
            "zstd not found -- install it (e.g. `sudo dnf install zstd` "
            "or `sudo apt-get install zstd`) or run `sudo ltvm install` "
            "to pick it up alongside the other prerequisites."
        )


def _tar_zstd(
    base_dir: Path, entries: list[str], out: Path
) -> None:
    """Create ``out``.tar.zst containing *entries* relative to
    ``base_dir``.  Uses ``tar --use-compress-program`` so we get a
    single-pass pipeline with no intermediate uncompressed tarball.
    """
    _check_zstd()
    compress_prog = (
        f"zstd -{ZSTD_LEVEL} -T{ZSTD_THREADS} --long={ZSTD_LONG}"
    )
    cmd = [
        "tar",
        f"--use-compress-program={compress_prog}",
        "-cf",
        str(out),
        "-C",
        str(base_dir),
        *entries,
    ]
    subprocess.run(cmd, check=True)


def _untar_zstd(tarball: Path, dest: Path) -> None:
    """Extract a .tar.zst into ``dest``.  ``dest`` must already exist.

    tar auto-detects zstd via the ``--zstd`` flag; on ancient tar (<1.31)
    we'd need ``--use-compress-program=zstd`` too.  All our supported
    targets ship tar 1.34+.
    """
    _check_zstd()
    subprocess.run(
        [
            "tar",
            "--zstd",
            "-xf",
            str(tarball),
            "-C",
            str(dest),
            "--overwrite",
            "--no-same-owner",
        ],
        check=True,
    )


def _zstd_file(src: Path, dst: Path) -> None:
    """Compress a single file (not a directory) to ``dst``.zst.
    Used for the bootable qcow2 asset where wrapping a qcow2 in a
    tar would add no structure and just slow things down."""
    _check_zstd()
    subprocess.run(
        [
            "zstd",
            f"-{ZSTD_LEVEL}",
            f"-T{ZSTD_THREADS}",
            f"--long={ZSTD_LONG}",
            "-f",
            "-o",
            str(dst),
            str(src),
        ],
        check=True,
    )


def _unzstd_file(src: Path, dst: Path) -> None:
    _check_zstd()
    subprocess.run(
        ["zstd", "-d", "-f", "-o", str(dst), str(src)],
        check=True,
    )


# ---------------------------------------------------------------------------
# Artifact discovery
# ---------------------------------------------------------------------------


def _resolve_kernel(output_dir: Path, kernel: str | None) -> tuple[str, Path]:
    """Resolve kernel name and directory under ``output_dir/kernels``.

    Exact match first; otherwise auto-detect by picking the lex-largest
    subdirectory that contains a ``vmlinux``.
    """
    kernels_dir = output_dir / "kernels"

    if kernel is not None:
        return kernel, kernels_dir / kernel

    if not kernels_dir.is_dir():
        raise ValueError(
            f"No kernels/ directory in {output_dir}. "
            f"Run 'ltvm build kernel <target>' to build one."
        )

    candidates = sorted(
        d
        for d in kernels_dir.iterdir()
        if d.is_dir() and (d / "vmlinux").exists()
    )
    if not candidates:
        raise ValueError(
            f"No kernel with vmlinux found under {kernels_dir}. "
            f"Run 'ltvm build kernel <target>' to build one."
        )
    chosen = candidates[-1]
    return chosen.name, chosen


def _variant_paths(
    output_dir: Path, kernel_name: str, variant: str
) -> dict[str, Path]:
    """Return the on-disk paths an artifact ``set`` lives at for the
    given variant, matching TargetConfig's layout convention (base
    variant uses the pre-variant paths; non-base nests one deeper).
    """
    variant_segment = "" if variant == DEFAULT_VARIANT else f"/{variant}"
    return {
        "container_dir": Path(f"{output_dir}/container{variant_segment}"),
        "kernel_dir": output_dir / "kernels" / kernel_name,
        "image_dir": Path(
            f"{output_dir}/images/{kernel_name}{variant_segment}"
        ),
    }


# ---------------------------------------------------------------------------
# Build-container export (podman save)
# ---------------------------------------------------------------------------


def export_build_container(
    target_name: str,
    output_dir: str | Path,
    arch: str = "x86_64",
    variant: str = DEFAULT_VARIANT,
) -> Path:
    """``podman save`` the builder container to ``container[/<variant>]/image.tar``.

    The base-variant tag is ``ltvm-build-<target>[-<arch>]``; variant
    tags gain a ``-<variant>`` suffix (see target_config.build_container_tag).
    The written path follows the same base/variant split TargetConfig
    uses for all other artifacts.
    """
    from ltvm_pkg.target_config import build_container_tag

    output_dir = Path(output_dir)
    container_tag = build_container_tag(target_name, arch, variant)
    container_dir = _variant_paths(output_dir, "__unused__", variant)[
        "container_dir"
    ]
    container_dir.mkdir(parents=True, exist_ok=True)
    image_tar = container_dir / "image.tar"

    try:
        check = subprocess.run(
            ["podman", "image", "exists", container_tag],
            capture_output=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "podman not found -- install podman to package targets"
        )
    if check.returncode != 0:
        v_hint = (
            "" if variant == DEFAULT_VARIANT else f" --variant {variant}"
        )
        raise RuntimeError(
            f"Build container '{container_tag}' not found in podman storage.\n"
            f"  Run: ltvm build container {target_name}{v_hint}"
        )

    print(f"  Exporting build container '{container_tag}' -> {image_tar}")
    if image_tar.exists():
        image_tar.unlink()
    r = subprocess.run(
        ["podman", "save", "-o", str(image_tar), container_tag],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"podman save failed (rc={r.returncode}) for {container_tag}: "
            f"{r.stderr.strip()}"
        )

    size_mb = image_tar.stat().st_size / (1024 * 1024)
    print(f"    Container image: {size_mb:.0f} MB")
    return image_tar


# ---------------------------------------------------------------------------
# Lustre snapshot (unchanged API, light internal cleanup)
# ---------------------------------------------------------------------------


def snapshot_lustre(
    lustre_tree: str | Path,
    output_dir: str | Path,
    target: str,
    kernel: str | None = None,
    arch: str = "x86_64",
    variant: str = DEFAULT_VARIANT,
) -> Path:
    """Snapshot the Lustre staging tree into
    ``kernels/<kver>/lustre-artifacts[/<variant>]/`` so the packager
    can bundle a prebuilt Lustre alongside the kernel.  Staging comes
    from the per-tree dir written by ``ltvm build lustre``.

    For non-base variants, the snapshot nests under a ``<variant>/``
    subdir so base-variant and MOFED-variant Lustre can coexist.
    """
    from .lustre_build import staging_path

    lustre_tree = Path(lustre_tree).resolve()
    output_dir = Path(output_dir)

    kernel_name, kernel_dir = _resolve_kernel(output_dir, kernel)

    staging_src = staging_path(
        lustre_tree, target, arch=arch, kernel=kernel_name, variant=variant
    )
    if not staging_src.is_dir():
        v_hint = (
            "" if variant == DEFAULT_VARIANT else f" --variant {variant}"
        )
        raise ValueError(
            f"No staging directory at {staging_src} -- "
            f"run `ltvm build lustre {target} --kernel {kernel_name} "
            f"--lustre-tree {lustre_tree}{v_hint}` first"
        )

    ko_files = list(staging_src.rglob("*.ko"))
    if not ko_files:
        raise ValueError(
            f"Staging dir {staging_src} has no .ko files -- "
            f"`ltvm build lustre` may have failed"
        )

    # Verify the staging modules match the target kernel via vermagic.
    meta_file = kernel_dir / "meta.json"
    meta = load_meta_safe(meta_file)
    if meta is not None:
        expected_kver = meta.get("kernel_version")
        if not expected_kver:
            raise RuntimeError(
                f"kernel meta.json missing kernel_version: {meta_file}"
            )
        sample = ko_files[0]
        r = subprocess.run(
            ["modinfo", "-F", "vermagic", str(sample)],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError(
                f"modinfo failed to read vermagic from {sample} "
                f"(rc={r.returncode}): {r.stderr.strip()}"
            )
        parts = r.stdout.split()
        actual_kver = parts[0] if parts else ""
        if actual_kver != expected_kver:
            raise ValueError(
                f"Lustre modules built for {actual_kver} but target "
                f"kernel is {expected_kver}\n"
                f"  Rebuild: ltvm build lustre <target> --force"
            )

    dest_parent = kernel_dir / "lustre-artifacts"
    dest = (
        dest_parent if variant == DEFAULT_VARIANT else dest_parent / variant
    )
    print(f"  Snapshotting Lustre staging tree to {dest}")
    print(f"    Staging: {staging_src}")
    print(f"    Source:  {lustre_tree}")
    print(f"    Kernel:  {kernel_name}")
    print(f"    Variant: {variant}")
    print(f"    Modules: {len(ko_files)} .ko files")

    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "rsync",
            "-a",
            "--delete",
            str(staging_src) + "/",
            str(dest) + "/",
        ],
        check=True,
    )

    try:
        r = subprocess.run(
            ["git", "-C", str(lustre_tree), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"git not found; cannot record lustre_commit for {lustre_tree}"
        ) from e
    if r.returncode != 0:
        raise RuntimeError(
            f"git rev-parse HEAD failed for {lustre_tree} "
            f"(rc={r.returncode}): {(r.stderr or '').strip()}"
        )
    lustre_commit = r.stdout.strip()
    if not lustre_commit:
        raise RuntimeError(
            f"git rev-parse HEAD returned empty output for {lustre_tree}"
        )

    snap_meta = {
        "source": str(lustre_tree),
        "kernel": kernel_name,
        "variant": variant,
        "ko_count": len(ko_files),
        "lustre_commit": lustre_commit,
    }
    (dest / ".ltvm-snapshot.json").write_text(
        json.dumps(snap_meta, indent=2) + "\n"
    )
    print(f"    Lustre commit: {lustre_commit[:12]}")
    return dest


# ---------------------------------------------------------------------------
# Packaging: emit split assets + manifest
# ---------------------------------------------------------------------------


def _asset_entry(
    kind: str, path: Path, tar_base: Path
) -> dict[str, Any]:
    """Build a manifest entry for a packaged asset."""
    stat = path.stat()
    return {
        "kind": kind,
        "name": path.name,
        "size": stat.st_size,
        "sha256": _sha256(path),
    }


def package_target(
    target_name: str,
    output_dir: str | Path,
    kernel: str | None = None,
    dest_dir: str | Path | None = None,
    arch: str = "x86_64",
    variant: str = DEFAULT_VARIANT,
) -> dict[str, Path]:
    """Emit the split-asset release for (target, arch, kernel, variant).

    Steps:
      1. Re-export the build container with podman save so the bundled
         image.tar is fresh (mandatory -- a package without a builder
         is useless to the fetcher).
      2. Compress each asset (container / kernel / image / lustre) into
         its own ``.tar.zst`` under ``dest_dir``.
      3. Write a JSON manifest tying the assets together with sha256s
         so ``fetch_target`` can verify downloads.

    Returns a dict of ``{kind: path}`` including the manifest.  The
    ``bootable`` asset is emitted by the separate ``package_bootable``
    function because it's a standalone artifact, not part of the
    ecosystem package.
    """
    _check_zstd()
    output_dir = Path(output_dir)

    # Always re-export the container before packaging so the published
    # image.tar matches whatever is currently tagged in podman.
    export_build_container(target_name, output_dir, arch=arch, variant=variant)

    kernel_name, kernel_dir = _resolve_kernel(output_dir, kernel)
    paths = _variant_paths(output_dir, kernel_name, variant)

    # Sanity: container/kernel/image must exist; lustre is optional.
    required = {
        "container": paths["container_dir"] / "image.tar",
        "kernel_vmlinux": kernel_dir / "vmlinux",
        "kernel_vmlinuz": kernel_dir / "vmlinuz",
        "kernel_build_tree": kernel_dir / "build-tree",
        "kernel_modules": kernel_dir / "modules",
        "image_dir": paths["image_dir"],
    }
    missing = [k for k, p in required.items() if not p.exists()]
    if missing:
        raise ValueError(
            f"missing artifacts for {target_name}/{arch}/{kernel_name}"
            f"/{variant}: {', '.join(missing)}\n"
            f"  Run: ltvm build all {target_name}"
            + (f" --variant {variant}" if variant != DEFAULT_VARIANT else "")
        )

    image_ext4 = next(paths["image_dir"].glob("*.ext4"), None)
    if image_ext4 is None:
        raise ValueError(
            f"no *.ext4 in {paths['image_dir']} -- did `ltvm build image` "
            f"finish successfully?"
        )

    # Read version for naming.
    kmeta = load_meta_safe(kernel_dir / "meta.json")
    if kmeta is None:
        raise RuntimeError(
            f"kernel meta.json missing at {kernel_dir / 'meta.json'}"
        )
    kver = kmeta.get("kernel_version")
    if not kver:
        raise RuntimeError(
            f"kernel_version missing from {kernel_dir / 'meta.json'}"
        )

    if dest_dir is None:
        dest_dir = output_dir.parent.parent  # OUTPUT_DIR root
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Relative-to-output-root paths for tar entries so extraction lands
    # back at output/<target>/<arch>/... verbatim.
    tar_base = output_dir.parent.parent  # == OUTPUT_DIR
    assets: dict[str, Path] = {}

    # ---- container asset ----
    cont_rel = paths["container_dir"].relative_to(tar_base)
    cont_asset = dest_dir / _container_asset_name(target_name, arch, variant)
    print(f"  [container] {cont_asset.name}")
    _tar_zstd(tar_base, [str(cont_rel)], cont_asset)
    assets["container"] = cont_asset

    # ---- kernel asset (variant-independent; same bytes for all variants) ----
    kernel_rel = kernel_dir.relative_to(tar_base)
    kern_asset = dest_dir / _kernel_asset_name(target_name, arch, kver)
    # Exclude lustre-artifacts/ from the kernel asset; it gets its own.
    # tar --exclude is applied after -C, so the path is relative to the
    # content being added (``kernels/<kver>/lustre-artifacts``).
    print(f"  [kernel]    {kern_asset.name}")
    _check_zstd()
    subprocess.run(
        [
            "tar",
            f"--use-compress-program=zstd -{ZSTD_LEVEL} -T{ZSTD_THREADS} --long={ZSTD_LONG}",
            "--exclude",
            f"{kernel_rel}/lustre-artifacts",
            "-cf",
            str(kern_asset),
            "-C",
            str(tar_base),
            str(kernel_rel),
        ],
        check=True,
    )
    assets["kernel"] = kern_asset

    # ---- image asset ----
    image_rel = paths["image_dir"].relative_to(tar_base)
    img_asset = dest_dir / _image_asset_name(target_name, arch, kver, variant)
    print(f"  [image]     {img_asset.name}")
    _tar_zstd(tar_base, [str(image_rel)], img_asset)
    assets["image"] = img_asset

    # ---- lustre asset (optional) ----
    lustre_parent = kernel_dir / "lustre-artifacts"
    lustre_src = (
        lustre_parent
        if variant == DEFAULT_VARIANT
        else lustre_parent / variant
    )
    if lustre_src.is_dir() and (lustre_src / ".ltvm-snapshot.json").exists():
        lustre_rel = lustre_src.relative_to(tar_base)
        lus_asset = dest_dir / _lustre_asset_name(
            target_name, arch, kver, variant
        )
        print(f"  [lustre]    {lus_asset.name}")
        _tar_zstd(tar_base, [str(lustre_rel)], lus_asset)
        assets["lustre"] = lus_asset

    # ---- manifest ----
    manifest = {
        "schema": "ltvm-release/1",
        "target": target_name,
        "arch": arch,
        "kernel": kernel_name,
        "kernel_version": kver,
        "variant": variant,
        "assets": [
            _asset_entry(kind, path, tar_base)
            for kind, path in assets.items()
        ],
    }
    # Pick up lustre_commit from the snapshot meta if present.
    if "lustre" in assets:
        snap_meta = load_meta_safe(
            lustre_src / ".ltvm-snapshot.json"
        )
        if snap_meta is not None:
            manifest["lustre_commit"] = snap_meta.get("lustre_commit")

    manifest_path = dest_dir / _manifest_name(
        target_name, arch, kver, variant
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    assets["manifest"] = manifest_path

    total_mb = sum(
        p.stat().st_size for p in assets.values()
    ) / (1024 * 1024)
    print(f"    Total published size: {total_mb:.0f} MB")

    return assets


# ---------------------------------------------------------------------------
# Bootable asset (standalone, not part of the ecosystem manifest)
# ---------------------------------------------------------------------------


def package_bootable(
    target_name: str,
    output_dir: str | Path,
    kernel: str | None = None,
    dest_dir: str | Path | None = None,
    arch: str = "x86_64",
    variant: str = DEFAULT_VARIANT,
    qcow2_path: str | Path | None = None,
) -> Path:
    """Compress a pre-built bootable qcow2 into a publishable asset.

    Caller is responsible for producing the qcow2 via
    ``ltvm target export`` first; this function only compresses +
    names the result so it uploads cleanly to a GitHub release.

    The bootable asset is deliberately *not* referenced from
    manifest-<...>.json.  It is a self-contained boot artifact for
    end-users who don't want the whole ltvm ecosystem.
    """
    _check_zstd()
    output_dir = Path(output_dir)
    kernel_name, kernel_dir = _resolve_kernel(output_dir, kernel)
    paths = _variant_paths(output_dir, kernel_name, variant)

    if qcow2_path is None:
        qcow2_path = paths["image_dir"] / f"bootable-{kernel_name}.qcow2"
    qcow2_path = Path(qcow2_path)
    if not qcow2_path.exists():
        v_hint = (
            "" if variant == DEFAULT_VARIANT else f" --variant {variant}"
        )
        raise FileNotFoundError(
            f"bootable qcow2 not found at {qcow2_path}.\n"
            f"  Run: ltvm target export {target_name}{v_hint} --format qcow2"
        )

    kmeta = load_meta_safe(kernel_dir / "meta.json")
    if kmeta is None or not kmeta.get("kernel_version"):
        raise RuntimeError(
            f"kernel meta.json missing kernel_version at "
            f"{kernel_dir / 'meta.json'}"
        )
    kver = kmeta["kernel_version"]

    ext = qcow2_path.suffix.lstrip(".") or "qcow2"

    if dest_dir is None:
        dest_dir = output_dir.parent.parent
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    out = dest_dir / _bootable_asset_name(
        target_name, arch, kver, variant, ext=ext
    )
    print(f"  [bootable]  {out.name}")
    _zstd_file(qcow2_path, out)
    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"    Compressed: {size_mb:.0f} MB "
          f"(from {qcow2_path.stat().st_size / (1024 * 1024):.0f} MB)")
    if out.stat().st_size > 2 * 1024 * 1024 * 1024:
        print(
            f"  WARNING: {out.name} is larger than GitHub's 2 GiB asset "
            f"cap; publish will fail.  Consider re-building with a "
            f"smaller image or splitting.",
            file=sys.stderr,
        )
    return out


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _download(url: str, dest: Path, *, quiet: bool = False) -> None:
    """Fetch ``url`` to ``dest`` with curl; fail loudly on non-2xx."""
    flags = ["-fSL", "--connect-timeout", "15", "--max-time", "1800"]
    if not quiet:
        flags.append("--progress-bar")
    try:
        r = subprocess.run(
            ["curl", *flags, "-o", str(dest), url],
            check=False,
            timeout=1850,
        )
    except FileNotFoundError:
        raise RuntimeError("curl not found -- run `sudo ltvm install`")
    if r.returncode != 0:
        raise RuntimeError(f"Download failed (rc={r.returncode}): {url}")


def _expect_sha256(path: Path, expected: str) -> None:
    actual = _sha256(path)
    if actual != expected:
        raise RuntimeError(
            f"sha256 mismatch for {path.name}\n"
            f"  expected: {expected}\n"
            f"  got:      {actual}"
        )


def fetch_target(
    target_name: str,
    manifest_url: str,
    output_base: str | Path,
    arch: str = "x86_64",
    variant: str = DEFAULT_VARIANT,
) -> Path:
    """Download and extract a split-asset release.

    ``manifest_url`` points at ``manifest-<key>.json``.  Fetch reads
    the manifest, downloads each listed asset, verifies sha256, and
    extracts into ``output_base/<target>/<arch>/``.

    After extraction, the build container tar is ``podman load``-ed so
    ``ltvm build lustre`` can find it.
    """
    output_base = Path(output_base)
    output_base.mkdir(parents=True, exist_ok=True)

    print(f"  Fetching manifest from {manifest_url}...")
    with tempfile.TemporaryDirectory(prefix="ltvm-fetch-") as td_str:
        td = Path(td_str)
        manifest_path = td / "manifest.json"
        _download(manifest_url, manifest_path, quiet=True)
        manifest = json.loads(manifest_path.read_text())

        if manifest.get("schema") != "ltvm-release/1":
            raise RuntimeError(
                f"unrecognized manifest schema in {manifest_url}: "
                f"{manifest.get('schema')!r}"
            )
        if manifest.get("target") != target_name:
            raise RuntimeError(
                f"manifest target mismatch: asked for {target_name!r}, "
                f"got {manifest.get('target')!r}"
            )
        m_variant = manifest.get("variant", DEFAULT_VARIANT)
        if m_variant != variant:
            raise RuntimeError(
                f"manifest variant mismatch: asked for {variant!r}, "
                f"got {m_variant!r} -- publish + fetch must agree"
            )

        # URL prefix for assets: same directory as the manifest URL.
        url_prefix = manifest_url.rsplit("/", 1)[0] + "/"

        total_bytes = sum(a["size"] for a in manifest["assets"])
        print(
            f"  {len(manifest['assets'])} assets, "
            f"{total_bytes / (1024 * 1024):.0f} MB total"
        )

        for asset in manifest["assets"]:
            name = asset["name"]
            sha = asset["sha256"]
            size = asset["size"]
            tarball = td / name
            print(
                f"    [{asset['kind']}] {name} "
                f"({size / (1024 * 1024):.0f} MB)"
            )
            _download(url_prefix + name, tarball)
            _expect_sha256(tarball, sha)
            _untar_zstd(tarball, output_base)
            tarball.unlink()  # free disk eagerly on a multi-GB fetch

    target_dir = output_base / target_name / arch
    if not target_dir.is_dir():
        raise RuntimeError(
            f"expected {target_dir} after extraction but not found"
        )

    # Load the build container into podman storage.
    paths = _variant_paths(
        target_dir, manifest["kernel"], variant
    )
    container_image = paths["container_dir"] / "image.tar"
    if not container_image.exists():
        raise RuntimeError(
            f"fetched asset set missing container image at {container_image}"
        )
    print(f"    Loading build container from {container_image}")
    try:
        load = subprocess.run(
            ["podman", "load", "-i", str(container_image)],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "podman not found -- install podman to load the fetched "
            "build container"
        )
    if load.returncode != 0:
        raise RuntimeError(
            f"podman load failed (rc={load.returncode}) for "
            f"{container_image}: {load.stderr.strip()}"
        )
    loaded_line = next(
        (
            ln
            for ln in load.stdout.splitlines()
            if ln.startswith("Loaded image")
        ),
        "",
    )
    if loaded_line:
        print(f"    {loaded_line}")

    return target_dir


def fetch_bootable(
    target_name: str,
    asset_url: str,
    output_base: str | Path,
    arch: str = "x86_64",
    variant: str = DEFAULT_VARIANT,
    expected_sha256: str | None = None,
) -> Path:
    """Download and decompress a single bootable qcow2 asset.

    Unlike ``fetch_target`` this does NOT load any builder container,
    touch the kernel/modules tree, or require the rest of the ltvm
    ecosystem.  It's the "just give me a bootable disk" path.
    """
    output_base = Path(output_base)
    # Land the decompressed qcow2 under the per-kernel image dir so a
    # subsequent `ltvm create --variant <v>` can find it.  We don't know
    # the kernel name from the asset alone -- the file name encodes it,
    # so parse it back out.
    name = asset_url.rsplit("/", 1)[-1]
    if not name.endswith(".zst"):
        raise ValueError(
            f"bootable URL should end in .zst: {asset_url}"
        )
    decompressed_name = name[: -len(".zst")]
    # Parse kver out of the asset name.
    # bootable-<target>-<arch>-<kver>[-<variant>].<ext>
    parts = decompressed_name.split("-")
    if len(parts) < 4 or parts[0] != "bootable":
        raise ValueError(
            f"cannot parse bootable asset name: {decompressed_name!r}"
        )

    # We don't need the kver to DECOMPRESS -- just save alongside the
    # target's output dir so users have a predictable path.
    target_dir = output_base / target_name / arch
    target_dir.mkdir(parents=True, exist_ok=True)
    out_dir = target_dir / "bootable"
    out_dir.mkdir(exist_ok=True)
    dst = out_dir / decompressed_name

    print(f"  Fetching bootable asset from {asset_url}...")
    with tempfile.NamedTemporaryFile(suffix=".zst", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        _download(asset_url, tmp_path)
        if expected_sha256 is not None:
            _expect_sha256(tmp_path, expected_sha256)
        _unzstd_file(tmp_path, dst)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    size_mb = dst.stat().st_size / (1024 * 1024)
    print(f"    Decompressed: {dst} ({size_mb:.0f} MB)")
    return dst
