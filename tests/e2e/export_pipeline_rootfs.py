"""End-to-end check of `ltvm target export`, including --format gce.

DELIBERATELY NOT NAMED test_*.py: it drives losetup, parted and mount
against real block devices.  Run it in a machine you can throw away --
an ltvm VM is ideal::

    ssh <vm> 'dnf install -y parted grub2-tools e2fsprogs qemu-img'
    scp -r . <vm>:/root/ltvm
    ssh <vm> 'python3 /root/ltvm/tests/e2e/export_pipeline_rootfs.py'

It builds its own small rootfs rather than needing built artifacts, so
it runs anywhere with loop devices and root.

grub2-install is stubbed: --target=i386-pc needs grub2-pc-modules,
which is not packaged for aarch64, and that step is unchanged
pre-existing code.  Everything around it -- partitioning, mkfs, the
rootfs copy, the fstab UUID rewrite, the GCE guest config, the ssh
key injection, and the oldgnu sparse tarball -- is real.
"""

import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo))

import ltvm_pkg.image_export as ie  # noqa: E402  (needs sys.path above)

FAILS = []
def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILS.append(label)

def sh(*a, **kw):
    return subprocess.run(a, capture_output=True, text=True, **kw)

KVER = "5.14.0-503.40.1.el9_5_lustre"
work = Path("/root/exp")
sh("rm", "-rf", str(work))
work.mkdir()

# ---- a small but structurally real rootfs -------------------------
print("== 1. Build a synthetic base.ext4")
rootfs = work / "rootfs"
for d in ("etc", "boot", "root/.ssh", "usr/lib/systemd/system",
          "usr/sbin", "usr/lib"):
    (rootfs / d).mkdir(parents=True, exist_ok=True)
# The fstab ltvm images actually ship, plus an extra mount to prove
# the rewrite is surgical.
(rootfs / "etc/fstab").write_text(
    "# ltvm\n"
    "/dev/vda  /  ext4  defaults,noatime  0 1\n"
    "/dev/vdb1  /mnt/scratch  ext4  defaults  0 2\n")
(rootfs / "usr/lib/systemd/system/NetworkManager.service").write_text(
    "[Unit]\nDescription=NM\n[Install]\nWantedBy=multi-user.target\n")
(rootfs / "root/.ssh/authorized_keys").write_text(
    "ssh-ed25519 AAAASHAREDKEY ltvm-shared\n")
(rootfs / "usr/sbin/mount.lustre").write_text("#!/bin/sh\n")
base = work / "base.ext4"
sh("truncate", "-s", "220M", str(base))
r = sh("mkfs.ext4", "-q", "-F", "-d", str(rootfs), str(base))
check(f"base.ext4 built rc={r.returncode} {r.stderr.strip()[:60]}", r.returncode == 0)

kdir = work / "kernels" / "k1"
(kdir / "build-tree/include/config").mkdir(parents=True)
(kdir / "build-tree/include/config/kernel.release").write_text(KVER + "\n")
sh("truncate", "-s", "8M", str(kdir / "vmlinuz"))
sh("truncate", "-s", "12M", str(kdir / f"initramfs-{KVER}.img"))
imgdir = work / "images" / "k1"
imgdir.mkdir(parents=True)
os.replace(str(base), str(imgdir / "base.ext4"))

sshkey = work / "id_test.pub"
sshkey.write_text("ssh-ed25519 AAAAMYTESTKEY me@laptop\n")

# ---- stub only grub2-install --------------------------------------
stub = work / "bin"
stub.mkdir()
(stub / "grub2-install").write_text(
    "#!/bin/sh\n# stub: aarch64 has no i386-pc modules\n"
    'for a in "$@"; do case "$a" in --boot-directory=*) B="${a#*=}";; esac; done\n'
    'mkdir -p "$B/grub2/i386-pc" && echo stub > "$B/grub2/i386-pc/core.img"\n'
    "exit 0\n")
os.chmod(stub / "grub2-install", 0o755)
os.environ["PATH"] = f"{stub}:{os.environ['PATH']}"

class Shim:
    name = "rocky9"
    def resolve_kernel(self, k=None): return "k1"
    def image_output_dir(self, k=None): return imgdir
    def kernel_output_dir(self, k=None): return kdir

# ---- the real export ----------------------------------------------
print("\n== 2. Real export pipeline, --format gce")
out = work / "gce-k1.tar.gz"
result = ie.export_image(Shim(), None, out, image_format="gce",
                         ssh_key=sshkey, force=True)
