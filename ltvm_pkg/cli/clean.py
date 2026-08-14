"""ltvm clean -- smart pruner for stale per-target artifacts.

Distinct from ``ltvm target clean`` (the per-target wipe-it-all hammer
in build.py): this command walks artifacts/<target>/<arch>/{kernels,
images}/, identifies superseded kernel builds, off-list (no longer in
targets.yaml ``kernels.available``) kernel groups, and orphan images
(no matching kernel), and previews them.  Default is dry-run; pass
``--apply`` to actually delete.

Always preserved (unless --force):
  - The target's default kernel (latest within its short-prefix group).
  - Any variant-pinned kernel (latest within its short-prefix group).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_OK,
    _error,
)
from ltvm_pkg.paths import load_meta_safe


@dataclass
class _Candidate:
    target: str
    arch: str
    kind: str  # "kernel" | "image"
    path: Path
    bytes: int
    reason: str
    age_days: float | None = None


@dataclass
class _TargetReport:
    target: str
    arch: str
    candidates: list[_Candidate] = field(default_factory=list)
    skipped: int = 0  # known-but-protected entries (informational only)


def _dir_size_bytes(path: Path) -> int:
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


def _meta_built_at(meta_path: Path) -> datetime | None:
    """Read built_at / build_date from a meta.json, return tz-aware UTC."""
    meta = load_meta_safe(meta_path)
    if not isinstance(meta, dict):
        return None
    for key in ("built_at", "build_date"):
        v = meta.get(key)
        if isinstance(v, str):
            try:
                dt = datetime.fromisoformat(v)
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    return None


def _entry_age_days(path: Path) -> float | None:
    """Return age in days for an artifact dir.

    Prefers meta.json's recorded build time; falls back to the dir's
    mtime so age filtering still works for legacy entries that pre-date
    the meta.json build_at field.
    """
    dt = _meta_built_at(path / "meta.json")
    if dt is None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
    delta = datetime.now(tz=timezone.utc) - dt
    return delta.total_seconds() / 86400.0


def _short_prefix(full_dirname: str, declared_shorts: list[str]) -> str:
    """Map a kernel directory name to its declared short prefix.

    Mirrors TargetConfig._short_kernel_name: prefer the longest declared
    short that matches the directory name (either exactly or as a
    ``<short>-`` prefix).  Falls back to the dirname itself when no
    declared short matches -- those entries are off-list.
    """
    matches = [
        s for s in declared_shorts
        if full_dirname == s or full_dirname.startswith(s + "-")
    ]
    if not matches:
        return full_dirname
    return max(matches, key=len)


def _scan_target(
    target: str,
    arch: str,
    *,
    keep: int,
    older_than_days: float | None,
    force: bool,
) -> _TargetReport | None:
    """Walk artifacts/<target>/<arch>/{kernels,images} and produce a
    report.  Returns None when the target/arch dir doesn't exist."""
    from ltvm_pkg.target_config import ARTIFACTS_DIR, DEFAULT_VARIANT

    arch_dir = ARTIFACTS_DIR / target / arch
    if not arch_dir.exists():
        return None

    report = _TargetReport(target=target, arch=arch)

    # Resolve declared shorts + protected shorts (default + variant pins)
    # from a TargetConfig.  If the target was removed from yaml, fall
    # back to "everything off-list" so the user can sweep it.
    declared_shorts: list[str] = []
    protected_shorts: set[str] = set()
    try:
        from ltvm_pkg.target_config import TargetConfig

        tc = TargetConfig(target, arch=arch)
        declared_shorts = list(tc.declared_kernels())
        protected_shorts.add(tc.default_kernel)
        for v in tc._variants.values():
            if v.pinned_kernel:
                protected_shorts.add(v.pinned_kernel)
    except ValueError as exc:
        if "Unknown target" in str(exc):
            # Target gone from yaml -> no protection, all groups
            # off-list, sweepable.
            pass
        else:
            # Present in yaml but failed to load (e.g. a schema
            # validation error).  Treating that like "gone" would strip
            # the default-kernel/variant-pin protection and offer
            # protected artifacts for deletion -- skip the target
            # entirely instead.
            print(
                f"  WARNING: skipping {target}/{arch}: targets.yaml "
                f"entry failed to load: {exc}",
                file=sys.stderr,
            )
            return None
    except Exception as exc:
        # Unexpected load failure: same conservative skip.
        print(
            f"  WARNING: skipping {target}/{arch}: targets.yaml "
            f"entry failed to load: {exc}",
            file=sys.stderr,
        )
        return None

    # ---- Kernels: group by short prefix, decide what's prunable ----
    kernels_dir = arch_dir / "kernels"
    on_disk_kernel_dirs: list[Path] = []
    if kernels_dir.exists():
        on_disk_kernel_dirs = sorted(
            d for d in kernels_dir.iterdir() if d.is_dir()
        )

    groups: dict[str, list[Path]] = {}
    for d in on_disk_kernel_dirs:
        prefix = _short_prefix(d.name, declared_shorts)
        groups.setdefault(prefix, []).append(d)

    # Track which kernel dir-names are scheduled for removal so we can
    # cascade their per-kernel images.
    pruned_kernel_names: set[str] = set()

    for prefix, dirs in groups.items():
        # Lex sort (matches TargetConfig.resolve_kernel's "latest"
        # picker so we keep what an unmodified `ltvm build` would pick).
        dirs_sorted = sorted(dirs, key=lambda p: p.name)
        on_list = prefix in declared_shorts
        is_protected_group = prefix in protected_shorts

        if on_list:
            # Keep the N most recent in this group, mark the rest
            # superseded.  --keep 0 sweeps the whole group (still
            # blocked from touching protected groups unless --force).
            kept_count = max(keep, 1) if is_protected_group and not force else keep
            if kept_count > 0:
                doomed = dirs_sorted[:-kept_count] if kept_count <= len(dirs_sorted) else []
            else:
                doomed = list(dirs_sorted)
            report.skipped += len(dirs_sorted) - len(doomed)
            reason = f"superseded within group {prefix!r}"
        else:
            # Off-list: every entry in the group is a removal candidate
            # (--keep doesn't preserve off-list groups by design; that's
            # the point of dropping the short from kernels.available).
            doomed = list(dirs_sorted)
            reason = f"off-list group {prefix!r} (not in kernels.available)"

        # Protected guard: never sweep the very last entry of a
        # protected group unless --force.
        if is_protected_group and not force and dirs_sorted:
            latest = dirs_sorted[-1]
            doomed = [d for d in doomed if d != latest]

        for d in doomed:
            age = _entry_age_days(d)
            if older_than_days is not None and (
                age is None or age < older_than_days
            ):
                continue
            report.candidates.append(
                _Candidate(
                    target=target,
                    arch=arch,
                    kind="kernel",
                    path=d,
                    bytes=_dir_size_bytes(d),
                    reason=reason,
                    age_days=age,
                )
            )
            pruned_kernel_names.add(d.name)

    # ---- Images: cascade with kernel removals + flag orphans ----
    images_dir = arch_dir / "images"
    if images_dir.exists():
        # Build the "kernel survives" set (on-disk kernel dirs minus
        # the ones we already scheduled for pruning).
        surviving_kernels = {
            d.name for d in on_disk_kernel_dirs if d.name not in pruned_kernel_names
        }

        for kdir in sorted(images_dir.iterdir()):
            if not kdir.is_dir():
                continue
            kernel_alive_on_disk = (kernels_dir / kdir.name).is_dir()
            kernel_pruned = kdir.name in pruned_kernel_names
            kernel_orphan = not kernel_alive_on_disk

            # Iterate the base image and any variant subdirs as
            # independently prunable units.
            image_dirs: list[tuple[str, Path]] = []
            base_meta = kdir / "meta.json"
            base_ext4 = kdir / "base.ext4"
            if base_meta.exists() or base_ext4.exists():
                image_dirs.append((DEFAULT_VARIANT, kdir))
            for sub in sorted(kdir.iterdir()):
                if sub.is_dir():
                    image_dirs.append((sub.name, sub))

            for variant_name, idir in image_dirs:
                if kernel_orphan:
                    reason = (
                        f"orphan image (no kernel for {kdir.name!r})"
                    )
                elif kernel_pruned:
                    reason = (
                        f"image of pruned kernel {kdir.name!r}"
                    )
                else:
                    # Kernel survives; image is fine.  (Aging-only
                    # cleanup of standalone images is a follow-up;
                    # mixed signals there are easy to misread.)
                    continue

                # Age filter: only kick the image out if the image
                # itself qualifies.  Otherwise we'd silently bypass the
                # filter via the kernel-side cascade.
                age = _entry_age_days(idir)
                if older_than_days is not None and (
                    age is None or age < older_than_days
                ):
                    continue

                report.candidates.append(
                    _Candidate(
                        target=target,
                        arch=arch,
                        kind=f"image[{variant_name}]",
                        path=idir,
                        bytes=_dir_size_bytes(idir),
                        reason=reason,
                        age_days=age,
                    )
                )

    return report


