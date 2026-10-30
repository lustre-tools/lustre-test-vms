"""Check worker completion deadlines without waiting or running guest commands."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.test_e2e import multirail_payload as payload
from tests.test_e2e.multirail_live import Guest, Lab, make_lab


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    """Create only the client record needed by the worker command boundary."""
    monkeypatch.setenv("LTVM_MULTIRAIL_TRANSPORT", "tcp")
    lab = make_lab("shared", "ipv6", tmp_path)
    guest = Guest(
        "e2e-mr-unit",
        ("192.0.2.10", "192.0.2.11"),
        ("2001:db8::10", "2001:db8::11"),
        ("2001:db8::10@tcp0", "2001:db8::11@tcp0"),
        ("mr-unit-1", "mr-unit-2"),
        {},
    )
    lab.guests = [guest, guest]
    monkeypatch.setattr(payload.time, "sleep", Mock())
    return lab


def _result(text: str) -> subprocess.CompletedProcess[str]:
    """Keep all model commands successful; worker status is separate output."""
    return subprocess.CompletedProcess([], 0, text, "")


def test_graceful_stop_allows_rpc_completion_after_thirty_seconds(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Active-I/O restoration can leave an RPC waiting beyond the old stop limit."""
    monkeypatch.setattr(payload.time, "monotonic", Mock(side_effect=range(100)))
    runner = Mock(
        side_effect=[_result(""), *[_result("running")] * 31, _result("0")]
    )
    monkeypatch.setattr(lab, "run", runner)
    payload.stop_worker(lab)
    assert runner.call_count == 33
    assert runner.call_args_list[0].args == (lab.guests[1], ": > /root/mr-stop")
    assert all(call.kwargs == {"timeout": 10} for call in runner.call_args_list)
    readback = runner.call_args.args[1]
    assert "IFS= read -r status < /root/mr-worker-exit" in readback
    assert payload.time.sleep.call_count == 31


def test_graceful_stop_cannot_exceed_the_filesystem_recovery_deadline(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cap the last guest read by the remaining time and fail at 180 seconds."""
    monkeypatch.setattr(
        payload.time, "monotonic", Mock(side_effect=[0, 179, 180])
    )
    runner = Mock(side_effect=[_result(""), _result("running")])
    monkeypatch.setattr(lab, "run", runner)
    with pytest.raises(pytest.fail.Exception, match="failed to stop in 180s"):
        payload.stop_worker(lab)
    assert runner.call_count == 2
    assert runner.call_args.kwargs == {"timeout": 1}
    payload.time.sleep.assert_called_once_with(1)


def test_nonzero_worker_status_fails_without_a_recovery_wait(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt payload or failed copy cannot use the recovery wait as a retry."""
    monkeypatch.setattr(payload.time, "monotonic", Mock(side_effect=[0, 1]))
    runner = Mock(
        side_effect=[_result(""), _result("37"), _result("cmp failed")]
    )
    monkeypatch.setattr(lab, "run", runner)
    with pytest.raises(AssertionError, match="cmp failed"):
        payload.stop_worker(lab)
    assert runner.call_count == 3
    payload.time.sleep.assert_not_called()
