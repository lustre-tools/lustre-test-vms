#!/bin/bash
# Build a kernel for QEMU microvm boot (Rocky 8, Rocky 9, or Ubuntu 24.04).
#
# Uses a container (podman or docker) to get the matching GCC version,
# avoiding cross-version -Werror and codegen problems.
#
# Usage:
#   build-kernel.sh --download --os rocky9    # download SRPM and build
#   build-kernel.sh --download --os ubuntu24  # download kernel.org tarball and build
#   build-kernel.sh --srpm <kernel.src.rpm>   # build from local SRPM
#   build-kernel.sh --source <tarball> --config <.config> --os rocky8
#
# Outputs:
#   <install-dir>/vmlinuz-<os>  -- bzImage for QEMU -kernel
#   <install-dir>/vmlinux-<os>  -- unstripped ELF for crash/drgn analysis
#
# Requirements: podman or docker, curl, rpm2cpio, cpio, readelf

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EL_VER=""
OS_NAME=""
SRPM=""
SOURCE_TAR=""
CONFIG_FILE=""
INSTALL_DIR="/opt/qemu-vms/kernel"
JOBS=$(nproc)
CONTAINER_RT=""
DO_DOWNLOAD=false
KERNEL_VERSION=""
KEEP_BUILD=false

# Rocky Linux mirror URLs for kernel SRPMs
ROCKY8_PKGS="https://dl.rockylinux.org/pub/rocky/8/BaseOS/source/tree/Packages/k"
ROCKY9_PKGS="https://dl.rockylinux.org/pub/rocky/9/BaseOS/source/tree/Packages/k"

