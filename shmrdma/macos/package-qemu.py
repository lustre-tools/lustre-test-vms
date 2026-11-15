#!/usr/bin/env python3
"""Package a QEMU install tree for macOS as a self-contained tarball.

    package-qemu.py STAGE ENTITLEMENTS OUT.tar.gz

STAGE is a `ninja install` tree laid out as <prefix>: bin/, share/qemu/.
Every non-system dylib the binaries load (Homebrew's glib, pixman, ...)
is copied into STAGE/lib, recursively, and every reference is rewritten
to @loader_path, so the tree runs from wherever it is unpacked with no
Homebrew at all.  install_name_tool breaks code signatures, so each
dylib is re-signed ad hoc and each qemu-system-* binary is re-signed
with the Hypervisor entitlement (ENTITLEMENTS, QEMU's
accel/hvf/entitlements.plist), which HVF needs.

Writes OUT.tar.gz (bin, lib, share) and OUT.tar.gz.sha256 next to it.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

SYSTEM = ("/usr/lib/", "/System/")


def run(*cmd: str) -> str:
    return subprocess.run(
        cmd, check=True, capture_output=True, text=True
    ).stdout


def deps(path: Path) -> list[str]:
    """Non-system libraries a Mach-O file loads (not its own install name)."""
    out = run("otool", "-L", str(path)).splitlines()[1:]
    names = [line.split()[0] for line in out if line.strip()]
    own = run("otool", "-D", str(path)).splitlines()[1:]
    return [
        n
        for n in names
        if not n.startswith(SYSTEM) and not n.startswith("@") and n not in own
    ]


def bundle(stage: Path) -> list[Path]:
    """Copy every dylib the binaries need into stage/lib; rewrite refs."""
    lib = stage / "lib"
    lib.mkdir(exist_ok=True)
    binaries = sorted((stage / "bin").glob("qemu-system-*"))
    todo = list(binaries)
    copied: dict[str, Path] = {}  # original install name -> bundled file
    done: set[Path] = set()
    while todo:
        f = todo.pop()
        if f in done:
            continue
        done.add(f)
        for name in deps(f):
            if name not in copied:
                src = Path(name).resolve()
                dst = lib / Path(name).name
                shutil.copy2(src, dst)
                dst.chmod(0o755)
                copied[name] = dst
                todo.append(dst)
            # binaries live in bin/, dylibs in lib/
            rel = (
                "@loader_path/../lib/"
                if f.parent.name == "bin"
                else ("@loader_path/")
            )
            run(
                "install_name_tool",
                "-change",
                name,
                rel + Path(name).name,
                str(f),
            )
    for dst in copied.values():
        run("install_name_tool", "-id", "@rpath/" + dst.name, str(dst))
    return binaries


def sign(stage: Path, binaries: list[Path], entitlements: Path) -> None:
    for dylib in sorted((stage / "lib").glob("*.dylib")):
        run("codesign", "--force", "--sign", "-", str(dylib))
    for b in binaries:
        run(
            "codesign",
            "--force",
            "--sign",
            "-",
            "--entitlements",
            str(entitlements),
            str(b),
        )


def check(stage: Path) -> None:
    """No reference into Homebrew may remain, and it must run."""
    for f in [*(stage / "bin").iterdir(), *(stage / "lib").iterdir()]:
        if f.is_file() and not f.is_symlink():
            try:
                left = deps(f)
            except subprocess.CalledProcessError:
                continue  # not Mach-O (scripts)
            if left:
                sys.exit(f"{f} still loads {left}")
    for b in sorted((stage / "bin").glob("qemu-system-*")):
        out = run(str(b), "-M", "none", "-device", "help")
        if '"vhost-user-fs-pci"' not in out:
            sys.exit(f"{b} has no vhost-user-fs-pci")
        # -netdev help lists vhost-user even when it is not built in;
        # a build with it complains about the missing chardev instead
        r = subprocess.run(
            [
                str(b),
                "-M",
                "none",
                "-display",
                "none",
                "-netdev",
                "vhost-user,id=probe,chardev=probe",
            ],
            capture_output=True,
            text=True,
        )
        if "not compiled" in r.stderr:
            sys.exit(f"{b} has no -netdev vhost-user")


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    stage, ent, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    # ninja install leaves an unsigned twin of each binary, and the
    # internal libfdt's static library; drop them
    for f in (stage / "bin").glob("*-unsigned"):
        f.unlink()
    for f in [*(stage / "lib").glob("*.a"), stage / "lib" / "pkgconfig"]:
        shutil.rmtree(f) if f.is_dir() else f.unlink()
    shutil.rmtree(stage / "include", ignore_errors=True)
    binaries = bundle(stage)
    sign(stage, binaries, ent)
    check(stage)
    with tarfile.open(out, "w:gz") as tar:
        for part in ("bin", "lib", "share"):
            tar.add(stage / part, arcname=part)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    Path(str(out) + ".sha256").write_text(f"{digest}  {out.name}\n")
    print(f"{out} {digest}")


if __name__ == "__main__":
    main()
