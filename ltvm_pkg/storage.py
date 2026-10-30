"""Where artifacts live: a hot directory, and optionally a cold one.

A shared host keeps the targets in use on fast storage -- the base
images VM overlays read as backing files, the kernel build trees every
Lustre build reads, the kernel ccache -- and the rest on big, slow
storage.  The admin names both in /etc/ltvm.conf::

    [storage]
    hot = /var/cache/ltvm
    cold = /mnt/data/ltvm

``hot`` is the artifacts directory: ``LTVM_ARTIFACTS_DIR`` still wins
over it (one user, one command), and ``<repo>/artifacts`` is the default
without either (target_config._resolve_artifacts_dir).  ``cold`` is
optional and used only by ``ltvm target archive``/``restore``.

Archiving is invisible to every path computation: it moves
``<hot>/<target>/<arch>`` to the same place under ``<cold>`` and leaves
a symlink behind, so ARTIFACTS_DIR / target / arch still names the
artifacts and a VM whose .info or overlay names a path under the hot
directory still boots.  Restore moves the tree back and drops the link.
The unit is always one arch directory -- ``<hot>/<target>`` stays a real
directory -- so a target with one arch archived and one not needs no
special case anywhere.

A move between filesystems copies to a temporary name beside the
destination, checks the copy (same names, types, sizes, mtimes and link
targets), and only then switches names and removes the source.  Any
failure before the switch removes the partial copy and leaves the source
as it was.  mtimes are kept to the nanosecond because a VM records its
base image as ``size:mtime_ns`` (IMAGE_ID) and refuses to boot when that
changes.
"""

from __future__ import annotations

import configparser
import contextlib
import errno
import fcntl
import grp
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import site_config

ENV = "LTVM_ARTIFACTS_DIR"
SECTION = "storage"

# Room to leave on the destination beyond the tree being moved.
HEADROOM_BYTES = 1024**3

_warned: set[str] = set()


class StorageError(Exception):
    """A storage setting or a move that cannot go ahead, said plainly."""


def _warn(problem: str) -> None:
    if problem not in _warned:
        _warned.add(problem)
        print(f"warning: {problem}", file=sys.stderr)


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------


def _read(key: str) -> tuple[Path | None, str | None]:
    """``[storage] <key>`` from the site file: (path, problem).

    A relative path is a problem rather than something to resolve
    against whatever directory ltvm happened to run in.
    """
    site = site_config.path()
    parser = configparser.ConfigParser()
    try:
        parser.read(site)
        raw = parser.get(SECTION, key, fallback="").strip()
    except configparser.Error as e:
        return None, f"ignoring [{SECTION}] in {site}: {e}"
    if not raw:
        return None, None
    p = Path(raw)
    if not p.is_absolute():
        return None, (
            f"ignoring [{SECTION}] {key} in {site}: {raw!r} is not an "
            f"absolute path"
        )
    return p, None


def configured(key: str) -> Path | None:
    """``[storage] hot`` or ``cold`` if set and absolute; else None.

    A bad setting warns once per process.
    """
    path, problem = _read(key)
    if problem:
        _warn(problem)
    return path


def hot_root() -> Path:
    """The artifacts directory in effect (target_config.ARTIFACTS_DIR)."""
    from . import target_config

    return target_config.ARTIFACTS_DIR


def hot_source() -> str:
    """Where hot_root() came from, for doctor."""
    site = site_config.path()
    if ENV in os.environ:
        return ENV
    if _read("hot")[0] is not None:
        return f"[{SECTION}] hot in {site}"
    return "default"


def cold_root() -> Path:
    """The cold directory, or StorageError saying why there is none.

    It must already exist: an unmounted disk's mount point is the likely
    reason it does not, and creating it would fill the root filesystem.
    """
    site = site_config.path()
    path, problem = _read("cold")
    if problem:
        raise StorageError(problem)
    if path is None:
        raise StorageError(
            f"no cold storage configured: set [{SECTION}] cold = <dir> "
            f"in {site}"
        )
    if not path.is_dir():
        raise StorageError(
            f"cold storage {path} ([{SECTION}] cold in {site}) does not "
            f"exist or is not a directory -- is its filesystem mounted?"
        )
    return path


