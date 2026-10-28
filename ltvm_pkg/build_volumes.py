"""Podman volumes that keep macOS builds off virtiofs.

On macOS podman runs its containers in a Linux VM (``podman machine``)
and reaches every bind mount over virtiofs, which is slow for the
access pattern of a kernel-module build: each compile opens and stats
thousands of kernel headers, and ccache writes a file per object.  A
Lustre build against a kernel build-tree on virtiofs spends more time
waiting on it than compiling -- 332 s, where the same build in a Linux
VM takes 137 s, and doubling the machine's CPUs gains nothing.

Two inputs are moved onto the machine's own disk, as named volumes:

- the kernel build-tree, which is read-only and changes only when the
  kernel artifact does: copied in once per kernel (~2 min for
  1.8 GB), keyed on the artifact so a rebuilt or re-fetched kernel gets
  a new volume and the old one is removed;
- ccache, which is a cache, so losing or not sharing it is harmless.

Together they bring that build to 160 s.  The Lustre tree itself stays
a bind mount: the user edits it on macOS and deploy reads its staging
dir there.

Every step falls back to the bind mount it replaces, with a warning,
so a podman that cannot do volumes builds as before.
``LTVM_MACOS_BUILD_VOLUMES=0`` turns the volumes off.

Each volume is labelled with what it is (``ltvm.kind``) and, for a
kernel, the build-tree it was copied from (``ltvm.build-tree``), so it
can be judged by any ltvm checkout on the Mac: a kernel volume is dead
once that path is gone or holds a different kernel.  ``ltvm clean``
and ``ltvm target clean`` remove a kernel's volume with the kernel,
and ``ltvm doctor`` shows what the volumes hold.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .host_setup import is_macos

log = logging.getLogger(__name__)

# Written into the kernel volume after the copy has finished; a volume
# without it is a copy that was interrupted, and is rebuilt.
READY_MARKER = ".ltvm-volume-ready"
# macOS's own tar; see _populate.
BSDTAR = "/usr/bin/tar"
KERNEL_PREFIX = "ltvm-kernel-"
# Not "ltvm-ccache-": volumes of that name were ltvm's ccache before
# 0e4c1c9 and may still be lying around with the unreadable root that
# made it switch to a bind mount.
CCACHE_PREFIX = "ltvm-build-ccache-"
LABEL_KIND = "ltvm.kind"
LABEL_TREE = "ltvm.build-tree"
# ccache's own default, made explicit so an image that changes it
# cannot leave the volume to grow without bound.
CCACHE_MAXSIZE = "5G"


def enabled() -> bool:
    """True when builds should use volumes: macOS, not turned off."""
    return is_macos() and os.environ.get("LTVM_MACOS_BUILD_VOLUMES") != "0"


def _volume_name(*parts: str) -> str:
    # podman volume names: [a-zA-Z0-9][a-zA-Z0-9_.-]*
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", "-".join(parts))


def kernel_volume_name(build_tree: Path) -> str:
    """The volume for this kernel artifact's build-tree.

    ``ltvm-kernel-<target>-<arch>-<kver>-<where>-<what>``: *where* hashes
    the artifact's path, so two ltvm checkouts on one Mac keep a volume
    each; *what* hashes its meta.json (which a rebuild or a fetch
    rewrites) and the kernel.release and Module.symvers that Lustre
    builds against, so a changed kernel never reads an old copy.  Only
    *what* may differ between a volume and the one that supersedes it.
    """
    build_tree = build_tree.resolve()
    kdir = build_tree.parent
    where = hashlib.sha256(str(build_tree).encode()).hexdigest()[:8]
    what = hashlib.sha256()
    for f in (kdir / "meta.json", build_tree / "include/config/kernel.release"):
        with contextlib.suppress(OSError):
            what.update(f.read_bytes())
    with contextlib.suppress(OSError):
        st = (build_tree / "Module.symvers").stat()
        what.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
    # .../artifacts/<target>/<arch>/kernels/<kver>/build-tree
    parts = kdir.parts
    layout = (
        list(parts[-4:-2]) if len(parts) >= 4 and parts[-2] == "kernels" else []
    )
    return _volume_name(
        KERNEL_PREFIX.rstrip("-"),
        *layout,
        kdir.name,
        where,
        what.hexdigest()[:12],
    )


def ccache_volume_name(container_tag: str) -> str:
    return _volume_name(
        CCACHE_PREFIX.rstrip("-"), container_tag.removeprefix("ltvm-build-")
    )


def _podman(
    *args: str, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    from ltvm_pkg.host_setup import hold_podman_usage

    hold_podman_usage()
    return subprocess.run(
        ["podman", *args], capture_output=True, text=True, timeout=timeout
    )


@contextlib.contextmanager
def _lock(name: str) -> Iterator[None]:
    """Serialise ltvm processes populating the same volume."""
    path = (
        Path(tempfile.gettempdir()) / f"ltvm-volume-{os.getuid()}-{name}.lock"
    )
    with path.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _probe(image: str, mounts: list[str], script: str) -> str | None:
    """Run *script* in *image* with *mounts*; its stdout, or None."""
    cmd = ["run", "--rm", "--security-opt", "label=disable"]
    for m in mounts:
        cmd += ["-v", m]
    r = _podman(*cmd, "--entrypoint", "sh", image, "-c", script, timeout=120)
    return r.stdout if r.returncode == 0 else None


def _ready(volume: str, image: str) -> bool:
    if _podman("volume", "exists", volume).returncode != 0:
        return False
    out = _probe(
        image,
        [f"{volume}:/v:ro"],
        f"test -f /v/{READY_MARKER} && echo ready || true",
    )
    return out is not None and out.strip() == "ready"


def _populate(volume: str, build_tree: Path, image: str) -> None:
    """Copy *build_tree* into a fresh *volume*, the marker last."""
    # An incomplete copy from an interrupted run.  Not -f: that would
    # remove a container using it too; if one is, create fails below
    # and the build falls back to the bind mount.
    _podman("volume", "rm", volume)
    r = _podman(
        "volume",
        "create",
        "--label",
        f"{LABEL_KIND}=kernel",
        "--label",
        f"{LABEL_TREE}={build_tree.resolve()}",
        volume,
    )
    if r.returncode != 0:
        raise RuntimeError(f"podman volume create {volume}: {r.stderr.strip()}")
    # Through podman's API rather than virtiofs: 92 s for a 1.8 GB tree,
    # against 254 s for a cp across the bind mount.  bsdtar's own uid
    # and AppleDouble entries are not mappable in the machine, so
    # everything goes in as root (the build container's own uid).
    # macOS's bsdtar by path: the flags are its own, and a Homebrew GNU
    # tar first on PATH would refuse them on every build.
    env = {**os.environ, "COPYFILE_DISABLE": "1"}
    tar = subprocess.Popen(
        [
            BSDTAR,
            "--uid",
            "0",
            "--gid",
            "0",
            "--no-mac-metadata",
            "--no-xattrs",
            "-C",
            str(build_tree),
            "-cf",
            "-",
            ".",
        ],
        stdout=subprocess.PIPE,
        env=env,
    )
    imp = subprocess.run(
        ["podman", "volume", "import", volume, "-"],
        stdin=tar.stdout,
        capture_output=True,
        text=True,
    )
    assert tar.stdout is not None
    tar.stdout.close()
    tar_rc = tar.wait()
    if tar_rc != 0 or imp.returncode != 0:
        raise RuntimeError(
            f"copying {build_tree} into {volume} failed "
            f"(tar rc={tar_rc}, import rc={imp.returncode}): {imp.stderr.strip()}"
        )
    if _probe(image, [f"{volume}:/v"], f": > /v/{READY_MARKER}") is None:
        raise RuntimeError(f"could not mark {volume} complete")


def _remove_superseded(keep: str, build_tree: Path) -> None:
    """Remove the older volumes copied from *build_tree*.

    Without -f, which would also remove -- kill -- a build still running
    against the old kernel.  A volume in use stays, and the next build,
    or `ltvm clean`, removes it.
    """
    tree = str(build_tree.resolve())
    for v in list_volumes() or []:
        if v.name != keep and v.kind == "kernel" and v.build_tree == tree:
            if _podman("volume", "rm", v.name).returncode == 0:
                log.info("removed superseded kernel volume %s", v.name)
            else:
                log.info("kept superseded kernel volume %s: in use", v.name)


def kernel_tree_mount(build_tree: Path, image: str) -> str:
    """The ``-v`` source for /kernel: a volume when possible.

    *image* is the build container, used to check and mark the volume.

    Returns the volume name, or *build_tree* itself (the bind mount)
    when volumes are off or anything about them fails.
    """
    if not enabled():
        return str(build_tree)
    volume = kernel_volume_name(build_tree)
    try:
        with _lock(volume):
            if not _ready(volume, image):
                print(
                    f"--- Copying the kernel build-tree into podman volume "
                    f"{volume} (once per kernel, about a minute or two)...",
                    flush=True,
                )
                _populate(volume, build_tree, image)
                _remove_superseded(volume, build_tree)
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        log.warning(
            "kernel build-tree volume unavailable (%s); building against "
            "the bind mount, which is slower on macOS",
            e,
        )
        _podman("volume", "rm", volume)
        return str(build_tree)
    return volume


def ccache_mount(container_tag: str, fallback: Path) -> str:
    """The ``-v`` source for /ccache: a volume when it is writable.

    A volume whose root the container cannot write -- what podman
    machine 5.x did in 0e4c1c9 -- falls back to *fallback*, the host
    directory ccache used before.
    """
    if not enabled():
        return str(fallback)
    volume = ccache_volume_name(container_tag)
    try:
        with _lock(volume):
            if _podman("volume", "exists", volume).returncode != 0:
                r = _podman(
                    "volume",
                    "create",
                    "--label",
                    f"{LABEL_KIND}=ccache",
                    volume,
                )
                if r.returncode != 0:
                    raise RuntimeError(r.stderr.strip())
            out = _probe(
                container_tag,
                [f"{volume}:/ccache"],
                "mkdir -p /ccache/tmp && : > /ccache/tmp/.ltvm-w && echo ok",
            )
            if out is None or out.strip() != "ok":
                raise RuntimeError(f"{volume} is not writable in the container")
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        log.warning("ccache volume unavailable (%s); using %s", e, fallback)
        return str(fallback)
    return volume


@dataclass
class Volume:
    name: str
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def kind(self) -> str | None:
        """kernel, ccache, or None for a volume that is not ltvm's."""
        kind = self.labels.get(LABEL_KIND)
        if kind:
            return kind
        if self.name.startswith(KERNEL_PREFIX):
            return "kernel"
        if self.name.startswith(CCACHE_PREFIX):
            return "ccache"
        return None

    @property
    def build_tree(self) -> str | None:
        return self.labels.get(LABEL_TREE)


