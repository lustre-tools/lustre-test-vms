"""Multi-node cluster management commands."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import vm_claim
from .qemu_run import die, is_running, run
from .vm_net import (
    SSH_KEEPALIVE_OPTS,
    SSH_OPTS,
    run_ssh,
    ssh_wait_budget,
    ssh_wait_cap,
    sshpass_argv,
    sshpass_ssh_argv,
)
from .vm_owner import resolve_owner_id
from .vm_state import (
    DEFAULT_TARGET,
    EXIT_TIMEOUT,
    EXIT_UNREACHABLE,
    SOCKETS,
    SSH_TIMEOUT,
    ClusterInfo,
    ClusterNode,
    ClusterNotFound,
    VMInfo,
    VMNotFound,
    lustre_libdir,
)

# Guest helpers that local.sh names: POWER_DOWN/POWER_UP and ZPOOL.
POWER_HELPER = "/usr/sbin/ltvm-power"
ZPOOL_HELPER = "/usr/sbin/ltvm-zpool"
GUEST_HELPERS = {
    POWER_HELPER: Path(__file__).parent / "ltvm-power.sh",
    ZPOOL_HELPER: Path(__file__).parent / "ltvm-zpool.sh",
}


def _sudo_prefix() -> list[str]:
    """``["sudo"]`` unless we are already root or VMs need no root here.

    Used for the per-node ``ltvm create`` / ``ltvm destroy`` children so
    they inherit the outer SUDO_USER rather than having a nested sudo
    overwrite it with "root".
    """
    from . import rootless

    return [] if os.geteuid() == 0 or rootless.ready() else ["sudo"]


def _ltvm() -> list[str]:
    """The argv that runs this checkout's ltvm, for a child command.

    A bare "ltvm" runs whichever install is first in PATH, and a
    worktree's cluster then creates its nodes with another checkout's
    code, which may not know a new flag ("unrecognized arguments").
    """
    return [
        sys.executable,
        str(Path(__file__).resolve().parent.parent / "ltvm"),
    ]


def parse_node_spec(spec: str) -> ClusterNode:
    """Parse a node spec like 'mgs+mds:myvm:1' or 'oss:myoss:3'.

    Format: roles:vmname[:diskcount]
    """
    parts = spec.split(":")
    if len(parts) < 2:
        die(f"invalid node spec '{spec}': need roles:name[:disks]")

    roles_str = parts[0]
    name = parts[1]
    # Validate the node name shares the same restrictions cmd_create
    # enforces -- spaces or special chars would break /etc/hosts /
    # SSH config / hostname / fc_name= cmdline.
    from .vm_commands import _validate_vm_name

    _validate_vm_name(name)
    if len(parts) > 2:
        try:
            disk_count = int(parts[2])
        except ValueError:
            die(
                f"invalid disk count '{parts[2]}' in spec '{spec}': "
                f"must be a non-negative integer (format: roles:name[:disks])"
            )
    else:
        disk_count = 0

    roles = roles_str.lower().split("+")
    for r in roles:
        if r not in ("mgs", "mds", "oss", "client"):
            die(f"unknown role '{r}' in spec '{spec}'")

    mdt_disks = 0
    ost_disks = 0
    if "mds" in roles:
        mdt_disks = max(disk_count, 1)
    if "oss" in roles:
        ost_disks = max(disk_count, 1)

    return ClusterNode(
        name=name,
        roles=roles,
        mdt_disks=mdt_disks,
        ost_disks=ost_disks,
    )


FAILOVER_ROLE = "failover"


def failover_partners(
    spec_nodes: list[ClusterNode], failover: dict[str, str]
) -> list[ClusterNode]:
    """The failover partner nodes `--failover PARTNER=PRIMARY` adds.

    A partner is a server with no disks of its own: it attaches its
    primary's disks and can mount each of the primary's targets.  The
    primary must hold disks, and has at most one partner; a partner is
    a new name, not one of the node specs.
    """
    from .vm_commands import _validate_vm_name

    by_name = {n.name: n for n in spec_nodes}
    seen: dict[str, str] = {}
    partners: list[ClusterNode] = []
    for partner, primary in failover.items():
        _validate_vm_name(partner)
        if partner in by_name:
            die(
                f"--failover {partner}={primary}: {partner!r} is a node "
                f"spec; the partner is a new node that ltvm creates"
            )
        node = by_name.get(primary)
        if node is None:
            die(
                f"--failover {partner}={primary}: no node {primary!r} in "
                f"the node specs"
            )
        if not (node.is_mgs or node.is_mds or node.is_oss):
            die(
                f"--failover {partner}={primary}: {primary!r} is a client "
                f"and has no targets to fail over"
            )
        if primary in seen:
            die(
                f"--failover: {primary!r} has two partners, {seen[primary]!r} "
                f"and {partner!r}; a failover pair has two nodes"
            )
        seen[primary] = partner
        partners.append(
            ClusterNode(
                name=partner, roles=[FAILOVER_ROLE], failover_for=primary
            )
        )
    return partners


# What a node's LNet will run on: the networks= value of its lnet.conf,
# a separator, then every address the node holds.  rc.local composes
# lnet.conf at boot from the node's NICs (setup-lnet-config.sh), so
# asking the node keeps that rule in one place.
LNET_PROBE = (
    'sed -n \'s/.*networks="\\([^"]*\\)".*/\\1/p\' '
    "/etc/modprobe.d/lnet.conf 2>/dev/null | head -n 1; "
    "echo --; ip -o addr show"
)

# The first net of a networks= value: its type ("o2ib" holds a digit,
# so the index is the digits that end the name), then the interfaces.
_NET_RE = re.compile(r"\s*([a-z][a-z0-9]*?)(\d*)\s*(?:\(([^)]*)\)|(?=,)|$)")


# The address families a cluster's NIDs can use.  The extra NICs hold
# both, so this is a choice made at deploy, not at create.
IP_FAMILIES = ("ipv4", "ipv6")


@dataclass(frozen=True)
class ClusterLnet:
    """The LNet net a cluster's local.sh names, as its MGS runs it."""

    nettype: str
    mgsnid: str
    # LNet takes an interface's IPv6 address only when it is configured
    # with `lnetctl lnet configure --large`.  FORCE_LARGE_NID=true is
    # what makes the test framework do that.
    force_large_nid: bool = False


def lnet_from_probe(output: str, ip_family: str = "ipv4") -> ClusterLnet:
    """Read LNET_PROBE output from the MGS into its NETTYPE and MGSNID.

    The first net in lnet.conf is the one the MGS NID is on, and its
    first interface is the one that carries it.  With no lnet.conf,
    LNet runs tcp on the first interface, which is eth0.  *ip_family*
    picks which of that interface's addresses the NID uses.

    Raises ValueError when the MGS has no usable address for that net.
    """
    networks, _, addrs = output.partition("--\n")
    m = _NET_RE.match(networks.strip() or "tcp0(eth0)")
    if not m:
        raise ValueError(f"cannot parse lnet.conf networks={networks!r}")
    nettype, index, ifaces = m.group(1), m.group(2), m.group(3) or "eth0"
    iface = ifaces.split(",")[0].strip()
    # Every generated config names the net without its index, and
    # "tcp" and "tcp0" are the same net to Lustre.
    net = nettype if index in ("", "0") else f"{nettype}{index}"
    ipv6 = ip_family == "ipv6"
    if ipv6 and nettype != "tcp":
        raise ValueError(
            f"--ip-family ipv6 needs tcp, and the MGS runs {net}: "
            f'test-framework.sh fails with "FORCE_LARGE_NID only '
            f'supported by tcp"'
        )

    for line in addrs.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[1] != iface:
            continue
        # A link-local IPv6 address is not a NID LNet will use.
        if fields[2] == ("inet6" if ipv6 else "inet") and (
            not ipv6 or "global" in fields
        ):
            ip = fields[3].split("/")[0]
            return ClusterLnet(
                nettype=nettype, mgsnid=f"{ip}@{net}", force_large_nid=ipv6
            )
    family = "global IPv6" if ipv6 else "IPv4"
    raise ValueError(f"no {family} address on {iface}, which carries {net}")


def _probe_node_lnet(node: ClusterNode, ip_family: str) -> ClusterLnet:
    """Ask *node* which net and address its NID is on."""
    r = run_ssh(node.ip, LNET_PROBE, timeout=30)
    if r.returncode != 0:
        die(
            f"cannot read the LNet config of {node.name}: "
            f"{(r.stderr or r.stdout).strip()}"
        )
    try:
        return lnet_from_probe(r.stdout, ip_family)
    except ValueError as e:
        die(f"{node.name}: {e}")


def probe_mgs_lnet(
    cluster: ClusterInfo, ip_family: str = "ipv4"
) -> ClusterLnet:
    """Ask the cluster's MGS which net and address its NID is on.

    An MGS with a failover partner has two NIDs, and MGSNID names both,
    colon-separated, which is how a client mount and --mgsnode list an
    MGS that can run on either node.
    """
    mgs = cluster.mgs_node()
    lnet = _probe_node_lnet(mgs, ip_family)
    partner = cluster.failover_partners().get(mgs.name)
    if partner is None:
        return lnet
    other = _probe_node_lnet(cluster.node(partner), ip_family)
    return dataclasses.replace(lnet, mgsnid=f"{lnet.mgsnid}:{other.mgsnid}")


CLUSTER_BLOCK_BEGIN = "# --- Cluster configuration"
CLUSTER_BLOCK_END = "# --- END cluster configuration"


@dataclass
class OstFilePlan:
    """Where a cluster's file-backed OSTs go: OST index to OSS node."""

    hosts: dict[int, str]
    size_kb: int


def plan_ost_files(
    cluster: ClusterInfo,
    ost_count: int,
    node_ips: dict[str, str],
    disk_sizes: dict[str, int] | None,
    *,
    prune: bool,
) -> OstFilePlan | None:
    """Spread the OSTs past the OSS disks over the OSS nodes, in turn.

    Every OSS node is prepared, so one that holds no file OST any more
    has its old files removed (with ``prune``).  The size is the
    smallest any node can give its share, so all OSTs stay alike.
    """
    from ltvm_pkg.deploy import (
        ost_file_kb,
        ost_space_error,
        prepare_ost_files,
    )

    oss_list = cluster.oss_nodes()
    if ost_count and not oss_list:
        die(f"cluster {cluster.name!r} has no OSS node for --ost-count")
    first = sum(n.ost_disks for n in oss_list) + 1
    hosts = {
        idx: oss_list[i % len(oss_list)].name
        for i, idx in enumerate(range(first, ost_count + 1))
    }
    cap_kb = _min_disk_kb(oss_list, disk_sizes)
    size_kb = 0
    for node in oss_list:
        keep = [i for i, h in hosts.items() if h == node.name]
        try:
            space = prepare_ost_files(node_ips[node.name], keep, prune=prune)
        except RuntimeError as e:
            die(f"{node.name}: {e}")
        if not keep:
            continue
        kb = ost_file_kb(space, len(keep), cap_kb)
        if not kb:
            die(ost_space_error(node.name, len(keep), space))
        size_kb = min(size_kb, kb) if size_kb else kb
    if not hosts:
        return None
    return OstFilePlan(hosts=hosts, size_kb=size_kb)


def _min_disk_kb(
    nodes: list[ClusterNode], disk_sizes: dict[str, int] | None
) -> int:
    """Smallest per-disk size among ``nodes`` in KB, or 0 if unknown."""
    sizes = [(disk_sizes or {}).get(n.name, 0) for n in nodes]
    if not sizes or not all(sizes):
        return 0
    return min(sizes) // 1024