usage() {
	cat <<-EOF
	Usage: ${0##*/} [options]

	  --download          Download a kernel SRPM/tarball (requires --os or --el)
	  --kernel-version V  Specific version (e.g., 553.97.1 or 611.36.1)
	                      Default: latest available
	  --keep              Keep the kernel build tree after install
	                      (needed for building Lustre against it)
	  --srpm FILE         Use a local kernel source RPM
	  --source FILE       Use a kernel source tarball (linux-*.tar.xz)
	  --config FILE       Kernel .config file (required with --source)
	  --os rocky8|rocky9|ubuntu24  Target OS (preferred)
	  --el 8|9            EL version (backward compat alias for --os rocky8/rocky9)
	  --install-dir DIR   Output directory (default: $INSTALL_DIR)
	  --jobs N            Parallel make jobs (default: $JOBS)
	  -h, --help          This help

	One of --download, --srpm, or (--source + --config) is required.

	Examples:
	  # Latest Rocky 8, Rocky 9, or Ubuntu 24.04 kernel
	  sudo ${0##*/} --download --os rocky8
	  sudo ${0##*/} --download --os rocky9
	  sudo ${0##*/} --download --os ubuntu24

	  # Backward compat (--el still works)
	  sudo ${0##*/} --download --el 8
	  sudo ${0##*/} --download --el 9

	  # Specific kernel version
	  sudo ${0##*/} --download --os rocky8 --kernel-version 553.97.1
	  sudo ${0##*/} --download --os rocky9 --kernel-version 611.36.1

	  # From a local SRPM (EL version auto-detected)
	  sudo ${0##*/} --srpm kernel-4.18.0-553.97.1.el8_10.src.rpm
	EOF
	exit 1
}

die() { echo "Error: $*" >&2; exit 1; }

# --- Requirement checks ---

check_requirements() {
	local missing=()

	# Container runtime
	if ! command -v podman &>/dev/null && \
	   ! command -v docker &>/dev/null; then
		missing+=("podman or docker")
	fi

	# SRPM extraction
	if ! command -v rpm2cpio &>/dev/null; then
		missing+=("rpm2cpio")
	fi
	if ! command -v cpio &>/dev/null; then
		missing+=("cpio")
	fi

	# Download
	if ! command -v curl &>/dev/null; then
		missing+=("curl")
	fi

	# Verification
	if ! command -v readelf &>/dev/null; then
		missing+=("readelf (binutils)")
	fi

	if [[ ${#missing[@]} -gt 0 ]]; then
		echo "Missing requirements:" >&2
		for tool in "${missing[@]}"; do
			echo "  - $tool" >&2
		done
		echo "" >&2
		# Suggest install commands for common distros
		echo "Install on EL/Fedora:" >&2
		echo "  dnf install podman rpm cpio curl binutils" >&2
		echo "Install on Debian/Ubuntu:" >&2
		echo "  apt install podman rpm2cpio cpio curl binutils" >&2
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

# Download vanilla kernel 6.8.12 for Ubuntu 24.04.
# Sets SOURCE_TAR and CONFIG_FILE.
download_ubuntu_kernel() {
	local kver="6.8.12"
	local url="https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-${kver}.tar.xz"

	echo "--- Downloading Linux ${kver} for Ubuntu 24.04..."
	local dl_dir
	dl_dir=$(mktemp -d "/tmp/kernel-ubuntu24-XXXXXX")
	SOURCE_TAR="${dl_dir}/linux-${kver}.tar.xz"

	curl -fSL --progress-bar -o "$SOURCE_TAR" "$url" || \
		die "Download failed: $url"
	echo "    Saved: $SOURCE_TAR"

	# We'll use defconfig + microvm tweaks (no separate config file needed)
	CONFIG_FILE=""
}

# Download the latest kernel SRPM for the given EL version.
# Sets SRPM to the path of the downloaded file.
download_srpm() {
	local el=$1
	local vault_url pkg_name

	echo "--- Finding latest kernel SRPM for EL${el}..."

	local base_url
	if [[ "$el" == "8" ]]; then
		base_url="$ROCKY8_PKGS"
	elif [[ "$el" == "9" ]]; then
		base_url="$ROCKY9_PKGS"
	else
		die "download only supports EL 8 or 9"
	fi

	# Scrape the directory listing for kernel SRPMs.
	# Exclude base packages (e.g., kernel-4.18.0-553.el8_10)
	# which sort after updates due to missing sub-version.
	local all_pkgs
	all_pkgs=$(curl -fsSL "$base_url/" \
		| grep -oP 'kernel-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+\.[0-9]+\.[0-9]+\.el'"${el}"'[^"]*\.src\.rpm' \
		| sort -Vu)

	if [[ -n "$KERNEL_VERSION" ]]; then
		# Match specific version (e.g., 553.97.1 or 611.36.1)
		pkg_name=$(echo "$all_pkgs" \
			| grep "$KERNEL_VERSION" | tail -1)
		[[ -n "$pkg_name" ]] || \
			die "No SRPM matching version '$KERNEL_VERSION' at $base_url/"
	else
		pkg_name=$(echo "$all_pkgs" | tail -1)
	fi

	[[ -n "$pkg_name" ]] || \
		die "Could not find kernel SRPM at $base_url/"

	echo "    Found: $pkg_name"

	local dl_dir
	dl_dir=$(mktemp -d "/tmp/kernel-srpm-XXXXXX")
	SRPM="$dl_dir/$pkg_name"

	echo "--- Downloading (~100-150 MB)..."
	curl -fSL --progress-bar -o "$SRPM" \
		"$base_url/$pkg_name" || \
		die "Download failed: $base_url/$pkg_name"
	echo "    Saved: $SRPM"
}

# Show a progress summary while the build runs.
# Reads make output on stdin, prints a status line to stderr,
# and passes all output through to stdout.
build_progress() {
	local count=0 last_file="" phase="compiling"
	while IFS= read -r line; do
		echo "$line"
		case "$line" in
			*"  CC "*)
				((count++)) || true
				last_file="${line##*CC *}"
				last_file="${last_file##*/}"
				;;
			*"  LD "*)
				phase="linking"
				last_file="${line##*LD *}"
				last_file="${last_file##*/}"
				;;
			*"  AR "*)
				last_file="${line##*AR *}"
				last_file="${last_file##*/}"
				;;
		esac
		if ((count % 100 == 0)) && ((count > 0)); then
			printf '\r--- [%d files compiled] %s  ' \
				"$count" "$phase" >&2
		fi
	done
	if ((count > 0)); then
		printf '\r--- [%d files compiled] done%*s\n' \
			"$count" 20 "" >&2
	fi
}

while [[ $# -gt 0 ]]; do
	case $1 in
		--download)    DO_DOWNLOAD=true; shift ;;
		--kernel-version) KERNEL_VERSION="$2"; shift 2 ;;
		--keep)        KEEP_BUILD=true; shift ;;
		--srpm)        SRPM="$2"; shift 2 ;;
		--source)      SOURCE_TAR="$2"; shift 2 ;;
		--config)      CONFIG_FILE="$2"; shift 2 ;;
		--os)          OS_NAME="$2"; shift 2 ;;
		--el)          EL_VER="$2"; shift 2 ;;
		--install-dir) INSTALL_DIR="$2"; shift 2 ;;
		--jobs)        JOBS="$2"; shift 2 ;;
		-h|--help)     usage ;;
		*)             die "Unknown option: $1" ;;
	esac
