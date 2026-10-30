"""Verify LST group acceptance without starting guest commands or payloads."""

from __future__ import annotations

import shlex
import subprocess
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.test_e2e import multirail_payload
from tests.test_e2e.multirail_live import Guest, Lab, make_lab
from tests.test_e2e.multirail_payload import (
    _add_selftest_group,
    _lst,
    lnet_selftest,
)


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    """Use two owned records and reject any unmocked external operation."""
    monkeypatch.setenv("LTVM_MULTIRAIL_TRANSPORT", "tcp")
    lab = make_lab("shared", "ipv6", tmp_path)
    for index in (1, 2):
        lab.guests.append(
            Guest(
                f"e2e-mr-{index}",
                (f"192.0.2.{index}", f"192.0.2.{index + 10}"),
                (f"2001:db8::{index}", f"2001:db8::{index + 10}"),
                (f"2001:db8::{index}@tcp0", f"2001:db8::{index + 10}@tcp0"),
                (f"test-{index}-1", f"test-{index}-2"),
                {},
            )
        )
    monkeypatch.setattr(
        lab, "run", Mock(side_effect=AssertionError("unmocked guest"))
    )
    return lab


def _rejection(
    nid: str, status: int = 0, group: str = "source"
) -> subprocess.CompletedProcess[str]:
    """Match the legacy add_group parser error and successful empty-group deletion."""
    error = (
        f"Invalid nid: {nid}\nIgnore invalid id list {nid}\n"
        f"No nodes added successfully, deleting group {group}\n"
    )
    return subprocess.CompletedProcess([], status, "Group is deleted\n", error)


