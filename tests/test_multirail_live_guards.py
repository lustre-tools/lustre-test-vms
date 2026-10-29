"""Pure checks for the live suite's ownership and prerequisite guardrails."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.test_e2e.multirail_live import ROOT, Guest, make_lab, unavailable
from tests.test_e2e.multirail_payload import lnet_selftest
from tests.test_parser_coverage import ltvm


@pytest.fixture(autouse=True)
def default_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unrelated guard tests independent from a caller's transport choice."""
    monkeypatch.delenv("LTVM_MULTIRAIL_TRANSPORT", raising=False)


def _guest() -> Guest:
    """Provide an owned test record without reading any live VM state."""
    return Guest(
        "e2e-mr-unit",
        ("172.18.100.10", "172.18.100.11"),
        ("fd17::10", "fd17::11"),
        ("172.18.100.10@tcp0", "172.18.100.11@tcp0"),
        ("test-tap1", "test-tap2"),
        {},
    )


def test_missing_environment_is_an_explicit_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary tests must state why live validation could not run."""
    monkeypatch.delenv("LTVM_REQUIRE_MULTIRAIL_E2E", raising=False)
    with pytest.raises(
        pytest.skip.Exception, match="environment unavailable: missing KVM"
    ):
        unavailable("missing KVM")


def test_required_environment_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CI gate cannot pass because every live case was skipped."""
    monkeypatch.setenv("LTVM_REQUIRE_MULTIRAIL_E2E", "1")
    with pytest.raises(pytest.fail.Exception, match="required Multi-Rail"):
        unavailable("missing KVM")


def test_exec_uses_this_checkout_and_bounded_consumer_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check the complete outgoing command, with no external process started."""
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
    monkeypatch.setattr(subprocess, "run", runner)
    lab = make_lab("shared", "ipv4", tmp_path)
    guest = _guest()
    lab.guests.append(guest)
    assert lab.run(guest, "printf ok", timeout=7).stdout == "ok"
    assert runner.call_args.args == (
        [
            sys.executable,
            str(ROOT / "ltvm"),
            "vm",
            "exec",
            "--timeout",
            "7",
            guest.name,
            "printf ok",
        ],
    )
    assert runner.call_args.kwargs == {
        "cwd": ROOT,
        "env": lab.env,
        "capture_output": True,
        "text": True,
        "timeout": 17,
    }
    parsed = ltvm.build_parser().parse_args(runner.call_args.args[0][2:])
    assert parsed.func.__name__ == "cmd_exec"
    assert parsed.command == "vm"
    assert (parsed.name, parsed.remote_command, parsed.timeout) == (
        guest.name,
        ["printf ok"],
        7,
    )


@pytest.mark.parametrize("status", (1, 2, 124, 255))
@pytest.mark.parametrize("selftest", (False, True))
def test_guest_probe_execution_error_never_skips(
    status: int,
    selftest: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed command cannot be mislabeled as a missing guest dependency."""
    monkeypatch.delenv("LTVM_REQUIRE_MULTIRAIL_E2E", raising=False)
    lab = make_lab("shared", "ipv4", tmp_path)
    guest = _guest()
    lab.guests.append(guest)
    monkeypatch.setattr(
        lab,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess([], status, "", "broken")
        ),
    )
    with pytest.raises(
        pytest.fail.Exception, match="prerequisite probe failed"
    ):
        if selftest:
            # Optional payload checks keep the same strict execution boundary.
            lnet_selftest(lab)
        else:
            lab.configure(guest)


@pytest.mark.parametrize("required", (False, True))
@pytest.mark.parametrize("selftest", (False, True))
def test_only_explicit_guest_dependency_status_qualifies(
    required: bool,
    selftest: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status 69 means a missing dependency; required mode still fails it."""
    monkeypatch.setenv("LTVM_REQUIRE_MULTIRAIL_E2E", "1" if required else "0")
    lab = make_lab("shared", "ipv4", tmp_path)
    guest = _guest()
    lab.guests.append(guest)
    monkeypatch.setattr(
        lab,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 69, "", "missing")),
    )
    expected = pytest.fail.Exception if required else pytest.skip.Exception
    with pytest.raises(expected, match="unavailable"):
        if selftest:
            # Both prerequisite probes use the same explicit missing status.
            lnet_selftest(lab)
        else:
            lab.configure(guest)


@pytest.mark.parametrize("index", (0, 3, -1))
def test_qmp_fault_rejects_management_and_unknown_interfaces(
    index: int, tmp_path: Path
) -> None:
    """Reject invalid indices before importing or contacting the QMP client."""
    lab = make_lab("shared", "ipv4", tmp_path)
    guest = _guest()
    lab.guests.append(guest)
    with pytest.raises(AssertionError):
        lab.link(guest, index, up=False)


def test_unowned_guest_cannot_be_executed_or_faulted(tmp_path: Path) -> None:
    """No VM outside the fixture may be addressed through its helper."""
    lab = make_lab("shared", "ipv4", tmp_path)
    with pytest.raises(AssertionError):
        lab.run(_guest(), "true")
    with pytest.raises(AssertionError):
        lab.link(_guest(), 1, up=False)


@pytest.mark.parametrize("index", (1, 2))
def test_qmp_fault_targets_only_named_data_backend(
    index: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check the full QMP request without opening a socket or touching a VM."""
    qmp = Mock(return_value={})
    monkeypatch.setattr("ltvm_pkg.vm_commands.qmp_execute", qmp)
    lab = make_lab("shared", "ipv4", tmp_path)
    guest = _guest()
    lab.guests.append(guest)
    lab.link(guest, index, up=False)
    qmp.assert_called_once_with(
        lab.sockets / f"{guest.name}.qmp",
        "set_link",
        {"name": f"net{index}", "up": False},
    )


def test_cli_failure_cannot_be_converted_to_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feature failures remain failures after environmental qualification."""
    runner = Mock(
        return_value=subprocess.CompletedProcess([], 1, "", "broken feature")
    )
    monkeypatch.setattr(subprocess, "run", runner)
    lab = make_lab("shared", "ipv4", tmp_path)
    with pytest.raises(pytest.fail.Exception, match="broken feature"):
        lab.cli("create", "e2e-mr-unit")


def test_timeout_preserves_partial_guest_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked later probe must not erase earlier kernel or worker output."""
    runner = Mock(
        side_effect=subprocess.TimeoutExpired(
            "guest probe",
            10,
            output=b"worker blocked\xff",
            stderr=b"probe error",
        )
    )
    monkeypatch.setattr(subprocess, "run", runner)
    lab = make_lab("shared", "ipv4", tmp_path)
    with pytest.raises(subprocess.TimeoutExpired):
        lab.cli("vm", "exec", "--timeout", "10", "e2e-mr-unit", "probe")
    evidence = (tmp_path / "0001.txt").read_text()
    assert "worker blocked" in evidence and "probe error" in evidence
