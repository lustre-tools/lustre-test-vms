"""ltvm -- Lustre test VM infrastructure package.

The version reported by ``ltvm --version`` is BASE_VERSION plus the short
commit hash of the checkout, e.g. ``0.10.abc1234``.

The hash is normally baked into ``ltvm_pkg/_build_info.py`` by the
``.githooks/post-commit`` hook (so we avoid shelling out to git on every
import).  When that file is missing -- fresh clone, hook not installed --
we fall back to ``git rev-parse --short HEAD`` against the repo root.
If git is unavailable (e.g. installed without a checkout) we report just
BASE_VERSION.

The base version is bumped by hand; the hash moves on every commit.
"""

from __future__ import annotations

import importlib
import re
import subprocess
from pathlib import Path

BASE_VERSION = "0.5"


def _git_short_hash() -> str | None:
    """Return the short HEAD hash for this checkout, or None on failure."""
    repo = Path(__file__).resolve().parent.parent
    if not (repo / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except (
        subprocess.SubprocessError,
        FileNotFoundError,
        OSError,
    ):
        return None
    out = result.stdout.strip()
    return out or None


def _read_baked_hash() -> str | None:
    """Parse BUILD_HASH straight out of _build_info.py.

    Used only on the post-update refresh path, where the module cache
    and the bytecode cache both hold the pre-update value.
    """
    path = Path(__file__).with_name("_build_info.py")
    try:
        text = path.read_text()
    except OSError:
        return None
    m = re.search(r'^BUILD_HASH\s*=\s*["\']([^"\']+)["\']', text, re.M)
    return m.group(1) if m else None


def _compute_version(refresh: bool = False) -> str:
    # Prefer the hash baked by the post-commit hook so importing this
    # package stays cheap.  The module is gitignored and may not exist
    # (fresh clone, hook not installed); use importlib so mypy doesn't
    # try to type-check a path that's missing on disk at install time.
    build_hash: str | None = None
    try:
        if refresh:
            # Read the file rather than importing it.  ltvm_pkg
            # imports _build_info at startup to compute __version__,
            # so import_module() hands back the already-loaded module
            # -- and cmd_update calls this right after rewriting
            # _build_info.py with the post-pull hash, so `ltvm update`
            # reported "Already up to date at <old>" after a real
            # fast-forward and --json reported changed=false.
            # Re-importing is not enough either: the file is one short
            # line, so pre- and post-update versions have identical
            # sizes, and a .pyc is revalidated on (mtime, size) -- two
            # writes within the same second keep serving the old hash
            # out of __pycache__.
            build_hash = _read_baked_hash()
        else:
            bi = importlib.import_module("ltvm_pkg._build_info")
            build_hash = getattr(bi, "BUILD_HASH", None)
    except ImportError:
        pass
    if build_hash:
        return f"{BASE_VERSION}.{build_hash}"
    h = _git_short_hash()
    if h:
        return f"{BASE_VERSION}.{h}"
    return BASE_VERSION


__version__ = _compute_version()
