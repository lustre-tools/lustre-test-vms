"""End-to-end check of `ltvm make-install` / `make-uninstall` using a
REAL Lustre DESTDIR, inside a real ltvm VM.

DELIBERATELY NOT NAMED test_*.py: it installs Lustre into `/`, loads
the modules, and then removes it all again.

Needs a Lustre DESTDIR at /root/staging.  The one `ltvm target fetch`
drops at artifacts/<target>/<arch>/kernels/<kver>/lustre-artifacts/ is
exactly the right shape, and is built for the kernel the VM runs::

    ltvm create co1-mkinst
    scp -r . co1-mkinst:/root/ltvm
    tar cf - -C artifacts/<t>/<a>/kernels/<k>/lustre-artifacts . |
        ssh co1-mkinst 'mkdir -p /root/staging && tar xf - -C /root/staging'
    ssh co1-mkinst 'cd /root/lustre-release &&
        python3 /root/ltvm/tests/e2e/make_install_vm.py'

Unlike a synthetic tree this exercises the parts only real content
reaches: depmod over 66 real .ko files, `modprobe lustre` actually
loading, and make-uninstall unloading a live Lustre.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo))

from ltvm_pkg.local_install import (  # noqa: E402  (needs sys.path above)
    check_is_ltvm_machine,
    install_staging_into_root,
    loaded_lustre_modules,
    read_manifest,
    resolve_local_image,
    run_depmod_ldconfig,
    staging_contents,
    write_manifest,
)

FAILS = []


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILS.append(label)


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True)


kver = os.uname().release
staging = Path("/root/staging")

print(f"== Real ltvm VM: {os.uname().nodename}, kernel {kver}")
print("\n== 1. Guards on the real machine")
ev = check_is_ltvm_machine()
check(f"machine guard passes ({ev})", bool(ev))
img = resolve_local_image(explicit_target="rocky9")
check(f"target resolved: {img.target}/{img.kernel}", img.target == "rocky9")

print("\n== 2. Inventory a real Lustre DESTDIR")
files, dirs = staging_contents(staging)
kos = [f for f in files if f.endswith(".ko")]
check(f"found {len(kos)} real .ko modules", len(kos) > 50)
check("found mount.lustre", any(f.endswith("sbin/mount.lustre") for f in files))
check(
    "modules target the RUNNING kernel",
    any(f"lib/modules/{kver}/" in f for f in files),
)

print("\n== 3. Install onto the real VM root")
install_staging_into_root(staging)
run_depmod_ldconfig(kver)
check("/lib still a symlink", Path("/lib").is_symlink())
check("lfs installed", Path("/usr/bin/lfs").is_file())
r = sh("/usr/bin/lfs", "--version")
check(f"lfs runs: {r.stdout.strip()[:50]}", r.returncode == 0)
check("mount.lustre present", Path("/usr/sbin/mount.lustre").exists())
dep = Path(f"/lib/modules/{kver}/modules.dep").read_text()
check("depmod picked up lustre.ko", "lustre.ko" in dep)

print("\n== 4. Actually load the modules (the real test of depmod)")
r = sh("modprobe", "lustre")
check(
    f"modprobe lustre rc={r.returncode} {r.stderr.strip()[:80]}",
    r.returncode == 0,
)
loaded = loaded_lustre_modules()
print(f"     loaded: {', '.join(loaded) or 'none'}")
check("lustre module is live", "lustre" in loaded)
check("lnet came up as a dependency", "lnet" in loaded)
r = sh("lctl", "get_param", "-n", "version")
check(f"lctl talks to the module: {r.stdout.strip()[:40]}", r.returncode == 0)

write_manifest(img, staging, Path("/root/lustre-release"), kver, files, dirs)
check("manifest written", read_manifest() is not None)

print("\n== 5. Uninstall through the REAL CLI command")
os.chdir("/root/lustre-release")
from ltvm_pkg.cli.make import cmd_make_uninstall  # noqa: E402

ns = argparse.Namespace(
    json=False,
    target=None,
    variant=None,
    kernel=None,
    arch=None,
    force=False,
    force_compat=False,
    lustre_tree=None,
    jobs=None,
    rebuild=False,
    no_unload=False,
)
rc = cmd_make_uninstall(ns)
check(f"make-uninstall returned {rc}", rc == 0)

print("\n== 6. Verify the machine is clean")
still = loaded_lustre_modules()
print(f"     still loaded: {', '.join(still) or 'none'}")
check("modules were unloaded", still == [])
check("lfs removed", not Path("/usr/bin/lfs").exists())
check("mount.lustre removed", not Path("/usr/sbin/mount.lustre").exists())
check(
    "no lustre .ko left",
    not list(Path(f"/lib/modules/{kver}/extra").rglob("lustre.ko")),
)
check("manifest cleared", read_manifest() is None)
check("/lib still a symlink", Path("/lib").is_symlink())
check(
    "shared dirs intact", Path("/usr/sbin").is_dir() and Path("/etc").is_dir()
)
check("system still works", sh("ls", "/").returncode == 0)
check(
    "ssh/systemd still fine",
    sh("systemctl", "is-system-running").returncode in (0, 1),
)

print("\n" + "=" * 58)
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