@pytest.mark.parametrize("status", (0, 255))
def test_exact_ipv6_group_rejection_is_reported_even_with_success_status(
    status: int, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero deletion status must not hide this specific unsupported parser path."""
    nid = lab.guests[1].nids[0]
    monkeypatch.setattr(lab, "run", Mock(return_value=_rejection(nid, status)))
    with pytest.raises(pytest.xfail.Exception, match="rejects IPv6 group NIDs"):
        _add_selftest_group(lab, "123", "source", nid)


def test_invalid_ipv4_group_is_a_failure(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid IPv4 NID is not excused as an IPv6 upstream limitation."""
    lab.family = "ipv4"
    nid = "192.0.2.10@tcp0"
    monkeypatch.setattr(lab, "run", Mock(return_value=_rejection(nid)))
    with pytest.raises(pytest.fail.Exception, match="setup failed"):
        _add_selftest_group(lab, "123", "source", nid)


@pytest.mark.parametrize("status", (0, 255))
def test_missing_group_error_is_never_classified_as_ipv6_unsupported(
    status: int, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Later batch setup errors cannot stand in for the exact NID parser evidence."""
    response = subprocess.CompletedProcess(
        [], status, "", "Reader error: No groups found"
    )
    monkeypatch.setattr(lab, "run", Mock(return_value=response))
    with pytest.raises(pytest.fail.Exception):
        _add_selftest_group(lab, "123", "source", lab.guests[1].nids[0])


@pytest.mark.parametrize("network", ("tcp", "o2ib"))
def test_successful_group_is_queried_for_real_active_membership(
    network: str, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check the full add/query request and normalize each transport's zero ID."""
    nid = f"192.0.2.10@{network}0"
    added = subprocess.CompletedProcess(
        [], 0, f"{nid} are added to session\n", ""
    )
    path = Path(__file__).parent / "fixtures" / "multirail"
    report = (path / f"lst-group-netlink-{network}.txt").read_text()
    runner = Mock(
        side_effect=[added, subprocess.CompletedProcess([], 0, report, "")]
    )
    monkeypatch.setattr(lab, "run", runner)
    _add_selftest_group(lab, "123", "source", nid)
    assert runner.call_args_list == [
        (
            (lab.guests[1], f"env LST_SESSION=123 lst add_group source {nid}"),
            {"timeout": 20, "check": False},
        ),
        (
            (lab.guests[1], "env LST_SESSION=123 lst list_group --all source"),
            {"timeout": 20, "check": False},
        ),
    ]


def test_success_text_cannot_hide_an_empty_group(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A positive add message still requires the requested node in the readback."""
    nid = lab.guests[1].nids[0]
    runner = Mock(
        side_effect=[
            subprocess.CompletedProcess(
                [], 0, f"{nid} are added to session\n", ""
            ),
            subprocess.CompletedProcess([], 0, "Total 0 groups\n", ""),
        ]
    )
    monkeypatch.setattr(lab, "run", runner)
    with pytest.raises(AssertionError, match="expected active node"):
        _add_selftest_group(lab, "123", "source", nid)


def test_unsupported_group_still_ends_its_own_session(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expected upstream limit must leave neither a session nor a payload batch."""
    commands = []

    def run(
        guest: Guest, command: str, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        """Model actual probes and LST calls, with no external operations."""
        commands.append(shlex.split(command))
        if "lst add_group source" in command:
            # The controller's local node uses loopback even in an IPv6 test.
            assert shlex.split(command)[-1] == "0@lo"
            return subprocess.CompletedProcess(
                [], 0, "0@lo are added to session\n", ""
            )
        if "lst list_group --all source" in command:
            return subprocess.CompletedProcess(
                [],
                0,
                "Group [ source ]\n12345-0@lo: Active\nTotal 1 nodes [ source ]\n",
                "",
            )
        if "lst add_group sink" in command:
            # Only the remote IPv6 NID can produce the expected parser limit.
            assert shlex.split(command)[-1] == lab.guests[0].nids[0]
            return _rejection(lab.guests[0].nids[0], group="sink")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(lab, "run", Mock(side_effect=run))
    with pytest.raises(pytest.xfail.Exception):
        lnet_selftest(lab)
    assert commands[-1][-1] == "end_session"
    assert not any("add_batch" in command for command in commands)


@pytest.mark.parametrize("status", (0, 255))
def test_later_lst_failure_remains_a_failure(
    status: int, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only add_group's exact IPv6 parse path can become an expected failure."""
    monkeypatch.setattr(
        lab, "run", Mock(return_value=_rejection(lab.guests[1].nids[0], status))
    )
    with pytest.raises(pytest.fail.Exception, match="setup failed"):
        _lst(lab, "123", ["add_test", "brw"])


def test_local_control_keeps_the_remote_payload_node(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check complete control requests and retain the other VM's real data NID."""
    requests = []
    remote = lab.guests[0].nids[0]

    def run(
        guest: Guest, command: str, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        """Answer the two exact group queries without starting guest processes."""
        words = shlex.split(command)
        output = ""
        if words[0] == "env":
            # All session commands execute on the same local controller.
            assert guest == lab.guests[1]
            assert kwargs == {"timeout": 20, "check": False}
            requests.append(words)
            if words[3] == "add_group":
                output = f"{words[5]} are added to session\n"
            elif words[3] == "list_group":
                group = words[5]
                nid = "0@lo" if group == "source" else remote
                output = f"Group [ {group} ]\n12345-{nid}: Active\nTotal 1 nodes [ {group} ]\n"
        return subprocess.CompletedProcess([], 0, output, "")

    batch = Mock()
    monkeypatch.setattr(lab, "run", Mock(side_effect=run))
    monkeypatch.setattr(multirail_payload, "_run_batch", batch)
    monkeypatch.setattr(
        multirail_payload.uuid, "uuid4", lambda: uuid.UUID(int=1)
    )
    lnet_selftest(lab)
    assert requests == [
        ["env", "LST_SESSION=2", "lst", *arguments]
        for arguments in (
            ["new_session", "mr-2"],
            ["add_group", "sink", remote],
            ["list_group", "--all", "sink"],
            ["add_group", "source", "0@lo"],
            ["list_group", "--all", "source"],
            ["end_session"],
        )
    ]
    assert batch.call_args_list == [
        ((lab, "2", "write"), {}),
        ((lab, "2", "read"), {}),
    ]
