"""Tests for ``ltvm --version`` and ``ltvm update``.

Covers:
- Base version constant
- Version composition with and without a baked _build_info
- --version flag exits 0 and prints "ltvm <version>"
- update parser registration
- cmd_update happy path, --check, dirty-tree refusal, --force, and
  the not-a-git-checkout error path

All git interaction is mocked -- nothing in here touches the real
network or filesystem outside tmp_path.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.machinery
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import ltvm_pkg
from ltvm_pkg import cli as ltvm_cli
from ltvm_pkg.cli import EXIT_ERROR, EXIT_OK, cmd_update

# ---------------------------------------------------------------------------
# Load the ltvm script (no .py extension) so we can exercise the
# top-level parser, just like tests/test_ltvm_cli.py does.
# ---------------------------------------------------------------------------

_LTVM_PATH = str(Path(__file__).parent.parent / "ltvm")


def _load_ltvm() -> Any:
    loader = importlib.machinery.SourceFileLoader("ltvm", _LTVM_PATH)
    spec = importlib.util.spec_from_loader("ltvm", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ltvm_script = _load_ltvm()


# ---------------------------------------------------------------------------
# Version computation
# ---------------------------------------------------------------------------


class TestVersion:
    """Assertions tied to BASE_VERSION as a variable, not a literal,
    so a version bump doesn't require re-plumbing every test.
    test_base_version_shape covers the separate concern that the
    constant itself looks reasonable."""

    def test_base_version_shape(self) -> None:
        # Reject empty, dev-marker suffixes, or anything that isn't
        # MAJOR.MINOR -- the compute-version logic and --version output
        # rely on this shape.
        assert re.fullmatch(r"\d+\.\d+", ltvm_pkg.BASE_VERSION), (
            f"BASE_VERSION must match MAJOR.MINOR, got "
            f"{ltvm_pkg.BASE_VERSION!r}"
        )

    def test_version_starts_with_base_version(self) -> None:
        base = ltvm_pkg.BASE_VERSION
        v = ltvm_pkg.__version__
        assert v == base or v.startswith(base + "."), (
            f"__version__ ({v!r}) should be {base!r} or {base!r}.<hash>"
        )

    def test_compute_version_uses_baked_hash(self) -> None:
        """When _build_info.BUILD_HASH exists, _compute_version uses it."""
        fake = type(sys)("ltvm_pkg._build_info")
        fake.BUILD_HASH = "deadbee"  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"ltvm_pkg._build_info": fake}):
            assert (
                ltvm_pkg._compute_version()
                == f"{ltvm_pkg.BASE_VERSION}.deadbee"
            )

    def test_compute_version_falls_back_to_git(self) -> None:
        """No _build_info → fall back to _git_short_hash."""
        with (
            patch.dict(sys.modules, {"ltvm_pkg._build_info": None}),
            patch.object(ltvm_pkg, "_git_short_hash", return_value="cafef00"),
        ):
            assert (
                ltvm_pkg._compute_version()
                == f"{ltvm_pkg.BASE_VERSION}.cafef00"
            )

    def test_compute_version_bare_when_no_git(self) -> None:
        """No _build_info and git unavailable → just BASE_VERSION."""
        with (
            patch.dict(sys.modules, {"ltvm_pkg._build_info": None}),
            patch.object(ltvm_pkg, "_git_short_hash", return_value=None),
        ):
            assert ltvm_pkg._compute_version() == ltvm_pkg.BASE_VERSION

    def test_git_short_hash_handles_missing_git(self, tmp_path: Path) -> None:
        """If the parent dir isn't a git checkout, return None."""
        fake_init = tmp_path / "ltvm_pkg" / "__init__.py"
        fake_init.parent.mkdir()
        fake_init.write_text("")
        with patch.object(ltvm_pkg, "__file__", str(fake_init)):
            assert ltvm_pkg._git_short_hash() is None


# ---------------------------------------------------------------------------
# --version flag on the parser
# ---------------------------------------------------------------------------


