"""Tests for ltvm_pkg/vm_commands.py::cmd_crash_collect.

The crash-collect flow is:

  1. If --trigger: echo c > /proc/sysrq-trigger, wait for kdump+reboot.
  2. Locate vmcore via `find /var/crash ...`.
  3. scp vmcore to local dir under --outdir (default ~/ltvm-crashes).
  4. Resolve vmlinux (prefer fresh build artifacts, then next-to kernel).
  5. If --mod-dir: run lustre_triage.py under the right user.

Each branch has an error/timeout sub-path that returns a specific
EXIT_* code so callers can distinguish 'VM dead' from 'scp failed'.
These tests pin the exit codes + user-visible messages so the split
into phase helpers can't silently regress.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import vm_commands
from ltvm_pkg.vm_state import (
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_TIMEOUT,
    VMInfo,
)


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


def _seed_vm(tmp_vmdir: Path, name: str, **kw) -> VMInfo:
    """Save a VMInfo with enough fields for cmd_crash_collect."""
    defaults = dict(
        ip="10.0.0.50",
        os_id="rocky9",
        arch="x86_64",
        kernel=str(tmp_vmdir / "kernels" / "5.14" / "vmlinuz"),
        kver="5.14.0-test",
    )
    defaults.update(kw)
    vm = VMInfo(name=name, **defaults)
    vm.save()
    return vm


def _args(name: str, **overrides) -> argparse.Namespace:
    defaults = dict(
        name=name,
        outdir=None,
        trigger=False,
        wait=5,
        mod_dir=None,
        json=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ── VM-not-running guard ──────────────────────────────────


class TestCrashNotRunning:
    def test_not_running_without_trigger_errors(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        _seed_vm(tmp_vmdir, "dead-vm")
        args = _args("dead-vm", outdir=str(tmp_path / "out"))
        with patch("ltvm_pkg.vm_commands.is_running", return_value=False):
            rc = vm_commands.cmd_crash_collect(args)
        assert rc == EXIT_ERROR
        assert "not running" in capsys.readouterr().err

    def test_trigger_on_stopped_vm_errors(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        _seed_vm(tmp_vmdir, "stopped-trig")
        args = _args(
            "stopped-trig", outdir=str(tmp_path / "out"), trigger=True
        )
        with patch("ltvm_pkg.vm_commands.is_running", return_value=False):
            rc = vm_commands.cmd_crash_collect(args)
        assert rc != 0
        assert "can't trigger crash" in capsys.readouterr().err


# ── vmcore resolution ────────────────────────────────────


class TestCrashVmcoreResolution:
    """The `find /var/crash ...` probe maps to dump path + copy."""

    def _build_mocks(
        self, tmp_path: Path, vmcore_path: str, vmlinux_present: bool = True
    ) -> tuple[Path, Path | None, dict]:
        """Create kernel dir (with vmlinux optional) and return mocks."""
        kdir = tmp_path / "kernels" / "5.14"
        kdir.mkdir(parents=True)
        (kdir / "vmlinuz").write_bytes(b"bz")
        if vmlinux_present:
            (kdir / "vmlinux").write_bytes(b"ELF")
            vmlinux = kdir / "vmlinux"
        else:
            vmlinux = None

        # ssh_rc: depends on which command is being sent.
        find_result = MagicMock(returncode=0, stdout=vmcore_path, stderr="")
        ls_result = MagicMock(
            returncode=0, stdout=f"-rw------- 1 root root 1G Jan 1 01:00 {vmcore_path}",
            stderr="",
        )

        def _ssh(ip, cmd, **kw):
            if "find /var/crash" in cmd:
                return find_result
            if cmd.startswith("ls -lh"):
                return ls_result
            return MagicMock(returncode=0, stdout="", stderr="")

        scp_result = MagicMock(returncode=0, stdout="", stderr="")

        return kdir, vmlinux, {
            "ssh": _ssh,
            "scp": scp_result,
        }

    def test_no_vmcore_returns_error(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        vm = _seed_vm(tmp_vmdir, "no-core")
        _build = self._build_mocks(tmp_path, vmcore_path="")
        # Inject empty stdout so find returns nothing.
        find_empty = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch(
                "ltvm_pkg.vm_commands.is_running", return_value=True
            ),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=find_empty),
        ):
            rc = vm_commands.cmd_crash_collect(
                _args("no-core", outdir=str(tmp_path / "out"))
            )
        assert rc != 0
        assert "no vmcore found" in capsys.readouterr().err

    def test_find_ssh_fails_surfaces_real_error(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        _seed_vm(tmp_vmdir, "ssh-err")
        ssh_fail = MagicMock(
            returncode=255, stdout="", stderr="Connection closed"
        )
        with (
            patch(
                "ltvm_pkg.vm_commands.is_running", return_value=True
            ),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=ssh_fail),
        ):
            rc = vm_commands.cmd_crash_collect(
                _args("ssh-err", outdir=str(tmp_path / "out"))
            )
        assert rc != 0
        err = capsys.readouterr().err
        assert "failed to probe /var/crash" in err
        # The real SSH error must surface (not be replaced by "no vmcore found")
        assert "Connection closed" in err

    def test_find_ssh_times_out(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        _seed_vm(tmp_vmdir, "ssh-timeout")
        with (
            patch(
                "ltvm_pkg.vm_commands.is_running", return_value=True
            ),
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=subprocess.TimeoutExpired("ssh", 10),
            ),
        ):
            rc = vm_commands.cmd_crash_collect(
                _args("ssh-timeout", outdir=str(tmp_path / "out"))
            )
        assert rc == EXIT_TIMEOUT
        assert "timed out" in capsys.readouterr().err


# ── scp + vmlinux + triage ────────────────────────────────


class TestCrashFullFlow:
    """The happy path through vmcore copy + vmlinux resolve + output."""

    def _setup(self, tmp_path: Path) -> tuple[Path, Path]:
        kdir = tmp_path / "kernels" / "5.14"
        kdir.mkdir(parents=True)
        (kdir / "vmlinuz").write_bytes(b"bz")
        (kdir / "vmlinux").write_bytes(b"ELF")
        return kdir, kdir / "vmlinux"

    def _ssh_side_effect(self, vmcore_path: str):
        def _ssh(ip, cmd, **kw):
            if "find /var/crash" in cmd:
                return MagicMock(
                    returncode=0, stdout=vmcore_path, stderr=""
                )
            if cmd.startswith("ls -lh"):
                return MagicMock(
                    returncode=0,
                    stdout=f"-rw------- 1 root root 1G {vmcore_path}",
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        return _ssh

    def _fake_scp_run(self, rc: int = 0):
        """Return a run() replacement that simulates scp by writing the
        destination file when rc=0.  Triage calls are just rc-only."""

        def _run(cmd, **kw):
            if cmd and "scp" in cmd:
                # scp args: [..., 'src', 'dst']; the last positional is
                # the local path.
                if rc == 0:
                    dst = Path(cmd[-1])
                    dst.write_bytes(b"\x7fELF mock vmcore\n")
                return MagicMock(returncode=rc, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        return _run

    def test_happy_path_no_mod_dir_hint(
        self,
        tmp_vmdir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        kdir, vmlinux = self._setup(tmp_path)
        vm = _seed_vm(
            tmp_vmdir, "crash-ok", kernel=str(kdir / "vmlinuz")
        )
        outdir = tmp_path / "out"
        args = _args("crash-ok", outdir=str(outdir), mod_dir=None)

        # resolve_os_artifacts returns a MagicMock with kernel pointing
        # at our kdir so the vmlinux next-to-kernel resolution succeeds.
        arts = MagicMock()
        arts.kernel = kdir / "vmlinuz"

        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=self._ssh_side_effect(
                    "/var/crash/127.0.0.1-2024/vmcore"
                ),
            ),
            patch(
                "ltvm_pkg.vm_commands.resolve_os_artifacts",
                return_value=arts,
            ),
            patch(
                "ltvm_pkg.vm_commands.run",
                side_effect=self._fake_scp_run(),
            ) as mock_run,
        ):
            rc = vm_commands.cmd_crash_collect(args)
        assert rc == EXIT_OK
        # scp was invoked once (the vmcore copy)
        assert mock_run.called
        # outdir contains crash-<name>-<ts>/vmcore
        created = list(outdir.glob("crash-crash-ok-*/vmcore"))
        assert len(created) == 1
        # vmlinux hint printed in non-mod-dir mode
        out = capsys.readouterr().out
        assert "crash-tool recipes lustre" in out
        assert "vmlinux" in out

    def test_scp_failure_returns_error(
        self,
        tmp_vmdir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        kdir, _ = self._setup(tmp_path)
        _seed_vm(tmp_vmdir, "scp-fail", kernel=str(kdir / "vmlinuz"))
        outdir = tmp_path / "out"
        args = _args("scp-fail", outdir=str(outdir))

        arts = MagicMock()
        arts.kernel = kdir / "vmlinuz"
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=self._ssh_side_effect(
                    "/var/crash/127.0.0.1-2024/vmcore"
                ),
            ),
            patch(
                "ltvm_pkg.vm_commands.resolve_os_artifacts",
                return_value=arts,
            ),
            patch(
                "ltvm_pkg.vm_commands.run",
                side_effect=self._fake_scp_run(rc=1),
            ),
        ):
            rc = vm_commands.cmd_crash_collect(args)
        assert rc != 0
        assert "failed to copy vmcore" in capsys.readouterr().err

    def test_scp_timeout_cleaned_up(
        self,
        tmp_vmdir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        kdir, _ = self._setup(tmp_path)
        _seed_vm(
            tmp_vmdir, "scp-timeout", kernel=str(kdir / "vmlinuz")
        )
        outdir = tmp_path / "out"
        args = _args("scp-timeout", outdir=str(outdir))

        arts = MagicMock()
        arts.kernel = kdir / "vmlinuz"

        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=self._ssh_side_effect(
                    "/var/crash/127.0.0.1-2024/vmcore"
                ),
            ),
            patch(
                "ltvm_pkg.vm_commands.resolve_os_artifacts",
                return_value=arts,
            ),
            patch(
                "ltvm_pkg.vm_commands.run",
                side_effect=subprocess.TimeoutExpired("scp", 300),
            ),
        ):
            rc = vm_commands.cmd_crash_collect(args)
        assert rc == EXIT_TIMEOUT
        # The partial vmcore (if any) must be unlinked
        partials = list(outdir.glob("crash-*/vmcore"))
        for p in partials:
            assert not p.exists()
        assert "timed out" in capsys.readouterr().err

    def test_no_vmlinux_returns_not_found(
        self,
        tmp_vmdir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Missing vmlinux -> warn + return EXIT_NOT_FOUND so CI can
        distinguish 'got the dump but no symbols' from full success."""
        kdir = tmp_path / "kernels" / "5.14"
        kdir.mkdir(parents=True)
        (kdir / "vmlinuz").write_bytes(b"bz")
        # no vmlinux file
        _seed_vm(
            tmp_vmdir, "no-vmlinux", kernel=str(kdir / "vmlinuz")
        )
        outdir = tmp_path / "out"
        args = _args("no-vmlinux", outdir=str(outdir))
        arts = MagicMock()
        arts.kernel = kdir / "vmlinuz"
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=self._ssh_side_effect(
                    "/var/crash/127.0.0.1-2024/vmcore"
                ),
            ),
            patch(
                "ltvm_pkg.vm_commands.resolve_os_artifacts",
                return_value=arts,
            ),
            patch(
                "ltvm_pkg.vm_commands.run",
                side_effect=self._fake_scp_run(),
            ),
        ):
            rc = vm_commands.cmd_crash_collect(args)
        assert rc == EXIT_NOT_FOUND
        err = capsys.readouterr().err
        assert "no vmlinux found" in err

    def test_no_os_id_raises(
        self, tmp_vmdir: Path, tmp_path: Path
    ) -> None:
        """An os_id-less .info file cannot reach vmlinux; we prefer a
        loud RuntimeError over a silent NotFound."""
        kdir = tmp_path / "kernels" / "5.14"
        kdir.mkdir(parents=True)
        (kdir / "vmlinuz").write_bytes(b"bz")
        _seed_vm(
            tmp_vmdir,
            "legacy",
            os_id="",
            kernel=str(kdir / "vmlinuz"),
        )
        args = _args("legacy", outdir=str(tmp_path / "out"))
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=self._ssh_side_effect(
                    "/var/crash/127.0.0.1-2024/vmcore"
                ),
            ),
            patch(
                "ltvm_pkg.vm_commands.run",
                side_effect=self._fake_scp_run(),
            ),
            pytest.raises(RuntimeError, match="no os_id"),
        ):
            vm_commands.cmd_crash_collect(args)


