#!/bin/bash
# Runs inside the mofed builder container. Rebuilds the MOFED kmod
# RPMs against the Lustre-patched kernel build-tree mounted at
# /kernel-build-tree, then drops the matching kmod-* RPMs into
# /mofed-kmods-out/.
#
# Inputs (env):
#   KVER -- kernel release string, e.g. 5.14.0-611.13.1.el9_7_lustre
#
# The MOFED bundle is expected at /opt/mofed-src/current (set up by
# mofed.container.Dockerfile).

set -euo pipefail

if [[ -z "${KVER:-}" ]]; then
    echo "error: KVER not set" >&2
    exit 2
fi

if [[ ! -d /kernel-build-tree ]]; then
    echo "error: /kernel-build-tree not mounted" >&2
    exit 2
fi

if [[ ! -d /opt/mofed-src/current ]]; then
    echo "error: /opt/mofed-src/current missing -- container not the mofed builder?" >&2
    exit 2
fi

mkdir -p /mofed-kmods-out

WORK=/tmp/mofed-kmod-build
rm -rf "$WORK"
mkdir -p "$WORK"

# Resolve the symlink: mlnx_add_kernel_support.sh does `cp -a` (which
# preserves symlinks), and /opt/mofed-src/current is a *relative*
# symlink -- after the copy it dangles in tmpdir, the script never
# notices, and the eventual install -> RPMS/ fails.
MOFED_DIR=$(readlink -f /opt/mofed-src/current)
if [[ ! -d "$MOFED_DIR" ]]; then
    echo "error: cannot resolve /opt/mofed-src/current -> $MOFED_DIR" >&2
    exit 2
fi

# Locate the OFED source tarball; mlnx_add_kernel_support.sh needs it
# to recompile the kmods (the bundle ships SRPMs but the script
# expects the upstream-style src tgz).
OFED_SRC=$(ls "$MOFED_DIR"/src/MLNX_OFED_SRC-*.tgz 2>/dev/null | head -1)
if [[ -z "$OFED_SRC" ]]; then
    echo "error: MLNX_OFED_SRC tgz missing under $MOFED_DIR/src" >&2
    exit 2
fi

# Detect distro from the bundle directory name (e.g. rhel9.5) so the
# script doesn't try to auto-detect from the running kernel and end
# up wrong (our kernel uname is el9_7_lustre).
DISTRO=$(basename "$(readlink -f "$MOFED_DIR")" \
    | sed -nE 's/^MLNX_OFED_LINUX-[0-9.-]+-([a-z0-9.]+)-[a-z0-9_]+$/\1/p')
if [[ -z "$DISTRO" ]]; then
    echo "error: could not parse distro from $MOFED_DIR" >&2
    exit 2
fi

# mlnx_add_kernel_support.sh repackages the bundle with kmods rebuilt
# against the supplied kernel sources and emits a new tarball; we then
# extract it and pluck out the kmod RPMs.
echo "==> Running mlnx_add_kernel_support.sh ($DISTRO) against kernel $KVER"
# Parallelism: mlnx_add_kernel_support.sh -> install.pl drives one
# rpmbuild per package (mlnx-ofa_kernel, knem, xpmem, kernel-mft,
# iser, srp, isert, mlnx-nfsrdma, fwctl) serially.  Each rpmbuild
# then runs make internally; setting MAKEFLAGS at this layer
# parallelises the *intra*-package compile (the long pole is
# mlnx-ofa_kernel's mlx5 driver build).
export MAKEFLAGS="-j$(nproc)"
echo "==> MAKEFLAGS=$MAKEFLAGS"
if ! "$MOFED_DIR/mlnx_add_kernel_support.sh" \
        --mlnx_ofed "$MOFED_DIR" \
        --ofed-sources "$OFED_SRC" \
        --distro "$DISTRO" \
        --make-tgz \
        --skip-repo \
        --kernel "$KVER" \
        --kernel-sources /kernel-build-tree \
        --tmpdir "$WORK" \
        --yes; then
    echo "==> mlnx_add_kernel_support.sh failed; preserving full logs" >&2
    # Preserve the entire log tree to the bind-mounted output dir so
    # the host can inspect everything (the tail-on-failure approach
    # truncated the actual rpmbuild error).  failure-logs/ sits next
    # to the (absent) RPMs so it's obvious where to look.
    LOG_OUT=/mofed-kmods-out/failure-logs
    rm -rf "$LOG_OUT"
    mkdir -p "$LOG_OUT"
    if compgen -G "$WORK/mlnx_iso.*_logs" > /dev/null; then
        cp -a "$WORK"/mlnx_iso.*_logs "$LOG_OUT"/
    fi
    echo "==> Full logs preserved at $LOG_OUT (visible on host as " \
         "$(dirname /mofed-kmods-out/x)/failure-logs/)" >&2
    exit 1
fi

# Find the produced ext tarball (name pattern:
# MLNX_OFED_LINUX-<ver>-<distro>-<arch>-ext.tgz).
EXT_TGZ=$(ls "$WORK"/MLNX_OFED_LINUX-*-ext.tgz 2>/dev/null | head -1)
if [[ -z "$EXT_TGZ" ]]; then
    echo "error: mlnx_add_kernel_support.sh produced no -ext.tgz" >&2
    ls -la "$WORK" >&2 || true
    exit 3
fi

echo "==> Produced $EXT_TGZ"
mkdir -p "$WORK/extracted"
tar -C "$WORK/extracted" -xzf "$EXT_TGZ"

# Copy out the kmod-* and mlnx-ofa_kernel* RPMs (the kernel-side bits).
# We deliberately skip userspace RPMs -- those are already installed by
# the image overlay's userspace pass.
SRC_RPMS=$(find "$WORK/extracted" -name 'kmod-*.rpm' \
        -o -name 'mlnx-ofa_kernel-modules-*.rpm' \
        -o -name 'kernel-mft-mlnx-*.rpm' 2>/dev/null)
if [[ -z "$SRC_RPMS" ]]; then
    echo "error: no kmod RPMs found under $WORK/extracted" >&2
    find "$WORK/extracted" -name '*.rpm' >&2 || true
    exit 4
fi

cp -v $SRC_RPMS /mofed-kmods-out/
echo "==> Copied $(echo "$SRC_RPMS" | wc -w) kmod RPMs to /mofed-kmods-out/"
ls -la /mofed-kmods-out/
