"""ltvm clean -- smart pruner for stale per-target artifacts.

Distinct from ``ltvm target clean`` (the per-target wipe-it-all hammer
in build.py): this command walks artifacts/<target>/<arch>/{kernels,
images}/, identifies superseded kernel builds, off-list (no longer in
targets.yaml ``kernels.available``) kernel groups, orphan images
(no matching kernel) and superseded image files (image_store), and
previews them.  Default is dry-run; pass ``--apply`` to actually delete.

Always preserved (unless --force):
  - The target's default kernel (latest within its short-prefix group).
  - Any variant-pinned kernel (latest within its short-prefix group).

Always preserved, --force or not: any image file a VM overlay on this
host is backed by, the current image of every image directory, and any
kernel a VM on this host boots.

A full scan (no target named) also offers the dead ltvm podman images
(podman_gc.dead_tags) and lists VMs left stopped for --idle-days, which
it never removes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ltvm_pkg import build_volumes, podman_gc
from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_OK,
    _error,
)
from ltvm_pkg.paths import load_meta_safe
from ltvm_pkg.podman_run import rmtree as podman_rmtree
from ltvm_pkg.target_config import kernel_dir_version_key


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
        s
        for s in declared_shorts
        if full_dirname == s or full_dirname.startswith(s + "-")
    ]
    if not matches:
        return full_dirname
    return max(matches, key=len)


def _file_age_days(path: Path) -> float | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
    return (datetime.now(tz=timezone.utc) - dt).total_seconds() / 86400.0


def _scan_target(
    target: str,
    arch: str,
    *,
    keep: int,
    older_than_days: float | None,
    force: bool,
    referenced: tuple[set[str], list[str]] | None = None,
    vm_kernels: set[str] | None = None,
) -> _TargetReport | None:
    """Walk artifacts/<target>/<arch>/{kernels,images} and produce a
    report.  Returns None when the target/arch dir doesn't exist.

    ``referenced`` is image_store.referenced_images(), computed here
    when not given.  While any overlay's backing file is unknown, no
    image is offered for removal.
    """
    from ltvm_pkg.image_store import (
        current_image,
        image_files,
        referenced_images,
    )
    from ltvm_pkg.target_config import ARTIFACTS_DIR, DEFAULT_VARIANT

    arch_dir = ARTIFACTS_DIR / target / arch
    if not arch_dir.exists():
        return None

    report = _TargetReport(target=target, arch=arch)
    refs, unknown_refs = (
        referenced if referenced is not None else referenced_images()
    )
    if vm_kernels is None:
        vm_kernels = _vm_kernel_dirs()

    def _in_use(files: list[Path]) -> bool:
        return bool(unknown_refs) or any(
            os.path.realpath(f) in refs for f in files
        )

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
        # Version order, as TargetConfig.resolve_kernel picks "latest":
        # as strings, 553.155.1 sorts below 553.89.1.
        dirs_sorted = sorted(dirs, key=lambda p: kernel_dir_version_key(p.name))
        on_list = prefix in declared_shorts
        is_protected_group = prefix in protected_shorts

        if on_list:
            # Keep the N most recent in this group, mark the rest
            # superseded.  --keep 0 sweeps the whole group (still
            # blocked from touching protected groups unless --force).
            kept_count = (
                max(keep, 1) if is_protected_group and not force else keep
            )
            if kept_count > 0:
                doomed = (
                    dirs_sorted[:-kept_count]
                    if kept_count <= len(dirs_sorted)
                    else []
                )
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
            if os.path.realpath(d) in vm_kernels:
                report.skipped += 1
                continue
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
        # Survival is decided per-image below, from the kernel dir
        # each image is named after: kernel_alive_on_disk and
        # kernel_pruned together say the same thing a precomputed
        # "surviving kernels" set used to.
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
            if base_meta.exists() or image_files(kdir):
                image_dirs.append((DEFAULT_VARIANT, kdir))
            for sub in sorted(kdir.iterdir()):
                if sub.is_dir():
                    image_dirs.append((sub.name, sub))

            for variant_name, idir in image_dirs:
                if kernel_orphan:
                    reason = f"orphan image (no kernel for {kdir.name!r})"
                elif kernel_pruned:
                    reason = f"image of pruned kernel {kdir.name!r}"
                else:
                    # Kernel survives: only the image files a rebuild
                    # superseded, and only once no overlay uses them.
                    current = current_image(idir)
                    if current is None:
                        continue
                    for f in image_files(idir):
                        if f == current:
                            continue
                        if _in_use([f]):
                            report.skipped += 1
                            continue
                        age = _file_age_days(f)
                        if older_than_days is not None and (
                            age is None or age < older_than_days
                        ):
                            continue
                        report.candidates.append(
                            _Candidate(
                                target=target,
                                arch=arch,
                                kind=f"image-file[{variant_name}]",
                                path=f,
                                bytes=f.stat().st_size,
                                reason="superseded image; no VM overlay "
                                "is backed by it",
                                age_days=age,
                            )
                        )
                    continue

                # rmtree of the base dir takes its variant subdirs too.
                tree_files = [
                    f
                    for d in [idir, *(p for p in idir.rglob("*") if p.is_dir())]
                    for f in image_files(d)
                ]
                if _in_use(tree_files):
                    report.skipped += 1
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


def _vm_kernel_dirs() -> set[str]:
    """Real paths of the kernel dirs the VMs on this host boot from."""
    from ltvm_pkg import vm_state

    dirs: set[str] = set()
    for name in vm_state.VMInfo.all_names():
        try:
            kernel = vm_state.VMInfo.load(name).kernel
        except (vm_state.VMNotFound, ValueError, OSError):
            continue
        if kernel:
            dirs.add(os.path.realpath(Path(kernel).parent))
    return dirs


def _allocated_bytes(paths: list[Path]) -> int:
    total = 0
    for p in paths:
        try:
            total += p.stat().st_blocks * 512
        except OSError:
            pass
    return total


def _idle_vms(idle_days: float) -> list[dict[str, Any]]:
    """Stopped VMs nothing has booted, deployed to or written for *idle_days*.

    Listed for the user to decide on, never removed: a VM is state, and
    an idle one may be kept on purpose.
    """
    from ltvm_pkg import vm_state
    from ltvm_pkg.qemu_run import is_running

    clusters: dict[str, str] = {}
    for cname in vm_state.ClusterInfo.all_names():
        try:
            for n in vm_state.ClusterInfo.load(cname).get_nodes():
                clusters[n.name] = cname
        except Exception:
            continue

    now = datetime.now(tz=timezone.utc).timestamp()
    idle: list[dict[str, Any]] = []
    for name in vm_state.VMInfo.all_names():
        try:
            vm = vm_state.VMInfo.load(name)
        except (vm_state.VMNotFound, ValueError, OSError):
            continue
        if is_running(vm):
            continue
        files = [
            vm.overlay_path,
            *sorted(vm_state.OVERLAYS.glob(f"{name}-disk*.img")),
            *(vm.disk_paths() if vm.disk_dir else []),
        ]
        last = max(vm.created, vm.last_boot, vm.last_deploy)
        for f in files:
            try:
                last = max(last, int(f.stat().st_mtime))
            except OSError:
                pass
        days = (now - last) / 86400.0
        if days < idle_days:
            continue
        idle.append(
            {
                "name": name,
                "idle_days": round(days, 1),
                "bytes": _allocated_bytes(files),
                "cluster": clusters.get(name),
            }
        )
    return idle


def _print_idle_vms(idle: list[dict[str, Any]], idle_days: float) -> None:
    from ltvm_pkg import vm_state

    total = sum(v["bytes"] for v in idle)
    print(
        f"VMs stopped and untouched for {idle_days:g}+ days "
        f"({_format_bytes(total)} of disk; clean never removes a VM):"
    )
    for v in sorted(idle, key=lambda v: -v["idle_days"]):
        where = f"  cluster {v['cluster']}" if v["cluster"] else ""
        print(
            f"  {v['name']:<24} {v['idle_days']:>5.0f}d "
            f"{_format_bytes(v['bytes']):>10}{where}"
        )
    idle_names = {v["name"] for v in idle}
    whole: list[str] = []
    for cname in sorted({v["cluster"] for v in idle if v["cluster"]}):
        try:
            members = {
                n.name for n in vm_state.ClusterInfo.load(cname).get_nodes()
            }
        except Exception:
            continue
        if members <= idle_names:
            whole.append(cname)
    singles = [
        v["name"] for v in idle if not v["cluster"] or v["cluster"] not in whole
    ]
    for cname in whole:
        print(f"  remove: ltvm cluster destroy {cname}")
    if singles:
        print(f"  remove: ltvm destroy {' '.join(sorted(singles))}")


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


@dataclass
class _VolumeReport:
    prune: list[dict[str, Any]] = field(default_factory=list)
    keep: list[dict[str, Any]] = field(default_factory=list)
    checked: bool = True


def _volume_report(
    target_arg: str | None, deleting: list[Path], user_zfs: bool
) -> _VolumeReport:
    """What macOS build volumes `ltvm clean` would remove, and keeps."""
    from ltvm_pkg.target_config import ARTIFACTS_DIR

    rep = _VolumeReport()
    if user_zfs or not build_volumes.is_macos():
        return rep
    scope = [ARTIFACTS_DIR / target_arg] if target_arg else None
    dead = build_volumes.dead_kernel_volumes(scope)
    going = build_volumes.kernel_volumes_under(deleting)
    vols = build_volumes.list_volumes()
    if dead is None or going is None or vols is None:
        rep.checked = False
        return rep
    sizes = build_volumes.volume_sizes() if vols else {}
    seen: set[str] = set()
    for v, why in dead:
        seen.add(v.name)
        rep.prune.append(
            {"name": v.name, "bytes": sizes.get(v.name), "reason": why}
        )
    for v in going:
        if v.name not in seen:
            seen.add(v.name)
            rep.prune.append(
                {
                    "name": v.name,
                    "bytes": sizes.get(v.name),
                    "reason": "its kernel is removed in this run",
                }
            )
    for v in vols:
        if v.kind == "ccache" and (
            target_arg is None
            or target_arg in v.name.removeprefix(build_volumes.CCACHE_PREFIX)
        ):
            rep.keep.append(
                {
                    "name": v.name,
                    "bytes": sizes.get(v.name),
                    "reason": f"ccache, capped at {build_volumes.CCACHE_MAXSIZE}",
                }
            )
    return rep


def _podman_counts(images: list[str], volumes: list[str]) -> str:
    parts = []
    if images or not volumes:
        parts.append(
            f"{len(images)} podman image{'' if len(images) == 1 else 's'}"
        )
    if volumes:
        parts.append(
            f"{len(volumes)} podman volume{'' if len(volumes) == 1 else 's'}"
        )
    return " and ".join(parts)


def _print_volumes(rep: _VolumeReport) -> None:
    def size(b: int | None) -> str:
        return _format_bytes(b) if b is not None else "?"

    if not rep.checked:
        print(
            "Build volumes: not checked -- podman could not be asked "
            "(is the podman machine running?)"
        )
        print()
        return
    if rep.prune:
        print("Kernel build-tree volumes to remove (macOS):")
        for v in rep.prune:
            print(f"  {v['name']:<60} {size(v['bytes']):>10}  {v['reason']}")
        print()
    if rep.keep:
        print("Build volumes kept (macOS):")
        for v in rep.keep:
            print(f"  {v['name']:<60} {size(v['bytes']):>10}  {v['reason']}")
        print()


def _scan_user_zfs(target_arg: str | None) -> _TargetReport:
    """This user's own ZFS builds whose kernel is gone from artifacts.

    ZFS falls back to ~/.cache/ltvm/zfs/ for a user who cannot write the
    shared kernel artifacts; each build there is ~900 MB, and nothing
    else removes one when its kernel is pruned.
    """
    from ltvm_pkg.target_config import ARTIFACTS_DIR
    from ltvm_pkg.zfs_build import user_cache_root

    report = _TargetReport(target=target_arg or "*", arch="*")
    root = user_cache_root() / "zfs"
    if not root.is_dir():
        return report
    for tdir in sorted(p for p in root.iterdir() if p.is_dir()):
        if target_arg and tdir.name != target_arg:
            continue
        for adir in sorted(p for p in tdir.iterdir() if p.is_dir()):
            for kdir in sorted(p for p in adir.iterdir() if p.is_dir()):
                kernel = ARTIFACTS_DIR / tdir.name / adir.name / "kernels"
                if (kernel / kdir.name).is_dir():
                    continue
                report.candidates.append(
                    _Candidate(
                        target=tdir.name,
                        arch=adir.name,
                        kind="user-zfs",
                        path=kdir,
                        bytes=_dir_size_bytes(kdir),
                        reason=f"kernel {kdir.name} is no longer in "
                        f"{ARTIFACTS_DIR}",
                        age_days=_entry_age_days(kdir),
                    )
                )
    return report


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
    if getattr(args, "user_zfs", False):
        # Only this user's cache: a slot user on a shared host has no
        # business pruning the shared artifacts, and cannot anyway.
        targets = []
        reports.append(_scan_user_zfs(target_arg))

    from ltvm_pkg.image_store import referenced_images

    referenced: tuple[set[str], list[str]] = (
        referenced_images() if targets else (set(), [])
    )
    if referenced[1]:
        print(
            f"  WARNING: cannot tell which base image the overlay of "
            f"{', '.join(referenced[1])} is backed by; keeping every image",
            file=sys.stderr,
        )
    vm_kernels = _vm_kernel_dirs() if targets else set()
    for t in targets:
        for a in _arches_for_target(t, arch_flag):
            r = _scan_target(
                t,
                a,
                keep=keep,
                older_than_days=older_than_days,
                force=force,
                referenced=referenced,
                vm_kernels=vm_kernels,
            )
            if r is not None:
                reports.append(r)

    all_candidates = [c for r in reports for c in r.candidates]
    total_bytes = sum(c.bytes for c in all_candidates)

    # Host-wide extras, only on a full scan of the shared artifacts.
    full_scan = not target_arg and not getattr(args, "user_zfs", False)
    podman_dead: list[tuple[podman_gc.TagInfo, str]] = []
    idle: list[dict[str, Any]] = []
    idle_days = float(getattr(args, "idle_days", 14))
    if full_scan:
        if shutil.which("podman"):
            now = datetime.now(tz=timezone.utc).timestamp()
            podman_dead = [
                (t, why)
                for t, why in podman_gc.dead_tags()
                if older_than_days is None
                or (now - t.created) / 86400.0 >= older_than_days
            ]
        idle = _idle_vms(idle_days)

    # macOS build volumes: the kernel volumes nothing serves any more
    # (within the target named, if one is) and those of the kernels this
    # run deletes -- a copy should not outlive what it copies.  ccache
    # is shown, never pruned: it is a live cache with its own cap.
    volumes = _volume_report(
        target_arg,
        [c.path for c in all_candidates],
        getattr(args, "user_zfs", False),
    )

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
                if c.path.is_dir() and not c.path.is_symlink():
                    podman_rmtree(c.path)
                elif c.kind.startswith("image-file["):
                    # A versioned support marker belongs to this exact image file.
                    from ltvm_pkg.image_store import remove_image

                    remove_image(c.path)
                else:
                    c.path.unlink()
                removed.append(c)
            except OSError as e:
                apply_errors.append((c.path, str(e)))
    removed_podman: list[str] = []
    removed_volumes: list[str] = []
    if apply:
        for t, _why in podman_dead:
            if podman_gc.remove_tag(t.tag):
                removed_podman.append(t.tag)
            else:
                apply_errors.append(
                    (Path(t.tag), "podman rmi refused (a container uses it?)")
                )
        for v in volumes.prune:
            if build_volumes.remove_volume(v["name"]):
                removed_volumes.append(v["name"])
            else:
                apply_errors.append(
                    (
                        Path(v["name"]),
                        "podman volume rm refused: a container uses it",
                    )
                )

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
        "errors": [{"path": str(p), "error": msg} for p, msg in apply_errors],
        "total_bytes": total_bytes,
        "podman_images": [
            {
                "tag": t.tag,
                "bytes": t.size,
                "reason": why,
                "age_days": round(
                    (datetime.now(tz=timezone.utc).timestamp() - t.created)
                    / 86400.0,
                    1,
                ),
            }
            for t, why in podman_dead
        ],
        "podman_volumes": volumes.prune,
        "podman_volumes_kept": volumes.keep,
        "podman_volumes_checked": volumes.checked,
        "removed_podman": removed_podman,
        "removed_volumes": removed_volumes,
        "idle_vms": idle,
    }

    if use_json:
        print(json.dumps(payload, indent=2))
        return EXIT_ERROR if apply_errors else EXIT_OK

    if idle:
        _print_idle_vms(idle, idle_days)
        print()

    if podman_dead:
        print("Dead ltvm podman images (sizes include layers they share):")
        for t, why in podman_dead:
            print(f"  {t.tag:<44} {_format_bytes(t.size):>10}  {why}")
        print()

    _print_volumes(volumes)

    if not all_candidates:
        if not podman_dead and not volumes.prune:
            print("Nothing to prune.")
        elif apply:
            print(f"Removed {_podman_counts(removed_podman, removed_volumes)}.")
        else:
            print("Re-run with --apply to remove them.")
        return EXIT_ERROR if apply_errors else EXIT_OK

    # Human table
    hdr = (
        f"{'target':<10} {'arch':<8} {'kind':<18} "
        f"{'age':>6} {'size':>10}  path / reason"
    )
    print(hdr)
    print("-" * len(hdr))
    for c in all_candidates:
        age = f"{c.age_days:.0f}d" if c.age_days is not None else "?"
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
                f"freed {_format_bytes(total_bytes)}"
                + (
                    f", and {_podman_counts(removed_podman, removed_volumes)}."
                    if removed_podman or removed_volumes
                    else "."
                )
            )
        return EXIT_ERROR if apply_errors else EXIT_OK

    print(
        f"Would free {_format_bytes(total_bytes)} across "
        f"{len(all_candidates)} entries.  Re-run with --apply to remove."
    )
    return EXIT_OK
