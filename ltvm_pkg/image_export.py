"""Export a built ltvm base.ext4 into a self-contained bootable disk.

The normal ltvm boot path uses QEMU microvm mode and passes the
kernel separately via `-kernel`.  That's perfect for our own use,
but leaves the rootfs dependent on ltvm (no bootloader, no kernel
inside the image).

`ltvm target export` packages the rootfs + matching kernel + a BIOS
GRUB2 bootloader into a single bootable disk image (qcow2 by default)
that any plain QEMU or libvirt can boot with just `-drive file=...`.

`--format gce` wraps that same disk as a Google Compute Engine
custom image (`disk.raw` in an oldgnu-format tar.gz) and applies the
guest tweaks GCE needs -- DHCP networking in place of ltvm's
cmdline-assigned addresses, and a UUID-based fstab.  Upload the
result to a GCS bucket and `gcloud compute images create ... --source-uri`.

Uses losetup + mount, so every external command is invoked through
``sudo_run`` from ``ltvm_pkg.priv``.  The CLI wrapper primes sudo
upfront so the user gets a single password prompt.  Tooling: parted,
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

from ltvm_pkg.priv import sudo_run

if TYPE_CHECKING:
    from .target_config import TargetConfig

log = logging.getLogger(__name__)

# Headroom above the source rootfs, covering /boot additions (kernel,
# initramfs, grub modules/core.img) plus a little slack.
_HEADROOM_MB = 512
# Reserve the first 1 MiB for the MBR + post-MBR gap where GRUB's
# core.img lives (matches the parted/grub default).
_PART_OFFSET_MIB = 1

# Formats `export_image` knows how to write.  "gce" is "raw", rounded
# up to a whole GiB and tarred the way Google's image import wants.
_FORMATS = ("qcow2", "raw", "gce")

# GCE requires the image tarball to hold exactly one file, named
# `disk.raw`, and requires its size to be a whole number of GiB.
_GCE_DISK_NAME = "disk.raw"
_MB_PER_GIB = 1024


def _run(
    cmd: list[str], quiet: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* under sudo (no-op prefix if already root).

    Export touches /dev/loopN, mounts and root-owned fs contents, so
    every external command goes through sudo regardless of euid.
    """
    log.info("Running: %s", " ".join(str(c) for c in cmd))
    return sudo_run(cmd, check=True, quiet=quiet)


