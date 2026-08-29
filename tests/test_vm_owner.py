"""Focused tests for durable VM owner/session metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import vm_cluster, vm_commands
from ltvm_pkg.cli import cmd_cluster
from ltvm_pkg.vm_owner import resolve_owner_id, validate_owner_id
from ltvm_pkg.vm_state import ClusterInfo, VMInfo


@pytest.fixture
def tmp_vm_state(tmp_path: Path):
    sockets = tmp_path / "sockets"
    overlays = tmp_path / "overlays"
    sockets.mkdir()
    overlays.mkdir()
    with (
        patch("ltvm_pkg.vm_state.SOCKETS", sockets),
        patch("ltvm_pkg.vm_state.OVERLAYS", overlays),
        patch("ltvm_pkg.vm_commands.SOCKETS", sockets),
        patch("ltvm_pkg.vm_commands.OVERLAYS", overlays),
        patch("ltvm_pkg.vm_cluster.SOCKETS", sockets),
    ):
        yield sockets, overlays


def _create_args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "name": "owned-vm",
        "vcpus": 2,
        "mem": 2048,
        "mdt_disks": 0,
        "ost_disks": 0,
        "disk_size": None,
        "image": "",
        "kernel": "",
        "target": "",
        "arch": None,
        "ip": None,
        "json": False,
        "_quiet": True,
        "nic": None,
        "owner_id": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class TestOwnerResolution:
    def test_explicit_cli_value_wins_over_environment(self) -> None:
        assert (
            resolve_owner_id(
                "patch-watcher:session-123",
                environ={"LTVM_OWNER_ID": "environment-owner"},
                pid=99,
            )
            == "patch-watcher:session-123"
        )

    def test_environment_wins_over_pid_fallback(self) -> None:
        assert (
            resolve_owner_id(
                environ={"LTVM_OWNER_ID": "session:from-env"}, pid=99
            )
            == "session:from-env"
        )

    def test_pid_fallback_is_typed(self) -> None:
        assert resolve_owner_id(environ={}, pid=4321) == "pid:4321"

    def test_empty_environment_uses_pid_fallback(self) -> None:
        assert (
            resolve_owner_id(environ={"LTVM_OWNER_ID": ""}, pid=4321)
            == "pid:4321"
        )

    @pytest.mark.parametrize("value", ["", "line1\nline2", "nul\x00byte"])
    def test_unsafe_explicit_values_are_rejected(self, value: str) -> None:
        with pytest.raises(ValueError, match="owner ID"):
            validate_owner_id(value)

    def test_overlong_owner_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="too long"):
            validate_owner_id("x" * 256)


class TestVMOwnerPersistence:
    def test_owner_survives_save_and_reload(self, tmp_vm_state) -> None:
        vm = VMInfo(
            name="persisted",
            ip="192.0.2.10",
            owner_id="patch-watcher:session-abc",
        )
        vm.save()

        assert (
            "OWNER_ID=patch-watcher:session-abc\n" in vm.info_path.read_text()
        )
        assert VMInfo.load("persisted").owner_id == "patch-watcher:session-abc"

    def test_legacy_state_without_owner_loads_as_null(
        self, tmp_vm_state
    ) -> None:
        sockets, _ = tmp_vm_state
        (sockets / "legacy.info").write_text(
            "NAME=legacy\nIP=192.0.2.11\nPID=0\nVCPUS=2\nMEM=2048\n"
        )

        assert VMInfo.load("legacy").owner_id is None

    def test_runtime_state_updates_retain_owner(self, tmp_vm_state) -> None:
        vm = VMInfo(
            name="restarted",
            ip="192.0.2.15",
            owner_id="patch-watcher:original-session",
        )
        vm.save()

        vm.update_pid(9912)
        vm.update_last_boot(1_700_000_000)

        loaded = VMInfo.load("restarted")
        assert loaded.pid == 9912
        assert loaded.last_boot == 1_700_000_000
        assert loaded.owner_id == "patch-watcher:original-session"

    def test_idempotent_create_does_not_reassign_owner(
        self, tmp_vm_state, capsys: pytest.CaptureFixture[str]
    ) -> None:
        VMInfo(
            name="existing",
            ip="192.0.2.16",
            pid=123,
            owner_id="patch-watcher:original-session",
        ).save()

        with (
            patch.dict(
                "os.environ",
                {"LTVM_OWNER_ID": "patch-watcher:different-session"},
                clear=False,
            ),
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.wait_for_ssh"),
            patch("ltvm_pkg.vm_commands.register_ssh_name"),
        ):
            vm_commands.cmd_create(_create_args(name="existing", json=True))

        payload = json.loads(capsys.readouterr().out)
        assert payload["owner_id"] == "patch-watcher:original-session"
        assert (
            VMInfo.load("existing").owner_id == "patch-watcher:original-session"
        )

    def test_create_uses_environment_owner(self, tmp_vm_state) -> None:
        sockets, _ = tmp_vm_state
        arts = SimpleNamespace(arch="x86_64")

        class Allocation:
            def __enter__(self) -> list[str]:
                return ["192.0.2.12"]

            def __exit__(self, *args: object) -> None:
                return None

        with (
            patch.dict(
                "os.environ",
                {"LTVM_OWNER_ID": "patch-watcher:session-env"},
                clear=False,
            ),
            patch("ltvm_pkg.vm_commands.alloc_ip", return_value=Allocation()),
            patch(
                "ltvm_pkg.vm_commands._resolve_os_and_kernel",
                return_value=(
                    arts,
                    "/tmp/base.ext4",
                    "/tmp/vmlinuz",
                    "5.14-test",
                    "rocky9",
                    "base",
                ),
            ),
            patch("ltvm_pkg.vm_commands._create_disks"),
            patch("ltvm_pkg.vm_commands._chown_disks_to_sudo_user"),
            patch("ltvm_pkg.vm_commands._launch_and_wait"),
        ):
            vm_commands.cmd_create(_create_args())

        assert (sockets / "owned-vm.info").exists()
        assert VMInfo.load("owned-vm").owner_id == "patch-watcher:session-env"

    def test_list_json_exposes_owner_and_legacy_null(
        self, tmp_vm_state, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sockets, _ = tmp_vm_state
        VMInfo(name="owned", ip="192.0.2.13", owner_id="session:list").save()
        (sockets / "legacy.info").write_text(
            "NAME=legacy\nIP=192.0.2.14\nPID=0\nVCPUS=2\nMEM=2048\n"
        )

        with patch("ltvm_pkg.vm_commands.is_running", return_value=False):
            vm_commands.cmd_list(argparse.Namespace(json=True))

        payload = json.loads(capsys.readouterr().out)
        owners = {vm["name"]: vm["owner_id"] for vm in payload["vms"]}
        assert owners == {"legacy": None, "owned": "session:list"}


class TestClusterOwnerPropagation:
    def test_cluster_cli_accepts_both_owner_flag_names(self) -> None:
        for flag in ("--owner", "--owner-id"):
            args = argparse.Namespace(
                action="create",
                cluster_args=[
                    "cluster-a",
                    flag,
                    "session:cluster",
                    "mgs+mds:cluster-a-mds:1",
                ],
                json=False,
            )
            with (
                patch("ltvm_pkg.cli._require_root", return_value=None),
                patch("ltvm_pkg.vm_cluster.cmd_cluster_create") as handler,
            ):
                assert cmd_cluster(args) == 0
            assert handler.call_args.args[0].owner_id == "session:cluster"

    def test_child_command_receives_resolved_owner(self) -> None:
        node = vm_cluster.parse_node_spec("mgs+mds:cluster-mds:1")
        completed = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("ltvm_pkg.vm_cluster._sudo_prefix", return_value=[]),
            patch(
                "ltvm_pkg.vm_cluster.subprocess.run", return_value=completed
            ) as run,
        ):
            vm_cluster._create_one_node(
                node, 2, 2048, owner_id="patch-watcher:cluster-1"
            )

        command = run.call_args.args[0]
        idx = command.index("--owner-id")
        assert command[idx + 1] == "patch-watcher:cluster-1"

    def test_every_member_and_cluster_state_share_environment_owner(
        self, tmp_vm_state
    ) -> None:
        def fake_create(node, *args):
            owner_id = args[-1]
            VMInfo(name=node.name, ip="192.0.2.20", owner_id=owner_id).save()
            return node.name, 0, ""

        args = argparse.Namespace(
            name="cluster-b",
            nodes=["mgs+mds:cluster-b-mds:1", "oss:cluster-b-oss:1"],
            vcpus=2,
            mem=2048,
            os=None,
            arch=None,
            disk_size=None,
            nic=[],
            owner_id=None,
        )
        with (
            patch.dict(
                "os.environ",
                {"LTVM_OWNER_ID": "patch-watcher:cluster-session"},
                clear=False,
            ),
            patch(
                "ltvm_pkg.vm_cluster._create_one_node", side_effect=fake_create
            ),
        ):
            vm_cluster.cmd_cluster_create(args)

        cluster = ClusterInfo.load("cluster-b")
        assert cluster.owner_id == "patch-watcher:cluster-session"
        assert {
            VMInfo.load("cluster-b-mds").owner_id,
            VMInfo.load("cluster-b-oss").owner_id,
        } == {"patch-watcher:cluster-session"}

    def test_legacy_cluster_state_loads_with_null_owner(
        self, tmp_vm_state
    ) -> None:
        sockets, _ = tmp_vm_state
        (sockets / "legacy.cluster").write_text(
            json.dumps({"name": "legacy", "nodes": []})
        )

        assert ClusterInfo.load("legacy").owner_id is None
