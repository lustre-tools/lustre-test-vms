"""Deploy / llmount subcommands.

cmd_deploy is the big one: auto-detects target from VM metadata,
picks up a bundled snapshot from ``ltvm fetch`` when present, else
runs ``ltvm build lustre`` into per-kernel staging, then calls
``deploy_to_vm`` to rsync modules and userland into the VM.

cmd_llmount is a thin wrapper around vm_commands.cmd_llmount.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ltvm_pkg.lustre_build import read_staging_meta

from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_OK,
    _error,
)


def _cli_attr(name: str) -> Any:
    """Look up ``name`` on ``ltvm_pkg.cli`` at call time."""
    import ltvm_pkg.cli as _cli

    return getattr(_cli, name)


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

    # Resolve build path:
    #   1. Explicit --build PATH wins (including --build .)
    #   2. Otherwise, if a bundled snapshot from `ltvm fetch` exists,
    #      copy it into staging and use it directly (no source rebuild)
    #   3. Otherwise, fall back to cwd
    build_arg = getattr(args, "build", None)
    bundled_snapshot: Path | None = None
    if build_arg is not None:
        build_path = Path(build_arg).resolve()
    else:
        # Use tc.output_dir (arch-qualified) instead of a hand-built
        # ltvm_root/artifacts/<target>/ path so the bundled-snapshot lookup
        # honors LTVM_ROOT and the /usr/local/bin/ltvm symlink resolution
        # AND finds the correct arch-qualified subdirectory.
        packaged = (
            tc.output_dir / "kernels" / resolved_kernel / "lustre-artifacts"
        )
        # A bundled snapshot is identified by the .ltvm-snapshot.json marker
        # written by snapshot_lustre.  It already has DESTDIR layout
        # (usr/, lib/modules/), so we can deploy it directly without
        # going through build-lustre.
        if packaged.is_dir() and (packaged / ".ltvm-snapshot.json").exists():
            bundled_snapshot = packaged
            build_path = packaged
            if not use_json:
                print("  Using bundled Lustre (from ltvm fetch)")
        else:
            build_path = Path(".").resolve()

    if not build_path.is_dir():
        return _error(f"Build path not found: {build_path}", use_json)

    # Validate that --build points at an actual Lustre source tree
    # before we try to feed it to `ltvm build lustre`.  Skip this when
    # we picked up a bundled snapshot, which is a DESTDIR layout (usr/,
    # lib/modules/), not a source tree.  Without this validation a typo
    # like `--build /wrong/dir` produces a confusing error several
    # subprocess hops away inside the build container.
    if bundled_snapshot is None:
        missing = [
            n
            for n in ("configure.ac", "lustre", "lnet")
            if not (build_path / n).exists()
        ]
        if missing:
            return _error(
                f"--build:'{build_path}' does not look like a Lustre "
                f"source tree (missing: {', '.join(missing)})",
                use_json,
            )

    userspace_only = getattr(args, "userspace_only", False)

    # Staging now lives inside the lustre tree at
    # <build_path>/.ltvm-staging/<target>/<arch>/<kernel>/, per-kernel
    # so two kernels' userland (usr/sbin, etc.) coexist without
    # clobbering each other.  The kernel key comes from the VM's
    # actual kernel (falling back to the target's default) so a VM
    # created with a non-default kernel deploys the Lustre that was
    # built against that kernel.
    from ltvm_pkg.lustre_build import staging_path as _staging_path

    deploy_kernel = resolved_kernel
    if vm.kernel:
        vm_kernel_name = Path(vm.kernel).parent.name
        if vm_kernel_name:
            deploy_kernel = tc.resolve_kernel(vm_kernel_name)
    staging = _staging_path(
        build_path, target, arch=vm_arch, kernel=deploy_kernel,
        variant=vm.variant,
    )
    # If the bundled-snapshot path is involved we DON'T require a
    # pre-existing per-kernel staging -- the snapshot rsync below
    # populates it.  Otherwise, if the user is deploying against a
    # source tree without having built Lustre for this kernel, refuse
    # with a clear hint rather than falling through to an automatic
    # `ltvm build lustre` that might target the wrong kernel.
    # If we picked up a bundled snapshot, mirror it into staging
    # unconditionally.  Previously we skipped the mirror whenever
    # staging already contained .ko files, but that silently shipped
    # stale modules from an earlier `ltvm build lustre` run under the
    # "Using bundled Lustre" banner -- the user thought they were
    # deploying what they fetched but actually got what was last built
    # locally.  rsync --delete is the right tool here: the bundled
    # snapshot is the declared source of truth when bundled_snapshot
    # is not None.
    if bundled_snapshot is not None:
        if not use_json:
            print(f"  Mirroring bundled snapshot into staging: {staging}")
        staging.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [
                "rsync",
                "-a",
                "--delete",
                str(bundled_snapshot) + "/",
                str(staging) + "/",
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            return _error(
                f"Failed to mirror bundled snapshot: {r.stderr.strip()}",
                use_json,
            )

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
        # Staging is outside the source tree so the find exclusions are
        # simpler -- just skip build artifacts and VCS dirs.
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
                "-print",
                "-quit",
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            return False  # treat find errors conservatively as stale
        return r.stdout.strip() == ""

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
        staging_fresh = _staging_is_fresh(staging, build_path)

        if staging_fresh:
            if not use_json:
                print("  Staging up to date, skipping build")
        else:
            _cli_attr("_gate_lustre_validation")(
                tc,
                build_path,
                force=args.force_compat,
                kernel_build_tree=tc.kernel_output_dir(kernel=deploy_kernel) / "build-tree",
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
            sudo_user = os.environ.get("SUDO_USER")
            if sudo_user:
                build_cmd = ["sudo", "-u", sudo_user] + build_cmd
            build_proc = subprocess.run(build_cmd, capture_output=False)
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

    try:
        _cli_attr("deploy_to_vm")(
            vm,
            staging,
            os_family=os_family,
            userspace_only=userspace_only,
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
    staging_meta = read_staging_meta(staging)
    kver = (
        staging_meta.get("kernel_version")
        if isinstance(staging_meta, dict)
        else None
    ) or vm.kver
    try:
        vm.update_deploy(int(_time.time()), str(build_path), kver)
    except PermissionError:
        # Non-root deploy can't take the lock file in a root-owned
        # sockets/ -- the actual module copy already happened, only
        # the "last deployed at" timestamp fails to persist.
        if not use_json:
            print(
                "  Warning: couldn't update deploy timestamp "
                "(missing write perm on sockets dir); "
                "run `sudo ltvm doctor --fix` or rerun as root",
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

    # Optionally mount Lustre
    if args.mount:
        rc = _cli_attr("lustre_mount_vm")(args.vm, os_family)
        if rc != EXIT_OK:
            return rc
        if not use_json:
            print(f"  Lustre mounted on {args.vm}")

    return EXIT_OK


def cmd_llmount(args: argparse.Namespace) -> int:
    from ltvm_pkg.vm_commands import cmd_llmount as _qllmount

    try:
        _qllmount(args)
        return EXIT_OK
    except SystemExit as e:
        return int(e.code) if e.code is not None else EXIT_ERROR
