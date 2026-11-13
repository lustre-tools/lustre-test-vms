"""NIC network options, stored layouts, and real locked IP allocation."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from ltvm_pkg import vm_net, vm_state
from ltvm_pkg.nic_network import (
    MAX_POOL_ADDRESSES,
    network_problem,
    select_address,
)
from ltvm_pkg.nic_spec import NicSpec, NicSpecError, load_stored
from ltvm_pkg.vm_state import VMInfo


@pytest.fixture
def address_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep allocator locks and persisted VMs inside a private directory."""
    sockets = tmp_path / "sockets"
    sockets.mkdir()
    monkeypatch.setattr(vm_state, "SUBNET", "198.51.100")
    monkeypatch.setattr(vm_state, "EXTRA_SUBNET", "192.0.2")
    monkeypatch.setattr(vm_state, "EXTRA_SUBNET6", "2001:db8:10:1")
    monkeypatch.setattr(vm_net, "EXTRA_SUBNET6", "2001:db8:10:1")
    monkeypatch.setattr(vm_state, "VM_DIR", tmp_path)
    monkeypatch.setattr(vm_state, "SOCKETS", sockets)
    monkeypatch.setattr(vm_net, "_IP_LOCK_PATH", tmp_path / ".ip-alloc.lock")
    return sockets


@contextmanager
def _alloc(
    name: str, nics: list[NicSpec], explicit_ip: str | None = None
) -> Iterator[tuple[list[str], list[str]]]:
    """Allocate both families the way create does, under one lock."""
    with vm_net.alloc_ip(name, 1 + len(nics), explicit_ip, nics) as ips:
        yield ips, vm_net.nic_ip6s(name, nics, ips[1:])


@pytest.mark.parametrize(
    "raw",
    [
        "tcp:net=tcp0,subnet=192.0.2.0/24,subnet6=2001:db8:1::/64",
        "softroce:node=1,net=o2ib65535,subnet=198.51.100.0/30",
        "tcp:net=tcp65535,subnet6=2001:db8::/126",
    ],
)
def test_network_options_round_trip(raw: str) -> None:
    """Canonical NICS retains each option through parse and load."""
    spec = NicSpec.parse(raw)
    assert str(spec) == raw
    assert load_stored(str(spec)) == spec
    assert spec.cmdline_entry() == spec.type


@pytest.mark.parametrize(
    "raw",
    [
        "tcp:net=tcp65536",
        "tcp:net=tcp01",
        "tcp:net=o2ib0",
        "softroce:net=tcp0",
        "tcp:net=tcp-1",
        "tcp:net=tcp0,net=tcp1",
        "tcp:subnet=192.0.2.1/24",
        "tcp:subnet=192.0.2.0/31",
        "tcp:subnet=2001:db8::/64",
        "tcp:subnet6=192.0.2.0/24",
        "tcp:subnet6=2001:db8::1/64",
        "tcp:subnet6=2001:db8::/127",
        "tcp:subnet=224.0.0.0/24",
        "tcp:subnet=127.0.0.0/24",
        "tcp:subnet=169.254.0.0/24",
        "tcp:subnet6=ff00::/64",
        "tcp:subnet6=fe80::/64",
        "passthrough:0000:85:00.1,net=o2ib0",
        "tcp:subnet6=2001:db8::%eth1/64",
        "tcp:net=tcp0\nfc_ip=192.0.2.1",
        "tcp:net=" + "x" * 512,
    ],
)
def test_bad_network_option_fails_in_cli_and_stored_state(raw: str) -> None:
    """Broken keyed values cannot fall back to a different stored layout."""
    with pytest.raises(NicSpecError):
        NicSpec.parse(raw)
    with pytest.raises(NicSpecError):
        load_stored(raw)


def test_shared_and_distinct_subnets_are_valid(address_state: Path) -> None:
    """Only unequal overlapping prefixes are ambiguous on the shared bridge."""
    for specs in [
        ["tcp:net=tcp0", "tcp:net=tcp0"],
        ["tcp:subnet=203.0.113.0/25", "tcp:subnet=203.0.113.128/25"],
    ]:
        assert (
            vm_net.nic_network_problem([NicSpec.parse(s) for s in specs])
            is None
        )


@pytest.mark.parametrize(
    "specs, expected",
    [
        (["tcp:subnet=198.51.100.0/24"], "management"),
        (["tcp:subnet=203.0.113.0/24", "tcp:subnet=203.0.113.0/25"], "overlap"),
        (["tcp:subnet=192.0.2.0/25"], "overlap"),
        (["tcp:subnet6=2001:db8:10:1::/80"], "overlap"),
        (["tcp:net=tcp0"] * 9, "at most 8"),
    ],
)
def test_layout_errors_before_allocation(
    address_state: Path, specs: list[str], expected: str
) -> None:
    """Preflight rejects layouts without creating VM state or a lock."""
    nics = [NicSpec.parse(s) for s in specs]
    assert expected in (vm_net.nic_network_problem(nics) or "")
    assert not list(address_state.iterdir())
    assert not (address_state.parent / ".ip-alloc.lock").exists()


