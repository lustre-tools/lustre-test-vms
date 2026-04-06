"""Tests for qemu/models.py -- VMInfo metadata fields."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from qemu.models import VMInfo, VMNotFound


@pytest.fixture
def tmp_sockets(tmp_path: Path) -> Path:
    """Redirect SOCKETS to a temp directory for isolation."""
    with patch("qemu.models.SOCKETS", tmp_path):
        yield tmp_path


class TestVMInfoMetadata:
    """VMInfo save/load round-trip with new metadata fields."""

    def test_save_load_all_fields(self, tmp_sockets: Path) -> None:
        """All metadata fields survive a save/load cycle."""
        vm = VMInfo(
            name="test-vm",
            ip="192.168.100.50",
            pid=12345,
            tap="tap-test-vm",
            mac="AA:BB:CC:DD:EE:FF",
            vcpus=4,
            mem=8192,
            mdt_disks=2,
            ost_disks=3,
            created=1700000000,
            last_boot=1700001000,
            last_deploy=1700002000,
            build_path="/home/admin/lustre-release",
            kver="5.14.0-test.x86_64",
            base_image="rocky9-base.ext4",
            os_id="rocky9",
        )
        vm.save()
        loaded = VMInfo.load("test-vm")

        assert loaded.created == 1700000000
        assert loaded.last_boot == 1700001000
        assert loaded.last_deploy == 1700002000
        assert loaded.build_path == "/home/admin/lustre-release"
        assert loaded.kver == "5.14.0-test.x86_64"
        assert loaded.base_image == "rocky9-base.ext4"
        assert loaded.os_id == "rocky9"

    def test_load_missing_metadata_defaults(self, tmp_sockets: Path) -> None:
        """Loading an old info file (no metadata) yields zero/empty defaults."""
        info = tmp_sockets / "old-vm.info"
        info.write_text(
            "NAME=old-vm\n"
            "IP=192.168.100.10\n"
            "PID=999\n"
            "TAP=tap-old\n"
            "MAC=AA:00:00:00:00:01\n"
            "VCPUS=2\n"
            "MEM=4096\n"
            "MDT_DISKS=1\n"
            "OST_DISKS=3\n"
            "IMAGE=\n"
            "KERNEL=\n"
        )
        vm = VMInfo.load("old-vm")
        assert vm.created == 0
        assert vm.last_boot == 0
        assert vm.last_deploy == 0
        assert vm.build_path == ""
        assert vm.kver == ""
        assert vm.base_image == ""
        assert vm.os_id == ""

    def test_update_field_adds_missing(self, tmp_sockets: Path) -> None:
        """_update_field adds a field that doesn't exist yet."""
        vm = VMInfo(name="add-test", ip="192.168.100.20")
        vm.save()
        vm._update_field("KVER", "5.14.0-new")
        text = vm.info_path.read_text()
        assert "KVER=5.14.0-new" in text

    def test_update_field_replaces_existing(self, tmp_sockets: Path) -> None:
        """_update_field replaces an existing field value."""
        vm = VMInfo(
            name="replace-test",
            ip="192.168.100.21",
            kver="old-version",
        )
        vm.save()
        assert "KVER=old-version" in vm.info_path.read_text()
        vm._update_field("KVER", "new-version")
        text = vm.info_path.read_text()
        assert "KVER=new-version" in text
        assert "KVER=old-version" not in text

    def test_update_pid(self, tmp_sockets: Path) -> None:
        """update_pid uses _update_field correctly."""
        vm = VMInfo(name="pid-test", ip="192.168.100.22", pid=100)
        vm.save()
        vm.update_pid(200)
        loaded = VMInfo.load("pid-test")
        assert loaded.pid == 200

    def test_update_last_boot(self, tmp_sockets: Path) -> None:
        """update_last_boot persists the timestamp."""
        vm = VMInfo(name="boot-test", ip="192.168.100.23")
        vm.save()
        vm.update_last_boot(1700005000)
        loaded = VMInfo.load("boot-test")
        assert loaded.last_boot == 1700005000

    def test_update_deploy(self, tmp_sockets: Path) -> None:
        """update_deploy sets all three deploy-related fields."""
        vm = VMInfo(name="deploy-test", ip="192.168.100.24")
        vm.save()
        vm.update_deploy(1700006000, "/builds/lustre", "5.14.0-x")
        loaded = VMInfo.load("deploy-test")
        assert loaded.last_deploy == 1700006000
        assert loaded.build_path == "/builds/lustre"
        assert loaded.kver == "5.14.0-x"


class TestVMInfoLoadNotFound:
    """VMNotFound raised for missing VMs."""

    def test_load_nonexistent(self, tmp_sockets: Path) -> None:
        with pytest.raises(VMNotFound):
            VMInfo.load("does-not-exist")
