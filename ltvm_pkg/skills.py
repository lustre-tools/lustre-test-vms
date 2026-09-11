"""Link this checkout's agent skills into the user's skill directories.

Claude Code reads ``~/.claude/skills``; Codex reads ``$CODEX_HOME/skills``
(default ``~/.codex/skills``).  Both load the same ``SKILL.md`` directory
format, so the one copy under ``skills/`` in this repo serves either.

Symlinks, not copies: ``git pull`` or ``ltvm update`` then updates the
skills along with the ltvm they describe, with no second install step.

``ltvm install`` runs as root, and root's ``~/.claude`` is not where
anyone reads skills from -- so the links go into the invoking human's
home, and anything created there is handed back to them.
"""

from __future__ import annotations

import logging
import os
import pwd
from pathlib import Path

from ltvm_pkg.priv import invoking_user

log = logging.getLogger("ltvm.skills")


def default_repo_root() -> Path:
    """The checkout this module was imported from.

    An installed ltvm is a symlink into a checkout, and ``resolve()``
    follows it, so this lands in the real tree either way.
    """
    return Path(__file__).resolve().parent.parent


def repo_skills(repo_root: Path) -> Path:
    """Where this checkout keeps its skills."""
    return Path(repo_root) / "skills"


def link_project_skills(repo_root: Path) -> None:
    """Point ``<repo>/.claude/skills`` at the visible ``skills/`` dir.

    Claude Code discovers project skills under ``.claude/skills``; the
    skills themselves are not hidden away there.  This pointer is local
    state (``.claude/`` is gitignored), so a session started inside the
    checkout finds them without anything being committed.
    """
    dot = Path(repo_root) / ".claude"
    link = dot / "skills"
    if link.exists() and not link.is_symlink():
        if any(link.iterdir()):
            return
        link.rmdir()
    dot.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        link.unlink()
    link.symlink_to(repo_skills(repo_root))


def _target_user() -> tuple[Path, int, int] | None:
    """(home, uid, gid) to install for, or None for "just us".

    None covers both the unprivileged case and a real root login, where
    ``Path.home()`` is already the right home and no chown is wanted.
    """
    user = invoking_user()
    if user is None:
        return None
    try:
        pw = pwd.getpwnam(user[0])
    except KeyError:
        return None
    return Path(pw.pw_dir), pw.pw_uid, pw.pw_gid


def skill_destinations(home: Path) -> list[Path]:
    """Skill directories to write, for the agents present on this host.

    Claude's is unconditional.  Codex's is added only when that
    installation exists, so a host without codex does not grow an empty
    ``~/.codex``.
    """
    dests = [home / ".claude" / "skills"]
    codex_home = os.environ.get("CODEX_HOME")
    codex = Path(codex_home) if codex_home else home / ".codex"
    if codex.is_dir():
        dests.append(codex / "skills")
    return dests


def _own(path: Path, uid: int | None, gid: int | None) -> None:
    if uid is None or gid is None:
        return
    try:
        os.chown(path, uid, gid, follow_symlinks=False)
    except OSError as e:  # not fatal: the link still works
        log.debug("could not chown %s: %s", path, e)


def install_skills(
    repo_root: Path,
    home: Path | None = None,
    uid: int | None = None,
    gid: int | None = None,
) -> dict:
    """Link every skill in this checkout into the user's skill dirs.

    Returns ``{"source", "linked": {dir: [names]}, "skipped": {...}}``.
    A destination entry that exists and is not a symlink is left alone:
    replacing it would delete a skill this installer did not create.
    """
    if home is None:
        target = _target_user()
        if target is not None:
            home, uid, gid = target
        else:
            home = Path.home()

    source = repo_skills(repo_root)
    result: dict = {"source": str(source), "linked": {}, "skipped": {}}
    if not source.is_dir():
        return result
    try:
        link_project_skills(repo_root)
    except OSError as e:  # a read-only checkout is not a failure
        log.debug("project skills pointer not made: %s", e)

    skills = sorted(p for p in source.iterdir() if (p / "SKILL.md").is_file())
    if not skills:
        return result

    for dest_dir in skill_destinations(home):
        linked: list[str] = []
        skipped: list[str] = []
        for parent in (dest_dir.parent, dest_dir):
            if not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
                _own(parent, uid, gid)
        for skill in skills:
            dest = dest_dir / skill.name
            if dest.exists() and not dest.is_symlink():
                skipped.append(skill.name)
                continue
            if dest.is_symlink():
                dest.unlink()
            dest.symlink_to(skill)
            _own(dest, uid, gid)
            linked.append(skill.name)
        if linked:
            result["linked"][str(dest_dir)] = linked
        if skipped:
            result["skipped"][str(dest_dir)] = skipped
    return result


def uninstall_skills(repo_root: Path, home: Path | None = None) -> dict:
    """Remove the links that point into this checkout, and nothing else."""
    if home is None:
        target = _target_user()
        home = target[0] if target is not None else Path.home()

    source = repo_skills(repo_root)
    result: dict = {"removed": {}}
    for dest_dir in skill_destinations(home):
        if not dest_dir.is_dir():
            continue
        removed: list[str] = []
        for dest in sorted(dest_dir.iterdir()):
            if not dest.is_symlink():
                continue
            try:
                points_at = Path(os.readlink(dest))
            except OSError:
                continue
            if source in points_at.parents:
                dest.unlink()
                removed.append(dest.name)
        if removed:
            result["removed"][str(dest_dir)] = removed
    return result


def link_status(repo_root: Path, home: Path | None = None) -> dict:
    """Which skills are linked, missing, or blocked, per destination.

    ``blocked`` is a destination that exists and is not a symlink --
    someone's own skill of the same name, which installing never
    replaces.  A symlink pointing at some other checkout counts as
    missing, because installing would repoint it.
    """
    if home is None:
        target = _target_user()
        home = target[0] if target is not None else Path.home()

    source = repo_skills(repo_root)
    status: dict = {"linked": {}, "missing": {}, "blocked": {}}
    if not source.is_dir():
        return status
    names = sorted(
        p.name for p in source.iterdir() if (p / "SKILL.md").is_file()
    )
    if not names:
        return status

    for dest_dir in skill_destinations(home):
        for name in names:
            dest = dest_dir / name
            if dest.is_symlink():
                key = (
                    "linked"
                    if Path(os.readlink(dest)) == source / name
                    else "missing"
                )
            elif dest.exists():
                key = "blocked"
            else:
                key = "missing"
            status[key].setdefault(str(dest_dir), []).append(name)
    return status


def describe(result: dict) -> list[str]:
    """Human-readable lines for an install or uninstall result."""
    lines: list[str] = []
    for where, names in sorted(result.get("linked", {}).items()):
        lines.append(f"Skills linked into {where}: {' '.join(names)}")
    for where, names in sorted(result.get("skipped", {}).items()):
        lines.append(
            f"Not linked into {where} (a real directory is already "
            f"there): {' '.join(names)}"
        )
    for where, names in sorted(result.get("removed", {}).items()):
        lines.append(f"Removed from {where}: {' '.join(names)}")
    if lines and "linked" in result:
        lines.append(
            "  (a running agent session does not see new skills; "
            "start a new one)"
        )
    return lines
