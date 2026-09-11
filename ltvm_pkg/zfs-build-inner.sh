#!/bin/bash
# Runs inside the target's build container.  Configures and builds
# OpenZFS against the ltvm kernel build-tree mounted at /kernel, then
# installs it into /zfs-staging as a DESTDIR tree.
#
# Two outputs, two consumers:
#   /zfs-src      stays configured+built in place.  `ltvm build lustre
#                 --zfs` bind-mounts it and passes --with-zfs, which
#                 wants zfs_config.h, module/Module.symvers and the
#                 in-tree libspl/libzfs headers and .libs.
#   /zfs-staging  the `make install DESTDIR=` tree that deploy-lustre
#                 streams into a VM (kmods + zpool/zfs/zdb + libs).
#
# Inputs (env):
#   KVER  -- kernel release string, e.g. 5.14.0-611.13.1.el9_7_lustre
#   JOBS  -- parallel make jobs
#
# Mounts:
#   /kernel      (ro)  kernel build-tree
#   /zfs-src     (rw)  unpacked ZFS source
#   /zfs-staging (rw)  DESTDIR install target

set -euo pipefail

if [[ -z "${KVER:-}" ]]; then
    echo "error: KVER not set" >&2
    exit 2
fi
JOBS=${JOBS:-$(nproc)}

for d in /kernel /zfs-src /zfs-staging; do
    [[ -d "$d" ]] || { echo "error: $d not mounted" >&2; exit 2; }
done

# ZFS build deps the base container does not carry.  Installed here
# rather than in packages-dev.txt so that adding ZFS support does not
# perturb the build-container input hash -- every existing container,
# kernel and image artifact stays valid, and a ZFS build (cached per
# kernel + version) pays this ~20s once.  The rest of what ZFS needs
# (gcc, autoconf, libtool, openssl, zlib, elfutils) is already in
# packages-dev.txt.
echo "==> Installing ZFS build dependencies"
if command -v dnf >/dev/null 2>&1; then
    dnf -y install \
        libtirpc-devel \
        libblkid-devel \
        libuuid-devel \
        libattr-devel \
        systemd-devel \
        2>&1 | tail -3
    # rpm knows where this distro puts libraries (lib64 on x86_64 EL,
    # lib elsewhere); ZFS has to agree with it or the VM's ldconfig
    # will not find libzfs.
    LIBDIR=$(rpm --eval '%{_libdir}')
elif command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq 2>&1 | tail -1
    apt-get install -y --no-install-recommends \
        libtirpc-dev \
        libblkid-dev \
        uuid-dev \
        libattr1-dev \
        libudev-dev \
        libssl-dev \
        zlib1g-dev \
        libelf-dev \
        2>&1 | tail -3
    # Debian is multiarch: libraries live under the GNU triplet, and
    # anything installed to a bare /usr/lib64 is invisible to ldconfig.
    LIBDIR=/usr/lib/$(dpkg-architecture -qDEB_HOST_MULTIARCH)
else
    echo "error: no dnf or apt-get in this container" >&2
    exit 2
fi

cd /zfs-src

# --with-linux-obj as well as --with-linux: ltvm's build-tree is a
# prepared tree that serves as both source and objdir, and ZFS
# defaults the obj path to a sibling it would not find here.
#
# --enable-pyzfs=no: pyzfs is the libzfs_core Python binding.  Lustre
# does not use it, and skipping it keeps python3-devel/setuptools out
# of both the container and the VM.
echo "==> Configuring ZFS (kernel $KVER, libdir $LIBDIR)"
./configure \
    --prefix=/usr \
    --libdir="$LIBDIR" \
    --sbindir=/usr/sbin \
    --sysconfdir=/etc \
    --with-udevdir=/usr/lib/udev \
    --with-systemdunitdir=/usr/lib/systemd/system \
    --with-linux=/kernel \
    --with-linux-obj=/kernel \
    --with-config=all \
    --enable-pyzfs=no \
    --disable-sysvinit \
    --enable-systemd

echo "==> Building ZFS (-j$JOBS)"
make -j"$JOBS"

# Wipe rather than merge: a rebuild at a different version would
# otherwise leave the previous version's libzfs.so.N and kmods
# alongside the new ones, and the VM's ldconfig would have two to
# choose from.
echo "==> Installing to DESTDIR"
rm -rf /zfs-staging/*
make install DESTDIR=/zfs-staging -j"$JOBS"

# The ZFS test suite is ~20 MiB of shell and is not what this artifact
# is for -- Lustre's own suites drive ZFS through mkfs.lustre.  Drop it
# so every deploy-lustre --zfs doesn't stream it over ssh.
rm -rf /zfs-staging/usr/share/zfs/zfs-tests

# `make install` is capable of returning 0 with a partial DESTDIR.
# Check the three things the two consumers actually need before
# letting the host stamp this artifact as good.
missing=()
[[ -n $(find /zfs-staging/lib/modules -name 'zfs.ko*' 2>/dev/null) ]] ||
    missing+=("zfs.ko under /lib/modules")
[[ -x /zfs-staging/usr/sbin/zpool ]] || missing+=("usr/sbin/zpool")
compgen -G "/zfs-staging$LIBDIR/libzfs.so*" >/dev/null ||
    missing+=("libzfs.so under $LIBDIR")
if (( ${#missing[@]} )); then
    echo "error: ZFS install incomplete: ${missing[*]}" >&2
    exit 3
fi

# What Lustre's LB_ZFS probes for.  Failing here rather than inside a
# later Lustre configure keeps the error next to its cause: LB_ZFS
# silently sets enable_zfs=no when these are absent, so Lustre would
# otherwise build *without* ZFS and only surface as a missing
# osd_zfs.ko much later.
for f in zfs_config.h module/Module.symvers include/libzfs.h; do
    [[ -e /zfs-src/$f ]] || {
        echo "error: /zfs-src/$f missing after build" >&2
        exit 3
    }
done

echo "==> ZFS build complete"
