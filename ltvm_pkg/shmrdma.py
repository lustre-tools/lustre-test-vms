"""shmrdma: RDMA and a virtio-net switch between VMs through host memory.

Two halves, both built from the sources under ``shmrdma/``:

* ``shmrdmad``, the host daemon.  It maps the RAM of every VM attached
  to it (QEMU hands over the memfd behind guest RAM through vhost-user)
  and moves data between guests with one memcpy: as an RDMA device
  (``--shmrdma``) and as a switch for virtio-net NICs (``--nic
  shmnet``).  ltvm builds it with the host compiler, cached by source
  hash, and runs one per user, started when a VM that needs it starts.
  It is never stopped or restarted behind a VM's back: QEMU's
  vhost-user devices do not reconnect, so a daemon restart breaks every
  attached guest's device until that VM restarts.

* ``shmrdma.ko``, the guest driver.  It is built per kernel in the
  target's build container and baked into the image (image_build), so a
  VM gets the device at boot with no deploy step.
"""

from __future__ import annotations

import errno
import fcntl
import getpass
import hashlib
import json
import logging
import os
import pwd
import select
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import vm_state
from .nic_spec import socket_path_problem, sun_path_problem
from .paths import load_meta_safe

if TYPE_CHECKING:
    from .target_config import TargetConfig
    from .vm_state import VMInfo

log = logging.getLogger(__name__)

SRC_DIR = Path(__file__).resolve().parent.parent / "shmrdma"

# Guest RAM QEMU can hand to shmrdmad: a memfd on Linux, POSIX shm
# (shm_open) elsewhere.
# (Through a variable: mypy evaluates sys.platform tests.)
_LINUX = sys.platform.startswith("linux")
SHARED_RAM_BACKEND = "memory-backend-memfd" if _LINUX else "memory-backend-shm"

_DAEMON_FILES = (
    "daemon/shmrdmad.c",
    "daemon/plat.h",
    "daemon/plat_linux.h",
    "daemon/plat_macos.h",
    "daemon/shmnet.c",
    "daemon/fault.c",
    "include/shmrdma_abi.h",
)
KMOD_FILES = ("kmod/shmrdma.c", "kmod/Makefile", "include/shmrdma_abi.h")
# Baked into the image next to the module, into /usr/local/sbin.
GUEST_FILES = ("guest/shmrdma-boot.sh", "tools/smc_ueid.py")

# Guest modprobe config baked next to the module.  The device is a
# vhost-user-fs to QEMU, so virtiofs matches it too; loading shmrdma
# first lets it take its device (it refuses any whose tag is not
# "shmrdma") before virtiofs sees it.
MODPROBE_CONF = (
    "# ltvm: shmrdma rides on a vhost-user-fs device tagged shmrdma\n"
    "softdep virtiofs pre: shmrdma\n"
)

DAEMON_CFLAGS = (
    "-O2",
    "-g",
    "-Wall",
    "-Wno-unused-parameter",
    "-Wno-unused-result",
    "-pthread",
    "-std=gnu11",
)


