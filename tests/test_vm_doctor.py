"""Tests for ltvm_pkg/vm_commands.py::cmd_doctor and its helpers.

cmd_doctor audits the host for VM state that has diverged from the
filesystem: stale PIDs, orphan overlays / disks / sockets, stale
/etc/hosts and ssh-config entries, dead TAPs, cluster files with
destroyed nodes, and missing host tools needed by `ltvm target export`.
With --fix it attempts to repair each (except partial-cluster
degradation, which is ambiguous).

These tests pin each orphan-detection + fix path so a future refactor
can't accidentally drop a case or mis-cite which --fix action was run.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import vm_commands
from ltvm_pkg.vm_state import MARKER, VMInfo


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
        patch("ltvm_pkg.vm_commands.SOCKETS", sockets),
        patch("ltvm_pkg.vm_commands.OVERLAYS", overlays),
    ):
        yield tmp_path


@pytest.fixture
def doctor_env(
    tmp_vmdir: Path, tmp_path: Path
) -> Iterator[dict]:
    """Isolate cmd_doctor from host /etc/hosts, ssh config, and
    `ip link`, and from every other checker it would trip on."""
    fake_hosts = tmp_path / "hosts"
    fake_hosts.write_text("")
    fake_ssh_dir = tmp_path / ".ssh"
    fake_ssh_dir.mkdir()
    (fake_ssh_dir / "config").write_text("")

    # `ip link` returns nothing (no tap devices)
    ip_out = MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("ltvm_pkg.vm_commands.Path", side_effect=lambda p: Path(p)),
        patch(
            "ltvm_pkg.vm_commands._real_user_ssh_dir",
            return_value=("root", fake_ssh_dir),
        ),
        patch(
            "ltvm_pkg.vm_commands.run", return_value=ip_out
        ) as mock_run,
        patch(
            "ltvm_pkg.vm_commands._check_export_tools", return_value=[]
        ),
        patch(
            "ltvm_pkg.vm_commands.is_running", return_value=False
        ),
    ):
        # Re-patch Path so the hosts file read goes to our fake.
        # cmd_doctor uses `Path("/etc/hosts")` literally, so patch that
        # specific string via side_effect lookup.
        real_path = Path

        def path_router(p: str | Path) -> Path:
            if str(p) == "/etc/hosts":
                return fake_hosts
            return real_path(p)

        with patch("ltvm_pkg.vm_commands.Path", side_effect=path_router):
            yield {
                "hosts": fake_hosts,
                "ssh_dir": fake_ssh_dir,
                "run": mock_run,
            }


def _make_args(fix: bool = False) -> argparse.Namespace:
    return argparse.Namespace(fix=fix, json=False)


# ── clean ─────────────────────────────────────────────────


class TestDoctorClean:
    """No orphans, no issues -> 'no issues found' + EXIT_OK."""

    def test_clean_host_returns_ok(
        self,
        tmp_vmdir: Path,
        doctor_env: dict,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = vm_commands.cmd_doctor(_make_args(fix=False))
        assert rc == 0
        assert "no issues found" in capsys.readouterr().out


# ── stale PID ─────────────────────────────────────────────


class TestDoctorStalePid:
    """A VM with pid != 0 whose process is dead is 'stale PID'."""

    def test_reports_stale_pid(
        self,
        tmp_vmdir: Path,
        doctor_env: dict,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        v = VMInfo(name="ghost", ip="10.0.0.1", pid=99999)
        v.save()
        rc = vm_commands.cmd_doctor(_make_args(fix=False))
        out = capsys.readouterr().out
        assert "stale PID: ghost" in out
        assert rc != 0  # issues present -> non-zero exit without --fix

    def test_fix_resets_pid(
        self, tmp_vmdir: Path, doctor_env: dict
    ) -> None:
        v = VMInfo(name="pid-reset", ip="10.0.0.1", pid=99999)
        v.save()
        rc = vm_commands.cmd_doctor(_make_args(fix=True))
        # --fix reports issues fixed with rc=0
        assert rc == 0
        reloaded = VMInfo.load("pid-reset")
        assert reloaded.pid == 0


# ── orphan overlay / disk ─────────────────────────────────


class TestDoctorOrphanOverlay:
    """Overlay qcow2 without matching .info -> orphan."""

    def test_reports_orphan_overlay(
        self,
        tmp_vmdir: Path,
        doctor_env: dict,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        overlay = tmp_vmdir / "overlays" / "orphan.qcow2"
        overlay.write_bytes(b"\x00" * 1024)
        rc = vm_commands.cmd_doctor(_make_args(fix=False))
        out = capsys.readouterr().out
        assert "orphan overlay: orphan" in out
        assert rc != 0
        assert overlay.exists()  # no --fix -> still there

    def test_fix_removes_orphan_overlay_and_disks(
        self, tmp_vmdir: Path, doctor_env: dict
    ) -> None:
        overlay = tmp_vmdir / "overlays" / "orph.qcow2"
        overlay.write_bytes(b"\x00")
        disk = tmp_vmdir / "overlays" / "orph-disk1.img"
        disk.write_bytes(b"\x00")
        vm_commands.cmd_doctor(_make_args(fix=True))
        assert not overlay.exists()
        assert not disk.exists()


class TestDoctorOrphanDisk:
    """Data disk whose overlay has been removed (mid-create crash)."""

    def test_orphan_disk_without_overlay_or_info(
        self,
        tmp_vmdir: Path,
        doctor_env: dict,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        disk = tmp_vmdir / "overlays" / "lone-disk1.img"
        disk.write_bytes(b"\x00")
        rc = vm_commands.cmd_doctor(_make_args(fix=False))
        assert "orphan disk: lone-disk1.img" in capsys.readouterr().out
        assert rc != 0
        assert disk.exists()

    def test_fix_removes_orphan_disk(
        self, tmp_vmdir: Path, doctor_env: dict
    ) -> None:
        disk = tmp_vmdir / "overlays" / "del-disk1.img"
        disk.write_bytes(b"\x00")
        vm_commands.cmd_doctor(_make_args(fix=True))
        assert not disk.exists()


# ── orphan socket-side files ──────────────────────────────


class TestDoctorOrphanSocketFiles:
    """PID/LOG/QMP/info.lock files with no matching .info are orphans."""

    @pytest.mark.parametrize("ext", ["pid", "log", "qmp"])
    def test_reports_orphan_file(
        self,
        tmp_vmdir: Path,
        doctor_env: dict,
        capsys: pytest.CaptureFixture[str],
        ext: str,
    ) -> None:
        f = tmp_vmdir / "sockets" / f"deadvm.{ext}"
        f.write_text("")
        rc = vm_commands.cmd_doctor(_make_args(fix=False))
        out = capsys.readouterr().out
        assert f"orphan {ext}: deadvm.{ext}" in out
        assert rc != 0

    def test_orphan_info_lock_reported(
        self,
        tmp_vmdir: Path,
        doctor_env: dict,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        f = tmp_vmdir / "sockets" / ".zombie.info.lock"
        f.write_text("")
        rc = vm_commands.cmd_doctor(_make_args(fix=False))
        out = capsys.readouterr().out
        assert "orphan info lock" in out
        assert rc != 0

    def test_fix_removes_all(
        self, tmp_vmdir: Path, doctor_env: dict
    ) -> None:
        files = [
            tmp_vmdir / "sockets" / "v.pid",
            tmp_vmdir / "sockets" / "v.log",
            tmp_vmdir / "sockets" / "v.qmp",
            tmp_vmdir / "sockets" / ".v.info.lock",
        ]
        for f in files:
            f.write_text("")
        vm_commands.cmd_doctor(_make_args(fix=True))
        for f in files:
            assert not f.exists()


# ── stale /etc/hosts ──────────────────────────────────────


class TestDoctorStaleHosts:
    """/etc/hosts entries with the ltvm MARKER whose VM is gone."""

    def test_reports_stale_entry(
        self,
        tmp_vmdir: Path,
        doctor_env: dict,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        doctor_env["hosts"].write_text(
            f"10.0.0.99 dead-vm {MARKER}:dead-vm\n"
        )
        rc = vm_commands.cmd_doctor(_make_args(fix=False))
        out = capsys.readouterr().out
        assert "stale hosts entry: dead-vm" in out
        assert rc != 0

    def test_fix_calls_unregister(
        self, tmp_vmdir: Path, doctor_env: dict
    ) -> None:
        doctor_env["hosts"].write_text(
            f"10.0.0.99 dead2 {MARKER}:dead2\n"
        )
        with patch(
            "ltvm_pkg.vm_commands.unregister_ssh_name"
        ) as mock_unreg:
            vm_commands.cmd_doctor(_make_args(fix=True))
        mock_unreg.assert_called_with("dead2")


# ── stale ssh config ──────────────────────────────────────


class TestDoctorStaleSshConfig:
    def test_reports_stale_ssh_entry(
        self,
        tmp_vmdir: Path,
        doctor_env: dict,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (doctor_env["ssh_dir"] / "config").write_text(
            f"Host dead-ssh\n  HostName 10.0.0.99  {MARKER}:dead-ssh\n"
        )
        rc = vm_commands.cmd_doctor(_make_args(fix=False))
        out = capsys.readouterr().out
        assert "stale ssh config: dead-ssh" in out
        assert rc != 0


# ── orphan TAPs ───────────────────────────────────────────


class TestDoctorOrphanTaps:
    """tap-* interfaces that don't belong to any running VM are orphans."""

    def test_reports_orphan_tap(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        # Mock everything as `doctor_env` did but with a non-empty `ip -o link`.
        fake_hosts = tmp_path / "hosts"
        fake_hosts.write_text("")
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "config").write_text("")

        ip_out = MagicMock(
            returncode=0,
            stdout="3: tap-orphaned@if22: <BROADCAST> ...\n",
            stderr="",
        )

        real_path = Path

        def path_router(p: str | Path) -> Path:
            if str(p) == "/etc/hosts":
                return fake_hosts
            return real_path(p)

        with (
            patch("ltvm_pkg.vm_commands.Path", side_effect=path_router),
            patch(
                "ltvm_pkg.vm_commands._real_user_ssh_dir",
                return_value=("root", ssh_dir),
            ),
            patch(
                "ltvm_pkg.vm_commands.run", return_value=ip_out
            ) as mock_run,
            patch(
                "ltvm_pkg.vm_commands._check_export_tools",
                return_value=[],
            ),
            patch(
                "ltvm_pkg.vm_commands.is_running", return_value=False
            ),
        ):
            rc = vm_commands.cmd_doctor(_make_args(fix=False))
        out = capsys.readouterr().out
        assert "orphan TAP: tap-orphaned" in out
        assert rc != 0
        # --fix would `ip link del` (not tested here: we use fix=False).
        # All run calls here are `ip -o link show` probes.
        assert mock_run.called


