"""Counter checks must reject quiet rails and probe-only false positives."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.multirail_metrics import (
    MIB,
    Snapshot,
    batch_stopped,
    bulk_progress,
    canonical_nid,
    parse_snapshot,
    peer_has_rails,
    peer_rails_healthy,
    selftest_group_has_node,
    selftest_ipv6_group_rejected,
)


def _networks() -> str:
    """Build the actual lnetctl per-NI statistics shape without host tools."""
    interfaces = []
    for index in (1, 2):
        interfaces.append(
            {
                "nid": f"172.18.100.{index}@tcp",
                "interfaces": {0: f"eth{index}"},
                "sent_stats": {"put": 10, "reply": 3},
                "received_stats": {"put": 7, "reply": 5},
                "health stats": {"health value": 1000},
            }
        )
    return yaml.safe_dump(
        {"net": [{"net type": "tcp", "local NI(s)": interfaces}]}
    )


def _snapshot(rail1: int = 0, rail2: int = 0, messages: int = 0) -> Snapshot:
    """Keep link traffic and LNet bulk counts independently controllable."""
    return Snapshot(
        {"eth0": 0, "eth1": rail1, "eth2": rail2},
        {"eth1": messages, "eth2": messages},
        {"eth1": 1000, "eth2": 1000},
    )


def test_snapshot_parses_complete_consumer_output() -> None:
    """Both rails must have separate byte and LNet operation measurements."""
    actual = parse_snapshot(
        "eth0 10 20\neth1 100 200\neth2 400 500\n", _networks()
    )
    assert actual.link_bytes == {"eth0": 30, "eth1": 300, "eth2": 900}
    assert actual.bulk_messages == {"eth1": 25, "eth2": 25}
    assert actual.health == {"eth1": 1000, "eth2": 1000}


@pytest.mark.parametrize(
    "links", ["", "eth0 1 2\neth1 3 4", "eth0 0 0\neth1 -1 0\neth2 0 0"]
)
def test_snapshot_rejects_missing_or_invalid_links(links: str) -> None:
    """Unavailable counters and negative values are failures, not zeros."""
    with pytest.raises(ValueError):
        parse_snapshot(links, _networks())


def test_snapshot_rejects_missing_health() -> None:
    """A missing health block cannot be interpreted as a healthy rail."""
    data = yaml.safe_load(_networks())
    del data["net"][0]["local NI(s)"][0]["health stats"]
    with pytest.raises(ValueError):
        parse_snapshot("eth0 0 0\neth1 0 0\neth2 0 0", yaml.safe_dump(data))


def test_bulk_progress_accepts_substantial_balanced_traffic() -> None:
    """Measure link-byte shares while checking independent LNet bulk counts."""
    assert bulk_progress(_snapshot(), _snapshot(64 * MIB, 64 * MIB, 64)) == {
        "eth1": 0.5,
        "eth2": 0.5,
    }


@pytest.mark.parametrize("size,messages", [(4096, 100), (64 * MIB, 0)])
def test_bulk_progress_rejects_health_probes_or_unrelated_traffic(
    size: int, messages: int
) -> None:
    """Neither many tiny pings nor non-LNet bytes establish bulk balancing."""
    with pytest.raises(AssertionError, match="substantial bulk"):
        bulk_progress(_snapshot(), _snapshot(size, size, messages))


def test_bulk_progress_rejects_idle_rail_and_unbalanced_large_transfer() -> (
    None
):
    """A busy first rail cannot hide a quiet or almost unused second rail."""
    for after in (
        _snapshot(128 * MIB, 0, 64),
        _snapshot(512 * MIB, 16 * MIB, 64),
    ):
        with pytest.raises(AssertionError):
            bulk_progress(_snapshot(), after)


def test_bulk_progress_rejects_counter_reset_and_management_payload() -> None:
    """Counter resets and accidental management traffic invalidate the sample."""
    after = _snapshot(64 * MIB, 64 * MIB, 64)
    with pytest.raises(AssertionError, match="reset"):
        bulk_progress(_snapshot(100 * MIB), after)
    changed = Snapshot(
        {**after.link_bytes, "eth0": 64 * MIB},
        after.bulk_messages,
        after.health,
    )
    with pytest.raises(AssertionError, match="management"):
        bulk_progress(_snapshot(), changed)


def test_surviving_rail_requires_bulk_progress_without_balance() -> None:
    """A one-rail failure test still needs substantial checked data transfer."""
    shares = bulk_progress(
        _snapshot(), _snapshot(0, 64 * MIB, 64), ("eth2",), balanced=False
    )
    assert shares == {"eth2": 1.0}


def test_peer_check_groups_two_networks_as_one_peer() -> None:
    """Distinct LNet networks can belong to the same Multi-Rail peer."""
    expected = ("172.18.101.10@tcp0", "172.18.102.10@tcp1")
    peer = {
        "Multi-Rail": True,
        "peer ni": [
            {"nid": expected[0].replace("tcp0", "tcp")},
            {"nid": expected[1]},
        ],
    }
    assert peer_has_rails(yaml.safe_dump({"peer": [peer]}), expected)
    separate = [{"Multi-Rail": True, "peer ni": [ni]} for ni in peer["peer ni"]]
    assert not peer_has_rails(yaml.safe_dump({"peer": separate}), expected)


def test_ipv6_nid_comparison_handles_compression_and_brackets() -> None:
    """NID spelling differences must not cause an IPv4 fallback or false failure."""
    assert canonical_nid("[fd17:0:0:0:0:0:0:1]@tcp") == "fd17::1@tcp0"


@pytest.mark.parametrize("network", ("tcp", "tcp0", "o2ib", "o2ib0"))
def test_nid_comparison_normalizes_zero_for_both_transports(
    network: str,
) -> None:
    """Actual @o2ib reports must match the saved @o2ib0 profile, like TCP."""
    expected = "o2ib0" if network.startswith("o2ib") else "tcp0"
    assert canonical_nid(f"192.0.2.10@{network}") == f"192.0.2.10@{expected}"
    assert (
        canonical_nid(f"[2001:db8:0:0:0:0:0:1]@{network}")
        == f"2001:db8::1@{expected}"
    )


@pytest.mark.parametrize("network", ("tcp7", "o2ib7"))
def test_nid_comparison_preserves_nonzero_network_identity(
    network: str,
) -> None:
    """Different isolated network IDs must never become interchangeable."""
    assert canonical_nid(f"192.0.2.10@{network}") == f"192.0.2.10@{network}"


@pytest.mark.parametrize("network", ("tcp", "o2ib"))
def test_selftest_group_requires_the_expected_active_node(network: str) -> None:
    """Verbose group output proves node acceptance, including printed zero IDs."""
    expected = f"192.0.2.10@{network}0"
    report = (
        "\tACTIVE\tBUSY\tDOWN\tUNKNOWN\tTOTAL\nGroup [ source ]\n"
        f"\t12345-192.0.2.10@{network}: Active\nTotal 1 nodes [ source ]\n"
    )
    assert selftest_group_has_node(report, "source", expected)
    assert not selftest_group_has_node(
        report.replace("Active", "Down"), "source", expected
    )
    assert not selftest_group_has_node(
        report.replace("192.0.2.10", "192.0.2.11"), "source", expected
    )
    assert not selftest_group_has_node(report, "sink", expected)


@pytest.mark.parametrize("network", ("tcp", "o2ib"))
def test_selftest_group_accepts_the_captured_netlink_formatter(
    network: str,
) -> None:
    """Keep the captured formatter shape; replace only its private node address."""
    path = Path(__file__).parent / "fixtures" / "multirail"
    report = (path / f"lst-group-netlink-{network}.txt").read_text()
    expected = f"192.0.2.10@{network}0"
    assert selftest_group_has_node(report, "source", expected)
    assert not selftest_group_has_node(report, "sink", expected)
    assert not selftest_group_has_node(
        report, "source", f"192.0.2.11@{network}0"
    )


@pytest.mark.parametrize(
    "old,new",
    (
        ("\tACTIVE\tBUSY\tDOWN\tUNKNOWN\tTOTAL\n", ""),
        ("No nodes found [ source ]\n", ""),
        ("No nodes found [ source ]", "No nodes found [ sink ]"),
        ("Group [ source ]", "Group [ sink ]"),
        ("Group [ (null) ]", "Group [ sink ]"),
        ("Total 1 nodes", "Total 0 nodes"),
        ("Active", "Down"),
        ("\t12345-192.0.2.10@tcp: Active\n", ""),
        ("Total 1 nodes [ (null) ]", "Total 1 nodes [ source ]"),
    ),
)
def test_netlink_formatter_exception_rejects_other_output(
    old: str,
    new: str,
) -> None:
    """Accept only the exact known wrapper, with one active expected member."""
    path = (
        Path(__file__).parent
        / "fixtures"
        / "multirail"
        / "lst-group-netlink-tcp.txt"
    )
    report = path.read_text()
    assert old in report
    assert not selftest_group_has_node(
        report.replace(old, new), "source", "192.0.2.10@tcp0"
    )
    assert not selftest_group_has_node(
        report + report, "source", "192.0.2.10@tcp0"
    )


@pytest.mark.parametrize(
    "report",
    (
        "",
        "No nodes found [ source ]\n",
        "Group [ source ]\nTotal 0 nodes [ source ]\n",
        "Group [ source ]\n\t192.0.2.10@tcp: Active\nTotal 2 nodes [ source ]\n",
    ),
)
def test_empty_or_inconsistent_selftest_group_never_passes(report: str) -> None:
    """A successful group command must still produce complete membership data."""
    assert not selftest_group_has_node(report, "source", "192.0.2.10@tcp0")


def test_legacy_ipv6_rejection_requires_the_exact_parser_path() -> None:
    """An unrelated error or invalid IPv4 NID must remain a failed test."""
    nid = "2001:db8::10@tcp0"
    error = (
        f"Invalid nid: {nid}\nIgnore invalid id list {nid}\n"
        "No nodes added successfully, deleting group source\n"
    )
    assert selftest_ipv6_group_rejected(
        "Group is deleted\n", error, "source", nid
    )
    assert not selftest_ipv6_group_rejected("", error, "source", nid)
    assert not selftest_ipv6_group_rejected(
        "Group is deleted\n", error + "RPC error\n", "source", nid
    )
    assert not selftest_ipv6_group_rejected(
        "Group is deleted\n", "Reader error: No groups found", "source", nid
    )
    assert not selftest_ipv6_group_rejected(
        "Group is deleted\n", error, "sink", nid
    )
    assert not selftest_ipv6_group_rejected(
        "Group is deleted\n", error, "source", "192.0.2.10@tcp0"
    )


def test_peer_health_requires_remote_recovery() -> None:
    """A restored local link does not prove the peer's failed NI recovered."""
    expected = ("172.18.100.10@tcp0", "172.18.100.11@tcp0")
    entries = [
        {"nid": nid, "health stats": {"health value": 1000}} for nid in expected
    ]
    data = {"peer": [{"peer ni": entries}]}
    assert peer_rails_healthy(yaml.safe_dump(data), expected)
    entries[1]["health stats"]["health value"] = 900
    assert not peer_rails_healthy(yaml.safe_dump(data), expected)
    del entries[1]["health stats"]
    with pytest.raises(ValueError):
        peer_rails_healthy(yaml.safe_dump(data), expected)


@pytest.mark.parametrize(
    "text,stopped",
    [("bulk [0] is stopped\n", True), ("bulk [0] is running\n", False)],
)
def test_selftest_status_recognizes_checked_states(
    text: str, stopped: bool
) -> None:
    """Successful command status is separate from successful batch completion."""
    assert batch_stopped(text) is stopped


@pytest.mark.parametrize(
    "text",
    [
        "",
        "bulk [0] 1 of 1 nodes are unknown, is stopped",
        "stopped: 0, running: 0, failed: 1",
    ],
)
def test_selftest_status_rejects_unknown_failed_or_empty_output(
    text: str,
) -> None:
    """lst query's zero exit status cannot conceal a failed node."""
    with pytest.raises(AssertionError):
        batch_stopped(text)
