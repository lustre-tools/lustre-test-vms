"""ltvm suite: run a Lustre test suite on a VM or cluster, detached.

A suite gives honest results only when started the way CI starts it,
and each part of that recipe was learned from a run that went wrong:

- unmounted (llmountcleanup.sh first): a suite that finds the targets
  mounted takes the raw disks as each target's device instead of the
  dm-flakey mapper, and sanity 39r fails every time;
- CLEANUP_DM_DEV=false, the framework's own default: true makes every
  stop() drop the mapper behind the framework's back, and the next
  start mounts a path that is gone (sanity 160h, 278);
- `auster -H`, so --only still honours ALWAYS_EXCEPT;
- under `keyctl session`: the ssh login's session keyring is revoked
  at logout, and every encryption test then runs unencrypted.

The run lives in the guest under /root/ltvm-suite/<run-id>/: run.sh, its
pid, the start time, auster's output and logs, and a `done` file holding
auster's exit status.  On a cluster everything happens on the node the
cluster's local.sh treats as local (ClusterInfo.local_node()).

Collected results come back to ./ltvm-suite/<target>/<run-id>/ under the
caller's working directory, unless the caller names another place with
--results-dir.  `status` also finds runs collected to ltvm's state
directory, ``$XDG_STATE_HOME/ltvm/suite/``, briefly the default.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import vm_claim
from .qemu_run import die, is_running
from .vm_net import SSH_OPTS, sshpass_argv, sshpass_ssh_argv
from .vm_state import (
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_TIMEOUT,
    EXIT_UNREACHABLE,
    ClusterInfo,
    ClusterNotFound,
    VMInfo,
    VMNotFound,
    lustre_libdir,
)

SUITE_ROOT = "/root/ltvm-suite"
# A suite or run id becomes a path component and a shell word in the
# guest, so it is held to characters that need no quoting there.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_ENV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
POLL_SECONDS = 30
# How long --wait keeps polling a node that does not answer.  A crash
# with kdump is back well inside it; a hung node never is.
LOST_SECONDS = 600
_SSH_EXTRA = ["-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15"]


class Unreachable(RuntimeError):
    pass


class TargetNotFound(RuntimeError):
    pass


@dataclass
class SuiteTarget:
    """A VM, or a cluster run from its local node."""

    name: str
    kind: str
    exec_node: str
    nodes: list[str]


def resolve_target(name: str) -> SuiteTarget:
    """A cluster of that name, else a VM of that name."""
    try:
        cluster = ClusterInfo.load(name)
    except ClusterNotFound:
        try:
            VMInfo.load(name)
        except VMNotFound:
            raise TargetNotFound(f"no VM or cluster named '{name}'") from None
        return SuiteTarget(name, "vm", name, [name])
    return SuiteTarget(
        name,
        "cluster",
        cluster.local_node().name,
        [n.name for n in cluster.get_nodes()],
    )


def _ssh(
    node: str, command: str, *, stdin: str | None = None, timeout: int = 60
) -> subprocess.CompletedProcess:
    ip = VMInfo.load(node).ip
    try:
        return subprocess.run(
            sshpass_ssh_argv(ip, command, extra_opts=_SSH_EXTRA),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise Unreachable(f"{node}: no answer in {timeout}s") from e


def check_name(kind: str, value: str) -> None:
    if not _NAME_RE.match(value):
        die(f"bad {kind} {value!r}: use letters, digits and ._+-")


def env_assignments(env: list[str]) -> list[str]:
    """VAR=val words for the auster command line, each value quoted."""
    out = []
    for item in env:
        m = _ENV_RE.match(item)
        if not m:
            die(f"--env wants VAR=value, not {item!r}")
        out.append(f"{m.group(1)}={shlex.quote(m.group(2))}")
    return out


def run_dir(run_id: str) -> str:
    return f"{SUITE_ROOT}/{run_id}"


def run_script(
    run_id: str,
    libdir: str,
    suite: str,
    *,
    only: str | None = None,
    env: list[str] | None = None,
    reformat: bool = True,
    slow: bool = False,
) -> str:
    """The run.sh a suite run executes in the guest."""
    d = run_dir(run_id)
    auster = ["./auster", "-k", "-v"]
    if slow:
        # auster's -s is SLOW=yes; the suite is a positional.
        auster.append("-s")
    if reformat:
        auster.append("-r")
    auster += ["-H", "-d", f"{d}/logs", suite]
    if only:
        auster += ["--only", only]
    lines = [
        "#!/bin/bash",
        f"echo $$ > {d}/pid",
        f"cd {libdir}/tests || {{ echo rc=127 > {d}/done; exit 127; }}",
        f"export LUSTRE={libdir}",
        "sed -i -E 's/^(export +)?CLEANUP_DM_DEV=.*/CLEANUP_DM_DEV=false/'"
        " cfg/local.sh",
        f"bash llmountcleanup.sh > {d}/cleanup.log 2>&1",
        " ".join(env_assignments(env or []) + [shlex.join(auster)]),
        f"echo rc=$? > {d}/done",
    ]
    return "\n".join(lines) + "\n"


def launch_command(run_id: str, boots: dict[str, str] | None = None) -> str:
    """Install run.sh from stdin and start it detached.

    ``boots`` is each node's boot_id as the run starts, kept in the run
    so status can tell a node that rebooted under it.

    Returns once run.sh has written its pid, so a status straight after
    never finds a run that has not started yet.
    """
    d = run_dir(run_id)
    keep = (
        [
            f"printf '%s\\n' {' '.join(shlex.quote(f'{n}={b}') for n, b in boots.items())}"
            f" > {d}/boots"
        ]
        if boots
        else []
    )
    return "\n".join(
        [
            "set -e",
            f"mkdir -p {SUITE_ROOT}",
            f"mkdir {d}",
            f"cat > {d}/run.sh",
            f"date +%s > {d}/started",
            *keep,
            f"setsid nohup keyctl session - bash {d}/run.sh"
            f" > {d}/out 2>&1 < /dev/null &",
            "for ((i = 0; i < 100; i++)); do",
            f"\t[[ -s {d}/pid ]] && exit 0",
            "\tsleep 0.1",
            "done",
            f"echo 'run.sh did not start; see {d}/out' >&2",
            "exit 1",
        ]
    )


def probe_script(run_id: str | None) -> str:
    """Shell that reports the node's clock, uptime and boot_id, then one
    tab-separated line per run: id, rc (empty until done), whether
    run.sh is alive, start time, the last `== test` line, and the
    nodes' boot_ids at the run's start."""
    ids = run_id if run_id else "*"
    return f"""\
