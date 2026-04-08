"""Data models and constants for QEMU VM management."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# ── constants ────────────────────────────────────────────
# Configurable via environment variables; defaults match the
# standard install layout from `ltvm setup`.

VM_DIR = Path(os.environ.get("LTVM_VM_DIR", "/opt/qemu-vms"))
QEMU_PREFIX = Path(os.environ.get("LTVM_QEMU_PREFIX", "/opt/qemu"))
QEMU = str(QEMU_PREFIX / "bin" / "qemu-system-x86_64")
QEMU_IMG = str(QEMU_PREFIX / "bin" / "qemu-img")


def qemu_binary_for_arch(arch: str = "x86_64") -> str:
    """Return the full path to qemu-system-<arch>."""
    _BINARY_MAP = {"x86_64": "qemu-system-x86_64", "aarch64": "qemu-system-aarch64"}
    name = _BINARY_MAP.get(arch, f"qemu-system-{arch}")
    return str(QEMU_PREFIX / "bin" / name)


def qemu_machine_for_arch(arch: str = "x86_64") -> str:
    """Return the -machine argument for a given arch."""
    _MACHINE_MAP = {
        "x86_64": "microvm,accel=kvm,pit=off,pic=off,rtc=on",
        "aarch64": "virt,accel=kvm,gic-version=max",
    }
    return _MACHINE_MAP.get(arch, _MACHINE_MAP["x86_64"])
DISK_SIZE_BYTES = 8 * 1024 * 1024 * 1024  # 8 GiB
BASE_IMAGE = VM_DIR / "images" / "rocky9-ltvm.ext4"
KERNEL = VM_DIR / "kernel" / "vmlinux"
IMAGES = VM_DIR / "images"
KERNELS = VM_DIR / "kernel"

# ltvm repo root — vm.py is installed to /opt/qemu-vms/qemu/models.py,
# and the repo may also be at the symlink source of /usr/local/bin/ltvm.
_LTVM_ROOT = Path(__file__).resolve().parent.parent
TARGETS_YAML = _LTVM_ROOT / "targets" / "targets.yaml"


@dataclass
class OSArtifacts:
    image: Path
    kernel: Path
    default_mem: int = 2048
    arch: str = "x86_64"


def resolve_os_artifacts(os_name: str, arch: str = "x86_64") -> OSArtifacts:
    """Return image, kernel paths and defaults for a target OS.

    Looks in output/<os>/ in the repo (from fetch or build).
    No separate install step needed.
    """
    # Read targets.yaml for kernel suffix and defaults
    kernel_suffix = ""
    default_mem = 2048
    target_cfg: dict = {}
    if TARGETS_YAML.exists():
        try:
            import yaml
            with open(TARGETS_YAML) as f:
                cfg = yaml.safe_load(f)
            defaults = cfg.get("defaults", {})
            target_cfg = cfg.get("targets", {}).get(os_name, {})
            kernel_suffix = target_cfg.get("kernels", {}).get("default", "")
            default_mem = int(target_cfg.get("default_mem", defaults.get("default_mem", 2048)))
        except Exception:
            pass

    output_dir = _LTVM_ROOT / "output" / os_name

    # Find image in output/<os>/image/
    img = output_dir / "image" / "base.ext4"
    if not img.exists():
        img = None
    if not img:
        raise FileNotFoundError(
            f"No image for '{os_name}'\n"
            f"Run: ltvm fetch {os_name}  (or: ltvm build-image {os_name})"
        )

    # Find kernel: check output dir first, then install dir
    # Prefer vmlinuz (works without PVH) over vmlinux
    kern = None

    # Search output/<os>/kernels/<suffix>/
    if kernel_suffix:
        kdir = output_dir / "kernels" / kernel_suffix
        if not kdir.is_dir():
            # Try glob for versioned subdirs
            matches = sorted(output_dir.glob(f"kernels/{kernel_suffix}*"))
            if matches:
                kdir = matches[0]
        for name in ("vmlinuz", "vmlinux"):
            c = kdir / name
            if c.exists():
                kern = c
                break

    # Search output/<os>/kernels/*/ (any kernel)
    if not kern and output_dir.is_dir():
        for kdir in sorted(output_dir.glob("kernels/*/vmlinuz")):
            kern = kdir
            break
        if not kern:
            for kdir in sorted(output_dir.glob("kernels/*/vmlinux")):
                kern = kdir
                break

    if not kern:
        raise FileNotFoundError(
            f"No kernel for '{os_name}'\n"
            f"Run: ltvm fetch {os_name}  (or: ltvm build-kernel {os_name})"
        )

    target_arch = target_cfg.get("arch", arch)
    return OSArtifacts(image=img, kernel=kern, default_mem=default_mem, arch=target_arch)
OVERLAYS = VM_DIR / "overlays"
SOCKETS = VM_DIR / "sockets"
BRIDGE = "fcbr0"
SUBNET = "192.168.100"
GATEWAY = f"{SUBNET}.1"
MARKER = "# qemu-vm"
ROOT_PASSWORD = "initial0"
SSH_TIMEOUT = 30

DEPLOY_SCRIPT = VM_DIR / "deploy-lustre.sh"

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
    image: str = ""  # base image path; empty = default (rocky9)
    kernel: str = ""  # kernel path; empty = default (vmlinux)
    created: int = 0  # epoch seconds when VM was created
    last_boot: int = 0  # epoch seconds when QEMU was last started
    last_deploy: int = 0  # epoch seconds when deploy-lustre.sh last ran
    build_path: str = ""  # Lustre build tree last deployed
    kver: str = ""  # kernel version running in the VM
    base_image: str = ""  # base image name (e.g. rocky9-base.ext4)
    os_id: str = ""  # OS identifier (e.g. rocky9, ubuntu24)
    arch: str = "x86_64"  # CPU architecture (x86_64, aarch64)

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
        self.info_path.write_text(
            f"NAME={self.name}\n"
            f"IP={self.ip}\n"
            f"PID={self.pid}\n"
            f"TAP={self.tap}\n"
            f"MAC={self.mac}\n"
            f"VCPUS={self.vcpus}\n"
            f"MEM={self.mem}\n"
            f"MDT_DISKS={self.mdt_disks}\n"
            f"OST_DISKS={self.ost_disks}\n"
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
        )

    def _update_field(self, key: str, value: str | int) -> None:
        """Update a single field in the info file (add if missing)."""
        if not self.info_path.exists():
            return
        text = self.info_path.read_text()
        pattern = rf"^{key}=.*$"
        replacement = f"{key}={value}"
        if re.search(pattern, text, flags=re.MULTILINE):
            text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
        else:
            text = text.rstrip("\n") + f"\n{replacement}\n"
        self.info_path.write_text(text)

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
        self._update_field("LAST_DEPLOY", epoch)
        self._update_field("BUILD_PATH", build_path)
        self._update_field("KVER", kver)

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
        return VMInfo(
            name=vals.get("NAME", name),
            ip=vals.get("IP", ""),
            pid=int(vals.get("PID", 0)),
            tap=vals.get("TAP", ""),
            mac=vals.get("MAC", ""),
            vcpus=int(vals.get("VCPUS", 2)),
            mem=int(vals.get("MEM", 2048)),
            mdt_disks=int(vals.get("MDT_DISKS", 0)),
            ost_disks=int(vals.get("OST_DISKS", 0)),
            image=vals.get("IMAGE", ""),
            kernel=vals.get("KERNEL", ""),
            created=int(vals.get("CREATED", 0)),
            last_boot=int(vals.get("LAST_BOOT", 0)),
            last_deploy=int(vals.get("LAST_DEPLOY", 0)),
            build_path=vals.get("BUILD_PATH", ""),
            kver=vals.get("KVER", ""),
            base_image=vals.get("BASE_IMAGE", ""),
            os_id=vals.get("OS_ID", ""),
            arch=vals.get("ARCH", "x86_64"),
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

    @property
    def total_disks(self) -> int:
        n = self.mdt_disks + self.ost_disks
        if self.is_mgs and not self.is_mds:
            n += 1
        return n


@dataclass
class ClusterInfo:
    name: str
    nodes: list[dict]

    @property
    def path(self) -> Path:
        return SOCKETS / f"{self.name}.cluster"

    def save(self) -> None:
        data = {"name": self.name, "nodes": self.nodes}
        self.path.write_text(json.dumps(data, indent=2) + "\n")

    @staticmethod
    def load(name: str) -> ClusterInfo:
        path = SOCKETS / f"{name}.cluster"
        if not path.exists():
            raise ClusterNotFound(name)
        data = json.loads(path.read_text())
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
        from .process import die

        die("cluster has no MGS node")

    def mds_nodes(self) -> list[ClusterNode]:
        return [n for n in self.get_nodes() if n.is_mds]

    def oss_nodes(self) -> list[ClusterNode]:
        return [n for n in self.get_nodes() if n.is_oss]

    def client_nodes(self) -> list[ClusterNode]:
        return [n for n in self.get_nodes() if n.is_client]