def _source_hash(files: tuple[str, ...]) -> str:
    h = hashlib.sha256()
    for rel in files:
        h.update(rel.encode())
        h.update(b"\0")
        h.update((SRC_DIR / rel).read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def kmod_source_hash() -> bytes:
    """The guest module's sources and boot files, as image inputs."""
    return _source_hash(KMOD_FILES + GUEST_FILES).encode()


# --- the guest module -------------------------------------------------


def kmod_dir(tc: TargetConfig, kernel: str | None = None) -> Path:
    """Per-kernel cache of the built module."""
    return tc.kernel_output_dir(kernel) / "shmrdma"


def _kmod_input_hash(kver: str, kernel_hash: str) -> str:
    h = hashlib.sha256()
    for part in (kver, kernel_hash, _source_hash(KMOD_FILES)):
        h.update(part.encode())
        h.update(b"\0")
    return h.hexdigest()


def user_kmod_dir(tc: TargetConfig, kernel: str | None = None) -> Path:
    """The per-user fallback for :func:`kmod_dir`, as ZFS has."""
    from .zfs_build import user_cache_root

    kdir = tc.kernel_output_dir(kernel)
    return user_cache_root() / "shmrdma" / tc.name / str(tc.arch) / kdir.name


def build_kmod(tc: TargetConfig, kernel: str | None, kver: str) -> Path | None:
    """shmrdma.ko for *kernel*, built if missing or stale.

    The kernel artifact's owner builds into it, anyone else into
    :func:`user_kmod_dir`, by the rules ZFS follows (zfs_build): a
    shared module is used only when nobody but that owner could have
    written it.  Returns None, with a warning, when it cannot be built:
    the image is then built without it, and ``create --shmrdma``
    refuses that image.
    """
    from .zfs_build import _no_more_writable_than, _shared_is_ours, _untrusted

    kdir = tc.kernel_output_dir(kernel)
    shared = kmod_dir(tc, kernel)
    user = user_kmod_dir(tc, kernel)
    build_tree = kdir / "build-tree"
    kmeta = load_meta_safe(tc.meta_path("kernel", kernel)) or {}
    kh = kmeta.get("input_hash")
    want = _kmod_input_hash(kver, kh if isinstance(kh, str) else "")

    def fresh(d: Path) -> bool:
        meta = load_meta_safe(d / "meta.json") or {}
        return meta.get("input_hash") == want and (d / "shmrdma.ko").is_file()

    def usable(d: Path) -> bool:
        return fresh(d) and (d != shared or _untrusted(shared, kdir) is None)

    if fresh(shared):
        why = _untrusted(shared, kdir)
        if why is None:
            return shared / "shmrdma.ko"
        log.warning(
            "shmrdma: ignoring the shared module in %s: %s", shared, why
        )
    if fresh(user):
        return user / "shmrdma.ko"
    if not build_tree.is_dir():
        log.warning("shmrdma: no kernel build tree at %s", build_tree)
        return None
    ours = _shared_is_ours(shared, kdir)
    out = shared if ours else user
    try:
        with _kmod_lock(out):
            # another image build of this kernel may have built it meanwhile
            if usable(out):
                return out / "shmrdma.ko"
            ko = _build_kmod_into(tc, build_tree, kver, want, out)
    except OSError as e:
        log.warning(
            "shmrdma.ko could not be built into %s (%s); the image will "
            "not have it, and create --shmrdma will refuse it",
            out,
            e,
        )
        return None
    if ours:
        try:
            _no_more_writable_than(out, kdir)
        except OSError:
            pass
    return ko


@contextmanager
def _kmod_lock(out: Path) -> Iterator[None]:
    """Serialize builds into one module dir; the lock sits beside it."""
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        out.parent / f".{out.name}.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o644,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _publish(path: Path, data: bytes) -> None:
    """Replace *path* whole: an image build may be copying the old one."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _build_kmod_into(
    tc: TargetConfig, build_tree: Path, kver: str, want: str, out: Path
) -> Path | None:
    out.mkdir(parents=True, exist_ok=True)
    ko = out / "shmrdma.ko"
    with tempfile.TemporaryDirectory(prefix="shmrdma-kmod-") as tmp:
        src = Path(tmp)
        for rel in KMOD_FILES:
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SRC_DIR / rel, src / rel)
        cmd = [
            "podman",
            "run",
            "--rm",
            "--security-opt",
            "label=disable",
            "-v",
            f"{build_tree}:/kbuild:ro",
            "-v",
            f"{src}:/src",
            "--entrypoint",
            "bash",
            tc.container_tag,
            "-c",
            "make -C /kbuild M=/src/kmod modules",
        ]
        log.info("Building shmrdma.ko for %s ...", kver)
        r = subprocess.run(cmd, capture_output=True, text=True)
        built = src / "kmod" / "shmrdma.ko"
        if r.returncode != 0 or not built.is_file():
            tail = "\n".join((r.stdout + r.stderr).splitlines()[-15:])
            log.warning(
                "shmrdma.ko did not build for %s; the image will not "
                "have it, and create --shmrdma will refuse it:\n%s",
                kver,
                tail,
            )
            return None
        _publish(ko, built.read_bytes())
    _publish(
        out / "meta.json",
        (
            json.dumps({"kernel": kver, "input_hash": want}, indent=2) + "\n"
        ).encode(),
    )
    return ko


def image_has_kmod(image: str | Path) -> bool:
    """Whether the image file at *image* was built with shmrdma.ko."""
    from .image_store import CURRENT, current_image

    path = Path(image)
    if path.name == CURRENT:
        path = current_image(path.parent) or path
    meta = load_meta_safe(path.parent / "meta.json") or {}
    per_file = meta.get("shmrdma_kmod_images")
    if isinstance(per_file, dict) and path.name in per_file:
        return bool(per_file[path.name])
    # Older meta.json, or a fetched image: the flag describes the
    # current image only.
    cur = current_image(path.parent)
    return (
        cur is not None
        and cur.name == path.name
        and bool(meta.get("shmrdma_kmod"))
    )


def image_has_nic_tags(image: str | Path) -> bool | None:
    """Whether the image at *image* binds --nic shmrdma devices; None
    when its directory's meta.json describes a newer image."""
    from .image_store import CURRENT, current_image

    path = Path(image)
    cur = current_image(path.parent)
    if path.name == CURRENT:
        path = cur or path
    if cur is None or cur.name != path.name:
        return None
    meta = load_meta_safe(path.parent / "meta.json") or {}
    return bool(meta.get("shmrdma_nic_tags"))


def vm_has_kmod(vm: VMInfo) -> bool:
    """Whether *vm*'s image has shmrdma.ko, by the VM's own record.

    A VM created before the record asks the image's meta.json, and
    records a yes, which a later fetch or build cannot then lose.
    """
    if vm.shmrdma_kmod is not None:
        return vm.shmrdma_kmod
    has = bool(vm.image) and image_has_kmod(vm.image)
    if has:
        try:
            vm._update_fields({"SHMRDMA_KMOD": 1}, noninteractive=True)
            vm.shmrdma_kmod = True
        except (OSError, RuntimeError, vm_state.VMNotFound):
            pass
    return has


# --- the host daemon --------------------------------------------------


class UnsafeDir(RuntimeError):
    """A daemon directory someone else owns or has redirected."""


def _owner() -> tuple[str, int]:
    """(user, uid) the daemon belongs to: under sudo, the one who typed it."""
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        name = os.environ["SUDO_USER"]
        return name, pwd.getpwnam(name).pw_uid
    return getpass.getuser(), os.geteuid()


def _check_private(d: Path, uid: int) -> None:
    """Refuse *d* unless it is a real directory of *uid* that only it can use."""
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
        raise UnsafeDir(f"{d} is not a directory owned by uid {uid}")
    if stat.S_IMODE(st.st_mode) & 0o077:
        if os.geteuid() != uid and os.geteuid() != 0:
            raise UnsafeDir(f"{d} is open to other users")
        # os.chmod(follow_symlinks=False) needs glibc 2.32 (not EL8)
        fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if not os.path.samestat(os.fstat(fd), st):
                raise UnsafeDir(f"{d} changed while it was checked")
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)


