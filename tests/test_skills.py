"""Tests for ltvm_pkg/skills.py -- linking agent skills into a home."""

from __future__ import annotations

from pathlib import Path

import pytest

from ltvm_pkg import skills

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A checkout with two skills, one of them not a skill at all."""
    source = tmp_path / "repo" / "skills"
    for name in ("ltvm", "other"):
        (source / name).mkdir(parents=True)
        (source / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: x\n---\n"
        )
    (source / "notaskill").mkdir()
    return tmp_path / "repo"


class TestInstall:
    def test_every_skill_is_linked(
        self, fake_repo: Path, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        skills.install_skills(fake_repo, home=home)
        for name in ("ltvm", "other"):
            link = home / ".claude" / "skills" / name
            assert link.is_symlink()
            assert (link / "SKILL.md").is_file()

    def test_a_directory_without_a_skill_file_is_not_linked(
        self, fake_repo: Path, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        skills.install_skills(fake_repo, home=home)
        assert not (home / ".claude" / "skills" / "notaskill").exists()

    def test_installing_twice_is_a_no_op(
        self, fake_repo: Path, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        skills.install_skills(fake_repo, home=home)
        result = skills.install_skills(fake_repo, home=home)
        linked = result["linked"][str(home / ".claude" / "skills")]
        assert sorted(linked) == ["ltvm", "other"]

    def test_a_real_directory_is_never_replaced(
        self, fake_repo: Path, tmp_path: Path
    ) -> None:
        # Someone's own skill of the same name is their work, not ours.
        home = tmp_path / "home"
        mine = home / ".claude" / "skills" / "ltvm"
        mine.mkdir(parents=True)
        (mine / "SKILL.md").write_text("mine\n")

        result = skills.install_skills(fake_repo, home=home)

        assert (mine / "SKILL.md").read_text() == "mine\n"
        assert not mine.is_symlink()
        where = str(home / ".claude" / "skills")
        assert result["skipped"][where] == ["ltvm"]

    def test_codex_is_written_only_when_codex_exists(
        self, fake_repo: Path, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        skills.install_skills(fake_repo, home=home)
        assert not (home / ".codex").exists()

        (home / ".codex").mkdir(parents=True)
        skills.install_skills(fake_repo, home=home)
        assert (home / ".codex" / "skills" / "ltvm").is_symlink()

    def test_a_checkout_without_skills_is_not_an_error(
        self, tmp_path: Path
    ) -> None:
        result = skills.install_skills(tmp_path / "empty", home=tmp_path)
        assert result["linked"] == {}


class TestUninstall:
    def test_only_this_checkouts_links_are_removed(
        self, fake_repo: Path, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        skills.install_skills(fake_repo, home=home)
        dest = home / ".claude" / "skills"
        elsewhere = dest / "someone-elses"
        elsewhere.symlink_to(tmp_path)
        real = dest / "handwritten"
        real.mkdir()

        skills.uninstall_skills(fake_repo, home=home)

        assert not (dest / "ltvm").exists()
        assert elsewhere.is_symlink()
        assert real.is_dir()


class TestThisCheckout:
    """The skills this repo actually ships must be loadable."""

    def test_every_skill_declares_the_name_of_its_directory(self) -> None:
        source = skills.repo_skills(REPO_ROOT)
        found = [p for p in source.iterdir() if (p / "SKILL.md").is_file()]
        assert found, "this checkout ships no skills"
        for skill in found:
            front = (skill / "SKILL.md").read_text().split("---")[1]
            fields = dict(
                line.split(":", 1)
                for line in front.strip().splitlines()
                if ":" in line and not line.startswith(" ")
            )
            assert fields["name"].strip() == skill.name
            assert fields["description"].strip()
