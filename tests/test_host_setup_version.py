"""Tests for the host setup version: the record install writes, the
daily stale-host notice, doctor's check, and ``ltvm update`` rerunning
install."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ltvm_pkg import cli as ltvm_cli
from ltvm_pkg import host_setup, update_check, vm_commands
from ltvm_pkg import host_setup_version as hsv
from ltvm_pkg.cli import EXIT_ERROR, EXIT_OK, cmd_update

# conftest stubs run_install for the whole suite; keep the real one.
_REAL_RUN_INSTALL = update_check.run_install


@pytest.fixture
def record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A record path with nothing in it: a host installed before this."""
    path = tmp_path / "lib" / "host-setup.json"
    monkeypatch.setenv("LTVM_HOST_SETUP_RECORD", str(path))
    _launcher(tmp_path, monkeypatch, hsv.this_script())
    return path


def _launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: Path
) -> Path:
    """An installed launcher, as ``ltvm install`` writes it, for *script*."""
    link = tmp_path / "bin" / "ltvm"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.write_text(f"#!/bin/sh\nexec '/usr/bin/python3' '{script}' \"$@\"\n")
    monkeypatch.setattr(hsv, "LAUNCHER", link)
    return link


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "state"
    monkeypatch.setattr(update_check, "_STATE_DIR", d)
    monkeypatch.setattr(update_check, "_STATE_FILE", d / "update_cache.json")
    return d / "update_cache.json"


class TestRecord:
    def test_no_record_is_version_zero(self, record: Path) -> None:
        assert hsv.recorded_version() == 0
        assert hsv.is_stale()

    @pytest.mark.parametrize(
        "text",
        [
            "not json",
            "[1]",
            '{"version": "2"}',
            '{"version": -1}',
            '{"version": true}',
            "{}",
        ],
    )
    def test_unusable_record_is_version_zero(
        self, record: Path, text: str
    ) -> None:
        record.parent.mkdir(parents=True)
        record.write_text(text)
        assert hsv.recorded_version() == 0

    def test_comparison(self) -> None:
        cur = hsv.HOST_SETUP_VERSION
        assert hsv.is_stale(cur - 1)
        assert not hsv.is_stale(cur)
        # A record from a newer ltvm (a downgrade) is not behind.
        assert not hsv.is_stale(cur + 1)

    def test_first_value_flags_hosts_from_before_it(self) -> None:
        assert hsv.HOST_SETUP_VERSION >= 1

    def test_write_records_the_current_version(self, record: Path) -> None:
        hsv.write_record()
        data = json.loads(record.read_text())
        assert data["version"] == hsv.HOST_SETUP_VERSION
        assert "ltvm_version" in data and "installed_iso" in data
        assert record.stat().st_mode & 0o777 == 0o644
        assert not hsv.is_stale()

    def test_install_records_and_survives_a_failed_write(
        self, record: Path
    ) -> None:
        host_setup._record_host_setup()
        assert hsv.recorded_version() == hsv.HOST_SETUP_VERSION
        with (
            patch.object(
                hsv, "write_record", side_effect=PermissionError("no")
            ),
            patch.object(host_setup.log, "warning") as warn,
        ):
            host_setup._record_host_setup()
        assert "could not record host setup" in warn.call_args.args[0]

    def test_default_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LTVM_HOST_SETUP_RECORD")
        monkeypatch.setattr(hsv, "_is_macos", lambda: False)
        assert hsv.record_path() == Path("/var/lib/ltvm/host-setup.json")
        assert hsv.install_command() == "sudo ltvm install"
        monkeypatch.setattr(hsv, "_is_macos", lambda: True)
        assert hsv.record_path().parts[:2] == ("/", "Library")
        assert hsv.install_command() == "ltvm install"


class TestNotice:
    def test_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hsv, "HOST_SETUP_VERSION", 3)
        monkeypatch.setattr(hsv, "_is_macos", lambda: False)
        assert hsv.notice_text(1) == (
            "ltvm: host setup is older than this ltvm (1 < 3). "
            "Run: sudo ltvm install"
        )

    def test_once_a_day(
        self, record: Path, state: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        hsv.maybe_notify()
        cap = capsys.readouterr()
        assert "host setup is older than this ltvm (0 <" in cap.err
        assert cap.out == ""
        hsv.maybe_notify()
        assert capsys.readouterr().err == ""

        # A day later it says it again.
        data = json.loads(state.read_text())
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        data[hsv._STATE_KEY] = old.isoformat()
        state.write_text(json.dumps(data))
        hsv.maybe_notify()
        assert "host setup is older" in capsys.readouterr().err

    def test_silent_when_current(
        self, record: Path, state: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        hsv.write_record()
        hsv.maybe_notify()
        assert capsys.readouterr().err == ""
        assert not state.exists()

    def test_keeps_the_update_checks_state(
        self, record: Path, state: Path
    ) -> None:
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"pending_update": {"local": "a"}}))
        hsv.maybe_notify()
        data = json.loads(state.read_text())
        assert data["pending_update"] == {"local": "a"}
        assert data[hsv._STATE_KEY]


