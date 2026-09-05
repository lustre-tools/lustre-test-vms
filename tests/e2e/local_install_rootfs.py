"""End-to-end check of `ltvm make-install` / `make-uninstall` against a
REAL root filesystem.

DELIBERATELY NOT NAMED test_*.py: it installs into `/` and then deletes
what it installed, so it must never be picked up by a stray
`pytest tests/e2e`.  Run it only inside a machine you can throw away.

What it is really here to catch is the /lib symlink.  On RHEL /lib
points at usr/lib, and an extraction that replaces it with a real
directory strands every library on the system.  A tmpdir test can
mimic that shape; only a real rootfs proves the behaviour.

Run it in a disposable Rocky 9 container::

    podman run --rm -v $PWD:/repo:ro rockylinux:9 bash -c \
        'dnf install -y -q python3 python3-pyyaml kmod && \
         python3 /repo/tests/e2e/local_install_rootfs.py'

or inside an ltvm VM (which is what it is modelled on)::

    ltvm create co1-single && scp -r . co1-single:/repo
    ssh co1-single 'python3 /repo/tests/e2e/local_install_rootfs.py'

Exits non-zero on the first failed expectation, listing every one.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# Works both mounted at /repo and run straight from a checkout.
_repo = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo))

from ltvm_pkg.local_install import (  # noqa: E402
    IMAGE_STAMP_SCHEMA,
    LocalImage,
    check_is_ltvm_machine,
    install_staging_into_root,
    loaded_lustre_modules,
    prune_empty_dirs,
    read_image_stamp,
    read_manifest,
    remove_installed_files,
    run_depmod_ldconfig,
    staging_contents,
    write_manifest,
)

FAILS = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    if not cond:
        FAILS.append(label)


kver = os.uname().release
print(f"== Host: {os.uname().sysname} {kver}, root={os.geteuid() == 0}")

# ---------------------------------------------------------------- 0
print("\n== 0. Preconditions (this is a real RHEL-family rootfs)")
check("/lib is a symlink (the case that makes tar -C / dangerous)",
      Path("/lib").is_symlink())
check("running as root", os.geteuid() == 0)

# ---------------------------------------------------------------- 1
print("\n== 1. Machine identity")
Path("/etc/ltvm-image.json").write_text(json.dumps({
    "schema": IMAGE_STAMP_SCHEMA,
    "target": "rocky9",
    "arch": os.uname().machine,
    "variant": "base",
    "kernel": "5.14-rhel9.7-1.el9",
    "kernel_version": kver,
    "os_family": "rhel",
    "built": 1,
}))
img = read_image_stamp()
check("stamp reads back", img is not None and img.target == "rocky9")
try:
    check_is_ltvm_machine()
    check("guard accepts a stamped machine", True)
except Exception as e:
    check(f"guard accepts a stamped machine ({e})", False)

# ---------------------------------------------------------------- 2
print("\n== 2. Build a Lustre-shaped DESTDIR")
staging = Path("/tmp/staging")
mod_dir = staging / "lib" / "modules" / kver / "extra" / "lustre"
mod_dir.mkdir(parents=True)
(staging / "usr" / "sbin").mkdir(parents=True)
(staging / "usr" / "bin").mkdir(parents=True)
(staging / "usr" / "lib64").mkdir(parents=True)
(staging / "etc").mkdir(parents=True)

(staging / "usr" / "sbin" / "mount.lustre").write_text("#!/bin/sh\necho mount\n")
os.chmod(staging / "usr" / "sbin" / "mount.lustre", 0o755)
(staging / "usr" / "bin" / "lfs").write_text("#!/bin/sh\necho lfs\n")
os.chmod(staging / "usr" / "bin" / "lfs", 0o755)
(staging / "usr" / "sbin" / "mount.lustre_tgt").symlink_to("mount.lustre")
(staging / "usr" / "lib64" / "liblustreapi.so").write_bytes(b"\x7fELF fake")
(staging / "etc" / "ldev.conf").write_text("# lustre\n")
(mod_dir / "lustre.ko").write_bytes(b"\x7fELF fake ko")
(mod_dir / "obdclass.ko").write_bytes(b"\x7fELF fake ko")
(staging / ".ltvm-staging-stamp").write_text("build marker")

# Sentinels that must survive the whole cycle untouched.
Path("/usr/sbin/UNRELATED-BINARY").write_text("do not delete me\n")
Path(f"/lib/modules/{kver}/extra").mkdir(parents=True, exist_ok=True)
Path(f"/lib/modules/{kver}/extra/UNRELATED.ko").write_bytes(b"keep")

files, dirs = staging_contents(staging)
check("inventory found the module", f"lib/modules/{kver}/extra/lustre/lustre.ko" in files)
check("inventory found the symlink", "usr/sbin/mount.lustre_tgt" in files)
check("inventory skipped the build marker",
      not any(f.startswith(".ltvm-") for f in files))
check("dirs are deepest-first",
      [d.count("/") for d in dirs] == sorted([d.count("/") for d in dirs], reverse=True))

# ---------------------------------------------------------------- 3
print("\n== 3. Install onto the real /")
install_staging_into_root(staging)
run_depmod_ldconfig(kver)

check("/lib is STILL a symlink after extraction", Path("/lib").is_symlink())
check("mount.lustre installed", Path("/usr/sbin/mount.lustre").is_file())
check("mount.lustre is executable",
      os.access("/usr/sbin/mount.lustre", os.X_OK))
check("mount.lustre actually runs",
      subprocess.run(["/usr/sbin/mount.lustre"], capture_output=True,
                     text=True).stdout.strip() == "mount")
check("lfs installed", Path("/usr/bin/lfs").is_file())
check("symlink preserved as a symlink",
      Path("/usr/sbin/mount.lustre_tgt").is_symlink())
check("library installed", Path("/usr/lib64/liblustreapi.so").is_file())
check("config installed", Path("/etc/ldev.conf").is_file())
check("module landed through the /lib symlink",
      Path(f"/usr/lib/modules/{kver}/extra/lustre/lustre.ko").is_file())
check("sentinel binary untouched", Path("/usr/sbin/UNRELATED-BINARY").is_file())
check("sentinel module untouched",
      Path(f"/lib/modules/{kver}/extra/UNRELATED.ko").is_file())

write_manifest(
    LocalImage("rocky9", os.uname().machine, "base", "5.14-rhel9.7-1.el9",
               kver, "rhel", "stamp"),
    staging, Path("/root/lustre-release"), kver, files, dirs,
)
m = read_manifest()
check("manifest written and re-read", m is not None and len(m["files"]) == len(files))
check("manifest at the documented path",
      Path("/var/lib/ltvm/lustre-install.json").is_file())

# ---------------------------------------------------------------- 4
print("\n== 4. Uninstall")
check("no Lustre modules loaded (nothing real to unload)",
      loaded_lustre_modules() == [])
removed = remove_installed_files(m["files"])
pruned = prune_empty_dirs(m["dirs"])
run_depmod_ldconfig(m["kernel_version"])
print(f"     removed {removed} files, pruned {pruned} dirs")

check("removed everything it installed", removed == len(files))
check("mount.lustre gone", not Path("/usr/sbin/mount.lustre").exists())
check("symlink gone", not Path("/usr/sbin/mount.lustre_tgt").is_symlink())
check("library gone", not Path("/usr/lib64/liblustreapi.so").exists())
check("config gone", not Path("/etc/ldev.conf").exists())
check("module gone (removed through the /lib symlink)",
      not Path(f"/usr/lib/modules/{kver}/extra/lustre/lustre.ko").exists())
check("lustre module dir pruned",
      not Path(f"/lib/modules/{kver}/extra/lustre").exists())

check("SENTINEL binary survived uninstall",
      Path("/usr/sbin/UNRELATED-BINARY").is_file())
check("SENTINEL module survived uninstall",
      Path(f"/lib/modules/{kver}/extra/UNRELATED.ko").is_file())
check("shared dir /usr/sbin NOT pruned", Path("/usr/sbin").is_dir())
check("shared dir /etc NOT pruned", Path("/etc").is_dir())
check("/lib STILL a symlink after uninstall", Path("/lib").is_symlink())
check("system still works (ls runs)",
      subprocess.run(["ls", "/"], capture_output=True).returncode == 0)

# ---------------------------------------------------------------- 5
print("\n== 5. CLI uninstall path, with no manifest left")
import argparse  # noqa: E402

from ltvm_pkg.cli.make import (  # noqa: E402
    cmd_make_uninstall,
)
from ltvm_pkg.priv import sudo_run  # noqa: E402

sudo_run(["rm", "-f", "/var/lib/ltvm/lustre-install.json"], check=False, quiet=True)
ns = argparse.Namespace(json=False, target=None, variant=None, kernel=None,
                        arch=None, force=False, force_compat=False,
                        lustre_tree=None, jobs=None, rebuild=False,
                        no_unload=False)
check("make-uninstall with nothing installed is a clean error",
      cmd_make_uninstall(ns) != 0)

print("\n" + "=" * 60)
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