def _passwd(uid: int) -> pwd.struct_passwd:
    try:
        return pwd.getpwuid(uid)
    except KeyError:
        raise RuntimeError(f"uid {uid} has no passwd entry") from None


def _state_home(uid: int) -> Path:
    # Not $XDG_STATE_HOME: sudo drops it, and root must look where the
    # user put the daemon.
    return Path(_passwd(uid).pw_dir) / ".local" / "state"


def _candidate_dirs(uid: int) -> list[Path]:
    # a home shared over NFS must not give two hosts' daemons one directory
    host = socket.gethostname().split(".")[0] or "localhost"
    return [
        vm_state.VM_DIR / f"shmrdma-{uid}",
        _state_home(uid) / "ltvm" / f"shmrdma-{host}",
    ]


def _is_users_dir(d: Path, uid: int) -> bool:
    """Whether *d* is a real directory of *uid* (no repair, no raise)."""
    try:
        st = os.lstat(d)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == uid


def creatable(d: Path) -> bool:
    """Whether this process could make *d*: its nearest existing parent
    is a directory it can write."""
    p = d.parent
    while not os.path.lexists(p):
        p = p.parent
    return p.is_dir() and os.access(p, os.W_OK | os.X_OK)


def run_dir(uid: int, create: bool = True) -> Path:
    """The private directory of *uid*'s daemon: sockets, pid, log, lock.

    ``/opt/qemu-vms/shmrdma-<uid>`` (mode 0700, owned by the user), which
    outlives a login and which both a QEMU running as the user and one
    running as root reach; the user's state directory when that one
    cannot be used, or someone else made it first.

    Lookup and create agree: a candidate the user owns that holds a live
    daemon wins, then the first the user owns.  Only the user creates
    one, never root on the user's behalf, so a sudo run cannot move the
    daemon somewhere new.  With nothing yet, lookup names the first
    candidate nobody else has taken.
    """
    cands = _candidate_dirs(uid)
    mine = [d for d in cands if _is_users_dir(d, uid)]
    chosen = next((d for d in mine if Daemon("", uid, d).pid()), None)
    if chosen is None and mine:
        chosen = mine[0]
    if chosen is not None:
        if create:
            _check_private(chosen, uid)
        return chosen
    if create and os.geteuid() == uid:
        for d in cands:
            try:
                if d is cands[-1]:
                    d.parent.mkdir(parents=True, exist_ok=True)
                try:
                    d.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                _check_private(d, uid)
                return d
            except (OSError, UnsafeDir) as e:
                log.debug("shmrdmad: not using %s: %s", d, e)
        raise RuntimeError(
            f"no private directory for shmrdmad ({', '.join(map(str, cands))})"
        )
    return next((d for d in cands if not os.path.lexists(d)), cands[0])


def _open_private(path: Path, flags: int, uid: int) -> int:
    """os.open *path* in a checked directory: only a regular file of
    *uid*, never a symlink, and never waiting on a FIFO planted there."""
    fd = os.open(
        path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600
    )
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
        os.close(fd)
        raise OSError(errno.EPERM, "not a regular file of the daemon's", path)
    return fd


