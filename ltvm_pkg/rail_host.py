"""Create only ltvm's fixed, unnumbered Linux data bridges through rtnetlink.

Management stays on fcbr0. Data bridges have no host addresses, DHCP,
NAT, or routes. Installation owns their lifetime; VM stop removes TAPs.
"""

from __future__ import annotations

import os
import platform
import pwd
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .rail_topology import RailTopology, configured_rails
from .vm_state import BRIDGE, VMInfo

SYS_NET = Path("/sys/class/net")
PROC_SYS = Path("/proc/sys")
OWNER = "ltvm:data-bridge:v1"
# SoftRoCE guests use a 4200-byte Ethernet MTU for a 4096-byte RXE payload.
DATA_MTU = 4200


def bridge_names() -> tuple[str, ...]:
    """Return the fixed allowlist used by setup and the QEMU bridge helper."""
    return tuple(rail.bridge for rail in configured_rails())


@contextmanager
def _iproute() -> Iterator[Any]:
    """Normalize expected kernel failures after the caller's rollback completes."""
    if platform.system() != "Linux":
        raise RuntimeError("explicit rail topologies require a Linux host")
    try:
        from pyroute2 import IPRoute
        from pyroute2.netlink.exceptions import NetlinkError
    except ImportError as error:
        raise RuntimeError(
            "rail bridge setup needs pyroute2; run uv sync"
        ) from error
    try:
        with IPRoute() as ipr:
            yield ipr
    except (NetlinkError, OSError) as error:
        # Kernel permissions, missing devices, and procfs errors are checked failures.
        raise RuntimeError(f"rail network API failed: {error}") from error


def _owned_link(ipr: Any, name: str) -> Any | None:
    """Find a managed bridge or reject a device with the same reserved name."""
    indices = ipr.link_lookup(ifname=name)
    if not indices:
        return None
    if len(indices) != 1:
        raise RuntimeError(f"ambiguous interface name: {name}")
    links = ipr.get_links(indices[0])
    if len(links) != 1:
        raise RuntimeError(f"interface {name} disappeared during inspection")
    link = links[0]
    info = link.get_attr("IFLA_LINKINFO")
    if (
        info is None
        or info.get_attr("IFLA_INFO_KIND") != "bridge"
        or link.get_attr("IFLA_IFALIAS") != OWNER
    ):
        # A familiar name is not evidence that ltvm owns the device.
        raise RuntimeError(f"refusing unmanaged interface {name}")
    return link


def _check_unnumbered(ipr: Any, index: int, name: str) -> None:
    """Refuse host addresses that could turn an isolated fabric into a router."""
    if ipr.get_addr(index=index):
        raise RuntimeError(
            f"data bridge {name} has host addresses; remove them first"
        )


def _checked_mtu(value: object, name: str, minimum: int = 1) -> int:
    """Reject missing or invalid kernel MTUs instead of assuming a safe path."""
    if type(value) is not int or not 0 < value <= 0xFFFFFFFF:
        raise RuntimeError(f"interface {name} has an invalid MTU")
    if value < minimum:
        # Small host ports drop full-size guest frames even when ping succeeds.
        raise RuntimeError(
            f"interface {name} MTU {value} is below required {minimum}; "
            "run ltvm install --network"
        )
    return value


def _link_mtu(ipr: Any, index: int, name: str, minimum: int = 1) -> int:
    """Read one live interface MTU and handle removal during inspection."""
    links = ipr.get_links(index)
    if len(links) != 1:
        raise RuntimeError(f"interface {name} disappeared during inspection")
    return _checked_mtu(links[0].get_attr("IFLA_MTU"), name, minimum)


