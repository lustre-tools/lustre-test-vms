#!/bin/bash
# kernel-build-inner.sh -- runs INSIDE the build container.
#
# Extracts a kernel SRPM, applies Lustre patches, merges the
# microvm config fragment, and builds vmlinux + bzImage.
#
# Expected environment:
#   JOBS     -- parallel make jobs (default: nproc)
#   LNXMAJ  -- kernel major version (e.g., 5.14.0)
#
# Expected bind mounts:
#   /input/kernel.src.rpm    -- the kernel SRPM
#   /input/staging/kernel.config     -- base kernel .config
#   /input/staging/config.fragment   -- microvm config overrides
#   /input/staging/series            -- patch filenames, one per line
#   /input/staging/patches/          -- patch files
#   /output/                         -- where we install results
#
# Outputs written to /output/:
#   vmlinux       -- unstripped ELF (crash/drgn + QEMU boot)
#   vmlinuz       -- compressed bzImage (kdump)
#   build-tree/   -- full kernel build tree (Lustre module builds)

set -euo pipefail

JOBS="${JOBS:-$(nproc)}"
LNXMAJ="${LNXMAJ:-}"
LNXREL="${LNXREL:-}"
BUILD=/build/kernel-src

echo "=== kernel-build-inner.sh ==="
echo "    GCC: $(gcc --version | head -1)"
echo "    Jobs: ${JOBS}"

# ------------------------------------------------------------------
# 1. Extract SRPM
# ------------------------------------------------------------------

echo "--- Extracting SRPM..."
SRPM_DIR=/build/srpm
mkdir -p "$SRPM_DIR"
cd "$SRPM_DIR"
rpm2cpio /input/kernel.src.rpm | cpio -idm 2>/dev/null

# Find the kernel source tarball
SRC_TAR=$(find "$SRPM_DIR" -name "linux-*.tar.xz" \
	-o -name "linux-*.tar.gz" | head -1)
if [[ -z "$SRC_TAR" ]]; then
	echo "ERROR: No linux source tarball in SRPM" >&2
	exit 1
fi
echo "    Source: $(basename "$SRC_TAR")"

# ------------------------------------------------------------------
# 2. Extract source
# ------------------------------------------------------------------

echo "--- Extracting kernel source..."
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
# 3. Apply Lustre patches
# ------------------------------------------------------------------

cd "$BUILD"

SERIES=/input/staging/series
if [[ -s "$SERIES" ]]; then
	echo "--- Applying Lustre patches..."
	count=0
	while IFS= read -r patch_name; do
		[[ -n "$patch_name" ]] || continue
		patch_file="/input/staging/patches/${patch_name}"
		if [[ ! -f "$patch_file" ]]; then
			echo "WARNING: patch not found: $patch_name" >&2
			continue
		fi
		if ! patch -p1 --forward --silent \
				< "$patch_file" 2>/dev/null; then
			# Try with --fuzz=2 for minor context issues
			if ! patch -p1 --forward --fuzz=2 --silent \
					< "$patch_file" 2>/dev/null; then
				echo "WARNING: patch failed: $patch_name" >&2
			fi
		fi
		((count++)) || true
	done < "$SERIES"
	echo "    Applied $count patches"
else
	echo "--- No patches to apply"
fi

# ------------------------------------------------------------------
# 4. Configure kernel
# ------------------------------------------------------------------

echo "--- Configuring kernel..."
cp /input/staging/kernel.config .config

# Apply the microvm config fragment on top.
# Use scripts/kconfig/merge_config.sh if available,
# otherwise append and run olddefconfig.
if [[ -f scripts/kconfig/merge_config.sh ]]; then
	KCONFIG_CONFIG=.config \
		./scripts/kconfig/merge_config.sh \
		-m .config /input/staging/config.fragment \
		2>&1 | tail -5
else
	cat /input/staging/config.fragment >> .config
fi

# Force-apply config fragment values using sed.
# merge_config.sh handles most cases but olddefconfig
# can revert =y back to =m due to dependency resolution.
# Applying via sed after merge ensures they stick.
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

echo "--- Running olddefconfig..."
make olddefconfig 2>&1 | tail -3

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
		# Force it again
		sed -i "s|^${key}=.*|${line}|" .config
	fi
done < /input/staging/config.fragment

# Set EXTRAVERSION from LNXREL so kernel version matches the SRPM
# e.g., 5.14.0 becomes 5.14.0-611.13.1.el9_7_lustre
if [[ -n "$LNXREL" ]]; then
	EXTRAVER="-${LNXREL}_lustre"
	sed -i "s/^EXTRAVERSION.*/EXTRAVERSION = ${EXTRAVER}/" Makefile
	echo "    EXTRAVERSION set to: ${EXTRAVER}"
fi

# ------------------------------------------------------------------
# 5. Build
# ------------------------------------------------------------------

echo "=== Building vmlinux + bzImage (j${JOBS}) ==="
make -j"$JOBS" vmlinux bzImage 2>&1

echo "--- Build complete"

# Also build modules to populate build tree for Lustre
echo "=== Building modules (j${JOBS}) ==="
make -j"$JOBS" modules 2>&1

echo "--- Modules complete"

# Prepare build tree for external module builds
echo "--- Running modules_prepare..."
make modules_prepare 2>&1 | tail -3

# ------------------------------------------------------------------
# 6. Install outputs
# ------------------------------------------------------------------

echo "--- Installing outputs to /output/..."

cp vmlinux /output/vmlinux
cp arch/x86/boot/bzImage /output/vmlinuz

# Kernel version
KVER=$(make -s kernelrelease)
echo "    Kernel version: $KVER"

# Create the build tree for Lustre module compilation.
# Lustre needs the full source tree (not just headers)
# because ldiskfs builds from the ext4 source.
# Use rsync to copy everything except .o/.ko files
# (which are huge and not needed for external builds).

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

# Copy back the essential build artifacts that were
# excluded by the .o filter
cp Module.symvers "$BUILD_TREE/"
cp .config "$BUILD_TREE/"
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
make INSTALL_MOD_PATH="$MODULES_DIR" \
	INSTALL_MOD_STRIP=1 \
	CONFIG_MODULE_SIG_ALL= \
	modules_install 2>&1 | tail -5
echo "    Modules: $(du -sh "$MODULES_DIR" | cut -f1)"

echo "=== Kernel build complete ==="
echo "    vmlinux: $(du -h /output/vmlinux | cut -f1)"
echo "    vmlinuz: $(du -h /output/vmlinuz | cut -f1)"
echo "    build-tree: $(du -sh "$BUILD_TREE" | cut -f1)"
echo "    modules: $(du -sh "$MODULES_DIR" | cut -f1)"