printf 'node\\t%s\\t%s\\t%s\\n' "$(date +%s)" "$(cut -d. -f1 /proc/uptime)" \\
	"$(cat {BOOT_ID} 2>/dev/null)"
cd {SUITE_ROOT} 2>/dev/null || exit 0
[[ -f power ]] && awk '{{print "power\t" $1 "\t" $2}}' power
for id in {ids}; do
	[[ -d $id ]] || continue
	rc=$(cat "$id/done" 2>/dev/null)
	pid=$(cat "$id/pid" 2>/dev/null)
	alive=0
	[[ -n $pid ]] && grep -aqF "/$id/run.sh" "/proc/$pid/cmdline" \\
		2>/dev/null && alive=1
	cur=$(grep -a '^== ' "$id/out" 2>/dev/null | tail -1 | tr '\\t' ' ' |
		cut -c1-100)
	printf 'run\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "$id" "${{rc#rc=}}" \\
		"$alive" "$(cat "$id/started" 2>/dev/null)" "$cur" \\
		"$(tr '\\n' ' ' < "$id/boots" 2>/dev/null)"
done
"""


BOOT_ID = "/proc/sys/kernel/random/boot_id"


def _int(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


def parse_probe(text: str) -> tuple[int | None, int | None, list[dict]]:
    now = uptime = None
    runs: list[dict] = []
    power: list[tuple[int, str]] = []
    for line in text.splitlines():
        f = line.split("\t")
        if f[0] == "node" and len(f) >= 3:
            now, uptime = _int(f[1]), _int(f[2])
        elif f[0] == "power" and len(f) >= 3:
            when = _int(f[1])
            if when is not None:
                power.append((when, f[2].strip()))
        elif f[0] == "run" and len(f) >= 6:
            runs.append(
                {
                    "id": f[1],
                    "rc": f[2].strip(),
                    "alive": f[3].strip() == "1",
                    "started": _int(f[4]),
                    "current": f[5].strip(),
                    "boots": dict(
                        w.split("=", 1)
                        for w in (f[6] if len(f) > 6 else "").split()
                        if "=" in w
                    ),
                }
            )
    for run in runs:
        started = run["started"]
        run["powered"] = {
            n for when, n in power if started is not None and when >= started
        }
    return now, uptime, runs


def probe_boot_id(text: str) -> str | None:
    """The probed node's boot_id, from probe_script's output."""
    for line in text.splitlines():
        f = line.split("\t")
        if f[0] == "node" and len(f) >= 4 and f[3].strip():
            return f[3].strip()
    return None


@dataclass
class RunStatus:
    run_id: str
    state: str
    rc: int | None = None
    age: int | None = None
    current: str = ""
    rebooted: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)

    def label(self) -> str:
        if self.state == "done":
            return f"done rc={self.rc if self.rc is not None else '?'}"
        return self.state

    def warnings(self) -> list[str]:
        out = []
        for n in self.rebooted:
            out.append(
                f"{n} rebooted after the run started, so it crashed: "
                f"look in /var/crash on {n} "
                f"(ltvm vm crash-collect {n})"
            )
        for n in self.unreachable:
            out.append(f"{n} does not answer (stopped, hung or rebooting)")
        return out


def classify(
    run: dict,
    now: int | None,
    uptimes: dict[str, int | None],
    boot_ids: dict[str, str | None] | None = None,
) -> RunStatus:
    """running / done / died for one probed run.

    A node whose boot_id differs from the one the run recorded at its
    start rebooted while the run was going.  A suite reboots a VM only
    through ltvm-power, which logs it, so any other reboot is a crash.
    Not reported for a finished run, which a later restart does not
    concern.

    A run from before boot_ids were recorded falls back to comparing
    each node's uptime with the run's age.  That age is the guest's own
    clock, which a time sync can step after boot, and it reported nodes
    booted just before the run as crashed.
    """
    started = run.get("started")
    age = now - started if now is not None and started is not None else None
    if run["rc"]:
        state, rc = "done", _int(run["rc"])
    elif run["alive"]:
        state, rc = "running", None
    else:
        state, rc = "died", None
    rebooted: list[str] = []
    recorded = run.get("boots") or {}
    if state != "done" and recorded:
        now_ids = boot_ids or {}
        rebooted = [
            n for n, b in recorded.items() if now_ids.get(n) and now_ids[n] != b
        ]
    elif state != "done" and age is not None:
        rebooted = [n for n, u in uptimes.items() if u is not None and u < age]
    powered = run.get("powered") or set()
    rebooted = [n for n in rebooted if n not in powered]
    return RunStatus(
        run_id=run["id"],
        state=state,
        rc=rc,
        age=age,
        current=run.get("current", ""),
        rebooted=rebooted,
        unreachable=[n for n, u in uptimes.items() if u is None],
    )


def _uptime_and_boot(node: str) -> tuple[int | None, str | None]:
    try:
        r = _ssh(node, f"cut -d. -f1 /proc/uptime; cat {BOOT_ID}", timeout=30)
    except Unreachable:
        return None, None
    if r.returncode != 0:
        return None, None
    lines = (r.stdout or "").split()
    up = _int(lines[0]) if lines else None
    return up, (lines[1] if len(lines) > 1 else None)


def boot_ids(nodes: list[str]) -> dict[str, str]:
    """Each node's boot_id, leaving out any that does not answer."""
    out = {}
    for n in nodes:
        try:
            r = _ssh(n, f"cat {BOOT_ID}", timeout=30)
        except Unreachable:
            continue
        b = (r.stdout or "").strip()
        if r.returncode == 0 and b:
            out[n] = b
    return out


