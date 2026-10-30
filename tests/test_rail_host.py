"""Host bridge API requests, ownership checks, and bounded cleanup."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, call

import pytest

from ltvm_pkg import host_setup, host_uninstall, rail_host, rootless
from ltvm_pkg.rail_topology import resolve_topology
from ltvm_pkg.vm_net import mac_for_name, tap_for_name
from ltvm_pkg.vm_state import VMInfo


class Nla(dict[str, Any]):
    """Represent the named netlink attributes consumed by the host adapter."""

    def get_attr(self, name: str) -> Any:
        """Return a named attribute using pyroute2's read interface."""
        return self.get(name)


class FakeIPRoute:
    """Model only the kernel objects required by bridge and TAP operations."""

    def __init__(self, sys_net: Path) -> None:
        """Keep bounded fake interfaces and an exact record of mutation requests."""
        self.sys_net = sys_net
        self.links: dict[str, Nla] = {}
        self.addresses: dict[int, list[dict[str, str]]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_set: str | None = None

    def __enter__(self) -> FakeIPRoute:
        """Open the fake session without contacting the kernel."""
        return self

    def __exit__(self, *args: Any) -> None:
        """The fake session owns no real file descriptor."""

    def add_existing(
        self,
        name: str,
        kind: str = "bridge",
        alias: str = rail_host.OWNER,
        mtu: int = 1500,
    ) -> int:
        """Register one pre-existing device and matching fake sysfs attributes."""
        index = 10 + len(self.links)
        self.links[name] = Nla(
            index=index,
            IFLA_LINKINFO=Nla(IFLA_INFO_KIND=kind),
            IFLA_IFALIAS=alias,
            IFLA_MTU=mtu,
        )
        directory = self.sys_net / name
        directory.mkdir(exist_ok=True)
        if kind == "bridge":
            # Attached-port checks read the kernel's bridge port directory.
            (directory / "bridge").mkdir(exist_ok=True)
            (directory / "brif").mkdir(exist_ok=True)
        (directory / "ifalias").write_text(alias + "\n")
        (directory / "flags").write_text("0x1\n")
        (directory / "mtu").write_text(f"{mtu}\n")
        return index

    def link_lookup(self, *, ifname: str) -> list[int]:
        """Find a unique interface exactly as the API does for a name."""
        return [self.links[ifname]["index"]] if ifname in self.links else []

    def get_links(self, index: int) -> list[Nla]:
        """Return the full attributes of the selected fake interface."""
        return [link for link in self.links.values() if link["index"] == index]

    def get_addr(self, *, index: int) -> list[dict[str, str]]:
        """Expose configured host addresses without inventing an empty result."""
        return self.addresses.get(index, [])

    def link(self, action: str, **values: Any) -> None:
        """Apply a requested mutation, with one deterministic failure injection."""
        self.calls.append((action, dict(values)))
        if action == "add":
            self.add_existing(
                values["ifname"], values["kind"], values.get("ifalias", "")
            )
            return
        name = next(
            name
            for name, link in self.links.items()
            if link["index"] == values["index"]
        )
        if action == "set":
            if name == self.fail_set:
                # Fail after creation so rollback must remove this device too.
                raise RuntimeError("injected link setup failure")
            self.links[name].update(
                {key: value for key, value in values.items() if key != "index"}
            )
            if "ifalias" in values:
                self.links[name]["IFLA_IFALIAS"] = values["ifalias"]
            if "master" in values:
                self.links[name]["IFLA_MASTER"] = values["master"]
            if "mtu" in values:
                # Mirror both kernel views so direct reads verify API writes.
                self.links[name]["IFLA_MTU"] = values["mtu"]
                (self.sys_net / name / "mtu").write_text(f"{values['mtu']}\n")
            return
        if action == "del":
            del self.links[name]
            return
        raise AssertionError(f"unexpected API operation {action}")


@pytest.fixture
def network_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeIPRoute:
    """Replace the kernel API and every writable network path with local fakes."""
    sys_net = tmp_path / "net"
    sys_net.mkdir()
    proc_sys = tmp_path / "proc-sys"
    for name in rail_host.bridge_names():
        directory = proc_sys / "net/ipv6/conf" / name
        directory.mkdir(parents=True)
        (directory / "disable_ipv6").write_text("0\n")
    api = FakeIPRoute(sys_net)
    monkeypatch.setattr(rail_host, "SYS_NET", sys_net)
    monkeypatch.setattr(rail_host, "PROC_SYS", proc_sys)
    monkeypatch.setattr(rail_host, "_iproute", MagicMock(return_value=api))
    monkeypatch.setattr(
        rail_host.platform, "system", MagicMock(return_value="Linux")
    )
    return api


def _profile_vm(mode: str = "isolated") -> VMInfo:
    """Build valid profile metadata for host lifecycle tests."""
    topology = resolve_topology(["tcp", "tcp"], mode)
    assert topology is not None
    ips = [
        rail.subnet4.rsplit(".", 1)[0] + f".{30 + index}"
        for index, rail in enumerate(topology.rails)
    ]
    name = "co1-host"
    return VMInfo(
        name,
        "192.168.100.30",
        tap=tap_for_name(name),
        mac=mac_for_name(name),
        nics=["tcp", "tcp"],
        nic_ips=ips,
        nic_ip6s=[rail.ipv6_for(ip) for rail, ip in zip(topology.rails, ips)],
        rail_topology=topology,
    )


def test_install_uses_exact_unnumbered_bridge_api(
    network_api: FakeIPRoute,
) -> None:
    """Only nine owned bridges are created; no host addresses, routes, or NAT."""
    rail_host.ensure_bridges()
    names = rail_host.bridge_names()
    assert len(names) == 9
    assert set(network_api.links) == set(names)
    assert network_api.calls == [
        call
        for index, name in enumerate(names, start=10)
        for call in (
            (
                "add",
                {"ifname": name, "kind": "bridge"},
            ),
            ("set", {"index": index, "ifalias": rail_host.OWNER}),
            ("set", {"index": index, "mtu": 4200, "state": "up"}),
        )
    ]
    for name in names:
        assert (
            rail_host.PROC_SYS / "net/ipv6/conf" / name / "disable_ipv6"
        ).read_text() == "1\n"


@pytest.mark.parametrize(
    "kind,alias", [("dummy", rail_host.OWNER), ("bridge", "administrator")]
)
def test_install_refuses_foreign_device_before_any_mutation(
    network_api: FakeIPRoute, kind: str, alias: str
) -> None:
    """A reserved name never grants ownership over an existing device."""
    network_api.add_existing("fcbr-rail3", kind=kind, alias=alias)
    with pytest.raises(RuntimeError, match="unmanaged"):
        rail_host.ensure_bridges()
    assert network_api.calls == []


def test_install_refuses_numbered_data_bridge(network_api: FakeIPRoute) -> None:
    """A host address cannot silently route between the data fabric and host."""
    index = network_api.add_existing("fcbr-data")
    network_api.addresses[index] = [{"address": "172.18.100.1"}]
    with pytest.raises(RuntimeError, match="host addresses"):
        rail_host.ensure_bridges()
    assert network_api.calls == []


def test_install_rollback_keeps_preexisting_owned_bridge(
    network_api: FakeIPRoute,
) -> None:
    """A partial install removes only devices created by that invocation."""
    network_api.add_existing("fcbr-data")
    network_api.fail_set = "fcbr-rail2"
    with pytest.raises(RuntimeError, match="injected"):
        rail_host.ensure_bridges()
    assert set(network_api.links) == {"fcbr-data"}
    assert [call for call in network_api.calls if call[0] == "del"] == [
        ("del", {"index": 12}),
        ("del", {"index": 11}),
    ]


def test_repeat_install_does_not_create_duplicate_bridges(
    network_api: FakeIPRoute,
) -> None:
    """A second install converges owned fabrics without creating more devices."""
    rail_host.ensure_bridges()
    network_api.calls.clear()
    rail_host.ensure_bridges()
    assert len(network_api.calls) == 9
    assert all(action == "set" for action, _values in network_api.calls)


def test_uninstall_preflights_attached_ports_before_deleting(
    network_api: FakeIPRoute,
) -> None:
    """One busy bridge prevents partial teardown of every other data fabric."""
    rail_host.ensure_bridges()
    network_api.calls.clear()
    (rail_host.SYS_NET / "fcbr-rail2/brif/tap-foreign").touch()
    with pytest.raises(RuntimeError, match="attached"):
        rail_host.remove_bridges()
    assert network_api.calls == []
    assert len(network_api.links) == 9


def test_uninstall_deletes_only_the_fixed_owned_allowlist(
    network_api: FakeIPRoute,
) -> None:
    """An unrelated bridge survives cleanup, even when no guest is running."""
    rail_host.ensure_bridges()
    network_api.add_existing("administrator-br", alias="administrator")
    network_api.calls.clear()
    rail_host.remove_bridges()
    assert set(network_api.links) == {"administrator-br"}
    assert network_api.calls == [
        ("del", {"index": index}) for index in range(18, 9, -1)
    ]


def test_require_bridges_is_read_only_and_checks_real_state(
    network_api: FakeIPRoute,
) -> None:
    """Start preflight reads kernel state and refuses down or numbered fabrics."""
    vm = _profile_vm()
    assert vm.rail_topology is not None
    for rail in vm.rail_topology.rails:
        network_api.add_existing(rail.bridge, mtu=4200)
    rail_host.require_bridges(vm.rail_topology)
    index = network_api.links["fcbr-rail2"]["index"]
    network_api.addresses[index] = [{"address": "172.18.102.1"}]
    with pytest.raises(RuntimeError, match="host addresses"):
        rail_host.require_bridges(vm.rail_topology)
    network_api.addresses[index] = []
    (rail_host.SYS_NET / "fcbr-rail2/flags").write_text("0x0\n")
    with pytest.raises(RuntimeError, match="down"):
        rail_host.require_bridges(vm.rail_topology)
    assert network_api.calls == []


def test_missing_stopped_taps_need_no_root_or_api(
    network_api: FakeIPRoute, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Helper-created TAPs normally disappear; cleanup must not ask for root."""
    monkeypatch.setattr(rail_host.os, "geteuid", MagicMock(return_value=1000))
    forbidden = MagicMock(
        side_effect=AssertionError("missing TAP cleanup opened netlink")
    )
    monkeypatch.setattr(rail_host, "_iproute", forbidden)
    rail_host.remove_taps(_profile_vm())
    forbidden.assert_not_called()


def test_prepare_taps_rolls_back_new_devices_on_collision(
    network_api: FakeIPRoute,
) -> None:
    """A foreign extra TAP cannot be replaced, and the new management TAP is removed."""
    vm = _profile_vm()
    network_api.add_existing("fcbr0", alias="management")
    assert vm.rail_topology is not None
    for rail in vm.rail_topology.rails:
        network_api.add_existing(rail.bridge, mtu=4200)
    extra_tap = vm.extra_nics()[0][2]
    network_api.add_existing(extra_tap, kind="tuntap", alias="foreign")
    before = set(network_api.links)
    with pytest.raises(RuntimeError, match="exists"):
        rail_host.prepare_taps(vm)
    assert set(network_api.links) == before
    assert vm.tap not in network_api.links


@pytest.mark.parametrize(
    "bad",
    [
        "/tmp/path\nnew",
        '/tmp/with"quote',
        "/tmp/%token",
        "/tmp/$token",
        "/tmp/back\\slash",
        "/tmp/tab\tpath",
    ],
)
def test_service_paths_cannot_inject_unit_directives(bad: str) -> None:
    """Invalid runtime paths fail before a root service can be written."""
    with pytest.raises(ValueError, match="unsupported character"):
        rail_host.render_service("/usr/bin/python3", Path(bad))


def test_install_service_uses_the_same_runtime_and_exact_unit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Boot persistence uses the project interpreter and the fixed host service."""
    unit = tmp_path / "ltvm-rail-bridges.service"
    ensure = MagicMock()
    run = MagicMock()
    monkeypatch.setattr(rail_host, "ensure_bridges", ensure)
    monkeypatch.setattr(host_setup, "RAIL_UNIT_PATH", unit)
    monkeypatch.setattr(host_setup, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(host_setup, "_run", run)
    host_setup.install_rail_network()
    ensure.assert_called_once_with()
    assert unit.read_text() == rail_host.render_service(
        host_setup.sys.executable, tmp_path
    )
    assert run.call_args_list == [
        call(["systemctl", "daemon-reload"]),
        call(["systemctl", "enable", "--now", host_setup.RAIL_UNIT]),
    ]


def test_helper_acl_honors_admin_deny_and_preserves_other_rules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Installation grants only fixed fabrics and does not weaken an explicit deny."""
    acl = tmp_path / "bridge.conf"
    acl.write_text("allow administrator-br\ndeny fcbr-rail2\n")
    monkeypatch.setattr(rootless, "BRIDGE_ACL", acl)
    monkeypatch.setattr(
        rootless,
        "installed_helper",
        MagicMock(return_value=tmp_path / "helper"),
    )
    monkeypatch.setattr(
        rootless, "is_setuid_root", MagicMock(return_value=True)
    )
    host_setup._setup_bridge_helper(
        cast(host_setup.HostInfo, SimpleNamespace(pkg_mgr="apt"))
    )
    assert "allow administrator-br\n" in acl.read_text()
    assert "deny fcbr-rail2\n" in acl.read_text()
    assert not rootless.bridge_allowed("fcbr-rail2", acl)
    assert all(
        rootless.bridge_allowed(name, acl)
        for name in rail_host.bridge_names()
        if name != "fcbr-rail2"
    )
    host_uninstall._linux_bridge_acl()
    assert acl.read_text() == "allow administrator-br\ndeny fcbr-rail2\n"


def test_tap_creation_attaches_every_nic_with_vm_ownership(
    network_api: FakeIPRoute, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact kernel requests bind management and data TAPs to their fabrics."""
    vm = _profile_vm()
    monkeypatch.setattr(
        rail_host.pwd,
        "getpwnam",
        MagicMock(return_value=SimpleNamespace(pw_uid=1234)),
    )
    network_api.add_existing("fcbr0", alias="management")
    assert vm.rail_topology is not None
    for rail in vm.rail_topology.rails:
        network_api.add_existing(rail.bridge, mtu=4200)
    rail_host.prepare_taps(vm)
    taps = [vm.tap, *(entry[2] for entry in vm.extra_nics())]
    expected = []
    for offset, (tap, master) in enumerate(zip(taps, (10, 11, 12)), start=13):
        expected += [
            (
                "add",
                {"ifname": tap, "kind": "tuntap", "mode": "tap", "uid": 1234},
            ),
            (
                "set",
                {"index": offset, "mtu": 1500 if master == 10 else 4200},
            ),
            (
                "set",
                {
                    "index": offset,
                    "master": master,
                    "ifalias": rail_host.tap_owner(vm),
                    "state": "up",
                },
            ),
        ]
    assert network_api.calls == expected
    network_api.calls.clear()
    monkeypatch.setattr(rail_host.os, "geteuid", MagicMock(return_value=0))
    rail_host.remove_taps(vm)
    assert network_api.calls == [
        ("del", {"index": index}) for index in (13, 14, 15)
    ]
    assert set(network_api.links) == {"fcbr0", "fcbr-rail1", "fcbr-rail2"}


@pytest.mark.parametrize("fault", ["kind", "alias", "master"])
def test_tap_removal_refuses_foreign_state_before_deleting(
    network_api: FakeIPRoute, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """Wrong type, owner, or fabric never permits deleting even an earlier good TAP."""
    vm = _profile_vm()
    network_api.add_existing("fcbr0", alias="management")
    assert vm.rail_topology is not None
    for rail in vm.rail_topology.rails:
        network_api.add_existing(rail.bridge, mtu=4200)
    rail_host.prepare_taps(vm)
    victim = network_api.links[vm.extra_nics()[0][2]]
    if fault == "kind":
        victim["IFLA_LINKINFO"] = Nla(IFLA_INFO_KIND="dummy")
    elif fault == "alias":
        victim["IFLA_IFALIAS"] = "another VM"
    else:
        victim["IFLA_MASTER"] = 999
    network_api.calls.clear()
    monkeypatch.setattr(rail_host.os, "geteuid", MagicMock(return_value=0))
    with pytest.raises(RuntimeError, match="unmanaged TAP"):
        rail_host.remove_taps(vm)
    assert network_api.calls == []


def test_tap_cleanup_rejects_changed_management_name(
    network_api: FakeIPRoute,
) -> None:
    """A modified VM record cannot name an arbitrary host device for deletion."""
    vm = _profile_vm()
    vm.tap = "administrator-device"
    network_api.add_existing(
        vm.tap, kind="tuntap", alias=rail_host.tap_owner(vm)
    )
    with pytest.raises(ValueError, match="does not match"):
        rail_host.remove_taps(vm)
    assert network_api.calls == []


def test_uninstall_preserves_preexisting_administrator_data_acl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Uninstall removes added grants but keeps unmarked administrator grants."""
    acl = tmp_path / "bridge.conf"
    acl.write_text("allow fcbr-data\nallow administrator-br\n")
    monkeypatch.setattr(rootless, "BRIDGE_ACL", acl)
    monkeypatch.setattr(
        rootless,
        "installed_helper",
        MagicMock(return_value=tmp_path / "helper"),
    )
    monkeypatch.setattr(
        rootless, "is_setuid_root", MagicMock(return_value=True)
    )
    host_setup._setup_bridge_helper(
        cast(host_setup.HostInfo, SimpleNamespace(pkg_mgr="apt"))
    )
    host_uninstall._linux_bridge_acl()
    assert acl.read_text() == "allow fcbr-data\nallow administrator-br\n"
