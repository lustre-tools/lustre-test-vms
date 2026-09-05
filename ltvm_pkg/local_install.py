"""Install a Lustre build onto the machine ltvm is running on.

`ltvm deploy-lustre` pushes a build from a build host *into* a VM over
ssh.  This module is the other direction: ltvm running *inside* a
machine it produced -- an ltvm VM, or a cloud node booted from
`ltvm target export --format gce` -- installing Lustre onto that
machine's own root filesystem.

The build still happens in the target's build container, because the
VM image ships the runtime packages but not the toolchain
(`packages-dev.txt` is build-container-only).  So the flow is:

  1. work out which ltvm target this machine's image came from
  2. `make install DESTDIR=<staging>` in that target's build container
  3. unpack the DESTDIR onto `/`, then depmod + ldconfig

Uninstall is driven by a manifest written at install time rather than
by `make uninstall`: the node has no configured source tree to run
that from, and the manifest removes exactly what we put there.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ltvm_pkg.priv import sudo_run

log = logging.getLogger(__name__)

# Written into every image by image_build; read back by ltvm running
# inside the resulting machine.  Bump the schema only if an older ltvm
# couldn't understand a newer stamp.
IMAGE_STAMP_PATH = Path("/etc/ltvm-image.json")
IMAGE_STAMP_SCHEMA = "ltvm-image/1"

# Record of what make-install put on this machine, so make-uninstall
# can take exactly that back off.  Under /var/lib rather than /etc:
# it's state we generate, not configuration anyone edits.
MANIFEST_PATH = Path("/var/lib/ltvm/lustre-install.json")
MANIFEST_SCHEMA = "ltvm-lustre-install/1"

# Kernel modules Lustre loads.  Ordered leaf-first so rmmod has a
# chance without dependency juggling.
_LUSTRE_MODULES = (
    "lustre", "lmv", "mdc", "osc", "lov", "fid", "fld", "ptlrpc",
    "obdclass", "ksocklnd", "lnet", "libcfs",
)

# rm/rmdir batch size.  Well under ARG_MAX, and keeps a failure
# report pointed at a small set of paths.
_RM_CHUNK = 500

# Directories we never rmdir even when a prune leaves them empty.
# rmdir refuses non-empty dirs anyway; this is the second lock on the
# door, because these are the ones where being wrong is unrecoverable.
_NEVER_PRUNE = frozenset((
    "", ".", "usr", "etc", "lib", "lib64", "bin", "sbin", "var", "opt",
    "usr/bin", "usr/sbin", "usr/lib", "usr/lib64", "usr/share",
    "usr/include", "usr/local", "var/lib", "var/run", "etc/init.d",
    "lib/modules", "usr/lib/modules",
))


class LocalInstallError(RuntimeError):
    """Anything that should surface to the CLI as a clean error."""


@dataclass(frozen=True)
class LocalImage:
    """Which ltvm target the running machine's image was built from."""

    target: str
    arch: str
    variant: str
    kernel: str            # kernel artifact dir name, e.g. 5.14-rhel9.7-1.el9
    kernel_version: str    # uname -r form, e.g. 5.14.0-503.ltvm.el9.x86_64
    os_family: str
    source: str            # "stamp" | "os-release" | "explicit"


# ----------------------------------------------------------------------
# Where am I?
# ----------------------------------------------------------------------


def read_image_stamp(path: Path | None = None) -> LocalImage | None:
    """Read the identity image_build baked into this machine's rootfs.

    Returns None when the file is missing, unreadable, malformed, or
    written by a schema this ltvm doesn't know -- every one of those
    means "fall back to sniffing", not "crash".
    """
    path = IMAGE_STAMP_PATH if path is None else path
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("Ignoring unreadable image stamp %s: %s", path, e)
        return None

    if not isinstance(raw, dict):
        log.warning("Ignoring malformed image stamp %s", path)
        return None
    schema = raw.get("schema")
    if schema != IMAGE_STAMP_SCHEMA:
        log.warning(
            "Image stamp %s has schema %r, expected %r -- ignoring it; "
            "pass --target to say which target this machine is",
            path, schema, IMAGE_STAMP_SCHEMA,
        )
        return None
    try:
        return LocalImage(
            target=str(raw["target"]),
            arch=str(raw["arch"]),
            variant=str(raw.get("variant") or "base"),
            kernel=str(raw["kernel"]),
            kernel_version=str(raw.get("kernel_version") or ""),
            os_family=str(raw.get("os_family") or ""),
            source="stamp",
        )
    except KeyError as e:
        log.warning("Image stamp %s missing field %s -- ignoring it", path, e)
        return None


