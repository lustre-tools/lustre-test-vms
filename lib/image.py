"""VM base image builder.

Builds a QEMU microvm root filesystem image for a given target
by building a container image (Dockerfile) and exporting it to
raw ext4.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import TARGETS_DIR

log = logging.getLogger(__name__)

# Default image size before resize2fs shrink (4 GiB)
_IMAGE_SIZE_MB = 4096


def _run(cmd, **kwargs):
    """Run a command, logging it and raising on failure."""
    log.info("Running: %s", " ".join(str(c) for c in cmd))
    return subprocess.run(
        cmd, check=True,
        capture_output=kwargs.pop("capture_output", True),
        text=kwargs.pop("text", True),
        **kwargs,
    )


def _need_root():
    if os.geteuid() != 0:
        raise PermissionError(
            "Image build requires root (mount, losetup). "
            "Run with sudo or use: sudo ltvm build-image <target>")


def _container_image_tag(target_config):
    return f"ltvm-image-{target_config.name}"


def build_image(target_config, force=False):
    """Build a VM base image for the given target.

    Steps:
      1. Build container image via podman build
      2. Export container filesystem to raw ext4
      3. Write meta.json

    Args:
        target_config: TargetConfig instance
        force: rebuild even if inputs unchanged
    """
    _need_root()

    if not force and not target_config.is_stale("image"):
        log.info("Image for %s is up to date, skipping "
                 "(use force=True to rebuild)",
                 target_config.name)
        return target_config.image_output_dir() / "base.ext4"

    out_dir = target_config.image_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    image_path = out_dir / "base.ext4"

    tag = _container_image_tag(target_config)
    dockerfile = (target_config.target_dir /
                  "image.Dockerfile")
    if not dockerfile.exists():
        raise FileNotFoundError(
            f"No image.Dockerfile for target "
            f"{target_config.name}")

    t0 = time.monotonic()

    # ── Step 1: Build container image ──
    log.info("Building container image %s ...", tag)
    _run(["podman", "build",
          "-t", tag,
          "-f", str(dockerfile),
          str(TARGETS_DIR)],
         capture_output=False)

    # ── Step 2: Export to ext4 ──
    log.info("Exporting container to ext4 ...")
    image_path = _export_to_ext4(tag, image_path)

    elapsed = time.monotonic() - t0

    # ── Step 3: Collect metadata ──
    size_mb = image_path.stat().st_size / (1024 * 1024)
    pkg_manifest = _get_package_manifest(tag)

    target_config.write_meta(
        "image",
        build_date=datetime.now(timezone.utc).isoformat(),
        build_seconds=round(elapsed, 1),
        image_size_mb=round(size_mb, 1),
        packages=pkg_manifest,
    )

    log.info("Image built: %s (%.0f MiB, %.0fs)",
             image_path, size_mb, elapsed)
    return image_path


def _export_to_ext4(container_tag, image_path):
    """Create a raw ext4 image from a container's filesystem.

    1. Create empty ext4 file
    2. Mount via loop device
    3. podman create + podman export | tar extract
    4. Unmount
    5. resize2fs -M to shrink
    """
    tmpfile = None
    mountpoint = None
    container_id = None

    try:
        # Create empty ext4 image
        tmpfile = tempfile.mktemp(
            suffix=".ext4", prefix="ltvm-image-")
        _run(["dd", "if=/dev/zero", f"of={tmpfile}",
              "bs=1M", f"count={_IMAGE_SIZE_MB}",
              "status=none"])
        _run(["mkfs.ext4", "-q", tmpfile])

        # Mount it
        mountpoint = tempfile.mkdtemp(prefix="ltvm-mnt-")
        _run(["mount", "-o", "loop", tmpfile, mountpoint])

        # Create a container (not started) and export
        result = _run(["podman", "create", container_tag])
        container_id = result.stdout.strip()

        log.info("Extracting container %s into %s ...",
                 container_id[:12], mountpoint)
        # Pipeline: podman export | tar extract
        export_proc = subprocess.Popen(
            ["podman", "export", container_id],
            stdout=subprocess.PIPE)
        tar_proc = subprocess.Popen(
            ["tar", "-C", mountpoint, "-xf", "-"],
            stdin=export_proc.stdout)
        export_proc.stdout.close()
        tar_proc.communicate()

        if export_proc.wait() != 0:
            raise subprocess.CalledProcessError(
                export_proc.returncode,
                ["podman", "export"])
        if tar_proc.returncode != 0:
            raise subprocess.CalledProcessError(
                tar_proc.returncode, ["tar"])

        # Unmount before resize
        _run(["umount", mountpoint])

        # Check filesystem, then shrink to minimum size
        _run(["e2fsck", "-fy", tmpfile])
        _run(["resize2fs", "-M", tmpfile])

        # Move to final location (shutil handles cross-fs)
        if image_path.exists():
            image_path.unlink()
        shutil.move(tmpfile, str(image_path))
        tmpfile = None  # prevent cleanup

        return image_path

    finally:
        # Clean up on error
        if container_id:
            subprocess.run(
                ["podman", "rm", "-f", container_id],
                capture_output=True)
        # Try unmount if still mounted
        if mountpoint:
            subprocess.run(
                ["umount", mountpoint],
                capture_output=True)
            try:
                os.rmdir(mountpoint)
            except OSError:
                pass
        if tmpfile and os.path.exists(tmpfile):
            os.unlink(tmpfile)


def _get_package_manifest(container_tag):
    """Get installed RPM list from the container image."""
    try:
        result = _run([
            "podman", "run", "--rm", container_tag,
            "rpm", "-qa", "--queryformat",
            "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\\n",
        ])
        packages = sorted(result.stdout.strip().splitlines())
        return packages
    except subprocess.CalledProcessError:
        log.warning("Failed to extract package manifest")
        return []


def image_status(target_config):
    """Return status dict for the target's image artifact.

    Keys:
        built: bool -- whether an image exists
        build_date: str or None -- ISO timestamp
        stale: bool -- whether inputs have changed
        size_mb: float or None -- image file size
        path: str or None -- path to base.ext4
    """
    out_dir = target_config.image_output_dir()
    image_path = out_dir / "base.ext4"
    meta_path = out_dir / "meta.json"

    if not image_path.exists():
        return {
            "built": False,
            "build_date": None,
            "stale": True,
            "size_mb": None,
            "path": None,
        }

    meta = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())

    size_mb = image_path.stat().st_size / (1024 * 1024)
    stale = target_config.is_stale("image")

    return {
        "built": True,
        "build_date": meta.get("build_date"),
        "stale": stale,
        "size_mb": round(size_mb, 1),
        "path": str(image_path),
    }
