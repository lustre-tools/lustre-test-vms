"""Unprivileged VMs on a shared host (ltvm_pkg.rootless and its callers)."""

from __future__ import annotations

import argparse
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import host_setup, priv, qemu_run, rootless, vm_commands, vm_net
from ltvm_pkg.cli import cluster as cli_cluster
from ltvm_pkg.cli import util as cli_util
from ltvm_pkg.vm_state import MARKER
from tests.test_qemu_run import _LaunchHarness, _make_vm, _run_launch

HELPER = Path("/usr/lib/qemu/qemu-bridge-helper")
REAL = {
    name: getattr(rootless, name)
    for name in ("readiness", "hosts_dir_writable", "hosts_entries")
}


def _ready() -> rootless.Readiness:
    return rootless.Readiness(helper=HELPER)


def _not_ready() -> rootless.Readiness:
    return rootless.Readiness(problems=["no"])


@pytest.fixture
def real_rootless(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo conftest's pin for tests of the module itself."""
    for name, fn in REAL.items():
        monkeypatch.setattr(rootless, name, fn)


@pytest.fixture
def tmp_vmdir(tmp_path: Path) -> Iterator[Path]:
    sockets = tmp_path / "sockets"
    overlays = tmp_path / "overlays"
    sockets.mkdir()
    overlays.mkdir()
    with (
        patch("ltvm_pkg.vm_state.VM_DIR", tmp_path),
        patch("ltvm_pkg.vm_state.SOCKETS", sockets),
        patch("ltvm_pkg.vm_state.OVERLAYS", overlays),
    ):
        yield tmp_path


@pytest.fixture
def hosts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "hosts.d"
    d.mkdir()
    monkeypatch.setattr(rootless, "HOSTS_DIR", d)
    monkeypatch.setattr(rootless, "VM_DIR", tmp_path)
    return d


# ── ACL ──────────────────────────────────────────────────


class TestBridgeAcl:
    def test_allow_line(self, tmp_path: Path) -> None:
        acl = tmp_path / "bridge.conf"
        acl.write_text("# qemu\nallow fcbr0\n")
        assert rootless.bridge_allowed("fcbr0", acl)
        assert not rootless.bridge_allowed("br0", acl)

    def test_allow_all(self, tmp_path: Path) -> None:
        acl = tmp_path / "bridge.conf"
        acl.write_text("allow all\n")
        assert rootless.bridge_allowed("fcbr0", acl)

    def test_deny_wins_whatever_the_order(self, tmp_path: Path) -> None:
        acl = tmp_path / "bridge.conf"
        acl.write_text("allow fcbr0\ndeny all\n")
        assert not rootless.bridge_allowed("fcbr0", acl)
        assert rootless.bridge_denied("fcbr0", acl)

    def test_include_is_followed(self, tmp_path: Path) -> None:
        extra = tmp_path / "ltvm.conf"
        extra.write_text("allow fcbr0  # ltvm\n")
        acl = tmp_path / "bridge.conf"
        acl.write_text(f"include {extra}\n")
        assert rootless.bridge_allowed("fcbr0", acl)

    def test_missing_file_allows_nothing(self, tmp_path: Path) -> None:
        assert not rootless.bridge_allowed("fcbr0", tmp_path / "absent")


class TestHelperDiscovery:
    def _stat(self, mode: int, uid: int) -> os.stat_result:
        return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))

    def test_setuid_root_helper(self) -> None:
        st = self._stat(stat.S_IFREG | 0o4755, 0)
        with patch.object(Path, "stat", return_value=st):
            assert rootless.is_setuid_root(HELPER)

    def test_not_setuid(self) -> None:
        st = self._stat(stat.S_IFREG | 0o755, 0)
        with patch.object(Path, "stat", return_value=st):
            assert not rootless.is_setuid_root(HELPER)

    def test_setuid_but_not_root(self) -> None:
        st = self._stat(stat.S_IFREG | 0o4755, 1000)
        with patch.object(Path, "stat", return_value=st):
            assert not rootless.is_setuid_root(HELPER)


# ── readiness ────────────────────────────────────────────


@pytest.mark.usefixtures("real_rootless")
class TestReadiness:
    @pytest.fixture
    def dirs(self, tmp_path: Path) -> Iterator[list[Path]]:
        dirs = [tmp_path / n for n in ("vm", "overlays", "sockets", "hosts.d")]
        for d in dirs:
            d.mkdir()
        with (
            patch.object(rootless, "shared_dirs", return_value=tuple(dirs)),
            patch.object(rootless, "KVM_DEVICE", tmp_path / "no-kvm"),
            patch.object(rootless.platform, "system", return_value="Linux"),
            patch.object(rootless, "bridge_helper", return_value=HELPER),
            patch.object(rootless, "bridge_allowed", return_value=True),
        ):
            yield dirs
        for d in dirs:
            d.chmod(0o755)

    def test_ready_when_everything_is_in_place(self, dirs: list[Path]) -> None:
        r = rootless.readiness()
        assert r.ok, r.problems
        assert r.helper == HELPER

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write anything")
    def test_unwritable_dir_names_the_group(self, dirs: list[Path]) -> None:
        dirs[2].chmod(0o555)
        r = rootless.readiness()
        assert not r.ok
        assert "'ltvm' group" in r.problems[0]

    def test_helper_without_setuid(self, dirs: list[Path]) -> None:
        with (
            patch.object(rootless, "bridge_helper", return_value=None),
            patch.object(rootless, "installed_helper", return_value=HELPER),
        ):
            r = rootless.readiness()
        assert r.problems == [f"{HELPER} is not setuid root"]

    def test_acl_refuses_the_bridge(self, dirs: list[Path]) -> None:
        with patch.object(rootless, "bridge_allowed", return_value=False):
            r = rootless.readiness()
        assert "does not allow fcbr0" in r.problems[0]

    def test_root_is_never_ready(self, dirs: list[Path]) -> None:
        with patch.object(rootless.os, "geteuid", return_value=0):
            assert not rootless.ready()


