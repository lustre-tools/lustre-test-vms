"""Tests for build step timing and the completion notification.

A full `ltvm build all` is tens of minutes, most of it the kernel, so
these exist to keep three things true: the durations are readable, the
breakdown accounts for the whole run, and the bell never fires where it
would be noise (a short run, a pipe, CI) or break anything if the user's
notify command is broken.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from ltvm_pkg.cli.util import (
    NOTIFY_AFTER_SECONDS,
    StepTimer,
    format_duration,
    notify_done,
)


class TestFormatDuration:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0s"),
            (0.4, "0s"),
            (1, "1s"),
            (59, "59s"),
            (59.6, "1m 00s"),  # rounds up into the next unit
            (60, "1m 00s"),
            (125, "2m 05s"),
            (3599, "59m 59s"),
            (3600, "1h 00m"),
            (4520, "1h 15m"),
            (7325, "2h 02m"),
        ],
    )
    def test_renders(self, seconds: float, expected: str) -> None:
        assert format_duration(seconds) == expected


class TestStepTimer:
    def test_records_each_step_in_order(self) -> None:
        timer = StepTimer("build all rocky9", quiet=True)
        for name in ("container", "kernel", "image"):
            with timer.step(name):
                pass
        assert [n for n, _ in timer.steps] == ["container", "kernel", "image"]

    def test_records_a_step_that_raised(self) -> None:
        """Knowing a failure took 30 minutes is worth as much as knowing
        a success did."""
        timer = StepTimer("build all rocky9", quiet=True)
        with pytest.raises(RuntimeError):
            with timer.step("kernel"):
                raise RuntimeError("boom")
        assert [n for n, _ in timer.steps] == ["kernel"]

    def test_prints_each_step_unless_quiet(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with StepTimer("x", quiet=False).step("kernel"):
            pass
        assert "kernel took" in capsys.readouterr().out

        with StepTimer("x", quiet=True).step("kernel"):
            pass
        assert capsys.readouterr().out == ""

    def test_summary_lists_the_total_then_the_breakdown(self) -> None:
        timer = StepTimer("build all rocky9", quiet=True)
        timer.steps = [("container", 63.0), ("kernel", 1960.0)]
        lines = timer.summary_lines()
        assert lines[0].startswith("build all rocky9 finished in ")
        assert "container" in lines[1] and "1m 03s" in lines[1]
        assert "kernel" in lines[2] and "32m 40s" in lines[2]

    def test_durations_are_right_aligned(self) -> None:
        """The column mixes 12s with 32m 40s and exists to be compared."""
        timer = StepTimer("t", quiet=True)
        timer.steps = [("a", 12.0), ("b", 1960.0)]
        lines = timer.summary_lines()[1:]
        assert [len(line) for line in lines] == [len(lines[0])] * 2
        assert lines[0].endswith("    12s")

    def test_a_single_step_command_gets_no_rule(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Nothing to break down, so no separator above one line."""
        StepTimer("build kernel rocky9", quiet=False).report()
        out = capsys.readouterr().out
        assert "---" not in out
        assert "build kernel rocky9 finished in" in out

    def test_json_mode_prints_nothing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        timer = StepTimer("build all rocky9", quiet=True)
        with timer.step("kernel"):
            pass
        timer.report()
        assert capsys.readouterr().out == ""


class TestNotifyDone:
    def _tty(self, is_tty: bool) -> Any:
        return patch("sys.stdout.isatty", return_value=is_tty)

    def test_rings_the_bell_on_a_long_interactive_run(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("LTVM_NO_BELL", raising=False)
        monkeypatch.delenv("LTVM_NOTIFY_COMMAND", raising=False)
        with self._tty(True):
            notify_done("build all rocky9", NOTIFY_AFTER_SECONDS + 1)
        # stderr, so it cannot land in anything redirecting stdout.
        assert "\a" in capsys.readouterr().err

    def test_silent_for_a_short_run(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("LTVM_NO_BELL", raising=False)
        with self._tty(True):
            notify_done("build container rocky9", NOTIFY_AFTER_SECONDS - 1)
        assert capsys.readouterr().err == ""

    def test_silent_when_not_a_terminal(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bell in a CI log or a pipe is pure noise."""
        monkeypatch.delenv("LTVM_NO_BELL", raising=False)
        with self._tty(False):
            notify_done("build all rocky9", 600)
        assert capsys.readouterr().err == ""

    def test_opt_out_env(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LTVM_NO_BELL", "1")
        with self._tty(True):
            notify_done("build all rocky9", 600)
        assert capsys.readouterr().err == ""

    def test_runs_the_user_notify_command_with_the_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_NOTIFY_COMMAND", "notify-send ltvm")
        monkeypatch.setenv("LTVM_NO_BELL", "1")
        with patch("subprocess.run") as run:
            notify_done("build all rocky9", 600)
        argv = run.call_args[0][0]
        # An argument list, never a shell string (see CLAUDE.md).
        assert argv[:2] == ["notify-send", "ltvm"]
        assert "build all rocky9 finished in 10m 00s" in argv[2]

    def test_notify_command_is_not_run_for_a_short_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_NOTIFY_COMMAND", "notify-send ltvm")
        with patch("subprocess.run") as run:
            notify_done("x", 1)
        assert not run.called

    @pytest.mark.parametrize(
        "failure",
        [
            FileNotFoundError("no such binary"),
            subprocess.SubprocessError("died"),
            OSError("nope"),
        ],
    )
    def test_a_broken_notify_command_never_fails_the_build(
        self, monkeypatch: pytest.MonkeyPatch, failure: Exception
    ) -> None:
        monkeypatch.setenv("LTVM_NOTIFY_COMMAND", "does-not-exist")
        monkeypatch.setenv("LTVM_NO_BELL", "1")
        with patch("subprocess.run", side_effect=failure):
            notify_done("build all rocky9", 600)  # must not raise

    def test_an_unparseable_notify_command_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_NOTIFY_COMMAND", 'notify "unclosed')
        monkeypatch.setenv("LTVM_NO_BELL", "1")
        with patch("subprocess.run") as run:
            notify_done("build all rocky9", 600)
        assert not run.called

    def test_an_empty_notify_command_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_NOTIFY_COMMAND", "   ")
        monkeypatch.setenv("LTVM_NO_BELL", "1")
        with patch("subprocess.run") as run:
            notify_done("build all rocky9", 600)
        assert not run.called
