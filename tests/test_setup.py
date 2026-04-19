"""Tests for ltvm_pkg/host_setup.py -- host detection, package mapping, SSH."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg.host_setup import (
    SSH_BLOCK_MARKER,
    HostInfo,
    _check_stale_ltvm_launcher,
    _install_ltvm_launcher,
    _ltvm_launcher_needs_write,
    _network_already_configured,
    _qemu_installed_version,
    _render_ltvm_launcher,
    _translate_pkgs,
    check_kvm,
    check_prerequisites,
    print_verify,
    setup_ssh,
    verify,
)


@pytest.mark.skipif(
    platform.system() == "Darwin", reason="HostInfo is Linux-only"
)
class TestHostInfo:
    def test_detects_current_host(self) -> None:
        """Should not raise on this machine."""
        info = HostInfo()
        assert info.id != "unknown"
        assert info.pkg_mgr in ("dnf", "apt")
        assert info.pretty_name != "unknown"

    def test_real_os_release_parses(self, tmp_path: Path) -> None:
        """Smoke check: HostInfo() reads the real /etc/os-release on this
        host and produces a non-None id.  The genuinely-missing-file case
        is hard to mock without breaking HostInfo's internal Path usage,
        so we don't cover it here."""
        info = HostInfo()
        assert info.id is not None

    def test_str_representation(self) -> None:
        info = HostInfo()
        s = str(info)
        assert "(" in s  # "(dnf)" or "(apt)"


class TestTranslatePkgs:
    def test_dnf_passthrough(self) -> None:
        host = HostInfo.__new__(HostInfo)
        host.id = "rocky"
        host.version = "9"
        host.pretty_name = "Rocky Linux 9"
        host.pkg_mgr = "dnf"
        result = _translate_pkgs(("glib2-devel", "make"), host)
        assert result == ["glib2-devel", "make"]

    def test_apt_translation(self) -> None:
        """Test package name mapping for apt-based hosts."""
        # Create a mock host with apt
        host = HostInfo.__new__(HostInfo)
        host.id = "ubuntu"
        host.version = "22.04"
        host.pretty_name = "Ubuntu 22.04"
        host.pkg_mgr = "apt"

        result = _translate_pkgs(("glib2-devel", "pixman-devel"), host)
        assert result == ["libglib2.0-dev", "libpixman-1-dev"]

    def test_apt_unknown_pkg_passthrough(self) -> None:
        """Unmapped packages pass through unchanged."""
        host = HostInfo.__new__(HostInfo)
        host.pkg_mgr = "apt"
        result = _translate_pkgs(("curl", "tar"), host)
        assert result == ["curl", "tar"]


class TestSetupSsh:
    """Tests that call the real setup_ssh() against a temp directory."""

    def _call_setup_ssh(
        self, tmp_path: Path, subnet: str = "192.168.100"
    ) -> Path:
        """Run setup_ssh() with /root/.ssh redirected to tmp_path."""
        ssh_dir = tmp_path / ".ssh"
        with patch(
            "ltvm_pkg.host_setup.Path",
            side_effect=lambda p: ssh_dir if p == "/root/.ssh" else Path(p),
        ):
            setup_ssh(subnet)
        return ssh_dir / "config"

    def test_creates_ssh_config(self, tmp_path: Path) -> None:
        config = self._call_setup_ssh(tmp_path)
        text = config.read_text()
        assert SSH_BLOCK_MARKER in text
        assert "StrictHostKeyChecking no" in text
        assert "Host 192.168.100.*" in text
        assert "User root" in text

    def test_marker_format(self) -> None:
        assert SSH_BLOCK_MARKER == "# lustre-test-vms"

    def test_ssh_block_contains_fast_timeouts(self, tmp_path: Path) -> None:
        """SSH config should have aggressive timeouts for VMs."""
        config = self._call_setup_ssh(tmp_path)
        text = config.read_text()
        assert "ServerAliveInterval 5" in text
        assert "ServerAliveCountMax 3" in text
        assert "ConnectTimeout 5" in text

    def test_custom_subnet(self, tmp_path: Path) -> None:
        """Different subnet produces the correct Host pattern."""
        config = self._call_setup_ssh(tmp_path, subnet="10.0.0")
        text = config.read_text()
        assert "Host 10.0.0.*" in text

    def test_idempotent(self, tmp_path: Path) -> None:
        """Calling setup_ssh twice does not duplicate the block."""
        self._call_setup_ssh(tmp_path)
        config = self._call_setup_ssh(tmp_path)
        text = config.read_text()
        assert text.count(SSH_BLOCK_MARKER) == 1

    def test_subnet_change_replaces_block(self, tmp_path: Path) -> None:
        """Changing subnet replaces the old block."""
        self._call_setup_ssh(tmp_path, subnet="192.168.100")
        config = self._call_setup_ssh(tmp_path, subnet="10.0.0")
        text = config.read_text()
        assert "Host 10.0.0.*" in text
        assert "192.168.100" not in text
        assert text.count(SSH_BLOCK_MARKER) == 1