def query(t: SuiteTarget, run_id: str | None = None) -> list[RunStatus]:
    """The state of one run, or of every run, on the target."""
    r = _ssh(t.exec_node, probe_script(run_id), timeout=60)
    if r.returncode != 0:
        raise Unreachable(
            f"{t.exec_node}: probe failed (rc={r.returncode}): "
            f"{r.stderr.strip()}"
        )
    now, up, runs = parse_probe(r.stdout)
    uptimes: dict[str, int | None] = {t.exec_node: up}
    ids: dict[str, str | None] = {t.exec_node: probe_boot_id(r.stdout)}
    if any(not run["rc"] for run in runs):
        for n in t.nodes:
            if n != t.exec_node:
                uptimes[n], ids[n] = _uptime_and_boot(n)
    return [classify(run, now, uptimes, ids) for run in runs]


# -- results.yml --------------------------------------------------------


_SUITE_LINE = re.compile(r"^ {8}(name|status|duration):\s?(.*)")
_SUB_NAME = re.compile(r"^ {12}name:\s?(\S+)")
_SUB_FIELD = re.compile(r"^ {12}(status|duration|return_code|error):\s?(.*)")


def _clean(value: str) -> str:
    return value.strip().strip("'")


def parse_results(text: str) -> list[dict[str, Any]]:
    """The suites in one auster results.yml, each with its subtests.

    Read line by line rather than with a YAML parser: a suite killed
    mid-run leaves a file that is not valid YAML, and what it did record
    is the part worth reading.  A suite that runs another (sanity-compr's
    test_sanity) writes the inner subtests between the outer test's name
    and its status; a status arriving for a subtest that already has one
    belongs to the outer test still waiting for it.
    """
    suites: list[dict[str, Any]] = []
    suite: dict[str, Any] | None = None
    cur: dict[str, Any] | None = None
    waiting: list[dict[str, Any]] = []
    for line in text.splitlines():
        m = _SUB_NAME.match(line)
        if m and suite is not None:
            if cur is not None and "status" not in cur:
                waiting.append(cur)
            cur = {"name": m.group(1)}
            suite["subtests"].append(cur)
            continue
        m = _SUB_FIELD.match(line)
        if m and cur is not None:
            key, value = m.group(1), _clean(m.group(2))
            if key == "status" and "status" in cur and waiting:
                cur = waiting.pop()
            cur[key] = value
            continue
        m = _SUITE_LINE.match(line)
        if m:
            key, value = m.group(1), _clean(m.group(2))
            if key == "name":
                suite = {"name": value, "subtests": []}
                suites.append(suite)
                cur, waiting = None, []
            elif suite is not None:
                suite[key] = value
    return suites


