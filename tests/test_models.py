"""Tests for qemu/models.py -- VMInfo metadata fields."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg.vm_state import ROOT_SIZE_BYTES, VMInfo, VMNotFound


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
        # Pre-ROOT_SIZE .info files describe VMs whose overlay was
        # grown to the old hardcoded 8G, which is what the default is.
        assert vm.root_size == ROOT_SIZE_BYTES
        assert vm.kernel_args == ""

    def test_root_size_round_trip(self, tmp_sockets: Path) -> None:
        vm = VMInfo(
            name="root-size", ip="192.168.100.60", root_size=20 * (1 << 30)
        )
        vm.save()
        assert VMInfo.load("root-size").root_size == 20 * (1 << 30)

    def test_kernel_args_round_trip(self, tmp_sockets: Path) -> None:
        args = 'slub_debug=FZPU page_owner=on dyndbg="file foo.c +p"'
        vm = VMInfo(name="kargs", ip="192.168.100.61", kernel_args=args)
        vm.save()
        assert VMInfo.load("kargs").kernel_args == args

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

    def test_nics_with_colon_arg_round_trip(self, tmp_sockets: Path) -> None:
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


class TestSubnetForNic:
    """The mgmt NIC and the extra NICs are on separate networks; the
    extras share one network with each other, because LNet multi-rail
    is several NIs on ONE LNet network."""

    def test_mgmt_nic_uses_the_mgmt_subnet(self) -> None:
        from ltvm_pkg import vm_state

        with patch("ltvm_pkg.vm_state.SUBNET", "192.168.100"):
            assert vm_state.subnet_for_nic(0) == "192.168.100"

    def test_every_extra_nic_shares_one_subnet(self) -> None:
        from ltvm_pkg import vm_state

        with (
            patch("ltvm_pkg.vm_state.SUBNET", "192.168.100"),
            patch("ltvm_pkg.vm_state.EXTRA_SUBNET", "172.16.100"),
        ):
            nets = [vm_state.subnet_for_nic(i) for i in (1, 2, 3)]
        assert nets == ["172.16.100"] * 3

    def test_extra_subnet_equal_to_mgmt_rejected(self) -> None:
        """A mgmt subnet reused as the extra network is a config
        error, not something to silently allocate over."""
        from ltvm_pkg import vm_state

        with (
            patch("ltvm_pkg.vm_state.SUBNET", "172.16.100"),
            patch("ltvm_pkg.vm_state.EXTRA_SUBNET", "172.16.100"),
        ):
            assert vm_state.subnet_for_nic(0) == "172.16.100"
            with pytest.raises(ValueError):
                vm_state.subnet_for_nic(1)


class TestVMInfoNicIp6sField:
    """NIC_IP6S is index-parallel to NIC_IPS and absent on older
    .info files, which must still load."""

    def test_nic_ip6s_round_trip(self, tmp_sockets: Path) -> None:
        vm = VMInfo(
            name="rails6",
            ip="192.168.100.60",
            nics=["softroce", "softroce"],
            nic_ips=["172.16.100.60", "172.16.100.61"],
            nic_ip6s=[
                "fd17:2016:1000:f100:f172:f016:f100:f060",
                "fd17:2016:1000:f100:f172:f016:f100:f061",
            ],
        )
        vm.save()
        assert (
            "NIC_IP6S=fd17:2016:1000:f100:f172:f016:f100:f060"
            "|fd17:2016:1000:f100:f172:f016:f100:f061\n"
            in vm.info_path.read_text()
        )
        loaded = VMInfo.load("rails6")
        assert loaded.nic_ip6s == vm.nic_ip6s

    def test_legacy_info_file_without_nic_ip6s_line(
        self, tmp_sockets: Path
    ) -> None:
        """A .info written before extras carried IPv6 loads with an
        empty list, keeping the VM usable across an ltvm upgrade."""
        info = tmp_sockets / "old6.info"
        info.write_text(
            "NAME=old6\n"
            "IP=192.168.100.90\n"
            "PID=0\n"
            "TAP=tap-old6\n"
            "NICS=softroce\n"
            "NIC_IPS=172.16.100.90\n"
        )
        loaded = VMInfo.load("old6")
        assert loaded.nic_ips == ["172.16.100.90"]
        assert loaded.nic_ip6s == []


class TestNicIp6:
    """Every extra NIC's IPv6 address is derived from its IPv4 one, and
    must render at full width."""

    def test_maps_an_ipv4_address_to_its_pair(self) -> None:
        from ltvm_pkg import vm_state

        with patch("ltvm_pkg.vm_state.EXTRA_SUBNET6", "fd17:2016:1000:f100"):
            assert (
                vm_state.nic_ip6("172.16.100.203")
                == "fd17:2016:1000:f100:f172:f016:f100:f203"
            )

    def test_different_octets_give_different_addresses(self) -> None:
        from ltvm_pkg import vm_state

        with patch("ltvm_pkg.vm_state.EXTRA_SUBNET6", "fd17:2016:1000:f100"):
            a = vm_state.nic_ip6("172.16.100.203")
            b = vm_state.nic_ip6("172.16.100.204")
        assert a != b

    def test_not_an_ipv4_address_rejected(self) -> None:
        from ltvm_pkg import vm_state

        with pytest.raises(ValueError):
            vm_state.nic_ip6("172.16.100")

    def test_addresses_always_render_at_full_width(self) -> None:
        """The property the whole scheme exists to hold: 39 characters,
        eight four-digit hextets, never compressed.  A short address is
        shorter than the IPv4 one it replaces, so it exercises nothing.
        """
        import ipaddress

        from ltvm_pkg import vm_state

        with patch("ltvm_pkg.vm_state.EXTRA_SUBNET6", "fd17:2016:1000:f100"):
            for octet in range(10, 254):
                a = vm_state.nic_ip6(f"172.16.100.{octet}")
                assert len(a) == 39, a
                assert "::" not in a, a
                assert str(ipaddress.IPv6Address(a)) == a, a
                assert all(len(h) == 4 for h in a.split(":")), a


class TestExtraSubnet6Validation:
    """An override is checked for shape only; the full-width rule holds
    for the default prefix, not for a prefix the operator chose."""

    def test_short_hextets_accepted_on_override(self) -> None:
        from ltvm_pkg import vm_state

        with patch.dict("os.environ", {"LTVM_EXTRA_SUBNET6": "fd00:1:2:3"}):
            assert vm_state._read_extra_subnet6() == "fd00:1:2:3"

    def test_short_override_gives_a_valid_address(self) -> None:
        import ipaddress

        from ltvm_pkg import vm_state

        with patch("ltvm_pkg.vm_state.EXTRA_SUBNET6", "fd00:0:0:0"):
            a = vm_state.nic_ip6("172.16.100.23")
        assert ipaddress.IPv6Address(a) == ipaddress.IPv6Address(
            "fd00::f172:f016:f100:f023"
        )

    def test_overlong_hextet_rejected(self) -> None:
        from ltvm_pkg import vm_state

        with patch.dict(
            "os.environ", {"LTVM_EXTRA_SUBNET6": "fd17:2016:1000:10000"}
        ):
            with pytest.raises(ValueError) as e:
                vm_state._read_extra_subnet6()
        assert "LTVM_EXTRA_SUBNET6" in str(e.value)

    def test_wrong_hextet_count_rejected(self) -> None:
        from ltvm_pkg import vm_state

        with patch.dict("os.environ", {"LTVM_EXTRA_SUBNET6": "fd17:2016:1000"}):
            with pytest.raises(ValueError) as e:
                vm_state._read_extra_subnet6()
        assert "LTVM_EXTRA_SUBNET6" in str(e.value)

    def test_non_hex_hextet_rejected(self) -> None:
        from ltvm_pkg import vm_state

        with patch.dict(
            "os.environ", {"LTVM_EXTRA_SUBNET6": "fd17:2016:1000:zzzz"}
        ):
            with pytest.raises(ValueError) as e:
                vm_state._read_extra_subnet6()
        assert "LTVM_EXTRA_SUBNET6" in str(e.value)

    def test_valid_override_accepted(self) -> None:
        from ltvm_pkg import vm_state

        with patch.dict(
            "os.environ", {"LTVM_EXTRA_SUBNET6": "fd99:1234:abcd:f001"}
        ):
            assert vm_state._read_extra_subnet6() == "fd99:1234:abcd:f001"

    def test_default_prefix_holds_the_invariant(self) -> None:
        from ltvm_pkg import vm_state

        with patch.dict("os.environ", {}, clear=True):
            with patch("ltvm_pkg.vm_state.VM_DIR", Path("/nonexistent")):
                prefix = vm_state._read_extra_subnet6()
        assert prefix == "fd17:2016:1000:f100"
        assert all(int(h, 16) >= 0x1000 for h in prefix.split(":"))


class TestVMInfoNicIpsField:
    """NIC_IPS stays a plain list of dotted quads even though extras
    moved off the mgmt /24 -- the network is derivable from the
    address, so the .info format did not have to grow."""

    def test_nic_ips_on_their_own_networks_round_trip(
        self, tmp_sockets: Path
    ) -> None:
        vm = VMInfo(
            name="rails",
            ip="192.168.100.60",
            nics=["softroce", "softroce"],
            nic_ips=["172.16.100.60", "172.16.100.61"],
        )
        vm.save()
        assert (
            "NIC_IPS=172.16.100.60|172.16.100.61\n" in vm.info_path.read_text()
        )
        loaded = VMInfo.load("rails")
        assert loaded.nic_ips == ["172.16.100.60", "172.16.100.61"]

    def test_legacy_info_file_without_nic_ips_line(
        self, tmp_sockets: Path
    ) -> None:
        """A pre-fix .info: NICS= present, NIC_IPS= absent entirely.

        It must still load, keeping the VM usable across an ltvm
        upgrade -- the extras simply have no recorded address.
        """
        info = tmp_sockets / "old.info"
        info.write_text(
            "NAME=old\n"
            "IP=192.168.100.90\n"
            "PID=0\n"
            "TAP=tap-old\n"
            "MAC=AA:FC:00:00:00:01\n"
            "VCPUS=2\n"
            "MEM=2048\n"
            "MDT_DISKS=0\n"
            "OST_DISKS=0\n"
            "IMAGE=\n"
            "KERNEL=\n"
            "NICS=tcp\n"
        )
        vm = VMInfo.load("old")
        assert vm.nics == ["tcp"]
        assert vm.nic_ips == []

    def test_legacy_info_file_with_mgmt_subnet_nic_ips(
        self, tmp_sockets: Path
    ) -> None:
        """A VM created before the fix has its extras on the mgmt /24.
        Loading must not rewrite or reject them."""
        info = tmp_sockets / "sameteam.info"
        info.write_text(
            "NAME=sameteam\n"
            "IP=192.168.100.33\n"
            "PID=0\n"
            "TAP=tap-sameteam\n"
            "MAC=AA:FC:00:00:00:02\n"
            "VCPUS=2\n"
            "MEM=2048\n"
            "MDT_DISKS=0\n"
            "OST_DISKS=0\n"
            "IMAGE=\n"
            "KERNEL=\n"
            "NICS=tcp\n"
            "NIC_IPS=192.168.100.34\n"
        )
        vm = VMInfo.load("sameteam")
        assert vm.nic_ips == ["192.168.100.34"]


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


class TestWaitForSshQemuExit:
    """Given the VM, the wait ends once its QEMU is gone."""

    def _run(self, running: bool, pid: int = 4242) -> tuple[int, str]:
        from ltvm_pkg.vm_net import wait_for_ssh
        from ltvm_pkg.vm_state import VMInfo

        vm = VMInfo(name="co9-x", ip="192.168.100.9", pid=pid)
        clock = _FakeClock()
        with (
            patch(
                "ltvm_pkg.vm_net.run_ssh",
                return_value=_ssh_result(255, "Connection refused"),
            ) as probe,
            patch("ltvm_pkg.vm_net.is_running", return_value=running),
            patch(
                "ltvm_pkg.vm_net.time.monotonic", side_effect=clock.monotonic
            ),
            patch("ltvm_pkg.vm_net.time.sleep", side_effect=clock.sleep),
            patch("ltvm_pkg.vm_net.die", side_effect=SystemExit) as die,
        ):
            with pytest.raises(SystemExit):
                wait_for_ssh(vm.ip, max_wait=120, vm=vm)
        return probe.call_count, die.call_args.args[0]

    def test_exited_qemu_stops_the_wait(self) -> None:
        calls, msg = self._run(running=False)
        assert calls == 1
        assert "QEMU for co9-x exited" in msg
        assert "console-log co9-x" in msg

    def test_live_qemu_waits_out_the_timeout(self) -> None:
        calls, msg = self._run(running=True)
        assert calls > 1
        assert "SSH not ready after" in msg

    def test_unknown_pid_is_not_taken_as_exited(self) -> None:
        calls, msg = self._run(running=False, pid=0)
        assert calls > 1
        assert "SSH not ready after" in msg


class TestEmulatedGuestsWaitLonger:
    """A TCG guest gets TCG_SSH_WAIT_FACTOR times the ssh budget."""

    def _elapsed(self, emulated: bool, monkeypatch) -> int:
        import re

        from ltvm_pkg.vm_net import wait_for_ssh
        from ltvm_pkg.vm_state import VMInfo

        vm = VMInfo(name="co9-x", ip="192.168.100.9", arch="aarch64", pid=0)
        clock = _FakeClock()
        with (
            patch(
                "ltvm_pkg.vm_net.run_ssh",
                return_value=_ssh_result(255, "No route to host"),
            ),
            patch("ltvm_pkg.vm_net.emulated", return_value=emulated),
            patch(
                "ltvm_pkg.vm_net.time.monotonic", side_effect=clock.monotonic
            ),
            patch("ltvm_pkg.vm_net.time.sleep", side_effect=clock.sleep),
            patch("ltvm_pkg.vm_net.die", side_effect=SystemExit) as die,
        ):
            with pytest.raises(SystemExit):
                wait_for_ssh(vm.ip, max_wait=120, vm=vm)
        m = re.search(r"after (\d+)s", die.call_args.args[0])
        assert m
        return int(m.group(1))

    def test_emulated_guest_waits_longer(self, monkeypatch) -> None:
        monkeypatch.delenv("LTVM_SSH_TIMEOUT", raising=False)
        assert self._elapsed(False, monkeypatch) < 200
        assert self._elapsed(True, monkeypatch) >= 600

    def test_an_explicit_timeout_is_kept(self, monkeypatch) -> None:
        monkeypatch.setenv("LTVM_SSH_TIMEOUT", "120")
        assert self._elapsed(True, monkeypatch) < 200


class _FakeClock:
    """time.monotonic that only moves when wait_for_ssh sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += secs + 4  # each probe costs about its 5s timeout