def _under(path: str | Path, root: str | Path) -> bool:
    p = os.path.normpath(os.path.abspath(path))
    r = os.path.normpath(os.path.abspath(root))
    return p == r or p.startswith(r.rstrip("/") + "/")


# ------------------------------------------------------------------
# Group sharing
# ------------------------------------------------------------------


@dataclass(frozen=True)
class Sharing:
    """How a storage directory is shared with a group."""

    group: str
    setgid: bool
    group_writable: bool
    default_acl: bool

    def describe(self) -> str:
        bits = [f"group {self.group}"]
        if self.setgid:
            bits.append("setgid")
        if self.group_writable:
            bits.append("group-writable")
        if self.default_acl:
            bits.append("default ACL")
        return ", ".join(bits)


def _has_default_acl(path: Path) -> bool:
    # POSIX ACLs are Linux's; macOS has no os.getxattr.
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        return False
    try:
        return bool(getxattr(path, "system.posix_acl_default"))
    except OSError:
        return False


def sharing(path: Path) -> Sharing | None:
    """How *path* is shared with its group, or None if it is not.

    Shared means setgid, or group-writable for a group other than the
    user's own: with user private groups a 0775 home directory is not
    shared with anyone.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    setgid = bool(st.st_mode & stat.S_ISGID)
    gw = bool(st.st_mode & stat.S_IWGRP)
    if not setgid and not (gw and st.st_gid != os.getgid()):
        return None
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return Sharing(group, setgid, gw, _has_default_acl(path))


def shared_roots() -> list[Path]:
    """The hot and cold directories that are shared with a group."""
    roots = [hot_root()]
    cold, _ = _read("cold")
    if cold is not None:
        roots.append(cold)
    return [r for r in roots if sharing(r) is not None]


def share_new_files() -> bool:
    """Leave the group's permission bits out of the umask, when shared.

    Another member of the group must be able to rebuild over, prune or
    archive what this user writes, and that needs the directories (and
    files updated in place, like ccache's) group-writable.  A default
    ACL does this by itself; a setgid directory alone does not, since
    the usual umask 022 strips group write from everything.  Returns
    True when the umask was changed.
    """
    if not shared_roots():
        return False
    old = os.umask(0)
    os.umask(old & ~0o070)
    return True


def podman_share_args(cmd: list[str]) -> list[str]:
    """``podman run`` options that keep a shared tree group-writable.

    podman gives a container its own umask (0022 by default), not the
    caller's, so a kernel build writing into a shared artifacts tree
    leaves files only their creator can replace.  Returned only when one
    of *cmd*'s read-write bind mounts is under a shared storage root.
    CCACHE_UMASK is what ccache documents for a shared cache.
    """
    roots = shared_roots()
    if not roots:
        return []
    for i, tok in enumerate(cmd):
        if tok in ("-v", "--volume") and i + 1 < len(cmd):
            spec = cmd[i + 1]
        elif tok.startswith("--volume="):
            spec = tok.split("=", 1)[1]
        else:
            continue
        parts = spec.split(":")
        if len(parts) < 2:
            continue
        if len(parts) > 2 and "ro" in parts[2].split(","):
            continue
        src = os.path.realpath(parts[0])
        if any(_under(src, os.path.realpath(r)) for r in roots):
            return ["--umask", "0002", "-e", "CCACHE_UMASK=002"]
    return []


# ------------------------------------------------------------------
# What is archived
# ------------------------------------------------------------------


def archived(arch_dir: Path) -> Path | None:
    """Where an archived ``<hot>/<target>/<arch>`` really is, or None."""
    if not arch_dir.is_symlink():
        return None
    return Path(os.path.realpath(arch_dir))


def storage_fields(arch_dir: Path) -> dict[str, str | None]:
    """The ``storage``/``cold_path`` pair reported for an arch dir.

    One place for what `build status` and `target list` say about
    ``<hot>/<target>/<arch>``, so the two cannot disagree: "cold" with
    where it really is when archived, else "hot" and None.
    """
    cold = archived(arch_dir)
    return {
        "storage": "cold" if cold else "hot",
        "cold_path": str(cold) if cold else None,
    }


def archived_links(path: Path) -> list[tuple[Path, Path]]:
    """(link, real dir) for each archived arch dir at or under *path*.

    *path* is ``<hot>/<target>/<arch>`` or ``<hot>/<target>``.  Only
    links into the cold directory count: a link somewhere else was not
    made by archive, and deleting what it points at is not ltvm's call.
    A directory this user cannot read, such as lost+found when hot is
    a filesystem of its own, holds nothing archive made.
    """
    try:
        cold = os.path.realpath(cold_root())
    except StorageError:
        return []
    if path.is_symlink():
        candidates = [path]
    elif path.is_dir():
        try:
            candidates = sorted(p for p in path.iterdir() if p.is_symlink())
        except OSError:
            return []
    else:
        return []
    out = []
    for link in candidates:
        real = os.path.realpath(link)
        if os.path.isdir(real) and _under(real, cold):
            out.append((link, Path(real)))
    return out


# ------------------------------------------------------------------
# Copying, checked
# ------------------------------------------------------------------


def _data_extents(fd: int, size: int) -> Iterator[tuple[int, int]]:
    """(offset, length) of each data region, so holes stay holes.

    VM base images are sparse; a plain copy would allocate every byte.
    """
    seek_data = getattr(os, "SEEK_DATA", None)
    seek_hole = getattr(os, "SEEK_HOLE", None)
    if seek_data is None or seek_hole is None or size == 0:
        if size:
            yield 0, size
        return
    try:
        os.lseek(fd, 0, seek_data)
    except OSError as e:
        if e.errno == errno.ENXIO:  # all hole
            return
        yield 0, size  # SEEK_DATA unsupported here
        return
    off = 0
    while off < size:
        try:
            data = os.lseek(fd, off, seek_data)
        except OSError as e:
            if e.errno == errno.ENXIO:
                return
            raise
        hole = os.lseek(fd, data, seek_hole)
        yield data, hole - data
        off = hole


def _copy_file(src: str, dst: str, size: int) -> None:
    fin = os.open(src, os.O_RDONLY)
    try:
        fout = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            for off, length in _data_extents(fin, size):
                end = off + length
                while off < end:
                    buf = os.pread(fin, min(4 << 20, end - off), off)
                    if not buf:
                        raise OSError(errno.EIO, "unexpected EOF", src)
                    pending = memoryview(buf)
                    while pending:
                        written = os.pwrite(fout, pending, off)
                        if written <= 0:
                            raise OSError(
                                errno.EIO, "write made no progress", dst
                            )
                        off += written
                        pending = pending[written:]
            os.ftruncate(fout, size)
        finally:
            os.close(fout)
    finally:
        os.close(fin)


def _share_mode(mode: int, *, parent_setgid: bool, is_dir: bool) -> int:
    """*mode* with the group given the owner's bits (umask 002 terms)."""
    mode |= (mode & 0o700) >> 3
    if is_dir and parent_setgid:
        mode |= stat.S_ISGID
    return mode


def _copy_tree(src: Path, dst: Path, *, share: bool) -> None:
    """Copy *src* to the new directory *dst*: files sparse, links as links.

    Modes and nanosecond mtimes are kept, not owners (only root could);
    on a shared destination the group also gets the owner's bits, and
    directories keep the setgid bit their parent hands down.
    """
    st = os.lstat(src)
    os.mkdir(dst, 0o700)
    with os.scandir(src) as it:
        entries = sorted(it, key=lambda e: e.name)
    for e in entries:
        s, d = Path(e.path), dst / e.name
        est = e.stat(follow_symlinks=False)
        if stat.S_ISLNK(est.st_mode):
            os.symlink(os.readlink(s), d)
            with contextlib.suppress(NotImplementedError, OSError):
                os.utime(
                    d,
                    ns=(est.st_atime_ns, est.st_mtime_ns),
                    follow_symlinks=False,
                )
        elif stat.S_ISDIR(est.st_mode):
            _copy_tree(s, d, share=share)
        elif stat.S_ISREG(est.st_mode):
            _copy_file(str(s), str(d), est.st_size)
            mode = stat.S_IMODE(est.st_mode)
            if share:
                mode = _share_mode(mode, parent_setgid=False, is_dir=False)
            os.chmod(d, mode)
            os.utime(d, ns=(est.st_atime_ns, est.st_mtime_ns))
        else:
            raise StorageError(
                f"cannot copy {s}: not a file, directory or symlink"
            )
    mode = stat.S_IMODE(st.st_mode)
    if share:
        parent_setgid = bool(os.stat(dst.parent).st_mode & stat.S_ISGID)
        mode = _share_mode(mode, parent_setgid=parent_setgid, is_dir=True)
    os.chmod(dst, mode)
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))