def _results_files(root: Path) -> list[Path]:
    return sorted(root.rglob("results.yml"))


def summarize(root: Path) -> dict[str, Any]:
    """Counts by status, the failures, and the subtests that never got
    a status (the suite stopped during them), from every results.yml
    under *root*."""
    counts: dict[str, int] = {}
    failures: list[dict[str, Any]] = []
    unfinished: list[dict[str, Any]] = []
    suites: list[dict[str, Any]] = []
    files = _results_files(root)
    for path in files:
        for s in parse_results(path.read_text(errors="replace")):
            suites.append(
                {
                    "name": s["name"],
                    "status": s.get("status"),
                    "duration": s.get("duration"),
                }
            )
            for t in s["subtests"]:
                st = t.get("status")
                row = {"suite": s["name"], **t}
                if not st:
                    unfinished.append(row)
                    continue
                counts[st] = counts.get(st, 0) + 1
                if st not in ("PASS", "SKIP"):
                    failures.append(row)
    return {
        "counts": counts,
        "suites": suites,
        "failures": failures,
        "unfinished": unfinished,
        "results_files": [str(p) for p in files],
    }


# -- collect ------------------------------------------------------------


DEBUG_LOG_GLOB = "*.debug_log.*"


def rsync_argv(
    t: SuiteTarget, run_id: str, dest: Path, debug_logs: bool = False
) -> list[str]:
    ip = VMInfo.load(t.exec_node).ip
    ssh = shlex.join([*sshpass_argv(), "ssh", *SSH_OPTS, *_SSH_EXTRA])
    exclude = [] if debug_logs else ["--exclude", DEBUG_LOG_GLOB]
    return [
        "rsync",
        "-a",
        *exclude,
        "-e",
        ssh,
        f"root@{ip}:{run_dir(run_id)}/",
        f"{dest}/",
    ]


