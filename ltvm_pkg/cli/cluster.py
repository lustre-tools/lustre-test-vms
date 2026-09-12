"""CLI layer for ``ltvm cluster``: create / destroy / deploy / status /
exec / list / ssh over ``ltvm_pkg.vm_cluster``.

One thin wrapper per action, each reaching the matching vm_cluster
handler through a ``_call`` adapter that turns SystemExit into an int
exit code and reports a missing or corrupt cluster as an error rather
than a traceback.

These used to be one ``cmd_cluster`` that hand-parsed
``argparse.REMAINDER`` -- a while-loop per action matching flag names by
string, with its own "unknown argument" errors and a hand-maintained
list of valid flags in the hint.  The parser owns all of that now (see
the cluster block in ``ltvm``), which is why these are short.
"""

from __future__ import annotations

import argparse
from typing import Any

from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_OK,
    _error,
    _qemu_ns,
)
from ltvm_pkg.vm_state import ClusterNotFound


def _require_root(*a: Any, **kw: Any) -> int | None:
    """Thunk to ltvm_pkg.cli._require_root so tests patching it on
    the package attribute still gate cluster subcommands."""
    import ltvm_pkg.cli as _cli

    return _cli._require_root(*a, **kw)


def _call(fn: Any, ns: argparse.Namespace, use_json: bool) -> int:
    """Run a vm_cluster handler, mapping its failures to exit codes.

    Every action but `list` calls ClusterInfo.load(), which raises
    ClusterNotFound for a mistyped name and RuntimeError for a corrupt
    .cluster file.  Without this, `ltvm cluster status co99-nope`
    answered a typo with a Python traceback.  Mirrors _vm_call's
    VMNotFound handling in cli/vm.py.
    """
    try:
        fn(ns)
        return EXIT_OK
    except SystemExit as e:
        return int(e.code) if e.code is not None else EXIT_ERROR
    except (ClusterNotFound, RuntimeError) as e:
        return _error(str(e), use_json)


def _handler(name: str) -> Any:
    """Fetch a vm_cluster handler by name at call time.

    Imported lazily, and by name, so tests can patch
    ``ltvm_pkg.vm_cluster.cmd_cluster_*`` and have the replacement seen
    here.
    """
    from ltvm_pkg import vm_cluster

    return getattr(vm_cluster, name)


def cmd_cluster_create(args: argparse.Namespace) -> int:
    """Create a cluster from node specs."""
    use_json = args.json

    # A dry run only reads, so it must not demand root: prompting for a
    # password nothing will spend is the surest way to stop people using
    # it.
    if not args.dry_run:
        err = _require_root(use_json)
        if err is not None:
            return err

    # `specs` holds an optional OS target followed by the node specs.
    # A node spec always contains ':' (roles:name[:disks]) and a target
    # never does, which is what makes the split unambiguous -- see the
    # parser for why argparse cannot express it as two positionals.
    specs = list(args.specs)
    pos_target: str | None = None
    if specs and ":" not in specs[0]:
        pos_target = specs.pop(0)
        if not specs:
            return _error(
                "cluster create requires at least one node spec",
                use_json,
                hint="ltvm cluster create <name> [TARGET] "
                "<roles:vm[:disks]> ...",
            )

    flag_target = args.target
    if (
        pos_target is not None
        and flag_target is not None
        and pos_target != flag_target
    ):
        return _error(
            f"--target {flag_target!r} conflicts with positional "
            f"target {pos_target!r}; pass only one",
            use_json,
        )

    return _call(
        _handler("cmd_cluster_create"),
        _qemu_ns(
            name=args.name,
            nodes=specs,
            vcpus=args.vcpus,
            # mem=None means "let cmd_create resolve the target's
            # default_mem" (rocky10 needs 4096), rather than silently
            # overriding it with a cluster-wide number.
            mem=args.mem,
            # vm_cluster reads the target as `os`.
            os=pos_target if pos_target is not None else flag_target,
            arch=args.arch,
            disk_size=args.disk_size,
            nic=list(args.nic or []),
            owner_id=args.owner_id,
            dry_run=args.dry_run,
        ),
        use_json,
    )


def cmd_cluster_destroy(args: argparse.Namespace) -> int:
    """Destroy a cluster and every node in it."""
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    return _call(
        _handler("cmd_cluster_destroy"), _qemu_ns(name=args.name), use_json
    )


def cmd_cluster_deploy(args: argparse.Namespace) -> int:
    """Build and deploy Lustre to every node in a cluster."""
    use_json = args.json
    return _call(
        _handler("cmd_cluster_deploy"),
        _qemu_ns(
            name=args.name,
            lustre_source=args.lustre_source,
            mount=args.mount,
            server_only=args.server_only,
            force_compat=args.force_compat,
            zfs=args.zfs,
            zfs_version=args.zfs_version,
            fstype=args.fstype,
        ),
        use_json,
    )


def cmd_cluster_status(args: argparse.Namespace) -> int:
    """Show a cluster's nodes and their state."""
    use_json = args.json
    return _call(
        _handler("cmd_cluster_status"), _qemu_ns(name=args.name), use_json
    )


def cmd_cluster_exec(args: argparse.Namespace) -> int:
    """Run a command on every node holding a role, or on one node."""
    use_json = args.json
    if not args.command:
        return _error(
            "cluster exec requires a command to run",
            use_json,
            hint="ltvm cluster exec <name> <role> '<cmd>'",
        )
    return _call(
        _handler("cmd_cluster_exec"),
        _qemu_ns(
            name=args.name,
            target=args.target,
            command=list(args.command),
            timeout=args.timeout,
            json=use_json,
        ),
        use_json,
    )


def cmd_cluster_list(args: argparse.Namespace) -> int:
    """List all clusters."""
    return _call(_handler("cmd_cluster_list"), _qemu_ns(), args.json)


def cmd_cluster_ssh(args: argparse.Namespace) -> int:
    """Open an interactive ssh session on one node of a cluster."""
    use_json = args.json
    return _call(
        _handler("cmd_cluster_ssh"),
        _qemu_ns(
            name=args.name,
            target=args.target,
            command=list(args.command or []),
        ),
        use_json,
    )
