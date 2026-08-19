#!/bin/bash
# kernel-build-inner.sh -- runs INSIDE the build container.
#
# Extracts a kernel SRPM, applies Lustre patches, merges the
# microvm config fragment, and builds vmlinux + bzImage.
#
# Expected environment:
#   JOBS         -- parallel make jobs (default: nproc)
#   LNXMAJ       -- kernel major version (e.g. 5.14.0) -- required
#   LNXREL       -- kernel release suffix (e.g. 503.el9_5) -- required
#   TARGET_ARCH  -- x86_64 or aarch64 (default: x86_64)
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
: "${LNXMAJ:?LNXMAJ must be set by the caller (ltvm kernel_build.py)}"
: "${LNXREL:?LNXREL must be set by the caller (ltvm kernel_build.py)}"
TARGET_ARCH="${TARGET_ARCH:-x86_64}"
BUILD=/build/kernel-src

# Source the shared cross-compile env helper.  It reads TARGET_ARCH
# and exports HOST_ARCH, CROSSING, KBUILD_ARCH, KERNEL_IMAGE,
# MAKE_TARGETS, CROSS_TRIPLE, CROSS_COMPILE, MAKE_ARCH_FLAGS,
# CONFIGURE_HOST, DEB_ARCH, IOZONE_TARGET.  The SRPM ships per-arch
# config files named by target_arch (aarch64, x86_64), which matches
# TARGET_ARCH verbatim -- no separate SRPM_CONFIG_ARCH mapping needed.
# shellcheck disable=SC1091
source /input/staging/cross-compile-env.sh
SRPM_CONFIG_ARCH="$TARGET_ARCH"

echo "=== kernel-build-inner.sh ==="
echo "    Jobs: ${JOBS}"
echo "    Target arch: ${TARGET_ARCH}"
if [[ "$CROSSING" == "1" ]]; then
	echo "    Cross-compiling: ${HOST_ARCH} -> ${TARGET_ARCH}"
fi

# Self-heal the ccache volume.  Rootless podman maps "root" inside
# the container to the host user's uid; if a previous build wrote
# to /ccache under a different uid namespace (e.g. because the
# storage backend was migrated, or a pre-rootless-mode build ran
# with a different mapping), the new container's root may see
# /ccache itself but not its entries -- mkdir /ccache/tmp then
# fails with "Permission denied" and every gcc invocation through
# the ccache wrapper aborts the build.  Probe by trying to write a
# canary; if it fails, blow away the whole tree and start over.
if [[ -d /ccache ]]; then
	if ! { mkdir -p /ccache/tmp && touch /ccache/.ltvm-canary; } 2>/dev/null; then
		echo "    Resetting unreachable ccache (uid-namespace drift)"
		find /ccache -mindepth 1 -delete 2>/dev/null || true
		mkdir -p /ccache/tmp
	fi
	rm -f /ccache/.ltvm-canary
fi

cross_ensure_toolchain

echo "    GCC: $(gcc --version | head -1)"

# ------------------------------------------------------------------
# 1. Extract SRPM
# ------------------------------------------------------------------

echo "--- Extracting SRPM..."
SRPM_DIR=/build/srpm
mkdir -p "$SRPM_DIR"
cd "$SRPM_DIR"
rpm2cpio /input/kernel.src.rpm | cpio -idm 2>/dev/null

# Find the kernel source tarball
SRC_TAR=$(find "$SRPM_DIR" \( -name "linux-*.tar.xz" \
	-o -name "linux-*.tar.gz" \) | head -1)
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
			# Try with --fuzz=2 for minor context issues
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
# 4. Configure kernel
# ------------------------------------------------------------------

echo "--- Configuring kernel..."
if [[ -s /input/staging/kernel.config ]]; then
	cp /input/staging/kernel.config .config
else
	# No Lustre-provided config -- extract from SRPM.
	# RHEL/Rocky SRPMs ship kernel-<arch>-rhel.config files.
	echo "    Extracting kernel config from SRPM (arch=$SRPM_CONFIG_ARCH)..."
	SRPM_CONFIG=$(find "$SRPM_DIR" -name "kernel-${SRPM_CONFIG_ARCH}-rhel.config" | head -1)
	if [[ -z "$SRPM_CONFIG" ]]; then
		# Fallback: any config for this arch, non-debug
		SRPM_CONFIG=$(find "$SRPM_DIR" -name "kernel-${SRPM_CONFIG_ARCH}*.config" \
			| grep -v debug | head -1)
	fi
	if [[ -z "$SRPM_CONFIG" ]]; then
		echo "ERROR: No ${SRPM_CONFIG_ARCH} kernel config found in SRPM" >&2
		echo "    Available configs:"
		find "$SRPM_DIR" -name "*.config" | head -20
		exit 1
	fi
	echo "    Using SRPM config: $(basename "$SRPM_CONFIG")"
	cp "$SRPM_CONFIG" .config
