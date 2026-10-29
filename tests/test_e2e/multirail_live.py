"""Bounded live-test operations, restricted to two newly created VMs.

Guest CLI commands exercise the actual Linux and Lustre tools shipped in the
image. Host measurements use files and QMP directly. No host network or global
VM cleanup command is used. Logs belong to pytest's temporary evidence folder.
"""

from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.multirail_metrics import (
    Snapshot,
    canonical_nid,
    parse_snapshot,
    peer_rails_healthy,
    read_yaml,
)

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = "LTVM_REQUIRE_MULTIRAIL_E2E"
TRANSPORT_MODULES = {"tcp": "ksocklnd", "softroce": "ko2iblnd"}
TRANSPORT_TIMEOUTS = {"tcp": "sock_timeout", "softroce": "timeout"}


def _partial_text(value: str | bytes | None) -> str:
    """Retain bounded command output when a timeout interrupts collection."""
    if isinstance(value, bytes):
        # TimeoutExpired may hold bytes even when the subprocess requested text.
        return value[-65536:].decode(errors="replace")
    return (value or "")[-65536:]


def unavailable(reason: str) -> None:
    """Make unavailable prerequisites visible, and fail a required live gate."""
    if os.environ.get(REQUIRED) == "1":
        pytest.fail(
            f"required Multi-Rail live environment unavailable: {reason}"
        )
    pytest.skip(f"Multi-Rail live environment unavailable: {reason}")


def qualify_host() -> tuple[str, str, Path]:
    """Resolve existing artifacts without downloading, installing, or building."""
    if platform.system() != "Linux" or os.geteuid() != 0:
        unavailable("requires Linux root and /dev/kvm")
    try:
        with Path("/dev/kvm").open("rb", buffering=0) as device:
            if fcntl.ioctl(device.fileno(), 0xAE00, 0) != 12:
                unavailable("unsupported KVM API version")
    except OSError as error:
        unavailable(f"cannot access /dev/kvm: {error}")
    for tool in ("ssh", "sshpass", "ip"):
        if shutil.which(tool) is None:
            unavailable(f"missing host tool {tool}")
    from ltvm_pkg.vm_state import qemu_binary_for_arch, resolve_os_artifacts

    target = os.environ.get("LTVM_MULTIRAIL_TARGET", "rocky9")
    architecture = {"arm64": "aarch64", "amd64": "x86_64"}.get(
        platform.machine(), platform.machine()
    )
    if not Path(qemu_binary_for_arch(architecture)).is_file():
        unavailable(f"QEMU for {architecture} is not installed")
    prefix = Path(os.environ.get("LTVM_QEMU_PREFIX", "/opt/qemu"))
    if not (prefix / "bin/qemu-img").is_file():
        unavailable("qemu-img is not installed in LTVM_QEMU_PREFIX/bin")
    try:
        assets = resolve_os_artifacts(
            target,
            arch=architecture,
            kernel=os.environ.get("LTVM_MULTIRAIL_KERNEL"),
        )
    except (FileNotFoundError, ValueError) as error:
        unavailable(str(error))
    if not assets.image.is_file() or not assets.kernel.is_file():
        unavailable("target image or kernel is missing")
    return target, assets.kernel.parent.name, assets.image


@dataclass(frozen=True)
class Guest:
    """Validated state of one VM owned by this live case."""

    name: str
    ips: tuple[str, str]
    ip6s: tuple[str, str]
    nids: tuple[str, str]
    taps: tuple[str, str]
    topology: dict[str, Any]


