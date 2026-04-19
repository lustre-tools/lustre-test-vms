"""Build MOFED kernel modules against a Lustre-patched kernel.

The mofed image overlay installs MOFED userspace only -- the bundle's
prebuilt kmod RPMs pin to a stock RHEL kernel-core that won't satisfy
deps against our Lustre-patched kernel.  This module rebuilds the
kmod RPMs against the target's actual kernel build-tree (using
mlnx_add_kernel_support.sh inside the mofed builder container), and
caches them per-kernel so the image build can install them.

Output layout:
    output/<target>/<arch>/kernels/<kver>/mofed-kmods/<mofed-version>/
        kmod-mlnx-ofa_kernel-<...>.rpm
        kmod-mlnx-ofa_kernel-modules-<...>.rpm
        ...
        meta.json    # input_hash, mofed_version, build_date

Inputs in the hash: kernel build-tree's kernel.release, mofed_version,
inner script bytes.  Same kernel + same mofed = cached.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .paths import load_meta_safe
from .podman_run import run_podman_with_cleanup

if TYPE_CHECKING:
    from .target_config import TargetConfig

log = logging.getLogger(__name__)

INNER_SCRIPT = Path(__file__).parent / "mofed-kmod-build-inner.sh"


def mofed_kmod_dir(tc: "TargetConfig", kernel: str | None = None) -> Path:
    """Return the per-kernel mofed-kmods cache directory.

    Keyed under the kernel dir (not the image dir) because the kmods
    are a property of (kernel build-tree, mofed_version) and outlive
    any particular image rebuild.  Variant subdir lets a future
    non-default mofed variant (e.g. a different MOFED version override)
    coexist with the standard one.
    """
    mofed_version = _mofed_version(tc)
    return (
        tc.kernel_output_dir(kernel)
        / "mofed-kmods"
        / mofed_version
    )


def _mofed_version(tc: "TargetConfig") -> str:
    """Resolve the MOFED version for the target's variant."""
    from .target_config import DEFAULT_VARIANT

    if tc.variant_name == DEFAULT_VARIANT:
        raise ValueError(
            f"target {tc.name!r} is not bound to a mofed variant"
        )
    v = tc.variant(tc.variant_name)
    ver = v.params.get("mofed_version")
    if not ver:
        raise ValueError(
            f"variant {tc.variant_name!r} has no mofed_version param"
        )
    return str(ver)


def _kver_from_build_tree(build_tree: Path) -> str:
    kver_file = build_tree / "include" / "config" / "kernel.release"
    if not kver_file.is_file():
        raise FileNotFoundError(
            f"kernel.release missing under {build_tree} -- "
            f"build the kernel first"
        )
    return kver_file.read_text().strip()


def _input_hash(kver: str, mofed_version: str) -> str:
    h = hashlib.sha256()
    h.update(kver.encode())
    h.update(b"\0")
    h.update(mofed_version.encode())
    h.update(b"\0")
    h.update(INNER_SCRIPT.read_bytes())
    return h.hexdigest()


def is_stale(tc: "TargetConfig", kernel: str | None = None) -> bool:
    out_dir = mofed_kmod_dir(tc, kernel)
    meta = load_meta_safe(out_dir / "meta.json")
    if meta is None:
        return True
    build_tree = tc.kernel_output_dir(kernel) / "build-tree"
    try:
        kver = _kver_from_build_tree(build_tree)
    except FileNotFoundError:
        return True
    expected = _input_hash(kver, _mofed_version(tc))
    return meta.get("input_hash") != expected


