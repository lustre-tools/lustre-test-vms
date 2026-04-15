"""Package, fetch, and install target artifacts.

One package per (target, arch) containing everything needed to
boot VMs, build Lustre, and optionally deploy pre-built Lustre.
Layout inside a tarball, rooted at <target>/<arch>/:
  - container/image.tar (build container, always included)
  - kernels/<kernel-full-name>/vmlinux
  - kernels/<kernel-full-name>/vmlinuz
  - kernels/<kernel-full-name>/modules/
  - kernels/<kernel-full-name>/build-tree/
  - kernels/<kernel-full-name>/lustre-artifacts/ (optional:
    prebuilt Lustre modules + binaries built against this kernel;
    output of `ltvm build lustre` snapshotted via `snapshot_lustre`)
  - kernels/<kernel-full-name>/meta.json
  - images/<kernel-full-name>/base.ext4 (VM rootfs, keyed per kernel)
  - images/<kernel-full-name>/meta.json

Multiple kernels per target are supported; each kernel has its
own paired image directory.  Users can replace the kernel later
with `ltvm build kernel` if they need custom patches or a
different version.

Lustre prebuilds live UNDER each kernel because Lustre modules
are kernel-version-specific (vermagic check enforces this).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from .paths import load_meta_safe


def _resolve_kernel(output_dir: Path, kernel: str | None) -> tuple[str, Path]:
    """Resolve kernel name and directory.

    If kernel is provided, return (kernel, output_dir/kernels/kernel).
    If kernel is None, auto-detect by scanning output_dir/kernels/ for
    subdirectories containing vmlinux; picks the lexicographically
    latest (typically the newest kernel version).

    Raises ValueError if no kernel can be found.
    """
    kernels_dir = output_dir / "kernels"

    if kernel is not None:
        return kernel, kernels_dir / kernel

    # Auto-detect: scan for subdirectories containing vmlinux
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


def _find_artifacts(
    output_dir: str | Path,
    kernel: str | None = None,
) -> dict[str, Path]:
    """Verify required artifacts exist in output_dir.

    Returns dict of paths or raises ValueError.

    Required:
      vmlinux, vmlinuz, build-tree, modules, image, container

    The container/image.tar file is a `podman save` of the build
    container (e.g. ltvm-build-rocky9) and is mandatory: a fetched
    target without it would fail at the next `ltvm build lustre`
    step with a confusing missing-container error.  We require it at
    package time so the failure surfaces at the publisher, not the
    consumer.

    Optional:
      lustre-artifacts: prebuilt Lustre install tree, included if
      `ltvm build lustre` was run before packaging.

    If kernel is None, auto-detects by scanning output_dir/kernels/
    for a subdirectory containing vmlinux.
    """
    output_dir = Path(output_dir)

    kernel_name, kernel_dir = _resolve_kernel(output_dir, kernel)

    # Per-kernel image layout: output/<target>/images/<kernel>/base.*
    image_dir = output_dir / "images" / kernel_name
    container_dir = output_dir / "container"

    required: dict[str, Path] = {
        "vmlinux": kernel_dir / "vmlinux",
        "vmlinuz": kernel_dir / "vmlinuz",
        "build-tree": kernel_dir / "build-tree",
        "modules": kernel_dir / "modules",
        "container": container_dir / "image.tar",
    }

    image_files: list[Path] = []
    if image_dir.is_dir():
        image_files = list(image_dir.glob("*.ext4"))
    if image_files:
        required["image"] = image_files[0]

    missing = []
    for name, path in required.items():
        if not path.exists():
            missing.append(name)

    if "image" not in required:
        missing.append("image (*.ext4)")

    if missing:
        hints = []
        if "container" in missing:
            hints.append(
                "  Container image: run `ltvm build container <target>`"
                " (package_target will export it automatically)"
            )
        if any(
            m in missing
            for m in ("vmlinux", "vmlinuz", "build-tree", "modules", "image")
        ):
            hints.append("  Build artifacts: run `ltvm build all <target>`")
        hint_text = "\n" + "\n".join(hints) if hints else ""
        raise ValueError(
            f"Missing artifacts in {output_dir} (kernel={kernel_name}): "
            f"{', '.join(missing)}{hint_text}"
        )

    # Prebuilt Lustre is optional -- lives under the kernel directory
    # because Lustre modules are kernel-version-specific.
    lustre_artifacts_dir = kernel_dir / "lustre-artifacts"
    if lustre_artifacts_dir.is_dir():
        required["lustre-artifacts"] = lustre_artifacts_dir

    return required


def snapshot_lustre(
    lustre_tree: str | Path,
    output_dir: str | Path,
    target: str,
    kernel: str | None = None,
    arch: str = "x86_64",
) -> Path:
    """Snapshot the Lustre staging tree for shipping in a fetch tarball.

    Sources from the per-tree staging dir written by `ltvm build lustre`
    (``<lustre_tree>/.ltvm-staging/<target>[/<arch>]/``).  rsyncs the
    DESTDIR install layout (usr/, lib/modules/, sbin/) into
    ``kernels/<kernel>/lustre-artifacts/`` under the canonical
    output dir so the resulting tarball pairs each kernel with its
    matching prebuilt Lustre.

    Raises ValueError if no staging dir exists for the tree+target --
    the caller is expected to run `ltvm build lustre` first.

    Returns the output lustre-artifacts directory path.
    """
    # Lazy import to avoid circular dependency: lustre_build imports
    # from vm_state, which release_package also imports from indirectly.
    from .lustre_build import staging_path

    lustre_tree = Path(lustre_tree).resolve()
    output_dir = Path(output_dir)

    kernel_name, kernel_dir = _resolve_kernel(output_dir, kernel)

    staging_src = staging_path(
        lustre_tree, target, arch=arch, kernel=kernel_name
    )
    if not staging_src.is_dir():
        raise ValueError(
            f"No staging directory at {staging_src} -- "
            f"run `ltvm build lustre {target} --kernel {kernel_name} "
            f"--lustre-tree {lustre_tree}` first"
        )

    ko_files = list(staging_src.rglob("*.ko"))
    if not ko_files:
        raise ValueError(
            f"Staging dir {staging_src} has no .ko files -- "
            f"`ltvm build lustre` may have failed"
        )

    # Verify the staging modules match the target kernel
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

    dest = kernel_dir / "lustre-artifacts"
    print(f"  Snapshotting Lustre staging tree to {dest}")
    print(f"    Staging: {staging_src}")
    print(f"    Source:  {lustre_tree}")
    print(f"    Kernel:  {kernel_name}")
    print(f"    Modules: {len(ko_files)} .ko files")

    # rsync the staging dir; --delete so a stale dest is replaced cleanly.
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

    size_mb = _dir_size_mb(dest)
    print(f"    Snapshot size: {size_mb:.0f} MB")

    # Capture git commit hash if available
    lustre_commit = None
    try:
        r = subprocess.run(
            ["git", "-C", str(lustre_tree), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode == 0:
            lustre_commit = r.stdout.strip()
    except FileNotFoundError:
        pass

    # Write metadata
    meta = {
        "source": str(lustre_tree),
        "kernel": kernel_name,
        "ko_count": len(ko_files),
        "lustre_commit": lustre_commit,
    }
    (dest / ".ltvm-snapshot.json").write_text(json.dumps(meta, indent=2) + "\n")
    if lustre_commit:
        print(f"    Lustre commit: {lustre_commit[:12]}")

    return dest


def _dir_size_mb(path: str | Path) -> float:
    """Get directory size in MB via du."""
    r = subprocess.run(["du", "-sm", str(path)], capture_output=True, text=True)
    if r.returncode == 0:
        return float(r.stdout.split()[0])
    return 0.0


def export_build_container(
    target_name: str, output_dir: str | Path, arch: str = "x86_64"
) -> Path:
    """Export the build container image to output/<target>/container/image.tar.

    Uses `podman save` to write a tarball that can later be loaded
    via `podman load -i` on a fetcher's host.  Build containers are
    mandatory in fetched packages, so package_target calls this
    automatically before packaging.

    Raises RuntimeError if the container image doesn't exist (the
    publisher must have built it via `ltvm build container <target>`)
    or if podman is missing/fails.
    """
    from ltvm_pkg.target_config import build_container_tag

    output_dir = Path(output_dir)
    container_tag = build_container_tag(target_name, arch)
    container_dir = output_dir / "container"
    container_dir.mkdir(parents=True, exist_ok=True)
    image_tar = container_dir / "image.tar"

    # Check the image exists in podman storage before attempting save --
    # `podman save` on a missing image gives a less obvious error.
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
        raise RuntimeError(
            f"Build container '{container_tag}' not found in podman storage.\n"
            f"  Run: ltvm build container {target_name}"
        )

    print(f"  Exporting build container '{container_tag}' -> {image_tar}")
    # podman >=4 errors on `save -o <existing-file>` with
    # "docker-archive doesn't support modifying existing images".
    # Always unlink first so re-packaging is idempotent.
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


def package_target(
    target_name: str,
    output_dir: str | Path,
    kernel: str | None = None,
    dest_dir: str | Path | None = None,
    arch: str = "x86_64",
) -> Path:
    """Create a compressed tarball of all target artifacts.

    kernel: kernel name under kernels/; auto-detected if None.
    arch: architecture of the artifacts (x86_64, aarch64, etc.).
    Returns the path to the created tarball.

    Always exports the build container into the output dir before
    packaging, since the container is a mandatory artifact.

    output_dir is always the arch-qualified target dir
    (output/<target>/<arch>/).  Inside the tarball, paths are
    <target>/<arch>/{kernels,images,container}/...  Fetch extracts
    to OUTPUT_DIR so files land at output/<target>/<arch>/...
    """
    output_dir = Path(output_dir)

    # Export the build container before _find_artifacts checks for it.
    # This is unconditional: every published package must include its
    # build container so the consumer can run `ltvm build lustre` after
    # `ltvm fetch`.
    export_build_container(target_name, output_dir, arch=arch)

    artifacts = _find_artifacts(output_dir, kernel=kernel)

    # tar_base is the directory two levels above the arch dir, i.e. the
    # OUTPUT_DIR root, so tarball entries start with <target>/<arch>/...
    tar_base = output_dir.parent.parent

    if dest_dir is None:
        dest_dir = tar_base
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Resolve actual kernel name for tarball naming
    kernel_name, kernel_dir = _resolve_kernel(output_dir, kernel)

    # Read version from kernel meta
    kernel_meta = kernel_dir / "meta.json"
    meta = load_meta_safe(kernel_meta)
    if meta is None:
        raise RuntimeError(
            f"kernel meta.json missing or unreadable at {kernel_meta} -- "
            f"build the kernel before packaging"
        )
    version = meta.get("kernel_version")
    if not version:
        raise RuntimeError(f"kernel_version missing from {kernel_meta}")

    base_name = f"{target_name}-{arch}-{version}"

    tar_paths = []
    kernel_rel = kernel_dir.relative_to(tar_base)
    image_rel = artifacts["image"].parent.relative_to(tar_base)
    tar_paths.append(str(kernel_rel))
    tar_paths.append(str(image_rel))
    container_dir = output_dir / "container"
    tar_paths.append(str(container_dir.relative_to(tar_base)))

    tarball = dest_dir / f"{base_name}.tar.gz"
    subprocess.run(
        ["tar", "-czf", str(tarball), "-C", str(tar_base)] + tar_paths,
        check=True,
    )

    print(
        f"  Packaging {target_name} (kernel={kernel_name}, arch={arch}) -> {tarball.name}"
    )
    print(f"    Kernel: {artifacts['vmlinux']}")
    print(f"    Image:  {artifacts['image']}")
    if "lustre-artifacts" in artifacts:
        print(f"    Lustre: {artifacts['lustre-artifacts']}")
    size_mb = tarball.stat().st_size / (1024 * 1024)
    print(f"    Size: {size_mb:.0f} MB")

    # Read lustre commit from snapshot metadata if present
    lustre_commit = None
    if "lustre-artifacts" in artifacts:
        snap_meta_data = load_meta_safe(
            artifacts["lustre-artifacts"] / ".ltvm-snapshot.json"
        )
        if snap_meta_data is not None:
            lustre_commit = snap_meta_data.get("lustre_commit")

    # Write a manifest alongside the tarball
    manifest = {
        "target": target_name,
        "kernel": kernel_name,
        "kernel_version": version,
        "arch": arch,
        "contents": list(artifacts.keys()),
        "has_lustre_artifacts": "lustre-artifacts" in artifacts,
        "lustre_commit": lustre_commit,
        "size_bytes": tarball.stat().st_size,
    }
    manifest_path = tarball.with_suffix(tarball.suffix + ".json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    return tarball


def fetch_target(
    target_name: str,
    url: str,
    output_base: str | Path,
    arch: str = "x86_64",
) -> Path:
    """Download and extract a target package.

    url: direct URL to the tarball
    output_base: parent of output/<target>/ (usually the
                 ltvm repo root's output/ directory)
    arch: architecture.  Tarballs always extract to
          output/<target>/<arch>/ regardless of arch.

    Returns the path to the extracted target directory.
    """
    output_base = Path(output_base)
    output_base.mkdir(parents=True, exist_ok=True)

    print(f"  Fetching {target_name} from {url}...")

    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # Download with curl (shows progress).  Timeouts bound the worst case:
        #   --connect-timeout: fail fast if GitHub is unreachable
        #   --max-time: overall ceiling; large tarballs on slow links still
        #               need a generous value, so 10 minutes.
        try:
            r = subprocess.run(
                [
                    "curl",
                    "-fSL",
                    "--progress-bar",
                    "--connect-timeout",
                    "15",
                    "--max-time",
                    "600",
                    "-o",
                    tmp_path,
                    url,
                ],
                check=False,
                timeout=650,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "curl not found -- install curl or run `ltvm install`"
            )
        if r.returncode != 0:
            raise RuntimeError(f"Download failed (rc={r.returncode}): {url}")

        size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
        print(f"    Downloaded: {size_mb:.0f} MB")

        # Extract -- auto-detects compression. --overwrite so a partial
        # local tree can be replaced cleanly; --no-same-owner so files
        # land owned by the running user instead of root from the tar.
        print(f"    Extracting to {output_base}/...")
        try:
            subprocess.run(
                [
                    "tar",
                    "-xf",
                    tmp_path,
                    "-C",
                    str(output_base),
                    "--overwrite",
                    "--no-same-owner",
                ],
                check=True,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "tar not found -- install tar or run `ltvm install`"
            )

    finally:
        os.unlink(tmp_path)

    target_dir = output_base / target_name / arch

    if not target_dir.is_dir():
        raise RuntimeError(
            f"Expected {target_dir} after extraction but not found"
        )

    # Verify the artifacts are there (auto-detect kernel).  This will
    # raise ValueError if container/image.tar is missing -- the only
    # way that happens with a properly published tarball is if someone
    # uploaded a stale package built before container packaging was
    # mandatory.  Refusing to proceed is correct: a fetched target
    # without a build container is half-broken (no `ltvm build lustre`).
    artifacts = _find_artifacts(target_dir)
    print(f"    Artifacts verified in {target_dir}")
    if "lustre-artifacts" in artifacts:
        print("    Includes prebuilt Lustre")

    # Load the build container into podman storage so subsequent
    # `ltvm build lustre <target>` finds it.  We use `podman load`
    # which preserves the original tag (ltvm-build-<target>).
    container_image = artifacts["container"]
    print(f"    Loading build container from {container_image}")
    try:
        load = subprocess.run(
            ["podman", "load", "-i", str(container_image)],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "podman not found -- install podman to load fetched build container"
        )
    if load.returncode != 0:
        raise RuntimeError(
            f"podman load failed (rc={load.returncode}) for {container_image}: "
            f"{load.stderr.strip()}"
        )
    # podman load prints "Loaded image: <repo>:<tag>" on success
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
    elif load.stdout.strip():
        print(f"    podman load output:\n{load.stdout.strip()}")

    return target_dir