def _wait(results: list, max_wait: int = 30) -> tuple[MagicMock, str]:
    """Run wait_for_ssh over *results* (the last one repeats).

    Returns (the run_ssh mock, the error printed).
    """
    from ltvm_pkg.vm_net import wait_for_ssh

    clock = _FakeClock()

    def probe(*_a: object, **_kw: object) -> object:
        r = results[min(probe.calls, len(results) - 1)]  # type: ignore[attr-defined]
        probe.calls += 1  # type: ignore[attr-defined]
        if isinstance(r, BaseException):
            raise r
        return r

    probe.calls = 0  # type: ignore[attr-defined]
    with (
        patch("ltvm_pkg.vm_net.run_ssh", side_effect=probe) as mock,
        patch("ltvm_pkg.vm_net.time.monotonic", side_effect=clock.monotonic),
        patch("ltvm_pkg.vm_net.time.sleep", side_effect=clock.sleep),
        patch("ltvm_pkg.vm_net.die", side_effect=SystemExit) as die,
    ):
        with pytest.raises(SystemExit):
            wait_for_ssh("192.168.100.7", max_wait=max_wait)
    return mock, die.call_args.args[0]


def _ssh_result(rc: int, stderr: str) -> MagicMock:
    return MagicMock(returncode=rc, stdout="", stderr=stderr)


