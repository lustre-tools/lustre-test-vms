"""Strict live Multi-Rail acceptance using two new Linux/KVM guests.

Run this checkout's pytest with LTVM_REQUIRE_MULTIRAIL_E2E=1 to make missing
host/image prerequisites fail. LTVM_MULTIRAIL_TARGET and LTVM_MULTIRAIL_KERNEL
select existing artifacts. No artifacts are downloaded or built by this suite.
Both topologies and both IP families are exercised. Exact upstream IPv6
filesystem/self-test limits are reported separately from LNet reachability.
"""

from __future__ import annotations

import ipaddress
import json
import shlex
import traceback
from collections.abc import Iterator
from pathlib import Path

import pytest

from ltvm_pkg.rail_host import DATA_MTU
from tests.multirail_metrics import peer_has_rails
from tests.test_e2e.multirail_diagnostics import capture_failure
from tests.test_e2e.multirail_lifecycle import (
    _discover_both_peers,
    restart_saved_profile,
)
from tests.test_e2e.multirail_live import Guest, Lab, make_lab, qualify_host
from tests.test_e2e.multirail_payload import (
    both_rails_fault,
    checked_copy,
    lnet_selftest,
    mount_filesystem,
    single_rail_fault,
)

pytestmark = pytest.mark.e2e
CASES = [
    (mode, family)
    for mode in ("shared", "isolated")
    for family in ("ipv4", "ipv6")
]


@pytest.fixture(
    scope="function",
    params=CASES,
    ids=[f"{mode}-{family}" for mode, family in CASES],
)
def multirail_lab(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Lab]:
    """Give every test fresh guests and guarantee teardown after any failure."""
    target, kernel, image = qualify_host()
    mode, family = request.param
    evidence = tmp_path_factory.mktemp(f"multirail-{mode}-{family}")
    lab = make_lab(mode, family, evidence)
    try:
        server = lab.create(target, kernel, image, server=True)
        client = lab.create(target, kernel, image, server=False)
        for guest in (server, client):
            lab.configure(guest)
        _discover_both_peers(lab, client)
        yield lab
    except BaseException:
        # Save the setup failure before teardown removes logs or changes links.
        capture_failure(lab, "fixture", traceback.format_exc(limit=32))
        raise
    finally:
        lab.close()


def _check_attachment(lab: Lab, guest: Guest) -> None:
    """Inspect actual TAP membership and guest addresses, not QEMU argv alone."""
    actual_bridges = []
    for index, (tap, rail) in enumerate(
        zip(guest.taps, guest.topology["rails"]), start=1
    ):
        master = (
            (Path("/sys/class/net") / tap / "master").resolve(strict=True).name
        )
        assert master == rail["bridge"]
        # Small packets cannot detect a host port that drops full RXE frames.
        for device in (tap, master):
            mtu = (Path("/sys/class/net") / device / "mtu").read_text()
            assert int(mtu) >= DATA_MTU, (device, mtu)
        actual_bridges.append(master)
        data = json.loads(
            lab.run(guest, f"ip -j address show dev eth{index}").stdout
        )
        addresses = {
            str(ipaddress.ip_address(entry["local"]))
            for entry in data[0]["addr_info"]
        }
        assert guest.ips[index - 1] in addresses
        assert guest.ip6s[index - 1] in addresses
    assert (len(set(actual_bridges)) == 1) == (lab.mode == "shared")
    defaults = json.loads(lab.run(guest, "ip -j -4 route show default").stdout)
    assert defaults and all(route["dev"] == "eth0" for route in defaults)


def _check_paths(lab: Lab, source: Guest, target: Guest) -> None:
    """Exercise the source-bound IP paths that LNet's NI selection will use."""
    family = "-6" if lab.family == "ipv6" else "-4"
    sources = source.ip6s if lab.family == "ipv6" else source.ips
    destinations = target.ip6s if lab.family == "ipv6" else target.ips
    # Disable fragmentation so a short host MTU cannot hide behind small pings.
    size = 4096 if lab.transport == "softroce" else 56
    for index, address in enumerate(sources, start=1):
        for remote_index, destination in enumerate(destinations, start=1):
            expected = lab.mode == "shared" or index == remote_index
            result = lab.run(
                source,
                f"ping {family} -n -c 1 -W 2 -M do -s {size} "
                f"-I {shlex.quote(address)} {shlex.quote(destination)}",
                timeout=8,
                check=False,
            )
            assert (result.returncode == 0) == expected, result
            if expected:
                route = json.loads(
                    lab.run(
                        source,
                        f"ip -j {family} route get {destination} from {address}",
                    ).stdout
                )
                assert route[0]["dev"] == f"eth{index}", route
                neighbor = json.loads(
                    lab.run(
                        source,
                        f"ip -j {family} neigh show to {destination} dev eth{index}",
                    ).stdout
                )
                mac = lab.run(
                    target, f"cat /sys/class/net/eth{remote_index}/address"
                ).stdout.strip()
                assert any(entry.get("lladdr") == mac for entry in neighbor), (
                    neighbor
                )


def test_multirail_topology_routes_and_discovery(multirail_lab: Lab) -> None:
    """Both modes and families need real paths and one discovered MR peer."""
    lab = multirail_lab
    server, client = lab.guests
    restart_saved_profile(lab, client)
    for source, target in ((server, client), (client, server)):
        _check_attachment(lab, source)
        _check_paths(lab, source, target)
        lab.run(source, f"lnetctl discover {shlex.quote(target.nids[0])}")
        for nid in target.nids:
            lab.run(source, f"lnetctl ping --timeout 5 {shlex.quote(nid)}")
        peers = lab.run(source, "lnetctl peer show --verbose 3").stdout
        assert peer_has_rails(peers, target.nids), peers
        snapshot = lab.snapshot(source)
        assert snapshot.health == {"eth1": 1000, "eth2": 1000}
    lab.management()


def test_multirail_checked_lustre_payload_and_faults(
    multirail_lab: Lab,
) -> None:
    """Measure real filesystem payloads, single-rail failover, and recovery."""
    lab = multirail_lab
    mount_filesystem(lab)
    checked_copy(lab, "healthy")
    for index in (1, 2):
        single_rail_fault(lab, index)
    both_rails_fault(lab)
    lab.management()


def test_multirail_checked_lnet_selftest(multirail_lab: Lab) -> None:
    """Keep optional BRW limitations separate from the filesystem data test."""
    lnet_selftest(multirail_lab)
