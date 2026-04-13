"""Data models and constants for QEMU VM management."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# ── constants ────────────────────────────────────────────
# Configurable via environment variables; defaults match the
# standard install layout from `ltvm setup`.

VM_DIR = Path(os.environ.get("LTVM_VM_DIR", "/opt/qemu-vms"))
QEMU_PREFIX = Path(os.environ.get("LTVM_QEMU_PREFIX", "/opt/qemu"))
# x86_64 qemu binary path -- non-x86_64 callers use qemu_binary_for_arch().
QEMU_IMG = str(QEMU_PREFIX / "bin" / "qemu-img")


def qemu_binary_for_arch(arch: str = "x86_64") -> str:
    """Return the full path to qemu-system-<arch>.

    x86_64 uses our custom-built binary under QEMU_PREFIX.
    Other arches fall back to the system binary (e.g. from qemu-system-arm
    package) since we only ship x86_64 in the release tarball.
    """
    import shutil

    name = f"qemu-system-{arch}"
    if arch == "x86_64":
        return str(QEMU_PREFIX / "bin" / name)
    # For non-x86_64, prefer QEMU_PREFIX if it has the binary, else system PATH
    candidate = QEMU_PREFIX / "bin" / name
    if candidate.exists():
        return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    return str(candidate)  # will fail with a clear FileNotFoundError


def qemu_machine_for_arch(arch: str = "x86_64") -> str:
    """Return the -machine argument for a given arch.

    Uses KVM only when the host arch matches (KVM can't accelerate a
    different ISA).  Cross-arch VMs fall back to TCG emulation.
    """
    import platform

    host_arch = platform.machine()
    # Normalise: aarch64 == arm64, x86_64 == amd64
    host_is_x86 = host_arch in ("x86_64", "amd64")
    host_is_arm64 = host_arch in ("aarch64", "arm64")

    if arch == "x86_64":
        accel = "accel=kvm" if host_is_x86 else "accel=tcg"
        return f"microvm,{accel},pit=off,pic=off,rtc=on"
    if arch == "aarch64":
        accel = "accel=kvm" if host_is_arm64 else "accel=tcg"
        return f"virt,{accel},gic-version=max"
    return "virt,accel=tcg"


DISK_SIZE_BYTES = 500 * 1024 * 1024  # 500 MiB default

# ltvm repo root -- single source of truth in paths.py so target_config
# (build side) and vm_state (runtime side) cannot drift.  Imported here
# rather than at the top of the file because vm_state.py is a hub other
# modules import from; isolating the helper import prevents an
# initialization cycle (paths.py -> vm_state.py -> paths.py).  E402 is
# suppressed deliberately for that reason.
from .paths import find_ltvm_root  # noqa: E402

_LTVM_ROOT = find_ltvm_root()
TARGETS_YAML = _LTVM_ROOT / "targets" / "targets.yaml"


@dataclass
class OSArtifacts:
    image: Path
    kernel: Path
    default_mem: int = 2048
    arch: str = "x86_64"


def resolve_os_artifacts(
    os_name: str,
    arch: str = "x86_64",
    kernel: str | None = None,
) -> OSArtifacts:
    """Return image, kernel paths and defaults for a target OS.

    Looks in output/<os>/ in the repo (from fetch or build).
    No separate install step needed.

    ``kernel`` may be:
      - None: use the target's default kernel and its paired image.
      - a path to a kernel file (vmlinuz/vmlinux): the basename of its
        parent directory identifies the kernel, and we pair it with
        output/<os>/images/<that-name>/base.ext4.
      - a kernel name (short or full, e.g. ``5.14-rhel9.7`` or
        ``5.14-rhel9.7-5.14.0-611.13.1.el9_7_lustre``): resolved to the
        matching kernel dir under output/<os>/kernels/.
    """
    # Read targets.yaml for kernel suffix and defaults
    kernel_suffix = ""
    default_mem = 2048
    target_cfg: dict = {}
    if TARGETS_YAML.exists():
        import yaml

        with open(TARGETS_YAML) as f:
            cfg = yaml.safe_load(f)
        defaults = cfg.get("defaults", {})
        target_cfg = cfg.get("targets", {}).get(os_name, {})
        kernel_suffix = target_cfg.get("kernels", {}).get("default", "")
        default_mem = int(
            target_cfg.get("default_mem", defaults.get("default_mem", 2048))
        )

    # Determine effective arch (CLI override > target config > default)
    effective_arch = arch or target_cfg.get("arch") or "x86_64"
    default_arch = "x86_64"

    # Output directory: arch-qualified subdir when non-default arch
    output_dir = _LTVM_ROOT / "output" / os_name
    if effective_arch != default_arch:
        arch_dir = output_dir / effective_arch
        if arch_dir.is_dir():
            output_dir = arch_dir

    arch_hint = (
        f" --arch {effective_arch}" if effective_arch != default_arch else ""
    )

    # ── Step 1: Resolve the kernel directory name we should use. ──
    #
    # --kernel accepts either a path (e.g. a literal vmlinuz) or a name
    # (short or full lustre-target).  Paths win by existence check: if
    # the value names a real file, trust it verbatim and derive the
    # kernel-name from the parent directory.  Otherwise treat it as a
    # name and resolve it against the kernels dir.
    kern: Path | None = None
    kernel_dirname: str | None = None
    kernels_root = output_dir / "kernels"

    if kernel:
        maybe_path = Path(kernel)
        if maybe_path.is_file():
            kern = maybe_path
            kernel_dirname = maybe_path.parent.name
        else:
            # Treat as a name.  Exact match first, then prefix match.
            cand = kernels_root / kernel
            if cand.is_dir():
                kernel_dirname = kernel
            else:
                prefix = kernel + "-"
                matches = sorted(
                    d.name
                    for d in kernels_root.iterdir()
                    if kernels_root.is_dir()
                    and d.is_dir()
                    and d.name.startswith(prefix)
                ) if kernels_root.is_dir() else []
                if matches:
                    kernel_dirname = matches[-1]
                else:
                    raise FileNotFoundError(
                        f"No kernel matching {kernel!r} for '{os_name}' "
                        f"(arch={effective_arch})\n"
                        f"Run: ltvm build-kernel {os_name} "
                        f"--kernel {kernel}{arch_hint}  "
                        f"(or: ltvm fetch {os_name}{arch_hint})"
                    )
    else:
        # No override: use default kernel suffix, falling back to
        # lex-latest built kernel dir if the default is not present.
        if kernel_suffix:
            cand = kernels_root / kernel_suffix
            if cand.is_dir():
                kernel_dirname = kernel_suffix
            else:
                prefix = kernel_suffix + "-"
                matches = sorted(
                    d.name
                    for d in kernels_root.iterdir()
                    if kernels_root.is_dir()
                    and d.is_dir()
                    and d.name.startswith(prefix)
                ) if kernels_root.is_dir() else []
                if matches:
                    kernel_dirname = matches[-1]
        if kernel_dirname is None and kernels_root.is_dir():
            any_built = sorted(
                d.name for d in kernels_root.iterdir() if d.is_dir()
            )
            if any_built:
                kernel_dirname = any_built[-1]

    if kernel_dirname is None:
        raise FileNotFoundError(
            f"No kernel for '{os_name}' (arch={effective_arch})\n"
            f"Run: ltvm fetch {os_name}{arch_hint}  "
            f"(or: ltvm build-kernel {os_name}{arch_hint})"
        )

    # Pick the actual kernel binary file if the caller didn't pass a path.
    if kern is None:
        kdir = kernels_root / kernel_dirname
        for nm in ("vmlinuz", "vmlinux"):
            c = kdir / nm
            if c.exists():
                kern = c
                break
        if kern is None:
            raise FileNotFoundError(
                f"No vmlinuz/vmlinux in {kdir}\n"
                f"Run: ltvm build-kernel {os_name} "
                f"--kernel {kernel_dirname}{arch_hint}"
            )

    # ── Step 2: Locate the image paired with this kernel. ──
    # New layout: output/<os>[/<arch>]/images/<kernel-dirname>/base.ext4
    img = output_dir / "images" / kernel_dirname / "base.ext4"
    if not img.exists():
        raise FileNotFoundError(
            f"No image for '{os_name}' kernel={kernel_dirname} "
            f"(arch={effective_arch})\n"
            f"Run: ltvm build-image {os_name} "
            f"--kernel {kernel_dirname}{arch_hint}  "
            f"(or: ltvm fetch {os_name}{arch_hint})"
        )

    return OSArtifacts(
        image=img, kernel=kern, default_mem=default_mem, arch=effective_arch
    )


OVERLAYS = VM_DIR / "overlays"
SOCKETS = VM_DIR / "sockets"
BRIDGE = "fcbr0"


def _read_subnet() -> str:
    """Return the persisted subnet, or the default.

    `ltvm setup --subnet X` writes the chosen subnet to VM_DIR/subnet
    so that vm_net.alloc_ip() (which runs in a separate process from
    setup) sees the same value as the host bridge config.  Without this
    file, --subnet would only configure the host side and VMs would
    silently get IPs from the wrong range.
    """
    env = os.environ.get("LTVM_SUBNET")
    if env:
        return env
    f = VM_DIR / "subnet"
    if f.is_file():
        v = f.read_text().strip()
        if v:
            return v
    return "192.168.100"


SUBNET = _read_subnet()
GATEWAY = f"{SUBNET}.1"
MARKER = "# qemu-vm"
ROOT_PASSWORD = "initial0"
SSH_TIMEOUT = 30
DEFAULT_TARGET = "rocky9"


def lustre_libdir(os_family: str = "rhel") -> str:
    """Return the on-VM Lustre library directory for the given OS family.

    rhel uses /usr/lib64; debian uses /usr/lib.  This is the single
    source of truth for that path; deploy.py and vm_cluster.py both
    derive everything else from it.
    """
    return "/usr/lib/lustre" if os_family == "debian" else "/usr/lib64/lustre"


# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_FOUND = 2
EXIT_TIMEOUT = 3
EXIT_UNREACHABLE = 4


# ── VM data ──────────────────────────────────────────────


@dataclass
class VMInfo:
    name: str
    ip: str
    pid: int = 0
    tap: str = ""
    mac: str = ""
    vcpus: int = 2
    mem: int = 2048
    mdt_disks: int = 0
    ost_disks: int = 0
    disk_size: int = DISK_SIZE_BYTES  # per-disk size in bytes
    image: str = ""  # base image path; empty = default (rocky9)
    kernel: str = ""  # kernel path; empty = default (vmlinux)
    created: int = 0  # epoch seconds when VM was created
    last_boot: int = 0  # epoch seconds when QEMU was last started
    last_deploy: int = 0  # epoch seconds when last deploy ran
    build_path: str = ""  # Lustre build tree last deployed
    kver: str = ""  # kernel version running in the VM
    base_image: str = ""  # base image name (e.g. rocky9-base.ext4)
    os_id: str = ""  # OS identifier (e.g. rocky9, ubuntu24)
    arch: str = "x86_64"  # CPU architecture (x86_64, aarch64)
    creator: str = ""  # username that created the VM (SUDO_USER, or "" for legacy)

    @property
    def info_path(self) -> Path:
        return SOCKETS / f"{self.name}.info"

    @property
    def pid_path(self) -> Path:
        return SOCKETS / f"{self.name}.pid"

    @property
    def log_path(self) -> Path:
        return SOCKETS / f"{self.name}.log"

    @property
    def socket_path(self) -> Path:
        return SOCKETS / f"{self.name}.qmp"

    @property
    def overlay_path(self) -> Path:
        return OVERLAYS / f"{self.name}.qcow2"

    def disk_path(self, n: int) -> Path:
        return OVERLAYS / f"{self.name}-disk{n}.img"

    def save(self) -> None:
        # Atomic write via tempfile + rename so a SIGKILL mid-save
        # cannot leave a half-written .info file (which would parse
        # back as a VM with empty IP/PID/etc).
        text = (
            f"NAME={self.name}\n"
            f"IP={self.ip}\n"
            f"PID={self.pid}\n"
            f"TAP={self.tap}\n"
            f"MAC={self.mac}\n"
            f"VCPUS={self.vcpus}\n"
            f"MEM={self.mem}\n"
            f"MDT_DISKS={self.mdt_disks}\n"
            f"OST_DISKS={self.ost_disks}\n"
            f"DISK_SIZE={self.disk_size}\n"
            f"IMAGE={self.image}\n"
            f"KERNEL={self.kernel}\n"
            f"CREATED={self.created}\n"
            f"LAST_BOOT={self.last_boot}\n"
            f"LAST_DEPLOY={self.last_deploy}\n"
            f"BUILD_PATH={self.build_path}\n"
            f"KVER={self.kver}\n"
            f"BASE_IMAGE={self.base_image}\n"
            f"OS_ID={self.os_id}\n"
            f"ARCH={self.arch}\n"
            f"CREATOR={self.creator}\n"
        )
        self.info_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(
            dir=str(self.info_path.parent),
            prefix=f".{self.info_path.name}.",
        )
        tmp = Path(tmp_str)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            tmp.rename(self.info_path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    @property
    def _lock_path(self) -> Path:
        return SOCKETS / f".{self.name}.info.lock"

    @contextmanager
    def _info_lock(self) -> Iterator[None]:
        """Per-VM exclusive lock for read-modify-write of the .info file.

        Two processes can call e.g. update_pid() and update_deploy() concurrently
        on the same VM (cluster deploy + a manual `ltvm start`).  Without this
        lock, both read the same text, both rename, the second write wins and
        the first update is silently lost.
        """
        SOCKETS.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _update_fields(self, fields: dict) -> None:
        """Update multiple fields in the info file atomically (single write).

        Held under a per-VM flock so concurrent updaters can't lose writes.
        Raises VMNotFound if the .info file is gone (e.g. concurrent
        destroy or partially rolled-back create) -- the previous silent
        no-op left in-memory VMInfo state diverged from disk with no
        signal to the caller.
        """
        with self._info_lock():
            if not self.info_path.exists():
                raise VMNotFound(self.name)
            text = self.info_path.read_text()
            for key, value in fields.items():
                pattern = rf"^{key}=.*$"
                replacement = f"{key}={value}"
                if re.search(pattern, text, flags=re.MULTILINE):
                    text = re.sub(
                        pattern, replacement, text, flags=re.MULTILINE
                    )
                else:
                    text = text.rstrip("\n") + f"\n{replacement}\n"
            fd, tmp_str = tempfile.mkstemp(
                dir=str(self.info_path.parent),
                prefix=f".{self.info_path.name}.",
            )
            tmp = Path(tmp_str)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(text)
                tmp.rename(self.info_path)
            except BaseException:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise

    def _update_field(self, key: str, value: str | int) -> None:
        """Update a single field in the info file (add if missing)."""
        self._update_fields({key: value})

    def update_pid(self, pid: int) -> None:
        self.pid = pid
        self._update_field("PID", pid)

    def update_last_boot(self, epoch: int) -> None:
        self.last_boot = epoch
        self._update_field("LAST_BOOT", epoch)

    def update_deploy(self, epoch: int, build_path: str, kver: str) -> None:
        self.last_deploy = epoch
        self.build_path = build_path
        self.kver = kver
        self._update_fields(
            {"LAST_DEPLOY": epoch, "BUILD_PATH": build_path, "KVER": kver}
        )

    @staticmethod
    def load(name: str) -> VMInfo:
        path = SOCKETS / f"{name}.info"
        if not path.exists():
            raise VMNotFound(name)
        vals = {}
        for line in path.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                vals[k] = v

        def _int(key: str, default: int) -> int:
            """Tolerate truncated/empty fields in a hand-edited or
            partially-written .info file: fall back to the default
            instead of raising ValueError."""
            try:
                return int(vals.get(key, default))
            except (ValueError, TypeError):
                return default

        return VMInfo(
            name=vals.get("NAME", name),
            ip=vals.get("IP", ""),
            pid=_int("PID", 0),
            tap=vals.get("TAP", ""),
            mac=vals.get("MAC", ""),
            vcpus=_int("VCPUS", 2),
            mem=_int("MEM", 2048),
            mdt_disks=_int("MDT_DISKS", 0),
            ost_disks=_int("OST_DISKS", 0),
            disk_size=_int("DISK_SIZE", DISK_SIZE_BYTES),
            image=vals.get("IMAGE", ""),
            kernel=vals.get("KERNEL", ""),
            created=_int("CREATED", 0),
            last_boot=_int("LAST_BOOT", 0),
            last_deploy=_int("LAST_DEPLOY", 0),
            build_path=vals.get("BUILD_PATH", ""),
            kver=vals.get("KVER", ""),
            base_image=vals.get("BASE_IMAGE", ""),
            os_id=vals.get("OS_ID", ""),
            arch=vals.get("ARCH", "x86_64"),
            creator=vals.get("CREATOR", ""),
        )

    @staticmethod
    def all_names() -> list[str]:
        return [f.stem for f in sorted(SOCKETS.glob("*.info"))]


class VMNotFound(Exception):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"VM '{name}' not found")


class ClusterNotFound(Exception):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"cluster '{name}' not found")


# ── Cluster data ─────────────────────────────────────────


@dataclass
class ClusterNode:
    name: str
    roles: list[str]
    mdt_disks: int = 0
    ost_disks: int = 0
    ip: str = ""

    @property
    def is_mgs(self) -> bool:
        return "mgs" in self.roles

    @property
    def is_mds(self) -> bool:
        return "mds" in self.roles

    @property
    def is_oss(self) -> bool:
        return "oss" in self.roles

    @property
    def is_client(self) -> bool:
        return "client" in self.roles


@dataclass
class ClusterInfo:
    name: str
    nodes: list[dict]

    @property
    def path(self) -> Path:
        return SOCKETS / f"{self.name}.cluster"

    def save(self) -> None:
        # Atomic write via tempfile + rename so a SIGKILL or disk-full
        # mid-write cannot leave a half-written .cluster file (which
        # would fail JSON parse on the next load).
        data = {"name": self.name, "nodes": self.nodes}
        text = json.dumps(data, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=f".{self.path.name}.",
        )
        tmp = Path(tmp_str)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            tmp.rename(self.path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    @staticmethod
    def load(name: str) -> ClusterInfo:
        path = SOCKETS / f"{name}.cluster"
        if not path.exists():
            raise ClusterNotFound(name)
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            # Corrupt cluster state file -- surface a clean error instead
            # of a raw JSONDecodeError traceback so the user can see what
            # to do (delete or restore the file).
            raise RuntimeError(
                f"corrupt cluster state at {path}: {e}\n"
                f"  remove the file and recreate the cluster with `ltvm cluster create`"
            )
        return ClusterInfo(name=data["name"], nodes=data["nodes"])

    @staticmethod
    def all_names() -> list[str]:
        return [f.stem for f in sorted(SOCKETS.glob("*.cluster"))]

    def get_nodes(self) -> list[ClusterNode]:
        return [ClusterNode(**n) for n in self.nodes]

    def mgs_node(self) -> ClusterNode:
        for n in self.get_nodes():
            if n.is_mgs:
                return n
        raise RuntimeError(f"cluster {self.name!r} has no MGS node")

    def mds_nodes(self) -> list[ClusterNode]:
        return [n for n in self.get_nodes() if n.is_mds]

    def oss_nodes(self) -> list[ClusterNode]:
        return [n for n in self.get_nodes() if n.is_oss]

    def client_nodes(self) -> list[ClusterNode]:
        return [n for n in self.get_nodes() if n.is_client]
