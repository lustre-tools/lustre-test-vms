"""VM base image builder.

Builds a QEMU microvm root filesystem image for a given target
by building a container image (Dockerfile) and exporting it to
raw ext4.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import TARGETS_DIR, arch_deb

if TYPE_CHECKING:
    from .config import TargetConfig

log = logging.getLogger(__name__)

# Image sizing:
#   _IMAGE_SIZE_MB: initial mke2fs allocation (must fit all packages + build outputs)
#   _IMAGE_HEADROOM_MB: free space added back after resize2fs -M shrink,
#     so the running VM has room for Lustre modules (~500 MB debug) + runtime data.
_IMAGE_SIZE_MB = 8192
_IMAGE_HEADROOM_MB = 4096  # 4 GiB headroom in running VM


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run a command, logging it and raising on failure."""
    log.info("Running: %s", " ".join(str(c) for c in cmd))
    return subprocess.run(
        cmd,
        check=True,
        capture_output=kwargs.pop("capture_output", True),
        text=kwargs.pop("text", True),
        **kwargs,
    )


def _check_mke2fs() -> None:
    """Verify mke2fs supports -d (populate from directory)."""
    result = subprocess.run(["mke2fs", "-V"], capture_output=True, text=True)
    # -d support was added in e2fsprogs 1.43 (2016)
    version_str = result.stderr + result.stdout
    if "mke2fs" not in version_str:
        raise RuntimeError("mke2fs not found; install e2fsprogs")


def _container_image_tag(target_config: TargetConfig) -> str:
    if target_config.arch != "x86_64":
        return f"ltvm-image-{target_config.name}-{target_config.arch}"
    return f"ltvm-image-{target_config.name}"


def _podman_platform(target_config: TargetConfig) -> list[str]:
    """Return --platform flag for podman if cross-arch build needed."""
    import platform
    host_machine = platform.machine()
    if target_config.arch == "aarch64" and host_machine != "aarch64":
        return ["--platform", "linux/arm64"]
    if target_config.arch == "x86_64" and host_machine != "x86_64":
        return ["--platform", "linux/amd64"]
    return []


def build_image(target_config: TargetConfig, force: bool = False) -> Path:
    """Build a VM base image for the given target.

    Steps:
      1. Build container image via podman build
      2. Export container filesystem to raw ext4
      3. Write meta.json

    Args:
        target_config: TargetConfig instance
        force: rebuild even if inputs unchanged
    """
    _check_mke2fs()

    if not force and not target_config.is_stale("image"):
        log.info(
            "Image for %s is up to date, skipping (use force=True to rebuild)",
            target_config.name,
        )
        return target_config.image_output_dir() / "base.ext4"

    out_dir = target_config.image_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    image_path = out_dir / "base.ext4"

    tag = _container_image_tag(target_config)
    dockerfile = target_config.target_dir / "image.Dockerfile"
    if not dockerfile.exists():
        raise FileNotFoundError(
            f"No image.Dockerfile for target {target_config.name}"
        )

    t0 = time.monotonic()

    # ── Step 1: Build container image ──
    log.info("Building container image %s ...", tag)
    log.info("Running: podman build -t %s -f %s %s", tag, dockerfile, TARGETS_DIR)
    platform_args = _podman_platform(target_config)
    _run(
        [
            "podman", "build",
            *platform_args,
            "--build-arg", f"BASE_IMAGE={target_config.container_image}",
            "-t", tag,
            "-f", str(dockerfile),
            str(TARGETS_DIR),
        ],
        capture_output=False,
    )

    # Find kernel modules to bake into the image
    kernel_modules_dir = None
    try:
        kernel_name = target_config.resolve_kernel(None)
        kdir = target_config.output_dir / "kernels" / kernel_name / "modules"
        lib_mods = kdir / "lib" / "modules"
        if lib_mods.is_dir():
            kernel_modules_dir = lib_mods
            log.info("Including kernel modules from %s", kdir)
        else:
            log.warning("No kernel modules found -- build kernel first for full image")
    except (ValueError, FileNotFoundError):
        log.warning("Could not resolve kernel -- image will not include kernel modules")

    # ── Step 2: Export to ext4 ──
    log.info("Exporting container to ext4 ...")
    image_path = _export_to_ext4(tag, image_path, kernel_modules_dir=kernel_modules_dir)

    elapsed = time.monotonic() - t0

    # ── Step 3: Collect metadata ──
    size_mb = image_path.stat().st_size / (1024 * 1024)
    pkg_manifest = _get_package_manifest(tag, target_config.os_family)

    target_config.write_meta(
        "image",
        build_date=datetime.now(timezone.utc).isoformat(),
        build_seconds=round(elapsed, 1),
        image_size_mb=round(size_mb, 1),
        packages=pkg_manifest,
    )

    log.info("Image built: %s (%.0f MiB, %.0fs)", image_path, size_mb, elapsed)
    return image_path


