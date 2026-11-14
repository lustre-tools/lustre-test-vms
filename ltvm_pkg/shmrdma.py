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

import fcntl
import getpass
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import vm_state
from .paths import load_meta_safe

if TYPE_CHECKING:
    from .target_config import TargetConfig
    from .vm_state import VMInfo

log = logging.getLogger(__name__)

SRC_DIR = Path(__file__).resolve().parent.parent / "shmrdma"

_DAEMON_FILES = (
    "daemon/shmrdmad.c",
    "daemon/shmnet.c",
    "include/shmrdma_abi.h",
)
KMOD_FILES = ("kmod/shmrdma.c", "kmod/Makefile", "include/shmrdma_abi.h")

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
    """The guest module's sources, as image_build folds them in."""
    return _source_hash(KMOD_FILES).encode()


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


def build_kmod(tc: TargetConfig, kernel: str | None, kver: str) -> Path | None:
    """shmrdma.ko for *kernel*, built if missing or stale.

    Returns None, with a warning, when it cannot be built: the image is
    then built without it, and ``create --shmrdma`` refuses that image.
    """
    out = kmod_dir(tc, kernel)
    ko = out / "shmrdma.ko"
    build_tree = tc.kernel_output_dir(kernel) / "build-tree"
    kmeta = load_meta_safe(tc.meta_path("kernel", kernel)) or {}
    kh = kmeta.get("input_hash")
    want = _kmod_input_hash(kver, kh if isinstance(kh, str) else "")
    meta = load_meta_safe(out / "meta.json") or {}
    if meta.get("input_hash") == want and ko.is_file():
        return ko
    if not build_tree.is_dir():
        log.warning("shmrdma: no kernel build tree at %s", build_tree)
        return None
    out.mkdir(parents=True, exist_ok=True)
    ko.unlink(missing_ok=True)
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
        shutil.copy2(built, ko)
    (out / "meta.json").write_text(
        json.dumps({"kernel": kver, "input_hash": want}, indent=2) + "\n"
    )
    return ko


def image_has_kmod(image: str | Path) -> bool:
    """Whether the image at *image* was built with shmrdma.ko."""
    meta = load_meta_safe(Path(image).parent / "meta.json") or {}
    return bool(meta.get("shmrdma_kmod"))


# --- the host daemon --------------------------------------------------


def _user() -> str:
    """The user the daemon is run for: under sudo, the one who typed it."""
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        return os.environ["SUDO_USER"]
    return getpass.getuser()


def run_dir() -> Path:
    """Where the daemons' sockets, pids and logs live.

    Under the VM directory, which outlives a login and which both a
    QEMU running as the user and one running as root can reach; a
    user's own state directory when that is not writable.
    """
    d = vm_state.VM_DIR / "shmrdma"
    try:
        if not d.is_dir():
            d.mkdir()
            # Every user's daemon keeps its files here; sticky, as /tmp.
            os.chmod(d, 0o3777)
        if os.access(d, os.W_OK | os.X_OK):
            return d
    except OSError:
        pass
    state = Path(
        os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    )
    d = state / "ltvm" / "shmrdma"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass(frozen=True)
class Daemon:
    """One user's shmrdmad: its files, whether or not it runs."""

    user: str
    dir: Path

    @property
    def rdma_socket(self) -> Path:
        return self.dir / f"{self.user}.rdma.sock"

    @property
    def net_socket(self) -> Path:
        return self.dir / f"{self.user}.net.sock"

    @property
    def pid_path(self) -> Path:
        return self.dir / f"{self.user}.pid"

    @property
    def log_path(self) -> Path:
        return self.dir / f"{self.user}.log"

    @property
    def lock_path(self) -> Path:
        return self.dir / f"{self.user}.lock"

    def pid(self) -> int:
        """The running daemon's pid, or 0."""
        try:
            pid = int(self.pid_path.read_text().split()[0])
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except (OSError, ValueError, IndexError):
            return 0
        return pid if comm == "shmrdmad" else 0

    def binary_hash(self) -> str:
        """Source hash the running daemon was built from, or ""."""
        try:
            return self.pid_path.read_text().split()[1]
        except (OSError, IndexError):
            return ""


def daemon() -> Daemon:
    return Daemon(_user(), run_dir())


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "ltvm" / "shmrdmad"


