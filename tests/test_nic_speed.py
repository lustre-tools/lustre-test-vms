"""The link speed virtio-net NICs advertise (speed=, --eth0-speed)."""

from __future__ import annotations

import argparse
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import vm_cluster, vm_commands
from ltvm_pkg.nic_spec import (
    DEFAULT_SPEED,
    NicSpec,
    NicSpecError,
    parse_speed,
    speed_device_opts,
)
from ltvm_pkg.vm_state import VMInfo
from tests.test_disk_options import _vm, parser, vm_dirs  # noqa: F401
from tests.test_qemu_run import (  # noqa: F401
    _force_linux_host,
    _LaunchHarness,
    _make_vm,
    _run_launch,
    tmp_vmdir,
)


class TestParseSpeed:
    @pytest.mark.parametrize(
        "raw, mbps",
        [
            ("100000", 100000),
            ("25000", 25000),
            ("0", 0),
            ("none", 0),
            ("NONE", 0),
            (" 10000 ", 10000),
        ],
    )
    def test_accepts(self, raw: str, mbps: int) -> None:
        assert parse_speed(raw) == mbps

    @pytest.mark.parametrize("raw", ["", "-1", "10G", "1e5", "²", str(2**31)])
    def test_rejects(self, raw: str) -> None:
        with pytest.raises(NicSpecError, match="expected Mbps"):
            parse_speed(raw)


class TestDeviceOpts:
    def test_default_is_100g(self) -> None:
        assert DEFAULT_SPEED == 100000
        assert speed_device_opts(None) == ",speed=100000,duplex=full"

    def test_explicit(self) -> None:
        assert speed_device_opts(25000) == ",speed=25000,duplex=full"

    def test_zero_advertises_nothing(self) -> None:
        assert speed_device_opts(0) == ""


class TestNicSpecKey:
    def test_default_is_unset(self) -> None:
        assert NicSpec.parse("tcp").speed is None

    @pytest.mark.parametrize("nic_type", ["tcp", "softroce"])
    def test_takes_speed(self, nic_type: str) -> None:
        spec = NicSpec.parse(f"{nic_type}:speed=25000")
        assert spec.speed == 25000
        assert str(spec) == f"{nic_type}:speed=25000"
        assert NicSpec.parse(str(spec)) == spec

    def test_none_is_stored_as_zero(self) -> None:
        spec = NicSpec.parse("tcp:speed=none,net=tcp1")
        assert spec.speed == 0
        assert str(spec) == "tcp:net=tcp1,speed=0"
        assert NicSpec.parse(str(spec)) == spec

    def test_passthrough_refuses_it(self) -> None:
        with pytest.raises(NicSpecError, match="unknown key 'speed'"):
            NicSpec.parse("passthrough:0000:85:00.1,speed=1000")

    def test_bad_value(self) -> None:
        with pytest.raises(NicSpecError, match="expected Mbps"):
            NicSpec.parse("tcp:speed=fast")

    def test_does_not_reach_the_guest_cmdline(self) -> None:
        assert NicSpec.parse("tcp:speed=0").cmdline_entry() == "tcp"


def _net_devices(args: list[str]) -> list[str]:
    return [
        args[i + 1]
        for i, a in enumerate(args[:-1])
        if a == "-device" and args[i + 1].startswith("virtio-net-pci")
    ]


class TestLaunchArgs:
    def test_every_virtio_nic_gets_the_default(self, tmp_vmdir: Any) -> None:  # noqa: F811
        vm = _make_vm(tmp_vmdir)
        vm.nics = [NicSpec.parse("tcp"), NicSpec.parse("softroce")]
        h = _LaunchHarness()
        _run_launch(vm, h)
        devices = _net_devices(h.qemu_args)
        assert len(devices) == 3
        for dev in devices:
            assert dev.endswith(",speed=100000,duplex=full")

    def test_per_nic_values(self, tmp_vmdir: Any) -> None:  # noqa: F811
        vm = _make_vm(tmp_vmdir)
        vm.eth0_speed = 0
        vm.nics = [
            NicSpec.parse("tcp:speed=25000"),
            NicSpec.parse("tcp:speed=none"),
        ]
        h = _LaunchHarness()
        _run_launch(vm, h)
        eth0, eth1, eth2 = _net_devices(h.qemu_args)
        assert "speed=" not in eth0 and "duplex=" not in eth0
        assert eth1.endswith(",speed=25000,duplex=full")
        assert "speed=" not in eth2

    def test_passthrough_is_left_alone(self, tmp_vmdir: Any) -> None:  # noqa: F811
        vm = _make_vm(tmp_vmdir)
        vm.nics = [NicSpec.parse("passthrough:0000:85:00.1")]
        h = _LaunchHarness()
        _run_launch(vm, h)
        vfio = [a for a in h.qemu_args if a.startswith("vfio-pci")]
        assert vfio and "speed=" not in vfio[0]