class TestRunInstall:
    def _run(
        self,
        tmp_path: Path,
        system: str,
        euid: int,
        rc: int = 0,
        env: dict[str, str] | None = None,
    ) -> tuple[bool, list[Any]]:
        (tmp_path / "ltvm").write_text("")
        calls: list[Any] = []

        def fake_run(cmd: list[str], **kw: Any) -> Any:
            calls.append((cmd, kw))
            if rc:
                raise subprocess.CalledProcessError(rc, cmd)
            return subprocess.CompletedProcess(cmd, 0)

        with (
            patch("platform.system", return_value=system),
            patch.object(update_check.os, "geteuid", return_value=euid),
            patch.object(update_check.subprocess, "run", fake_run),
            patch.dict(update_check.os.environ, env or {}),
        ):
            ok = _REAL_RUN_INSTALL(tmp_path)
        return ok, calls

    def test_linux_user_goes_through_sudo(self, tmp_path: Path) -> None:
        ok, calls = self._run(tmp_path, "Linux", 1000)
        assert ok
        cmd = calls[0][0]
        assert cmd == [
            "sudo",
            sys.executable,
            str(tmp_path / "ltvm"),
            "install",
        ]

    def test_linux_root_runs_it_directly(self, tmp_path: Path) -> None:
        ok, calls = self._run(tmp_path, "Linux", 0)
        assert ok
        assert calls[0][0][0] == sys.executable
        assert "user" not in calls[0][1]

    def test_macos_user_runs_it_as_itself(self, tmp_path: Path) -> None:
        ok, calls = self._run(tmp_path, "Darwin", 501)
        assert ok
        assert calls[0][0][0] == sys.executable
        assert "user" not in calls[0][1]

    def test_macos_under_sudo_drops_to_the_user(self, tmp_path: Path) -> None:
        import pwd

        me = pwd.getpwuid(update_check.os.getuid())
        ok, calls = self._run(
            tmp_path, "Darwin", 0, env={"SUDO_USER": me.pw_name}
        )
        assert ok
        cmd, kw = calls[0]
        assert cmd[0] == sys.executable
        assert kw["user"] == me.pw_uid
        assert kw["env"]["HOME"] == me.pw_dir
        assert "SUDO_USER" not in kw["env"]

    def test_failure(self, tmp_path: Path) -> None:
        ok, _ = self._run(tmp_path, "Linux", 1000, rc=1)
        assert not ok

    def test_apply_update_uses_it(self, tmp_path: Path) -> None:
        with (
            patch.object(update_check.subprocess, "run"),
            patch.object(update_check, "run_install", return_value=True) as ri,
        ):
            assert update_check._apply_update()
        ri.assert_called_once()


def _update_args(**kw: Any) -> argparse.Namespace:
    d: dict[str, Any] = dict(json=False, check=False, force=False)
    d.update(kw)
    return argparse.Namespace(**d)