def _verify_copy(src: Path, dst: Path) -> None:
    """Raise StorageError unless *dst* holds what *src* does."""
    stack = [Path()]
    while stack:
        rel = stack.pop()
        with os.scandir(src / rel) as it:
            a = {e.name: e.stat(follow_symlinks=False) for e in it}
        with os.scandir(dst / rel) as it:
            b = {e.name: e.stat(follow_symlinks=False) for e in it}
        if a.keys() != b.keys():
            missing = sorted(a.keys() - b.keys())[:3]
            extra = sorted(b.keys() - a.keys())[:3]
            raise StorageError(
                f"copy of {src / rel} differs: missing {missing}, "
                f"unexpected {extra}"
            )
        for name, sa in a.items():
            sb = b[name]
            r = rel / name
            if stat.S_IFMT(sa.st_mode) != stat.S_IFMT(sb.st_mode):
                raise StorageError(f"copy of {src / r} has another type")
            if stat.S_ISDIR(sa.st_mode):
                stack.append(r)
            elif stat.S_ISLNK(sa.st_mode):
                if os.readlink(src / r) != os.readlink(dst / r):
                    raise StorageError(f"copy of link {src / r} differs")
            elif (sa.st_size, sa.st_mtime_ns) != (sb.st_size, sb.st_mtime_ns):
                raise StorageError(
                    f"copy of {src / r} differs: size:mtime "
                    f"{sa.st_size}:{sa.st_mtime_ns} became "
                    f"{sb.st_size}:{sb.st_mtime_ns}"
                )


