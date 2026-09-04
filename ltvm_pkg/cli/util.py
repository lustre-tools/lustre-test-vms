"""Shared helpers for ltvm CLI submodules.

Output formatting, error emission, target loading, and small utilities
used across command implementations.  Other cli submodules import from
here; nothing here imports from another cli submodule (to keep the
dependency graph cycle-free).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import sys
from pathlib import Path
from typing import Any

from ltvm_pkg.paths import load_meta_safe
from ltvm_pkg.target_config import TargetConfig as _TargetConfig
from ltvm_pkg.target_config import list_targets as _list_targets

# TargetConfig / list_targets are re-exported on ltvm_pkg.cli so that
# tests can patch them at a stable location (``patch.object(cli_mod,
# "TargetConfig", ...)``).  Helpers here look those names up through
# ltvm_pkg.cli at call time so the monkey-patched value wins, matching
# the pre-split behavior when everything lived in cli.py.
def _cli_attr(name: str) -> Any:
    import ltvm_pkg.cli as _cli

    return getattr(_cli, name)

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_FOUND = 2


def host_arch() -> str:
    """Return the host CPU architecture, normalized for ltvm.

    Linux reports ``aarch64`` and ``x86_64``; macOS reports ``arm64``
    and ``x86_64``.  We fold ``arm64`` into ``aarch64`` so the rest of
    the codebase (artifact paths, release asset names) has a single
    spelling.  Other values pass through unchanged.
    """
    m = platform.machine()
    return "aarch64" if m in ("aarch64", "arm64") else m


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
    name: str | None,
    use_json: bool,
    arch: str | None = None,
    variant: str = "base",
) -> tuple[_TargetConfig | None, int | None]:
    """Load a TargetConfig, returning (config, None) or
    (None, exit_code) on failure.

    Handles the "no target given" case explicitly: ``_reconcile_target_args``
    resolves positional vs --target but leaves ``args.target`` as ``None``
    when neither was supplied.  Without this guard, ``TargetConfig(None)``
    crashes with ``TypeError: PosixPath / NoneType`` deep inside __init__,
    which is useless to the user.
    """
    TargetConfig = _cli_attr("TargetConfig")
    list_targets = _cli_attr("list_targets")
    if not name:
        targets = list_targets()
        hint = (
            f"Available targets: {', '.join(targets)}"
            if targets
            else "No targets configured"
        )
        code = _emit_error(
            "target required (pass a positional target or --target)",
            use_json,
            hint=hint,
            code=EXIT_NOT_FOUND,
        )
        return None, code
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
) -> tuple[_TargetConfig | None, int | None]:
    """Load TargetConfig from args.target + optional args.arch + --variant.

    Applies CLI param overrides (e.g. --mofed-version) onto the variant
    so they fold into the input hash.
    """
    variant = getattr(args, "variant", "base") or "base"
    arch = getattr(args, "arch", None) or host_arch()
    tc, err = _load_target(
        args.target, use_json, arch=arch, variant=variant
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


def _container_status(target_config: _TargetConfig) -> dict[str, Any]:
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


def _local_lustre_version(
    tc: _TargetConfig, kernel: str | None, variant: str
) -> str | None:
    """Read the baked Lustre version from the target's image meta.

    Returns ``None`` when no image is on disk (pre-fetch / pre-build)
    or when the image was built with ``--no-lustre``.  Used by
    :func:`_print_target_header` so the header reflects what's
    currently sitting in ``artifacts/<target>/``.
    """
    try:
        img_dir = tc.image_output_dir(kernel, variant=variant)
    except Exception:
        return None
    meta_path = img_dir / "meta.json"
    meta = load_meta_safe(meta_path)
    if not isinstance(meta, dict):
        return None
    v = meta.get("lustre_version")
    if not isinstance(v, str) or not v:
        return None
    # Reject the historical "2.8.0 (in-kernel)" stub: LNet modules like
    # ko2iblnd.ko carry a legacy MODULE_VERSION from the in-tree-Lustre
    # era, and an older image_build scan picked whichever .ko rglob
    # returned first.  Show "?" instead of known-wrong data so the
    # header doesn't lie.  Newer builds scan lustre.ko first and avoid
    # writing this value at all.
    if "in-kernel" in v:
        return None
    return v


def _lustre_tree_version(tree: Path | str) -> str | None:
    """Read the Lustre version from a source tree.

    Prefers ``LUSTRE-VERSION-FILE`` (generated, present in release
    tarballs and after a build) over the ``LUSTRE-VERSION-GEN`` script
    so we don't fork a subprocess on every header print.  Returns
    ``None`` if the tree has neither.
    """
    tree = Path(tree)
    vf = tree / "LUSTRE-VERSION-FILE"
    if vf.is_file():
        try:
            text = vf.read_text().strip()
        except OSError:
            return None
        # Format: "LUSTRE_VERSION = 2.17.51_dirty"
        _, _, val = text.partition("=")
        val = val.strip()
        if val:
            return val
    gen = tree / "LUSTRE-VERSION-GEN"
    if gen.is_file():
        import subprocess

        try:
            r = subprocess.run(
                [str(gen)],
                cwd=str(tree),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def _print_target_header(
    tc: _TargetConfig,
    kernel: str | None = None,
    variant: str = "base",
    action: str = "Target",
    lustre_version: str | None = None,
) -> None:
    """Print a two-line target-description header.

    Shared by ``target fetch`` / ``build all`` / ``target delete``
    (and anything else that wants to show the user what target
    they're about to operate on).  ``action`` lets callers pick the
    lead verb -- "Fetching", "Building", "Deleting", "Target".

    ``lustre_version`` may be passed explicitly (e.g. a fetch has
    just resolved the release's manifest); otherwise the helper
    falls back to whatever is baked into the target's local image
    meta.  ``?`` is shown when nothing is known, so the field is
    always present and never silently missing.

    Callers suppress this in ``--json`` mode; the helper just prints.
    """
    # Prefer the short/user-facing kernel name (as declared in
    # targets.yaml) over ``tc.resolve_kernel()`` -- resolve_kernel
    # returns the on-disk ``<short>-<uname>`` directory name when the
    # artifact is already built, which is noisy in a header.
    short = kernel or tc.default_kernel
    if lustre_version is None:
        lustre_version = _local_lustre_version(tc, kernel, variant)
    lv = lustre_version or "?"
    print(
        f"{action}: {tc.name} ({tc.os_name} {tc.os_version}, "
        f"{tc.arch}, {tc.lustre_mode.value})"
    )
    print(f"  kernel={short}  variant={variant}  lustre={lv}")


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
# Release-tag bookkeeping
# ------------------------------------------------------------------
#
# Which published release is on disk is tracked per (kernel, variant),
# not per (target, arch).  A single file per arch conflated artifact
# sets that are designed to coexist -- kernels/<k>/ and images/<k>/ are
# per-kernel -- so fetching a second kernel read as a "divergent
# release" and was refused, and the remedy fetch itself suggested
# (--replace) rmtree'd the whole arch directory, taking the first
# kernel's artifacts with it.

LEGACY_RELEASE_TAG = ".ltvm-release-tag"
RELEASE_TAG_DIR = ".ltvm-release-tags"

# All of these take ``root`` -- the <artifacts>/<target>/<arch> dir --
# explicitly rather than deriving it from the global ARTIFACTS_DIR.
# Callers already hold it (TargetConfig.output_dir is exactly this),
# and reaching for the global instead writes into the real artifacts
# tree under tests that patch only the TargetConfig.


def release_tag_dir(root: Path) -> Path:
    return Path(root) / RELEASE_TAG_DIR


def kver_from_release_tag(
    tag: str, target: str, arch: str, variant: str = "base"
) -> str:
    """Extract the kernel-version core of a release tag.

    Tags are ``<target>-<arch>-<kver>[-<variant>]``.
    """
    core = tag.strip()
    prefix = f"{target}-{arch}-"
    if core.startswith(prefix):
        core = core[len(prefix):]
    if variant != "base" and core.endswith(f"-{variant}"):
        core = core[: -(len(variant) + 1)]
    return core


def release_tag_file(root: Path, kver: str, variant: str = "base") -> Path:
    # "/" cannot appear in a kver, but be defensive: this becomes a
    # filename.
    return release_tag_dir(root) / f"{variant}__{kver.replace('/', '_')}"


def migrate_legacy_release_tag(root: Path, target: str, arch: str) -> None:
    """Move a pre-per-kernel .ltvm-release-tag into the new layout.

    One-time and best-effort: a tree fetched by an older ltvm should
    not suddenly read as "nothing fetched".
    """
    legacy = Path(root) / LEGACY_RELEASE_TAG
    if not legacy.is_file():
        return
    try:
        tag = legacy.read_text().strip()
        if tag:
            # The legacy file records one tag for the whole arch and
            # doesn't say which variant wrote it, so recover that from
            # the tag's own suffix -- filing a mofed tag under base
            # would make a base query claim the variant's release.
            variant = "base"
            from ltvm_pkg.release_package import _declared_variant_names

            for v in _declared_variant_names(target, arch):
                if tag.endswith(f"-{v}"):
                    variant = v
                    break
            else:
                # Not a variant this ltvm declares.  Fall back to the
                # shape rule, applied to the part after
                # "<target>-<arch>-": a kver's last dashed segment
                # always carries a digit, so a purely alphabetic tail
                # is a variant name we no longer know about.  Require
                # more than one segment, or a single-segment kver
                # (which need not contain a digit) reads as a variant.
                core = kver_from_release_tag(tag, target, arch)
                segs = core.split("-")
                if len(segs) > 1 and not any(ch.isdigit() for ch in segs[-1]):
                    variant = segs[-1]
            kver = kver_from_release_tag(tag, target, arch, variant)
            dest = release_tag_file(root, kver, variant)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                dest.write_text(tag + "\n")
        legacy.unlink()
    except OSError:
        pass


def read_release_tag(
    root: Path, target: str, arch: str, kver: str, variant: str = "base"
) -> str:
    """Release tag recorded for this (kernel, variant), or ""."""
    migrate_legacy_release_tag(root, target, arch)
    try:
        return release_tag_file(root, kver, variant).read_text().strip()
    except OSError:
        return ""


def write_release_tag(
    root: Path, target: str, arch: str, tag: str, variant: str = "base"
) -> None:
    kver = kver_from_release_tag(tag, target, arch, variant)
    f = release_tag_file(root, kver, variant)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(tag + "\n")


def released_kvers(
    root: Path, target: str, arch: str, variant: str = "base"
) -> list[str]:
    """kvers with a recorded release tag for this variant."""
    migrate_legacy_release_tag(root, target, arch)
    d = release_tag_dir(root)
    if not d.is_dir():
        return []
    pre = f"{variant}__"
    return sorted(
        p.name[len(pre):] for p in d.iterdir()
        if p.is_file() and p.name.startswith(pre)
    )