def count_debug_logs(t: SuiteTarget, run_id: str) -> tuple[int, int] | None:
    """(files, bytes) of the run's Lustre debug logs, or None."""
    cmd = (
        f"find {run_dir(run_id)} -type f -name '{DEBUG_LOG_GLOB}' "
        "-printf '%s\\n'"
    )
    try:
        r = _ssh(t.exec_node, cmd, timeout=60)
    except Unreachable:
        return None
    if r.returncode != 0:
        return None
    sizes = [int(x) for x in r.stdout.split() if x.isdigit()]
    return len(sizes), sum(sizes)


def collect(
    t: SuiteTarget, run_id: str, dest: Path, debug_logs: bool = False
) -> tuple[int, int] | None:
    """Copy the run back; returns the debug logs left behind, if any
    were and they could be counted."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            rsync_argv(t, run_id, dest, debug_logs),
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        die("rsync is not installed on this host")
    # 24: files vanished while copying, which a running suite does.
    if r.returncode not in (0, 24):
        die(
            f"rsync from {t.exec_node} failed (rc={r.returncode}): "
            f"{r.stderr.strip()}"
        )
    if debug_logs:
        return None
    skipped = count_debug_logs(t, run_id)
    return skipped if skipped and skipped[0] else None


def _cwd_root() -> Path | None:
    try:
        return Path.cwd() / "ltvm-suite"
    except OSError:
        return None  # the working directory is gone


def results_root() -> Path:
    """Where collected runs go by default: ./ltvm-suite under the working
    directory, keyed below it by target and run id."""
    root = _cwd_root()
    if root is None:
        die("the working directory is gone; cd elsewhere or give --results-dir")
    return root


def state_root() -> Path:
    """ltvm's state directory, where collects put runs for a while."""
    base = os.environ.get("XDG_STATE_HOME") or str(
        Path.home() / ".local" / "state"
    )
    return Path(base) / "ltvm" / "suite"


def default_dest(target: str, run_id: str) -> Path:
    return results_root() / target / run_id


def results_dest(explicit: str | None, target: str, run_id: str) -> Path:
    """The --results-dir given, made absolute, else default_dest()."""
    if explicit:
        return Path(os.path.abspath(os.path.expanduser(explicit)))
    return default_dest(target, run_id)


def collected_dir(target: str, run_id: str) -> Path | None:
    """Where this run's results were collected to, if anywhere ltvm
    knows to look: the working directory, then the state directory."""
    for root in (_cwd_root(), state_root()):
        if root is not None and (root / target / run_id).is_dir():
            return root / target / run_id
    return None


