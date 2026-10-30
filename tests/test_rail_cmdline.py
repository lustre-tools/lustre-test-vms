"""Full eight-rail command lines respect the supported kernels' byte limit."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ltvm_pkg import qemu_run, rail_host, vm_commands
from ltvm_pkg.rail_topology import resolve_topology
from ltvm_pkg.vm_net import mac_for_name, tap_for_name
from ltvm_pkg.vm_state import VMInfo
from tests.test_rail_qemu import (
    _flag_values,
    _LaunchHarness,
    _run_launch,
    _vm,
)
from tests.test_rail_qemu import (
    launch_dir as _launch_dir,
)


@pytest.fixture
def rail_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Reuse the existing network isolation fixture without importing its tests."""
    return _launch_dir.__wrapped__(tmp_path, monkeypatch)


def _maximum_vm(directory: Path, arch: str, kind: str) -> VMInfo:
    """Use the longest valid hostname and LNet IDs with every full IPv6 NID."""
    vm = _vm(directory, "isolated", count=8, kind=kind)
    vm.name = "co1-" + "x" * 59
    vm.tap = tap_for_name(vm.name)
    vm.mac = mac_for_name(vm.name)
    vm.overlay_path.touch()
    vm.arch = arch
    transport = "tcp" if kind == "tcp" else "o2ib"
    vm.rail_topology = resolve_topology(
        vm.nics,
        "isolated",
        [f"{transport}{65528 + index}" for index in range(8)],
    )
    return vm


def _commandline(vm: VMInfo) -> str:
    """Capture the actual complete QEMU append value without launching a child."""
    harness = _LaunchHarness()
    _run_launch(vm, harness)
    assert harness.qemu_argv is not None
    return _flag_values(harness.qemu_argv, "-append")[0]


def _assert_no_tap_mutation() -> None:
    """A rejected parameter must not enter either API or legacy shell TAP setup."""
    rail_host.prepare_taps.assert_not_called()
    rail_host.remove_taps.assert_not_called()
    qemu_run.sudo_run.assert_not_called()


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
@pytest.mark.parametrize("kind", ["tcp", "softroce"])
def test_eight_ipv6_rails_with_max_user_characters(
    rail_dir: Path, monkeypatch: pytest.MonkeyPatch, arch: str, kind: str
) -> None:
    """Maximal user input either fits in full or is refused before root TAP work."""
    vm = _maximum_vm(rail_dir, arch, kind)
    before = _commandline(vm)
    vm.kernel_args = vm_commands._validate_kernel_args("x=" + "a" * 1022)
    expected = before + " " + vm.kernel_args
    monkeypatch.setattr(qemu_run.os, "geteuid", MagicMock(return_value=0))
    if len(expected.encode()) > qemu_run.PROFILE_CMDLINE_MAX_BYTES:
        # x86 also carries the NMI panic parameters, reducing available space.
        with pytest.raises(SystemExit):
            _commandline(vm)
        _assert_no_tap_mutation()
    else:
        # A fitting ARM64 request retains the complete user and rail arguments.
        assert _commandline(vm) == expected
        assert len(expected.encode()) <= 2047
        for ipv6 in vm.nic_ip6s:
            assert ipv6 in expected


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_exact_byte_boundary_and_one_byte_over(
    rail_dir: Path, monkeypatch: pytest.MonkeyPatch, arch: str
) -> None:
    """The terminating NUL has space at 2047 bytes; 2048 is refused before TAP changes."""
    vm = _maximum_vm(rail_dir, arch, "tcp")
    before = _commandline(vm)
    remaining = 2047 - len(before.encode()) - 1
    payload = remaining - len("x=")
    vm.kernel_args = vm_commands._validate_kernel_args(
        "x=" + "é" * (payload // 2) + "a" * (payload % 2)
    )
    assert len(vm.kernel_args) <= vm_commands.KERNEL_ARGS_MAX
    accepted = _commandline(vm)
    assert accepted == before + " " + vm.kernel_args
    assert len(accepted.encode()) == 2047
    vm.kernel_args += "a"
    monkeypatch.setattr(qemu_run.os, "geteuid", MagicMock(return_value=0))
    with pytest.raises(SystemExit):
        _commandline(vm)
    _assert_no_tap_mutation()


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_max_multibyte_user_characters_are_checked_as_bytes(
    rail_dir: Path, monkeypatch: pytest.MonkeyPatch, arch: str
) -> None:
    """A character limit cannot replace the kernel's UTF-8 byte limit."""
    vm = _maximum_vm(rail_dir, arch, "tcp")
    vm.kernel_args = vm_commands._validate_kernel_args("x=" + "é" * 1022)
    assert len(vm.kernel_args) == vm_commands.KERNEL_ARGS_MAX
    monkeypatch.setattr(qemu_run.os, "geteuid", MagicMock(return_value=0))
    with pytest.raises(SystemExit):
        _commandline(vm)
    _assert_no_tap_mutation()
