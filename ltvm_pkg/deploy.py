"""Deploy Lustre staging tree to a running VM.

Shared by single-node deploy (cli.py cmd_deploy) and
multi-node cluster deploy (vm_cluster.py).
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .paths import read_modinfo_field
from .vm_net import (
    SSH_CONNECT_RETRY_DELAYS,
    SSH_KEEPALIVE_OPTS,
    run_ssh,
    ssh_never_connected,
    sshpass_ssh_argv,
)
from .vm_state import (
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    VMInfo,
    VMNotFound,
    lustre_libdir,
)

DEPLOY_TIMEOUT_ENV = "LTVM_DEPLOY_TIMEOUT"
# A deploy's guest steps are short (depmod, ldconfig, rewriting
# cfg/local.sh) but a guest on a host at load 60 has taken over a minute
# for depmod alone.  A dead guest fails sooner, through ssh's keepalives.
DEFAULT_STEP_TIMEOUT = 600


def step_timeout() -> int:
    """Seconds one guest step of a deploy may take.

    Also how long the tree transfer may go without moving a byte.
    LTVM_DEPLOY_TIMEOUT overrides it.
    """
    raw = os.environ.get(DEPLOY_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
        print(
            f"warning: ignoring {DEPLOY_TIMEOUT_ENV}={raw!r}: not a whole "
            f"number of seconds > 0",
            file=sys.stderr,
        )
    return DEFAULT_STEP_TIMEOUT


def timeout_hint() -> str:
    return f"raise {DEPLOY_TIMEOUT_ENV}=<seconds> on a slow host"


def llmount_timeout(targets: int, *, zfs: bool = False) -> int:
    """A time limit for llmount.sh that grows with what it formats.

    Each target is formatted and mounted in turn, and on ZFS each is a
    zpool create as well: 1 MDT and 4 OSTs took 10 minutes on a loaded
    host.  The framework's own default is 1 MDT and 2 OSTs.
    """
    per_target = 180 if zfs else 60
    return 180 + per_target * (targets or 3)


_CHUNK = 1 << 20


def _compressor() -> list[str]:
    # pigz writes the same format on every core; gzip -1 alone is about
    # 100 MB/s, as fast as ssh to a VM on an idle host.
    return [shutil.which("pigz") or "gzip", "-1", "-c"]


class _NotConnected(RuntimeError):
    pass


def _stream_tree(
    vm: VMInfo, tree: Path, *, exclude_modules: bool = False
) -> None:
    """_stream_tree_once, again when its ssh never got in."""
    for delay in SSH_CONNECT_RETRY_DELAYS:
        try:
            _stream_tree_once(vm, tree, exclude_modules=exclude_modules)
            return
        except _NotConnected as e:
            print(f"{e}; retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)
    try:
        _stream_tree_once(vm, tree, exclude_modules=exclude_modules)
    except _NotConnected as e:
        raise RuntimeError(str(e)) from None


def _stream_tree_once(
    vm: VMInfo, tree: Path, *, exclude_modules: bool = False
) -> None:
    """tar | gzip | ssh a DESTDIR tree into the VM's /.

    ``exclude_modules`` drops lib/modules/ so kernel modules already in
    the VM are not overwritten (the --userspace-only path; depmod is
    skipped for it too).

    The stream goes through this process so that the limit is on
    progress rather than on the whole transfer: a 380 MB staging takes
    minutes to cross ssh on a loaded host, so any fixed limit on the
    whole either fails such a host or waits that long on a hung one.
    gzip -1 takes a staging to about 40% of its size.
    """
    tar_argv = ["tar", "cf", "-", "-C", str(tree)]
    if exclude_modules:
        tar_argv.append("--exclude=./lib/modules")
    if vm.rail_topology is not None:
        # The profile owns this guest configuration across staging deployments.
        tar_argv.append("--exclude=./etc/modprobe.d/lnet.conf")
    # Never ship ltvm's own build bookkeeping into the VM's /.
    tar_argv += ["--exclude=./.ltvm-*", "."]
    ssh_argv = sshpass_ssh_argv(
        vm.ip,
        "tar xzf - -C / --keep-directory-symlink --no-same-owner",
        extra_opts=["-o", "ConnectTimeout=5", *SSH_KEEPALIVE_OPTS],
    )
    stall = step_timeout()
    last = [time.monotonic()]
    stalled = threading.Event()
    done = threading.Event()
    sent = 0

    with (
        tempfile.TemporaryFile() as local_err,
        tempfile.TemporaryFile() as ssh_out,
    ):
        tar = subprocess.Popen(
            tar_argv, stdout=subprocess.PIPE, stderr=local_err
        )
        assert tar.stdout is not None
        gz = subprocess.Popen(
            _compressor(),
            stdin=tar.stdout,
            stdout=subprocess.PIPE,
            stderr=local_err,
        )
        # gzip holds the pipe now; tar must see SIGPIPE if gzip dies.
        tar.stdout.close()
        ssh = subprocess.Popen(
            ssh_argv,
            stdin=subprocess.PIPE,
            stdout=ssh_out,
            stderr=subprocess.STDOUT,
        )
        procs = (tar, gz, ssh)

        def watchdog() -> None:
            while not done.wait(min(5.0, stall / 4)):
                if time.monotonic() - last[0] > stall:
                    stalled.set()
                    for p in procs:
                        with contextlib.suppress(OSError):
                            p.kill()
                    return

        dog = threading.Thread(target=watchdog, daemon=True)
        dog.start()
        assert gz.stdout is not None and ssh.stdin is not None
        try:
            while chunk := os.read(gz.stdout.fileno(), _CHUNK):
                ssh.stdin.write(chunk)
                sent += len(chunk)
                last[0] = time.monotonic()
        except (BrokenPipeError, ValueError):
            # ssh exited, or the watchdog killed it; its status says which.
            pass
        finally:
            gz.stdout.close()
            with contextlib.suppress(BrokenPipeError, ValueError):
                ssh.stdin.close()
        tar_rc, gz_rc = tar.wait(), gz.wait()
        local_rc = tar_rc or gz_rc
        # The guest is still writing out what it has received.
        last[0] = time.monotonic()
        ssh_rc = ssh.wait()
        done.set()
        dog.join()

        if stalled.is_set():
            raise RuntimeError(
                f"tar deploy to {vm.ip} stopped: no progress for {stall}s "
                f"after {sent // (1 << 20)} MB (compressed) of {tree}; "
                f"{timeout_hint()}"
            )
        local_err.seek(0)
        local_msg = local_err.read().decode(errors="replace").strip()
        if ssh_rc != 0:
            ssh_out.seek(0)
            output = ssh_out.read().decode(errors="replace").strip()
            if ssh_never_connected(ssh_rc, output):
                raise _NotConnected(
                    f"tar deploy failed (rc={ssh_rc}): {output}"
                )
            if local_rc != 0 and local_msg:
                output += f"; {local_msg}"
            raise RuntimeError(f"tar deploy failed (rc={ssh_rc}): {output}")
        if local_rc != 0:
            raise RuntimeError(
                f"tar deploy failed reading {tree} (rc={local_rc}): {local_msg}"
            )


# Images since 2026-10-06 carry this (targets/common/build-tools.sh):
# auster by name without the tests directory on PATH, exec'd by full
# path since it finds the tree from its own $0.  Older ones get it here.
_AUSTER_WRAPPER = """#!/bin/sh
for d in /usr/lib64/lustre/tests /usr/lib/lustre/tests; do
\t[ -x "$d/auster" ] && exec "$d/auster" "$@"
done
echo "auster: Lustre tests are not installed" >&2
exit 127
"""
AUSTER_WRAPPER_SCRIPT = (
    "{ [ -e /usr/local/bin/auster ] || "
    "{ mkdir -p /usr/local/bin && printf '%s' "
    + shlex.quote(_AUSTER_WRAPPER)
    + " > /usr/local/bin/auster && chmod 755 /usr/local/bin/auster; }; }"
)


def ensure_auster_wrapper(vm: VMInfo) -> None:
    """Write the auster wrapper into an image from before it had one."""
    try:
        r = run_ssh(vm.ip, AUSTER_WRAPPER_SCRIPT, timeout=step_timeout())
    except subprocess.TimeoutExpired:
        r = None
    if r is None or r.returncode != 0:
        print(
            f"warning: could not write /usr/local/bin/auster on {vm.name}; "
            f"run auster from the Lustre tests directory there",
            file=sys.stderr,
        )


def deploy_to_vm(
    vm: VMInfo,
    staging: Path,
    *,
    os_family: str = "rhel",
    userspace_only: bool = False,
    ram_osts: int = 0,
    ram_ost_size_gb: int = 32,
    ram_mdt: bool = False,
    zfs_staging: Path | None = None,
    fstype: str | None = None,
    ost_count: int | None = None,
) -> None:
    """Stream a Lustre staging tree into a VM.

    1. tar | ssh the ZFS staging dir into / (``zfs_staging``), then the
       Lustre one -- ZFS first so one depmod covers both
    2. depmod + ldconfig
    3. Configure test disk mappings in cfg/local.sh
    4. Optionally set the OST count (``ost_count``), file-backing the
       OSTs past the VM's disks, or repoint the OSTs at brd ram devices
       (``ram_osts``), for benchmarks that want the backing store out
       of the picture.
    5. Pin FSTYPE in cfg/local.sh (``fstype``)
    6. Put back the OST count, FSTYPE and cluster blocks the staging's
       stock cfg/local.sh replaced, where this deploy did not set them

    Raises RuntimeError on failure.
    """
    try:
        _deploy_to_vm(
            vm,
            staging,
            os_family=os_family,
            userspace_only=userspace_only,
            ram_osts=ram_osts,
            ram_ost_size_gb=ram_ost_size_gb,
            ram_mdt=ram_mdt,
            zfs_staging=zfs_staging,
            fstype=fstype,
            ost_count=ost_count,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"deploy to {vm.name}: {e}; {timeout_hint()}"
        ) from None


def _deploy_to_vm(
    vm: VMInfo,
    staging: Path,
    *,
    os_family: str,
    userspace_only: bool,
    ram_osts: int,
    ram_ost_size_gb: int,
    ram_mdt: bool,
    zfs_staging: Path | None,
    fstype: str | None,
    ost_count: int | None,
) -> None:
    if not staging.is_dir():
        raise RuntimeError(f"Staging directory not found: {staging}")

    if userspace_only and fstype == "zfs":
        require_zfs_modules(vm)

    if not userspace_only:
        unload_lustre(vm)

    kept = read_kept_blocks(vm.ip, os_family)

    # ZFS first: osd_zfs.ko depends on zfs.ko, and the single depmod
    # below only resolves that if both are already on disk.
    if zfs_staging is not None and not userspace_only:
        if not zfs_staging.is_dir():
            raise RuntimeError(
                f"ZFS staging directory not found: {zfs_staging}"
            )
        _stream_tree(vm, zfs_staging, exclude_modules=False)

    _stream_tree(vm, staging, exclude_modules=userspace_only)

    # depmod + ldconfig to pick up new modules and libraries.
    post_deploy_cmd = "ldconfig" if userspace_only else "depmod -a && ldconfig"
    r = run_ssh(vm.ip, post_deploy_cmd, timeout=step_timeout())
    if r.returncode != 0:
        raise RuntimeError(
            f"post-deploy ({post_deploy_cmd}) failed "
            f"(rc={r.returncode}): {r.stderr}"
        )

    ensure_auster_wrapper(vm)

    if not userspace_only:
        verify_deployed_modules(vm, staging)

    if zfs_staging is not None and not userspace_only:
        retire_stale_zfs(vm)

    if (zfs_staging is not None and not userspace_only) or fstype == "zfs":
        configure_zfs_mmp(vm)

    # Configure test framework's local.sh with VM disk topology
    if vm.mdt_disks or vm.ost_disks:
        configure_test_disks(
            vm.ip,
            vm.mdt_disks,
            vm.ost_disks,
            vm.disk_size,
            os_family=os_family,
        )

    if ost_count is not None:
        configure_ost_count(
            vm.ip,
            ost_count,
            vm.ost_disks,
            vm.disk_size,
            os_family=os_family,
            prune=not userspace_only,
            name=vm.name,
        )

    # ... then let ram OSTs override it, if asked for.  Ordering is
    # deliberate: the virtio block stays in the file and the ram block
    # follows it, so `source cfg/local.sh` takes the ram devices.
    if ram_osts:
        configure_ram_osts(
            vm.ip,
            ram_osts,
            ram_ost_size_gb,
            ram_mdt=ram_mdt,
            os_family=os_family,
        )

    if fstype is not None:
        configure_fstype(vm.ip, fstype, os_family=os_family)

    skip = set()
    if ost_count is not None or ram_osts:
        skip.add(OST_COUNT_BEGIN)
    if fstype is not None:
        skip.add(FSTYPE_BEGIN)
    restore_kept_blocks(vm.ip, kept, skip, os_family=os_family)


def require_zfs_modules(vm: VMInfo) -> None:
    """Refuse to pin FSTYPE=zfs on a VM without ZFS and osd_zfs."""
    r = run_ssh(
        vm.ip,
        "modinfo -n zfs >/dev/null 2>&1 && modinfo -n osd_zfs >/dev/null 2>&1",
        timeout=step_timeout(),
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"{vm.name} has no ZFS or no osd_zfs module, so a "
            f"userspace-only deploy cannot run it on ZFS; deploy with "
            f"--zfs first"
        )


# A client, or a target mounted with -t lustre, is type lustre; newer
# test frameworks mount their targets as lustre_tgt.
_LUSTRE_MOUNTS = (
    'awk \'$3 == "lustre" || $3 == "lustre_tgt" {print $2}\' /proc/mounts'
)

# libcfs is under every Lustre and LNet module; ldiskfs is the one that
# is not, and lustre_rmmod unloads it too.
_UNLOAD_SCRIPT = (
    "[ -d /sys/module/libcfs ] || [ -d /sys/module/ldiskfs ] || exit 0; "
    # Reverse mount order, so a client goes before the targets under it.
    # Forced, as llmountcleanup.sh does: on a cluster the servers may be
    # going down on another node at the same time.
    f"mnts=$({_LUSTRE_MOUNTS} | tac); "
    'for m in $mnts; do umount -f "$m"; done; '
    "lustre_rmmod >/dev/null 2>&1; "
    'echo "unmounted" $mnts; '
    f'echo "mounted" $({_LUSTRE_MOUNTS}); '
    'echo "loaded" $(cd /sys/module && ls -d libcfs ldiskfs 2>/dev/null)'
)


def unload_lustre(vm: VMInfo) -> None:
    """Unmount every Lustre filesystem on the VM and unload its modules.

    New modules on disk change nothing while the old ones stay loaded,
    so skipping this leaves the VM running the previous build -- however
    its targets were mounted, by llmount.sh or by hand.  Raises when
    anything is still mounted or loaded afterwards.
    """
    restart = f"ltvm stop {vm.name} && ltvm start {vm.name}"
    try:
        r = run_ssh(vm.ip, _UNLOAD_SCRIPT, timeout=180)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"unmounting Lustre on {vm.name} timed out after {e.timeout}s; "
            f"restart it ({restart}) and deploy again"
        )
    if r.returncode != 0:
        raise RuntimeError(
            f"could not unload Lustre on {vm.name} (rc={r.returncode}): "
            f"{(r.stderr or '').strip()}"
        )
    state: dict[str, list[str]] = {}
    for line in (r.stdout or "").splitlines():
        key, _, rest = line.partition(" ")
        state[key] = rest.split()
    if not state:
        return
    mounted, loaded = state.get("mounted", []), state.get("loaded", [])
    if mounted or loaded:
        still = (
            f"still mounted on {', '.join(mounted)}"
            if mounted
            else f"still loaded ({', '.join(loaded)})"
        )
        raise RuntimeError(
            f"Lustre on {vm.name} is {still} after umount and lustre_rmmod, "
            f"so the new modules would not take effect.  Restart it "
            f"({restart}) and deploy again."
        )
    unmounted = state.get("unmounted", [])
    detail = f"; unmounted {', '.join(unmounted)}" if unmounted else ""
    print(
        f"  Unloaded the running Lustre on {vm.name}{detail}", file=sys.stderr
    )


def verify_deployed_modules(vm: VMInfo, staging: Path) -> None:
    """Check the modules on the VM are the ones we just staged.

    A deploy that silently ships stale modules is expensive: every
    test result afterwards describes the previous build.  It happens
    easily -- a build that failed leaves the old .ko in place, and the
    Lustre version string does not change between two builds of the
    same tree, so the usual "lctl get_param version" check still
    agrees.

    srcversion is a hash of the module's own sources, so it does
    distinguish them, but only per module: editing osc_cache.c leaves
    obdclass.ko byte-identical.  Checking one module is therefore not
    a proxy for the rest, which is why this compares every staged
    module against its counterpart on the VM.

    Warns rather than raises: the deploy itself did happen, and a VM
    that is merely mid-reboot should not turn into a hard failure.
    """
    staged: dict[str, str] = {}
    for ko in staging.rglob("*.ko"):
        srcver = read_modinfo_field(ko, "srcversion")
        if srcver:
            staged[ko.name] = srcver
    if not staged:
        return

    # One round trip, asking by MODULE NAME so modinfo resolves the
    # path itself: modules land under .../extra on some images and
    # .../updates on others, and compressed .ko.xz is also possible.
    names = sorted(n[:-3] for n in staged)
    script = "; ".join(
        f"printf '%s %s\\n' {shlex.quote(n)} "
        f'"$(modinfo -F srcversion {shlex.quote(n)} 2>/dev/null)"'
        for n in names
    )
    r = run_ssh(vm.ip, script, timeout=step_timeout())
    if r.returncode != 0:
        print(
            f"warning: could not verify deployed modules on {vm.name} "
            f"(rc={r.returncode}); skipping the check",
            file=sys.stderr,
        )
        return

    on_vm = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            on_vm[parts[0]] = parts[1]

    stale = [
        name
        for name, srcver in staged.items()
        # only compare modules the VM actually has loaded on disk;
        # a staged module absent there is not evidence of staleness
        if on_vm.get(name[:-3]) and on_vm[name[:-3]] != srcver
    ]
    if stale:
        shown = ", ".join(sorted(stale)[:5])
        more = "" if len(stale) <= 5 else f" (+{len(stale) - 5} more)"
        print(
            f"warning: {len(stale)} module(s) on {vm.name} do not match "
            f"the staged build: {shown}{more}\n"
            "  The VM is running different code than was just built, so "
            "test results will describe the older modules.  Check that "
            "the build actually succeeded, then redeploy.",
            file=sys.stderr,
        )


ZFS_MMP_CONF = "/etc/modprobe.d/ltvm-zfs-mmp.conf"
_ZFS_MMP_PARAM = "/sys/module/zfs/parameters/zfs_multihost_fail_intervals"


def configure_zfs_mmp(vm: VMInfo) -> None:
    """Keep ZFS from suspending a pool whose MMP writes run late.

    mkfs.lustre turns multihost on for every pool it makes, and ZFS then
    suspends a pool whose MMP writes have not landed in ten intervals,
    which Lustre's failmode=panic turns into a kernel panic.  A guest on
    a loaded host stalls that long: three servers of one cluster
    panicked within 40 minutes.  zfs_multihost_fail_intervals=0 keeps
    the MMP writes and the import check, and drops only the suspend.
    In modprobe.d for the next load, and on the loaded module now.
    """
    script = (
        f"echo 'options zfs zfs_multihost_fail_intervals=0' > {ZFS_MMP_CONF}"
        f" && {{ [ ! -e {_ZFS_MMP_PARAM} ] || echo 0 > {_ZFS_MMP_PARAM}; }}"
    )
    r = run_ssh(vm.ip, script, timeout=step_timeout())
    if r.returncode != 0:
        print(
            f"warning: could not turn off ZFS's MMP suspend on {vm.name} "
            f"({(r.stderr or '').strip()}); a stalled guest may panic",
            file=sys.stderr,
        )


def retire_stale_zfs(vm: VMInfo) -> None:
    """Unload a running zfs.ko that the new osd_zfs.ko cannot bind to.

    Modules are matched by symbol version, and installing a new zfs.ko
    on disk does not touch the one already in the kernel -- so after a
    version change osd_zfs fails to load with "disagrees about version
    of symbol ...", several steps from the deploy that caused it.

    Warns rather than raises if the old module can't go: the deploy
    itself succeeded, and stopping Lustre is the caller's call.
    """
    script = (
        "loaded=$(cat /sys/module/zfs/version 2>/dev/null || true); "
        '[ -n "$loaded" ] || exit 0; '
        "ondisk=$(modinfo -F version zfs 2>/dev/null || true); "
        '[ "$loaded" != "$ondisk" ] || exit 0; '
        'echo "retiring loaded ZFS $loaded for $ondisk"; '
        "for p in $(zpool list -H -o name 2>/dev/null); do "
        'zpool export -f "$p" 2>/dev/null; done; '
        "rmmod osd_zfs 2>/dev/null; "
        "modprobe -r zfs 2>/dev/null; "
        "cat /sys/module/zfs/version 2>/dev/null || true"
    )
    r = run_ssh(vm.ip, script, timeout=step_timeout())
    still = (r.stdout or "").strip().splitlines()
    still = [ln for ln in still if ln and not ln.startswith("retiring")]
    if still:
        print(
            f"warning: {vm.name} still has ZFS {still[-1]} loaded; the "
            f"ZFS just deployed is a different version and osd_zfs will "
            f"not load against the old one.  Stop Lustre (ltvm llmount "
            f"{vm.name} --cleanup) or reboot the VM.",
            file=sys.stderr,
        )


def configure_test_disks(
    ip: str,
    mdt_disks: int,
    ost_disks: int,
    disk_size_bytes: int = 0,
    os_family: str = "rhel",
) -> None:
    """Write OSTCOUNT/OSTDEV*/MDSCOUNT/MDSDEV*/OSTSIZE into cfg/local.sh.

    Virtio disks are attached in order (MDT first, then OST),
    starting at /dev/vdb (vda = rootfs).
    """
    testdir = f"{lustre_libdir(os_family)}/tests"
    lines = []

    # Set device sizes in KB (test framework uses KB for OSTSIZE/MDSSIZE)
    if disk_size_bytes:
        size_kb = disk_size_bytes // 1024
        if mdt_disks:
            lines.append(f"MDSSIZE={size_kb}")
        if ost_disks:
            lines.append(f"OSTSIZE={size_kb}")

    if mdt_disks:
        lines.append(env_count("MDSCOUNT", mdt_disks))
        for n in range(1, mdt_disks + 1):
            letter = chr(ord("a") + n)
            lines.append(f"MDSDEV{n}=/dev/vd{letter}")

    if ost_disks:
        lines.append(env_count("OSTCOUNT", ost_disks))
        for n in range(1, ost_disks + 1):
            letter = chr(ord("a") + mdt_disks + n)
            lines.append(f"OSTDEV{n}=/dev/vd{letter}")

    # The framework sets these in init_clients_lists(), but init_gss()
    # (SHARED_KEY setup) runs first and uses both: an empty CLIENTS sends
    # do_nodes to no_dsh, an empty CLIENTCOUNT is a bash syntax error.
    lines.append("CLIENTS=${CLIENTS:-$HOSTNAME}")
    lines.append("CLIENTCOUNT=${CLIENTCOUNT:-1}")

    snippet = "\\n".join(lines)
    cfg = f"{testdir}/cfg/local.sh"
    tmp = "/tmp/.ltvm-disk-block"
    script = (
        f"printf '# --- VM disk configuration (generated by ltvm deploy) ---\\n"
        f"{snippet}\\n"
        f"# --- END VM disk configuration ---\\n' > {tmp}; "
        + splice_local_sh_block(
            cfg,
            tmp,
            "# --- VM disk configuration",
            "# --- END VM disk configuration",
        )
        + f"; rm -f {tmp}"
    )
    r = run_ssh(ip, script, timeout=step_timeout())
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to configure test disks in {testdir}/cfg/local.sh: "
            f"{r.stderr.strip()}"
        )


# The settings ltvm's blocks write that the environment still overrides.
ENV_SAVED = ("MDSCOUNT", "OSTCOUNT", "FSTYPE")
ENV_SAVE_BEGIN = "# --- ltvm: target counts from the environment"
ENV_SAVE_END = "# --- END ltvm: target counts from the environment"


def env_count(var: str, n: int) -> str:
    """``var=n``, unless the environment set ``var`` before cfg/local.sh.

    The blocks sit after the stock ``${VAR:-default}`` lines, which have
    already filled in any unset count, so the environment's own value is
    the one ensure_env_save() kept at the top of the file.
    """
    return f"{var}=${{_ltvm_env_{var}:-{n}}}"


def ensure_env_save(cfg: str) -> str:
    """Shell that puts the lines saving the environment's settings at
    the top of ``cfg``, ahead of the stock defaults, once.

    A block from an older ltvm that saved fewer of them is replaced.
    """
    saves = "\\n".join(f"_ltvm_env_{v}=${{{v}-}}" for v in ENV_SAVED)
    return (
        f"if ! grep -q '^_ltvm_env_{ENV_SAVED[-1]}=' {cfg} 2>/dev/null; then "
        f"{{ printf '{ENV_SAVE_BEGIN} ---\\n{saves}\\n{ENV_SAVE_END} ---\\n'; "
        f"sed '/^{ENV_SAVE_BEGIN}/,/^{ENV_SAVE_END}/d' {cfg} 2>/dev/null; }} "
        f"> {cfg}.ltvmnew && mv {cfg}.ltvmnew {cfg}; fi"
    )


def splice_local_sh_block(cfg: str, block: str, begin: str, end: str) -> str:
    """Shell that puts the marked block in file ``block`` into ``cfg``.

    The block is rewritten where it already sits, or appended.  Deleting
    and re-appending would be wrong: cfg/local.sh is sourced, so the last
    assignment wins, and moving a block past a later one changes which
    wins -- that is how an llmount silently put the OSTs back on the
    virtio disks after configure_ram_osts had repointed them at brd.
    ``begin`` and ``end`` are the starts of the marker lines, matched as
    patterns by grep and awk, so they must hold no pattern characters.
    """
    return (
        f"{{ {ensure_env_save(cfg)}; "
        f"if grep -q '^{begin}' {cfg} 2>/dev/null; then "
        f"awk -v f={block} '"
        f"/^{begin}/ "
        f"{{ while ((getline l < f) > 0) print l; close(f); s=1; next }} "
        f"/^{end}/ {{ s=0; next }} "
        f"!s {{ print }}' {cfg} > {cfg}.ltvmnew "
        f"&& mv {cfg}.ltvmnew {cfg}; "
        f"else printf '\\n' >> {cfg}; cat {block} >> {cfg}; fi; }}"
    )


def configure_ram_osts(
    ip: str,
    count: int,
    size_gb: int,
    ram_mdt: bool = False,
    os_family: str = "rhel",
) -> None:
    """Back the OSTs with brd ram devices instead of virtio disks.

    For benchmarks that want the OST's backing store out of the
    measurement: a ram OST is the honest "infinitely fast disk".  It is
    not the same as the 0x238 fake-IO fail_loc, which short-circuits the
    OST's IO submission but leaves the full RPC and bulk path -- a ram
    OST runs everything and removes only the disk.

    Two brd properties this has to work around:

    * Pages are allocated on write, so ``rd_size`` is a ceiling rather
      than a reservation -- but the memory is real and is not reclaimed
      until the device goes away.  Size for the intended sweep, not for
      the node's RAM.
    * ``rd_nr``/``rd_size`` are load-time only and cannot be changed on a
      loaded module, which in turn cannot be unloaded while a device is
      held open.  So a differing geometry needs Lustre unmounted first;
      say that plainly rather than failing inside modprobe.

    The OSTDEV block is appended *after* whatever ``configure_test_disks``
    wrote.  cfg/local.sh is sourced, so the later assignment wins, and
    both blocks stay visible in the file -- which matters when someone
    is trying to work out why an OST is not where they expected.
    """
    testdir = f"{lustre_libdir(os_family)}/tests"
    env_save = ensure_env_save(f"{testdir}/cfg/local.sh")
    ndev = count + 1 if ram_mdt else count
    rd_size_kb = size_gb * 1024 * 1024
    size_kb = size_gb * 1024 * 1024

    lines = [env_count("OSTCOUNT", count), f"OSTSIZE={size_kb}"]
    for n in range(1, count + 1):
        lines.append(f"OSTDEV{n}=/dev/ram{n - 1}")
    if ram_mdt:
        lines += [
            env_count("MDSCOUNT", 1),
            f"MDSSIZE={size_kb}",
            f"MDSDEV1=/dev/ram{count}",
        ]
    snippet = "\\n".join(lines)

    script = (
        # Reload brd only when the geometry actually differs: a needless
        # rmmod would fail on a mounted filesystem for no reason.
        f"want_nr={ndev}; want_kb={rd_size_kb}; "
        f"if [ -d /sys/module/brd ]; then "
        f"  have_nr=$(ls -d /sys/block/ram* 2>/dev/null | wc -l); "
        f"  have_kb=$(( $(cat /sys/block/ram0/size 2>/dev/null || echo 0) / 2 )); "
        f'  if [ "$have_nr" -lt "$want_nr" ] || '
        f'     [ "$have_kb" -ne "$want_kb" ]; then '
        f"    rmmod brd || {{ echo 'brd is in use; unmount Lustre first' >&2; "
        f"      exit 1; }}; "
        f"    modprobe brd rd_nr=$want_nr rd_size=$want_kb || exit 1; "
        f"  fi; "
        f"else modprobe brd rd_nr=$want_nr rd_size=$want_kb || exit 1; fi; "
        # A ram device reloaded with a new geometry can still carry a
        # stale ldiskfs superblock, which mkfs.lustre refuses.
        f"for i in $(seq 0 $((want_nr - 1))); do "
        f'  [ -b /dev/ram$i ] || {{ echo "/dev/ram$i missing" >&2; exit 1; }}; '
        f"  wipefs -a /dev/ram$i >/dev/null 2>&1 || true; "
        f"done; "
        f"{env_save}; "
        f"sed -i '/^# --- RAM OST configuration/,/^# --- END RAM OST/d' "
        f"{testdir}/cfg/local.sh 2>/dev/null || true; "
        f"printf '\\n# --- RAM OST configuration (generated by ltvm deploy) ---\\n"
        f"{snippet}\\n"
        f"# --- END RAM OST configuration ---\\n' >> {testdir}/cfg/local.sh"
    )
    r = run_ssh(ip, script, timeout=step_timeout())
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to configure ram OSTs: {r.stderr.strip() or r.stdout.strip()}"
        )


OST_FILE_DIR = "/var/lib/ltvm/ost-files"
# Root space left over when every file-backed OST is full.
OST_FILE_RESERVE_KB = 1024 * 1024
OST_FILE_MIN_KB = 256 * 1024
OST_COUNT_BEGIN = "# --- OST count"
OST_COUNT_END = "# --- END OST count"


def ost_file_path(n: int) -> str:
    return f"{OST_FILE_DIR}/ost{n}"


def prepare_ost_files(ip: str, keep: list[int], *, prune: bool) -> int:
    """Make the node ready for file-backed OSTs ``keep``; return the KB
    they may share.

    That is the free space plus what the kept files hold already, since
    mkfs.lustre recreates a file-backed target empty.  With ``prune``,
    the files of every other OST are removed first.  Without either,
    nothing runs and 0 comes back.
    """
    names = [f"ost{n}" for n in keep]
    script = f"mkdir -p {OST_FILE_DIR} && cd {OST_FILE_DIR} || exit 1; "
    if prune:
        script += (
            f"keep={shlex.quote(' ' + ' '.join(names) + ' ')}; "
            'for f in ost*; do [ -e "$f" ] || continue; '
            'case "$keep" in *" $f "*) ;; *) rm -f "$f" ;; esac; done; '
        )
    if names:
        script += (
            "used=$(du -kc " + " ".join(names) + " 2>/dev/null "
            "| awk 'END {print $1}'); "
            "echo \"$(df -kP . | awk 'NR == 2 {print $4}') ${used:-0}\""
        )
    elif not prune:
        return 0
    r = run_ssh(ip, script, timeout=step_timeout())
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to prepare {OST_FILE_DIR} on {ip}: "
            f"{r.stderr.strip() or r.stdout.strip()}"
        )
    if not names:
        return 0
    try:
        avail, used = (int(x) for x in r.stdout.split()[-2:])
    except ValueError:
        raise RuntimeError(
            f"Cannot read the free space in {OST_FILE_DIR} on {ip}: "
            f"{r.stdout.strip()!r}"
        )
    return avail + used


def ost_file_kb(space_kb: int, nfiles: int, cap_kb: int) -> int:
    """OSTSIZE for ``nfiles`` file-backed OSTs sharing ``space_kb``.

    An equal share of the space after the reserve, no larger than
    ``cap_kb`` (the VM's disk size, when it has one) and rounded down to
    a MiB.  0 when the share is below OST_FILE_MIN_KB.
    """
    share = (space_kb - OST_FILE_RESERVE_KB) // nfiles
    if cap_kb:
        share = min(share, cap_kb)
    share = share // 1024 * 1024
    return share if share >= OST_FILE_MIN_KB else 0


def ost_space_error(where: str, nfiles: int, space_kb: int) -> str:
    need_mb = (nfiles * OST_FILE_MIN_KB + OST_FILE_RESERVE_KB) // 1024
    return (
        f"{nfiles} file-backed OSTs need at least {need_mb} MB free in "
        f"{OST_FILE_DIR} on {where}, which has {space_kb // 1024} MB.  "
        f"Ask for fewer OSTs, or recreate it with a larger --root-size "
        f"or more --ost-disks."
    )


def configure_ost_count(
    ip: str,
    count: int,
    ost_disks: int,
    disk_size_bytes: int = 0,
    os_family: str = "rhel",
    *,
    prune: bool = True,
    name: str = "",
) -> None:
    """Run ``count`` OSTs, file-backing those past the VM's OST disks.

    The block stays in cfg/local.sh until a deploy asks for another
    count; 0 removes it, which puts the VM back on one OST per disk.
    It is spliced in place after the disk block, which llmount and
    `suite run` rewrite in place too, so neither undoes it.  A RAM OST
    block would outrank it, being appended later, so it goes.

    ``prune`` removes the files of OSTs no longer wanted; leave it off
    when Lustre may still be running on them (--userspace-only).
    """
    testdir = f"{lustre_libdir(os_family)}/tests"
    cfg = f"{testdir}/cfg/local.sh"
    keep = list(range(ost_disks + 1, count + 1))
    space = prepare_ost_files(ip, keep, prune=prune)

    drop_ram = (
        f"sed -i '/^# --- RAM OST configuration/,/^# --- END RAM OST/d' "
        f"{cfg} 2>/dev/null || true"
    )
    if count == 0:
        script = (
            f"sed -i '/^{OST_COUNT_BEGIN}/,/^{OST_COUNT_END}/d' {cfg} "
            f"2>/dev/null || true"
        )
    else:
        lines = [env_count("OSTCOUNT", count)]
        if keep:
            size_kb = ost_file_kb(space, len(keep), disk_size_bytes // 1024)
            if not size_kb:
                raise RuntimeError(
                    ost_space_error(name or ip, len(keep), space)
                )
            # One OSTSIZE for every OST: the disk-backed ones are
            # formatted down to the files' size, so all are alike.
            lines.append(f"OSTSIZE={size_kb}")
            lines += [f"OSTDEV{n}={ost_file_path(n)}" for n in keep]
            print(
                f"  {count} OSTs: {min(count, ost_disks)} on disks, "
                f"{len(keep)} file-backed in {OST_FILE_DIR}, "
                f"{size_kb // 1024} MB each",
                file=sys.stderr,
            )
        snippet = "\\n".join(lines)
        tmp = "/tmp/.ltvm-ost-count-block"
        script = (
            f"{drop_ram}; "
            f"printf '{OST_COUNT_BEGIN} (generated by ltvm deploy) ---\\n"
            f"{snippet}\\n"
            f"{OST_COUNT_END} ---\\n' > {tmp}; "
            + splice_local_sh_block(cfg, tmp, OST_COUNT_BEGIN, OST_COUNT_END)
            + f"; rm -f {tmp}"
        )
    r = run_ssh(ip, script, timeout=step_timeout())
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to set the OST count in {cfg}: {r.stderr.strip()}"
        )


FSTYPE_BEGIN = "# --- FSTYPE"
FSTYPE_END = "# --- END FSTYPE"


def fstype_line(fstype: str) -> str:
    """``FSTYPE=fstype``, unless the environment set FSTYPE.

    Set it in the environment (``FSTYPE=ldiskfs sanity.sh``) to run the
    other backend on a VM that has both, as with the counts.
    """
    return f"FSTYPE=${{_ltvm_env_FSTYPE:-{fstype}}}"


def configure_fstype(ip: str, fstype: str, os_family: str = "rhel") -> None:
    """Pin FSTYPE in cfg/local.sh.

    In the file rather than exported for llmount.sh alone: auster and a
    bare sanity.sh source cfg/local.sh too, and would otherwise take
    the stock ``FSTYPE=${FSTYPE:-ldiskfs}``.

    Nothing else changes for ZFS.  The framework reads MDSDEV*/OSTDEV*
    as the *vdevs* to build pools on (mdsvdevname/ostvdevname in
    test-framework.sh) and derives the dataset names itself, so the
    /dev/vd* mapping configure_test_disks wrote is what ZFS wants too.
    """
    if fstype not in ("ldiskfs", "zfs"):
        raise ValueError(f"unsupported fstype: {fstype!r}")
    cfg = f"{lustre_libdir(os_family)}/tests/cfg/local.sh"
    script = (
        f"sed -i '/^{FSTYPE_BEGIN}/,/^{FSTYPE_END}/d' {cfg} "
        f"2>/dev/null || true; "
        f"{ensure_env_save(cfg)}; "
        f"printf '\\n{FSTYPE_BEGIN} (generated by ltvm deploy) ---\\n"
        f"{fstype_line(fstype)}\\n"
        f"{FSTYPE_END} ---\\n' >> {cfg}"
    )
    r = run_ssh(ip, script, timeout=step_timeout())
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to set FSTYPE={fstype} in {cfg}: {r.stderr.strip()}"
        )


def _kept_blocks() -> list[tuple[str, str]]:
    """The cfg/local.sh blocks a deploy keeps unless it rewrites them,
    in the order it puts them back."""
    from .vm_cluster import CLUSTER_BLOCK_BEGIN, CLUSTER_BLOCK_END

    return [
        (OST_COUNT_BEGIN, OST_COUNT_END),
        (FSTYPE_BEGIN, FSTYPE_END),
        (CLUSTER_BLOCK_BEGIN, CLUSTER_BLOCK_END),
    ]


def parse_kept_blocks(text: str) -> dict[str, str]:
    """The kept blocks in a cfg/local.sh, by begin marker."""
    lines = text.splitlines()
    blocks: dict[str, str] = {}
    for begin, end in _kept_blocks():
        start = next(
            (i for i, ln in enumerate(lines) if ln.startswith(begin)), None
        )
        if start is None:
            continue
        stop = next(
            (
                i
                for i in range(start + 1, len(lines))
                if lines[i].startswith(end)
            ),
            None,
        )
        if stop is not None:
            blocks[begin] = "\n".join(lines[start : stop + 1]) + "\n"
    # An older ltvm pinned FSTYPE outright, ignoring the environment.
    if FSTYPE_BEGIN in blocks:
        blocks[FSTYPE_BEGIN] = re.sub(
            r"^FSTYPE=(\w+)$",
            lambda m: fstype_line(m.group(1)),
            blocks[FSTYPE_BEGIN],
            flags=re.M,
        )
    return blocks


def read_kept_blocks(ip: str, os_family: str = "rhel") -> dict[str, str]:
    """The kept blocks in the VM's cfg/local.sh, read before a deploy
    replaces the file with the staging's stock one."""
    cfg = f"{lustre_libdir(os_family)}/tests/cfg/local.sh"
    r = run_ssh(ip, f"cat {cfg} 2>/dev/null; true", timeout=step_timeout())
    if r.returncode != 0 or not isinstance(r.stdout, str):
        return {}
    return parse_kept_blocks(r.stdout)


def restore_kept_blocks(
    ip: str,
    blocks: dict[str, str],
    skip: set[str],
    os_family: str = "rhel",
) -> None:
    """Put back the blocks a deploy did not rewrite itself."""
    cfg = f"{lustre_libdir(os_family)}/tests/cfg/local.sh"
    tmp = "/tmp/.ltvm-kept-block"
    steps = []
    for begin, end in _kept_blocks():
        if begin in skip or begin not in blocks:
            continue
        data = base64.b64encode(blocks[begin].encode()).decode()
        steps.append(
            f"echo {data} | base64 -d > {tmp} && "
            + splice_local_sh_block(cfg, tmp, begin, end)
        )
    if not steps:
        return
    r = run_ssh(
        ip,
        " && ".join(steps) + f"; rc=$?; rm -f {tmp}; exit $rc",
        timeout=step_timeout(),
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to keep ltvm's settings in {cfg}: {r.stderr.strip()}"
        )


def targets_unformatted(vm: VMInfo) -> bool:
    """True when the VM's first target disk holds no filesystem.

    Only llmount.sh, or a suite run with REFORMAT=yes, formats the
    targets; a plain sanity.sh on such a VM fails setup with "has not
    been formatted with mkfs.lustre".  False whenever it cannot tell.
    """
    if not (vm.mdt_disks or vm.ost_disks):
        return False
    try:
        r = run_ssh(vm.ip, "blkid -p -s TYPE -o value /dev/vdb", timeout=15)
    except subprocess.TimeoutExpired:
        return False
    # blkid exits 2 when the device carries nothing it can identify.
    return r.returncode == 2


def premount_sweep_script() -> str:
    """Shell that frees a node's disks for llmount.sh to format.

    llmount.sh stops whatever is mounted before it formats, but with
    CLEANUP_DM_DEV=false, which init_test_env forces: the dm-flakey
    maps the targets were mounted through survive and hold the disks,
    and mkfs.lustre fails with "Unable to build fs /dev/vdb".  So the
    maps go here, after every Lustre mount, since a mounted map cannot
    be removed.  Every step tolerates having nothing to do.
    """
    return (
        f"for m in $({_LUSTRE_MOUNTS} | tac); do "
        'umount -f "$m" 2>/dev/null; done; '
        # Export any imported zpool, as unconditionally as the dmsetup
        # sweep and for the same reason: an imported pool holds its vdev
        # open whichever backend is formatted next.  Export, not
        # destroy: formatall reformats with --reformat anyway.
        "if command -v zpool >/dev/null 2>&1; then "
        "for p in $(zpool list -H -o name 2>/dev/null); do "
        'zpool export -f "$p" 2>/dev/null; done; fi; '
        "lustre_rmmod 2>/dev/null; "
        # Must follow lustre_rmmod, which takes osd_zfs off the top of
        # zfs.ko.  llmount.sh reloads it, which is what makes a
        # newly-deployed ZFS of a different version take effect.
        "modprobe -r zfs 2>/dev/null; "
        "dmsetup remove_all 2>/dev/null; true"
    )


def premount_cleanup_script(libdir: str) -> str:
    """Shell that takes a single VM's Lustre down before llmount.sh.

    llmountcleanup.sh first, which stops the targets the way the test
    framework set them up, then the sweep for anything it left.
    """
    return (
        f"cd {libdir}/tests && LUSTRE={libdir} bash llmountcleanup.sh "
        f"2>/dev/null; {premount_sweep_script()}"
    )


def lustre_mount_vm(
    name: str,
    os_family: str,
    *,
    quiet: bool = False,
    timeout: int | None = None,
    zfs: bool = False,
    ost_count: int | None = None,
) -> int:
    """Run llmount.sh inside a VM. Returns exit code.

    ``quiet`` sends llmount's own stdout to stderr instead -- for
    ``deploy-lustre --json``, whose stdout belongs to the JSON
    document.  ``timeout`` defaults to llmount_timeout() for the VM's
    targets.
    """
    try:
        vm = VMInfo.load(name)
    except VMNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_NOT_FOUND
    if timeout is None:
        osts = vm.ost_disks if ost_count is None else ost_count
        timeout = llmount_timeout(vm.mdt_disks + osts, zfs=zfs)
    libdir = lustre_libdir(os_family)
    out = sys.stderr if quiet else sys.stdout
    try:
        print(f"  Cleaning up previous Lustre state on {name}...", file=out)
        run_ssh(vm.ip, premount_cleanup_script(libdir), timeout=timeout)
        print(f"  Running llmount.sh on {name} (up to {timeout}s)...", file=out)
        # Streamed: llmount.sh takes minutes, and captured its progress
        # would only appear once it was over.
        r = run_ssh(
            vm.ip,
            f"cd {libdir}/tests && LUSTRE={libdir} bash llmount.sh",
            timeout=timeout,
            stream=out,
        )
        return r.returncode
    except subprocess.TimeoutExpired as e:
        print(
            f"error: Lustre mount failed: {e}\n"
            f"  {name} may be left partly mounted.  Retry with a longer "
            f"limit: ltvm llmount {name} --timeout <seconds> (or "
            f"deploy-lustre --mount-timeout)",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except Exception as e:
        print(f"error: Lustre mount failed: {e}", file=sys.stderr)
        return EXIT_ERROR
