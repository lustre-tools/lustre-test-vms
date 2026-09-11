"""Tests for the telemetry client.

The network is never exercised: what matters here is the opt-out
precedence, the schedule, and the promise about what the payload
does and does not contain.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated XDG config + state, and no site config by default."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LTVM_SITE_CONFIG", str(tmp_path / "absent.conf"))
    monkeypatch.delenv("LTVM_TELEMETRY", raising=False)
    import importlib

    import ltvm_pkg.telemetry as t

    importlib.reload(t)
    return tmp_path


# ---------------------------------------------------------------------------
# Opt-out precedence
# ---------------------------------------------------------------------------


def test_enabled_by_default(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    assert t.is_enabled() is True


def test_env_kill_switch(_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ltvm_pkg.telemetry as t

    for value in ("0", "no", "false"):
        monkeypatch.setenv("LTVM_TELEMETRY", value)
        assert t.is_enabled() is False, value


def test_user_opt_out(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    t.set_enabled(False)
    assert t.is_enabled() is False
    t.set_enabled(True)
    assert t.is_enabled() is True


def test_site_config_disables(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    t._SITE_CONFIG.write_text("[telemetry]\nenabled = false\n")
    assert t.is_enabled() is False


def test_site_opt_out_beats_the_user(_home: Path) -> None:
    """A site opt-out a user could silently undo would not be one."""
    import ltvm_pkg.telemetry as t

    t._SITE_CONFIG.write_text("[telemetry]\nenabled = false\n")
    t.set_enabled(True)
    assert t.is_enabled() is False
    assert "site-wide" in (t.status()["disabled_by"] or "")


def test_unreadable_site_config_is_ignored(_home: Path) -> None:
    """A broken /etc/ltvm.conf must not be a broken ltvm."""
    import ltvm_pkg.telemetry as t

    t._SITE_CONFIG.write_text("this is not ini [[[\n")
    assert t.is_enabled() is True


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_install_id_is_stable_and_random(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    first = t.install_id()
    assert t.install_id() == first
    # Not derived from anything about the host.
    import socket
    import uuid as _uuid

    assert socket.gethostname() not in first
    assert _uuid.UUID(first).version == 4


def test_config_write_preserves_update_check(_home: Path) -> None:
    """telemetry and update_check share config.json.

    Clobbering someone's update mode to record an install ID would be
    a memorable bug; this is the test that stops it.
    """
    import ltvm_pkg.telemetry as t

    t._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    t._CONFIG_FILE.write_text(json.dumps({"update_check": {"mode": "never"}}))
    t.install_id()
    data = json.loads(t._CONFIG_FILE.read_text())
    assert data["update_check"]["mode"] == "never"
    assert data["telemetry"]["install_id"]


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def test_due_when_never_sent(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    assert t._due_for_send({"last_send_iso": None}, "abc") is True


def test_not_due_the_next_day(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    state = {
        "last_send_iso": (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).isoformat()
    }
    assert t._due_for_send(state, "abc") is False


def test_due_after_the_interval_plus_jitter(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    state = {
        "last_send_iso": (
            datetime.now(timezone.utc) - timedelta(days=9)
        ).isoformat()
    }
    assert t._due_for_send(state, "abc") is True


def test_jitter_is_stable_and_bounded(_home: Path) -> None:
    """Stable per install, so it does not move between runs."""
    import ltvm_pkg.telemetry as t

    assert t._jitter("abc") == t._jitter("abc")
    assert t._jitter("abc") != t._jitter("xyz")
    for uid in ("a", "b", "c", "d", "e"):
        assert abs(t._jitter(uid)) <= timedelta(hours=12)


def test_corrupt_stamp_is_treated_as_due(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    assert t._due_for_send({"last_send_iso": "not a date"}, "abc") is True


# ---------------------------------------------------------------------------
# The payload promise
# ---------------------------------------------------------------------------


def test_payload_is_a_closed_list(_home: Path) -> None:
    """The set of fields is the product promise, so pin it.

    A new field here is a deliberate act that has to update this test
    and the docstring's never-send list -- not something that arrives
    with an unrelated change.
    """
    import ltvm_pkg.telemetry as t

    assert set(t._payload()) == {
        "schema",
        "install_id",
        "sent_at",
        "ltvm_version",
    }


def test_payload_leaks_nothing_identifying(_home: Path) -> None:
    import getpass
    import socket

    import ltvm_pkg.telemetry as t

    blob = json.dumps(t._payload())
    for secret in (socket.gethostname(), getpass.getuser(), str(Path.home())):
        if secret:
            assert secret not in blob


def test_preview_matches_what_send_would_post(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    with patch.object(t, "_post", return_value=True) as mock_post:
        t.send_now()
    posted = mock_post.call_args[0][0]
    assert set(posted) == set(t.preview())


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def test_stamp_moves_even_when_the_send_fails(_home: Path) -> None:
    """No retry storms: a missed week is just a missed week."""
    import ltvm_pkg.telemetry as t

    with patch.object(t, "_post", return_value=False):
        assert t.send_now() is False
    assert t._load_state()["last_send_iso"] is not None


def test_first_run_notices_but_does_not_send(
    _home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The notice is the consent point, so nothing leaves before it.

    Seeding the clock here is what gives someone a week in which
    `ltvm telemetry off` means nothing was ever sent.
    """
    import ltvm_pkg.telemetry as t

    with patch.object(t, "_spawn_detached_send") as mock_spawn:
        t.maybe_send()
    mock_spawn.assert_not_called()
    assert "anonymous weekly check-in" in capsys.readouterr().err
    assert t._load_state()["last_send_iso"] is not None


def test_notice_prints_once_only(
    _home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import ltvm_pkg.telemetry as t

    with patch.object(t, "_spawn_detached_send"):
        t.maybe_send()
        capsys.readouterr()
        for _ in range(3):
            t.maybe_send()
    assert capsys.readouterr().err == ""


def test_sends_once_due(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    with patch.object(t, "_spawn_detached_send") as mock_spawn:
        t.maybe_send()  # notice run
        t._save_state(
            {
                "last_send_iso": (
                    datetime.now(timezone.utc) - timedelta(days=9)
                ).isoformat()
            }
        )
        t.maybe_send()
    mock_spawn.assert_called_once()


def test_disabled_never_spawns(_home: Path) -> None:
    import ltvm_pkg.telemetry as t

    t.set_enabled(False)
    with patch.object(t, "_spawn_detached_send") as mock_spawn:
        t.maybe_send()
    mock_spawn.assert_not_called()


def test_never_raises_when_the_config_dir_is_unwritable(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A telemetry failure must never be able to fail a command."""
    import ltvm_pkg.telemetry as t

    def _boom(*a: object, **kw: object) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", _boom)
    monkeypatch.setattr(Path, "mkdir", _boom)
    t._save_config({"enabled": True})
    t._save_state({"last_send_iso": None})
