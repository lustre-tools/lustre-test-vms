"""Host-setup and self-update subcommands, plus the create / destroy
/ doctor thin wrappers (root-gated VM lifecycle entry points).

``cmd_setup`` wraps ``host_setup.run_setup`` / ``host_setup.verify``.
``cmd_update`` pulls the ltvm repo with --ff-only and refreshes
``_build_info.py`` so the next invocation reports the new git hash
without a reload.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from ltvm_pkg import host_setup

from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_OK,
    _error,
    _output,
)


def _require_root(*a: Any, **kw: Any) -> Any:
    """Thunk to ltvm_pkg.cli._require_root so tests patching it at the
    package level still gate create/destroy/doctor/update."""
    import ltvm_pkg.cli as _cli

    return _cli._require_root(*a, **kw)


def _vm_call(*a: Any, **kw: Any) -> int:
    """Thunk to ltvm_pkg.cli._vm_call (from cli.vm).

    Tests don't currently patch _vm_call itself, but going through the
    package attribute keeps the dispatch consistent with every other
    vm-dispatching command.
    """
    import ltvm_pkg.cli as _cli

    return _cli._vm_call(*a, **kw)


# ------------------------------------------------------------------
# VM lifecycle: root-gated create / destroy + doctor (read-only but
# installed root-gated historically).
# ------------------------------------------------------------------


def cmd_create(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_create as _create

    return _vm_call(_create, args, use_json)


def cmd_destroy(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_destroy as _destroy

    return _vm_call(_destroy, args, use_json)


def cmd_doctor(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_doctor as _doctor

    return _vm_call(_doctor, args, use_json)


# ------------------------------------------------------------------
# Subcommand: setup
# ------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    """Run host setup (QEMU, network, scripts, SSH)."""
    use_json = args.json

    # Collect requested steps
    explicit = []
    if args.qemu:
        explicit.append("qemu")
    if args.network:
        explicit.append("network")
    if args.install:
        explicit.append("install")
    if args.ssh:
        explicit.append("ssh")
    steps = explicit or None  # None = all

    if args.verify:
        try:
            results = host_setup.verify(subnet=args.subnet)
        except Exception as e:
            return _error(str(e), use_json)
        if use_json:
            print(json.dumps(results, indent=2))
        else:
            host_setup.print_verify(results)
        return EXIT_OK if results["all_ok"] else EXIT_ERROR

    try:
        host_setup.run_setup(
            steps=steps,
            subnet=args.subnet,
            force=getattr(args, "force", False),
        )
    except RuntimeError as e:
        return _error(str(e), use_json)
    except Exception as e:
        return _error(f"Setup failed: {e}", use_json)

    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: update
# ------------------------------------------------------------------


def _ltvm_repo_root() -> Path:
    """Return the on-disk repo root for this ltvm checkout.

    `ltvm install` symlinks the entry-point script into ``/usr/local/bin``
    and then resolves that symlink at startup, so when the user runs
    ``ltvm update`` from an installed copy we still load ``ltvm_pkg``
    from the real checkout.

    We read ``__file__`` off ``ltvm_pkg.cli`` (not this submodule)
    because a test flips ``ltvm_pkg.cli.__file__`` via ``patch.object``
    to simulate a symlinked install.  The cli module used to be
    ``ltvm_pkg/cli.py`` and now lives at ``ltvm_pkg/cli/__init__.py``,
    so the on-disk depth varies.  Walk up from the resolved path
    until we find the ``ltvm_pkg`` package dir and return its parent.
    """
    import ltvm_pkg.cli as _cli

    resolved = Path(_cli.__file__).resolve()
    for parent in resolved.parents:
        if parent.name == "ltvm_pkg":
            return parent.parent
    raise RuntimeError(
        f"cannot locate ltvm_pkg package directory above {resolved}"
    )


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess:
    """Run a git command against ``repo`` and return the CompletedProcess."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=60,
    )


