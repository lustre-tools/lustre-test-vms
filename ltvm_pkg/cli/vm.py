"""Thin wrappers around ``ltvm_pkg.vm_commands`` for VM lifecycle /
observation commands.

Each cmd_* here just forwards args to the underlying vm_commands
function through ``_vm_call``, which normalizes SystemExit and
VMNotFound into the CLI's (int exit code) protocol.  ``_require_root``
is applied for commands that touch host resources (networking, QEMU
launch, snapshot/restore, NMI).
"""

from __future__ import annotations

import argparse
from typing import Any

from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_OK,
    _error,
)


def _require_root(*a: Any, **kw: Any) -> Any:
    """Thunk to ltvm_pkg.cli._require_root so tests patching it on
    the package attribute still affect cmd_* in this submodule."""
    import ltvm_pkg.cli as _cli

    return _cli._require_root(*a, **kw)


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


def cmd_vm_start(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_start as _start

    return _vm_call(_start, args, use_json)


def cmd_vm_stop(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
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
