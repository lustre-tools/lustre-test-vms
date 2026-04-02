"""Runtime wrappers around vm.sh and deploy-lustre.sh.

Each function shells out to the battle-tested existing tools and
returns a consistent dict: {'ok': bool, 'output': str, 'returncode': int}.
"""

import subprocess
from pathlib import Path

VM_SH = "vm.sh"
DEPLOY_SH = "deploy-lustre.sh"


def _run(cmd, timeout=None):
    """Run a command list under sudo, capture output, return result dict."""
    full = ["sudo"] + cmd
    try:
        r = subprocess.run(
            full,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
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

def vm_create(name, target=None, vcpus=2, mem=4096,
              mdt_disks=0, ost_disks=0):
    """Create a VM.  --target is accepted but currently ignored
    (vm.sh uses its own default kernel)."""
    cmd = [VM_SH, "create", "--name", name,
           "--vcpus", str(vcpus), "--mem", str(mem)]
    if mdt_disks:
        cmd += ["--mdt-disks", str(mdt_disks)]
    if ost_disks:
        cmd += ["--ost-disks", str(ost_disks)]
    return _run(cmd)


def vm_ensure(name, target=None, vcpus=2, mem=4096,
              mdt_disks=0, ost_disks=0):
    """Idempotent create-if-missing, start-if-stopped."""
    cmd = [VM_SH, "ensure", name,
           "--vcpus", str(vcpus), "--mem", str(mem)]
    if mdt_disks:
        cmd += ["--mdt-disks", str(mdt_disks)]
    if ost_disks:
        cmd += ["--ost-disks", str(ost_disks)]
    return _run(cmd)


def vm_destroy(name):
    return _run([VM_SH, "destroy", name])


def vm_start(name):
    return _run([VM_SH, "start", name])


def vm_stop(name):
    return _run([VM_SH, "stop", name])


def vm_restart(name):
    return _run([VM_SH, "restart", name])


def vm_list(json_output=False):
    cmd = [VM_SH, "list"]
    if json_output:
        cmd.append("--json")
    return _run(cmd)


def vm_status(name, json_output=False):
    cmd = [VM_SH, "status"]
    if json_output:
        cmd.append("--json")
    cmd.append(name)
    return _run(cmd)


def vm_exec(name, cmd, timeout=120):
    """Execute a command inside a VM.

    Returns the usual result dict.  The returncode reflects
    vm.sh exit conventions: 0=ok, 1=error, 2=not-found,
    3=timeout, 4=unreachable.
    """
    return _run(
        [VM_SH, "exec", "--timeout", str(timeout), name, cmd],
        timeout=timeout + 30,  # outer safety margin
    )


def vm_log(name, lines=50):
    return _run([VM_SH, "log", name, str(lines)])


def vm_dmesg(name, tail=100):
    return _run([VM_SH, "dmesg", "--tail", str(tail), name])


# ------------------------------------------------------------------
# Deploy
# ------------------------------------------------------------------

def deploy(vm_name, build_path=".", mount=False):
    build_path = str(Path(build_path).resolve())
    cmd = [DEPLOY_SH, "--vm", vm_name, "--build", build_path]
    if mount:
        cmd.append("--mount")
    return _run(cmd, timeout=300)


# ------------------------------------------------------------------
# Cluster
# ------------------------------------------------------------------

def cluster_create(name, *node_specs):
    """node_specs: strings like 'mgs+mds:c1-srv:1'."""
    cmd = [VM_SH, "cluster", "create", name] + list(node_specs)
    return _run(cmd)


def cluster_destroy(name):
    return _run([VM_SH, "cluster", "destroy", name])


def cluster_deploy(name, build_path, mount=False):
    build_path = str(Path(build_path).resolve())
    cmd = [VM_SH, "cluster", "deploy", name,
           "--build", build_path]
    if mount:
        cmd.append("--mount")
    return _run(cmd, timeout=300)


def cluster_status(name):
    return _run([VM_SH, "cluster", "status", name])


def cluster_exec(name, role, cmd):
    return _run([VM_SH, "cluster", "exec", name, role, cmd])
