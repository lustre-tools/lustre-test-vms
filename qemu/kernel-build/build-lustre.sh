#!/bin/bash
# Build Lustre inside a container against a kernel build tree.
#
# Produces kernel modules (.ko) and userspace binaries that can be
# deployed to VMs via deploy-lustre.sh.
#
# Usage:
#   build-lustre.sh --lustre ~/lustre-release --kernel-tree DIR [--el 8|9]
#   build-lustre.sh --lustre ~/lustre-release --download-kernel [--el 8|9]
#
# The --kernel-tree is the full kernel source tree from build-kernel.sh
# (or any tree with .config, Module.symvers, and built vmlinux).
#
# With --download-kernel, automatically runs build-kernel.sh first.
#
# After building, deploy with:
#   sudo deploy-lustre.sh --vm myvm --build ~/lustre-release --mount

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LUSTRE_DIR=""
KERNEL_TREE=""
EL_VER=""
JOBS=$(nproc)
CONTAINER_RT=""
CONFIGURE_ARGS=""
DO_DOWNLOAD_KERNEL=false

usage() {
	cat <<-EOF
	Usage: ${0##*/} [options]

	  --lustre DIR        Lustre source tree (required)
	  --kernel-tree DIR   Kernel build tree (from build-kernel.sh)
	  --download-kernel   Build a kernel first (calls build-kernel.sh)
	  --el 8|9            EL version (default: 9)
	  --configure ARGS    Extra configure arguments (e.g., --disable-server)
	  --jobs N            Parallel make jobs (default: $JOBS)
	  -h, --help          This help

	Either --kernel-tree or --download-kernel is required.

	Examples:
	  # Build against existing kernel tree
	  ${0##*/} --lustre ~/lustre-release \\
	      --kernel-tree /tmp/kernel-build-el9-*/linux-*

	  # Download kernel, build it, then build Lustre
	  ${0##*/} --lustre ~/lustre-release --download-kernel --el 9

	  # Client-only build
	  ${0##*/} --lustre ~/lustre-release \\
	      --kernel-tree /tmp/kernel-build-el9-*/linux-* \\
	      --configure "--disable-server"
	EOF
	exit 1
}

die() { echo "Error: $*" >&2; exit 1; }

check_requirements() {
	local missing=()

	if ! command -v podman &>/dev/null && \
	   ! command -v docker &>/dev/null; then
		missing+=("podman or docker")
	fi

	if [[ ${#missing[@]} -gt 0 ]]; then
		echo "Missing: ${missing[*]}" >&2
		echo "  EL/Fedora: dnf install podman" >&2
		echo "  Debian:    apt install podman" >&2
		exit 1
	fi
}

detect_container_rt() {
	if command -v podman &>/dev/null; then
		CONTAINER_RT=podman
	elif command -v docker &>/dev/null; then
		CONTAINER_RT=docker
	fi
}

while [[ $# -gt 0 ]]; do
	case $1 in
		--lustre)          LUSTRE_DIR="$2"; shift 2 ;;
		--kernel-tree)     KERNEL_TREE="$2"; shift 2 ;;
		--download-kernel) DO_DOWNLOAD_KERNEL=true; shift ;;
		--el)              EL_VER="$2"; shift 2 ;;
		--configure)       CONFIGURE_ARGS="$2"; shift 2 ;;
		--jobs)            JOBS="$2"; shift 2 ;;
		-h|--help)         usage ;;
		*)                 die "Unknown option: $1" ;;
	esac
done

check_requirements
detect_container_rt

# --- Validate ---

[[ -n "$LUSTRE_DIR" ]] || die "--lustre is required"
LUSTRE_DIR="$(cd "$LUSTRE_DIR" && pwd)"
[[ -f "$LUSTRE_DIR/LUSTRE-VERSION-GEN" ]] || \
	die "$LUSTRE_DIR does not look like a Lustre source tree"

[[ -z "$EL_VER" ]] && EL_VER=9
[[ "$EL_VER" == "8" || "$EL_VER" == "9" ]] || \
	die "--el must be 8 or 9"

# --- Kernel tree ---

if $DO_DOWNLOAD_KERNEL; then
	echo "=== Building kernel first ==="
	# build-kernel.sh cleans up its temp dir, but we need
	# the tree to persist.  Build into a known location.
	KERNEL_BUILD_DIR="/tmp/lustre-kernel-el${EL_VER}"
	mkdir -p "$KERNEL_BUILD_DIR"

	bash "$SCRIPT_DIR/build-kernel.sh" \
		--download --el "$EL_VER" --keep \
		--install-dir "$KERNEL_BUILD_DIR"

	# Find the kernel source tree (--keep preserves it)
	KERNEL_TREE=$(find /tmp -maxdepth 2 \
		-name "linux-*el${EL_VER}*" -type d \
		-newer "$KERNEL_BUILD_DIR" 2>/dev/null | head -1)

	[[ -d "$KERNEL_TREE" ]] || \
		die "Could not find kernel build tree after build"
	echo "    Kernel tree: $KERNEL_TREE"
fi

[[ -n "$KERNEL_TREE" ]] || \
	die "Either --kernel-tree or --download-kernel is required"
KERNEL_TREE="$(cd "$KERNEL_TREE" && pwd)"
[[ -f "$KERNEL_TREE/.config" ]] || \
	die "No .config in kernel tree: $KERNEL_TREE"
[[ -f "$KERNEL_TREE/Module.symvers" ]] || \
	die "No Module.symvers in kernel tree: $KERNEL_TREE"

# --- Container image ---

CONTAINERFILE="${SCRIPT_DIR}/Containerfile.el${EL_VER}"
[[ -f "$CONTAINERFILE" ]] || \
	die "Containerfile not found: $CONTAINERFILE"
IMAGE_TAG="el${EL_VER}-kernel-builder"

echo "=== Building container image: $IMAGE_TAG ==="
$CONTAINER_RT build -t "$IMAGE_TAG" \
	-f "$CONTAINERFILE" "$SCRIPT_DIR"

# --- Build Lustre ---

echo "=== Building Lustre (EL${EL_VER} container, j${JOBS}) ==="
echo "    Source:  $LUSTRE_DIR"
echo "    Kernel:  $KERNEL_TREE"
echo "    Args:    ${CONFIGURE_ARGS:-(defaults)}"

# Rootless podman maps container root to the invoking user.
# Files owned by host root are not writable inside the
# container.  Fix by changing ownership of the source tree.
echo "--- Fixing source tree ownership..."
sudo chown -R "$(id -u):$(id -g)" "$LUSTRE_DIR"

$CONTAINER_RT run --rm \
	--security-opt label=disable \
	-v "$LUSTRE_DIR:/lustre" \
	-v "$KERNEL_TREE:/kernel:ro" \
	"$IMAGE_TAG" -c "
set -e
cd /lustre

echo '--- GCC version:'
gcc --version | head -1

# Track which kernel we built against.  If the kernel changed,
# we must do a full reconfigure + clean rebuild -- stale .o files
# compiled against old kernel headers cause symbol mismatches.
KERNEL_RELEASE=\$(make -s -C /kernel kernelrelease 2>/dev/null || echo unknown)
STAMP_FILE=/lustre/.build-lustre-kernel
NEED_CLEAN=false

if [[ -f Makefile ]] && grep -q '/home\|/usr/src' Makefile 2>/dev/null; then
    echo '--- Cleaning stale host build artifacts...'
    NEED_CLEAN=true
elif [[ -f \"\$STAMP_FILE\" ]]; then
    PREV_KERNEL=\$(cat \"\$STAMP_FILE\")
    if [[ \"\$PREV_KERNEL\" != \"\$KERNEL_RELEASE\" ]]; then
        echo \"--- Kernel changed (\$PREV_KERNEL -> \$KERNEL_RELEASE), rebuilding...\"
        NEED_CLEAN=true
    fi
elif [[ -f config.status ]]; then
    # First container build of a previously-configured tree
    echo '--- First container build, reconfiguring...'
    NEED_CLEAN=true
fi

if \$NEED_CLEAN; then
    make distclean 2>/dev/null || true
fi

echo '--- Running autogen.sh...'
bash autogen.sh 2>&1 | tail -3

# Default: client + server.  Server requires Whamcloud e2fsprogs
# (>= 1.47.3-wc2).  If not available, fall back to client-only.
echo '--- Running configure...'
set +e
./configure \
    --with-linux=/kernel \
    --disable-gss --disable-crypto \
    $CONFIGURE_ARGS \
    > /tmp/configure.log 2>&1
rc=\$?
set -e
tail -10 /tmp/configure.log
if [[ \$rc -ne 0 ]]; then
    if grep -q 'ext2fs' /tmp/configure.log && \
       ! echo \"$CONFIGURE_ARGS\" | grep -q 'disable-server'; then
        echo ''
        echo '--- NOTE: server build requires Whamcloud-patched e2fsprogs'
        echo '---   (https://downloads.whamcloud.com/public/e2fsprogs/)'
        echo '---   Falling back to client-only build.'
        echo '---   Use --configure \"--disable-server\" to skip this message,'
        echo '---   or install e2fsprogs-wc and rebuild for server support.'
        echo ''
        ./configure \
            --with-linux=/kernel \
            --disable-gss --disable-crypto \
            --disable-server \
            $CONFIGURE_ARGS \
            2>&1 | tail -10
    else
        echo 'configure failed' >&2
        exit 1
    fi
fi

echo '--- Building (j${JOBS})...'
make -j$JOBS 2>&1

# Record which kernel we built against so future runs
# can detect a kernel change and trigger a clean rebuild.
echo \"\$KERNEL_RELEASE\" > \"\$STAMP_FILE\"
echo '--- Build complete'
" 2>&1 | tail -30

# --- Verify ---

KO_COUNT=$(find "$LUSTRE_DIR" -name "*.ko" \
	-not -path "*/kconftest*" 2>/dev/null | wc -l)
echo ""
echo "=== Build complete ==="
echo "    $KO_COUNT kernel modules (.ko)"
echo "    Source tree: $LUSTRE_DIR"
echo ""
echo "Deploy to a VM with:"
echo "    sudo deploy-lustre.sh --vm myvm \\"
echo "        --build $LUSTRE_DIR --mount"