def generate_local_sh(
    cluster: ClusterInfo,
    os_family: str = "rhel",
    fstype: str = "ldiskfs",
    lnet: ClusterLnet | None = None,
    disk_sizes: dict[str, int] | None = None,
    ost_count: int = 0,
    ost_files: OstFilePlan | None = None,
) -> str:
    """Generate the cluster block for cfg/local.sh.

    The block goes into the tree's own cfg/local.sh, after each node's
    disk block, rather than replacing the file: the stock file defines
    what every suite reads -- TSTUSR, RUNAS, QUOTA_USERS and the rest --
    with ``${VAR:-default}``, and this block overrides only what the
    cluster decides.

    ``fstype`` selects the OSD backend.  Only this line changes for
    ZFS: the MDSDEV*/OSTDEV* written above are the *vdevs* ZFS builds
    its pools on (test-framework.sh's mdsvdevname/ostvdevname), and the
    dataset names default to ``$FSNAME-mdt<n>/mdt<n>``.

    ``lnet`` is the net the MGS runs, from probe_mgs_lnet().  Without
    it, the MGS NID is its mgmt address on tcp.

    ``disk_sizes`` maps node name to its per-disk size in bytes.  The
    sizes go in this block because it is the only one a client gets,
    and llmount formats every target from the first client: without
    them it falls back to the framework's few-hundred-MB defaults.

    ``ost_count`` (--ost-count) replaces the count of OSS disks, and
    ``ost_files`` places the OSTs past those disks in files.
    """
    from ltvm_pkg.deploy import env_count, fstype_line, ost_file_path

    if fstype not in ("ldiskfs", "zfs"):
        raise ValueError(f"unsupported fstype: {fstype!r}")
    mgs = cluster.mgs_node()
    if lnet is None:
        lnet = ClusterLnet(nettype="tcp", mgsnid=f"{mgs.ip}@tcp")
    mds_list = cluster.mds_nodes()
    oss_list = cluster.oss_nodes()
    client_list = cluster.client_nodes()
    # primary -> partner.  The framework moves a facet to
    # ${facet}failover_HOST on `fail <facet>`, formats it with
    # --failnode for that host (which turns ldiskfs MMP on), and finds
    # it there under the same device name: the partner attaches the
    # primary's disks in the primary's order.
    partner_of = cluster.failover_partners()

    lustre_dir = lustre_libdir(os_family)
    lines = [
        f"{CLUSTER_BLOCK_BEGIN} (generated by ltvm cluster deploy) ---",
        f"# Cluster: {cluster.name}",
        "",
        "FSNAME=lustre",
        f"NETTYPE={lnet.nettype}",
        # Written for both families: an explicit false stops a true
        # from an earlier ipv6 deploy from staying in effect.
        f"FORCE_LARGE_NID={'true' if lnet.force_large_nid else 'false'}",
        "",
        f"LUSTRE={lustre_dir}",
        f"RLUSTRE={lustre_dir}",
        f"RPWD={lustre_dir}/tests",
        "",
    ]

    lines.append(f"mgs_HOST={mgs.name}")
    if mgs.name in partner_of:
        lines.append(f"mgsfailover_HOST={partner_of[mgs.name]}")
    lines.append(f"MGSNID={lnet.mgsnid}")

    combined = mgs.is_mds
    if not combined:
        lines.append("MGSDEV=/dev/vdb")
    lines.append("")

    if mds_list:
        lines.append(f"mds_HOST={mds_list[0].name}")
        total_mdts = sum(n.mdt_disks for n in mds_list)
        lines.append(env_count("MDSCOUNT", total_mdts))
        mds_kb = _min_disk_kb(mds_list, disk_sizes)
        if mds_kb:
            lines.append(f"MDSSIZE={mds_kb}")

        mdt_idx = 1
        for mds_node in mds_list:
            # MDS disks always start at /dev/vdb on the MDS node.  If
            # MGS+MDS share a node (`combined`), no separate MGS disk
            # exists.  If they don't share, the MGS disk lives on the
            # MGS node, not on this MDS node, so no offset is needed
            # here either.
            disk_offset = 1

            for d in range(mds_node.mdt_disks):
                letter = chr(ord("a") + disk_offset + d)
                lines.append(f"MDSDEV{mdt_idx}=/dev/vd{letter}")
                # Per target even with one MDS node: all_mdts_nodes()
                # reads these directly, before facet_host() fills them.
                lines.append(f"mds{mdt_idx}_HOST={mds_node.name}")
                if mds_node.name in partner_of:
                    lines.append(
                        f"mds{mdt_idx}failover_HOST={partner_of[mds_node.name]}"
                    )
                mdt_idx += 1
        lines.append("")

    if oss_list:
        lines.append(f"ost_HOST={oss_list[0].name}")
        total_osts = ost_count or sum(n.ost_disks for n in oss_list)
        lines.append(env_count("OSTCOUNT", total_osts))
        ost_kb = _min_disk_kb(oss_list, disk_sizes)
        if ost_files is not None:
            ost_kb = ost_files.size_kb
        if ost_kb:
            lines.append(f"OSTSIZE={ost_kb}")

        ost_idx = 1
        for oss_node in oss_list:
            disk_offset = 1
            if oss_node.is_mgs and not oss_node.is_mds:
                disk_offset = 2
            if oss_node.is_mds:
                # If this OSS node is also the MDS, OST disks come
                # after the MDT disks.  Note: an OSS+MDS+MGS node has
                # `combined=True` (mgs.is_mds is True for it), so the
                # extra-MGS-disk offset only applies in the standalone
                # MGS case which lives on a different node entirely.
                disk_offset = 1 + oss_node.mdt_disks

            for d in range(oss_node.ost_disks):
                letter = chr(ord("a") + disk_offset + d)
                lines.append(f"OSTDEV{ost_idx}=/dev/vd{letter}")
                lines.append(f"ost{ost_idx}_HOST={oss_node.name}")
                if oss_node.name in partner_of:
                    lines.append(
                        f"ost{ost_idx}failover_HOST={partner_of[oss_node.name]}"
                    )
                ost_idx += 1
        for idx, host in (ost_files.hosts if ost_files else {}).items():
            lines.append(f"OSTDEV{idx}={ost_file_path(idx)}")
            lines.append(f"ost{idx}_HOST={host}")
        lines.append("")

    if client_list:
        client_names = ",".join(n.name for n in client_list)
        lines.append(f"CLIENTS={client_names}")
        # RCLIENTS is the variable the test framework actually reads.
        #
        # init_clients_lists() (test-framework.sh, called unconditionally
        # from init_logging()) rebuilds CLIENTS *from* RCLIENTS:
        #
        #     clients="$SINGLECLIENT $HOSTNAME $RCLIENTS"
        #     export CLIENTS=$(comma_list $clients)
        #
        # so a CLIENTS= set here and nothing else is overwritten with
        # just the local node, and every remote client silently drops
        # out.  Multi-client suites (sanityn, parallel-scale,
        # recovery-*-scale) then run green against one client while
        # appearing to have used the whole cluster.
        #
        # Tests are driven from the first client, which is the node
        # init_clients_lists() treats as local, so the remote set is
        # clients[1:].  Space-separated to match what hostlist_expand()
        # and the rest of the framework expect.
        rclients = [n.name for n in client_list[1:]]
        if rclients:
            lines.append('RCLIENTS="{}"'.format(" ".join(rclients)))
        # init_gss() reads CLIENTCOUNT during setup, before
        # init_clients_lists() sets it; empty, its key loop is a bash
        # syntax error and SHARED_KEY setup never copies the keys out.
        lines.append(f"CLIENTCOUNT=${{CLIENTCOUNT:-{len(client_list)}}}")
    lines.append("")

    lines.append(fstype_line(fstype))
    lines.append("OSTSEQWIDTH=${OSTSEQWIDTH:-0x20000}")
    lines.append("")

    lines.append("LCTL=/usr/sbin/lctl")
    lines.append("LFS=/usr/bin/lfs")
    lines.append("")

    # FAILURE_MODE=HARD calls these with a node name; see ltvm-power.sh.
    lines.append(f'POWER_DOWN="{POWER_HELPER} off"')
    lines.append(f'POWER_UP="{POWER_HELPER} on"')
    # A failover partner must reread the partitions before an import;
    # see ltvm-zpool.sh.
    lines.append(f"ZPOOL={ZPOOL_HELPER}")
    lines.append("")

    lines.append('PDSH="pdsh -S -Rssh -w"')
    lines.append("LOAD_MODULES_REMOTE=true")
    lines.append("MOUNT=/mnt/lustre")
    lines.append("MOUNT1=${MOUNT1:-$MOUNT}")
    lines.append("MOUNT2=/mnt/lustre2")
    lines.append("MOUNT3=${MOUNT3:-${MOUNT}3}")
    lines.append("DIR=${DIR:-$MOUNT}")
    lines.append("DIR1=${DIR1:-$MOUNT1}")
    lines.append("DIR2=${DIR2:-$MOUNT2}")
    lines.append("DIR3=${DIR3:-$MOUNT3}")
    lines.append("")

    lines.append(f"{CLUSTER_BLOCK_END} ---")

    return "\n".join(lines) + "\n"


# ── cluster commands ─────────────────────────────────────


def _create_one_node(
    node: ClusterNode,
    vcpus: int,
    mem: int | None,
    os_target: str | None = None,
    arch: str | None = None,
    disk_size: str | None = None,
    root_size: str | None = None,
    nics: list[str] | None = None,
    kernel_args: str = "",
    owner_id: str | None = None,
    wait_seconds: int = 0,
    numa_nodes: int = 0,
    kernel: str | None = None,
    variant: str = "base",
    share_disks: bool = False,
    disk_dir: str = "",
    disk_cache: str = "",
    disk_prealloc: str = "",
) -> tuple[str, int, str]:
    """Create a single cluster VM via ltvm subprocess.

    Returns (node_name, returncode, combined_output).
    Running via subprocess avoids threading issues with shared print/state
    inside cmd_create, and lets all nodes boot concurrently.

    `mem=None` means "let the inner cmd_create resolve from the target's
    default_mem", so cluster nodes inherit the per-target memory default
    instead of being silently overridden by a hardcoded constant in the
    cluster CLI parser.

    `nics` is an optional list of `--nic` spec strings that get applied
    uniformly to every node.  Validation of each spec (including the
    reserved-type rejection for softroce/passthrough) happens inside
    the child `ltvm create`, so bad specs surface per-node with the
    follow-up-issue hint the single-node path prints.
    """
    mgs_disk = 1 if (node.is_mgs and not node.is_mds) else 0
    # No `sudo` prefix when already root: cluster create requires root, so
    # a nested sudo would reset SUDO_USER to "root" in the child and the
    # child's ~/.ssh bookkeeping would target root's home instead of the
    # invoking user's.  (On macOS that is a hard failure -- root's home is
    # /var/root and /root is on the read-only system volume.)
    cmd = _sudo_prefix() + [
        *_ltvm(),
        "create",
        node.name,
        "--vcpus",
        str(vcpus),
    ]
    if mem is not None:
        cmd += ["--mem", str(mem)]
    if os_target:
        cmd += ["--target", os_target]
    if arch:
        cmd += ["--arch", arch]
    if kernel:
        cmd += ["--kernel", kernel]
    if variant != "base":
        cmd += ["--variant", variant]
    if disk_size:
        cmd += ["--disk-size", disk_size]
    if root_size:
        cmd += ["--root-size", root_size]
    if owner_id:
        # Pass the cluster parent's resolved value explicitly so all members
        # share one owner even though each child has a different process PID.
        cmd += ["--owner-id", owner_id]
    # Always pass both counts, including zero.  Omitting a flag lets
    # `ltvm create`'s defaults (1 MDT, 2 OST) apply, so an oss-only
    # node silently gained an MDT-slot disk ahead of its OSTs -- which
    # shifts every /dev/vd* letter past what generate_local_sh computes
    # (its disk_offset assumes exactly the disks the role asked for).
    # The result mounted fine, because the backing files are
    # identical-size scratch images, while OSTDEV1 pointed at the
    # spurious disk and the last real OST disk went unused.
    cmd += ["--mdt-disks", str(node.mdt_disks + mgs_disk)]
    cmd += ["--ost-disks", str(node.ost_disks)]
    for nic in nics or []:
        cmd += ["--nic", nic]
    if share_disks:
        cmd += ["--share-disks"]
    if node.failover_for:
        cmd += ["--disks-from", node.failover_for]
    else:
        # A partner has no disks of its own, and shares its primary's.
        if disk_dir:
            cmd += ["--disk-dir", disk_dir]
        if disk_cache:
            cmd += ["--disk-cache", disk_cache]
        if disk_prealloc:
            cmd += [f"--disk-prealloc={disk_prealloc}"]
    if kernel_args:
        # One argv word, so a value starting with '-' is not an option.
        cmd += [f"--kernel-args={kernel_args}"]
    if numa_nodes:
        cmd += ["--numa-nodes", str(numa_nodes)]
    if wait_seconds:
        cmd += ["--wait", str(wait_seconds)]

    try:
        r = _run_prefixed(
            cmd,
            node.name,
            timeout=_node_create_timeout(_node_arch(os_target, arch))
            + wait_seconds,
        )
    except subprocess.TimeoutExpired as e:
        # See _write_cluster_local_sh: don't propagate out of the parallel
        # executor; report the failure via the (name, rc, output) tuple
        # so the cluster create driver can summarise per-node failures.
        return node.name, 1, f"create timed out after {e.timeout}s"
    combined = r.stdout
    if r.stderr:
        combined = combined + r.stderr if combined else r.stderr
    return node.name, r.returncode, combined.rstrip("\n")


