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
import logging
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

log = logging.getLogger("ltvm.setup")


def _require_root(*a: Any, **kw: Any) -> int | None:
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
# VM lifecycle: create / destroy / doctor.
#
# These run as the invoking user; the per-operation sudo elevation
# happens inside vm_commands / vm_net / qemu_run via priv.sudo_run.
# ``sudo_prime`` here gives the user a single password prompt at the
# top instead of a scatter of mid-flow prompts.  JSON mode skips
# priming so an interactive password prompt can't clobber the
# structured output stream (sudo_run will still elevate ad-hoc).
# ------------------------------------------------------------------


def _maybe_prime_sudo(reason: str, use_json: bool) -> None:
    if use_json:
        return
    from ltvm_pkg.priv import sudo_prime

    sudo_prime(reason)


def cmd_create(args: argparse.Namespace) -> int:
    use_json = args.json
    _maybe_prime_sudo(
        "ltvm create needs root for bridge/tap/qemu-img writes",
        use_json,
    )
    from ltvm_pkg.vm_commands import cmd_create as _create

    return _vm_call(_create, args, use_json)


def cmd_destroy(args: argparse.Namespace) -> int:
    use_json = args.json
    _maybe_prime_sudo(
        "ltvm destroy needs root for tap teardown and VM_DIR cleanup",
        use_json,
    )
    from ltvm_pkg.vm_commands import cmd_destroy as _destroy

    return _vm_call(_destroy, args, use_json)


def cmd_doctor(args: argparse.Namespace) -> int:
    use_json = args.json
    _maybe_prime_sudo(
        "ltvm doctor needs root for tap/bridge inspection",
        use_json,
    )
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

    # Agent skills are a convenience, not part of the host setup: a
    # failure here must not fail an install that otherwise worked.
    if steps is None or "install" in active_steps(steps):
        try:
            _link_skills(use_json)
        except Exception as e:  # noqa: BLE001
            log.warning("skills not linked: %s", e)

    return EXIT_OK


def active_steps(steps: list[str] | None) -> list[str]:
    """The steps a setup run covers; None means all of them."""
    return steps if steps is not None else ["qemu", "network", "install", "ssh"]


def _link_skills(use_json: bool) -> None:
    from ltvm_pkg import skills

    result = skills.install_skills(_ltvm_repo_root())
    if use_json:
        return
    for line in skills.describe(result):
        print(line)


# ------------------------------------------------------------------
# Subcommand: skills
# ------------------------------------------------------------------


def cmd_skills(args: argparse.Namespace) -> int:
    """Link (or unlink) this checkout's agent skills."""
    from ltvm_pkg import skills

    use_json = args.json
    try:
        if getattr(args, "uninstall", False):
            result = skills.uninstall_skills(_ltvm_repo_root())
        else:
            result = skills.install_skills(_ltvm_repo_root())
    except Exception as e:  # noqa: BLE001
        return _error(f"Skills: {e}", use_json)

    if use_json:
        _output(result, True)
        return EXIT_OK
    lines = skills.describe(result)
    _output(lines or ["Nothing to do."], False)
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


def _current_version(refresh: bool = False) -> str:
    """Return the version string, recomputing fresh from disk.

    ``ltvm_pkg.__version__`` is captured at import time, so after a
    successful update we recompute via ``_compute_version`` to pick up
    the new git hash.  Pass ``refresh=True`` once the update has
    rewritten ``_build_info.py``: ltvm_pkg imported that module at
    startup, so without dropping the cached copy the "new" version is
    the old one and `ltvm update` reports "Already up to date" after a
    real fast-forward.
    """
    from ltvm_pkg import _compute_version

    return _compute_version(refresh=refresh)


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
        new_hash = _cli._git(
            repo, "rev-parse", "--short", "HEAD"
        ).stdout.strip()
        if new_hash:
            (repo / "ltvm_pkg" / "_build_info.py").write_text(
                '"""Auto-generated by ltvm update. Do not edit or commit."""\n\n'
                f'BUILD_HASH = "{new_hash}"\n'
            )
    except (subprocess.CalledProcessError, OSError):
        # Non-fatal: version reporting will fall back to the runtime
        # git rev-parse path.
        pass

    new_version = _cli._current_version(refresh=True)

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


# ------------------------------------------------------------------
# Subcommand: completion
# ------------------------------------------------------------------


def cmd_completion(args: argparse.Namespace) -> int:
    """Print -- or install -- the shell code that enables tab completion.

    `ltvm install` already installs for every shell on the host, so this
    exists for the cases that does not cover: a host installed before
    zsh/fish support, a shell whose completion dir appeared later, and
    anyone who wants it in their own dotfiles rather than system-wide.
    """
    from ltvm_pkg import shell_completion

    use_json = args.json
    shell = args.shell or shell_completion.detect_shell()

    if getattr(args, "uninstall", False):
        shells = (shell,) if args.shell else None
        results = shell_completion.uninstall(shells)
        return _report_completion(results, use_json, "Nothing to remove.")

    if getattr(args, "install", False):
        # An explicit --shell narrows the install to that one shell;
        # without it we cover whatever the host has.
        shells = (shell,) if args.shell else None
        results = shell_completion.install(
            shells, all_shells=getattr(args, "all_shells", False)
        )
        rc = _report_completion(results, use_json, "Nothing to install.")
        if rc == EXIT_OK and not use_json:
            _output(["", "Open a new shell to pick it up."], False)
        return rc

    try:
        code = shell_completion.shellcode(shell)
    except (ImportError, ValueError) as e:
        return _error(f"Completion: {e}", use_json)
    if use_json:
        _output({"shell": shell, "shellcode": code}, True)
        return EXIT_OK
    # Straight to stdout with no trailing commentary, so that
    # `eval "$(ltvm completion)"` and a redirect into a completion
    # directory both work.  Guidance goes to the log (stderr).
    print(code, end="" if code.endswith("\n") else "\n")
    log.info("%s", _completion_hint(shell))
    return EXIT_OK


def _completion_hint(shell: str) -> str:
    """How to actually use the code we just printed, per shell."""
    if shell == "fish":
        return (
            "save to ~/.config/fish/completions/ltvm.fish, "
            "or run `ltvm completion --install` as root"
        )
    if shell == "zsh":
        return (
            "save as _ltvm in a directory on your fpath (after compinit), "
            "or run `ltvm completion --install` as root"
        )
    return (
        'add `eval "$(ltvm completion)"` to ~/.bashrc, '
        "or run `ltvm completion --install` as root"
    )


def _report_completion(
    results: list[Any], use_json: bool, empty_msg: str
) -> int:
    """Render install/uninstall results; non-zero if anything failed."""
    if use_json:
        _output(
            [
                {
                    "shell": r.shell,
                    "path": str(r.path),
                    "status": r.status,
                    "detail": r.detail,
                }
                for r in results
            ],
            True,
        )
    else:
        lines = [
            f"{r.shell}: {r.status} {r.path}"
            + (f" ({r.detail})" if r.detail else "")
            for r in results
        ]
        _output(lines or [empty_msg], False)
    if any(r.status == "failed" for r in results):
        return EXIT_ERROR
    return EXIT_OK
