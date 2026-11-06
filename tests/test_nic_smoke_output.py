"""Regression tests for iproute2 output read by the NIC smoke test."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tests.test_e2e import test_08_nic_options as smoke

# These route fields were returned by the guest's IPv4 and IPv6 lookups.
# The test keeps the output shapes and uses documentation addresses.
_ROUTES = [
    (
        "192.0.2.10",
        "192.0.2.11",
        "-4",
        "from",
        {"dev": "eth1", "table": "101", "from": "192.0.2.10"},
    ),
    (
        "2001:db8:1::10",
        "2001:db8:1::11",
        "-6",
        "prefsrc",
        {"dev": "eth1", "table": "101", "prefsrc": "2001:db8:1::10"},
    ),
]


@pytest.mark.parametrize("source,peer,family,source_key,capture", _ROUTES)
@pytest.mark.parametrize("numeric_table", [False, True])
def test_route_check_reads_real_ipv4_and_ipv6_shapes(
    source: str,
    peer: str,
    family: str,
    source_key: str,
    capture: dict[str, str],
    numeric_table: bool,
) -> None:
    """Both source field names and numeric table encodings are valid."""
    route: dict[str, str | int] = dict(capture)
    route["table"] = 101 if numeric_table else "101"
    with patch.object(
        smoke, "ssh_run", return_value=(0, json.dumps([route]), "")
    ) as ssh:
        smoke._check_route("co1-client", 1, source, peer)
    ssh.assert_called_once_with(
        "co1-client",
        f"ip -j {family} route get {peer} from {source}",
        timeout=30,
    )
    assert capture[source_key] == source


@pytest.mark.parametrize("source,peer,family,source_key,capture", _ROUTES)
@pytest.mark.parametrize("bad_field", ["dev", "table", "source"])
def test_route_check_rejects_wrong_interface_table_or_source(
    source: str,
    peer: str,
    family: str,
    source_key: str,
    capture: dict[str, str],
    bad_field: str,
) -> None:
    """A partial match cannot hide a wrong source route on either family."""
    route = dict(capture)
    key = source_key if bad_field == "source" else bad_field
    wrong = {"dev": "eth2", "table": "102", "source": peer}
    route[key] = wrong[bad_field]
    with (
        patch.object(
            smoke, "ssh_run", return_value=(0, json.dumps([route]), "")
        ),
        pytest.raises(AssertionError),
    ):
        smoke._check_route("co1-client", 1, source, peer)