def _run_prefixed(
    cmd: list[str], name: str, *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run a node's child command, echoing each line it writes as
    ``  <name>: <line>`` as it comes, and return its whole output.

    Captured to the end, a five-node create printed nothing for minutes
    while the nodes booted, so a redirected log could not show which
    one was slow.  Raises TimeoutExpired, having killed the child, past
    ``timeout``.
    """
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    lines: list[str] = []
    expired = threading.Event()
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        env=env,
    ) as proc:

        def _expire() -> None:
            expired.set()
            proc.kill()

        timer = threading.Timer(timeout, _expire)
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.append(line)
                print(f"  {name}: {line.rstrip()}", flush=True)
            rc = proc.wait()
        finally:
            timer.cancel()
    if expired.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout, "".join(lines))
    return subprocess.CompletedProcess(cmd, rc, "".join(lines), "")


# Headroom over the inner `ltvm create`'s own budgets: disk creation
# and launching QEMU happen outside them.
_NODE_CREATE_HEADROOM = 270


def _node_arch(os_target: str | None, arch: str | None) -> str:
    """The arch a node's `ltvm create` will boot."""
    if arch:
        return arch
    from .target_config import TargetConfig

    try:
        return TargetConfig(os_target or DEFAULT_TARGET).arch
    except Exception:
        return "x86_64"


def _node_create_timeout(arch: str = "x86_64") -> int:
    """Wall-clock budget for one node's child `ltvm create`.

    A backstop over the child's own limits, which end it first: the
    longest wait for SSH (ssh_wait_cap, which stretches SSH_TIMEOUT
    while the guest's console moves) and the kdump seeding after it,
    whose restart alone may take 300s on a loaded host.  A budget of
    SSH_TIMEOUT + 270s killed nodes that were up and seeding kdump, and
    the cleanup loop then destroyed the whole cluster.
    """
    from .vm_commands import KDUMP_SEED_BUDGET

    return (
        ssh_wait_cap(ssh_wait_budget(SSH_TIMEOUT, arch))
        + KDUMP_SEED_BUDGET
        + _NODE_CREATE_HEADROOM
    )


def _node_targets(
    node_specs: list[ClusterNode],
    targets: list[str | None] | None,
    os_target: str | None,
) -> dict[str, str | None]:
    """Map each node name to its own target, or None for the cluster's.

    A node whose own target is the cluster-wide one maps to None, so
    `rocky9 mgs:a:1 rocky9 client:b` behaves as if written once.
    """
    if targets is None:
        targets = [None] * len(node_specs)
    if len(targets) != len(node_specs):
        die("internal error: node_targets does not match node specs")
    from .target_config import list_targets

    known = set(list_targets())
    result: dict[str, str | None] = {}
    for node, t in zip(node_specs, targets):
        if t is not None and t not in known:
            die(f"unknown target {t!r} for node {node.name!r}")
        result[node.name] = t if t != os_target else None
    return result


def _node_kernels(
    node_specs: list[ClusterNode],
    kernels: list[str | None] | None,
    node_targets: dict[str, str | None],
    os_target: str | None,
) -> dict[str, str | None]:
    """Map each node name to its own kernel, or None.

    A node kernel is one of the node's own target's: declared in
    targets.yaml or already built.  Checked here, before any node is
    created, rather than by the node's `ltvm create` once the others
    are up.
    """
    from .target_config import TargetConfig

    if kernels is None:
        kernels = [None] * len(node_specs)
    if len(kernels) != len(node_specs):
        die("internal error: node_kernels does not match node specs")
    result: dict[str, str | None] = {}
    for node, k in zip(node_specs, kernels):
        result[node.name] = k
        if k is None:
            continue
        target = node_targets.get(node.name) or os_target or DEFAULT_TARGET
        try:
            tc = TargetConfig(target)
        except ValueError as e:
            die(f"node {node.name!r}: {e}")
        declared = tc.declared_kernels()
        if tc._short_kernel_name(k) in declared:
            continue
        if (tc.output_dir / "kernels" / tc.resolve_kernel(k)).is_dir():
            continue
        die(
            f"node {node.name!r}: {target} has no kernel {k!r} "
            f"(declared: {', '.join(declared)})"
        )
    return result


def _print_cluster_plan(
    cluster_name: str,
    node_specs: list[ClusterNode],
    *,
    vcpus: int,
    mem: int | None,
    os_target: str | None,
    node_targets: dict[str, str | None],
    node_kernels: dict[str, str | None],
    arch: str | None,
    kernel: str | None,
    variant: str,
    disk_size: str | None,
    root_size: str | None,
    nics: list[str],
    kernel_args: str,
    owner_id: str | None,
    disk_dir: str = "",
    disk_cache: str = "",
    disk_prealloc: str = "",
) -> None:
    """Report what `cluster create` would do, for --dry-run.

    Per-node IPs are not shown: each node's `ltvm create` claims one
    under a lock, so there is no honest answer without claiming them.
    """
    print(
        f"Would create cluster '{cluster_name}' with {len(node_specs)} nodes:"
    )
    for node in node_specs:
        disks = []
        if node.mdt_disks:
            disks.append(f"{node.mdt_disks} MDT")
        if node.ost_disks:
            disks.append(f"{node.ost_disks} OST")
        if node.failover_for:
            disks.append(f"those of {node.failover_for}")
        own = node_targets.get(node.name)
        own_kernel = node_kernels.get(node.name)
        print(
            f"  {node.name:<20} roles={'+'.join(node.roles):<16} "
            f"disks={', '.join(disks) or 'none'}"
            + (f"  target={own}" if own else "")
            + (f"  kernel={own_kernel}" if own_kernel else "")
        )
    mem_desc = f"{mem} MB" if mem is not None else "target default"
    arch_desc = f" arch={arch}" if arch else ""
    print(
        "Applied to every node:"
        if not any(node_targets.values())
        else "Applied to every node without its own target:"
    )
    variant_desc = f" variant={variant}" if variant != "base" else ""
    print(f"  target:  {os_target or 'default'}{arch_desc}{variant_desc}")
    print(f"  kernel:  {kernel or 'target default'}")
    print(f"  cpu/mem: {vcpus} vcpus, {mem_desc}")
    if disk_size:
        print(f"  disk:    {disk_size} each")
    if root_size:
        print(f"  root:    {root_size} each")
    if disk_dir:
        print(f"  disks:   in {disk_dir}")
    if disk_cache:
        print(f"  cache:   {disk_cache}")
    if disk_prealloc:
        print(f"  prealloc: {disk_prealloc}")
    if nics:
        print(f"  nics:    eth0 (mgmt) + {', '.join(nics)}")
    if kernel_args:
        print(f"  cmdline: + {kernel_args}")
    print(f"  owner:   {owner_id}")
    print("Nothing was written.  Re-run without --dry-run to create it.")


def _require_cluster_memory(
    node_specs: list[ClusterNode],
    mem: int | None,
    node_targets: dict[str, str | None],
    os_target: str | None,
    wait_seconds: int,
) -> None:
    """Refuse a cluster whose nodes cannot all start, before any does.

    Each node's create checks only its own memory, so a cluster too big
    for the budget built the first nodes, failed the next and rolled
    them all back.  With --wait the running VMs may yet stop, so only
    a cluster too big for an idle host is refused.
    """
    from .qemu_run import guest_memory_budget, running_vm_memory
    from .target_config import TargetConfig

    budget = guest_memory_budget()
    if budget is None:
        return
    sizes = {}
    for node in node_specs:
        if mem is not None:
            sizes[node.name] = mem
            continue
        target = node_targets.get(node.name) or os_target or DEFAULT_TARGET
        try:
            sizes[node.name] = TargetConfig(target).default_mem
        except (ValueError, FileNotFoundError):
            return
    need = sum(sizes.values())
    running = [] if wait_seconds else running_vm_memory()
    used = sum(m for _, m in running)
    biggest = max(sizes.values(), default=0)
    if biggest <= budget.budget_mb and used + need <= budget.limit_mb:
        return
    lines = [
        f"not enough memory for the cluster's {len(node_specs)} nodes",
        f"  requested:    {need} MiB ({', '.join(f'{n} {m}' for n, m in sizes.items())})",
        f"  host budget:  {budget.limit_mb} MiB ({budget.describe()}"
        + (
            f", overcommit {budget.overcommit:g}"
            if budget.overcommit != 1.0
            else ""
        )
        + ")",
    ]
    if running:
        lines.append(
            f"  already used: {used} MiB across {len(running)} running VM(s): "
            + ", ".join(
                f"{n} {m}" for n, m in sorted(running, key=lambda x: -x[1])
            )
        )
    lines.append(
        "  stop VMs, pass a smaller --mem, or --wait SECONDS for running "
        "VMs to stop"
        if running
        else "  pass a smaller --mem or create fewer nodes"
    )
    die("\n".join(lines))


def cmd_cluster_create(args: argparse.Namespace) -> None:
    cluster_name = args.name
    # Cluster name flows into filesystem paths (SOCKETS/<name>.cluster)
    # so it needs the same character whitelist as VM names.
    from .vm_commands import (
        _validate_disk_options,
        _validate_kernel_args,
        _validate_vm_name,
    )

    _validate_vm_name(cluster_name)
    record = SOCKETS / f"{cluster_name}.cluster"
    if record.exists():
        try:
            orphaned = ClusterInfo.load(cluster_name).orphaned()
        except ClusterNotFound:
            orphaned = None
        except (RuntimeError, ValueError):
            orphaned = False
        if orphaned is False:
            die(f"cluster '{cluster_name}' already exists")
        if orphaned:
            record.unlink(missing_ok=True)
            print(
                f"removed stale cluster record {cluster_name}: "
                f"all its VMs are gone"
            )

    spec_nodes = [parse_node_spec(s) for s in args.nodes]
    partners = failover_partners(
        spec_nodes, dict(getattr(args, "failover", None) or {})
    )
    node_specs = spec_nodes + partners

    # Refuse a spec that names the same node twice.  Nothing checked,
    # so `cluster create co2 mgs+mds:co2-a:1 oss:co2-a:3` launched two
    # concurrent `ltvm create co2-a` subprocesses racing on one .info,
    # overlay and TAP -- or the loser adopted the winner and reported
    # success.  The saved ClusterInfo then carried the same host twice
    # with contradictory disk roles, and generate_local_sh assigned it
    # MDSDEV and OSTDEV letters from two different disk_offset
    # computations, pointing at overlapping devices.
    seen_names: set[str] = set()
    dupes: set[str] = set()
    for n in node_specs:
        if n.name in seen_names:
            dupes.add(n.name)
        seen_names.add(n.name)
    if dupes:
        die(
            f"node name(s) listed more than once: "
            f"{', '.join(sorted(dupes))}\n"
            f"Each node in a cluster needs its own name."
        )

    # Refuse to build a cluster on top of VMs that already exist.
    # `ltvm create` is idempotent -- _handle_existing_vm starts a
    # stopped VM and exits 0 -- so without this check the cluster
    # silently adopts them.  That matters because the failure path
    # below destroys every name in node_specs: one node failing to
    # come up would delete a pre-existing VM (overlay and data disks
    # included) that this command never created.  `cluster destroy`
    # would do the same later.  Adopting is also wrong on its own
    # terms, since an existing VM won't have the disk layout the role
    # implies.
    taken = [
        n.name for n in node_specs if (SOCKETS / f"{n.name}.info").exists()
    ]
    if taken:
        die(
            f"VM(s) already exist: {', '.join(taken)}\n"
            f"cluster create would adopt them, and would destroy them "
            f"if another node failed.\n"
            f"Destroy them first, or pick different node names."
        )

    mgs_count = sum(1 for n in node_specs if n.is_mgs)
    if mgs_count == 0:
        die("cluster needs at least one node with mgs role")
    if mgs_count > 1:
        die("cluster can only have one MGS node")

    if not any(n.is_mds for n in node_specs):
        die("cluster needs at least one node with mds role")

    vcpus = args.vcpus
    mem = args.mem
    os_target = getattr(args, "os", None)
    arch = getattr(args, "arch", None)
    kernel = getattr(args, "kernel", None)
    variant = getattr(args, "variant", None) or "base"
    # A node named after its own target boots that target's default
    # kernel, or its --node-kernel, and base variant: --kernel/--variant
    # name things in the cluster-wide target, and another target has
    # its own names.
    node_targets = _node_targets(
        spec_nodes, getattr(args, "node_targets", None), os_target
    )
    node_kernels = _node_kernels(
        spec_nodes,
        getattr(args, "node_kernels", None),
        node_targets,
        os_target,
    )
    # A partner boots what its primary boots: it mounts the same targets.
    for p in partners:
        node_targets[p.name] = node_targets[p.failover_for]
        node_kernels[p.name] = node_kernels[p.failover_for]
    shared = {p.failover_for for p in partners}
    disk_size = getattr(args, "disk_size", None)
    root_size = getattr(args, "root_size", None)
    disk_cache = getattr(args, "disk_cache", None) or ""
    if partners and disk_cache not in ("", "none"):
        die(
            f"--disk-cache {disk_cache}: a failover pair's disks are "
            f"always cache=none"
        )
    _validate_disk_options(args)
    disk_opts = {
        "disk_dir": args.disk_dir,
        "disk_cache": args.disk_cache,
        "disk_prealloc": args.disk_prealloc,
    }
    # Multi-NIC: same list of --nic specs applies to every node in the
    # cluster.  The CLI layer (cli.py::cmd_cluster) collects them into
    # args.nic; older call sites that don't know about --nic land here
    # with the attr unset, in which case we pass an empty list through.
    nics: list[str] = list(getattr(args, "nic", None) or [])
    kernel_args = _validate_kernel_args(getattr(args, "kernel_args", None))
    wait_seconds: int = getattr(args, "wait", 0)
    numa_nodes: int = getattr(args, "numa_nodes", 0) or 0
    try:
        owner_id = resolve_owner_id(getattr(args, "owner_id", None))
    except ValueError as e:
        die(str(e))

    if getattr(args, "dry_run", False):
        # Everything above is validation and reads -- name rules,
        # duplicate node names, already-taken VMs, the mgs/mds role
        # rules, owner resolution.  The first thing written is a node,
        # created by the pool below.
        _print_cluster_plan(
            cluster_name,
            node_specs,
            vcpus=vcpus,
            mem=mem,
            os_target=os_target,
            node_targets=node_targets,
            node_kernels=node_kernels,
            arch=arch,
            kernel=kernel,
            variant=variant,
            disk_size=disk_size,
            root_size=root_size,
            nics=nics,
            kernel_args=kernel_args,
            owner_id=owner_id,
            **disk_opts,
        )
        return

    _require_cluster_memory(
        node_specs, mem, node_targets, os_target, wait_seconds
    )

    print(f"=== Creating cluster '{cluster_name}' ===", flush=True)
    if os_target:
        print(f"    Target: {os_target}{(' arch=' + arch) if arch else ''}")
    if kernel:
        print(f"    Kernel: {kernel}")
    if variant != "base":
        print(f"    Variant: {variant}")
    for name, t in node_targets.items():
        if t:
            print(f"    Target: {t} for {name}")
    for name, k in node_kernels.items():
        if k:
            print(f"    Kernel: {k} for {name}")
    print(f"    Creating {len(spec_nodes)} nodes in parallel...")

    def create_wave(wave: list[ClusterNode]) -> list[str]:
        failed: list[str] = []
        with ThreadPoolExecutor(max_workers=len(wave)) as executor:
            futures = {
                executor.submit(
                    _create_one_node,
                    node,
                    vcpus,
                    mem,
                    node_targets[node.name] or os_target,
                    arch,
                    disk_size,
                    root_size,
                    nics,
                    kernel_args,
                    owner_id,
                    wait_seconds=wait_seconds,
                    numa_nodes=numa_nodes,
                    kernel=node_kernels[node.name]
                    or (None if node_targets[node.name] else kernel),
                    variant="base" if node_targets[node.name] else variant,
                    share_disks=node.name in shared,
                    **disk_opts,
                ): node
                for node in wave
            }
            for future in as_completed(futures):
                node = futures[future]
                try:
                    name, rc, output = future.result()
                except Exception as e:
                    # Don't let one node's exception (e.g. TimeoutExpired
                    # from the inner subprocess) abandon the remaining
                    # futures.  Mark this one as failed and keep
                    # collecting -- the cleanup loop below needs an
                    # accurate `failed` list.
                    name = node.name
                    rc = -1
                    output = f"exception: {type(e).__name__}: {e}"
                if rc != 0:
                    print(f"\n--- {name}: FAILED (rc={rc}) ---\n{output}")
                    failed.append(name)
                else:
                    print(f"  {name}: up")
        return failed

    failed = create_wave(spec_nodes)
    # A partner's create reads its primary's .info and opens its disks,
    # so the partners come up after every primary is up.
    if partners and not failed:
        print(f"    Creating {len(partners)} failover partner(s)...")
        failed = create_wave(partners)

    if failed:
        # Clean up ALL nodes, including the failed ones.  A failed
        # `ltvm create` may have already created the TAP, overlay,
        # and .info file before erroring out (e.g. SSH wait timed out
        # so the rollback in cmd_create couldn't run).  `ltvm destroy`
        # is idempotent on missing VMs, so it's safe to call on every
        # node and necessary to avoid leaking partial state.  A partner
        # goes first: destroy refuses a primary whose partner stays.
        for node in sorted(node_specs, key=lambda n: not n.failover_for):
            r = subprocess.run(
                _sudo_prefix() + [*_ltvm(), "destroy", node.name],
                capture_output=True,
            )
            if r.returncode != 0:
                print(
                    f"warning: failed to destroy VM '{node.name}' during cleanup",
                    file=sys.stderr,
                )
        die(f"vm create failed for: {', '.join(failed)}")

    # Load IPs now that all VMs are running
    for node in node_specs:
        vm = VMInfo.load(node.name)
        node.ip = vm.ip

    cluster = ClusterInfo(
        name=cluster_name,
        owner_id=owner_id,
        nodes=[
            {
                "name": n.name,
                "roles": n.roles,
                "mdt_disks": n.mdt_disks,
                "ost_disks": n.ost_disks,
                "ip": n.ip,
                # Only when set, so a cluster without failover keeps
                # the record an older ltvm reads without a warning.
                **({"failover_for": n.failover_for} if n.failover_for else {}),
            }
            for n in node_specs
        ],
    )
    cluster.save()

    print("\n--- Writing the cluster's names into each node's /etc/hosts...")
    hosts_failed = _distribute_cluster_hosts(
        cluster_name, node_specs, {n.name: n.ip for n in node_specs}
    )
    if hosts_failed:
        print(
            f"  Warning: /etc/hosts not written on "
            f"{', '.join(hosts_failed)}; those nodes resolve the others "
            f"through the host's DNS until `ltvm cluster deploy` "
            f"writes it.",
            file=sys.stderr,
        )

    print(f"\n=== Cluster '{cluster_name}' created ===")
    print(f"  owner: {owner_id}")
    for n in node_specs:
        partner = f" of {n.failover_for}" if n.failover_for else ""
        print(f"  {n.name:<20} {n.ip:<18} {'+'.join(n.roles)}{partner}")
    print(
        f"\nNext: ltvm cluster deploy {cluster_name} "
        f"--build /path/to/lustre-release [--mount]"
    )


def _deploy_one_node(
    node_name: str,
    lustre_tree: str | Path,
    os_family: str = "rhel",
    zfs_staging: Path | None = None,
    userspace_only: bool = False,
    need_zfs: bool = False,
) -> tuple[str, int, str]:
    """Deploy Lustre to one cluster node.

    ``need_zfs`` refuses a node that has no ZFS to run its targets on.

    Returns (node_name, returncode, output_message).

    `lustre_tree` is the source tree the cluster's `--build` was
    pointed at; it determines where the per-tree staging dir lives.
    """
    from ltvm_pkg.deploy import (
        configure_zfs_mmp,
        deploy_to_vm,
        require_zfs_modules,
    )
    from ltvm_pkg.lustre_build import staging_path as _staging_path

    vm = VMInfo.load(node_name)
    if not vm.os_id:
        raise RuntimeError(f"VM '{vm.name}' has no os_id; recreate it")
    target = vm.os_id
    # Pass vm.arch + kernel so the staging dir matches what
    # build-lustre wrote.  Staging is keyed per-kernel so two kernels'
    # userland can coexist under one lustre tree.
    vm_arch = vm.arch
    deploy_kernel: str | None = None
    if vm.kernel:
        vm_kernel_name = Path(vm.kernel).parent.name
        if vm_kernel_name:
            deploy_kernel = vm_kernel_name
    if deploy_kernel is None:
        return (
            node_name,
            1,
            f"cannot resolve kernel for {node_name}: vm.kernel is unset",
        )
    staging = _staging_path(
        lustre_tree,
        target,
        arch=vm_arch,
        kernel=deploy_kernel,
        variant=vm.variant,
    )
    try:
        if need_zfs:
            require_zfs_modules(vm)
            configure_zfs_mmp(vm)
        # No fstype here: the cluster block written further down
        # (generate_local_sh) sets FSTYPE for every node, after anything
        # written here.
        deploy_to_vm(
            vm,
            staging,
            os_family=os_family,
            zfs_staging=zfs_staging,
            userspace_only=userspace_only,
        )
        return node_name, 0, "ok"
    except RuntimeError as e:
        return node_name, 1, str(e)


def _deployed_fstype(cluster: ClusterInfo, os_family: str) -> str | None:
    """The FSTYPE in the cluster block on a cluster deployed before its
    record kept one, or None."""
    from ltvm_pkg.deploy import read_kept_blocks

    try:
        ip = VMInfo.load(cluster.local_node().name).ip
        block = read_kept_blocks(ip, os_family).get(CLUSTER_BLOCK_BEGIN, "")
    except (VMNotFound, OSError, subprocess.SubprocessError):
        return None
    m = re.search(
        r"^FSTYPE=(?:\$\{_ltvm_env_FSTYPE:-)?(ldiskfs|zfs)\b", block, re.M
    )
    return m.group(1) if m else None


def _write_cluster_local_sh(
    node_name: str,
    node_ip: str,
    local_sh: str,
    ssh_opts: list[str],
    os_family: str = "rhel",
) -> tuple[str, int, str]:
    """Put the cluster block into a node's cfg/local.sh.

    Returns (node_name, returncode, output).
    """
    from ltvm_pkg.deploy import splice_local_sh_block

    lustre_dir = lustre_libdir(os_family)
    cfg_path = f"{lustre_dir}/tests/cfg/local.sh"
    script = (
        f"mkdir -p {lustre_dir}/tests/cfg && t=$(mktemp) && cat > $t && "
        + splice_local_sh_block(
            cfg_path, "$t", CLUSTER_BLOCK_BEGIN, CLUSTER_BLOCK_END
        )
        + "; rc=$?; rm -f $t; exit $rc"
    )
    from ltvm_pkg.deploy import step_timeout

    try:
        r = subprocess.run(
            sshpass_ssh_argv(node_ip, script, extra_opts=SSH_KEEPALIVE_OPTS),
            input=local_sh,
            capture_output=True,
            text=True,
            timeout=step_timeout(),
        )
    except subprocess.TimeoutExpired as e:
        # Don't propagate out of the parallel executor; the caller checks
        # the returncode field and reports per-node failures.  Without
        # this catch, one stalled node tracebacks out of future.result()
        # and aborts the cluster config rollout mid-flight.
        return node_name, 1, f"timed out after {e.timeout}s writing {cfg_path}"
    combined = r.stdout
    if r.stderr:
        combined = combined + r.stderr if combined else r.stderr
    return node_name, r.returncode, combined.rstrip("\n")


# rocky10 images built before their Dockerfile took pdsh from epel-10
# have none: the 10.1 base pins an EPEL branch without it.
_EPEL10_METALINK = (
    "https://mirrors.fedoraproject.org/metalink?repo=epel-10&arch=$basearch"
)
_PDSH_SCRIPTS = {
    "rhel": (
        "rpm -q pdsh pdsh-rcmd-ssh >/dev/null 2>&1 && exit 0; "
        "dnf -y -q install pdsh pdsh-rcmd-ssh >/dev/null 2>&1 || "
        f"dnf -y -q install --setopt=epel.metalink='{_EPEL10_METALINK}' "
        "pdsh pdsh-rcmd-ssh"
    ),
    "debian": (
        "command -v pdsh >/dev/null && exit 0; "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -q pdsh"
    ),
    "suse": (
        "rpm -q pdsh >/dev/null 2>&1 && exit 0; "
        "zypper -n -q install --no-recommends pdsh"
    ),
}


def _install_guest_helpers(
    node_name: str, node_ip: str
) -> tuple[str, int, str]:
    """Install the helpers that local.sh names (GUEST_HELPERS).

    Every node gets them: the framework runs POWER_DOWN from whichever
    node drives the suite, and $ZPOOL on the servers.  Returns
    (node_name, returncode, output).
    """
    from ltvm_pkg.deploy import step_timeout

    for dest, src in GUEST_HELPERS.items():
        try:
            r = subprocess.run(
                sshpass_ssh_argv(
                    node_ip,
                    f"cat > {dest} && chmod 755 {dest}",
                    extra_opts=SSH_KEEPALIVE_OPTS,
                ),
                input=src.read_text(),
                capture_output=True,
                text=True,
                timeout=step_timeout(),
            )
        except subprocess.TimeoutExpired as e:
            return node_name, 1, f"timed out after {e.timeout}s writing {dest}"
        if r.returncode != 0:
            return node_name, r.returncode, (r.stdout + r.stderr).rstrip("\n")
    return node_name, 0, ""


def _ensure_pdsh(
    node_name: str, node_ip: str, os_family: str
) -> tuple[str, int, str]:
    """Install pdsh on a node that lacks it.

    The cluster block sets PDSH to pdsh over ssh, and without it
    llmount.sh fails every remote command with rc 127.
    """
    script = _PDSH_SCRIPTS.get(os_family, _PDSH_SCRIPTS["rhel"])
    try:
        r = run_ssh(node_ip, script, timeout=600)
    except subprocess.TimeoutExpired as e:
        return node_name, 1, f"timed out after {e.timeout}s installing pdsh"
    return (
        node_name,
        r.returncode,
        ((r.stdout or "") + (r.stderr or "")).strip(),
    )


def _cluster_os_family(node_families: dict[str, str]) -> str:
    """The OS family whose Lustre directory the cluster block names.

    cfg/local.sh sets LUSTRE, RLUSTRE and RPWD once for every node, and
    the framework uses them on the other nodes too.  Any family will do
    once _link_lustre_libdirs has made each node's directory reachable
    under the others' names.
    """
    return min(node_families.values(), default="rhel")


def _link_lustre_libdirs(
    nodes: list[ClusterNode],
    node_ips: dict[str, str],
    node_families: dict[str, str],
) -> list[str]:
    """In a cluster whose nodes install Lustre in different places (rhel
    and suse /usr/lib64/lustre, debian /usr/lib/lustre), link each
    other place to the node's own.  Returns the nodes that failed.

    A directory already there is not replaced: it would be another
    Lustre install, and the framework would run its scripts.
    """
    libdirs = {lustre_libdir(f) for f in node_families.values()}
    if len(libdirs) < 2:
        return []

    def _link(node: ClusterNode) -> tuple[str, int, str]:
        own = lustre_libdir(node_families[node.name])
        steps = []
        for other in sorted(libdirs - {own}):
            q_own, q_other = shlex.quote(own), shlex.quote(other)
            steps.append(
                f"if [ -e {q_other} ] && [ ! -L {q_other} ]; then "
                f"echo {q_other} is not a link to {q_own} >&2; exit 1; fi; "
                f"mkdir -p {shlex.quote(str(Path(other).parent))} && "
                f"ln -sfn {q_own} {q_other}"
            )
        try:
            r = run_ssh(node_ips[node.name], " && ".join(steps), timeout=60)
        except subprocess.TimeoutExpired as e:
            return node.name, 1, f"timed out after {e.timeout}s"
        return (
            node.name,
            r.returncode,
            ((r.stdout or "") + (r.stderr or "")).strip(),
        )

    return _parallel_cluster_op(
        nodes,
        _link,
        success_verb="Lustre directories linked",
        failure_verb="Lustre directory link FAILED",
    )


GUEST_HOSTS_BEGIN = "# --- ltvm cluster"
GUEST_HOSTS_END = "# --- END ltvm cluster"

# Replace the block, or append it.  sed rather than a rewrite through a
# new file, so /etc/hosts keeps its inode and mode; a file with no
# trailing newline gets one, or the first entry would join its last line.
_GUEST_HOSTS_SCRIPT = (
    f"sed -i '/^{GUEST_HOSTS_BEGIN}/,/^{GUEST_HOSTS_END}/d' /etc/hosts && "
    '{ [ -z "$(tail -c 1 /etc/hosts)" ] || echo; cat; } >> /etc/hosts'
)


def cluster_hosts_block(name: str, members: list[tuple[str, str]]) -> str:
    """The /etc/hosts block naming every member of cluster *name*.

    *members* is ``(node name, ip)`` pairs.
    """
    lines = [f"{GUEST_HOSTS_BEGIN} {name} (generated by ltvm) ---"]
    lines += [f"{ip}\t{node}" for node, ip in members]
    lines.append(f"{GUEST_HOSTS_END} ---")
    return "\n".join(lines) + "\n"


def _write_cluster_hosts(
    node_name: str, node_ip: str, block: str
) -> tuple[str, int, str]:
    """Put the cluster's hosts block into one node's /etc/hosts."""
    from ltvm_pkg.deploy import step_timeout

    try:
        r = subprocess.run(
            sshpass_ssh_argv(
                node_ip, _GUEST_HOSTS_SCRIPT, extra_opts=SSH_KEEPALIVE_OPTS
            ),
            input=block,
            capture_output=True,
            text=True,
            timeout=step_timeout(),
        )
    except subprocess.TimeoutExpired as e:
        return node_name, 1, f"timed out after {e.timeout}s writing /etc/hosts"
    return node_name, r.returncode, (r.stdout + r.stderr).rstrip("\n")


def _distribute_cluster_hosts(
    cluster_name: str, nodes: list[ClusterNode], ips: dict[str, str]
) -> list[str]:
    """Write every member's name into each member's /etc/hosts.

    The nodes reach each other by name -- pdsh, ssh, mds_HOST and the
    rest of cfg/local.sh -- and the host's DNS is not something every
    host provides: a dnsmasq that forwards a name it does not know, or
    the AAAA query for one it does, can leave each lookup waiting out
    the resolver's timeout.  Returns the nodes that failed.
    """
    block = cluster_hosts_block(
        cluster_name, [(n.name, ips[n.name]) for n in nodes]
    )
    return _parallel_cluster_op(
        nodes,
        lambda node: _write_cluster_hosts(node.name, ips[node.name], block),
        success_verb="/etc/hosts written",
        failure_verb="/etc/hosts FAILED",
    )


def _parallel_cluster_op(
    nodes: list,
    submit: Any,
    success_verb: str,
    failure_verb: str,
) -> list[str]:
    """Fan out ``submit(node)`` across a thread pool and collect results.

    Each submitted future must return a ``(name, rc, output)`` tuple.
    Prints per-node progress and returns the list of failed node names
    so the caller can decide whether to die() or continue.
    """
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=len(nodes)) as executor:
        futures = {executor.submit(submit, node): node for node in nodes}
        for future in as_completed(futures):
            node = futures[future]
            try:
                name, rc, output = future.result()
            except Exception as e:
                # _deploy_one_node can raise past its own except
                # RuntimeError -- VMInfo.load raises VMNotFound, which
                # is not a RuntimeError -- and an unguarded result()
                # aborted the whole fan-out with a Python traceback:
                # no per-node report, no "deploy failed for: ...",
                # update_deploy never recorded, local.sh never
                # distributed.  cmd_cluster_create already wraps its
                # result() for exactly this reason.
                name = getattr(node, "name", str(node))
                print(f"\n--- {name}: {failure_verb} ---\n{e}")
                failed.append(name)
                continue
            if rc != 0:
                print(f"\n--- {name}: {failure_verb} (rc={rc}) ---\n{output}")
                failed.append(name)
            else:
                print(f"  {name}: {success_verb}")
    return failed