# ── missing export tools ──────────────────────────────────


class TestCheckExportTools:
    """Direct test of the _check_export_tools helper so its decisions
    are pinned independently of the surrounding doctor plumbing."""

    def test_all_present_no_warnings(self) -> None:
        with patch(
            "shutil.which",
            side_effect=lambda tool: f"/usr/bin/{tool}",
        ):
            assert vm_commands._check_export_tools() == []

    def test_parted_missing(self) -> None:
        def which(t: str) -> str | None:
            return None if t == "parted" else f"/usr/bin/{t}"

        with patch("shutil.which", side_effect=which):
            warnings = vm_commands._check_export_tools()
        assert any("parted" in w for w in warnings)

    def test_qemu_img_missing(self) -> None:
        def which(t: str) -> str | None:
            return None if t == "qemu-img" else f"/usr/bin/{t}"

        with patch("shutil.which", side_effect=which):
            warnings = vm_commands._check_export_tools()
        assert any("qemu-img" in w for w in warnings)

    def test_either_grub_flavour_accepted(self) -> None:
        """grub2-install XOR grub-install is fine."""

        def which(t: str) -> str | None:
            if t == "grub-install":
                return "/usr/sbin/grub-install"
            if t == "grub2-install":
                return None
            return f"/usr/bin/{t}"

        with patch("shutil.which", side_effect=which):
            warnings = vm_commands._check_export_tools()
        assert not any("grub" in w for w in warnings)

    def test_neither_grub_flavour_warns(self) -> None:
        def which(t: str) -> str | None:
            if t in ("grub-install", "grub2-install"):
                return None
            return f"/usr/bin/{t}"

        with patch("shutil.which", side_effect=which):
            warnings = vm_commands._check_export_tools()
        assert any("grub" in w for w in warnings)


