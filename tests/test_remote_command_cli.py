"""Run main dispatch with remote argv that must not replace its command key."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock

import pytest

from ltvm_pkg import telemetry, update_check, vm_claim, vm_commands, vm_state
from tests.test_parser_coverage import ltvm


@pytest.fixture
def quiet_main(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Keep real parsing and dispatch while replacing unrelated background work."""
    monkeypatch.setattr(update_check, "maybe_check_for_updates", MagicMock())
    record = MagicMock()
    monkeypatch.setattr(telemetry, "record", record)
    monkeypatch.setattr(telemetry, "maybe_send", MagicMock())
    return record


def test_vm_exec_reaches_ssh_and_preserves_dispatch_key(
    quiet_main: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A native VM exec reaches the exact SSH boundary and returns its status."""
    words = ["echo", "a b", "--mode=value"]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ltvm",
            "vm",
            "exec",
            "--json",
            "--timeout",
            "7",
            "co1-remote",
            *words,
        ],
    )
    vm = vm_state.VMInfo("co1-remote", "192.168.100.20")
    monkeypatch.setattr(vm_state.VMInfo, "load", MagicMock(return_value=vm))
    monkeypatch.setattr(vm_claim, "require", MagicMock())
    monkeypatch.setattr(vm_commands, "is_running", MagicMock(return_value=True))
    ssh = MagicMock(
        return_value=MagicMock(returncode=7, stdout="guest\n", stderr="")
    )
    monkeypatch.setattr(vm_commands, "run_ssh", ssh)
    assert ltvm.main() == 7
    ssh.assert_called_once_with(
        "192.168.100.20", "echo 'a b' --mode=value", timeout=7, stream=None
    )
    recorded = quiet_main.call_args.args[0]
    assert recorded.command == "vm"
    assert recorded.remote_command == words
    report = json.loads(capsys.readouterr().out)
    assert report["rc"] == 7 and report["stdout"] == "guest\n"


@pytest.mark.parametrize(
    "action,words",
    [("exec", ["lctl", "dl", "-t"]), ("ssh", ["printf", "a b"]), ("ssh", [])],
)
def test_cluster_remote_argv_keeps_main_command(
    quiet_main: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    words: list[str],
) -> None:
    """Cluster adapters pass guest argv without changing dispatch or telemetry."""
    from ltvm_pkg import vm_cluster

    handler = MagicMock(return_value=0)
    monkeypatch.setattr(vm_cluster, f"cmd_cluster_{action}", handler)
    monkeypatch.setattr(
        sys, "argv", ["ltvm", "cluster", action, "co1-lab", "oss", *words]
    )
    assert ltvm.main() == 0
    forwarded = handler.call_args.args[0]
    assert (forwarded.name, forwarded.target, forwarded.command) == (
        "co1-lab",
        "oss",
        words,
    )
    recorded = quiet_main.call_args.args[0]
    assert recorded.command == "cluster"
    assert recorded.remote_command == words


def test_empty_vm_exec_reports_its_own_error(
    quiet_main: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty guest command is checked by the handler after valid dispatch."""
    monkeypatch.setattr(sys, "argv", ["ltvm", "vm", "exec", "co1-remote"])
    ssh = MagicMock()
    monkeypatch.setattr(vm_commands, "run_ssh", ssh)
    assert ltvm.main() == 1
    assert "requires a command" in capsys.readouterr().err
    ssh.assert_not_called()
