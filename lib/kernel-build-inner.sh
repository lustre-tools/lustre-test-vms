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
# We need: source + configured tree + Module.symvers.
# Rather than copying everything (multi-GB), we keep
# the build in place and bind-mount at build time.
# For now, copy the essentials.

BUILD_TREE=/output/build-tree
rm -rf "$BUILD_TREE"
mkdir -p "$BUILD_TREE"

# Copy the files needed for external module builds
echo "--- Populating build tree..."

# Core build infrastructure
cp .config "$BUILD_TREE/"
cp Module.symvers "$BUILD_TREE/"
cp Makefile "$BUILD_TREE/"

# Kbuild / Kconfig infrastructure
find . -name 'Makefile*' -o -name 'Kconfig*' \
	-o -name 'Kbuild*' | \
	while IFS= read -r f; do
		d="$BUILD_TREE/$(dirname "$f")"
		mkdir -p "$d"
		cp "$f" "$d/"
	done

# Generated headers (include/generated, include/config)
if [[ -d include/generated ]]; then
	mkdir -p "$BUILD_TREE/include"
	cp -a include/generated "$BUILD_TREE/include/"
fi
if [[ -d include/config ]]; then
	mkdir -p "$BUILD_TREE/include"
	cp -a include/config "$BUILD_TREE/include/"
fi

# Architecture headers and boot files
if [[ -d arch/x86/include/generated ]]; then
	mkdir -p "$BUILD_TREE/arch/x86/include"
	cp -a arch/x86/include/generated \
		"$BUILD_TREE/arch/x86/include/"
fi

# scripts/ directory (fixdep, modpost, etc.)
if [[ -d scripts ]]; then
	cp -a scripts "$BUILD_TREE/"
fi

# tools/objtool (needed by some module builds)
if [[ -d tools/objtool ]]; then
	mkdir -p "$BUILD_TREE/tools"
	cp -a tools/objtool "$BUILD_TREE/tools/"
fi

# All headers (include/ and arch/x86/include/)
# Use rsync to merge with already-copied generated dirs
rsync -a --ignore-existing include/ \
	"$BUILD_TREE/include/"
if [[ -d arch/x86/include ]]; then
	mkdir -p "$BUILD_TREE/arch/x86"
	rsync -a --ignore-existing arch/x86/include/ \
		"$BUILD_TREE/arch/x86/include/"
fi

# Arch Makefiles and configs
for f in arch/x86/Makefile arch/x86/Makefile.postlink \
		arch/x86/Kconfig; do
	if [[ -f "$f" ]]; then
		mkdir -p "$BUILD_TREE/$(dirname "$f")"
		cp "$f" "$BUILD_TREE/$f"
	fi
done

# Record kernel version in build tree
echo "$KVER" > "$BUILD_TREE/kernel-version"

echo "=== Kernel build complete ==="
echo "    vmlinux: $(du -h /output/vmlinux | cut -f1)"
echo "    vmlinuz: $(du -h /output/vmlinuz | cut -f1)"
echo "    build-tree: $(du -sh "$BUILD_TREE" | cut -f1)"
