#!/bin/bash
# kernel-build-inner-suse.sh -- runs INSIDE the build container.
#
# Builds a SUSE kernel the way SUSE/kernel-source's rpm/kernel-binary.spec
# does, from SUSE's expanded source for the release (SUSE/kernel's tag:
# the vanilla tree with series.conf applied) and its config/<arch>/default.
# Lustre's own kernel patches, when the .target names a series, go on
# top.  Then the microvm config fragment and the same outputs
# kernel-build-inner.sh produces.
#
# Expected environment:
#   JOBS            -- parallel make jobs (default: nproc)
#   KERNEL_RELEASE  -- SUSE release, e.g. 150700.53.66 -- required
#   TARGET_ARCH     -- x86_64 or aarch64 (default: x86_64)
#
# Expected bind mounts:
#   /input/kernel.tar.gz             -- SUSE/kernel's archive of the tag
#   /input/staging/kernel.config     -- SUSE's config/<arch>/default
#   /input/staging/config.fragment   -- microvm config overrides
#   /input/staging/series            -- Lustre patch filenames, one per line
#   /input/staging/patches/          -- Lustre patch files
#   /output/                         -- where we install results
#
# Outputs written to /output/:
#   vmlinux       -- unstripped ELF (crash/drgn + QEMU boot)
#   vmlinuz       -- compressed bzImage (kdump)
#   build-tree/   -- full kernel build tree (Lustre module builds)
#   modules/      -- modules_install tree

set -euo pipefail

JOBS="${JOBS:-$(nproc)}"
: "${KERNEL_RELEASE:?KERNEL_RELEASE must be set by the caller (ltvm kernel_build.py)}"
TARGET_ARCH="${TARGET_ARCH:-x86_64}"
BUILD=/build/kernel-src

# shellcheck disable=SC1091
source /input/staging/cross-compile-env.sh
source /input/staging/kernel-config-overlay.sh

echo "=== kernel-build-inner-suse.sh ==="
echo "    Jobs: ${JOBS}"
echo "    Target arch: ${TARGET_ARCH}"
if [[ "$CROSSING" == "1" ]]; then
	echo "    Cross-compiling: ${HOST_ARCH} -> ${TARGET_ARCH}"
fi

# Self-heal the ccache volume (see kernel-build-inner.sh).
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
# 1. Extract source
# ------------------------------------------------------------------

echo "--- Extracting SUSE kernel source..."
mkdir -p "$BUILD"
tar xf /input/kernel.tar.gz -C "$BUILD" --strip-components=1
cd "$BUILD"

# ------------------------------------------------------------------
# 2. Apply Lustre patches
# ------------------------------------------------------------------

SERIES=/input/staging/series
if [[ -s "$SERIES" ]]; then
	echo "--- Applying Lustre patches..."
	count=0
	while IFS= read -r patch_name; do
		[[ -n "$patch_name" ]] || continue
		patch_file="/input/staging/patches/${patch_name}"
		if ! patch -p1 --forward --fuzz=2 --silent < "$patch_file"; then
			echo "ERROR: patch failed: $patch_name" >&2
			exit 1
		fi
		((count++)) || true
	done < "$SERIES"
	echo "    Applied $count Lustre patches"
else
	echo "--- No Lustre patches to apply"
fi

# ------------------------------------------------------------------
# 3. Configure kernel
# ------------------------------------------------------------------

echo "--- Configuring kernel (SUSE's config/<arch>/default)..."
cp /input/staging/kernel.config .config

# What kernel-binary.spec sets on top of the config.  The kABI check
# that SUSE_HAVE_STABLE_KABI enables is part of the rpm build, which
# this is not, so it goes off as it does there without reference data.
# The release keeps SUSE's -<release>-<flavor> shape, with the _lustre
# marker Lustre's own .target files put on a kernel they built.
./scripts/config \
	--set-str CONFIG_LOCALVERSION "-${KERNEL_RELEASE}_lustre-default" \
	--enable CONFIG_SUSE_KERNEL \
	--disable CONFIG_SUSE_KERNEL_RELEASED \
	--disable CONFIG_SUSE_HAVE_STABLE_KABI

