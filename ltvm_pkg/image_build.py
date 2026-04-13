"""VM base image builder.

Builds a QEMU microvm root filesystem image for a given target
by building a container image (Dockerfile) and exporting it to
raw ext4.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .paths import load_meta_safe
from .target_config import TARGETS_DIR

if TYPE_CHECKING:
    from .target_config import TargetConfig

log = logging.getLogger(__name__)

# Image sizing:
#   _IMAGE_SIZE_MB: initial mke2fs allocation (must fit all packages + build outputs).
#   After building, the image is shrunk to minimum with resize2fs -M.
#   The qcow2 overlay is resized to 8G at VM creation, and rc.local runs
#   resize2fs on boot to expand the ext4 to fill the overlay.
_IMAGE_SIZE_MB = 8192


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


def _is_cross_build(target_config: TargetConfig) -> bool:
    """True if the target arch differs from the host."""
    import platform

    return target_config.arch != platform.machine()


def _podman_platform(target_config: TargetConfig) -> list[str]:
    """Return --platform flag for podman if cross-arch build needed."""
    if not _is_cross_build(target_config):
        return []
    _PLAT = {"aarch64": "linux/arm64", "x86_64": "linux/amd64"}
    plat = _PLAT.get(target_config.arch)
    return ["--platform", plat] if plat else []


def _prebuild_tools_native(
    target_config: TargetConfig,
    output_dir: Path,
) -> None:
    """Cross-compile IOR, iozone, pjdfstest, e2fsprogs using the
    host-native build container + cross-compiler.

    This avoids compiling under QEMU user-mode emulation by running
    the cross-compiler natively. The output goes to *output_dir* and
    is injected into the emulated image build via COPY.
    """
    # We deliberately want the NATIVE (host-arch) build container, not
    # the cross-compile one.  Build the tag from the *host* arch, not
    # an "unsuffixed == native" assumption: an aarch64 host has its
    # native container tagged ltvm-build-<name>-aarch64 (created by
    # _ensure_container_image with default_arch=x86_64), and the
    # unsuffixed tag does not exist there.
    import platform as _platform

    arch = target_config.arch
    host_machine = _platform.machine()
    if host_machine in ("x86_64", "amd64"):
        build_tag = f"ltvm-build-{target_config.name}"
    else:
        build_tag = f"ltvm-build-{target_config.name}-{host_machine}"

    output_dir.mkdir(parents=True, exist_ok=True)

    # Ensure the native build container exists (build it if missing).
    r = subprocess.run(
        ["podman", "image", "exists", build_tag], capture_output=True
    )
    if r.returncode != 0:
        log.info("Building native container %s for cross-compile...", build_tag)
        build_dockerfile = target_config.target_dir / "container.Dockerfile"
        subprocess.run(
            [
                "podman",
                "build",
                "-t",
                build_tag,
                "--build-arg",
                f"BASE_IMAGE={target_config.container_image}",
                "-f",
                str(build_dockerfile),
                str(TARGETS_DIR),
            ],
            check=True,
        )

    log.info(
        "Pre-building tools natively for %s (cross-compile in %s)...",
        arch,
        build_tag,
    )

    script = (
        f"export TARGET_ARCH={arch} DESTDIR=/output\n"
        "bash /input/build-tools.sh\n"
        "bash /input/build-e2fsprogs.sh\n"
    )

    common_dir = TARGETS_DIR / "common"
    cmd = [
        "podman",
        "run",
        "--rm",
        "-v",
        f"{common_dir}:/input:ro,Z",
        "-v",
        f"{output_dir}:/output:Z",
        build_tag,
        "-c",
        script,
    ]
    subprocess.run(cmd, check=True)
    log.info("Pre-built tools at %s", output_dir)


def build_image(
    target_config: TargetConfig,
    force: bool = False,
    kernel: str | None = None,
) -> Path:
    """Build a VM base image for the given target.

    Steps:
      1. Build container image via podman build
      2. Export container filesystem to raw ext4
      3. Write meta.json

    Args:
        target_config: TargetConfig instance
        force: rebuild even if inputs unchanged
        kernel: kernel name (short or full) whose modules to bake in;
                defaults to the target's default kernel
    """
    _check_mke2fs()

    kernel_name = target_config.resolve_kernel(kernel)

    if not force and not target_config.is_stale("image", kernel=kernel):
        log.info(
            "Image for %s (kernel=%s) is up to date, skipping (use force=True to rebuild)",
            target_config.name,
            kernel_name,
        )
        return target_config.image_output_dir(kernel) / "base.ext4"

    out_dir = target_config.image_output_dir(kernel)
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
    cross = _is_cross_build(target_config)
    platform_args = _podman_platform(target_config)
    effective_dockerfile = dockerfile
    prebuilt_dir: Path | None = None

    if cross:
        # Cross-build: compile tools natively, then inject into the
        # emulated image build (avoids slow compilation under QEMU).
        prebuilt_dir = out_dir / "_prebuilt"
        _prebuild_tools_native(target_config, prebuilt_dir)

        # Patch the Dockerfile: replace the RUN build-tools.sh and
        # build-e2fsprogs.sh steps with COPY from pre-built output.
        original = dockerfile.read_text()
        patched = original
        replacements = {
            "RUN bash /tmp/build-tools.sh": "COPY _prebuilt/usr/local/ /usr/local/",
            "RUN bash /tmp/build-e2fsprogs.sh": "COPY _prebuilt/usr/ /usr/",
        }
        for old, new in replacements.items():
            # Match any line starting with the key (ignoring trailing args
            # like version strings) so version bumps don't silently skip.
            pattern = re.compile(re.escape(old) + r".*", re.MULTILINE)
            patched, n = pattern.subn(new, patched)
            if n == 0:
                raise RuntimeError(
                    f"Cross-build Dockerfile patching failed: "
                    f"could not find '{old}' in {dockerfile}. "
                    f"Has the Dockerfile changed?"
                )

        # Write patched Dockerfile next to the original
        effective_dockerfile = out_dir / "image.Dockerfile.cross"
        effective_dockerfile.write_text(patched)
        log.info(
            "Using patched Dockerfile for cross-build: %s", effective_dockerfile
        )

        # Build context needs the prebuilt dir AND the targets/ content.
        # Podman doesn't follow symlinks outside the context, so we
        # hard-copy the target dirs into the build context.
        build_context = out_dir
        for name in ("common", target_config.name):
            dest = build_context / name
            src = TARGETS_DIR / name
            if dest.exists():
                shutil.rmtree(str(dest))
            shutil.copytree(str(src), str(dest))
    else:
        build_context = TARGETS_DIR

    log.info("Building container image %s ...", tag)
    _run(
        [
            "podman",
            "build",
            *platform_args,
            "--build-arg",
            f"BASE_IMAGE={target_config.container_image}",
            "-t",
            tag,
            "-f",
            str(effective_dockerfile),
            str(build_context),
        ],
        capture_output=False,
    )

    # ── Step 1b: Add kernel modules + Lustre via a second Dockerfile stage ──
    # resolve_kernel never raises -- it returns the short name if no
    # built directory exists yet.  The actual "no kernel built yet"
    # case is detected below by checking has_modules / has_lustre.
    final_tag = tag

    if kernel_name is not None:
        kdir = target_config.output_dir / "kernels" / kernel_name
        modules_dir = kdir / "modules"
        # Read the exact kernel release string the modules were built for
        # so the injected `depmod -a <kver>` is deterministic instead of
        # globbing /lib/modules.
        kver_file = (
            kdir / "build-tree" / "include" / "config" / "kernel.release"
        )
        kver = kver_file.read_text().strip() if kver_file.exists() else None
        # Lustre staging now lives per-tree under <lustre_tree>/.ltvm-staging,
        # so build_image can no longer auto-inject Lustre from a global
        # location it owns.  Maintainers who want to bake Lustre into the
        # image should run `ltvm build-lustre` then deploy via
        # `ltvm deploy --build`, or rely on `ltvm fetch` which carries
        # prebuilt Lustre in the package via lustre-artifacts/.  We keep
        # injecting kernel modules because those live deterministically
        # next to the kernel build output, not in a per-user tree.
        has_modules = (modules_dir / "lib" / "modules").is_dir()

        if has_modules:
            # Build a context dir with the files to inject
            inject_dir = out_dir / "_inject"
            if inject_dir.exists():
                shutil.rmtree(str(inject_dir))
            inject_dir.mkdir()

            # Write a tiny Dockerfile
            lines = [f"FROM {tag}"]
            if has_modules:
                log.info("Including kernel modules")
                # Copy modules into inject context, clean symlinks
                mod_dest = inject_dir / "modules"
                shutil.copytree(
                    str(modules_dir / "lib" / "modules"),
                    str(mod_dest),
                    symlinks=False,
                    ignore=shutil.ignore_patterns("build", "source"),
                )
            # Lustre auto-inject was here.  It looked at a global
            # staging dir and silently baked whatever was there into the
            # image.  That doesn't work in the multi-user model where
            # staging is per-tree, and was foot-gunny in single-user mode
            # too (a stale build_lustre run would silently leak into
            # every subsequent build_image).  Maintainers who need
            # baked-in Lustre should snapshot it via `ltvm package`,
            # which puts it in lustre-artifacts/ for the fetcher to use.
            if (inject_dir / "modules").is_dir() and any(
                (inject_dir / "modules").iterdir()
            ):
                lines.append("COPY modules/ /lib/modules/")
            if kver:
                lines.append(f"RUN ldconfig && depmod -a {kver}")
            else:
                # No kernel.release file available; fall back to whatever
                # /lib/modules contains.  Sorted -V picks the highest.
                lines.append(
                    "RUN ldconfig && depmod -a "
                    "$(ls /lib/modules/ | sort -V | tail -1)"
                )
            lines.append(
                "RUN ln -sf mount.lustre /usr/sbin/mount.lustre_tgt 2>/dev/null || true"
            )

            inject_dockerfile = inject_dir / "Dockerfile"
            inject_dockerfile.write_text("\n".join(lines) + "\n")

            final_tag = f"{tag}-final"
            log.info("Building final image with kernel modules...")
            _run(
                [
                    "podman",
                    "build",
                    "-t",
                    final_tag,
                    "-f",
                    str(inject_dockerfile),
                    str(inject_dir),
                ],
                capture_output=False,
            )
            # Clean up inject dir
            shutil.rmtree(str(inject_dir), ignore_errors=True)

    # ── Step 2: Export to ext4 ──
    # If the export fails, the injected image (when we built one)
    # would otherwise leak as a podman layer on every retry.  Wrap
    # so we can rmi it on failure.  We don't rmi on success because
    # the next build pass benefits from layer caching.
    log.info("Exporting container to ext4 ...")
    try:
        image_path = _export_to_ext4(final_tag, image_path)
    except BaseException:
        if final_tag != tag:
            subprocess.run(
                ["podman", "rmi", "-f", final_tag],
                capture_output=True,
            )
        raise

    elapsed = time.monotonic() - t0

    # ── Step 3: Collect metadata ──
    size_mb = image_path.stat().st_size / (1024 * 1024)
    pkg_manifest = _get_package_manifest(tag, target_config.os_family)

    target_config.write_meta(
        "image",
        kernel=kernel,
        build_date=datetime.now(timezone.utc).isoformat(),
        build_seconds=round(elapsed, 1),
        image_size_mb=round(size_mb, 1),
        packages=pkg_manifest,
        kernel_name=kernel_name,
    )

    log.info("Image built: %s (%.0f MiB, %.0fs)", image_path, size_mb, elapsed)
    return image_path


def _export_to_ext4(
    container_tag: str,
    image_path: Path,
) -> Path:
    """Export a container image to a raw ext4 file.

    1. podman create + podman export | fakeroot tar + mke2fs
    2. resize2fs -M to shrink
    """
    tmpdir = None
    tmpfile = None
    container_id = None

    try:
        result = _run(["podman", "create", container_tag])
        container_id = result.stdout.strip()
        log.info("Container: %s", container_id[:12])

        tmpdir = tempfile.mkdtemp(prefix="ltvm-rootfs-")
        tmp_f = tempfile.NamedTemporaryFile(
            suffix=".ext4", prefix="ltvm-image-", delete=False
        )
        tmpfile = tmp_f.name
        tmp_f.close()

        qcid = shlex.quote(container_id)
        qtmp = shlex.quote(tmpdir)

        log.info("Exporting to ext4...")
        # set -o pipefail so a `podman export` failure (stale container,
        # missing image) propagates out of the pipeline instead of being
        # masked by a successful downstream `mke2fs` against an empty tar
        # stream -- which would silently produce a tiny rootfs image.
        _run(
            [
                "bash",
                "-o",
                "pipefail",
                "-c",
                f"podman export {qcid} "
                f"| fakeroot bash -c '"
                f"tar -C {qtmp} -xf - --exclude=dev/* "
                f"&& mkdir -p {qtmp}/dev/pts {qtmp}/dev/shm {qtmp}/dev/mqueue "
                f"&& find {qtmp} ! -readable -exec chmod u+r {{}} + 2>/dev/null; "
                f"mke2fs -t ext4 -d {qtmp} -b 4096 "
                f"-L rootfs {shlex.quote(tmpfile)} {_IMAGE_SIZE_MB}M'",
            ],
            capture_output=False,
        )

        # Remove the podman container before shrinking.
        subprocess.run(
            ["podman", "rm", "-f", container_id], capture_output=True
        )
        container_id = None

        # Shrink to minimum. The qcow2 overlay is resized to 8G at VM
        # creation, and rc.local runs resize2fs on first boot to expand
        # the ext4 to fill the overlay — no headroom needed here.
        r_fsck = subprocess.run(["e2fsck", "-fy", tmpfile], capture_output=True)
        # e2fsck exit codes: 0 = clean, 1 = errors corrected (both OK),
        # 2+ = errors remain or operational failure.
        if r_fsck.returncode > 1:
            raise RuntimeError(
                f"e2fsck failed (rc={r_fsck.returncode}): "
                f"{r_fsck.stderr.decode(errors='replace').strip()}"
            )
        _run(["resize2fs", "-M", tmpfile])

        # Move to final location atomically
        os.rename(tmpfile, str(image_path))
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


def image_status(
    target_config: TargetConfig,
    kernel: str | None = None,
) -> dict[str, bool | str | float | None]:
    """Return status dict for the target's image artifact for a kernel.

    Keys:
        built: bool -- whether an image exists
        build_date: str or None -- ISO timestamp
        stale: bool -- whether inputs have changed
        size_mb: float or None -- image file size
        path: str or None -- path to base.ext4
        kernel: str -- resolved kernel name this image is paired with
    """
    kernel_name = target_config.resolve_kernel(kernel)
    out_dir = target_config.image_output_dir(kernel)
    image_path = out_dir / "base.ext4"
    meta_path = out_dir / "meta.json"

    if not image_path.exists():
        return {
            "built": False,
            "build_date": None,
            "stale": True,
            "size_mb": None,
            "path": None,
            "kernel": kernel_name,
        }

    meta = load_meta_safe(meta_path) or {}

    size_mb = image_path.stat().st_size / (1024 * 1024)
    stale = target_config.is_stale("image", kernel=kernel)

    return {
        "built": True,
        "build_date": meta.get("build_date"),
        "stale": stale,
        "size_mb": round(size_mb, 1),
        "path": str(image_path),
        "kernel": kernel_name,
    }