class TestVersionFlag:
    def test_version_flag_exits_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        p = ltvm_script.build_parser()
        with pytest.raises(SystemExit) as exc_info:
            p.parse_args(["--version"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert out.startswith(f"ltvm {ltvm_pkg.BASE_VERSION}")

    def test_version_flag_via_main(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch.object(sys, "argv", ["ltvm", "--version"]):
            with pytest.raises(SystemExit) as exc_info:
                ltvm_script.main()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert f"ltvm {ltvm_pkg.BASE_VERSION}" in out


# ---------------------------------------------------------------------------
# update subcommand parser registration
# ---------------------------------------------------------------------------


class TestUpdateParser:
    def test_update_subcommand_registered(self) -> None:
        p = ltvm_script.build_parser()
        args = p.parse_args(["update"])
        assert args.command == "update"
        assert args.func is cmd_update
        assert args.check is False
        assert args.force is False

    def test_update_check_flag(self) -> None:
        p = ltvm_script.build_parser()
        args = p.parse_args(["update", "--check"])
        assert args.check is True

    def test_update_force_flag(self) -> None:
        p = ltvm_script.build_parser()
        args = p.parse_args(["update", "--force"])
        assert args.force is True

    def test_update_help_exits_zero(self) -> None:
        p = ltvm_script.build_parser()
        with pytest.raises(SystemExit) as exc_info:
            p.parse_args(["update", "--help"])
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# cmd_update implementation
# ---------------------------------------------------------------------------


def _make_args(**overrides: Any) -> argparse.Namespace:
    defaults = dict(json=False, check=False, force=False)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _fake_repo(tmp_path: Path) -> Path:
    """Create a minimal repo layout that _ltvm_repo_root() will accept.

    We need a .git directory and a ltvm_pkg/ directory so cmd_update can
    write _build_info.py into it.
    """
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "ltvm_pkg").mkdir()
    return repo


class TestCmdUpdate:
    @pytest.fixture(autouse=True)
    def _bypass_require_root(self):
        # cmd_update requires root to avoid leaking a git permission
        # error when attendees run it against an admin-owned checkout.
        # Tests call it directly as an unprivileged user, so stub the
        # guard out.
        with patch.object(ltvm_cli, "_require_root", return_value=None):
            yield

    def test_repo_root_resolves_through_symlink(self, tmp_path: Path) -> None:
        """``ltvm update`` is run from an installed (symlinked) copy.

        Verify _ltvm_repo_root() follows symlinks back to the real repo
        rather than landing in /usr/local/bin or similar.
        """
        real_repo = tmp_path / "real-repo"
        (real_repo / "ltvm_pkg").mkdir(parents=True)
        real_cli = real_repo / "ltvm_pkg" / "cli.py"
        real_cli.write_text("# stub cli\n")

        # Mimic an installed copy: a symlink at a different path that
        # points back at the real cli.py.
        link_dir = tmp_path / "installed" / "ltvm_pkg"
        link_dir.mkdir(parents=True)
        link_cli = link_dir / "cli.py"
        link_cli.symlink_to(real_cli)

        with patch.object(ltvm_cli, "__file__", str(link_cli)):
            assert ltvm_cli._ltvm_repo_root() == real_repo.resolve()

    def test_not_a_git_checkout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "no-git"
        repo.mkdir()
        with patch.object(ltvm_cli, "_ltvm_repo_root", return_value=repo):
            rc = cmd_update(_make_args())
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "not a git checkout" in err

    def test_dirty_tree_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _fake_repo(tmp_path)

        def fake_git(repo_arg: Path, *args: str, check: bool = True):
            if args[:1] == ("status",):
                return subprocess.CompletedProcess(
                    args=list(args),
                    returncode=0,
                    stdout=" M foo.py\n",
                    stderr="",
                )
            raise AssertionError(f"unexpected git call: {args}")

        with (
            patch.object(ltvm_cli, "_ltvm_repo_root", return_value=repo),
            patch.object(
                ltvm_cli, "_current_version", return_value="0.10.aaaa"
            ),
            patch.object(ltvm_cli, "_git", side_effect=fake_git),
        ):
            rc = cmd_update(_make_args())
        assert rc == EXIT_ERROR
        assert "local changes" in capsys.readouterr().err

    def test_dirty_tree_allowed_with_force(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _fake_repo(tmp_path)
        calls: list[tuple[str, ...]] = []

        def fake_git(repo_arg: Path, *args: str, check: bool = True):
            calls.append(args)
            if args[:1] == ("fetch",):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout="", stderr=""
                )
            if args[:1] == ("pull",):
                return subprocess.CompletedProcess(
                    args=list(args),
                    returncode=0,
                    stdout="Updating aaaa..bbbb\nFast-forward\n",
                    stderr="",
                )
            if args[:1] == ("rev-parse",):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout="bbbbbbb\n", stderr=""
                )
            raise AssertionError(f"unexpected git call (force path): {args}")

        versions = iter(["0.10.aaaaaaa", "0.10.bbbbbbb"])

        with (
            patch.object(ltvm_cli, "_ltvm_repo_root", return_value=repo),
            patch.object(
                ltvm_cli,
                "_current_version",
                side_effect=lambda **kw: next(versions),
            ),
            patch.object(ltvm_cli, "_git", side_effect=fake_git),
        ):
            rc = cmd_update(_make_args(force=True))
        assert rc == EXIT_OK
        # No status call because --force skips the dirty check
        assert all(args[:1] != ("status",) for args in calls)
        out = capsys.readouterr().out
        assert "Updated ltvm: 0.10.aaaaaaa -> 0.10.bbbbbbb" in out
        # _build_info.py should have been refreshed with the new hash
        bi = (repo / "ltvm_pkg" / "_build_info.py").read_text()
        assert 'BUILD_HASH = "bbbbbbb"' in bi

    def test_clean_tree_happy_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _fake_repo(tmp_path)

        def fake_git(repo_arg: Path, *args: str, check: bool = True):
            if args[:1] == ("status",):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout="", stderr=""
                )
            if args[:1] == ("fetch",):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout="", stderr=""
                )
            if args[:1] == ("pull",):
                return subprocess.CompletedProcess(
                    args=list(args),
                    returncode=0,
                    stdout="Updating aaaa..bbbb\nFast-forward\n",
                    stderr="",
                )
            if args[:1] == ("rev-parse",):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout="bbbbbbb\n", stderr=""
                )
            raise AssertionError(f"unexpected git call: {args}")

        versions = iter(["0.10.aaaaaaa", "0.10.bbbbbbb"])
        with (
            patch.object(ltvm_cli, "_ltvm_repo_root", return_value=repo),
            patch.object(
                ltvm_cli,
                "_current_version",
                side_effect=lambda **kw: next(versions),
            ),
            patch.object(ltvm_cli, "_git", side_effect=fake_git),
        ):
            rc = cmd_update(_make_args())
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "Updated ltvm: 0.10.aaaaaaa -> 0.10.bbbbbbb" in out
        assert "Fast-forward" in out

    def test_already_up_to_date(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _fake_repo(tmp_path)

        def fake_git(repo_arg: Path, *args: str, check: bool = True):
            if args[:1] == ("status",):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout="", stderr=""
                )
            if args[:1] == ("fetch",):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout="", stderr=""
                )
            if args[:1] == ("pull",):
                return subprocess.CompletedProcess(
                    args=list(args),
                    returncode=0,
                    stdout="Already up to date.\n",
                    stderr="",
                )
            if args[:1] == ("rev-parse",):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout="aaaaaaa\n", stderr=""
                )
            raise AssertionError(f"unexpected git call: {args}")

        with (
            patch.object(ltvm_cli, "_ltvm_repo_root", return_value=repo),
            patch.object(
                ltvm_cli, "_current_version", return_value="0.10.aaaaaaa"
            ),
            patch.object(ltvm_cli, "_git", side_effect=fake_git),
        ):
            rc = cmd_update(_make_args())
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "Already up to date at 0.10.aaaaaaa" in out

    def test_check_flag_reports_behind_count(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _fake_repo(tmp_path)

        def fake_git(repo_arg: Path, *args: str, check: bool = True):
            if args[:1] == ("fetch",):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout="", stderr=""
                )
            if args[:1] == ("rev-list",):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout="3\n", stderr=""
                )
            raise AssertionError(f"unexpected git call: {args}")

        with (
            patch.object(ltvm_cli, "_ltvm_repo_root", return_value=repo),
            patch.object(
                ltvm_cli, "_current_version", return_value="0.10.aaaa"
            ),
            patch.object(ltvm_cli, "_git", side_effect=fake_git),
        ):
            rc = cmd_update(_make_args(check=True, json=True))
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        result = json.loads(out)
        assert result == {
            "version": "0.10.aaaa",
            "behind": 3,
            "update_available": True,
        }

    def test_check_flag_zero_behind(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _fake_repo(tmp_path)

        def fake_git(repo_arg: Path, *args: str, check: bool = True):
            if args[:1] == ("fetch",):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout="", stderr=""
                )
            if args[:1] == ("rev-list",):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout="0\n", stderr=""
                )
            raise AssertionError(f"unexpected git call: {args}")

        with (
            patch.object(ltvm_cli, "_ltvm_repo_root", return_value=repo),
            patch.object(
                ltvm_cli, "_current_version", return_value="0.10.aaaa"
            ),
            patch.object(ltvm_cli, "_git", side_effect=fake_git),
        ):
            rc = cmd_update(_make_args(check=True, json=True))
        assert rc == EXIT_OK
        result = json.loads(capsys.readouterr().out)
        assert result["update_available"] is False
        assert result["behind"] == 0

    def test_pull_failure_returns_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _fake_repo(tmp_path)

        def fake_git(repo_arg: Path, *args: str, check: bool = True):
            if args[:1] == ("status",):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout="", stderr=""
                )
            if args[:1] == ("fetch",):
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout="", stderr=""
                )
            if args[:1] == ("pull",):
                raise subprocess.CalledProcessError(
                    returncode=1,
                    cmd=["git", "pull"],
                    stderr="fatal: not a fast-forward",
                )
            raise AssertionError(f"unexpected git call: {args}")

        with (
            patch.object(ltvm_cli, "_ltvm_repo_root", return_value=repo),
            patch.object(
                ltvm_cli, "_current_version", return_value="0.10.aaaa"
            ),
            patch.object(ltvm_cli, "_git", side_effect=fake_git),
        ):
            rc = cmd_update(_make_args())
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "fast-forward" in err

    def test_fetch_failure_in_check(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _fake_repo(tmp_path)

        def fake_git(repo_arg: Path, *args: str, check: bool = True):
            if args[:1] == ("fetch",):
                raise subprocess.CalledProcessError(
                    returncode=1,
                    cmd=["git", "fetch"],
                    stderr="fatal: unable to access remote",
                )
            raise AssertionError(f"unexpected git call: {args}")

        with (
            patch.object(ltvm_cli, "_ltvm_repo_root", return_value=repo),
            patch.object(
                ltvm_cli, "_current_version", return_value="0.10.aaaa"
            ),
            patch.object(ltvm_cli, "_git", side_effect=fake_git),
        ):
            rc = cmd_update(_make_args(check=True))
        assert rc == EXIT_ERROR
        assert "git fetch failed" in capsys.readouterr().err


class TestVersionRefreshAfterUpdate:
    """`ltvm update` must report the version it just pulled.

    ltvm_pkg imports _build_info at startup to compute __version__, so
    _compute_version() returned the cached module's BUILD_HASH -- and
    cmd_update calls it right after rewriting _build_info.py with the
    post-pull hash.  The result was "Already up to date at <old>"
    after a real fast-forward, and --json reporting changed=false with
    a stale new_version.  Every existing test patched
    _current_version with a side_effect list, so none could see it.
    """

    def test_refresh_picks_up_rewritten_build_info(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        import importlib
        import sys as _sys

        import ltvm_pkg

        pkg_dir = Path(ltvm_pkg.__file__).parent
        build_info = pkg_dir / "_build_info.py"
        original = build_info.read_text() if build_info.exists() else None

        try:
            build_info.write_text('BUILD_HASH = "aaaaaaa"\n')
            # Prime both caches the way package import does.
            _sys.modules.pop("ltvm_pkg._build_info", None)
            importlib.import_module("ltvm_pkg._build_info")
            assert ltvm_pkg._compute_version(refresh=True).endswith(
                ".aaaaaaa"
            )

            # cmd_update rewrites the file with the post-pull hash.
            # Same length as the previous content, written within the
            # same second -- so a .pyc revalidated on (mtime, size) is
            # still considered current, which is why re-importing is
            # not enough and the refresh path reads the file.
            build_info.write_text('BUILD_HASH = "bbbbbbb"\n')

            # The already-imported module still holds the old hash...
            assert ltvm_pkg._compute_version().endswith(".aaaaaaa")
            # ...and refresh reports what is actually on disk.
            assert ltvm_pkg._compute_version(refresh=True).endswith(
                ".bbbbbbb"
            )
        finally:
            if original is not None:
                build_info.write_text(original)
            elif build_info.exists():
                build_info.unlink()
            _sys.modules.pop("ltvm_pkg._build_info", None)