def _arches_for_target(target: str, arch_flag: str | None) -> list[str]:
    """Return the list of arches to scan for a target.

    --arch overrides the search.  Otherwise we scan every arch that
    actually has a directory under artifacts/<target>/, so a host that's
    built both x86_64 and aarch64 prunes both in one pass.
    """
    from ltvm_pkg.target_config import ARTIFACTS_DIR

    if arch_flag:
        return [arch_flag]
    target_dir = ARTIFACTS_DIR / target
    if not target_dir.exists():
        return []
    return sorted(d.name for d in target_dir.iterdir() if d.is_dir())


def cmd_prune(args: argparse.Namespace) -> int:
    """Smart prune of stale per-target artifacts (top-level ``ltvm clean``)."""
    use_json = args.json
    apply = bool(getattr(args, "apply", False))
    keep = int(getattr(args, "keep", 1))
    if keep < 0:
        return _error("--keep must be >= 0", use_json)
    older_than = getattr(args, "older_than", None)
    older_than_days: float | None = None
    if older_than is not None:
        try:
            older_than_days = float(older_than)
        except ValueError:
            return _error(
                f"--older-than must be a number of days, got {older_than!r}",
                use_json,
            )
        if older_than_days < 0:
            return _error("--older-than must be >= 0", use_json)
    force = bool(getattr(args, "force", False))
    arch_flag = getattr(args, "arch", None)
    target_arg = getattr(args, "target", None)

    from ltvm_pkg.target_config import list_targets

    if target_arg:
        targets = [target_arg]
    else:
        targets = list(list_targets())

    reports: list[_TargetReport] = []
    for t in targets:
        for a in _arches_for_target(t, arch_flag):
            r = _scan_target(
                t, a,
                keep=keep,
                older_than_days=older_than_days,
                force=force,
            )
            if r is not None:
                reports.append(r)

    all_candidates = [c for r in reports for c in r.candidates]
    total_bytes = sum(c.bytes for c in all_candidates)

    # ---- Apply phase ----
    # Sort deepest-first so a base-variant kdir that contains nested
    # variant subdirs has the variant subdirs removed first; the
    # ancestor's rmtree still no-ops cleanly when the dir is already
    # empty.  Any candidate whose path was already swept by a parent
    # rmtree (or never existed) is recorded as removed -- the user
    # wanted it gone, and it is gone.
    removed: list[_Candidate] = []
    apply_errors: list[tuple[Path, str]] = []
    if apply and all_candidates:
        ordered = sorted(
            all_candidates, key=lambda c: len(c.path.parts), reverse=True
        )
        for c in ordered:
            if not c.path.exists():
                removed.append(c)
                continue
            try:
                shutil.rmtree(c.path)
                removed.append(c)
            except OSError as e:
                apply_errors.append((c.path, str(e)))

    # ---- Output ----
    payload: dict[str, Any] = {
        "applied": apply,
        "candidates": [
            {
                "target": c.target,
                "arch": c.arch,
                "kind": c.kind,
                "path": str(c.path),
                "bytes": c.bytes,
                "reason": c.reason,
                "age_days": c.age_days,
            }
            for c in all_candidates
        ],
        "removed": [str(c.path) for c in removed],
        "errors": [
            {"path": str(p), "error": msg} for p, msg in apply_errors
        ],
        "total_bytes": total_bytes,
    }

    if use_json:
        print(json.dumps(payload, indent=2))
        return EXIT_ERROR if apply_errors else EXIT_OK

    if not all_candidates:
        print("Nothing to prune.")
        return EXIT_OK

    # Human table
    hdr = (
        f"{'target':<10} {'arch':<8} {'kind':<18} "
        f"{'age':>6} {'size':>10}  path / reason"
    )
    print(hdr)
    print("-" * len(hdr))
    for c in all_candidates:
        age = (
            f"{c.age_days:.0f}d" if c.age_days is not None else "?"
        )
        print(
            f"{c.target:<10} {c.arch:<8} {c.kind:<18} "
            f"{age:>6} {_format_bytes(c.bytes):>10}  {c.path}"
        )
        print(f"{'':<10} {'':<8} {'':<18} {'':>6} {'':>10}    -> {c.reason}")

    print()
    if apply:
        if apply_errors:
            print(
                f"Removed {len(removed)} of {len(all_candidates)} entries; "
                f"{len(apply_errors)} failed:"
            )
            for p, msg in apply_errors:
                print(f"  {p}: {msg}")
        else:
            print(
                f"Removed {len(removed)} entries, "
                f"freed {_format_bytes(total_bytes)}."
            )
        return EXIT_ERROR if apply_errors else EXIT_OK

    print(
        f"Would free {_format_bytes(total_bytes)} across "
        f"{len(all_candidates)} entries.  Re-run with --apply to remove."
    )
    return EXIT_OK