def check_dest(dest: Path) -> None:
    """Fail now, not after the suite, when *dest* cannot be written."""
    d = dest
    while not d.exists():
        d = d.parent
    if not d.is_dir():
        die(f"results dir {dest}: {d} is not a directory")
    if not os.access(d, os.W_OK | os.X_OK):
        die(f"results dir {dest}: {d} is not writable")


def _note(msg: str, use_json: bool) -> None:
    """A progress line: stdout, or stderr when stdout carries JSON."""
    print(msg, file=sys.stderr if use_json else sys.stdout, flush=True)


def print_summary(
    status: RunStatus,
    summary: dict[str, Any],
    dest: Path,
    skipped: tuple[int, int] | None = None,
) -> None:
    print(f"{status.run_id}: {status.label()}, results in {dest}")
    for w in status.warnings():
        print(f"  warning: {w}")
    c = summary["counts"]
    other = sum(n for s, n in c.items() if s not in ("PASS", "SKIP"))
    print(f"  {c.get('PASS', 0)} pass, {other} fail, {c.get('SKIP', 0)} skip")
    for f in summary["failures"]:
        print(
            f"  {f['status']} {f['suite']} {f['name']} "
            f"({f.get('duration', '?')}s): {f.get('error', '')}"
        )
    for u in summary["unfinished"]:
        print(f"  unfinished {u['suite']} {u['name']}")
    if not summary["results_files"]:
        print(
            "  no results.yml: the suite never started a subtest; "
            f"see {dest}/out and {dest}/cleanup.log"
        )
    if skipped:
        print(
            f"  {skipped[0]} debug log(s) ({skipped[1] / 2**20:.0f} MiB) "
            "not copied; --debug-logs copies them"
        )


def failed(status: RunStatus, summary: dict[str, Any]) -> bool:
    return (
        status.state != "done"
        or status.rc != 0
        or bool(summary["failures"])
        or bool(summary["unfinished"])
    )


# -- commands -----------------------------------------------------------


def _require_up(t: SuiteTarget, nodes: list[str]) -> None:
    down = [n for n in nodes if not is_running(VMInfo.load(n))]
    if not down:
        return
    if t.kind == "cluster":
        die(
            f"cluster '{t.name}' has nodes not running: {', '.join(down)}\n"
            f"  start them with: ltvm cluster start {t.name}",
            EXIT_UNREACHABLE,
        )
    die(f"VM '{t.name}' not running", EXIT_UNREACHABLE)


def _emit(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2))


def _ensure_layout(
    t: SuiteTarget, exec_vm: VMInfo, os_family: str, libdir: str
) -> None:
    """Make cfg/local.sh describe the target's disks before the run.

    Only deploy writes them, so a VM booted from a fetched image would
    otherwise format the framework's defaults -- one ~120 MB MDT, two
    OSTs, 100k inodes -- and fail the larger tests with ENOSPC.  A VM
    gets its disk block here, as `ltvm llmount` does; a cluster's block
    names every node's role, which only `cluster deploy` writes.
    """
    if t.kind == "vm":
        from .deploy import configure_test_disks

        try:
            configure_test_disks(
                exec_vm.ip,
                exec_vm.mdt_disks,
                exec_vm.ost_disks,
                disk_size_bytes=exec_vm.disk_size,
                os_family=os_family,
            )
        except RuntimeError as e:
            die(str(e))
        return
    from .vm_cluster import CLUSTER_BLOCK_BEGIN

    cfg = f"{libdir}/tests/cfg/local.sh"
    r = _ssh(
        t.exec_node,
        f"grep -qF {shlex.quote(CLUSTER_BLOCK_BEGIN)} {cfg}",
        timeout=30,
    )
    if r.returncode != 0:
        die(
            f"{t.exec_node}'s {cfg} has no cluster block, so the suite "
            "would format one node's defaults\n"
            f"  ltvm cluster deploy {t.name}   (with --build <tree>, or "
            "without to use the image's Lustre)"
        )


