"""Exact network requests and failure boundaries for QEMU rail profiles."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from ltvm_pkg import qemu_run, rail_host, rootless, vm_state
from ltvm_pkg.rail_image import capability_path, write_capability
from ltvm_pkg.rail_topology import resolve_topology
from ltvm_pkg.vm_state import VMInfo
from tests.test_qemu_run import _LaunchHarness, _make_vm, _run_launch
from tests.test_rail_host import FakeIPRoute

HELPER = Path("/fake/qemu-bridge-helper")
_PREPARE_TAPS = rail_host.prepare_taps
_REMOVE_TAPS = rail_host.remove_taps
_REQUIRE_BRIDGES = rail_host.require_bridges


@pytest.fixture
def launch_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep launch files local and replace every privilege and network action."""
    sockets = tmp_path / "sockets"
    overlays = tmp_path / "overlays"
    sockets.mkdir()
    overlays.mkdir()
    monkeypatch.setattr(vm_state, "VM_DIR", tmp_path)
    monkeypatch.setattr(vm_state, "SOCKETS", sockets)
    monkeypatch.setattr(vm_state, "OVERLAYS", overlays)
    monkeypatch.setattr(qemu_run, "GATEWAY", "192.168.100.1")
    monkeypatch.setattr(qemu_run, "is_macos", MagicMock(return_value=False))
    monkeypatch.setattr(
        qemu_run, "sudo_run", MagicMock(return_value=MagicMock(returncode=0))
    )
    monkeypatch.setattr(qemu_run, "invoking_user", MagicMock(return_value=None))
    monkeypatch.setattr(qemu_run.os, "geteuid", MagicMock(return_value=1000))
    monkeypatch.setattr(
        rootless,
        "readiness",
        MagicMock(return_value=rootless.Readiness(helper=HELPER)),
    )
    monkeypatch.setattr(
        rootless, "bridge_allowed", MagicMock(return_value=True)
    )
    monkeypatch.setattr(rail_host, "require_bridges", MagicMock())
    monkeypatch.setattr(rail_host, "prepare_taps", MagicMock())
    monkeypatch.setattr(rail_host, "remove_taps", MagicMock())
    return tmp_path


def _vm(
    directory: Path, mode: str, *, count: int = 2, kind: str = "tcp"
) -> VMInfo:
    """Use real VM paths and complete paired source addresses for each rail."""
    vm = _make_vm(directory, name="co1-rails")
    vm.image = str(directory / "base.ext4")
    Path(vm.image).touch()
    write_capability(Path(vm.image))
    vm.nics = [kind] * count
    topology = resolve_topology(vm.nics, mode)
    assert topology is not None
    vm.rail_topology = topology
    vm.nic_ips = [
        rail.subnet4.rsplit(".", 1)[0] + f".{30 + index}"
        for index, rail in enumerate(topology.rails)
    ]
    vm.nic_ip6s = [
        rail.ipv6_for(ip) for rail, ip in zip(topology.rails, vm.nic_ips)
    ]
    return vm


def _flag_values(arguments: list[str], flag: str) -> list[str]:
    """Read repeated QEMU options without searching fragments of a joined string."""
    return [
        arguments[index + 1]
        for index, value in enumerate(arguments[:-1])
        if value == flag
    ]