class TestUpdateRunsInstall:
    def _update(
        self,
        tmp_path: Path,
        versions: list[str],
        install_ok: bool = True,
        **kw: Any,
    ) -> tuple[int, Any]:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "ltvm_pkg").mkdir()

        def fake_git(repo_arg: Path, *args: str, check: bool = True) -> Any:
            out = "bbbbbbb\n" if args[:1] == ("rev-parse",) else ""
            rc = 128 if args[:1] == ("describe",) else 0
            return subprocess.CompletedProcess(list(args), rc, out, "")

        it = iter(versions)
        with (
            patch.object(ltvm_cli, "_require_root", return_value=None),
            patch.object(ltvm_cli, "_ltvm_repo_root", return_value=repo),
            patch.object(
                ltvm_cli, "_current_version", side_effect=lambda **k: next(it)
            ),
            patch.object(ltvm_cli, "_git", side_effect=fake_git),
            patch.object(
                update_check, "run_install", return_value=install_ok
            ) as ri,
        ):
            rc = cmd_update(_update_args(**kw))
        return rc, ri

    def test_runs_install_after_a_pull(self, tmp_path: Path) -> None:
        rc, ri = self._update(tmp_path, ["0.1.a", "0.1.b"])
        assert rc == EXIT_OK
        ri.assert_called_once()
        assert ri.call_args.args[0] == tmp_path / "repo"

    def test_up_to_date_and_current_skips_it(self, tmp_path: Path) -> None:
        rc, ri = self._update(tmp_path, ["0.1.a", "0.1.a"])
        assert rc == EXIT_OK
        ri.assert_not_called()

    def test_up_to_date_but_stale_runs_it(
        self, tmp_path: Path, record: Path
    ) -> None:
        rc, ri = self._update(tmp_path, ["0.1.a", "0.1.a"])
        assert rc == EXIT_OK
        ri.assert_called_once()

    def test_no_install(self, tmp_path: Path) -> None:
        rc, ri = self._update(tmp_path, ["0.1.a", "0.1.b"], no_install=True)
        assert rc == EXIT_OK
        ri.assert_not_called()

    def test_failed_install_fails_the_update(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _ = self._update(tmp_path, ["0.1.a", "0.1.b"], install_ok=False)
        assert rc == EXIT_ERROR
        assert "`ltvm install` failed" in capsys.readouterr().err

    def test_json_keeps_stdout_clean(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, ri = self._update(tmp_path, ["0.1.a", "0.1.b"], json=True)
        assert rc == EXIT_OK
        assert ri.call_args.kwargs["stdout"] is sys.stderr
        assert json.loads(capsys.readouterr().out)["install"] == "ran"

    def test_parser_flag(self) -> None:
        from tests.test_version_update import ltvm_script

        args = ltvm_script.build_parser().parse_args(["update", "--no-install"])
        assert args.no_install is True


class TestDoctor:
    def test_current(
        self, record: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        hsv.write_record()
        assert vm_commands._check_host_setup_version(fix=False) == (0, 0)
        out = capsys.readouterr().out
        assert f"{hsv.HOST_SETUP_VERSION} recorded in {record}" in out

    def test_stale_reports(
        self, record: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert vm_commands._check_host_setup_version(fix=False) == (1, 0)
        assert "host setup is behind this ltvm" in capsys.readouterr().out

    def test_fix_runs_install(
        self, record: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch.object(
            update_check,
            "run_install",
            side_effect=lambda r: hsv.write_record() or True,
        ):
            assert vm_commands._check_host_setup_version(fix=True) == (1, 0)
        assert "fixed: ran ltvm install" in capsys.readouterr().out

    def test_fix_that_fails(
        self, record: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch.object(update_check, "run_install", return_value=False):
            assert vm_commands._check_host_setup_version(fix=True) == (1, 1)
        assert "FAILED: run" in capsys.readouterr().out


class TestInstalledCopy:
    def test_wrapper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _launcher(tmp_path, monkeypatch, tmp_path / "wt" / "ltvm")
        assert hsv.installed_script() == (tmp_path / "wt" / "ltvm").resolve()
        assert not hsv.is_installed_copy()

    def test_symlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link = tmp_path / "ltvm"
        link.symlink_to(hsv.this_script())
        monkeypatch.setattr(hsv, "LAUNCHER", link)
        assert hsv.is_installed_copy()

    def test_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(hsv, "LAUNCHER", tmp_path / "missing")
        assert hsv.installed_script() is None
        assert not hsv.is_installed_copy()

    def test_doctor_fix_leaves_other_checkout(
        self,
        record: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _launcher(tmp_path, monkeypatch, tmp_path / "main" / "ltvm")
        with patch.object(update_check, "run_install") as ri:
            assert vm_commands._check_host_setup_version(fix=True) == (0, 0)
        ri.assert_not_called()
        out = capsys.readouterr().out
        assert "so doctor leaves it" in out
        assert str(hsv.this_script()) in out

    def test_no_notice_from_other_checkout(
        self,
        record: Path,
        state: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _launcher(tmp_path, monkeypatch, tmp_path / "main" / "ltvm")
        hsv.maybe_notify()
        assert capsys.readouterr().err == ""


class TestMainNotice:
    def _main(self, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
        import ltvm_pkg.telemetry as tel
        from tests.test_version_update import ltvm_script as ltvm

        monkeypatch.delenv("LTVM_NO_UPDATE", raising=False)
        p = ltvm.build_parser()
        choices = p._subparsers._group_actions[0].choices
        for name in ("list", "update", "install"):
            choices[name].set_defaults(func=lambda a: 0)
        calls: list[int] = []
        with (
            patch.object(ltvm, "build_parser", return_value=p),
            patch.object(sys, "argv", ["ltvm", *argv]),
            patch.object(hsv, "maybe_notify", lambda: calls.append(1)),
            patch.object(tel, "record", lambda a, rc: None),
            patch.object(tel, "maybe_send", lambda: None),
        ):
            assert ltvm.main() == 0
        return len(calls)

    def test_where_it_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._main(["list"], monkeypatch) == 1
        assert self._main(["--no-update", "list"], monkeypatch) == 0
        assert self._main(["update"], monkeypatch) == 0
        assert self._main(["install"], monkeypatch) == 0
