"""Rebuild every published target release on this host: ``ltvm build release``.

The set to build is what GitHub publishes for one arch: each release
with a base manifest names a target and, through its kernel version, a
kernel declared in targets.yaml.

It runs in three passes, so what is likely to break breaks early:

1. preflight: every target's build container and image package
   layers, in parallel.  A package that no longer installs shows up
   here in minutes, not after an hour of kernel builds.
2. builds: ``ltvm build all`` for each target and kernel, breadth
   first (every target's first kernel before any target's second), in
   parallel slots.  Each slot has its own CPUs and its own clone of the
   Lustre tree.  Two builds of one target never run at once: they
   share the target's image tag and caches.
3. verify: each build's kernel, Lustre snapshot and image are read
   back, and a build counts only if all three match.

Finished builds go in a ledger, so a rerun with the same ltvm and
Lustre commit skips them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

GIB = 1024**3
# Measured on the #60 rebuild: about 5.4 GiB of artifacts per build,
# and a kernel build's objects plus an image's rootfs (three copies)
# in podman's storage while a slot runs.
ARTIFACT_BYTES_PER_BUILD = 6 * GIB
WORK_BYTES_PER_SLOT = 25 * GIB
# image export writes the rootfs tarball and the extracted tree to TMPDIR
TMP_BYTES_PER_SLOT = 12 * GIB
# a target's build container and image package layers, kept in podman
TARGET_STORE_BYTES = 6 * GIB
# a build's Lustre staging, kept in its slot's tree, plus the slot's
# own build
STAGING_BYTES_PER_BUILD = 1 * GIB
TREE_BYTES_PER_SLOT = 2 * GIB
# a target's Lustre ccache in the work directory, at ccache's default cap
CCACHE_BYTES_PER_TARGET = 5 * GIB
# a cross build's preflight: prebuilt tools and the copied build context
CROSS_PREFLIGHT_BYTES = 2 * GIB
STOP_GRACE_SECONDS = 120
# exit status of a build the stall watchdog stopped
STALLED = 124
MARGIN_BYTES = 10 * GIB

_IMAGE_KERNEL_RE = re.compile(
    r"^==> Building image for \S+ \(kernel=(\S+)\)\.\.\.$", re.MULTILINE
)
_STEP_RE = re.compile(
    r"^error: (Container|Kernel|Lustre|Image) (?:build|snapshot) failed",
    re.MULTILINE,
)


class ReleaseBuildError(Exception):
    """A release build cannot start; the message says why."""


@dataclass(frozen=True, order=True)
class Build:
    target: str
    kernel: str

    @property
    def name(self) -> str:
        return f"{self.target}:{self.kernel}"


@dataclass
class Result:
    build: Build
    status: str  # ok, failed, skipped, done (verified by an earlier run)
    step: str | None = None
    seconds: float = 0.0
    log: str | None = None
    problems: list[str] = field(default_factory=list)
    kernel_dir: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.build.target,
            "kernel": self.build.kernel,
            "status": self.status,
            "step": self.step,
            "seconds": round(self.seconds),
            "log": self.log,
            "problems": self.problems,
            "kernel_dir": self.kernel_dir,
        }


# ------------------------------------------------------------------
# What to build
# ------------------------------------------------------------------


def published_builds(
    releases: Iterable[dict[str, Any]],
    arch: str,
    declared: dict[str, list[str]],
    kernel_of: Callable[[str, str, str], str | None],
    is_base: Callable[[str, str, str], bool],
) -> tuple[list[Build], list[str], list[str]]:
    """The (target, kernel) pairs published for ``arch``, in the order
    targets.yaml declares them (``declared`` maps each target to its
    kernels), and the tags of releases whose manifest matched no declared
    kernel.  ``kernel_of(target, manifest_stem, arch)`` maps a base
    manifest's name to a kernel, and
    ``is_base(target, arch, manifest_stem)`` tells a base release from
    a variant's.  Also returns the tags of releases carrying ZFS, which
    a plain ``build all`` would rebuild without it.
    """
    targets = list(declared)
    by_length = sorted(targets, key=len, reverse=True)
    found: set[Build] = set()
    unmatched: list[str] = []
    with_zfs: list[str] = []
    zfs_builds: set[Build] = set()
    for rel in releases:
        # The manifest names the target, arch and kernel; the tag is
        # whatever publish was given (`target publish --tag`).
        tag = rel.get("tag_name", "")
        names = [a.get("name", "") for a in rel.get("assets", [])]
        for n in names:
            if not (n.startswith("manifest-") and n.endswith(".json")):
                continue
            stem = n[: -len(".json")]
            target = next(
                (
                    t
                    for t in by_length
                    if stem.startswith(f"manifest-{t}-{arch}-")
                ),
                None,
            )
            if target is None or not is_base(target, arch, stem):
                continue
            kver = stem[len(f"manifest-{target}-{arch}-") :]
            # A release candidate is mainline's moving `latest`, which a
            # series name's numbers would otherwise claim.
            if re.search(r"-rc\d", kver):
                if tag not in unmatched:
                    unmatched.append(tag)
                continue
            if any(m.startswith(f"zfs-{target}-{arch}-{kver}-") for m in names):
                if tag not in with_zfs:
                    with_zfs.append(tag)
                zfs_kernel = kernel_of(target, stem, arch)
                if zfs_kernel is not None:
                    zfs_builds.add(Build(target, zfs_kernel))
                continue
            kernel = kernel_of(target, stem, arch)
            if kernel is None:
                if tag not in unmatched:
                    unmatched.append(tag)
                continue
            found.add(Build(target, kernel))
    rank = {t: i for i, t in enumerate(targets)}

    def key(b: Build) -> tuple[int, int, str]:
        names = declared[b.target]
        pos = names.index(b.kernel) if b.kernel in names else len(names)
        return (rank[b.target], pos, b.kernel)

    # Another manifest of a kernel published with ZFS must not bring it
    # back: its build would replace the ZFS one without ZFS.
    return sorted(found - zfs_builds, key=key), unmatched, with_zfs


def breadth_first(builds: list[Build]) -> list[Build]:
    """Every target's first kernel, then every target's second, and so
    on: a failure common to a target shows on its first kernel, while
    the other targets still have work queued."""
    per_target: dict[str, list[Build]] = {}
    for b in builds:
        per_target.setdefault(b.target, []).append(b)
    out: list[Build] = []
    depth = max((len(v) for v in per_target.values()), default=0)
    for i in range(depth):
        out += [v[i] for v in per_target.values() if i < len(v)]
    return out


def filter_builds(builds: list[Build], only: list[str] | None) -> list[Build]:
    """Keep the builds ``--only`` names, as TARGET or TARGET:KERNEL."""
    if not only:
        return builds
    keep = []
    for b in builds:
        for spec in only:
            t, _, k = spec.partition(":")
            if b.target == t and (not k or b.kernel == k):
                keep.append(b)
                break
    return keep


# ------------------------------------------------------------------
# CPUs
# ------------------------------------------------------------------


def parse_cpulist(text: str) -> list[int]:
    """Parse a Linux cpulist such as ``0-15,64-79``."""
    cpus: list[int] = []
    for part in text.strip().split(","):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        cpus += range(int(lo), int(hi or lo) + 1)
    return cpus


def format_cpulist(cpus: list[int]) -> str:
    out: list[str] = []
    for c in sorted(cpus):
        if out and "-" in out[-1] and int(out[-1].split("-")[1]) == c - 1:
            out[-1] = f"{out[-1].split('-')[0]}-{c}"
        elif out and "-" not in out[-1] and int(out[-1]) == c - 1:
            out[-1] = f"{out[-1]}-{c}"
        else:
            out.append(str(c))
    return ",".join(out)


def numa_nodes(
    sysfs: Path = Path("/sys/devices/system/node"),
) -> list[list[int]]:
    nodes = []
    for d in sorted(sysfs.glob("node[0-9]*"), key=lambda p: int(p.name[4:])):
        try:
            cpus = parse_cpulist((d / "cpulist").read_text())
        except (OSError, ValueError):
            continue
        if cpus:
            nodes.append(cpus)
    return nodes


def _chunks(items: list[Any], n: int) -> list[list[Any]]:
    size, extra = divmod(len(items), n)
    out, i = [], 0
    for k in range(n):
        step = size + (1 if k < extra else 0)
        out.append(items[i : i + step])
        i += step
    return out


def cpu_sets(
    jobs: int,
    spec: str | None,
    available: list[int] | None,
    nodes: list[list[int]],
) -> list[list[int] | None]:
    """One CPU set per slot.  ``spec`` gives them by hand, separated by
    colons; otherwise whole NUMA nodes are shared out when there are at
    least as many as slots, and plain CPU ranges when not.  None (no
    pinning) when the platform cannot pin, or with a single slot."""
    if spec:
        sets: list[list[int] | None] = [
            parse_cpulist(s) for s in spec.split(":")
        ]
        if any(not s for s in sets):
            raise ReleaseBuildError(f"--cpus {spec} has an empty CPU set")
        if len(sets) != jobs:
            raise ReleaseBuildError(
                f"--cpus gives {len(sets)} CPU sets for {jobs} jobs"
            )
        return sets
    if available is None or jobs == 1:
        return [None] * jobs
    if jobs > len(available):
        raise ReleaseBuildError(
            f"{jobs} jobs but only {len(available)} CPUs to pin them to"
        )
    avail = set(available)
    usable = [sorted(avail & set(n)) for n in nodes]
    usable = [n for n in usable if n]
    if len(usable) >= jobs:
        return [
            sorted(c for n in group for c in n)
            for group in _chunks(usable, jobs)
        ]
    return [c for c in _chunks(sorted(avail), jobs)]


def default_jobs(available: list[int] | None) -> int:
    """One slot per 32 CPUs, at most four; one where nothing pins."""
    if available is None:
        return 1
    return max(1, min(4, len(available) // 32))


def available_cpus() -> list[int] | None:
    if sys.platform != "linux" or shutil.which("taskset") is None:
        return None
    return sorted(os.sched_getaffinity(0))


# ------------------------------------------------------------------
# Disk
# ------------------------------------------------------------------


def podman_store() -> Path | None:
    """Where podman keeps images and build layers for this user.
    CONTAINERS_STORAGE_CONF can move it off the home filesystem.  None
    when it is not on this host's filesystems: on macOS, inside the
    podman machine, and with a remote podman service."""
    if sys.platform == "darwin":
        return None
    try:
        r = subprocess.run(
            [
                "podman",
                "info",
                "--format",
                "{{.Host.ServiceIsRemote}} {{.Store.GraphRoot}}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        remote, _, root = r.stdout.strip().partition(" ")
        if r.returncode == 0 and remote == "true":
            return None
        if r.returncode == 0 and root:
            return Path(root)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return Path.home() / ".local/share/containers"


def _existing(p: Path) -> Path:
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def disk_problems(
    artifacts: list[Path],
    store: Path | None,
    work: Path,
    jobs: int,
    builds: int,
    usage: Callable[[Path], Any] = shutil.disk_usage,
    tmp: Path | None = None,
    new_targets: int = 0,
    preflight_slots: int = 0,
    ccache_bytes: int = 0,
    cross_preflight: list[Path] | None = None,
    extra: list[tuple[Path, int, str]] | None = None,
) -> list[str]:
    """What will not fit, per filesystem.  Artifacts grow by one set per
    build, in that build's ``artifacts`` directory (an archived target's
    is on cold storage); each running slot needs room for a kernel and an image in
    podman's store, for an image export in TMPDIR, and for its Lustre
    tree's build.  Podman also keeps each target's container and image
    layers (``new_targets`` have none yet), and the trees keep each
    build's staging.  The preflight needs working room in podman's
    storage for each of its ``preflight_slots`` even with no builds, and
    the targets' Lustre ccaches in the work directory grow by up to
    ``ccache_bytes``.  A cross build's preflight writes tools into
    its target's artifacts (``cross_preflight``).  ``extra`` adds (path,
    bytes, what) needs, such as kernel ccaches.  ``store`` None leaves
    podman's storage unchecked."""
    if builds == 0 and new_targets == 0 and preflight_slots == 0:
        return []
    cross_preflight = cross_preflight or []
    jobs = min(jobs, builds)
    tmp = Path(tempfile.gettempdir()) if tmp is None else tmp
    needs: dict[int, list[Any]] = {}

    def add(path: Path, n: int, what: str) -> None:
        if n <= 0:
            return
        p = _existing(path)
        dev = p.stat().st_dev
        entry = needs.setdefault(dev, [p, 0, []])
        entry[1] += n
        if what not in entry[2]:
            entry[2].append(what)

    for a in artifacts:
        add(
            a, ARTIFACT_BYTES_PER_BUILD, f"artifacts ({_existing(a).resolve()})"
        )
    for a in cross_preflight:
        add(a, CROSS_PREFLIGHT_BYTES, f"artifacts ({_existing(a).resolve()})")
    for path, n, what in extra or []:
        add(path, n, what)
    if store is not None:
        add(
            store,
            max(jobs, preflight_slots) * WORK_BYTES_PER_SLOT
            + new_targets * TARGET_STORE_BYTES,
            f"podman storage ({store})",
        )
    add(tmp, jobs * TMP_BYTES_PER_SLOT, f"temporary files ({tmp})")
    add(
        work,
        jobs * TREE_BYTES_PER_SLOT
        + builds * STAGING_BYTES_PER_BUILD
        + ccache_bytes,
        f"Lustre trees ({work})",
    )
    out = []
    for p, need, what in needs.values():
        need += MARGIN_BYTES
        free = usage(p).free
        if free < need:
            out.append(
                f"{p}: {free / GIB:.0f} GiB free, about {need / GIB:.0f} GiB "
                f"needed for {', '.join(what)}"
            )
    return out


