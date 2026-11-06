"""Deploy / llmount subcommands.

cmd_deploy is the big one: auto-detects target from VM metadata,
picks up a bundled snapshot from ``ltvm fetch`` when present, else
runs ``ltvm build lustre`` into per-kernel staging, then calls
``deploy_to_vm`` to rsync modules and userland into the VM.

cmd_llmount is a thin wrapper around vm_commands.cmd_llmount.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_OK,
    _error,
    _output,
)
from ltvm_pkg.lustre_build import read_staging_meta


def _cli_attr(name: str) -> Any:
    """Look up ``name`` on ``ltvm_pkg.cli`` at call time."""
    import ltvm_pkg.cli as _cli

    return getattr(_cli, name)


def _sources_newer_than(
    src: Path, stamp: Path, *, first_only: bool
) -> list[str] | None:
    """Source files in ``src`` modified after ``stamp``; None if find failed.

    Build products, VCS dirs and ltvm's own state are skipped.
    ``first_only`` stops at the first hit, which is all a yes/no needs.
    """
    tail = ["-print", "-quit"] if first_only else ["-print"]
    r = subprocess.run(
        [
            "find",
            str(src),
            "-path",
            "*/.git",
            "-prune",
            "-o",
            "-path",
            "*/autom4te.cache",
            "-prune",
            "-o",
            "-path",
            "*/_lpb",
            "-prune",
            "-o",
            "-path",
            "*/kconftest.dir",
            "-prune",
            "-o",
            "(",
            "-name",
            "*.o",
            "-o",
            "-name",
            "*.ko",
            "-o",
            "-name",
            "*.a",
            "-o",
            "-name",
            "*.so",
            "-o",
            "-name",
            "*.so.*",
            "-o",
            "-name",
            "*.cmd",
            "-o",
            "-name",
            "*.d",
            "-o",
            "-name",
            "*.tmp_*",
            "-o",
            "-name",
            "conftest*",
            "-o",
            "-name",
            "config.log",
            "-o",
            "-name",
            "config.status",
            "-o",
            "-name",
            ".ltvm-*",
            ")",
            "-prune",
            "-o",
            "-newer",
            str(stamp),
            *tail,
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return None
    return [ln for ln in r.stdout.splitlines() if ln]


def _refresh_staged_sources(
    staging: Path, tree: Path, *, userspace_only: bool, quiet: bool
) -> str | None:
    """Copy edited scripts and other verbatim sources into staging.

    Returns an error message when a staged file cannot be refreshed.
    Under --userspace-only, which never rebuilds, also warns about any
    other source changed since the build: what is compiled from it is
    not in staging.
    """
    from ltvm_pkg import staging_sources

    try:
        refreshed = staging_sources.refresh(staging, tree)
    except PermissionError as e:
        import getpass

        return (
            f"staging {staging} is not writable by {getpass.getuser()}, "
            f"so edited sources cannot be copied into it ({e}).  It was "
            f"built by another user or under sudo: fix its ownership, or "
            f"remove it and rebuild as yourself with `ltvm build lustre "
            f"--lustre-tree {tree} --force`."
        )
    except OSError as e:
        return f"could not refresh staged files from {tree}: {e}"
    if refreshed and not quiet:
        print(staging_sources.describe(refreshed))

    stamp = staging / ".ltvm-staging-stamp"
    if not userspace_only or not stamp.is_file():
        return None
    covered = {
        str(tree / s)
        for s in staging_sources.source_pairs(staging, tree).values()
    }
    newer = _sources_newer_than(tree, stamp, first_only=False) or []
    # Only files git tracks: untracked logs and scratch files are not
    # something a rebuild would ship.
    tracked = staging_sources.tracked_files(tree)
    rest = sorted(
        rel
        for rel in (str(Path(p).relative_to(tree)) for p in newer)
        if str(tree / rel) not in covered
        and (tree / rel).is_file()
        and (tracked is None or rel in tracked)
    )
    if rest:
        print(
            f"warning: {len(rest)} source file(s) changed since this "
            f"staging was built, and --userspace-only does not rebuild: "
            f"{staging_sources.sample(rest)}\n"
            "  Deploy without --userspace-only to build and ship them.",
            file=sys.stderr,
        )
    return None


def _bundled_snapshot(tc: Any, kernel: str, variant: str | None) -> Path | None:
    """The Lustre that `ltvm fetch` unpacked for this kernel and variant.

    Identified by the .ltvm-snapshot.json marker snapshot_lustre writes;
    a non-base variant's snapshot nests under its own name.
    """
    root = tc.output_dir / "kernels" / kernel / "lustre-artifacts"
    snap = root if variant in (None, "", "base") else root / variant
    return snap if (snap / ".ltvm-snapshot.json").is_file() else None


def _bundled_kernels(tc: Any, variant: str | None) -> list[str]:
    """The kernels of this target that have a bundled Lustre."""
    try:
        names = sorted(p.name for p in (tc.output_dir / "kernels").iterdir())
    except OSError:
        return []
    return [n for n in names if _bundled_snapshot(tc, n, variant) is not None]


def cmd_deploy(args: argparse.Namespace) -> int:
    use_json = args.json
    target = getattr(args, "target", None)
    kernel = getattr(args, "kernel", None)

    from ltvm_pkg.vm_state import VMInfo, VMNotFound

    # Get VM info
    try:
        vm = VMInfo.load(args.vm)
    except VMNotFound as e:
        return _error(str(e), use_json)

    from ltvm_pkg import vm_claim

    try:
        vm_claim.check(vm.name, "deploy to")
        vm_claim.require_no_deploy([vm.name])
    except vm_claim.ClaimError as e:
        return _error(str(e), use_json)
    tree = getattr(args, "lustre_tree", None)
    try:
        vm_claim.auto_claim(
            vm.name, str(Path(tree).resolve()) if tree else None
        )
    except vm_claim.ClaimHeld as e:
        return _error(str(e), use_json)

    # Auto-detect target from VM metadata
    if not target:
        target = vm.os_id or None
        if target and not use_json:
            print(f"  Auto-detected target: {target}")
        if not target:
            return _error(
                f"Cannot detect target OS for VM '{args.vm}'. "
                f"Pass --target explicitly.",
                use_json,
            )

    # Resolve kernel name and target config.  Pass vm.arch through so
    # the target's output_dir is arch-qualified -- otherwise an aarch64
    # VM looks for its kernel/staging under the x86_64 output paths and
    # fails to find anything.  We require a valid target here so a
    # missing entry in targets.yaml fails loudly instead of silently
    # falling back to RHEL paths.
    vm_arch = vm.arch
    # Thread the VM's recorded variant into TargetConfig so tc.container_tag
    # picks the matching build container (e.g. ltvm-build-rocky9-mofed for
    # a MOFED VM) and tc.image_output_dir() resolves to the variant image.
    vm_variant = getattr(vm, "variant", "base") or "base"
    TargetConfig = _cli_attr("TargetConfig")
    try:
        tc = TargetConfig(target, arch=vm_arch, variant=vm_variant)
    except ValueError as e:
        return _error(
            f"Unknown target '{target}' for VM '{args.vm}': {e}",
            use_json,
            hint="Check `ltvm status` for valid targets.",
        )
    resolved_kernel = tc.resolve_kernel(kernel)
    os_family = tc.os_family

    # The kernel the VM boots, falling back to the target's default:
    # both the staging and a bundled snapshot are per kernel, and modules
    # built for any other kernel will not load.
    deploy_kernel = resolved_kernel
    if vm.kernel:
        vm_kernel_name = Path(vm.kernel).parent.name
        if vm_kernel_name:
            deploy_kernel = tc.resolve_kernel(vm_kernel_name)

    # Resolve build path:
    #   1. Explicit --lustre-tree PATH wins (including --lustre-tree .)
    #   2. Otherwise the bundled snapshot from `ltvm fetch` for the VM's
    #      kernel, deployed straight from the artifacts dir, which may be
    #      read-only: nothing is written there.
    #   3. Otherwise, fall back to cwd
    build_arg = getattr(args, "lustre_tree", None)
    bundled_snapshot: Path | None = None
    if build_arg is not None:
        build_path = Path(build_arg).resolve()
    else:
        bundled_snapshot = _bundled_snapshot(tc, deploy_kernel, vm_variant)
        if bundled_snapshot is not None:
            build_path = bundled_snapshot
            from ltvm_pkg.paths import load_meta_safe

            snap_meta = load_meta_safe(bundled_snapshot / ".ltvm-snapshot.json")
            snap_kernel = (
                snap_meta.get("kernel") if isinstance(snap_meta, dict) else None
            )
            if snap_kernel and not (
                deploy_kernel == snap_kernel
                or deploy_kernel.startswith(f"{snap_kernel}-")
            ):
                return _error(
                    f"The bundled Lustre at {bundled_snapshot} was built "
                    f"for kernel {snap_kernel}, but {args.vm} boots "
                    f"{deploy_kernel}",
                    use_json,
                    hint="Pass --lustre-tree <Lustre source tree> to build "
                    "for the VM's kernel.",
                )
            if not use_json:
                print(
                    f"  Using bundled Lustre (from ltvm fetch) for "
                    f"{deploy_kernel}"
                )
        else:
            build_path = Path(".").resolve()

    if not build_path.is_dir():
        return _error(f"Build path not found: {build_path}", use_json)

    # Validate that --lustre-tree points at an actual Lustre source tree
    # before we try to feed it to `ltvm build lustre`.  Skip this when
    # we picked up a bundled snapshot, which is a DESTDIR layout (usr/,
    # lib/modules/), not a source tree.  Without this validation a typo
    # like `--lustre-tree /wrong/dir` produces a confusing error several
    # subprocess hops away inside the build container.
    if bundled_snapshot is None:
        missing = [
            n
            for n in ("configure.ac", "lustre", "lnet")
            if not (build_path / n).exists()
        ]
        if missing and build_arg is None:
            others = _bundled_kernels(tc, vm_variant)
            bundles = (
                f"the bundled Lustre from `ltvm fetch` is for "
                f"{', '.join(others)} only"
                if others
                else "there is no bundled Lustre from `ltvm fetch`"
            )
            return _error(
                f"Nothing to deploy to {args.vm}: it boots {deploy_kernel}, "
                f"{bundles}, and no --lustre-tree was given (the current "
                f"directory {build_path} is not a Lustre source tree)",
                use_json,
                hint="Pass --lustre-tree <Lustre source tree> to build for "
                "this kernel"
                + (
                    ", or create the VM with --kernel naming a kernel "
                    "that has a bundled Lustre."
                    if others
                    else "."
                ),
            )
        if missing:
            return _error(
                f"--lustre-tree:'{build_path}' does not look like a Lustre "
                f"source tree (missing: {', '.join(missing)})",
                use_json,
            )

    userspace_only = getattr(args, "userspace_only", False)

    # --zfs-version implies --zfs; --fstype zfs implies it too, since
    # a VM cannot mount a ZFS target without ZFS on it.  Conversely
    # --zfs on a *deploy* means "run this VM on ZFS", so it implies
    # --fstype zfs unless the user pinned an fstype explicitly -- the
    # one case for `--zfs --fstype ldiskfs` is staging ZFS on a VM you
    # want to keep running ldiskfs for now.
    fstype = getattr(args, "fstype", None)
    zfs_source = getattr(args, "zfs_source", None)
    if zfs_source and getattr(args, "zfs_version", None):
        return _error("pass --zfs-version or --zfs-source, not both", use_json)
    ship_zfs = (
        bool(getattr(args, "zfs", False))
        or bool(getattr(args, "zfs_version", None))
        or bool(zfs_source)
    )
    want_zfs = ship_zfs or (fstype == "zfs" and not userspace_only)
    if want_zfs and fstype is None:
        fstype = "zfs"
    zfs_version_arg = getattr(args, "zfs_version", None)
    if zfs_source:
        from ltvm_pkg.zfs_build import ZfsBuildError, source_label

        zfs_source = str(Path(zfs_source).expanduser().resolve())
        try:
            # The version a build from it records, to judge the staging.
            zfs_version_arg = source_label(Path(zfs_source))
        except ZfsBuildError as e:
            return _error(str(e), use_json)

    # --userspace-only ships no modules, so it cannot install ZFS.  It
    # can still pin --fstype zfs on a VM that has ZFS already, which
    # deploy_to_vm checks.
    if ship_zfs and userspace_only:
        return _error(
            "--zfs and --userspace-only are incompatible: a "
            "userspace-only deploy ships no kernel modules (--fstype zfs "
            "alone keeps a VM that already has ZFS on it)",
            use_json,
        )

    ost_count = getattr(args, "ost_count", None)
    if ost_count is not None:
        if ost_count < 0:
            return _error("--ost-count must be 0 or more", use_json)
        if getattr(args, "ram_osts", 0):
            return _error(
                "--ost-count and --ram-osts both set the OSTs; pass one",
                use_json,
            )

    # Staging lives inside the lustre tree at
    # <build_path>/.ltvm-staging/<target>/<arch>/<kernel>/, per-kernel
    # so two kernels' userland (usr/sbin, etc.) coexist without
    # clobbering each other.  A bundled snapshot is already a staging
    # tree for the VM's kernel, and is deployed as it stands.
    from ltvm_pkg.lustre_build import staging_lock
    from ltvm_pkg.lustre_build import staging_path as _staging_path

    if bundled_snapshot is not None:
        staging = bundled_snapshot
    else:
        staging = _staging_path(
            build_path,
            target,
            arch=vm_arch,
            kernel=deploy_kernel,
            variant=vm.variant,
        )

    def _staging_matches_kernel_and_flags(staging: Path) -> bool:
        """Was this staging built against the kernel and flags in play now?

        Source-file mtimes -- the only thing this fast path used to
        consider -- cannot see either.  Rebuilding the kernel from an
        edited patch series or a changed kernels.config leaves the
        kernel directory name and kernel.release identical while
        Module.symvers changes, so `ltvm deploy-lustre` said "Staging
        up to date" and shipped modules linked against the previous
        ABI: the VM then fails `modprobe lustre` with "disagrees about
        version of symbol", or loads modules whose config.h HAVE_*
        macros were probed against the old kernel.  Changing
        configure_args in targets.yaml was equally invisible.

        build_lustre records both signals in .ltvm-staging-meta.json;
        this is the consumer that was missing.  Unknown/absent fields
        mean staging predates them -- rebuild rather than guess.
        """
        meta = _cli_attr("read_staging_meta")(staging)
        if not isinstance(meta, dict):
            return False

        build_tree = tc.kernel_output_dir(kernel=deploy_kernel) / "build-tree"
        recorded_symvers = meta.get("module_symvers_sha256")
        if not isinstance(recorded_symvers, str) or not recorded_symvers:
            return False
        current_symvers = _cli_attr("_hash_file")(build_tree / "Module.symvers")
        if current_symvers != recorded_symvers:
            if not use_json:
                print("  Kernel ABI changed since staging was built")
            return False

        # A staging tree built without ZFS carries the same
        # configure_sha256 as the tree's stamp (both are the non-ZFS
        # hash), so the checks below would call it fresh and --zfs
        # would silently do nothing.  The staged build's own record of
        # which ZFS it used is the only thing that can answer this.
        staged_zfs = meta.get("zfs_version")
        if want_zfs:
            if not staged_zfs:
                if not use_json:
                    print("  Staging was built without ZFS")
                return False
            if zfs_version_arg and staged_zfs != zfs_version_arg:
                if not use_json:
                    print(
                        f"  Staging was built against ZFS {staged_zfs}, "
                        f"not {zfs_version_arg}"
                    )
                return False

        recorded_cfg = meta.get("configure_sha256")
        if not isinstance(recorded_cfg, str) or not recorded_cfg:
            return False
        cfg_stamp = (
            build_path
            / f".ltvm-configure-{_cli_attr('_stamp_suffix')(target, tc.arch)}"
        )
        if not cfg_stamp.is_file():
            return False
        if cfg_stamp.read_text().strip() != recorded_cfg:
            if not use_json:
                print("  Configure flags changed since staging was built")
            return False
        return True

    def _staging_is_fresh(staging: Path, src: Path) -> bool:
        """Check if the staging dir is newer than all source files.

        Uses an explicit `.ltvm-staging-stamp` file written at the end
        of a successful build_lustre run as the reference mtime, NOT
        the staging dir's own mtime: directory mtime only changes when
        entries are added/removed in that exact directory, so an
        in-place rewrite of an existing .ko file under
        lib/modules/.../extra/ leaves the top-level staging mtime
        unchanged and the freshness check would lie.
        """
        if not staging.is_dir():
            return False
        if not any(staging.rglob("*.ko")):
            return False
        stamp = staging / ".ltvm-staging-stamp"
        if not stamp.is_file():
            # Pre-stamp builds (or a build that crashed before writing
            # the stamp): treat as stale so we rebuild rather than
            # silently skip.
            return False
        if not _staging_matches_kernel_and_flags(staging):
            return False
        newer = _sources_newer_than(src, stamp, first_only=True)
        return newer == []

    if userspace_only:
        # No compat gate here: --userspace-only installs userspace RPMs
        # only, never triggers a Lustre rebuild.  The staging was
        # vetted when it was originally built.  --force-compat is a
        # no-op on this branch by design.
        if not staging.is_dir():
            return _error(
                f"No staging for {target} -- run: ltvm build lustre "
                f"{target} --lustre-tree {build_path}",
                use_json,
            )
        if not use_json:
            print("  Userspace-only deploy (skipping kernel modules)")
    elif bundled_snapshot is not None:
        # Bundled snapshot: staging was either just mirrored or already
        # populated.  Don't run _staging_is_fresh -- build_path here is
        # the snapshot's DESTDIR layout, NOT a Lustre source tree, so
        # falling through to `ltvm build lustre --lustre-tree <snapshot>`
        # would error out with "not a Lustre source tree".
        # --force-compat is a no-op on this branch: the snapshot was
        # compat-gated by the publisher at package time.
        if not use_json:
            print("  Using bundled staging, skipping source build")
    else:
        # The freshness check compares the staging with the tree's last
        # configure, not with the flags asked for now; only the build
        # can tell whether --configure changes anything.
        configure = getattr(args, "configure", None)
        staging_fresh = not configure and _staging_is_fresh(staging, build_path)

        if staging_fresh:
            if not use_json:
                print("  Staging up to date, skipping build")
        else:
            _cli_attr("_gate_lustre_validation")(
                tc,
                build_path,
                force=args.force_compat,
                kernel_build_tree=tc.kernel_output_dir(kernel=deploy_kernel)
                / "build-tree",
                kernel=deploy_kernel,
            )
            build_cmd = [
                "ltvm",
                "build",
                "lustre",
                target,
                "--lustre-tree",
                str(build_path),
            ]
            # Forward the VM's actual kernel to build-lustre.  Without
            # this, a VM created with a non-default kernel rebuilds
            # Lustre against the target's *default* kernel tree, producing
            # modules that the running kernel can't load.  Cluster deploy
            # already does this; single-node deploy was missing it.
            if vm.kernel:
                kernel_name = Path(vm.kernel).parent.name
                if kernel_name:
                    build_cmd += ["--kernel", kernel_name]
            # Forward the VM's arch unconditionally so cross-arch builds
            # end up in the right staging dir and link against the right
            # toolchain.  Comparing against the literal "x86_64" was
            # wrong for a target whose default arch is something else:
            # an x86_64 VM built against an aarch64-default target would
            # then NOT forward --arch, and the inner build-lustre would
            # default to aarch64 and deploy the wrong modules.  Idempotent
            # for x86_64-default targets too, so just always forward.
            build_cmd += ["--arch", vm_arch]
            # Forward the VM's variant for the same reason as --kernel
            # and --arch above.  staging was computed with the VM's
            # variant (which nests one level deeper for non-base), so
            # without this the child builds the base variant, writes to
            # the base staging dir, and the check below fails with
            # "no staging with modules" after a full build.
            if vm_variant and vm_variant != "base":
                build_cmd += ["--variant", vm_variant]
            # Forward --force-compat.  The parent has already run the
            # compat gate with force=True and passed, but the child
            # `ltvm build lustre` re-runs the same gate with
            # force=False and SystemExit(1)s -- so the flag the help
            # text promises applies to "deploy-lustre ... when it
            # rebuilds from source" could never actually override a
            # refusal.  cmd_cluster_deploy already forwards it.
            if args.force_compat:
                build_cmd += ["--force-compat"]
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
            # Under --json the child's human output would land in the
            # middle of this command's JSON document, so it goes to
            # stderr instead -- still live, still visible, just not in
            # the stream a consumer is parsing.  (--json is not
            # forwarded: the child's own envelope is not this
            # command's.)
            sys.stdout.flush()
            build_proc = subprocess.run(
                build_cmd,
                capture_output=False,
                stdout=sys.stderr if use_json else None,
            )
            if build_proc.returncode != 0:
                return _error(
                    f"Lustre build failed (rc={build_proc.returncode})",
                    use_json,
                )

            if not staging.is_dir() or not any(staging.rglob("*.ko")):
                return _error(
                    f"Lustre build succeeded but no staging with modules for {target}",
                    use_json,
                )

    if bundled_snapshot is None:
        with staging_lock(staging, exclusive=True):
            err = _refresh_staged_sources(
                staging,
                build_path,
                userspace_only=userspace_only,
                quiet=use_json,
            )
        if err is not None:
            return _error(err, use_json)

    # Shared from here to the end of the stream: other deploys of this
    # staging may stream alongside, but none may rebuild or refresh it.
    # Nothing rewrites a bundled snapshot but `ltvm fetch`.
    stream_lock = (
        contextlib.nullcontext()
        if bundled_snapshot is not None
        else staging_lock(staging, exclusive=False)
    )
    with stream_lock:
        # Which ZFS to ship is the staged build's decision, not the
        # command line's: osd_zfs.ko is linked against one specific ZFS
        # build, so shipping any other would produce a module that won't
        # load.  build_lustre records the version it used.
        zfs_staging: Path | None = None
        staged_meta = read_staging_meta(staging)
        staged_zfs_version = (
            staged_meta.get("zfs_version")
            if isinstance(staged_meta, dict)
            else None
        )
        if staged_zfs_version and not userspace_only:
            from ltvm_pkg.zfs_build import (
                ZfsBuildError,
                find_zfs_staging,
                rebuild_flag,
            )

            try:
                zfs_staging = find_zfs_staging(
                    tc,
                    deploy_kernel,
                    staged_zfs_version,
                    recorded=(
                        staged_meta.get("zfs_dir")
                        if isinstance(staged_meta, dict)
                        else None
                    ),
                )
            except ZfsBuildError as e:
                return _error(
                    str(e),
                    use_json,
                    hint=f"Rebuild Lustre with it: ltvm build lustre "
                    f"{target} --lustre-tree {build_path} --kernel "
                    f"{deploy_kernel} {rebuild_flag(staged_zfs_version)} "
                    f"--force",
                )
            if not use_json:
                print(f"  Shipping ZFS {staged_zfs_version}")
        elif want_zfs and not userspace_only:
            return _error(
                "ZFS was requested but the Lustre staging being deployed "
                "was not built with it",
                use_json,
                hint="This happens with a bundled snapshot from `ltvm "
                "fetch`, which is published without ZFS.  Pass "
                "--lustre-tree <source tree> to build one with it.",
            )

        try:
            _cli_attr("deploy_to_vm")(
                vm,
                staging,
                os_family=os_family,
                userspace_only=userspace_only,
                ram_osts=getattr(args, "ram_osts", 0) or 0,
                ram_ost_size_gb=getattr(args, "ram_ost_size", 32),
                ram_mdt=getattr(args, "ram_mdt", False),
                zfs_staging=zfs_staging,
                fstype=fstype,
                ost_count=ost_count,
            )
        except RuntimeError as e:
            return _error(str(e), use_json)

    # Record successful deploy.  Swallow VMNotFound: the deploy itself
    # already succeeded, so a concurrent `ltvm destroy` racing with the
    # final .info write shouldn't turn the whole command into a
    # traceback.  Round 17 made _update_fields raise instead of silently
    # no-op'ing, so we now explicitly handle the race here -- cmd_deploy
    # is dispatched directly (not through _vm_call), so without this
    # catch the exception leaks as a Python traceback to the user.
    import time as _time

    # Record the kver we actually just deployed, not vm.kver (which is
    # the *running* kernel at the time the VM booted).  After a deploy
    # the on-disk /boot kernel may differ from the running one -- the
    # VM needs a reboot to actually pick up the new kernel, but the
    # recorded kver should reflect what's installed, not what's
    # currently running.  Source of truth: .ltvm-staging-meta.json under
    # the staging dir we just deployed from.
    kver = (
        staged_meta.get("kernel_version")
        if isinstance(staged_meta, dict)
        else None
    ) or vm.kver
    try:
        vm.update_deploy(int(_time.time()), str(build_path), kver)
    except PermissionError:
        # sockets/ is root-owned and sudo would need a password.  The
        # modules are already on the VM; only LAST_DEPLOY/BUILD_PATH/
        # KVER in the .info go unrecorded.  deploy-lustre must never
        # prompt -- an unattended deploy+test loop would hang on it.
        if not use_json:
            print(
                "  Warning: deploy metadata not recorded (sockets dir "
                "is root-owned and sudo needs a password); the deploy "
                "itself succeeded.  Run `sudo -v` first to have it "
                "recorded.",
                file=sys.stderr,
            )
    except VMNotFound:
        if not use_json:
            print(
                f"  Warning: VM '{args.vm}' was destroyed mid-deploy; "
                f"metadata not recorded",
                file=sys.stderr,
            )

    if not use_json:
        print(f"  Deployed Lustre to {args.vm}")
        if not args.mount and _cli_attr("targets_unformatted")(vm):
            print(
                f"  Its targets are not formatted yet: `ltvm llmount "
                f"{args.vm}` (or llmount.sh in the VM) formats and mounts "
                f"them.  A suite run before that needs REFORMAT=yes "
                f'(auster -r), or fails with "has not been formatted '
                f'with mkfs.lustre".'
            )

    # Optionally mount Lustre
    mounted = False
    if args.mount:
        # _cli_attr resolves at call time (so tests can patch), which
        # means it is typed Any; the callee returns an exit code.
        rc = int(
            _cli_attr("lustre_mount_vm")(
                args.vm,
                os_family,
                quiet=use_json,
                timeout=getattr(args, "mount_timeout", None),
                zfs=fstype == "zfs",
                ost_count=ost_count,
            )
        )
        if rc != EXIT_OK:
            return rc
        mounted = True
        if not use_json:
            print(f"  Lustre mounted on {args.vm}")

    # The success envelope.  Every print in this function is
    # `if not use_json`-guarded and there was no final _output, so
    # `deploy-lustre --json` wrote nothing at all to stdout on success
    # -- an empty document for the consumers --json exists for.
    if use_json:
        _output(
            {
                "action": "deploy-lustre",
                "vm": args.vm,
                "target": target,
                "kernel": deploy_kernel,
                "kernel_version": kver,
                "build_path": str(build_path),
                "staging": str(staging),
                "os_family": os_family,
                "zfs": want_zfs,
                "ost_count": ost_count,
                "mounted": mounted,
            },
            use_json,
        )

    return EXIT_OK


def cmd_llmount(args: argparse.Namespace) -> int:
    from ltvm_pkg.vm_commands import cmd_llmount as _qllmount

    try:
        _qllmount(args)
        return EXIT_OK
    except SystemExit as e:
        return int(e.code) if e.code is not None else EXIT_ERROR
