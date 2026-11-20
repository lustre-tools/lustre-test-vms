"""External dependencies of the hardware LNDs: kfilnd and efalnd.

Lustre builds two LNDs only when configure is pointed at code that is
not in the kernel:

  kfilnd  ``--with-kfi=DIR``: a built kfabric tree, DIR/Module.symvers
          and DIR/include/kfi_endpoint.h.  HPE's shs-kfabric provides
          both; only its core module (kfi/kfabric.ko) is built, since
          the CXI provider needs the cxi-ss1 driver and Cassini
          hardware.  kfabric's own headers carry kfi_cxi_ext.h, so
          configure's kfi_cxi probes pass without it.
  efalnd  ``--enable-efa=DIR``: DIR/efa_verbs.h from Amazon's
          out-of-tree EFA driver.  It is one header-only file; the rest
          (ib_verbs) comes from the kernel's own RDMA headers.

Neither has a software provider a VM could run (kfilnd needs a CXI NIC
or HPE's private netsim, efalnd an EFA device), so this exists to
compile-check them, and ltvm builds them only on request
(``build lustre --lnds kfi,efa``).

Both sources are pinned: kfabric to a commit, fetched by that commit
id (git checks the content against it), and the EFA header to a commit
and its sha256.

Output layout:
    artifacts/<target>/<arch>/kernels/<kver>/lnds/kfabric-<commit12>/
        include/         kfabric's headers
        Module.symvers   kfabric.ko's exports
        kfabric.ko       shipped into the Lustre staging beside kkfilnd
        meta.json        input_hash, written last
    $XDG_CACHE_HOME/ltvm/lnds/efa-<commit12>/efa_verbs.h

A user who cannot write the kernel artifact builds kfabric into
``$XDG_CACHE_HOME/ltvm/lnds/<target>/<arch>/<kver>/`` instead, by the
rules ZFS follows (zfs_build).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from .paths import load_meta_safe

if TYPE_CHECKING:
    from .target_config import TargetConfig

log = logging.getLogger(__name__)

# HewlettPackard/shs-kfabric, tag release/shs-14.0.1.  GPL-2.0.
KFABRIC_REPO = "https://github.com/HewlettPackard/shs-kfabric"
KFABRIC_COMMIT = "0801b2ceb5767a860ba6e04e1d346f4f51cae722"

# amzn/amzn-drivers, tag efa_linux_3.4.1.  GPL-2.0 OR Linux-OpenIB.
EFA_COMMIT = "77facd6719172a5f39ea5c810b126a8b40b7e4fe"
EFA_HEADER_URL = (
    "https://raw.githubusercontent.com/amzn/amzn-drivers/"
    f"{EFA_COMMIT}/kernel/linux/efa/src/efa_verbs.h"
)
EFA_HEADER_SHA256 = (
    "ba74ced594a4634dee5b31060febf8a099eb85d09cbb08676251abbd666a7e2c"
)

# What --lnds takes, and the Lustre module each one adds.
LNDS = {"kfi": "kkfilnd", "efa": "kefalnd"}
_ALIASES = {"kfilnd": "kfi", "efalnd": "efa"}

# The container mount points configure is given.
KFI_MOUNT = "/kfi"
EFA_MOUNT = "/efa"

_KFABRIC_BUILD = "make -C /kernel M=/src/kfi modules"


class LndDepError(RuntimeError):
    """An LND dependency could not be fetched or built."""


def parse_lnds(spec: str | None) -> tuple[str, ...]:
    """``"kfi,efa"`` -> ``("efa", "kfi")``; ValueError on an unknown name."""
    if not spec:
        return ()
    out: set[str] = set()
    for raw in spec.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        name = _ALIASES.get(name, name)
        if name == "all":
            out.update(LNDS)
        elif name in LNDS:
            out.add(name)
        else:
            raise ValueError(
                f"unknown LND {raw.strip()!r} in --lnds: "
                f"takes {', '.join(LNDS)} or all"
            )
    return tuple(sorted(out))


def identity(lnds: tuple[str, ...]) -> str | None:
    """What the configure stamp records of these LNDs' dependencies.

    None for none, so a build without --lnds hashes as it always did.
    """
    if not lnds:
        return None
    pins = {"kfi": KFABRIC_COMMIT, "efa": EFA_HEADER_SHA256}
    return ",".join(f"{n}={pins[n]}" for n in lnds)


def configure_args(lnds: tuple[str, ...]) -> list[str]:
    args = []
    if "kfi" in lnds:
        args.append(f"--with-kfi={KFI_MOUNT}")
    if "efa" in lnds:
        args.append(f"--enable-efa={EFA_MOUNT}")
    return args


# --- efa_verbs.h ------------------------------------------------------


def efa_dir() -> Path:
    from .zfs_build import user_cache_root

    return user_cache_root() / "lnds" / f"efa-{EFA_COMMIT[:12]}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_efa() -> Path:
    """The directory holding the pinned efa_verbs.h, fetched if needed."""
    d = efa_dir()
    hdr = d / "efa_verbs.h"
    if hdr.is_file() and _sha256(hdr) == EFA_HEADER_SHA256:
        return d
    d.mkdir(parents=True, exist_ok=True)
    log.info("Fetching efa_verbs.h (amzn-drivers %s)", EFA_COMMIT[:12])
    try:
        with urllib.request.urlopen(EFA_HEADER_URL, timeout=60) as r:
            data = r.read()
    except OSError as e:
        raise LndDepError(f"cannot fetch {EFA_HEADER_URL}: {e}") from e
    got = hashlib.sha256(data).hexdigest()
    if got != EFA_HEADER_SHA256:
        raise LndDepError(
            f"{EFA_HEADER_URL} has sha256 {got}, expected {EFA_HEADER_SHA256}"
        )
    fd, tmp = tempfile.mkstemp(prefix=".efa_verbs.h.", dir=d)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, hdr)
    return d


# --- kfabric ----------------------------------------------------------


def kfabric_dir(tc: TargetConfig, kernel: str | None) -> Path:
    """Per-kernel kfabric build in the shared artifacts."""
    return (
        tc.kernel_output_dir(kernel) / "lnds" / f"kfabric-{KFABRIC_COMMIT[:12]}"
    )


def user_kfabric_dir(tc: TargetConfig, kernel: str | None) -> Path:
    """The per-user fallback for :func:`kfabric_dir`."""
    from .zfs_build import user_cache_root

    kdir = tc.kernel_output_dir(kernel)
    return (
        user_cache_root()
        / "lnds"
        / tc.name
        / str(tc.arch)
        / kdir.name
        / f"kfabric-{KFABRIC_COMMIT[:12]}"
    )


def _kfabric_input_hash(kver: str, kernel_hash: str) -> str:
    h = hashlib.sha256()
    for part in (kver, kernel_hash, KFABRIC_COMMIT, _KFABRIC_BUILD):
        h.update(part.encode())
        h.update(b"\0")
    return h.hexdigest()


def _kfabric_fresh(d: Path, want: str, kernel_meta: dict | None) -> bool:
    from .target_config import kernel_abi_mismatch

    meta = load_meta_safe(d / "meta.json")
    if meta is None or meta.get("input_hash") != want:
        return False
    if kernel_abi_mismatch(meta, kernel_meta):
        return False
    return all(
        (d / f).is_file()
        for f in ("Module.symvers", "kfabric.ko", "include/kfi_endpoint.h")
    )


def ensure_kfabric(tc: TargetConfig, kernel: str | None) -> Path:
    """A kfabric tree for ``--with-kfi``, built for *kernel* if missing
    or stale.  Raises LndDepError when it cannot be had."""
    from .lustre_build import _kernel_release
    from .zfs_build import _no_more_writable_than, _shared_is_ours, _untrusted

    kdir = tc.kernel_output_dir(kernel)
    build_tree = kdir / "build-tree"
    if not (build_tree / "Module.symvers").is_file():
        raise LndDepError(f"no built kernel at {build_tree}")
    kver = _kernel_release(build_tree)
    kmeta = load_meta_safe(tc.meta_path("kernel", kernel))
    kh = (kmeta or {}).get("input_hash")
    want = _kfabric_input_hash(kver, kh if isinstance(kh, str) else "")
    shared = kfabric_dir(tc, kernel)
    user = user_kfabric_dir(tc, kernel)

    if _kfabric_fresh(shared, want, kmeta):
        why = _untrusted(shared, kdir)
        if why is None:
            return shared
        log.warning("kfabric: ignoring %s: %s", shared, why)
    if _kfabric_fresh(user, want, kmeta):
        return user

    ours = _shared_is_ours(shared, kdir)
    out = shared if ours else user
    with _lock(out):
        if _kfabric_fresh(out, want, kmeta) and (
            out != shared or _untrusted(shared, kdir) is None
        ):
            return out
        _build_kfabric(tc, build_tree, kver, want, kmeta, out)
    if ours:
        try:
            _no_more_writable_than(out, kdir)
        except OSError:
            pass
    return out


@contextmanager
def _lock(out: Path) -> Iterator[None]:
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        out.parent / f".{out.name}.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o644,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _fetch_kfabric(dest: Path) -> None:
    """Check out KFABRIC_COMMIT into *dest* (shallow, by commit id)."""

    def git(*args: str) -> None:
        r = subprocess.run(
            ["git", "-C", str(dest), *args], capture_output=True, text=True
        )
        if r.returncode != 0:
            raise LndDepError(
                f"git {' '.join(args)} failed: {r.stderr.strip()}"
            )

    dest.mkdir(parents=True)
    git("init", "-q")
    git("fetch", "-q", "--depth", "1", KFABRIC_REPO, KFABRIC_COMMIT)
    git("-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD")
    head = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != KFABRIC_COMMIT:
        raise LndDepError(f"kfabric checkout is {head}, not {KFABRIC_COMMIT}")


def _build_kfabric(
    tc: TargetConfig,
    build_tree: Path,
    kver: str,
    want: str,
    kmeta: dict | None,
    out: Path,
) -> None:
    from . import build_volumes
    from .cli.util import host_arch
    from .target_config import kernel_abi_marks

    if str(tc.arch) != host_arch():
        raise LndDepError(
            f"--lnds kfi is not supported for a cross-arch build "
            f"({tc.arch} on {host_arch()})"
        )
    print(f"--- Building kfabric {KFABRIC_COMMIT[:12]} for {kver}...")
    with tempfile.TemporaryDirectory(prefix="ltvm-kfabric-") as tmp:
        src = Path(tmp) / "src"
        _fetch_kfabric(src)
        kernel_src = build_volumes.kernel_tree_mount(
            build_tree, tc.container_tag
        )
        cmd = [
            "podman",
            "run",
            "--rm",
            "--security-opt",
            "label=disable",
            "-v",
            f"{kernel_src}:/kernel:ro",
            "-v",
            f"{src}:/src",
            "--entrypoint",
            "bash",
            tc.container_tag,
            "-c",
            _KFABRIC_BUILD,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        ko = src / "kfi" / "kfabric.ko"
        if r.returncode != 0 or not ko.is_file():
            tail = "\n".join((r.stdout + r.stderr).splitlines()[-20:])
            raise LndDepError(f"kfabric did not build for {kver}:\n{tail}")
        # meta.json goes last: until then the dir is not fresh
        (out / "meta.json").unlink(missing_ok=True)
        shutil.rmtree(out / "include", ignore_errors=True)
        out.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src / "include", out / "include")
        shutil.copy2(src / "kfi" / "Module.symvers", out / "Module.symvers")
        shutil.copy2(ko, out / "kfabric.ko")
    meta = {
        "kernel": kver,
        "input_hash": want,
        "kfabric_commit": KFABRIC_COMMIT,
        "kfabric_repo": KFABRIC_REPO,
        **kernel_abi_marks(kmeta),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")


def ensure(
    tc: TargetConfig, kernel: str | None, lnds: tuple[str, ...]
) -> dict[str, Path]:
    """Prepare what *lnds* need: ``{"kfi": dir, "efa": dir}``."""
    out: dict[str, Path] = {}
    if "kfi" in lnds:
        out["kfi"] = ensure_kfabric(tc, kernel)
    if "efa" in lnds:
        out["efa"] = ensure_efa()
    return out
