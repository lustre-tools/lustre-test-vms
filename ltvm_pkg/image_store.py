"""Versioned base images, and the VM overlays that sit on them.

Each image build lands under a name of its own,
``base-<UTC stamp>-<hex>.ext4``, and ``current.ext4`` -- a symlink in
the same directory -- names the one new VMs get.  A VM's overlay is
created against the versioned file, never the pointer, so no later
build changes the bytes under an existing overlay.

``base.ext4`` is the legacy name.  Images built before this scheme sit
there as a plain file that existing overlays name as their backing
file, so nothing writes it again: it is the current image only for a
directory with no pointer, and ``ltvm clean`` removes it once no
overlay refers to it.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .vm_state import VMInfo

CURRENT = "current.ext4"
LEGACY = "base.ext4"
_VERSIONED_RE = re.compile(r"^base-\d{8}T\d{6}-[0-9a-f]{6}\.ext4$")

_QCOW2_MAGIC = b"QFI\xfb"


def is_image_name(name: str) -> bool:
    return name == LEGACY or bool(_VERSIONED_RE.match(name))


def current_image(image_dir: Path) -> Path | None:
    """The image new VMs in *image_dir* get, or None if there is none.

    The pointer is resolved one level, to the versioned file inside
    *image_dir*, so callers record the name that stays put.
    """
    pointer = image_dir / CURRENT
    if pointer.is_symlink():
        target = Path(os.readlink(pointer))
        if not target.is_absolute():
            target = image_dir / target
        return target if target.is_file() else None
    legacy = image_dir / LEGACY
    return legacy if legacy.is_file() else None


def image_files(image_dir: Path) -> list[Path]:
    """Every image file in *image_dir*, legacy and versioned, oldest name first."""
    try:
        entries = sorted(image_dir.iterdir())
    except OSError:
        return []
    return [
        p
        for p in entries
        if is_image_name(p.name) and p.is_file() and not p.is_symlink()
    ]


def _versioned_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"base-{stamp}-{secrets.token_hex(3)}.ext4"


def set_current(image_dir: Path, name: str) -> None:
    """Point ``current.ext4`` at *name*, atomically."""
    tmp = image_dir / f".{CURRENT}.{secrets.token_hex(4)}"
    os.symlink(name, tmp)
    try:
        os.replace(tmp, image_dir / CURRENT)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def install_image(src: Path, image_dir: Path) -> Path:
    """Move a finished image into *image_dir* and make it current.

    *src* must be on the same filesystem as *image_dir*.  Returns the
    versioned path.
    """
    from .rail_image import capability_path, require_capability

    sidecar = capability_path(src)
    has_capability = os.path.lexists(sidecar)
    if has_capability:
        # Validate before any rename so a corrupt archive cannot become current.
        require_capability(src)
    for _attempt in range(32):
        dest = image_dir / _versioned_name()
        if not os.path.lexists(dest) and not os.path.lexists(
            capability_path(dest)
        ):
            break
    else:
        raise RuntimeError("could not choose a free versioned image name")
    os.rename(src, dest)
    if has_capability:
        # Publish support with the same immutable filename before changing current.
        try:
            os.rename(sidecar, capability_path(dest))
        except BaseException:
            # Leave the extracted pair available when its marker could not move.
            os.rename(dest, src)
            raise
    set_current(image_dir, dest.name)
    return dest


def remove_image(image: Path) -> None:
    """Remove one unreferenced image and its exact per-image capability marker."""
    from .rail_image import CAPABILITY_SUFFIX

    # Delete beside this filename; a replaced image symlink must not select a target.
    sidecar = Path(str(image) + CAPABILITY_SUFFIX)
    image.unlink()
    # A missing marker is normal for images made before rail profiles existed.
    sidecar.unlink(missing_ok=True)


def image_identity(path: str | Path) -> str:
    """Size and mtime: what a rebuild or a refetch over *path* changes.

    The inode is left out so a copied artifacts tree (cp -a, rsync -a)
    still matches.
    """
    st = os.stat(path)
    return f"{st.st_size}:{st.st_mtime_ns}"


def overlay_backing_file(overlay: Path) -> Path | None:
    """The backing file named in a qcow2 overlay's header, or None.

    A relative name is resolved against the overlay's directory, as
    QEMU does.
    """
    try:
        with open(overlay, "rb") as f:
            hdr = f.read(20)
            if len(hdr) < 20 or hdr[:4] != _QCOW2_MAGIC:
                return None
            offset = int.from_bytes(hdr[8:16], "big")
            size = int.from_bytes(hdr[16:20], "big")
            if not offset or not size or size > 1023:
                return None
            f.seek(offset)
            raw = f.read(size)
    except OSError:
        return None
    name = raw.decode("utf-8", "surrogateescape")
    if name.startswith("json:"):
        return None
    p = Path(name)
    return p if p.is_absolute() else overlay.parent / p


def referenced_images() -> tuple[set[str], list[str]]:
    """Real paths of every image an overlay on this host is backed by.

    Also returns the names of overlays whose backing file could not be
    determined -- neither the header nor the VM's .info names one --
    so a caller deciding what is safe to delete can refuse to guess.
    """
    from . import vm_state

    refs: set[str] = set()
    unknown: list[str] = []
    recorded: dict[str, str] = {}
    for name in vm_state.VMInfo.all_names():
        try:
            image = vm_state.VMInfo.load(name).image
        except (vm_state.VMNotFound, ValueError, OSError):
            continue
        if image:
            recorded[name] = image
            refs.add(os.path.realpath(image))
    try:
        overlays = sorted(vm_state.OVERLAYS.glob("*.qcow2"))
    except OSError:
        overlays = []
    for overlay in overlays:
        backing = overlay_backing_file(overlay)
        if backing is not None:
            refs.add(os.path.realpath(backing))
        elif overlay.stem not in recorded and not _has_no_backing(overlay):
            unknown.append(overlay.stem)
    return refs, unknown


def prune_superseded(image_dir: Path) -> int:
    """Remove *image_dir*'s image files that are neither current nor used.

    Returns the bytes freed.  Removes nothing while any overlay's
    backing file is unknown.
    """
    current = current_image(image_dir)
    if current is None:
        return 0
    refs, unknown = referenced_images()
    if unknown:
        return 0
    freed = 0
    for f in image_files(image_dir):
        if f == current or os.path.realpath(f) in refs:
            continue
        try:
            size = f.stat().st_size
            remove_image(f)
        except OSError:
            continue
        freed += size
    return freed


def _has_no_backing(overlay: Path) -> bool:
    """True for a readable qcow2 that names no backing file at all."""
    try:
        with open(overlay, "rb") as f:
            hdr = f.read(20)
    except OSError:
        return False
    return (
        len(hdr) == 20
        and hdr[:4] == _QCOW2_MAGIC
        and int.from_bytes(hdr[8:16], "big") == 0
    )


def check_backing(vm: VMInfo) -> str | None:
    """Return why *vm* must not boot, or None if its base image is intact.

    A VM records its base image's identity at create.  One that does not
    (created before the record existed) is warned about when the image
    changed after the VM was created, and otherwise adopts the record so
    later changes are caught.
    """
    from_header = overlay_backing_file(vm.overlay_path)
    backing = from_header or (Path(vm.image) if vm.image else None)
    if backing is None:
        return None
    try:
        st = os.stat(backing)
    except FileNotFoundError:
        if from_header is None and not vm.image_id:
            return None
        return (
            f"the base image of '{vm.name}' is gone: {backing}\n"
            f"  Recreate the VM: ltvm destroy {vm.name}, then ltvm create"
        )
    except OSError:
        return None
    now = f"{st.st_size}:{st.st_mtime_ns}"
    if vm.image_id:
        if vm.image_id == now:
            return None
        return (
            f"the base image under '{vm.name}' has changed since the VM "
            f"was created: {backing}\n"
            f"  (size:mtime was {vm.image_id}, is {now})\n"
            f"  Its root disk overlay holds only the blocks the VM wrote, "
            f"so booting it over a different image corrupts its root "
            f"filesystem.\n"
            f"  Recreate the VM: ltvm destroy {vm.name}, then ltvm create"
        )
    if not vm.created:
        return None
    changed = max(st.st_mtime, st.st_ctime)
    if changed > vm.created:
        print(
            f"warning: the base image of '{vm.name}' ({backing}) changed "
            f"after the VM was created.  If an image rebuild or fetch "
            f"replaced it, the root filesystem will read as corrupt; "
            f"recreate the VM if it fails to boot.",
            file=sys.stderr,
        )
        return None
    try:
        vm._update_fields({"IMAGE_ID": now}, noninteractive=True)
        vm.image_id = now
    except Exception:  # noqa: BLE001
        pass
    return None