@dataclass(frozen=True)
class Daemon:
    """One user's shmrdmad: its files, whether or not it runs."""

    user: str
    uid: int
    dir: Path

    @property
    def rdma_socket(self) -> Path:
        return self.dir / "rdma.sock"

    @property
    def net_socket(self) -> Path:
        return self.dir / "net.sock"

    @property
    def ctl_socket(self) -> Path:
        # the daemon's default: its RDMA socket's path and ".ctl"
        return self.dir / "rdma.sock.ctl"

    @property
    def pid_path(self) -> Path:
        return self.dir / "shmrdmad.pid"

    @property
    def log_path(self) -> Path:
        return self.dir / "shmrdmad.log"

    @property
    def lock_path(self) -> Path:
        return self.dir / "shmrdmad.lock"

    def _record(self) -> list[str]:
        try:
            fd = _open_private(self.pid_path, os.O_RDONLY, self.uid)
        except OSError:
            return []
        with os.fdopen(fd) as f:
            # pid, source hash, then the binary's path, which may hold spaces
            line = f.read(8192).splitlines()
        return line[0].split(" ", 2) if line else []

    def pid(self) -> int:
        """The running daemon's pid, or 0.

        Only a shmrdmad process of this user, running the binary the pid
        file names, counts: a pid file is no proof of who listens.
        """
        rec = self._record()
        try:
            pid, exe_want = int(rec[0]), rec[2]
        except (ValueError, IndexError):
            return 0
        uid, comm, exe = _proc_info(pid)
        if comm != "shmrdmad" or uid != self.uid:
            return 0
        if exe == exe_want or os.path.realpath(exe) == os.path.realpath(
            exe_want
        ):
            return pid
        return 0

    def binary_hash(self) -> str:
        """Source hash the running daemon was built from, or ""."""
        rec = self._record()
        return rec[1] if len(rec) > 1 else ""

    def identity(self) -> str:
        """``pid:start`` of the running daemon, or "": a pid can be reused."""
        pid = self.pid()
        start = _proc_start(pid) if pid else ""
        return f"{pid}:{start}" if start else ""


def _proc_info(pid: int) -> tuple[int, str, str]:
    """(uid, command name, executable path) of *pid*; (-1, "", "") if gone."""
    if _LINUX:
        try:
            uid = os.stat(f"/proc/{pid}").st_uid
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return -1, "", ""
        # the cached binary was rebuilt or cleaned while it ran
        return uid, comm, exe.removesuffix(" (deleted)")
    # macOS: no /proc; ps prints the full executable path as comm
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "uid=", "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        uid_s, exe = r.stdout.strip().split(None, 1)
        return int(uid_s), Path(exe).name, exe
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return -1, "", ""


def _proc_start(pid: int) -> str:
    """When *pid* started, in the system's own units, or "" if gone."""
    if _LINUX:
        try:
            stat_line = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return ""
        # comm may hold spaces and parentheses: fields follow the last ')'
        fields = stat_line.rsplit(")", 1)[-1].split()
        return fields[19] if len(fields) > 19 else ""
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return "_".join(r.stdout.split())


def daemon(create: bool = True) -> Daemon:
    """This user's daemon; *create* false never makes its directory."""
    user, uid = _owner()
    return Daemon(user, uid, run_dir(uid, create))


def _vm_uid(vm: VMInfo) -> int | None:
    try:
        return os.stat(vm.info_path).st_uid
    except OSError:
        return None


def daemon_for(vm: VMInfo, create: bool = False) -> Daemon:
    """The daemon *vm*'s ``auto`` sockets name: its owner's, whoever runs
    ltvm.  A VM root owns (a classic ``sudo ltvm cluster create``) uses
    the invoking user's."""
    uid = _vm_uid(vm)
    if uid is None or uid in (0, _owner()[1]):
        return daemon(create)
    return Daemon(_passwd(uid).pw_name, uid, run_dir(uid, create))


def _cache_dir(uid: int | None = None) -> Path:
    uid = os.geteuid() if uid is None else uid
    if uid == os.geteuid() and os.environ.get("XDG_CACHE_HOME"):
        base = Path(os.environ["XDG_CACHE_HOME"])
    else:
        base = Path(_passwd(uid).pw_dir) / ".cache"
    return base / "ltvm" / "shmrdmad"


def _host_abi() -> str:
    """What a binary built here needs to run: another host sharing the
    cache in an NFS home may have another arch or an older libc."""
    import platform

    libc = ""
    if _LINUX:
        try:
            libc = os.confstr("CS_GNU_LIBC_VERSION") or ""
        except (ValueError, OSError):
            libc = ""
    else:
        libc = platform.mac_ver()[0]
    return f"{platform.system()} {platform.machine()} {libc}"


def daemon_digest() -> str:
    """What the daemon binary is built from: its sources and flags, for
    this host's arch and libc."""
    h = hashlib.sha256(_source_hash(_DAEMON_FILES).encode())
    h.update("\0".join(DAEMON_CFLAGS).encode())
    h.update(b"\0" + _host_abi().encode())
    return h.hexdigest()[:16]


def _compiler() -> list[str] | None:
    """The compiler command as found, $CC split as make splits it (so
    "ccache gcc" works), its first word resolved on PATH but run by that
    name, since a ccache wrapper is a compiler only when its argv[0]
    names one."""
    env = os.environ.get("CC", "")
    try:
        words = shlex.split(env)
    except ValueError:
        words = env.split()
    if not words:
        cc = shutil.which("cc") or shutil.which("gcc")
        words = [cc] if cc else []
    if not words:
        return None
    return [shutil.which(words[0]) or words[0], *words[1:]]