# ------------------------------------------------------------------
# TestHostInfoNoPkgMgr
# ------------------------------------------------------------------


@pytest.mark.skipif(
    platform.system() == "Darwin", reason="HostInfo is Linux-only"
)
class TestHostInfoNoPkgMgr:
    def test_raises_when_no_package_manager(self) -> None:
        """RuntimeError raised when neither dnf nor apt-get found."""
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="package manager"):
                HostInfo()


# ------------------------------------------------------------------
# TestNetworkAlreadyConfigured
# ------------------------------------------------------------------


class TestNetworkAlreadyConfigured:
    """The detection short-circuit must respect a working pre-existing
    setup, so a host with a hand-rolled or coexisting dnsmasq drop-in
    (e.g. an older firecracker tooling shipping `bind-interfaces`) is
    not stomped on by `ltvm install`."""

    def _mock_run_quiet(self, ip_returncode, ip_stdout, dnsmasq_returncode):
        def side(cmd, **kw):
            r = MagicMock()
            if "ip" in cmd:
                r.returncode = ip_returncode
                r.stdout = ip_stdout
            elif "systemctl" in cmd:
                r.returncode = dnsmasq_returncode
                r.stdout = ""
            return r

        return side

    def test_bridge_missing_returns_false(self) -> None:
        """No fcbr0 -> we should configure."""
        with patch(
            "ltvm_pkg.host_setup._run_quiet",
            side_effect=self._mock_run_quiet(
                ip_returncode=1, ip_stdout="", dnsmasq_returncode=0
            ),
        ):
            assert _network_already_configured("192.168.100") is False

    def test_bridge_wrong_subnet_returns_false(self) -> None:
        """fcbr0 exists but with a different address -> still configure
        (we will need to reconcile)."""
        with patch(
            "ltvm_pkg.host_setup._run_quiet",
            side_effect=self._mock_run_quiet(
                ip_returncode=0,
                ip_stdout="    inet 10.0.0.1/24 scope global fcbr0\n",
                dnsmasq_returncode=0,
            ),
        ):
            assert _network_already_configured("192.168.100") is False

    def test_dnsmasq_inactive_returns_false(self) -> None:
        """Bridge exists with the right address but dnsmasq isn't running
        -> still need to configure."""
        with patch(
            "ltvm_pkg.host_setup._run_quiet",
            side_effect=self._mock_run_quiet(
                ip_returncode=0,
                ip_stdout="    inet 192.168.100.1/24 scope global fcbr0\n",
                dnsmasq_returncode=3,  # inactive
            ),
        ):
            assert _network_already_configured("192.168.100") is False

    def test_both_present_returns_true(self) -> None:
        """The happy short-circuit case."""
        with patch(
            "ltvm_pkg.host_setup._run_quiet",
            side_effect=self._mock_run_quiet(
                ip_returncode=0,
                ip_stdout="    inet 192.168.100.1/24 scope global fcbr0\n",
                dnsmasq_returncode=0,
            ),
        ):
            assert _network_already_configured("192.168.100") is True


# ------------------------------------------------------------------
# TestCheckPrerequisites
# ------------------------------------------------------------------


class TestCheckPrerequisites:
    def _make_host(self, pkg_mgr: str) -> HostInfo:
        host = HostInfo.__new__(HostInfo)
        host.id = "rocky"
        host.version = "9"
        host.pretty_name = "Rocky Linux 9"
        host.pkg_mgr = pkg_mgr
        return host

    def test_no_missing_no_install(self) -> None:
        """When all cmds present, _pkg_install is not called."""
        host = self._make_host("dnf")
        with patch("shutil.which", return_value="/usr/bin/cmd"):
            with patch("ltvm_pkg.host_setup._pkg_install") as mock_install:
                check_prerequisites(host)
        mock_install.assert_not_called()

    def test_missing_cmds_calls_install(self) -> None:
        """Missing cmds trigger _pkg_install with the right packages."""
        host = self._make_host("dnf")

        def _which(cmd: str) -> str | None:
            # curl and tar are missing; everything else present
            if cmd in ("curl", "tar"):
                return None
            return f"/usr/bin/{cmd}"

        with patch("shutil.which", side_effect=_which):
            with patch("ltvm_pkg.host_setup._pkg_install") as mock_install:
                check_prerequisites(host)

        # Should be called once with the two missing packages
        mock_install.assert_called_once()
        args = mock_install.call_args[0]
        assert args[0] is host
        assert "curl" in args
        assert "tar" in args

    def test_iproute_pkg_name_dnf(self) -> None:
        """dnf host uses 'iproute' for the 'ip' command."""
        host = self._make_host("dnf")

        def _which(cmd: str) -> str | None:
            if cmd == "ip":
                return None
            return f"/usr/bin/{cmd}"

        with patch("shutil.which", side_effect=_which):
            with patch("ltvm_pkg.host_setup._pkg_install") as mock_install:
                check_prerequisites(host)

        args = mock_install.call_args[0]
        assert "iproute" in args
        assert "iproute2" not in args

    def test_iproute2_pkg_name_apt(self) -> None:
        """apt host uses 'iproute2' for the 'ip' command."""
        host = self._make_host("apt")

        def _which(cmd: str) -> str | None:
            if cmd == "ip":
                return None
            return f"/usr/bin/{cmd}"

        with patch("shutil.which", side_effect=_which):
            with patch("ltvm_pkg.host_setup._pkg_install") as mock_install:
                check_prerequisites(host)

        args = mock_install.call_args[0]
        assert "iproute2" in args
        assert "iproute" not in args

    def test_podman_missing_no_exception(self) -> None:
        """Missing podman only logs a warning -- no exception raised."""
        host = self._make_host("dnf")

        def _which(cmd: str) -> str | None:
            if cmd == "podman":
                return None
            return f"/usr/bin/{cmd}"

        # Should not raise
        with patch("shutil.which", side_effect=_which):
            with patch("ltvm_pkg.host_setup._pkg_install"):
                check_prerequisites(host)


