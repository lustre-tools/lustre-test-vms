"""argcomplete dynamic completers for the ltvm CLI.

Each completer returns a list of candidate strings given the current
prefix.  argcomplete filters by prefix itself, so we just return every
candidate we know about.  All of these are called during tab completion,
where an unhandled exception just produces no completions -- so we wrap
everything defensively.  A broken target registry shouldn't make tab
hang or dump a traceback into the user's prompt.
"""

from __future__ import annotations

import argparse
from typing import Any


def _safe(fn):  # type: ignore[no-untyped-def]
    """Wrap a completer so any exception degrades to 'no completions'."""

    def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            return list(fn(*args, **kwargs))
        except Exception:
            return []

    return wrapper


@_safe
def complete_targets(
    prefix: str = "",
    parsed_args: argparse.Namespace | None = None,
    **kwargs: Any,
) -> list[str]:
    from .target_config import list_targets

    return list_targets()


@_safe
def complete_vms(
    prefix: str = "",
    parsed_args: argparse.Namespace | None = None,
    **kwargs: Any,
) -> list[str]:
    from .vm_state import VMInfo

    return VMInfo.all_names()


@_safe
def complete_clusters(
    prefix: str = "",
    parsed_args: argparse.Namespace | None = None,
    **kwargs: Any,
) -> list[str]:
    from .vm_state import ClusterInfo

    return ClusterInfo.all_names()


@_safe
def complete_kernels(
    prefix: str = "",
    parsed_args: argparse.Namespace | None = None,
    **kwargs: Any,
) -> list[str]:
    """Complete --kernel values.  Scopes to the target if one is parsed."""
    return _kernels_for(
        getattr(parsed_args, "target", None) if parsed_args else None
    )


def _kernels_for(target: str | None) -> list[str]:
    """Kernels *target* declares, or the union across all targets.

    The union is what makes `--kernel` useful when it is typed before
    the target -- which argparse allows, so completion has to.
    """
    from .target_config import TargetConfig, list_targets

    if target:
        return list(TargetConfig(target).declared_kernels())
    seen: set[str] = set()
    out: list[str] = []
    for name in list_targets():
        try:
            for k in TargetConfig(name).declared_kernels():
                if k not in seen:
                    seen.add(k)
                    out.append(k)
        except Exception:
            continue
    return out


@_safe
def complete_variants(
    prefix: str = "",
    parsed_args: argparse.Namespace | None = None,
    **kwargs: Any,
) -> list[str]:
    from .target_config import TargetConfig, list_targets

    target = getattr(parsed_args, "target", None) if parsed_args else None
    names: set[str] = {"base"}
    targets = [target] if target else list_targets()
    for t in targets:
        try:
            for v in TargetConfig(t).variants().keys():
                names.add(v)
        except Exception:
            continue
    return sorted(names)


@_safe
def complete_arches(
    prefix: str = "",
    parsed_args: argparse.Namespace | None = None,
    **kwargs: Any,
) -> list[str]:
    """Complete --arch.  Canonical names only, not the aliases.

    ``arm64`` and ``amd64`` are accepted by the CLI (normalized on the
    way in), but offering both spellings of one architecture would make
    the list look like four choices instead of two.
    """
    from .cross_compile import supported_arches

    return supported_arches()


