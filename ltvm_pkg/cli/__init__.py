"""Command implementations for ltvm CLI.

Each cmd_* function takes an argparse.Namespace and returns an int
exit code.  Implementation now lives in per-concern submodules
(util, build, targets, fetch, deploy, cluster, vm, setup); this
package's __init__ re-exports every public name those submodules
expose so ``from ltvm_pkg.cli import cmd_build_all`` keeps working
and attribute-patching tests (``patch.object(cli_mod, "X")``) still
find every name they expect.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from ltvm_pkg import host_setup
from ltvm_pkg.deploy import deploy_to_vm, lustre_mount_vm
from ltvm_pkg.image_build import build_image, image_status
from ltvm_pkg.kernel_build import build_kernel, kernel_status
from ltvm_pkg.lustre_build import (
    build_lustre,
    read_staging_meta,
    staging_path,
)
from ltvm_pkg.lustre_compat import ValidationResult, validate_target
from ltvm_pkg.paths import load_meta_safe
from ltvm_pkg.release_package import (
    fetch_target,
    package_target,
    snapshot_lustre,
)
from ltvm_pkg.target_config import LustreMode, TargetConfig, list_targets

from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    _artifact_label,
    _container_status,
    _emit_error,
    _error,
    _load_target,
    _load_target_args,
    _maybe_print_traceback,
    _output,
    _qemu_ns,
    _require_root,
)
from ltvm_pkg.cli.build import (
    _do_build_container,
    _gate_lustre_validation,
    _resolve_lustre_tree,
    cmd_build_all,
    cmd_build_container,
    cmd_build_image,
    cmd_build_kernel,
    cmd_build_lustre,
    cmd_build_mofed_kmods,
    cmd_build_shell,
    cmd_clean,
    cmd_status,
)

# GitHub repo for release downloads.  Override with LTVM_GITHUB_REPO
# so a fork can use `ltvm fetch` / `ltvm publish` without editing
# source.
GITHUB_REPO = os.environ.get("LTVM_GITHUB_REPO", "lustre-tools/lustre-test-vms")

# Imported AFTER GITHUB_REPO so fetch.py's module-level _gh_api reads
# the live value (tests flip it via monkeypatch).  fetch uses the
# _cli_attr indirection at call time, so order here is mostly a
# style issue -- but keep it consistent with the "constants first,
# submodule re-exports second" pattern.
from ltvm_pkg.cli.fetch import (  # noqa: E402
    _KVER_PREFIX_RE,
    _RHEL_RE,
    _find_release_url,
    _gh_api,
    _gh_next_link,
    _gh_release_upload,
    _kernel_release_signature,
    _list_releases,
    _release_matches_kernel,
    cmd_fetch,
    cmd_package,
    cmd_publish,
)
from ltvm_pkg.cli.targets import (  # noqa: E402
    _VALIDATE_EXIT,
    _release_status,
    _validation_result_to_dict,
    _variant_suffix_in_tag,
    cmd_target_export,
    cmd_target_show,
    cmd_targets,
    cmd_validate,
)
from ltvm_pkg.cli.vm import (  # noqa: E402
    _vm_call,
    cmd_console_log,
    cmd_crash_collect,
    cmd_list,
    cmd_nmi,
    cmd_restore,
    cmd_snapshot,
    cmd_vm_start,
    cmd_vm_stop,
)


def cmd_create(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_create as _create

    return _vm_call(_create, args, use_json)


def cmd_destroy(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_destroy as _destroy

    return _vm_call(_destroy, args, use_json)


def cmd_doctor(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_doctor as _doctor

    return _vm_call(_doctor, args, use_json)


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
        # ltvm_root/output/<target>/ path so the bundled-snapshot lookup
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
            _gate_lustre_validation(
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
        deploy_to_vm(
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
        rc = lustre_mount_vm(args.vm, os_family)
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


def cmd_cluster(args: argparse.Namespace) -> int:
    use_json = args.json
    action = args.action
    cargs = args.cluster_args

    from ltvm_pkg.vm_cluster import (
        cmd_cluster_create as _qc_create,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_deploy as _qc_deploy,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_destroy as _qc_destroy,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_exec as _qc_exec,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_list as _qc_list,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_ssh as _qc_ssh,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_status as _qc_status,
    )

    def _call(fn: Any, ns: argparse.Namespace) -> int:
        try:
            fn(ns)
            return EXIT_OK
        except SystemExit as e:
            return int(e.code) if e.code is not None else EXIT_ERROR

    if action == "create":
        err = _require_root(use_json)
        if err is not None:
            return err
        if len(cargs) < 2:
            return _error(
                "cluster create requires a name and at least one node spec",
                use_json,
                hint="ltvm cluster create <name> [--target TARGET] "
                "[--arch ARCH] [--vcpus N] [--mem MB] "
                "<role:vm[:disks]> ...",
            )
        # Parse optional flags out of cargs; remaining positionals are
        # name + node specs.
        vcpus = 2
        # mem=None means "let cmd_create resolve from os_arts.default_mem"
        # so cluster nodes inherit the per-target default (e.g. rocky10
        # needs 4096) instead of being silently overridden.
        mem: int | None = None
        os_target: str | None = None
        arch: str | None = None
        disk_size: str | None = None
        nics: list[str] = []
        positional: list[str] = []
        i = 0
        while i < len(cargs):
            if cargs[i] == "--vcpus" and i + 1 < len(cargs):
                vcpus = int(cargs[i + 1])
                i += 2
            elif cargs[i] == "--mem" and i + 1 < len(cargs):
                mem = int(cargs[i + 1])
                i += 2
            elif cargs[i] == "--target" and i + 1 < len(cargs):
                os_target = cargs[i + 1]
                i += 2
            elif cargs[i] == "--arch" and i + 1 < len(cargs):
                arch = cargs[i + 1]
                i += 2
            elif cargs[i] == "--disk-size" and i + 1 < len(cargs):
                disk_size = cargs[i + 1]
                i += 2
            elif cargs[i] == "--nic" and i + 1 < len(cargs):
                # --nic is repeatable and applies uniformly to every
                # node in the cluster.  Validation happens inside each
                # node's `ltvm create`, so a bad value (e.g. softroce)
                # surfaces per-node with the usual follow-up-issue hint.
                nics.append(cargs[i + 1])
                i += 2
            elif cargs[i].startswith("--"):
                return _error(
                    f"cluster create: unknown argument '{cargs[i]}'",
                    use_json,
                    hint="valid: --vcpus, --mem, --target, --arch, "
                    "--disk-size, --nic",
                )
            else:
                positional.append(cargs[i])
                i += 1
        if len(positional) < 2:
            return _error(
                "cluster create requires a name and at least one node spec",
                use_json,
                hint="ltvm cluster create <name> [TARGET | --target TARGET] "
                "[--arch ARCH] [--vcpus N] [--mem MB] "
                "<role:vm[:disks]> ...",
            )
        # Accept a positional target after the cluster name: any
        # bare token (no ':') between the name and the first node
        # spec is treated as the OS target.  Node specs always
        # contain ':' (role:vm[:disks]) so this is unambiguous.  If
        # both the positional and --target are given, they must agree.
        pos_target: str | None = None
        if len(positional) >= 2 and ":" not in positional[1]:
            pos_target = positional[1]
            positional = [positional[0]] + positional[2:]
            if len(positional) < 2:
                return _error(
                    "cluster create requires at least one node spec",
                    use_json,
                    hint="ltvm cluster create <name> "
                    "[TARGET | --target TARGET] <role:vm[:disks]> ...",
                )
        if pos_target is not None and os_target is not None \
                and pos_target != os_target:
            return _error(
                f"--target {os_target!r} conflicts with positional "
                f"target {pos_target!r}; pass only one",
                use_json,
            )
        final_target = pos_target if pos_target is not None else os_target
        return _call(
            _qc_create,
            _qemu_ns(
                name=positional[0],
                nodes=positional[1:],
                vcpus=vcpus,
                mem=mem,
                os=final_target,
                arch=arch,
                disk_size=disk_size,
                nic=nics,
            ),
        )

    if action == "destroy":
        err = _require_root(use_json)
        if err is not None:
            return err
        if not cargs:
            return _error("cluster destroy requires a name", use_json)
        return _call(_qc_destroy, _qemu_ns(name=cargs[0]))

    if action == "deploy":
        if not cargs:
            return _error("cluster deploy requires a name", use_json)
        name = cargs[0]
        build_path = "."
        mount = False
        server_only = False
        force_compat = False
        i = 1
        while i < len(cargs):
            if cargs[i] == "--build" and i + 1 < len(cargs):
                build_path = cargs[i + 1]
                i += 2
            elif cargs[i] == "--mount":
                mount = True
                i += 1
            elif cargs[i] == "--server-only":
                server_only = True
                i += 1
            elif cargs[i] == "--force-compat":
                force_compat = True
                i += 1
            else:
                return _error(
                    f"cluster deploy: unknown argument '{cargs[i]}'",
                    use_json,
                    hint="valid: --build PATH, --mount, --server-only, "
                    "--force-compat",
                )
        return _call(
            _qc_deploy,
            _qemu_ns(
                name=name,
                lustre_source=build_path,
                mount=mount,
                server_only=server_only,
                force_compat=force_compat,
            ),
        )

    if action == "status":
        if not cargs:
            return _error("cluster status requires a name", use_json)
        return _call(_qc_status, _qemu_ns(name=cargs[0]))

    if action == "exec":
        if len(cargs) < 3:
            return _error(
                "cluster exec requires a name, role, and command",
                use_json,
                hint="ltvm cluster exec <name> <role> '<cmd>'",
            )
        return _call(
            _qc_exec,
            _qemu_ns(
                name=cargs[0],
                target=cargs[1],
                command=cargs[2:],
                timeout=120,
                json=use_json,
            ),
        )

    if action == "list":
        return _call(_qc_list, _qemu_ns())

    if action == "ssh":
        if len(cargs) < 2:
            return _error(
                "cluster ssh requires a name and a target (role or vm name)",
                use_json,
                hint="ltvm cluster ssh <name> <role> [cmd...]",
            )
        return _call(
            _qc_ssh,
            _qemu_ns(name=cargs[0], target=cargs[1], command=cargs[2:]),
        )

    return _error(f"Unknown cluster action: {action}", use_json)


# ------------------------------------------------------------------
# Subcommand: setup
# ------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    """Run host setup (QEMU, network, scripts, SSH)."""
    use_json = args.json

    # Collect requested steps
    explicit = []
    if args.qemu:
        explicit.append("qemu")
    if args.network:
        explicit.append("network")
    if args.install:
        explicit.append("install")
    if args.ssh:
        explicit.append("ssh")
    steps = explicit or None  # None = all

    if args.verify:
        try:
            results = host_setup.verify(subnet=args.subnet)
        except Exception as e:
            return _error(str(e), use_json)
        if use_json:
            print(json.dumps(results, indent=2))
        else:
            host_setup.print_verify(results)
        return EXIT_OK if results["all_ok"] else EXIT_ERROR

    try:
        host_setup.run_setup(
            steps=steps,
            subnet=args.subnet,
            force=getattr(args, "force", False),
        )
    except RuntimeError as e:
        return _error(str(e), use_json)
    except Exception as e:
        return _error(f"Setup failed: {e}", use_json)

    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: update
# ------------------------------------------------------------------


def _ltvm_repo_root() -> Path:
    """Return the on-disk repo root for this ltvm checkout.

    `ltvm install` symlinks the entry-point script into ``/usr/local/bin``
    and then resolves that symlink at startup, so when the user runs
    ``ltvm update`` from an installed copy we still load ``ltvm_pkg``
    from the real checkout.  ``Path(__file__).resolve()`` follows any
    intermediate symlink and lands us in the real ``<repo>/ltvm_pkg/``,
    so the parent of the parent is the real repo root regardless of how
    ltvm was invoked.
    """
    # cli.py lives at <repo>/ltvm_pkg/cli.py
    return Path(__file__).resolve().parent.parent


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess:
    """Run a git command against ``repo`` and return the CompletedProcess."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=60,
    )


