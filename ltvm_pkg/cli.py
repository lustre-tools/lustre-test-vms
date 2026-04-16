"""Command implementations for ltvm CLI.

Each cmd_* function takes an argparse.Namespace and returns an int
exit code.  Private helpers shared across commands live here too.
The top-level ``ltvm`` script owns argparse setup and dispatch.
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

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_FOUND = 2

# GitHub repo for release downloads.  Override with LTVM_GITHUB_REPO
# so a fork can use `ltvm fetch` / `ltvm publish` without editing
# source.
GITHUB_REPO = os.environ.get("LTVM_GITHUB_REPO", "lustre-tools/lustre-test-vms")


# ------------------------------------------------------------------
# Output helpers
# ------------------------------------------------------------------


def _resolve_lustre_tree(
    arg_value: str | None,
) -> tuple[Path | None, str | None]:
    """Resolve --lustre-tree, defaulting to cwd.

    Returns (Path, error_string).  error_string is None on success.
    """
    from .lustre_tree import kp_root

    p = Path(arg_value).resolve() if arg_value else Path.cwd()
    if not p.is_dir():
        return None, f"Not a directory: {p}"
    kp = kp_root(p)
    if not kp.is_dir():
        return None, (
            f"{p} does not look like a Lustre tree (no lustre/kernel_patches/)"
        )
    return p, None


def _output(data: Any, use_json: bool) -> None:
    """Print data as JSON or as a human-readable string."""
    if use_json:
        print(json.dumps(data, indent=2))
    else:
        if isinstance(data, str):
            print(data)
        elif isinstance(data, dict):
            for k, v in data.items():
                print(f"  {k}: {v}")
        elif isinstance(data, list):
            for item in data:
                print(item)


def _emit_error(
    msg: str,
    use_json: bool,
    hint: str | None = None,
    code: int = EXIT_ERROR,
) -> int:
    """Print an error message and return the given exit code.

    When called from inside an ``except`` block with LTVM_VERBOSE=1 (or
    --verbose flipped the root logger to DEBUG), append the in-flight
    traceback so programming bugs (TypeError, AttributeError) surface
    their real origin instead of being flattened into
    ``"<Cmd> failed: <str(exc)>"`` mystery strings.
    """
    if use_json:
        err = {"error": msg}
        if hint:
            err["hint"] = hint
        print(json.dumps(err, indent=2), file=sys.stderr)
    else:
        print(f"error: {msg}", file=sys.stderr)
        if hint:
            print(f"hint: {hint}", file=sys.stderr)
        _maybe_print_traceback()
    return code


def _maybe_print_traceback() -> None:
    """Print the active exception's traceback iff verbose logging is on.

    Reads the root logger level so --verbose (which sets DEBUG in the
    main entry point) enables tracebacks without requiring callers to
    thread a flag through.  LTVM_VERBOSE=1 is honored as an alternative
    for contexts where argparse state isn't reachable.
    """
    import os as _os
    import traceback as _tb

    if sys.exc_info()[0] is None:
        return
    verbose = (
        logging.getLogger().isEnabledFor(logging.DEBUG)
        or _os.environ.get("LTVM_VERBOSE") == "1"
    )
    if verbose:
        _tb.print_exc(file=sys.stderr)


def _error(msg: str, use_json: bool, hint: str | None = None) -> int:
    return _emit_error(msg, use_json, hint=hint, code=EXIT_ERROR)


def _load_target(
    name: str,
    use_json: bool,
    arch: str | None = None,
    variant: str = "base",
) -> tuple[TargetConfig | None, int | None]:
    """Load a TargetConfig, returning (config, None) or
    (None, exit_code) on failure."""
    try:
        return TargetConfig(name, arch=arch, variant=variant), None
    except ValueError as e:
        targets = list_targets()
        hint = (
            f"Available targets: {', '.join(targets)}"
            if targets
            else "No targets configured"
        )
        code = _emit_error(str(e), use_json, hint=hint, code=EXIT_NOT_FOUND)
        return None, code


def _load_target_args(
    args: argparse.Namespace, use_json: bool
) -> tuple[TargetConfig | None, int | None]:
    """Load TargetConfig from args.target + optional args.arch + --variant.

    Applies CLI param overrides (e.g. --mofed-version) onto the variant
    so they fold into the input hash.
    """
    variant = getattr(args, "variant", "base") or "base"
    tc, err = _load_target(
        args.target, use_json, arch=getattr(args, "arch", None), variant=variant
    )
    if tc is None:
        return None, err
    # Thread ad-hoc param overrides into the bound variant.
    overrides: dict[str, Any] = {}
    if getattr(args, "mofed_version", None):
        overrides["mofed_version"] = args.mofed_version
    if overrides and variant != "base":
        tc._variants[variant] = tc._variants[variant].with_param_overrides(
            overrides
        )
    return tc, None


# ------------------------------------------------------------------
# Container status helper
# ------------------------------------------------------------------


def _container_status(target_config: TargetConfig) -> dict[str, Any]:
    """Return status dict for the build container artifact."""
    meta_file = target_config.container_output_dir() / "meta.json"
    meta = load_meta_safe(meta_file)
    if meta is None:
        return {"built": False, "stale": True}
    stale = target_config.is_stale("container")
    return {"built": True, "stale": stale, **meta}


def _artifact_label(status_dict: dict[str, Any]) -> str:
    """Produce a human label like 'current', 'stale (config changed)',
    or 'not built'.

    `stale` may be None for kernel artifacts when called from cmd_status,
    which has no Lustre tree on hand to recompute the round-17
    Lustre-inputs hash -- in that case we can't honestly say whether the
    cached vmlinuz is stale, so we render "built (?)" rather than lying
    in either direction.
    """
    if not status_dict.get("built", False):
        return "not built"
    stale = status_dict.get("stale", False)
    if stale is None:
        return "built (?)"
    if stale:
        return "stale"
    return "current"


def _require_root(use_json: bool, hint: str = "") -> int | None:
    """Return an error code if not root, or None if root."""
    if os.getuid() != 0:
        msg = "This command requires root. Use: sudo ltvm ..."
        if hint:
            msg += f"\n  {hint}"
        return _error(msg, use_json)
    return None


def _qemu_ns(**kwargs: Any) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for qemu command functions."""
    return argparse.Namespace(**kwargs)


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

    With --lustre-build, also builds the Lustre source tree
    against the freshly built kernel.
    """
    use_json = args.json
    tc, err = _load_target_args(args, use_json)
    if err is not None:
        return err
    assert tc is not None

    # build-all always requires a Lustre tree -- even for deb targets
    # where the kernel build itself doesn't need one, the surrounding
    # workflow (image inject, optional --lustre-build, packaging) does.
    lustre_tree, err_msg = _resolve_lustre_tree(args.lustre_tree)
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

    _gate_lustre_validation(
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
        _do_build_container(tc)
        results["container"] = "ok"
    except Exception as e:
        return _error(f"Container build failed: {e}", use_json)

    # 2. Kernel
    if not use_json:
        print(f"==> Building kernel {resolved_kernel} for {args.target}...")
    try:
        kmeta = build_kernel(
            tc,
            lustre_tree,
            force=args.force,
            kernel=resolved_kernel,
        )
        results["kernel"] = kmeta
    except Exception as e:
        return _error(f"Kernel build failed: {e}", use_json)

    # 3. Lustre (optional -- only when --lustre-build is passed).
    # Runs BEFORE the image so its per-kernel staging is in place for
    # the image-bake step to auto-inject.
    lustre_build = getattr(args, "lustre_build", False)
    if lustre_build:
        if not use_json:
            print(
                f"==> Building Lustre against {resolved_kernel} kernel tree..."
            )
        build_tree = tc.kernel_output_dir(kernel=resolved_kernel) / "build-tree"
        try:
            container_tag = tc.container_tag
            lmeta = build_lustre(
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
        build_image(
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

    if not use_json:
        print(f"Building container for {args.target}...")

    try:
        tag = _do_build_container(tc)
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

    # Deb-based targets don't need a Lustre tree for kernel builds
    lustre_tree = None
    if not tc.kernel_deb_source:
        lustre_tree, err_msg = _resolve_lustre_tree(args.lustre_tree)
        if err_msg:
            return _error(
                err_msg,
                use_json,
                hint="Run from a Lustre tree, or pass "
                "--lustre-tree /path/to/lustre-release",
            )
        assert lustre_tree is not None
        _gate_lustre_validation(
            tc, lustre_tree, force=args.force_compat
        )

    kernel = getattr(args, "kernel", None)

    if not use_json:
        k = tc.resolve_kernel(kernel)
        print(f"Building kernel {k} for {args.target}...")

    try:
        meta = build_kernel(
            tc,
            lustre_tree,
            force=args.force,
            kernel=kernel,
        )
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

    from ltvm_pkg.mofed_kmod_build import build_mofed_kmods
    from ltvm_pkg.target_config import DEFAULT_VARIANT

    if tc.variant_name == DEFAULT_VARIANT:
        return _error(
            f"target {tc.name!r} is bound to base variant -- "
            f"mofed-kmods only applies to a mofed variant",
            use_json,
            hint=f"Pass --variant mofed-24 (or whichever mofed-* is declared)",
        )

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
                f"no Lustre staging at {candidate}",
                use_json,
                hint=(
                    f"run `ltvm build lustre {args.target} --kernel "
                    f"{resolved_kernel}` first, or pass --no-lustre to "
                    f"bake a kernel-only image"
                ),
            )
        _gate_lustre_validation(
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
        path = build_image(
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

    lustre_tree_arg = getattr(args, "lustre_tree_pos", None) or getattr(
        args, "lustre_tree", None
    )
    lustre_tree, err_msg = _resolve_lustre_tree(lustre_tree_arg)
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

    _gate_lustre_validation(
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

    # Pre-flight: check the container exists in podman storage and give a
    # clean, distinctive error if not.  build_lustre would otherwise raise
    # the same condition wrapped in "Lustre build failed: ...", which buries
    # the actionable hint.  This is the most common first-run error path,
    # so it gets first-class treatment.
    container_check = subprocess.run(
        ["podman", "image", "exists", container_tag],
        capture_output=True,
    )
    if container_check.returncode != 0:
        return _error(
            f"Build container '{container_tag}' not found in podman storage",
            use_json,
            hint=(
                f"Run: ltvm build container {args.target}\n"
                f"  Or fetch a published target: ltvm target fetch {args.target}"
            ),
        )

    try:
        meta = build_lustre(
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
# Subcommand: package
# ------------------------------------------------------------------


def cmd_package(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target_args(args, use_json)
    if err is not None:
        return err
    assert tc is not None

    kernel = getattr(args, "kernel", None)
    variant = getattr(args, "variant", None) or "base"

    # Bundling Lustre is mandatory by default.  The publisher's whole
    # job is to ship something a fetcher can immediately deploy from --
    # a "kernel-only" package without Lustre forces every consumer to
    # build it themselves on first deploy, which defeats the whole
    # point of the fetch flow.  --no-lustre is the explicit opt-out.
    no_lustre = getattr(args, "no_lustre", False)
    if not no_lustre:
        lustre_tree_arg = getattr(args, "lustre_tree", None)
        lustre_path, err_msg = _resolve_lustre_tree(lustre_tree_arg)
        if err_msg:
            return _error(
                err_msg,
                use_json,
                hint=(
                    "Run from a Lustre tree, pass --lustre-tree, or "
                    "use --no-lustre to publish a kernel-only package"
                ),
            )
        assert lustre_path is not None
        if not tc.kernel_deb_source:
            _gate_lustre_validation(
                tc, lustre_path, force=args.force_compat
            )
        if not use_json:
            print(f"Snapshotting Lustre tree from {lustre_path}...")
        try:
            snapshot_lustre(
                lustre_path,
                tc.output_dir,
                target=args.target,
                kernel=kernel,
                arch=tc.arch,
                variant=variant,
            )
        except Exception as e:
            return _error(f"Lustre snapshot failed: {e}", use_json)

    if not use_json:
        v_hint = "" if variant == "base" else f" variant={variant}"
        print(f"Packaging {args.target}{v_hint}...")

    try:
        assets = package_target(
            args.target,
            tc.output_dir,
            kernel=kernel,
            dest_dir=getattr(args, "output", None),
            arch=tc.arch,
            variant=variant,
        )
    except Exception as e:
        return _error(f"Package failed: {e}", use_json)

    result = {
        "target": args.target,
        "variant": variant,
        "assets": {kind: str(p) for kind, p in assets.items()},
    }
    _output(result, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: fetch
# ------------------------------------------------------------------


def _gh_api(endpoint: str) -> dict | list:
    """Call GitHub API and return parsed JSON.

    For list endpoints (e.g. "releases"), follows the Link: rel="next"
    pagination header so callers see the full result set.  GitHub's
    default per_page is 30; we ask for 100 to minimize round trips.
    Without this, anything past the first 30 releases vanished from
    `ltvm target fetch --list` and produced "no release found" for older
    targets.
    """
    sep = "&" if "?" in endpoint else "?"
    url: str | None = (
        f"https://api.github.com/repos/{GITHUB_REPO}/{endpoint}{sep}per_page=100"
    )
    aggregated: list = []
    first_result: dict | list | None = None

    while url:
        # -D - dumps headers to stdout, then \r\n\r\n separates headers from body.
        try:
            r = subprocess.run(
                ["curl", "-fsSL", "--max-time", "30", "-D", "-", url],
                capture_output=True,
                text=True,
                timeout=35,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"GitHub API timed out after {e.timeout}s: {url}"
            )
        if r.returncode != 0:
            raise RuntimeError(
                f"GitHub API failed (rc={r.returncode}): {url}\n  {r.stderr.strip()}"
            )
        # Split on the last blank line: headers above, body below.
        # curl -D - prints all header blocks (including any redirects)
        # before the body.  Although HTTP uses \r\n\r\n as the separator
        # on the wire, subprocess text=True mode runs universal-newline
        # decoding, which translates \r\n to \n -- so in r.stdout the
        # separator is \n\n.  rpartition takes the LAST match so we keep
        # only the final response when curl followed redirects.
        headers, _, body = r.stdout.rpartition("\n\n")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            snippet = body[:200].replace("\n", " ")
            raise RuntimeError(
                f"GitHub API returned non-JSON: {url}\n  {e}\n  body: {snippet}"
            )

        if first_result is None:
            first_result = data
        if isinstance(data, list):
            aggregated.extend(data)
        else:
            # Non-list endpoint -- pagination doesn't apply, return as-is.
            assert isinstance(data, dict)
            return data

        url = _gh_next_link(headers)

    return (
        aggregated if isinstance(first_result, list) else (first_result or {})
    )


def _gh_next_link(headers: str) -> str | None:
    """Extract the rel="next" URL from a Link header, or None.

    Link header looks like:
        Link: <https://api.github.com/...?page=2>; rel="next", <...>; rel="last"
    """
    for line in headers.splitlines():
        if not line.lower().startswith("link:"):
            continue
        # Each comma-separated entry: <URL>; rel="..."
        for entry in line[5:].split(","):
            entry = entry.strip()
            if 'rel="next"' not in entry:
                continue
            lt = entry.find("<")
            gt = entry.find(">")
            if lt != -1 and gt != -1 and gt > lt:
                return entry[lt + 1 : gt]
    return None


_RHEL_RE = __import__("re").compile(r"rhel(\d+)\.(\d+)")
_KVER_PREFIX_RE = __import__("re").compile(r"^(\d+\.\d+)")


def _kernel_release_signature(kname: str) -> str | None:
    """Derive a substring found in release tag/asset names for ``kname``.

    Release asset names use the kernel uname-r suffix:
    - RHEL kernels contain ``elX_Y`` where X.Y is the distro minor
      version, so ``5.14-rhel9.5`` -> ``el9_5``.
    - Non-RHEL (e.g. Ubuntu) fall back to the leading ``MAJOR.MINOR``
      kernel version, so ``6.8-ubuntu2404`` -> ``6.8``.  This only
      discriminates if the target's ``kernels.available`` entries
      differ in their kernel-major version; same-major variants would
      collide and need a richer signature.

    Returns None if no signature can be derived; callers then skip
    kernel-name filtering.
    """
    m = _RHEL_RE.search(kname)
    if m:
        return f"el{m.group(1)}_{m.group(2)}"
    m = _KVER_PREFIX_RE.match(kname)
    if m:
        return m.group(1)
    return None


def _release_matches_kernel(rel: dict, signature: str, arch: str) -> bool:
    """True iff ``rel`` has an arch-matching asset whose name contains
    the kernel signature."""
    arch_match = f"-{arch}-"
    for asset in rel.get("assets", []):
        name = asset.get("name", "")
        if arch_match not in name:
            continue
        if signature in name:
            return True
    # Tag itself sometimes carries the signature (publish derives tag
    # from tarball stem, which contains the kernel_version).
    tag = rel.get("tag_name", "")
    return signature in tag


def _find_release_url(
    target: str,
    filter_str: str | None = None,
    arch: str = "x86_64",
    kernel_signature: str | None = None,
    variant: str = "base",
    mode: str = "ecosystem",
) -> str:
    """Find an asset download URL from GitHub releases.

    ``mode`` selects which asset to look for:

      * "ecosystem" (default) -- the manifest JSON produced by
        ``package_target``; ``fetch_target`` consumes this URL.
      * "bootable" -- a standalone bootable qcow2.zst produced by
        ``package_bootable``.

    The asset name encodes (target, arch, kver, [variant]); for a
    variant fetch we require the exact ``-<variant>`` suffix so a
    ``--variant mofed`` request doesn't silently grab the base
    asset and vice versa.
    """
    releases = _gh_api("releases")
    if not isinstance(releases, list):
        releases = [releases]

    arch_match = f"-{arch}-"
    if mode == "bootable":
        prefix = f"bootable-{target}{arch_match}"
        suffix = ".zst"
        # Bootable publish tags them as ``bootable-<target>-<arch>-<kver>[-<variant>]``,
        # NOT ``<target>-<arch>-...`` like the ecosystem path -- the two
        # release namespaces are intentionally separate (see cmd_publish
        # docstring).  Filter tags accordingly or we'd never match.
        tag_prefix = f"bootable-{target}"
    else:
        prefix = f"manifest-{target}{arch_match}"
        suffix = ".json"
        tag_prefix = target

    variant_tail = (
        f"-{variant}{suffix}" if variant != "base" else suffix
    )
    # For base variant we need to reject names that have any variant
    # suffix (e.g. `-mofed.json`); otherwise a base lookup could grab
    # a mofed asset.  We check by stripping the suffix and looking for
    # a '-' in what remains after the kver.  Simpler: require the name
    # to end with a kver-looking token (digits/dots) before suffix.
    for rel in releases:
        tag = rel.get("tag_name", "")
        if tag != tag_prefix and not tag.startswith(tag_prefix + "-"):
            continue
        if filter_str and filter_str not in tag:
            continue
        if kernel_signature and not _release_matches_kernel(
            rel, kernel_signature, arch
        ):
            continue
        for asset in rel.get("assets", []):
            name = asset.get("name", "")
            if not name.startswith(prefix):
                continue
            if not name.endswith(variant_tail):
                continue
            if variant == "base":
                # Reject names whose tail before the suffix ends with
                # a non-numeric `-<variant>` segment.  The kver
                # always ends in digits.dots; variant segments are
                # letters.
                stem = name[:-len(suffix)]
                last_seg = stem.rsplit("-", 1)[-1]
                if last_seg and not any(
                    ch.isdigit() for ch in last_seg
                ):
                    continue
            if kernel_signature and kernel_signature not in name:
                continue
            return str(asset["browser_download_url"])

    avail = [r.get("tag_name", "?") for r in releases]
    hint = f" matching '{filter_str}'" if filter_str else ""
    if kernel_signature:
        hint += f" kernel-signature={kernel_signature!r}"
    if variant != "base":
        hint += f" variant={variant!r}"
    raise RuntimeError(
        f"No {mode} release found for '{target}'{hint}\n"
        f"  Available releases: {', '.join(avail)}\n"
        f"  Try: ltvm target fetch --list"
    )


def _list_releases(
    target: str | None = None,
    kernel_signature: str | None = None,
    arch: str = "x86_64",
) -> list[dict]:
    """List available releases, optionally filtered by target prefix."""
    releases = _gh_api("releases")
    if not isinstance(releases, list):
        releases = [releases]
    result = []
    for rel in releases:
        tag = rel.get("tag_name", "")
        if target and tag != target and not tag.startswith(target + "-"):
            continue
        if kernel_signature and not _release_matches_kernel(
            rel, kernel_signature, arch
        ):
            continue
        assets = [
            a["name"]
            for a in rel.get("assets", [])
            if a["name"].endswith(".tar.gz")
        ]
        size_mb = sum(a.get("size", 0) for a in rel.get("assets", [])) / (
            1024 * 1024
        )
        result.append(
            {
                "tag": tag,
                "date": rel.get("published_at", "")[:10],
                "assets": assets,
                "size_mb": round(size_mb),
            }
        )
    return result


def cmd_fetch(args: argparse.Namespace) -> int:
    use_json = args.json
    url = getattr(args, "url", None)
    target = getattr(args, "target", None)
    filt = getattr(args, "filter", None)
    arch = getattr(args, "arch", None) or "x86_64"
    kernel = getattr(args, "kernel", None)
    variant = getattr(args, "variant", None) or "base"
    image_mode = bool(getattr(args, "image", False))

    # Validate --kernel against the target's declared kernels (if both
    # are given).  This is user-friendly: it catches typos before we
    # make a round trip to GitHub.
    kernel_signature: str | None = None
    if kernel:
        if not target:
            return _error(
                "--kernel requires a target (e.g. ltvm target fetch rocky9 "
                "--kernel 5.14-rhel9.5)",
                use_json,
            )
        tc, err = _load_target(target, use_json, arch=arch)
        if err is not None:
            return err
        assert tc is not None
        declared = tc.declared_kernels()
        if kernel not in declared:
            return _error(
                f"--kernel {kernel!r} not in targets.yaml kernels.available "
                f"for {target}",
                use_json,
                hint=f"Available: {', '.join(declared)}",
            )
        kernel_signature = _kernel_release_signature(kernel)
        if kernel_signature is None and not use_json:
            print(
                f"warning: could not derive release signature from "
                f"--kernel {kernel!r}; falling back to first match.",
                file=sys.stderr,
            )

    # --list: show available releases
    if getattr(args, "list", False):
        try:
            releases = _list_releases(
                target, kernel_signature=kernel_signature, arch=arch
            )
        except RuntimeError as e:
            return _error(str(e), use_json)
        if use_json:
            _output(releases, use_json)
        else:
            if not releases:
                print("  (no releases found)")
            for r in releases:
                print(f"  {r['tag']:<60s}  {r['size_mb']:>5d} MB  {r['date']}")
        return EXIT_OK

    if not target:
        return _error("target required (e.g. ltvm target fetch rocky9)", use_json)

    from ltvm_pkg.target_config import OUTPUT_DIR

    # Bootable mode: fetch a single qcow2.zst asset, no manifest, no
    # ecosystem.  Handled up-front so we skip all the tag/ecosystem
    # plumbing below.
    if image_mode:
        from ltvm_pkg.release_package import fetch_bootable

        if not url:
            if not use_json:
                print(
                    f"Looking up bootable {target} ({variant}) "
                    f"from GitHub releases..."
                )
            try:
                url = _find_release_url(
                    target,
                    filter_str=filt,
                    arch=arch,
                    kernel_signature=kernel_signature,
                    variant=variant,
                    mode="bootable",
                )
            except RuntimeError as e:
                return _error(str(e), use_json)

        try:
            path = fetch_bootable(
                target, url, OUTPUT_DIR, arch=arch, variant=variant
            )
        except Exception as e:
            return _error(f"Fetch bootable failed: {e}", use_json)

        if not use_json:
            print(f"  Bootable disk at: {path}")
        _output(
            {"target": target, "variant": variant, "path": str(path),
             "mode": "bootable"},
            use_json,
        )
        return EXIT_OK

    # Resolve URL: explicit --url, or GitHub release lookup.  In the
    # normal (ecosystem) path we look for the manifest; fetch_target
    # pulls the rest of the assets from the same release by parsing
    # their names out of the manifest.
    if not url:
        if not use_json:
            print(f"Looking up {target} ({variant}) from GitHub releases...")
        try:
            url = _find_release_url(
                target,
                filter_str=filt,
                arch=arch,
                kernel_signature=kernel_signature,
                variant=variant,
                mode="ecosystem",
            )
        except RuntimeError as e:
            return _error(str(e), use_json)

    # Extract release tag from URL to check if already fetched.
    # URL: .../releases/download/<tag>/<filename>
    # The tag file lives under the arch-qualified output dir so each
    # arch tracks its own release independently.
    if "/releases/download/" in url:
        release_tag = url.split("/releases/download/")[1].split("/")[0]
    else:
        release_tag = ""
    tag_file = OUTPUT_DIR / target / arch / ".ltvm-release-tag"
    replace = bool(getattr(args, "replace", False))
    force = bool(getattr(args, "force", False))
    existing_tag = (
        tag_file.read_text().strip() if tag_file.exists() else ""
    )
    if release_tag and existing_tag == release_tag:
        # Same tag already on disk.  Without --replace: no-op success.
        # With --replace but no --force: refuse, because the "clean
        # re-fetch" would produce identical bytes -- probably not
        # what the user meant to pay for.
        if replace and not force:
            return _error(
                f"local copy already at {release_tag}; "
                f"--replace would re-download identical bytes",
                use_json,
                hint="pass --force to re-fetch anyway",
            )
        if not replace:
            if not use_json:
                print(f"  Already up to date ({release_tag})")
            result = {"target": target, "path": str(OUTPUT_DIR / target / arch)}
            _output(result, use_json)
            return EXIT_OK

    # --replace: wipe the target's output dir so a partial or
    # mismatched prior fetch doesn't leave stale files behind the
    # new extraction.  The reference directory is target/arch, not
    # target/, because per-arch fetches share output/<target>/.
    if replace:
        target_out = OUTPUT_DIR / target / arch
        if target_out.exists():
            if not use_json:
                print(f"  Removing existing {target_out}...")
            import shutil as _shutil
            _shutil.rmtree(target_out)

    if not use_json:
        print(f"Fetching {target}...")

    try:
        target_dir = fetch_target(
            target, url, OUTPUT_DIR, arch=arch, variant=variant
        )
        # Record the release tag so repeat fetches are instant
        tag_file.parent.mkdir(parents=True, exist_ok=True)
        tag_file.write_text(release_tag + "\n")
    except Exception as e:
        # A schema-mismatch error means the published manifest was
        # produced by a newer (or older) ltvm.  Force an immediate
        # update-check prompt so the user can self-heal instead of
        # just reading a cryptic error.
        if "unrecognized manifest schema" in str(e) and not use_json:
            try:
                from ltvm_pkg.update_check import maybe_check_for_updates

                maybe_check_for_updates(force=True, use_json=False)
            except (ImportError, OSError):
                # The update check is advisory -- a missing module
                # or a network/IO hiccup must not mask the actual
                # fetch failure we're already reporting.  Genuine
                # programming bugs (TypeError, AttributeError) still
                # propagate so they surface with a traceback.
                pass
        return _error(f"Fetch failed: {e}", use_json)

    result = {"target": target, "path": str(target_dir)}
    _output(result, use_json)

    if not use_json:
        print()
        print("Next:")
        arch_flag = f" --arch {arch}" if arch != "x86_64" else ""
        print(
            f"  sudo ltvm create co1-test --target {target}{arch_flag} "
            f"--vcpus 2 --mdt-disks 1 --ost-disks 2"
        )
        print("  ltvm llmount co1-test")
        try:
            tc_hint = TargetConfig(target)
            avail = tc_hint.declared_kernels()
            default_k = tc_hint.default_kernel
        except (ValueError, FileNotFoundError):
            # Best-effort hint -- if the target isn't parseable we
            # just drop the "try another kernel" line rather than
            # failing the fetch that already succeeded.
            avail = []
            default_k = ""
        if len(avail) > 1:
            alt = next((k for k in avail if k != default_k), None)
            if alt is not None:
                print(f"  # or pass --kernel {alt} to select a different kernel")

    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: publish
# ------------------------------------------------------------------


def _gh_release_upload(
    tag: str, assets: list[Path], notes: str, use_json: bool
) -> tuple[int | None, str | None]:
    """Create (if needed) a GitHub release at ``tag`` and upload every
    path in ``assets`` to it.  Returns (exit_code, err_msg): on success,
    (None, None).  ``--clobber`` is set so re-runs overwrite prior
    uploads with the same asset name, which matches the rest of the
    publish flow.
    """
    try:
        create = subprocess.run(
            [
                "gh", "release", "create", tag,
                "--repo", GITHUB_REPO,
                "--title", tag,
                "--notes", notes,
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return EXIT_ERROR, "gh CLI not found (https://cli.github.com/)"
    if create.returncode != 0:
        gh_msg = (create.stderr or "") + (create.stdout or "")
        if "already exists" not in gh_msg:
            return EXIT_ERROR, (
                f"gh release create failed (rc={create.returncode}): "
                f"{gh_msg.strip()}"
            )

    for a in assets:
        if not use_json:
            size_mb = a.stat().st_size / (1024 * 1024)
            print(f"  Uploading {a.name} ({size_mb:.0f} MB)...")
        r = subprocess.run(
            [
                "gh", "release", "upload", tag, str(a),
                "--repo", GITHUB_REPO, "--clobber",
            ],
        )
        if r.returncode != 0:
            return EXIT_ERROR, (
                f"gh release upload failed (rc={r.returncode}) for {a.name}"
            )
    return None, None


def cmd_publish(args: argparse.Namespace) -> int:
    """Upload a packaged asset set to a GitHub release.

    Two modes, selected by ``--image``:

    * default: publishes the ecosystem -- every asset listed by the
      manifest produced by ``package_target`` (container, kernel, image,
      optional lustre) to a single release tagged ``<target>-<arch>-<kver>[-<variant>]``.
      The manifest is uploaded alongside so ``fetch`` can verify.

    * ``--image``: publishes only a bootable qcow2 (produced by
      ``ltvm target export`` earlier) to a dedicated release tagged
      ``bootable-<target>-<arch>-<kver>[-<variant>]``.  Completely
      separate from the ecosystem release so fetching one doesn't
      accidentally grab the other.
    """
    use_json = args.json
    tc, err = _load_target_args(args, use_json)
    if err is not None:
        return err
    assert tc is not None

    kernel = getattr(args, "kernel", None)
    variant = getattr(args, "variant", None) or "base"
    image_mode = bool(getattr(args, "image", False))
    tag = getattr(args, "tag", None)

    if image_mode:
        from ltvm_pkg.release_package import package_bootable

        if not use_json:
            print(f"Preparing bootable asset for {args.target}...")
        try:
            asset = package_bootable(
                args.target,
                tc.output_dir,
                kernel=kernel,
                arch=tc.arch,
                variant=variant,
            )
        except Exception as e:
            return _error(f"Package bootable failed: {e}", use_json)

        # Tag: derive from the filename, stripping .qcow2.zst so the
        # tag reads naturally.  Keeps bootable releases separate from
        # ecosystem releases (they use different tag prefixes).
        if not tag:
            name = asset.name
            for suffix in (".qcow2.zst", ".raw.zst", ".zst"):
                if name.endswith(suffix):
                    tag = name[: -len(suffix)]
                    break
            else:
                tag = asset.stem

        if not use_json:
            print(f"  Tag: {tag}")
        exit_code, err_msg = _gh_release_upload(
            tag, [asset],
            notes=(
                f"Bootable disk image for {args.target} ({variant}) -- "
                f"self-contained, no ltvm runtime required"
            ),
            use_json=use_json,
        )
        if exit_code is not None:
            return _error(err_msg or "upload failed", use_json)

        url = f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}"
        if not use_json:
            print(f"  Published: {url}")
        _output(
            {"target": args.target, "tag": tag, "asset": str(asset),
             "url": url, "mode": "bootable"},
            use_json,
        )
        return EXIT_OK

    # --- Ecosystem publish: package, then upload every asset. ---
    # cmd_package did the work if the caller ran it first, but we
    # can't tell cheaply; re-run it inline so `publish` is always
    # self-sufficient (package_target is idempotent given fresh
    # artifacts -- it just recompresses).
    no_lustre = getattr(args, "no_lustre", False)
    if not no_lustre:
        lustre_tree_arg = getattr(args, "lustre_tree", None)
        lustre_path, err_msg = _resolve_lustre_tree(lustre_tree_arg)
        if err_msg:
            return _error(
                err_msg, use_json,
                hint=(
                    "Run from a Lustre tree, pass --lustre-tree, or "
                    "use --no-lustre for a kernel-only publish"
                ),
            )
        assert lustre_path is not None
        if not tc.kernel_deb_source:
            _gate_lustre_validation(tc, lustre_path, force=args.force_compat)
        if not use_json:
            print(f"Snapshotting Lustre tree from {lustre_path}...")
        try:
            snapshot_lustre(
                lustre_path, tc.output_dir,
                target=args.target, kernel=kernel,
                arch=tc.arch, variant=variant,
            )
        except Exception as e:
            return _error(f"Lustre snapshot failed: {e}", use_json)

    if not use_json:
        v_hint = "" if variant == "base" else f" variant={variant}"
        print(f"Packaging {args.target}{v_hint}...")
    try:
        assets = package_target(
            args.target, tc.output_dir,
            kernel=kernel, arch=tc.arch, variant=variant,
            dest_dir=getattr(args, "output", None),
        )
    except Exception as e:
        return _error(f"Package failed: {e}", use_json)

    # Tag: derive from the manifest filename (strips .json, natural
    # read).  Variant is embedded in the manifest name for free.
    if not tag:
        manifest_name = assets["manifest"].name
        tag = manifest_name[len("manifest-"): -len(".json")]

    if not use_json:
        print(f"  Tag: {tag}")
        print(f"  Assets: {len(assets)}")

    # Upload EVERYTHING including the manifest so fetch can find it.
    to_upload = list(assets.values())
    exit_code, err_msg = _gh_release_upload(
        tag, to_upload,
        notes=f"Pre-built artifacts for {args.target} (variant={variant})",
        use_json=use_json,
    )
    if exit_code is not None:
        return _error(err_msg or "upload failed", use_json)

    url = f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}"
    if not use_json:
        print(f"  Published: {url}")

    # Record the release tag locally so subsequent `ltvm fetch` knows
    # the artifacts already on disk match this release.
    from ltvm_pkg.target_config import OUTPUT_DIR

    arch = getattr(args, "arch", None) or "x86_64"
    tag_file = OUTPUT_DIR / args.target / arch / ".ltvm-release-tag"
    tag_file.parent.mkdir(parents=True, exist_ok=True)
    tag_file.write_text(tag + "\n")

    result = {
        "target": args.target,
        "variant": variant,
        "tag": tag,
        "assets": {k: str(p) for k, p in assets.items()},
        "url": url,
    }
    _output(result, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: shell
# ------------------------------------------------------------------


def cmd_build_shell(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target_args(args, use_json)
    if err is not None:
        return err
    assert tc is not None

    tag = tc.container_tag
    mount_path = Path(args.path).resolve()

    if not mount_path.is_dir():
        return _error(f"Mount path not found: {mount_path}", use_json)

    # Check container image exists
    try:
        result = subprocess.run(
            ["podman", "image", "exists", tag], capture_output=True
        )
    except FileNotFoundError:
        return _error(
            "podman not found",
            use_json,
            hint="install podman or run `ltvm install` to set up the host",
        )
    if result.returncode != 0:
        return _error(
            f"Container image {tag} not found",
            use_json,
            hint=f"Run: ltvm build container {args.target}",
        )

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
    targets = list_targets()

    if not targets:
        if use_json:
            print(json.dumps({"targets": []}))
        else:
            print("No targets configured.")
        return EXIT_OK

    all_status = {}
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
# Subcommand: targets (list configured target OSes)
# ------------------------------------------------------------------


def _release_status(
    target: str,
    arch: str,
    all_releases: list | None,
    kernel_signature: str | None = None,
    variant: str = "base",
) -> tuple[str, str]:
    """Return (local_tag, remote_tag) for a target/arch/variant.

    Both strip the shared ``<target>-<arch>-`` prefix so only the bit
    that actually varies shows up in the table.  ``-`` means "nothing"
    built/published; ``?`` means GitHub was unreachable.

    For non-base variants, only releases that ship a
    ``manifest-*-<variant>.json`` asset are considered; base lookups
    reject any asset that has a variant-ish suffix so a mofed
    publish doesn't satisfy a base query.  (Matches the filter logic
    in _find_release_url so `target list`, `target fetch`, and
    `target show` agree on what's available.)
    """
    from ltvm_pkg.target_config import OUTPUT_DIR

    prefix = f"{target}-{arch}-"

    def _trim(tag: str) -> str:
        return tag[len(prefix):] if tag.startswith(prefix) else tag

    tag_file = OUTPUT_DIR / target / arch / ".ltvm-release-tag"
    if tag_file.exists():
        raw_local = tag_file.read_text().strip()
        if kernel_signature and kernel_signature not in raw_local:
            local = "-"
        elif variant != "base" and not raw_local.endswith(f"-{variant}"):
            local = "-"
        elif variant == "base" and _variant_suffix_in_tag(raw_local):
            local = "-"
        else:
            local = _trim(raw_local)
    else:
        local = "-"

    if all_releases is None:
        remote = "?"
    else:
        arch_match = f"-{arch}-"
        remote = "-"
        for rel in all_releases:
            tag = rel.get("tag_name", "")
            if tag != target and not tag.startswith(target + "-"):
                continue
            # Require a manifest asset matching the variant.  This is
            # the same rule _find_release_url uses, so list/fetch/show
            # agree on availability.
            manifest_match = False
            for a in rel.get("assets", []):
                name = a.get("name", "")
                if not name.startswith(f"manifest-{target}{arch_match}"):
                    continue
                if not name.endswith(".json"):
                    continue
                if variant == "base":
                    stem = name[: -len(".json")]
                    last_seg = stem.rsplit("-", 1)[-1]
                    # Variant suffixes are alphabetic; kvers end in digits.
                    if last_seg and not any(
                        ch.isdigit() for ch in last_seg
                    ):
                        continue
                else:
                    if not name.endswith(f"-{variant}.json"):
                        continue
                manifest_match = True
                break
            if not manifest_match:
                continue
            if kernel_signature and not _release_matches_kernel(
                rel, kernel_signature, arch
            ):
                continue
            remote = _trim(tag)
            break

    return (local, remote)


def _variant_suffix_in_tag(tag: str) -> str | None:
    """Heuristic: does ``tag`` look like it ends with ``-<variant>``?

    Only used to reject a base-variant ``local`` claim when the stored
    .ltvm-release-tag was written by a variant fetch.  A bare kver
    (digits+dots+underscore) returns None; ``rocky9-x86_64-...-mofed``
    returns ``"mofed"``.
    """
    last = tag.rsplit("-", 1)[-1]
    if last and not any(ch.isdigit() for ch in last):
        return last
    return None


def cmd_targets(args: argparse.Namespace) -> int:
    use_json = args.json
    names = list_targets()

    # One API call is enough to answer every row -- releases list is
    # target-agnostic, we just filter client-side.  Network failure
    # degrades to "?" in the Remote column rather than aborting.
    all_releases: list | None
    try:
        resp = _gh_api("releases")
        all_releases = resp if isinstance(resp, list) else [resp]
    except Exception:
        all_releases = None

    rows: list[dict[str, Any]] = []
    for name in names:
        try:
            tc = TargetConfig(name)
        except ValueError as e:
            rows.append({"name": name, "error": f"error: {e}"})
            continue
        declared = tc.declared_kernels()
        declared_variants = ["base", *tc.declared_variants()]
        for kname in declared:
            signature = _kernel_release_signature(kname)
            # Emit one header-style row per kernel with blank Variants;
            # each variant then gets its own row below so "base" reads
            # explicitly alongside any declared variants (instead of
            # being the implicit interpretation of the kernel row).
            rows.append(
                {
                    "name": name,
                    "arch": tc.arch,
                    "status": tc.status,
                    "kernel": kname,
                    "variant": None,  # header row
                    "is_default": kname == tc.default_kernel,
                    "server": tc.lustre_mode != LustreMode.CLIENT,
                    "default_kernel": tc.default_kernel,
                    "lustre_mode": tc.lustre_mode.value,
                    "available": "",
                    "built": False,
                    "local_release": "-",
                    "remote_release": "-",
                }
            )
            for variant in declared_variants:
                # Honor variant kernel-pin: a pinned variant only
                # surfaces under its single declared kernel (see
                # lustre_test_vms_v2-stp).
                if (
                    variant != "base"
                    and kname not in tc.applicable_kernels(variant)
                ):
                    continue
                local, remote = _release_status(
                    name, tc.arch, all_releases,
                    kernel_signature=signature, variant=variant,
                )
                # "Built" here = a variant-specific image meta exists on
                # disk.  The kernel meta is variant-independent, so
                # checking image.meta is a better proxy for "this
                # variant is actually ready to run on this kernel".
                if variant == "base":
                    built = tc.meta_path("kernel", kname).exists()
                else:
                    img_meta = (
                        tc.image_output_dir(kname, variant=variant)
                        / "meta.json"
                    )
                    built = img_meta.exists()
                if built:
                    avail = "ready"
                elif remote not in ("-", "?"):
                    avail = "fetch"
                else:
                    avail = "build"
                behind = (
                    local not in ("-", "?")
                    and remote not in ("-", "?")
                    and local != remote
                )
                if behind:
                    avail = f"{avail}!"
                rows.append(
                    {
                        "name": name,
                        "arch": tc.arch,
                        "status": tc.status,
                        "kernel": kname,
                        "variant": variant,
                        # Default is a per-kernel property; attach it to
                        # the kernel's header row only so JSON consumers
                        # can match `is_default==True` to "exactly one
                        # default kernel".
                        "is_default": False,
                        "server": tc.lustre_mode != LustreMode.CLIENT,
                        "default_kernel": tc.default_kernel,
                        "lustre_mode": tc.lustre_mode.value,
                        "available": avail,
                        "built": built,
                        "local_release": local,
                        "remote_release": remote,
                    }
                )

    if use_json:
        print(json.dumps(rows, indent=2))
        return EXIT_OK

    if not rows:
        print("No targets configured.")
        return EXIT_OK

    hdr = (
        f"{'Local':<6} {'Remote':<7} {'Target':<12} {'Arch':<8} "
        f"{'Variants':<30} {'Mode':<16} Default?"
    )
    print(hdr)
    print("-" * len(hdr))
    prev_key: tuple[str, str] | None = None
    prev_kernel_key: tuple[str, str, str] | None = None
    has_experimental = False
    has_behind = False
    has_unreachable = False
    for r in rows:
        if "kernel" not in r:
            print(f"{r['name']:<12} {r.get('error', '')}")
            prev_key = None
            prev_kernel_key = None
            continue
        default_mark = "yes" if r["is_default"] else ""
        is_header = r["variant"] is None

        if is_header:
            # Per-kernel header row: no Local/Remote/Variants cells.
            local_col = "-"
            remote_col = "-"
        else:
            local_col = "yes" if r["built"] else "-"
            remote_raw = r["remote_release"]
            if remote_raw == "?":
                remote_col = "?"
                has_unreachable = True
            elif remote_raw == "-":
                remote_col = "-"
            else:
                remote_col = "yes"
            if (
                r["built"]
                and r["local_release"] not in ("-", "?")
                and remote_raw not in ("-", "?")
                and r["local_release"] != remote_raw
            ):
                local_col = "yes!"
                has_behind = True

        key = (r["name"], r["arch"])
        kernel_key = (r["name"], r["arch"], r["kernel"])
        if key == prev_key:
            name_col = ""
            arch_col = ""
            mode_col = ""
        else:
            marker = "*" if r["status"] != "working" else ""
            if marker:
                has_experimental = True
            name_col = f"{r['name']}{marker}"
            arch_col = r["arch"]
            mode_col = r["lustre_mode"]
        # Kernel and variant fold into a single Variants column: a
        # kernel-header row prints the kernel name; per-variant rows
        # below indent with a leading tree glyph to show they're nested
        # under that kernel.
        if is_header:
            variants_col = r["kernel"]
            default_col = default_mark
        else:
            variants_col = f"  {r['variant']}"
            default_col = ""
        print(
            f"{local_col:<6} {remote_col:<7} {name_col:<12} {arch_col:<8} "
            f"{variants_col:<30} {mode_col:<16} "
            f"{default_col}"
        )
        prev_key = key
        prev_kernel_key = kernel_key
    if has_experimental or has_behind or has_unreachable:
        print()
        if has_experimental:
            print("* experimental -- may not build or boot cleanly")
        if has_unreachable:
            print("? github unreachable -- remote status unknown")
        if has_behind:
            print(
                "yes! = local copy differs from latest release -- "
                "`sudo ltvm target fetch --replace <target>` to refresh"
            )
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: target show (one-target detail view)
# ------------------------------------------------------------------


def cmd_target_show(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target_args(args, use_json)
    if err is not None:
        return err
    assert tc is not None

    try:
        resp = _gh_api("releases")
        all_releases: list | None = resp if isinstance(resp, list) else [resp]
    except Exception:
        all_releases = None

    kernels = []
    for kname in tc.declared_kernels():
        signature = _kernel_release_signature(kname)
        local, remote = _release_status(
            tc.name, tc.arch, all_releases, kernel_signature=signature
        )
        built = tc.meta_path("kernel", kname).exists()
        if built:
            avail = "ready"
        elif remote not in ("-", "?"):
            avail = "fetch"
        else:
            avail = "build"
        kernels.append({
            "kernel": kname,
            "is_default": kname == tc.default_kernel,
            "available": avail,
            "built": built,
            "local_release": local,
            "remote_release": remote,
        })

    payload = {
        "name": tc.name,
        "status": tc.status,
        "arch": tc.arch,
        "os_family": tc.os_family,
        "os_name": tc.os_name,
        "os_version": tc.os_version,
        "container_image": tc.container_image,
        "lustre_mode": tc.lustre_mode.value,
        "default_mem": tc.default_mem,
        "default_kernel": tc.default_kernel,
        "kernels": kernels,
        "output_dir": str(tc.output_dir),
    }

    if use_json:
        print(json.dumps(payload, indent=2))
        return EXIT_OK

    print(f"target:           {payload['name']}"
          + (f"  ({payload['status']})" if payload['status'] != 'working' else ""))
    print(f"arch:             {payload['arch']}")
    print(f"os:               {payload['os_family']} / "
          f"{payload['os_name']} {payload['os_version']}")
    print(f"container image:  {payload['container_image']}")
    print(f"lustre mode:      {payload['lustre_mode']}")
    print(f"default mem:      {payload['default_mem']} MB")
    print(f"output dir:       {payload['output_dir']}")
    print()
    print("kernels:")
    for k in kernels:
        mark = "  (default)" if k["is_default"] else ""
        print(f"  {k['available']:<8} {k['kernel']}{mark}")
        if k["local_release"] != "-":
            print(f"            local:  {k['local_release']}")
        if k["remote_release"] not in ("-", "?"):
            print(f"            remote: {k['remote_release']}")
    return EXIT_OK


# ------------------------------------------------------------------
# ------------------------------------------------------------------
# Subcommand: target export (bootable-disk packaging)
# ------------------------------------------------------------------


def cmd_target_export(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(
        use_json,
        hint="export uses losetup + mount; run: sudo ltvm target export ...",
    )
    if err is not None:
        return err

    tc, terr = _load_target_args(args, use_json)
    if terr is not None:
        return terr
    assert tc is not None

    from ltvm_pkg.image_export import export_image

    kernel = getattr(args, "kernel", None)
    kernel_name = tc.resolve_kernel(kernel)
    fmt = args.format
    ext = "qcow2" if fmt == "qcow2" else "raw"
    if args.output:
        out = Path(args.output).expanduser().resolve()
    else:
        out = tc.image_output_dir(kernel) / f"bootable-{kernel_name}.{ext}"

    try:
        result = export_image(
            tc, kernel, out, image_format=fmt, force=args.force,
        )
    except FileExistsError as e:
        return _error(str(e), use_json,
                      hint="Re-run with --force to overwrite")
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        return _error(str(e), use_json)

    payload = {
        "target": tc.name,
        "kernel": kernel_name,
        "format": fmt,
        "path": str(result),
        "size_mb": round(result.stat().st_size / (1024 * 1024), 1),
    }
    _output(payload, use_json)
    return EXIT_OK


# Subcommand: validate (Lustre compatibility gate)
# ------------------------------------------------------------------


# Exit codes used by cmd_validate.  "refuse" is a first-class
# failure (1); "error" is reserved for parse / IO problems (2) so
# scripts can distinguish "Lustre says no" from "we couldn't even
# tell".
_VALIDATE_EXIT = {
    "ok": EXIT_OK,
    "best_effort": EXIT_OK,
    "refuse": EXIT_ERROR,
    "error": EXIT_NOT_FOUND,
}


def _validation_result_to_dict(r: ValidationResult) -> dict[str, Any]:
    return {
        "status": r.status,
        "mode": r.mode.value if r.mode is not None else None,
        "kernel_version": r.kernel_version,
        "matched_in": r.matched_in,
        "message": r.message,
    }


def cmd_validate(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target_args(args, use_json)
    if err is not None:
        return err
    assert tc is not None

    lustre_arg = getattr(args, "lustre_tree", None)
    if lustre_arg is None:
        default = Path.home() / "lustre-release"
        lustre_arg = str(default)
    lustre_tree, err_msg = _resolve_lustre_tree(lustre_arg)
    if err_msg:
        return _error(
            err_msg,
            use_json,
            hint="Pass --lustre-tree /path/to/lustre-release",
        )
    assert lustre_tree is not None

    kernel = getattr(args, "kernel", None)
    resolved_kernel = tc.resolve_kernel(kernel)
    kbt = tc.kernel_output_dir(kernel=resolved_kernel) / "build-tree"
    result = validate_target(tc, lustre_tree, kernel_build_tree=kbt)
    exit_code = _VALIDATE_EXIT[result.status]
    force = args.force_compat

    if use_json:
        print(json.dumps(_validation_result_to_dict(result), indent=2))
    else:
        tag = f"[{result.status}]"
        if result.status == "refuse" and force:
            print(f"--force-compat: {tag} {result.message}")
        else:
            print(f"{tag} {result.message}")

    if result.status == "refuse" and force:
        return EXIT_OK
    return exit_code


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
    result = validate_target(tc, lustre_tree, kernel_build_tree=kernel_build_tree)
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


# ------------------------------------------------------------------
# Runtime: VM management
# ------------------------------------------------------------------


def _vm_call(fn: Any, ns: argparse.Namespace, use_json: bool) -> int:
    """Call a vm_commands function, catching SystemExit and VMNotFound.

    Honors the return code of the wrapped function so handlers like
    cmd_doctor can signal "issues found" via a non-zero exit.
    """
    from ltvm_pkg.vm_state import VMNotFound

    try:
        rc = fn(ns)
        return rc if isinstance(rc, int) else EXIT_OK
    except SystemExit as e:
        return int(e.code) if e.code is not None else EXIT_ERROR
    except VMNotFound as e:
        return _error(str(e), use_json)
    except FileNotFoundError as e:
        return _error(str(e), use_json)


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


def cmd_vm_start(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_start as _start

    return _vm_call(_start, args, use_json)


def cmd_vm_stop(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_stop as _stop

    return _vm_call(_stop, args, use_json)


def cmd_list(args: argparse.Namespace) -> int:
    use_json = args.json
    from ltvm_pkg.vm_commands import cmd_list as _list

    return _vm_call(_list, args, use_json)


def cmd_console_log(args: argparse.Namespace) -> int:
    use_json = args.json
    from ltvm_pkg.vm_commands import cmd_console_log as _log

    return _vm_call(_log, args, use_json)


def cmd_crash_collect(args: argparse.Namespace) -> int:
    use_json = args.json
    from ltvm_pkg.vm_commands import cmd_crash_collect as _crash_collect

    return _vm_call(_crash_collect, args, use_json)


def cmd_nmi(args: argparse.Namespace) -> int:
    use_json = args.json
    from ltvm_pkg.vm_commands import cmd_nmi as _nmi

    return _vm_call(_nmi, args, use_json)


def cmd_snapshot(args: argparse.Namespace) -> int:
    use_json = args.json
    from ltvm_pkg.vm_commands import cmd_snapshot as _snapshot

    return _vm_call(_snapshot, args, use_json)


def cmd_restore(args: argparse.Namespace) -> int:
    use_json = args.json
    from ltvm_pkg.vm_commands import cmd_restore as _restore

    return _vm_call(_restore, args, use_json)


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