done

# --- Check requirements before doing anything ---

check_requirements
detect_container_rt

# --- Resolve OS name / EL version ---

# --os takes precedence; --el is a backward-compat alias
if [[ -n "$OS_NAME" ]]; then
	case "$OS_NAME" in
		rocky8)   EL_VER=8 ;;
		rocky9)   EL_VER=9 ;;
		ubuntu24) EL_VER="ubuntu24" ;;
		*) die "--os must be rocky8, rocky9, or ubuntu24 (got: $OS_NAME)" ;;
	esac
elif [[ -n "$EL_VER" ]]; then
	# Backward compat: --el 8 -> rocky8, --el 9 -> rocky9
	case "$EL_VER" in
		8) OS_NAME="rocky8" ;;
		9) OS_NAME="rocky9" ;;
		*) die "--el must be 8 or 9 (got: $EL_VER)" ;;
	esac
fi

# --- Resolve source ---

if $DO_DOWNLOAD; then
	if [[ "$EL_VER" == "ubuntu24" ]]; then
		# Ubuntu 24: download vanilla kernel from kernel.org
		download_ubuntu_kernel
	elif [[ -n "$EL_VER" ]]; then
		download_srpm "$EL_VER"
	else
		die "--os or --el required with --download"
	fi
fi

if [[ -n "$SRPM" ]]; then
	[[ -f "$SRPM" ]] || die "SRPM not found: $SRPM"
	if [[ -z "$EL_VER" ]]; then
		if [[ "$SRPM" == *el8* ]]; then
			EL_VER=8
		elif [[ "$SRPM" == *el9* ]]; then
			EL_VER=9
		else
			die "Cannot detect EL version from SRPM name; use --el or --os"
		fi
	fi
elif [[ -n "$SOURCE_TAR" ]]; then
	[[ -f "$SOURCE_TAR" ]] || die "Source tarball not found: $SOURCE_TAR"
	if [[ "$EL_VER" != "ubuntu24" ]]; then
		[[ -n "$CONFIG_FILE" ]] || die "--config required with --source"
		[[ -f "$CONFIG_FILE" ]] || die "Config not found: $CONFIG_FILE"
	fi
	if [[ -z "$EL_VER" ]]; then
		if [[ "$SOURCE_TAR" == *el8* ]]; then
			EL_VER=8
		elif [[ "$SOURCE_TAR" == *el9* ]]; then
			EL_VER=9
		else
			die "Cannot detect EL version from tarball; use --el or --os"
		fi
	fi
