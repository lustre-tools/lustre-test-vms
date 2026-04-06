"""CLI argument parsing and dispatch for vm.py."""

from __future__ import annotations

import argparse
import sys

from .cluster import cmd_cluster
from .commands import (
    cmd_cp_from,
    cmd_cp_to,
    cmd_crash_collect,
    cmd_create,
    cmd_destroy,
    cmd_dmesg,
    cmd_doctor,
    cmd_ensure,
    cmd_exec,
    cmd_list,
    cmd_log,
    cmd_lustre_log,
    cmd_restart,
    cmd_restore,
    cmd_snapshot,
    cmd_ssh,
    cmd_start,
    cmd_start_all,
    cmd_status,
    cmd_stop,
    cmd_stop_all,
)
from .models import EXIT_NOT_FOUND, VMNotFound
from .process import die


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vm.py",
        description="QEMU microVM manager for Lustre testing",
    )
    sub = p.add_subparsers(dest="subcmd", metavar="COMMAND")

    # create
    c = sub.add_parser("create", help="Create and start a new VM")
    c.add_argument("name", nargs="?", default="")
    c.add_argument("--name", dest="name_flag", default="")
    c.add_argument("--vcpus", type=int, default=2)
    c.add_argument("--mem", type=int, default=2048)
    c.add_argument("--ip", default="")
    c.add_argument("--rootfs", default="")
    c.add_argument(
        "--image",
        default="",
        help="Base image path (default: rocky9-base.ext4)",
    )
    c.add_argument(
        "--kernel",
        default="",
        help="Kernel path (default: vmlinux)",
    )
    c.add_argument("--mdt-disks", type=int, default=0)
    c.add_argument("--ost-disks", type=int, default=0)

    # ensure
    c = sub.add_parser(
        "ensure",
        help="Idempotent: create/start as needed",
    )
    c.add_argument("name")
    c.add_argument("--vcpus", type=int, default=2)
    c.add_argument("--mem", type=int, default=2048)
    c.add_argument("--mdt-disks", type=int, default=0)
    c.add_argument("--ost-disks", type=int, default=0)
    c.add_argument(
        "--image",
        default="",
        help="Base image path (default: rocky9-base.ext4)",
    )
    c.add_argument(
        "--kernel",
        default="",
        help="Kernel path (default: vmlinux)",
    )
    c.add_argument("--json", action="store_true")

    # start
    c = sub.add_parser("start", help="Start stopped VM(s)")
    c.add_argument("names", nargs="+", metavar="NAME")

    # start-all
    sub.add_parser("start-all", help="Start all stopped VMs")

    # stop
    c = sub.add_parser("stop", help="Stop VM(s), keep disks")
    c.add_argument("names", nargs="+", metavar="NAME")

    # stop-all
    sub.add_parser("stop-all", help="Stop all running VMs")

    # restart
    c = sub.add_parser("restart", help="Stop + start VM(s)")
    c.add_argument("names", nargs="+", metavar="NAME")

    # destroy
    c = sub.add_parser(
        "destroy",
        help="Stop + delete everything",
    )
    c.add_argument("names", nargs="+", metavar="NAME")
    c = sub.add_parser("rm", help=argparse.SUPPRESS)
    c.add_argument("names", nargs="+", metavar="NAME")

    # exec
    c = sub.add_parser(
        "exec",
        help="Run command with timeout + exit codes",
    )
    c.add_argument("--timeout", type=int, default=120)
    c.add_argument("--json", action="store_true")
    c.add_argument("name")
    c.add_argument("command", nargs=argparse.REMAINDER)

    # ssh
    c = sub.add_parser(
        "ssh",
        help="Interactive SSH (no timeout)",
    )
    c.add_argument("name")
    c.add_argument("command", nargs=argparse.REMAINDER)

    # cp-to
    c = sub.add_parser("cp-to", help="Copy file(s) to VM")
    c.add_argument("name")
    c.add_argument("src")
    c.add_argument("dest")
    c = sub.add_parser("push", help=argparse.SUPPRESS)
    c.add_argument("name")
    c.add_argument("src")
    c.add_argument("dest")

    # cp-from
    c = sub.add_parser("cp-from", help="Copy file(s) from VM")
    c.add_argument("name")
    c.add_argument("src")
    c.add_argument("dest")
    c = sub.add_parser("pull", help=argparse.SUPPRESS)
    c.add_argument("name")
    c.add_argument("src")
    c.add_argument("dest")

    # list
    c = sub.add_parser(
        "list",
        help="Show all VMs + resource totals",
    )
    c.add_argument("--json", action="store_true")
    for alias in ("ls", "ps"):
        c = sub.add_parser(alias, help=argparse.SUPPRESS)
        c.add_argument("--json", action="store_true")

    # status
    c = sub.add_parser("status", help="Health check")
    c.add_argument("--json", action="store_true")
    c.add_argument("name")

    # log
    c = sub.add_parser("log", help="Tail serial console log")
    c.add_argument("name")
    c.add_argument("lines", type=int, nargs="?", default=50)

    # dmesg
    c = sub.add_parser("dmesg", help="Kernel log from VM")
    c.add_argument("--tail", type=int, default=200)
    c.add_argument("name")

    # lustre-log
    c = sub.add_parser("lustre-log", help="lctl dk from VM")
    c.add_argument("name")

    # snapshot
    c = sub.add_parser(
        "snapshot",
        help="Create qcow2 snapshot",
    )
    c.add_argument("name")
    c.add_argument("tag", nargs="?", default="")
    c = sub.add_parser("snap", help=argparse.SUPPRESS)
    c.add_argument("name")
    c.add_argument("tag", nargs="?", default="")

    # restore
    c = sub.add_parser(
        "restore",
        help="Restore snapshot (no tag = list)",
    )
    c.add_argument("name")
    c.add_argument("tag", nargs="?", default="")

    # crash-collect
    c = sub.add_parser(
        "crash-collect",
        help="Collect vmcore + run triage",
    )
    c.add_argument("name")
    c.add_argument(
        "--trigger",
        action="store_true",
        help="Crash the VM first via sysrq-trigger",
    )
    c.add_argument(
        "--mod-dir",
        help="Lustre build tree for triage symbols",
    )
    c.add_argument(
        "--outdir",
        default="/tmp",
        help="Output directory (default: /tmp)",
    )
    c.add_argument(
        "--wait",
        type=int,
        default=60,
        help="Seconds to wait for reboot (default: 60)",
    )

    # doctor
    c = sub.add_parser("doctor", help="Find/fix stale state")
    c.add_argument("--fix", action="store_true")

    # cluster
    c = sub.add_parser(
        "cluster",
        help="Multi-node cluster management",
    )
    csub = c.add_subparsers(
        dest="cluster_cmd",
        metavar="CMD",
    )

    cc = csub.add_parser("create", help="Create a cluster")
    cc.add_argument("name", help="Cluster name")
    cc.add_argument(
        "nodes",
        nargs="+",
        metavar="SPEC",
        help="Node specs: roles:vmname[:disks]",
    )
    cc.add_argument("--vcpus", type=int, default=2)
    cc.add_argument("--mem", type=int, default=4096)

    cc = csub.add_parser(
        "deploy",
        help="Deploy Lustre to cluster",
    )
    cc.add_argument("name", help="Cluster name")
    cc.add_argument(
        "--lustre-source",
        required=True,
        metavar="DIR",
        help=(
            "Path to a built Lustre source tree "
            "(must have configure.ac and built .ko files)"
        ),
    )
    cc.add_argument(
        "--mount",
        action="store_true",
        help="Run llmount.sh after deploy",
    )
    cc.add_argument(
        "--server-only",
        action="store_true",
        help="With --mount, skip client mount",
    )

    cc = csub.add_parser(
        "destroy",
        help="Destroy cluster and VMs",
    )
    cc.add_argument("name", help="Cluster name")

    cc = csub.add_parser("list", help="List clusters")

    cc = csub.add_parser("status", help="Cluster health")
    cc.add_argument("name", help="Cluster name")

    cc = csub.add_parser(
        "ssh",
        help="SSH to cluster node",
    )
    cc.add_argument("name", help="Cluster name")
    cc.add_argument("target", help="Node name or role")
    cc.add_argument("command", nargs=argparse.REMAINDER)

    cc = csub.add_parser(
        "exec",
        help="Exec on cluster node",
    )
    cc.add_argument("--timeout", type=int, default=120)
    cc.add_argument("name", help="Cluster name")
    cc.add_argument("target", help="Node name or role")
    cc.add_argument("command", nargs=argparse.REMAINDER)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.subcmd:
        parser.print_help()
        sys.exit(0)

    # Handle name from --name flag vs positional for create
    if args.subcmd == "create":
        args.name = args.name_flag or args.name

    # Alias handling
    cmd_map = {
        "rm": "destroy",
        "push": "cp-to",
        "pull": "cp-from",
        "ls": "list",
        "ps": "list",
        "snap": "snapshot",
    }
    cmd = cmd_map.get(args.subcmd, args.subcmd)

    if cmd != args.subcmd and cmd in ("list",):
        args.json = False

    try:
        dispatch = {
            "create": cmd_create,
            "ensure": cmd_ensure,
            "start": cmd_start,
            "start-all": cmd_start_all,
            "stop": cmd_stop,
            "stop-all": cmd_stop_all,
            "restart": cmd_restart,
            "destroy": cmd_destroy,
            "exec": cmd_exec,
            "ssh": cmd_ssh,
            "cp-to": cmd_cp_to,
            "cp-from": cmd_cp_from,
            "list": cmd_list,
            "status": cmd_status,
            "log": cmd_log,
            "dmesg": cmd_dmesg,
            "lustre-log": cmd_lustre_log,
            "snapshot": cmd_snapshot,
            "restore": cmd_restore,
            "crash-collect": cmd_crash_collect,
            "doctor": cmd_doctor,
            "cluster": cmd_cluster,
        }
        fn = dispatch.get(cmd)
        if fn:
            fn(args)
        else:
            parser.print_help()
    except VMNotFound as e:
        die(str(e), EXIT_NOT_FOUND)
