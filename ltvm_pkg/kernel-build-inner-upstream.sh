#!/bin/bash
# kernel-build-inner-upstream.sh -- runs INSIDE the build container.
#
# Builds a vanilla kernel.org kernel from a source tarball.  Unlike the
# SRPM and deb paths there is no distro .config to start from and no
# Lustre kernel patch series to apply: a mainline kernel is taken as it
# ships, so that a Lustre build failure against it is attributable to
# Lustre or to the kernel, and never to a patch ltvm applied.
#
# Expected environment:
#   JOBS          -- parallel make jobs (default: nproc)
#   KERNEL_TARBALL-- basename of the tarball under /input/cache
#   KERNEL_VERSION-- upstream version, e.g. 7.2.3 or 7.3-rc1
#   TARGET_ARCH   -- x86_64 or aarch64
#
# Expected bind mounts:
#   /input/cache/<tarball>          -- kernel source tarball (ro)
#   /input/staging/config.fragment  -- microvm config overrides
#   /output/                        -- where results are installed
#
# Outputs written to /output/:
#   vmlinux       -- unstripped ELF (crash/drgn + QEMU boot)
#   vmlinuz       -- compressed image (kdump)
#   build-tree/   -- full kernel build tree (Lustre module builds)
#   modules/      -- installed modules for VM deployment

set -euo pipefail

JOBS="${JOBS:-$(nproc)}"
KERNEL_TARBALL="${KERNEL_TARBALL:?KERNEL_TARBALL is required}"
KERNEL_VERSION="${KERNEL_VERSION:-unknown}"
TARGET_ARCH="${TARGET_ARCH:-x86_64}"
BUILD=/build/kernel-src

# shellcheck disable=SC1091
source /input/staging/cross-compile-env.sh

echo "=== kernel-build-inner-upstream.sh ==="
echo "    Jobs: ${JOBS}"
echo "    Target arch: ${TARGET_ARCH}"
echo "    Upstream version: ${KERNEL_VERSION}"
echo "    Tarball: ${KERNEL_TARBALL}"
if [[ "$CROSSING" == "1" ]]; then
	echo "    Cross-compiling: ${HOST_ARCH} -> ${TARGET_ARCH}"
fi

cross_ensure_toolchain

echo "    GCC: $(gcc --version | head -1)"

# ------------------------------------------------------------------
# 1. Extract
# ------------------------------------------------------------------

SRC_TAR="/input/cache/${KERNEL_TARBALL}"
[[ -f "$SRC_TAR" ]] || {
	echo "ERROR: tarball not found: $SRC_TAR" >&2
	exit 1
}

echo "--- Extracting $(basename "$SRC_TAR")..."
mkdir -p /build/extract
# -rc snapshots are .tar.gz, CDN releases are .tar.xz; tar sniffs both.
tar xf "$SRC_TAR" -C /build/extract

SRC_DIR=$(find /build/extract -maxdepth 1 -name "linux-*" -type d | head -1)
[[ -n "$SRC_DIR" ]] || {
	echo "ERROR: no linux-* directory after extraction" >&2
	ls -la /build/extract >&2
	exit 1
}
mv "$SRC_DIR" "$BUILD"
cd "$BUILD"
echo "    Source dir: $BUILD"

# ------------------------------------------------------------------
# 2. Configure
# ------------------------------------------------------------------

echo "--- Configuring (defconfig + ltvm fragment)..."
make "${MAKE_ARCH_FLAGS[@]}" defconfig 2>&1 | tail -3

# Cross-built aarch64: defconfig turns on thousands of drivers that a
# QEMU virt guest has no use for and that are the usual source of
# cross-build breakage.  Same trim as the deb path.
if [[ "$TARGET_ARCH" == "aarch64" && "$HOST_ARCH" != "aarch64" ]]; then
	echo "    Trimming config for QEMU virt (cross-compile)..."
	for opt in DRM SOUND MEDIA_SUPPORT WLAN WIRELESS NFC CAN BT \
			INFINIBAND USB_GADGET PCI_ENDPOINT CORESIGHT \
			HWTRACING MTD SPI I2C GPIO_SYSFS HWMON \
			REGULATOR MFD_CORE IIO; do
		scripts/config --disable "$opt"
	done
fi

# Rust is off in defconfig and the build containers ship no rustc or
# bindgen; say so rather than let a fragment silently turn it on.
scripts/config --disable RUST || true

if [[ -f scripts/kconfig/merge_config.sh ]]; then
	KCONFIG_CONFIG=.config \
		./scripts/kconfig/merge_config.sh \
		-m .config /input/staging/config.fragment 2>&1 | tail -5
else
	cat /input/staging/config.fragment >> .config
fi