# ------------------------------------------------------------------
# Lustre trees
# ------------------------------------------------------------------


def _git(tree: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(tree), *args], capture_output=True, text=True
    )
    if r.returncode != 0:
        raise ReleaseBuildError(
            f"git {' '.join(args)} in {tree} failed: {r.stderr.strip()}"
        )
    return r.stdout.strip()


def lustre_head(src: Path) -> str:
    """The commit to build.  A release is built from a commit, so the
    tree must have no changes to tracked files."""
    if _git(src, "status", "--porcelain", "--untracked-files=no"):
        raise ReleaseBuildError(
            f"{src} has uncommitted changes; a release is built from a "
            f"commit, so commit or stash them"
        )
    return _git(src, "rev-parse", "HEAD")


def lock_paths(
    artifacts: Path, work: Path, home: Path | None = None
) -> list[tuple[Path, str]]:
    """The lock files a run holds, each once: for its artifacts, its
    Lustre trees, and the user's podman.  Rootless podman keeps one run
    directory per user (its locks and mount state) whatever storage it
    is pointed at, so two runs by one user collide even on separate
    storage; the lock is per user, in the user's cache."""
    cache = (home or Path.home()) / ".cache" / "ltvm"
    out: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for p, why in (
        (artifacts / ".release-build.lock", str(artifacts)),
        (work / ".release-build.lock", str(work)),
        (cache / "release-build-podman.lock", "this user's podman"),
    ):
        k = p.parent.resolve() / p.name
        if k not in seen:
            seen.add(k)
            out.append((p, why))
    return out