def _export_to_ext4(
    container_tag: str,
    image_path: Path,
    kernel_modules_dir: Path | None = None,
) -> Path:
    """Create a raw ext4 image from a container's filesystem.

    Entirely rootless using mke2fs -d (populate from directory).

    1. podman create + podman export | tar into temp directory
    2. (optional) copy kernel modules into the temp directory
    3. mke2fs -d <dir> to create populated ext4 image
    """
    tmpdir = None
    tmpfile = None
    container_id = None

    try:
        # Export container filesystem to a temp directory
        tmpdir = tempfile.mkdtemp(prefix="ltvm-rootfs-")

        result = _run(["podman", "create", container_tag])
        container_id = result.stdout.strip()

        log.info(
            "Extracting container %s and building ext4 under fakeroot ...",
            container_id[:12],
        )

        # Create ext4 image file (NamedTemporaryFile avoids mktemp TOCTOU)
        tmp_f = tempfile.NamedTemporaryFile(
            suffix=".ext4", prefix="ltvm-image-", delete=False
        )
        tmpfile = tmp_f.name
        tmp_f.close()

        # Use fakeroot to preserve root ownership.
        # Without it, extracted files would be owned by
        # our uid, and mke2fs -d bakes that into the ext4.
        # podman runs OUTSIDE fakeroot (it checks real uid),
        # tar + mke2fs run INSIDE fakeroot.
        qcid = shlex.quote(container_id)
        qtmp = shlex.quote(tmpdir)
        qimg = shlex.quote(tmpfile)

        # Export container filesystem
        _run(
            [
                "bash",
                "-c",
                f"podman export {qcid} "
                f"| fakeroot bash -c '"
                f"tar -C {qtmp} -xf - --exclude=dev/* "
                f"&& mkdir -p {qtmp}/dev/pts {qtmp}/dev/shm "
                f"{qtmp}/dev/mqueue "
                f"&& find {qtmp} ! -readable -exec "
                f"chmod u+r {{}} + 2>/dev/null; true'",
            ],
            capture_output=False,
        )

        # Inject kernel modules into the rootfs
        if kernel_modules_dir and kernel_modules_dir.is_dir():
            dest = Path(tmpdir) / "lib" / "modules"
            dest.mkdir(parents=True, exist_ok=True)
            for kver_dir in kernel_modules_dir.iterdir():
                if kver_dir.is_dir():
                    log.info("  Copying modules: %s", kver_dir.name)
                    subprocess.run(
                        ["cp", "-a", str(kver_dir), str(dest / kver_dir.name)],
                        check=True,
                    )
                    # Remove dangling build/source symlinks
                    for name in ("build", "source"):
                        link = dest / kver_dir.name / name
                        if link.is_symlink():
                            link.unlink()

        # Create ext4 image
        _run(
            [
                "fakeroot",
                "mke2fs", "-t", "ext4",
                "-d", tmpdir,
                "-b", "4096",
                "-L", "rootfs",
                tmpfile, f"{_IMAGE_SIZE_MB}M",
            ],
            capture_output=False,
        )

        # Remove the podman container
        subprocess.run(
            ["podman", "rm", "-f", container_id], capture_output=True
        )
        container_id = None

        # The image is sized to fit all packages plus headroom for
        # Lustre modules (~500 MiB unstripped) and runtime writes.
        # No shrink -- the image is used directly at _IMAGE_SIZE_MB.

        # Move to final location
        if image_path.exists():
            image_path.unlink()
        shutil.move(tmpfile, str(image_path))
        tmpfile = None

        return image_path

    finally:
        if container_id:
            subprocess.run(
                ["podman", "rm", "-f", container_id], capture_output=True
            )
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
        if tmpfile and os.path.exists(tmpfile):
            os.unlink(tmpfile)


def _get_package_manifest(
    container_tag: str, os_family: str = "rhel"
) -> list[str]:
    """Get installed package list from the container image."""
    try:
        if os_family == "debian":
            result = _run(
                [
                    "podman",
                    "run",
                    "--rm",
                    container_tag,
                    "dpkg-query",
                    "-W",
                    "-f",
                    "${Package} ${Version} ${Architecture}\n",
                ]
            )
        else:
            result = _run(
                [
                    "podman",
                    "run",
                    "--rm",
                    container_tag,
                    "rpm",
                    "-qa",
                    "--queryformat",
                    "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\\n",
                ]
            )
        packages = sorted(result.stdout.strip().splitlines())
        return packages
    except subprocess.CalledProcessError:
        log.warning("Failed to extract package manifest")
        return []


def image_status(
    target_config: TargetConfig,
) -> dict[str, bool | str | float | None]:
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
