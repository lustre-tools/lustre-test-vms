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
    _claim_error,
    _drop_to_vm_owner,
    _error,
    _uses_passthrough,
    _vm_privileges,
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


def cmd_vm_start(args: argparse.Namespace) -> int:
    use_json = args.json
    names = list(args.names)
    err = _claim_error(list(args.names), "start", use_json)
    if err is not None:
        return err
    err = _vm_privileges(
        "ltvm start needs root for tap setup",
        use_json,
        passthrough=_uses_passthrough(names),
        drop=lambda: _drop_to_vm_owner(names),
    )
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_start as _start

    return _vm_call(_start, args, use_json)


def cmd_vm_stop(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _claim_error(list(args.names), "stop", use_json)
    if err is not None:
        return err
    err = _vm_privileges(
        "ltvm stop needs root for tap teardown",
        use_json,
        passthrough=_uses_passthrough(list(args.names)),
    )
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


def cmd_exec(args: argparse.Namespace) -> int:
    use_json = args.json
    from ltvm_pkg.vm_commands import cmd_exec as _exec

    return _vm_call(_exec, args, use_json)


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


def cmd_vm_set(args: argparse.Namespace) -> int:
    use_json = args.json
    from ltvm_pkg.vm_commands import cmd_set as _set

    return _vm_call(_set, args, use_json)


def cmd_vm_link(args: argparse.Namespace) -> int:
    """Show or change the faults on a VM's links (shmrdmad)."""
    import json

    from ltvm_pkg import shmlink
    from ltvm_pkg.qemu_run import is_running
    from ltvm_pkg.vm_state import VMInfo, VMNotFound

    use_json = args.json
    try:
        vm = VMInfo.load(args.name)
    except VMNotFound as e:
        return _error(str(e), use_json)
    action = args.action
    changing = action in ("down", "up", "set")
    if changing and not args.nic:
        return _error(
            f"vm link {action} needs a NIC (eth0, eth1, ...)", use_json
        )
    if changing and not is_running(vm):
        return _error(f"VM '{vm.name}' is not running", use_json)
    opts = {
        "delay_ms": args.delay or 0,
        "loss_pct": args.loss or 0,
        "rate_mbit": args.rate or 0,
        "retry_ms": args.retry or 0,
    }
    if action != "set" and any(opts.values()) and action != "down":
        return _error(
            "--delay, --loss, --rate and --retry go with set or down", use_json
        )
    try:
        if action == "show":
            links = (
                [shmlink.link_of(vm, args.nic)]
                if args.nic
                else shmlink.links_of(vm)
            )
            have = (
                shmlink.rules(vm) if any(lk.on_switch for lk in links) else {}
            )
            off = {lk.nic for lk in links if not lk.on_switch}
            out = {lk.nic: have.get(lk.nic) for lk in links}
            if use_json:
                print(json.dumps(out))
            elif not out:
                print(f"{vm.name}: no links on shmrdmad")
            else:
                for nic, r in out.items():
                    if nic in off:
                        print(f"{vm.name} {nic}: not on shmrdmad")
                        continue
                    if not r:
                        print(f"{vm.name} {nic}: ok")
                        continue
                    what = []
                    if r.get("down") == "1":
                        what.append("down")
                    for k, unit in (
                        ("delay_ms", "ms delay"),
                        ("loss_pct", "% loss"),
                        ("rate_mbit", " Mbit/s"),
                        ("retry_ms", "ms retry"),
                    ):
                        if r.get(k, "0") != "0":
                            what.append(f"{r[k]}{unit}")
                    print(
                        f"{vm.name} {nic}: {', '.join(what)} "
                        f"(drops {r.get('drops')}, lost {r.get('lost')}, "
                        f"failed sends {r.get('fails')}, "
                        f"for {r.get('age_s')} s)"
                    )
            return EXIT_OK
        link = shmlink.link_of(vm, args.nic)
        if action == "up":
            had = shmlink.clear_fault(vm, link) if link.on_switch else False
            if link.netdev and not args.silent:
                shmlink.set_carrier(vm, link, True)
            if use_json:
                print(json.dumps({"cleared": had}))
            else:
                print(
                    f"{vm.name} {link.nic}: up"
                    + (
                        ""
                        if had or not link.on_switch
                        else " (it had no fault)"
                    )
                )
        else:
            rule = {}
            if link.on_switch and not args.carrier_only:
                rule = shmlink.set_fault(
                    vm, link, down=action == "down" or args.down, **opts
                )
            elif not link.netdev or action != "down":
                return _error(
                    f"{vm.name} {link.nic} is not on shmrdmad's switch: "
                    f"only `down --carrier-only` applies to it",
                    use_json,
                )
            if action == "down" and link.netdev and not args.silent:
                shmlink.set_carrier(vm, link, False)
            if use_json:
                print(json.dumps(rule))
            else:
                carrier = (
                    ""
                    if action != "down" or not link.netdev or args.silent
                    else ", carrier off"
                )
                print(f"{vm.name} {link.nic}: {action}{carrier}")
    except shmlink.LinkError as e:
        return _error(str(e), use_json)
    return EXIT_OK


def cmd_vm_shmrdmad(args: argparse.Namespace) -> int:
    """Status, start or stop of this user's shmrdmad."""
    import json

    from ltvm_pkg import shmrdma

    use_json = args.json
    action = args.action
    try:
        if action == "stop":
            msg = shmrdma.stop_daemon(force=args.force)
            if not use_json:
                print(msg)
        elif action == "start":
            shmrdma.ensure_daemon()
        st = shmrdma.status()
    except (RuntimeError, OSError) as e:
        return _error(str(e), use_json)
    if use_json:
        print(json.dumps(st))
    elif action != "stop":
        if not st["running"]:
            print("shmrdmad: not running (VMs that need it start it)")
            return EXIT_OK
        print(f"shmrdmad: running, pid {st['pid']}, user {st['user']}")
        print(f"  rdma socket: {st['rdma_socket']}")
        print(f"  net socket:  {st['net_socket']}")
        print(f"  link faults: {st['ctl_socket']} (ltvm vm link)")
        print(f"  log:         {st['log']}")
        if st["cpus"]:
            print(f"  cpus:        {st['cpus']}")
        if st["current"] is False:
            print("  built from older sources; stop it once no VM uses it")
        vms = st["vms"]
        print(f"  VMs:         {', '.join(vms) if vms else 'none'}")  # type: ignore[arg-type]
    return EXIT_OK
