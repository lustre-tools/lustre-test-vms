"""ltvm's own dnsmasq on Linux, and moving a host off the old layout,
in which ltvm configured the host's dnsmasq.service instead."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import host_setup, vm_net

# What `ss -Hlntup` printed on a Rocky 9 host an older ltvm set up.
SS_LEGACY = """\
udp   UNCONN 0 0 192.168.200.1:53 0.0.0.0:* users:(("dnsmasq",pid=2584,fd=6))
udp   UNCONN 0 0 0.0.0.0%fcbr0:67 0.0.0.0:* users:(("dnsmasq",pid=2584,fd=4))
udp   UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:* users:(("systemd-resolve",pid=600,fd=14))
tcp   LISTEN 0 32 192.168.200.1:53 0.0.0.0:* users:(("dnsmasq",pid=2584,fd=7))
tcp   LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=900,fd=3))
"""

# The host's dnsmasq once it serves only its own addresses.
SS_HOST_OWN = """\
udp   UNCONN 0 0 127.0.0.1:53 0.0.0.0:* users:(("dnsmasq",pid=2584,fd=6))
tcp   LISTEN 0 32 192.168.100.5:53 0.0.0.0:* users:(("dnsmasq",pid=2584,fd=7))
"""
# ... and Debian's package config on its own: the wildcard address.
SS_WILDCARD = """\
udp   UNCONN 0 0 0.0.0.0:53 0.0.0.0:* users:(("dnsmasq",pid=2584,fd=4))
tcp   LISTEN 0 0 0.0.0.0:53 0.0.0.0:* users:(("dnsmasq",pid=2584,fd=5))
tcp   LISTEN 0 0 *:53 *:* users:(("dnsmasq",pid=2584,fd=7))
"""


def _done(rc: int = 0, out: str = "") -> MagicMock:
    r = MagicMock()
    r.returncode = rc
    r.stdout = out
    return r


class _Host:
    """A fake host: ss output, dnsmasq.service's state, and a record of
    every command run against it."""

    def __init__(
        self,
        ss: str | list[str] = "",
        host_pid: int = 0,
        enabled: bool = True,
        restart_rc: int = 0,
    ) -> None:
        # One ss output per call, the last repeating: what the host
        # looks like after each restart.
        self.ss = [ss] if isinstance(ss, str) else ss
        self.host_pid = host_pid
        self.enabled = enabled
        self.restart_rc = restart_rc
        self.cmds: list[list[str]] = []

    def __call__(self, cmd: list[str], **kw: object) -> MagicMock:
        self.cmds.append(cmd)
        if cmd[0] == "ss":
            return _done(0, self.ss.pop(0) if len(self.ss) > 1 else self.ss[0])
        if cmd[:2] == ["systemctl", "show"]:
            unit = cmd[-1]
            pid = self.host_pid if unit == "dnsmasq.service" else 0
            return _done(0, f"{pid}\n")
        if cmd[:2] == ["systemctl", "is-enabled"]:
            return _done(0 if self.enabled else 1)
        if cmd[:2] == ["systemctl", "restart"]:
            return _done(self.restart_rc)
        return _done(0)

    def ran(self, *prefix: str) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.cmds)

    def count(self, *prefix: str) -> int:
        return sum(c[: len(prefix)] == list(prefix) for c in self.cmds)


@pytest.fixture
def paths(tmp_path: Path):
    dropins = tmp_path / "dnsmasq.d"
    dropins.mkdir()
    legacy = dropins / "qemu-vms.conf"
    with (
        patch.object(host_setup, "LEGACY_DNSMASQ_CONF", legacy),
        patch.object(host_setup, "HOST_DNSMASQ_DIR", dropins),
        patch.object(
            host_setup, "HOST_DNSMASQ_EXCEPT", dropins / "ltvm-fcbr0.conf"
        ),
    ):
        yield dropins, legacy


class TestRender:
    def test_conf_is_self_contained(self, tmp_path: Path) -> None:
        with patch.object(host_setup, "VM_DIR", tmp_path):
            text = host_setup._render_ltvm_dnsmasq_conf("192.168.200")
        lines = text.splitlines()
        assert "listen-address=192.168.200.1" in lines
        assert "dhcp-range=192.168.200.10,192.168.200.254,12h" in lines
        assert not any("192.168.100" in ln for ln in lines)
        # The default lease file is the host dnsmasq's.
        assert "leasefile-ro" in lines
        # It must never pull the host's configuration back in.
        assert not any(ln.startswith(("conf-dir", "conf-file")) for ln in lines)
        assert f"hostsdir={tmp_path / 'hosts.d'}" in lines

    def test_unit_runs_only_our_config(self) -> None:
        unit = host_setup._render_ltvm_dnsmasq_unit("/usr/sbin/dnsmasq")
        assert "@" not in unit
        start = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
        assert start == [
            "ExecStart=/usr/sbin/dnsmasq "
            f"--conf-file={host_setup.LTVM_DNSMASQ_CONF} "
            f"--pid-file={host_setup.LTVM_DNSMASQ_PID}"
        ]
        assert f"PIDFile={host_setup.LTVM_DNSMASQ_PID}" in unit


class TestBridgeSockets:
    def test_finds_the_bridge_and_wildcard_listeners_only(self) -> None:
        ss = SS_LEGACY + (
            'tcp LISTEN 0 32 0.0.0.0:53 0.0.0.0:* users:(("named",pid=77,fd=5))\n'
        )
        with patch.object(host_setup, "_run_quiet", _Host(ss=ss)):
            lines = host_setup._bridge_sockets("192.168.200")
        assert host_setup._socket_pids(lines) == {2584, 77}
        assert not any("systemd-resolve" in ln or "sshd" in ln for ln in lines)


class TestHandOver:
    """Moving the bridge off the host's dnsmasq.service."""

    def test_legacy_disabled_host_dnsmasq_is_stopped(self, paths) -> None:
        """EL: an older ltvm started dnsmasq.service and never enabled
        it.  Stopping it is what the next boot would do anyway."""
        dropins, legacy = paths
        legacy.write_text("interface=fcbr0\n")
        host = _Host(ss=SS_LEGACY, host_pid=2584, enabled=False)
        with patch.object(host_setup, "_run_quiet", host):
            host_setup._hand_over_from_host_dnsmasq("192.168.200")
        assert not legacy.exists()
        assert host.ran("systemctl", "stop", "dnsmasq")
        assert not host.ran("systemctl", "restart")

    def test_legacy_enabled_host_dnsmasq_gets_its_own_config_back(
        self, paths
    ) -> None:
        dropins, legacy = paths
        legacy.write_text("interface=fcbr0\n")
        host = _Host(ss=[SS_LEGACY, SS_HOST_OWN], host_pid=2584, enabled=True)
        with patch.object(host_setup, "_run_quiet", host):
            host_setup._hand_over_from_host_dnsmasq("192.168.200")
        assert host.count("systemctl", "restart", "dnsmasq") == 1
        assert not host.ran("systemctl", "stop")
        assert (
            (dropins / "ltvm-fcbr0.conf")
            .read_text()
            .endswith("except-interface=fcbr0\n")
        )

    def test_host_dnsmasq_that_cannot_run_alone_is_reported(
        self, paths
    ) -> None:
        """Ubuntu: dnsmasq.service could only ever run with ltvm's
        settings, since systemd-resolved holds port 53 on loopback.
        Say so; disabling a host service is the user's call."""
        _, legacy = paths
        legacy.write_text("interface=fcbr0\n")
        host = _Host(ss=SS_LEGACY, host_pid=2584, restart_rc=1)
        with (
            patch.object(host_setup, "_run_quiet", host),
            patch.object(host_setup, "log") as log,
        ):
            host_setup._hand_over_from_host_dnsmasq("192.168.200")
        assert "systemctl disable dnsmasq" in log.warning.call_args.args[0]
        assert not host.ran("systemctl", "disable")

    def test_host_dnsmasq_off_the_bridge_is_not_touched(self, paths) -> None:
        """The issue's own case: a host dnsmasq serving something else
        is neither restarted nor stopped."""
        host = _Host(ss=SS_LEGACY, host_pid=4242)
        with patch.object(host_setup, "_run_quiet", host):
            host_setup._hand_over_from_host_dnsmasq("192.168.200")
        assert not host.ran("systemctl", "restart")
        assert not host.ran("systemctl", "stop")

    def test_bind_dynamic_host_dnsmasq_on_the_bridge_is_restarted(
        self, paths
    ) -> None:
        """No old layout, but a host dnsmasq running bind-dynamic took
        the bridge address as it appeared: restarting it loads the
        except-interface drop-in."""
        host = _Host(ss=[SS_LEGACY, SS_HOST_OWN], host_pid=2584, enabled=False)
        with patch.object(host_setup, "_run_quiet", host):
            host_setup._hand_over_from_host_dnsmasq("192.168.200")
        assert host.count("systemctl", "restart", "dnsmasq") == 1

    def test_debian_wildcard_host_dnsmasq_is_made_to_bind_addresses(
        self, paths
    ) -> None:
        """Debian, no systemd-resolved: without ltvm's old settings the
        package's own config listens on 0.0.0.0:53, which leaves no
        other DNS server port 53.  bind-dynamic binds the same
        addresses one by one, and except-interface leaves fcbr0 out."""
        dropins, legacy = paths
        legacy.write_text("interface=fcbr0\n")
        host = _Host(ss=[SS_LEGACY, SS_WILDCARD, SS_HOST_OWN], host_pid=2584)
        with patch.object(host_setup, "_run_quiet", host):
            host_setup._hand_over_from_host_dnsmasq("192.168.200")
        assert host.count("systemctl", "restart", "dnsmasq") == 2
        lines = (dropins / "ltvm-fcbr0.conf").read_text().splitlines()
        assert "except-interface=fcbr0" in lines
        assert "bind-dynamic" in lines

    def test_bind_dynamic_once_added_is_kept(self, paths) -> None:
        """Dropping it again would put the host's dnsmasq back on the
        wildcard address at its next restart."""
        dropins, _ = paths
        (dropins / "ltvm-fcbr0.conf").write_text(
            host_setup.HOST_DNSMASQ_EXCEPT_TEXT
            + host_setup.HOST_DNSMASQ_BIND_DYNAMIC_TEXT
        )
        with patch.object(host_setup, "_run_quiet", _Host()):
            host_setup._hand_over_from_host_dnsmasq("192.168.200")
        assert (
            "bind-dynamic" in (dropins / "ltvm-fcbr0.conf").read_text().split()
        )

    def test_wildcard_dns_that_is_not_the_host_dnsmasq_is_not_touched(
        self, paths
    ) -> None:
        host = _Host(ss=SS_WILDCARD, host_pid=4242)
        with patch.object(host_setup, "_run_quiet", host):
            host_setup._hand_over_from_host_dnsmasq("192.168.200")
        assert not host.ran("systemctl", "restart")
        assert "bind-dynamic" not in (
            (paths[0] / "ltvm-fcbr0.conf").read_text().split()
        )

    def test_no_drop_in_without_the_hosts_directory(
        self, tmp_path: Path
    ) -> None:
        """dnsmasq-base alone ships no /etc/dnsmasq.d, and no service."""
        absent = tmp_path / "dnsmasq.d"
        with (
            patch.object(host_setup, "LEGACY_DNSMASQ_CONF", absent / "q.conf"),
            patch.object(host_setup, "HOST_DNSMASQ_DIR", absent),
            patch.object(host_setup, "HOST_DNSMASQ_EXCEPT", absent / "x.conf"),
            patch.object(host_setup, "_run_quiet", _Host()),
        ):
            host_setup._hand_over_from_host_dnsmasq("192.168.200")
        assert not absent.exists()


