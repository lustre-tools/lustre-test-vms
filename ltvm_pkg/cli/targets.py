"""Target-management subcommands.

Covers:
  * `ltvm target list`     -- multi-target table with fetch/build hints
  * `ltvm target show`     -- single-target detail view
  * `ltvm target export`   -- bundle built image as a bootable qcow2/raw
  * `ltvm target validate` -- run the Lustre-compat gate without building

Depends on fetch.py for release-listing helpers (_find_release_url,
_release_matches_kernel, _kernel_release_signature, _gh_api).  Tests
monkey-patch those on ltvm_pkg.cli, so this submodule reaches them
via _cli_attr at call time.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ltvm_pkg.lustre_compat import ValidationResult
from ltvm_pkg.target_config import LustreMode

from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    _error,
    _load_target_args,
    _output,
    _require_root,
)


def _cli_attr(name: str) -> Any:
    """Look up ``name`` on ``ltvm_pkg.cli`` at call time."""
    import ltvm_pkg.cli as _cli

    return getattr(_cli, name)


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
            if kernel_signature and not _cli_attr("_release_matches_kernel")(
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
    names = _cli_attr("list_targets")()

    # One API call is enough to answer every row -- releases list is
    # target-agnostic, we just filter client-side.  Network failure
    # degrades to "?" in the Remote column rather than aborting.
    all_releases: list | None
    try:
        resp = _cli_attr("_gh_api")("releases")
        all_releases = resp if isinstance(resp, list) else [resp]
    except Exception:
        all_releases = None

    TargetConfig = _cli_attr("TargetConfig")
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
            signature = _cli_attr("_kernel_release_signature")(kname)
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

                # A built image can still be Lustre-less (happens when
                # someone ran `ltvm build image --no-lustre` during
                # iteration, or when the target is client-only and
                # Lustre wasn't baked).  `ltvm create` picks the base
                # image verbatim, so a Lustre-less image produces a VM
                # that can't mount anything -- the precise failure mode
                # the user hit with `pafvm`.  Record the miss so the
                # renderer can flag it.
                lustre_missing = False
                if built:
                    img_meta_path = (
                        tc.image_output_dir(kname, variant=variant)
                        / "meta.json"
                    )
                    try:
                        meta_doc = _cli_attr("load_meta_safe")(img_meta_path)
                    except Exception:
                        meta_doc = None
                    if meta_doc is not None:
                        # Only the image meta carries these fields; kernel
                        # meta does not.  Treat None/empty as "missing".
                        lv = meta_doc.get("lustre_version")
                        wl = meta_doc.get("with_lustre")
                        lustre_missing = not (lv or wl)

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
                        "lustre_missing": lustre_missing,
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
    has_no_lustre = False
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
            # 'yes?' -> image is built but has no Lustre baked in.
            # A VM created from this image can't mount Lustre.  Stacks
            # with the 'yes!' behind marker so `yes?!` is possible
            # when the image is both no-lustre AND out-of-date.
            if r.get("lustre_missing") and local_col.startswith("yes"):
                local_col = f"{local_col}?" if "?" not in local_col else local_col
                has_no_lustre = True

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
    if (has_experimental or has_behind or has_unreachable
            or has_no_lustre):
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
        if has_no_lustre:
            print(
                "yes? = image built WITHOUT Lustre baked in -- a VM "
                "created from this image can't mount Lustre.  Rebuild "
                "with `ltvm build image <target> --lustre-tree <path>` "
                "(drop --no-lustre) or `ltvm target fetch <target>`."
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
        resp = _cli_attr("_gh_api")("releases")
        all_releases: list | None = resp if isinstance(resp, list) else [resp]
    except Exception:
        all_releases = None

    kernels = []
    for kname in tc.declared_kernels():
        signature = _cli_attr("_kernel_release_signature")(kname)
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


# ------------------------------------------------------------------
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
    lustre_tree, err_msg = _cli_attr("_resolve_lustre_tree")(lustre_arg)
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
    result = _cli_attr("validate_target")(
        tc, lustre_tree, kernel_build_tree=kbt
    )
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
