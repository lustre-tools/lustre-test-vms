"""Regression test: --verbose / LTVM_VERBOSE must surface tracebacks
from inside ``except`` blocks so programming bugs (TypeError,
AttributeError) don't get flattened into opaque ``str(exc)`` messages.
"""

from __future__ import annotations

import logging

import pytest


def test_traceback_hidden_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    from ltvm_pkg.cli import _emit_error

    # Pretend we're in an except block with a live exception.
    try:
        raise TypeError("oops")
    except TypeError:
        _emit_error("something broke", use_json=False)

    err = capsys.readouterr().err
    assert "error: something broke" in err
    # No traceback lines in the default path.
    assert "Traceback" not in err
    assert "TypeError" not in err


def test_traceback_surfaced_by_root_debug(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ltvm_pkg.cli import _emit_error

    root = logging.getLogger()
    prev = root.level
    root.setLevel(logging.DEBUG)
    try:
        try:
            raise ValueError("detailed-cause-marker")
        except ValueError:
            _emit_error("wrapped", use_json=False)
    finally:
        root.setLevel(prev)

    err = capsys.readouterr().err
    assert "error: wrapped" in err
    assert "Traceback" in err
    assert "ValueError: detailed-cause-marker" in err


def test_traceback_surfaced_by_env_var(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ltvm_pkg.cli import _emit_error

    monkeypatch.setenv("LTVM_VERBOSE", "1")
    try:
        raise RuntimeError("env-marker")
    except RuntimeError:
        _emit_error("wrapped", use_json=False)

    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "RuntimeError: env-marker" in err


def test_no_traceback_when_no_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Called outside an except block, _emit_error stays quiet about
    tracebacks regardless of verbose setting -- there is no exception
    to print."""
    from ltvm_pkg.cli import _emit_error

    root = logging.getLogger()
    prev = root.level
    root.setLevel(logging.DEBUG)
    try:
        _emit_error("plain error, no cause", use_json=False)
    finally:
        root.setLevel(prev)

    err = capsys.readouterr().err
    assert "error: plain error, no cause" in err
    assert "Traceback" not in err


def test_json_mode_skips_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON callers are scripts; never inject traceback text into
    their stderr (it wouldn't parse).  The JSON body remains
    machine-readable."""
    from ltvm_pkg.cli import _emit_error

    root = logging.getLogger()
    prev = root.level
    root.setLevel(logging.DEBUG)
    try:
        try:
            raise ValueError("nope")
        except ValueError:
            _emit_error("machine-readable", use_json=True)
    finally:
        root.setLevel(prev)

    err = capsys.readouterr().err
    assert "Traceback" not in err
    # Body is still valid JSON.
    import json

    parsed = json.loads(err.split("\n", 1)[0] if "\n" not in err else err)
    assert parsed["error"] == "machine-readable"
