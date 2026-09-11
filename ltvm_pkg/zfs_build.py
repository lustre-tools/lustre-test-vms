"""Build OpenZFS against an ltvm-built kernel.

ZFS sits between the kernel and Lustre: it is entirely out-of-tree, so
it consumes the kernel build-tree (headers, .config, Module.symvers)
and changes nothing about it.  Nothing here touches the kernel or the
VM base image -- which is what lets ZFS be a per-build option rather
than a property of a target's artifacts.

Output layout:
    artifacts/<target>/<arch>/kernels/<kver>/zfs/<version>/
        src/         configured+built source tree (Lustre's --with-zfs)
        staging/     `make install DESTDIR=` tree (deploy-lustre)
        meta.json    input_hash, zfs_version, build_date

Inputs in the hash: the kernel's release string AND its recorded
input_hash (kmods link against Module.symvers, which changes when a
kernel patch or config option does without changing kernel.release),
the ZFS version, and the inner script's bytes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .paths import load_meta_safe
from .podman_run import run_podman_with_cleanup

if TYPE_CHECKING:
    from .target_config import TargetConfig

log = logging.getLogger(__name__)

INNER_SCRIPT = Path(__file__).parent / "zfs-build-inner.sh"

# What lustre/ChangeLog's newest entry lists under "Recommended ZFS
# version".  Only the fallback: a target may name its own under a
# `zfs: {version: ...}` block in targets.yaml, and --zfs-version
# overrides both.
DEFAULT_ZFS_VERSION = "2.4.0"

_RELEASE_URL = (
    "https://github.com/openzfs/zfs/releases/download/"
    "zfs-{ver}/zfs-{ver}.tar.gz"
)

# Only rhel-family build containers are wired up: the inner script
# installs its extra build deps with dnf.  Every target that declares a
# server lustre.mode today is rhel-family; ubuntu2404 is a client
# target and has no use for ZFS.
_SUPPORTED_OS_FAMILIES = ("rhel",)


class ZfsBuildError(RuntimeError):
    """A ZFS build (or its prerequisites) failed."""


def zfs_dir(tc: TargetConfig, kernel: str | None, version: str) -> Path:
    """Per-(kernel, version) ZFS artifact directory.

    Keyed under the kernel dir, like mofed-kmods: the modules are a
    property of (kernel build-tree, ZFS version) and are unaffected by
    image or container rebuilds.  Not variant-scoped -- ZFS links
    against the kernel, which is itself variant-independent.
    """
    return tc.kernel_output_dir(kernel) / "zfs" / version


def zfs_src_dir(tc: TargetConfig, kernel: str | None, version: str) -> Path:
    """The built ZFS source tree, for Lustre's ``--with-zfs``."""
    return zfs_dir(tc, kernel, version) / "src"


def zfs_staging_dir(tc: TargetConfig, kernel: str | None, version: str) -> Path:
    """The DESTDIR install tree, for deploying into a VM."""
    return zfs_dir(tc, kernel, version) / "staging"


def resolve_zfs_version(tc: TargetConfig, override: str | None = None) -> str:
    """Pick the ZFS version: CLI override, then targets.yaml, then default."""
    if override:
        return str(override)
    return tc.zfs_version or DEFAULT_ZFS_VERSION


def tarball_cache_dir() -> Path:
    """Where release tarballs are cached.

    Global rather than per-target/arch: the tarball is architecture-
    and distro-independent source, and `ltvm target clean <t>` should
    not force a re-download for every other target.
    """
    from .target_config import ARTIFACTS_DIR

    return ARTIFACTS_DIR / "cache" / "zfs"


def _kver_from_build_tree(build_tree: Path) -> str:
    kver_file = build_tree / "include" / "config" / "kernel.release"
    if not kver_file.is_file():
        raise FileNotFoundError(
            f"kernel.release missing under {build_tree} -- build the "
            f"kernel first"
        )
    return kver_file.read_text().strip()


def _kernel_input_hash(tc: TargetConfig, kernel: str | None) -> str:
    """The kernel artifact's recorded input_hash, or "" if unbuilt."""
    meta = load_meta_safe(tc.meta_path("kernel", kernel))
    if meta is None:
        return ""
    h = meta.get("input_hash")
    return h if isinstance(h, str) else ""


def _input_hash(kver: str, version: str, kernel_hash: str) -> str:
    h = hashlib.sha256()
    for part in (kver, version, kernel_hash):
        h.update(part.encode())
        h.update(b"\0")
    h.update(INNER_SCRIPT.read_bytes())
    return h.hexdigest()


