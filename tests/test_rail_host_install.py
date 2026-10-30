"""Host install, uninstall privilege, and checked network failure regressions."""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ltvm_pkg import host_setup, host_uninstall, rail_host
from tests.test_rail_host import FakeIPRoute, _profile_vm
from tests.test_rail_host import network_api as network_api

_NETWORK_CONTEXT = rail_host._iproute


def test_install_validates_service_before_creating_bridges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unsupported service path must leave live host networking untouched."""
    ensure = MagicMock()
    run = MagicMock()
    monkeypatch.setattr(host_setup, "REPO_ROOT", tmp_path / "$runtime")
    monkeypatch.setattr(rail_host, "ensure_bridges", ensure)
    monkeypatch.setattr(host_setup, "_run", run)
    with pytest.raises(ValueError, match="unsupported character"):
        host_setup.install_rail_network()
    ensure.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("root", [False, True])
def test_uninstall_bridge_fallback_uses_required_privilege(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, root: bool
) -> None:
    """A failed or inactive unit still gets API cleanup with enough privilege."""
    unit = tmp_path / "ltvm-rail-bridges.service"
    unit.write_text("test unit\n")
    remove = MagicMock()
    elevated = MagicMock()
    monkeypatch.setattr(host_setup, "RAIL_UNIT_PATH", unit)
    monkeypatch.setattr(host_setup, "REPO_ROOT", tmp_path / "installed project")
    monkeypatch.setattr(host_setup, "_systemctl_ok", lambda *args: False)
    monkeypatch.setattr(rail_host, "remove_bridges", remove)
    monkeypatch.setattr(
        host_uninstall.os, "geteuid", lambda: 0 if root else 1000
    )
    monkeypatch.setattr(host_uninstall, "sudo_run", elevated)
    monkeypatch.setattr(host_uninstall, "_rm", lambda *paths: [])
    monkeypatch.setattr(host_uninstall, "which", lambda *args: None)
    host_uninstall._linux_services(host_uninstall.Survey(macos=False))
    if root:
        remove.assert_called_once_with()
        elevated.assert_not_called()
    else:
        remove.assert_not_called()
        elevated.assert_called_once_with(
            [
                "env",
                f"PYTHONPATH={tmp_path / 'installed project'}",
                host_uninstall.sys.executable,
                "-m",
                "ltvm_pkg.rail_host",
                "stop",
            ],
            quiet=True,
        )


def test_uninstall_keeps_unit_when_bridge_fallback_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cleanup errors retain the service file so the user can retry safely."""
    unit = tmp_path / "ltvm-rail-bridges.service"
    unit.write_text("test unit\n")
    remove_files = MagicMock()
    monkeypatch.setattr(host_setup, "RAIL_UNIT_PATH", unit)
    monkeypatch.setattr(host_setup, "_systemctl_ok", lambda *args: False)
    monkeypatch.setattr(host_uninstall.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        host_uninstall,
        "sudo_run",
        MagicMock(side_effect=RuntimeError("cleanup refused")),
    )
    monkeypatch.setattr(host_uninstall, "_rm", remove_files)
    with pytest.raises(RuntimeError, match="cleanup refused"):
        host_uninstall._linux_services(host_uninstall.Survey(macos=False))
    remove_files.assert_not_called()
    assert unit.read_text() == "test unit\n"


@pytest.mark.parametrize("failure", ["kernel", "os"])
def test_network_errors_are_checked_after_bridge_rollback(
    network_api: FakeIPRoute, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Real kernel exception types must not escape the CLI or bypass rollback."""
    pyroute2 = pytest.importorskip(
        "pyroute2", reason="Linux network API dependency is unavailable"
    )
    from pyroute2.netlink.exceptions import NetlinkError

    real_link = network_api.link

    def fail_second_bridge(action: str, **values: Any) -> None:
        """Fail one alias update after both new bridge indices were recorded."""
        if action == "set" and values.get("ifalias") and values["index"] == 11:
            if failure == "kernel":
                raise NetlinkError(errno.EPERM)
            raise PermissionError(errno.EPERM, "not permitted")
        real_link(action, **values)

    monkeypatch.setattr(network_api, "link", fail_second_bridge)
    monkeypatch.setattr(pyroute2, "IPRoute", lambda: network_api)
    monkeypatch.setattr(rail_host, "_iproute", _NETWORK_CONTEXT)
    with pytest.raises(RuntimeError, match="rail network API failed"):
        rail_host.ensure_bridges()
    assert network_api.links == {}
    assert [
        values["index"]
        for action, values in network_api.calls
        if action == "del"
    ] == [11, 10]


def test_network_context_preserves_programming_errors(
    network_api: FakeIPRoute, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unexpected implementation errors remain visible instead of becoming setup errors."""
    pyroute2 = pytest.importorskip(
        "pyroute2", reason="Linux network API dependency is unavailable"
    )

    monkeypatch.setattr(pyroute2, "IPRoute", lambda: network_api)
    monkeypatch.setattr(rail_host, "_iproute", _NETWORK_CONTEXT)
    with pytest.raises(TypeError, match="unexpected implementation error"):
        with rail_host._iproute():
            raise TypeError("unexpected implementation error")


def test_install_checks_bridge_removed_during_inspection(
    network_api: FakeIPRoute, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link disappearing between API reads is a checked race, with no mutation."""
    network_api.add_existing("fcbr-data")
    monkeypatch.setattr(network_api, "get_links", lambda index: [])
    with pytest.raises(RuntimeError, match="disappeared during inspection"):
        rail_host.ensure_bridges()
    assert network_api.calls == []


def test_tap_cleanup_checks_device_removed_during_inspection(
    network_api: FakeIPRoute, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TAP disappearing between kernel reads is a checked failure, not IndexError."""
    vm = _profile_vm()
    network_api.add_existing("fcbr0", alias="management")
    network_api.add_existing(
        vm.tap, kind="tuntap", alias=rail_host.tap_owner(vm)
    )
    monkeypatch.setattr(rail_host.os, "geteuid", lambda: 0)
    monkeypatch.setattr(network_api, "get_links", lambda index: [])
    with pytest.raises(RuntimeError, match="disappeared during inspection"):
        rail_host.remove_taps(vm)
    assert network_api.calls == []