overlay_apply

# The fragment is the common one plus the target's overrides, so a key
# can appear twice; the target's, which comes last, wins.
awk -F= '/^CONFIG_/ { if (!($1 in v)) k[++n] = $1; v[$1] = $0 }
	END { for (i = 1; i <= n; i++) print v[k[i]] }' \
	/input/staging/config.fragment > /build/config.fragment

./scripts/kconfig/merge_config.sh -m .config /build/config.fragment \
	2>&1 | tail -5

# merge_config.sh handles most cases, but olddefconfig can turn a =y
# back into =m; force the fragment's values in before and after it, as
# kernel-build-inner.sh does.
force_fragment() {
	local line key
	while IFS= read -r line; do
		key="${line%%=*}"
		if grep -q "^${key}=" .config; then
			sed -i "s|^${key}=.*|${line}|" .config
		elif grep -q "^# ${key} is not set" .config; then
			sed -i "s|^# ${key} is not set|${line}|" .config
		else
			echo "${line}" >> .config
		fi
	done < /build/config.fragment
}
force_fragment

echo "--- Running olddefconfig..."
make "${MAKE_ARCH_FLAGS[@]}" olddefconfig 2>&1 | tail -3
overlay_report

echo "--- Verifying config overrides..."
while IFS= read -r line; do
	key="${line%%=*}"
	if [[ "$line" == *=n ]]; then
		grep -q "^${key}=[ym]" .config || continue
		actual=$(grep "^${key}=" .config)
	else
		actual=$(grep "^${key}=" .config || echo "NOT SET")
		[[ "$actual" == "$line" ]] && continue
	fi
	echo "    WARNING: $key override not preserved"
	echo "      wanted: $line"
	echo "      got:    $actual"
done < /build/config.fragment
force_fragment

# ------------------------------------------------------------------
# 4. Build
# ------------------------------------------------------------------

echo "=== Building ${MAKE_TARGETS} (j${JOBS}) ==="
make "${MAKE_ARCH_FLAGS[@]}" -j"$JOBS" $MAKE_TARGETS 2>&1

echo "--- Build complete"

# Save vmlinux and vmlinuz before 'make modules', which may re-link
# vmlinux and change its build-id (see kernel-build-inner.sh).
echo "--- Installing outputs to /output/..."
cp vmlinux /output/vmlinux
cp "$KERNEL_IMAGE" /output/vmlinuz

echo "=== Building modules (j${JOBS}) ==="
make "${MAKE_ARCH_FLAGS[@]}" -j"$JOBS" modules 2>&1

echo "--- Modules complete"

echo "--- Running modules_prepare..."
make "${MAKE_ARCH_FLAGS[@]}" modules_prepare 2>&1 | tail -3

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
# The Lustre build mounts the vmlinux here, for its modules' BTF.
ln -sfn /vmlinux "$BUILD_TREE/vmlinux"
if [[ -d scripts ]]; then
	rsync -a scripts/ "$BUILD_TREE/scripts/"
fi
if [[ -d tools/objtool ]]; then
	rsync -a tools/objtool/ "$BUILD_TREE/tools/objtool/"
fi

echo "$KVER" > "$BUILD_TREE/kernel-version"

echo "--- Installing modules..."
MODULES_DIR=/output/modules
rm -rf "$MODULES_DIR"
make "${MAKE_ARCH_FLAGS[@]}" INSTALL_MOD_PATH="$MODULES_DIR" \
	INSTALL_MOD_STRIP=1 \
	CONFIG_MODULE_SIG_ALL= \
	modules_install 2>&1 | tail -5
find "$MODULES_DIR" -maxdepth 3 \( -name build -o -name source \) -type l -exec rm -f {} +
echo "    Modules: $(du -sh "$MODULES_DIR" | cut -f1)"

echo "=== Kernel build complete ==="
echo "    vmlinux: $(du -h /output/vmlinux | cut -f1)"
echo "    vmlinuz: $(du -h /output/vmlinuz | cut -f1)"
echo "    build-tree: $(du -sh "$BUILD_TREE" | cut -f1)"
echo "    modules: $(du -sh "$MODULES_DIR" | cut -f1)"
