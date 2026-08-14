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
    from ltvm_pkg.target_config import ARTIFACTS_DIR

    prefix = f"{target}-{arch}-"

    def _trim(tag: str) -> str:
        return tag[len(prefix):] if tag.startswith(prefix) else tag

    tag_file = ARTIFACTS_DIR / target / arch / ".ltvm-release-tag"
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


def _filter_rows(
    rows: list[dict[str, Any]], scope: str | None,
) -> list[dict[str, Any]]:
    """Apply the ``local`` / ``remote`` filter to the row list.

    Drops variant rows that don't match, and only keeps a kernel
    header row when at least one variant row beneath it survives
    (so empty kernel sections don't linger).  Error rows (no
    ``kernel`` key) pass through unchanged -- the user should still
    see parse failures.
    """
    if scope is None:
        return rows

    def keep(r: dict[str, Any]) -> bool:
        if scope == "local":
            return bool(r.get("built"))
        # scope == "remote": '-' = no release, '?' = unreachable,
        # anything else is a real release tag.
        return r.get("remote_release") not in (None, "-", "?")

    kept: list[dict[str, Any]] = []
    pending_header: dict[str, Any] | None = None
    for r in rows:
        if "kernel" not in r:
            kept.append(r)
            pending_header = None
            continue
        if r["variant"] is None:
            pending_header = r
            continue
        if keep(r):
            if pending_header is not None:
                kept.append(pending_header)
                pending_header = None
            kept.append(r)
    return kept