def build_mofed_kmods(
    tc: "TargetConfig",
    kernel: str | None = None,
    *,
    force: bool = False,
) -> Path:
    """Build MOFED kmod RPMs against tc's kernel build-tree.

    Returns the directory containing the produced RPMs.  Idempotent:
    skips work when meta.json's input_hash matches.
    """
    from .target_config import DEFAULT_VARIANT

    if tc.variant_name == DEFAULT_VARIANT:
        raise ValueError(
            f"target {tc.name!r} is bound to the base variant -- "
            f"mofed kmods only apply to the mofed variant"
        )

    build_tree = tc.kernel_output_dir(kernel) / "build-tree"
    if not build_tree.is_dir():
        raise FileNotFoundError(
            f"Kernel build-tree not found: {build_tree} -- "
            f"run: ltvm build kernel {tc.name}"
        )

    container_tag = tc.container_tag
    check = subprocess.run(
        ["podman", "image", "exists", container_tag],
        capture_output=True,
    )
    if check.returncode != 0:
        raise RuntimeError(
            f"Build container {container_tag!r} not found in podman storage"
        )

    kver = _kver_from_build_tree(build_tree)
    mofed_version = _mofed_version(tc)
    expected_hash = _input_hash(kver, mofed_version)

    out_dir = mofed_kmod_dir(tc, kernel)
    if not force and not is_stale(tc, kernel):
        log.info(
            "MOFED kmods for %s (kernel=%s, mofed=%s) are up to date",
            tc.name, kver, mofed_version,
        )
        return out_dir

    out_dir.mkdir(parents=True, exist_ok=True)
    # Wipe stale RPMs from a prior incompatible build before we drop
    # new ones in -- otherwise dnf could pick up an old kmod for a
    # mismatched kernel.
    for rpm in out_dir.glob("*.rpm"):
        rpm.unlink()

    log.info(
        "Building MOFED kmods for %s (kernel=%s, mofed=%s)...",
        tc.name, kver, mofed_version,
    )
    t0 = time.monotonic()

    # Bind-mount the inner script + the kernel build-tree + the output
    # directory.  Run as bash because the inner script uses bash-isms.
    cmd = [
        "podman", "run", "--rm",
        "--security-opt", "label=disable",
        "--timeout", "1800",  # MOFED kmod rebuild can run 10-15 min
        "-e", f"KVER={kver}",
        "-v", f"{INNER_SCRIPT}:/mofed-kmod-build-inner.sh:ro",
        "-v", f"{build_tree}:/kernel-build-tree:ro",
        "-v", f"{out_dir}:/mofed-kmods-out",
        "--entrypoint", "bash",
        container_tag,
        "/mofed-kmod-build-inner.sh",
    ]
    r = run_podman_with_cleanup(cmd)
    if r.returncode != 0:
        have_rpms = any(out_dir.glob("*.rpm"))
        if getattr(r, "cleanup_eof", False) and have_rpms:
            log.warning(
                "MOFED kmod build finished but podman cleanup exited %d "
                "with an EOF (macOS podman-machine socket drop).  RPMs "
                "are on disk; treating as success.",
                r.returncode,
            )
        else:
            raise RuntimeError(
                f"MOFED kmod build failed (rc={r.returncode})"
            )

    rpms = sorted(p.name for p in out_dir.glob("*.rpm"))
    if not rpms:
        raise RuntimeError(
            f"MOFED kmod build produced no RPMs in {out_dir}"
        )

    # Chown RPMs back to the invoking user when running under sudo, so
    # later non-sudo `ltvm build status` / cleanup commands can read
    # and remove them.  Same pattern as lustre_build.
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.getuid() == 0:
        try:
            import pwd
            pw = pwd.getpwnam(sudo_user)
            subprocess.run(
                ["chown", "-R", f"{pw.pw_uid}:{pw.pw_gid}", str(out_dir)],
                check=False,
            )
        except (KeyError, OSError):
            pass

    elapsed = round(time.monotonic() - t0, 1)
    meta = {
        "target": tc.name,
        "kernel": kver,
        "mofed_version": mofed_version,
        "input_hash": expected_hash,
        "build_date": datetime.now(timezone.utc).isoformat(),
        "build_seconds": elapsed,
        "rpms": rpms,
    }
    _atomic_write_json(out_dir / "meta.json", meta)
    log.info(
        "MOFED kmods built: %d RPMs in %s (%.0fs)",
        len(rpms), out_dir, elapsed,
    )
    return out_dir


def _atomic_write_json(path: Path, payload: dict) -> None:
    text = json.dumps(payload, indent=2) + "\n"
    fd, tmp_str = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()