def ensure_bridges() -> None:
    """Create at most nine owned bridges; roll back only this call's additions."""
    with _iproute() as ipr:
        names = bridge_names()
        existing = {name: _owned_link(ipr, name) for name in names}
        for name, link in existing.items():
            if link is not None:
                _check_unnumbered(ipr, link["index"], name)
        created: list[int] = []
        try:
            for name in names:
                link = existing[name]
                if link is None:
                    ipr.link("add", ifname=name, kind="bridge")
                    indices = ipr.link_lookup(ifname=name)
                    if len(indices) != 1:
                        raise RuntimeError(
                            f"bridge {name} missing after creation"
                        )
                    created.append(indices[0])
                    # Some kernels ignore IFLA_IFALIAS during RTM_NEWLINK creation.
                    ipr.link("set", index=indices[0], ifalias=OWNER)
                    link = _owned_link(ipr, name)
                    if link is None:
                        raise RuntimeError(
                            f"bridge {name} missing after creation"
                        )
                # Data bridges never become an IPv6 router or acquire link-local IPs.
                ipv6 = PROC_SYS / "net/ipv6/conf"
                if ipv6.is_dir():
                    (ipv6 / name / "disable_ipv6").write_text("1\n")
                # The QEMU helper copies this MTU to new unprivileged TAPs.
                ipr.link("set", index=link["index"], mtu=DATA_MTU, state="up")
                _link_mtu(ipr, link["index"], name, DATA_MTU)
                _check_unnumbered(ipr, link["index"], name)
        except BaseException:
            for index in reversed(created):
                ipr.link("del", index=index)
            raise


def remove_bridges() -> None:
    """Delete empty owned bridges; never remove a bridge with attached guests."""
    with _iproute() as ipr:
        links = [(name, _owned_link(ipr, name)) for name in bridge_names()]
        for name, link in links:
            if link is not None and any((SYS_NET / name / "brif").iterdir()):
                raise RuntimeError(
                    f"data bridge {name} still has attached interfaces"
                )
        for _name, link in reversed(links):
            if link is not None:
                ipr.link("del", index=link["index"])


def require_bridges(topology: RailTopology) -> None:
    """Check prepared fabrics without root, writes, or subprocesses."""
    if platform.system() != "Linux":
        raise RuntimeError("explicit rail topologies require a Linux host")
    for name in dict.fromkeys(rail.bridge for rail in topology.rails):
        directory = SYS_NET / name
        try:
            alias = (directory / "ifalias").read_text().strip()
            if not (directory / "bridge").is_dir() or alias != OWNER:
                raise RuntimeError(f"data bridge {name} is not owned by ltvm")
            if not int((directory / "flags").read_text().strip(), 16) & 1:
                raise RuntimeError(f"data bridge {name} is down")
            with (directory / "mtu").open() as stream:
                raw_mtu = stream.read(17).strip()
            # A sysfs MTU is a short decimal u32, never a truncated prefix.
            if (
                len(raw_mtu) > 10
                or not raw_mtu.isascii()
                or not raw_mtu.isdecimal()
            ):
                raise RuntimeError(f"interface {name} has an invalid MTU")
            _checked_mtu(int(raw_mtu), name, DATA_MTU)
        except OSError as error:
            raise RuntimeError(
                f"data bridge {name} is unavailable; run ltvm install --network"
            ) from error
    with _iproute() as ipr:
        for name in dict.fromkeys(rail.bridge for rail in topology.rails):
            link = _owned_link(ipr, name)
            if link is None:
                # Recheck after sysfs reads so removed fabrics cannot pass startup.
                raise RuntimeError(f"data bridge {name} disappeared")
            _check_unnumbered(ipr, link["index"], name)
            _checked_mtu(link.get_attr("IFLA_MTU"), name, DATA_MTU)


def _tap_targets(
    ipr: Any, attachments: list[tuple[str, str]]
) -> list[tuple[str, int, int]]:
    """Inspect every target fabric and collision before creating a TAP."""
    prepared: list[tuple[str, int, int]] = []
    for tap, bridge in attachments:
        masters = ipr.link_lookup(ifname=bridge)
        if len(masters) != 1 or ipr.link_lookup(ifname=tap):
            # A device name cannot authorize replacing a running VM's TAP.
            raise RuntimeError(
                f"TAP {tap} exists or bridge {bridge} is missing"
            )
        # Management keeps its existing MTU; all data fabrics support SoftRoCE.
        minimum = 1 if bridge == BRIDGE else DATA_MTU
        mtu = _link_mtu(ipr, masters[0], bridge, minimum)
        prepared.append((tap, masters[0], mtu))
    return prepared