def _current_version() -> str:
    """Return the version string, recomputing fresh from disk.

    ``ltvm_pkg.__version__`` is captured at import time, so after a
    successful update we recompute via ``_compute_version`` to pick up
    the new git hash without forcing a reload.
    """
    from ltvm_pkg import _compute_version

    return _compute_version()


def cmd_update(args: argparse.Namespace) -> int:
    """Pull the latest ltvm from the upstream git remote.

    Refuses to act on a dirty working tree unless --force is given.
    Uses --ff-only so we never silently create a merge commit on the
    user's checkout.  Reports the old and new version on success.
    """
    import ltvm_pkg.cli as _cli

    use_json = args.json
    # git pull writes into the checkout (.git/FETCH_HEAD, refs, ...).
    # In shared-install deployments the ltvm repo is owned by one user
    # (e.g. admin) and everyone else runs ltvm through PATH, so letting
    # the unprivileged caller hit this leaks a git permission error
    # mid-command.  Require root up front so sudo is the obvious fix.
    err = _require_root(use_json)
    if err is not None:
        return err
    repo = _cli._ltvm_repo_root()

    if not (repo / ".git").exists():
        return _error(
            f"{repo} is not a git checkout -- cannot update",
            use_json,
            hint="Reinstall ltvm by cloning "
            "https://github.com/lustre-tools/lustre-test-vms",
        )

    old_version = _cli._current_version()

    # --check: just report whether an update is available
    if getattr(args, "check", False):
        try:
            _cli._git(repo, "fetch", "--quiet")
        except subprocess.CalledProcessError as e:
            return _error(
                f"git fetch failed: {e.stderr.strip() or e}", use_json
            )
        try:
            behind = _cli._git(
                repo, "rev-list", "--count", "HEAD..@{u}"
            ).stdout.strip()
        except subprocess.CalledProcessError as e:
            return _error(
                f"git rev-list failed: {e.stderr.strip() or e}",
                use_json,
                hint="Is the current branch tracking an upstream?",
            )
        n = int(behind or "0")
        result = {
            "version": old_version,
            "behind": n,
            "update_available": n > 0,
        }
        _output(result, use_json)
        return EXIT_OK

    # Refuse on dirty working tree unless forced
    if not getattr(args, "force", False):
        status = _cli._git(repo, "status", "--porcelain").stdout
        if status.strip():
            return _error(
                "working tree has local changes -- refusing to update",
                use_json,
                hint="Commit or stash your changes, or pass --force",
            )

    try:
        _cli._git(repo, "fetch", "--quiet")
    except subprocess.CalledProcessError as e:
        return _error(f"git fetch failed: {e.stderr.strip() or e}", use_json)

    try:
        pull = _cli._git(repo, "pull", "--ff-only")
    except subprocess.CalledProcessError as e:
        return _error(
            f"git pull --ff-only failed: {e.stderr.strip() or e}",
            use_json,
            hint="The local branch has diverged from upstream. "
            "Resolve manually with git.",
        )

    # Refresh _build_info.py so the new short hash takes effect
    # immediately, even if the post-commit hook isn't installed.
    try:
        new_hash = _cli._git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
        if new_hash:
            (repo / "ltvm_pkg" / "_build_info.py").write_text(
                '"""Auto-generated by ltvm update. Do not edit or commit."""\n\n'
                f'BUILD_HASH = "{new_hash}"\n'
            )
    except (subprocess.CalledProcessError, OSError):
        # Non-fatal: version reporting will fall back to the runtime
        # git rev-parse path.
        pass

    new_version = _cli._current_version()

    result = {
        "old_version": old_version,
        "new_version": new_version,
        "changed": old_version != new_version,
        "git": pull.stdout.strip(),
    }
    if not use_json:
        if old_version == new_version:
            print(f"Already up to date at {new_version}")
        else:
            print(f"Updated ltvm: {old_version} -> {new_version}")
        if pull.stdout.strip():
            print(pull.stdout.strip())
    else:
        print(json.dumps(result, indent=2))
    return EXIT_OK