class TestVMInfo:
    def test_round_trip(self, vm_dirs: Any) -> None:  # noqa: F811
        for speed in (None, 0, 25000):
            _vm("s-a", eth0_speed=speed)
            assert VMInfo.load("s-a").eth0_speed == speed

    def test_older_info_loads_as_default(self, vm_dirs: Any) -> None:  # noqa: F811
        sockets, _, _ = vm_dirs
        vm = _vm("s-old")
        text = vm.info_path.read_text()
        vm.info_path.write_text(
            "".join(
                line
                for line in text.splitlines(keepends=True)
                if not line.startswith("ETH0_SPEED=")
            )
        )
        assert VMInfo.load("s-old").eth0_speed is None


class TestVmSet:
    def _set(self, speed: str, **vm_kw: Any) -> VMInfo:
        vm = _vm("s-a", **vm_kw)
        ns = argparse.Namespace(
            name=vm.name,
            vcpus=None,
            mem=None,
            numa_nodes=None,
            eth0_speed=speed,
        )
        with patch.object(vm_commands, "is_running", return_value=False):
            vm_commands.cmd_set(ns)
        return VMInfo.load(vm.name)

    def test_sets_it(self, vm_dirs: Any) -> None:  # noqa: F811
        assert self._set("25000").eth0_speed == 25000

    def test_none(self, vm_dirs: Any) -> None:  # noqa: F811
        assert self._set("none").eth0_speed == 0

    def test_default_clears_it(self, vm_dirs: Any) -> None:  # noqa: F811
        assert self._set("default", eth0_speed=0).eth0_speed is None

    def test_bad_value(self, vm_dirs: Any) -> None:  # noqa: F811
        with pytest.raises(SystemExit):
            self._set("fast")


class TestCluster:
    def _argv(self, **kw: Any) -> list[str]:
        node = vm_cluster.parse_node_spec("oss:s-oss:2")
        with patch.object(vm_cluster, "_run_prefixed") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._create_one_node(node, 2, None, **kw)
        return run.call_args.args[0]

    def test_reaches_each_node(self) -> None:
        argv = self._argv(eth0_speed=0)
        assert argv[argv.index("--eth0-speed") + 1] == "0"

    def test_unset_adds_nothing(self) -> None:
        assert "--eth0-speed" not in self._argv()


class TestParser:
    def test_default_is_unset(
        self,
        parser: argparse.ArgumentParser,  # noqa: F811
    ) -> None:
        assert parser.parse_args(["create", "co1-x"]).eth0_speed is None

    def test_create_and_cluster(
        self,
        parser: argparse.ArgumentParser,  # noqa: F811
    ) -> None:
        a = parser.parse_args(["create", "co1-x", "--eth0-speed", "none"])
        assert a.eth0_speed == 0
        a = parser.parse_args(
            ["cluster", "create", "co1", "oss:co1-o:1", "--eth0-speed=25000"]
        )
        assert a.eth0_speed == 25000

    def test_bad_value_is_refused(
        self,
        parser: argparse.ArgumentParser,  # noqa: F811
    ) -> None:
        with pytest.raises(SystemExit):
            parser.parse_args(["create", "co1-x", "--eth0-speed", "10G"])

    def test_vm_set_takes_default(
        self,
        parser: argparse.ArgumentParser,  # noqa: F811
    ) -> None:
        a = parser.parse_args(["vm", "set", "co1-x", "--eth0-speed", "default"])
        assert a.eth0_speed == "default"
