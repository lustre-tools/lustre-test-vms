#!/bin/bash
# kernel-build-inner-deb.sh -- runs INSIDE the build container.
#
# Extracts kernel source from a deb linux-source package, applies
# the microvm config fragment, and builds vmlinux + bzImage.
# Used for Debian/Ubuntu targets where kernels come from apt
# rather than SRPMs.
#
# Expected environment:
#   JOBS              -- parallel make jobs (default: nproc)
#   KERNEL_DEB_SOURCE -- deb package name (e.g., linux-source-6.8.0)
#   KERNEL_DEB_CONFIG_DIR -- optional; a distro kernel config per
#                        arch (config-amd64, config-arm64) staged by
#                        the container, used instead of defconfig
#
# Expected bind mounts:
#   /input/staging/config.fragment   -- microvm config overrides
#   /input/staging/series            -- patch filenames (optional)
#   /input/staging/patches/          -- patch files (optional)
#   /output/                         -- where we install results
#
# Outputs written to /output/:
#   vmlinux       -- unstripped ELF (crash/drgn + QEMU boot)
#   vmlinuz       -- compressed bzImage (kdump)
#   build-tree/   -- full kernel build tree (Lustre module builds)

set -euo pipefail

JOBS="${JOBS:-$(nproc)}"
KERNEL_DEB_SOURCE="${KERNEL_DEB_SOURCE:-linux-source-6.8.0}"
TARGET_ARCH="${TARGET_ARCH:-x86_64}"
BUILD=/build/kernel-src

# Source the shared cross-compile env helper (see kernel-build-inner.sh
# for the full list of exports it provides).
# shellcheck disable=SC1091
source /input/staging/cross-compile-env.sh
source /input/staging/kernel-config-overlay.sh

echo "=== kernel-build-inner-deb.sh ==="
echo "    Jobs: ${JOBS}"
echo "    Target arch: ${TARGET_ARCH}"
echo "    Source package: ${KERNEL_DEB_SOURCE}"
if [[ "$CROSSING" == "1" ]]; then
	echo "    Cross-compiling: ${HOST_ARCH} -> ${TARGET_ARCH}"
fi

cross_ensure_toolchain

echo "    GCC: $(gcc --version | head -1)"

# ------------------------------------------------------------------
# 1. Extract kernel source from deb package
# ------------------------------------------------------------------

echo "--- Extracting kernel source..."

# The linux-source-X.Y.Z package installs a tarball under /usr/src/
SRC_TAR=$(find /usr/src -name "linux-source-*.tar.bz2" \
	-o -name "linux-source-*.tar.xz" \
	-o -name "linux-source-*.tar.gz" 2>/dev/null | head -1)

if [[ -z "$SRC_TAR" ]]; then
	echo "ERROR: No linux-source tarball found in /usr/src/" >&2
	echo "       Expected from package: ${KERNEL_DEB_SOURCE}" >&2
	ls -la /usr/src/ >&2
	exit 1
fi
echo "    Source: $(basename "$SRC_TAR")"

mkdir -p /build/extract
tar xf "$SRC_TAR" -C /build/extract

SRC_DIR=$(find /build/extract -maxdepth 1 \
	-name "linux-*" -type d | head -1)
if [[ -z "$SRC_DIR" ]]; then
	echo "ERROR: No linux-* directory after extraction" >&2
	exit 1
fi

# Move to a fixed path for predictable build-tree output
mv "$SRC_DIR" "$BUILD"
echo "    Source dir: $BUILD"

# ------------------------------------------------------------------
# 2. Apply patches (if any)
# ------------------------------------------------------------------

cd "$BUILD"

SERIES=/input/staging/series
if [[ -s "$SERIES" ]]; then
	echo "--- Applying patches..."
	count=0
	failures=0
	while IFS= read -r patch_name; do
		[[ -n "$patch_name" ]] || continue
		patch_file="/input/staging/patches/${patch_name}"
		if [[ ! -f "$patch_file" ]]; then
			echo "WARNING: patch not found: $patch_name" >&2
			((failures++)) || true
			continue
		fi
		if patch -p1 --forward --silent \
				< "$patch_file" 2>/dev/null; then
			((count++)) || true
		else
			if patch -p1 --forward --fuzz=2 --silent \
					< "$patch_file"; then
				((count++)) || true
			else
				echo "ERROR: patch failed: $patch_name" >&2
				((failures++)) || true
			fi
		fi
	done < "$SERIES"
	echo "    Applied $count patches"
	if [[ $failures -gt 0 ]]; then
		echo "ERROR: $failures patch(es) failed -- aborting build" >&2
		exit 1
	fi