@dataclass
class Survey:
    """What moving a tree takes: its allocated bytes, and what blocks it."""

    bytes: int = 0
    problems: list[str] = field(default_factory=list)


def survey(root: Path, *, max_problems: int = 5) -> Survey:
    """Size *root* and check this user can copy it and then delete it.

    Checked up front so a move never ends with two copies because the
    source could not be removed: every directory must be writable, and
    in a sticky one every entry must be ours.
    """
    out = Survey()
    uid = os.geteuid()
    count = 0

    def problem(msg: str) -> None:
        nonlocal count
        count += 1
        if len(out.problems) < max_problems:
            out.problems.append(msg)

    def onerror(e: OSError) -> None:
        problem(f"cannot read {e.filename}: {e.strerror}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        try:
            dst = os.lstat(dirpath)
        except OSError as e:
            problem(f"cannot read {dirpath}: {e.strerror}")
            continue
        if not os.access(dirpath, os.R_OK | os.W_OK | os.X_OK):
            problem(
                f"{dirpath} is not writable by you (owner uid "
                f"{dst.st_uid}, mode {stat.S_IMODE(dst.st_mode):o})"
            )
        sticky = dst.st_mode & stat.S_ISVTX and dst.st_uid != uid
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
            except OSError as e:
                problem(f"cannot read {p}: {e.strerror}")
                continue
            if sticky and st.st_uid != uid:
                problem(f"{p} belongs to uid {st.st_uid} in sticky {dirpath}")
            if stat.S_ISREG(st.st_mode):
                out.bytes += st.st_blocks * 512
                if not os.access(p, os.R_OK):
                    problem(f"{p} is not readable by you")
    if count > len(out.problems):
        out.problems.append(f"... and {count - len(out.problems)} more")
    return out


def _check_space(sv: Survey, dest_dir: Path) -> None:
    free = shutil.disk_usage(dest_dir).free
    need = sv.bytes + HEADROOM_BYTES
    if free < need:
        raise StorageError(
            f"not enough space in {dest_dir}: the move needs "
            f"{_fmt_bytes(sv.bytes)} plus {_fmt_bytes(HEADROOM_BYTES)} "
            f"headroom, and {_fmt_bytes(free)} is free"
        )


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def _existing(path: Path) -> Path:
    """*path*, or its nearest ancestor that exists."""
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _same_fs(a: Path, b: Path) -> bool:
    """Whether a rename can move *a* into *b*."""
    return a.stat().st_dev == b.stat().st_dev


def _remove_tree(path: Path) -> None:
    from .podman_run import rmtree

    rmtree(path)


def relocate(
    src: Path,
    dest: Path,
    *,
    link_back: bool,
    share: bool = False,
    remove: Callable[[Path], None] | None = None,
) -> str | None:
    """Move the directory *src* to *dest*, safely across filesystems.

    *dest* must not exist, or be a symlink to *src* (a restore), which
    is replaced.  With *link_back*, *src* becomes a symlink to *dest*
    (an archive).  Returns a warning when everything moved but the old
    copy could not all be removed; raises StorageError, with *src* as it
    was and no partial copy left, when the move did not happen.
    """
    remove = remove or _remove_tree
    if not src.is_dir() or src.is_symlink():
        raise StorageError(f"{src} is not a directory")
    replace_link = dest.is_symlink()
    if replace_link:
        if os.path.realpath(dest) != os.path.realpath(src):
            raise StorageError(f"{dest} is a symlink to somewhere else")
    elif os.path.lexists(dest):
        raise StorageError(f"{dest} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tag = f".{dest.name}.ltvm-{os.getpid()}"
    same_fs = _same_fs(src, dest.parent)

    staged = src
    if not same_fs:
        staged = dest.parent / f"{tag}-copy"
        try:
            _copy_tree(src, staged, share=share)
            _verify_copy(src, staged)
        except BaseException as e:
            shutil.rmtree(staged, ignore_errors=True)
            if isinstance(e, OSError):
                raise StorageError(f"copying {src} failed: {e}") from e
            raise

    def undo_copy() -> None:
        if not same_fs:
            shutil.rmtree(staged, ignore_errors=True)

    aside: Path | None = None
    if replace_link:
        aside = dest.parent / f"{tag}-link"
        try:
            os.rename(dest, aside)
        except OSError as e:
            undo_copy()
            raise StorageError(f"cannot move {dest} aside: {e}") from e

    def put_back_link() -> None:
        if aside is not None:
            os.rename(aside, dest)

    try:
        os.rename(staged, dest)
    except OSError as e:
        put_back_link()
        undo_copy()
        raise StorageError(f"cannot rename into {dest}: {e}") from e

    retire: Path | None = None if same_fs else src
    if link_back:
        try:
            if same_fs:
                # src was renamed away just now; put the link in its place.
                os.symlink(dest, src)
            else:
                link_tmp = src.parent / f".{src.name}.ltvm-{os.getpid()}-link"
                os.symlink(dest, link_tmp)
                old = src.parent / f".{src.name}.ltvm-{os.getpid()}-old"
                try:
                    os.rename(src, old)
                except OSError:
                    os.unlink(link_tmp)
                    raise
                try:
                    os.rename(link_tmp, src)
                except OSError:
                    os.rename(old, src)
                    os.unlink(link_tmp)
                    raise
                retire = old
        except OSError as e:
            if same_fs:
                os.rename(dest, src)
            else:
                shutil.rmtree(dest, ignore_errors=True)
            put_back_link()
            raise StorageError(f"cannot link {src} to {dest}: {e}") from e

    if aside is not None:
        with contextlib.suppress(OSError):
            os.unlink(aside)
    if retire is not None:
        try:
            remove(retire)
        except OSError as e:
            return (
                f"moved, but could not remove all of the old copy at "
                f"{retire}: {e}; remove it by hand"
            )
    return None


# ------------------------------------------------------------------
# VMs that use a tree
# ------------------------------------------------------------------


@dataclass(frozen=True)
class VMRef:
    """One path a VM uses: its .info IMAGE or KERNEL, or its backing file."""

    vm: str
    running: bool
    what: str  # "IMAGE", "KERNEL" or "backing file"
    path: str
    overlay: Path | None = None
    has_info: bool = True


def _vm_running(vm: object) -> bool:
    from .qemu_run import is_running

    try:
        return bool(is_running(vm))  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return False


def vm_references() -> list[VMRef]:
    """Every artifact path a VM on this host uses."""
    from . import image_store, vm_state

    refs: list[VMRef] = []
    names = vm_state.VMInfo.all_names()
    for name in names:
        try:
            vm = vm_state.VMInfo.load(name)
        except (vm_state.VMNotFound, ValueError, OSError):
            continue
        running = _vm_running(vm)
        if vm.image:
            refs.append(VMRef(name, running, "IMAGE", vm.image))
        if vm.kernel:
            refs.append(VMRef(name, running, "KERNEL", vm.kernel))
        backing = image_store.overlay_backing_file(vm.overlay_path)
        if backing is not None:
            refs.append(
                VMRef(
                    name, running, "backing file", str(backing), vm.overlay_path
                )
            )
    try:
        overlays = sorted(vm_state.OVERLAYS.glob("*.qcow2"))
    except OSError:
        overlays = []
    for overlay in overlays:
        if overlay.stem in names:
            continue
        backing = image_store.overlay_backing_file(overlay)
        if backing is not None:
            refs.append(
                VMRef(
                    overlay.stem,
                    False,
                    "backing file",
                    str(backing),
                    overlay,
                    has_info=False,
                )
            )
    return refs


# ------------------------------------------------------------------
# archive / restore
# ------------------------------------------------------------------


@dataclass
class Move:
    target: str
    arch: str
    hot: Path  # <hot>/<target>/<arch>, the path everything uses
    cold: Path  # <cold>/<target>/<arch>
    src: Path  # what moves: hot on archive, cold on restore
    dest: Path
    bytes: int = 0
    refs: list[VMRef] = field(default_factory=list)
    # (ref, new path) for references a restore would leave dangling.
    repoint: list[tuple[VMRef, str]] = field(default_factory=list)
    warning: str | None = None


def _valid_name(name: str, what: str) -> None:
    if not name or name.startswith(".") or "/" in name or name == "cache":
        raise StorageError(f"{name!r} is not a {what}")


def _plan(target: str, arch: str | None, *, to_cold: bool) -> list[Move]:
    _valid_name(target, "target")
    if arch is not None:
        _valid_name(arch, "arch")
    hot_t = hot_root() / target
    cold_t = cold_root() / target
    if to_cold:
        if arch is not None:
            arches = [arch]
        elif hot_t.is_dir():
            arches = sorted(
                p.name
                for p in hot_t.iterdir()
                if p.is_dir()
                and not p.is_symlink()
                and not p.name.startswith(".")
            )
        else:
            arches = []
    elif arch is not None:
        arches = [arch]
    elif cold_t.is_dir():
        arches = sorted(
            p.name
            for p in cold_t.iterdir()
            if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
        )
    else:
        arches = []

    moves = []
    for a in arches:
        hot, cold = hot_t / a, cold_t / a
        if to_cold:
            if hot.is_symlink():
                raise StorageError(
                    f"{target}/{a} is already archived: {hot} -> "
                    f"{os.path.realpath(hot)}"
                )
            if not hot.is_dir():
                raise StorageError(f"nothing to archive: no {hot}")
            if os.path.lexists(cold):
                raise StorageError(
                    f"{cold} already exists; remove it, or restore it "
                    f"first if it is an older archive of {target}/{a}"
                )
            moves.append(Move(target, a, hot, cold, hot, cold))
        else:
            if not cold.is_dir() or cold.is_symlink():
                raise StorageError(f"nothing to restore: no {cold}")
            if hot.is_symlink():
                if os.path.realpath(hot) != os.path.realpath(cold):
                    raise StorageError(
                        f"{hot} is a symlink to {os.readlink(hot)}, not to "
                        f"{cold}"
                    )
            elif os.path.lexists(hot):
                raise StorageError(
                    f"{target}/{a} exists in both places: {hot} and {cold}; "
                    f"remove the one you do not want"
                )
            moves.append(Move(target, a, hot, cold, cold, hot))
    if not moves:
        where = hot_t if to_cold else cold_t
        raise StorageError(
            f"nothing to {'archive' if to_cold else 'restore'} under {where}"
        )
    return moves


def _match_refs(moves: list[Move], refs: list[VMRef], *, to_cold: bool) -> None:
    """Attach to each move the VM references into it.

    A reference spelled through the hot path keeps working either way:
    archive leaves a symlink there and restore puts the tree back.  One
    spelled through the cold path (``create --image`` resolves symlinks)
    stops working on restore and is what --force repoints.
    """
    for m in moves:
        real_src = os.path.realpath(m.src)
        for ref in refs:
            via_hot = _under(ref.path, m.hot)
            via_cold = _under(ref.path, m.cold)
            real = os.path.realpath(ref.path)
            if not (via_hot or via_cold or _under(real, real_src)):
                continue
            m.refs.append(ref)
            if to_cold or via_hot:
                continue
            if via_cold:
                rel = os.path.relpath(os.path.normpath(ref.path), m.cold)
            else:
                rel = os.path.relpath(real, real_src)
            m.repoint.append((ref, str(m.hot / rel)))


def _refusal(moves: list[Move], *, force: bool, to_cold: bool) -> str | None:
    running = sorted({r.vm for m in moves for r in m.refs if r.running})
    if running:
        return (
            f"VMs running on these artifacts: {', '.join(running)}.  "
            f"Stop them first (ltvm stop <vm>); a running VM is never "
            f"moved under, --force included."
        )
    users = sorted({r.vm for m in moves for r in m.refs})
    if users and not force:
        lines = [f"VMs use these artifacts: {', '.join(users)}"]
        for m in moves:
            for r in m.refs:
                lines.append(f"  {r.vm}: {r.what} {r.path}")
        if to_cold:
            lines.append(
                "--force archives anyway: the VMs keep working through the "
                "symlink left at the old path, reading from cold storage."
            )
        else:
            lines.append(
                "--force restores anyway, repointing any VM that names a "
                "cold path (its .info and its overlay's backing file)."
            )
        return "\n".join(lines)
    for m in moves:
        for ref, _new in m.repoint:
            if ref.what == "backing file" and ref.overlay is not None:
                if not os.access(ref.overlay, os.W_OK):
                    return (
                        f"{ref.vm}'s overlay {ref.overlay} names "
                        f"{ref.path} and is not writable by you, so it "
                        f"cannot be repointed; ask its owner to run the "
                        f"restore"
                    )
    return None


def _repoint(ref: VMRef, new: str) -> None:
    from . import vm_state

    if ref.what in ("IMAGE", "KERNEL"):
        vm = vm_state.VMInfo.load(ref.vm)
        vm._update_fields({ref.what: new}, noninteractive=True)
        return
    assert ref.overlay is not None
    r = subprocess.run(
        [
            vm_state.QEMU_IMG,
            "rebase",
            "-u",
            "-b",
            new,
            "-F",
            "raw",
            str(ref.overlay),
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise OSError(
            r.stderr.strip() or f"qemu-img rebase exited {r.returncode}"
        )


@contextlib.contextmanager
def _target_lock(target: str) -> Iterator[None]:
    d = hot_root() / target
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".storage.lock", "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise StorageError(
                f"another archive or restore of {target} is running"
            ) from e
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def move_target(
    target: str,
    arch: str | None,
    *,
    to_cold: bool,
    force: bool = False,
    say: Callable[[str], None] = print,
) -> list[Move]:
    """``ltvm target archive`` (to_cold) or ``restore``.

    Every check is made for every arch before anything moves.  Raises
    StorageError when nothing was moved; returns the moves, each with a
    warning when something after the switch went wrong.
    """
    moves = _plan(target, arch, to_cold=to_cold)
    _match_refs(moves, vm_references(), to_cold=to_cold)
    refusal = _refusal(moves, force=force, to_cold=to_cold)
    if refusal:
        raise StorageError(refusal)

    total = 0
    for m in moves:
        sv = survey(m.src)
        if sv.problems:
            raise StorageError(
                f"cannot move {m.src}:\n  " + "\n  ".join(sv.problems)
            )
        m.bytes = sv.bytes
        total += sv.bytes
    for m in moves:
        for d in (m.src.parent, _existing(m.dest.parent)):
            if not os.access(d, os.W_OK | os.X_OK):
                raise StorageError(f"{d} is not writable by you")
    probe = _existing(moves[0].dest.parent)
    if not _same_fs(moves[0].src, probe):
        _check_space(Survey(bytes=total), probe)

    share = sharing(cold_root() if to_cold else hot_root()) is not None
    verb = "archiving" if to_cold else "restoring"
    with _target_lock(target):
        for m in moves:
            say(
                f"{verb} {m.target}/{m.arch} ({_fmt_bytes(m.bytes)}): "
                f"{m.src} -> {m.dest}"
            )
            try:
                m.warning = relocate(
                    m.src, m.dest, link_back=to_cold, share=share
                )
            except StorageError as e:
                done = [f"{d.target}/{d.arch}" for d in moves[: moves.index(m)]]
                if done:
                    raise StorageError(
                        f"{e}\n({', '.join(done)} moved; {m.target}/{m.arch} "
                        f"and the rest did not)"
                    ) from e
                raise
            if to_cold:
                say(f"  {m.hot} now links to {m.cold}")
            else:
                with contextlib.suppress(OSError):
                    m.cold.parent.rmdir()  # only if empty
            moved_vms = {ref.vm for ref, _ in m.repoint}
            kept = sorted({r.vm for r in m.refs} - moved_vms)
            if kept:
                say(f"  unchanged paths, still valid for: {', '.join(kept)}")
            for ref, new in m.repoint:
                try:
                    _repoint(ref, new)
                    say(f"  repointed {ref.vm}'s {ref.what}: {new}")
                except Exception as e:  # noqa: BLE001
                    msg = (
                        f"could not repoint {ref.vm}'s {ref.what} from "
                        f"{ref.path} to {new}: {e}"
                    )
                    m.warning = f"{m.warning}; {msg}" if m.warning else msg
            if m.warning:
                say(f"  warning: {m.warning}")
    return moves


# ------------------------------------------------------------------
# doctor
# ------------------------------------------------------------------


def _describe_dir(path: Path) -> tuple[str, bool]:
    """One line about a storage directory, and whether it is writable."""
    if not path.exists():
        return "does not exist", False
    if not path.is_dir():
        return "is not a directory", False
    bits = []
    writable = os.access(path, os.W_OK | os.X_OK)
    bits.append("writable by you" if writable else "NOT writable by you")
    sh = sharing(path)
    bits.append(f"group-shared ({sh.describe()})" if sh else "not group-shared")
    try:
        bits.append(f"{_fmt_bytes(shutil.disk_usage(path).free)} free")
    except OSError:
        pass
    return ", ".join(bits), writable


def doctor_report() -> tuple[list[str], list[str]]:
    """(issues, notes) about the hot and cold directories, for doctor."""
    issues: list[str] = []
    notes: list[str] = []
    site = site_config.path()
    for key in ("hot", "cold"):
        _path, problem = _read(key)
        if problem:
            issues.append(f"storage: {problem}")

    hot = hot_root()
    src = hot_source()
    if src == ENV and _read("hot")[0] is not None:
        src += f", over [{SECTION}] hot in {site}"
    desc, writable = _describe_dir(hot)
    if not hot.exists():
        if src == "default":
            notes.append(f"storage hot: {hot} (default): not created yet")
        else:
            issues.append(f"storage hot: {hot} ({src}): does not exist")
    else:
        notes.append(f"storage hot: {hot} ({src}): {desc}")
        if not writable:
            issues.append(
                f"storage hot: {hot} is not writable by you -- builds and "
                f"fetches will fail (on a shared host, join its group)"
            )

    cold, problem = _read("cold")
    if cold is None:
        if not problem:
            notes.append(
                f"storage cold: not configured ([{SECTION}] cold in {site})"
            )
        return issues, notes
    desc, writable = _describe_dir(cold)
    if not cold.is_dir():
        issues.append(
            f"storage cold: {cold} ([{SECTION}] cold in {site}) {desc} -- "
            f"is its filesystem mounted?"
        )
        return issues, notes
    archived_n = 0
    if hot.is_dir():
        for tdir in hot.iterdir():
            if tdir.is_dir() and not tdir.is_symlink():
                archived_n += len(archived_links(tdir))
    notes.append(
        f"storage cold: {cold}: {desc}; {archived_n} arch dir(s) archived"
    )
    if not writable:
        notes.append(
            "  (not writable by you: target archive/restore will be refused)"
        )
    return issues, notes