# What a booting guest answers with.  None of these may end the wait.
_BOOT_TIME_ERRORS = [
    "ssh: connect to host 192.168.100.7 port 22: Connection refused",
    "ssh: connect to host 192.168.100.7 port 22: Connection timed out",
    "ssh: connect to host 192.168.100.7 port 22: No route to host",
    "kex_exchange_identification: read: Connection reset by peer",
    "kex_exchange_identification: Connection closed by remote host",
    "Connection closed by 192.168.100.7 port 22",
    "Connection reset by 192.168.100.7 port 22",
    "root@192.168.100.7: Permission denied (publickey,password).",
    # Warnings ssh prints and then carries on to password auth.
    'Load key "/home/u/.ssh/id_rsa": Permission denied\n'
    "ssh: connect to host 192.168.100.7 port 22: Connection refused",
    "no such identity: /home/u/.ssh/id_x: No such file or directory\n"
    "ssh: connect to host 192.168.100.7 port 22: Connection refused",
]

# Failures on this host that no amount of waiting changes.
_PERMANENT_ERRORS = [
    (255, "Bad owner or permissions on /etc/ssh/ssh_config.d/pw-run.conf"),
    (
        255,
        "/home/u/.ssh/config: line 3: Bad configuration option: bogus\n"
        "/home/u/.ssh/config: terminating, 1 bad configuration options",
    ),
    (255, "Can't open user config file nope.conf: No such file or directory"),
    (
        255,
        "Received disconnect from 192.168.100.7 port 22:2: Too many "
        "authentication failures\nDisconnected from 192.168.100.7 port 22",
    ),
    (3, "Failed to get a pseudo terminal: No such file or directory"),
]


