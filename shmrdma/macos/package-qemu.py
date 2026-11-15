#!/usr/bin/env python3
"""Package a QEMU install tree for macOS as a self-contained tarball.

    package-qemu.py STAGE ENTITLEMENTS OUT.tar.gz QEMU_SRC PATCH

STAGE is a `ninja install` tree laid out as <prefix>: bin/, share/qemu/.
Every non-system dylib the binaries load (Homebrew's glib, pixman, ...)
is copied into STAGE/lib, recursively, and every reference is rewritten
to @loader_path, so the tree runs from wherever it is unpacked with no
Homebrew at all.  install_name_tool breaks code signatures, so each
dylib is re-signed ad hoc and each qemu-system-* binary is re-signed
with the Hypervisor entitlement (ENTITLEMENTS, QEMU's
accel/hvf/entitlements.plist), which HVF needs.

share/doc/ltvm-qemu/ records what is in the tarball: VERSIONS (each
component, its version, license and source), the license files of QEMU
(QEMU_SRC) and of each bundled Homebrew package, and PATCH, the change
made to QEMU.  A bundled package with no license file anywhere stops
the packaging.

Writes OUT.tar.gz (bin, lib, share) and OUT.tar.gz.sha256 next to it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

SYSTEM = ("/usr/lib/", "/System/")

LICENSE_GLOBS = ("COPYING*", "LICEN[CS]E*", "NOTICE*", "COPYRIGHT*")

# Homebrew kegs that install no license file: where upstream keeps it,
# for the installed version.
LICENSE_URLS = {
    "glib": "https://gitlab.gnome.org/GNOME/glib/-/raw/{v}/LICENSES/"
    "LGPL-2.1-or-later.txt",
    "libslirp": "https://gitlab.freedesktop.org/slirp/libslirp/-/raw/"
    "v{v}/COPYRIGHT",
}


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
        n for n in names
        if not n.startswith(SYSTEM) and not n.startswith("@") and n not in own
    ]


def bundle(stage: Path) -> tuple[list[Path], dict[str, Path]]:
    """Copy every dylib the binaries need into stage/lib; rewrite refs.

    Returns the binaries and, per bundled dylib name, the file it was
    copied from (in Homebrew's Cellar)."""
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
            rel = "@loader_path/../lib/" if f.parent.name == "bin" else (
                "@loader_path/"
            )
            run(
                "install_name_tool", "-change", name,
                rel + Path(name).name, str(f),
            )
    for dst in copied.values():
        run("install_name_tool", "-id", "@rpath/" + dst.name, str(dst))
    origins = {Path(n).name: Path(n).resolve() for n in copied}
    return binaries, origins


def _keg(path: Path) -> tuple[str, str, Path]:
    """(formula, installed version, keg dir) of a file in Homebrew's Cellar."""
    parts = path.parts
    i = parts.index("Cellar")
    return parts[i + 1], parts[i + 2], Path(*parts[: i + 3])


def write_docs(
    stage: Path, origins: dict[str, Path], qemu_src: Path, patch: Path
) -> None:
    """share/doc/ltvm-qemu: VERSIONS, licenses/, and the QEMU patch."""
    doc = stage / "share" / "doc" / "ltvm-qemu"
    lic = doc / "licenses"
    lic.mkdir(parents=True, exist_ok=True)
    qver = (qemu_src / "VERSION").read_text().strip()
    lines = [
        "component  version  license  source  (files in this tarball)",
        "",
        f"qemu  {qver}  GPL-2.0-only  "
        f"https://download.qemu.org/qemu-{qver}.tar.xz + {patch.name}  "
        f"(bin/, share/qemu)",
    ]
    (lic / "qemu").mkdir()
    for name in ("COPYING", "COPYING.LIB", "LICENSE"):
        if (qemu_src / name).is_file():
            shutil.copy2(qemu_src / name, lic / "qemu" / name)
    shutil.copy2(patch, doc / patch.name)

    kegs: dict[str, tuple[str, Path, list[str]]] = {}
    for lib_name, src in sorted(origins.items()):
        formula, version, keg = _keg(src)
        kegs.setdefault(formula, (version, keg, []))[2].append(lib_name)
    info = json.loads(run("brew", "info", "--json=v2", *sorted(kegs)))
    by_name = {f["name"]: f for f in info["formulae"]}
    for formula, (version, keg, libs) in sorted(kegs.items()):
        f = by_name[formula]
        upstream = version.split("_")[0]  # Homebrew's _N is its rebuild
        stable = f["urls"]["stable"]
        if f["versions"]["stable"] == upstream:
            source = f"{stable['url']} (sha256 {stable.get('checksum')})"
        else:
            source = (
                f"upstream {formula} {upstream} (Homebrew's formula has "
                f"moved on to {f['versions']['stable']})"
            )
        lines.append(
            f"{formula}  {upstream}  {f.get('license')}  {source}  "
            f"(lib/{', lib/'.join(libs)})"
        )
        dest = lic / formula
        dest.mkdir()
        found = [
            p for g in LICENSE_GLOBS for p in keg.glob(g) if p.is_file()
        ]
        for p in found:
            shutil.copy2(p, dest / p.name)
        if not found:
            url = LICENSE_URLS.get(formula)
            if not url:
                sys.exit(
                    f"{formula} {version}: no license file in {keg} and no "
                    f"upstream URL in LICENSE_URLS; add one"
                )
            url = url.format(v=upstream)
            with urllib.request.urlopen(url, timeout=60) as r:
                (dest / Path(url).name).write_bytes(r.read())
    lines.append("")
    lines.append(
        "Homebrew libraries come from its bottles; each source is the "
        "upstream release named, built per its Homebrew formula."
    )
    (doc / "VERSIONS").write_text("\n".join(lines) + "\n")


def sign(stage: Path, binaries: list[Path], entitlements: Path) -> None:
    for dylib in sorted((stage / "lib").glob("*.dylib")):
        run("codesign", "--force", "--sign", "-", str(dylib))
    for b in binaries:
        run(
            "codesign", "--force", "--sign", "-",
            "--entitlements", str(entitlements), str(b),
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
            [str(b), "-M", "none", "-display", "none",
             "-netdev", "vhost-user,id=probe,chardev=probe"],
            capture_output=True, text=True,
        )
        if "not compiled" in r.stderr:
            sys.exit(f"{b} has no -netdev vhost-user")


def main() -> None:
    if len(sys.argv) != 6:
        sys.exit(__doc__)
    stage, ent, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    qemu_src, patch = Path(sys.argv[4]), Path(sys.argv[5])
    # ninja install leaves an unsigned twin of each binary, and the
    # internal libfdt's static library; drop them
    for f in (stage / "bin").glob("*-unsigned"):
        f.unlink()
    for f in [*(stage / "lib").glob("*.a"), stage / "lib" / "pkgconfig"]:
        shutil.rmtree(f) if f.is_dir() else f.unlink()
    shutil.rmtree(stage / "include", ignore_errors=True)
    binaries, origins = bundle(stage)
    write_docs(stage, origins, qemu_src, patch)
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
