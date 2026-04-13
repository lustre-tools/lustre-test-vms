"""Tests for ltvm_pkg/vm_commands.py: validation, parsing, lifecycle paths."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import vm_commands
from ltvm_pkg.vm_state import DISK_SIZE_BYTES, VMInfo

# ── _validate_vm_name ────────────────────────────────────


class TestValidateVmName:
    """VM names must be safe for /etc/hosts, ssh config, shell and cmdline."""

    @pytest.mark.parametrize(
        "name",
        ["co1-mds", "co1.single", "vm_1", "A-123.test", "x"],
    )
    def test_accepts_valid(self, name: str) -> None:
        vm_commands._validate_vm_name(name)  # must not raise

    def test_empty_rejected(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._validate_vm_name("")

    def test_too_long_rejected(self) -> None:
        """63-char DNS label limit."""
        with pytest.raises(SystemExit):
            vm_commands._validate_vm_name("a" * 64)

    def test_boundary_63_accepted(self) -> None:
        vm_commands._validate_vm_name("a" * 63)

    @pytest.mark.parametrize(
        "name",
        [
            "-leading-dash",
            "has space",
            "tab\there",
            "under/slash",
            "quote'here",
            "new\nline",
            "émoji",
            ".leading-dot",
        ],
    )
    def test_unsafe_chars_rejected(self, name: str) -> None:
        with pytest.raises(SystemExit):
            vm_commands._validate_vm_name(name)


# ── _parse_disk_size ─────────────────────────────────────


class TestParseDiskSize:
    """Disk size accepts M/G suffixes with explicit bounds."""

    def test_default_on_none(self) -> None:
        assert vm_commands._parse_disk_size(None) == DISK_SIZE_BYTES

    def test_default_on_empty(self) -> None:
        assert vm_commands._parse_disk_size("") == DISK_SIZE_BYTES

    def test_default_on_whitespace(self) -> None:
        assert vm_commands._parse_disk_size("   ") == DISK_SIZE_BYTES

    def test_parses_megabytes(self) -> None:
        assert vm_commands._parse_disk_size("500M") == 500 * (1 << 20)

    def test_parses_gigabytes(self) -> None:
        assert vm_commands._parse_disk_size("2G") == 2 * (1 << 30)

    def test_lowercase_suffix_accepted(self) -> None:
        """500m upcases to 500M before parsing."""
        assert vm_commands._parse_disk_size("500m") == 500 * (1 << 20)

    def test_int_value_returned_as_is_when_big_enough(self) -> None:
        assert vm_commands._parse_disk_size(200 * (1 << 20)) == 200 * (1 << 20)

    def test_int_below_minimum_falls_back_to_default(self) -> None:
        assert vm_commands._parse_disk_size(1024) == DISK_SIZE_BYTES

    def test_invalid_suffix_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_disk_size("500K")

    def test_no_suffix_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_disk_size("500")

    def test_non_numeric_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_disk_size("foo M")

    def test_zero_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_disk_size("0G")

    def test_below_64m_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_disk_size("32M")

    def test_above_100g_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_disk_size("200G")

    def test_boundary_64m_accepted(self) -> None:
        assert vm_commands._parse_disk_size("64M") == 64 * (1 << 20)

    def test_boundary_100g_accepted(self) -> None:
        assert vm_commands._parse_disk_size("100G") == 100 * (1 << 30)


# ── _ago formatter ───────────────────────────────────────


class TestAgoFormatter:
    """_ago renders deltas in seconds/minutes/hours/days."""

    def test_zero_returns_dash(self) -> None:
        assert vm_commands._ago(0) == "-"

    def test_recent_is_just_now(self) -> None:
        with patch("ltvm_pkg.vm_commands.time.time", return_value=1000):
            assert vm_commands._ago(990) == "just now"

    def test_minutes(self) -> None:
        with patch("ltvm_pkg.vm_commands.time.time", return_value=10_000):
            assert vm_commands._ago(10_000 - 300) == "5m ago"

    def test_hours(self) -> None:
        with patch("ltvm_pkg.vm_commands.time.time", return_value=100_000):
            assert vm_commands._ago(100_000 - 7200) == "2h ago"

    def test_days(self) -> None:
        with patch("ltvm_pkg.vm_commands.time.time", return_value=1_000_000):
            assert vm_commands._ago(1_000_000 - 2 * 86400) == "2d ago"


# ── _destroy_vm_artifacts ────────────────────────────────


@pytest.fixture
def tmp_vmdir(tmp_path: Path) -> Path:
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


def _seed_vm_files(
    tmp_vmdir: Path,
    name: str,
    *,
    mdt: int = 0,
    ost: int = 0,
) -> VMInfo:
    """Create overlay, data disks, and info file for *name*."""
    vm = VMInfo(
        name=name,
        ip="192.168.100.50",
        pid=0,
        tap=f"tap-{name}",
        mdt_disks=mdt,
        ost_disks=ost,
    )
    vm.save()
    vm.overlay_path.write_text("")
    vm.log_path.write_text("")
    vm.pid_path.write_text("")
    vm.socket_path.write_text("")
    for n in range(1, mdt + ost + 1):
        vm.disk_path(n).write_text("")
    return vm


class TestDestroyVmArtifacts:
    """_destroy_vm_artifacts removes overlay, disks, and control-plane files."""

    def test_removes_all_files(self, tmp_vmdir: Path) -> None:
        vm = _seed_vm_files(tmp_vmdir, "gone", mdt=1, ost=2)
        # Sanity: everything exists pre-destroy
        assert vm.overlay_path.exists()
        assert vm.info_path.exists()
        for n in range(1, 4):
            assert vm.disk_path(n).exists()

        vm_commands._destroy_vm_artifacts("gone")

        assert not vm.overlay_path.exists()
        assert not vm.info_path.exists()
        assert not vm.log_path.exists()
        assert not vm.pid_path.exists()
        assert not vm.socket_path.exists()
        for n in range(1, 4):
            assert not vm.disk_path(n).exists()

    def test_missing_vm_is_noop(self, tmp_vmdir: Path) -> None:
        """Destroying a non-existent VM does not raise."""
        vm_commands._destroy_vm_artifacts("never-existed")

    def test_partial_state_cleaned(self, tmp_vmdir: Path) -> None:
        """Works even if some files are missing (partial create rollback)."""
        vm = _seed_vm_files(tmp_vmdir, "half", mdt=1)
        vm.info_path.unlink()  # .info missing, others remain
        vm_commands._destroy_vm_artifacts("half")
        assert not vm.overlay_path.exists()
        assert not vm.disk_path(1).exists()

    def test_info_lock_file_also_removed(self, tmp_vmdir: Path) -> None:
        """The per-VM lock file is cleaned up alongside .info."""
        vm = _seed_vm_files(tmp_vmdir, "locked")
        # update_pid creates the lock file
        vm.update_pid(0)
        lock = vm._lock_path
        assert lock.exists()
        vm_commands._destroy_vm_artifacts("locked")
        assert not lock.exists()


# ── cmd_create validation ────────────────────────────────


def _create_args(**overrides) -> argparse.Namespace:
    defaults = {
        "name": "co1-single",
        "vcpus": 2,
        "mem": 2048,
        "mdt_disks": 0,
        "ost_disks": 0,
        "disk_size": None,
        "image": "",
        "kernel": "",
        "os": "",
        "arch": None,
        "ip": None,
        "json": False,
        "_quiet": True,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestCmdCreateValidation:
    """cmd_create short-circuits on bad arguments before touching the filesystem."""

    def test_existing_running_vm_noop(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """create on a running VM re-registers SSH and prints 'already running'."""
        _seed_vm_files(tmp_vmdir, "running")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.wait_for_ssh"),
            patch("ltvm_pkg.vm_commands.register_ssh_name"),
        ):
            vm_commands.cmd_create(_create_args(name="running"))
        out = capsys.readouterr().out
        assert "already running" in out

    def test_existing_stopped_vm_starts(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """create on a stopped VM launches QEMU and prints 'started'."""
        _seed_vm_files(tmp_vmdir, "stopped-c")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=False),
            patch("ltvm_pkg.vm_commands.launch_qemu"),
            patch("ltvm_pkg.vm_commands.wait_for_ssh"),
            patch("ltvm_pkg.vm_commands.register_ssh_name"),
            patch("ltvm_pkg.vm_commands.deploy_ssh_key"),
            patch("ltvm_pkg.vm_commands._seed_kdump_boot"),
        ):
            vm_commands.cmd_create(_create_args(name="stopped-c"))
        out = capsys.readouterr().out
        assert "started" in out

    def test_nonpositive_vcpus_dies(self, tmp_vmdir: Path) -> None:
        with pytest.raises(SystemExit):
            vm_commands.cmd_create(_create_args(name="bad-vcpus", vcpus=0))

    def test_nonpositive_mem_dies(self, tmp_vmdir: Path) -> None:
        with pytest.raises(SystemExit):
            vm_commands.cmd_create(_create_args(name="bad-mem", mem=-1))

    def test_too_many_data_disks_dies(self, tmp_vmdir: Path) -> None:
        """vda is rootfs; max 25 data disks (vdb..vdz)."""
        with pytest.raises(SystemExit):
            vm_commands.cmd_create(
                _create_args(name="too-many", mdt_disks=20, ost_disks=10)
            )

    def test_invalid_name_dies(self, tmp_vmdir: Path) -> None:
        with pytest.raises(SystemExit):
            vm_commands.cmd_create(_create_args(name="bad name"))


# ── cmd_destroy ──────────────────────────────────────────


class TestCmdDestroy:
    """cmd_destroy stops QEMU, removes artifacts, and unregisters DNS."""

    def test_destroy_missing_is_soft_error(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Destroying a missing VM prints 'not found' instead of lying.

        Used to print 'destroyed ghost' even when the VM never existed,
        which made typos like 'ltvm destroy co1-signle' look like a
        successful destroy.
        """
        args = argparse.Namespace(names=["ghost"])
        with (
            patch("ltvm_pkg.vm_commands.kill_qemu") as mock_kill,
            patch("ltvm_pkg.vm_commands.unregister_ssh_name"),
        ):
            vm_commands.cmd_destroy(args)
        mock_kill.assert_not_called()
        assert "destroy: ghost not found" in capsys.readouterr().out

    def test_destroy_live_vm_kills_and_unregisters(
        self,
        tmp_vmdir: Path,
    ) -> None:
        vm = _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(names=["live"])
        with (
            patch("ltvm_pkg.vm_commands.kill_qemu") as mock_kill,
            patch("ltvm_pkg.vm_commands.unregister_ssh_name") as mock_unreg,
        ):
            vm_commands.cmd_destroy(args)
        mock_kill.assert_called_once()
        mock_unreg.assert_called_once_with("live")
        assert not vm.info_path.exists()
        assert not vm.overlay_path.exists()