def _validate_lustre_source(path: Path) -> None:
    """Die with a clear message if path is not a Lustre source tree.

    Only checks that the directory looks like a Lustre tree.
    Cluster deploy runs `ltvm build lustre` itself, so a freshly
    cloned tree (no .ko files yet) is valid input.
    """
    if not path.is_dir():
        die(f"--build:'{path}' is not a directory")

    missing = [
        name
        for name in ("configure.ac", "lustre", "lnet")
        if not (path / name).exists()
    ]
    if missing:
        die(
            f"--build:'{path}' does not look like a Lustre "
            f"source tree (missing: {', '.join(missing)})"
        )


def _staged_zfs(
    build: str,
    target: str,
    kernel_name: str | None,
    arch: str,
    variant: str,
    zfs_version_arg: str | None,
) -> Path:
    """Return the ZFS staging dir the Lustre build for this kernel used."""
    from .lustre_build import read_staging_meta
    from .lustre_build import staging_path as _staging_path
    from .target_config import TargetConfig
    from .zfs_build import ZfsBuildError, find_zfs_staging, rebuild_flag

    tc = TargetConfig(target, arch=arch, variant=variant)
    meta = read_staging_meta(
        _staging_path(
            build,
            target,
            arch=arch,
            kernel=kernel_name or tc.default_kernel,
            variant=variant,
        )
    )
    staged_zfs = meta.get("zfs_version") if isinstance(meta, dict) else None
    if not staged_zfs:
        die(
            "ZFS was requested but the Lustre build produced no ZFS "
            "record -- rerun with --force to reconfigure"
        )
    try:
        zfs_staging = find_zfs_staging(
            tc,
            kernel_name,
            staged_zfs,
            recorded=meta.get("zfs_dir") if isinstance(meta, dict) else None,
        )
    except ZfsBuildError as e:
        die(
            f"{e}\n  Rebuild Lustre with it: ltvm build lustre {target} "
            f"--lustre-tree {build} --kernel {kernel_name} "
            f"{rebuild_flag(staged_zfs)} --force"
        )
    print(f"    ZFS: {staged_zfs} ({target})")
    return zfs_staging