def prepare_taps(vm: VMInfo) -> None:
    """Attach a root-run profile VM's TAPs using the network API, not ip."""
    from .vm_net import tap_for_name

    if vm.rail_topology is None or vm.tap != tap_for_name(vm.name):
        raise ValueError("TAP setup requires a validated profile VM")
    vm.rail_topology.validate_for_nics(vm.nics)
    owner = pwd.getpwnam(
        os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
    )
    attachments = [(vm.tap, BRIDGE)] + [
        (tap, vm.rail_topology.rails[index - 1].bridge)
        for index, _kind, tap, _mac in vm.extra_nics()
    ]
    created: list[int] = []
    with _iproute() as ipr:
        prepared = _tap_targets(ipr, attachments)
        try:
            for tap, master, mtu in prepared:
                ipr.link(
                    "add",
                    ifname=tap,
                    kind="tuntap",
                    mode="tap",
                    uid=owner.pw_uid,
                )
                indices = ipr.link_lookup(ifname=tap)
                if len(indices) != 1:
                    raise RuntimeError(f"TAP {tap} missing after creation")
                created.append(indices[0])
                # Set MTU before attachment so the bridge never sees a 1500 port.
                ipr.link("set", index=indices[0], mtu=mtu)
                _link_mtu(ipr, indices[0], tap, mtu)
                ipr.link(
                    "set",
                    index=indices[0],
                    master=master,
                    ifalias=tap_owner(vm),
                    state="up",
                )
        except BaseException:
            for index in reversed(created):
                ipr.link("del", index=index)
            raise


def remove_taps(vm: VMInfo) -> None:
    """Delete only this profile VM's stopped TAPs through the network API."""
    from .vm_net import tap_for_name

    if vm.rail_topology is None:
        raise ValueError("TAP cleanup requires a profile VM")
    if vm.tap != tap_for_name(vm.name):
        raise ValueError("profile management TAP name does not match its VM")
    vm.rail_topology.validate_for_nics(vm.nics)
    taps = [vm.tap, *(tap for _index, _kind, tap, _mac in vm.extra_nics())]
    present = [tap for tap in taps if (SYS_NET / tap).exists()]
    if not present:
        return
    if os.geteuid() != 0:
        # Helper-created TAPs normally disappear when QEMU closes them.
        raise RuntimeError(
            "profile TAP cleanup needs root; a TAP remains after QEMU stopped"
        )
    with _iproute() as ipr:
        bridges = [BRIDGE, *(rail.bridge for rail in vm.rail_topology.rails)]
        owned: list[int] = []
        for tap, bridge in zip(taps, bridges):
            indices = ipr.link_lookup(ifname=tap)
            if not indices:
                continue
            if len(indices) != 1:
                raise RuntimeError(f"refusing ambiguous TAP {tap}")
            links = ipr.get_links(indices[0])
            if len(links) != 1:
                raise RuntimeError(f"TAP {tap} disappeared during inspection")
            link = links[0]
            info = link.get_attr("IFLA_LINKINFO")
            masters = ipr.link_lookup(ifname=bridge)
            if (
                info is None
                or info.get_attr("IFLA_INFO_KIND") not in ("tun", "tuntap")
                or link.get_attr("IFLA_IFALIAS") != tap_owner(vm)
                or masters != [link.get_attr("IFLA_MASTER")]
            ):
                # Preflight every device before deleting the first owned TAP.
                raise RuntimeError(f"refusing unmanaged TAP {tap}")
            owned.append(indices[0])
        for index in owned:
            ipr.link("del", index=index)


def tap_owner(vm: VMInfo) -> str:
    """Bind a TAP's ownership marker to one VM's canonical name."""
    return f"ltvm:tap:v1:{vm.name}"


def render_service(python: str, root: Path) -> str:
    """Render a bounded bridge service using the same installed Python runtime."""
    if any(character in python + str(root) for character in '\n\r\t\x00"%$\\'):
        raise ValueError("unsupported character in rail service runtime path")
    return (
        "[Unit]\nDescription=ltvm isolated data bridges\nAfter=network.target\n\n"
        "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
        f'WorkingDirectory="{root}"\n'
        f'ExecStart="{python}" -m ltvm_pkg.rail_host start\n'
        f'ExecStop="{python}" -m ltvm_pkg.rail_host stop\n\n'
        "[Install]\nWantedBy=multi-user.target\n"
    )


def main() -> int:
    """Accept only a fixed positional service action and require root."""
    if len(sys.argv) != 2 or sys.argv[1] not in ("start", "stop"):
        print("usage: python -m ltvm_pkg.rail_host start|stop", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("rail bridge setup requires root", file=sys.stderr)
        return 1
    try:
        if sys.argv[1] == "start":
            ensure_bridges()
        else:
            remove_bridges()
    except (OSError, RuntimeError, ValueError) as error:
        print(f"rail bridge setup: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
