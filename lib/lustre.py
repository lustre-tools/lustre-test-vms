"""Lustre source tree build support.

Builds a Lustre tree inside the target's build container
against the ltvm kernel build-tree.  The container provides
the correct toolchain for the target OS (e.g., Rocky 9 GCC
for Rocky 9 kernel modules), enabling cross-OS builds.

The Lustre source tree and kernel build-tree are bind-mounted
into the container.  Build artifacts stay in the host's Lustre
tree, so incremental builds are fast -- make sees the same .o
files from last time.

The container image (e.g., ltvm-build-rocky9) is retained by
podman after `ltvm build-container` or `ltvm build-all`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TypedDict


class BuildResult(TypedDict):
    lustre_tree: str
    kernel_tree: str
    kernel_version: str
    ko_count: int
    container: str | None


class StatusResult(TypedDict):
    configured: bool
    ko_count: int
    built_against: str | None
    current_kernel: str | None
    stale: bool


def _kernel_release(build_tree: str | Path) -> str:
    """Read the kernel version from the build-tree.

    Reads include/config/kernel.release, which is written by the
    kernel build (kernel.py).  Returns "unknown" if not present.
    """
    release_file = Path(build_tree) / "include" / "config" / "kernel.release"
    if release_file.exists():
        return release_file.read_text().strip()
    return "unknown"


def _container_exists(tag: str) -> bool:
    """Check if a podman image exists."""
    r = subprocess.run(["podman", "image", "exists", tag], capture_output=True)
    return r.returncode == 0


def _needs_reconfigure(
    lustre_tree: Path,
    build_tree: Path,
    force: bool,
    container_path: Path,
) -> bool:
    """Return True if configure needs to be re-run.

    container_path is the path to the kernel build-tree as
    seen inside the container (may differ from host path).
    """
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

    # Check if previous configure used a different
    # --with-linux path (host vs container path change)
    stamp = lustre_tree / ".ltvm-kernel"
    stamp_path = lustre_tree / ".ltvm-kernel-path"
    if stamp.exists():
        prev = stamp.read_text().strip()
        cur = _kernel_release(build_tree)
        if prev != cur:
            print(f"  Kernel changed ({prev} -> {cur}), reconfiguring")
            return True
    if stamp_path.exists():
        prev_path = stamp_path.read_text().strip()
        if prev_path != str(container_path):
            print("  Kernel path changed, reconfiguring")
            return True

    return False


def build_lustre(
    lustre_tree: str | Path,
    build_tree: str | Path,
    *,
    container_tag: str | None = None,
    enable_server: bool = True,
    extra_configure: list[str] | None = None,
    jobs: int | None = None,
    force: bool = False,
) -> BuildResult:
    """Build a Lustre source tree.

    Runs inside the build container when available (cross-OS
    capable).  Falls back to host build if no container.

    lustre_tree:    Path -- Lustre source directory
    build_tree:     Path -- ltvm kernel build-tree
    container_tag:  str  -- podman image tag (e.g.,
                            'ltvm-build-rocky9')
    enable_server:  bool -- pass --enable-server to configure
    extra_configure: list[str] -- additional configure args
    jobs:           int or None -- parallel jobs (None = nproc)
    force:          bool -- force full clean + reconfigure

    Raises RuntimeError on build failure.
    """
    lustre_tree = Path(lustre_tree).resolve()
    build_tree = Path(build_tree).resolve()

    if not lustre_tree.is_dir():
        raise ValueError(f"Not a directory: {lustre_tree}")
    if not (lustre_tree / "lustre" / "kernel_patches").is_dir():
        raise ValueError(f"{lustre_tree} does not look like a Lustre tree")
    if not build_tree.is_dir():
        raise ValueError(
            f"Kernel build-tree not found: {build_tree}\n"
            f"Run 'ltvm build-kernel <target>' first"
        )
    if not (build_tree / "Module.symvers").exists():
        raise ValueError(
            f"Module.symvers missing from {build_tree}\n"
            f"Kernel build may be incomplete"
        )

    if jobs is None:
        jobs = os.cpu_count() or 4

    kver = _kernel_release(build_tree)

    # Decide: container or host build
    use_container = container_tag and _container_exists(container_tag)

    if use_container:
        assert container_tag is not None  # narrowing for mypy
        return _build_in_container(
            lustre_tree,
            build_tree,
            container_tag,
            kver,
            enable_server,
            extra_configure,
            jobs,
            force,
        )
    else:
        if container_tag:
            print(
                f"  WARNING: container {container_tag} "
                f"not found, building on host"
            )
            print("  Run 'ltvm build-container <target>' for cross-OS builds")
        return _build_on_host(
            lustre_tree,
            build_tree,
            kver,
            enable_server,
            extra_configure,
            jobs,
            force,
        )


def _build_in_container(
    lustre_tree: Path,
    build_tree: Path,
    container_tag: str,
    kver: str,
    enable_server: bool,
    extra_configure: list[str] | None,
    jobs: int,
    force: bool,
) -> BuildResult:
    """Build Lustre inside the build container.

    Mount layout:
      /lustre  -- Lustre source (read-write, build here)
      /kernel  -- kernel build-tree (read-only)
    """
    print(f"  Container: {container_tag}")
    print(f"  Lustre:    {lustre_tree}")
    print(f"  Kernel:    {build_tree}")
    print(f"  Kernel:    {kver}")

    container_kernel = Path("/kernel")
    need_reconf = _needs_reconfigure(
        lustre_tree, build_tree, force, container_kernel
    )

    # Build the shell script to run inside the container
    script_parts = ["set -e", "cd /lustre"]

    if force:
        script_parts.append(
            "if [ -f Makefile ]; then make distclean 2>/dev/null || true; fi"
        )

    if need_reconf or force:
        # Remove stale .ko files from any previous build before
        # reconfiguring.  distclean only cleans dirs the current
        # Makefile knows about, so server .ko files survive a
        # client-only reconfigure (and vice versa).
        script_parts.append("find . -name '*.ko' -delete 2>/dev/null || true")
        # Remove configure residue that poisons re-runs: conftest dirs/files
        # and the parallel kconftest/lpb directories.
        script_parts.append(
            "rm -rf conftest conftest.c conftest.dir _lpb"
            " kconftest.dir conftest.err 2>/dev/null || true"
        )

    # Always run autogen.sh + configure inside the container.
    # autogen.sh regenerates aclocal.m4 / libtool stubs with the
    # container's toolchain (e.g. libtool 2.4.7 on Ubuntu 24.04).
    # Running autogen.sh on the host (libtool 2.4.6) produces stubs
    # that are incompatible with the container's libtool 2.4.7,
    # causing a version mismatch error during the userspace build.
    # Running configure explicitly (not relying on make's implicit
    # remade-Makefile rules) prevents make from re-triggering
    # configure with a different autoconf version mid-build.
    script_parts.append("bash autogen.sh")
    cfg = "./configure --with-linux=/kernel --disable-gss --disable-crypto"
    if enable_server:
        cfg += " --enable-server"
    else:
        cfg += " --disable-server"
    if extra_configure:
        cfg += " " + " ".join(extra_configure)
    script_parts.append(cfg)

    script_parts.append(f"make -j{jobs}")
    # Create a staging tree so deploy-lustre.sh can rsync the
    # installed layout directly instead of tracking individual files.
    script_parts.append("rm -rf /lustre/.staging")
    script_parts.append(f"make install DESTDIR=/lustre/.staging -j{jobs}")
    script = "\n".join(script_parts)

    # Use a persistent ccache volume so incremental container
    # builds benefit from cached compilations across runs
    cmd = [
        "podman",
        "run",
        "--rm",
        "--security-opt",
        "label=disable",
        "-v",
        f"{lustre_tree}:/lustre",
        "-v",
        f"{build_tree}:/kernel:ro",
        "-v",
        f"ltvm-ccache-{container_tag.removeprefix('ltvm-build-')}:/ccache",
        container_tag,
        "-c",
        script,
    ]

    print(f"--- Building in container (j{jobs})...")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"Container build failed (rc={r.returncode})")

    # Record stamps on the host filesystem
    (lustre_tree / ".ltvm-kernel").write_text(kver + "\n")
    (lustre_tree / ".ltvm-kernel-path").write_text(str(container_kernel) + "\n")

    ko_files = [
        f for f in lustre_tree.rglob("*.ko") if "kconftest" not in str(f)
    ]
    print(f"--- Build complete: {len(ko_files)} kernel modules")

    return {
        "lustre_tree": str(lustre_tree),
        "kernel_tree": str(build_tree),
        "kernel_version": kver,
        "ko_count": len(ko_files),
        "container": container_tag,
    }


def _build_on_host(
    lustre_tree: Path,
    build_tree: Path,
    kver: str,
    enable_server: bool,
    extra_configure: list[str] | None,
    jobs: int,
    force: bool,
) -> BuildResult:
    """Build Lustre directly on the host."""
    print(f"  Lustre:  {lustre_tree}")
    print(f"  Kernel:  {build_tree}")
    print(f"  Kernel:  {kver}")
    print("  (host build)")

    need_reconf = _needs_reconfigure(lustre_tree, build_tree, force, build_tree)

    if force:
        if (lustre_tree / "Makefile").exists():
            print("--- Cleaning (make distclean)...")
            subprocess.run(
                ["make", "distclean"], cwd=str(lustre_tree), capture_output=True
            )

    if need_reconf or force:
        # Remove stale .ko files from any previous build before
        # reconfiguring (see container build path for rationale).
        subprocess.run(
            ["find", ".", "-name", "*.ko", "-delete"],
            cwd=str(lustre_tree),
            capture_output=True,
        )

    if need_reconf:
        _run_step(["bash", "autogen.sh"], lustre_tree, "autogen.sh")

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

    (lustre_tree / ".ltvm-kernel").write_text(kver + "\n")
    (lustre_tree / ".ltvm-kernel-path").write_text(str(build_tree) + "\n")

    _run_step(["make", f"-j{jobs}"], lustre_tree, f"make -j{jobs}")

    # Create a staging tree so deploy-lustre.sh can rsync the
    # installed layout directly instead of tracking individual files.
    staging = lustre_tree / ".staging"
    if staging.exists():
        import shutil

        shutil.rmtree(staging)
    _run_step(
        ["make", "install", f"DESTDIR={staging}", f"-j{jobs}"],
        lustre_tree,
        "make install (staging)",
    )

    ko_files = [
        f for f in lustre_tree.rglob("*.ko") if "kconftest" not in str(f)
    ]
    print(f"--- Build complete: {len(ko_files)} kernel modules")

    return {
        "lustre_tree": str(lustre_tree),
        "kernel_tree": str(build_tree),
        "kernel_version": kver,
        "ko_count": len(ko_files),
        "container": None,
    }


def _run_step(cmd: list[str], cwd: Path, label: str) -> None:
    """Run a build step, streaming output.  Raises on failure."""
    print(f"--- {label}...")
    r = subprocess.run(cmd, cwd=str(cwd))
    if r.returncode != 0:
        raise RuntimeError(f"{label} failed (rc={r.returncode})")


def lustre_status(
    lustre_tree: str | Path, build_tree: str | Path
) -> StatusResult:
    """Return a status dict for the Lustre build."""
    lustre_tree = Path(lustre_tree).resolve()
    build_tree = Path(build_tree).resolve()

    stamp = lustre_tree / ".ltvm-kernel"
    config_status = lustre_tree / "config.status"
    ko_count = len(
        [f for f in lustre_tree.rglob("*.ko") if "kconftest" not in str(f)]
    )

    built_against = stamp.read_text().strip() if stamp.exists() else None
    current_kver = _kernel_release(build_tree) if build_tree.exists() else None

    stale = (
        built_against != current_kver
        if built_against and current_kver
        else True
    )

    return {
        "configured": config_status.exists(),
        "ko_count": ko_count,
        "built_against": built_against,
        "current_kernel": current_kver,
        "stale": stale,
    }