def cmd_targets(args: argparse.Namespace) -> int:
    use_json = args.json
    scope = getattr(args, "list_filter", None)
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

    # Pick which arches to render per target: every arch the target
    # actually exists for -- arches with local artifacts on disk AND
    # arches that have a published (remote) release.  Without the remote
    # half, a target published only for x86_64 rendered under the host
    # arch (e.g. aarch64) with every cell dashed, hiding its real x86_64
    # release.  --arch pins to a single arch.  A target with nothing
    # built or published anywhere falls back to the host arch so it still
    # shows one row inviting a local build.
    from ltvm_pkg.cli.util import host_arch
    from ltvm_pkg.target_config import ARTIFACTS_DIR
    explicit_arch = getattr(args, "arch", None)
    host_a = host_arch()
    _KNOWN_ARCHES = ("x86_64", "aarch64")

    def _archs_for(target_name: str) -> list[str]:
        if explicit_arch:
            return [explicit_arch]
        archs: set[str] = set()
        # arches with local build artifacts
        target_root = ARTIFACTS_DIR / target_name
        if target_root.is_dir():
            for entry in target_root.iterdir():
                if entry.is_dir() and (entry / "kernels").is_dir():
                    archs.add(entry.name)
        # arches with a published release (tag "<target>-<arch>-...")
        for rel in all_releases or []:
            tag = rel.get("tag_name", "")
            for a in _KNOWN_ARCHES:
                if tag.startswith(f"{target_name}-{a}-"):
                    archs.add(a)
        # Nothing anywhere -- show the host arch so the target still
        # lists one row (available action: build it here).
        if not archs:
            archs.add(host_a)
        return sorted(archs)

    TargetConfig = _cli_attr("TargetConfig")
    rows: list[dict[str, Any]] = []
    for name in names:
        for arch in _archs_for(name):
            try:
                tc = TargetConfig(name, arch=arch)
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
                        "os_name": tc.os_name,
                        "os_version": tc.os_version,
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

    # Preserve the pre-filter GH-unreachable signal so a `list remote`
    # with no hits on unreachable network doesn't look indistinguishable
    # from "nothing is published".
    gh_unreachable = all_releases is None
    rows = _filter_rows(rows, scope)

    if use_json:
        print(json.dumps(rows, indent=2))
        return EXIT_OK

    if not rows:
        if scope == "local":
            print("No targets with local builds.")
        elif scope == "remote":
            if gh_unreachable:
                print(
                    "github unreachable -- remote status unknown; "
                    "try `ltvm target list` without a filter"
                )
            else:
                print("No targets with published remote releases.")
        else:
            print("No targets configured.")
        return EXIT_OK

    # ---- Text renderer: one block per (target, arch) ----
    #
    # The old flat table doubled every kernel with a "base" sub-row and
    # parked the check-mark columns four columns away from what they
    # qualified.  Blocks read top-down instead: a header line names the
    # target and says in words what it is; each kernel is ONE line with
    # its status cells adjacent; non-base variants indent beneath the
    # kernels they apply to.  "base" is the kernel line itself and is
    # never printed as a row.
    _MODE_HUMAN = {
        "server_ldiskfs": "Lustre server (ldiskfs backend)",
        "server_zfs": "Lustre server (ZFS backend)",
        "client": "Lustre client only",
    }
    CHECK = "✓"
    has_experimental = False
    has_behind = False
    has_unreachable = False
    has_no_lustre = False

    def _cells(r: dict[str, Any]) -> tuple[str, str, str]:
        """Local / Remote / State cells for a variant row."""
        nonlocal has_behind, has_unreachable, has_no_lustre
        local_col = CHECK if r["built"] else "-"
        remote_raw = r["remote_release"]
        if remote_raw == "?":
            remote_col = "?"
            has_unreachable = True
        elif remote_raw == "-":
            remote_col = "-"
        else:
            remote_col = CHECK
        if (
            r["built"]
            and r["local_release"] not in ("-", "?")
            and remote_raw not in ("-", "?")
            and r["local_release"] != remote_raw
        ):
            local_col = f"{CHECK}!"
            has_behind = True
        # '✓*' -> image is built but has no Lustre baked in.
        # Stacks with the '✓!' behind marker.
        if r.get("lustre_missing") and local_col.startswith(CHECK):
            local_col = f"{local_col}*"
            has_no_lustre = True
        return local_col, remote_col, r.get("available", "")

    # Group rows into (target, arch) blocks, preserving order.  Each
    # block holds kernel entries; each entry holds its base row (the
    # kernel line) and any non-base variant rows (indented lines).
    blocks: list[dict[str, Any]] = []
    cur_block: dict[str, Any] | None = None
    cur_kernel: dict[str, Any] | None = None
    for r in rows:
        if "kernel" not in r:
            blocks.append({"error": r})
            cur_block = None
            cur_kernel = None
            continue
        key = (r["name"], r["arch"])
        if cur_block is None or cur_block["key"] != key:
            cur_block = {"key": key, "meta": r, "kernels": []}
            cur_kernel = None
            blocks.append(cur_block)
        if r["variant"] is None:
            cur_kernel = {"header": r, "base": None, "variants": []}
            cur_block["kernels"].append(cur_kernel)
            continue
        # Variant rows normally follow their kernel's header row, but a
        # local/remote scope filter can drop the header -- resynthesize
        # an entry from the variant row itself in that case.
        if cur_kernel is None or cur_kernel["header"]["kernel"] != r["kernel"]:
            cur_kernel = {"header": r, "base": None, "variants": []}
            cur_block["kernels"].append(cur_kernel)
        if r["variant"] == "base":
            cur_kernel["base"] = r
        else:
            cur_kernel["variants"].append(r)

    # Arch scoping: text output shows only the host arch by default --
    # other arches collapse into a closing note instead of interleaving
    # with the blocks the user actually runs here.  --all-arches (or an
    # explicit --arch) widens the view; JSON always carries all rows.
    all_arches = bool(getattr(args, "all_arches", False))
    show_all = all_arches or explicit_arch is not None
    error_blocks = [b for b in blocks if "error" in b]
    real_blocks = [b for b in blocks if "error" not in b]
    hidden: list[tuple[str, str]] = []
    if not show_all:
        kept_blocks = []
        for blk in real_blocks:
            if blk["key"][1] == host_a:
                kept_blocks.append(blk)
            else:
                hidden.append(blk["key"])
        real_blocks = kept_blocks

    # Never interleave arches: render one section per arch, each with
    # its own heading when more than one arch is shown.
    arches_present = sorted({b["key"][1] for b in real_blocks})
    sectioned = len(arches_present) > 1

    ordered: list[dict[str, Any]] = list(error_blocks)
    for a in arches_present:
        ordered.extend(b for b in real_blocks if b["key"][1] == a)

    first = True
    prev_arch: str | None = None
    for blk in ordered:
        if not first:
            print()
        first = False
        if "error" in blk:
            e = blk["error"]
            print(f"{e['name']}: {e.get('error', '')}")
            continue
        if sectioned and blk["key"][1] != prev_arch:
            prev_arch = blk["key"][1]
            print(f"===== {prev_arch} =====")
            print()
        meta = blk["meta"]
        marker = "*" if meta["status"] != "working" else ""
        if marker:
            has_experimental = True
        mode_h = _MODE_HUMAN.get(meta["lustre_mode"], meta["lustre_mode"])
        os_bits = " ".join(
            str(meta[k]) for k in ("os_name", "os_version") if meta.get(k)
        )
        os_part = f", {os_bits}" if os_bits else ""
        print(f"{meta['name']}{marker} ({meta['arch']}) -- {mode_h}{os_part}")
        print(f"  {'Kernel':<28} {'Local':<7} {'Remote':<7} State")
        for entry in blk["kernels"]:
            hdr_row = entry["header"]
            kname = hdr_row["kernel"]
            is_default = (
                hdr_row["is_default"]
                or kname == hdr_row.get("default_kernel")
            )
            label = f"{kname} (default)" if is_default else kname
            if entry["base"] is not None:
                local_col, remote_col, state = _cells(entry["base"])
            else:
                # Base row filtered out by a local/remote scope: still
                # print the kernel line as context for its variants.
                local_col, remote_col, state = "-", "-", ""
            print(f"  {label:<28} {local_col:<7} {remote_col:<7} {state}")
            for vr in entry["variants"]:
                local_col, remote_col, state = _cells(vr)
                print(
                    f"    {vr['variant']:<26} {local_col:<7} {remote_col:<7} "
                    f"{state}"
                )

    print()
    print(
        f"Local = built on this machine, Remote = published release; "
        f"{CHECK} yes, - no"
    )
    print(
        "State: ready = usable now | fetch = prebuilt available "
        "(`ltvm target fetch <target>`) | build = build locally "
        "(`ltvm build all <target>`)"
    )
    if has_experimental:
        print("* experimental -- may not build or boot cleanly")
    if has_unreachable:
        print("? github unreachable -- remote status unknown")
    if has_behind:
        print(
            "✓! = local copy differs from latest release -- "
            "`sudo ltvm target fetch --replace <target>` to refresh"
        )
    if has_no_lustre:
        print(
            "✓* = image does NOT have Lustre baked in.  Lustre "
            "must be installed (`ltvm deploy-lustre`) before this "
            "image can use Lustre, or rebuild with `ltvm build "
            "image <target> --lustre-tree <path>` (drop "
            "--no-lustre) or `ltvm target fetch <target>`."
        )
    if hidden:
        other_arches = ", ".join(sorted({a for _, a in hidden}))
        print()
        print(
            f"Note: {len(hidden)} listing(s) for other arches "
            f"({other_arches}) hidden -- "
            f"`ltvm target list --all-arches` shows them."
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

    tc, terr = _load_target_args(args, use_json)
    if terr is not None:
        return terr
    assert tc is not None

    # losetup + mount need root; prime sudo up front (single password
    # prompt) instead of demanding the whole command run under sudo.
    # JSON mode skips the prompt since interactive auth would clobber
    # the JSON stream -- sudo_run will then prompt mid-flow if needed,
    # which is acceptable for the structured-output path.
    if not use_json:
        from ltvm_pkg.priv import sudo_prime

        sudo_prime(
            "ltvm target export needs root for losetup/mount"
        )

    from ltvm_pkg.cli.util import _print_target_header
    from ltvm_pkg.image_export import export_image

    kernel = getattr(args, "kernel", None)
    kernel_name = tc.resolve_kernel(kernel)
    fmt = args.format
    ext = "qcow2" if fmt == "qcow2" else "raw"

    if not use_json:
        _print_target_header(
            tc, kernel=kernel,
            variant=getattr(args, "variant", None) or "base",
            action="Exporting",
        )
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

    # Default to cwd like every other --lustre-tree consumer does
    # (see _resolve_lustre_tree).  Previously this defaulted to
    # ~/lustre-release, which disagreed with `build all / kernel /
    # image / lustre`, `target publish` and surprised users
    # who had ``cd``'d into their tree.
    lustre_arg = getattr(args, "lustre_tree", None)
    lustre_tree, err_msg = _cli_attr("_resolve_lustre_tree")(lustre_arg)
    if err_msg:
        return _error(
            err_msg,
            use_json,
            hint="Run from a Lustre tree, or pass "
            "--lustre-tree /path/to/lustre-release",
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