class TestSshPermanentFailure:
    @pytest.mark.parametrize("stderr", _BOOT_TIME_ERRORS)
    def test_boot_time_errors_are_not_permanent(self, stderr: str) -> None:
        from ltvm_pkg.vm_net import ssh_permanent_failure

        assert not ssh_permanent_failure(255, stderr)

    @pytest.mark.parametrize(("rc", "stderr"), _PERMANENT_ERRORS)
    def test_local_failures_are_permanent(self, rc: int, stderr: str) -> None:
        from ltvm_pkg.vm_net import ssh_permanent_failure

        assert ssh_permanent_failure(rc, stderr)

    def test_message_without_the_matching_exit_code_is_not(self) -> None:
        """rc 1 from the remote command, not ssh itself."""
        from ltvm_pkg.vm_net import ssh_permanent_failure

        assert not ssh_permanent_failure(1, "Bad configuration option: x")
        assert not ssh_permanent_failure(
            255, "PTY allocation request failed; pseudo terminal"
        )


class TestWaitForSshErrors:
    def test_timeout_reports_the_last_ssh_error(self) -> None:
        mock, msg = _wait(
            [
                _ssh_result(255, "ssh: connect to host x port 22: No route"),
                _ssh_result(
                    255,
                    "ssh: connect to host 192.168.100.7 port 22: "
                    "Connection refused\n",
                ),
            ]
        )
        assert mock.call_count > 2
        assert "SSH not ready after" in msg
        assert (
            "last ssh error: rc=255: ssh: connect to host 192.168.100.7 "
            "port 22: Connection refused" in msg
        )
        assert "No route" not in msg
        assert "LTVM_SSH_TIMEOUT" in msg

    def test_timeout_after_a_hung_probe_says_so(self) -> None:
        _, msg = _wait([subprocess.TimeoutExpired("ssh", 5)])
        assert "last ssh error: ssh probe timed out after 5s" in msg

    def test_failure_with_no_stderr_reports_the_rc(self) -> None:
        _, msg = _wait([_ssh_result(5, "")])
        assert "last ssh error: rc=5" in msg

    @pytest.mark.parametrize("stderr", _BOOT_TIME_ERRORS)
    def test_boot_time_errors_wait_out_the_timeout(self, stderr: str) -> None:
        mock, msg = _wait([_ssh_result(255, stderr)])
        assert mock.call_count > 2
        assert "SSH not ready after" in msg

    @pytest.mark.parametrize(("rc", "stderr"), _PERMANENT_ERRORS)
    def test_local_failure_stops_at_once(self, rc: int, stderr: str) -> None:
        mock, msg = _wait([_ssh_result(rc, stderr)])
        assert mock.call_count == 1
        assert "SSH not ready" not in msg
        assert "fails on this host, not in the guest" in msg
        assert stderr.splitlines()[0] in msg

    def test_local_failure_after_boot_errors_still_stops(self) -> None:
        bad = "Bad owner or permissions on /etc/ssh/ssh_config.d/pw-run.conf"
        mock, msg = _wait(
            [
                _ssh_result(255, "ssh: connect to host x: Connection refused"),
                _ssh_result(255, bad),
            ]
        )
        assert mock.call_count == 2
        assert bad in msg


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

    def test_named_kernel_selects_matching_image(self, tmp_path: Path) -> None:
        from ltvm_pkg import vm_state

        root = self._setup(tmp_path)
        from ltvm_pkg import target_config as tc_mod

        with (
            patch.object(vm_state, "_LTVM_ROOT", root),
            patch.object(
                vm_state, "TARGETS_YAML", root / "targets" / "targets.yaml"
            ),
            patch.object(
                tc_mod, "TARGETS_YAML", root / "targets" / "targets.yaml"
            ),
            patch.object(tc_mod, "TARGETS_DIR", root / "targets"),
            patch.object(tc_mod, "ARTIFACTS_DIR", root / "artifacts"),
        ):
            arts = vm_state.resolve_os_artifacts("rocky9", kernel="6.1-rhel9.7")
        assert arts.kernel.parent.name == "6.1-rhel9.7"
        assert arts.image == (
            root
            / "artifacts"
            / "rocky9"
            / "x86_64"
            / "images"
            / "6.1-rhel9.7"
            / "base.ext4"
        )

    def test_default_uses_default_kernel_image(self, tmp_path: Path) -> None:
        from ltvm_pkg import vm_state

        root = self._setup(tmp_path)
        from ltvm_pkg import target_config as tc_mod

        with (
            patch.object(vm_state, "_LTVM_ROOT", root),
            patch.object(
                vm_state, "TARGETS_YAML", root / "targets" / "targets.yaml"
            ),
            patch.object(
                tc_mod, "TARGETS_YAML", root / "targets" / "targets.yaml"
            ),
            patch.object(tc_mod, "TARGETS_DIR", root / "targets"),
            patch.object(tc_mod, "ARTIFACTS_DIR", root / "artifacts"),
        ):
            arts = vm_state.resolve_os_artifacts("rocky9")
        assert arts.image.parent.name == "5.14-rhel9.7"

    def test_missing_image_raises_with_hint(self, tmp_path: Path) -> None:
        from ltvm_pkg import vm_state

        root = self._setup(tmp_path)
        # Remove the 6.1 image to force the failure path.
        (
            root
            / "artifacts"
            / "rocky9"
            / "x86_64"
            / "images"
            / "6.1-rhel9.7"
            / "base.ext4"
        ).unlink()
        from ltvm_pkg import target_config as tc_mod

        with (
            patch.object(vm_state, "_LTVM_ROOT", root),
            patch.object(
                vm_state, "TARGETS_YAML", root / "targets" / "targets.yaml"
            ),
            patch.object(
                tc_mod, "TARGETS_YAML", root / "targets" / "targets.yaml"
            ),
            patch.object(tc_mod, "TARGETS_DIR", root / "targets"),
            patch.object(tc_mod, "ARTIFACTS_DIR", root / "artifacts"),
        ):
            with pytest.raises(FileNotFoundError, match="build image"):
                vm_state.resolve_os_artifacts("rocky9", kernel="6.1-rhel9.7")


