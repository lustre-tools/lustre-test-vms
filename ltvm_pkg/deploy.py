"""Deploy Lustre staging tree to a running VM.

Shared by single-node deploy (cli.py cmd_deploy) and
multi-node cluster deploy (vm_cluster.py).
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from .paths import read_modinfo_field
from .vm_net import SSH_OPTS, run_ssh
from .vm_state import (
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    ROOT_PASSWORD,
    VMInfo,
    VMNotFound,
    lustre_libdir,
)


def _stream_tree(
    vm: VMInfo, tree: Path, *, exclude_modules: bool = False
) -> None:
    """tar | ssh a DESTDIR tree into the VM's /.

    ``exclude_modules`` drops lib/modules/ so kernel modules already in
    the VM are not overwritten (the --userspace-only path; depmod is
    skipped for it too).

    COMPROMISE: this is the ONLY shell-string SSH caller in the codebase
    (all others use vm_net.sshpass_*_argv). The tests patch
    subprocess.run with a bash -c pipeline expectation, so a full
    Popen(argv) pipeline rewrite would break them.  At minimum we build
    the ssh option string from vm_net.SSH_OPTS so
    UserKnownHostsFile=/dev/null isn't silently dropped here (as it was
    before), keeping this call site consistent with the rest.
    """
    exclude_mod = "--exclude=./lib/modules" if exclude_modules else ""
    # Never ship ltvm's own build bookkeeping into the VM's /.
    exclude_bookkeeping = "--exclude=./.ltvm-*"
    ssh_opt_str = " ".join(shlex.quote(o) for o in SSH_OPTS)
    tar_cmd = (
        f"set -o pipefail; "
        f"tar cf - -C {shlex.quote(str(tree))} "
        f"{exclude_mod} {exclude_bookkeeping} . "
        f"| sshpass -p {shlex.quote(ROOT_PASSWORD)} ssh {ssh_opt_str} "
        f"root@{shlex.quote(vm.ip)} "
        f"'tar xf - -C / --keep-directory-symlink --no-same-owner'"
    )
    try:
        r = subprocess.run(
            ["bash", "-c", tar_cmd], capture_output=True, text=True, timeout=120
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"tar deploy to {vm.ip} timed out after {e.timeout}s"
        )
    if r.returncode != 0:
        output = (r.stdout or "") + (r.stderr or "")
        raise RuntimeError(f"tar deploy failed: {output.strip()}")


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
) -> None:
    """Stream a Lustre staging tree into a VM.

    1. tar | ssh the ZFS staging dir into / (``zfs_staging``), then the
       Lustre one -- ZFS first so one depmod covers both
    2. depmod + ldconfig
    3. Configure test disk mappings in cfg/local.sh
    4. Optionally repoint the OSTs at brd ram devices (``ram_osts``),
       for benchmarks that want the backing store out of the picture.
    5. Pin FSTYPE in cfg/local.sh (``fstype``)

    Raises RuntimeError on failure.
    """
    if not staging.is_dir():
        raise RuntimeError(f"Staging directory not found: {staging}")

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
    r = run_ssh(vm.ip, post_deploy_cmd, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(
            f"post-deploy ({post_deploy_cmd}) failed "
            f"(rc={r.returncode}): {r.stderr}"
        )

    if not userspace_only:
        verify_deployed_modules(vm, staging)

    if zfs_staging is not None and not userspace_only:
        retire_stale_zfs(vm)

    # Configure test framework's local.sh with VM disk topology
    if vm.mdt_disks or vm.ost_disks:
        configure_test_disks(
            vm.ip,
            vm.mdt_disks,
            vm.ost_disks,
            vm.disk_size,
            os_family=os_family,
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
    r = run_ssh(vm.ip, script, timeout=120)
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
    r = run_ssh(vm.ip, script, timeout=60)
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
        lines.append(f"MDSCOUNT={mdt_disks}")
        for n in range(1, mdt_disks + 1):
            letter = chr(ord("a") + n)
            lines.append(f"MDSDEV{n}=/dev/vd{letter}")

    if ost_disks:
        lines.append(f"OSTCOUNT={ost_disks}")
        for n in range(1, ost_disks + 1):
            letter = chr(ord("a") + mdt_disks + n)
            lines.append(f"OSTDEV{n}=/dev/vd{letter}")

    snippet = "\\n".join(lines)
    script = (
        f"sed -i '/^# --- VM disk configuration/,/^# --- END VM disk/d' "
        f"{testdir}/cfg/local.sh 2>/dev/null || true; "
        f"printf '\\n# --- VM disk configuration (generated by ltvm deploy) ---\\n"
        f"{snippet}\\n"
        f"# --- END VM disk configuration ---\\n' >> {testdir}/cfg/local.sh"
    )
    r = run_ssh(ip, script, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to configure test disks in {testdir}/cfg/local.sh: "
            f"{r.stderr.strip()}"
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
    ndev = count + 1 if ram_mdt else count
    rd_size_kb = size_gb * 1024 * 1024
    size_kb = size_gb * 1024 * 1024

    lines = [f"OSTCOUNT={count}", f"OSTSIZE={size_kb}"]
    for n in range(1, count + 1):
        lines.append(f"OSTDEV{n}=/dev/ram{n - 1}")
    if ram_mdt:
        lines += [
            "MDSCOUNT=1",
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
        f"sed -i '/^# --- RAM OST configuration/,/^# --- END RAM OST/d' "
        f"{testdir}/cfg/local.sh 2>/dev/null || true; "
        f"printf '\\n# --- RAM OST configuration (generated by ltvm deploy) ---\\n"
        f"{snippet}\\n"
        f"# --- END RAM OST configuration ---\\n' >> {testdir}/cfg/local.sh"
    )
    r = run_ssh(ip, script, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to configure ram OSTs: {r.stderr.strip() or r.stdout.strip()}"
        )


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
    testdir = f"{lustre_libdir(os_family)}/tests"
    script = (
        f"sed -i '/^# --- FSTYPE (generated by ltvm/,/^# --- END FSTYPE/d' "
        f"{testdir}/cfg/local.sh 2>/dev/null || true; "
        f"printf '\\n# --- FSTYPE (generated by ltvm deploy) ---\\n"
        f"FSTYPE={fstype}\\n"
        f"# --- END FSTYPE ---\\n' >> {testdir}/cfg/local.sh"
    )
    r = run_ssh(ip, script, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(
            f"Failed to set FSTYPE={fstype} in {testdir}/cfg/local.sh: "
            f"{r.stderr.strip()}"
        )


def lustre_mount_vm(name: str, os_family: str) -> int:
    """Run llmount.sh inside a VM. Returns exit code."""
    try:
        vm = VMInfo.load(name)
    except VMNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_NOT_FOUND
    libdir = lustre_libdir(os_family)
    # Export any imported zpool, as unconditionally as the dmsetup
    # sweep below and for the same reason: an imported pool holds its
    # vdev open, so the next format fails with "apparently in use by
    # the system" whichever backend is being formatted.  Doing this
    # only for ZFS would skip the case that needs it most -- switching
    # a VM back to ldiskfs, when the pools are still imported.
    #
    # Export, not destroy: formatall reformats with --reformat anyway.
    zfs_cleanup = (
        "if command -v zpool >/dev/null 2>&1; then "
        "for p in $(zpool list -H -o name 2>/dev/null); do "
        'zpool export -f "$p" 2>/dev/null; done; fi; '
    )
    try:
        # Clean up any existing Lustre state before formatting.  llmount.sh
        # runs its own stopall internally, but does not call dmsetup remove_all
        # afterward, so mke2fs refuses to reformat backing devices that are
        # still "in use" by leftover dm targets on re-deploy.
        run_ssh(
            vm.ip,
            f"cd {libdir}/tests && LUSTRE={libdir} bash llmountcleanup.sh 2>/dev/null; "
            f"{zfs_cleanup}"
            "lustre_rmmod 2>/dev/null; "
            # Must follow lustre_rmmod, which takes osd_zfs off the top
            # of zfs.ko.  llmount.sh reloads it, which is what makes a
            # newly-deployed ZFS of a different version take effect.
            "modprobe -r zfs 2>/dev/null; "
            "dmsetup remove_all 2>/dev/null; true",
            timeout=60,
        )
        r = run_ssh(
            vm.ip,
            f"cd {libdir}/tests && LUSTRE={libdir} bash llmount.sh",
            timeout=180,
        )
        if r.stdout:
            print(r.stdout, end="")
        if r.stderr:
            print(r.stderr, end="", file=sys.stderr)
        return r.returncode
    except Exception as e:
        print(f"error: Lustre mount failed: {e}", file=sys.stderr)
        return EXIT_ERROR
