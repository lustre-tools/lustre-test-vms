"""Export a built ltvm base.ext4 into a self-contained bootable disk.

The normal ltvm boot path uses QEMU microvm mode and passes the
kernel separately via `-kernel`.  That's perfect for our own use,
but leaves the rootfs dependent on ltvm (no bootloader, no kernel
inside the image).

`ltvm target export` packages the rootfs + matching kernel + a BIOS
GRUB2 bootloader into a single bootable disk image (qcow2 by default)
that any plain QEMU or libvirt can boot with just `-drive file=...`.

Requires root on the host (uses losetup + mount).  Tooling: parted,
mkfs.ext4, grub2-install (grub-install on Debian), qemu-img.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .target_config import TargetConfig

log = logging.getLogger(__name__)

# Headroom above the source rootfs, covering /boot additions (kernel,
# initramfs, grub modules/core.img) plus a little slack.
_HEADROOM_MB = 512
# Reserve the first 1 MiB for the MBR + post-MBR gap where GRUB's
# core.img lives (matches the parted/grub default).
_PART_OFFSET_MIB = 1


def _run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    log.info("Running: %s", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=True, text=True, **kw)  # type: ignore[arg-type]


def _which_or_die(names: list[str]) -> str:
    """Return the first binary found on PATH, else raise."""
    for n in names:
        if shutil.which(n) is not None:
            return n
    raise RuntimeError(
        f"None of {names} found on PATH -- install one "
        f"(grub2-pc / grub-pc-bin) and retry"
    )


def _check_host_tools() -> dict[str, str]:
    """Verify every tool the export needs is installed."""
    missing = [
        t for t in ("parted", "mkfs.ext4", "losetup", "mount",
                    "umount", "qemu-img", "e2fsck", "blkid")
        if shutil.which(t) is None
    ]
    if missing:
        raise RuntimeError(
            f"missing host tool(s): {', '.join(missing)} -- "
            f"install parted, e2fsprogs, util-linux, qemu-utils"
        )
    # grub2-install on RHEL/Rocky, grub-install on Debian/Ubuntu.
    grub = _which_or_die(["grub2-install", "grub-install"])
    return {"grub_install": grub}


def _image_size_mb(rootfs: Path, kernel_dir: Path) -> int:
    rootfs_mb = rootfs.stat().st_size // (1024 * 1024)
    kernel_mb = 0
    for f in ("vmlinuz", "vmlinux"):
        p = kernel_dir / f
        if p.exists():
            kernel_mb += p.stat().st_size // (1024 * 1024)
    return rootfs_mb + kernel_mb + _HEADROOM_MB


def _losetup_attach(image: Path) -> str:
    """losetup --partscan and return the /dev/loopN device."""
    r = subprocess.run(
        ["losetup", "--show", "-f", "-P", str(image)],
        check=True, capture_output=True, text=True,
    )
    return r.stdout.strip()


def _losetup_detach(dev: str) -> None:
    subprocess.run(["losetup", "-d", dev], check=False)


def _write_grub_cfg(
    boot_dir: Path, kver: str, fs_uuid: str,
    grub_install: str = "grub2-install",
) -> None:
    """Write a minimal serial-friendly grub.cfg.

    Points root= at the filesystem UUID so the image is portable
    across whatever /dev/{sda,vda,nvme0n1}p1 name the host hands out.

    Writes to BOTH /boot/grub/ and /boot/grub2/: the running bootloader
    looks at the path its binary was compiled for (Debian grub-install
    -> /boot/grub, RHEL grub2-install -> /boot/grub2), which may differ
    from whatever grub2 package the guest rootfs expects to find its
    config in.  Duplicating is cheap (<1 KiB) and makes the image
    portable whether you export on a Debian or RHEL host.
    """
    subdir = "grub" if Path(grub_install).name == "grub-install" else "grub2"
    cfg_dir = boot_dir / subdir
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "grub.cfg"
    cfg.write_text(
        "set timeout=2\n"
        "serial --unit=0 --speed=115200\n"
        "terminal_input console serial\n"
        "terminal_output console serial\n"
        "\n"
        "menuentry 'ltvm' {\n"
        f"    search --no-floppy --fs-uuid --set=root {fs_uuid}\n"
        f"    linux /boot/vmlinuz-{kver} root=UUID={fs_uuid} rw "
        "console=tty0 console=ttyS0,115200 "
        "net.ifnames=0 biosdevname=0\n"
        f"    initrd /boot/initramfs-{kver}.img\n"
        "}\n"
    )


def _fs_uuid(dev: str) -> str:
    r = subprocess.run(
        ["blkid", "-s", "UUID", "-o", "value", dev],
        check=True, capture_output=True, text=True,
    )
    uuid = r.stdout.strip()
    if not uuid:
        raise RuntimeError(f"blkid returned no UUID for {dev}")
    return uuid


def export_image(
    target_config: "TargetConfig",
    kernel: str | None,
    output: Path,
    image_format: str = "qcow2",
    force: bool = False,
) -> Path:
    """Build a self-contained bootable disk for the given target.

    Args:
        target_config: target whose base.ext4 + kernel to package.
        kernel: optional kernel selector (short or full); defaults
                to the target's default kernel.
        output: destination file path (parent will be created).
        image_format: "qcow2" or "raw".
        force: overwrite *output* if it exists.

    Returns:
        The final written path.
    """
    if image_format not in ("qcow2", "raw"):
        raise ValueError(f"unknown format: {image_format!r}")
    if output.exists() and not force:
        raise FileExistsError(f"{output} exists; use --force to overwrite")

    tools = _check_host_tools()
    grub_install = tools["grub_install"]

    kernel_name = target_config.resolve_kernel(kernel)
    image_dir = target_config.image_output_dir(kernel)
    base_ext4 = image_dir / "base.ext4"
    if not base_ext4.exists():
        raise FileNotFoundError(
            f"No base.ext4 for {target_config.name} kernel={kernel_name}. "
            f"Build first: ltvm build image {target_config.name}"
        )

    kdir = target_config.kernel_output_dir(kernel)
    vmlinuz = kdir / "vmlinuz"
    if not vmlinuz.exists():
        raise FileNotFoundError(
            f"No vmlinuz at {vmlinuz}. "
            f"Build first: ltvm build kernel {target_config.name}"
        )

    kver_file = (
        kdir / "build-tree" / "include" / "config" / "kernel.release"
    )
    if not kver_file.exists():
        raise FileNotFoundError(
            f"Cannot read kernel release from {kver_file}. "
            f"Kernel build tree incomplete."
        )
    kver = kver_file.read_text().strip()

    t0 = time.monotonic()
    size_mb = _image_size_mb(base_ext4, kdir)
    log.info(
        "Exporting %s (kernel %s) -> %s (%s, ~%d MiB)",
        target_config.name, kernel_name, output, image_format, size_mb,
    )

    tmpdir = Path(tempfile.mkdtemp(prefix="ltvm-export-"))
    raw = tmpdir / "disk.raw"
    src_mnt = tmpdir / "src"
    dst_mnt = tmpdir / "dst"
    src_mnt.mkdir()
    dst_mnt.mkdir()
    loop: str | None = None
    src_loop: str | None = None

    try:
        # 1. Create a sparse raw disk and partition it.
        with raw.open("wb") as fp:
            fp.truncate(size_mb * 1024 * 1024)
        _run([
            "parted", "-s", str(raw),
            "mklabel", "msdos",
            "mkpart", "primary", "ext4",
            f"{_PART_OFFSET_MIB}MiB", "100%",
            "set", "1", "boot", "on",
        ])

        # 2. Attach loop (with partscan) and format the root partition.
        loop = _losetup_attach(raw)
        part = f"{loop}p1"
        for _ in range(20):
            if Path(part).exists():
                break
            time.sleep(0.1)
        if not Path(part).exists():
            raise RuntimeError(f"{part} did not appear after partscan")
        _run(["mkfs.ext4", "-q", "-L", "rootfs", part])

        # 3. Copy rootfs contents via fs-level cp -a.
        src_loop = _losetup_attach(base_ext4)
        _run(["mount", "-o", "ro", src_loop, str(src_mnt)])
        _run(["mount", part, str(dst_mnt)])
        _run([
            "cp", "-a", "--reflink=auto",
            f"{src_mnt}/.", str(dst_mnt),
        ])
        subprocess.run(["umount", str(src_mnt)], check=True)
        _losetup_detach(src_loop)
        src_loop = None

        # 4. Drop in kernel + initramfs.  image_build bakes these into
        #    /boot already; re-copy defensively so older images also work.
        boot = dst_mnt / "boot"
        boot.mkdir(exist_ok=True)
        shutil.copy2(vmlinuz, boot / f"vmlinuz-{kver}")
        initramfs_src = kdir / f"initramfs-{kver}.img"
        if initramfs_src.exists():
            shutil.copy2(initramfs_src, boot / f"initramfs-{kver}.img")
        elif not (boot / f"initramfs-{kver}.img").exists():
            log.warning(
                "No initramfs for %s; boot will likely fail. "
                "Rebuild the image so dracut bakes one in.",
                kver,
            )

        # 5. Install GRUB2 (i386-pc BIOS).  --boot-directory points at
        #    the mounted target fs; no chroot needed.
        fs_uuid = _fs_uuid(part)
        _write_grub_cfg(boot, kver, fs_uuid, grub_install=grub_install)
        _run([
            grub_install,
            "--target=i386-pc",
            f"--boot-directory={boot}",
            "--modules=part_msdos ext2 biosdisk",
            loop,
        ])

        # 6. Tidy up.
        subprocess.run(["umount", str(dst_mnt)], check=True)
        _losetup_detach(loop)
        loop = None
        subprocess.run(["e2fsck", "-fy", part], capture_output=True)

        # 7. Convert to final format.
        output.parent.mkdir(parents=True, exist_ok=True)
        if image_format == "raw":
            shutil.move(str(raw), str(output))
        else:
            _run([
                "qemu-img", "convert", "-f", "raw", "-O", "qcow2",
                "-c",
                str(raw), str(output),
            ])

        elapsed = time.monotonic() - t0
        size_final_mb = output.stat().st_size / (1024 * 1024)
        log.info(
            "Wrote %s (%.0f MiB, %.0fs)",
            output, size_final_mb, elapsed,
        )
        return output

    finally:
        for m in (src_mnt, dst_mnt):
            subprocess.run(["umount", str(m)], capture_output=True)
        for d in (src_loop, loop):
            if d:
                _losetup_detach(d)
        shutil.rmtree(tmpdir, ignore_errors=True)