else
	echo "--- No patches to apply"
fi

# ------------------------------------------------------------------
# 3. Configure kernel
# ------------------------------------------------------------------

echo "--- Configuring kernel..."

DISTRO_CONFIG="${KERNEL_DEB_CONFIG_DIR:-/nonexistent}/config-${DEB_ARCH}"

# Use the Ubuntu default config as starting point.
# Ubuntu's config is in debian.master/config/ or we can use the
# running config from the deb package.
# For a clean build, use the arch default + ubuntu annotations,
# or just use the distro config shipped with linux-source.
# Try debian.master/config/<arch>/config.flavour.generic
UBUNTU_CONFIG2=$(find "$BUILD" -path "*/debian.master/config/${DEB_ARCH}/config.flavour.generic" \
	2>/dev/null | head -1)
# Try the annotations-based approach
UBUNTU_ANNOTATIONS=$(find "$BUILD" -path "*/debian.master/config/annotations" \
	2>/dev/null | head -1)

if [[ -f "$DISTRO_CONFIG" ]]; then
	echo "    Using the container's distro config: $DISTRO_CONFIG"
	cp "$DISTRO_CONFIG" .config
elif [[ -n "$UBUNTU_ANNOTATIONS" ]] && [[ -f "$BUILD/debian.master/config/annotations" ]]; then
	echo "    Using Ubuntu annotations-based config..."
	# Ubuntu 24.04 uses an annotations file + config script to
	# generate the actual .config. Use make defconfig as base
	# then apply the fragment.
	make "${MAKE_ARCH_FLAGS[@]}" defconfig 2>&1 | tail -3
elif [[ -n "$UBUNTU_CONFIG2" ]] && [[ -f "$UBUNTU_CONFIG2" ]]; then
	echo "    Using Ubuntu flavour config: $UBUNTU_CONFIG2"
	cp "$UBUNTU_CONFIG2" .config
else
	echo "    No Ubuntu config found, using defconfig as base"
	make "${MAKE_ARCH_FLAGS[@]}" defconfig 2>&1 | tail -3
fi

# For cross-compiled aarch64: defconfig enables thousands of HW
# drivers we don't need in QEMU virt.  Disable entire subsystems
# that are irrelevant for a virtual machine to avoid broken drivers.
if [[ "$TARGET_ARCH" == "aarch64" && "$HOST_ARCH" != "aarch64" ]]; then
	echo "    Trimming config for QEMU virt (cross-compile)..."
	scripts/config --disable DRM
	scripts/config --disable SOUND
	scripts/config --disable MEDIA_SUPPORT
	scripts/config --disable WLAN
	scripts/config --disable WIRELESS
	scripts/config --disable NFC
	scripts/config --disable CAN
	scripts/config --disable BT
	scripts/config --disable USB_GADGET
	scripts/config --disable PCI_ENDPOINT
	scripts/config --disable CORESIGHT
	scripts/config --disable HWTRACING
	scripts/config --disable MTD
	scripts/config --disable SPI
	scripts/config --disable I2C
	scripts/config --disable GPIO_SYSFS
	scripts/config --disable HWMON
	scripts/config --disable REGULATOR
	scripts/config --disable MFD_CORE
	scripts/config --disable IIO
fi

overlay_apply

# Apply the microvm config fragment on top
if [[ -f scripts/kconfig/merge_config.sh ]]; then
	KCONFIG_CONFIG=.config \
		./scripts/kconfig/merge_config.sh \
		-m .config /input/staging/config.fragment \
		2>&1 | tail -5
else
	cat /input/staging/config.fragment >> .config
fi

# Force-apply config fragment values using sed
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
echo "    Applied overrides from config fragment"

# Ensure key options for Lustre client modules
for opt in CONFIG_NETWORK_FILESYSTEMS CONFIG_LNET CONFIG_LUSTRE_FS; do
	if ! grep -q "^${opt}=" .config; then
		echo "${opt}=m" >> .config
	fi
done

echo "--- Running olddefconfig..."
make "${MAKE_ARCH_FLAGS[@]}" olddefconfig 2>&1 | tail -3
overlay_report