@_safe
def complete_snapshot_tags(
    prefix: str = "",
    parsed_args: argparse.Namespace | None = None,
    **kwargs: Any,
) -> list[str]:
    """Complete a snapshot tag for `vm restore` / `vm snapshot --delete`.

    Needs the VM name, which is the preceding positional -- so this
    yields nothing until the user has typed one.  Reads the overlay with
    ``qemu-img snapshot -l -U``: the -U is what makes this safe to run
    against a *running* VM's disk, which tab completion must be.
    """
    import subprocess

    from .vm_commands import _parse_snapshot_tags
    from .vm_state import QEMU_IMG, VMInfo

    name = getattr(parsed_args, "name", None) if parsed_args else None
    if not name:
        return []
    overlay = VMInfo.load(name).overlay_path
    if not overlay.exists():
        return []
    # Bounded: a completer that hangs hangs the user's terminal.
    r = subprocess.run(
        [QEMU_IMG, "snapshot", "-l", "-U", str(overlay)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return sorted(_parse_snapshot_tags(r.stdout or ""))


@_safe
def complete_zfs_versions(
    prefix: str = "",
    parsed_args: argparse.Namespace | None = None,
    **kwargs: Any,
) -> list[str]:
    """Complete --zfs-version: the target's pin, then anything built.

    The target's configured version comes first because it is the one
    the user most likely wants to name explicitly; the rest are versions
    this host has actually built or cached, which is a better list than
    a hardcoded set of OpenZFS releases that would rot.
    """
    from .target_config import TargetConfig, list_targets
    from .zfs_build import DEFAULT_ZFS_VERSION

    target = getattr(parsed_args, "target", None) if parsed_args else None
    out: list[str] = []

    def add(v: str | None) -> None:
        if v and v not in out:
            out.append(v)

    for name in [target] if target else list_targets():
        try:
            add(TargetConfig(name).zfs_version)
        except Exception:
            continue
    add(DEFAULT_ZFS_VERSION)
    for v in _cached_zfs_versions():
        add(v)
    return out


def _cached_zfs_versions() -> list[str]:
    """ZFS releases whose tarballs are in the global cache."""
    from .zfs_build import tarball_cache_dir

    cache = tarball_cache_dir()
    if not cache.is_dir():
        return []
    versions: set[str] = set()
    for f in cache.iterdir():
        # zfs-2.4.0.tar.gz -> 2.4.0
        stem = f.name
        if not stem.startswith("zfs-"):
            continue
        for suffix in (".tar.gz", ".tar.xz", ".tar.bz2"):
            if stem.endswith(suffix):
                versions.add(stem[len("zfs-") : -len(suffix)])
                break
    return sorted(versions)


@_safe
def complete_mofed_versions(
    prefix: str = "",
    parsed_args: argparse.Namespace | None = None,
    **kwargs: Any,
) -> list[str]:
    """Complete --mofed-version from the variants' declared params.

    The flag overrides a variant's ``mofed_version`` param, so the
    versions worth offering are the ones targets.yaml already names.
    """
    from .target_config import TargetConfig, list_targets

    target = getattr(parsed_args, "target", None) if parsed_args else None
    out: list[str] = []
    for name in [target] if target else list_targets():
        try:
            variants = TargetConfig(name).variants()
        except Exception:
            continue
        for spec in variants.values():
            params = getattr(spec, "params", None) or {}
            v = params.get("mofed_version")
            if v and str(v) not in out:
                out.append(str(v))
    return out


@_safe
def complete_fetch_filter(
    prefix: str = "",
    parsed_args: argparse.Namespace | None = None,
    **kwargs: Any,
) -> list[str]:
    """Complete `target fetch`'s optional release filter.

    The filter is matched against release names, so the target's
    declared kernels are the useful candidates -- the same list
    ``--kernel`` offers, which is what a filter is usually narrowing to.
    """
    return _kernels_for(
        getattr(parsed_args, "target", None) if parsed_args else None
    )


# Roles a node spec accepts, per vm_cluster.parse_node_spec.
CLUSTER_ROLES = ("mgs", "mds", "oss", "client")


@_safe
def complete_cluster_members(
    prefix: str = "",
    parsed_args: argparse.Namespace | None = None,
    **kwargs: Any,
) -> list[str]:
    """Complete the role-or-node argument of `cluster exec` / `ssh`.

    Both accept either a role, which fans out across every node holding
    it, or a single node name.  Roles come first because fanning out is
    the common case.  Scoped to the cluster already named, so it offers
    that cluster's roles rather than all four.
    """
    name = getattr(parsed_args, "name", None) if parsed_args else None
    if not name:
        return []
    return _cluster_role_and_node_names(name)


@_safe
def complete_cluster_specs(
    prefix: str = "",
    parsed_args: argparse.Namespace | None = None,
    **kwargs: Any,
) -> list[str]:
    """Complete `cluster create`'s specs, which start with a bare target.

    The first of these positionals may be an OS target; the rest are
    ``roles:vm[:disks]`` triples.  Only the target is completable -- a
    node name is one the user is inventing -- so targets are offered
    while the word has no ':' in it, and the role prefixes that can
    legally start a spec once it does not look like a target.

    Offering the roles is worth it because `mgs+mds:` is easy to
    misremember (the separator is '+', not ','), and a wrong role is
    refused only after the whole command line is typed.
    """
    from .target_config import list_targets

    # Past the first word, a target is no longer accepted.
    already = list(getattr(parsed_args, "specs", None) or [])
    if already and prefix and already[-1] == prefix:
        already.pop()
    if not already and ":" not in prefix:
        return [*list_targets(), *CLUSTER_ROLES]
    return list(CLUSTER_ROLES)


def _cluster_role_and_node_names(cluster: str) -> list[str]:
    """Roles held by *cluster*, then its member VM names.

    `cluster exec` and `cluster ssh` take either, and roles come first
    because fanning out across a role is the common case.
    """
    from .vm_state import ClusterInfo

    info = ClusterInfo.load(cluster)
    roles: list[str] = []
    names: list[str] = []
    # A hand-edited .cluster file could hold anything; _safe turns the
    # resulting TypeError into "no completions", which is the right
    # outcome here and cheaper than validating the shape.
    for node in info.nodes:
        for role in node.get("roles") or []:
            if role not in roles:
                roles.append(role)
        name = node.get("name")
        if name and name not in names:
            names.append(name)
    return [*sorted(roles), *names]


def complete_directories(
    prefix: str = "",
    parsed_args: argparse.Namespace | None = None,
    **kwargs: Any,
) -> Any:
    """Directories only, for arguments that name a tree rather than a file.

    argcomplete's default for an argument with no completer is
    ``FilesCompleter``, which offers regular files too -- noise for
    ``--lustre-tree`` and friends, where only a directory is ever valid.

    Not wrapped in ``_safe``: argcomplete's directory completer returns
    its own types and handles its own quoting, so we hand it through
    untouched rather than flattening it to a list of strings.
    """
    from argcomplete.completers import DirectoriesCompleter

    return DirectoriesCompleter()(
        prefix=prefix, parsed_args=parsed_args, **kwargs
    )
