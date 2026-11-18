"""Link faults on shmrdmad links: ``ltvm vm link``.

shmrdmad is the wire between the VMs on its switch, so it can do to a
link what a switch port or a cable does: take it down, delay it, lose
packets on it, or limit its rate.  It takes rules on its control socket
(``rdma.sock.ctl`` beside its other sockets), one per link, naming the
link by MAC (a shmnet or shmrdma NIC, and the RDMA device on it) or by
IP address (an RDMA device, eth0's under ``--shmrdma`` included).

Down is silent to the far end, as a dead switch port is: RDMA sends to
it wait for their retries to run out, and frames are dropped.  The VM's
own NIC also loses carrier unless ``--silent``, as it does when its
switch port is disabled; eth0 never does, since ssh rides on it.
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .vm_state import VMInfo


class LinkError(RuntimeError):
    """A link that cannot be named, or a request the daemon refused."""


@dataclass(frozen=True)
class Link:
    """One NIC of a VM, as the daemon and QEMU name it."""

    vm: str
    nic: str  # eth0, eth1, ...
    ip: str  # its IPv4 address, "" when the daemon should not match on it
    mac: str  # its MAC, "" when the daemon should not match on it
    netdev: str  # QEMU's netdev id, "" for eth0
    on_switch: bool  # the daemon carries it
    ip6: str = ""  # its IPv6 address, which a rule may name instead

    def selector(self) -> str:
        parts = []
        if self.ip:
            parts.append(f"ip={self.ip}")
        if self.mac:
            parts.append(f"mac={self.mac}")
        return " ".join(parts)


def _addr(s: str) -> str:
    """*s* as the daemon prints an address."""
    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        return s


def link_of(vm: VMInfo, nic: str) -> Link:
    """*vm*'s NIC *nic* (``ethN``)."""
    if not nic.startswith("eth") or not nic[3:].isdigit():
        raise LinkError(f"{nic!r}: expected a NIC name, eth0, eth1, ...")
    idx = int(nic[3:])
    if idx == 0:
        # The bridge carries eth0; only the RDMA device on it is ours.
        return Link(
            vm.name, nic, vm.ip if vm.shmrdma else "", "", "", bool(vm.shmrdma)
        )
    extras = {i: (n, mac) for i, n, _tap, mac in vm.extra_nics()}
    if idx not in extras:
        raise LinkError(f"{vm.name} has no {nic}")
    spec, mac = extras[idx]
    ip = vm.nic_ips[idx - 1] if idx - 1 < len(vm.nic_ips) else ""
    ip6 = vm.nic_ip6s[idx - 1] if idx - 1 < len(vm.nic_ip6s) else ""
    return Link(
        vm.name,
        nic,
        ip if spec.type == "shmrdma" else "",
        mac if spec.on_switch else "",
        # a passthrough NIC is the host's device, with no QEMU netdev
        "" if spec.type == "passthrough" else f"net{idx}",
        spec.on_switch,
        ip6 if spec.type == "shmrdma" else "",
    )


def links_of(vm: VMInfo) -> list[Link]:
    """Every NIC of *vm* the daemon carries."""
    out = []
    for nic in ["eth0"] + [f"eth{i}" for i, *_ in vm.extra_nics()]:
        link = link_of(vm, nic)
        if link.on_switch:
            out.append(link)
    return out


def _own_socket(vm: VMInfo, nic: str) -> str:
    """The socket *nic* was given in place of ltvm's daemon, or ""."""
    idx = int(nic[3:])
    if idx == 0:
        sock = "" if vm.shmrdma == "auto" else vm.shmrdma
    else:
        spec = next(n for i, n, _t, _m in vm.extra_nics() if i == idx)
        sock = spec.socket
    return sock


def control_socket(vm: VMInfo, link: Link) -> Path:
    """The control socket of the daemon carrying *link*."""
    from . import shmrdma

    d = shmrdma.daemon_for(vm)
    sock = _own_socket(vm, link.nic)
    if sock and sock not in (str(d.rdma_socket), str(d.net_socket)):
        raise LinkError(
            f"{vm.name} {link.nic} uses a shmrdmad of its own ({sock}); "
            f"send its control socket the requests by hand"
        )
    return d.ctl_socket


