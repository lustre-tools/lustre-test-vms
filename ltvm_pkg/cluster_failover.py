"""Fail a server over to its partner, and fail it back: the host side of
`ltvm cluster failover/failback`.

The fault comes from the host, where a guest cannot reach: a SIGKILL of
QEMU, a QMP stop, a QMP set_link.  The facet moves go through the test
framework, from failover-facets.sh on the first client, so the record
of the active node that `fail <facet>` keeps stays correct.

Neither action waits for recovery.  In a test, recovery may never end,
and some clients may never come back; `cluster status` shows both.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import vm_claim
from .qemu_run import _signal_qemu, die, is_running
from .vm_net import run_ssh, sshpass_ssh_argv
from .vm_state import ClusterInfo, VMInfo, lustre_libdir

FACETS_SCRIPT = Path(__file__).parent / "failover-facets.sh"
_GUEST_SCRIPT = "/root/ltvm-failover-facets"

# How `cluster failover` takes the primary down.
HOWS = {
    "umount": "unmount the targets (a planned move)",
    "kill": "SIGKILL the QEMU process (power loss)",
    "panic": "crash the kernel; kdump saves a vmcore, then the node reboots",
    "freeze": "SIGSTOP the QEMU process (a hung node); resumed after the "
    "takeover.  Linux hosts only",
    "split-brain": "take the link down while the node runs, then try the "
    "takeover: MMP must refuse it.  Exit 0 when it did",
}

# Why `freeze` needs a Linux host.  SIGSTOP of QEMU is a hung node:
# the guest's clock keeps running, and when the guest resumes, ldiskfs
# and ZFS MMP see the gap and find the takeover.  On macOS a stopped QEMU
# reads no packets, and socket_vmnet then stalls every VM on the host.
# QMP stop with the link down keeps the network up there, but it stops
# the guest's clock too: the resumed node saw no gap, and on ldiskfs and
# on ZFS both nodes kept the targets mounted.
FREEZE_REFUSED = (
    "--how freeze needs a Linux host.  On macOS, SIGSTOP of QEMU stalls "
    "socket_vmnet for every VM, and QMP stop pauses the guest's clock, "
    "which hides the takeover from MMP (both nodes keep the targets).  "
    "Use --how kill"
)

# The mgmt NIC's netdev id (qemu_run.build_qemu_args).
_NETDEV = "net0"
_SSH_PROBE = 10


def node_facets(cluster: ClusterInfo) -> dict[str, list[str]]:
    """The facets each server node holds, MGS first, as local.sh names
    them.

    Read from generate_local_sh, so the indexes match the ones the
    framework uses.  OSTs in files (--ost-count) have no `_HOST` line
    there without their plan, and a partner cannot reach them anyway.
    """
    from .vm_cluster import generate_local_sh

    facets: dict[str, list[str]] = {}
    text = generate_local_sh(cluster)
    separate_mgs = "MGSDEV=" in text
    for m in re.finditer(r"^(mgs|mds\d+|ost\d+)_HOST=(\S+)$", text, re.M):
        facet, host = m.groups()
        if facet == "mgs" and not separate_mgs:
            continue
        facets.setdefault(host, []).append(facet)
    return facets


class _Pair:
    """A primary, its partner and the client that runs the framework."""

    def __init__(self, cluster: ClusterInfo, primary: str) -> None:
        partners = cluster.failover_partners()
        if primary not in partners:
            for p, q in partners.items():
                if primary == q:
                    die(
                        f"{primary} is the partner of {p}; name the "
                        f"primary: ltvm cluster failover {cluster.name} {p}"
                    )
            die(f"{primary} has no failover partner in '{cluster.name}'")
        self.cluster = cluster
        self.primary = VMInfo.load(primary)
        self.partner = VMInfo.load(partners[primary])
        self.facets = node_facets(cluster).get(primary, [])
        if not self.facets:
            die(f"{primary} holds no targets that can move")
        clients = cluster.client_nodes()
        if not clients:
            die(f"'{cluster.name}' has no client to run the test framework on")
        self.client = VMInfo.load(clients[0].name)
        from .vm_commands import _os_family_for_vm

        self.lustre = lustre_libdir(_os_family_for_vm(self.client, "failover"))

    def facets_cmd(
        self, action: str, facets: list[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        """Run failover-facets.sh on the client, and wait for its end.

        From a file, not from stdin: pdsh's ssh would read the rest of
        a script that came in on stdin.  Detached, and polled with short
        ssh calls: a frozen node on macOS makes socket_vmnet lose most
        packets, and one ssh session that lasts the whole move dies.
        """
        # Per run: bash reads a script as it runs, so a new copy must
        # not overwrite one that an earlier run still reads.
        run = f"{_GUEST_SCRIPT}.{os.getpid()}.{action}"
        args = " ".join(shlex.quote(a) for a in (self.lustre, action, *facets))
        # Only the job goes to the background, in braces: a bare `&`
        # takes the whole list, and the `cat` then loses its stdin.
        start = (
            f"cat > {run}.sh && rm -f {run}.rc && "
            f"{{ setsid nohup bash -c 'bash {run}.sh {args} "
            f"> {run}.out 2>&1 < /dev/null; echo $? > {run}.rc' "
            "> /dev/null 2>&1 < /dev/null & }"
        )
        try:
            r = subprocess.run(
                sshpass_ssh_argv(self.client.ip, start),
                input=FACETS_SCRIPT.read_text(),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                start, 124, "", f"cannot start {action} on {self.client.name}"
            )
        if r.returncode != 0:
            return r
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(2)
            try:
                p = run_ssh(
                    self.client.ip,
                    f"cat {run}.rc 2>/dev/null && cat {run}.out",
                    timeout=_SSH_PROBE,
                )
            except subprocess.TimeoutExpired:
                continue
            rc, _, out = (p.stdout or "").partition("\n")
            if p.returncode == 0 and rc.strip().isdigit():
                return subprocess.CompletedProcess(start, int(rc), out, "")
        # Stop the job: a later action must not run beside it.
        with contextlib.suppress(subprocess.TimeoutExpired):
            run_ssh(self.client.ip, f"pkill -f {run}.sh", timeout=_SSH_PROBE)
        return subprocess.CompletedProcess(
            start, 124, "", f"{action} timed out after {timeout}s; stopped it"
        )

    def active(self) -> dict[str, tuple[str, str]]:
        """facet -> (its active node by the framework's record, and
        whether that node has it mounted, unmounted or is unreachable)."""
        r = self.facets_cmd("active", self.facets, 120)
        if r.returncode != 0:
            die(
                f"cannot read the active nodes on {self.client.name}:\n"
                f"{(r.stdout + r.stderr).strip()}"
            )
        rows = (line.split() for line in r.stdout.splitlines())
        active = {row[0]: (row[1], row[2]) for row in rows if len(row) == 3}
        missing = [f for f in self.facets if f not in active]
        if missing:
            die(
                f"no active node for {', '.join(missing)} from "
                f"{self.client.name}:\n{(r.stdout + r.stderr).strip()}"
            )
        return active


def moved_facets(cluster: ClusterInfo) -> dict[str, list[str]]:
    """primary -> its facets that the partner holds, or may hold.

    A facet counts when the framework's record names the partner, and
    the partner has it mounted or does not answer.  A record left from a
    partner that unmounted everything (a deploy unloads Lustre) is
    harmless: the next init_test_env deletes it.
    """
    moved: dict[str, list[str]] = {}
    for primary, partner in cluster.failover_partners().items():
        for facet, (host, state) in _Pair(cluster, primary).active().items():
            if host == partner and state != "unmounted":
                moved.setdefault(primary, []).append(facet)
    return moved


def require_no_moved_facets(cluster: ClusterInfo, action: str) -> None:
    """Refuse *action* while a facet is on its failover partner.

    Every suite, llmount.sh and llmountcleanup.sh start with an
    init_test_env that deletes the records of the active node.  They
    then stop, format or mount each facet on its primary, while the
    partner still holds it.
    """
    moved = moved_facets(cluster)
    if not moved:
        return
    lines = "\n".join(
        f"  ltvm cluster failback {cluster.name} {primary}   "
        f"# {', '.join(facets)}"
        for primary, facets in moved.items()
    )
    die(
        f"cannot {action}: targets are on their failover partners, and "
        f"the test framework would act on the primaries.  Fail back "
        f"first:\n{lines}"
    )


def _qmp(vm: VMInfo, command: str, **arguments: Any) -> Any:
    from .vm_commands import qmp_execute

    return qmp_execute(vm.socket_path, command, arguments or None)


def _ssh_up(vm: VMInfo) -> bool:
    try:
        # A probe in a polling loop: the loop retries, not run_ssh.
        return (
            run_ssh(
                vm.ip, "true", timeout=_SSH_PROBE, retry_connect=False
            ).returncode
            == 0
        )
    except subprocess.TimeoutExpired:
        return False


def _wait_ssh(vm: VMInfo, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while not _ssh_up(vm):
        if time.monotonic() > deadline:
            die(f"{vm.name} did not answer ssh in {timeout}s")
        time.sleep(2)


def _signal(vm: VMInfo, sig: int, what: str) -> None:
    if not _signal_qemu(vm, sig):
        die(
            f"cannot send {signal.Signals(sig).name} to the QEMU of {vm.name}; {what}"
        )


def _fault(pair: _Pair, how: str, timeout: int) -> None:
    vm = pair.primary
    if how == "umount":
        r = pair.facets_cmd("stop", pair.facets, timeout)
        if r.returncode != 0:
            die(
                f"unmount on {vm.name} failed:\n{(r.stdout + r.stderr).strip()}"
            )
    elif how == "kill":
        _signal(vm, signal.SIGKILL, "it still runs, and nothing moved")
    elif how == "panic":
        try:
            run_ssh(vm.ip, "echo c > /proc/sysrq-trigger", timeout=5)
        except subprocess.TimeoutExpired:
            pass  # the panic took ssh down with it
    elif how == "freeze":
        # The link goes down first: the partner then sees the node stop
        # answering at the same moment as it stops.
        _qmp(vm, "set_link", name=_NETDEV, up=False)
        if not _signal_qemu(vm, signal.SIGSTOP):
            _qmp(vm, "set_link", name=_NETDEV, up=True)
            die(f"cannot send SIGSTOP to the QEMU of {vm.name}; nothing moved")
    elif how == "split-brain":
        _qmp(vm, "set_link", name=_NETDEV, up=False)


def _resume(vm: VMInfo, how: str) -> None:
    if how == "freeze":
        # Before any QMP command: a stopped QEMU does not answer.  A
        # SIGCONT to a QEMU that runs does nothing.
        _signal(vm, signal.SIGCONT, "it stays stopped")
    _qmp(vm, "set_link", name=_NETDEV, up=True)


def _report(result: dict[str, Any], use_json: bool) -> None:
    if use_json:
        print(json.dumps(result, indent=2))
        return
    for line in result["output"].splitlines():
        print(f"  {line}")
    if "split_brain" in result:
        split = result["split_brain"]
        state = (
            f"SPLIT BRAIN: {', '.join(split)} mounted on both nodes"
            if split
            else "refused by MMP, as expected"
        )
    elif result["rc"] == 0:
        state = "mounted"
    else:
        state = f"FAILED (rc={result['rc']})"
    print(
        f"{result['action']} {result['from']} -> {result['to']}: "
        f"{', '.join(result['facets'])} {state} in {result['mount_secs']}s"
    )


def _require(cluster: ClusterInfo) -> None:
    vm_claim.require_all(
        [n.name for n in cluster.get_nodes()], "fail over targets on"
    )


def cmd_cluster_failover(args: argparse.Namespace) -> None:
    use_json = bool(getattr(args, "json", False))
    if args.how == "freeze" and sys.platform == "darwin":
        die(FREEZE_REFUSED)
    cluster = ClusterInfo.load(args.name)
    _require(cluster)
    pair = _Pair(cluster, args.node)
    for vm in (pair.primary, pair.partner, pair.client):
        if not is_running(vm):
            die(f"{vm.name} is not running")
    active = pair.active()
    moved = [
        f for f in pair.facets if active.get(f, ("",))[0] == pair.partner.name
    ]
    if moved:
        die(
            f"{', '.join(moved)} already on {pair.partner.name}; run "
            f"ltvm cluster failback {cluster.name} {pair.primary.name}"
        )

    t0 = time.monotonic()
    _fault(pair, args.how, args.timeout)
    t_fault = time.monotonic()
    r = pair.facets_cmd("move", pair.facets, args.timeout)
    t_mount = time.monotonic()
    rc = r.returncode
    extra: dict[str, Any] = {}
    if args.how == "split-brain" and rc != 124:
        # The expected result is a refusal.  Any facet that the partner
        # mounted while the primary runs is a split brain.
        after = pair.active()
        split = [
            f for f in pair.facets if after[f] == (pair.partner.name, "mounted")
        ]
        extra = {"split_brain": split}
        rc = 1 if split else 0
    resumed = False
    if args.how in ("freeze", "split-brain") and not args.hold:
        _resume(pair.primary, args.how)
        resumed = True

    _report(
        {
            "cluster": cluster.name,
            "action": "failover",
            "how": args.how,
            "from": pair.primary.name,
            "to": pair.partner.name,
            "facets": pair.facets,
            "rc": rc,
            "fault_secs": round(t_fault - t0, 1),
            "mount_secs": round(t_mount - t_fault, 1),
            "primary_resumed": resumed,
            **extra,
            "output": (r.stdout + r.stderr).strip(),
        },
        use_json,
    )
    if rc != 0:
        raise SystemExit(1)


def _unfault(vm: VMInfo, timeout: int) -> None:
    """Bring the primary back from any fault that failover left on it."""
    from .vm_cluster import _ltvm, _sudo_prefix

    if not is_running(vm):
        r = subprocess.run(
            [*_sudo_prefix(), *_ltvm(), "start", vm.name],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            die(f"cannot start {vm.name}:\n{(r.stdout + r.stderr).strip()}")
    else:
        _resume(vm, "freeze")
    _wait_ssh(vm, timeout)


def cmd_cluster_failback(args: argparse.Namespace) -> None:
    use_json = bool(getattr(args, "json", False))
    cluster = ClusterInfo.load(args.name)
    _require(cluster)
    pair = _Pair(cluster, args.node)
    for vm in (pair.partner, pair.client):
        if not is_running(vm):
            die(f"{vm.name} is not running")
    _unfault(pair.primary, args.timeout)
    active = pair.active()
    # On the partner: move back.  On the primary but mounted nowhere (a
    # failed move): mount there.
    back = [f for f in pair.facets if active[f][0] == pair.partner.name]
    remount = [
        f for f in pair.facets if active[f] == (pair.primary.name, "unmounted")
    ]
    if not back and not remount:
        # After --hold, the failback only had to restore the primary.
        print(f"every target of {pair.primary.name} is mounted on it")
        return

    # A primary that froze or lost its link may still hold the targets
    # it lost.  Drop them, or its mount would find them busy.
    if back:
        r = pair.facets_cmd("drop", back, 120)
        if r.returncode != 0:
            die(
                f"cannot drop the old mounts on {pair.primary.name} "
                f"(rc={r.returncode}); restart it: ltvm stop --force "
                f"{pair.primary.name} && ltvm start {pair.primary.name}\n"
                f"{(r.stdout + r.stderr).strip()}"
            )

    t0 = time.monotonic()
    rc = 0
    output = []
    for action, facets in (("stop", back), ("move", back), ("start", remount)):
        if not facets:
            continue
        r = pair.facets_cmd(action, facets, args.timeout)
        output.append((r.stdout + r.stderr).strip())
        if r.returncode != 0:
            rc = r.returncode
            if action == "stop":
                break
    t_mount = time.monotonic()
    _report(
        {
            "cluster": cluster.name,
            "action": "failback",
            "from": pair.partner.name,
            "to": pair.primary.name,
            "facets": back + remount,
            "rc": rc,
            "mount_secs": round(t_mount - t0, 1),
            "output": "\n".join(o for o in output if o),
        },
        use_json,
    )
    if rc != 0:
        raise SystemExit(1)