class TestStart:
    def test_failure_names_the_cause(self) -> None:
        ss = 'tcp LISTEN 0 32 0.0.0.0:53 0.0.0.0:* users:(("dnsmasq",pid=9,fd=5))\n'
        host = _Host(ss=ss, restart_rc=1)
        with (
            patch.object(host_setup, "_run_quiet", host),
            pytest.raises(RuntimeError) as e,
        ):
            host_setup._start_ltvm_dnsmasq("192.168.200")
        assert "ltvm-dnsmasq.service did not start" in str(e.value)
        assert "0.0.0.0:53" in str(e.value)


class TestSelinuxLabel:
    def test_spec_is_an_escaped_regex(self) -> None:
        with (
            patch("ltvm_pkg.host_setup.shutil.which", return_value="/x"),
            patch.object(host_setup, "_run_quiet", return_value=_done(0)) as rq,
        ):
            host_setup._label_hosts_dir(Path("/opt/qemu-vms/hosts.d"))
        semanage = rq.call_args_list[0].args[0]
        assert semanage == [
            "semanage",
            "fcontext",
            "-a",
            "-t",
            "dnsmasq_etc_t",
            r"/opt/qemu-vms/hosts\.d(/.*)?",
        ]
        assert rq.call_args_list[-1].args[0][0] == "restorecon"