@pytest.mark.parametrize("mode", ["shared", "isolated"])
@pytest.mark.parametrize("kind", ["tcp", "softroce"])
def test_exact_qemu_network_and_guest_contract(
    launch_dir: Path, mode: str, kind: str
) -> None:
    """QEMU receives each selected bridge and every paired guest parameter exactly once."""
    vm = _vm(launch_dir, mode, kind=kind)
    harness = _LaunchHarness()
    _run_launch(vm, harness)
    assert harness.qemu_argv is not None
    assert vm.rail_topology is not None
    topology = vm.rail_topology
    bridges = ["fcbr0", *(rail.bridge for rail in topology.rails)]
    assert _flag_values(harness.qemu_argv, "-netdev") == [
        f"bridge,id=net{index},br={bridge},helper={HELPER}"
        for index, bridge in enumerate(bridges)
    ]
    devices = [
        value
        for value in _flag_values(harness.qemu_argv, "-device")
        if value.startswith("virtio-net-pci,")
    ]
    macs = [vm.mac, *(entry[3] for entry in vm.extra_nics())]
    assert devices == [
        f"virtio-net-pci,netdev=net{index},mac={mac}"
        for index, mac in enumerate(macs)
    ]
    assert _flag_values(harness.qemu_argv, "-append") == [
        "console=ttyS0 reboot=k panic=1 crashkernel=512M net.ifnames=0 biosdevname=0 "
        "systemd.journald.forward_to_console=1 systemd.log_target=console "
        f"root=/dev/vda rw fc_ip={vm.ip} fc_gw=192.168.100.1 fc_name={vm.name} "
        f"fc_nics={kind},{kind} fc_nic_ips={','.join(vm.nic_ips)} fc_nic_prefixes=24,24 "
        f"fc_nic_ip6s={','.join(vm.nic_ip6s)} fc_nic_ip6_prefixes=64,64"
        + qemu_run.NMI_PANIC_ARGS
        + f" fc_rail_topology={mode} fc_lnet_nets="
        + ",".join(rail.lnet_network for rail in topology.rails)
    ]
    rail_host.require_bridges.assert_called_once_with(topology)
    rail_host.prepare_taps.assert_not_called()
    assert not any(
        call.args[0][0] == "ip" for call in qemu_run.sudo_run.call_args_list
    )


def test_legacy_vm_keeps_all_nics_on_the_existing_bridge(
    launch_dir: Path,
) -> None:
    """Omitted profiles preserve old netdev arguments and omit new kernel keys."""
    vm = _make_vm(launch_dir)
    vm.nics = ["tcp", "tcp"]
    vm.nic_ips = ["172.16.100.30", "172.16.100.31"]
    harness = _LaunchHarness()
    _run_launch(vm, harness)
    assert harness.qemu_argv is not None
    assert _flag_values(harness.qemu_argv, "-netdev") == [
        f"bridge,id=net{index},br=fcbr0,helper={HELPER}" for index in range(3)
    ]
    command_line = _flag_values(harness.qemu_argv, "-append")[0]
    assert "fc_rail_topology=" not in command_line
    assert "fc_lnet_nets=" not in command_line
    rail_host.require_bridges.assert_not_called()


