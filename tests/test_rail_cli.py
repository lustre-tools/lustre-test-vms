"""Native CLI boundaries for explicit virtual rail profiles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ltvm_pkg import completion, vm_cluster, vm_commands, vm_state
from ltvm_pkg.cli import cluster as cluster_cli
from ltvm_pkg.cli import setup as setup_cli
from ltvm_pkg.rail_image import (
    MAX_CAPABILITY_BYTES,
    capability_path,
    write_capability,
)
from ltvm_pkg.rail_topology import resolve_topology
from ltvm_pkg.vm_state import ClusterNode, VMInfo
from tests.test_parser_coverage import ltvm


@pytest.fixture
def linux_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise Linux validation on any test host, without network operations."""
    monkeypatch.setattr(vm_commands.sys, "platform", "linux")
    monkeypatch.setattr(vm_state, "SUBNET", "192.168.100")


def _args(*options: str, cluster: bool = False) -> argparse.Namespace:
    """Parse the real command surface, including shared VM aliases."""
    command = (
        ["cluster", "create", "co1-lab", "mgs+mds:co1-srv:1"]
        if cluster
        else ["create", "co1-vm"]
    )
    return ltvm.build_parser().parse_args(
        [*command, "--nic", "tcp", "--nic", "tcp", *options]
    )


@pytest.mark.parametrize("cluster", [False, True])
@pytest.mark.parametrize(
    "flags", [("-T", "-L"), ("--network-topology", "--lnet-network")]
)
def test_both_cli_spellings_preserve_nic_order(
    cluster: bool, flags: tuple[str, str]
) -> None:
    """Short and long forms select the same profile and independent LNet IDs."""
    mode_flag, network_flag = flags
    args = _args(
        mode_flag,
        "isolated",
        network_flag,
        "tcp7",
        network_flag,
        "tcp8",
        cluster=cluster,
    )
    assert args.network_topology == "isolated"
    assert args.lnet_networks == ["tcp7", "tcp8"]
    assert args.nic == ["tcp", "tcp"]


def test_completion_is_bounded_and_includes_both_transports() -> None:
    """LNet completion offers profile defaults without calling host tools."""
    values = completion.complete_lnet_networks()
    assert len(values) == 16
    assert "tcp0" in values and "o2ib7" in values


@pytest.mark.parametrize("cluster", [False, True])
def test_invalid_profile_fails_before_privilege_prompt(
    linux_profile: None, monkeypatch: pytest.MonkeyPatch, cluster: bool
) -> None:
    """An invalid grouping never reaches sudo, host setup, or a child process."""
    args = _args("-T", "shared", "-L", "tcp0", "-L", "tcp1", cluster=cluster)
    privilege = MagicMock(
        side_effect=AssertionError("unexpected privilege prompt")
    )
    if cluster:
        # Each wrapper has its own privilege entry point.
        monkeypatch.setattr(cluster_cli, "_cluster_privileges", privilege)
        assert cluster_cli.cmd_cluster_create(args) == 1
    else:
        monkeypatch.setattr(setup_cli, "_vm_privileges", privilege)
        assert setup_cli.cmd_create(args) == 1
    privilege.assert_not_called()


def test_non_linux_profile_fails_before_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed bridges fail clearly on unsupported hosts, including dry-run."""
    args = _args("-T", "shared", "--dry-run")
    monkeypatch.setattr(vm_commands.sys, "platform", "darwin")
    resolve = MagicMock(
        side_effect=AssertionError("unexpected artifact lookup")
    )
    monkeypatch.setattr(vm_commands, "_resolve_os_and_kernel", resolve)
    with pytest.raises(SystemExit):
        vm_commands.cmd_create(args)
    resolve.assert_not_called()


def test_profile_overlap_is_rejected_before_artifacts(
    linux_profile: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Management and data NICs cannot claim the same connected network."""
    monkeypatch.setattr(vm_state, "SUBNET", "172.18.100")
    with pytest.raises(SystemExit):
        vm_commands._requested_rail_topology(_args("-T", "shared"))