check("export produced the tarball", result.exists())

print("\n== 3. The tarball GCE will receive")
with tarfile.open(out, "r:gz") as tf:
    members = tf.getmembers()
check(f"exactly one member, named disk.raw: {[m.name for m in members]}",
      [m.name for m in members] == ["disk.raw"])
raw_size = members[0].size
GIB = 1024 ** 3
check(f"disk.raw is a whole number of GiB ({raw_size / GIB:.2f} GiB)",
      raw_size % GIB == 0)
check(f"tarball stayed small via sparse packing "
      f"({out.stat().st_size / 1e6:.1f} MB for {raw_size / GIB:.0f} GiB)",
      out.stat().st_size < raw_size / 4)

print("\n== 4. Unpack and inspect the produced disk")
sh("tar", "xzf", str(out), "-C", str(work))
disk = work / "disk.raw"
r = sh("parted", "-s", str(disk), "print")
check("msdos label with one bootable partition",
      "msdos" in r.stdout and "boot" in r.stdout)

loop = sh("losetup", "--show", "-f", "-P", str(disk)).stdout.strip()
check(f"loop attached: {loop}", loop.startswith("/dev/loop"))
part = loop + "p1"
mnt = work / "mnt"
mnt.mkdir()
try:
    r = sh("mount", part, str(mnt))
    check(f"root partition mounts rc={r.returncode}", r.returncode == 0)
    uuid = sh("blkid", "-s", "UUID", "-o", "value", part).stdout.strip()

    fstab = (mnt / "etc/fstab").read_text()
    print("     fstab now:\n       " + "\n       ".join(fstab.splitlines()))
    check("fstab / entry rewritten to the real filesystem UUID",
          f"UUID={uuid}" in fstab)
    check("no /dev/vda left on the / line",
          not any(line.split()[:2] == ["/dev/vda", "/"]
                  for line in fstab.splitlines() if line.split()))
    check("mount options preserved", "defaults,noatime" in fstab)
    check("the other fstab entry untouched", "/dev/vdb1" in fstab)
    check("comment preserved", fstab.startswith("# ltvm"))

    nm = mnt / "etc/NetworkManager/system-connections/ltvm-gce.nmconnection"
    check("GCE NetworkManager profile written", nm.is_file())
    check("profile is DHCP on eth0",
          "method=auto" in nm.read_text() and "interface-name=eth0" in nm.read_text())
    check(f"profile mode is 0600 (NM ignores it otherwise): "
          f"{oct(nm.stat().st_mode & 0o777)}",
          nm.stat().st_mode & 0o777 == 0o600)

    want = mnt / "etc/systemd/system/multi-user.target.wants/NetworkManager.service"
    check("NetworkManager enabled via .wants symlink", want.is_symlink())
    check(f"symlink points at the unit: {os.readlink(want)}",
          os.readlink(want) == "/usr/lib/systemd/system/NetworkManager.service")

    auth = (mnt / "root/.ssh/authorized_keys").read_text()
    check("injected key present", "AAAAMYTESTKEY" in auth)
    check("shared ltvm key NOT clobbered", "AAAASHAREDKEY" in auth)

    check("kernel copied into /boot", (mnt / f"boot/vmlinuz-{KVER}").is_file())
    check("initramfs copied into /boot",
          (mnt / f"boot/initramfs-{KVER}.img").is_file())
    grubcfg = mnt / "boot/grub2/grub.cfg"
    check("grub.cfg written", grubcfg.is_file())
    check("grub.cfg roots on the same UUID as fstab",
          f"root=UUID={uuid}" in grubcfg.read_text())
    check("rootfs contents carried over",
          (mnt / "usr/sbin/mount.lustre").is_file())
finally:
    sh("umount", str(mnt))
    sh("losetup", "-d", loop)

print("\n== 5. qcow2 export still works (and gets the fstab fix too)")
out2 = work / "bootable.qcow2"
ie.export_image(Shim(), None, out2, image_format="qcow2", force=True)
check("qcow2 produced", out2.exists())
r = sh("qemu-img", "info", "--output=json", str(out2))
if r.returncode == 0:
    info = json.loads(r.stdout)
    check(f"qemu-img says qcow2 ({info.get('format')})",
          info.get("format") == "qcow2")
else:
    print("  [SKIP] qemu-img not installed")

print("\n" + "=" * 58)
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