def cmd_cluster_deploy(args: argparse.Namespace) -> None:
    cluster = ClusterInfo.load(args.name)
    nodes = cluster.get_nodes()
    src = Path(args.lustre_source).expanduser().resolve()
    _validate_lustre_source(src)
    build = str(src)
    vm_claim.require_all([n.name for n in nodes], "deploy to")
    try:
        for n in nodes:
            vm_claim.auto_claim(n.name, build)
    except vm_claim.ClaimHeld as e:
        die(str(e))

    # The nodes built for and deployed to.  The rest of the cluster keeps
    # the Lustre it has, which is how one cluster runs two trees (2.15
    # servers under a master client): every node still gets the cluster's
    # local.sh, /etc/hosts and pdsh.
    server_only = bool(getattr(args, "server_only", False))
    client_only = bool(getattr(args, "client_only", False))
    if server_only and client_only:
        die("--server-only and --client-only exclude each other")
    deploy_nodes = nodes
    if server_only:
        deploy_nodes = [n for n in nodes if n.is_server]
    elif client_only:
        # A node that is also a server would lose its server build.
        deploy_nodes = [n for n in nodes if n.is_client and not n.is_server]
    if not deploy_nodes:
        die(
            f"cluster {cluster.name!r} has no "
            f"{'server' if server_only else 'client-only'} nodes to deploy to"
        )
    deploying = {n.name for n in deploy_nodes}

    # Before the build: a cluster whose MGS cannot name its NID is an
    # error to report now, not after every node is deployed.
    ip_family = getattr(args, "ip_family", None) or cluster.ip_family or "ipv4"
    lnet = probe_mgs_lnet(cluster, ip_family)

    # Group the nodes by what their Lustre build depends on, from each
    # VM's own metadata: a cluster may mix targets (e.g. 4 KiB-page
    # servers with a 16 KiB-page client), and build-lustre needs the
    # kernel tree and arch the VM actually runs -- not just the target's
    # defaults.
    groups: dict[tuple[str, str | None, str, str], list[str]] = {}
    node_families: dict[str, str] = {}
    for n in nodes:
        try:
            vm = VMInfo.load(n.name)
        except VMNotFound as e:
            die(f"cluster {cluster.name!r}: cannot load node: {e}")
        target = vm.os_id or DEFAULT_TARGET
        # Let ValueError from TargetConfig propagate up with a clear
        # error.  Previously a broad `except (..., ValueError, ...): pass`
        # silently fell back to DEFAULT_TARGET, which deployed rocky9 .ko
        # files onto whatever the cluster actually ran.
        if vm.os_id:
            from .target_config import TargetConfig

            try:
                node_families[n.name] = TargetConfig(
                    target, arch=vm.arch or None
                ).os_family
            except ValueError as e:
                die(f"cluster {cluster.name!r}: target {target!r}: {e}")
        else:
            node_families[n.name] = "rhel"
        # vm.kernel points at .../kernels/<name>/vmlinuz; extract <name>
        kernel_name = Path(vm.kernel).parent.name if vm.kernel else None
        key = (target, kernel_name, vm.arch or "x86_64", vm.variant)
        if n.name in deploying:
            groups.setdefault(key, []).append(n.name)
    os_family = _cluster_os_family(node_families)

    print(f"=== Deploying to cluster '{cluster.name}' ===")
    print(f"    Build: {build}")
    if len(deploy_nodes) < len(nodes):
        kept = [n.name for n in nodes if n.name not in deploying]
        print(
            f"    {'Servers' if server_only else 'Clients'} only: "
            f"{', '.join(sorted(deploying))}; leaving {', '.join(kept)} as "
            f"they are"
        )

    # Build Lustre from source before deploying, once per distinct
    # target+kernel+arch+variant: nodes sharing one rsync from the same
    # staging dir.
    # --zfs-version implies --zfs; --fstype zfs implies both, since
    # the cluster cannot mount a ZFS target without ZFS on its nodes.
    # And --zfs on a deploy means "run on ZFS" unless --fstype says
    # otherwise -- same rule as single-node deploy-lustre.
    # Without --fstype or --zfs the cluster stays on the backend its
    # last deploy ran it on.
    fstype = getattr(args, "fstype", None)
    zfs_version_arg = getattr(args, "zfs_version", None)
    zfs_source = getattr(args, "zfs_source", None)
    if zfs_source and zfs_version_arg:
        die("pass --zfs-version or --zfs-source, not both")
    if zfs_source:
        zfs_source = str(Path(zfs_source).expanduser().resolve())
    ship_zfs = (
        bool(getattr(args, "zfs", False))
        or bool(zfs_version_arg)
        or bool(zfs_source)
    )
    userspace_only = bool(getattr(args, "userspace_only", False))
    if ship_zfs and userspace_only:
        die(
            "--zfs and --userspace-only are incompatible: a "
            "userspace-only deploy ships no kernel modules (--fstype zfs "
            "alone keeps a cluster that already has ZFS on it)"
        )
    if ship_zfs and client_only:
        die(
            "--zfs ships ZFS for the servers' targets, and --client-only "
            "deploys to no server"
        )
    if ship_zfs and fstype is None:
        fstype = "zfs"
    if fstype is None:
        fstype = cluster.fstype or _deployed_fstype(cluster, os_family)
    if fstype is None:
        fstype = "ldiskfs"
    # A pure client never mounts a ZFS target.
    want_zfs = ship_zfs or (
        fstype == "zfs" and not userspace_only and not client_only
    )
    ost_count_arg = getattr(args, "ost_count", None)
    if ost_count_arg is not None and ost_count_arg < 0:
        die("--ost-count must be 0 or more")
    ost_count = cluster.ost_count if ost_count_arg is None else ost_count_arg

    from .lustre_build import staging_lock
    from .lustre_build import staging_path as _staging_path

    if userspace_only:
        print("--- Userspace-only deploy (skipping build and kernel modules)")
    for (target, kernel_name, arch, variant), members in groups.items():
        which = f" for {', '.join(members)}" if len(groups) > 1 else ""
        if userspace_only:
            # As single-node deploy --userspace-only: no build, the staging
            # as it stands plus any edited scripts copied into it.
            if not kernel_name:
                die(f"cluster {cluster.name!r}: {members[0]} has no kernel")
            staging = _staging_path(
                build, target, arch=arch, kernel=kernel_name, variant=variant
            )
            if not staging.is_dir():
                die(
                    f"No staging for {target}{which} -- run: ltvm build "
                    f"lustre {target} --lustre-tree {build} --kernel "
                    f"{kernel_name}"
                )
            from .cli.deploy import _refresh_staged_sources

            with staging_lock(staging, exclusive=True):
                err = _refresh_staged_sources(
                    staging, src, userspace_only=True, quiet=False
                )
            if err is not None:
                die(err)
            continue

        build_cmd = [
            *_ltvm(),
            "build",
            "lustre",
            target,
            "--lustre-tree",
            build,
        ]
        if kernel_name:
            build_cmd += ["--kernel", kernel_name]
        # Forward --arch unconditionally.  Comparing against the literal
        # "x86_64" is wrong for a target whose default arch is something
        # else -- see the matching cmd_deploy comment.
        build_cmd += ["--arch", arch]
        # Each node deploys from its variant's staging dir, which a base
        # build does not write.
        if variant != "base":
            build_cmd += ["--variant", variant]
        if getattr(args, "force_compat", False):
            build_cmd += ["--force-compat"]
        # Without this a tree last built with `build lustre --configure`
        # is reconfigured with the defaults by every deploy.
        configure = getattr(args, "configure", None)
        if configure:
            build_cmd += [f"--configure={configure}"]
        if want_zfs:
            build_cmd += ["--zfs"]
            if zfs_source:
                build_cmd += ["--zfs-source", zfs_source]
            elif zfs_version_arg:
                build_cmd += ["--zfs-version", zfs_version_arg]
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            build_cmd = ["sudo", "-u", sudo_user] + build_cmd
        # Flush first: with stdout not a terminal, this and the deploy
        # header above would otherwise land after the build's own output.
        print(
            f"--- Building Lustre against {target} kernel tree{which}...",
            flush=True,
        )
        rb = subprocess.run(build_cmd)
        if rb.returncode != 0:
            die(f"Lustre build failed (rc={rb.returncode})")

    # Shared from here to the end of the streams: they may run beside
    # another deploy's, but no other build or refresh may rewrite these
    # stagings under them.  No kernel means that group's nodes fail anyway.
    zfs_by_node: dict[str, Path | None] = {}
    with contextlib.ExitStack() as stream_locks:
        for (target, kernel_name, arch, variant), members in groups.items():
            if kernel_name:
                stream_locks.enter_context(
                    staging_lock(
                        _staging_path(
                            build,
                            target,
                            arch=arch,
                            kernel=kernel_name,
                            variant=variant,
                        ),
                        exclusive=False,
                    )
                )
            # Which ZFS to ship is the staged build's decision (osd_zfs.ko
            # is linked against one specific build), so resolve it once
            # per build rather than per node.
            zfs_staging: Path | None = None
            if want_zfs:
                zfs_staging = _staged_zfs(
                    build, target, kernel_name, arch, variant, zfs_version_arg
                )
            for m in members:
                zfs_by_node[m] = zfs_staging

        print(f"    Deploying to {len(deploy_nodes)} nodes in parallel...")

        # Deploy to the nodes in parallel -- same as single-node deploy,
        # just run concurrently.
        failed = _parallel_cluster_op(
            deploy_nodes,
            lambda node: _deploy_one_node(
                node.name,
                build,
                node_families[node.name],
                zfs_by_node.get(node.name),
                userspace_only=userspace_only,
                need_zfs=(
                    userspace_only and fstype == "zfs" and node.is_server
                ),
            ),
            success_verb="deployed",
            failure_verb="FAILED",
        )

    if failed:
        die(f"deploy failed for: {', '.join(failed)}")

    # Record successful deploys on each VMInfo so `ltvm list` shows
    # last_deploy/build_path/kver -- single-node cmd_deploy does this
    # at the end of its flow; cluster deploy was missing it.
    import time as _time

    _now = int(_time.time())
    unrecorded: list[str] = []
    for node in deploy_nodes:
        try:
            vm = VMInfo.load(node.name)
            vm.update_deploy(_now, build, vm.kver)
        except VMNotFound:
            pass
        except PermissionError:
            # Same as single-node deploy: cluster deploy is unprivileged
            # and update_deploy never prompts, so a root-owned sockets/
            # without cached sudo costs only the bookkeeping.
            unrecorded.append(node.name)
    if unrecorded:
        print(
            f"  Warning: deploy metadata not recorded for "
            f"{', '.join(unrecorded)} (sockets dir is root-owned and "
            f"sudo needs a password); the deploy itself succeeded.",
            file=sys.stderr,
        )

    # After each node's own disk block, so the cluster topology wins.
    disk_sizes = {}
    for node in nodes:
        try:
            disk_sizes[node.name] = VMInfo.load(node.name).disk_size
        except VMNotFound:
            pass
    ssh_opts = SSH_OPTS
    try:
        node_ips = {node.name: VMInfo.load(node.name).ip for node in nodes}
    except VMNotFound as e:
        die(f"cluster node missing: {e}")
    # Also when going back to 0, to remove the files.
    ost_files = None
    if ost_count or ost_count_arg is not None:
        ost_files = plan_ost_files(
            cluster,
            ost_count,
            node_ips,
            disk_sizes,
            prune=not userspace_only,
        )
    local_sh = generate_local_sh(
        cluster,
        os_family=os_family,
        fstype=fstype,
        lnet=lnet,
        disk_sizes=disk_sizes,
        ost_count=ost_count,
        ost_files=ost_files,
    )
    print("\n--- Distributing cluster config (local.sh)...")
    print(local_sh)

    # Again on every deploy: a cluster made before create wrote it, or a
    # node whose write failed then, gets it here.
    failed_hosts = _distribute_cluster_hosts(cluster.name, nodes, node_ips)
    if failed_hosts:
        die(f"/etc/hosts update failed for: {', '.join(failed_hosts)}")
    failed_pdsh = _parallel_cluster_op(
        nodes,
        lambda node: _ensure_pdsh(
            node.name, node_ips[node.name], node_families[node.name]
        ),
        success_verb="pdsh ready",
        failure_verb="pdsh install FAILED",
    )
    if failed_pdsh:
        die(
            f"pdsh is missing on {', '.join(failed_pdsh)} and could not be "
            f"installed; the framework runs every remote command through "
            f"it.  Rebuild the image (ltvm build image <target>) and "
            f"recreate the cluster."
        )
    failed_helpers = _parallel_cluster_op(
        nodes,
        lambda node: _install_guest_helpers(node.name, node_ips[node.name]),
        success_verb="helpers installed",
        failure_verb="helper install FAILED",
    )
    if failed_helpers:
        die(f"guest helper install failed for: {', '.join(failed_helpers)}")
    failed_links = _link_lustre_libdirs(nodes, node_ips, node_families)
    if failed_links:
        die(f"Lustre directory links failed for: {', '.join(failed_links)}")
    failed_sh = _parallel_cluster_op(
        nodes,
        lambda node: _write_cluster_local_sh(
            node.name,
            node_ips[node.name],
            local_sh,
            ssh_opts,
            node_families[node.name],
        ),
        success_verb="local.sh written",
        failure_verb="local.sh FAILED",
    )

    if failed_sh:
        die(f"local.sh distribution failed for: {', '.join(failed_sh)}")

    # Only once every node holds the new local.sh, so the record always
    # describes what the nodes run.
    if (
        cluster.ip_family != ip_family
        or cluster.ost_count != ost_count
        or cluster.fstype != fstype
    ):
        cluster.ip_family = ip_family
        cluster.ost_count = ost_count
        cluster.fstype = fstype
        try:
            cluster.save()
        except OSError as e:
            print(
                f"    warning: cannot record ip_family={ip_family} "
                f"ost_count={ost_count} fstype={fstype}: {e}"
            )

    if args.mount:
        print("=== Mounting Lustre filesystem ===")
        timeout = getattr(args, "mount_timeout", None)
        if timeout is None:
            from ltvm_pkg.deploy import llmount_timeout

            targets = sum(n.mdt_disks for n in cluster.mds_nodes()) + (
                ost_count or sum(n.ost_disks for n in cluster.oss_nodes())
            )
            timeout = llmount_timeout(targets, zfs=fstype == "zfs")
        _run_llmount(
            cluster,
            os_family,
            server_only=server_only,
            timeout=timeout,
            ost_count=ost_count,
        )
        print(
            "=== Lustre servers mounted ==="
            if server_only
            else "=== Lustre mounted ==="
        )

    print(f"\n=== Cluster '{cluster.name}' deployed ===")