def is_stale(
    tc: TargetConfig, kernel: str | None = None, version: str | None = None
) -> bool:
    """Does the ZFS artifact need (re)building?"""
    ver = resolve_zfs_version(tc, version)
    out_dir = zfs_dir(tc, kernel, ver)
    meta = load_meta_safe(out_dir / "meta.json")
    if meta is None:
        return True
    build_tree = tc.kernel_output_dir(kernel) / "build-tree"
    try:
        kver = _kver_from_build_tree(build_tree)
    except FileNotFoundError:
        return True
    expected = _input_hash(kver, ver, _kernel_input_hash(tc, kernel))
    if meta.get("input_hash") != expected:
        return True
    # A failed or half-deleted build can leave the matching meta.json
    # over an empty tree, which the hash alone would call fresh.  Probe
    # what the two consumers actually read.
    src = zfs_src_dir(tc, kernel, ver)
    staging = zfs_staging_dir(tc, kernel, ver)
    if not (src / "zfs_config.h").is_file():
        return True
    if not (src / "module" / "Module.symvers").is_file():
        return True
    return not any((staging / "lib" / "modules").rglob("zfs.ko*"))


def fetch_tarball(version: str, dest_dir: Path | None = None) -> Path:
    """Download the OpenZFS release tarball, or reuse the cached copy."""
    cache = dest_dir if dest_dir is not None else tarball_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    tarball = cache / f"zfs-{version}.tar.gz"
    if tarball.is_file() and tarball.stat().st_size > 0:
        log.info("Using cached ZFS tarball %s", tarball)
        return tarball

    url = _RELEASE_URL.format(ver=version)
    log.info("Downloading %s", url)
    # Download to a temp name in the same directory and rename, so an
    # interrupted transfer can never be picked up as a cached tarball
    # by the next run.
    fd, tmp_str = tempfile.mkstemp(dir=str(cache), prefix=f".zfs-{version}.")
    tmp = Path(tmp_str)
    os.close(fd)
    try:
        with (
            urllib.request.urlopen(url, timeout=120) as resp,
            tmp.open("wb") as out,
        ):
            shutil.copyfileobj(resp, out)
        if tmp.stat().st_size == 0:
            raise ZfsBuildError(f"Downloaded an empty tarball from {url}")
        # mkstemp gives 0600; the cache is shared, and the next user to
        # build this version should reuse the download rather than be
        # unable to read it.
        os.chmod(tmp, 0o644)
        tmp.replace(tarball)
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        if e.code == 404:
            raise ZfsBuildError(
                f"No OpenZFS release {version!r} at {url} (HTTP 404).  "
                f"Check the version against "
                f"https://github.com/openzfs/zfs/releases"
            ) from e
        raise ZfsBuildError(f"Download of {url} failed: {e}") from e
    except (urllib.error.URLError, OSError) as e:
        tmp.unlink(missing_ok=True)
        raise ZfsBuildError(f"Download of {url} failed: {e}") from e
    return tarball


def _unpack(tarball: Path, version: str, src_dir: Path) -> None:
    """Unpack the tarball so its contents land directly in ``src_dir``."""
    if src_dir.exists():
        shutil.rmtree(src_dir)
    src_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(src_dir.parent)) as td:
        tmp = Path(td)
        with tarfile.open(tarball, "r:gz") as tf:
            # data filter: refuse absolute paths and ../ escapes rather
            # than trusting a downloaded archive to stay in its dir.
            # Python 3.12+ defaults to it; 3.10/3.11 need it named.
            tf.extractall(tmp, filter="data")
        inner = tmp / f"zfs-{version}"
        if not inner.is_dir():
            # Tolerate a differently-named top directory (e.g. an rc
            # tag) as long as there is exactly one.
            entries = [p for p in tmp.iterdir() if p.is_dir()]
            if len(entries) != 1:
                raise ZfsBuildError(
                    f"Unexpected layout in {tarball.name}: expected one "
                    f"top-level directory, found {len(entries)}"
                )
            inner = entries[0]
        inner.rename(src_dir)