else
	die "One of --download, --srpm, or --source is required"
fi

# Determine OS_NAME if not already set
if [[ -z "$OS_NAME" ]]; then
	case "$EL_VER" in
		8) OS_NAME="rocky8" ;;
		9) OS_NAME="rocky9" ;;
		ubuntu24) OS_NAME="ubuntu24" ;;
		*) die "Unknown EL_VER: $EL_VER" ;;
	esac
fi

if [[ "$EL_VER" == "ubuntu24" ]]; then
	CONTAINERFILE="${SCRIPT_DIR}/Containerfile.ubuntu24"
	IMAGE_TAG="ubuntu24-kernel-builder"
else
	[[ "$EL_VER" == "8" || "$EL_VER" == "9" ]] || \
		die "--el must be 8 or 9 (got: $EL_VER)"
	CONTAINERFILE="${SCRIPT_DIR}/Containerfile.el${EL_VER}"
	IMAGE_TAG="el${EL_VER}-kernel-builder"
fi
[[ -f "$CONTAINERFILE" ]] || \
	die "Containerfile not found: $CONTAINERFILE"

# --- Build container image if needed ---

echo "=== Building container image: $IMAGE_TAG ==="
$CONTAINER_RT build -t "$IMAGE_TAG" \
	-f "$CONTAINERFILE" "$SCRIPT_DIR"

# --- Prepare build directory ---

BUILD_DIR=$(mktemp -d "/tmp/kernel-build-${OS_NAME}-XXXXXX")
if ! $KEEP_BUILD; then
	trap "rm -rf $BUILD_DIR" EXIT
fi
echo "=== Build directory: $BUILD_DIR ==="

if [[ -n "$SRPM" ]]; then
	echo "--- Extracting SRPM..."
	RPM_DIR="$BUILD_DIR/rpmbuild"
	mkdir -p "$RPM_DIR"/{SOURCES,SPECS}
	rpm2cpio "$SRPM" | \
		(cd "$RPM_DIR/SOURCES" && cpio -idm 2>/dev/null)

	# Find the source tarball
	SOURCE_TAR=$(find "$RPM_DIR/SOURCES" \
		-name "linux-*.tar.xz" | head -1)
	[[ -n "$SOURCE_TAR" ]] || die "No linux-*.tar.xz in SRPM"

	# Find the config -- prefer non-debug x86_64 config
	CONFIG_FILE=$(find "$RPM_DIR/SOURCES" \
		-name "kernel-x86_64*.config" \
		! -name "*debug*" | head -1)
	[[ -n "$CONFIG_FILE" ]] || \
		die "No x86_64 config in SRPM"
fi

echo "--- Extracting source tarball..."
tar xf "$SOURCE_TAR" -C "$BUILD_DIR"
SRC_DIR=$(find "$BUILD_DIR" -maxdepth 1 \
	-name "linux-*" -type d | head -1)
[[ -d "$SRC_DIR" ]] || \
	die "Source directory not found after extraction"

if [[ "$EL_VER" == "ubuntu24" ]]; then
	# Ubuntu 24: use defconfig + microvm tweaks
	echo "--- Generating defconfig + microvm tweaks..."
	$CONTAINER_RT run --rm \
		-v "$SRC_DIR:/build:Z" \
		"$IMAGE_TAG" -c "make defconfig"
	# Enable configs needed for QEMU microvm
	cat >>"$SRC_DIR/.config" <<-UBCFG
	CONFIG_XEN_PVH=y
	CONFIG_PVH=y
	CONFIG_VIRTIO_BLK=y
	CONFIG_VIRTIO_NET=y
	CONFIG_VIRTIO_PCI=y
	CONFIG_VIRTIO_CONSOLE=y
	CONFIG_VIRTIO_MMIO=y
	CONFIG_9P_FS=y
	CONFIG_NET_9P=y
	CONFIG_NET_9P_VIRTIO=y
	CONFIG_EXT4_FS=y
	CONFIG_OVERLAY_FS=y
	CONFIG_SERIAL_8250=y
	CONFIG_SERIAL_8250_CONSOLE=y
	CONFIG_VETH=y
	CONFIG_BRIDGE=y
	CONFIG_TUN=y
	CONFIG_FUSE_FS=y
	CONFIG_KEXEC=y
	CONFIG_CRASH_DUMP=y
	UBCFG
