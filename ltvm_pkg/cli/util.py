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
    name: str,
    use_json: bool,
    arch: str | None = None,
    variant: str = "base",
) -> tuple[_TargetConfig | None, int | None]:
    """Load a TargetConfig, returning (config, None) or
    (None, exit_code) on failure."""
    TargetConfig = _cli_attr("TargetConfig")
    list_targets = _cli_attr("list_targets")
    if name is None:
        targets = list_targets()
        hint = (
            f"Available targets: {', '.join(targets)}"
            if targets
            else "No targets configured"
        )
        code = _emit_error(
            "missing target (e.g. rocky9)",
            use_json,
            hint=hint,
            code=EXIT_ERROR,
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