@dataclass
class Lab:
    """Own all commands, faults, and evidence for one mode/family case."""

    mode: str
    family: str
    evidence: Path
    # Test failure summaries must not expose the controller's environment.
    env: dict[str, str] = field(repr=False)
    transport: str = "tcp"
    guests: list[Guest] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    command_count: int = 0

    @property
    def sockets(self) -> Path:
        """Read state from the CLI's directory, not pytest's mocked model."""
        return Path(self.env.get("LTVM_VM_DIR", "/opt/qemu-vms")) / "sockets"

    def cli(
        self, *arguments: str, timeout: int = 60, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Run this checkout with this interpreter and save bounded evidence."""
        command = [sys.executable, str(ROOT / "ltvm"), *arguments]
        self.command_count += 1
        log = self.evidence / f"{self.command_count:04d}.txt"
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            # Preserve which bounded command failed before teardown continues.
            partial = ""
            if isinstance(error, subprocess.TimeoutExpired):
                # Earlier diagnostic output must survive a later blocked read.
                partial = (
                    f"\npartial stdout:\n{_partial_text(error.stdout)}"
                    f"\npartial stderr:\n{_partial_text(error.stderr)}"
                )
            log.write_text(f"{shlex.join(command)}\n{error}\n{partial}")
            raise
        log.write_text(
            f"{shlex.join(command)}\nrc={result.returncode}\n{result.stdout[-65536:]}\n{result.stderr[-65536:]}"
        )
        if check and result.returncode != 0:
            pytest.fail(
                f"live command failed; evidence {log}:\n"
                f"{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
            )
        return result

    def run(
        self,
        guest: Guest,
        command: str,
        *,
        timeout: int = 30,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Use the VM command boundary; never resolve a stale installed ltvm."""
        assert guest in self.guests, "guest must be owned by this case"
        return self.cli(
            "vm",
            "exec",
            "--timeout",
            str(timeout),
            guest.name,
            command,
            timeout=timeout + 10,
            check=check,
        )

    def create(
        self, target: str, kernel: str, image: Path, *, server: bool
    ) -> Guest:
        """Create one new two-rail VM; register its name before any mutation."""
        name = f"e2e-mr-{uuid.uuid4().hex[:12]}"
        self.names.append(name)
        self.cli(
            "create",
            name,
            target,
            "--kernel",
            kernel,
            "--image",
            str(image),
            "--mem",
            "4096",
            "--vcpus",
            "2",
            "--mdt-disks",
            "1" if server else "0",
            "--ost-disks",
            "1" if server else "0",
            "--disk-size",
            "2G",
            "--nic",
            self.transport,
            "--nic",
            self.transport,
            "-T",
            self.mode,
            timeout=300,
        )
        guest = self.read_guest(name)
        self.guests.append(guest)
        return guest

    def read_guest(self, name: str) -> Guest:
        """Validate the persisted address and attachment contract after create."""
        assert name in self.names, "state must belong to a registered test VM"
        values = dict(
            line.split("=", 1)
            for line in (self.sockets / f"{name}.info").read_text().splitlines()
            if "=" in line
        )
        assert values["NAME"] == name
        assert values["NICS"] == "|".join([self.transport] * 2)
        topology = json.loads(values["RAIL_TOPOLOGY"])
        assert topology["mode"] == self.mode and len(topology["rails"]) == 2
        ips = tuple(
            str(ipaddress.IPv4Address(ip))
            for ip in values["NIC_IPS"].split("|")
        )
        ip6s = tuple(
            str(ipaddress.IPv6Address(ip))
            for ip in values["NIC_IP6S"].split("|")
        )
        assert len(ips) == len(ip6s) == 2
        addresses = ip6s if self.family == "ipv6" else ips
        nids = tuple(
            f"{address}@{rail['lnet_network']}"
            for address, rail in zip(addresses, topology["rails"])
        )
        from ltvm_pkg.vm_net import extra_tap_for_name

        return Guest(
            name,
            ips,
            ip6s,
            nids,
            tuple(extra_tap_for_name(name, i) for i in (1, 2)),
            topology,
        )

    def configure(self, guest: Guest) -> None:
        """Load only the generated interfaces, choosing one address family."""
        probe = self.run(
            guest,
            "for t in lnetctl lctl mkfs.lustre timeout dd cmp ping ip modinfo; do "
            'command -v "$t" || exit 69; done; '
            "modinfo lnet >/dev/null || exit 69; "
            "modinfo lustre >/dev/null || exit 69; "
            f"modinfo {TRANSPORT_MODULES[self.transport]} >/dev/null || exit 69",
            check=False,
        )
        if probe.returncode == 69:
            # Only the guest probe's explicit missing-dependency status qualifies.
            unavailable(
                f"guest image lacks required Lustre tools/modules: {probe.stdout} {probe.stderr}"
            )
        if probe.returncode != 0:
            # CLI, SSH, and other execution errors are failed validation.
            pytest.fail(
                f"guest prerequisite probe failed; evidence {self.evidence}: "
                f"rc={probe.returncode} {probe.stdout} {probe.stderr}"
            )
        large = " --large" if self.family == "ipv6" else ""
        self.configure_transport(guest)
        self.run(guest, f"lnetctl lnet configure --all{large}")
        self.run(
            guest,
            "lnetctl set discovery 1 && lnetctl set health_sensitivity 100 && "
            "lnetctl set retry_count 2 && lnetctl set transaction_timeout 10",
        )
        settings = read_yaml(self.run(guest, "lnetctl global show").stdout)[
            "global"
        ]
        expected = {
            "discovery": 1,
            "health_sensitivity": 100,
            "retry_count": 2,
            "transaction_timeout": 10,
        }
        assert all(
            settings.get(key) == value for key, value in expected.items()
        ), settings
        data = read_yaml(self.run(guest, "lnetctl net show --verbose 3").stdout)
        actual = {
            canonical_nid(ni["nid"])
            for net in data["net"]
            for ni in net.get("local NI(s)", [])
            if not ni["nid"].endswith("@lo")
        }
        assert actual == {canonical_nid(nid) for nid in guest.nids}, data
        for net in data["net"]:
            for ni in net.get("local NI(s)", []):
                timeout = ni.get("lnd tunables", {}).get("timeout")
                if timeout is not None:
                    # Older reports may omit it; the module value is checked directly.
                    assert timeout == 3, ni

    def configure_transport(self, guest: Guest) -> None:
        """Set the test guest's LND timeout before configuration opens connections."""
        module = TRANSPORT_MODULES[self.transport]
        parameter = TRANSPORT_TIMEOUTS[self.transport]
        path = f"/sys/module/{module}/parameters/{parameter}"
        self.run(
            guest,
            f"modprobe lnet && modprobe {module} && "
            f"printf '3\\n' > {path} && "
            f"IFS= read -r transport_timeout < {path} && "
            'test "$transport_timeout" = 3',
        )

    def snapshot(self, guest: Guest) -> Snapshot:
        """Read counters from sysfs and LNet's per-NI report."""
        links = self.run(
            guest,
            "for n in eth0 eth1 eth2; do "
            "read -r rx < /sys/class/net/$n/statistics/rx_bytes; "
            "read -r tx < /sys/class/net/$n/statistics/tx_bytes; "
            'printf \'%s %s %s\\n\' "$n" "$rx" "$tx"; done',
        ).stdout
        networks = self.run(guest, "lnetctl net show --verbose 3").stdout
        return parse_snapshot(links, networks)

    def link(self, guest: Guest, index: int, *, up: bool) -> None:
        """Fault only an owned data NIC through QMP; net0 is never allowed."""
        assert guest in self.guests and index in (1, 2)
        from ltvm_pkg.vm_commands import qmp_execute

        qmp_execute(
            self.sockets / f"{guest.name}.qmp",
            "set_link",
            {"name": f"net{index}", "up": up},
        )

    def management(self) -> None:
        """Require both control paths to work while data rails are faulty."""
        for guest in self.guests:
            self.run(guest, "true", timeout=10)

    def close(self) -> None:
        """Restore links and destroy only this case's VMs, even after failure."""
        from ltvm_pkg.vm_net import tap_for_name

        failures = []
        for guest in self.guests:
            failures.extend(self.restore_for_close(guest))
        for name in reversed(self.names):
            if not (self.sockets / f"{name}.info").exists():
                # Failed creation may already have rolled back all VM state.
                continue
            try:
                result = self.cli("destroy", name, timeout=120, check=False)
            except (OSError, subprocess.SubprocessError) as error:
                failures.append(f"destroy {name}: {error}")
                continue
            if (
                result.returncode != 0
                or (self.sockets / f"{name}.info").exists()
            ):
                failures.append(f"destroy failed for {name}: {result.stderr}")
        for guest in self.guests:
            if any(
                (Path("/sys/class/net") / tap).exists()
                for tap in (tap_for_name(guest.name), *guest.taps)
            ):
                failures.append(f"TAP remains for {guest.name}")
        for name in self.names:
            for suffix in ("info", "pid", "qmp"):
                if (self.sockets / f"{name}.{suffix}").exists():
                    failures.append(f"VM state remains: {name}.{suffix}")
        if failures:
            pytest.fail(
                f"live cleanup failed; evidence {self.evidence}: {failures}"
            )

    def restore_for_close(self, guest: Guest) -> list[str]:
        """Try every data link and control cleanup even if one operation fails."""
        failures = []
        for index in (1, 2):
            try:
                self.link(guest, index, up=True)
            except (OSError, RuntimeError) as error:
                failures.append(f"restore {guest.name}/net{index}: {error}")
        try:
            self.run(
                guest,
                "touch /root/mr-stop; umount /mnt/mr-client 2>/dev/null; "
                "umount /mnt/mr-ost 2>/dev/null; "
                "umount /mnt/mr-mdt 2>/dev/null; true",
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            # Losing guest control must not prevent destroy of the other VM.
            failures.append(f"control cleanup {guest.name}: {error}")
        return failures


def make_lab(mode: str, family: str, evidence: Path) -> Lab:
    """Give child CLI processes one fixed owner and isolated user state."""
    env = os.environ.copy()
    transport = env.get("LTVM_MULTIRAIL_TRANSPORT", "tcp")
    if transport not in TRANSPORT_MODULES:
        # A bad test selection is a configuration failure, not a missing host.
        pytest.fail("LTVM_MULTIRAIL_TRANSPORT must be tcp or softroce")
    env.update(
        {
            "LTVM_ROOT": str(ROOT),
            "LTVM_TELEMETRY": "0",
            "LTVM_OWNER_ID": f"mr-e2e:{uuid.uuid4().hex}",
            "XDG_CONFIG_HOME": str(evidence / "config"),
            "XDG_STATE_HOME": str(evidence / "state"),
            "XDG_CACHE_HOME": str(evidence / "cache"),
        }
    )
    return Lab(mode, family, evidence, env, transport=transport)


def wait_healthy(lab: Lab, guest: Guest, timeout: int = 90) -> None:
    """Require local and remote NI recovery before accepting resumed balancing."""
    deadline = time.monotonic() + timeout
    remote = next(other for other in lab.guests if other != guest)
    for _ in range(timeout):
        local_healthy = all(
            value == 1000 for value in lab.snapshot(guest).health.values()
        )
        peers = lab.run(guest, "lnetctl peer show --verbose 3").stdout
        if local_healthy and peer_rails_healthy(peers, remote.nids):
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
    pytest.fail(
        f"rails did not recover within {timeout}s; evidence {lab.evidence}"
    )
