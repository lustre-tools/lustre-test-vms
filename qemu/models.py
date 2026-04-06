"""Data models and constants for QEMU VM management."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# ── constants ────────────────────────────────────────────

VM_DIR = Path("/opt/qemu-vms")
QEMU = "/opt/qemu/bin/qemu-system-x86_64"
DISK_SIZE_BYTES = 8 * 1024 * 1024 * 1024  # 8 GiB
BASE_IMAGE = Path("/opt/qemu-vms/images/rocky9-base.ext4")
KERNEL = VM_DIR / "kernel" / "vmlinux"
OVERLAYS = VM_DIR / "overlays"
SOCKETS = VM_DIR / "sockets"
BRIDGE = "fcbr0"
SUBNET = "192.168.100"
GATEWAY = f"{SUBNET}.1"
MARKER = "# qemu-vm"

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
        )

    def update_pid(self, pid: int) -> None:
        self.pid = pid
        if self.info_path.exists():
            text = self.info_path.read_text()
            text = re.sub(
                r"^PID=.*$",
                f"PID={pid}",
                text,
                flags=re.MULTILINE,
            )
            self.info_path.write_text(text)

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
        )

    @staticmethod
    def all_names() -> list[str]:
        return [f.stem for f in sorted(SOCKETS.glob("*.info"))]


class VMNotFound(Exception):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"VM '{name}' not found")


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
            from .process import die

            die(f"cluster '{name}' not found", EXIT_NOT_FOUND)
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
        raise AssertionError("unreachable")

    def mds_nodes(self) -> list[ClusterNode]:
        return [n for n in self.get_nodes() if n.is_mds]

    def oss_nodes(self) -> list[ClusterNode]:
        return [n for n in self.get_nodes() if n.is_oss]

    def client_nodes(self) -> list[ClusterNode]:
        return [n for n in self.get_nodes() if n.is_client]