def _ensure_dir(path: Path) -> None:
    """``mkdir -p`` *path*, falling back to sudo only when the user
    can't create it directly (e.g. inside a root-owned mount)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        return
    except PermissionError:
        pass
    sudo_run(["mkdir", "-p", str(path)], quiet=True)


def _sudo_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    """Write *text* to *path*, falling back to sudo only when the
    user can't write directly (e.g. inside a root-owned mount)."""
    try:
        path.write_text(text)
        path.chmod(mode)
        return
    except PermissionError:
        pass
    log.info("Writing (sudo): %s", path)
    subprocess.run(
        ["sudo", "tee", str(path)],
        input=text,
        text=True,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(["sudo", "chmod", f"{mode:o}", str(path)], check=True)


def _which_or_die(names: list[str]) -> str:
    """Return the first binary found on PATH, else raise."""
    for n in names:
        if shutil.which(n) is not None:
            return n
    raise RuntimeError(
        f"None of {names} found on PATH -- install one "
        f"(grub2-pc / grub-pc-bin) and retry"
    )


def _check_gnu_tar() -> None:
    """Fail fast unless PATH's tar is GNU tar.

    The GCE tarball must be in GNU (oldgnu) format with sparse
    members; bsdtar -- the default `tar` on macOS and on some
    minimal images -- can write neither, and would silently produce
    a tarball Google's image import rejects.
    """
    if shutil.which("tar") is None:
        raise RuntimeError("tar not found on PATH -- needed for --format gce")
    r = subprocess.run(
        ["tar", "--version"], capture_output=True, text=True, check=False
    )
    if "GNU tar" not in (r.stdout or ""):
        found = (r.stdout or "").splitlines()
        raise RuntimeError(
            "GNU tar is required for --format gce (the GCE image tarball "
            "must be oldgnu format) -- found "
            f"{found[0] if found else 'an unrecognised tar'}"
        )


def _check_host_tools(image_format: str = "qcow2") -> dict[str, str]:
    """Verify every tool the export needs is installed.

    Per-format, not one fixed list: only the qcow2 path shells out to
    qemu-img (raw is a move, gce is a tar), so demanding qemu-utils on
    a node that just wants a GCE image is a install-this-for-nothing
    error.
    """
    needed = [
        "parted",
        "mkfs.ext4",
        "losetup",
        "mount",
        "umount",
        "e2fsck",
        "blkid",
    ]
    if image_format == "qcow2":
        needed.append("qemu-img")
    missing = [t for t in needed if shutil.which(t) is None]
    if missing:
        raise RuntimeError(
            f"missing host tool(s): {', '.join(missing)} -- "
            f"install parted, e2fsprogs, util-linux, qemu-utils"
        )
    if image_format == "gce":
        _check_gnu_tar()
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
    r = sudo_run(
        ["losetup", "--show", "-f", "-P", str(image)],
        check=True,
        quiet=True,
    )
    return r.stdout.strip()


def _losetup_detach(dev: str) -> None:
    sudo_run(["losetup", "-d", dev], check=False, quiet=True)


def _write_grub_cfg(
    boot_dir: Path,
    kver: str,
    fs_uuid: str,
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
    _ensure_dir(cfg_dir)
    cfg_text = (
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
    _sudo_write_text(cfg_dir / "grub.cfg", cfg_text)


def _fs_uuid(dev: str) -> str:
    r = sudo_run(
        ["blkid", "-s", "UUID", "-o", "value", dev],
        check=True,
        quiet=True,
    )
    uuid = r.stdout.strip()
    if not uuid:
        raise RuntimeError(f"blkid returned no UUID for {dev}")
    return uuid


def _sudo_read_text(path: Path, missing_ok: bool = False) -> str:
    """Read *path*, falling back to sudo only when the user can't
    read it directly (e.g. inside a root-owned mount).

    Never uses ``Path.exists()`` to decide: under a root-owned 0700
    directory that reports False for a file that is really there,
    which would turn an append into a clobber.
    """
    try:
        return path.read_text()
    except FileNotFoundError:
        if missing_ok:
            return ""
        raise
    except PermissionError:
        pass
    r = sudo_run(["cat", str(path)], check=not missing_ok, quiet=True)
    return r.stdout if r.returncode == 0 else ""


def _rewrite_fstab_root(dst_mnt: Path, fs_uuid: str) -> None:
    """Point the exported image's fstab "/" entry at *fs_uuid*.

    The base image ships ``/dev/vda / ext4 ...`` because ltvm's own
    microvm boot hands the rootfs over as one unpartitioned virtio
    disk.  An exported disk is partitioned and its device name
    depends on the hypervisor -- /dev/vda1 under virtio-blk,
    /dev/sda1 on GCE's virtio-scsi, /dev/nvme0n1p1 on NVMe -- so any
    literal device node is wrong somewhere.  The UUID is right
    everywhere, and matches the root= the grub.cfg already emits.
    """
    fstab = dst_mnt / "etc" / "fstab"
    lines = _sudo_read_text(fstab, missing_ok=True).splitlines()

    out: list[str] = []
    replaced = False
    for line in lines:
        fields = line.split()
        if (
            not line.lstrip().startswith("#")
            and len(fields) >= 2
            and fields[1] == "/"
        ):
            fields[0] = f"UUID={fs_uuid}"
            out.append("  ".join(fields))
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"UUID={fs_uuid}  /  ext4  defaults,noatime  0 1")
    _sudo_write_text(fstab, "\n".join(out) + "\n")


def _inject_ssh_key(dst_mnt: Path, pubkey: Path) -> None:
    """Append *pubkey* to root's authorized_keys inside the image.

    Appends rather than replaces: the base image already holds the
    shared inter-VM ltvm key there, and dropping it would break
    VM-to-VM ssh for anyone using the export as a cluster node.
    """
    key_text = pubkey.read_text().strip()
    if not key_text:
        raise ValueError(f"ssh key file is empty: {pubkey}")

    ssh_dir = dst_mnt / "root" / ".ssh"
    _ensure_dir(ssh_dir)
    sudo_run(["chmod", "700", str(ssh_dir)], check=False, quiet=True)

    auth = ssh_dir / "authorized_keys"
    existing = _sudo_read_text(auth, missing_ok=True)
    if key_text in existing:
        return
    if existing and not existing.endswith("\n"):
        existing += "\n"
    _sudo_write_text(auth, existing + key_text + "\n", mode=0o600)


# NetworkManager keyfile for the GCE guest.  eth0 is the right name
# because the grub.cfg we emit already pins net.ifnames=0/biosdevname=0.
_GCE_NM_PROFILE = """\
# Written by `ltvm target export --format gce`.
[connection]
id=ltvm-gce
type=ethernet
interface-name=eth0
autoconnect=true
autoconnect-priority=100

[ipv4]
method=auto

[ipv6]
method=disabled
"""

# sshd drop-in for the GCE guest.  The name sorts BEFORE the image's
# own 99-ltvm.conf on purpose: sshd takes the FIRST value it obtains
# for these keywords, so a lower-numbered file wins.
_GCE_SSHD_HARDENING = """\
# Written by `ltvm target export --format gce`.
#
# The base image ships ltvm's lab defaults -- root with an empty
# password, PermitRootLogin yes, PermitEmptyPasswords yes (see
# 99-ltvm.conf).  Those are fine on a private hypervisor and are an
# instant root shell for anyone who can reach port 22 of a cloud
# instance.  This file sorts first, and sshd keeps the first value it
# obtains for a keyword, so these win over 99-ltvm.conf.
#
# Key-based root login still works: pass --ssh-key to `target export`.
PermitEmptyPasswords no
PasswordAuthentication no
KbdInteractiveAuthentication no
"""

_NM_UNIT_CANDIDATES = (
    "usr/lib/systemd/system/NetworkManager.service",
    "lib/systemd/system/NetworkManager.service",
)


def _lock_root_password(dst_mnt: Path) -> bool:
    """Lock root's password in the image's /etc/shadow.

    ltvm images ship ``root::`` -- an empty password field, meaning
    root logs in with no password at all.  Combined with the image's
    ``PermitEmptyPasswords yes`` that is a credential-free root shell,
    which is exactly what must not boot on a public address.

    Locking replaces the empty field with ``!``, which no password
    hashes to.  It does not disable the account: key-based SSH and the
    serial-console autologin both still work, so a locked root is not
    a lockout.

    Returns True if the file was rewritten.  /etc/shadow's mode (0000
    on RHEL) is preserved -- writing it 0644 would itself be a finding.
    """
    shadow = dst_mnt / "etc" / "shadow"
    text = _sudo_read_text(shadow, missing_ok=True)
    if not text:
        log.warning("no /etc/shadow in the image; not locking root")
        return False

    out: list[str] = []
    changed = False
    for line in text.splitlines():
        fields = line.split(":")
        # An already-locked or hashed root ("!...", "*", a real hash)
        # is left alone -- only the empty-password case is rewritten,
        # so re-exporting an image twice is a no-op.
        if len(fields) >= 2 and fields[0] == "root" and fields[1] == "":
            fields[1] = "!"
            changed = True
            out.append(":".join(fields))
        else:
            out.append(line)
    if not changed:
        return False
    _sudo_write_text(shadow, "\n".join(out) + "\n", mode=0o000)
    return True


def _harden_gce_ssh(dst_mnt: Path) -> None:
    """Turn off password auth and lock root, for a cloud-bound image.

    Called only for ``--format gce``.  The qcow2/raw exports keep the
    lab defaults: those boot on someone's own hypervisor, where
    passwordless root between nodes is the point.
    """
    sshd_dir = dst_mnt / "etc" / "ssh" / "sshd_config.d"
    _ensure_dir(sshd_dir)
    _sudo_write_text(
        sshd_dir / "00-ltvm-gce-hardening.conf",
        _GCE_SSHD_HARDENING,
        mode=0o600,
    )
    locked = _lock_root_password(dst_mnt)
    log.info(
        "GCE hardening: password auth disabled%s",
        ", root password locked" if locked else "",
    )


def _apply_gce_guest_config(dst_mnt: Path) -> None:
    """Make the rootfs usable as a GCE custom image.

    ltvm images take their address from the kernel cmdline
    (``fc_ip=``/``fc_gw=``, parsed by rc.local) because the microvm
    boot path has no DHCP server.  GCE passes no ltvm cmdline and
    hands out address, routes, DNS and the metadata server over DHCP
    instead, so rc.local's network block is simply skipped and the
    instance would boot with no networking at all.

    Fix: an explicit NetworkManager profile for eth0.  Explicit is
    what makes it work -- setup-network.sh sets ``no-auto-default=*``
    to stop NM fighting rc.local, but that only suppresses NM's
    *implicit* default wired connection, not a profile on disk.
    """
    nm_dir = dst_mnt / "etc" / "NetworkManager" / "system-connections"
    _ensure_dir(nm_dir)
    # NM ignores a keyfile that is group- or world-readable.
    _sudo_write_text(
        nm_dir / "ltvm-gce.nmconnection", _GCE_NM_PROFILE, mode=0o600
    )

    unit = next(
        (c for c in _NM_UNIT_CANDIDATES if (dst_mnt / c).exists()), None
    )
    if unit is None:
        log.warning(
            "NetworkManager.service not found in the image; the GCE "
            "instance may come up without networking"
        )
        return
    # Offline `systemctl enable` for a WantedBy=multi-user.target unit is
    # exactly this symlink -- doing it by hand avoids needing a host
    # systemctl that understands --root.
    wants = dst_mnt / "etc" / "systemd" / "system" / "multi-user.target.wants"
    _ensure_dir(wants)
    sudo_run(
        ["ln", "-sf", "/" + unit, str(wants / "NetworkManager.service")],
        check=False,
        quiet=True,
    )


def _round_up_gib_mb(size_mb: int) -> int:
    """Round *size_mb* up to a whole GiB.  GCE rejects an image whose
    disk.raw is not a whole number of gigabytes."""
    return ((size_mb + _MB_PER_GIB - 1) // _MB_PER_GIB) * _MB_PER_GIB


def _package_gce(raw: Path, output: Path) -> None:
    """Pack *raw* into the tarball GCE's image import expects.

    One member, named exactly ``disk.raw``, oldgnu tar format, gzip
    compressed.  ``-S`` keeps the sparse regions sparse so a mostly
    empty multi-GiB disk still packs in seconds instead of streaming
    gigabytes of zeroes through gzip.  ``-C`` keeps the member name
    bare -- a leading path component makes GCE reject the tarball.
    """
    if raw.name != _GCE_DISK_NAME:
        raise RuntimeError(
            f"GCE tarball member must be named {_GCE_DISK_NAME}, got {raw.name}"
        )
    log.info("Packing %s -> %s (oldgnu tar.gz)", raw.name, output)
    subprocess.run(
        [
            "tar",
            "--format=oldgnu",
            "-Sczf",
            str(output),
            "-C",
            str(raw.parent),
            _GCE_DISK_NAME,
        ],
        check=True,
    )


def export_image(
    target_config: TargetConfig,
    kernel: str | None,
    output: Path,
    image_format: str = "qcow2",
    force: bool = False,
    disk_size_gb: int | None = None,
    ssh_key: Path | None = None,
) -> Path:
    """Build a self-contained bootable disk for the given target.

    Args:
        target_config: target whose base.ext4 + kernel to package.
        kernel: optional kernel selector (short or full); defaults
                to the target's default kernel.
        output: destination file path (parent will be created).
        image_format: "qcow2", "raw", or "gce" (a disk.raw tar.gz for
                Google Compute Engine's custom-image import).
        force: overwrite *output* if it exists.
        disk_size_gb: grow the disk to this many GiB instead of
                sizing it to the rootfs.  Must be at least as large
                as the rootfs needs.
        ssh_key: public key file to append to root's authorized_keys
                inside the image.

    Returns:
        The final written path.
    """
    if image_format not in _FORMATS:
        raise ValueError(
            f"unknown format: {image_format!r} "
            f"(expected one of {', '.join(_FORMATS)})"
        )
    if output.exists() and not force:
        raise FileExistsError(f"{output} exists; use --force to overwrite")
    if ssh_key is not None and not ssh_key.exists():
        raise FileNotFoundError(f"ssh key file not found: {ssh_key}")

    tools = _check_host_tools(image_format)
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

    kver_file = kdir / "build-tree" / "include" / "config" / "kernel.release"
    if not kver_file.exists():
        raise FileNotFoundError(
            f"Cannot read kernel release from {kver_file}. "
            f"Kernel build tree incomplete."
        )
    kver = kver_file.read_text().strip()

    t0 = time.monotonic()
    size_mb = _image_size_mb(base_ext4, kdir)
    if disk_size_gb is not None:
        requested_mb = disk_size_gb * _MB_PER_GIB
        if requested_mb < size_mb:
            raise ValueError(
                f"--disk-size-gb {disk_size_gb} is too small: this image "
                f"needs at least {size_mb} MiB"
            )
        size_mb = requested_mb
    if image_format == "gce":
        size_mb = _round_up_gib_mb(size_mb)
    log.info(
        "Exporting %s (kernel %s) -> %s (%s, ~%d MiB)",
        target_config.name,
        kernel_name,
        output,
        image_format,
        size_mb,
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
        _run(
            [
                "parted",
                "-s",
                str(raw),
                "mklabel",
                "msdos",
                "mkpart",
                "primary",
                "ext4",
                f"{_PART_OFFSET_MIB}MiB",
                "100%",
                "set",
                "1",
                "boot",
                "on",
            ]
        )

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
        _run(
            [
                "cp",
                "-a",
                "--reflink=auto",
                f"{src_mnt}/.",
                str(dst_mnt),
            ]
        )
        _run(["umount", str(src_mnt)])
        _losetup_detach(src_loop)
        src_loop = None

        # 4. Drop in kernel + initramfs.  image_build bakes these into
        #    /boot already; re-copy defensively so older images also work.
        #    dst_mnt is a root-owned mount, so the mkdir and cp's run via
        #    sudo.
        boot = dst_mnt / "boot"
        _run(["mkdir", "-p", str(boot)], quiet=True)
        _run(["cp", "-p", str(vmlinuz), str(boot / f"vmlinuz-{kver}")])
        initramfs_src = kdir / f"initramfs-{kver}.img"
        if initramfs_src.exists():
            _run(
                [
                    "cp",
                    "-p",
                    str(initramfs_src),
                    str(boot / f"initramfs-{kver}.img"),
                ]
            )
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

        # 5a. Guest-side fixups, while the rootfs is still mounted.
        #     The fstab rewrite is unconditional: /dev/vda is wrong for
        #     every exported (partitioned) disk, not just GCE's.
        _rewrite_fstab_root(dst_mnt, fs_uuid)
        if image_format == "gce":
            _apply_gce_guest_config(dst_mnt)
            _harden_gce_ssh(dst_mnt)
        if ssh_key is not None:
            _inject_ssh_key(dst_mnt, ssh_key)

        _run(
            [
                grub_install,
                "--target=i386-pc",
                f"--boot-directory={boot}",
                "--modules=part_msdos ext2 biosdisk",
                loop,
            ]
        )

        # 6. Tidy up.
        _run(["umount", str(dst_mnt)])
        _losetup_detach(loop)
        loop = None
        sudo_run(["e2fsck", "-fy", part], check=False, quiet=True)

        # 7. Convert to final format.
        output.parent.mkdir(parents=True, exist_ok=True)
        if image_format == "raw":
            shutil.move(str(raw), str(output))
        elif image_format == "gce":
            _package_gce(raw, output)
        else:
            _run(
                [
                    "qemu-img",
                    "convert",
                    "-f",
                    "raw",
                    "-O",
                    "qcow2",
                    "-c",
                    str(raw),
                    str(output),
                ]
            )

        elapsed = time.monotonic() - t0
        size_final_mb = output.stat().st_size / (1024 * 1024)
        log.info(
            "Wrote %s (%.0f MiB, %.0fs)",
            output,
            size_final_mb,
            elapsed,
        )
        return output

    finally:
        for m in (src_mnt, dst_mnt):
            sudo_run(["umount", str(m)], check=False, quiet=True)
        for d in (src_loop, loop):
            if d:
                _losetup_detach(d)
        shutil.rmtree(tmpdir, ignore_errors=True)
