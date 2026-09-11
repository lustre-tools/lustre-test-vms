"""Tests for .githooks/bump-version -- the patch bump on commit."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BUMP = REPO_ROOT / ".githooks" / "bump-version"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A miniature ltvm checkout with one commit."""
    repo = tmp_path / "repo"
    (repo / "ltvm_pkg").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "ltvm"\nversion = "0.20.0"\n'
    )
    (repo / "ltvm_pkg" / "__init__.py").write_text('BASE_VERSION = "0.20"\n')
    (repo / "ltvm").write_text("#!/bin/bash\n")
    (repo / "docs" / "notes.md").write_text("hello\n")
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "Test")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "initial")
    return repo


def run_bump(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(BUMP)], cwd=repo, capture_output=True, text=True, check=False
    )


def version(repo: Path) -> str:
    line = (repo / "pyproject.toml").read_text().splitlines()[-1]
    return line.split('"')[1]


def test_a_code_change_bumps_the_patch(repo: Path) -> None:
    (repo / "ltvm_pkg" / "thing.py").write_text("x = 1\n")
    git(repo, "add", "ltvm_pkg/thing.py")

    assert run_bump(repo).returncode == 0

    assert version(repo) == "0.20.1"
    # The bump must be part of the commit it belongs to, not left behind.
    assert "pyproject.toml" in git(repo, "diff", "--cached", "--name-only")


def test_a_docs_only_change_does_not_bump(repo: Path) -> None:
    (repo / "docs" / "notes.md").write_text("more\n")
    git(repo, "add", "docs/notes.md")

    assert run_bump(repo).returncode == 0

    assert version(repo) == "0.20.0"


def test_a_test_only_change_does_not_bump(repo: Path) -> None:
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    git(repo, "add", "tests/test_x.py")

    assert run_bump(repo).returncode == 0

    assert version(repo) == "0.20.0"


def test_a_hand_edited_version_is_not_bumped_on_top(repo: Path) -> None:
    # A deliberate minor bump in the same commit is the committer's
    # decision; adding a patch bump to it would undo what they chose.
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "ltvm"\nversion = "0.21.0"\n'
    )
    (repo / "ltvm_pkg" / "__init__.py").write_text('BASE_VERSION = "0.21"\n')
    (repo / "ltvm_pkg" / "thing.py").write_text("x = 1\n")
    git(repo, "add", "-A")

    assert run_bump(repo).returncode == 0

    assert version(repo) == "0.21.0"


def test_a_drifting_base_version_is_refused(repo: Path) -> None:
    # ltvm --version reports BASE_VERSION; letting it disagree with
    # pyproject is how two versions end up in one release.
    (repo / "ltvm_pkg" / "__init__.py").write_text('BASE_VERSION = "0.19"\n')
    (repo / "ltvm_pkg" / "thing.py").write_text("x = 1\n")
    git(repo, "add", "-A")

    result = run_bump(repo)

    assert result.returncode != 0
    assert "disagree" in result.stderr
    assert version(repo) == "0.20.0"


def test_the_hook_is_executable_and_wired_in() -> None:
    assert BUMP.is_file()
    assert shutil.which(str(BUMP)) or BUMP.stat().st_mode & 0o111
    pre_commit = (REPO_ROOT / ".githooks" / "pre-commit").read_text()
    assert "bump-version" in pre_commit