# ------------------------------------------------------------------
# TestCheckKvm
# ------------------------------------------------------------------


class TestCheckKvm:
    def test_kvm_exists_returns_true(self) -> None:
        """Returns True when /dev/kvm is present."""
        with patch("ltvm_pkg.host_setup.Path") as mock_path_cls:
            mock_kvm = MagicMock()
            mock_kvm.exists.return_value = True
            mock_path_cls.return_value = mock_kvm
            result = check_kvm()
        assert result is True

    def test_kvm_missing_require_true_raises(self) -> None:
        """Raises RuntimeError when /dev/kvm absent and require=True."""
        with patch("ltvm_pkg.host_setup.Path") as mock_path_cls:
            mock_kvm = MagicMock()
            mock_kvm.exists.return_value = False
            mock_path_cls.return_value = mock_kvm
            with pytest.raises(RuntimeError, match="/dev/kvm"):
                check_kvm(require=True)

    def test_kvm_missing_require_false_returns_false(self) -> None:
        """Returns False without raising when require=False."""
        with patch("ltvm_pkg.host_setup.Path") as mock_path_cls:
            mock_kvm = MagicMock()
            mock_kvm.exists.return_value = False
            mock_path_cls.return_value = mock_kvm
            result = check_kvm(require=False)
        assert result is False


# ------------------------------------------------------------------
# TestQemuInstalledVersion
# ------------------------------------------------------------------


class TestQemuInstalledVersion:
    def test_qemu_binary_missing_returns_none(self, tmp_path: Path) -> None:
        """Returns None when the qemu binary does not exist."""
        fake_prefix = tmp_path / "qemu"
        # Do NOT create the binary -- it should not exist.
        with patch("ltvm_pkg.host_setup.QEMU_PREFIX", fake_prefix):
            result = _qemu_installed_version()
        assert result is None

    def test_qemu_version_parsed(self, tmp_path: Path) -> None:
        """Returns version string when output contains 'version X.Y.Z'."""
        fake_bin = tmp_path / "bin" / "qemu-system-x86_64"
        fake_bin.parent.mkdir(parents=True)
        fake_bin.touch()

        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="QEMU emulator version 9.2.2\n"
        )
        with patch("ltvm_pkg.host_setup.QEMU_PREFIX", tmp_path):
            with patch(
                "ltvm_pkg.host_setup._run_quiet", return_value=completed
            ):
                result = _qemu_installed_version()
        assert result == "9.2.2"

    def test_qemu_no_version_in_output(self, tmp_path: Path) -> None:
        """Returns 'unknown' when output has no recognisable version."""
        fake_bin = tmp_path / "bin" / "qemu-system-x86_64"
        fake_bin.parent.mkdir(parents=True)
        fake_bin.touch()

        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="something completely different\n"
        )
        with patch("ltvm_pkg.host_setup.QEMU_PREFIX", tmp_path):
            with patch(
                "ltvm_pkg.host_setup._run_quiet", return_value=completed
            ):
                result = _qemu_installed_version()
        assert result == "unknown"

    def test_qemu_exception_returns_none(self, tmp_path: Path) -> None:
        """Returns None when running the binary raises an exception."""
        fake_bin = tmp_path / "bin" / "qemu-system-x86_64"
        fake_bin.parent.mkdir(parents=True)
        fake_bin.touch()

        with patch("ltvm_pkg.host_setup.QEMU_PREFIX", tmp_path):
            with patch(
                "ltvm_pkg.host_setup._run_quiet", side_effect=OSError("oops")
            ):
                result = _qemu_installed_version()
        assert result is None


# ------------------------------------------------------------------
# TestVerify
# ------------------------------------------------------------------


def _mock_completed(returncode: int = 0, stdout: str = "") -> MagicMock:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    return r


