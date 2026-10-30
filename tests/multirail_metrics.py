"""Strict, host-independent checks for live Multi-Rail measurements."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any

import yaml

MIB = 1024 * 1024
DATA_INTERFACES = ("eth1", "eth2")
MAX_CAPTURE = 256 * 1024


def _counter(value: Any) -> int:
    """Reject missing, negative, boolean, and non-integer counters."""
    if type(value) is not int or value < 0:
        # Missing measurements cannot count as a quiet or healthy interface.
        raise ValueError(f"invalid measurement counter: {value!r}")
    return value


def read_yaml(text: str) -> dict[str, Any]:
    """Decode a bounded CLI result and require its top-level mapping."""
    if len(text) > MAX_CAPTURE:
        raise ValueError("measurement exceeds the capture limit")
    value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ValueError("measurement is not a YAML mapping")
    return value


def canonical_nid(nid: str) -> str:
    """Compare IPv6 spellings and equivalent zero TCP or o2ib network names."""
    if nid == "0@lo":
        # LNet loopback uses a literal zero, not an IPv4 or IPv6 address.
        return nid
    address, network = nid.rsplit("@", 1)
    address = str(ipaddress.ip_address(address.strip("[]")))
    if network in ("tcp", "o2ib"):
        # LNet omits zero in reports for both supported transports.
        network += "0"
    return f"{address}@{network}"


def selftest_ipv6_group_rejected(
    stdout: str, stderr: str, group: str, nid: str
) -> bool:
    """Identify the exact legacy IPv6 parser and empty-group deletion path."""
    if len(stdout) + len(stderr) > MAX_CAPTURE:
        raise ValueError("self-test group report exceeds the capture limit")
    if nid == "0@lo":
        # Local controller traffic does not exercise the IPv6 NID parser.
        return False
    address = nid.rsplit("@", 1)[0].strip("[]")
    if ipaddress.ip_address(address).version != 6:
        # Invalid IPv4 inputs are setup failures, not an IPv6 compatibility limit.
        return False
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    expected = {
        f"Invalid nid: {nid}",
        f"Ignore invalid id list {nid}",
        f"No nodes added successfully, deleting group {group}",
    }
    return (
        stdout.strip() == "Group is deleted"
        and len(lines) == 3
        and set(lines) == expected
    )


def selftest_group_has_node(text: str, group: str, nid: str) -> bool:
    """Require one active member from an exact named-group query to lst."""
    if len(text) > MAX_CAPTURE:
        raise ValueError("self-test group report exceeds the capture limit")
    rows = [line.strip() for line in text.splitlines() if line.strip()]
    has_title = bool(rows) and rows[0].split() == [
        "ACTIVE",
        "BUSY",
        "DOWN",
        "UNKNOWN",
        "TOTAL",
    ]
    if has_title:
        rows = rows[1:]
    name = re.escape(group)
    if len(rows) not in (3, 5) or not re.fullmatch(
        rf"Group \[\s*{name}\s*\]", rows[0]
    ):
        # Reject extra groups, missing membership, and a different named group.
        return False
    if len(rows) == 5:
        # A named netlink query selects one kernel group despite this display bug.
        if not has_title or rows[1:3] != [
            f"No nodes found [ {group} ]",
            "Group [ (null) ]",
        ]:
            return False
        footer = r"Total 1 nodes \[\s*\(null\)\s*\]"
    else:
        footer = rf"Total 1 nodes \[\s*{name}\s*\]"
    if not re.fullmatch(footer, rows[-1]):
        return False
    node = re.fullmatch(r"(\S+):\s*(Active|Busy|Down|Unknown)", rows[-2])
    if node is None or node.group(2) != "Active":
        return False
    reported = re.sub(r"^\d+-", "", node.group(1))
    return canonical_nid(reported) == canonical_nid(nid)


@dataclass(frozen=True)
class Snapshot:
    """Interface bytes, LNet PUT/REPLY counts, and local NI health."""

    link_bytes: dict[str, int]
    bulk_messages: dict[str, int]
    health: dict[str, int]


def parse_snapshot(links: str, networks: str) -> Snapshot:
    """Read exact link rows and active data NIs; reject incomplete captures."""
    link_bytes: dict[str, int] = {}
    for line in links.splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[0] in link_bytes:
            raise ValueError("invalid or duplicate interface measurement")
        name, received, sent = fields
        link_bytes[name] = _counter(int(received)) + _counter(int(sent))
    if set(link_bytes) != {"eth0", *DATA_INTERFACES}:
        raise ValueError("expected management and both data interfaces")
    bulk: dict[str, int] = {}
    health: dict[str, int] = {}
    nets = read_yaml(networks).get("net")
    if not isinstance(nets, list) or len(nets) > 10:
        raise ValueError("invalid LNet network list")
    for net in nets:
        for ni in net.get("local NI(s)", []):
            if str(ni.get("nid", "")).endswith("@lo"):
                # Loopback is expected but cannot prove a physical rail works.
                continue
            names = list(ni.get("interfaces", {}).values())
            if len(names) != 1 or names[0] not in DATA_INTERFACES:
                raise ValueError("LNet contains an unexpected interface")
            name = names[0]
            if name in bulk:
                raise ValueError("duplicate data NI")
            bulk[name] = sum(
                _counter(ni.get(direction, {}).get(operation))
                for direction in ("sent_stats", "received_stats")
                for operation in ("put", "reply")
            )
            health[name] = _counter(
                ni.get("health stats", {}).get("health value")
            )
            if health[name] > 1000:
                raise ValueError("NI health exceeds its maximum")
    if set(bulk) != set(DATA_INTERFACES):
        raise ValueError("both active data NIs must be present")
    return Snapshot(link_bytes, bulk, health)


def bulk_progress(
    before: Snapshot,
    after: Snapshot,
    active: tuple[str, ...] = DATA_INTERFACES,
    *,
    balanced: bool = True,
) -> dict[str, float]:
    """Require substantial data on each rail; counters alone are not payload."""
    if not active or any(name not in DATA_INTERFACES for name in active):
        raise ValueError("invalid active data interfaces")
    delta = {
        name: after.link_bytes[name] - before.link_bytes[name]
        for name in before.link_bytes
    }
    messages = {
        name: after.bulk_messages[name] - before.bulk_messages[name]
        for name in DATA_INTERFACES
    }
    if min(*delta.values(), *messages.values()) < 0:
        raise AssertionError("measurement counters reset during the workload")
    total = sum(delta[name] for name in DATA_INTERFACES)
    if delta["eth0"] > max(MIB, total // 20):
        raise AssertionError(
            "management traffic is too large for this data-only measurement"
        )
    shares: dict[str, float] = {}
    for name in active:
        if delta[name] < 16 * MIB or messages[name] < 8:
            # Health pings and ARP can increment counters without bulk transfer.
            raise AssertionError(
                f"{name} has no substantial bulk traffic: {delta}, {messages}"
            )
        shares[name] = delta[name] / total
        if balanced and shares[name] < 0.10:
            raise AssertionError(
                f"{name} carries less than 10% of data-interface bytes"
            )
    return shares


def peer_has_rails(text: str, expected: tuple[str, str]) -> bool:
    """Require both remote NIDs in one discovered Multi-Rail peer."""
    wanted = {canonical_nid(nid) for nid in expected}
    peers = read_yaml(text).get("peer", [])
    if not isinstance(peers, list) or len(peers) > 64:
        raise ValueError("invalid peer capture")
    for peer in peers:
        nids = {
            canonical_nid(ni["nid"])
            for ni in peer.get("peer ni", [])
            if not ni["nid"].endswith("@lo")
        }
        if wanted <= nids and peer.get("Multi-Rail") is True:
            return True
    return False


def peer_rails_healthy(text: str, expected: tuple[str, str]) -> bool:
    """Check remote NI recovery, which is separate from local link recovery."""
    wanted = {canonical_nid(nid) for nid in expected}
    for peer in read_yaml(text).get("peer", []):
        health = {
            canonical_nid(ni["nid"]): _counter(
                ni.get("health stats", {}).get("health value")
            )
            for ni in peer.get("peer ni", [])
            if not ni["nid"].endswith("@lo")
        }
        if wanted <= health.keys():
            return all(health[nid] == 1000 for nid in wanted)
    return False


def batch_stopped(text: str) -> bool:
    """Check lst's status text because query can return success on errors."""
    if "unknown" in text or re.search(r"failed:\s*[1-9]", text):
        raise AssertionError(
            f"self-test reports failed or unknown nodes: {text}"
        )
    if re.search(r"\bis stopped\s*$", text):
        return True
    if re.search(r"\bis running\s*$", text):
        return False
    raise AssertionError(f"unrecognized self-test status: {text}")