def test_profile_without_helper_never_prompts_for_sudo(
    launch_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unprivileged profile launch fails before TAP creation or QEMU execution."""
    vm = _vm(launch_dir, "shared")
    monkeypatch.setattr(
        rootless,
        "readiness",
        MagicMock(return_value=rootless.Readiness(problems=["helper missing"])),
    )
    harness = _LaunchHarness()
    with pytest.raises(SystemExit):
        _run_launch(vm, harness)
    assert harness.qemu_args is None
    rail_host.prepare_taps.assert_not_called()
    rail_host.remove_taps.assert_not_called()
    qemu_run.sudo_run.assert_not_called()


def test_profile_start_rejects_old_image_before_host_checks(
    launch_dir: Path,
) -> None:
    """A saved profile cannot start against an image without guest routing support."""
    vm = _vm(launch_dir, "shared")
    capability_path(Path(vm.image)).unlink()
    harness = _LaunchHarness()
    with pytest.raises(SystemExit):
        _run_launch(vm, harness)
    assert harness.qemu_args is None
    rail_host.require_bridges.assert_not_called()
    rail_host.prepare_taps.assert_not_called()


def test_profile_acl_denial_is_not_bypassed(
    launch_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Management permission alone cannot grant attachment to another fabric."""
    vm = _vm(launch_dir, "isolated")
    allowed = MagicMock(side_effect=[True, False])
    monkeypatch.setattr(rootless, "bridge_allowed", allowed)
    harness = _LaunchHarness()
    with pytest.raises(SystemExit):
        _run_launch(vm, harness)
    assert harness.qemu_args is None
    assert [call.args[0] for call in allowed.call_args_list] == [
        "fcbr-rail1",
        "fcbr-rail2",
    ]
    rail_host.prepare_taps.assert_not_called()
    qemu_run.sudo_run.assert_not_called()


def test_missing_kernel_never_creates_profile_taps(
    launch_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-launch validation fails before a root-run VM can leave TAPs behind."""
    vm = _vm(launch_dir, "shared")
    vm.kernel = ""
    monkeypatch.setattr(qemu_run.os, "geteuid", MagicMock(return_value=0))
    harness = _LaunchHarness()
    with pytest.raises(SystemExit):
        _run_launch(vm, harness)
    assert harness.qemu_args is None
    rail_host.prepare_taps.assert_not_called()


def test_qemu_failure_rolls_back_profile_taps(
    launch_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed child launch removes the VM's TAPs through the network API."""
    vm = _vm(launch_dir, "shared")
    monkeypatch.setattr(qemu_run.os, "geteuid", MagicMock(return_value=0))
    harness = _LaunchHarness()
    harness.subprocess_run = MagicMock(return_value=MagicMock(returncode=1))
    with pytest.raises(SystemExit):
        _run_launch(vm, harness)
    rail_host.prepare_taps.assert_called_once_with(vm)
    assert rail_host.remove_taps.call_args_list == [call(vm), call(vm)]
    assert not any(
        call.args[0][0] == "ip" for call in qemu_run.sudo_run.call_args_list
    )


def test_eight_rails_keep_both_address_families_within_kernel_limit(
    launch_dir: Path,
) -> None:
    """The maximum supported rail count preserves every IPv6 source address."""
    vm = _vm(launch_dir, "isolated", count=8)
    harness = _LaunchHarness()
    _run_launch(vm, harness)
    assert harness.qemu_argv is not None
    command_line = _flag_values(harness.qemu_argv, "-append")[0]
    assert len(command_line.encode()) < 2048
    fields = dict(
        word.split("=", 1) for word in command_line.split() if "=" in word
    )
    assert fields["fc_nic_ips"].split(",") == vm.nic_ips
    assert fields["fc_nic_ip6s"].split(",") == vm.nic_ip6s
    assert len(_flag_values(harness.qemu_argv, "-netdev")) == 9


def test_stored_kernel_override_cannot_replace_saved_topology(
    launch_dir: Path,
) -> None:
    """Start validates stored overrides before any host or child operation."""
    vm = _vm(launch_dir, "shared")
    vm.kernel_args = "fc_lnet_nets=tcp1,tcp2"
    harness = _LaunchHarness()
    with pytest.raises(SystemExit):
        _run_launch(vm, harness)
    assert harness.qemu_args is None
    rail_host.require_bridges.assert_not_called()
    rail_host.prepare_taps.assert_not_called()
    rail_host.remove_taps.assert_not_called()


def test_stop_uses_profile_tap_cleanup_without_shell(launch_dir: Path) -> None:
    """Stopping an already-stopped profile never falls back to an ip command."""
    vm = _vm(launch_dir, "shared")
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(qemu_run, "is_running", MagicMock(return_value=False))
        patcher.setattr(VMInfo, "update_pid", MagicMock())
        qemu_run.kill_qemu(vm)
    rail_host.remove_taps.assert_called_once_with(vm)
    qemu_run.sudo_run.assert_not_called()


def _stale_profile_network(
    directory: Path, vm: VMInfo, monkeypatch: pytest.MonkeyPatch
) -> FakeIPRoute:
    """Exercise real TAP ownership rules over a stopped VM's bounded fake devices."""
    assert vm.rail_topology is not None
    sys_net = directory / "net"
    sys_net.mkdir()
    api = FakeIPRoute(sys_net)
    bridges = ["fcbr0", *(rail.bridge for rail in vm.rail_topology.rails)]
    for bridge in dict.fromkeys(bridges):
        api.add_existing(bridge, mtu=1500 if bridge == "fcbr0" else 4200)
    taps = [vm.tap, *(entry[2] for entry in vm.extra_nics())]
    for tap, bridge in zip(taps, bridges):
        api.add_existing(tap, kind="tun", alias=rail_host.tap_owner(vm))
        api.links[tap]["IFLA_MASTER"] = api.links[bridge]["index"]
    monkeypatch.setattr(rail_host, "SYS_NET", sys_net)
    monkeypatch.setattr(rail_host, "_iproute", MagicMock(return_value=api))
    monkeypatch.setattr(rail_host, "prepare_taps", _PREPARE_TAPS)
    monkeypatch.setattr(rail_host, "remove_taps", _REMOVE_TAPS)
    monkeypatch.setattr(qemu_run.os, "geteuid", MagicMock(return_value=0))
    return api


@pytest.mark.parametrize("mode", ["shared", "isolated"])
def test_restart_replaces_only_stale_owned_taps(
    launch_dir: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """A root-run restart replaces its stale devices before the next QEMU launch."""
    vm = _vm(launch_dir, mode)
    api = _stale_profile_network(launch_dir, vm, monkeypatch)
    taps = [vm.tap, *(entry[2] for entry in vm.extra_nics())]
    original = [api.links[tap]["index"] for tap in taps]
    harness = _LaunchHarness()
    _run_launch(vm, harness)
    assert harness.qemu_args is not None
    assert api.calls[:3] == [("del", {"index": index}) for index in original]
    assert [action for action, _values in api.calls] == [
        "del",
        "del",
        "del",
        "add",
        "set",
        "set",
        "add",
        "set",
        "set",
        "add",
        "set",
        "set",
    ]
    assert all(
        api.links[tap]["IFLA_IFALIAS"] == rail_host.tap_owner(vm)
        for tap in taps
    )
    assert not any(
        entry.args[0][0] == "ip" for entry in qemu_run.sudo_run.call_args_list
    )


def test_restart_refuses_foreign_tap_before_any_deletion(
    launch_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One foreign TAP prevents removal of even the VM-owned devices in the set."""
    vm = _vm(launch_dir, "isolated")
    api = _stale_profile_network(launch_dir, vm, monkeypatch)
    foreign = vm.extra_nics()[-1][2]
    api.links[foreign]["IFLA_IFALIAS"] = "administrator"
    names = set(api.links)
    harness = _LaunchHarness()
    with pytest.raises(RuntimeError, match="unmanaged TAP"):
        _run_launch(vm, harness)
    assert harness.qemu_args is None
    assert api.calls == [] and set(api.links) == names
    qemu_run.sudo_run.assert_not_called()


@pytest.mark.parametrize("mode", ["shared", "isolated"])
def test_small_data_mtu_prevents_launch_and_stale_tap_deletion(
    launch_dir: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Real bridge preflight must fail before touching any stopped VM devices."""
    vm = _vm(launch_dir, mode, kind="softroce")
    api = _stale_profile_network(launch_dir, vm, monkeypatch)
    assert vm.rail_topology is not None
    bridge = vm.rail_topology.rails[-1].bridge
    (rail_host.SYS_NET / bridge / "mtu").write_text("1500\n")
    monkeypatch.setattr(rail_host, "require_bridges", _REQUIRE_BRIDGES)
    monkeypatch.setattr(rail_host.platform, "system", lambda: "Linux")
    before = set(api.links)
    harness = _LaunchHarness()
    with pytest.raises(RuntimeError, match="1500.*4200"):
        _run_launch(vm, harness)
    assert harness.qemu_args is None
    assert api.calls == [] and set(api.links) == before
    qemu_run.sudo_run.assert_not_called()


def test_running_vm_never_touches_profile_taps(
    launch_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process guard precedes stale-device cleanup and all host checks."""
    vm = _vm(launch_dir, "shared")
    monkeypatch.setattr(qemu_run, "is_running", MagicMock(return_value=True))
    qemu_run.launch_qemu(vm)
    rail_host.require_bridges.assert_not_called()
    rail_host.prepare_taps.assert_not_called()
    rail_host.remove_taps.assert_not_called()


def test_concurrent_start_rechecks_pidfile_under_launch_lock(
    launch_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale VM object cannot remove TAPs after another start completed."""
    vm = _vm(launch_dir, "shared")
    vm.pid = 111
    vm.pid_path.write_text("222\n")
    stale_process = MagicMock(return_value=False)
    new_process = MagicMock(return_value=True)
    monkeypatch.setattr(qemu_run, "is_running", stale_process)
    monkeypatch.setattr(qemu_run, "_qemu_pid_is", new_process)
    qemu_run.launch_qemu(vm)
    assert stale_process.call_count == 2
    new_process.assert_called_once_with(222, vm.name)
    rail_host.require_bridges.assert_not_called()
    rail_host.prepare_taps.assert_not_called()
    rail_host.remove_taps.assert_not_called()
