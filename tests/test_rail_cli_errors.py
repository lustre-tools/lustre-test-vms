"""Checked CLI failures after profile validation and launch cleanup."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from ltvm_pkg import vm_commands, vm_state
from ltvm_pkg.cli import cluster as cluster_cli
from ltvm_pkg.cli import vm as vm_cli
from ltvm_pkg.rail_image import write_capability
from ltvm_pkg.rail_topology import resolve_topology
from ltvm_pkg.vm_state import VMInfo
from tests.test_parser_coverage import ltvm


@pytest.fixture
def profile_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[argparse.Namespace, VMInfo]:
    """Reach the real create rollback block without disks or host operations."""
    args = ltvm.build_parser().parse_args(
        ["create", "co1-errors", "--nic", "tcp", "--nic", "tcp", "-T", "shared"]
    )
    monkeypatch.setattr(vm_commands.sys, "platform", "linux")
    monkeypatch.setattr(vm_commands, "SOCKETS", tmp_path)
    monkeypatch.setattr(vm_state, "SUBNET", "192.168.100")
    image = tmp_path / "base.ext4"
    image.touch()
    write_capability(image)
    stages = {
        "_handle_existing_vm": False,
        "_validate_create_bounds": (["tcp", "tcp"], []),
        "_validate_failover_disks": None,
        "_validate_disk_options": None,
        "check_tap_collision": None,
        "_check_root_size_fits": None,
        "_resolve_os_and_kernel": (
            None,
            str(image),
            "vmlinuz",
            "test",
            "rocky9",
            "base",
        ),
    }
    for name, result in stages.items():
        # Each stub replaces one earlier boundary; rollback remains real control flow.
        monkeypatch.setattr(vm_commands, name, MagicMock(return_value=result))
    vm = VMInfo(
        args.name,
        "192.168.100.20",
        nics=["tcp", "tcp"],
        rail_topology=resolve_topology(["tcp", "tcp"], "shared"),
    )
    monkeypatch.setattr(
        vm_commands, "_allocate_and_persist_vm", MagicMock(return_value=vm)
    )
    return args, vm


@pytest.mark.parametrize("use_json", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("managed data bridge missing: fcbr-data"),
        OSError("cannot inspect bridge addresses"),
    ],
)
def test_create_reports_host_failure_after_rollback(
    profile_create: tuple[argparse.Namespace, VMInfo],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    use_json: bool,
) -> None:
    """Expected bridge errors leave through the CLI only after launch rollback."""
    args, vm = profile_create
    args.json = use_json
    launch = MagicMock(side_effect=failure)
    rollback = MagicMock()
    order = MagicMock()
    order.attach_mock(launch, "launch")
    order.attach_mock(rollback, "rollback")
    monkeypatch.setattr(vm_commands, "_launch_and_wait", launch)
    monkeypatch.setattr(vm_commands, "_rollback_launch_failure", rollback)
    assert vm_cli._vm_call(vm_commands.cmd_create, args, use_json) == 1
    assert order.mock_calls == [
        call.launch(vm, [], args.wait),
        call.rollback(vm),
    ]
    output = capsys.readouterr()
    assert "Traceback" not in output.err
    if use_json:
        # JSON failures remain parseable by callers, without a partial report.
        assert json.loads(output.err) == {"error": str(failure)}
        assert output.out == ""
    else:
        # Human output carries the actionable host error.
        assert output.err == f"error: {failure}\n"


@pytest.mark.parametrize("wrapper", [vm_cli._vm_call, cluster_cli._call])
def test_invalid_saved_profile_has_a_checked_cli_error(
    wrapper: Callable[[MagicMock, argparse.Namespace, bool], int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Invalid persisted metadata cannot escape as an ordinary CLI traceback."""
    handler = MagicMock(
        side_effect=ValueError("invalid rail topology addresses")
    )
    assert wrapper(handler, argparse.Namespace(), True) == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "invalid rail topology addresses"
    }


@pytest.mark.parametrize("wrapper", [vm_cli._vm_call, cluster_cli._call])
def test_programming_errors_are_not_hidden(
    wrapper: Callable[[MagicMock, argparse.Namespace, bool], int],
) -> None:
    """The operational boundary must preserve unexpected implementation failures."""
    handler = MagicMock(side_effect=TypeError("invalid internal call"))
    with pytest.raises(TypeError, match="invalid internal call"):
        wrapper(handler, argparse.Namespace(), False)
