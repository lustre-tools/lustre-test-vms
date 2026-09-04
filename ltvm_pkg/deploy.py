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


def deploy_to_vm(
    vm: VMInfo,
    staging: Path,
    *,
    os_family: str = "rhel",
    userspace_only: bool = False,
) -> None:
    """Stream a Lustre staging tree into a VM.

    1. tar | ssh the staging dir into /
    2. depmod + ldconfig
    3. Configure test disk mappings in cfg/local.sh

    Raises RuntimeError on failure.
    """
    if not staging.is_dir():
        raise RuntimeError(f"Staging directory not found: {staging}")

    # Stream staging tree into the VM, unpacking directly into /.
    # --userspace-only: exclude lib/modules/ so kernel modules already in
    # the VM are not overwritten (and depmod is skipped below).
    #
    # COMPROMISE: this is the ONLY shell-string SSH caller in the codebase
    # (all others use vm_net.sshpass_*_argv). The tests patch
    # subprocess.run with a bash -c pipeline expectation, so a full
    # Popen(argv) pipeline rewrite would break them.  At minimum we build
    # the ssh option string from vm_net.SSH_OPTS so
    # UserKnownHostsFile=/dev/null isn't silently dropped here (as it was
    # before), keeping this call site consistent with the rest.
    exclude_modules = "--exclude=./lib/modules" if userspace_only else ""
    ssh_opt_str = " ".join(shlex.quote(o) for o in SSH_OPTS)
    tar_cmd = (
        f"set -o pipefail; "
        f"tar cf - -C {shlex.quote(str(staging))} {exclude_modules} . "
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

    # Configure test framework's local.sh with VM disk topology
    if vm.mdt_disks or vm.ost_disks:
        configure_test_disks(
            vm.ip,
            vm.mdt_disks,
            vm.ost_disks,
            vm.disk_size,
            os_family=os_family,
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
        "printf '%%s %%s\\n' %s \"$(modinfo -F srcversion %s 2>/dev/null)\""
        % (shlex.quote(n), shlex.quote(n))
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
        name for name, srcver in staged.items()
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


def lustre_mount_vm(name: str, os_family: str) -> int:
    """Run llmount.sh inside a VM. Returns exit code."""
    try:
        vm = VMInfo.load(name)
    except VMNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_NOT_FOUND
    libdir = lustre_libdir(os_family)
    try:
        # Clean up any existing Lustre state before formatting.  llmount.sh
        # runs its own stopall internally, but does not call dmsetup remove_all
        # afterward, so mke2fs refuses to reformat backing devices that are
        # still "in use" by leftover dm targets on re-deploy.
        run_ssh(
            vm.ip,
            f"cd {libdir}/tests && LUSTRE={libdir} bash llmountcleanup.sh 2>/dev/null; "
            "lustre_rmmod 2>/dev/null; dmsetup remove_all 2>/dev/null; true",
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