# merge_config is advisory: olddefconfig can still drop a symbol whose
# dependencies are unmet.  Force each fragment line in, then verify
# after olddefconfig and report what did not survive.
echo "--- Force-applying config fragment overrides..."
while IFS= read -r line; do
	[[ "$line" =~ ^# ]] && continue
	[[ -z "$line" ]] && continue
	key="${line%%=*}"
	if grep -q "^${key}=" .config; then
		sed -i "s|^${key}=.*|${line}|" .config
	elif grep -q "^# ${key} is not set" .config; then
		sed -i "s|^# ${key} is not set|${line}|" .config
	else
		echo "${line}" >> .config
	fi
done < /input/staging/config.fragment

echo "--- Running olddefconfig..."
make "${MAKE_ARCH_FLAGS[@]}" olddefconfig 2>&1 | tail -3

echo "--- Verifying config overrides..."
unmet=0
while IFS= read -r line; do
	[[ "$line" =~ ^# ]] && continue
	[[ -z "$line" ]] && continue
	key="${line%%=*}"
	actual=$(grep "^${key}=" .config || echo "NOT SET")
	if [[ "$actual" != "$line" ]]; then
		echo "    WARNING: $key not preserved (wanted: $line, got: $actual)"
		((unmet++)) || true
	fi
done < /input/staging/config.fragment
# A mainline kernel can rename or retire a symbol the fragment still
# names.  That is information about the kernel, not a reason to stop,
# so report the count and keep going -- except for the two that decide
# whether the VM can boot at all.
echo "    $unmet fragment override(s) not satisfied"
for must in CONFIG_PVH CONFIG_BLK_DEV_INITRD; do
	if ! grep -q "^${must}=y" .config; then
		echo "ERROR: ${must}=y is required to boot this kernel under" >&2
		echo "       QEMU microvm but is not set in the final .config." >&2
		echo "       The kernel likely renamed or retired it; update" >&2
		echo "       targets/common/kernel-config.fragment." >&2
		exit 1
	fi
done

# ------------------------------------------------------------------
# 3. Build
# ------------------------------------------------------------------

echo "=== Building ${MAKE_TARGETS} (j${JOBS}) ==="
make "${MAKE_ARCH_FLAGS[@]}" -j"$JOBS" $MAKE_TARGETS 2>&1

# Save vmlinux/vmlinuz before 'make modules', which can re-link vmlinux
# and change its build-id out from under the vmlinuz just produced --
# crash/drgn then pair a vmcore with a vmlinux that never booted.
echo "--- Installing outputs to /output/..."
cp vmlinux /output/vmlinux
cp "$KERNEL_IMAGE" /output/vmlinuz

echo "=== Building modules (j${JOBS}) ==="
make "${MAKE_ARCH_FLAGS[@]}" -j"$JOBS" modules 2>&1

echo "--- Running modules_prepare..."
make "${MAKE_ARCH_FLAGS[@]}" modules_prepare 2>&1 | tail -3

# ------------------------------------------------------------------
# 4. Install
# ------------------------------------------------------------------

KVER=$(make "${MAKE_ARCH_FLAGS[@]}" -s kernelrelease)
echo "    Kernel version: $KVER"

BUILD_TREE=/output/build-tree
rm -rf "$BUILD_TREE"
mkdir -p "$BUILD_TREE"

echo "--- Populating build tree (full source)..."
rsync -a \
	--exclude='*.o' \
	--exclude='*.ko' \
	--exclude='*.cmd' \
	--exclude='.tmp_*' \
	--exclude='vmlinux' \
	--exclude='vmlinuz' \
	--exclude='bzImage' \
	--exclude='*.a' \
	./ "$BUILD_TREE/"

cp Module.symvers "$BUILD_TREE/"
cp .config "$BUILD_TREE/"
[[ -d scripts ]] && rsync -a scripts/ "$BUILD_TREE/scripts/"
[[ -d tools/objtool ]] && rsync -a tools/objtool/ "$BUILD_TREE/tools/objtool/"
echo "$KVER" > "$BUILD_TREE/kernel-version"

echo "--- Installing modules..."
MODULES_DIR=/output/modules
rm -rf "$MODULES_DIR"
make "${MAKE_ARCH_FLAGS[@]}" INSTALL_MOD_PATH="$MODULES_DIR" \
	INSTALL_MOD_STRIP=1 \
	CONFIG_MODULE_SIG_ALL= \
	modules_install 2>&1 | tail -5
# build/source symlinks point inside the container; they break scp -r
# when the modules tree is copied to a VM.
find "$MODULES_DIR" -maxdepth 3 \( -name build -o -name source \) \
	-type l -exec rm -f {} +
echo "    Modules: $(du -sh "$MODULES_DIR" | cut -f1)"

echo "=== Kernel build complete ==="
echo "    version:    $KVER (upstream $KERNEL_VERSION)"
echo "    vmlinux:    $(du -h /output/vmlinux | cut -f1)"
echo "    vmlinuz:    $(du -h /output/vmlinuz | cut -f1)"
echo "    build-tree: $(du -sh "$BUILD_TREE" | cut -f1)"