class TestVerifyDnsmasq:
    def test_reports_ltvm_dnsmasq(self, paths) -> None:
        with patch.object(host_setup, "_run_quiet", return_value=_done(0)):
            v = host_setup._verify_dnsmasq()
        assert v == {"running": True, "service": "ltvm-dnsmasq.service"}

    def test_old_layout_is_named(self, paths) -> None:
        _, legacy = paths
        legacy.write_text("")

        def side(cmd: list[str], **kw: object) -> MagicMock:
            return _done(0 if cmd[-1] == "dnsmasq" else 3)

        with patch.object(host_setup, "_run_quiet", side_effect=side):
            v = host_setup._verify_dnsmasq()
        assert v["legacy"] and v["running"]
        assert v["service"] == "dnsmasq.service"

    def test_host_dnsmasq_does_not_count_as_ours(self, paths) -> None:
        """The old check, `systemctl is-active dnsmasq`, read a host's
        own dnsmasq as healthy while nothing served the bridge."""

        def side(cmd: list[str], **kw: object) -> MagicMock:
            return _done(0 if cmd[-1] == "dnsmasq" else 3)

        with patch.object(host_setup, "_run_quiet", side_effect=side):
            assert host_setup._verify_dnsmasq()["running"] is False


class TestReloadDns:
    @pytest.fixture
    def linux(self, tmp_path: Path):
        with (
            patch("ltvm_pkg.host_setup.is_macos", return_value=False),
            patch.object(host_setup, "LTVM_DNSMASQ_PID", tmp_path / "ltvm.pid"),
            patch.object(
                host_setup, "LEGACY_DNSMASQ_CONF", tmp_path / "q.conf"
            ),
            patch("ltvm_pkg.vm_net.os.kill") as kill,
            patch("ltvm_pkg.vm_net.run") as run,
        ):
            yield tmp_path, kill, run

    def test_signals_ltvm_dnsmasq(self, linux) -> None:
        tmp_path, kill, run = linux
        (tmp_path / "ltvm.pid").write_text("123\n")
        vm_net.reload_dns()
        assert kill.call_args.args[0] == 123
        run.assert_not_called()

    def test_never_signals_some_other_dnsmasq(self, linux) -> None:
        """pgrep would find the host's own dnsmasq, or libvirt's."""
        _, kill, run = linux
        with pytest.raises(RuntimeError, match="ltvm-dnsmasq"):
            vm_net.reload_dns()
        kill.assert_not_called()
        run.assert_not_called()

    def test_old_layout_signals_the_host_dnsmasq(self, linux) -> None:
        tmp_path, kill, run = linux
        (tmp_path / "q.conf").write_text("")
        run.return_value = _done(0, "456\n")
        with patch("ltvm_pkg.vm_net.Path") as P:
            P.return_value.exists.return_value = False
            vm_net.reload_dns()
        assert kill.call_args.args[0] == 456


class TestStartOnRerun:
    def test_rerun_leaves_a_running_unchanged_instance(self) -> None:
        host = _Host()
        with patch.object(host_setup, "_run_quiet", host):
            host_setup._start_ltvm_dnsmasq("192.168.200", restart=False)
        assert host.ran("systemctl", "is-active")
        assert not host.ran("systemctl", "restart")

    def test_rerun_starts_a_stopped_instance(self) -> None:
        host = _Host()
        real = host.__call__

        def stopped(cmd: list[str], **kw: object) -> MagicMock:
            if cmd[:2] == ["systemctl", "is-active"]:
                host.cmds.append(cmd)
                return _done(3)
            return real(cmd, **kw)

        with patch.object(host_setup, "_run_quiet", stopped):
            host_setup._start_ltvm_dnsmasq("192.168.200", restart=False)
        assert host.ran("systemctl", "restart")
