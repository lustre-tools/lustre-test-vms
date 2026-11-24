"""Versioned kernels, and the VMs that boot them.

A kernel artifact directory is keyed on the kernel VERSION, so a
rebuild whose inputs moved without moving kernel.release (a config
overlay change, a patch edit) lands in the same directory.  The
container's ``cp`` then wrote the new ``vmlinuz`` over the old one in
place, and every VM created earlier -- whose root image carries the
modules of the kernel that was current when the image was built --
booted the new kernel with its old modules: "disagrees about version
of symbol module_layout", ``modprobe lnet``: Exec format error.

So each build's (and each fetch's) ``vmlinuz`` is also linked under a
name of its own, ``vmlinuz-<UTC stamp>-<hex>``, a hard link to the same
inode.  ``vmlinuz`` stays a real file naming the current kernel, which
is what every reader, publish and an older ltvm expect; before a build
or fetch writes it again, :func:`detach_current` takes the name off the
inode (the versioned link keeps the bytes), so the write makes a new
file instead of changing the old one.  ``create`` records the versioned
file (:func:`current_kernel`) and its identity (``KERNEL_ID``), and
``start`` refuses a VM whose kernel moved (:func:`check_kernel`).

A ``vmlinuz`` with no versioned link is a legacy one (built before
this scheme, or put there by hand); :func:`detach_current` renames it
to a versioned name, so a VM that recorded it by identity can still
find it.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .image_store import image_identity

if TYPE_CHECKING:
    from .vm_state import VMInfo

CURRENT = "vmlinuz"
_VERSIONED_RE = re.compile(r"^vmlinuz-\d{8}T\d{6}-[0-9a-f]{6}$")

kernel_identity = image_identity


def is_versioned_name(name: str) -> bool:
    return bool(_VERSIONED_RE.match(name))


def versioned_files(kdir: Path) -> list[Path]:
    """Every versioned vmlinuz in *kdir*, oldest name first."""
    try:
        entries = sorted(kdir.iterdir())
    except OSError:
        return []
    return [
        p
        for p in entries
        if is_versioned_name(p.name) and p.is_file() and not p.is_symlink()
    ]


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    """The same inode, or (where a hard link could not be made) a copy
    with the same identity."""
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino) or (
        a.st_size == b.st_size and a.st_mtime_ns == b.st_mtime_ns
    )


def _versioned_twin(path: Path) -> Path | None:
    """The versioned file that is *path*'s kernel, or None."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    for f in versioned_files(path.parent):
        try:
            if _same_file(f.stat(), st):
                return f
        except OSError:
            continue
    return None


def current_kernel(kdir: Path) -> Path | None:
    """The kernel file new VMs in *kdir* boot, or None if there is none.

    The versioned link to ``vmlinuz`` when it has one, so callers
    record the name that stays put; otherwise ``vmlinuz`` itself (a
    legacy build, an older fetch, or an out-of-tree kernel put there by
    hand), else ``vmlinux``.
    """
    vmlinuz = kdir / CURRENT
    if vmlinuz.is_file():
        if vmlinuz.is_symlink():
            return vmlinuz
        return _versioned_twin(vmlinuz) or vmlinuz
    vmlinux = kdir / "vmlinux"
    return vmlinux if vmlinux.is_file() else None


def _versioned_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"vmlinuz-{stamp}-{secrets.token_hex(3)}"


def _new_versioned_path(kdir: Path) -> Path:
    while True:
        dest = kdir / _versioned_name()
        if not os.path.lexists(dest):
            return dest


def install_current(kdir: Path) -> Path | None:
    """Give a freshly written ``vmlinuz`` its versioned link.

    Returns the versioned path, or None when there is no real
    ``vmlinuz`` to version (a symlink is the user's and left alone).
    """
    vmlinuz = kdir / CURRENT
    if not vmlinuz.is_file() or vmlinuz.is_symlink():
        return None
    twin = _versioned_twin(vmlinuz)
    if twin is not None:
        return twin
    dest = _new_versioned_path(kdir)
    try:
        os.link(vmlinuz, dest)
    except OSError:
        # no hard links here: a copy with the same size and mtime
        shutil.copy2(vmlinuz, dest)
    return dest