# ── cmd_stop ─────────────────────────────────────────────


class TestCmdStop:
    """cmd_stop is lenient about missing VMs."""

    def test_missing_vm_not_an_error(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        args = argparse.Namespace(names=["ghost"])
        with patch("ltvm_pkg.vm_commands.kill_qemu") as mock_kill:
            vm_commands.cmd_stop(args)
        mock_kill.assert_not_called()
        assert "not found" in capsys.readouterr().out

    def test_live_vm_is_killed(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(names=["live"])
        with patch("ltvm_pkg.vm_commands.kill_qemu") as mock_kill:
            vm_commands.cmd_stop(args)
        mock_kill.assert_called_once()


# ── cmd_list totals ──────────────────────────────────────


class TestCmdList:
    """cmd_list aggregates vcpu/memory totals and handles JSON output."""

    def test_empty(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        args = argparse.Namespace(json=False)
        vm_commands.cmd_list(args)
        assert "(no VMs)" in capsys.readouterr().out

    def test_json_totals(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        v1 = VMInfo(
            name="a",
            ip="10.0.0.1",
            pid=100,
            vcpus=4,
            mem=2048,
            mdt_disks=1,
            ost_disks=0,
        )
        v2 = VMInfo(
            name="b",
            ip="10.0.0.2",
            pid=0,
            vcpus=2,
            mem=1024,
        )
        v1.save()
        v2.save()
        args = argparse.Namespace(json=True)
        # a is "running" (pid 100 live), b is stopped (pid 0)
        with patch(
            "ltvm_pkg.vm_commands.is_running",
            side_effect=lambda vm: vm.pid > 0,
        ):
            vm_commands.cmd_list(args)
        out = json.loads(capsys.readouterr().out)
        assert len(out["vms"]) == 2
        assert out["totals"]["running"] == 1
        assert out["totals"]["stopped"] == 1
        # Only the running VM contributes to vcpu/mem totals
        assert out["totals"]["vcpus_used"] == 4
        assert out["totals"]["mem_used_mb"] == 2048


# ── cmd_console_log ──────────────────────────────────────


class TestCmdConsoleLog:
    """console_log prints the tail of the serial-log file."""

    def test_tails_last_n_lines(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        vm = _seed_vm_files(tmp_vmdir, "logged")
        vm.log_path.write_text("\n".join(f"line{i}" for i in range(20)) + "\n")
        args = argparse.Namespace(name="logged", lines=5)
        vm_commands.cmd_console_log(args)
        out = capsys.readouterr().out.splitlines()
        assert out == [f"line{i}" for i in range(15, 20)]

    def test_missing_log_dies(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "nolog")
        # Remove the log file
        (tmp_vmdir / "sockets" / "nolog.log").unlink()
        args = argparse.Namespace(name="nolog", lines=10)
        with pytest.raises(SystemExit):
            vm_commands.cmd_console_log(args)


# ── cmd_llmount ──────────────────────────────────────────


LIBDIR = "/usr/lib64/lustre"
_MOUNT_CMD = f"dmsetup remove_all; cd {LIBDIR}/tests && LUSTRE={LIBDIR} bash llmount.sh"
_CLEANUP_CMD = (
    f"cd {LIBDIR}/tests && LUSTRE={LIBDIR} bash llmountcleanup.sh && lustre_rmmod"
)


class TestCmdLlmount:
    """cmd_llmount runs the correct remote commands and forwards exit codes."""

    def test_missing_vm_dies(self, tmp_vmdir: Path) -> None:
        args = argparse.Namespace(name="ghost", timeout=300, cleanup=False)
        with pytest.raises(SystemExit) as exc:
            vm_commands.cmd_llmount(args)
        assert exc.value.code == 2  # EXIT_NOT_FOUND

    def test_not_running_dies(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "stopped")
        args = argparse.Namespace(name="stopped", timeout=300, cleanup=False)
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=False),
            pytest.raises(SystemExit) as exc,
        ):
            vm_commands.cmd_llmount(args)
        assert exc.value.code == 4  # EXIT_UNREACHABLE

    def test_default_mount_command(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(name="live", timeout=300, cleanup=False)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.configure_test_disks"),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r) as mock_ssh,
            pytest.raises(SystemExit) as exc,
        ):
            vm_commands.cmd_llmount(args)
        assert exc.value.code == 0
        mock_ssh.assert_called_once()
        cmd_sent = mock_ssh.call_args[0][1]
        assert cmd_sent == _MOUNT_CMD

    def test_configure_test_disks_called_with_vm_topology(
        self, tmp_vmdir: Path
    ) -> None:
        _seed_vm_files(tmp_vmdir, "live", mdt=1, ost=3)
        args = argparse.Namespace(name="live", timeout=300, cleanup=False)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.configure_test_disks"
            ) as mock_configure,
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r),
            pytest.raises(SystemExit),
        ):
            vm_commands.cmd_llmount(args)
        mock_configure.assert_called_once()
        args_passed, kwargs_passed = mock_configure.call_args
        assert args_passed[1] == 1  # mdt_disks
        assert args_passed[2] == 3  # ost_disks

    def test_configure_test_disks_skipped_on_cleanup(
        self, tmp_vmdir: Path
    ) -> None:
        _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(name="live", timeout=300, cleanup=True)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.configure_test_disks"
            ) as mock_configure,
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r),
            pytest.raises(SystemExit),
        ):
            vm_commands.cmd_llmount(args)
        mock_configure.assert_not_called()

    def test_cleanup_command(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(name="live", timeout=300, cleanup=True)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r) as mock_ssh,
            pytest.raises(SystemExit) as exc,
        ):
            vm_commands.cmd_llmount(args)
        assert exc.value.code == 0
        cmd_sent = mock_ssh.call_args[0][1]
        assert cmd_sent == _CLEANUP_CMD

    def test_nonzero_exit_forwarded(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(name="live", timeout=300, cleanup=False)
        r = MagicMock()
        r.returncode = 1
        r.stdout = "mkfs.lustre FATAL: Unable to build fs\n"
        r.stderr = ""
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r),
            pytest.raises(SystemExit) as exc,
        ):
            vm_commands.cmd_llmount(args)
        assert exc.value.code == 1

    def test_timeout_plumbed_through(self, tmp_vmdir: Path) -> None:
        import subprocess as _sp

        _seed_vm_files(tmp_vmdir, "slow")
        args = argparse.Namespace(name="slow", timeout=42, cleanup=False)
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.configure_test_disks"),
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=_sp.TimeoutExpired(cmd="ssh", timeout=42),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            vm_commands.cmd_llmount(args)
        assert exc.value.code == 3  # EXIT_TIMEOUT

    def test_timeout_passed_to_run_ssh(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(name="live", timeout=99, cleanup=False)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.configure_test_disks"),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r) as mock_ssh,
            pytest.raises(SystemExit),
        ):
            vm_commands.cmd_llmount(args)
        _, call_kwargs = mock_ssh.call_args
        assert call_kwargs.get("timeout") == 99