def build_daemon() -> tuple[Path, str]:
    """The shmrdmad binary for the current sources, building it if needed.

    A cached binary built by another compiler than the one found now is
    rebuilt; with no compiler at all, the cached one is used.
    """
    digest = daemon_digest()
    out = _cache_dir() / digest / "shmrdmad"
    stamp = out.with_name("cc")
    cc = _compiler()
    real_cc = " ".join([os.path.realpath(cc[0]), *cc[1:]]) if cc else None
    if out.is_file():
        try:
            built_by = stamp.read_text().strip()
        except OSError:
            built_by = ""
        if cc is None or built_by == real_cc:
            return out, digest
    if not cc:
        raise RuntimeError(
            "shmrdmad is built on the host and needs a C compiler: install gcc"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".shmrdmad.{os.getpid()}")
    try:
        r = subprocess.run(
            [*cc, *DAEMON_CFLAGS, "-o", str(tmp), "shmrdmad.c"],
            cwd=SRC_DIR / "daemon",
            capture_output=True,
            text=True,
        )
    except OSError as e:
        raise RuntimeError(
            f"building shmrdmad: cannot run {cc[0]}: {e.strerror}"
        ) from None
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"building shmrdmad failed:\n{r.stderr.strip()}")
    tmp.replace(out)
    stamp.write_text(f"{real_cc}\n")
    return out, digest


def daemon_needs_compiler() -> bool:
    """Whether starting this user's daemon would compile it: no binary
    is cached for the current sources and none runs."""
    try:
        if (_cache_dir() / daemon_digest() / "shmrdmad").is_file():
            return False
        return not daemon(create=False).pid()
    except (OSError, RuntimeError, KeyError):
        return True


def vm_uses(vm: VMInfo, d: Daemon) -> bool:
    """Whether *vm*, as last started, is attached to daemon *d*."""
    socks = {str(d.rdma_socket), str(d.net_socket)}
    return any(s in socks for s in vm.shm_sockets)


def vm_sockets(vm: VMInfo, d: Daemon) -> list[str]:
    """The vhost-user sockets *vm* connects to, with *d*'s for its own."""
    socks = []
    if vm.shmrdma:
        socks.append(str(d.rdma_socket) if vm.shmrdma == "auto" else vm.shmrdma)
    for nic in vm.nics:
        if nic.type == "shmnet":
            socks.append(nic.socket or str(d.net_socket))
        elif nic.type == "shmrdma":
            # its NIC, then the RDMA device bound to it
            socks += [str(d.net_socket), str(d.rdma_socket)]
    return socks


def attached_vms(d: Daemon) -> list[str]:
    """Running VMs of *d*'s user (or root's) that use daemon *d*.

    Anyone can write a socket path into a ``.info`` of their own, so
    another user's VM never counts.
    """
    return [vm.name for vm in _attached(d)]


def _attached(d: Daemon) -> list[VMInfo]:
    from .qemu_run import is_running
    from .vm_state import VMInfo, VMNotFound

    vms = []
    for name in VMInfo.all_names():
        try:
            vm = VMInfo.load(name)
            owner = os.stat(vm.info_path).st_uid
        except (VMNotFound, ValueError, OSError):
            continue
        if owner not in (d.uid, 0):
            continue
        if vm.shared_ram and vm_uses(vm, d) and is_running(vm):
            vms.append(vm)
    return vms


@contextmanager
def _daemon_lock(d: Daemon) -> Iterator[None]:
    # root only opens the user's lock: a file it created would be root's
    flags = os.O_RDWR | os.O_CREAT if os.geteuid() == d.uid else os.O_RDONLY
    fd = _open_private(d.lock_path, flags, d.uid)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _spawn_deferring_sigint(
    argv: list[str], **kw: Any
) -> subprocess.Popen[bytes]:
    """Popen, with a SIGINT that arrives while it runs raised only once it
    has returned, so the caller always holds the child it must kill.

    A handler, not a blocked mask: exec resets the handler in the child,
    but a blocked mask would be inherited by the daemon.
    """
    import threading

    if threading.current_thread() is not threading.main_thread():
        return subprocess.Popen(argv, **kw)
    pending: list[int] = []
    old = signal.signal(
        signal.SIGINT, lambda signum, frame: pending.append(signum)
    )
    try:
        proc: subprocess.Popen[bytes] = subprocess.Popen(argv, **kw)
    finally:
        signal.signal(signal.SIGINT, old)
    if pending:
        proc.kill()
        proc.wait()
        raise KeyboardInterrupt
    return proc