class TestVerify:
    def _patch_all_ok(self) -> dict:
        """Return a dict of patch kwargs representing a fully healthy host."""
        return {
            "ltvm_pkg.host_setup._qemu_installed_version": "9.2.2",
            # /dev/kvm exists
            # bridge ip cmd succeeds with an inet address
            # dnsmasq is active
            # scripts are in PATH
            # podman is present
            # SSH config exists and has SSH_BLOCK_MARKER
        }

    def test_all_ok(self) -> None:
        """all_ok=True when every component is healthy."""
        ssh_mock = MagicMock()
        ssh_mock.exists.return_value = True
        ssh_mock.read_text.return_value = f"{SSH_BLOCK_MARKER}\n"

        def _run_quiet_side(cmd: list, **kw: object) -> MagicMock:
            # ip addr show fcbr0
            if "fcbr0" in cmd:
                return _mock_completed(0, "inet 192.168.100.1/24")
            # systemctl is-active dnsmasq
            if "dnsmasq" in cmd:
                return _mock_completed(0)
            # podman --version
            if "podman" in cmd:
                return _mock_completed(0, "podman version 4.9.0")
            return _mock_completed(0)

        with (
            patch(
                "ltvm_pkg.host_setup._qemu_installed_version",
                return_value="9.2.2",
            ),
            patch(
                "ltvm_pkg.host_setup.Path",
                side_effect=lambda p: (
                    ssh_mock
                    if p == "/root/.ssh/config"
                    else MagicMock(exists=MagicMock(return_value=True))
                ),
            ),
            patch(
                "ltvm_pkg.host_setup._run_quiet", side_effect=_run_quiet_side
            ),
            patch(
                "shutil.which",
                side_effect=lambda cmd: f"/usr/bin/{cmd}",
            ),
            patch(
                "ltvm_pkg.host_setup.socket_vmnet_path",
                return_value=Path(
                    "/opt/homebrew/opt/socket_vmnet/bin/socket_vmnet"
                ),
            ),
        ):
            result = verify()

        assert result["all_ok"] is True
        assert result["qemu"]["installed"] is True
        assert result["qemu"]["version"] == "9.2.2"
        assert result["kvm"]["available"] is True
        assert result["bridge"]["up"] is True
        assert result["dnsmasq"]["running"] is True
        assert result["ltvm"]["installed"] is True
        assert result["podman"]["installed"] is True
        assert result["ssh"]["configured"] is True

    def test_qemu_missing(self) -> None:
        """all_ok=False and qemu.installed=False when QEMU absent."""
        ssh_mock = MagicMock()
        ssh_mock.exists.return_value = True
        ssh_mock.read_text.return_value = f"{SSH_BLOCK_MARKER}\n"

        def _run_quiet_side(cmd: list, **kw: object) -> MagicMock:
            if "fcbr0" in cmd:
                return _mock_completed(0, "inet 192.168.100.1/24")
            if "dnsmasq" in cmd:
                return _mock_completed(0)
            if "podman" in cmd:
                return _mock_completed(0, "podman version 4.9.0")
            return _mock_completed(0)

        with (
            patch(
                "ltvm_pkg.host_setup._qemu_installed_version", return_value=None
            ),
            patch(
                "ltvm_pkg.host_setup.Path",
                side_effect=lambda p: (
                    ssh_mock
                    if p == "/root/.ssh/config"
                    else MagicMock(exists=MagicMock(return_value=True))
                ),
            ),
            patch(
                "ltvm_pkg.host_setup._run_quiet", side_effect=_run_quiet_side
            ),
            patch("shutil.which", side_effect=lambda cmd: f"/usr/bin/{cmd}"),
        ):
            result = verify()

        assert result["qemu"]["installed"] is False
        assert result["all_ok"] is False

    @pytest.mark.skipif(
        platform.system() == "Darwin",
        reason="verify() KVM check is Linux-only",
    )
    def test_kvm_missing(self) -> None:
        """all_ok=False when /dev/kvm is absent."""
        ssh_mock = MagicMock()
        ssh_mock.exists.return_value = True
        ssh_mock.read_text.return_value = f"{SSH_BLOCK_MARKER}\n"

        def _path_side(p: str) -> MagicMock:
            m = MagicMock()
            if p == "/root/.ssh/config":
                return ssh_mock
            if p == "/dev/kvm":
                m.exists.return_value = False
                return m
            m.exists.return_value = True
            return m

        def _run_quiet_side(cmd: list, **kw: object) -> MagicMock:
            if "fcbr0" in cmd:
                return _mock_completed(0, "inet 192.168.100.1/24")
            if "dnsmasq" in cmd:
                return _mock_completed(0)
            if "podman" in cmd:
                return _mock_completed(0, "podman version 4.9.0")
            return _mock_completed(0)

        with (
            patch(
                "ltvm_pkg.host_setup._qemu_installed_version",
                return_value="9.2.2",
            ),
            patch("ltvm_pkg.host_setup.Path", side_effect=_path_side),
            patch(
                "ltvm_pkg.host_setup._run_quiet", side_effect=_run_quiet_side
            ),
            patch("shutil.which", side_effect=lambda cmd: f"/usr/bin/{cmd}"),
        ):
            result = verify()

        assert result["kvm"]["available"] is False
        assert result["all_ok"] is False

    def test_result_structure(self) -> None:
        """Result dict contains all expected keys."""
        ssh_mock = MagicMock()
        ssh_mock.exists.return_value = False
        ssh_mock.read_text.return_value = ""

        def _run_quiet_side(cmd: list, **kw: object) -> MagicMock:
            return _mock_completed(1)

        with (
            patch(
                "ltvm_pkg.host_setup._qemu_installed_version", return_value=None
            ),
            patch(
                "ltvm_pkg.host_setup.Path",
                side_effect=lambda p: (
                    ssh_mock
                    if p == "/root/.ssh/config"
                    else MagicMock(exists=MagicMock(return_value=False))
                ),
            ),
            patch(
                "ltvm_pkg.host_setup._run_quiet", side_effect=_run_quiet_side
            ),
            patch("shutil.which", return_value=None),
        ):
            result = verify()

        for key in (
            "qemu",
            "kvm",
            "bridge",
            "dnsmasq",
            "ltvm",
            "podman",
            "ssh",
            "all_ok",
        ):
            assert key in result, f"Missing key: {key}"


