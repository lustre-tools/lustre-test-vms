"""Validation system for ltvm pipeline.

Spins up a VM from built artifacts, optionally deploys Lustre,
and runs a series of checks to prove the pipeline works from
scratch -- catching hidden dependencies on host config, manually
installed packages, or environment assumptions.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import TargetConfig

log = logging.getLogger(__name__)


# Expected packages in the base image (subset for validation)
EXPECTED_PACKAGES = [
    "fio",
    "attr",
    "pdsh",
    "gdb",
    "perf",
    "iperf3",
    "blktrace",
    "strace",
    "rsync",
    "bc",
    "acl",
    "bpftrace",
]


class CheckResult:
    """Result of a single validation check."""

    def __init__(
        self, name: str, passed: bool, detail: str = "", elapsed: float = 0.0
    ) -> None:
        self.name = name
        self.passed = passed
        self.detail = detail
        self.elapsed = elapsed

    def to_dict(self) -> dict[str, str | bool | float]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "elapsed_s": round(self.elapsed, 2),
        }

    def __repr__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"{self.name}: {status} ({self.detail})"


def _vm_name(target: str) -> str:
    """Generate a unique temporary VM name."""
    return f"validate-{target}-{os.getpid()}"


def _vm_exec(vm_name: str, cmd: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a command in a VM via vm.sh exec.

    Returns (returncode, stdout, stderr).
    """
    result = subprocess.run(
        ["sudo", "vm.py", "exec", "--timeout", str(timeout), vm_name, cmd],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def _vm_ensure(
    vm_name: str,
    image_path: Path,
    kernel_path: Path,
    mdt_disks: int = 0,
    ost_disks: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Create/ensure a validation VM."""
    cmd = [
        "sudo",
        "vm.py",
        "ensure",
        vm_name,
        "--vcpus",
        "2",
        "--mem",
        "4096",
        "--image",
        str(image_path),
        "--kernel",
        str(kernel_path),
    ]
    if mdt_disks:
        cmd += ["--mdt-disks", str(mdt_disks)]
    if ost_disks:
        cmd += ["--ost-disks", str(ost_disks)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to create VM {vm_name}: {result.stderr.strip()}"
        )
    return result


def _vm_destroy(vm_name: str) -> None:
    """Destroy a validation VM (best-effort)."""
    subprocess.run(
        ["sudo", "vm.py", "destroy", vm_name],
        capture_output=True,
        text=True,
    )


def _deploy_lustre(
    vm_name: str, lustre_tree: Path
) -> subprocess.CompletedProcess[str]:
    """Deploy Lustre to a VM."""
    result = subprocess.run(
        [
            "sudo",
            "deploy-lustre.sh",
            "--vm",
            vm_name,
            "--build",
            str(lustre_tree),
            "--mount",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Lustre deploy failed: {result.stderr.strip()}")
    return result


# ------------------------------------------------------------------
# Individual checks
# ------------------------------------------------------------------


def check_artifacts(target_config: TargetConfig) -> CheckResult:
    """Check 1: Verify all build artifacts exist."""
    t0 = time.monotonic()
    out = target_config.output_dir
    kernel_dir = target_config.kernel_output_dir()
    missing = []

    checks = {
        "vmlinux": kernel_dir / "vmlinux",
        "build-tree/.config": kernel_dir / "build-tree" / ".config",
        "build-tree/Module.symvers": kernel_dir
        / "build-tree"
        / "Module.symvers",
        "base.ext4": out / "image" / "base.ext4",
        "container/meta.json": out / "container" / "meta.json",
    }

    for label, path in checks.items():
        if not path.exists():
            missing.append(label)

    elapsed = time.monotonic() - t0
    if missing:
        return CheckResult(
            "Artifacts exist", False, f"missing: {', '.join(missing)}", elapsed
        )
    return CheckResult(
        "Artifacts exist", True, f"{len(checks)}/{len(checks)} found", elapsed
    )


def check_version_consistency(target_config: TargetConfig) -> CheckResult:
    """Check 2: Kernel version matches across artifacts."""
    t0 = time.monotonic()

    # Read version from kernel meta.json
    kernel_dir = target_config.kernel_output_dir()
    meta_path = kernel_dir / "meta.json"
    if not meta_path.exists():
        return CheckResult(
            "Version consistency",
            False,
            "kernel meta.json not found",
            time.monotonic() - t0,
        )

    meta = json.loads(meta_path.read_text())
    meta_version = meta.get("kernel_version", "unknown")

    # Read version from build-tree kernel.release
    kr_path = (
        kernel_dir / "build-tree" / "include" / "config" / "kernel.release"
    )
    if not kr_path.exists():
        return CheckResult(
            "Version consistency",
            False,
            "kernel.release not found in build-tree",
            time.monotonic() - t0,
        )

    kr_version = kr_path.read_text().strip()

    elapsed = time.monotonic() - t0
    if meta_version != kr_version:
        return CheckResult(
            "Version consistency",
            False,
            f"meta.json={meta_version} vs kernel.release={kr_version}",
            elapsed,
        )

    return CheckResult("Version consistency", True, meta_version, elapsed)


def check_vm_boot(target_config: TargetConfig, vm_name: str) -> CheckResult:
    """Check 3a: VM boots and responds."""
    t0 = time.monotonic()

    rc, stdout, stderr = _vm_exec(vm_name, "echo ok", timeout=30)
    elapsed = time.monotonic() - t0

    if rc != 0 or "ok" not in stdout:
        detail = stderr.strip() if stderr.strip() else "no response"
        return CheckResult("Virgin VM boot", False, detail, elapsed)

    return CheckResult(
        "Virgin VM boot", True, f"booted in {elapsed:.1f}s", elapsed
    )


def check_vm_kernel_version(
    target_config: TargetConfig, vm_name: str
) -> CheckResult:
    """Check 3b: uname -r matches expected kernel version."""
    t0 = time.monotonic()

    meta_path = target_config.output_dir / "kernel" / "meta.json"
    if not meta_path.exists():
        return CheckResult(
            "Kernel version match",
            False,
            "kernel meta.json not found",
            time.monotonic() - t0,
        )

    meta = json.loads(meta_path.read_text())
    expected = meta.get("kernel_version", "unknown")

    rc, stdout, stderr = _vm_exec(vm_name, "uname -r")
    elapsed = time.monotonic() - t0

    if rc != 0:
        return CheckResult(
            "Kernel version match",
            False,
            f"uname -r failed: {stderr.strip()}",
            elapsed,
        )

    actual = stdout.strip()
    if actual != expected:
        return CheckResult(
            "Kernel version match",
            False,
            f"expected={expected}, got={actual}",
            elapsed,
        )

    return CheckResult("Kernel version match", True, actual, elapsed)


def check_networking(vm_name: str) -> CheckResult:
    """Check 3c: VM can reach the host bridge."""
    t0 = time.monotonic()

    rc, stdout, stderr = _vm_exec(
        vm_name, "ping -c 1 -W 2 192.168.100.1", timeout=10
    )
    elapsed = time.monotonic() - t0

    if rc != 0:
        return CheckResult(
            "Networking", False, stderr.strip() or "ping failed", elapsed
        )

    return CheckResult("Networking", True, "", elapsed)


def check_packages(vm_name: str) -> CheckResult:
    """Check 3d: Expected packages are installed."""
    t0 = time.monotonic()

    pkg_list = " ".join(EXPECTED_PACKAGES)
    rc, stdout, stderr = _vm_exec(vm_name, f"rpm -q {pkg_list}", timeout=15)
    elapsed = time.monotonic() - t0

    # Parse output to find missing
    missing = []
    for line in stdout.strip().splitlines():
        if "is not installed" in line:
            # Extract package name from "package X is not installed"
            parts = line.split()
            if len(parts) >= 2:
                missing.append(parts[1])

    total = len(EXPECTED_PACKAGES)
    found = total - len(missing)

    if missing:
        return CheckResult(
            "Package check",
            False,
            f"{found}/{total} found, missing: {', '.join(missing)}",
            elapsed,
        )

    return CheckResult(
        "Package check", True, f"{found}/{total} packages found", elapsed
    )


def check_no_lustre(vm_name: str) -> CheckResult:
    """Check 3e: No Lustre modules loaded on virgin boot."""
    t0 = time.monotonic()

    rc, stdout, stderr = _vm_exec(vm_name, "lsmod | grep -i lustre", timeout=10)
    elapsed = time.monotonic() - t0

    # grep returns 1 when no match -- that's what we want
    if rc == 0 and stdout.strip():
        return CheckResult(
            "No Lustre loaded", False, f"found: {stdout.strip()}", elapsed
        )

    return CheckResult("No Lustre loaded", True, "", elapsed)


def check_lustre_deploy(vm_name: str, lustre_tree: Path) -> CheckResult:
    """Check 4a: Deploy Lustre and mount filesystem."""
    t0 = time.monotonic()

    try:
        _deploy_lustre(vm_name, lustre_tree)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        return CheckResult(
            "Lustre deploy + mount", False, str(e), time.monotonic() - t0
        )

    # Verify /mnt/lustre is mounted
    rc, stdout, stderr = _vm_exec(
        vm_name, "mountpoint -q /mnt/lustre", timeout=10
    )
    elapsed = time.monotonic() - t0

    if rc != 0:
        return CheckResult(
            "Lustre deploy + mount",
            False,
            "/mnt/lustre not mounted after deploy",
            elapsed,
        )

    return CheckResult("Lustre deploy + mount", True, "", elapsed)


def check_basic_io(vm_name: str) -> CheckResult:
    """Check 4b: Write and read back a file on Lustre."""
    t0 = time.monotonic()

    test_data = "ltvm-validation-test-data-12345"
    write_cmd = (
        f"echo '{test_data}' > "
        f"/mnt/lustre/validate_test && "
        f"cat /mnt/lustre/validate_test"
    )

    rc, stdout, stderr = _vm_exec(vm_name, write_cmd, timeout=30)
    elapsed = time.monotonic() - t0

    if rc != 0:
        return CheckResult(
            "Basic I/O", False, f"write/read failed: {stderr.strip()}", elapsed
        )

    if test_data not in stdout:
        return CheckResult(
            "Basic I/O",
            False,
            f"readback mismatch: got '{stdout.strip()}'",
            elapsed,
        )

    # Cleanup
    _vm_exec(vm_name, "rm -f /mnt/lustre/validate_test", timeout=10)

    return CheckResult("Basic I/O", True, "", elapsed)


# ------------------------------------------------------------------
# Main validation entry point
# ------------------------------------------------------------------


def validate_target(
    target_config: TargetConfig,
    lustre_tree: str | Path | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Run the full validation suite for a target.

    Args:
        target_config: TargetConfig instance
        lustre_tree: Path to Lustre source/build tree,
            or None to skip Lustre deploy checks
        verbose: print extra detail

    Returns:
        dict with keys: target, passed, total, checks
    """
    results = []
    target = target_config.name
    out = target_config.output_dir
    image_path = out / "image" / "base.ext4"
    kernel_path = out / "kernel" / "vmlinux"

    # -- Check 1: Artifacts --
    r = check_artifacts(target_config)
    results.append(r)
    if not r.passed:
        return _summary(target, results)

    # -- Check 2: Version consistency --
    r = check_version_consistency(target_config)
    results.append(r)
    if not r.passed:
        return _summary(target, results)

    # -- Checks 3: Virgin VM boot --
    vm_name = _vm_name(target)
    try:
        _vm_ensure(vm_name, image_path, kernel_path)

        r = check_vm_boot(target_config, vm_name)
        results.append(r)
        if not r.passed:
            return _summary(target, results)

        r = check_vm_kernel_version(target_config, vm_name)
        results.append(r)
        if not r.passed:
            return _summary(target, results)

        r = check_networking(vm_name)
        results.append(r)
        if not r.passed:
            return _summary(target, results)

        r = check_packages(vm_name)
        results.append(r)
        if not r.passed:
            return _summary(target, results)

        r = check_no_lustre(vm_name)
        results.append(r)
        if not r.passed:
            return _summary(target, results)
    finally:
        _vm_destroy(vm_name)

    # -- Checks 4: Lustre deploy (if tree provided) --
    if lustre_tree is not None:
        lustre_tree = Path(lustre_tree).resolve()
        vm_name_lustre = _vm_name(target) + "-lustre"
        try:
            _vm_ensure(
                vm_name_lustre,
                image_path,
                kernel_path,
                mdt_disks=1,
                ost_disks=3,
            )

            r = check_lustre_deploy(vm_name_lustre, lustre_tree)
            results.append(r)
            if not r.passed:
                return _summary(target, results)

            r = check_basic_io(vm_name_lustre)
            results.append(r)
        finally:
            _vm_destroy(vm_name_lustre)

    return _summary(target, results)


def _summary(target: str, results: list[CheckResult]) -> dict[str, Any]:
    """Build summary dict from a list of CheckResults."""
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    return {
        "target": target,
        "passed": passed,
        "total": total,
        "all_passed": passed == total,
        "checks": [r.to_dict() for r in results],
    }


def print_results(summary: dict[str, Any], verbose: bool = False) -> None:
    """Print validation results in human-readable format."""
    target = summary["target"]
    print(f"Validating {target}...")

    for check in summary["checks"]:
        status = "PASS" if check["passed"] else "FAIL"
        detail = ""
        if check["detail"]:
            detail = f"  ({check['detail']})"
        print(f"  {check['name']:<24} {status}{detail}")

    passed = summary["passed"]
    total = summary["total"]
    overall = "PASSED" if summary["all_passed"] else "FAILED"
    print(f"\nValidation: {overall} ({passed}/{total} checks)")