def cmd_suite_run(args: argparse.Namespace) -> int:
    check_name("suite", args.suite)
    if args.timeout is not None and not args.wait:
        die("--timeout only applies with --wait")
    results_dir = getattr(args, "results_dir", None)
    if results_dir and not args.wait:
        die(
            "--results-dir only applies with --wait; without it, give "
            "it to `ltvm suite collect`"
        )
    if getattr(args, "debug_logs", False) and not args.wait:
        die(
            "--debug-logs only applies with --wait; without it, give "
            "it to `ltvm suite collect`"
        )
    only = " ".join(args.only) if args.only else None
    env = list(args.env or [])
    env_assignments(env)

    t = resolve_target(args.target)
    vm_claim.require_all(t.nodes, "run a suite on")
    _require_up(t, t.nodes)
    try:
        busy = [s for s in query(t) if s.state == "running"]
    except Unreachable as e:
        die(str(e), EXIT_UNREACHABLE)
    if busy:
        die(
            f"{busy[0].run_id} is still running on {t.exec_node}; two "
            "suites on one filesystem break each other\n"
            f"  ltvm suite status {t.name} {busy[0].run_id}"
        )

    from .vm_commands import _os_family_for_vm

    exec_vm = VMInfo.load(t.exec_node)
    os_family = _os_family_for_vm(exec_vm, "libdir")
    libdir = lustre_libdir(os_family)
    _ensure_layout(t, exec_vm, os_family, libdir)
    if t.kind == "cluster":
        from .cluster_failover import require_no_moved_facets

        require_no_moved_facets(ClusterInfo.load(t.name), "start a suite")
    run_id = time.strftime(f"{args.suite}-%Y%m%d-%H%M%S")
    dest = results_dest(results_dir, t.name, run_id)
    if args.wait:
        check_dest(dest)
    script = run_script(
        run_id,
        libdir,
        args.suite,
        only=only,
        env=env,
        reformat=not args.no_reformat,
        slow=args.slow,
    )
    try:
        r = _ssh(
            t.exec_node,
            launch_command(run_id, boot_ids(t.nodes)),
            stdin=script,
            timeout=60,
        )
    except Unreachable as e:
        die(str(e), EXIT_UNREACHABLE)
    if r.returncode != 0:
        die(f"could not start {run_id} on {t.exec_node}: {r.stderr.strip()}")

    if not args.wait:
        if args.json:
            _emit(
                {
                    "run_id": run_id,
                    "target": t.name,
                    "node": t.exec_node,
                    "dir": run_dir(run_id),
                    "results_dir": str(dest),
                }
            )
        else:
            print(run_id)
            print(
                f"started on {t.exec_node}:{run_dir(run_id)}; "
                f"`ltvm suite status {t.name} {run_id}`, then "
                f"`ltvm suite collect {t.name} {run_id}`, which puts "
                f"the results in {dest}",
                file=sys.stderr,
            )
        return EXIT_OK
    if not args.json:
        print(f"{run_id} started on {t.exec_node}", flush=True)
    _note(f"results will be collected to {dest}", args.json)
    return _wait_and_collect(
        t,
        run_id,
        args.timeout,
        args.json,
        dest,
        getattr(args, "debug_logs", False),
    )