@pytest.mark.usefixtures("real_rootless")
class TestMacosReadiness:
    @pytest.fixture
    def host(self, tmp_path: Path) -> Iterator[dict[str, Path]]:
        dirs = [tmp_path / n for n in ("vm", "overlays", "sockets", "hosts.d")]
        for d in dirs:
            d.mkdir()
        sock = tmp_path / "socket_vmnet"
        sock.touch()
        plist = tmp_path / "reload.plist"
        plist.touch()
        script = tmp_path / "ltvm" / "ltvm-dnsmasq-reload"
        with (
            patch.object(rootless, "shared_dirs", return_value=tuple(dirs)),
            patch.object(rootless.platform, "system", return_value="Darwin"),
            patch.object(rootless, "DNSMASQ_RELOAD_PLIST", plist),
            patch.object(rootless, "DNSMASQ_RELOAD_SCRIPT", script),
            patch.object(
                host_setup, "socket_vmnet_socket_path", return_value=sock
            ),
        ):
            yield {
                "sock": sock,
                "plist": plist,
                "script": script,
                "sockets": dirs[2],
            }
        for d in dirs:
            d.chmod(0o755)

    def test_ready_without_a_helper(self, host: dict[str, Path]) -> None:
        r = rootless.readiness()
        assert r.ok, r.problems
        assert r.helper is None

    def test_missing_socket(self, host: dict[str, Path]) -> None:
        host["sock"].unlink()
        r = rootless.readiness()
        assert r.problems == [
            f"{host['sock']} is not usable by this user -- `ltvm install` "
            "gives it the 'ltvm' group"
        ]

    def test_missing_reload_job(self, host: dict[str, Path]) -> None:
        host["plist"].unlink()
        r = rootless.readiness()
        assert "VM names would not resolve" in r.problems[0]

    def _names_script(self, host: dict[str, Path]) -> Path:
        script = host["script"]
        host["plist"].write_text(f"<string>{script}</string>")
        return script

    def test_missing_reload_script(self, host: dict[str, Path]) -> None:
        script = self._names_script(host)
        r = rootless.readiness()
        assert r.problems == [
            f"{script} is missing or not executable -- VM names would "
            "not resolve; `ltvm install` puts it back"
        ]

    def test_reload_script_the_user_can_write(
        self, host: dict[str, Path]
    ) -> None:
        script = self._names_script(host)
        script.parent.mkdir()
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        r = rootless.readiness()
        # tmp_path is the user's, not root's.
        assert "is not root's alone" in r.problems[0]
        with patch.object(rootless, "root_only", return_value=True):
            assert rootless.readiness().ok

    def test_plist_from_before_the_script(self, host: dict[str, Path]) -> None:
        host["plist"].write_text(
            "<string>/bin/launchctl</string><string>kill</string>"
        )
        assert rootless.readiness().ok

    def test_root_only(self, tmp_path: Path) -> None:
        f = tmp_path / "f"
        f.touch()
        link = tmp_path / "l"
        link.symlink_to(f)
        assert not rootless.root_only(tmp_path / "missing")
        assert not rootless.root_only(link)
        # /bin/sh is a link to dash on Debian and Ubuntu.
        assert rootless.root_only(Path("/bin/sh").resolve())
        if os.geteuid() != 0:
            assert not rootless.root_only(f)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write anything")
    def test_unwritable_dir(self, host: dict[str, Path]) -> None:
        host["sockets"].chmod(0o555)
        r = rootless.readiness()
        assert "'ltvm' group" in r.problems[0]


# ── dropping root ────────────────────────────────────────


