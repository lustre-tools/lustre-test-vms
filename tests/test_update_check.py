"""Tests for the update-check gate logic.

We don't exercise the network or the actual `git pull` / `sudo
install` side-effects -- the interesting logic is the schedule gate,
config persistence, and ancestry comparison.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def _config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point update_check at an isolated XDG config + state home."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    import importlib

    import ltvm_pkg.update_check as uc

    importlib.reload(uc)
    return tmp_path / "ltvm"


def test_default_mode_is_prompt(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    cfg = uc._load_config()
    assert cfg["update_check"]["mode"] == "prompt"
    state = uc._load_state()
    assert state["last_check_iso"] is None
    assert state["pending_update"] is None


def test_due_for_check_fresh(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    state = uc._load_state()
    assert uc._due_for_check(state) is True


def test_due_for_check_recent(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    state = uc._load_state()
    state["last_check_iso"] = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).isoformat()
    assert uc._due_for_check(state) is False


def test_due_for_check_stale(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    state = uc._load_state()
    state["last_check_iso"] = (
        datetime.now(timezone.utc) - timedelta(days=8)
    ).isoformat()
    assert uc._due_for_check(state) is True


def test_never_mode_skips(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    _config_dir.mkdir(parents=True, exist_ok=True)
    (_config_dir / "config.json").write_text(
        json.dumps({"update_check": {"mode": "never"}})
    )
    with (
        patch.object(uc, "_is_interactive", return_value=True),
        patch.object(uc, "_remote_hash") as mock_remote,
    ):
        uc.maybe_check_for_updates()
    mock_remote.assert_not_called()


def test_force_bypasses_schedule_and_never(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    _config_dir.mkdir(parents=True, exist_ok=True)
    (_config_dir / "config.json").write_text(
        json.dumps({"update_check": {"mode": "never"}})
    )
    # force=True should still be suppressed by "never" -- the user
    # explicitly opted out.
    with (
        patch.object(uc, "_is_interactive", return_value=True),
        patch.object(uc, "_remote_hash") as mock_remote,
    ):
        uc.maybe_check_for_updates(force=True)
    mock_remote.assert_not_called()


@contextlib.contextmanager
def _pending(uc):  # type: ignore[no-untyped-def]
    """Make the call under test find a pending update."""
    with (
        patch.object(uc, "_local_hash", return_value="aaaaaaa"),
        patch.object(uc, "_remote_hash", return_value="bbbbbbb"),
        patch.object(uc, "_is_newer", return_value=True),
    ):
        yield


def test_json_mode_notifies_but_never_prompts(
    _config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json must not block on a prompt, but must still tell.

    --json is what a scripted or agent caller reaches for, so
    suppressing the notice there hides it from the audience that
    most needs it.  stdout stays clean; the notice goes to stderr.
    """
    import ltvm_pkg.update_check as uc

    with (
        _pending(uc),
        patch.object(uc, "_prompt_choice") as mock_prompt,
        patch.object(uc, "_apply_update") as mock_apply,
        patch.object(uc, "_is_interactive", return_value=True),
    ):
        uc.maybe_check_for_updates(use_json=True)

    mock_prompt.assert_not_called()
    mock_apply.assert_not_called()
    captured = capsys.readouterr()
    assert "update available" in captured.err
    assert "sudo ltvm update" in captured.err
    assert captured.out == ""


