"""`ltvm make-install` / `make-uninstall` / `make-reinstall`.

These run on a machine ltvm produced -- an ltvm VM, or a cloud node
booted from `ltvm target export --format gce` -- and install a Lustre
checkout onto *that machine's own* root filesystem.  Contrast with
`ltvm deploy-lustre`, which runs on a build host and pushes a build
into a VM over ssh.

The build itself still happens in the target's build container: the
VM image carries the runtime packages but not the toolchain.  So this
is `make install DESTDIR=<staging>` in the container, followed by
unpacking that DESTDIR onto `/`.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ltvm_pkg.cli.util import (
    EXIT_OK,
    _error,
    _output,
)
from ltvm_pkg.local_install import (
    MANIFEST_PATH,
    LocalImage,
    LocalInstallError,
    check_in_lustre_tree,
    check_is_ltvm_machine,
    check_kernel_match,
    install_staging_into_root,
    loaded_lustre_modules,
    prune_empty_dirs,
    read_manifest,
    remove_installed_files,
    resolve_local_image,
    run_depmod_ldconfig,
    staging_contents,
    unload_lustre_modules,
    write_manifest,
)


def _cli_attr(name: str) -> Any:
    """Look up ``name`` on ``ltvm_pkg.cli`` at call time."""
    import ltvm_pkg.cli as _cli

    return getattr(_cli, name)


def _guard(
    args: argparse.Namespace, use_json: bool
) -> tuple[tuple[str, Path] | None, int | None]:
    """Both preconditions, for every make-* command.

    They only make sense on a machine ltvm built (they write to /) and
    from inside a Lustre checkout (install builds what the tree holds;
    uninstall is the other half of that pair).  Returns
    ((evidence, tree), None) or (None, exit_code).
    """
    try:
        evidence = check_is_ltvm_machine(force=getattr(args, "force", False))
        tree = check_in_lustre_tree(getattr(args, "lustre_tree", None))
    except LocalInstallError as e:
        return None, _error(str(e), use_json)
    return (evidence, tree), None


def _resolve_machine(
    args: argparse.Namespace, use_json: bool
) -> tuple[LocalImage | None, int | None]:
    """Identify which target this machine corresponds to."""
    try:
        image = resolve_local_image(
            explicit_target=getattr(args, "target", None),
            explicit_kernel=getattr(args, "kernel", None),
            explicit_variant=getattr(args, "variant", None),
        )
    except LocalInstallError as e:
        return None, _error(str(e), use_json)
    return image, None


def _build_lustre_locally(
    args: argparse.Namespace, image: LocalImage, lustre_tree: Path,
    use_json: bool,
) -> tuple[dict | None, int | None]:
    """`make install DESTDIR=<staging>` inside the build container.

    Mirrors cmd_build_lustre's preflight/gate sequence rather than
    shelling out to `ltvm build lustre`, so a failure surfaces here
    instead of as an opaque non-zero from a re-exec.
    """
    from ltvm_pkg.cli.build import (
        _podman_machine_autostop,
        _preflight_container,
        _preflight_podman,
    )
    from ltvm_pkg.target_config import LustreMode, TargetConfig

    try:
        tc = TargetConfig(image.target, arch=image.arch, variant=image.variant)
    except (ValueError, KeyError) as e:
        return None, _error(f"Unknown target {image.target!r}: {e}", use_json)

    build_tree = tc.kernel_output_dir(kernel=image.kernel) / "build-tree"
    if not build_tree.is_dir():
        return None, _error(
            f"Kernel build-tree not found: {build_tree}",
            use_json,
            hint=f"This machine has no local ltvm artifacts yet.  Run: "
            f"ltvm target fetch {image.target}",
        )

    err = _preflight_podman(use_json)
    if err is not None:
        return None, err
    err = _preflight_container(tc, use_json)
    if err is not None:
        return None, err

    with _podman_machine_autostop() as autostop:
        _cli_attr("_gate_lustre_validation")(
            tc,
            lustre_tree,
            force=getattr(args, "force_compat", False),
            kernel_build_tree=build_tree,
            kernel=image.kernel,
        )

        enable_server = tc.lustre_mode != LustreMode.CLIENT
        if not use_json:
            print(f"  Building Lustre for {image.target} "
                  f"(kernel {image.kernel}) in the build container...")
        try:
            meta = _cli_attr("build_lustre")(
                lustre_tree,
                build_tree,
                container_tag=tc.container_tag,
                target=image.target,
                enable_server=enable_server,
                extra_configure=list(tc.configure_args),
                jobs=getattr(args, "jobs", None),
                force=getattr(args, "rebuild", False),
                arch=tc.arch,
                kernel=image.kernel,
                variant=tc.variant_name,
            )
        except Exception as e:
            return None, _error(f"Lustre build failed: {e}", use_json)
        autostop.success = True

    meta["lustre_tree"] = str(lustre_tree)
    return meta, None


def _do_install(args: argparse.Namespace, use_json: bool) -> int:
    guard, err = _guard(args, use_json)
    if err is not None:
        return err
    assert guard is not None
    evidence, lustre_tree = guard

    image, err = _resolve_machine(args, use_json)
    if err is not None:
        return err
    assert image is not None

    if not use_json:
        print(f"  ltvm machine: {evidence}")
        print(f"  Lustre tree:  {lustre_tree}")
        print(f"  Machine: {image.target} ({image.arch}, variant "
              f"{image.variant}, kernel {image.kernel}) "
              f"[detected via {image.source}]")

    meta, err = _build_lustre_locally(args, image, lustre_tree, use_json)
    if err is not None:
        return err
    assert meta is not None

    staging = Path(meta["staging"])
    kver = str(meta.get("kernel_version") or "")

    warning = check_kernel_match(image, kver)
    if warning and not use_json:
        print(f"  WARNING: {warning}")

    try:
        files, dirs = staging_contents(staging)
        install_staging_into_root(staging)
        run_depmod_ldconfig(kver or None)
        write_manifest(
            image, staging, Path(meta["lustre_tree"]), kver, files, dirs
        )
    except LocalInstallError as e:
        return _error(str(e), use_json)

    payload = {
        "action": "make-install",
        "target": image.target,
        "arch": image.arch,
        "variant": image.variant,
        "kernel": image.kernel,
        "kernel_version": kver,
        "lustre_tree": meta["lustre_tree"],
        "staging": str(staging),
        "files_installed": len(files),
        "manifest": str(MANIFEST_PATH),
        "warning": warning,
    }
    _output(payload, use_json)
    if not use_json:
        print(f"  Installed {len(files)} files into /  "
              f"(manifest: {MANIFEST_PATH})")
    return EXIT_OK


def _do_uninstall(
    args: argparse.Namespace, use_json: bool, missing_ok: bool = False
) -> int:
    # Guards only: the manifest already records everything uninstall
    # needs, so a target that has since changed in targets.yaml must
    # not block removing files we put on the disk.
    _, err = _guard(args, use_json)
    if err is not None:
        return err

    try:
        manifest = read_manifest()
    except LocalInstallError as e:
        return _error(str(e), use_json)

    if manifest is None:
        msg = (
            f"Nothing to uninstall: no install manifest at {MANIFEST_PATH}.  "
            f"make-uninstall only removes what `ltvm make-install` put here."
        )
        if missing_ok:
            if not use_json:
                print(f"  {msg}")
            return EXIT_OK
        return _error(msg, use_json)

    unloaded = True
    unload_msg = "skipped (--no-unload)"
    if not getattr(args, "no_unload", False):
        unloaded, unload_msg = unload_lustre_modules()
        if not use_json:
            print(f"  Modules: {unload_msg}")
        if not unloaded and not getattr(args, "force", False):
            return _error(
                f"Refusing to remove module files while Lustre is loaded: "
                f"{unload_msg}",
                use_json,
                hint="Unmount Lustre first, or pass --force / --no-unload",
            )

    files = list(manifest.get("files", []))
    dirs = list(manifest.get("dirs", []))
    removed = remove_installed_files(files)
    pruned = prune_empty_dirs(dirs)
    run_depmod_ldconfig(manifest.get("kernel_version") or None)

    from ltvm_pkg.priv import sudo_run

    sudo_run(["rm", "-f", str(MANIFEST_PATH)], check=False, quiet=True)

    payload = {
        "action": "make-uninstall",
        "files_removed": removed,
        "dirs_pruned": pruned,
        "modules_unloaded": unloaded,
        "modules_message": unload_msg,
        "still_loaded": loaded_lustre_modules(),
    }
    _output(payload, use_json)
    if not use_json:
        print(f"  Removed {removed} files, pruned {pruned} directories")
    return EXIT_OK


def cmd_make_install(args: argparse.Namespace) -> int:
    return _do_install(args, args.json)


def cmd_make_uninstall(args: argparse.Namespace) -> int:
    return _do_uninstall(args, args.json)


def cmd_make_reinstall(args: argparse.Namespace) -> int:
    """Uninstall then install.

    The uninstall half tolerates "nothing installed" -- reinstall is
    what people reach for after changing the source, and refusing
    because a previous install wasn't ltvm's would just be in the way.
    """
    use_json = args.json
    rc = _do_uninstall(args, use_json, missing_ok=True)
    if rc != EXIT_OK:
        return rc
    return _do_install(args, use_json)