# ------------------------------------------------------------------
# TestPrintVerify
# ------------------------------------------------------------------


def _all_ok_result() -> dict:
    return {
        "qemu": {"installed": True, "version": "9.2.2", "path": "/opt/qemu"},
        "qemu_aarch64": {"installed": False, "version": None},
        "kvm": {"available": True},
        "bridge": {"up": True, "address": "192.168.100.1/24"},
        "dnsmasq": {"running": True},
        "ltvm": {"installed": True, "path": "/usr/local/bin/ltvm"},
        "podman": {"installed": True, "version": "4.9.0"},
        "ssh": {"configured": True},
        "all_ok": True,
    }


def _failing_result() -> dict:
    r = _all_ok_result()
    r["qemu"]["installed"] = False
    r["qemu"]["version"] = None
    r["kvm"]["available"] = False
    r["all_ok"] = False
    return r


class TestPrintVerify:
    def test_all_ok_message(self, capsys: pytest.CaptureFixture) -> None:
        """Prints 'All checks passed.' when all_ok=True."""
        print_verify(_all_ok_result())
        captured = capsys.readouterr()
        assert "All checks passed." in captured.out

    def test_some_failing_message(self, capsys: pytest.CaptureFixture) -> None:
        """Prints failure summary when all_ok=False."""
        print_verify(_failing_result())
        captured = capsys.readouterr()
        assert "Some checks failed" in captured.out

    def test_qemu_installed_prints_version_and_path(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """When QEMU is installed, prints its version and path."""
        print_verify(_all_ok_result())
        captured = capsys.readouterr()
        assert "9.2.2" in captured.out
        assert "/opt/qemu" in captured.out

    def test_qemu_missing_prints_warning(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """When QEMU is missing, prints 'WARNING: QEMU x86_64: not installed'."""
        result = _all_ok_result()
        result["qemu"]["installed"] = False
        result["qemu"]["version"] = None
        result["all_ok"] = False
        print_verify(result)
        captured = capsys.readouterr()
        assert "WARNING: QEMU x86_64: not installed" in captured.out

    def test_kvm_missing_prints_warning(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """KVM absent produces a warning line."""
        result = _all_ok_result()
        result["kvm"]["available"] = False
        result["all_ok"] = False
        print_verify(result)
        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "kvm" in captured.out.lower()


# ------------------------------------------------------------------
# TestInstallPodmanMacos
# ------------------------------------------------------------------


class TestInstallPodmanMacos:
    """Covers the four install_podman_macos branches on macOS.

    Uses mocks for brew + podman so no real binaries are invoked.
    """

    def _completed(
        self, returncode: int = 0, stdout: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=""
        )

    def test_podman_not_installed_and_no_machine(self) -> None:
        """brew install podman; then init + start (no existing machine)."""
        from ltvm_pkg.host_setup import install_podman_macos

        which_calls: list[str] = []

        def which(cmd: str) -> str | None:
            which_calls.append(cmd)
            if cmd == "podman":
                return None if len(which_calls) == 1 else "/opt/homebrew/bin/podman"
            if cmd == "brew":
                return "/opt/homebrew/bin/brew"
            return None

        run_calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            run_calls.append(list(cmd))
            if "machine" in cmd and "list" in cmd:
                return self._completed(0, "[]")
            return self._completed(0, "")

        with (
            patch("ltvm_pkg.host_setup.shutil.which", side_effect=which),
            patch(
                "ltvm_pkg.host_setup._run",
                side_effect=lambda c, **kw: fake_run(c, **kw),
            ),
            patch(
                "ltvm_pkg.host_setup.subprocess.run",
                side_effect=lambda c, **kw: fake_run(c, **kw),
            ),
        ):
            started = install_podman_macos()

        assert started is True
        assert any("brew" in c[0] and "install" in c for c in run_calls)
        assert any("machine" in c and "init" in c for c in run_calls)
        assert any("machine" in c and "start" in c for c in run_calls)

    def test_podman_installed_no_machine(self) -> None:
        """podman present, no machine defined: init + start."""
        from ltvm_pkg.host_setup import install_podman_macos

        run_calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            run_calls.append(list(cmd))
            if "machine" in cmd and "list" in cmd:
                return self._completed(0, "[]")
            return self._completed(0, "")

        with (
            patch(
                "ltvm_pkg.host_setup.shutil.which",
                return_value="/opt/homebrew/bin/podman",
            ),
            patch(
                "ltvm_pkg.host_setup._run",
                side_effect=lambda c, **kw: fake_run(c, **kw),
            ),
            patch(
                "ltvm_pkg.host_setup.subprocess.run",
                side_effect=lambda c, **kw: fake_run(c, **kw),
            ),
        ):
            started = install_podman_macos()

        assert started is True
        assert not any("brew" in c[0] and "install" in c for c in run_calls)
        assert any("machine" in c and "init" in c for c in run_calls)
        assert any("machine" in c and "start" in c for c in run_calls)

    def test_podman_installed_machine_stopped(self) -> None:
        """Machine exists but not running: start it (no init)."""
        from ltvm_pkg.host_setup import install_podman_macos

        run_calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            run_calls.append(list(cmd))
            if "machine" in cmd and "list" in cmd:
                return self._completed(
                    0,
                    '[{"Name": "podman-machine-default", "Running": false}]',
                )
            return self._completed(0, "")

        with (
            patch(
                "ltvm_pkg.host_setup.shutil.which",
                return_value="/opt/homebrew/bin/podman",
            ),
            patch(
                "ltvm_pkg.host_setup._run",
                side_effect=lambda c, **kw: fake_run(c, **kw),
            ),
            patch(
                "ltvm_pkg.host_setup.subprocess.run",
                side_effect=lambda c, **kw: fake_run(c, **kw),
            ),
        ):
            started = install_podman_macos()

        assert started is True
        assert not any("machine" in c and "init" in c for c in run_calls)
        assert any("machine" in c and "start" in c for c in run_calls)

    def test_podman_installed_machine_running(self) -> None:
        """Already running: no init, no start, returns False."""
        from ltvm_pkg.host_setup import install_podman_macos

        run_calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            run_calls.append(list(cmd))
            if "machine" in cmd and "list" in cmd:
                return self._completed(
                    0,
                    '[{"Name": "podman-machine-default", "Running": true}]',
                )
            return self._completed(0, "")

        with (
            patch(
                "ltvm_pkg.host_setup.shutil.which",
                return_value="/opt/homebrew/bin/podman",
            ),
            patch(
                "ltvm_pkg.host_setup._run",
                side_effect=lambda c, **kw: fake_run(c, **kw),
            ),
            patch(
                "ltvm_pkg.host_setup.subprocess.run",
                side_effect=lambda c, **kw: fake_run(c, **kw),
            ),
        ):
            started = install_podman_macos()

        assert started is False
        assert not any("machine" in c and "init" in c for c in run_calls)
        assert not any("machine" in c and "start" in c for c in run_calls)

    def test_no_brew_raises(self) -> None:
        """If podman missing and Homebrew also missing: RuntimeError."""
        from ltvm_pkg.host_setup import install_podman_macos

        with (
            patch("ltvm_pkg.host_setup.shutil.which", return_value=None),
        ):
            with pytest.raises(RuntimeError, match="Homebrew not found"):
                install_podman_macos()


# ------------------------------------------------------------------
# TestShouldStopPodmanMachineMacos
# ------------------------------------------------------------------


class TestShouldStopPodmanMachineMacos:
    """The auto-stop heuristic: stop when no non-ltvm containers running."""

    def _completed(
        self, returncode: int = 0, stdout: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=""
        )

    def test_no_containers_running_returns_true(self) -> None:
        from ltvm_pkg.host_setup import should_stop_podman_machine_macos

        with patch(
            "ltvm_pkg.host_setup.subprocess.run",
            return_value=self._completed(0, "[]"),
        ):
            assert should_stop_podman_machine_macos() is True

    def test_all_ltvm_build_images_returns_true(self) -> None:
        from ltvm_pkg.host_setup import should_stop_podman_machine_macos

        stdout = (
            '[{"Image": "localhost/ltvm-build-rocky9:latest"},'
            ' {"Image": "ltvm-build-rocky10"}]'
        )
        with patch(
            "ltvm_pkg.host_setup.subprocess.run",
            return_value=self._completed(0, stdout),
        ):
            assert should_stop_podman_machine_macos() is True

    def test_mix_with_non_ltvm_returns_false(self) -> None:
        from ltvm_pkg.host_setup import should_stop_podman_machine_macos

        stdout = (
            '[{"Image": "ltvm-build-rocky9"},'
            ' {"Image": "docker.io/library/postgres:15"}]'
        )
        with patch(
            "ltvm_pkg.host_setup.subprocess.run",
            return_value=self._completed(0, stdout),
        ):
            assert should_stop_podman_machine_macos() is False

    def test_non_ltvm_only_returns_false(self) -> None:
        from ltvm_pkg.host_setup import should_stop_podman_machine_macos

        stdout = '[{"Image": "docker.io/library/nginx:latest"}]'
        with patch(
            "ltvm_pkg.host_setup.subprocess.run",
            return_value=self._completed(0, stdout),
        ):
            assert should_stop_podman_machine_macos() is False

    def test_podman_ps_fails_returns_false(self) -> None:
        """Don't stop if we can't tell what's running."""
        from ltvm_pkg.host_setup import should_stop_podman_machine_macos

        with patch(
            "ltvm_pkg.host_setup.subprocess.run",
            return_value=self._completed(1, ""),
        ):
            assert should_stop_podman_machine_macos() is False

    def test_podman_ps_raises_returns_false(self) -> None:
        from ltvm_pkg.host_setup import should_stop_podman_machine_macos

        with patch(
            "ltvm_pkg.host_setup.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="podman", timeout=10),
        ):
            assert should_stop_podman_machine_macos() is False

    def test_malformed_json_returns_false(self) -> None:
        from ltvm_pkg.host_setup import should_stop_podman_machine_macos

        with patch(
            "ltvm_pkg.host_setup.subprocess.run",
            return_value=self._completed(0, "not json"),
        ):
            assert should_stop_podman_machine_macos() is False


# ------------------------------------------------------------------
# TestLtvmLauncher
# ------------------------------------------------------------------


class TestRenderLtvmLauncher:
    def test_contains_shebang(self) -> None:
        text = _render_ltvm_launcher("/opt/homebrew/bin/python3.13", "/r/ltvm")
        assert text.startswith("#!/bin/sh\n")

    def test_exec_line_pins_python_and_script(self) -> None:
        text = _render_ltvm_launcher(
            "/opt/homebrew/bin/python3.13",
            "/Users/me/repo/ltvm",
        )
        assert (
            "exec '/opt/homebrew/bin/python3.13' '/Users/me/repo/ltvm' \"$@\"\n"
            in text
        )

    def test_different_python_produces_different_text(self) -> None:
        a = _render_ltvm_launcher("/usr/bin/python3.11", "/r/ltvm")
        b = _render_ltvm_launcher("/opt/homebrew/bin/python3.13", "/r/ltvm")
        assert a != b


class TestInstallLtvmLauncher:
    def _make_repo_ltvm(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        script = repo / "ltvm"
        script.write_text("#!/usr/bin/env python3\n")
        script.chmod(0o755)
        return script

    def test_writes_wrapper_when_missing(self, tmp_path: Path) -> None:
        script = self._make_repo_ltvm(tmp_path)
        link = tmp_path / "bin" / "ltvm"
        link.parent.mkdir()

        def fake_sudo(cmd: list, **kw: object) -> subprocess.CompletedProcess:
            # Simulate `install -m 0755 <tmp> <dest>` by copying.
            src, dst = cmd[-2], cmd[-1]
            Path(dst).write_text(Path(src).read_text())
            Path(dst).chmod(0o755)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch(
                "ltvm_pkg.host_setup.sys.executable",
                "/opt/homebrew/bin/python3.13",
            ),
            patch("ltvm_pkg.host_setup._sudo_run", side_effect=fake_sudo) as sr,
        ):
            wrote = _install_ltvm_launcher(link, script)

        assert wrote is True
        sr.assert_called_once()
        text = link.read_text()
        assert text.startswith("#!/bin/sh\n")
        assert (
            f"exec '/opt/homebrew/bin/python3.13' '{script.resolve()}' \"$@\"\n"
            in text
        )

    def test_idempotent_no_rewrite(self, tmp_path: Path) -> None:
        """Existing identical wrapper -> no sudo call, returns False."""
        script = self._make_repo_ltvm(tmp_path)
        link = tmp_path / "bin" / "ltvm"
        link.parent.mkdir()

        python = "/opt/homebrew/bin/python3.13"
        link.write_text(_render_ltvm_launcher(python, str(script.resolve())))
        link.chmod(0o755)

        with (
            patch("ltvm_pkg.host_setup.sys.executable", python),
            patch("ltvm_pkg.host_setup._sudo_run") as sr,
        ):
            wrote = _install_ltvm_launcher(link, script)

        assert wrote is False
        sr.assert_not_called()

    def test_changed_python_triggers_rewrite(self, tmp_path: Path) -> None:
        """A different sys.executable than last install -> rewrite."""
        script = self._make_repo_ltvm(tmp_path)
        link = tmp_path / "bin" / "ltvm"
        link.parent.mkdir()
        link.write_text(
            _render_ltvm_launcher("/usr/bin/python3.11", str(script.resolve()))
        )
        link.chmod(0o755)

        def fake_sudo(cmd: list, **kw: object) -> subprocess.CompletedProcess:
            src, dst = cmd[-2], cmd[-1]
            Path(dst).write_text(Path(src).read_text())
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch(
                "ltvm_pkg.host_setup.sys.executable",
                "/opt/homebrew/bin/python3.13",
            ),
            patch("ltvm_pkg.host_setup._sudo_run", side_effect=fake_sudo) as sr,
        ):
            wrote = _install_ltvm_launcher(link, script)

        assert wrote is True
        sr.assert_called_once()
        assert "/opt/homebrew/bin/python3.13" in link.read_text()
        assert "/usr/bin/python3.11" not in link.read_text()

    def test_replaces_symlink(self, tmp_path: Path) -> None:
        """A pre-existing symlink (old install layout) is replaced by a file."""
        script = self._make_repo_ltvm(tmp_path)
        link = tmp_path / "bin" / "ltvm"
        link.parent.mkdir()
        link.symlink_to(script)

        def fake_sudo(cmd: list, **kw: object) -> subprocess.CompletedProcess:
            src, dst = cmd[-2], cmd[-1]
            dst_p = Path(dst)
            if dst_p.is_symlink() or dst_p.exists():
                dst_p.unlink()
            dst_p.write_text(Path(src).read_text())
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with (
            patch(
                "ltvm_pkg.host_setup.sys.executable",
                "/opt/homebrew/bin/python3.13",
            ),
            patch("ltvm_pkg.host_setup._sudo_run", side_effect=fake_sudo),
        ):
            wrote = _install_ltvm_launcher(link, script)

        assert wrote is True
        assert not link.is_symlink()
        assert link.read_text().startswith("#!/bin/sh\n")


class TestLtvmLauncherNeedsWrite:
    def test_missing_link_needs_write(self, tmp_path: Path) -> None:
        script = tmp_path / "ltvm"
        script.write_text("x")
        link = tmp_path / "bin" / "ltvm"
        with patch(
            "ltvm_pkg.host_setup.sys.executable", "/opt/homebrew/bin/python3.13"
        ):
            assert _ltvm_launcher_needs_write(link, script) is True

    def test_symlink_needs_write(self, tmp_path: Path) -> None:
        script = tmp_path / "ltvm"
        script.write_text("x")
        link = tmp_path / "ltvm-link"
        link.symlink_to(script)
        with patch(
            "ltvm_pkg.host_setup.sys.executable", "/opt/homebrew/bin/python3.13"
        ):
            assert _ltvm_launcher_needs_write(link, script) is True

    def test_matching_wrapper_no_write(self, tmp_path: Path) -> None:
        script = tmp_path / "ltvm"
        script.write_text("x")
        link = tmp_path / "bin"
        link.parent.mkdir(parents=True, exist_ok=True)
        python = "/opt/homebrew/bin/python3.13"
        link.write_text(_render_ltvm_launcher(python, str(script.resolve())))
        with patch("ltvm_pkg.host_setup.sys.executable", python):
            assert _ltvm_launcher_needs_write(link, script) is False


class TestCheckStaleLtvmLauncher:
    def test_missing_link_silent(self, tmp_path: Path) -> None:
        link = tmp_path / "ltvm"
        with patch("ltvm_pkg.host_setup.log") as mock_log:
            _check_stale_ltvm_launcher(link)
        mock_log.warning.assert_not_called()

    def test_symlink_silent(self, tmp_path: Path) -> None:
        """Old-style symlink: not our wrapper, don't complain."""
        target = tmp_path / "ltvm"
        target.write_text("x")
        link = tmp_path / "ltvm-link"
        link.symlink_to(target)
        with patch("ltvm_pkg.host_setup.log") as mock_log:
            _check_stale_ltvm_launcher(link)
        mock_log.warning.assert_not_called()

    def test_live_python_silent(self, tmp_path: Path) -> None:
        """Wrapper pointing at an existing Python -> no warning."""
        script = tmp_path / "ltvm"
        script.write_text("x")
        python = tmp_path / "python"
        python.write_text("")
        link = tmp_path / "bin-ltvm"
        link.write_text(_render_ltvm_launcher(str(python), str(script)))
        with patch("ltvm_pkg.host_setup.log") as mock_log:
            _check_stale_ltvm_launcher(link)
        mock_log.warning.assert_not_called()

    def test_dead_python_warns(self, tmp_path: Path) -> None:
        script = tmp_path / "ltvm"
        script.write_text("x")
        dead = tmp_path / "does-not-exist"
        link = tmp_path / "bin-ltvm"
        link.write_text(_render_ltvm_launcher(str(dead), str(script)))
        with patch("ltvm_pkg.host_setup.log") as mock_log:
            _check_stale_ltvm_launcher(link)
        mock_log.warning.assert_called_once()
        args = mock_log.warning.call_args[0]
        assert "stale" in args[0]
        assert str(dead) in args
