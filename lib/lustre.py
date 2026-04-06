"""Lustre source tree build support.

Builds a Lustre tree on the host against the ltvm kernel
build-tree (output/<target>/kernel/build-tree/).  This runs
on the host rather than in a container because the host already
has the required Whamcloud-patched e2fsprogs and the exact
toolchain used during development.
"""

import subprocess
import sys
from pathlib import Path


def _run_step(cmd, cwd, label):
    """Run a build step, streaming output.  Raises on failure."""
    print(f"--- {label}...")
    r = subprocess.run(cmd, cwd=str(cwd))
    if r.returncode != 0:
        raise RuntimeError(
            f"{label} failed (rc={r.returncode})")


def _kernel_release(build_tree):
    """Read kernel version from the build-tree stamp file."""
    stamp = Path(build_tree) / "kernel-version"
    if stamp.exists():
        return stamp.read_text().strip()
    # Fallback: ask make
    r = subprocess.run(
        ["make", "-s", "kernelrelease"],
        cwd=str(build_tree),
        capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def _needs_reconfigure(lustre_tree, build_tree, force):
    """Return True if configure needs to be re-run."""
    if force:
        return True

    configure_script = lustre_tree / "configure"
    config_status = lustre_tree / "config.status"

    # No configure script yet -- autogen not run
    if not configure_script.exists():
        return True

    # No config.status -- never configured
    if not config_status.exists():
        return True

    # Kernel changed since last configure
    stamp = lustre_tree / ".ltvm-kernel"
    if stamp.exists():
        prev = stamp.read_text().strip()
        cur = _kernel_release(build_tree)
        if prev != cur:
            print(f"  Kernel changed ({prev} -> {cur}), "
                  f"reconfiguring")
            return True

    return False


def build_lustre(lustre_tree, build_tree, *,
                 enable_server=True,
                 extra_configure=None,
                 jobs=None,
                 force=False):
    """Build a Lustre source tree on the host.

    lustre_tree: Path  -- Lustre source directory
    build_tree:  Path  -- ltvm kernel build-tree
                          (output/<target>/kernel/build-tree/)
    enable_server: bool -- pass --enable-server to configure
    extra_configure: list[str] -- additional configure args
    jobs: int or None  -- parallel jobs (None = nproc)
    force: bool        -- force full clean + reconfigure

    Raises RuntimeError on build failure.
    """
    lustre_tree = Path(lustre_tree).resolve()
    build_tree = Path(build_tree).resolve()

    if not lustre_tree.is_dir():
        raise ValueError(f"Not a directory: {lustre_tree}")
    if not (lustre_tree / "lustre" / "kernel_patches").is_dir():
        raise ValueError(
            f"{lustre_tree} does not look like a Lustre tree")
    if not build_tree.is_dir():
        raise ValueError(
            f"Kernel build-tree not found: {build_tree}\n"
            f"Run 'ltvm build-kernel <target>' first")
    if not (build_tree / "Module.symvers").exists():
        raise ValueError(
            f"Module.symvers missing from {build_tree}\n"
            f"Kernel build may be incomplete")

    kver = _kernel_release(build_tree)
    print(f"  Lustre tree: {lustre_tree}")
    print(f"  Kernel tree: {build_tree}")
    print(f"  Kernel ver:  {kver}")

    need_reconf = _needs_reconfigure(
        lustre_tree, build_tree, force)

    if force:
        # Clean out stale build artifacts
        if (lustre_tree / "Makefile").exists():
            print("--- Cleaning (make distclean)...")
            subprocess.run(
                ["make", "distclean"],
                cwd=str(lustre_tree),
                capture_output=True)

    if need_reconf:
        _run_step(
            ["bash", "autogen.sh"],
            lustre_tree,
            "autogen.sh")

        cfg_cmd = [
            "./configure",
            f"--with-linux={build_tree}",
            "--disable-gss",
            "--disable-crypto",
        ]
        if enable_server:
            cfg_cmd.append("--enable-server")
        else:
            cfg_cmd.append("--disable-server")
        if extra_configure:
            cfg_cmd.extend(extra_configure)

        _run_step(cfg_cmd, lustre_tree, "configure")

    # Record the kernel version so future runs can detect changes
    (lustre_tree / ".ltvm-kernel").write_text(kver + "\n")

    if jobs is None:
        import os
        jobs = os.cpu_count() or 4

    _run_step(
        ["make", f"-j{jobs}"],
        lustre_tree,
        f"make -j{jobs}")

    # Count .ko files as a quick sanity check
    ko_files = list(lustre_tree.rglob("*.ko"))
    ko_files = [f for f in ko_files
                if "kconftest" not in str(f)]
    print(f"--- Build complete: {len(ko_files)} kernel modules")
    return {
        "lustre_tree": str(lustre_tree),
        "kernel_tree": str(build_tree),
        "kernel_version": kver,
        "ko_count": len(ko_files),
    }


def lustre_status(lustre_tree, build_tree):
    """Return a status dict for the Lustre build."""
    lustre_tree = Path(lustre_tree).resolve()
    build_tree = Path(build_tree).resolve()

    stamp = lustre_tree / ".ltvm-kernel"
    config_status = lustre_tree / "config.status"
    ko_count = len([
        f for f in lustre_tree.rglob("*.ko")
        if "kconftest" not in str(f)
    ])

    built_against = (stamp.read_text().strip()
                     if stamp.exists() else None)
    current_kver = (_kernel_release(build_tree)
                    if build_tree.exists() else None)

    stale = (built_against != current_kver
             if built_against and current_kver else True)

    return {
        "configured": config_status.exists(),
        "ko_count": ko_count,
        "built_against": built_against,
        "current_kernel": current_kver,
        "stale": stale,
    }