class TestWaitForSshStretch:
    """A guest whose console keeps printing is booting, not stuck."""

    def _run(
        self, tmp_sockets: Path, grows_until: float, ready_at: float = 1e9
    ) -> tuple[bool, float, str]:
        from ltvm_pkg.vm_net import wait_for_ssh
        from ltvm_pkg.vm_state import VMInfo

        vm = VMInfo(name="co9-slow", ip="192.168.100.9", pid=4242)
        vm.log_path.write_text("")
        clock = _FakeClock()
        start = clock.now

        def probe(*_a: object, **_k: object) -> MagicMock:
            if clock.now - start < grows_until:
                with vm.log_path.open("a") as f:
                    f.write("[  OK  ] Started something.\n")
            if clock.now - start >= ready_at:
                return _ssh_result(0, "")
            return _ssh_result(255, "No route to host")

        with (
            patch("ltvm_pkg.vm_net.run_ssh", side_effect=probe),
            patch("ltvm_pkg.vm_net.is_running", return_value=True),
            # An x86_64 guest is emulated on an aarch64 host, whose wait
            # is TCG_SSH_WAIT_FACTOR times longer; these are about KVM's.
            patch("ltvm_pkg.vm_net.emulated", return_value=False),
            patch(
                "ltvm_pkg.vm_net.time.monotonic", side_effect=clock.monotonic
            ),
            patch("ltvm_pkg.vm_net.time.sleep", side_effect=clock.sleep),
            patch("ltvm_pkg.vm_net.die", side_effect=SystemExit) as die,
        ):
            try:
                wait_for_ssh(vm.ip, max_wait=120, vm=vm)
                ok = True
            except SystemExit:
                ok = False
        msg = die.call_args.args[0] if die.called else ""
        return ok, clock.now - start, msg

    def test_a_booting_guest_gets_more_time(self, tmp_sockets: Path) -> None:
        """Regression: cluster create rolled back five Rocky 10 nodes at
        120s while their consoles showed NetworkManager starting."""
        ok, took, _ = self._run(tmp_sockets, grows_until=200, ready_at=230)
        assert ok
        assert took >= 230

    def test_a_quiet_console_ends_the_wait(self, tmp_sockets: Path) -> None:
        ok, took, msg = self._run(tmp_sockets, grows_until=30)
        assert not ok
        assert took < 130
        assert "SSH not ready after" in msg

    def test_the_stretch_has_a_cap(self, tmp_sockets: Path) -> None:
        from ltvm_pkg.vm_net import ssh_wait_cap

        ok, took, _ = self._run(tmp_sockets, grows_until=1e9)
        assert not ok
        assert ssh_wait_cap(120) <= took < ssh_wait_cap(120) + 15

    def test_no_vm_no_stretch(self) -> None:
        _, msg = _wait([_ssh_result(255, "No route to host")], max_wait=30)
        assert "after 3" in msg
