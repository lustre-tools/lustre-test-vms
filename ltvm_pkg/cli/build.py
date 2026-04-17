"""Build subcommands: container / kernel / image / lustre / mofed-kmods,
plus `build shell`, `build status`, and `clean`.

Helpers shared across submodules (_resolve_lustre_tree,
_gate_lustre_validation, _do_build_container) live here; other
submodules reach them via ``ltvm_pkg.cli.<name>`` so that tests
patching those attributes on ``ltvm_pkg.cli`` affect every caller
(including cmd_* in this file), matching pre-split behavior.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_OK,
    _artifact_label,
    _container_status,
    _error,
    _load_target,
    _load_target_args,
    _output,
)
from ltvm_pkg.host_setup import (
    PodmanMachineError,
    check_podman_machine_macos,
    is_macos,
    should_stop_podman_machine_macos,
    stop_podman_machine_macos,
)
from ltvm_pkg.image_build import image_status
from ltvm_pkg.kernel_build import kernel_status
from ltvm_pkg.lustre_build import staging_path
from ltvm_pkg.target_config import LustreMode, TargetConfig


def _preflight_podman(use_json: bool) -> int | None:
    """Check podman is usable; return an error code if not, else None."""
    try:
        check_podman_machine_macos()
    except PodmanMachineError as e:
        return _error(str(e), use_json)
    return None


def _preflight_container(tc: TargetConfig, use_json: bool) -> int | None:
    """Return an error code if the build container tag is missing, else None.

    Keeps downstream commands from burning time on SRPM downloads / Lustre
    tree parsing before discovering that podman will fail at `run`.
    """
    tag = tc.container_tag
    try:
        r = subprocess.run(
            ["podman", "image", "exists", tag], capture_output=True
        )
    except FileNotFoundError:
        return _error(
            "podman not found",
            use_json,
            hint="install podman or run `ltvm install` to set up the host",
        )
    if r.returncode != 0:
        return _error(
            f"build container {tag} not found",
            use_json,
            hint=f"Run: ltvm build container {tc.name}",
        )
    return None


@contextlib.contextmanager
def _podman_machine_autostop() -> Iterator[None]:
    """On macOS, stop the podman machine after a successful block if idle.

    Only stops on normal completion -- if the block raises, we leave the
    machine running so the user can retry without waiting for a cold
    start.  No-op on non-macOS; bails out quietly if the podman-ps query
    fails or any non-ltvm container is running.
    """
    if not is_macos():
        yield
        return
    yield
    try:
        if should_stop_podman_machine_macos():
            stop_podman_machine_macos()
    except Exception:
        pass


def _cli_attr(name: str) -> Any:
    """Look up ``name`` on ``ltvm_pkg.cli`` at call time.

    Lets callers (tests) monkey-patch ``ltvm_pkg.cli._do_build_container``
    etc. and have the replacement observed by cmd_* in this submodule.
    """
    import ltvm_pkg.cli as _cli

    return getattr(_cli, name)


def _resolve_lustre_tree(
    arg_value: str | None,
) -> tuple[Path | None, str | None]:
    """Resolve --lustre-tree, defaulting to cwd.

    Returns (Path, error_string).  error_string is None on success.
    """
    from ltvm_pkg.lustre_tree import kp_root

    p = Path(arg_value).resolve() if arg_value else Path.cwd()
    if not p.is_dir():
        return None, f"Not a directory: {p}"
    kp = kp_root(p)
    if not kp.is_dir():
        return None, (
            f"{p} does not look like a Lustre tree (no lustre/kernel_patches/)"
        )
    return p, None


# ------------------------------------------------------------------
# Subcommand: build-all
# ------------------------------------------------------------------


def _do_build_container(target_config: TargetConfig) -> str:
    """Run podman build for the build container and write meta.

    Delegates to kernel_build._ensure_container_image so the podman
    invocation lives in exactly one place.
    """
    from ltvm_pkg.kernel_build import _ensure_container_image

    tag = _ensure_container_image(target_config)
    # Schema: see ltvm_pkg.meta_schema.ContainerMeta.
    # target/input_hash are written by TargetConfig.write_meta.
    target_config.write_meta("container", image_tag=tag)
    return tag


def cmd_build_all(args: argparse.Namespace) -> int:
    """Build container + kernel + image for a target.

    Also builds the Lustre source tree against the freshly built
    kernel by default; pass --skip-lustre to opt out.
    """
    use_json = args.json
    tc, err = _load_target_args(args, use_json)
    if err is not None:
        return err
    assert tc is not None

    err = _preflight_podman(use_json)
    if err is not None:
        return err

    with _podman_machine_autostop():
        return _cmd_build_all_body(args, tc, use_json)


def _cmd_build_all_body(
    args: argparse.Namespace, tc: TargetConfig, use_json: bool
) -> int:

    # build-all always requires a Lustre tree -- even for deb targets
    # where the kernel build itself doesn't need one, the surrounding
    # workflow (image inject, optional Lustre build, packaging) does.
    lustre_tree, err_msg = _cli_attr("_resolve_lustre_tree")(args.lustre_tree)
    if err_msg:
        return _error(
            err_msg,
            use_json,
            hint="Run from a Lustre tree, or pass "
            "--lustre-tree /path/to/lustre-release",
        )
    assert lustre_tree is not None

    # Honor the variant's kernel pin when --kernel is omitted: without
    # this, the per-step calls below fall through to tc.default_kernel
    # inside kernel_build / image_build, silently building the wrong
    # kernel for a variant pinned to a non-default (e.g. mofed-24 pinned
    # to rhel9.5 while default is rhel9.7).  Mirrors the fix applied to
    # vm_state.resolve_os_artifacts in commit 107b73b.  resolve_kernel
    # returns the pin when --kernel is None and a variant pin exists,
    # otherwise default_kernel; build_kernel / build_image both accept
    # the full cached-dir name or the short form.
    resolved_kernel = tc.resolve_kernel(getattr(args, "kernel", None))

    _cli_attr("_gate_lustre_validation")(
        tc,
        lustre_tree,
        force=args.force_compat,
        kernel_build_tree=tc.kernel_output_dir(kernel=resolved_kernel) / "build-tree",
    )

    results: dict[str, Any] = {}

    # 1. Container
    if not use_json:
        print(f"==> Building container for {args.target}...")
    try:
        _cli_attr("_do_build_container")(tc)
        results["container"] = "ok"
    except Exception as e:
        return _error(f"Container build failed: {e}", use_json)

    # 2. Kernel
    if not use_json:
        print(f"==> Building kernel {resolved_kernel} for {args.target}...")
    try:
        kmeta = _cli_attr("build_kernel")(
            tc,
            lustre_tree,
            force=args.force,
            kernel=resolved_kernel,
        )
        results["kernel"] = kmeta
    except _cli_attr("SrpmNotFoundError") as e:
        return _error(str(e), use_json)
    except Exception as e:
        return _error(f"Kernel build failed: {e}", use_json)

    # 3. Lustre (enabled by default; --skip-lustre opts out).
    # Runs BEFORE the image so its per-kernel staging is in place for
    # the image-bake step to auto-inject.
    lustre_build = not getattr(args, "skip_lustre", False)
    if lustre_build:
        if not use_json:
            print(
                f"==> Building Lustre against {resolved_kernel} kernel tree..."
            )
        build_tree = tc.kernel_output_dir(kernel=resolved_kernel) / "build-tree"
        try:
            container_tag = tc.container_tag
            lmeta = _cli_attr("build_lustre")(
                lustre_tree,
                build_tree,
                container_tag=container_tag,
                target=args.target,
                enable_server=tc.lustre_mode != LustreMode.CLIENT,
                extra_configure=list(tc.configure_args),
                jobs=getattr(args, "jobs", None),
                force=args.force,
                arch=tc.arch,
                kernel=resolved_kernel,
                variant=tc.variant_name,
            )
            results["lustre"] = lmeta
        except Exception as e:
            return _error(f"Lustre build failed: {e}", use_json)

    # 4. Image (picks up Lustre staging from step 3 if it was built).
    if not use_json:
        print(f"==> Building image for {args.target} (kernel={resolved_kernel})...")
    try:
        _cli_attr("build_image")(
            tc,
            force=args.force,
            kernel=resolved_kernel,
            with_lustre=str(lustre_tree) if lustre_build else None,
        )
        results["image"] = "ok"
    except Exception as e:
        return _error(f"Image build failed: {e}", use_json)

    _output(results, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: build-container
# ------------------------------------------------------------------


def cmd_build_container(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target_args(args, use_json)
    if err is not None:
        return err
    assert tc is not None

    err = _preflight_podman(use_json)
    if err is not None:
        return err

    with _podman_machine_autostop():
        if not use_json:
            print(f"Building container for {args.target}...")

        try:
            tag = _cli_attr("_do_build_container")(tc)
        except Exception as e:
            return _error(f"Container build failed: {e}", use_json)

        result = {"target": args.target, "image_tag": tag}
        _output(result, use_json)
        return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: build-kernel
# ------------------------------------------------------------------


def cmd_build_kernel(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target_args(args, use_json)
    if err is not None:
        return err
    assert tc is not None

    err = _preflight_podman(use_json)
    if err is not None:
        return err

    err = _preflight_container(tc, use_json)
    if err is not None:
        return err

    with _podman_machine_autostop():
        # Deb-based targets don't need a Lustre tree for kernel builds
        lustre_tree = None
        if not tc.kernel_deb_source:
            lustre_tree, err_msg = _cli_attr("_resolve_lustre_tree")(args.lustre_tree)
            if err_msg:
                return _error(
                    err_msg,
                    use_json,
                    hint="Run from a Lustre tree, or pass "
                    "--lustre-tree /path/to/lustre-release",
                )
            assert lustre_tree is not None
            _cli_attr("_gate_lustre_validation")(
                tc, lustre_tree, force=args.force_compat
            )

        kernel = getattr(args, "kernel", None)

        if not use_json:
            k = tc.resolve_kernel(kernel)
            print(f"Building kernel {k} for {args.target}...")

        try:
            meta = _cli_attr("build_kernel")(
                tc,
                lustre_tree,
                force=args.force,
                kernel=kernel,
            )
        except _cli_attr("SrpmNotFoundError") as e:
            return _error(str(e), use_json)
        except Exception as e:
            return _error(f"Kernel build failed: {e}", use_json)

        _output(meta, use_json)
        return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: build mofed-kmods
# ------------------------------------------------------------------


def cmd_build_mofed_kmods(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target_args(args, use_json)
    if err is not None:
        return err
    assert tc is not None

    err = _preflight_podman(use_json)
    if err is not None:
        return err

    from ltvm_pkg.mofed_kmod_build import build_mofed_kmods
    from ltvm_pkg.target_config import DEFAULT_VARIANT

    if tc.variant_name == DEFAULT_VARIANT:
        return _error(
            f"target {tc.name!r} is bound to base variant -- "
            f"mofed-kmods only applies to a mofed variant",
            use_json,
            hint="Pass --variant mofed-24 (or whichever mofed-* is declared)",
        )

    err = _preflight_container(tc, use_json)
    if err is not None:
        return err

    try:
        out_dir = build_mofed_kmods(
            tc, kernel=getattr(args, "kernel", None),
            force=getattr(args, "force", False),
        )
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        return _error(f"MOFED kmod build failed: {e}", use_json)

    rpms = sorted(p.name for p in out_dir.glob("*.rpm"))
    _output(
        {
            "target": tc.name,
            "variant": tc.variant_name,
            "kernel": tc.resolve_kernel(getattr(args, "kernel", None)),
            "path": str(out_dir),
            "rpms": rpms,
        },
        use_json,
    )
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: build-image
# ------------------------------------------------------------------


def cmd_build_image(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target_args(args, use_json)
    if err is not None:
        return err
    assert tc is not None

    err = _preflight_podman(use_json)
    if err is not None:
        return err

    err = _preflight_container(tc, use_json)
    if err is not None:
        return err

    with _podman_machine_autostop():
        kernel = getattr(args, "kernel", None)
        resolved_kernel = tc.resolve_kernel(kernel)

        with_lustre: str | None = None
        if not args.no_lustre:
            # Resolve to an absolute path so a ``--lustre-tree ./rel`` passed
            # from a subdirectory lands on the same staging dir that
            # ``cmd_build_lustre`` writes to (which goes through
            # ``_resolve_lustre_tree`` -> ``.resolve()``).  Without this,
            # the two sides compute different staging keys and the image
            # build refuses to find staging the lustre build just produced.
            lustre_tree = (
                Path(args.lustre_tree).resolve() if args.lustre_tree
                else Path(os.getcwd()).resolve()
            )
            candidate = staging_path(
                lustre_tree, args.target, arch=tc.arch,
                kernel=resolved_kernel, variant=tc.variant_name,
            )
            build_tree = tc.kernel_output_dir(kernel=resolved_kernel) / "build-tree"
            if not candidate.exists():
                return _error(
                    f"Lustre not built for {args.target} kernel "
                    f"{resolved_kernel} -- no staging at {candidate}",
                    use_json,
                    hint=(
                        f"build Lustre first:\n"
                        f"  ltvm build lustre {args.target} --kernel "
                        f"{resolved_kernel} --lustre-tree <path>\n"
                        f"or disable Lustre for a kernel-only image:\n"
                        f"  ltvm build image {args.target} --kernel "
                        f"{resolved_kernel} --no-lustre"
                    ),
                )
            _cli_attr("_gate_lustre_validation")(
                tc,
                lustre_tree,
                force=args.force_compat,
                kernel_build_tree=build_tree,
            )
            with_lustre = str(lustre_tree)

        if not use_json:
            extra = f" +lustre={with_lustre}" if with_lustre else ""
            print(
                f"Building image for {args.target} "
                f"(kernel={resolved_kernel}){extra}..."
            )

        try:
            path = _cli_attr("build_image")(
                tc,
                force=args.force,
                kernel=kernel,
                with_lustre=with_lustre,
            )
        except Exception as e:
            return _error(f"Image build failed: {e}", use_json)

        result = {
            "target": args.target,
            "kernel": resolved_kernel,
            "path": str(path),
            "with_lustre": with_lustre,
        }
        _output(result, use_json)
        return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: clean
# ------------------------------------------------------------------


def _dir_size_bytes(path: Path) -> int:
    """Total size of all regular files under ``path`` (bytes)."""
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _format_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for u in units:
        if size < 1024 or u == units[-1]:
            return f"{size:.1f} {u}" if u != "B" else f"{int(size)} B"
        size /= 1024
    return f"{int(n)} B"


def cmd_clean(args: argparse.Namespace) -> int:
    """Remove built artifacts for a target.

    By default wipes output/<target>/<arch>/ for the target's default
    arch (x86_64).  --arch narrows to a specific arch; --all-arches
    wipes the whole output/<target>/ tree.
    """
    import shutil

    from ltvm_pkg.target_config import OUTPUT_DIR

    use_json = args.json
    target = args.target
    all_arches = bool(getattr(args, "all_arches", False))
    arch_flag = getattr(args, "arch", None)

    # Validate target exists (via TargetConfig).  We don't need to
    # instantiate an arch-specific TargetConfig for --all-arches, but
    # we still want to ensure target is known.
    tc, err = _load_target(target, use_json, arch=arch_flag)
    if err is not None:
        return err
    assert tc is not None

    if all_arches and arch_flag:
        return _error(
            "--arch and --all-arches are mutually exclusive", use_json
        )

    if all_arches:
        wipe_paths = [OUTPUT_DIR / target]
    else:
        # Use the arch actually configured in the TargetConfig (honors
        # --arch override; defaults to x86_64).
        wipe_paths = [OUTPUT_DIR / target / tc.arch]

    wiped: list[dict[str, Any]] = []
    for p in wipe_paths:
        if p.exists():
            size = _dir_size_bytes(p)
            try:
                shutil.rmtree(p)
            except OSError as e:
                return _error(f"Failed to remove {p}: {e}", use_json)
            wiped.append(
                {"path": str(p), "bytes": size, "removed": True}
            )
        else:
            wiped.append(
                {"path": str(p), "bytes": 0, "removed": False}
            )

    result = {
        "target": target,
        "arch": None if all_arches else tc.arch,
        "all_arches": all_arches,
        "wiped": wiped,
    }

    if use_json:
        _output(result, use_json)
    else:
        total = sum(w["bytes"] for w in wiped)
        any_removed = any(w["removed"] for w in wiped)
        for w in wiped:
            if w["removed"]:
                print(f"removed {w['path']} ({_format_bytes(w['bytes'])})")
            else:
                print(f"nothing to clean at {w['path']}")
        if any_removed:
            print(f"total freed: {_format_bytes(total)}")
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: build-lustre
# ------------------------------------------------------------------


def cmd_build_lustre(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target_args(args, use_json)
    if err is not None:
        return err
    assert tc is not None

    err = _preflight_podman(use_json)
    if err is not None:
        return err

    err = _preflight_container(tc, use_json)
    if err is not None:
        return err

    with _podman_machine_autostop():
        lustre_tree, err_msg = _cli_attr("_resolve_lustre_tree")(
            getattr(args, "lustre_tree", None)
        )
        if err_msg:
            return _error(
                err_msg,
                use_json,
                hint="Pass --lustre-tree or run from a Lustre source tree",
            )
        assert lustre_tree is not None

        kernel = getattr(args, "kernel", None)
        resolved_kernel = tc.resolve_kernel(kernel)
        build_tree = tc.kernel_output_dir(kernel=resolved_kernel) / "build-tree"

        _cli_attr("_gate_lustre_validation")(
            tc,
            lustre_tree,
            force=args.force_compat,
            kernel_build_tree=build_tree,
        )

        if not build_tree.is_dir():
            return _error(
                f"Kernel build-tree not found: {build_tree}",
                use_json,
                hint=f"Run: ltvm build kernel {args.target} "
                f"--kernel {resolved_kernel}",
            )

        # Server build follows lustre.mode unless overridden
        enable_server = tc.lustre_mode != LustreMode.CLIENT
        if getattr(args, "disable_server", False):
            enable_server = False
        elif getattr(args, "enable_server", False):
            enable_server = True

        extra = list(tc.configure_args)
        if getattr(args, "configure", None):
            extra += shlex.split(args.configure)

        jobs = getattr(args, "jobs", None)

        if not use_json:
            srv = "server+client" if enable_server else "client-only"
            print(f"Building Lustre ({srv}) against {args.target} kernel tree...")

        container_tag = tc.container_tag

        try:
            meta = _cli_attr("build_lustre")(
                lustre_tree,
                build_tree,
                container_tag=container_tag,
                target=args.target,
                enable_server=enable_server,
                extra_configure=extra,
                jobs=jobs,
                force=getattr(args, "force", False),
                arch=tc.arch,
                kernel=resolved_kernel,
                variant=tc.variant_name,
            )
        except Exception as e:
            return _error(f"Lustre build failed: {e}", use_json)

        _output(meta, use_json)
        return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: build shell
# ------------------------------------------------------------------


def cmd_build_shell(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target_args(args, use_json)
    if err is not None:
        return err
    assert tc is not None

    err = _preflight_podman(use_json)
    if err is not None:
        return err

    tag = tc.container_tag
    mount_path = Path(args.path).resolve()

    if not mount_path.is_dir():
        return _error(f"Mount path not found: {mount_path}", use_json)

    err = _preflight_container(tc, use_json)
    if err is not None:
        return err

    if not use_json:
        print(
            f"Entering build container for {args.target} "
            f"with {mount_path} mounted at /src..."
        )

    rc = subprocess.run(
        [
            "podman",
            "run",
            "--rm",
            "-it",
            "-v",
            f"{mount_path}:/src:Z",
            "-w",
            "/src",
            tag,
            "bash",
        ]
    ).returncode

    return rc


# ------------------------------------------------------------------
# Subcommand: status
# ------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    use_json = args.json
    targets = _cli_attr("list_targets")()

    if not targets:
        if use_json:
            print(json.dumps({"targets": []}))
        else:
            print("No targets configured.")
        return EXIT_OK

    all_status = {}
    TargetConfig = _cli_attr("TargetConfig")
    for name in targets:
        try:
            tc = TargetConfig(name)
        except ValueError:
            continue  # skip planned/disabled targets
        cs = _container_status(tc)
        ks = kernel_status(tc)
        # Images are per-(kernel, variant): build one row per built
        # kernel dir, plus the default kernel (even if nothing is built
        # yet) so the user sees "not built" instead of an empty section.
        # For each kernel, always report the base variant, then append a
        # row for every non-base variant subdir that actually has a
        # built image -- that way the `mofed-24` image shows up here
        # even when the user didn't remember to look under a subdir.
        from ltvm_pkg.target_config import DEFAULT_VARIANT

        built_kernels = tc.available_kernels()
        # Dedup while preserving order: available_kernels() returns full
        # directory names, but the default may resolve to the same full
        # name -- without dedup we'd emit that row twice (base + any
        # variants, doubled).
        kernels_to_report: list[str] = []
        seen: set[str] = set()
        for kname in [*built_kernels, tc.resolve_kernel(None)]:
            if kname and kname not in seen:
                seen.add(kname)
                kernels_to_report.append(kname)
        if not kernels_to_report:
            kernels_to_report = [tc.resolve_kernel(None)]
        declared_variants = tc.declared_variants()
        images: list[dict[str, Any]] = []
        for k in kernels_to_report:
            images.append(image_status(tc, kernel=k, variant=DEFAULT_VARIANT))
            # Non-base variants only get a row when actually built --
            # otherwise every kernel row would sprout a "mofed-24: not
            # built" line, which is noise for users who never touch MOFED.
            kernel_image_dir = tc.image_output_dir(
                kernel=k, variant=DEFAULT_VARIANT
            )
            for v in declared_variants:
                v_dir = kernel_image_dir / v
                if not (v_dir / "base.ext4").exists():
                    continue
                images.append(image_status(tc, kernel=k, variant=v))
        all_status[name] = {
            "container": cs,
            "kernel": ks,
            "images": images,
        }

    if use_json:
        print(json.dumps(all_status, indent=2))
    else:
        # Table output: one row per (target, kernel, variant) image.
        hdr = (
            f"{'Target':<12} {'Container':<14} {'Kernel':<26} "
            f"{'Image-Kernel':<44} {'Variant':<10} {'Image':<14}"
        )
        print(hdr)
        print("-" * len(hdr))
        for name, st in all_status.items():
            c = _artifact_label(st["container"])
            k = _artifact_label(st["kernel"])
            for ims in st["images"]:
                i = _artifact_label(ims)
                image_kernel = ims.get("kernel", "")
                variant = ims.get("variant", "base")
                print(
                    f"{name:<12} {c:<14} {k:<26} "
                    f"{image_kernel:<44} {variant:<10} {i:<14}"
                )

    return EXIT_OK


# ------------------------------------------------------------------
# Lustre/kernel compat gate (shared with fetch, targets, deploy)
# ------------------------------------------------------------------


def _gate_lustre_validation(
    tc: TargetConfig,
    lustre_tree: Path,
    *,
    force: bool,
    kernel_build_tree: Path | None = None,
) -> None:
    """Run validate_target as a gate before producing Lustre artifacts.

    Behavior by status:
      ok           silent pass
      best_effort  one-line stderr warning, pass
      refuse       print message; raise SystemExit(EXIT_ERROR) unless
                   force is True (then print override line, pass)
      error        print message; raise SystemExit(EXIT_ERROR) regardless
                   of force -- parse/IO failures are not overridable

    validate_target now owns the decision for all targets, including
    deb-based ones (client-mode ubuntu lives in ChangeLog's client
    kernel lists).
    """
    result = _cli_attr("validate_target")(
        tc, lustre_tree, kernel_build_tree=kernel_build_tree
    )
    if result.status == "ok":
        return
    if result.status == "best_effort":
        print(f"warning: [best_effort] {result.message}", file=sys.stderr)
        return
    if result.status == "refuse":
        if force:
            print(
                f"--force-compat: overriding refusal: {result.message}",
                file=sys.stderr,
            )
            return
        print(f"[refuse] {result.message}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    # "error": parse / IO problems.  Not force-able.
    print(f"[error] {result.message}", file=sys.stderr)
    raise SystemExit(EXIT_ERROR)