# ── _seed_kdump_boot ─────────────────────────────────────


class TestSeedKdumpBoot:
    """Fast path: image has baked-in kdump artifacts, only reload kdump.
    Fallback path: artifacts missing, scp + dracut/update-initramfs."""

    def _vm(self, tmp_path: Path, os_id: str = "rocky9") -> VMInfo:
        kdir = tmp_path / "kernels" / "5.14"
        kdir.mkdir(parents=True)
        (kdir / "vmlinux").write_bytes(b"ELF")
        (kdir / "vmlinuz").write_bytes(b"bz")
        return VMInfo(
            name="co1-test",
            ip="10.0.0.99",
            kernel=str(kdir / "vmlinux"),
            kver="5.14.0-test",
            os_id=os_id,
        )

    def _ssh_results(self, present: bool):
        """Return a side_effect fn where only the `test -f` probe
        varies with *present*; every other command succeeds."""
        def _fn(ip, cmd, **kw):
            r = MagicMock()
            if cmd.startswith("test -f"):
                r.returncode = 0 if present else 1
            else:
                r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r
        return _fn

    def test_fast_path_no_scp_no_dracut(self, tmp_path: Path) -> None:
        vm = self._vm(tmp_path)
        with (
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=self._ssh_results(present=True),
            ) as mock_ssh,
            patch("ltvm_pkg.vm_commands.run") as mock_run,
            patch(
                "ltvm_pkg.target_config.TargetConfig"
            ) as mock_tc,
        ):
            mock_tc.return_value.os_family = "rhel"
            vm_commands._seed_kdump_boot(vm)

        mock_run.assert_not_called()
        cmds = [c.args[1] for c in mock_ssh.call_args_list]
        assert any("test -f /boot/vmlinuz-" in c for c in cmds)
        assert any("systemctl restart kdump" in c for c in cmds)
        assert not any("dracut" in c for c in cmds)

    def test_fast_path_debian_uses_kdump_config(self, tmp_path: Path) -> None:
        vm = self._vm(tmp_path, os_id="ubuntu24")
        with (
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=self._ssh_results(present=True),
            ) as mock_ssh,
            patch("ltvm_pkg.vm_commands.run") as mock_run,
            patch(
                "ltvm_pkg.target_config.TargetConfig"
            ) as mock_tc,
        ):
            mock_tc.return_value.os_family = "debian"
            vm_commands._seed_kdump_boot(vm)

        mock_run.assert_not_called()
        cmds = [c.args[1] for c in mock_ssh.call_args_list]
        assert any(
            "test -f /var/lib/kdump/initrd.img-" in c for c in cmds
        )
        assert any("kdump-config load" in c for c in cmds)
        assert not any("update-initramfs" in c for c in cmds)

    def test_fallback_path_runs_scp_and_dracut(self, tmp_path: Path) -> None:
        vm = self._vm(tmp_path)
        scp_rc = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=self._ssh_results(present=False),
            ) as mock_ssh,
            patch(
                "ltvm_pkg.vm_commands.run", return_value=scp_rc
            ) as mock_run,
            patch(
                "ltvm_pkg.target_config.TargetConfig"
            ) as mock_tc,
        ):
            mock_tc.return_value.os_family = "rhel"
            vm_commands._seed_kdump_boot(vm)

        mock_run.assert_called()
        scp_cmd = mock_run.call_args_list[0].args[0]
        assert "scp" in scp_cmd
        cmds = [c.args[1] for c in mock_ssh.call_args_list]
        assert any("dracut --kver 5.14.0-test" in c for c in cmds)

    def test_no_kernel_returns_early(self, tmp_path: Path) -> None:
        vm = VMInfo(name="x", ip="10.0.0.1")  # kernel=""
        with (
            patch("ltvm_pkg.vm_commands.run_ssh") as mock_ssh,
            patch("ltvm_pkg.vm_commands.run") as mock_run,
        ):
            vm_commands._seed_kdump_boot(vm)
        mock_ssh.assert_not_called()
        mock_run.assert_not_called()
