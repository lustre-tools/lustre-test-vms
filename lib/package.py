"""Package, fetch, and install target artifacts.

One package per target containing everything needed to boot
VMs, build Lustre, and optionally deploy pre-built Lustre:
  - kernels/<kernel-name>/vmlinux
  - kernels/<kernel-name>/vmlinuz
  - kernels/<kernel-name>/modules/
  - kernels/<kernel-name>/build-tree/
  - image.ext4 (VM rootfs)
  - kernels/<kernel-name>/lustre/ (optional: pre-built Lustre)
  - meta.json (version, build info)

Multiple kernels are supported under output/<target>/kernels/.
Users can replace the kernel later with `ltvm build-kernel`
if they need custom patches or a different version.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


def _resolve_kernel(output_dir: Path, kernel: str | None) -> tuple[str, Path]:
    """Resolve kernel name and directory.

    If kernel is provided, return (kernel, output_dir/kernels/kernel).
    If kernel is None, auto-detect by scanning output_dir/kernels/ for
    a subdirectory containing vmlinux; picks the first match.

    Raises ValueError if no kernel can be found.
    """
    kernels_dir = output_dir / "kernels"

    if kernel is not None:
        return kernel, kernels_dir / kernel

    # Auto-detect: scan for subdirectories containing vmlinux
    if not kernels_dir.is_dir():
        raise ValueError(
            f"No kernels/ directory in {output_dir}. "
            f"Run 'ltvm build-kernel <target>' to build one."
        )

    candidates = sorted(
        d
        for d in kernels_dir.iterdir()
        if d.is_dir() and (d / "vmlinux").exists()
    )
    if not candidates:
        raise ValueError(
            f"No kernel with vmlinux found under {kernels_dir}. "
            f"Run 'ltvm build-kernel <target>' to build one."
        )

    chosen = candidates[0]
    return chosen.name, chosen


def _find_artifacts(
    output_dir: str | Path,
    kernel: str | None = None,
) -> dict[str, Path]:
    """Verify required artifacts exist in output_dir.

    Returns dict of paths or raises ValueError.
    Lustre is optional -- included if present.

    If kernel is None, auto-detects by scanning output_dir/kernels/
    for a subdirectory containing vmlinux.
    """
    output_dir = Path(output_dir)

    kernel_name, kernel_dir = _resolve_kernel(output_dir, kernel)

    image_dir = output_dir / "image"

    required: dict[str, Path] = {
        "vmlinux": kernel_dir / "vmlinux",
        "vmlinuz": kernel_dir / "vmlinuz",
        "build-tree": kernel_dir / "build-tree",
        "modules": kernel_dir / "modules",
    }

    # Image can be .ext4 or .img
    image_files = list(image_dir.glob("*.ext4")) + list(image_dir.glob("*.img"))
    if image_files:
        required["image"] = image_files[0]

    missing = []
    for name, path in required.items():
        if not path.exists():
            missing.append(name)

    if "image" not in required:
        missing.append("image (*.ext4 or *.img)")

    if missing:
        raise ValueError(
            f"Missing artifacts in {output_dir} (kernel={kernel_name}): "
            f"{', '.join(missing)}\n"
            f"Run 'ltvm build-all <target>' to build them"
        )

    # Lustre is optional -- lives under the kernel directory
    lustre_dir = kernel_dir / "lustre"
    if lustre_dir.is_dir():
        required["lustre"] = lustre_dir

    return required


def snapshot_lustre(
    lustre_tree: str | Path,
    output_dir: str | Path,
    kernel: str | None = None,
) -> Path:
    """Copy the deployable subset of a built Lustre tree
    into output/<target>/kernels/<kernel>/lustre/.

    Includes everything deploy-lustre.sh needs: .ko files,
    libraries, binaries, test scripts, config.  Excludes
    .git, .o files, and kernel_patches (large, not needed
    for deploy).

    kernel must be provided or resolvable via auto-detection.
    Raises ValueError if kernel is None and cannot be auto-detected.

    Returns the output lustre directory path.
    """
    lustre_tree = Path(lustre_tree).resolve()
    output_dir = Path(output_dir)

    kernel_name, kernel_dir = _resolve_kernel(output_dir, kernel)

    dest = kernel_dir / "lustre"

    # Verify the tree looks built
    ko_files = [
        f for f in lustre_tree.rglob("*.ko")
        if "kconftest" not in str(f) and ".staging" not in str(f)
    ]
    if not ko_files:
        raise ValueError(f"No .ko files in {lustre_tree} -- build Lustre first")

    # Verify the .ko files match the target kernel
    meta_file = kernel_dir / "meta.json"
    if meta_file.exists():
        meta = json.loads(meta_file.read_text())
        expected_kver = meta.get("kernel_version", "")
        if expected_kver:
            sample = ko_files[0]
            r = subprocess.run(
                ["modinfo", "-F", "vermagic", str(sample)],
                capture_output=True, text=True, check=False,
            )
            actual_kver = r.stdout.split()[0] if r.returncode == 0 else ""
            if actual_kver and actual_kver != expected_kver:
                raise ValueError(
                    f"Lustre modules built for {actual_kver} but target "
                    f"kernel is {expected_kver}\n"
                    f"  Rebuild: ltvm build-lustre <target> --lustre-tree {lustre_tree} --force"
                )

    print(f"  Snapshotting Lustre tree to {dest}")
    print(f"    Source: {lustre_tree}")
    print(f"    Kernel: {kernel_name}")
    print(f"    Modules: {len(ko_files)} .ko files")

    # rsync the tree, excluding large unnecessary items
    subprocess.run(
        [
            "rsync",
            "-a",
            "--delete",
            "--exclude=.git",
            "--exclude=*.o",
            "--exclude=*.cmd",
            "--exclude=.tmp_*",
            "--exclude=lustre/kernel_patches",
            "--exclude=libcfs/libcfs/util/.libs/*.o",
            str(lustre_tree) + "/",
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
            capture_output=True, text=True, check=False,
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


def package_target(
    target_name: str,
    output_dir: str | Path,
    kernel: str | None = None,
    dest_dir: str | Path | None = None,
) -> Path:
    """Create a compressed tarball of all target artifacts.

    kernel: kernel name under kernels/; auto-detected if None.
    Returns the path to the created tarball.
    """
    output_dir = Path(output_dir)
    artifacts = _find_artifacts(output_dir, kernel=kernel)

    if dest_dir is None:
        dest_dir = output_dir.parent
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Resolve actual kernel name for tarball naming
    kernel_name, kernel_dir = _resolve_kernel(output_dir, kernel)

    # Read version from kernel meta
    kernel_meta = kernel_dir / "meta.json"
    version = "unknown"
    if kernel_meta.exists():
        meta = json.loads(kernel_meta.read_text())
        version = meta.get("kernel_version", "unknown")

    tarball = dest_dir / f"{target_name}-{version}.tar.gz"

    print(f"  Packaging {target_name} (kernel={kernel_name}) -> {tarball.name}")
    print(f"    Kernel: {artifacts['vmlinux']}")
    print(f"    Image:  {artifacts['image']}")
    if "lustre" in artifacts:
        print(f"    Lustre: {artifacts['lustre']}")

    subprocess.run(
        ["tar", "-czf", str(tarball), "-C", str(output_dir.parent), target_name],
        check=True,
    )

    size_mb = tarball.stat().st_size / (1024 * 1024)
    print(f"    Size: {size_mb:.0f} MB")

    # Read lustre commit from snapshot metadata if present
    lustre_commit = None
    if "lustre" in artifacts:
        snap_meta = artifacts["lustre"] / ".ltvm-snapshot.json"
        if snap_meta.exists():
            lustre_commit = json.loads(snap_meta.read_text()).get("lustre_commit")

    # Write a manifest alongside the tarball
    manifest = {
        "target": target_name,
        "kernel": kernel_name,
        "kernel_version": version,
        "contents": list(artifacts.keys()),
        "has_lustre": "lustre" in artifacts,
        "lustre_commit": lustre_commit,
        "size_bytes": tarball.stat().st_size,
    }
    manifest_path = tarball.with_suffix(tarball.suffix + ".json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    return tarball


def fetch_target(target_name: str, url: str, output_base: str | Path) -> Path:
    """Download and extract a target package.

    url: direct URL to the tarball
    output_base: parent of output/<target>/ (usually the
                 ltvm repo root's output/ directory)
    """
    output_base = Path(output_base)
    output_base.mkdir(parents=True, exist_ok=True)

    print(f"  Fetching {target_name} from {url}...")

    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # Download with curl (shows progress)
        r = subprocess.run(
            ["curl", "-fSL", "--progress-bar", "-o", tmp_path, url], check=False
        )
        if r.returncode != 0:
            raise RuntimeError(f"Download failed (rc={r.returncode}): {url}")

        size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
        print(f"    Downloaded: {size_mb:.0f} MB")

        # Extract -- auto-detects compression
        print(f"    Extracting to {output_base}/...")
        subprocess.run(
            ["tar", "-xf", tmp_path, "-C", str(output_base)], check=True
        )

    finally:
        os.unlink(tmp_path)

    target_dir = output_base / target_name
    if not target_dir.is_dir():
        raise RuntimeError(
            f"Expected {target_dir} after extraction but not found"
        )

    # Verify the artifacts are there (auto-detect kernel)
    artifacts = _find_artifacts(target_dir)
    print(f"    Artifacts verified in {target_dir}")
    if "lustre" in artifacts:
        print("    Includes pre-built Lustre")

    return target_dir


# Default install paths (matching vm.py conventions)
_VM_DIR = Path(os.environ.get("LTVM_VM_DIR", "/opt/qemu-vms"))
DEFAULT_KERNEL_DIR = _VM_DIR / "kernel"
DEFAULT_IMAGE_DIR = _VM_DIR / "images"


def install_target(
    target_name: str,
    output_dir: str | Path,
    kernel: str | None = None,
    *,
    kernel_dir: str | Path | None = None,
    image_dir: str | Path | None = None,
) -> dict[str, str]:
    """Install kernel + image to system paths for vm.py.

    kernel: kernel name under kernels/; auto-detected if None.
    This requires sudo (writes to /opt/qemu-vms/).
    Returns dict of installed paths.
    """
    output_dir = Path(output_dir)
    artifacts = _find_artifacts(output_dir, kernel=kernel)

    # Resolve kernel name for installed file naming
    kernel_name, _ = _resolve_kernel(output_dir, kernel)

    kernel_dir = Path(kernel_dir or DEFAULT_KERNEL_DIR)
    image_dir = Path(image_dir or DEFAULT_IMAGE_DIR)

    installed: dict[str, str] = {}

    # Install kernel -- include kernel name in destination filename
    vmlinux_dest = kernel_dir / f"vmlinux-{target_name}-{kernel_name}"
    vmlinuz_dest = kernel_dir / f"vmlinuz-{target_name}-{kernel_name}"

    print(f"  Installing kernel ({kernel_name}) to {kernel_dir}/")
    subprocess.run(["sudo", "mkdir", "-p", str(kernel_dir)], check=True)
    subprocess.run(
        ["sudo", "cp", str(artifacts["vmlinux"]), str(vmlinux_dest)], check=True
    )
    subprocess.run(
        ["sudo", "cp", str(artifacts["vmlinuz"]), str(vmlinuz_dest)], check=True
    )

    # Also install as the default vmlinux if none exists
    default_vmlinux = kernel_dir / "vmlinux"
    if not default_vmlinux.exists():
        subprocess.run(
            ["sudo", "ln", "-sf", str(vmlinux_dest), str(default_vmlinux)],
            check=True,
        )
        print(f"    Default kernel -> vmlinux-{target_name}-{kernel_name}")

    installed["vmlinux"] = str(vmlinux_dest)
    installed["vmlinuz"] = str(vmlinuz_dest)

    # Install image
    image_src = artifacts["image"]
    image_dest = image_dir / f"{target_name}-ltvm.ext4"

    print(f"  Installing image to {image_dir}/")
    subprocess.run(["sudo", "mkdir", "-p", str(image_dir)], check=True)
    subprocess.run(["sudo", "cp", str(image_src), str(image_dest)], check=True)

    installed["image"] = str(image_dest)

    # Note Lustre availability
    if "lustre" in artifacts:
        installed["lustre"] = str(artifacts["lustre"])

    print("  Installed:")
    for k, v in installed.items():
        print(f"    {k}: {v}")

    return installed