# Verify critical overrides survived olddefconfig
echo "--- Verifying config overrides..."
while IFS= read -r line; do
	[[ "$line" =~ ^# ]] && continue
	[[ -z "$line" ]] && continue
	key="${line%%=*}"
	actual=$(grep "^${key}=" .config || echo "NOT SET")
	if [[ "$actual" != "$line" ]]; then
		echo "    WARNING: $key override not preserved"
		echo "      wanted: $line"
		echo "      got:    $actual"
		sed -i "s|^${key}=.*|${line}|" .config
	fi
done < /input/staging/config.fragment

# ------------------------------------------------------------------
# 4. Build
# ------------------------------------------------------------------

echo "=== Building ${MAKE_TARGETS} (j${JOBS}) ==="
make "${MAKE_ARCH_FLAGS[@]}" -j"$JOBS" $MAKE_TARGETS 2>&1

echo "--- Build complete"

# Save vmlinux and vmlinuz NOW, before 'make modules' which may re-link
# vmlinux (e.g. via BTF/kallsyms passes) and change its build-id.
# The vmlinuz (bzImage) is built from the vmlinux at this point; we must
# save the matching vmlinux so crash/drgn analysis works correctly.
echo "--- Installing outputs to /output/..."
cp vmlinux /output/vmlinux
cp "$KERNEL_IMAGE" /output/vmlinuz

# Also build modules to populate build tree for Lustre
echo "=== Building modules (j${JOBS}) ==="
make "${MAKE_ARCH_FLAGS[@]}" -j"$JOBS" modules 2>&1

echo "--- Modules complete"

# Prepare build tree for external module builds
echo "--- Running modules_prepare..."
make "${MAKE_ARCH_FLAGS[@]}" modules_prepare 2>&1 | tail -3

# ------------------------------------------------------------------
# 5. Install remaining outputs
# ------------------------------------------------------------------

# Kernel version
KVER=$(make "${MAKE_ARCH_FLAGS[@]}" -s kernelrelease)
echo "    Kernel version: $KVER"

# Create the build tree for Lustre module compilation
BUILD_TREE=/output/build-tree
rm -rf "$BUILD_TREE"
mkdir -p "$BUILD_TREE"

echo "--- Populating build tree (full source)..."
# --no-inc-recursive: with incremental recursion rsync can get ahead of
# itself on a large built tree (6.12) and fail a mkdir whose parent is
# not there yet.
rsync -a --no-inc-recursive \
	--exclude='*.o' \
	--exclude='*.ko' \
	--exclude='*.cmd' \
	--exclude='.tmp_*' \
	--exclude='vmlinux' \
	--exclude='vmlinuz' \
	--exclude='bzImage' \
	--exclude='*.a' \
	./ "$BUILD_TREE/"

# Copy back essential build artifacts excluded by the .o filter
cp Module.symvers "$BUILD_TREE/"
cp .config "$BUILD_TREE/"
# The Lustre build mounts the vmlinux here, for its modules' BTF.
ln -sfn /vmlinux "$BUILD_TREE/vmlinux"
if [[ -d scripts ]]; then
	rsync -a scripts/ "$BUILD_TREE/scripts/"
fi
if [[ -d tools/objtool ]]; then
	rsync -a tools/objtool/ "$BUILD_TREE/tools/objtool/"
fi

# Record kernel version in build tree
echo "$KVER" > "$BUILD_TREE/kernel-version"

# Install kernel modules to output for VM deployment
echo "--- Installing modules..."
MODULES_DIR=/output/modules
rm -rf "$MODULES_DIR"
make "${MAKE_ARCH_FLAGS[@]}" INSTALL_MOD_PATH="$MODULES_DIR" \
	INSTALL_MOD_STRIP=1 \
	CONFIG_MODULE_SIG_ALL= \
	modules_install 2>&1 | tail -5
# Remove build/source symlinks -- they point inside the container and
# break scp -r during VM deployment.
find "$MODULES_DIR" -maxdepth 3 \( -name build -o -name source \) -type l -exec rm -f {} +
echo "    Modules: $(du -sh "$MODULES_DIR" | cut -f1)"

echo "=== Kernel build complete ==="
echo "    vmlinux: $(du -h /output/vmlinux | cut -f1)"
echo "    vmlinuz: $(du -h /output/vmlinuz | cut -f1)"
echo "    build-tree: $(du -sh "$BUILD_TREE" | cut -f1)"
echo "    modules: $(du -sh "$MODULES_DIR" | cut -f1)"