def lock_dir(path: Path, why: str) -> Any:
    """Hold lock file ``path`` (or ``path``/.release-build.lock for a
    directory) for this run, or raise naming ``why`` another run would
    collide.  Returns the open lock file, which holds the lock until it
    is closed."""
    import fcntl

    if path.suffix != ".lock":
        path = path / ".release-build.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "w")  # noqa: SIM115 -- held for the run
    except OSError as e:
        raise ReleaseBuildError(f"cannot lock {why} at {path}: {e}") from None
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise ReleaseBuildError(
            f"another `ltvm build release` is using {why}; wait for it"
        ) from None
    return f


def prepare_trees(src: Path, head: str, work: Path, jobs: int) -> list[Path]:
    """One clone of ``src`` per slot, at ``head``, reused across runs.
    Lustre builds in its own tree and a tree is rebuilt whenever the
    target changes, so slots cannot share one.  The clones own their
    objects, so a later run from another source still works."""
    trees = []
    for i in range(jobs):
        t = work / f"lustre-{i}"
        # A clone borrowing another's objects breaks when that one goes.
        if (t / ".git" / "objects" / "info" / "alternates").exists():
            shutil.rmtree(t)
        if not (t / ".git").exists():
            work.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(
                [
                    "git",
                    "clone",
                    "-q",
                    "--no-checkout",
                    str(src),
                    str(t),
                ],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                raise ReleaseBuildError(
                    f"cannot clone {src} to {t}: {r.stderr.strip()}"
                )
        _git(t, "fetch", "-q", "--tags", str(src), head)
        try:
            was = _git(t, "rev-parse", "-q", "--verify", "HEAD")
        except ReleaseBuildError:
            was = ""
        _git(t, "checkout", "-q", "--force", "--detach", head)
        if was != head:
            # The build would keep the old configure state, and with it
            # the old version, when only the commit changed.
            _git(t, "clean", "-q", "-x", "-d", "-f")
        trees.append(t)
    return trees


# ------------------------------------------------------------------
# Verify and ledger
# ------------------------------------------------------------------


def built_kernel_dir(log_text: str) -> str | None:
    """The kernel directory a ``build all`` baked its image on, from its
    log: it can be an older build than the newest one cached."""
    m = None
    for m in _IMAGE_KERNEL_RE.finditer(log_text):
        pass
    return m.group(1) if m else None


def payload_digest(
    kdir: Path, image: Path | None = None, variants: Iterable[str] = ()
) -> str:
    """What a build left in kernel directory ``kdir`` for publishing:
    every file of its Lustre snapshot by contents (less the snapshots of
    ``variants`` nested in it, which are not this release's), its
    installed kernel modules by path and size, and which image is
    current (each install gets a new versioned name) and its size.  Not
    file times, which a copy need not keep to the nanosecond."""
    import hashlib

    h = hashlib.sha256()
    snap = kdir / "lustre-artifacts"
    skip = {snap / v for v in variants}
    for p in sorted(snap.rglob("*")):
        if any(p == d or d in p.parents for d in skip):
            continue
        rel = p.relative_to(kdir)
        if p.is_symlink():
            h.update(f"L {rel}\0{os.readlink(p)}\0".encode())
        elif p.is_file():
            h.update(f"F {rel}\0".encode())
            try:
                with p.open("rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                h.update(b"\0unreadable")
            h.update(b"\0")
    for p in sorted((kdir / "modules").rglob("*")):
        if p.is_file() and not p.is_symlink():
            h.update(f"M {p.relative_to(kdir)}\0{p.stat().st_size}\0".encode())
    if image is not None:
        try:
            h.update(
                f"I {image.resolve().name}\0{image.stat().st_size}\0".encode()
            )
        except OSError:
            h.update(b"I missing\0")
    return h.hexdigest()


def build_payload(build: Build, arch: str, kdir_name: str) -> str:
    """payload_digest for a build, with its current image and the
    target's variants found the way publishing finds them."""
    from .image_store import current_image
    from .release_package import _declared_variant_names
    from .target_config import TargetConfig

    tc = TargetConfig(build.target, arch=arch)
    return payload_digest(
        tc.kernel_output_dir(kernel=kdir_name),
        current_image(tc.image_output_dir(kdir_name)),
        _declared_variant_names(build.target, arch),
    )


def verify(
    build: Build,
    arch: str,
    head: str,
    kdir_name: str,
    payload: str | None = None,
) -> list[str]:
    """Read a build's artifacts back from kernel directory ``kdir_name``:
    a complete kernel, the Lustre snapshot beside it (from ``head``,
    with modules), an image current against both, and when ``payload``
    is given, the same files as when the build was verified."""
    from .image_build import _lustre_staging_hash_input
    from .paths import load_meta_safe
    from .target_config import TargetConfig

    tc = TargetConfig(build.target, arch=arch)
    kdir = tc.kernel_output_dir(kernel=kdir_name)
    problems = []
    if load_meta_safe(kdir / "meta.json") is None or not tc.outputs_complete(
        "kernel", kernel=kdir_name
    ):
        problems.append(f"kernel in {kdir} is missing or incomplete")
    snap_dir = kdir / "lustre-artifacts"
    snap = load_meta_safe(snap_dir / ".ltvm-snapshot.json")
    got = snap.get("lustre_commit") if isinstance(snap, dict) else None
    if got != head:
        problems.append(
            f"Lustre snapshot is from {str(got)[:12]}, not {head[:12]}"
            if got
            else f"no Lustre snapshot in {kdir}"
        )
    elif not any(snap_dir.rglob("*.ko")):
        problems.append(f"Lustre snapshot in {kdir} has no modules")
    from .lustre_build import _hash_file

    staged_meta = load_meta_safe(snap_dir / ".ltvm-staging-meta.json") or {}
    symvers = _hash_file(kdir / "build-tree" / "Module.symvers")
    if got == head and symvers != staged_meta.get("module_symvers_sha256"):
        problems.append(
            f"Lustre snapshot in {kdir} was built against another kernel ABI"
        )
    try:
        extra = _lustre_staging_hash_input(snap_dir)
    except FileNotFoundError:
        extra = None
    if not tc.outputs_complete("image", kernel=kdir_name):
        problems.append("no image")
    elif extra is None or tc.is_stale(
        "image", kernel=kdir_name, extra_hash=extra
    ):
        problems.append("image does not match its inputs and this Lustre")
    if payload is not None and build_payload(build, arch, kdir_name) != payload:
        problems.append(f"files in {kdir} changed since the build was verified")
    return problems


class Ledger:
    """Builds that finished and verified, by arch, target and kernel,
    with the ltvm and Lustre commits they were built from."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        try:
            self.data: dict[str, Any] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}

    @staticmethod
    def _key(arch: str, b: Build) -> str:
        return f"{arch} {b.target} {b.kernel}"

    def done(
        self, arch: str, b: Build, ltvm: str, lustre: str
    ) -> tuple[str, str | None] | None:
        """The kernel directory and payload digest of a matching
        finished build, or None."""
        e = self.data.get(self._key(arch, b))
        if (
            isinstance(e, dict)
            and e.get("ltvm") == ltvm
            and e.get("lustre") == lustre
            and isinstance(e.get("kernel_dir"), str)
        ):
            m = e.get("payload")
            return str(e["kernel_dir"]), m if isinstance(m, str) else None
        return None

    def forget(self, arch: str, b: Build) -> None:
        with self._lock:
            if self.data.pop(self._key(arch, b), None) is not None:
                self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)

    def record(
        self,
        arch: str,
        b: Build,
        ltvm: str,
        lustre: str,
        kernel_dir: str,
        payload: str | None = None,
    ) -> None:
        with self._lock:
            self.data[self._key(arch, b)] = {
                "ltvm": ltvm,
                "lustre": lustre,
                "kernel_dir": kernel_dir,
                "payload": payload,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            self._write()


# ------------------------------------------------------------------
# Running
# ------------------------------------------------------------------


def failed_step(log_text: str) -> str | None:
    m = None
    for m in _STEP_RE.finditer(log_text):
        pass
    return m.group(1).lower() if m else None


@dataclass
class Runner:
    """Runs ltvm commands in slots, each pinned to its CPU set."""

    ltvm: list[str]
    arch: str
    cpus: list[list[int] | None]
    say: Callable[[str], None]
    env: dict[str, str] = field(default_factory=dict)
    _procs: set[subprocess.Popen[bytes]] = field(default_factory=set)
    _plock: threading.Lock = field(default_factory=threading.Lock)
    stopping: threading.Event = field(default_factory=threading.Event)
    # A build whose log has not grown for this long is stopped and
    # reported as stalled (a wedged podman or fuse-overlayfs otherwise
    # holds the run forever).  None never stops one.
    stall_seconds: float | None = 3600
    poll_seconds: float = 30

    def run(self, slot: int, argv: list[str], log: Path) -> int:
        cmd = list(self.ltvm) + argv + ["--arch", self.arch, "--no-update"]
        cpus = self.cpus[slot]
        if cpus is not None:
            cmd = ["taskset", "-c", format_cpulist(cpus)] + cmd
        env = {**os.environ, "LTVM_NO_UPDATE": "1", **self.env}
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as f:
            f.write(f"$ {' '.join(cmd)}\n".encode())
            f.flush()
            # Checked, started and registered under the lock stop()
            # takes, so no command starts after stop() has looked.  Its
            # own session, so stop() reaches the podman and make
            # processes under it too.
            with self._plock:
                if self.stopping.is_set():
                    return 130
                proc = subprocess.Popen(
                    cmd,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
                self._procs.add(proc)
            try:
                return self._wait(proc, log, f)
            finally:
                # Whatever ended the wait, the build must not outlive it
                # untracked.
                if proc.poll() is None:
                    _stop_group(proc)
                with self._plock:
                    self._procs.discard(proc)

    def _wait(self, proc: subprocess.Popen[bytes], log: Path, f: Any) -> int:
        last_size, last_change = -1, time.monotonic()
        while True:
            try:
                return proc.wait(timeout=self.poll_seconds)
            except subprocess.TimeoutExpired:
                pass
            try:
                size = log.stat().st_size
            except OSError:
                size = last_size
            now = time.monotonic()
            if size != last_size:
                last_size, last_change = size, now
                continue
            if (
                self.stall_seconds is None
                or now - last_change < self.stall_seconds
            ):
                continue
            mins = round(self.stall_seconds / 60)
            self.say(
                f"{_clock()} {log.name}: no output for {mins} min, stopping it"
            )
            try:
                f.write(
                    f"\nltvm build release: no output for {mins} min; "
                    f"stopped\n".encode()
                )
                f.flush()
            except OSError:
                pass
            _stop_group(proc)
            return STALLED

    def stop(self, grace: float = STOP_GRACE_SECONDS) -> None:
        """Interrupt every running command's process group, as Ctrl-C in
        its terminal would, so ltvm's own cleanup runs; kill what is
        left after ``grace`` seconds."""
        with self._plock:
            self.stopping.set()
            procs = list(self._procs)
        for p in procs:
            _signal_group(p, signal.SIGINT)
        deadline = time.monotonic() + grace
        for p in procs:
            try:
                p.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        for p in procs:
            _signal_group(p, signal.SIGKILL)


def _stop_group(proc: subprocess.Popen[bytes]) -> None:
    """Interrupt a build's process group, kill what is left after the
    grace period, and reap it."""
    _signal_group(proc, signal.SIGINT)
    try:
        proc.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc, signal.SIGKILL)
    proc.wait()


def _signal_group(p: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(p.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _clock() -> str:
    return time.strftime("%H:%M:%S")


def _mins(s: float) -> str:
    return f"{s / 60:.0f}m"


def run_parallel(
    items: list[Build],
    jobs: int,
    work: Callable[[int, Build], Result],
    runner: Runner,
    one_per_target: bool,
) -> dict[Build, Result]:
    """Run ``work(slot, item)`` over ``items`` in order with ``jobs``
    slots; with ``one_per_target``, never two items of one target at
    once.  Ctrl-C stops the running commands and returns what finished,
    with ``runner.stopping`` set.
    """
    pending = list(items)
    busy: set[str] = set()
    results: dict[Build, Result] = {}
    cond = threading.Condition()
    # An interrupted join can leave a thread looking finished while its
    # worker still runs, so the workers say when they are done.
    finished = [threading.Event() for _ in range(jobs)]

    def worker(slot: int) -> None:
        try:
            _worker(slot)
        finally:
            finished[slot].set()

    def _worker(slot: int) -> None:
        while True:
            with cond:
                while True:
                    if runner.stopping.is_set() or not pending:
                        return
                    item = next(
                        (
                            b
                            for b in pending
                            if not one_per_target or b.target not in busy
                        ),
                        None,
                    )
                    if item is not None:
                        break
                    cond.wait()
                pending.remove(item)
                busy.add(item.target)
            try:
                res = work(slot, item)
            except Exception as e:  # noqa: BLE001 -- one build, not the run
                res = Result(
                    item, "failed", problems=[f"{type(e).__name__}: {e}"]
                )
            with cond:
                busy.discard(item.target)
                results[item] = res
                cond.notify_all()

    threads = [
        threading.Thread(target=worker, args=(i,), daemon=True)
        for i in range(jobs)
    ]
    try:
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive():
                t.join(timeout=1)
    except KeyboardInterrupt:
        runner.say(
            f"{_clock()} interrupted: stopping the running builds "
            f"(up to {STOP_GRACE_SECONDS} s)"
        )
        # No build may start with SIGINT ignored: mark the runner stopped
        # under its launch lock first, so none starts after this.
        with runner._plock:
            runner.stopping.set()
        old = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            runner.stop()
        finally:
            signal.signal(signal.SIGINT, old)
        with cond:
            cond.notify_all()
        for t, done in zip(threads, finished):
            if t.ident is not None:
                done.wait(timeout=STOP_GRACE_SECONDS)
    with cond:
        return dict(results)


def preflight(
    builds: list[Build], jobs: int, runner: Runner, log_dir: Path
) -> dict[str, Result]:
    """Build each target's container and image package layers: cheap when
    podman has them, and a release needs the container to publish."""
    firsts = list({b.target: b for b in reversed(builds)}.values())[::-1]

    def work(slot: int, b: Build) -> Result:
        log = log_dir / f"preflight-{b.target}.log"
        t0 = time.monotonic()
        for step, argv in (
            ("container", ["build", "container", b.target]),
            ("image", ["build", "image", b.target, "--base-only"]),
        ):
            rc = runner.run(slot, argv, log)
            if rc != 0:
                runner.say(
                    f"{_clock()} preflight {b.target}: FAILED at {step} ({log})"
                )
                return Result(
                    b, "failed", step, time.monotonic() - t0, str(log)
                )
        runner.say(f"{_clock()} preflight {b.target}: ok")
        return Result(b, "ok", None, time.monotonic() - t0, str(log))

    res = run_parallel(firsts, jobs, work, runner, one_per_target=True)
    return {b.target: r for b, r in res.items()}


def build_all(
    builds: list[Build],
    jobs: int,
    runner: Runner,
    trees: list[Path],
    log_dir: Path,
    head: str,
    on_ok: Callable[[Build, str], None],
    force: bool | set[Build] = False,
    on_start: Callable[[Build], None] = lambda b: None,
) -> dict[Build, Result]:
    def work(slot: int, b: Build) -> Result:
        log = log_dir / f"{b.target}-{b.kernel}.log"
        cpus = runner.cpus[slot]
        where = f"slot {slot}" + (
            f", cpus {format_cpulist(cpus)}" if cpus else ""
        )
        runner.say(f"{_clock()} start {b.name} ({where})")
        # A failed attempt can leave old and new outputs mixed, so the
        # earlier success must not stand while this one runs.
        on_start(b)
        t0 = time.monotonic()
        rc = runner.run(
            slot,
            [
                "build",
                "all",
                b.target,
                "--kernel",
                b.kernel,
                "--lustre-tree",
                str(trees[slot]),
                "-y",
            ]
            + (["--force"] if force is True or (force and b in force) else []),
            log,
        )
        secs = time.monotonic() - t0
        if rc != 0 and runner.stopping.is_set():
            runner.say(f"{_clock()} interrupted {b.name} after {_mins(secs)}")
            return Result(b, "interrupted", None, secs, str(log))
        if rc != 0:
            try:
                step = failed_step(log.read_text(errors="replace"))
            except OSError:
                step = None
            if rc == STALLED:
                step = "stalled"
            runner.say(
                f"{_clock()} FAILED {b.name} at {step or 'an unknown step'} "
                f"after {_mins(secs)} ({log})"
            )
            return Result(
                b, "failed", step, secs, str(log), [f"exit status {rc}"]
            )
        try:
            kdir = built_kernel_dir(log.read_text(errors="replace"))
        except OSError:
            kdir = None
        if kdir is None:
            problems = ["the log does not say which kernel the image is on"]
        else:
            problems = verify(b, runner.arch, head, kdir)
        if problems:
            runner.say(
                f"{_clock()} FAILED {b.name} verify: {'; '.join(problems)}"
            )
            return Result(b, "failed", "verify", secs, str(log), problems)
        assert kdir is not None
        on_ok(b, kdir)
        runner.say(f"{_clock()} ok {b.name} in {_mins(secs)}")
        return Result(b, "ok", None, secs, str(log), kernel_dir=kdir)

    return run_parallel(builds, jobs, work, runner, one_per_target=True)


def summary_table(results: list[Result]) -> str:
    rows = [("build", "result", "time", "detail")]
    for r in results:
        detail = ""
        if r.status == "failed":
            detail = f"at {r.step or '?'}: {r.log}"
            if r.problems and r.step == "verify":
                detail = "; ".join(r.problems)
        elif r.status == "skipped":
            detail = "; ".join(r.problems)
        rows.append(
            (
                r.build.name,
                r.status,
                _mins(r.seconds) if r.seconds else "",
                detail,
            )
        )
    w = [max(len(row[i]) for row in rows) for i in range(3)]
    return "\n".join(
        f"{a:<{w[0]}}  {b:<{w[1]}}  {c:>{w[2]}}  {d}".rstrip()
        for a, b, c, d in rows
    )