def _require_cluster_block(cluster: ClusterInfo, os_family: str) -> None:
    """Refuse to mount from a node whose cfg/local.sh does not describe
    the cluster: llmount.sh would format a filesystem on that node alone
    and report success."""
    from .deploy import read_kept_blocks

    node = cluster.local_node()
    try:
        blocks = read_kept_blocks(VMInfo.load(node.name).ip, os_family)
    except subprocess.TimeoutExpired:
        return
    if CLUSTER_BLOCK_BEGIN not in blocks:
        die(
            f"{node.name}'s cfg/local.sh has no cluster configuration, so "
            f"llmount.sh would format a single-node filesystem on it.  "
            f"Write it first: ltvm cluster deploy {cluster.name} --build "
            f"<lustre tree> --userspace-only"
        )


# llmount.sh --server-only, or what that does for a llmount.sh without
# the option (before 2.15.56, so every 2.15.x release), which hands its
# arguments to init_test_env: that prints its usage and exits 0, having
# mounted nothing.  setupall's server_only argument is older.
_SERVER_ONLY_MOUNT = (
    "if grep -q -e --server-only llmount.sh; then "
    "exec bash llmount.sh --server-only; fi; "
    'echo "ltvm: this llmount.sh has no --server-only; mounting the '
    'servers through test-framework.sh"; '
    '. "$LUSTRE/tests/test-framework.sh"; init_test_env; '
    '[ -z "$NOFORMAT" ] && formatall; '
    "setupall server_only"
)

