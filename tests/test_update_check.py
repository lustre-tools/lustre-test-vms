"""Tests for the update-check gate logic.

We don't exercise the network or the actual `git pull` / `sudo
install` side-effects -- the interesting logic is the schedule gate,
config persistence, and ancestry comparison.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def _config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point update_check at an isolated XDG_CONFIG_HOME."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    import importlib

    import ltvm_pkg.update_check as uc

    importlib.reload(uc)
    return tmp_path / "ltvm"


def test_default_mode_is_prompt(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    cfg = uc._load_config()
    assert cfg["update_check"]["mode"] == "prompt"
    assert cfg["update_check"]["last_check_iso"] is None


def test_due_for_check_fresh(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    cfg = uc._load_config()
    assert uc._due_for_check(cfg) is True


def test_due_for_check_recent(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    cfg = uc._load_config()
    cfg["update_check"]["last_check_iso"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    assert uc._due_for_check(cfg) is False


def test_due_for_check_stale(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    cfg = uc._load_config()
    cfg["update_check"]["last_check_iso"] = (
        datetime.now(timezone.utc) - timedelta(hours=48)
    ).isoformat()
    assert uc._due_for_check(cfg) is True


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
        json.dumps(
            {
                "update_check": {
                    "mode": "never",
                    "last_check_iso": datetime.now(timezone.utc).isoformat(),
                }
            }
        )
    )
    # force=True should still be suppressed by "never" -- the user
    # explicitly opted out.
    with (
        patch.object(uc, "_is_interactive", return_value=True),
        patch.object(uc, "_remote_hash") as mock_remote,
    ):
        uc.maybe_check_for_updates(force=True)
    mock_remote.assert_not_called()


def test_json_mode_skips(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    with (
        patch.object(uc, "_is_interactive", return_value=True),
        patch.object(uc, "_remote_hash") as mock_remote,
    ):
        uc.maybe_check_for_updates(use_json=True)
    mock_remote.assert_not_called()


def test_non_tty_skips(_config_dir: Path) -> None:
    import ltvm_pkg.update_check as uc

    with (
        patch.object(uc, "_is_interactive", return_value=False),
        patch.object(uc, "_remote_hash") as mock_remote,
    ):
        uc.maybe_check_for_updates()
    mock_remote.assert_not_called()


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
