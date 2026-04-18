"""Tests for qemu/models.py -- VMInfo metadata fields."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg.vm_state import VMInfo, VMNotFound


@pytest.fixture
def tmp_sockets(tmp_path: Path) -> Path:
    """Redirect SOCKETS to a temp directory for isolation."""
    with patch("ltvm_pkg.vm_state.SOCKETS", tmp_path):
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
            creator="alice",
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
        assert loaded.creator == "alice"

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
        # Legacy info file with no CREATOR= line: load() falls back
        # to the default ("") so old VMs show `by=-` in `ltvm list`
        # and don't crash anyone's existing tooling.
        assert vm.creator == ""

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


class TestVMInfoNicsField:
    """VMInfo.nics survives save/load, and legacy .info files parse as []."""

    def test_default_is_empty_list(self, tmp_sockets: Path) -> None:
        """A VM created without --nic has an empty nics list."""
        vm = VMInfo(name="solo", ip="192.168.100.50")
        assert vm.nics == []

    def test_nics_round_trip(self, tmp_sockets: Path) -> None:
        """nics survives save/load."""
        vm = VMInfo(
            name="twonic",
            ip="192.168.100.51",
            nics=["tcp", "tcp"],
        )
        vm.save()
        loaded = VMInfo.load("twonic")
        assert loaded.nics == ["tcp", "tcp"]

    def test_nics_with_colon_arg_round_trip(
        self, tmp_sockets: Path
    ) -> None:
        """NICs whose storage string has ':' (e.g. passthrough) survive.

        VMInfo joins on '|' so that a spec like
        'passthrough:0000:00:02.0' doesn't get confused with the CSV
        separator.  We shouldn't be able to create one of these via the
        CLI today, but a future VMInfo written by -5a0 must still load
        cleanly.
        """
        vm = VMInfo(
            name="vfio",
            ip="192.168.100.52",
            nics=["tcp", "passthrough:0000:00:02.0"],
        )
        vm.save()
        loaded = VMInfo.load("vfio")
        assert loaded.nics == ["tcp", "passthrough:0000:00:02.0"]

    def test_legacy_info_file_without_nics_line(
        self, tmp_sockets: Path
    ) -> None:
        """A .info file missing the NICS= line (pre-multi-NIC) loads as []."""
        info = tmp_sockets / "legacy.info"
        info.write_text(
            "NAME=legacy\n"
            "IP=192.168.100.90\n"
            "PID=0\n"
            "TAP=tap-legacy\n"
            "MAC=AA:FC:00:00:00:01\n"
            "VCPUS=2\n"
            "MEM=2048\n"
            "MDT_DISKS=0\n"
            "OST_DISKS=0\n"
            "IMAGE=\n"
            "KERNEL=\n"
        )
        vm = VMInfo.load("legacy")
        # Backward compat: no NICS= line -> empty list, not None / error
        assert vm.nics == []

    def test_empty_nics_line_parses_as_empty_list(
        self, tmp_sockets: Path
    ) -> None:
        """A VM saved with nics=[] writes ``NICS=`` and loads it back empty."""
        vm = VMInfo(name="nonics", ip="192.168.100.91")
        vm.save()
        # Confirm NICS= is present but empty-valued
        assert "NICS=\n" in vm.info_path.read_text()
        loaded = VMInfo.load("nonics")
        assert loaded.nics == []

    def test_extra_nics_empty_by_default(self, tmp_sockets: Path) -> None:
        """extra_nics() is empty when nics is empty."""
        vm = VMInfo(name="empty", ip="192.168.100.92")
        assert vm.extra_nics() == []

    def test_extra_nics_generates_deterministic_names(
        self, tmp_sockets: Path
    ) -> None:
        """extra_nics() yields (idx, type, tap, mac) starting at idx=1."""
        vm = VMInfo(
            name="co1-two",
            ip="192.168.100.93",
            nics=["tcp", "tcp"],
        )
        entries = vm.extra_nics()
        assert len(entries) == 2
        (i1, t1, tap1, mac1), (i2, t2, tap2, mac2) = entries
        # Index, type
        assert (i1, t1) == (1, "tcp")
        assert (i2, t2) == (2, "tcp")
        # Deterministic TAP names tied to the VM name
        assert tap1.startswith("tap-")
        assert tap1.endswith("-1")
        assert tap2.endswith("-2")
        # Distinct MACs per NIC
        assert mac1 != mac2
        # Extras don't collide with the mgmt NIC
        from ltvm_pkg.vm_net import mac_for_name, tap_for_name

        assert tap1 != tap_for_name(vm.name)
        assert mac1 != mac_for_name(vm.name)


class TestVMInfoLoadCorruption:
    """VMInfo.load fails loud on corrupt int fields -- writes are atomic
    (tempfile + rename) so a truncated/garbage int signals real damage."""

    def test_empty_pid_field_raises(self, tmp_sockets: Path) -> None:
        info = tmp_sockets / "broken.info"
        info.write_text(
            "NAME=broken\n"
            "IP=192.168.100.99\n"
            "PID=\n"
            "TAP=tap-broken\n"
            "MAC=AA:00:00:00:00:99\n"
        )
        with pytest.raises(ValueError):
            VMInfo.load("broken")

    def test_garbage_int_field_raises(self, tmp_sockets: Path) -> None:
        info = tmp_sockets / "weird.info"
        info.write_text(
            "NAME=weird\n"
            "IP=192.168.100.99\n"
            "PID=12345\n"
            "VCPUS=not-a-number\n"
            "MEM=4096\n"
            "TAP=tap-weird\n"
            "MAC=AA:00:00:00:00:98\n"
        )
        with pytest.raises(ValueError):
            VMInfo.load("weird")


class TestVMInfoLoadNotFound:
    """VMNotFound raised for missing VMs."""

    def test_load_nonexistent(self, tmp_sockets: Path) -> None:
        with pytest.raises(VMNotFound):
            VMInfo.load("does-not-exist")


class TestUpdateFieldsAtomic:
    """_update_fields writes all fields in a single atomic operation."""

    def test_all_fields_persisted(self, tmp_sockets: Path) -> None:
        """Calling _update_fields with multiple keys persists every key."""
        vm = VMInfo(name="atomic-test", ip="192.168.100.30")
        vm.save()
        vm._update_fields(
            {"LAST_DEPLOY": 123, "BUILD_PATH": "/x", "KVER": "5.14"}
        )
        text = vm.info_path.read_text()
        assert "LAST_DEPLOY=123" in text
        assert "BUILD_PATH=/x" in text
        assert "KVER=5.14" in text

    def test_file_reloads_correctly(self, tmp_sockets: Path) -> None:
        """Round-trip: fields written by _update_fields survive VMInfo.load."""
        vm = VMInfo(name="roundtrip-test", ip="192.168.100.31")
        vm.save()
        vm._update_fields(
            {"LAST_DEPLOY": 999, "BUILD_PATH": "/lustre", "KVER": "6.1"}
        )
        loaded = VMInfo.load("roundtrip-test")
        assert loaded.last_deploy == 999
        assert loaded.build_path == "/lustre"
        assert loaded.kver == "6.1"

    def test_raises_vmnotfound_on_missing_file(self, tmp_sockets: Path) -> None:
        """_update_fields raises when the .info file is gone.

        The previous silent no-op caused in-memory VMInfo state to
        diverge from disk with no signal -- a concurrent destroy or
        partially rolled-back create would silently lose updates.
        """
        from ltvm_pkg.vm_state import VMNotFound

        vm = VMInfo(name="no-file-test", ip="192.168.100.32")
        # Do NOT call vm.save() -- info file does not exist
        with pytest.raises(VMNotFound):
            vm._update_fields({"KVER": "5.14"})
        assert not vm.info_path.exists()


# ---------------------------------------------------------------------------
# vm_net: wait_for_ssh and deploy_ssh_key
# ---------------------------------------------------------------------------


class TestWaitForSsh:
    """wait_for_ssh raises SystemExit (via die()) when SSH never becomes ready."""

    def test_raises_on_timeout(self, tmp_sockets: Path) -> None:
        """When run_ssh always fails, wait_for_ssh exhausts retries and dies."""
        from ltvm_pkg.vm_net import wait_for_ssh

        fail_result = MagicMock()
        fail_result.returncode = 1

        with (
            patch("ltvm_pkg.vm_net.run_ssh", return_value=fail_result),
            patch("ltvm_pkg.vm_net.time.sleep"),
        ):
            with pytest.raises(SystemExit):
                wait_for_ssh("192.168.100.99", max_wait=3)

    def test_returns_on_success(self, tmp_sockets: Path) -> None:
        """wait_for_ssh returns normally when SSH succeeds on the first attempt."""
        from ltvm_pkg.vm_net import wait_for_ssh

        ok_result = MagicMock()
        ok_result.returncode = 0

        with patch("ltvm_pkg.vm_net.run_ssh", return_value=ok_result):
            wait_for_ssh("192.168.100.50", max_wait=5)  # should not raise


class TestDeploySshKey:
    """deploy_ssh_key raises SystemExit (via die()) on SSH timeout."""

    def test_raises_on_timeout_expired(self, tmp_path: Path) -> None:
        """When run_ssh raises TimeoutExpired, deploy_ssh_key calls die()."""
        from ltvm_pkg.vm_net import deploy_ssh_key

        fake_ssh_dir = tmp_path / ".ssh"
        fake_ssh_dir.mkdir()
        (fake_ssh_dir / "id_rsa.pub").write_text(
            "ssh-rsa AAAA fake-key user@host"
        )

        with (
            patch(
                "ltvm_pkg.vm_net._real_user_ssh_dir",
                return_value=("testuser", fake_ssh_dir),
            ),
            patch(
                "ltvm_pkg.vm_net.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=10),
            ),
        ):
            with pytest.raises(SystemExit):
                deploy_ssh_key("192.168.100.99")


class TestResolveOsArtifactsPerKernel:
    """resolve_os_artifacts picks the per-kernel image matching --kernel."""

    def _setup(self, tmp_path: Path) -> Path:
        out = tmp_path / "artifacts" / "rocky9" / "x86_64"
        # Two built kernels, each with a matching image.
        k1 = "5.14-rhel9.7"
        k2 = "6.1-rhel9.7"
        for k in (k1, k2):
            (out / "kernels" / k).mkdir(parents=True)
            (out / "kernels" / k / "vmlinuz").write_bytes(b"k")
            (out / "images" / k).mkdir(parents=True)
            (out / "images" / k / "base.ext4").write_bytes(b"i")
        # targets.yaml
        yml = tmp_path / "targets" / "targets.yaml"
        yml.parent.mkdir(parents=True)
        yml.write_text(
            "defaults: {}\n"
            "targets:\n"
            "  rocky9:\n"
            "    os_name: rocky\n"
            "    os_version: '9'\n"
            "    container_image: rockylinux:9\n"
            "    status: working\n"
            "    kernels:\n"
            "      default: 5.14-rhel9.7\n"
            "    lustre: {mode: server_ldiskfs}\n"
        )
        return tmp_path

    def test_named_kernel_selects_matching_image(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg import vm_state

        root = self._setup(tmp_path)
        from ltvm_pkg import target_config as tc_mod
        with (
            patch.object(vm_state, "_LTVM_ROOT", root),
            patch.object(vm_state, "TARGETS_YAML", root / "targets" / "targets.yaml"),
            patch.object(tc_mod, "TARGETS_YAML", root / "targets" / "targets.yaml"),
            patch.object(tc_mod, "TARGETS_DIR", root / "targets"),
            patch.object(tc_mod, "ARTIFACTS_DIR", root / "artifacts"),
        ):
            arts = vm_state.resolve_os_artifacts("rocky9", kernel="6.1-rhel9.7")
        assert arts.kernel.parent.name == "6.1-rhel9.7"
        assert arts.image == root / "artifacts" / "rocky9" / "x86_64" / "images" / "6.1-rhel9.7" / "base.ext4"

    def test_default_uses_default_kernel_image(self, tmp_path: Path) -> None:
        from ltvm_pkg import vm_state

        root = self._setup(tmp_path)
        from ltvm_pkg import target_config as tc_mod
        with (
            patch.object(vm_state, "_LTVM_ROOT", root),
            patch.object(vm_state, "TARGETS_YAML", root / "targets" / "targets.yaml"),
            patch.object(tc_mod, "TARGETS_YAML", root / "targets" / "targets.yaml"),
            patch.object(tc_mod, "TARGETS_DIR", root / "targets"),
            patch.object(tc_mod, "ARTIFACTS_DIR", root / "artifacts"),
        ):
            arts = vm_state.resolve_os_artifacts("rocky9")
        assert arts.image.parent.name == "5.14-rhel9.7"

    def test_missing_image_raises_with_hint(self, tmp_path: Path) -> None:
        from ltvm_pkg import vm_state

        root = self._setup(tmp_path)
        # Remove the 6.1 image to force the failure path.
        (root / "artifacts" / "rocky9" / "x86_64" / "images" / "6.1-rhel9.7" / "base.ext4").unlink()
        from ltvm_pkg import target_config as tc_mod
        with (
            patch.object(vm_state, "_LTVM_ROOT", root),
            patch.object(vm_state, "TARGETS_YAML", root / "targets" / "targets.yaml"),
            patch.object(tc_mod, "TARGETS_YAML", root / "targets" / "targets.yaml"),
            patch.object(tc_mod, "TARGETS_DIR", root / "targets"),
            patch.object(tc_mod, "ARTIFACTS_DIR", root / "artifacts"),
        ):
            with pytest.raises(FileNotFoundError, match="build image"):
                vm_state.resolve_os_artifacts("rocky9", kernel="6.1-rhel9.7")

