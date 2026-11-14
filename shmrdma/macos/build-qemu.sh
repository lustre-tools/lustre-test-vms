#!/bin/sh
# Build the QEMU that shmrdma and shmnet need on macOS (Apple Silicon).
#
# Homebrew's QEMU is built without vhost-user, so it has no
# vhost-user-fs-pci and no -netdev vhost-user.  This builds the same
# release with vhost-user enabled.  QEMU's meson turns vhost-user off by
# default outside Linux, and one file, hw/net/vhost_net.c, includes a
# Linux-only header; the patch next to this script fixes that.
#
#   build-qemu.sh [PREFIX]      default PREFIX: ./qemu-shmrdma-install
#
# Needs: Xcode command line tools, and from Homebrew ninja, pkg-config,
# glib, pixman and libslirp.  ltvm uses the result through
# LTVM_QEMU_PREFIX=PREFIX (or by installing it as /opt/qemu).
set -eu

VERSION=v11.1.3
here=$(cd "$(dirname "$0")" && pwd)
prefix=${1:-$PWD/qemu-shmrdma-install}
src=${QEMU_SRC:-$PWD/qemu-$VERSION}

if [ ! -d "$src" ]; then
	git clone --depth 1 -b "$VERSION" \
		https://gitlab.com/qemu-project/qemu.git "$src"
fi
cd "$src"
if ! git apply --reverse --check "$here/qemu-vhost-net-non-linux.patch" \
	2>/dev/null; then
	git apply "$here/qemu-vhost-net-non-linux.patch"
fi
mkdir -p build
cd build
../configure \
	--prefix="$prefix" \
	--target-list=aarch64-softmmu \
	--enable-hvf \
	--enable-vhost-user \
	--enable-slirp \
	--disable-docs
ninja
ninja install
"$prefix/bin/qemu-system-aarch64" -M none -device help |
	grep -q '"vhost-user-fs-pci"'
echo "QEMU with vhost-user installed in $prefix"
