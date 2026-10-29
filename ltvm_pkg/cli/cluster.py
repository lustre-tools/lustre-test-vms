"""CLI layer for ``ltvm cluster``: create / destroy / start / stop /
deploy / llmount / status / failover / failback / exec / list / ssh over
``ltvm_pkg.vm_cluster``.

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
import os
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
    # The handlers read args.json, which _qemu_ns does not carry over.
    ns.json = use_json
    try:
        fn(ns)
        return EXIT_OK
    except SystemExit as e:
        return int(e.code) if e.code is not None else EXIT_ERROR
    except (ClusterNotFound, OSError, RuntimeError, ValueError) as e:
        # A child or saved profile can fail after its own cleanup completes.
        return _error(str(e), use_json)


def _handler(name: str) -> Any:
    """Fetch a vm_cluster handler by name at call time.

    Imported lazily, and by name, so tests can patch
    ``ltvm_pkg.vm_cluster.cmd_cluster_*`` and have the replacement seen
    here.
    """
    from ltvm_pkg import vm_cluster

    return getattr(vm_cluster, name)


def _cluster_privileges(use_json: bool, *, drop: bool) -> int | None:
    """Clusters need root unless the host runs VMs unprivileged.

    Under sudo, a create continues as the user who typed it (see
    cli.util._vm_privileges); a destroy stays root.
    """
    from ltvm_pkg import rootless

    if os.geteuid() == 0:
        if drop:
            rootless.drop_to_sudo_user()
        return None
    if rootless.ready():
        return None
    return _require_root(use_json)


def cmd_cluster_create(args: argparse.Namespace) -> int:
    """Create a cluster from node specs."""
    use_json = args.json
    from ltvm_pkg.vm_cluster import _requested_cluster_topology

    try:
        # Reject unsupported hosts and malformed profiles before asking for root.
        _requested_cluster_topology(args)
    except SystemExit as error:
        return int(error.code) if error.code is not None else EXIT_ERROR

    # A dry run only reads, so it must not demand root: prompting for a
    # password nothing will spend is the surest way to stop people using
    # it.
    if not args.dry_run:
        err = _cluster_privileges(use_json, drop=True)
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

    nodes = specs
    bad = [s for s in nodes if ":" not in s]
    if bad:
        return _error(
            f"not a node spec: {bad[0]!r}; give per-node targets with "
            f"--node-target VM=TARGET",
            use_json,
            hint="ltvm cluster create <name> [TARGET] <roles:vm[:disks]> "
            "... [--node-target VM=TARGET]",
        )

    # --node-target VM=TARGET gives one node its own target, so a cluster
    # can mix them, e.g. 4 KiB-page servers with a 16 KiB-page client;
    # --node-kernel VM=KERNEL its own kernel.  None means the cluster's.
    names = [s.split(":")[1] for s in nodes]
    per_node: dict[str, dict[str, str]] = {}
    for flag, what, items in (
        ("--node-target", "TARGET", args.node_target),
        ("--node-kernel", "KERNEL", args.node_kernel),
    ):
        given: dict[str, str] = {}
        for item in items or []:
            vm, sep, value = item.partition("=")
            if not sep or not vm or not value:
                return _error(f"{flag} {item!r}: expected VM={what}", use_json)
            if vm not in names:
                return _error(
                    f"{flag} {item!r}: no node {vm!r} in the node specs",
                    use_json,
                )
            if vm in given and given[vm] != value:
                return _error(
                    f"{flag} gives {vm!r} two values: "
                    f"{given[vm]!r} and {value!r}",
                    use_json,
                )
            given[vm] = value
        per_node[flag] = given
    node_targets: list[str | None] = [
        per_node["--node-target"].get(n) for n in names
    ]
    node_kernels: list[str | None] = [
        per_node["--node-kernel"].get(n) for n in names
    ]

    # --failover PARTNER=PRIMARY: PARTNER is a new node, so it is not
    # among the spec names; vm_cluster checks the rest.
    failover: dict[str, str] = {}
    for item in getattr(args, "failover", None) or []:
        partner, sep, primary = item.partition("=")
        if not sep or not partner or not primary:
            return _error(
                f"--failover {item!r}: expected PARTNER=PRIMARY", use_json
            )
        if partner in failover:
            return _error(f"--failover names {partner!r} twice", use_json)
        failover[partner] = primary

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
            nodes=nodes,
            node_targets=node_targets,
            node_kernels=node_kernels,
            failover=failover,
            vcpus=args.vcpus,
            # mem=None means "let cmd_create resolve the target's
            # default_mem" (rocky10 needs 4096), rather than silently
            # overriding it with a cluster-wide number.
            mem=args.mem,
            # vm_cluster reads the target as `os`.
            os=pos_target if pos_target is not None else flag_target,
            arch=args.arch,
            kernel=args.kernel,
            variant=args.variant,
            disk_size=args.disk_size,
            root_size=args.root_size,
            disk_dir=args.disk_dir,
            disk_cache=args.disk_cache,
            disk_prealloc=args.disk_prealloc,
            nic=list(args.nic or []),
            kernel_args=args.kernel_args,
            numa_nodes=args.numa_nodes,
            owner_id=args.owner_id,
            dry_run=args.dry_run,
            wait=args.wait,
        ),
        use_json,
    )


def cmd_cluster_destroy(args: argparse.Namespace) -> int:
    """Destroy clusters and every node in them."""
    use_json = args.json
    err = _cluster_privileges(use_json, drop=False)
    if err is not None:
        return err
    return _call(
        _handler("cmd_cluster_destroy"), _qemu_ns(names=args.names), use_json
    )


def _node_names(names: list[str]) -> list[str]:
    """Every node of the named clusters, in cluster order."""
    from ltvm_pkg.vm_state import ClusterInfo

    return [n.name for c in names for n in ClusterInfo.load(c).get_nodes()]


def cmd_cluster_start(args: argparse.Namespace) -> int:
    """Start every node of the named clusters, as `ltvm start` would."""
    try:
        nodes = _node_names(args.names)
    except (ClusterNotFound, RuntimeError) as e:
        return _error(str(e), args.json)
    from ltvm_pkg import vm_claim

    vm_claim.require_all(nodes, "start")
    from ltvm_pkg.cli.vm import cmd_vm_start

    return cmd_vm_start(_qemu_ns(names=nodes, wait=args.wait, json=args.json))


def cmd_cluster_stop(args: argparse.Namespace) -> int:
    """Stop every node of the named clusters, as `ltvm stop` would."""
    try:
        nodes = _node_names(args.names)
    except (ClusterNotFound, RuntimeError) as e:
        return _error(str(e), args.json)
    from ltvm_pkg import vm_claim

    vm_claim.require_all(nodes, "stop")
    from ltvm_pkg.cli.vm import cmd_vm_stop

    return cmd_vm_stop(
        _qemu_ns(
            names=nodes, json=args.json, force=getattr(args, "force", False)
        )
    )


def cmd_cluster_deploy(args: argparse.Namespace) -> int:
    """Build and deploy Lustre to every node in a cluster, or to its
    servers or its pure clients only."""
    use_json = args.json
    if args.server_only and args.client_only:
        return _error(
            "--server-only and --client-only exclude each other", use_json
        )
    return _call(
        _handler("cmd_cluster_deploy"),
        _qemu_ns(
            name=args.name,
            lustre_source=args.lustre_source,
            mount=args.mount,
            mount_timeout=args.mount_timeout,
            server_only=args.server_only,
            client_only=args.client_only,
            userspace_only=args.userspace_only,
            force_compat=args.force_compat,
            configure=args.configure,
            zfs=args.zfs,
            zfs_version=args.zfs_version,
            zfs_source=getattr(args, "zfs_source", None),
            fstype=args.fstype,
            ip_family=args.ip_family,
            ost_count=args.ost_count,
        ),
        use_json,
    )


def cmd_cluster_llmount(args: argparse.Namespace) -> int:
    """Mount or unmount Lustre across a cluster."""
    use_json = args.json
    if args.cleanup and args.server_only:
        return _error("--server-only has no effect with --cleanup", use_json)
    return _call(
        _handler("cmd_cluster_llmount"),
        _qemu_ns(
            name=args.name,
            cleanup=args.cleanup,
            server_only=args.server_only,
            timeout=args.timeout,
        ),
        use_json,
    )


def cmd_cluster_krb5(args: argparse.Namespace) -> int:
    """Set up Kerberos across a cluster, for sanity-krb5."""
    use_json = args.json
    return _call(
        _handler("cmd_cluster_krb5"),
        _qemu_ns(name=args.name, realm=args.realm, timeout=args.timeout),
        use_json,
    )


def cmd_cluster_status(args: argparse.Namespace) -> int:
    """Show a cluster's nodes and their state."""
    use_json = args.json
    return _call(
        _handler("cmd_cluster_status"),
        _qemu_ns(name=args.name, wait_recovery=args.wait_recovery),
        use_json,
    )


def cmd_cluster_failover(args: argparse.Namespace) -> int:
    """Move a server's targets to its failover partner."""
    use_json = args.json
    return _call(
        _handler("cmd_cluster_failover"),
        _qemu_ns(
            name=args.name,
            node=args.node,
            how=args.how,
            hold=args.hold,
            timeout=args.timeout,
        ),
        use_json,
    )


def cmd_cluster_failback(args: argparse.Namespace) -> int:
    """Move a server's targets back from its failover partner."""
    use_json = args.json
    return _call(
        _handler("cmd_cluster_failback"),
        _qemu_ns(name=args.name, node=args.node, timeout=args.timeout),
        use_json,
    )


def cmd_cluster_exec(args: argparse.Namespace) -> int:
    """Run a command on every node holding a role, or on one node."""
    use_json = args.json
    remote_command = getattr(args, "remote_command", args.command)
    if not remote_command:
        # An empty remainder is valid parser input but cannot run a guest command.
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
            command=list(remote_command),
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
    remote_command = getattr(args, "remote_command", args.command)
    return _call(
        _handler("cmd_cluster_ssh"),
        _qemu_ns(
            name=args.name,
            target=args.target,
            command=list(remote_command or []),
        ),
        use_json,
    )