def detach_current(kdir: Path) -> Path | None:
    """Take the name ``vmlinuz`` off the current kernel's inode.

    Called before anything writes ``vmlinuz`` again, so the write makes
    a new file: the container's ``cp``, or tar, would otherwise write
    through to the inode a VM boots.  A legacy ``vmlinuz`` (no versioned
    link) is renamed to a versioned name.  Returns the versioned file
    that was current, or None.  A symlink is left alone.
    """
    vmlinuz = kdir / CURRENT
    if not vmlinuz.is_file() or vmlinuz.is_symlink():
        return None
    twin = _versioned_twin(vmlinuz)
    if twin is not None:
        vmlinuz.unlink()
        return twin
    dest = _new_versioned_path(kdir)
    os.rename(vmlinuz, dest)
    return dest


def restore_current(kdir: Path, previous: Path | None) -> None:
    """Point ``vmlinuz`` back at *previous* if nothing replaced it."""
    vmlinuz = kdir / CURRENT
    if previous is None or os.path.lexists(vmlinuz):
        return
    try:
        os.link(previous, vmlinuz)
    except OSError:
        try:
            shutil.copy2(previous, vmlinuz)
        except OSError:
            pass


@contextmanager
def replacing(kdir: Path) -> Iterator[None]:
    """Around a build or fetch that writes ``kdir/vmlinuz``.

    On success the new file is versioned and superseded versioned files
    no VM uses are pruned; on failure the previous kernel is current
    again unless the failed run left a ``vmlinuz`` of its own.
    """
    previous = detach_current(kdir)
    try:
        yield
    except BaseException:
        restore_current(kdir, previous)
        raise
    if install_current(kdir) is None:
        restore_current(kdir, previous)
        return
    prune_superseded(kdir)


def referenced_kernels() -> tuple[set[str], set[str]]:
    """Real paths of every kernel a VM on this host records, and the
    identities (``KERNEL_ID``) they recorded.

    A file whose identity a VM recorded is in use even when the path it
    names now holds another kernel: :func:`check_kernel` boots it.
    """
    from . import vm_state

    paths: set[str] = set()
    ids: set[str] = set()
    for name in vm_state.VMInfo.all_names():
        try:
            vm = vm_state.VMInfo.load(name)
            kernel, kernel_id = vm.kernel, vm.kernel_id
        except vm_state.VMNotFound:
            continue
        except (ValueError, OSError):
            # One this ltvm cannot load (a NIC type it does not know)
            # still boots its kernel: read the two keys raw.
            kernel, kernel_id = _raw_kernel_keys(
                vm_state.SOCKETS / f"{name}.info"
            )
        if kernel:
            paths.add(os.path.realpath(kernel))
        if kernel_id:
            ids.add(kernel_id)
    return paths, ids


def _raw_kernel_keys(info: Path) -> tuple[str, str]:
    vals = {"KERNEL": "", "KERNEL_ID": ""}
    try:
        for line in info.read_text().splitlines():
            key, _, value = line.partition("=")
            if key in vals:
                vals[key] = value
    except OSError:
        pass
    return vals["KERNEL"], vals["KERNEL_ID"]


def in_use(f: Path, refs: tuple[set[str], set[str]] | None = None) -> bool:
    paths, ids = refs if refs is not None else referenced_kernels()
    if os.path.realpath(f) in paths:
        return True
    try:
        return kernel_identity(f) in ids
    except OSError:
        return False


def superseded(
    kdir: Path, refs: tuple[set[str], set[str]] | None = None
) -> list[Path]:
    """Versioned files in *kdir* that are not current and no VM uses."""
    vmlinuz = kdir / CURRENT
    try:
        cur = os.stat(vmlinuz) if vmlinuz.is_file() else None
    except OSError:
        cur = None
    if refs is None:
        refs = referenced_kernels()
    out = []
    for f in versioned_files(kdir):
        try:
            if cur is not None and _same_file(f.stat(), cur):
                continue
        except OSError:
            continue
        if in_use(f, refs):
            continue
        out.append(f)
    return out


def prune_superseded(kdir: Path) -> int:
    """Remove *kdir*'s versioned kernels that are neither current nor
    used by a VM.  Returns the bytes freed."""
    freed = 0
    for f in superseded(kdir):
        try:
            size = f.stat().st_size
            f.unlink()
        except OSError:
            continue
        freed += size
    return freed


