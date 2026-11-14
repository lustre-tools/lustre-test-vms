"""vhost-net: the NIC args, the fallback, and the host side (doctor, install)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ltvm_pkg import qemu_run, vhost_net
from ltvm_pkg.nic_spec import NicSpec
from tests.test_qemu_run import (  # noqa: F401
    _force_linux_host,
    _LaunchHarness,
    _make_vm,
    _run_launch,
    tmp_vmdir,
)

HELPER = Path("/usr/lib/qemu/qemu-bridge-helper")


class TestNetdev:
    def test_helper_with_vhost(self) -> None:
        # -netdev bridge has no vhost=; a tap netdev runs the helper itself
        assert (
            qemu_run.bridge_netdev("net0", "tap0", HELPER, True)
            == f"tap,id=net0,helper={HELPER},br=fcbr0,vhost=on"
        )

    def test_helper_without_vhost_is_unchanged(self) -> None:
        assert (
            qemu_run.bridge_netdev("net1", "tap1", HELPER, False)
            == f"bridge,id=net1,br=fcbr0,helper={HELPER}"
        )

    def test_own_tap(self) -> None:
        assert (
            qemu_run.bridge_netdev("net0", "tap0", None, True)
            == "tap,id=net0,ifname=tap0,script=no,downscript=no,vhost=on"
        )
        assert (
            qemu_run.bridge_netdev("net0", "tap0", None, False)
            == "tap,id=net0,ifname=tap0,script=no,downscript=no"
        )


@pytest.fixture
def no_site_config(monkeypatch, tmp_path: Path) -> Path:
    conf = tmp_path / "ltvm.conf"
    monkeypatch.setenv("LTVM_SITE_CONFIG", str(conf))
    monkeypatch.delenv("LTVM_VHOST_NET", raising=False)
    return conf


class TestSetting:
    def test_default_auto(self, no_site_config: Path) -> None:
        assert qemu_run.vhost_net_setting() == "auto"

    def test_site_config_off(self, no_site_config: Path) -> None:
        no_site_config.write_text("[network]\nvhost_net = off\n")
        assert qemu_run.vhost_net_setting() == "off"

    def test_env_wins(self, no_site_config: Path, monkeypatch) -> None:
        no_site_config.write_text("[network]\nvhost_net = off\n")
        monkeypatch.setenv("LTVM_VHOST_NET", "auto")
        assert qemu_run.vhost_net_setting() == "auto"
        monkeypatch.setenv("LTVM_VHOST_NET", "0")
        assert qemu_run.vhost_net_setting() == "off"


class TestUsable:
    @pytest.fixture
    def device(self, tmp_path: Path, no_site_config: Path):
        dev = tmp_path / "vhost-net"
        dev.write_text("")
        with patch("ltvm_pkg.qemu_run.VHOST_NET", dev):
            yield dev

    def test_opens(self, device: Path) -> None:
        assert qemu_run.vhost_net_usable(as_user=True)

    def test_off(self, device: Path, monkeypatch) -> None:
        monkeypatch.setenv("LTVM_VHOST_NET", "off")
        assert not qemu_run.vhost_net_usable(as_user=True)

    def test_no_device(self, device: Path) -> None:
        device.unlink()
        assert not qemu_run.vhost_net_usable(as_user=False)

    def test_user_cannot_open(self, device: Path) -> None:
        with patch("ltvm_pkg.qemu_run.os.open", side_effect=PermissionError):
            assert not qemu_run.vhost_net_usable(as_user=True)

    def test_root_qemu_needs_the_module(self, device: Path) -> None:
        with (
            patch("ltvm_pkg.qemu_run.os.open", side_effect=PermissionError),
            patch("ltvm_pkg.qemu_run.Path.is_dir", return_value=True),
        ):
            assert qemu_run.vhost_net_usable(as_user=False)
        with (
            patch("ltvm_pkg.qemu_run.os.open", side_effect=PermissionError),
            patch("ltvm_pkg.qemu_run.Path.is_dir", return_value=False),
        ):
            assert not qemu_run.vhost_net_usable(as_user=False)

    def test_module_missing(self, device: Path) -> None:
        # the node exists, but opening it finds no driver
        with patch("ltvm_pkg.qemu_run.os.open", side_effect=OSError(19, "x")):
            assert not qemu_run.vhost_net_usable(as_user=False)


class TestLaunch:
    def _argv(self, vmdir: Path, usable: bool) -> list[str]:
        vm = _make_vm(vmdir)
        vm.nics = [NicSpec(type="tcp")]
        vm.nic_ips = ["172.16.100.50"]
        h = _LaunchHarness()
        with patch("ltvm_pkg.qemu_run.vhost_net_usable", return_value=usable):
            _run_launch(vm, h)
        return h.qemu_argv or []

    def test_every_nic_gets_vhost(self, tmp_vmdir: Path) -> None:  # noqa: F811
        argv = self._argv(tmp_vmdir, True)
        netdevs = [argv[i + 1] for i, a in enumerate(argv) if a == "-netdev"]
        assert len(netdevs) == 2
        assert all(n.endswith(",vhost=on") for n in netdevs)

    def test_fallback(self, tmp_vmdir: Path) -> None:  # noqa: F811
        argv = self._argv(tmp_vmdir, False)
        assert "vhost=on" not in " ".join(argv)


SOOKIE = """\
install tap /usr/bin/logger -t modblocker "blocked tap"; /bin/false
install vhost /usr/bin/logger -t modblocker "blocked vhost"; /bin/false
install vhost_net /usr/bin/logger -t modblocker "blocked vhost_net"; /bin/false
install nvidia /sbin/modprobe --ignore-install nvidia; /bin/true
install vhost_iotlb /bin/true
alias foo bar
"""


def test_blocked_rules() -> None:
    assert vhost_net.blocked(SOOKIE) == [
        "tap",
        "vhost",
        "vhost_net",
        "vhost_iotlb",
    ]
    assert (
        vhost_net.blocked(
            "install vhost_net /sbin/modprobe --ignore-install vhost_net\n"
        )
        == []
    )
    assert vhost_net.blocked("") == []


class TestDoctor:
    def test_blocked_is_reported_not_fixed(self, no_site_config: Path) -> None:
        with (
            patch("ltvm_pkg.vhost_net.blocked", return_value=["vhost_net"]),
            patch("ltvm_pkg.vhost_net.ensure") as ensure,
        ):
            rep = vhost_net.doctor(fix=True, qemu_as_user=True)
        ensure.assert_not_called()
        assert rep.issues == 0 and "blocked" in rep.lines[0]

    def test_fix_loads_and_persists(self, no_site_config: Path) -> None:
        with (
            patch("ltvm_pkg.vhost_net.blocked", return_value=[]),
            patch("ltvm_pkg.vhost_net.loaded", return_value=False),
            patch("ltvm_pkg.vhost_net.persistent", return_value=False),
            patch("ltvm_pkg.vhost_net.DEVICE", Path("/nonexistent")),
            patch("ltvm_pkg.vhost_net.ensure") as ensure,
        ):
            rep = vhost_net.doctor(fix=True, qemu_as_user=True)
        ensure.assert_called_once()
        assert rep.issues == 1 and rep.failures == 0

    def test_ensure(self) -> None:
        with (
            patch("ltvm_pkg.vhost_net.blocked", return_value=[]),
            patch("ltvm_pkg.vhost_net.loaded", return_value=False),
            patch("ltvm_pkg.vhost_net.persistent", return_value=False),
            patch("ltvm_pkg.priv.sudo_run") as sudo_run,
            patch("ltvm_pkg.priv.atomic_write") as write,
        ):
            vhost_net.ensure()
        sudo_run.assert_called_once_with(["modprobe", "vhost_net"], quiet=True)
        assert write.call_args[0][0] == vhost_net.MODULES_LOAD
        assert "vhost_net" in write.call_args[0][1]

    def test_ensure_refuses_when_blocked(self) -> None:
        with (
            patch("ltvm_pkg.vhost_net.blocked", return_value=["tap"]),
            patch("ltvm_pkg.priv.sudo_run") as sudo_run,
            pytest.raises(vhost_net.Blocked),
        ):
            vhost_net.ensure()
        sudo_run.assert_not_called()

    def test_permissions(self, no_site_config: Path, tmp_path: Path) -> None:
        dev = tmp_path / "vhost-net"
        dev.write_text("")
        with (
            patch("ltvm_pkg.vhost_net.blocked", return_value=[]),
            patch("ltvm_pkg.vhost_net.loaded", return_value=True),
            patch("ltvm_pkg.vhost_net.persistent", return_value=True),
            patch("ltvm_pkg.vhost_net.DEVICE", dev),
            patch("ltvm_pkg.vhost_net.os.access", return_value=False),
        ):
            rep = vhost_net.doctor(fix=False, qemu_as_user=True)
            assert rep.issues == 1 and "add the user to group" in rep.lines[0]
            # a root QEMU does not care
            assert vhost_net.doctor(fix=False, qemu_as_user=False).issues == 0


def test_install_sets_it_up() -> None:
    from ltvm_pkg import host_setup

    with patch("ltvm_pkg.vhost_net.ensure", side_effect=vhost_net.Blocked("x")):
        host_setup._setup_vhost_net()  # a blocked host is not an error
    with patch("ltvm_pkg.vhost_net.ensure") as ensure:
        host_setup._setup_vhost_net()
    ensure.assert_called_once()
