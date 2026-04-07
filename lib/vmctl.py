"""Subprocess client for qemu/vm.py and deploy-lustre.sh.

VM operations (TAP devices, QEMU, disk I/O) require root.
This module provides a normal-user Python API that delegates
to vm.py and deploy-lustre.sh via sudo subprocess calls.

Every function returns RunResult: {'ok': bool, 'output': str,
'returncode': int}.

The implementation lives in qemu/ (commands.py, cluster.py,
net.py, etc.).  This module is the boundary between the ltvm
build pipeline (runs as user) and the VM engine (runs as root).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TypedDict


class RunResult(TypedDict):
    ok: bool
    output: str
    returncode: int


VM_SH = "vm.py"
DEPLOY_SH = shutil.which("deploy-lustre.sh") or "/usr/local/bin/deploy-lustre.sh"


def _run(cmd: list[str], timeout: int | None = None) -> RunResult:
    """Run a command list under sudo, capture output, return result dict."""
    full = ["sudo"] + cmd
    return _run_impl(full, timeout)


def _run_impl(full: list[str], timeout: int | None = None) -> RunResult:
    try:
        r = subprocess.run(
            full,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "output": f"Command timed out after {timeout}s",
            "returncode": 3,
        }

    combined = r.stdout
    if r.stderr:
        combined = combined + r.stderr if combined else r.stderr

    return {
        "ok": r.returncode == 0,
        "output": combined.rstrip("\n") if combined else "",
        "returncode": r.returncode,
    }


# ------------------------------------------------------------------
# VM management
# ------------------------------------------------------------------


def vm_create(
    name: str,
    target: str | None = None,
    vcpus: int = 2,
    mem: int = 4096,
    mdt_disks: int = 0,
    ost_disks: int = 0,
) -> RunResult:
    """Create a VM.  --target is accepted but currently ignored
    (vm.sh uses its own default kernel)."""
    cmd = [
        VM_SH,
        "create",
        "--name",
        name,
        "--vcpus",
        str(vcpus),
        "--mem",
        str(mem),
    ]
    if mdt_disks:
        cmd += ["--mdt-disks", str(mdt_disks)]
    if ost_disks:
        cmd += ["--ost-disks", str(ost_disks)]
    return _run(cmd)


def vm_ensure(
    name: str,
    target: str | None = None,
    vcpus: int = 2,
    mem: int = 4096,
    mdt_disks: int = 0,
    ost_disks: int = 0,
    image: str | Path | None = None,
    kernel: str | Path | None = None,
) -> RunResult:
    """Idempotent create-if-missing, start-if-stopped."""
    cmd = [VM_SH, "ensure", name, "--vcpus", str(vcpus), "--mem", str(mem)]
    if mdt_disks:
        cmd += ["--mdt-disks", str(mdt_disks)]
    if ost_disks:
        cmd += ["--ost-disks", str(ost_disks)]
    if image:
        cmd += ["--image", str(image)]
    if kernel:
        cmd += ["--kernel", str(kernel)]
    return _run(cmd)


def vm_destroy(name: str) -> RunResult:
    return _run([VM_SH, "destroy", name])


def vm_start(name: str) -> RunResult:
    return _run([VM_SH, "start", name])


def vm_stop(name: str) -> RunResult:
    return _run([VM_SH, "stop", name])


def vm_restart(name: str) -> RunResult:
    return _run([VM_SH, "restart", name])


def vm_list(json_output: bool = False) -> RunResult:
    cmd = [VM_SH, "list"]
    if json_output:
        cmd.append("--json")
    return _run(cmd)


def vm_status(name: str, json_output: bool = False) -> RunResult:
    cmd = [VM_SH, "status"]
    if json_output:
        cmd.append("--json")
    cmd.append(name)
    return _run(cmd)


def vm_exec(name: str, cmd: str, timeout: int = 120) -> RunResult:
    """Execute a command inside a VM.

    Returns the usual result dict.  The returncode reflects
    vm.sh exit conventions: 0=ok, 1=error, 2=not-found,
    3=timeout, 4=unreachable.
    """
    return _run(
        [VM_SH, "exec", "--timeout", str(timeout), name, cmd],
        timeout=timeout + 30,  # outer safety margin
    )


def vm_log(name: str, lines: int = 50) -> RunResult:
    return _run([VM_SH, "log", name, str(lines)])


def vm_dmesg(name: str, tail: int = 100) -> RunResult:
    return _run([VM_SH, "dmesg", "--tail", str(tail), name])


# ------------------------------------------------------------------
# Deploy
# ------------------------------------------------------------------


def deploy(
    vm_name: str,
    build_path: str | Path = ".",
    mount: bool = False,
    kernel_modules: str | Path | None = None,
) -> RunResult:
    """Deploy Lustre to a VM.

    If kernel_modules is set (path to modules/ from kernel
    build output containing lib/modules/<ver>/), rsync them
    into the VM first so kernel deps like sunrpc are
    available when Lustre modules load.
    """
    build_path = str(Path(build_path).resolve())

    # Deploy kernel modules if provided
    if kernel_modules:
        mods = Path(kernel_modules)
        lib_mods = mods / "lib" / "modules"
        if lib_mods.is_dir():
            res = _deploy_kernel_modules(vm_name, lib_mods)
            if not res["ok"]:
                return res

    cmd = [DEPLOY_SH, "--vm", vm_name, "--build", build_path]
    if mount:
        cmd.append("--mount")
    return _run(cmd, timeout=300)


def lustre_mount(vm_name: str) -> RunResult:
    """Start Lustre on a VM that has already been deployed.

    Runs llmount.sh from the standard test framework location
    (/usr/lib64/lustre/tests/) inside the VM.  deploy-lustre.sh
    always rsyncs the test framework there (not to the host build
    path), so this is independent of where the build tree lives on
    the host.

    The VM must have been deployed with deploy() first.
    """
    return vm_exec(
        vm_name,
        "cd /usr/lib64/lustre/tests"
        " && LUSTRE=/usr/lib64/lustre bash llmount.sh",
        timeout=180,
    )


def _deploy_kernel_modules(vm_name: str, lib_modules_path: Path) -> RunResult:
    """Copy kernel modules into the VM and run depmod.

    lib_modules_path: path to lib/modules/ containing
    <version>/ subdirectories.
    """
    versions = [d for d in lib_modules_path.iterdir() if d.is_dir()]
    if not versions:
        return {
            "ok": False,
            "output": f"No version dirs in {lib_modules_path}",
            "returncode": 1,
        }

    for ver_dir in versions:
        res = _run(
            [VM_SH, "cp-to", vm_name, str(ver_dir), "/lib/modules/"],
            timeout=120,
        )
        if not res["ok"]:
            return res

    # Run depmod for the VM's running kernel
    res = _run([VM_SH, "exec", "--timeout", "10", vm_name, "depmod -a"])
    if not res["ok"]:
        return res

    # Install vmlinuz for kdump.
    # lib_modules_path = .../modules/lib/modules  ->  vmlinuz is at .../vmlinuz
    kernel_dir = lib_modules_path.parent.parent.parent
    vmlinuz = kernel_dir / "vmlinuz"
    if vmlinuz.exists():
        kver = versions[0].name
        _run(
            [
                VM_SH,
                "cp-to",
                vm_name,
                str(vmlinuz),
                f"/boot/vmlinuz-{kver}",
            ],
            timeout=30,
        )
        _run(
            [
                VM_SH,
                "exec",
                "--timeout",
                "15",
                vm_name,
                "kdumpctl rebuild 2>/dev/null; systemctl restart kdump 2>/dev/null",
            ],
            timeout=20,
        )

    return res


# ------------------------------------------------------------------
# Cluster
# ------------------------------------------------------------------


def cluster_create(name: str, *node_specs: str) -> RunResult:
    """node_specs: strings like 'mgs+mds:c1-srv:1'."""
    cmd = [VM_SH, "cluster", "create", name] + list(node_specs)
    return _run(cmd)


def cluster_destroy(name: str) -> RunResult:
    return _run([VM_SH, "cluster", "destroy", name])


def cluster_deploy(
    name: str, build_path: str | Path, mount: bool = False
) -> RunResult:
    build_path = str(Path(build_path).resolve())
    cmd = [VM_SH, "cluster", "deploy", name, "--lustre-source", build_path]
    if mount:
        cmd.append("--mount")
    return _run(cmd, timeout=300)


def cluster_status(name: str) -> RunResult:
    return _run([VM_SH, "cluster", "status", name])


def cluster_exec(name: str, role: str, cmd: str) -> RunResult:
    return _run([VM_SH, "cluster", "exec", name, role, cmd])