# ── non-interactive --fix flow ───────────────────────────


class TestDoctorInteractivePrompt:
    """Non-tty callers get the 'run with --fix' hint; tty callers get
    the interactive prompt that can recurse with args.fix=True."""

    def test_no_tty_prints_fix_hint(
        self,
        tmp_vmdir: Path,
        doctor_env: dict,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Seed an orphan overlay so issues > 0.
        (tmp_vmdir / "overlays" / "pending.qcow2").write_bytes(b"\x00")
        with (
            patch("sys.stdin.isatty", return_value=False),
            patch("sys.stdout.isatty", return_value=False),
        ):
            rc = vm_commands.cmd_doctor(_make_args(fix=False))
        out = capsys.readouterr().out
        assert "run with --fix" in out
        assert rc != 0

    def test_tty_yes_runs_fix(
        self, tmp_vmdir: Path, doctor_env: dict
    ) -> None:
        (tmp_vmdir / "overlays" / "promptfix.qcow2").write_bytes(b"\x00")
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("builtins.input", return_value="y"),
        ):
            rc = vm_commands.cmd_doctor(_make_args(fix=False))
        # After recursion with args.fix=True, rc is 0 (fixed)
        assert rc == 0
        assert not (tmp_vmdir / "overlays" / "promptfix.qcow2").exists()

    def test_tty_no_returns_error(
        self, tmp_vmdir: Path, doctor_env: dict
    ) -> None:
        (tmp_vmdir / "overlays" / "promptkeep.qcow2").write_bytes(b"\x00")
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("builtins.input", return_value="n"),
        ):
            rc = vm_commands.cmd_doctor(_make_args(fix=False))
        assert rc != 0
        # User said no -> file stays
        assert (tmp_vmdir / "overlays" / "promptkeep.qcow2").exists()