def _current_version() -> str:
    """Return the version string, recomputing fresh from disk.

    ``ltvm_pkg.__version__`` is captured at import time, so after a
    successful update we recompute via ``_compute_version`` to pick up
    the new git hash without forcing a reload.
    """
    from ltvm_pkg import _compute_version

    return _compute_version()


def cmd_update(args: argparse.Namespace) -> int:
    """Pull the latest ltvm from the upstream git remote.

    Refuses to act on a dirty working tree unless --force is given.
    Uses --ff-only so we never silently create a merge commit on the
    user's checkout.  Reports the old and new version on success.
    """
    use_json = args.json
    # git pull writes into the checkout (.git/FETCH_HEAD, refs, ...).
    # In shared-install deployments the ltvm repo is owned by one user
    # (e.g. admin) and everyone else runs ltvm through PATH, so letting
    # the unprivileged caller hit this leaks a git permission error
    # mid-command.  Require root up front so sudo is the obvious fix.
    err = _require_root(use_json)
    if err is not None:
        return err
    repo = _ltvm_repo_root()

    if not (repo / ".git").exists():
        return _error(
            f"{repo} is not a git checkout -- cannot update",
            use_json,
            hint="Reinstall ltvm by cloning "
            "https://github.com/lustre-tools/lustre-test-vms",
        )

    old_version = _current_version()

    # --check: just report whether an update is available
    if getattr(args, "check", False):
        try:
            _git(repo, "fetch", "--quiet")
        except subprocess.CalledProcessError as e:
            return _error(
                f"git fetch failed: {e.stderr.strip() or e}", use_json
            )
        try:
            behind = _git(
                repo, "rev-list", "--count", "HEAD..@{u}"
            ).stdout.strip()
        except subprocess.CalledProcessError as e:
            return _error(
                f"git rev-list failed: {e.stderr.strip() or e}",
                use_json,
                hint="Is the current branch tracking an upstream?",
            )
        n = int(behind or "0")
        result = {
            "version": old_version,
            "behind": n,
            "update_available": n > 0,
        }
        _output(result, use_json)
        return EXIT_OK

    # Refuse on dirty working tree unless forced
    if not getattr(args, "force", False):
        status = _git(repo, "status", "--porcelain").stdout
        if status.strip():
            return _error(
                "working tree has local changes -- refusing to update",
                use_json,
                hint="Commit or stash your changes, or pass --force",
            )

    try:
        _git(repo, "fetch", "--quiet")
    except subprocess.CalledProcessError as e:
        return _error(f"git fetch failed: {e.stderr.strip() or e}", use_json)

    try:
        pull = _git(repo, "pull", "--ff-only")
    except subprocess.CalledProcessError as e:
        return _error(
            f"git pull --ff-only failed: {e.stderr.strip() or e}",
            use_json,
            hint="The local branch has diverged from upstream. "
            "Resolve manually with git.",
        )

    # Refresh _build_info.py so the new short hash takes effect
    # immediately, even if the post-commit hook isn't installed.
    try:
        new_hash = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
        if new_hash:
            (repo / "ltvm_pkg" / "_build_info.py").write_text(
                '"""Auto-generated by ltvm update. Do not edit or commit."""\n\n'
                f'BUILD_HASH = "{new_hash}"\n'
            )
    except (subprocess.CalledProcessError, OSError):
        # Non-fatal: version reporting will fall back to the runtime
        # git rev-parse path.
        pass

    new_version = _current_version()

    result = {
        "old_version": old_version,
        "new_version": new_version,
        "changed": old_version != new_version,
        "git": pull.stdout.strip(),
    }
    if not use_json:
        if old_version == new_version:
            print(f"Already up to date at {new_version}")
        else:
            print(f"Updated ltvm: {old_version} -> {new_version}")
        if pull.stdout.strip():
            print(pull.stdout.strip())
    else:
        print(json.dumps(result, indent=2))
    return EXIT_OK