def _wait_and_collect(
    t: SuiteTarget,
    run_id: str,
    timeout: int | None,
    use_json: bool,
    dest: Path,
    debug_logs: bool = False,
) -> int:
    start = time.monotonic()
    lost_since: float | None = None
    shown = ""
    warned: set[str] = set()
    while True:
        now = time.monotonic()
        try:
            found = query(t, run_id)
        except Unreachable as e:
            lost_since = lost_since or now
            if now - lost_since > LOST_SECONDS:
                print(
                    f"error: {e}; gave up after {LOST_SECONDS}s.  The run "
                    f"may still finish: ltvm suite status {t.name} {run_id}",
                    file=sys.stderr,
                )
                return EXIT_UNREACHABLE
        else:
            lost_since = None
            if not found:
                die(f"{run_id} has gone from {t.exec_node}")
            status = found[0]
            if not use_json:
                if status.current and status.current != shown:
                    shown = status.current
                    print(f"{time.strftime('%H:%M:%S')} {shown}", flush=True)
                for w in status.warnings():
                    if w not in warned:
                        warned.add(w)
                        print(f"warning: {w}", file=sys.stderr, flush=True)
            if status.state != "running":
                break
        if timeout is not None and now - start > timeout:
            print(
                f"error: {run_id} still running after {timeout}s; it was "
                f"left running: ltvm suite status {t.name} {run_id}",
                file=sys.stderr,
            )
            return EXIT_TIMEOUT
        time.sleep(POLL_SECONDS)

    skipped = collect(t, run_id, dest, debug_logs)
    summary = summarize(dest)
    if use_json:
        _emit(_collect_json(t, status, summary, dest, skipped))
    else:
        print_summary(status, summary, dest, skipped)
    return EXIT_ERROR if failed(status, summary) else EXIT_OK


def _collect_json(
    t: SuiteTarget,
    status: RunStatus,
    summary: dict[str, Any],
    dest: Path,
    skipped: tuple[int, int] | None = None,
) -> dict[str, Any]:
    return {
        "target": t.name,
        "node": t.exec_node,
        "dest": str(dest),
        "status": asdict(status),
        "debug_logs_skipped": skipped[0] if skipped else 0,
        **summary,
    }


def cmd_suite_status(args: argparse.Namespace) -> int:
    if args.run_id:
        check_name("run id", args.run_id)
    t = resolve_target(args.target)
    _require_up(t, [t.exec_node])
    try:
        found = query(t, args.run_id)
    except Unreachable as e:
        die(str(e), EXIT_UNREACHABLE)
    if args.run_id and not found:
        die(f"no run {args.run_id} on {t.exec_node}", EXIT_NOT_FOUND)
    collected = {s.run_id: collected_dir(t.name, s.run_id) for s in found}
    root = _cwd_root()
    if args.json:
        _emit(
            {
                "target": t.name,
                "node": t.exec_node,
                "results_root": str(root / t.name) if root else None,
                "runs": [
                    {
                        **asdict(s),
                        "results_dir": (
                            str(collected[s.run_id])
                            if collected[s.run_id]
                            else None
                        ),
                    }
                    for s in found
                ],
            }
        )
        return EXIT_OK
    if not found:
        print(f"no suite runs on {t.exec_node}")
    for s in found:
        age = f"{s.age // 60}m" if s.age is not None else "?"
        line = f"{s.run_id:<36} {s.label():<10} {age:>6}"
        if s.state == "running" and s.current:
            line += f"  {s.current}"
        print(line)
        for w in s.warnings():
            print(f"  warning: {w}")
        if collected[s.run_id]:
            print(f"  collected to {collected[s.run_id]}")
    return EXIT_OK


def cmd_suite_collect(args: argparse.Namespace) -> int:
    check_name("run id", args.run_id)
    t = resolve_target(args.target)
    _require_up(t, [t.exec_node])
    try:
        found = query(t, args.run_id)
    except Unreachable as e:
        die(str(e), EXIT_UNREACHABLE)
    if not found:
        die(f"no run {args.run_id} on {t.exec_node}", EXIT_NOT_FOUND)
    status = found[0]
    dest = results_dest(args.results_dir, t.name, args.run_id)
    check_dest(dest)
    _note(f"collecting {args.run_id} to {dest}", args.json)
    skipped = collect(t, args.run_id, dest, getattr(args, "debug_logs", False))
    summary = summarize(dest)
    if args.json:
        _emit(_collect_json(t, status, summary, dest, skipped))
    else:
        print_summary(status, summary, dest, skipped)
        if status.state == "running":
            print("  (still running: these results are partial)")
    return EXIT_OK