fi

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
make "${MAKE_ARCH_FLAGS[@]}" olddefconfig 2>&1 | tail -3

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

# ------------------------------------------------------------------
# 4b. SBAT data (RHEL 9.8+)
# ------------------------------------------------------------------
# RHEL 9.8 kernels embed an SBAT (Secure Boot Advanced Targeting)
# section in the x86 boot stub.  With CONFIG_EFI_SBAT=y plus
# CONFIG_EFI_SBAT_FILE="kernel.sbat", arch/x86/boot/compressed/sbat.o
# takes that file (path relative to the top of the tree) as a
# prerequisite and .incbin's it.  kernel.spec generates it during
# %prep from Source83 (kernel.sbat.template); we build straight from
# the source tarball, so nothing writes it and the build dies with
#   No rule to make target 'kernel.sbat'
# Only the x86_64 config turns CONFIG_EFI_SBAT on, which is why
# aarch64 builds of the same kernel are unaffected.
if grep -q '^CONFIG_EFI_SBAT=y' .config; then
	SBAT_FILE=$(sed -n 's/^CONFIG_EFI_SBAT_FILE="\(.*\)"$/\1/p' .config)
	if [[ -n "$SBAT_FILE" && ! -f "$SBAT_FILE" ]]; then
		echo "--- Generating ${SBAT_FILE} (SBAT data)..."
		mkdir -p "$(dirname "$SBAT_FILE")"
		SBAT_KVER="${LNXMAJ}-${LNXREL}.${TARGET_ARCH}"
		# sbat_suffix is "rhel" for both RHEL and CentOS/Rocky.
		SBAT_TEMPLATE=$(find "$SRPM_DIR" -name kernel.sbat.template | head -1)
		if [[ -n "$SBAT_TEMPLATE" ]]; then
			sed -e "s,@KVER,${SBAT_KVER}," \
				-e "s,@SBAT_SUFFIX,rhel," \
				"$SBAT_TEMPLATE" > "$SBAT_FILE"
		else
			printf '%s\n' \
				'sbat,1,SBAT Version,sbat,1,https://github.com/rhboot/shim/blob/main/SBAT.md' \
				"kernel.rhel,1,Red Hat,kernel-core,${SBAT_KVER},mailto:secalert@redhat.com" \
				> "$SBAT_FILE"
		fi
	fi
fi

# Set EXTRAVERSION from LNXREL so kernel version matches the SRPM
# e.g., 5.14.0 becomes 5.14.0-611.13.1.el9_7_lustre
if [[ -n "$LNXREL" ]]; then
	EXTRAVER="-${LNXREL}_lustre"
	sed -i "s/^EXTRAVERSION.*/EXTRAVERSION = ${EXTRAVER}/" Makefile
	echo "    EXTRAVERSION set to: ${EXTRAVER}"
fi

# The RHEL kernel source ships two top-level files that case-fold to
# the same name: `Makefile` (the real kernel Makefile, 2000+ lines,
# what kbuild and Lustre's `include Makefile` probe expect) and
# `makefile` (a 16-line RH dist wrapper whose `include Makefile`
# self-references and triggers infinite make recursion if it ever
# stands in for the real one).
#
# They coexist fine on Linux (case-sensitive) -- the build uses
# `Makefile` and `makefile` is inert.  But when the build tree gets
# rsynced to /output (bind-mounted from a macOS host with a
# case-insensitive filesystem) the two paths collapse and the
# smaller `makefile` silently overwrites the real `Makefile`.  The
# kernel build has already finished at that point so it succeeds,
# but Lustre's configure then dies in a Makefile:2 loop with
# "Too many open files.  Stop." when it `include Makefile`s.
#
# Removing the dist wrapper before the rsync avoids the collision.
# It is only used by RH's `make dist-*` targets which we never run.
rm -f makefile

# ------------------------------------------------------------------
# 5. Build
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

# Kernel version
KVER=$(make "${MAKE_ARCH_FLAGS[@]}" -s kernelrelease)
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