def test_existing_vm_explicit_mismatch_fails_before_start(
    linux_profile: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Idempotent create must not start an existing VM with a different profile."""
    monkeypatch.setattr(vm_commands, "SOCKETS", tmp_path)
    (tmp_path / "co1-vm.info").touch()
    legacy = VMInfo(name="co1-vm", ip="192.168.100.20", nics=["tcp", "tcp"])
    monkeypatch.setattr(VMInfo, "load", MagicMock(return_value=legacy))
    launch = MagicMock(side_effect=AssertionError("unexpected launch"))
    monkeypatch.setattr(vm_commands, "launch_qemu", launch)
    with pytest.raises(SystemExit):
        vm_commands.cmd_create(_args("-T", "shared"))
    launch.assert_not_called()


def test_existing_profile_accepts_matching_request(linux_profile: None) -> None:
    """The existing-VM guard accepts an exact request and omitted topology."""
    args = _args("-T", "shared")
    args.rail_topology = vm_commands._requested_rail_topology(args)
    vm = VMInfo(
        name="co1-vm",
        ip="192.168.100.20",
        nics=args.nic,
        rail_topology=args.rail_topology,
    )
    vm_commands._check_existing_topology(vm, args)
    omitted = _args()
    omitted.rail_topology = None
    vm_commands._check_existing_topology(vm, omitted)


def test_cluster_forwards_complete_topology_request(
    linux_profile: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real child boundary receives both NICs and their exact network IDs."""
    result = MagicMock(returncode=0, stdout="", stderr="")
    run = MagicMock(return_value=result)
    monkeypatch.setattr(vm_cluster, "_run_prefixed", run)
    monkeypatch.setattr(vm_cluster, "_sudo_prefix", MagicMock(return_value=[]))
    monkeypatch.setattr(vm_cluster, "_ltvm", MagicMock(return_value=["ltvm"]))
    vm_cluster._create_one_node(
        ClusterNode("co1-srv", ["mgs", "mds"], mdt_disks=1),
        2,
        2048,
        nics=["tcp", "tcp"],
        network_topology="isolated",
        lnet_networks=["tcp7", "tcp8"],
    )
    command = run.call_args.args[0]
    assert command == [
        "ltvm",
        "create",
        "co1-srv",
        "--vcpus",
        "2",
        "--mem",
        "2048",
        "--mdt-disks",
        "1",
        "--ost-disks",
        "0",
        "--nic",
        "tcp",
        "--nic",
        "tcp",
        "--network-topology",
        "isolated",
        "--lnet-network",
        "tcp7",
        "--lnet-network",
        "tcp8",
    ]
    assert command.count("--nic") == 2
    assert "-T" not in command and "-L" not in command


def test_reserved_profile_kernel_args_fail_before_privileges(
    linux_profile: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guest override cannot change a profile-owned source or network ID."""
    args = _args("-T", "shared", "--kernel-args=fc_lnet_nets=tcp7,tcp8")
    privilege = MagicMock(
        side_effect=AssertionError("unexpected privilege prompt")
    )
    monkeypatch.setattr(setup_cli, "_vm_privileges", privilege)
    assert setup_cli.cmd_create(args) == 1
    privilege.assert_not_called()


def test_cluster_invalid_profile_keeps_stale_state(
    linux_profile: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Validation runs before even a stale cluster record could be removed."""
    record = tmp_path / "co1-lab.cluster"
    record.write_text("unrelated saved state\n")
    monkeypatch.setattr(vm_cluster, "SOCKETS", tmp_path)
    args = _args("-T", "isolated", "-L", "tcp0", "-L", "tcp0", cluster=True)
    args.nodes = ["mgs+mds:co1-srv:1"]
    with pytest.raises(SystemExit):
        vm_cluster.cmd_cluster_create(args)
    assert record.read_text() == "unrelated saved state\n"


def test_create_plan_reports_resolved_fabrics(
    linux_profile: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON dry-run exposes both subnet families without allocating addresses."""
    args = _args("-T", "shared", "--dry-run", "--json")
    args.rail_topology = vm_commands._requested_rail_topology(args)
    vm_commands._print_create_plan(
        args,
        exists=False,
        tap="tap-co1-vm",
        mac="aa:fc:00:01:02:03",
        image="base.ext4",
        kernel="vmlinuz",
        kver="test",
        os_target="rocky9",
        variant="base",
        extra_nic_types=args.nic,
        disk_size=1024,
        root_size=4096,
    )
    report = json.loads(capsys.readouterr().out)
    assert report["rail_topology"] == args.rail_topology.to_dict()
    assert report["ip"] is None
    assert "nic_ips" not in report


def test_profile_report_includes_actual_ipv4_and_ipv6() -> None:
    """JSON create/list diagnostics include the real addresses of every rail."""
    topology = resolve_topology(["tcp", "tcp"], "shared")
    assert topology is not None
    ipv4s = ["172.18.100.20", "172.18.100.21"]
    ipv6s = [rail.ipv6_for(ip) for rail, ip in zip(topology.rails, ipv4s)]
    vm = VMInfo(
        "co1-json",
        "192.168.100.20",
        nics=["tcp", "tcp"],
        nic_ips=ipv4s,
        nic_ip6s=ipv6s,
        rail_topology=topology,
    )
    assert vm_commands._rail_report(vm) == {
        "rail_topology": topology.to_dict(),
        "nic_ips": ipv4s,
        "nic_ip6s": ipv6s,
    }
    assert vm_commands._rail_report(VMInfo("co1-old", "192.168.100.21")) == {}


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"rail_topology_version": True},
        {"rail_topology_version": 0},
        {"rail_topology_version": 1},
        ["bad mapping"],
    ],
)
def test_old_profile_image_fails_before_vm_state_allocation(
    linux_profile: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    metadata: object,
) -> None:
    """A missing or invalid capability marker stops create before reserving addresses."""
    image = tmp_path / "base.ext4"
    image.touch()
    if metadata is not None:
        # Shared metadata cannot grant capabilities to a particular image file.
        (tmp_path / "meta.json").write_text(json.dumps(metadata))
    args = _args("-T", "shared", "--json")
    monkeypatch.setattr(vm_commands, "SOCKETS", tmp_path)
    monkeypatch.setattr(
        vm_commands, "_handle_existing_vm", MagicMock(return_value=False)
    )
    monkeypatch.setattr(
        vm_commands,
        "_validate_create_bounds",
        MagicMock(return_value=(["tcp", "tcp"], [])),
    )
    monkeypatch.setattr(
        vm_commands, "_validate_failover_disks", MagicMock(return_value=None)
    )
    monkeypatch.setattr(vm_commands, "_validate_disk_options", MagicMock())
    monkeypatch.setattr(vm_commands, "check_tap_collision", MagicMock())
    monkeypatch.setattr(
        vm_commands,
        "_resolve_os_and_kernel",
        MagicMock(
            return_value=(None, str(image), "vmlinuz", "test", "rocky9", "base")
        ),
    )
    allocate = MagicMock(
        side_effect=AssertionError("old image allocated VM state")
    )
    monkeypatch.setattr(vm_commands, "_allocate_and_persist_vm", allocate)
    with pytest.raises(SystemExit):
        vm_commands.cmd_create(args)
    allocate.assert_not_called()
    assert not list(tmp_path.glob("*.info"))


def test_supported_profile_image_and_legacy_image_guard(tmp_path: Path) -> None:
    """Only explicit profiles need the versioned guest-support marker."""
    image = tmp_path / "base.ext4"
    image.touch()
    topology = resolve_topology(["tcp", "tcp"], "shared")
    assert topology is not None
    vm_commands._require_profile_image(str(image), None)
    write_capability(image)
    vm_commands._require_profile_image(str(image), topology)


@pytest.mark.parametrize("kind", ["oversized", "deep", "binary"])
def test_invalid_profile_sidecar_is_bounded_and_checked(
    tmp_path: Path, kind: str
) -> None:
    """Malformed sidecars cannot consume unbounded memory or escape as a traceback."""
    topology = resolve_topology(["tcp", "tcp"], "shared")
    assert topology is not None
    raw = {
        "oversized": b" " * MAX_CAPABILITY_BYTES + b"1",
        "deep": b"[" * 4000 + b"]" * 4000,
        "binary": b"\xff\xfe\x00\x00",
    }[kind]
    image = tmp_path / "base.ext4"
    image.touch()
    capability_path(image).write_bytes(raw)
    with pytest.raises(SystemExit):
        vm_commands._require_profile_image(
            str(tmp_path / "base.ext4"), topology
        )
