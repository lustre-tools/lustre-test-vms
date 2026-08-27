"""Thin wrappers around ``ltvm_pkg.vm_commands`` for VM lifecycle /
observation commands.

Each cmd_* here just forwards args to the underlying vm_commands
function through ``_vm_call``, which normalizes SystemExit and
VMNotFound into the CLI's (int exit code) protocol. Lifecycle commands
prime sudo once and their implementation elevates only the individual
host operations that require it.
"""

from __future__ import annotations

import argparse
from typing import Any

from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_OK,
    _error,
)


def _vm_call(fn: Any, ns: argparse.Namespace, use_json: bool) -> int:
    """Call a vm_commands function, catching SystemExit and VMNotFound.

    Honors the return code of the wrapped function so handlers like
    cmd_doctor can signal "issues found" via a non-zero exit.
    """
    from ltvm_pkg.vm_state import VMNotFound

    try:
        rc = fn(ns)
        return rc if isinstance(rc, int) else EXIT_OK
    except SystemExit as e:
        return int(e.code) if e.code is not None else EXIT_ERROR
    except VMNotFound as e:
        return _error(str(e), use_json)
    except FileNotFoundError as e:
        return _error(str(e), use_json)


def _maybe_prime_sudo(reason: str, use_json: bool) -> None:
    if use_json:
        return
    from ltvm_pkg.priv import sudo_prime

    sudo_prime(reason)


def cmd_vm_start(args: argparse.Namespace) -> int:
    use_json = args.json
    _maybe_prime_sudo(
        "ltvm start needs root for tap setup", use_json
    )
    from ltvm_pkg.vm_commands import cmd_start as _start

    return _vm_call(_start, args, use_json)


def cmd_vm_stop(args: argparse.Namespace) -> int:
    use_json = args.json
    _maybe_prime_sudo(
        "ltvm stop needs root for tap teardown", use_json
    )
    from ltvm_pkg.vm_commands import cmd_stop as _stop

    return _vm_call(_stop, args, use_json)


def cmd_list(args: argparse.Namespace) -> int:
    use_json = args.json
    from ltvm_pkg.vm_commands import cmd_list as _list

    return _vm_call(_list, args, use_json)


def cmd_console_log(args: argparse.Namespace) -> int:
    use_json = args.json
    from ltvm_pkg.vm_commands import cmd_console_log as _log

    return _vm_call(_log, args, use_json)


def cmd_crash_collect(args: argparse.Namespace) -> int:
    use_json = args.json
    from ltvm_pkg.vm_commands import cmd_crash_collect as _crash_collect

    return _vm_call(_crash_collect, args, use_json)


def cmd_nmi(args: argparse.Namespace) -> int:
    use_json = args.json
    from ltvm_pkg.vm_commands import cmd_nmi as _nmi

    return _vm_call(_nmi, args, use_json)


def cmd_snapshot(args: argparse.Namespace) -> int:
    use_json = args.json
    from ltvm_pkg.vm_commands import cmd_snapshot as _snapshot

    return _vm_call(_snapshot, args, use_json)


def cmd_restore(args: argparse.Namespace) -> int:
    use_json = args.json
    from ltvm_pkg.vm_commands import cmd_restore as _restore

    return _vm_call(_restore, args, use_json)