def test_non_tty_checks_and_notifies(
    _config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No TTY means no prompt -- it does not mean no check.

    Scripts, CI and agents driving ltvm through a subprocess are a
    large share of real usage and used to see nothing at all.
    """
    import ltvm_pkg.update_check as uc

    with (
        _pending(uc),
        patch.object(uc, "_is_interactive", return_value=False),
        patch.object(uc, "_prompt_choice") as mock_prompt,
        patch.object(uc, "_apply_update") as mock_apply,
    ):
        uc.maybe_check_for_updates()

    mock_prompt.assert_not_called()
    mock_apply.assert_not_called()
    assert "update available" in capsys.readouterr().err
    assert uc._load_state()["pending_update"]["remote"] == "bbbbbbb"


def test_non_tty_auto_mode_never_applies(
    _config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The regression that would actually hurt.

    auto mode runs `sudo ltvm install` and then exits 0.  With no TTY
    the sudo either fails or blocks on a password prompt, and a
    "successful" run exits 0 without having run the user's command --
    which the caller reads as that command succeeding.  Acting
    without a TTY is forbidden, not merely discouraged.
    """
    import ltvm_pkg.update_check as uc

    _config_dir.mkdir(parents=True, exist_ok=True)
    (_config_dir / "config.json").write_text(
        json.dumps({"update_check": {"mode": "auto"}})
    )
    with (
        _pending(uc),
        patch.object(uc, "_is_interactive", return_value=False),
        patch.object(uc, "_apply_update") as mock_apply,
    ):
        uc.maybe_check_for_updates()

    mock_apply.assert_not_called()
    assert "update available" in capsys.readouterr().err


def test_notice_is_rate_limited(
    _config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An agent running twenty ltvm commands gets told once."""
    import ltvm_pkg.update_check as uc

    with (
        _pending(uc),
        patch.object(uc, "_is_interactive", return_value=False),
    ):
        uc.maybe_check_for_updates()
        assert "update available" in capsys.readouterr().err
        for _ in range(5):
            uc.maybe_check_for_updates()
        assert capsys.readouterr().err == ""


def test_pending_cleared_when_local_moves(_config_dir: Path) -> None:
    """Someone who just ran `sudo ltvm update` stops being nagged."""
    import ltvm_pkg.update_check as uc

    with (
        _pending(uc),
        patch.object(uc, "_is_interactive", return_value=False),
    ):
        uc.maybe_check_for_updates()
    assert uc._load_state()["pending_update"] is not None

    # Local tree has moved on; the cached verdict is stale.
    with patch.object(uc, "_local_hash", return_value="bbbbbbb"):
        assert uc._pending_if_current(uc._load_state()) is None
    assert uc._load_state()["pending_update"] is None


def test_refresh_keeps_verdict_when_offline(_config_dir: Path) -> None:
    """An offline week must not make a known update disappear."""
    import ltvm_pkg.update_check as uc

    state = uc._load_state()
    state["pending_update"] = {"local": "aaaaaaa", "remote": "bbbbbbb"}
    with (
        patch.object(uc, "_local_hash", return_value="aaaaaaa"),
        patch.object(uc, "_remote_hash", return_value=None),
    ):
        uc._refresh_cache(state)
    assert state["pending_update"] == {"local": "aaaaaaa", "remote": "bbbbbbb"}
    assert state["last_check_iso"] is not None


def test_prompt_yes_triggers_update(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    with (
        patch.object(uc, "_is_interactive", return_value=True),
        patch.object(uc, "_local_hash", return_value="aaaaaaa"),
        patch.object(uc, "_remote_hash", return_value="bbbbbbb"),
        patch.object(uc, "_is_newer", return_value=True),
        patch.object(uc, "_prompt_choice", return_value="y"),
        patch.object(uc, "_apply_update") as mock_apply,
        pytest.raises(SystemExit) as exc,
    ):
        uc.maybe_check_for_updates()
    # A successful self-update must stop the process rather than run
    # the user's command with old code against the new tree.
    assert exc.value.code == 0
    mock_apply.assert_called_once()


def test_prompt_auto_flips_config(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    with (
        patch.object(uc, "_is_interactive", return_value=True),
        patch.object(uc, "_local_hash", return_value="aaaaaaa"),
        patch.object(uc, "_remote_hash", return_value="bbbbbbb"),
        patch.object(uc, "_is_newer", return_value=True),
        patch.object(uc, "_prompt_choice", return_value="a"),
        patch.object(uc, "_apply_update") as mock_apply,
        pytest.raises(SystemExit) as exc,
    ):
        uc.maybe_check_for_updates()
    assert exc.value.code == 0
    mock_apply.assert_called_once()
    cfg = uc._load_config()
    assert cfg["update_check"]["mode"] == "auto"


def test_prompt_never_persists(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    with (
        patch.object(uc, "_is_interactive", return_value=True),
        patch.object(uc, "_local_hash", return_value="aaaaaaa"),
        patch.object(uc, "_remote_hash", return_value="bbbbbbb"),
        patch.object(uc, "_is_newer", return_value=True),
        patch.object(uc, "_prompt_choice", return_value="x"),
        patch.object(uc, "_apply_update") as mock_apply,
    ):
        uc.maybe_check_for_updates()
    mock_apply.assert_not_called()
    cfg = uc._load_config()
    assert cfg["update_check"]["mode"] == "never"


class TestApplyUpdateInterpreterPinning:
    """`_apply_update` must invoke the installer under the same Python
    that's currently running ltvm -- not via the script's shebang.
    Without this, on a host whose /usr/bin/env python3 falls below the
    floor, the install step bombs and the update aborts mid-flight.
    """

    def test_install_step_uses_sys_executable(
        self, _config_dir: Path, tmp_path: Path
    ) -> None:
        import sys
        from unittest.mock import MagicMock

        import ltvm_pkg.update_check as uc

        # Fake repo with a .git dir + an `ltvm` script so _apply_update
        # gets past its preconditions.
        repo = tmp_path / "fakerepo"
        (repo / ".git").mkdir(parents=True)
        (repo / "ltvm").write_text("# stub\n")

        # Make _apply_update think *this* is its repo.
        # update_check looks up `Path(__file__).resolve().parent.parent`,
        # so we need to patch the module-level path lookup.  The
        # cleanest seam is __file__ itself.
        with (
            patch.object(
                uc, "__file__", str(repo / "ltvm_pkg" / "update_check.py")
            ),
            patch.object(uc.subprocess, "run") as mock_run,
            patch("platform.system", return_value="Linux"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            ok = uc._apply_update()

        assert ok is True
        # Two subprocess calls: git pull, then sudo <python> ltvm install.
        # (We don't pin the order argument by argument -- just check the
        # install command included sys.executable as the python.)
        install_calls = [
            c
            for c in mock_run.call_args_list
            if "install" in c.args[0] and "git" not in c.args[0]
        ]
        assert install_calls, (
            f"no install call seen in {mock_run.call_args_list}"
        )
        argv = install_calls[0].args[0]
        assert argv[0] == "sudo"
        assert argv[1] == sys.executable
        assert argv[-1] == "install"

    def test_install_step_macos_skips_sudo_but_pins_python(
        self, _config_dir: Path, tmp_path: Path
    ) -> None:
        import sys
        from unittest.mock import MagicMock

        import ltvm_pkg.update_check as uc

        repo = tmp_path / "fakerepo"
        (repo / ".git").mkdir(parents=True)
        (repo / "ltvm").write_text("# stub\n")

        with (
            patch.object(
                uc, "__file__", str(repo / "ltvm_pkg" / "update_check.py")
            ),
            patch.object(uc.subprocess, "run") as mock_run,
            patch("platform.system", return_value="Darwin"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            uc._apply_update()

        install_calls = [
            c
            for c in mock_run.call_args_list
            if "install" in c.args[0] and "git" not in c.args[0]
        ]
        assert install_calls
        argv = install_calls[0].args[0]
        assert argv[0] == sys.executable
        assert argv[-1] == "install"


def test_auto_mode_no_prompt(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    _config_dir.mkdir(parents=True, exist_ok=True)
    (_config_dir / "config.json").write_text(
        json.dumps({"update_check": {"mode": "auto"}})
    )
    with (
        patch.object(uc, "_is_interactive", return_value=True),
        patch.object(uc, "_local_hash", return_value="aaaaaaa"),
        patch.object(uc, "_remote_hash", return_value="bbbbbbb"),
        patch.object(uc, "_is_newer", return_value=True),
        patch.object(uc, "_prompt_choice") as mock_prompt,
        patch.object(uc, "_apply_update") as mock_apply,
        pytest.raises(SystemExit) as exc,
    ):
        uc.maybe_check_for_updates()
    assert exc.value.code == 0
    mock_prompt.assert_not_called()
    mock_apply.assert_called_once()
