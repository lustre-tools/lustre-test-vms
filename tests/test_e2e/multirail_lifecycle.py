"""Check that a stopped profile VM restarts with its saved network identity."""

from __future__ import annotations

import json
import shlex
import uuid
from pathlib import Path

from ltvm_pkg.vm_net import tap_for_name
from tests.multirail_metrics import peer_has_rails
from tests.test_e2e.multirail_live import Guest, Lab

SYS_NET = Path("/sys/class/net")
BOOT_ID_COMMAND = (
    "read -r boot_id < /proc/sys/kernel/random/boot_id && "
    "printf '%s\\n' \"$boot_id\""
)
IDENTITY_KEYS = (
    "NAME",
    "IP",
    "MAC",
    "TAP",
    "NICS",
    "NIC_IPS",
    "NIC_IP6S",
    "RAIL_TOPOLOGY",
)


def _saved_identity(lab: Lab, guest: Guest) -> dict[str, str]:
    """Read exact persisted addresses and topology while excluding runtime PIDs."""
    with (lab.sockets / f"{guest.name}.info").open("rb") as source:
        raw = source.read(65537)
    assert len(raw) <= 65536, "VM state exceeds the live test's input limit"
    values = dict(
        line.split("=", 1) for line in raw.decode().splitlines() if "=" in line
    )
    return {key: values[key] for key in IDENTITY_KEYS}


def _tap_indices(guest: Guest) -> dict[str, int]:
    """Require real TAPs on their expected bridges and return kernel indices."""
    bridges = ["fcbr0", *(rail["bridge"] for rail in guest.topology["rails"])]
    taps = (tap_for_name(guest.name), *guest.taps)
    assert len(taps) == len(bridges) == 3
    indices = {}
    for name, bridge in zip(taps, bridges):
        directory = SYS_NET / name
        assert (directory / "master").resolve(strict=True).name == bridge
        indices[name] = int((directory / "ifindex").read_text().strip())
    return indices


def _discover_both_peers(lab: Lab, first: Guest) -> None:
    """Check discovered grouping and both NIDs before and after a guest restart."""
    assert first in lab.guests and len(lab.guests) == 2
    second = next(guest for guest in lab.guests if guest != first)
    for source, target in ((first, second), (second, first)):
        lab.run(source, f"lnetctl discover {shlex.quote(target.nids[0])}")
        for nid in target.nids:
            lab.run(source, f"lnetctl ping --timeout 5 {shlex.quote(nid)}")
        peers = lab.run(source, "lnetctl peer show --verbose 3").stdout
        assert peer_has_rails(peers, target.nids), peers


def restart_saved_profile(lab: Lab, guest: Guest) -> None:
    """Restart one owned guest before mounting and verify saved and live state."""
    assert guest in lab.guests and len(lab.guests) == 2
    identity = _saved_identity(lab, guest)
    before = _tap_indices(guest)
    boot_before = uuid.UUID(lab.run(guest, BOOT_ID_COMMAND).stdout.strip())
    _discover_both_peers(lab, guest)
    lab.cli("stop", guest.name, timeout=120)
    assert lab.read_guest(guest.name) == guest
    assert _saved_identity(lab, guest) == identity
    for name in before:
        assert not (SYS_NET / name).exists(), f"TAP remains after stop: {name}"
    lab.cli("start", guest.name, timeout=300)
    assert lab.read_guest(guest.name) == guest
    assert _saved_identity(lab, guest) == identity
    after = _tap_indices(guest)
    assert all(after[name] != index for name, index in before.items())
    boot_after = uuid.UUID(lab.run(guest, BOOT_ID_COMMAND).stdout.strip())
    assert boot_after != boot_before, "start did not boot a new guest kernel"
    lab.configure(guest)
    # The fresh client opens connections before the survivor checks it again.
    _discover_both_peers(lab, guest)
    lab.management()
    (lab.evidence / "restart-identity.json").write_text(
        json.dumps(
            {
                "guest": guest.name,
                "saved_identity": identity,
                "tap_indices_before": before,
                "tap_indices_after": after,
                "boot_id_before": str(boot_before),
                "boot_id_after": str(boot_after),
            },
            indent=2,
        )
    )