else
	echo "--- Applying config..."
	cp "$CONFIG_FILE" "$SRC_DIR/.config"

	# --- Config tweaks for QEMU microvm boot ---

	# CONFIG_XEN_PVH provides the PVH ELF note that QEMU microvm
	# requires when booting an uncompressed vmlinux.
	if ! grep -q "^CONFIG_XEN_PVH=y" "$SRC_DIR/.config"; then
		sed -i \
			's/# CONFIG_XEN_PVH is not set/CONFIG_XEN_PVH=y/' \
			"$SRC_DIR/.config"
		echo "    Enabled CONFIG_XEN_PVH for QEMU microvm"
	fi
fi

SRC_BASENAME=$(basename "$SRC_DIR")
echo "=== Building $SRC_BASENAME (${OS_NAME} container, j${JOBS}) ==="

$CONTAINER_RT run --rm \
	-v "$SRC_DIR:/build:Z" \
	"$IMAGE_TAG" -c "
set -e
echo '--- GCC version:'
gcc --version | head -1
echo '--- Running olddefconfig...'
make olddefconfig 2>&1 | tail -3
echo '--- Building vmlinux + bzImage...'
make -j$JOBS vmlinux bzImage 2>&1
echo '--- Build complete'
" 2>&1 | build_progress

# --- Verify outputs ---

VMLINUX="$SRC_DIR/vmlinux"
BZIMAGE="$SRC_DIR/arch/x86/boot/bzImage"

[[ -f "$VMLINUX" ]] || die "vmlinux not found after build"
[[ -f "$BZIMAGE" ]] || die "bzImage not found after build"

echo "=== Verifying build ==="
echo "    vmlinux: $(ls -lh "$VMLINUX" | awk '{print $5}')"
echo "    bzImage: $(ls -lh "$BZIMAGE" | awk '{print $5}')"

if readelf -n "$VMLINUX" 2>/dev/null | grep -q "0x00000012"; then
	echo "    PVH ELF note: present"
else
	echo "    PVH ELF note: absent (ok for bzImage boot)"
fi

KERNEL_VER=$($CONTAINER_RT run --rm \
	-v "$SRC_DIR:/build:Z" \
	"$IMAGE_TAG" -c 'make -s kernelrelease')
echo "    Kernel version: $KERNEL_VER"

# --- Install ---

mkdir -p "$INSTALL_DIR"
cp "$VMLINUX" "$INSTALL_DIR/vmlinux-${OS_NAME}"
cp "$BZIMAGE" "$INSTALL_DIR/vmlinuz-${OS_NAME}"
echo "=== Installed ==="
echo "    $INSTALL_DIR/vmlinux-${OS_NAME}  (debug symbols)"
echo "    $INSTALL_DIR/vmlinuz-${OS_NAME}  (bzImage for boot)"
echo ""
echo "Boot a VM with:"
echo "    sudo vm.sh ensure myvm --vcpus 2 --mem 4096 \\"
echo "        --kernel $INSTALL_DIR/vmlinuz-${OS_NAME}"

if $KEEP_BUILD; then
	echo ""
	echo "Kernel build tree (for Lustre builds):"
	echo "    $SRC_DIR"
	echo ""
	echo "Build Lustre against it:"
	echo "    build-lustre.sh --lustre ~/lustre-release \\"
	echo "        --kernel-tree $SRC_DIR --os ${OS_NAME}"
fi

echo "=== Done ==="