# Lustre mounts on a node: its targets, and its clients (whose device is
# the MGS NID and fsname, nid:/fsname).
_COUNT_MOUNTS = (
    'awk \'$3 == "lustre" || $3 == "lustre_tgt" '
    '{ if (index($1, ":/")) c++; else t++ } '
    "END { print t + 0, c + 0 }' /proc/mounts"
)


def expected_targets(cluster: ClusterInfo, ost_count: int = 0) -> int:
    """How many targets llmount.sh mounts, as the cluster block counts
    them: the MDTs, the OSTs, and an MGS that is not on the MDS."""
    mdts = sum(n.mdt_disks for n in cluster.mds_nodes())
    osts = ost_count or sum(n.ost_disks for n in cluster.oss_nodes())
    return mdts + osts + (0 if cluster.mgs_node().is_mds else 1)


def _check_mounted(
    cluster: ClusterInfo,
    node: ClusterNode,
    *,
    server_only: bool,
    ost_count: int,
) -> None:
    """Die unless the cluster's targets, and unless ``server_only`` a
    client on ``node``, are mounted.

    llmount.sh's exit status is not enough: one that does not know an
    option may print its usage and exit 0, or ignore it and mount
    something else.
    """
    servers = {
        n.name for n in cluster.get_nodes() if n.is_mgs or n.is_mds or n.is_oss
    }
    targets: dict[str, int] = {}
    clients = 0
    for name in sorted(servers | {node.name}):
        try:
            r = run(
                sshpass_ssh_argv(VMInfo.load(name).ip, _COUNT_MOUNTS),
                capture_output=True,
                timeout=60,
            )
            t, c = (int(x) for x in r.stdout.split())
        except (subprocess.TimeoutExpired, ValueError) as e:
            die(f"cannot read the Lustre mounts on {name}: {e}")
        if name in servers:
            targets[name] = t
        if name == node.name:
            clients = c
    want = expected_targets(cluster, ost_count)
    found = sum(targets.values())
    problems = []
    if found < want:
        per_node = ", ".join(f"{k} {v}" for k, v in sorted(targets.items()))
        problems.append(f"{found} of {want} targets are mounted ({per_node})")
    if not server_only and not clients:
        problems.append(f"no client is mounted on {node.name}")
    if problems:
        die(
            f"the mount exited 0, but {' and '.join(problems)}; the output "
            f"above says why"
        )


