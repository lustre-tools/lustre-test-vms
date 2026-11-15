#!/bin/sh
# Build the QEMU that shmrdma and shmnet need on macOS (Apple Silicon),
# and package it the way ltvm publishes its own QEMU.
#
# Homebrew's QEMU is built without vhost-user, so it has no
# vhost-user-fs-pci and no -netdev vhost-user.  This builds the same
# release with vhost-user enabled.  QEMU's meson turns vhost-user off by
# default outside Linux.  The patch next to this script fixes two things
# that break without eventfd: hw/net/vhost_net.c includes a Linux-only
# header, and vhost-user's SET_VRING_KICK writes a kick into the kick
# fd, which off Linux is the read end of a pipe (EBADF, and vhost start
# fails).
#
#   build-qemu.sh [OUTDIR]       default OUTDIR: .
#
# Writes OUTDIR/qemu-$VERSION-$REVISION-macos-arm64.tar.gz and its .sha256:
# bin/qemu-system-aarch64, share/qemu, and lib/ with the few Homebrew
# dylibs it loads (glib, pixman, libslirp, zstd and theirs), so it runs
# without Homebrew.  share/doc/ltvm-qemu/ lists each component's
# version, license and source, with the license files and the patch.
# The release should also carry the QEMU source tarball, the patch, and
# the source of each LGPL library (glib, gettext) with Homebrew's patch
# and formula for it; see the release notes of qemu-macos-11.1.3-1.  ltvm installs it (`ltvm install`, or
# `ltvm doctor --fix --shmrdma`) from the GitHub release
# qemu-macos-$VERSION-$REVISION; to publish a new build:
#
#   gh release create qemu-macos-$VERSION-$REVISION \
#       -R lustre-tools/lustre-test-vms <tarball> <tarball>.sha256
#
# and bump QEMU_MACOS_VERSION in ltvm_pkg/host_setup.py.  Bump REVISION
# whenever the patch or the configure flags change.
#
# Needs: Xcode command line tools, and from Homebrew ninja, pkg-config,
# glib, pixman, libslirp and zstd (at build time only).
set -eu

VERSION=v11.1.3
REVISION=1
here=$(cd "$(dirname "$0")" && pwd)
outdir=$(cd "${1:-.}" && pwd)
src=${QEMU_SRC:-$PWD/qemu-$VERSION}
name=qemu-${VERSION#v}-$REVISION-macos-arm64

if [ ! -d "$src" ]; then
	git clone --depth 1 -b "$VERSION" \
		https://gitlab.com/qemu-project/qemu.git "$src"
fi
cd "$src"
if ! git apply --reverse --check "$here/qemu-vhost-user-non-linux.patch" \
	2>/dev/null; then
	git apply "$here/qemu-vhost-user-non-linux.patch"
fi
# Only the host features ltvm's guests use: no display, audio, USB,
# TLS, curl or ssh, so the package carries four Homebrew dylibs.
mkdir -p build-pkg
cd build-pkg
../configure \
	--prefix=/opt/qemu \
	--target-list=aarch64-softmmu \
	--without-default-features \
	--enable-hvf \
	--enable-vhost-user \
	--enable-vhost-net \
	--enable-slirp \
	--enable-zstd \
	--enable-pixman \
	--enable-fdt=internal \
	--disable-docs
ninja
stage=$(mktemp -d "${TMPDIR:-/tmp}/qemu-stage.XXXXXX")
trap 'rm -rf "$stage"' EXIT
DESTDIR="$stage" ninja install >/dev/null
python3 "$here/package-qemu.py" "$stage/opt/qemu" \
	"$src/accel/hvf/entitlements.plist" "$outdir/$name.tar.gz" \
	"$src" "$here/qemu-vhost-user-non-linux.patch"