# ── triage script resolution ──────────────────────────────


class TestCrashTriageScript:
    """--mod-dir triggers lustre_triage.py lookup with a well-defined
    search order: LTVM_TRIAGE_SCRIPT, Path.home(), SUDO_USER's home."""

    def _setup_vm(
        self, tmp_vmdir: Path, tmp_path: Path
    ) -> tuple[Path, Path]:
        kdir = tmp_path / "kernels" / "5.14"
        kdir.mkdir(parents=True)
        (kdir / "vmlinuz").write_bytes(b"bz")
        (kdir / "vmlinux").write_bytes(b"ELF")
        _seed_vm(
            tmp_vmdir, "triage-vm", kernel=str(kdir / "vmlinuz")
        )
        return kdir, kdir / "vmlinux"

    def _ssh_side_effect(self):
        def _ssh(ip, cmd, **kw):
            if "find /var/crash" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="/var/crash/127.0.0.1-2024/vmcore",
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        return _ssh

    def test_env_var_takes_priority(
        self, tmp_vmdir: Path, tmp_path: Path
    ) -> None:
        kdir, vmlinux = self._setup_vm(tmp_vmdir, tmp_path)
        outdir = tmp_path / "out"
        triage_script = tmp_path / "my-triage.py"
        triage_script.write_text("#!/usr/bin/env python3\n")

        args = _args(
            "triage-vm", outdir=str(outdir), mod_dir="/build/tree"
        )
        arts = MagicMock()
        arts.kernel = kdir / "vmlinuz"
        scp_ok = MagicMock(returncode=0, stdout="", stderr="")
        triage_ok = MagicMock(returncode=0, stdout="", stderr="")

        run_calls: list[list] = []

        def _run(cmd, **kw):
            run_calls.append(cmd)
            if cmd and "scp" in cmd:
                Path(cmd[-1]).write_bytes(b"vmcore\n")
                return scp_ok
            return triage_ok

        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=self._ssh_side_effect(),
            ),
            patch(
                "ltvm_pkg.vm_commands.resolve_os_artifacts",
                return_value=arts,
            ),
            patch("ltvm_pkg.vm_commands.run", side_effect=_run),
            patch.dict(
                os.environ,
                {"LTVM_TRIAGE_SCRIPT": str(triage_script)},
                clear=False,
            ),
        ):
            rc = vm_commands.cmd_crash_collect(args)
        assert rc == EXIT_OK
        # One of the run() calls must be the triage invocation using the
        # env-var script
        triage_invocations = [
            c for c in run_calls if str(triage_script) in " ".join(str(x) for x in c)
        ]
        assert triage_invocations

    def test_triage_script_not_found_prints_hint(
        self,
        tmp_vmdir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """If neither env var nor home has the triage script, print the
        LTVM_TRIAGE_SCRIPT hint and return EXIT_OK (vmcore was still
        collected successfully)."""
        kdir, _ = self._setup_vm(tmp_vmdir, tmp_path)
        outdir = tmp_path / "out"
        # Point HOME at a place that definitely has no triage script.
        fake_home = tmp_path / "no-tools"
        fake_home.mkdir()

        args = _args(
            "triage-vm", outdir=str(outdir), mod_dir="/build/tree"
        )
        arts = MagicMock()
        arts.kernel = kdir / "vmlinuz"

        def _run(cmd, **kw):
            if cmd and "scp" in cmd:
                Path(cmd[-1]).write_bytes(b"vmcore\n")
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=self._ssh_side_effect(),
            ),
            patch(
                "ltvm_pkg.vm_commands.resolve_os_artifacts",
                return_value=arts,
            ),
            patch("ltvm_pkg.vm_commands.run", side_effect=_run),
            patch(
                "ltvm_pkg.vm_commands.Path.home", return_value=fake_home
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            rc = vm_commands.cmd_crash_collect(args)
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "LTVM_TRIAGE_SCRIPT" in out

    def test_triage_runs_as_sudo_user_when_set(
        self, tmp_vmdir: Path, tmp_path: Path
    ) -> None:
        """SUDO_USER invokes triage as that user (drgn is installed in
        their python path, not root's)."""
        kdir, vmlinux = self._setup_vm(tmp_vmdir, tmp_path)
        outdir = tmp_path / "out"
        triage_script = tmp_path / "t.py"
        triage_script.write_text("#!/usr/bin/env python3\n")

        args = _args(
            "triage-vm", outdir=str(outdir), mod_dir="/build/t"
        )
        arts = MagicMock()
        arts.kernel = kdir / "vmlinuz"
        scp_ok = MagicMock(returncode=0, stdout="", stderr="")
        triage_ok = MagicMock(returncode=0, stdout="", stderr="")

        run_calls: list[list] = []

        def _run(cmd, **kw):
            run_calls.append(cmd)
            if cmd and "scp" in cmd:
                Path(cmd[-1]).write_bytes(b"vmcore\n")
                return scp_ok
            return triage_ok

        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=self._ssh_side_effect(),
            ),
            patch(
                "ltvm_pkg.vm_commands.resolve_os_artifacts",
                return_value=arts,
            ),
            patch("ltvm_pkg.vm_commands.run", side_effect=_run),
            patch.dict(
                os.environ,
                {
                    "SUDO_USER": "alice",
                    "LTVM_TRIAGE_SCRIPT": str(triage_script),
                },
                clear=False,
            ),
        ):
            vm_commands.cmd_crash_collect(args)

        triage_calls = [
            c for c in run_calls if str(triage_script) in " ".join(str(x) for x in c)
        ]
        assert triage_calls
        # First two elements should be ["sudo", "-u", "alice", "python3", ...]
        assert triage_calls[0][:4] == ["sudo", "-u", "alice", "python3"]

    def test_trigger_sends_sysrq(
        self, tmp_vmdir: Path, tmp_path: Path
    ) -> None:
        """--trigger fires 'echo c > /proc/sysrq-trigger' then waits
        for the VM to come back up before looking for the vmcore."""
        kdir, vmlinux = self._setup_vm(tmp_vmdir, tmp_path)
        outdir = tmp_path / "out"
        args = _args(
            "triage-vm", outdir=str(outdir), trigger=True, wait=3
        )
        arts = MagicMock()
        arts.kernel = kdir / "vmlinuz"

        ssh_calls: list[str] = []
        probe_count = {"n": 0}

        def _ssh(ip, cmd, **kw):
            ssh_calls.append(cmd)
            if "sysrq-trigger" in cmd:
                raise subprocess.TimeoutExpired("ssh", 5)
            if cmd == "true":
                # First probe fails (VM still booting), second succeeds
                probe_count["n"] += 1
                rc = 0 if probe_count["n"] >= 2 else 255
                return MagicMock(returncode=rc, stdout="", stderr="")
            if "find /var/crash" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="/var/crash/127.0.0.1-2024/vmcore",
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        def _run(cmd, **kw):
            if cmd and "scp" in cmd:
                Path(cmd[-1]).write_bytes(b"vmcore\n")
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run_ssh", side_effect=_ssh),
            patch(
                "ltvm_pkg.vm_commands.resolve_os_artifacts",
                return_value=arts,
            ),
            patch("ltvm_pkg.vm_commands.run", side_effect=_run),
            # Short-circuit the 5s warm-up sleep and per-probe sleep
            patch("ltvm_pkg.vm_commands.time.sleep"),
        ):
            rc = vm_commands.cmd_crash_collect(args)
        assert rc == EXIT_OK
        # The sysrq trigger command was the first ssh call
        assert "sysrq-trigger" in ssh_calls[0]
        # The true probe was invoked at least once
        assert probe_count["n"] >= 1

    def test_trigger_reports_failure_when_ssh_returns_error(
        self,
        tmp_vmdir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When the trigger ssh returns cleanly but rc!=0 (e.g. auth
        error), surface that rather than hanging in the wait loop."""
        kdir, _ = self._setup_vm(tmp_vmdir, tmp_path)
        outdir = tmp_path / "out"
        args = _args(
            "triage-vm", outdir=str(outdir), trigger=True, wait=3
        )

        ssh_fail = MagicMock(
            returncode=1, stdout="", stderr="permission denied"
        )
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=ssh_fail),
        ):
            rc = vm_commands.cmd_crash_collect(args)
        assert rc != 0
        assert "failed to trigger crash" in capsys.readouterr().err

    def test_trigger_times_out_if_vm_never_returns(
        self,
        tmp_vmdir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """VM never comes back -> EXIT_TIMEOUT."""
        kdir, _ = self._setup_vm(tmp_vmdir, tmp_path)
        outdir = tmp_path / "out"
        args = _args(
            "triage-vm", outdir=str(outdir), trigger=True, wait=2
        )

        def _ssh(ip, cmd, **kw):
            if "sysrq-trigger" in cmd:
                raise subprocess.TimeoutExpired("ssh", 5)
            if cmd == "true":
                # Never responsive
                raise subprocess.TimeoutExpired("ssh", 3)
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run_ssh", side_effect=_ssh),
            patch("ltvm_pkg.vm_commands.time.sleep"),
        ):
            rc = vm_commands.cmd_crash_collect(args)
        assert rc == EXIT_TIMEOUT
        assert "did not come back" in capsys.readouterr().err

    def test_triage_script_failure_warns_but_succeeds(
        self,
        tmp_vmdir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A triage-script failure doesn't fail crash-collect -- the
        user still got the vmcore.  Print a warning + return OK."""
        kdir, _ = self._setup_vm(tmp_vmdir, tmp_path)
        outdir = tmp_path / "out"
        triage_script = tmp_path / "t.py"
        triage_script.write_text("#!/usr/bin/env python3\n")

        args = _args(
            "triage-vm", outdir=str(outdir), mod_dir="/build/t"
        )
        arts = MagicMock()
        arts.kernel = kdir / "vmlinuz"
        scp_ok = MagicMock(returncode=0, stdout="", stderr="")
        triage_fail = MagicMock(returncode=7, stdout="", stderr="")

        def _run(cmd, **kw):
            if cmd and "scp" in cmd:
                Path(cmd[-1]).write_bytes(b"vmcore\n")
                return scp_ok
            return triage_fail

        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=self._ssh_side_effect(),
            ),
            patch(
                "ltvm_pkg.vm_commands.resolve_os_artifacts",
                return_value=arts,
            ),
            patch("ltvm_pkg.vm_commands.run", side_effect=_run),
            patch.dict(
                os.environ,
                {"LTVM_TRIAGE_SCRIPT": str(triage_script)},
                clear=False,
            ),
        ):
            rc = vm_commands.cmd_crash_collect(args)
        assert rc == EXIT_OK
        err = capsys.readouterr().err
        assert "triage script failed" in err