class TestDropToSudoUser:
    @pytest.fixture
    def as_sudo_root(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
        # drop_to_sudo_user rewrites these; monkeypatch puts them back.
        for var in ("HOME", "USER", "LOGNAME"):
            monkeypatch.setenv(var, os.environ.get(var, "root"))
        monkeypatch.setenv("SUDO_USER", "alice")
        monkeypatch.setenv("SUDO_UID", "1234")
        user = MagicMock(
            pw_name="alice", pw_uid=1234, pw_gid=1234, pw_dir="/home/alice"
        )
        mocks: dict[str, Any] = {}
        with (
            patch.object(rootless.platform, "system", return_value="Linux"),
            patch.object(rootless.os, "geteuid", return_value=0),
            patch.object(rootless.pwd, "getpwnam", return_value=user),
            patch.object(rootless.os, "getgrouplist", return_value=[1234, 99]),
            patch.object(rootless.os, "getgroups", return_value=[0]),
            patch.object(rootless.os, "getegid", return_value=0),
        ):
            for fn in (
                "setgroups",
                "initgroups",
                "setegid",
                "seteuid",
                "setgid",
                "setuid",
            ):
                p = patch.object(rootless.os, fn)
                mocks[fn] = p.start()
            yield mocks
            patch.stopall()

    def test_not_root_does_nothing(self) -> None:
        assert not rootless.drop_to_sudo_user()

    def test_drops_when_user_is_ready(self, as_sudo_root: dict) -> None:
        with patch.object(rootless, "readiness", return_value=_ready()):
            assert rootless.drop_to_sudo_user()
        as_sudo_root["setuid"].assert_called_once_with(1234)
        as_sudo_root["setgid"].assert_called_once_with(1234)
        assert os.environ["HOME"] == "/home/alice"
        assert os.environ["USER"] == "alice"
        assert "SUDO_USER" not in os.environ

    def test_stays_root_when_user_is_not_ready(
        self, as_sudo_root: dict
    ) -> None:
        with patch.object(rootless, "readiness", return_value=_not_ready()):
            assert not rootless.drop_to_sudo_user()
        as_sudo_root["setuid"].assert_not_called()
        # The trial identity is given back.
        assert as_sudo_root["seteuid"].call_args_list[-1].args == (0,)
        assert os.environ["SUDO_USER"] == "alice"

    def test_stays_root_elsewhere_without_touching_groups(
        self, as_sudo_root: dict
    ) -> None:
        with patch.object(rootless.platform, "system", return_value="FreeBSD"):
            assert not rootless.drop_to_sudo_user()
        for fn in (
            "setgroups",
            "initgroups",
            "setegid",
            "seteuid",
            "setgid",
            "setuid",
        ):
            as_sudo_root[fn].assert_not_called()
        assert os.environ["SUDO_USER"] == "alice"

    def test_macos_joins_groups_through_initgroups(
        self, as_sudo_root: dict
    ) -> None:
        """setgroups() takes at most 16 groups there: EINVAL for an
        admin account that has joined ltvm."""
        with (
            patch.object(rootless.platform, "system", return_value="Darwin"),
            patch.object(rootless, "readiness", return_value=_ready()),
        ):
            assert rootless.drop_to_sudo_user()
        as_sudo_root["setgroups"].assert_not_called()
        as_sudo_root["initgroups"].assert_called_with("alice", 1234)


class TestDropToOwner:
    def _info(self, tmp_path: Path, name: str) -> Path:
        f = tmp_path / f"{name}.info"
        f.write_text("")
        return f

    def _uid_of(self, uids: dict[str, int]) -> Any:
        real_stat = Path.stat

        def fake(self: Path, *a: Any, **k: Any) -> Any:
            st = real_stat(self, *a, **k)
            uid = uids.get(self.stem, st.st_uid)
            return os.stat_result((st.st_mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))

        return patch.object(Path, "stat", fake)

    def test_single_owner(self, tmp_path: Path) -> None:
        paths = [self._info(tmp_path, "co1-a"), self._info(tmp_path, "co1-b")]
        owner = MagicMock(pw_name="bob")
        with (
            patch.object(rootless.os, "geteuid", return_value=0),
            self._uid_of({"co1-a": 1500, "co1-b": 1500}),
            patch.object(rootless.pwd, "getpwuid", return_value=owner) as gp,
            patch.object(rootless, "drop_to", return_value=True) as drop,
        ):
            assert rootless.drop_to_owner(paths)
        gp.assert_called_once_with(1500)
        drop.assert_called_once_with(owner)

    def test_mixed_owners_stay_root(self, tmp_path: Path) -> None:
        paths = [self._info(tmp_path, "co1-a"), self._info(tmp_path, "co1-b")]
        with (
            patch.object(rootless.os, "geteuid", return_value=0),
            self._uid_of({"co1-a": 1500, "co1-b": 1501}),
            patch.object(rootless, "drop_to") as drop,
        ):
            assert not rootless.drop_to_owner(paths)
        drop.assert_not_called()

    def test_root_owned_vm_stays_root(self, tmp_path: Path) -> None:
        paths = [self._info(tmp_path, "co1-a")]
        with (
            patch.object(rootless.os, "geteuid", return_value=0),
            self._uid_of({"co1-a": 0}),
            patch.object(rootless, "drop_to") as drop,
        ):
            assert not rootless.drop_to_owner(paths)
        drop.assert_not_called()

    def test_not_root(self, tmp_path: Path) -> None:
        assert not rootless.drop_to_owner([self._info(tmp_path, "co1-a")])


# ── hosts.d ──────────────────────────────────────────────


@pytest.mark.usefixtures("real_rootless")
class TestHostsEntries:
    def test_round_trip(self, hosts_dir: Path) -> None:
        old = os.umask(0o077)
        try:
            rootless.write_hosts_entry("co1-a", "192.168.100.7", MARKER)
        finally:
            os.umask(old)
        f = hosts_dir / "co1-a"
        assert f.read_text() == f"192.168.100.7\tco1-a {MARKER}:co1-a\n"
        assert stat.S_IMODE(f.stat().st_mode) == 0o644
        assert rootless.read_hosts_ip("co1-a") == "192.168.100.7"
        assert rootless.hosts_entries() == ["co1-a"]
        rootless.remove_hosts_entry("co1-a")
        rootless.remove_hosts_entry("co1-a")
        assert rootless.hosts_entries() == []

    def test_no_temp_file_is_left_in_the_watched_dir(
        self, hosts_dir: Path
    ) -> None:
        rootless.write_hosts_entry("co1-a", "192.168.100.7", MARKER)
        assert [p.name for p in hosts_dir.iterdir()] == ["co1-a"]


# ── QEMU launch and stop ─────────────────────────────────


@pytest.fixture
def linux_host() -> Iterator[None]:
    def routed(cmd, **kw):
        return qemu_run.run(cmd, capture_output=kw.get("quiet", False))

    with (
        patch("ltvm_pkg.qemu_run.is_macos", return_value=False),
        patch("ltvm_pkg.qemu_run.sudo_run", side_effect=routed),
    ):
        yield


@pytest.mark.usefixtures("linux_host")
class TestHelperLaunch:
    def _launch(self, vm: Any, ready: rootless.Readiness) -> _LaunchHarness:
        h = _LaunchHarness()
        with patch.object(rootless, "readiness", return_value=ready):
            _run_launch(vm, h)
        return h

    def test_qemu_runs_as_the_user_on_the_bridge(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        h = self._launch(vm, _ready())
        assert h.qemu_args is not None
        assert h.qemu_args[0] != "sudo"
        assert f"bridge,id=net0,br=fcbr0,helper={HELPER}" in h.qemu_args
        flat = [" ".join(c) for c in h.run_calls]
        assert not any("tuntap" in c or "chown" in c for c in flat)

    def test_extra_nics_use_the_helper_too(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir, name="co1-two")
        vm.nics = ["tcp", "softroce"]
        h = self._launch(vm, _ready())
        assert h.qemu_args is not None
        joined = " ".join(h.qemu_args)
        assert f"bridge,id=net1,br=fcbr0,helper={HELPER}" in joined
        assert f"bridge,id=net2,br=fcbr0,helper={HELPER}" in joined
        assert "ifname=" not in joined

    def test_log_is_created_without_sudo(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        h = self._launch(vm, _ready())
        assert vm.log_path.exists()
        assert not any(c[:1] == ["touch"] for c in h.run_calls)

    def test_passthrough_keeps_the_root_path(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir, name="co1-pt")
        vm.nics = ["passthrough:0000:00:02.0"]
        h = self._launch(vm, _ready())
        assert h.qemu_args is not None
        assert h.qemu_args[0] == "sudo"
        assert "tap,id=net0,ifname=tap-co1-pt,script=no,downscript=no" in (
            h.qemu_args
        )

    def test_not_ready_keeps_the_root_path(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        h = self._launch(vm, _not_ready())
        assert h.qemu_args is not None
        assert h.qemu_args[0] == "sudo"


class TestMacosLaunch:
    """socket_vmnet needs no helper: a ready Mac runs QEMU as the user."""

    _SOCK = Path("/opt/homebrew/var/run/socket_vmnet")

    def _launch(self, vm: Any, ready: rootless.Readiness) -> _LaunchHarness:
        h = _LaunchHarness()
        with (
            patch.object(rootless, "readiness", return_value=ready),
            patch("ltvm_pkg.qemu_run.is_macos", return_value=True),
            patch(
                "ltvm_pkg.qemu_run.socket_vmnet_socket_path",
                return_value=self._SOCK,
            ),
            patch(
                "ltvm_pkg.host_setup.ensure_socket_vmnet_running",
                return_value=None,
            ),
        ):
            _run_launch(vm, h)
        return h

    def test_qemu_runs_as_the_user(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        h = self._launch(vm, rootless.Readiness())
        assert h.qemu_args is not None
        assert h.qemu_args[0] != "sudo"
        assert f"addr.path={self._SOCK}" in " ".join(h.qemu_args)
        flat = [" ".join(c) for c in h.run_calls]
        assert not any("chown" in c or c.startswith("touch") for c in flat)

    def test_not_ready_keeps_the_root_path(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        h = self._launch(vm, _not_ready())
        assert h.qemu_args is not None
        assert h.qemu_args[0] == "sudo"


class TestHelperStop:
    def _kill(self, vm: Any, sys_taps: set[str]) -> MagicMock:
        real_exists = Path.exists

        def exists(self: Path) -> bool:
            if str(self).startswith("/sys/class/net/"):
                return self.name in sys_taps
            return real_exists(self)

        sudo = MagicMock(return_value=MagicMock(returncode=0))
        with (
            patch("ltvm_pkg.qemu_run.is_macos", return_value=False),
            patch("ltvm_pkg.qemu_run.sudo_run", sudo),
            patch.object(rootless, "readiness", return_value=_ready()),
            patch.object(Path, "exists", exists),
            patch.object(type(vm), "update_pid"),
        ):
            qemu_run.kill_qemu(vm)
        return sudo

    def test_nothing_to_tear_down(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        sudo = self._kill(vm, set())
        sudo.assert_not_called()

    def test_old_tap_is_removed_without_prompting(
        self, tmp_vmdir: Path
    ) -> None:
        vm = _make_vm(tmp_vmdir)
        sudo = self._kill(vm, {vm.tap})
        sudo.assert_called_once_with(
            ["ip", "link", "del", vm.tap],
            check=False,
            quiet=True,
            noninteractive=True,
        )


# ── name registration ────────────────────────────────────


class TestSharedRegistration:
    @pytest.fixture
    def paths(
        self, tmp_path: Path, hosts_dir: Path
    ) -> Iterator[dict[str, Path]]:
        # Its own directory: a write needs that directory, not the file.
        (tmp_path / "etc").mkdir()
        etc_hosts = tmp_path / "etc" / "hosts"
        etc_hosts.write_text("127.0.0.1 localhost\n")
        ssh = tmp_path / ".ssh"
        ssh.mkdir()
        with (
            patch.object(vm_net, "HOSTS_FILE", etc_hosts),
            # The lock must be isolated even when register takes the shared-host path.
            patch.object(vm_net, "_HOSTS_LOCK_PATH", tmp_path / ".hosts.lock"),
            patch.object(
                vm_net, "_real_user_ssh_dir", return_value=("alice", ssh)
            ),
            patch.object(vm_net, "reload_dns") as reload,
            patch.object(vm_net, "sudo_ready", return_value=False),
            patch.object(rootless, "hosts_dir_writable", return_value=True),
            patch.object(rootless, "hosts_entries", REAL["hosts_entries"]),
            patch.object(vm_net.os, "chown"),
        ):
            yield {"etc": etc_hosts, "ssh": ssh, "reload": reload}

    def test_register_without_root_uses_hosts_d(
        self, paths: dict, hosts_dir: Path
    ) -> None:
        # The user owns the file but not its directory, as on a Mac
        # where /etc/hosts was handed over: a write would still prompt.
        paths["etc"].parent.chmod(0o555)
        try:
            vm_net.register_ssh_name("co1-a", "192.168.100.7")
        finally:
            paths["etc"].parent.chmod(0o755)
        assert (hosts_dir / "co1-a").exists()
        assert "co1-a" not in paths["etc"].read_text()
        paths["reload"].assert_not_called()
        assert "Host co1-a" in (paths["ssh"] / "config").read_text()

    def test_register_with_root_also_edits_etc_hosts(
        self, paths: dict, hosts_dir: Path
    ) -> None:
        vm_net.register_ssh_name("co1-a", "192.168.100.7")
        assert (hosts_dir / "co1-a").exists()
        assert f"{MARKER}:co1-a" in paths["etc"].read_text()
        # dnsmasq watches hosts.d; a reload would only fail where its
        # pidfile is out of reach, as inside a Patch Watcher run.
        paths["reload"].assert_not_called()
        vm_net.unregister_ssh_name("co1-a")
        assert f"{MARKER}:co1-a" not in paths["etc"].read_text()
        paths["reload"].assert_not_called()

    def test_a_name_hosts_d_did_not_take_is_reloaded(
        self, paths: dict, hosts_dir: Path
    ) -> None:
        with patch.object(
            rootless, "write_hosts_entry", side_effect=OSError("full")
        ):
            vm_net.register_ssh_name("co1-a", "192.168.100.7")
        assert f"{MARKER}:co1-a" in paths["etc"].read_text()
        paths["reload"].assert_called_once()

    def test_unregister_removes_the_entry(
        self, paths: dict, hosts_dir: Path
    ) -> None:
        vm_net.register_ssh_name("co1-a", "192.168.100.7")
        paths["etc"].parent.chmod(0o555)
        try:
            vm_net.unregister_ssh_name("co1-a")
        finally:
            paths["etc"].parent.chmod(0o755)
        assert not (hosts_dir / "co1-a").exists()
        # /etc/hosts could not be edited without a prompt, so it stays.
        assert f"{MARKER}:co1-a" in paths["etc"].read_text()
        assert "Host co1-a" not in (paths["ssh"] / "config").read_text()


# ── doctor ───────────────────────────────────────────────


@pytest.fixture
def doctor(tmp_path: Path) -> Iterator[Path]:
    sockets = tmp_path / "sockets"
    overlays = tmp_path / "overlays"
    sockets.mkdir()
    overlays.mkdir()
    hosts = tmp_path / "hosts"
    hosts.write_text("")
    ok = MagicMock(returncode=0, stdout="", stderr="")
    with (
        patch("ltvm_pkg.vm_state.SOCKETS", sockets),
        patch("ltvm_pkg.vm_state.OVERLAYS", overlays),
        patch.object(vm_commands, "SOCKETS", sockets),
        patch.object(vm_commands, "OVERLAYS", overlays),
        patch.object(vm_commands, "HOSTS_FILE", hosts),
        patch.object(vm_commands, "run", return_value=ok),
        patch.object(vm_commands, "_check_export_tools", return_value=[]),
        patch.object(
            vm_commands, "_check_skill_links", return_value=([], [], 0)
        ),
        patch.object(
            vm_commands, "_check_completion", return_value=([], [], 0)
        ),
        patch.object(
            vm_commands, "_check_artifacts_disk_usage", return_value=([], None)
        ),
        patch.object(vm_commands, "is_macos", return_value=False),
        patch.object(
            vm_commands,
            "_real_user_ssh_dir",
            return_value=("alice", tmp_path / ".ssh"),
        ),
    ):
        yield tmp_path


def _doctor(fix: bool = False) -> int:
    return vm_commands.cmd_doctor(argparse.Namespace(fix=fix, json=False))


class TestDoctorShared:
    def test_shared_dir_mode_is_fine(
        self, doctor: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for d in ("sockets", "overlays"):
            (doctor / d).chmod(0o2775)
        _doctor()
        assert "tight perms" not in capsys.readouterr().out

    def test_does_not_chmod_through_a_symlink(self, doctor: Path) -> None:
        target = doctor / "secret"
        target.write_text("")
        target.chmod(0o600)
        (doctor / "sockets" / "evil.info").symlink_to(target)
        _doctor(fix=True)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_stale_hosts_d_entry(
        self, doctor: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch.object(rootless, "hosts_entries", return_value=["co1-gone"]),
            patch.object(rootless, "remove_hosts_entry") as remove,
        ):
            _doctor(fix=True)
        assert "stale hosts.d entry: co1-gone" in capsys.readouterr().out
        remove.assert_called_once_with("co1-gone")

    def test_reports_readiness(
        self, doctor: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch.object(rootless, "readiness", return_value=_ready()):
            _doctor()
        assert "unprivileged VMs: ready" in capsys.readouterr().out

    def test_gap_on_a_shared_install_is_an_issue(
        self, doctor: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gid = os.getgid()
        with (
            patch.object(vm_commands, "_group_id", return_value=gid),
            patch.object(vm_commands, "VM_DIR", doctor),
            patch.object(
                rootless,
                "readiness",
                return_value=rootless.Readiness(problems=["helper missing"]),
            ),
        ):
            rc = _doctor()
        out = capsys.readouterr().out
        assert "unprivileged VMs unavailable: helper missing" in out
        assert rc != 0


# ── CLI privilege choices ────────────────────────────────


class TestVmPrivileges:
    def test_ready_user_needs_no_sudo(self) -> None:
        with (
            patch.object(rootless, "ready", return_value=True),
            patch.object(priv, "sudo_prime") as sp,
        ):
            assert cli_util._vm_privileges("x", False) is None
        sp.assert_not_called()

    def test_passthrough_still_primes_sudo(self) -> None:
        with (
            patch.object(rootless, "ready", return_value=True),
            patch.object(priv, "sudo_prime") as sp,
        ):
            cli_util._vm_privileges("x", False, passthrough=True)
        sp.assert_called_once()

    def test_root_runs_the_drop(self) -> None:
        drop = MagicMock()
        with (
            patch.object(cli_util.os, "geteuid", return_value=0),
            patch.object(priv, "sudo_prime") as sp,
        ):
            assert cli_util._vm_privileges("x", False, drop=drop) is None
        drop.assert_called_once()
        sp.assert_not_called()

    def test_create_drops_to_the_sudo_user(self) -> None:
        from ltvm_pkg.cli import setup as cli_setup

        with (
            patch.object(cli_util.os, "geteuid", return_value=0),
            patch.object(rootless, "drop_to_sudo_user") as drop,
            patch("ltvm_pkg.vm_commands.cmd_create"),
        ):
            cli_setup.cmd_create(
                argparse.Namespace(json=False, dry_run=False, nic=[])
            )
        drop.assert_called_once()

    def test_start_drops_to_the_vm_owner(self, tmp_path: Path) -> None:
        from ltvm_pkg.cli import vm as cli_vm

        with (
            patch.object(cli_util.os, "geteuid", return_value=0),
            patch("ltvm_pkg.vm_state.SOCKETS", tmp_path),
            patch.object(rootless, "drop_to_owner") as drop,
            patch.object(rootless, "drop_to_sudo_user") as drop_sudo,
            patch("ltvm_pkg.vm_commands.cmd_start"),
        ):
            cli_vm.cmd_vm_start(argparse.Namespace(json=False, names=["co1-a"]))
        drop.assert_called_once_with([tmp_path / "co1-a.info"])
        drop_sudo.assert_not_called()

    def test_stop_and_destroy_stay_root(self) -> None:
        from ltvm_pkg.cli import setup as cli_setup
        from ltvm_pkg.cli import vm as cli_vm

        ns = argparse.Namespace(json=False, names=["co1-a"])
        with (
            patch.object(cli_util.os, "geteuid", return_value=0),
            patch.object(rootless, "drop_to") as drop,
            patch("ltvm_pkg.vm_commands.cmd_stop"),
            patch("ltvm_pkg.vm_commands.cmd_destroy"),
        ):
            cli_vm.cmd_vm_stop(ns)
            cli_setup.cmd_destroy(ns)
        drop.assert_not_called()


class TestClusterPrivileges:
    def test_ready_user_needs_no_root(self) -> None:
        with (
            patch.object(rootless, "ready", return_value=True),
            patch.object(cli_cluster, "_require_root") as need_root,
        ):
            assert cli_cluster._cluster_privileges(False, drop=True) is None
        need_root.assert_not_called()

    def test_otherwise_root_is_required(self) -> None:
        with patch.object(cli_cluster, "_require_root", return_value=2) as nr:
            assert cli_cluster._cluster_privileges(False, drop=True) == 2
        nr.assert_called_once()

    def test_root_destroy_stays_root(self) -> None:
        with (
            patch.object(cli_cluster.os, "geteuid", return_value=0),
            patch.object(rootless, "drop_to_sudo_user") as drop,
        ):
            assert cli_cluster._cluster_privileges(False, drop=False) is None
        drop.assert_not_called()

    def test_children_run_without_sudo(self) -> None:
        from ltvm_pkg import vm_cluster

        with patch.object(rootless, "ready", return_value=True):
            assert vm_cluster._sudo_prefix() == []


# ── install ──────────────────────────────────────────────


class TestInstall:
    def test_ltvm_dnsmasq_serves_hosts_d(self, tmp_path: Path) -> None:
        with patch.object(host_setup, "VM_DIR", tmp_path / "vm"):
            text = host_setup._render_ltvm_dnsmasq_conf("192.168.200")
        assert f"hostsdir={tmp_path / 'vm' / 'hosts.d'}\n" in text

    def _apt_host(self) -> MagicMock:
        host = MagicMock()
        host.pkg_mgr = "apt"
        return host

    def test_helper_made_setuid_by_statoverride(self, tmp_path: Path) -> None:
        acl = tmp_path / "qemu" / "bridge.conf"
        listed = MagicMock(returncode=1, stdout="")
        with (
            patch.object(rootless, "installed_helper", return_value=HELPER),
            patch.object(rootless, "is_setuid_root", return_value=False),
            patch.object(rootless, "BRIDGE_ACL", acl),
            patch.object(host_setup.shutil, "which", return_value="/usr/bin/x"),
            patch.object(host_setup, "_run_quiet", return_value=listed),
            patch.object(host_setup, "_run") as run,
        ):
            host_setup._setup_bridge_helper(self._apt_host())
        run.assert_called_once_with(
            [
                "dpkg-statoverride",
                "--update",
                "--add",
                "root",
                "root",
                "4755",
                str(HELPER),
            ]
        )
        from ltvm_pkg.rail_host import bridge_names

        assert acl.read_text() == "allow fcbr0\n" + "".join(
            f"# ltvm data bridge\nallow {name}\n" for name in bridge_names()
        )

    def test_acl_with_a_deny_is_not_touched(self, tmp_path: Path) -> None:
        acl = tmp_path / "bridge.conf"
        acl.write_text("deny all\n")
        with (
            patch.object(rootless, "installed_helper", return_value=HELPER),
            patch.object(rootless, "is_setuid_root", return_value=True),
            patch.object(rootless, "BRIDGE_ACL", acl),
        ):
            host_setup._setup_bridge_helper(self._apt_host())
        assert acl.read_text() == "deny all\n"

    def test_shared_dirs_and_membership(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUDO_USER", "alice")
        vm_dir = tmp_path / "vm"
        group = MagicMock(gr_gid=os.getgid())
        user = MagicMock(pw_gid=os.getgid())
        with (
            patch.object(host_setup, "VM_DIR", vm_dir),
            patch.object(host_setup, "_run") as run,
            patch("grp.getgrnam", return_value=group),
            patch("pwd.getpwnam", return_value=user),
            patch.object(host_setup.os, "chown"),
            patch.object(host_setup.os, "getgrouplist", return_value=[]),
        ):
            host_setup._setup_shared_vm_dirs()
        for d in (vm_dir, vm_dir / "overlays", vm_dir / "sockets"):
            assert stat.S_IMODE(d.stat().st_mode) == rootless.SHARED_DIR_MODE
        assert (vm_dir / "hosts.d").is_dir()
        assert ["groupadd", "-f", "ltvm"] in [c.args[0] for c in run.mock_calls]
        assert ["usermod", "-aG", "ltvm,kvm", "alice"] in [
            c.args[0] for c in run.mock_calls
        ]

    def test_macos_shared_dirs_and_membership(self, tmp_path: Path) -> None:
        vm_dir = tmp_path / "vm"
        not_member = MagicMock(returncode=1)
        with (
            patch.object(host_setup, "VM_DIR", vm_dir),
            patch.object(host_setup, "_sudo_prime"),
            patch.object(host_setup, "_sudo_run") as sudo,
            patch.object(host_setup, "_run_quiet", return_value=not_member),
            patch("grp.getgrnam", side_effect=KeyError("ltvm")),
            patch("getpass.getuser", return_value="alice"),
        ):
            host_setup._setup_shared_vm_dirs_macos()
        cmds = [c.args[0] for c in sudo.mock_calls]
        dirs = [str(vm_dir)] + [
            str(vm_dir / n) for n in ("overlays", "sockets", "hosts.d")
        ]
        assert cmds[0][:3] == ["dseditgroup", "-o", "create"]
        assert ["chown", "-h", "root:ltvm", *dirs] in cmds
        assert ["chmod", "-h", "3775", *dirs] in cmds
        assert [
            "dseditgroup",
            "-o",
            "edit",
            "-a",
            "alice",
            "-t",
            "user",
            "ltvm",
        ] in cmds

    def test_macos_shared_dir_symlink_refused(self, tmp_path: Path) -> None:
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()
        (vm_dir / "sockets").symlink_to(tmp_path)
        with (
            patch.object(host_setup, "VM_DIR", vm_dir),
            patch.object(host_setup, "_sudo_prime"),
            patch.object(host_setup, "_sudo_run") as sudo,
            patch("grp.getgrnam"),
            pytest.raises(RuntimeError, match="symlink"),
        ):
            host_setup._setup_shared_vm_dirs_macos()
        assert not any(
            c.args[0][0] in ("chown", "chmod") for c in sudo.mock_calls
        )

    def test_macos_reload_job_watches_hosts_d(self, tmp_path: Path) -> None:
        plist = tmp_path / "reload.plist"
        script = tmp_path / "ltvm" / "ltvm-dnsmasq-reload"
        installed: dict[str, tuple[str, str]] = {}

        def sudo(cmd: list[str], **_kw: Any) -> MagicMock:
            if cmd[0] == "install" and "-d" not in cmd:
                installed[cmd[-1]] = (cmd[2], Path(cmd[-2]).read_text())
            return MagicMock(returncode=0)

        with (
            patch.object(rootless, "DNSMASQ_RELOAD_PLIST", plist),
            patch.object(rootless, "DNSMASQ_RELOAD_SCRIPT", script),
            patch.object(host_setup, "VM_DIR", tmp_path / "vm"),
            patch.object(host_setup, "_sudo_prime"),
            patch.object(host_setup, "_sudo_run", side_effect=sudo) as run,
            patch.object(
                host_setup, "_run_quiet", return_value=MagicMock(returncode=1)
            ),
        ):
            host_setup.install_dnsmasq_reload_macos()
        mode, text = installed[str(plist)]
        assert mode == "0644"
        assert f"<string>{tmp_path / 'vm' / 'hosts.d'}</string>" in text
        # Login Items names the job by its program: the script, not
        # launchctl.
        assert f"<string>{script}</string>" in text
        assert "launchctl" not in text
        assert "@" not in text
        mode, body = installed[str(script)]
        assert mode == "0755"
        assert "system/io.github.ltvm.dnsmasq" in body
        assert [
            "install",
            "-d",
            "-m",
            "0755",
            "-o",
            "root",
            "-g",
            "wheel",
            str(script.parent),
        ] in [c.args[0] for c in run.mock_calls]
        assert ["launchctl", "bootstrap", "system", str(plist)] in [
            c.args[0] for c in run.mock_calls
        ]

    def _reinstall(
        self,
        tmp_path: Path,
        *,
        script_text: str | None = None,
        root_only: bool = True,
        loaded: bool = True,
        script_is_dir: bool = False,
    ) -> list[list[str]]:
        """Install over a current one; the elevated commands it ran.

        The plist on disk is the one install writes.  *script_text*
        replaces the script's; *root_only* is what the owner and mode
        check says.
        """
        plist = tmp_path / "reload.plist"
        script = tmp_path / "ltvm" / "ltvm-dnsmasq-reload"
        script.parent.mkdir()
        hosts = tmp_path / "vm" / "hosts.d"
        cfg = host_setup.HOST_CONFIG_DIR
        plist.write_text(
            (cfg / f"{rootless.DNSMASQ_RELOAD_LABEL}.plist")
            .read_text()
            .replace("@HOSTS_DIR@", str(hosts))
            .replace("@RELOAD_SCRIPT@", str(script))
        )
        if script_text is None:
            script_text = (cfg / script.name).read_text()
        if script_is_dir:
            script.mkdir()  # read_text raises
        else:
            script.write_text(script_text)
        cmds: list[list[str]] = []

        def sudo(cmd: list[str], **_kw: Any) -> MagicMock:
            cmds.append(cmd)
            return MagicMock(returncode=0)

        with (
            patch.object(rootless, "DNSMASQ_RELOAD_PLIST", plist),
            patch.object(rootless, "DNSMASQ_RELOAD_SCRIPT", script),
            patch.object(rootless, "root_only", return_value=root_only),
            patch.object(host_setup, "VM_DIR", tmp_path / "vm"),
            patch.object(host_setup, "_sudo_prime"),
            patch.object(host_setup, "_sudo_run", side_effect=sudo),
            # The install's own check, then _launchd_replace's.
            patch.object(
                host_setup, "_launchd_loaded", side_effect=[loaded, False]
            ),
        ):
            host_setup.install_dnsmasq_reload_macos()
        return cmds

    def test_macos_reload_job_current_is_left(self, tmp_path: Path) -> None:
        assert self._reinstall(tmp_path) == []

    def test_macos_reload_script_not_roots_alone_is_put_back(
        self, tmp_path: Path
    ) -> None:
        """The text matches, but someone else could change what root runs."""
        cmds = self._reinstall(tmp_path, root_only=False)
        dest = str(tmp_path / "ltvm" / "ltvm-dnsmasq-reload")
        assert [c for c in cmds if c[0] == "install" and c[-1] == dest]
        assert ["install", "-d", "-m", "0755", "-o", "root", "-g", "wheel",
                str(tmp_path / "ltvm")] in cmds  # fmt: skip
        # launchd runs the script afresh: no reload for it.
        assert not [c for c in cmds if c[0] == "launchctl"]

    def test_macos_reload_new_script_alone_needs_no_reload(
        self, tmp_path: Path
    ) -> None:
        cmds = self._reinstall(tmp_path, script_text="#!/bin/sh\nold\n")
        assert [c for c in cmds if c[0] == "install"]
        assert not [c for c in cmds if c[0] == "launchctl"]

    def test_macos_reload_unloaded_job_is_loaded(self, tmp_path: Path) -> None:
        cmds = self._reinstall(tmp_path, loaded=False)
        assert cmds[-1][:3] == ["launchctl", "bootstrap", "system"]
        # The script was current: only the plist went in.
        assert len([c for c in cmds if c[0] == "install"]) == 1

    def test_macos_reload_unreadable_script_is_put_back(
        self, tmp_path: Path
    ) -> None:
        cmds = self._reinstall(tmp_path, script_is_dir=True)
        dest = str(tmp_path / "ltvm" / "ltvm-dnsmasq-reload")
        assert [c for c in cmds if c[0] == "install" and c[-1] == dest]

    def test_socket_vmnet_found_without_running_brew(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """brew refuses to run with differing real and effective uids,
        which is how `sudo ltvm` checks whether the user is ready."""
        prefix = tmp_path / "opt" / "socket_vmnet"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin" / "socket_vmnet").touch()
        monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path))
        monkeypatch.delenv("LTVM_VMNET_SOCKET", raising=False)
        with patch.object(host_setup, "_run_quiet") as run:
            sock = host_setup.socket_vmnet_socket_path()
        run.assert_not_called()
        assert sock == prefix / "var" / "run" / "socket_vmnet"

    def test_macos_dnsmasq_reads_hosts_d(self) -> None:
        text = (
            host_setup.HOST_CONFIG_DIR / "ltvm-dnsmasq-macos.conf"
        ).read_text()
        assert "addn-hosts=@HOSTS_DIR@" in text.splitlines()

    def test_root_file_write_replaces_a_symlink(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.write_text("keep")
        link = tmp_path / "subnet"
        link.symlink_to(target)
        host_setup._write_root_file(link, "192.168.100\n")
        assert target.read_text() == "keep"
        assert not link.is_symlink()
        assert link.read_text() == "192.168.100\n"


# ── symlink safety in priv ───────────────────────────────


class TestNoFollow:
    def test_chmod_regular_refuses_a_link(self, tmp_path: Path) -> None:
        target = tmp_path / "t"
        target.write_text("")
        target.chmod(0o600)
        link = tmp_path / "l"
        link.symlink_to(target)
        with pytest.raises(OSError):
            priv.chmod_regular(link, 0o644)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_lock_file_never_follows_a_dangling_link(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "planted"
        link = tmp_path / ".ip-alloc.lock"
        link.symlink_to(target)
        priv.ensure_lock_file(link)
        assert not target.exists()


# ── base images a user's QEMU can read ───────────────────


def _image(root: Path, kernel: str = "5.14-k") -> Path:
    img = root / "rocky9" / "x86_64" / "images" / kernel / "base.ext4"
    img.parent.mkdir(parents=True)
    img.write_text("")
    img.chmod(0o600)
    return img


class TestBaseImageReadable:
    def test_fetch_opens_up_base_images(self, tmp_path: Path) -> None:
        from ltvm_pkg import release_package

        img = _image(tmp_path)
        secret = tmp_path / "secret"
        secret.write_text("")
        secret.chmod(0o600)
        link_dir = img.parent.parent / "5.14-l"
        link_dir.mkdir()
        (link_dir / "base.ext4").symlink_to(secret)
        release_package.share_base_images(tmp_path / "rocky9" / "x86_64")
        assert stat.S_IMODE(img.stat().st_mode) == 0o644
        assert stat.S_IMODE(secret.stat().st_mode) == 0o600

    def test_doctor_reports_and_fixes(
        self, doctor: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        artifacts = doctor / "artifacts"
        img = _image(artifacts)
        with (
            patch("ltvm_pkg.target_config.ARTIFACTS_DIR", artifacts),
            patch.object(rootless, "readiness", return_value=_ready()),
        ):
            _doctor(fix=True)
        assert "base image not readable" in capsys.readouterr().out
        assert stat.S_IMODE(img.stat().st_mode) == 0o644

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
    def test_create_names_an_unreadable_image(
        self, tmp_vmdir: Path, tmp_path: Path
    ) -> None:
        img = tmp_path / "base.ext4"
        img.write_text("")
        img.chmod(0o000)
        vm = _make_vm(tmp_vmdir)
        vm.overlay_path.unlink()
        with (
            patch.object(vm_commands, "_checked") as checked,
            pytest.raises(SystemExit),
        ):
            vm_commands._create_disks(vm, str(img))
        checked.assert_not_called()


# ── another user's VM ───────────────────────────────────


class TestOtherUsersVm:
    def _vm(self, tmp_vmdir: Path) -> Any:
        vm = _make_vm(tmp_vmdir)
        (tmp_vmdir / "sockets" / f"{vm.name}.info").write_text("")
        return vm

    def _stat_owner(self, uid: int) -> Any:
        real_stat = Path.stat

        def fake(self: Path, *a: Any, **k: Any) -> Any:
            st = real_stat(self, *a, **k)
            if self.suffix == ".info":
                return os.stat_result((st.st_mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))
            return st

        return patch.object(Path, "stat", fake)

    def test_refused_without_sudo(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vm = self._vm(tmp_vmdir)
        with (
            self._stat_owner(os.geteuid() + 1),
            patch.object(rootless, "readiness", return_value=_ready()),
            patch.object(
                vm_commands, "sudo_prime", side_effect=priv.SudoUnavailable("x")
            ),
            patch("pwd.getpwuid", return_value=MagicMock(pw_name="bob")),
            pytest.raises(SystemExit),
        ):
            vm_commands._require_manageable(vm, "destroy")
        assert "only bob or root can destroy it" in capsys.readouterr().err

    def test_sudoer_is_asked_once(self, tmp_vmdir: Path) -> None:
        vm = self._vm(tmp_vmdir)
        with (
            self._stat_owner(os.geteuid() + 1),
            patch.object(rootless, "readiness", return_value=_ready()),
            patch.object(vm_commands, "sudo_prime") as sp,
        ):
            vm_commands._require_manageable(vm, "stop")
        sp.assert_called_once()

    def test_start_is_left_to_the_owner(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vm = self._vm(tmp_vmdir)
        with (
            self._stat_owner(os.geteuid() + 1),
            patch.object(rootless, "readiness", return_value=_ready()),
            patch.object(vm_commands, "sudo_prime") as sp,
            patch("pwd.getpwuid", return_value=MagicMock(pw_name="bob")),
            pytest.raises(SystemExit),
        ):
            vm_commands._require_manageable(vm, "start")
        sp.assert_not_called()
        assert "start it as bob" in capsys.readouterr().err

    def test_own_vm_needs_nothing(self, tmp_vmdir: Path) -> None:
        vm = self._vm(tmp_vmdir)
        with (
            patch.object(rootless, "readiness", return_value=_ready()),
            patch.object(vm_commands, "sudo_prime") as sp,
        ):
            vm_commands._require_manageable(vm, "stop")
        sp.assert_not_called()

    def test_classic_host_keeps_its_fallbacks(self, tmp_vmdir: Path) -> None:
        vm = self._vm(tmp_vmdir)
        with (
            self._stat_owner(os.geteuid() + 1),
            patch.object(vm_commands, "sudo_prime") as sp,
        ):
            vm_commands._require_manageable(vm, "stop")
        sp.assert_not_called()


class TestRootQemu:
    """Our own VM whose QEMU was started as root, before the host was set up
    for unprivileged VMs."""

    def _vm(self, tmp_vmdir: Path) -> Any:
        vm = _make_vm(tmp_vmdir)
        vm.pid = 51721
        return vm

    def _root_qemu(self) -> Any:
        return patch.object(vm_commands.os, "kill", side_effect=PermissionError)

    def test_refused_up_front_without_sudo(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vm = self._vm(tmp_vmdir)
        with (
            patch.object(vm_commands, "is_running", return_value=True),
            self._root_qemu(),
            patch.object(
                vm_commands, "sudo_prime", side_effect=priv.SudoUnavailable("x")
            ),
            pytest.raises(SystemExit),
        ):
            vm_commands._require_signalable(vm, "stop")
        assert "sudo ltvm stop co1-single" in capsys.readouterr().err

    def test_sudoer_is_asked_once(self, tmp_vmdir: Path) -> None:
        vm = self._vm(tmp_vmdir)
        with (
            patch.object(vm_commands, "is_running", return_value=True),
            self._root_qemu(),
            patch.object(vm_commands, "sudo_prime") as sp,
        ):
            vm_commands._require_signalable(vm, "destroy")
        sp.assert_called_once()

    def test_own_qemu_needs_nothing(self, tmp_vmdir: Path) -> None:
        vm = self._vm(tmp_vmdir)
        with (
            patch.object(vm_commands, "is_running", return_value=True),
            patch.object(vm_commands.os, "kill"),
            patch.object(vm_commands, "sudo_prime") as sp,
        ):
            vm_commands._require_signalable(vm, "stop")
        sp.assert_not_called()

    def test_stopped_vm_needs_nothing(self, tmp_vmdir: Path) -> None:
        vm = self._vm(tmp_vmdir)
        with (
            patch.object(vm_commands, "is_running", return_value=False),
            patch.object(vm_commands, "sudo_prime") as sp,
        ):
            vm_commands._require_signalable(vm, "stop")
        sp.assert_not_called()


class TestStickyRename:
    def test_rename_refused_goes_through_sudo(self, tmp_path: Path) -> None:
        dest = tmp_path / "co1.info"
        dest.write_text("old")
        real_rename = os.rename

        def refuse(src: str, dst: str) -> None:
            if dst == str(dest):
                raise PermissionError(1, "Operation not permitted")
            real_rename(src, dst)

        sudo = MagicMock(return_value=MagicMock(returncode=0))
        with (
            patch.object(priv.os, "rename", side_effect=refuse),
            patch.object(priv, "sudo_run", sudo),
        ):
            priv.atomic_write(dest, "new")
        commands = [c.args[0][0] for c in sudo.call_args_list]
        assert commands[:2] == ["install", "mv"]
        assert [p.name for p in tmp_path.iterdir()] == ["co1.info"]


class TestStateOwnership:
    def _write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uid: int
    ) -> tuple[MagicMock, MagicMock]:
        state = tmp_path / "co1.info"
        state.write_text("old")
        monkeypatch.setenv("LTVM_VM_DIR", str(tmp_path))
        real_stat = Path.stat

        def fake(self: Path, *a: Any, **k: Any) -> Any:
            st = real_stat(self, *a, **k)
            if self == state:
                return os.stat_result(
                    (st.st_mode, 0, 0, 1, uid, 1600, 0, 0, 0, 0)
                )
            return st

        with (
            patch.object(Path, "stat", fake),
            patch.object(priv.os, "chown") as chown,
            patch.object(priv, "chown_to_invoking_user") as give,
        ):
            priv.atomic_write(state, "new")
        assert state.read_text() == "new"
        return chown, give

    def test_a_users_file_keeps_its_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chown, give = self._write(tmp_path, monkeypatch, 1500)
        assert chown.call_args.args[1:] == (1500, 1600)
        give.assert_not_called()

    def test_a_root_owned_file_goes_to_the_invoking_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chown, give = self._write(tmp_path, monkeypatch, 0)
        chown.assert_not_called()
        give.assert_called_once()