def build_daemon() -> tuple[Path, str]:
    """The shmrdmad binary for the current sources, building it if needed."""
    digest = _source_hash(_DAEMON_FILES)[:16]
    out = _cache_dir() / digest / "shmrdmad"
    if out.is_file():
        return out, digest
    cc = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc")
    if not cc:
        raise RuntimeError(
            "shmrdmad is built on the host and needs a C compiler: install gcc"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".shmrdmad.{os.getpid()}")
    r = subprocess.run(
        [cc, *DAEMON_CFLAGS, "-o", str(tmp), "shmrdmad.c"],
        cwd=SRC_DIR / "daemon",
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"building shmrdmad failed:\n{r.stderr.strip()}")
    tmp.replace(out)
    return out, digest


def vm_uses(vm: VMInfo, d: Daemon) -> bool:
    """Whether *vm* is attached to daemon *d*."""
    socks = {str(d.rdma_socket), str(d.net_socket)}
    return any(s in socks for s in vm_sockets(vm, d))


def vm_sockets(vm: VMInfo, d: Daemon | None = None) -> list[str]:
    """The vhost-user sockets *vm* connects to, ltvm's own resolved."""
    own = d or daemon()
    socks = []
    if vm.shmrdma:
        socks.append(
            str(own.rdma_socket) if vm.shmrdma == "auto" else vm.shmrdma
        )
    for nic in vm.nics:
        if nic.type == "shmnet":
            socks.append(nic.socket or str(own.net_socket))
    return socks


def attached_vms(d: Daemon) -> list[str]:
    """Running VMs that use daemon *d*."""
    from .qemu_run import is_running
    from .vm_state import VMInfo, VMNotFound

    names = []
    for name in VMInfo.all_names():
        try:
            vm = VMInfo.load(name)
        except (VMNotFound, ValueError):
            continue
        if vm.shared_ram and vm_uses(vm, d) and is_running(vm):
            names.append(name)
    return names


def ensure_daemon() -> Daemon:
    """Start this user's shmrdmad unless it already runs; return it."""
    d = daemon()
    with open(d.lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if d.pid():
            if d.binary_hash() != _source_hash(_DAEMON_FILES)[:16]:
                log.info(
                    "shmrdmad (pid %d) predates the current sources; it "
                    "keeps running until `ltvm vm shmrdmad stop`",
                    d.pid(),
                )
            return d
        binary, digest = build_daemon()
        for s in (d.rdma_socket, d.net_socket):
            s.unlink(missing_ok=True)
        from .qemu_run import _guest_scope
        from .vm_state import VMInfo

        scope = _guest_scope(VMInfo(name=f"shmrdmad-{d.user}", ip=""))
        extra = os.environ.get("LTVM_SHMRDMAD_ARGS", "").split()
        with open(d.log_path, "a") as logf:
            logf.write(
                f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting {binary}\n"
            )
            logf.flush()
            proc = subprocess.Popen(
                [
                    *scope,
                    str(binary),
                    "-s",
                    str(d.rdma_socket),
                    "-n",
                    str(d.net_socket),
                    *extra,
                ],
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        d.pid_path.write_text(f"{proc.pid} {digest}\n")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            if d.rdma_socket.is_socket() and d.net_socket.is_socket():
                return d
            time.sleep(0.05)
        d.pid_path.unlink(missing_ok=True)
        raise RuntimeError(f"shmrdmad did not come up; see {d.log_path}")


def stop_daemon(force: bool = False) -> str:
    """Stop this user's shmrdmad; refuse while VMs use it unless *force*."""
    d = daemon()
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
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not d.pid():
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
    for p in (d.pid_path, d.rdma_socket, d.net_socket):
        p.unlink(missing_ok=True)
    return f"stopped shmrdmad (pid {pid})"


def status() -> dict[str, object]:
    d = daemon()
    pid = d.pid()
    current = _source_hash(_DAEMON_FILES)[:16]
    return {
        "running": bool(pid),
        "pid": pid,
        "user": d.user,
        "rdma_socket": str(d.rdma_socket),
        "net_socket": str(d.net_socket),
        "log": str(d.log_path),
        "current": (d.binary_hash() == current) if pid else None,
        "vms": attached_vms(d) if pid else [],
    }


def prepare_launch(vm: VMInfo) -> list[str]:
    """Make sure every socket *vm* needs is served; return them.

    ltvm's own daemon is started on demand.  A socket given by hand
    belongs to a daemon run by hand, so it must already be there.
    """
    from .qemu_run import die

    d = daemon()
    needs_own = vm.shmrdma == "auto" or any(
        n.type == "shmnet" and not n.socket for n in vm.nics
    )
    if needs_own:
        try:
            ensure_daemon()
        except RuntimeError as e:
            die(f"{vm.name}: {e}")
    socks = vm_sockets(vm, d)
    for s in socks:
        if not Path(s).is_socket():
            die(f"{vm.name} needs shmrdmad, but nothing listens on {s}")
    return socks