def _chown_to_sudo_user(path: Path) -> None:
    """Hand build output back to the invoking user under sudo.

    Rootful podman maps container root to host root in bind mounts, so
    everything the build wrote is root-owned; without this a later
    non-sudo `ltvm build status` or `ltvm clean` cannot read or remove
    it.  Same pattern as lustre_build / mofed_kmod_build.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_user or os.getuid() != 0:
        return
    try:
        import pwd

        pw = pwd.getpwnam(sudo_user)
        subprocess.run(
            ["chown", "-R", f"{pw.pw_uid}:{pw.pw_gid}", str(path)],
            check=False,
        )
    except (KeyError, OSError):
        pass


def build_zfs(
    tc: TargetConfig,
    kernel: str | None = None,
    *,
    version: str | None = None,
    force: bool = False,
    jobs: int | None = None,
) -> Path:
    """Build ZFS against tc's kernel build-tree.

    Returns the artifact directory (holding ``src/`` and ``staging/``).
    Idempotent: skips the work when meta.json's input_hash matches and
    both consumers' outputs are present.
    """
    ver = resolve_zfs_version(tc, version)

    if tc.os_family not in _SUPPORTED_OS_FAMILIES:
        raise ZfsBuildError(
            f"target {tc.name!r} is os_family {tc.os_family!r}; ZFS builds "
            f"are only wired up for {'/'.join(_SUPPORTED_OS_FAMILIES)} "
            f"build containers"
        )

    build_tree = tc.kernel_output_dir(kernel) / "build-tree"
    if not build_tree.is_dir():
        raise FileNotFoundError(
            f"Kernel build-tree not found: {build_tree} -- "
            f"run: ltvm build kernel {tc.name}"
        )

    container_tag = tc.container_tag
    check = subprocess.run(
        ["podman", "image", "exists", container_tag], capture_output=True
    )
    if check.returncode != 0:
        raise ZfsBuildError(
            f"Build container {container_tag!r} not found in podman "
            f"storage -- run: ltvm build container {tc.name}"
        )

    kver = _kver_from_build_tree(build_tree)
    expected_hash = _input_hash(kver, ver, _kernel_input_hash(tc, kernel))

    out_dir = zfs_dir(tc, kernel, ver)
    if not force and not is_stale(tc, kernel, ver):
        log.info("ZFS %s for %s (kernel=%s) is up to date", ver, tc.name, kver)
        return out_dir

    src_dir = zfs_src_dir(tc, kernel, ver)
    staging_dir = zfs_staging_dir(tc, kernel, ver)

    tarball = fetch_tarball(ver)

    log.info("Building ZFS %s for %s (kernel=%s)...", ver, tc.name, kver)
    t0 = time.monotonic()

    # Unpack fresh: the tree carries a configure cache keyed to the
    # kernel it was configured against, and reaching here at all means
    # something in the input hash moved.
    _unpack(tarball, ver, src_dir)
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    # Drop any stale meta before the build: if the build dies partway,
    # the next run must see a stale artifact rather than a meta.json
    # vouching for a half-built tree.
    (out_dir / "meta.json").unlink(missing_ok=True)

    cmd = [
        "podman",
        "run",
        "--rm",
        "--security-opt",
        "label=disable",
        # A clean ZFS build runs ~3-5 min on a warm container; 30 min
        # catches a wedged configure without killing a slow arm build.
        "--timeout",
        "1800",
        "-e",
        f"KVER={kver}",
        "-e",
        f"JOBS={jobs or os.cpu_count() or 4}",
        "-v",
        f"{INNER_SCRIPT}:/zfs-build-inner.sh:ro",
        "-v",
        f"{build_tree}:/kernel:ro",
        "-v",
        f"{src_dir}:/zfs-src",
        "-v",
        f"{staging_dir}:/zfs-staging",
        "--entrypoint",
        "bash",
        container_tag,
        "/zfs-build-inner.sh",
    ]
    r = run_podman_with_cleanup(cmd)
    if r.returncode != 0:
        raise ZfsBuildError(f"ZFS {ver} build failed (rc={r.returncode})")

    _chown_to_sudo_user(out_dir)

    elapsed = round(time.monotonic() - t0, 1)
    kmods = sorted(
        p.name for p in (staging_dir / "lib" / "modules").rglob("*.ko*")
    )
    meta = {
        "target": tc.name,
        "arch": tc.arch,
        "kernel": kver,
        "zfs_version": ver,
        "input_hash": expected_hash,
        "build_date": datetime.now(timezone.utc).isoformat(),
        "build_seconds": elapsed,
        "modules": kmods,
    }
    _atomic_write_json(out_dir / "meta.json", meta)
    log.info(
        "ZFS %s built: %d modules in %s (%.0fs)",
        ver,
        len(kmods),
        out_dir,
        elapsed,
    )
    return out_dir


def ensure_zfs(
    tc: TargetConfig,
    kernel: str | None = None,
    *,
    version: str | None = None,
    force: bool = False,
    jobs: int | None = None,
) -> tuple[Path, Path, str]:
    """Build ZFS if stale; return (src_dir, staging_dir, version)."""
    ver = resolve_zfs_version(tc, version)
    if force or is_stale(tc, kernel, ver):
        build_zfs(tc, kernel, version=ver, force=force, jobs=jobs)
    return (
        zfs_src_dir(tc, kernel, ver),
        zfs_staging_dir(tc, kernel, ver),
        ver,
    )


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2) + "\n"
    fd, tmp_str = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}."
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()