def _run_llmount(
    cluster: ClusterInfo,
    os_family: str,
    *,
    cleanup: bool = False,
    server_only: bool = False,
    timeout: int = 300,
    ost_count: int | None = None,
) -> None:
    """Run llmount.sh, or with ``cleanup`` llmountcleanup.sh, for the cluster.

    The cluster block in cfg/local.sh names every node, so the one run
    brings the whole cluster up or down, remote module loads included.
    It runs on the first client, the node that block treats as local: run
    anywhere else, llmount.sh also mounts a client there that
    llmountcleanup.sh never unmounts, and lustre_rmmod then fails on it.
    A servers-only mount mounts no client, so it runs on the MGS node
    with the servers' own scripts: the clients may hold another Lustre,
    or none yet.
    """
    from .cluster_failover import require_no_moved_facets

    require_no_moved_facets(cluster, "unmount" if cleanup else "mount")
    node = cluster.local_node()
    if server_only and not cleanup:
        node = cluster.mgs_node()
    node_vm = VMInfo.load(node.name)
    lustre_dir = lustre_libdir(os_family)
    script = "llmountcleanup.sh" if cleanup else "llmount.sh"
    cmd = f"cd {lustre_dir}/tests && LUSTRE={lustre_dir} bash {script}"
    if cleanup:
        cmd += " && lustre_rmmod"
    elif server_only:
        cmd = (
            f"cd {lustre_dir}/tests && LUSTRE={lustre_dir} bash -c "
            f"{shlex.quote(_SERVER_ONLY_MOUNT)} llmount.sh"
        )

    if not cleanup:
        # A mounted cluster first comes down as llumount takes it down,
        # from the same node; then every node is swept for what that
        # left, dm-flakey maps above all, which hold the disks and fail
        # the format in llmount.sh.
        from .deploy import premount_sweep_script

        down = cluster.local_node()
        print(f"Cleaning up previous Lustre state from {down.name}...")
        with contextlib.suppress(subprocess.TimeoutExpired):
            run(
                sshpass_ssh_argv(
                    VMInfo.load(down.name).ip,
                    f"cd {lustre_dir}/tests && LUSTRE={lustre_dir} "
                    "bash llmountcleanup.sh; true",
                ),
                capture_output=True,
                timeout=timeout,
            )
        for n in cluster.get_nodes():
            with contextlib.suppress(subprocess.TimeoutExpired):
                run(
                    sshpass_ssh_argv(
                        VMInfo.load(n.name).ip, premount_sweep_script()
                    ),
                    capture_output=True,
                    timeout=180,
                )

    print(f"Running {script} from {node.name}...", flush=True)
    try:
        r = run(
            sshpass_ssh_argv(node_vm.ip, cmd),
            capture_output=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        die(
            f"{script} timed out after {e.timeout}s on {node.name}\n"
            f"  check `ssh {node.name} dmesg` and "
            f"`ltvm cluster exec {cluster.name} mds 'lctl dl'`; if it was "
            f"still making progress, rerun with a longer limit: "
            f"`ltvm cluster llmount {cluster.name} --timeout <seconds>`",
            EXIT_TIMEOUT,
        )
    if r.returncode != 0:
        die(f"{script} failed (rc={r.returncode})")
    if not cleanup:
        _check_mounted(
            cluster,
            node,
            server_only=server_only,
            ost_count=cluster.ost_count if ost_count is None else ost_count,
        )


def cmd_cluster_llmount(args: argparse.Namespace) -> None:
    cluster = ClusterInfo.load(args.name)
    vm_claim.require_all(
        [n.name for n in cluster.get_nodes()],
        "unmount" if getattr(args, "cleanup", False) else "mount",
    )
    down = [n.name for n in cluster.get_nodes() if _node_state(n.name) != "up"]
    if down:
        die(
            f"cluster '{cluster.name}' has nodes not running: "
            f"{', '.join(down)}\n"
            f"  start them with: ltvm cluster start {cluster.name}",
            EXIT_UNREACHABLE,
        )
    from .vm_commands import _os_family_for_vm

    mgs_vm = VMInfo.load(cluster.mgs_node().name)
    os_family = _os_family_for_vm(mgs_vm, "libdir")
    if not args.cleanup:
        _require_cluster_block(cluster, os_family)
    _run_llmount(
        cluster,
        os_family,
        cleanup=args.cleanup,
        server_only=args.server_only,
        timeout=args.timeout,
    )


def cmd_cluster_destroy(args: argparse.Namespace) -> None:
    # Nodes go through the single-VM destroy, so a cluster node is torn
    # down exactly as `ltvm destroy <node>` would do it.
    from .vm_commands import cmd_destroy

    for name in args.names:
        try:
            cluster = ClusterInfo.load(name)
        except ClusterNotFound:
            # Match `ltvm destroy`: a name that is already gone is not
            # an error, so a cleanup script can run it unconditionally.
            print(f"destroy: cluster {name} not found")
            continue
        vm_claim.require_all([n.name for n in cluster.get_nodes()], "destroy")
        print(f"=== Destroying cluster '{cluster.name}' ===")
        cmd_destroy(
            argparse.Namespace(
                names=[n.name for n in cluster.get_nodes()],
                drop_cluster_records=False,
            )
        )
        cluster.path.unlink(missing_ok=True)
        print(f"=== Cluster '{cluster.name}' destroyed ===")


def _node_state(name: str) -> str:
    """ "up" / "down" / "missing" / "corrupt" for one cluster member.

    ValueError is VMInfo.load's deliberate failure on a truncated or
    hand-edited .info; one casualty must not take out a whole listing.
    """
    try:
        return "up" if is_running(VMInfo.load(name)) else "down"
    except VMNotFound:
        return "missing"
    except ValueError:
        return "corrupt"


def cmd_cluster_list(args: argparse.Namespace) -> None:
    use_json = bool(getattr(args, "json", False))
    clusters: list[dict[str, Any]] = []
    for cname in ClusterInfo.all_names():
        try:
            cluster = ClusterInfo.load(cname)
        except ClusterNotFound:
            # Race: .cluster file disappeared between all_names() and load().
            continue
        except (RuntimeError, ValueError) as e:
            # Corrupt or malformed cluster file -- flag it and keep
            # listing the rest rather than crashing.  Better to see
            # "clusterA: <BROKEN: ...>" than to get nothing at all.
            clusters.append({"cluster": cname, "error": str(e)})
            continue
        clusters.append(
            {
                "cluster": cname,
                "owner_id": cluster.owner_id,
                "nodes": [
                    {
                        "name": n.name,
                        "roles": list(n.roles),
                        "state": _node_state(n.name),
                    }
                    for n in cluster.get_nodes()
                ],
            }
        )

    if use_json:
        print(json.dumps({"clusters": clusters}, indent=2))
        return
    if not clusters:
        print("(no clusters)")
        return
    for c in clusters:
        if "error" in c:
            print(f"{c['cluster']}: <BROKEN: {c['error']}>")
            continue
        summary = " ".join(
            f"{n['name']}({'+'.join(n['roles'])},{n['state']})"
            for n in c["nodes"]
        )
        print(f"{c['cluster']}: {summary} owner={c['owner_id'] or '-'}")


def cmd_cluster_status(args: argparse.Namespace) -> None:
    use_json = bool(getattr(args, "json", False))
    cluster = ClusterInfo.load(args.name)
    nodes = cluster.get_nodes()
    rows = [
        {
            "name": node.name,
            "ip": node.ip,
            "state": _node_state(node.name),
            "roles": list(node.roles),
            "mdt_disks": node.mdt_disks,
            "ost_disks": node.ost_disks,
            "mgs_disk": bool(node.is_mgs and not node.is_mds),
            "failover_for": node.failover_for or None,
        }
        for node in nodes
    ]
    from .cluster_lustre_state import (
        lustre_state,
        print_lustre_state,
        wait_recovery,
    )

    wait = getattr(args, "wait_recovery", None)
    if wait is None:
        lustre, recovered = lustre_state(cluster), None
    else:
        lustre, recovered = wait_recovery(cluster, wait)

    if use_json:
        print(
            json.dumps(
                {
                    "cluster": cluster.name,
                    "owner_id": cluster.owner_id,
                    "ip_family": cluster.ip_family or "ipv4",
                    "ost_count": cluster.ost_count or None,
                    "nodes": rows,
                    "lustre": lustre,
                    **({} if recovered is None else {"recovered": recovered}),
                },
                indent=2,
            )
        )
        if recovered is False:
            raise SystemExit(1)
        return

    print(f"cluster: {cluster.name}")
    print(f"owner:   {cluster.owner_id or '-'}")
    print(f"family:  {cluster.ip_family or 'ipv4'}")
    if cluster.ost_count:
        print(f"osts:    {cluster.ost_count} (--ost-count)")
    print(f"nodes:   {len(nodes)}")
    print()

    # The human path walks the nodes, not `rows`: a dict of mixed value
    # types comes back out as `object`, and "+".join(object) is not a
    # thing mypy will accept -- nor should it.
    for node in nodes:
        status = "running" if _node_state(node.name) == "up" else "stopped"
        disks = ""
        if node.mdt_disks:
            disks += f" mdt={node.mdt_disks}"
        if node.ost_disks:
            disks += f" ost={node.ost_disks}"
        if node.is_mgs and not node.is_mds:
            disks += " mgs=1"
        if node.failover_for:
            disks += f" disks of {node.failover_for}"
        roles = "+".join(node.roles)
        print(
            f"  {node.name:<20} {node.ip:<18} {status:<8} {roles:<12}{disks}",
        )
    print_lustre_state(lustre)
    if recovered is False:
        die(f"recovery did not end in {wait}s")


def cmd_cluster_ssh(args: argparse.Namespace) -> None:
    cluster = ClusterInfo.load(args.name)
    target = args.target
    nodes = cluster.get_nodes()

    # ssh opens one interactive session, so a role selects its first
    # node (unlike `cluster exec`, which fans out).  Prefer an exact
    # node-name match so an explicit name is never shadowed by a role.
    exact = [n for n in nodes if n.name == target]
    by_role = [n for n in nodes if target in n.roles]
    candidates = exact or by_role
    matched = candidates[0] if candidates else None
    if matched is None:
        die(
            f"no node matching '{target}' in cluster '{args.name}'",
        )
        raise AssertionError("unreachable")

    vm = VMInfo.load(matched.name)
    ssh_args = [
        *sshpass_argv(),
        "ssh",
        *SSH_OPTS,
        f"root@{vm.ip}",
        *args.command,
    ]
    os.execvp("sshpass", ssh_args)


def cmd_cluster_exec(args: argparse.Namespace) -> None:
    cluster = ClusterInfo.load(args.name)
    target = args.target
    nodes = cluster.get_nodes()

    # A role names every node holding it.  Running on only the first
    # match reported one OSS out of three and exited 0, which reads as
    # "the whole role is healthy" -- and the docs describe this exact
    # command as "run a command on all OSS nodes".  An exact node name
    # still selects exactly that node.
    exact = [n for n in nodes if n.name == target]
    matches = exact or [n for n in nodes if target in n.roles]
    if not matches:
        die(
            f"no node matching '{target}' in cluster '{args.name}'",
        )
        raise AssertionError("unreachable")

    # Two idioms the user might type:
    #   ltvm cluster exec co2 oss 'lctl dl'     -> argv=['lctl dl']
    #   ltvm cluster exec co2 oss lctl dl       -> argv=['lctl', 'dl']
    #
    # shlex.join unconditionally quotes each arg, so the first form
    # becomes "'lctl dl'" -- a single shell token the remote bash
    # reads as a command name containing a space ("lctl dl: command
    # not found", rc=127).  When the argv is already one pre-joined
    # shell string (the user did the quoting), pass it verbatim;
    # otherwise safely quote each argv element so spaces / globs /
    # quotes inside individual args survive transport.
    if len(args.command) == 1:
        command = args.command[0]
    else:
        command = shlex.join(args.command)
    use_json = bool(getattr(args, "json", False))
    multi = len(matches) > 1
    worst = 0
    # Under --json the per-node output is collected and emitted as one
    # document at the end rather than streamed: a caller parsing stdout
    # cannot use a stream of remote command output interleaved with
    # "--- node ---" separators.  The human path is unchanged, and
    # still streams.
    results: list[dict[str, Any]] = []
    vm_claim.require_all([n.name for n in matches], "run commands on")
    for node in matches:
        vm = VMInfo.load(node.name)
        if multi and not use_json:
            print(f"--- {node.name} ---")
        try:
            r = run_ssh(vm.ip, command, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            if not multi and not use_json:
                die(f"timeout after {args.timeout}s", EXIT_TIMEOUT)
            results.append(
                {
                    "node": node.name,
                    "rc": EXIT_TIMEOUT,
                    "timed_out": True,
                    "stdout": "",
                    "stderr": f"timeout after {args.timeout}s",
                }
            )
            if not use_json:
                print(
                    f"{node.name}: timeout after {args.timeout}s",
                    file=sys.stderr,
                )
            worst = EXIT_TIMEOUT
            continue
        results.append(
            {
                "node": node.name,
                "rc": r.returncode,
                "timed_out": False,
                "stdout": r.stdout or "",
                "stderr": r.stderr or "",
            }
        )
        if not use_json:
            if r.stdout:
                print(r.stdout, end="")
            if r.stderr:
                print(r.stderr, end="", file=sys.stderr)
        # Exit non-zero if any node did; a role-wide command that
        # failed somewhere must not look like a clean run.
        if r.returncode != 0 and worst == 0:
            worst = r.returncode
    if use_json:
        print(
            json.dumps(
                {
                    "cluster": args.name,
                    "target": target,
                    "command": command,
                    "rc": worst,
                    "nodes": results,
                },
                indent=2,
            )
        )
    sys.exit(worst)


def cmd_cluster_krb5(args: argparse.Namespace) -> None:
    from .cluster_krb5 import cmd_cluster_krb5 as krb5

    krb5(args)


def cmd_cluster_failover(args: argparse.Namespace) -> None:
    from .cluster_failover import cmd_cluster_failover as failover

    failover(args)


def cmd_cluster_failback(args: argparse.Namespace) -> None:
    from .cluster_failover import cmd_cluster_failback as failback

    failback(args)