@contextlib.contextmanager
def change_lock(vm: VMInfo) -> Iterator[None]:
    """Hold *vm*'s link lock: one change's daemon rule and carrier
    must not interleave with another's."""
    from . import vm_claim

    with vm_claim.link_lock(vm.name):
        yield


def request(ctl: Path, line: str, timeout: float = 5.0) -> list[str]:
    """Send one request; return the reply's lines before ``ok``."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(ctl))
            s.sendall(line.encode() + b"\n")
            data = bytearray()
            while chunk := s.recv(65536):
                data.extend(chunk)
    except FileNotFoundError:
        raise LinkError(
            f"shmrdmad has no control socket at {ctl}: it is not running, "
            f"or predates link faults (`ltvm vm shmrdmad status`; it can "
            f"be restarted once no VM uses it)"
        ) from None
    except OSError as e:
        raise LinkError(f"shmrdmad control socket {ctl}: {e}") from None
    lines = data.decode(errors="replace").splitlines()
    if not lines or lines[-1] != "ok":
        why = next((x for x in lines if x.startswith("error:")), None)
        raise LinkError(f"shmrdmad: {why or 'no reply'}")
    return lines[:-1]


def parse_rule(line: str) -> dict[str, str]:
    """A ``link ...`` line of a reply as key/value pairs."""
    words = line.split()
    return dict(w.split("=", 1) for w in words[1:] if "=" in w)


def set_carrier(vm: VMInfo, link: Link, up: bool) -> None:
    """Raise or drop the carrier of *link*'s NIC in the guest."""
    from .vm_commands import qmp_execute

    if not link.netdev:
        raise LinkError(
            "eth0 carries ssh: its carrier stays up"
            if link.nic == "eth0"
            else f"{vm.name} {link.nic} is a passthrough NIC: QEMU has no "
            f"carrier for it"
        )
    try:
        qmp_execute(vm.socket_path, "set_link", {"name": link.netdev, "up": up})
    except (OSError, RuntimeError) as e:
        raise LinkError(f"{vm.name}: QMP set_link {link.netdev}: {e}") from None


def set_fault(
    vm: VMInfo,
    link: Link,
    *,
    down: bool = False,
    delay_ms: float = 0,
    loss_pct: float = 0,
    rate_mbit: float = 0,
    retry_ms: float = 0,
) -> dict[str, str]:
    """Put one rule on *link*, replacing the one it had."""
    if not link.on_switch:
        raise LinkError(
            f"{vm.name} {link.nic} is not on shmrdmad's switch: only its "
            f"carrier can be changed (--carrier-only)"
        )
    words = ["set", link.selector()]
    if down:
        words.append("down")
    if delay_ms:
        words.append(f"delay={delay_ms:g}")
    if loss_pct:
        words.append(f"loss={loss_pct:g}")
    if rate_mbit:
        words.append(f"rate={rate_mbit:g}")
    if retry_ms:
        words.append(f"retry={retry_ms:g}")
    lines = request(control_socket(vm, link), " ".join(words))
    return parse_rule(lines[0]) if lines else {}


def clear_fault(vm: VMInfo, link: Link) -> bool:
    """Drop *link*'s rule; False when it had none."""
    if not link.on_switch:
        return False
    try:
        request(control_socket(vm, link), f"clear {link.selector()}")
    except LinkError as e:
        if "no such link" in str(e):
            return False
        raise
    return True


def rules(vm: VMInfo) -> dict[str, dict[str, str]]:
    """The rules on *vm*'s links, by NIC name."""
    mine = []
    ctl = None
    for link in links_of(vm):
        try:
            ctl = control_socket(vm, link)
        except LinkError:
            continue
        mine.append(link)
    out: dict[str, dict[str, str]] = {}
    if ctl is None:
        return out
    for line in request(ctl, "list"):
        r = parse_rule(line)
        ip, mac = _addr(r.get("ip", "-")), r.get("mac", "-").lower()
        for link in mine:
            # the daemon matches a rule naming any of them
            if (
                (link.ip and _addr(link.ip) == ip)
                or (link.ip6 and _addr(link.ip6) == ip)
                or (link.mac and link.mac.lower() == mac)
            ):
                out.setdefault(link.nic, r)
    return out
