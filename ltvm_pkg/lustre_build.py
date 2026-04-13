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

import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import TypedDict

from .vm_state import DEFAULT_TARGET


def _hash_file(path: Path) -> str | None:
    """Return hex sha256 of a file, or None if it doesn't exist."""
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_staging_meta(staging: Path) -> dict | None:
    """Load staging meta, or None if missing/unparseable."""
    meta_file = staging / ".ltvm-staging-meta.json"
    if not meta_file.is_file():
        return None
    try:
        data = json.loads(meta_file.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def staging_path(
    lustre_tree: str | Path,
    target: str,
    arch: str = "x86_64",
    kernel: str | None = None,
) -> Path:
    """Return the host-side Lustre build staging directory.

    Lives **inside the user's lustre tree** at
    ``<tree>/.ltvm-staging/<target>/<arch>/<kernel>/`` rather than
    under the shared ltvm output dir.  This is the only sane layout
    for a multi-user host: alice's
    `~alice/lustre-release/.ltvm-staging/` cannot collide with bob's
    `~bob/lustre-release/.ltvm-staging/`, each user owns their own
    staging tree, and `rm -rf <tree>` cleans up the staging along
    with the source.

    ``kernel`` is the resolved kernel directory name (as produced by
    ``target_config.resolve_kernel``).  It must be keyed at the
    staging root because DESTDIR layout installs userland files
    (``usr/sbin``, ``usr/bin``, ``etc``, etc.) into the same paths
    regardless of kver -- only ``lib/modules/<kver>/`` naturally
    co-exists.  Two sequential `ltvm build-lustre` runs against
    different kernels would silently clobber each other's userland
    without a per-kernel key.  Pre-existing per-target-only staging
    dirs are preserved as a legacy fallback so migration is gradual.

    Callers that don't have a resolved kernel yet (e.g. early status
    callers) may pass ``kernel=None``, which returns the legacy
    per-(target,arch) path.  That path must not be used for new
    builds: use ``build_lustre`` which resolves a kernel first.
    """
    base = Path(lustre_tree) / ".ltvm-staging" / target / arch
    if kernel is None:
        return base
    return base / kernel


class BuildResult(TypedDict):
    lustre_tree: str
    kernel_tree: str
    kernel_version: str
    ko_count: int
    container: str | None
    staging: str


class StatusResult(TypedDict):
    configured: bool
    ko_count: int
    built_against: str | None
    current_kernel: str | None
    stale: bool


def _kernel_release(build_tree: str | Path) -> str:
    """Read the kernel version from the build-tree.

    Reads include/config/kernel.release, which is written by the
    kernel build (kernel_build.py).  Returns "unknown" if not present.
    """
    release_file = Path(build_tree) / "include" / "config" / "kernel.release"
    if release_file.exists():
        return release_file.read_text().strip()
    return "unknown"


def _container_exists(tag: str) -> bool:
    """Check if a podman image exists."""
    r = subprocess.run(["podman", "image", "exists", tag], capture_output=True)
    return r.returncode == 0


def _stamp_suffix(target: str, arch: str) -> str:
    """Reconfigure-stamp suffix that captures both target and arch.

    Stamps live in the (potentially shared) Lustre source tree, so they
    MUST distinguish between two arch builds for the same target --
    otherwise switching arches on the same source tree skips autogen +
    configure and `make` runs against whatever `config.status` the
    other arch left behind, producing corrupt cross-arch artifacts.
    """
    if arch and arch != "x86_64":
        return f"{target}-{arch}"
    return target


def _needs_reconfigure(
    lustre_tree: Path,
    build_tree: Path,
    force: bool,
    target: str = DEFAULT_TARGET,
    enable_server: bool = True,
    arch: str = "x86_64",
) -> bool:
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

    # Check if previous configure used a different kernel or server
    # flag.  Stamps are per-(target,arch) so switching targets OR
    # arches forces reconfigure even when the source tree is shared.
    suffix = _stamp_suffix(target, arch)
    stamp = lustre_tree / f".ltvm-kernel-{suffix}"
    stamp_server = lustre_tree / f".ltvm-server-{suffix}"
    if stamp.exists():
        prev = stamp.read_text().strip()
        cur = _kernel_release(build_tree)
        if prev != cur:
            print(f"  Kernel changed ({prev} -> {cur}), reconfiguring")
            return True
    else:
        return True  # no stamp = never built for this target
    if stamp_server.exists():
        prev_server = stamp_server.read_text().strip()
        if prev_server != str(enable_server):
            print(
                f"  Server flag changed ({prev_server} -> {enable_server}), reconfiguring"
            )
            return True
    else:
        return True  # no server stamp = never built for this target

    return False


def build_lustre(
    lustre_tree: str | Path,
    build_tree: str | Path,
    *,
    container_tag: str | None = None,
    target: str = DEFAULT_TARGET,
    enable_server: bool = True,
    extra_configure: list[str] | None = None,
    jobs: int | None = None,
    force: bool = False,
    arch: str = "x86_64",
    kernel: str | None = None,
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

    if not container_tag:
        raise RuntimeError(
            "No build container specified. Run: ltvm build-container <target>"
        )
    if not _container_exists(container_tag):
        raise RuntimeError(
            f"Build container '{container_tag}' not found.\n"
            f"Run: ltvm build-container <target>"
        )

    return _build_in_container(
        lustre_tree,
        build_tree,
        container_tag,
        kver,
        enable_server,
        extra_configure,
        jobs,
        force,
        arch=arch,
        target=target,
        kernel=kernel,
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
    arch: str = "x86_64",
    target: str = DEFAULT_TARGET,
    kernel: str | None = None,
) -> BuildResult:
    """Build Lustre inside the build container.

    Mount layout:
      /lustre  -- Lustre source (read-write, build here)
      /kernel  -- kernel build-tree (read-only)
    """
    print(f"  Container: {container_tag}")
    print(f"  Lustre:    {lustre_tree}")
    print(f"  Kernel:    {build_tree}")
    print(f"  Version:   {kver}")

    need_reconf = _needs_reconfigure(
        lustre_tree,
        build_tree,
        force,
        target=target,
        enable_server=enable_server,
        arch=arch,
    )

    # Detect cross-compilation
    import platform

    host_machine = platform.machine()
    cross_compiling = arch == "aarch64" and host_machine != "aarch64"

    # Build the shell script to run inside the container
    script_parts = ["set -e", "cd /lustre"]

    # Install cross-compiler and cross-arch dev libraries if needed
    if cross_compiling:
        script_parts.append(
            "echo '--- Installing aarch64 cross-compiler and dev libs...'"
        )
        script_parts.append(
            "if command -v dnf &>/dev/null; then "
            "dnf -y install gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu 2>&1 | tail -3; "
            "elif command -v apt-get &>/dev/null; then "
            # Install the cross-compiler first (amd64 package, no multiarch needed)
            "apt-get update -qq && "
            "apt-get install -y gcc-aarch64-linux-gnu 2>&1 | tail -3 && "
            # Now set up multiarch for arm64 cross-dev libraries.
            # Ubuntu 24.04 uses DEB822 .sources files; pin them to amd64
            # and add a separate arm64 source pointing at ports.ubuntu.com
            "dpkg --add-architecture arm64 && "
            r"grep -rl '^Types:' /etc/apt/sources.list.d/*.sources 2>/dev/null "
            r"| xargs -I{} sed -i '/^Architectures:/d; /^Types:/a Architectures: amd64' {} && "
            "printf 'Types: deb\\n"
            "URIs: http://ports.ubuntu.com/ubuntu-ports\\n"
            "Suites: noble noble-updates\\n"
            "Components: main universe\\n"
            "Architectures: arm64\\n' > /etc/apt/sources.list.d/arm64-ports.sources && "
            "apt-get update -qq 2>&1 | tail -3 && "
            "apt-get install -y "
            "libmount-dev:arm64 libyaml-dev:arm64 libselinux1-dev:arm64 "
            "zlib1g-dev:arm64 libnl-3-dev:arm64 libnl-genl-3-dev:arm64 "
            "libaio-dev:arm64 libkeyutils-dev:arm64 2>&1 | tail -5; "
            "fi"
        )

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
        # Remove stale config/compile lock dirs (*.d directories).
        # When a previous configure was killed mid-compile, it leaves
        # behind empty .d lock dirs that the next configure spins forever
        # trying to acquire (the mkdir-based lock loop has no timeout).
        # Use rmdir so legitimate non-empty .d dirs (e.g. kbuild dependency
        # tracking) are preserved -- only empty leftovers get cleaned.
        script_parts.append(
            "find . -maxdepth 3 -name '*.d' -type d"
            " -not -path '*/.git/*' -exec rmdir {} + 2>/dev/null || true"
        )

    # Run autogen.sh + configure only when needed.
    #
    # autogen.sh regenerates aclocal.m4/libtool stubs with the container's
    # toolchain.  We must re-run it when:
    #   1. _needs_reconfigure() says so (kernel/path changed, --force)
    #   2. The container's libtool version changed since last autogen run
    #      (stamp file: .ltvm-container-libtool)
    #
    # The libtool check happens inside the container so we can compare the
    # exact version the container has.
    cfg = "./configure --with-linux=/kernel --disable-gss --disable-crypto"
    if cross_compiling:
        cfg += " --host=aarch64-linux-gnu"
        cfg += " CC=aarch64-linux-gnu-gcc"
        cfg += " ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu-"
        cfg += " PKG_CONFIG_PATH=/usr/lib/aarch64-linux-gnu/pkgconfig"
        cfg += " PKG_CONFIG_LIBDIR=/usr/lib/aarch64-linux-gnu/pkgconfig"
    if enable_server:
        cfg += " --enable-server"
    else:
        cfg += " --disable-server"
    if extra_configure:
        # shlex.quote each arg so paths with spaces (e.g.
        # --with-linux="/tmp/build dir/linux") and configure flags with
        # shell metacharacters (e.g. CFLAGS='-O2 -g') survive
        # interpolation into the bash heredoc fed to `podman run -c`.
        # Plain space-join would split a quoted value into multiple
        # configure args -- or worse, a metachar in the value would be
        # re-interpreted by the container shell.
        cfg += " " + " ".join(shlex.quote(a) for a in extra_configure)

    # Shell block: run autogen+configure when force-requested OR when the
    # container's libtool version differs from the last autogen stamp.
    force_reconf_flag = "1" if need_reconf else "0"
    script_parts.append(f"""\
FORCE_RECONF={force_reconf_flag}
LTVER=$(libtool --version 2>/dev/null | head -1)
STAMPED=$(cat .ltvm-container-libtool 2>/dev/null || echo '')
if [ "$FORCE_RECONF" = "1" ] || [ "$LTVER" != "$STAMPED" ]; then
  [ "$LTVER" != "$STAMPED" ] && echo "  libtool changed, re-running autogen+configure"
  bash autogen.sh
  {cfg}
  echo "$LTVER" > .ltvm-container-libtool
else
  echo "  autogen/configure up to date, skipping"
fi""")

    make_cross = ""
    if cross_compiling:
        make_cross = " ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu-"
    script_parts.append(f"make{make_cross} -j{jobs}")
    # Install into /staging (bind-mounted from
    # <lustre_tree>/.ltvm-staging/<target>[/<arch>]/) so build artifacts
    # stay outside the autotools tree but still inside the user's lustre
    # tree -- which means they're naturally per-user on a multi-user
    # host with no extra namespacing.
    script_parts.append("rm -rf /staging/*")
    script_parts.append(f"make{make_cross} install DESTDIR=/staging -j{jobs}")
    script = "\n".join(script_parts)

    # Ensure the staging directory exists on the host before mounting.
    # arch + kernel are both folded into the path so cross-arch and
    # cross-kernel builds for the same target don't clobber each
    # other's .ko files OR the userland portion (usr/sbin, etc.) that
    # has no per-kver discriminator inside the DESTDIR layout.
    resolved_kernel = kernel
    if resolved_kernel is None:
        try:
            from .target_config import TargetConfig

            resolved_kernel = TargetConfig(target, arch=arch).resolve_kernel(
                None
            )
        except Exception:
            resolved_kernel = kver
    host_staging = staging_path(
        lustre_tree, target, arch=arch, kernel=resolved_kernel
    )
    host_staging.mkdir(parents=True, exist_ok=True)
    # When invoked via sudo, chown the staging dir to the real user so
    # the user can read their own modules and `rm -rf` their tree
    # without sudo.  The container side runs as root inside the
    # container and writes to the bind mount, but the host-side dir
    # we just created is owned by whoever ran ltvm.
    sudo_user_env = os.environ.get("SUDO_USER")
    if sudo_user_env and os.getuid() == 0:
        try:
            import pwd

            pw = pwd.getpwnam(sudo_user_env)
            os.chown(host_staging, pw.pw_uid, pw.pw_gid)
            # Also chown all parent dirs we may have just created up to
            # (but not including) the lustre tree root.
            tree_root = Path(lustre_tree).resolve()
            cur = host_staging.parent
            while cur != tree_root and cur != cur.parent:
                try:
                    st = cur.stat()
                    if st.st_uid == 0:
                        os.chown(cur, pw.pw_uid, pw.pw_gid)
                except OSError:
                    break
                cur = cur.parent
        except (KeyError, OSError):
            # SUDO_USER not in passwd or chown failed -- leave the dir
            # root-owned; the user can fix it manually if needed.  Don't
            # block the build over chown.
            pass

    # Use a persistent ccache volume so incremental container
    # builds benefit from cached compilations across runs
    cmd = [
        "podman",
        "run",
        "--rm",
        "--security-opt",
        "label=disable",
        # 10 minute ceiling: a clean rocky9 build runs in ~5 minutes,
        # double that catches stuck builds without false-positive killing
        # slow-but-progressing ones.  Without this, a hung autoconf or
        # mkdir lock loop blocks the entire `ltvm build-lustre` invocation
        # forever instead of failing cleanly.
        "--timeout",
        "600",
        "-v",
        f"{lustre_tree}:/lustre",
        "-v",
        f"{build_tree}:/kernel:ro",
        "-v",
        f"{host_staging}:/staging",
        "-v",
        f"ltvm-ccache-{container_tag.removeprefix('ltvm-build-')}:/ccache",
        container_tag,
        "-c",
        script,
    ]

    # Run podman as whoever ltvm itself runs as: do NOT drop privileges
    # to SUDO_USER here.
    #
    # Earlier versions did `sudo -u $SUDO_USER podman run ...` so the
    # files left in the bind-mounted source tree were owned by the real
    # user.  That worked in the single-user model where each user had
    # their own rootless podman storage holding the build container,
    # but it breaks the multi-user model: when ltvm is invoked via
    # sudo the build container lives in root's podman storage (placed
    # there by `sudo ltvm fetch`).  Dropping to admin would point
    # podman at admin's empty storage, the container would not be
    # found, and podman would attempt a short-name pull from a remote
    # registry and die with "short-name resolution enforced but cannot
    # prompt".
    #
    # The trade-off is that autotools writes (Makefile, config.status,
    # .deps/, *.ko in the source tree under ldiskfs/, lnet/, lustre/)
    # land owned by root on the host because the container's root maps
    # to host root in rootful podman.  We chown the whole tree back to
    # SUDO_USER after the build so the user can git pull, edit, and
    # rerun the build without sudo if they want.
    print(f"--- Building in container (j{jobs})...")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"Container build failed (rc={r.returncode})")

    # Chown the lustre tree back to the real user after the build.
    # The container's root mapped to host root in the bind mount, so
    # autotools left a trail of root-owned files (Makefile,
    # config.status, .deps/, intermediate .o/.ko, etc.) inside the
    # tree.  Without this fix the user can't `git pull`, can't edit,
    # and can't even `rm -rf` their own checkout.  Best-effort: if
    # SUDO_USER isn't set or chown fails for any reason, we leave the
    # tree as-is rather than failing the build.
    if sudo_user_env and os.getuid() == 0:
        try:
            import pwd

            pw = pwd.getpwnam(sudo_user_env)
            subprocess.run(
                [
                    "chown",
                    "-R",
                    f"{pw.pw_uid}:{pw.pw_gid}",
                    str(lustre_tree),
                ],
                check=False,
            )
        except (KeyError, OSError):
            pass

    # Record per-(target, arch) stamps on the host filesystem so a
    # subsequent build for the OTHER arch sees them as missing and
    # forces a fresh autogen+configure pass.
    suffix = _stamp_suffix(target, arch)
    (lustre_tree / f".ltvm-kernel-{suffix}").write_text(kver + "\n")
    (lustre_tree / f".ltvm-server-{suffix}").write_text(
        str(enable_server) + "\n"
    )
    # Best-effort: clean up the legacy .ltvm-kernel-path-* stamp left
    # by older builds; the value was always "/kernel" so the check it
    # protected was a no-op.
    legacy = lustre_tree / f".ltvm-kernel-path-{target}"
    if legacy.exists():
        legacy.unlink()

    # Drop a build stamp at the staging root.  cmd_deploy uses it as
    # the reference mtime for the source-tree freshness check, so an
    # in-place rewrite of an existing .ko file under
    # lib/modules/.../extra/ (which doesn't update the staging dir's
    # own mtime) still gets a fresh "newer than this" comparison.
    (host_staging / ".ltvm-staging-stamp").write_text(kver + "\n")
    # Per-kernel meta so build-image --with-lustre can fold the built
    # Module.symvers hash into the image input hash and invalidate the
    # cache when a rebuilt Lustre lands new modules.
    symvers_hash = _hash_file(build_tree / "Module.symvers")
    meta_text = json.dumps(
        {
            "kernel_version": kver,
            "kernel_name": resolved_kernel,
            "target": target,
            "arch": arch,
            "module_symvers_sha256": symvers_hash,
        },
        indent=2,
    )
    (host_staging / ".ltvm-staging-meta.json").write_text(meta_text + "\n")

    ko_files = list(host_staging.rglob("*.ko"))
    print(f"--- Build complete: {len(ko_files)} kernel modules")

    return {
        "lustre_tree": str(lustre_tree),
        "kernel_tree": str(build_tree),
        "kernel_version": kver,
        "ko_count": len(ko_files),
        "container": container_tag,
        "staging": str(host_staging),
    }


def lustre_status(
    lustre_tree: str | Path,
    build_tree: str | Path,
    target: str = DEFAULT_TARGET,
    arch: str = "x86_64",
    kernel: str | None = None,
) -> StatusResult:
    """Return a status dict for the Lustre build."""
    lustre_tree = Path(lustre_tree).resolve()
    build_tree = Path(build_tree).resolve()

    stamp = lustre_tree / f".ltvm-kernel-{_stamp_suffix(target, arch)}"
    config_status = lustre_tree / "config.status"
    # Prefer the per-kernel staging when a kernel is known.  Fall back
    # to the legacy per-(target,arch) path so status for pre-migration
    # trees still reports a ko_count instead of silently zero.
    host_staging = staging_path(lustre_tree, target, arch=arch, kernel=kernel)
    if kernel is not None and not host_staging.is_dir():
        legacy = staging_path(lustre_tree, target, arch=arch, kernel=None)
        if legacy.is_dir():
            host_staging = legacy
    ko_count = (
        len(list(host_staging.rglob("*.ko"))) if host_staging.is_dir() else 0
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