def _parse_os_release(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def detect_targets_from_os_release(
    path: Path = Path("/etc/os-release"),
) -> list[str]:
    """Guess candidate ltvm targets for the running OS.

    Only a fallback, for machines whose image predates the stamp.  It
    matches on ID + major version, so it can legitimately return
    several targets (rocky9 and rocky9-64k both match a Rocky 9 node);
    the caller turns that into a "pass --target" error rather than
    picking one.
    """
    osr = _parse_os_release(path)
    os_id = osr.get("ID", "").lower()
    version_id = osr.get("VERSION_ID", "")
    if not os_id or not version_id:
        return []
    major = version_id.split(".")[0]

    from ltvm_pkg.target_config import TargetConfig, list_targets

    matches: list[str] = []
    for name in list_targets():
        try:
            tc = TargetConfig(name)
        except (ValueError, KeyError):
            continue
        if tc.os_name.lower() != os_id:
            continue
        if tc.os_version.split(".")[0] != major:
            continue
        matches.append(name)
    return matches


def running_kernel() -> str:
    return platform.release()


def check_is_ltvm_machine(force: bool = False) -> None:
    """Refuse to run anywhere that isn't a machine ltvm built.

    make-install unpacks a tree onto `/`.  On the build host -- which
    is where someone is most likely to type it by accident -- that
    means scattering Lustre binaries and modules across their
    workstation.  The image stamp is the evidence that we're on a
    machine whose whole filesystem is disposable.
    """
    if platform.system() != "Linux":
        raise LocalInstallError(
            f"ltvm make-install installs into / and only runs on Linux "
            f"(this is {platform.system()}).  To install into a VM from "
            f"a build host, use: ltvm deploy-lustre <vm>"
        )
    if IMAGE_STAMP_PATH.exists():
        return
    if force:
        log.warning(
            "No %s -- this does not look like a machine built from an "
            "ltvm image, but --force was given.  Installing into / anyway.",
            IMAGE_STAMP_PATH,
        )
        return
    raise LocalInstallError(
        f"This machine does not look like one ltvm built ({IMAGE_STAMP_PATH} "
        f"is missing), and make-install installs Lustre into /.\n"
        f"  To install into a VM from a build host: ltvm deploy-lustre <vm>\n"
        f"  To install here anyway:                 add --force"
    )


def resolve_local_image(
    explicit_target: str | None = None,
    explicit_kernel: str | None = None,
    explicit_variant: str | None = None,
) -> LocalImage:
    """Work out which target/kernel/variant this machine corresponds to.

    Order: the image stamp, then an explicit --target, then an
    /etc/os-release guess.  Explicit flags always override individual
    fields of whatever was found.
    """
    image = read_image_stamp()

    if image is None:
        target = explicit_target
        if target is None:
            candidates = detect_targets_from_os_release()
            if not candidates:
                raise LocalInstallError(
                    "Cannot tell which ltvm target this machine is: no "
                    f"{IMAGE_STAMP_PATH}, and /etc/os-release matches no "
                    "target in targets.yaml.  Pass --target <name>."
                )
            if len(candidates) > 1:
                raise LocalInstallError(
                    "/etc/os-release matches more than one target "
                    f"({', '.join(sorted(candidates))}).  Pass --target "
                    "<name> to choose."
                )
            target = candidates[0]
            log.warning(
                "No %s -- guessed target %r from /etc/os-release.  Pass "
                "--target to be sure.", IMAGE_STAMP_PATH, target,
            )
        image = LocalImage(
            target=target, arch="", variant="base", kernel="",
            kernel_version="", os_family="", source="os-release",
        )

    target = explicit_target or image.target
    variant = explicit_variant or image.variant or "base"

    from ltvm_pkg.target_config import TargetConfig

    arch = image.arch or platform.machine()
    try:
        tc = TargetConfig(target, arch=arch, variant=variant)
    except (ValueError, KeyError) as e:
        raise LocalInstallError(f"Unknown target {target!r}: {e}") from e

    kernel = tc.resolve_kernel(explicit_kernel or image.kernel or None)
    return LocalImage(
        target=target,
        arch=tc.arch,
        variant=variant,
        kernel=kernel,
        kernel_version=image.kernel_version,
        os_family=tc.os_family,
        source="explicit" if explicit_target else image.source,
    )


def check_kernel_match(image: LocalImage, kver: str) -> str | None:
    """Return a warning when the build's kernel isn't the running one.

    Modules built against a different kernel release won't load, and
    the failure shows up much later as a confusing modprobe error --
    so say it here, at the point where it's still cheap to fix.
    """
    running = running_kernel()
    if not kver or kver == running:
        return None
    return (
        f"Kernel mismatch: this build targets {kver}, but the running "
        f"kernel is {running}.  The modules will install but will not "
        f"load until you boot {kver}."
    )


# ----------------------------------------------------------------------
# Install
# ----------------------------------------------------------------------


def _sudo_argv(cmd: list[str]) -> list[str]:
    """sudo prefix for the Popen call sites (priv.sudo_run has no
    streaming variant, and the install is a tar pipeline)."""
    return cmd if os.geteuid() == 0 else ["sudo", *cmd]


def staging_contents(staging: Path) -> tuple[list[str], list[str]]:
    """Return (files, dirs) under *staging* as relative POSIX paths.

    Files include symlinks -- they are removed with rm like anything
    else.  Dirs come back deepest-first so a prune pass can rmdir
    them in order.
    """
    files: list[str] = []
    dirs: list[str] = []
    for root, dirnames, filenames in os.walk(staging):
        rel_root = Path(root).relative_to(staging)
        for d in dirnames:
            rel = (rel_root / d).as_posix()
            dirs.append(rel)
        for f in filenames:
            rel = (rel_root / f).as_posix()
            # Build bookkeeping, not part of the install.
            if rel.startswith(".ltvm-"):
                continue
            files.append(rel)
    files.sort()
    # Deepest first, so uninstall can rmdir bottom-up.
    dirs.sort(key=lambda p: (-p.count("/"), p))
    return files, dirs


def install_staging_into_root(
    staging: Path, root: Path = Path("/")
) -> None:
    """Unpack the DESTDIR tree at *staging* onto *root*.

    tar rather than `cp -a`: `--keep-directory-symlink` stops the
    extraction replacing `/lib` (a symlink to `/usr/lib` on RHEL)
    with a real directory, which would strand every library on the
    system.  `deploy_to_vm` uses tar over ssh for the same reason.

    *root* is a seam for tests; in production it is always `/`.
    """
    if not staging.is_dir():
        raise LocalInstallError(f"Staging directory not found: {staging}")

    log.info("Installing %s into %s", staging, root)
    src = subprocess.Popen(
        ["tar", "cf", "-", "-C", str(staging), "."],
        stdout=subprocess.PIPE,
    )
    assert src.stdout is not None
    try:
        dst = subprocess.Popen(
            _sudo_argv([
                "tar", "xf", "-", "-C", str(root),
                "--keep-directory-symlink", "--no-same-owner",
            ]),
            stdin=src.stdout,
        )
    finally:
        # Let the reader see EOF/SIGPIPE if the writer dies.
        src.stdout.close()

    dst_rc = dst.wait()
    src_rc = src.wait()
    if src_rc != 0:
        raise LocalInstallError(
            f"Reading staging tree failed (tar rc={src_rc})"
        )
    if dst_rc != 0:
        raise LocalInstallError(
            f"Installing into {root} failed (tar rc={dst_rc})"
        )


def run_depmod_ldconfig(kver: str | None = None) -> None:
    """Refresh module deps and the linker cache after a change to /."""
    depmod = ["depmod", "-a"]
    if kver:
        depmod.append(kver)
    r = sudo_run(depmod, check=False, quiet=True)
    if r.returncode != 0:
        log.warning("depmod exited %d: %s", r.returncode, r.stderr.strip())
    r = sudo_run(["ldconfig"], check=False, quiet=True)
    if r.returncode != 0:
        log.warning("ldconfig exited %d: %s", r.returncode, r.stderr.strip())


def write_manifest(
    image: LocalImage,
    staging: Path,
    lustre_tree: Path,
    kver: str,
    files: list[str],
    dirs: list[str],
    path: Path | None = None,
) -> None:
    """Record what we installed so uninstall can undo exactly that."""
    from ltvm_pkg.priv import atomic_write

    path = MANIFEST_PATH if path is None else path

    payload = {
        "schema": MANIFEST_SCHEMA,
        "installed": int(time.time()),
        "image": asdict(image),
        "lustre_tree": str(lustre_tree),
        "staging": str(staging),
        "kernel_version": kver,
        "files": files,
        "dirs": dirs,
    }
    atomic_write(path, json.dumps(payload, indent=2) + "\n")


def read_manifest(path: Path | None = None) -> dict | None:
    path = MANIFEST_PATH if path is None else path
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise LocalInstallError(f"Unreadable install manifest {path}: {e}")
    if raw.get("schema") != MANIFEST_SCHEMA:
        raise LocalInstallError(
            f"Install manifest {path} has schema {raw.get('schema')!r}, "
            f"expected {MANIFEST_SCHEMA!r}"
        )
    return raw


# ----------------------------------------------------------------------
# Uninstall
# ----------------------------------------------------------------------


def loaded_lustre_modules(
    proc_modules: Path | None = None,
) -> list[str]:
    """Which of Lustre's modules are currently loaded."""
    proc_modules = (
        Path("/proc/modules") if proc_modules is None else proc_modules
    )
    try:
        text = proc_modules.read_text()
    except OSError:
        return []
    loaded = {line.split()[0] for line in text.splitlines() if line.split()}
    return [m for m in _LUSTRE_MODULES if m in loaded]


def unload_lustre_modules() -> tuple[bool, str]:
    """Best-effort unload before removing the .ko files.

    Returns (clean, message).  Removing a loaded module's file is
    harmless in itself -- the copy in memory keeps running -- but it
    leaves a machine whose loaded Lustre no longer matches anything on
    disk, which is exactly the state that makes a later reinstall look
    like it didn't work.  So we try, and we say so when we fail.
    """
    still = loaded_lustre_modules()
    if not still:
        return True, "no Lustre modules loaded"

    if shutil.which("lustre_rmmod"):
        sudo_run(["lustre_rmmod"], check=False, quiet=True)
    else:
        for mod in _LUSTRE_MODULES:
            sudo_run(["modprobe", "-r", mod], check=False, quiet=True)

    still = loaded_lustre_modules()
    if still:
        return False, (
            "still loaded after unload attempt: " + ", ".join(still)
            + " (unmount Lustre and stop any targets first)"
        )
    return True, "unloaded Lustre modules"


def _safe_relative(rel: str) -> bool:
    """Reject anything that isn't a plain path under the root.

    The manifest is ours, but it lives in a writable file on a machine
    people poke at, and every entry becomes an `rm` argument.
    """
    if not rel or rel.startswith("/") or rel.startswith("-"):
        return False
    # Path(".").parts is (), and Path("a/../b").parts keeps the "..",
    # so an empty tuple means "this names no file" just as surely as
    # a traversal component does.
    parts = Path(rel).parts
    return bool(parts) and ".." not in parts and "." not in parts


def remove_installed_files(files: list[str], root: Path = Path("/")) -> int:
    """rm the manifest's files.  Returns the number removed."""
    targets = []
    for rel in files:
        if not _safe_relative(rel):
            log.warning("Skipping suspicious manifest entry: %r", rel)
            continue
        p = root / rel
        if p.is_symlink() or p.is_file():
            targets.append(str(p))

    for i in range(0, len(targets), _RM_CHUNK):
        chunk = targets[i:i + _RM_CHUNK]
        r = sudo_run(["rm", "-f", *chunk], check=False, quiet=True)
        if r.returncode != 0:
            log.warning("rm exited %d: %s", r.returncode, r.stderr.strip())
    return len(targets)


def prune_empty_dirs(dirs: list[str], root: Path = Path("/")) -> int:
    """rmdir the directories the install created, deepest first.

    rmdir refuses a non-empty directory, so a path shared with the
    base image (/usr/sbin, /usr/lib/modules/<kver>) survives on its
    own; _NEVER_PRUNE covers the ones where being wrong would be
    unrecoverable rather than merely annoying.
    """
    removed = 0
    for rel in dirs:
        if not _safe_relative(rel) or rel in _NEVER_PRUNE:
            continue
        p = root / rel
        if not p.is_dir() or p.is_symlink():
            continue
        if any(p.iterdir()):
            continue
        r = sudo_run(["rmdir", str(p)], check=False, quiet=True)
        if r.returncode == 0:
            removed += 1
    return removed