def list_volumes() -> list[Volume] | None:
    """ltvm's volumes, or None when podman cannot be asked.

    None rather than [] so a caller can say "not checked" -- on a Mac,
    most often because the podman machine is stopped -- instead of
    "there are none".  Only on macOS: no other host gets volumes.
    """
    if not is_macos():
        return []
    try:
        r = _podman("volume", "ls", "--format", "json", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        rows = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    vols = [
        Volume(str(row.get("Name", "")), dict(row.get("Labels") or {}))
        for row in rows
        if isinstance(row, dict)
    ]
    return [v for v in vols if v.kind in ("kernel", "ccache")]


def _under(tree: str | None, paths: Iterable[Path]) -> bool:
    if not tree:
        return False
    t = Path(tree)
    return any(t == p or p in t.parents for p in (q.resolve() for q in paths))


def why_dead(v: Volume) -> str | None:
    """Why kernel volume *v* has nothing left to serve, or None."""
    if v.kind != "kernel":
        return None
    if not v.build_tree:
        return "no record of the kernel it was copied from"
    tree = Path(v.build_tree)
    if not tree.is_dir():
        return f"its kernel is gone ({tree.parent.name})"
    if kernel_volume_name(tree) != v.name:
        return f"superseded: {tree.parent.name} was rebuilt or re-fetched"
    return None


def dead_kernel_volumes(
    under: Iterable[Path] | None = None,
) -> list[tuple[Volume, str]] | None:
    """Kernel volumes with no kernel left to serve, and why.

    *under* limits it to volumes copied from inside those directories
    (one target's artifacts); a volume of unknown origin is then left
    out, since nothing says it belongs there.  None when podman could
    not be asked.
    """
    vols = list_volumes()
    if vols is None:
        return None
    paths = list(under) if under is not None else None
    found = []
    for v in vols:
        why = why_dead(v)
        if why is None:
            continue
        if paths is not None and not _under(v.build_tree, paths):
            continue
        found.append((v, why))
    return found


def kernel_volumes_under(paths: Iterable[Path]) -> list[Volume] | None:
    """Kernel volumes copied from inside *paths*: what deleting them orphans."""
    vols = list_volumes()
    if vols is None:
        return None
    paths = list(paths)
    return [
        v for v in vols if v.kind == "kernel" and _under(v.build_tree, paths)
    ]


_UNITS = {
    "B": 1,
    "kB": 10**3,
    "KB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
}


def volume_sizes() -> dict[str, int]:
    """Bytes per volume, from `podman system df -v`; {} when unknown.

    Its volume table is text only (``--format`` and ``-v`` do not mix),
    with go-units' decimal sizes: ``1.8GB``.
    """
    try:
        r = _podman("system", "df", "-v", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {}
    if r.returncode != 0:
        return {}
    sizes: dict[str, int] = {}
    table = r.stdout.split("Local Volumes space usage:", 1)
    if len(table) != 2:
        return {}
    for line in table[1].splitlines():
        m = re.match(r"(\S+)\s+\d+\s+([0-9.]+)\s*([kKMGT]?B)\s*$", line.strip())
        if m:
            sizes[m.group(1)] = int(float(m.group(2)) * _UNITS[m.group(3)])
    return sizes


def remove_volume(name: str) -> bool:
    """Remove *name* unless a container uses it (never -f)."""
    try:
        return _podman("volume", "rm", name, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def machine_running() -> bool:
    """True when a podman machine is up.  Asks; never starts one."""
    try:
        r = _podman("machine", "list", "--format", "json", timeout=15)
        rows = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return False
    return any(isinstance(m, dict) and m.get("Running") for m in rows)


def doctor_report() -> tuple[list[str], list[str]]:
    """(warnings, info) about the build volumes, for `ltvm doctor`.

    Only on macOS, and only when the podman machine is already up:
    doctor must not start a VM to look inside it.
    """
    if not is_macos():
        return [], []
    if not machine_running():
        return [], ["build volumes: not checked (podman machine stopped)"]
    vols = list_volumes()
    if vols is None:
        return [], ["build volumes: not checked (podman did not answer)"]
    if not vols:
        return [], ["build volumes: none yet"]
    sizes = volume_sizes()

    def total(vs: list[Volume]) -> str:
        known = [sizes[v.name] for v in vs if v.name in sizes]
        return _human(sum(known)) if known else "?"

    kernels = [v for v in vols if v.kind == "kernel"]
    caches = [v for v in vols if v.kind == "ccache"]
    info = [
        f"build volumes: {len(kernels)} kernel build-tree(s), {total(kernels)}; "
        f"{len(caches)} ccache, {total(caches)} (capped at {CCACHE_MAXSIZE} each)"
    ]
    dead = [(v, why) for v in kernels if (why := why_dead(v))]
    warnings = []
    if dead:
        warnings.append(
            f"{len(dead)} kernel build-tree volume(s) with no kernel left, "
            f"{total([v for v, _ in dead])} -- `ltvm clean --apply` removes them"
        )
    return warnings, info


def _human(n: int) -> str:
    for unit, scale in (("GB", 10**9), ("MB", 10**6), ("kB", 10**3)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} B"