def ensure_daemon(d: Daemon | None = None) -> Daemon:
    """Start *d*, by default this user's shmrdmad, unless it already
    runs; return it."""
    d = d or daemon()
    for s in (d.rdma_socket, d.net_socket):
        problem = sun_path_problem(str(s))
        if problem:
            raise RuntimeError(f"shmrdmad cannot listen there: {problem}")
    if os.geteuid() != d.uid:
        # Under sudo: use the user's own daemon, never start one as root.
        if d.pid():
            return d
        raise RuntimeError(
            f"shmrdmad is not running for {d.user}, and ltvm will not start "
            f"it as root: run `ltvm vm shmrdmad start` as {d.user} first"
        )
    with _daemon_lock(d):
        if d.pid():
            if d.binary_hash() != daemon_digest():
                log.info(
                    "shmrdmad (pid %d) predates the current sources; it "
                    "keeps running until `ltvm vm shmrdmad stop`",
                    d.pid(),
                )
            return d
        binary, digest = build_daemon()
        # /proc/<pid>/exe is the resolved path, which pid() compares
        binary = binary.resolve()
        for s in (d.rdma_socket, d.net_socket):
            s.unlink(missing_ok=True)
        from .qemu_run import _guest_scope, _scope_refused
        from .vm_state import VMInfo

        scope = _guest_scope(VMInfo(name=f"shmrdmad-{d.user}", ip=""))
        cpus = daemon_cpus()
        if cpus and not shutil.which("taskset"):
            log.warning(
                "shmrdmad nodes are set but taskset is missing; unpinned "
                "(install util-linux)"
            )
            cpus = ""
        pin = ["taskset", "-c", cpus] if cpus else []
        extra = os.environ.get("LTVM_SHMRDMAD_ARGS", "").split()
        argv = [
            *pin,
            str(binary),
            "-s",
            str(d.rdma_socket),
            "-n",
            str(d.net_socket),
            *extra,
        ]
        # As QEMU's launch does: where systemd-run refuses the scope, run
        # without one rather than not at all.
        for prefix in (scope, []) if scope else ([],):
            logfd = _open_private(
                d.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, d.uid
            )
            proc = None
            # Only the pid file lets a stop find the daemon: one started
            # without it (Ctrl-C, a failed write) is killed here.
            try:
                with os.fdopen(logfd, "a") as logf:
                    logf.write(
                        f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"starting {binary}\n"
                    )
                    logf.flush()
                    logged = os.fstat(logf.fileno()).st_size
                    proc = _spawn_deferring_sigint(
                        [*prefix, *argv],
                        stdin=subprocess.DEVNULL,
                        stdout=logf,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                pidfd = _open_private(
                    d.pid_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, d.uid
                )
                with os.fdopen(pidfd, "w") as f:
                    f.write(f"{proc.pid} {digest} {binary}\n")
            except BaseException:
                if proc is not None:
                    proc.kill()
                    proc.wait()
                    d.pid_path.unlink(missing_ok=True)
                raise
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                if d.rdma_socket.is_socket() and d.net_socket.is_socket():
                    return d
                time.sleep(0.05)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            d.pid_path.unlink(missing_ok=True)
            if not (prefix and _scope_refused(d.log_path, logged)):
                break
        raise RuntimeError(f"shmrdmad did not come up; see {d.log_path}")


def daemon_nodes() -> list[int]:
    """Host NUMA nodes the daemon runs on: ``LTVM_SHMRDMAD_NODES`` or
    /etc/ltvm.conf's ``[shmrdma] nodes``, comma-separated; empty is
    unpinned."""
    raw = os.environ.get("LTVM_SHMRDMAD_NODES")
    if raw is None:
        import configparser

        from . import site_config

        parser = configparser.ConfigParser()
        try:
            parser.read(site_config.path())
            raw = parser.get("shmrdma", "nodes", fallback="")
        except configparser.Error:
            raw = ""
    try:
        return [int(n) for n in raw.replace(" ", "").split(",") if n]
    except ValueError:
        log.warning("shmrdmad nodes %r are not node numbers; unpinned", raw)
        return []


def daemon_cpus() -> str:
    """The cpulist the daemon is pinned to, or "" for none."""
    from .qemu_run import host_node_cpus

    lists = [host_node_cpus(n) for n in daemon_nodes()]
    if not all(lists):
        log.warning(
            "shmrdmad nodes %s: not all exist; unpinned", daemon_nodes()
        )
        return ""
    return ",".join(lists)


def stop_daemon(force: bool = False) -> str:
    """Stop this user's shmrdmad; refuse while VMs use it unless *force*."""
    from .qemu_run import _launch_lock

    d = daemon(create=False)
    # Under the launch lock: a VM whose launch found the sockets there
    # (verify_sockets) has no QEMU pid recorded until it is released.  Then the
    # daemon's lock, in the order a launch takes them, so a `vm shmrdmad
    # start` cannot slip a new daemon in before the files are removed.
    with _launch_lock():
        if not d.pid():
            return "shmrdmad is not running"
        with _daemon_lock(d):
            return _stop_locked(d, force)


def _stop_locked(d: Daemon, force: bool) -> str:
    pid = d.pid()
    if not pid:
        return "shmrdmad is not running"
    users = attached_vms(d)
    if users and not force:
        raise RuntimeError(
            f"shmrdmad is in use by {', '.join(users)}: their shmrdma "
            f"devices and shmnet NICs stop working until they restart.  "
            f"Stop them first, or pass --force"
        )
    _kill(d, pid)
    _remove_files(d)
    return f"stopped shmrdmad (pid {pid})"


def _remove_files(d: Daemon) -> None:
    """Unlink the pid file and sockets of *d*, possibly as root: through
    the directory *d.uid* owns, never a path the user can repoint."""
    try:
        fd = os.open(d.dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        if os.fstat(fd).st_uid != d.uid:
            return
        for p in (d.pid_path, d.rdma_socket, d.net_socket, d.ctl_socket):
            try:
                os.unlink(p.name, dir_fd=fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(fd)


_SYS_PIDFD_SEND_SIGNAL = 424  # the same on every Linux arch
_SYS_PIDFD_OPEN = 434
_KILL_WAIT_MS = 5000


def _syscall(nr: int, *args: object) -> int:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    rc = int(libc.syscall(nr, *args))
    if rc < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return rc


def _pidfd_open(pid: int) -> int | None:
    """A pidfd for *pid*, or None where there are none."""
    if hasattr(os, "pidfd_open"):
        return int(os.pidfd_open(pid))
    if not sys.platform.startswith("linux"):
        return None
    import ctypes

    return _syscall(_SYS_PIDFD_OPEN, ctypes.c_int(pid), ctypes.c_uint(0))


def _pidfd_signal(fd: int, sig: int) -> None:
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, sig)
        return
    import ctypes

    _syscall(
        _SYS_PIDFD_SEND_SIGNAL,
        ctypes.c_int(fd),
        ctypes.c_int(sig),
        None,
        ctypes.c_uint(0),
    )


def _kill(d: Daemon, pid: int) -> None:
    try:
        fd = _pidfd_open(pid)
    except OSError as e:
        if e.errno == errno.ESRCH:
            return
        fd = None
    if fd is None:
        _kill_by_pid(d, pid)
        return
    # The pidfd names one process: vouch for it after taking it, then
    # signal only through it, so a reused pid is never signalled.
    try:
        if d.pid() != pid:
            return
        try:
            _pidfd_signal(fd, signal.SIGTERM)
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            if not poll.poll(_KILL_WAIT_MS):
                _pidfd_signal(fd, signal.SIGKILL)
        except ProcessLookupError:
            pass
    finally:
        os.close(fd)


def _kill_by_pid(d: Daemon, pid: int) -> None:
    # A numeric pid can be reused at any time: vouch for it just before
    # each signal.
    try:
        if d.pid() != pid:
            return
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if d.pid() != pid:
                return
            time.sleep(0.1)
        if d.pid() == pid:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_dir(d: Path, uid: int) -> str | None:
    """Stop *uid*'s daemon whose files are in *d*, VMs or not, when its
    pid file vouches for it; what was stopped, or None.  For uninstall,
    possibly as root, so nothing in *d* is written."""
    try:
        st = os.lstat(d)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
        return None
    dm = Daemon("", uid, d)
    pid = dm.pid()
    if not pid:
        return None
    _kill(dm, pid)
    return f"pid {pid}"


def status() -> dict[str, object]:
    d = daemon(create=False)
    pid = d.pid()
    current = daemon_digest()
    return {
        "running": bool(pid),
        "cpus": daemon_cpus(),
        "pid": pid,
        "user": d.user,
        "rdma_socket": str(d.rdma_socket),
        "net_socket": str(d.net_socket),
        "ctl_socket": str(d.ctl_socket),
        "log": str(d.log_path),
        "current": (d.binary_hash() == current) if pid else None,
        "vms": attached_vms(d) if pid else [],
    }


def _needs_own(vm: VMInfo) -> bool:
    return vm.shmrdma == "auto" or any(
        (n.type == "shmnet" and not n.socket) or n.type == "shmrdma"
        for n in vm.nics
    )


def start_for(vm: VMInfo) -> None:
    """Start ltvm's own daemon if *vm* uses it and it is not running."""
    from .qemu_run import die

    if not _needs_own(vm):
        return
    try:
        ensure_daemon(daemon_for(vm, create=True))
    except (RuntimeError, OSError, KeyError) as e:
        die(f"{vm.name}: {e}")


def verify_sockets(vm: VMInfo) -> list[str]:
    """Every socket *vm* connects to, each checked to be listening, to
    belong to the VM's owner (root's VMs: root or the invoking user), and
    to sit where nobody else could replace it.

    Run under the launch lock, after ``start_for``: a socket given by
    hand belongs to a daemon run by hand, so it must already be there.
    A QEMU run through sudo is not held back by a socket's 0600 mode,
    so the owner is checked here.
    """
    from .qemu_run import die

    try:
        d = daemon_for(vm)
    except (RuntimeError, OSError, KeyError) as e:
        die(f"{vm.name}: {e}")
    owner = _vm_uid(vm)
    allowed = {0, d.uid} if owner is None or owner == 0 else {owner}
    socks = vm_sockets(vm, d)
    own = {str(d.rdma_socket), str(d.net_socket)}
    for s in socks:
        problem = sun_path_problem(s)
        if problem:
            die(f"{vm.name}: {problem}")
        problem = socket_path_problem(s)
        if problem and s in own:
            die(
                f"{vm.name}: shmrdmad's directory {d.dir} cannot hold its "
                f"sockets: QEMU needs an {problem}"
            )
        if problem:
            die(f"{vm.name}: {s} is not an {problem}")
        try:
            st = os.lstat(s)
        except PermissionError:
            die(
                f"{vm.name}: uid {os.geteuid()} cannot reach {s} (a "
                f"root-squashed NFS home?)"
            )
        except OSError:
            st = None
        if st is None or not stat.S_ISSOCK(st.st_mode):
            die(f"{vm.name} needs shmrdmad, but nothing listens on {s}")
        if st.st_uid not in allowed:
            die(
                f"{vm.name}: {s} belongs to uid {st.st_uid}, not to the "
                f"VM's owner: another user's daemon would map its RAM"
            )
        problem = replaceable(s, allowed | {0})
        if problem:
            die(
                f"{vm.name}: {problem}, so another user could put their "
                f"own socket at {s} before QEMU connects"
            )
        # ltvm's own daemon was vouched for by its pid; a probe would
        # make it set up and tear down a device
        if s not in own and _refused(s):
            die(f"{vm.name} needs shmrdmad, but nothing listens on {s}")
    return socks


def _refused(path: str) -> bool:
    """Whether a connect to the socket at *path* is refused: the socket
    of a daemon that is gone."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
        c.settimeout(5)
        try:
            c.connect(path)
        except ConnectionRefusedError:
            return True
        except OSError:
            return False
    return False


def replaceable(path: str, uids: set[int]) -> str | None:
    """Why a uid outside *uids* could replace the entry at *path*, or None.

    The path is walked as the kernel resolves it.  Every directory on the
    way must belong to one of *uids* and be writable by nobody else,
    unless it is sticky; so must every symlink, whose owner can repoint
    it, before its target is walked in turn.
    """
    parts = path.split("/")
    if not path.startswith("/"):
        parts = os.getcwd().split("/") + parts
    parts.reverse()
    cur = "/"
    hops = 0
    problem = _dir_problem(cur, uids)
    while parts and not problem:
        name = parts.pop()
        if name in ("", "."):
            continue
        if name == "..":
            cur = os.path.dirname(cur)
            continue
        nxt = os.path.join(cur, name)
        try:
            st = os.lstat(nxt)
        except OSError as e:
            if not any(p not in ("", ".") for p in parts):
                return None
            return f"{nxt} cannot be checked ({e.strerror})"
        if stat.S_ISLNK(st.st_mode):
            if st.st_uid not in uids:
                return f"{nxt} is a symlink belonging to uid {st.st_uid}"
            hops += 1
            if hops > 40:
                return f"{path} has too many levels of symlinks"
            target = os.readlink(nxt)
            if target.startswith("/"):
                cur = "/"
            parts.extend(reversed(target.split("/")))
            continue
        if not any(p not in ("", ".") for p in parts):
            return None
        if not stat.S_ISDIR(st.st_mode):
            return f"{nxt} is not a directory"
        cur = nxt
        problem = _dir_problem(cur, uids)
    return problem


def _dir_problem(d: str, uids: set[int]) -> str | None:
    try:
        st = os.lstat(d)
    except OSError as e:
        return f"{d} cannot be checked ({e.strerror})"
    if st.st_uid not in uids:
        return f"{d} belongs to uid {st.st_uid}"
    if st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX:
        return f"{d} is writable by other users"
    return None


def daemon_identity(vm: VMInfo, socks: list[str]) -> str:
    """Which ltvm daemon *vm* reaches through *socks*, or "" for none."""
    try:
        d = daemon_for(vm)
    except (RuntimeError, OSError, KeyError):
        return ""
    if not {str(d.rdma_socket), str(d.net_socket)} & set(socks):
        return ""
    return d.identity()


def check_root_reach(vm: VMInfo, socks: list[str]) -> None:
    """Refuse when a QEMU started through sudo could not open *socks*:
    root is squashed on an NFS home, where the daemon's directory falls
    back to on a classic host."""
    from .priv import sudo_run
    from .qemu_run import die

    if os.geteuid() == 0 or not _LINUX:
        return
    for s in socks:
        r = sudo_run(["stat", "-c", "%F", s], check=False, quiet=True)
        if r.returncode != 0 and "Permission denied" in (r.stderr or ""):
            die(
                f"{vm.name}: QEMU runs as root on this host, and root "
                f"cannot reach {s} (a root-squashed NFS home?).  Run "
                f"`sudo ltvm install` so QEMU runs as you, or run "
                f"shmrdmad by hand with sockets root can reach and pass "
                f"them with --shmrdma SOCKET / --nic shmnet:SOCKET"
            )


def orphaned_vms(d: Daemon) -> list[str]:
    """Running VMs of *d*'s user attached to a daemon at *d*'s sockets
    that is no longer running: stopped, or replaced by another."""
    now = d.identity()
    return [
        vm.name
        for vm in _attached(d)
        if not now or (vm.shm_daemon and vm.shm_daemon != now)
    ]
