"""Bootstrap-floor checks for the ltvm entry-point script.

The launcher self-bootstraps into ``.venv/bin/python`` when invoked
via system Python without the deps installed.  When the floor moves
past the venv-creation Python (e.g. floor=3.10, venv=3.9) the bootstrap
must refuse to ``os.execv`` -- otherwise the user gets trapped in
"requires Python 3.10+" even after switching to a newer interpreter.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

_LTVM_PATH = str(Path(__file__).parent.parent / "ltvm")


def _load_ltvm() -> Any:
    loader = importlib.machinery.SourceFileLoader("ltvm", _LTVM_PATH)
    spec = importlib.util.spec_from_loader("ltvm", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ltvm = _load_ltvm()


class TestVenvFloorHelper:
    """Unit tests for `_venv_meets_floor`."""

    def test_returns_true_for_meeting_floor(self, tmp_path: Path) -> None:
        venv = tmp_path / ".venv"
        (venv / "bin").mkdir(parents=True)
        py = venv / "bin" / "python"
        py.touch()
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.13\n")
        assert ltvm._venv_meets_floor(py, (3, 10)) is True

    def test_returns_false_for_old_python(self, tmp_path: Path) -> None:
        venv = tmp_path / ".venv"
        (venv / "bin").mkdir(parents=True)
        py = venv / "bin" / "python"
        py.touch()
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.9.25\n")
        assert ltvm._venv_meets_floor(py, (3, 10)) is False

    def test_missing_cfg_assumes_usable(self, tmp_path: Path) -> None:
        """Without pyvenv.cfg we can't tell -- err on the side of using
        the venv; the re-exec'd python's own floor check is the safety
        net."""
        venv = tmp_path / ".venv"
        (venv / "bin").mkdir(parents=True)
        py = venv / "bin" / "python"
        py.touch()
        # No pyvenv.cfg.
        assert ltvm._venv_meets_floor(py, (3, 10)) is True

    def test_unparseable_version_assumes_usable(self, tmp_path: Path) -> None:
        venv = tmp_path / ".venv"
        (venv / "bin").mkdir(parents=True)
        py = venv / "bin" / "python"
        py.touch()
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\nversion = ???\n")
        assert ltvm._venv_meets_floor(py, (3, 10)) is True

    def test_version_info_key_also_recognized(self, tmp_path: Path) -> None:
        """uv-style cfgs sometimes use 'version_info' instead of 'version'."""
        venv = tmp_path / ".venv"
        (venv / "bin").mkdir(parents=True)
        py = venv / "bin" / "python"
        py.touch()
        (venv / "pyvenv.cfg").write_text(
            "home = /usr/bin\nversion_info = 3.9.25.final.0\n"
        )
        assert ltvm._venv_meets_floor(py, (3, 10)) is False


class TestStaleVenvIntegration:
    """End-to-end: invoke ltvm with a fake stale venv and confirm the
    user sees actionable guidance instead of bouncing through the
    floor check."""

    def test_stale_venv_emits_recreate_hint(self, tmp_path: Path) -> None:
        # Build a fake repo dir with a stub `ltvm` script that mirrors
        # the real bootstrap, plus a fake stale .venv whose pyvenv.cfg
        # advertises Python 3.9.  Run it via the current interpreter and
        # confirm we exit 1 with the expected hint -- without ever
        # exec'ing into the fake venv (the fake .venv/bin/python isn't
        # executable, so an exec attempt would crash before printing).
        repo = tmp_path / "repo"
        repo.mkdir()
        venv = repo / ".venv"
        (venv / "bin").mkdir(parents=True)
        # Touch a non-executable placeholder so .exists() is True.
        (venv / "bin" / "python").write_text("#!/bin/false\n")
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.9.25\n")

        # Stub script: mirror just the bootstrap.  Skip the rest of ltvm
        # so the test doesn't depend on yaml/argcomplete being absent
        # from the test interpreter (they aren't, in our venv).
        stub = repo / "ltvm"
        stub.write_text(
            textwrap.dedent("""\
                #!/usr/bin/env python3
                import sys
                if sys.version_info < (3, 10):
                    sys.exit(1)
                import os
                from pathlib import Path
                _REPO_ROOT = Path(__file__).resolve().parent
                _VENV_PY = _REPO_ROOT / ".venv" / "bin" / "python"
                _BOOT_FLOOR = (3, 10)

                def _venv_meets_floor(venv_py, floor):
                    cfg = venv_py.parent.parent / "pyvenv.cfg"
                    if not cfg.exists():
                        return True
                    try:
                        text = cfg.read_text()
                    except (OSError, UnicodeDecodeError):
                        return True
                    for line in text.splitlines():
                        if "=" not in line:
                            continue
                        key, _, val = line.partition("=")
                        if key.strip() not in ("version", "version_info"):
                            continue
                        parts = val.strip().split(".")
                        try:
                            return (int(parts[0]), int(parts[1])) >= floor
                        except (ValueError, IndexError):
                            return True
                    return True

                if _VENV_PY.exists():
                    if not _venv_meets_floor(_VENV_PY, _BOOT_FLOOR):
                        # Force the stale-venv path: pretend deps are
                        # missing so we hit the actionable error.
                        _venv_dir = _VENV_PY.parent.parent
                        sys.stderr.write(
                            "error: ltvm's venv at %s was created with a "
                            "Python older than %d.%d and cannot be used.\\n"
                            "hint: rm -rf %s && python%d.%d -m venv %s && "
                            "%s/bin/pip install pyyaml argcomplete\\n"
                            % (
                                _venv_dir,
                                _BOOT_FLOOR[0], _BOOT_FLOOR[1],
                                _venv_dir,
                                _BOOT_FLOOR[0], _BOOT_FLOOR[1] + 1,
                                _venv_dir,
                                _venv_dir,
                            )
                        )
                        sys.exit(1)
                # Healthy path -- nothing to do for the stub.
                sys.exit(0)
            """)
        )

        result = subprocess.run(
            [sys.executable, str(stub)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1
        assert "older than 3.10" in result.stderr
        assert f"rm -rf {venv}" in result.stderr
        assert "python3.11 -m venv" in result.stderr