def test_default_layout_has_no_new_nic_count_limit() -> None:
    """The explicit-layout bound preserves legacy NIC list support."""
    assert (
        network_problem(
            [NicSpec("tcp")] * 9,
            "198.51.100.0/24",
            "192.0.2.0/24",
            "2001:db8::/64",
        )
        is None
    )


def test_implicit_addresses_keep_old_values(address_state: Path) -> None:
    """Extended allocation preserves old IPv4 and derived IPv6 defaults."""
    with vm_net.alloc_ip("same", count=3) as old:
        expected = old.copy()
    with _alloc("same", [NicSpec("tcp")] * 2) as (ips, ip6s):
        assert ips == expected
        assert ip6s == [vm_state.nic_ip6(ip) for ip in ips[1:]]


def test_explicit_addresses_are_saved_and_reload(address_state: Path) -> None:
    """Both families and options survive the real .info write path."""
    nics = [
        NicSpec.parse(
            "tcp:net=tcp0,subnet=203.0.113.0/25,subnet6=2001:db8:1::/64"
        ),
        NicSpec.parse(
            "tcp:net=tcp0,subnet=203.0.113.128/25,subnet6=2001:db8:2::/64"
        ),
    ]
    with _alloc("one", nics, "198.51.100.100") as (
        ips,
        ip6s,
    ):
        VMInfo(
            name="one", ip=ips[0], nics=nics, nic_ips=ips[1:], nic_ip6s=ip6s
        ).save()
    vm = VMInfo.load("one")
    assert vm.nics == nics
    assert vm.ip == "198.51.100.100"
    for nic, address, address6 in zip(nics, vm.nic_ips, vm.nic_ip6s):
        assert ipaddress.ip_address(address) in ipaddress.ip_network(nic.subnet)
        assert ipaddress.ip_address(address6) in ipaddress.ip_network(
            nic.subnet6
        )


def test_small_pool_exhaustion_releases_lock(
    address_state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Failed reservation saves nothing and a later allocation can proceed."""
    nic = NicSpec.parse("tcp:subnet=203.0.113.0/30,subnet6=2001:db8:1::/126")
    with pytest.raises(SystemExit):
        with _alloc("full", [nic] * 3):
            pytest.fail("three data NICs cannot fit two IPv4 host addresses")
    assert "no free IP addresses" in capsys.readouterr().err
    assert not list(address_state.iterdir())
    with _alloc("later", [nic] * 2) as (ips, ip6s):
        assert len(set(ips[1:])) == len(set(ip6s)) == 2
        assert set(ips[1:]) == {"203.0.113.1", "203.0.113.2"}


def test_peer_overlapping_subnet_is_rejected(
    address_state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Existing allocations participate in the subnet overlap check."""
    VMInfo(
        name="peer",
        ip="198.51.100.10",
        nics=[NicSpec.parse("tcp:subnet=203.0.113.0/24")],
        nic_ips=["203.0.113.11"],
    ).save()
    with pytest.raises(SystemExit):
        with _alloc("new", [NicSpec.parse("tcp:subnet=203.0.113.0/25")]):
            pytest.fail("overlap must fail before yielding")
    assert "overlap" in capsys.readouterr().err


def test_corrupt_peer_stops_only_subnet_creates(address_state: Path) -> None:
    """A damaged peer stops a subnet create, not a plain create."""
    VMInfo(
        name="peer",
        ip="198.51.100.10",
        nics=[NicSpec("tcp")],
        nic_ips=["192.0.2.11"],
        nic_ip6s=["fd17:2016:1"],
    ).save()
    with _alloc("plain", [NicSpec("tcp")]) as (ips, ip6s):
        assert ip6s == [vm_state.nic_ip6(ips[1])]
    nic = NicSpec.parse("tcp:subnet6=2001:db8:1::/64")
    with pytest.raises(SystemExit):
        with _alloc("explicit", [nic]):
            pytest.fail("a subnet create must not ignore a corrupt peer")


def test_parallel_creates_reserve_both_families(address_state: Path) -> None:
    """The real lock covers allocation through each real VMInfo.save()."""
    nic = NicSpec.parse(
        "tcp:net=tcp0,subnet=203.0.113.0/26,subnet6=2001:db8:1::/120"
    )

    def create(index: int) -> tuple[list[str], list[str]]:
        """Persist the reservation before another creator can proceed."""
        name = f"parallel-{index}"
        with _alloc(name, [nic, nic]) as (ips, ip6s):
            VMInfo(
                name=name,
                ip=ips[0],
                nics=[nic, nic],
                nic_ips=ips[1:],
                nic_ip6s=ip6s,
            ).save()
            return ips, ip6s

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(create, index) for index in range(16)]
        results = [future.result(timeout=10) for future in futures]
    assert len({ip for ips, _ in results for ip in ips}) == 48
    assert len({ip for _, ip6s in results for ip in ip6s}) == 32


def test_large_subnet_scans_only_the_documented_pool() -> None:
    """Large CIDRs do not create an unbounded scan or hosts() collection."""
    network = ipaddress.ip_network("2001:db8::/32")
    used = {
        str(network.network_address + offset)
        for offset in range(1, MAX_POOL_ADDRESSES + 1)
    }
    with pytest.raises(ValueError, match="65534-address pool"):
        select_address("full", network, used)
