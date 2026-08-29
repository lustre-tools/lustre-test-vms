"""cmd_cluster dispatch: create / destroy / deploy / status / exec /
list / ssh over ``ltvm_pkg.vm_cluster``.

Parses cluster-subcommand flags out of ``args.cluster_args`` (which
the top-level parser treats as opaque ``*cluster_args``), routes to
the corresponding vm_cluster function via a small ``_call`` adapter
that turns SystemExit into an int exit code.
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


def _require_root(*a: Any, **kw: Any) -> Any:
    """Thunk to ltvm_pkg.cli._require_root so tests patching it on
    the package attribute still gate cluster subcommands."""
    import ltvm_pkg.cli as _cli

    return _cli._require_root(*a, **kw)


def cmd_cluster(args: argparse.Namespace) -> int:
    use_json = args.json
    action = args.action
    cargs = args.cluster_args

    from ltvm_pkg.vm_cluster import (
        cmd_cluster_create as _qc_create,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_deploy as _qc_deploy,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_destroy as _qc_destroy,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_exec as _qc_exec,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_list as _qc_list,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_ssh as _qc_ssh,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_status as _qc_status,
    )

    def _call(fn: Any, ns: argparse.Namespace) -> int:
        try:
            fn(ns)
            return EXIT_OK
        except SystemExit as e:
            return int(e.code) if e.code is not None else EXIT_ERROR

    if action == "create":
        err = _require_root(use_json)
        if err is not None:
            return err
        if len(cargs) < 2:
            return _error(
                "cluster create requires a name and at least one node spec",
                use_json,
                hint="ltvm cluster create <name> [--target TARGET] "
                "[--arch ARCH] [--vcpus N] [--mem MB] "
                "<role:vm[:disks]> ...",
            )
        # Parse optional flags out of cargs; remaining positionals are
        # name + node specs.
        vcpus = 2
        # mem=None means "let cmd_create resolve from os_arts.default_mem"
        # so cluster nodes inherit the per-target default (e.g. rocky10
        # needs 4096) instead of being silently overridden.
        mem: int | None = None
        os_target: str | None = None
        arch: str | None = None
        disk_size: str | None = None
        nics: list[str] = []
        owner_id: str | None = None
        positional: list[str] = []
        i = 0
        while i < len(cargs):
            if cargs[i] == "--vcpus" and i + 1 < len(cargs):
                vcpus = int(cargs[i + 1])
                i += 2
            elif cargs[i] == "--mem" and i + 1 < len(cargs):
                mem = int(cargs[i + 1])
                i += 2
            elif cargs[i] == "--target" and i + 1 < len(cargs):
                os_target = cargs[i + 1]
                i += 2
            elif cargs[i] == "--arch" and i + 1 < len(cargs):
                arch = cargs[i + 1]
                i += 2
            elif cargs[i] == "--disk-size" and i + 1 < len(cargs):
                disk_size = cargs[i + 1]
                i += 2
            elif cargs[i] == "--nic" and i + 1 < len(cargs):
                # --nic is repeatable and applies uniformly to every
                # node in the cluster.  Validation happens inside each
                # node's `ltvm create`, so a bad value (e.g. softroce)
                # surfaces per-node with the usual follow-up-issue hint.
                nics.append(cargs[i + 1])
                i += 2
            elif (
                cargs[i] in ("--owner", "--owner-id")
                and i + 1 < len(cargs)
            ):
                owner_id = cargs[i + 1]
                i += 2
            elif cargs[i].startswith("--"):
                return _error(
                    f"cluster create: unknown argument '{cargs[i]}'",
                    use_json,
                    hint="valid: --vcpus, --mem, --target, --arch, "
                    "--disk-size, --nic, --owner-id",
                )
            else:
                positional.append(cargs[i])
                i += 1
        if len(positional) < 2:
            return _error(
                "cluster create requires a name and at least one node spec",
                use_json,
                hint="ltvm cluster create <name> [TARGET | --target TARGET] "
                "[--arch ARCH] [--vcpus N] [--mem MB] "
                "<role:vm[:disks]> ...",
            )
        # Accept a positional target after the cluster name: any
        # bare token (no ':') between the name and the first node
        # spec is treated as the OS target.  Node specs always
        # contain ':' (role:vm[:disks]) so this is unambiguous.  If
        # both the positional and --target are given, they must agree.
        pos_target: str | None = None
        if len(positional) >= 2 and ":" not in positional[1]:
            pos_target = positional[1]
            positional = [positional[0]] + positional[2:]
            if len(positional) < 2:
                return _error(
                    "cluster create requires at least one node spec",
                    use_json,
                    hint="ltvm cluster create <name> "
                    "[TARGET | --target TARGET] <role:vm[:disks]> ...",
                )
        if pos_target is not None and os_target is not None \
                and pos_target != os_target:
            return _error(
                f"--target {os_target!r} conflicts with positional "
                f"target {pos_target!r}; pass only one",
                use_json,
            )
        final_target = pos_target if pos_target is not None else os_target
        return _call(
            _qc_create,
            _qemu_ns(
                name=positional[0],
                nodes=positional[1:],
                vcpus=vcpus,
                mem=mem,
                os=final_target,
                arch=arch,
                disk_size=disk_size,
                nic=nics,
                owner_id=owner_id,
            ),
        )

    if action == "destroy":
        err = _require_root(use_json)
        if err is not None:
            return err
        if not cargs:
            return _error("cluster destroy requires a name", use_json)
        return _call(_qc_destroy, _qemu_ns(name=cargs[0]))

    if action == "deploy":
        if not cargs:
            return _error("cluster deploy requires a name", use_json)
        name = cargs[0]
        build_path = "."
        mount = False
        server_only = False
        force_compat = False
        i = 1
        while i < len(cargs):
            if cargs[i] == "--build" and i + 1 < len(cargs):
                build_path = cargs[i + 1]
                i += 2
            elif cargs[i] == "--mount":
                mount = True
                i += 1
            elif cargs[i] == "--server-only":
                server_only = True
                i += 1
            elif cargs[i] == "--force-compat":
                force_compat = True
                i += 1
            else:
                return _error(
                    f"cluster deploy: unknown argument '{cargs[i]}'",
                    use_json,
                    hint="valid: --build PATH, --mount, --server-only, "
                    "--force-compat",
                )
        return _call(
            _qc_deploy,
            _qemu_ns(
                name=name,
                lustre_source=build_path,
                mount=mount,
                server_only=server_only,
                force_compat=force_compat,
            ),
        )

    if action == "status":
        if not cargs:
            return _error("cluster status requires a name", use_json)
        return _call(_qc_status, _qemu_ns(name=cargs[0]))

    if action == "exec":
        if len(cargs) < 3:
            return _error(
                "cluster exec requires a name, role, and command",
                use_json,
                hint="ltvm cluster exec <name> <role> '<cmd>'",
            )
        return _call(
            _qc_exec,
            _qemu_ns(
                name=cargs[0],
                target=cargs[1],
                command=cargs[2:],
                timeout=120,
                json=use_json,
            ),
        )

    if action == "list":
        return _call(_qc_list, _qemu_ns())

    if action == "ssh":
        if len(cargs) < 2:
            return _error(
                "cluster ssh requires a name and a target (role or vm name)",
                use_json,
                hint="ltvm cluster ssh <name> <role> [cmd...]",
            )
        return _call(
            _qc_ssh,
            _qemu_ns(name=cargs[0], target=cargs[1], command=cargs[2:]),
        )

    return _error(f"Unknown cluster action: {action}", use_json)