def _by_identity(kdir: Path, ident: str) -> Path | None:
    for f in versioned_files(kdir):
        try:
            if kernel_identity(f) == ident:
                return f
        except OSError:
            continue
    return None


def _repoint(vm: VMInfo, path: Path, ident: str) -> None:
    """Record *path* as *vm*'s kernel, in memory and, if it can be
    written without a prompt, in its .info."""
    vm.kernel = str(path)
    vm.kernel_id = ident
    try:
        vm._update_fields(
            {"KERNEL": str(path), "KERNEL_ID": ident}, noninteractive=True
        )
    except Exception:  # noqa: BLE001
        pass


def _fix_hint(vm: VMInfo) -> str:
    return (
        f"  Its root image holds the modules of the kernel it was created "
        f"with, so booting another kernel fails to load them "
        f'("disagrees about version of symbol", Exec format error).\n'
        f"  Recreate the VM: ltvm destroy {vm.name}, then ltvm create\n"
        f"  Or keep its disks: `ltvm vm set {vm.name} --current-kernel`, "
        f"start it, copy {Path(vm.kernel).parent}/modules/lib/modules/"
        f"<kver> into the guest's /lib/modules/ and run depmod -a, then "
        f"redeploy Lustre"
    )


def kernel_problem(vm: VMInfo) -> str | None:
    """Why *vm*'s recorded kernel no longer boots it, or None.

    Read-only, for doctor: no record is adopted or repointed.  A VM
    whose kernel moved but whose recorded identity is still on disk
    under a versioned name is no problem -- a start boots that file.
    """
    if not vm.kernel:
        return None
    path = Path(vm.kernel)
    try:
        now = kernel_identity(path)
    except FileNotFoundError:
        if vm.kernel_id and _by_identity(path.parent, vm.kernel_id):
            return None
        return f"kernel file gone: {path}"
    except OSError:
        return None
    if vm.kernel_id:
        if vm.kernel_id == now or _by_identity(path.parent, vm.kernel_id):
            return None
        return f"kernel changed since create: {path}"
    if not vm.created:
        return None
    st = os.stat(path)
    if max(st.st_mtime, st.st_ctime) > vm.created:
        return f"kernel changed after create (no KERNEL_ID): {path}"
    return None


def check_kernel(vm: VMInfo) -> str | None:
    """Return why *vm* must not boot, or None if its kernel is intact.

    A VM records its kernel's identity at create.  When the path it
    names now holds another kernel (or is gone) but a versioned file
    with the recorded identity is still there, the VM is pointed at
    that file.  One without a record (created before it existed) is
    warned about when the kernel changed after the VM was created, and
    otherwise adopts the record, and the versioned name, so later
    rebuilds are caught.
    """
    if not vm.kernel:
        return None
    path = Path(vm.kernel)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        if vm.kernel_id:
            found = _by_identity(path.parent, vm.kernel_id)
            if found is not None:
                _repoint(vm, found, vm.kernel_id)
                return None
        return f"the kernel of '{vm.name}' is gone: {path}\n" + _fix_hint(vm)
    except OSError:
        return None
    now = f"{st.st_size}:{st.st_mtime_ns}"
    if vm.kernel_id:
        if vm.kernel_id == now:
            return None
        found = _by_identity(path.parent, vm.kernel_id)
        if found is not None:
            _repoint(vm, found, vm.kernel_id)
            return None
        return (
            f"the kernel of '{vm.name}' has changed since the VM was "
            f"created: {path}\n"
            f"  (size:mtime was {vm.kernel_id}, is {now})\n" + _fix_hint(vm)
        )
    if not vm.created:
        return None
    if max(st.st_mtime, st.st_ctime) > vm.created:
        print(
            f"warning: the kernel of '{vm.name}' ({path}) changed after "
            f"the VM was created.  If a kernel rebuild or fetch replaced "
            f"it, the guest's modules will not load (\"disagrees about "
            f'version of symbol"); recreate the VM if they do not.',
            file=sys.stderr,
        )
        return None
    _repoint(vm, _versioned_twin(path) or path, now)
    return None
