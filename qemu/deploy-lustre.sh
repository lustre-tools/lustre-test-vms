#!/bin/bash
# Deploy a Lustre build to a QEMU VM and optionally mount a filesystem.
#
# Usage:
#   deploy-lustre.sh --vm NAME --build /path/to/lustre-release [options]
#
# Rsyncs kernel modules, userspace binaries, shared libraries, and test scripts
# to the target VM, runs depmod, and optionally runs llmount.sh.
#
# By default deploys everything. Use --userspace-only or --tests-only to limit scope.

set -euo pipefail

VM_DIR=/opt/qemu-vms
SSHPASS="sshpass -p initial0"
SSH_OPTS="-o StrictHostKeyChecking=no -o LogLevel=ERROR"

VM_NAME=""
BUILD_DIR=""
DO_MOUNT=false
SERVER_ONLY=false
DEPLOY_MODULES=true
DEPLOY_USERSPACE=true
DEPLOY_TESTS=true

usage() {
    cat <<EOF
Usage: ${0##*/} --vm NAME --build DIR [options]

  --vm NAME         VM name (as shown by vm.sh list)
  --build DIR       Path to a built lustre-release tree
  --mount           After deploying, run llmount.sh to create and mount a filesystem
  --server-only     With --mount, pass --server-only to llmount.sh (no client mount)
  --userspace-only  Deploy only userspace binaries and libraries (skip modules and tests)
  --tests-only      Deploy only the test framework scripts
  -h, --help        This help

By default all components are deployed (modules + userspace + tests).
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --vm)              VM_NAME="$2"; shift 2;;
        --build)           BUILD_DIR="$2"; shift 2;;
        --mount)           DO_MOUNT=true; shift;;
        --server-only)     SERVER_ONLY=true; shift;;
        --userspace-only)  DEPLOY_MODULES=false; DEPLOY_TESTS=false; shift;;
        --tests-only)      DEPLOY_MODULES=false; DEPLOY_USERSPACE=false; shift;;
        -h|--help)         usage;;
        *)                 echo "Unknown option: $1"; usage;;
    esac
done

[[ -z "${VM_NAME}" ]] && { echo "Error: --vm required"; usage; }
[[ -z "${BUILD_DIR}" ]] && { echo "Error: --build required"; usage; }
[[ -d "${BUILD_DIR}" ]] || { echo "Error: ${BUILD_DIR} is not a directory"; exit 1; }

# Resolve VM IP
INFO="${VM_DIR}/sockets/${VM_NAME}.info"
[[ -f "${INFO}" ]] || { echo "Error: VM '${VM_NAME}' not found (no ${INFO})"; exit 1; }
source "${INFO}"
# INFO may use VM_NAME or NAME depending on which script created it
TARGET_IP="${IP}"

RSYNC="$SSHPASS rsync -az -e 'ssh ${SSH_OPTS}'"
REMOTE="root@${TARGET_IP}"

echo "=== Deploying Lustre to ${VM_NAME} (${TARGET_IP}) ==="
echo "    Build: ${BUILD_DIR}"
echo "    Mode: redeploy-safe (will clean existing state)"

# Wait for SSH to be ready
echo "--- Waiting for SSH..."
for i in $(seq 1 30); do
    $SSHPASS ssh $SSH_OPTS -o ConnectTimeout=2 ${REMOTE} true 2>/dev/null && break
    sleep 1
done
$SSHPASS ssh $SSH_OPTS -o ConnectTimeout=2 ${REMOTE} true 2>/dev/null || {
    echo "Error: Cannot SSH to ${REMOTE} after 30s"
    exit 1
}

# Ensure rsync is available on the VM
$SSHPASS ssh $SSH_OPTS ${REMOTE} "which rsync" &>/dev/null || {
    echo "--- Installing rsync on VM..."
    VM_OS_ID=$($SSHPASS ssh $SSH_OPTS ${REMOTE} '. /etc/os-release; echo $ID' 2>/dev/null || echo "rocky")
    if [[ "${VM_OS_ID}" == "ubuntu" ]]; then
        $SSHPASS ssh $SSH_OPTS ${REMOTE} "apt-get install -y rsync" 2>&1 | tail -1
    else
        $SSHPASS ssh $SSH_OPTS ${REMOTE} "dnf install -y -q rsync" 2>&1 | tail -1
    fi
}

# Get kernel version on the VM
KVER=$($SSHPASS ssh $SSH_OPTS ${REMOTE} uname -r)
echo "    VM kernel: ${KVER}"

MODDIR="/lib/modules/${KVER}/extra/lustre"

# Detect VM OS -- use ID field (rocky vs ubuntu) and VERSION_ID
VM_OS_ID=$($SSHPASS ssh $SSH_OPTS ${REMOTE} \
	'. /etc/os-release; echo $ID' 2>/dev/null || echo "rocky")
VM_OS_VER=$($SSHPASS ssh $SSH_OPTS ${REMOTE} \
	'. /etc/os-release; echo ${VERSION_ID%%.*}' 2>/dev/null || echo "9")

# Set TESTDIR based on OS (Ubuntu uses /usr/lib, Rocky uses /usr/lib64)
if [[ "${VM_OS_ID}" == "ubuntu" ]]; then
	TESTDIR="/usr/lib/lustre/tests"
else
	TESTDIR="/usr/lib64/lustre/tests"
fi

# EL8 userspace cannot run EL9-built binaries (glibc version gap),
# so we build userspace inside the VM for EL8 only.
# Rocky 9 and Ubuntu 24 can use host-built binaries.
BUILD_IN_VM=false
[[ "${VM_OS_ID}" == "rocky" && "${VM_OS_VER}" == "8" ]] && BUILD_IN_VM=true
if $BUILD_IN_VM; then
	echo "    VM OS: EL${VM_OS_VER} -- userspace will be built in-VM"
else
	echo "    VM OS: ${VM_OS_ID} ${VM_OS_VER}"
fi

# --- Clean up existing Lustre state ---
echo "--- Cleaning existing Lustre state..."

# Unmount Lustre filesystems if mounted
$SSHPASS ssh $SSH_OPTS ${REMOTE} '
	if mount -t lustre 2>/dev/null | grep -q lustre; then
		echo "  Unmounting Lustre..."
		cd /tmp
		bash /usr/lib64/lustre/tests/llmountcleanup.sh 2>/dev/null || true
		umount -a -t lustre 2>/dev/null || true
	fi
' 2>/dev/null || true

# Unload Lustre modules if loaded
$SSHPASS ssh $SSH_OPTS ${REMOTE} '
	if lsmod 2>/dev/null | grep -q lustre; then
		echo "  Unloading Lustre modules..."
		lustre_rmmod 2>/dev/null || true
	fi
' 2>/dev/null || true

# Clear any stale dm devices that block mkfs
$SSHPASS ssh $SSH_OPTS ${REMOTE} \
	"dmsetup remove_all 2>/dev/null" || true

# --- Kernel modules ---
if $DEPLOY_MODULES; then
    echo "--- Syncing kernel modules..."
    KO_TMPDIR=$(mktemp -d)
    trap "rm -rf ${KO_TMPDIR}" EXIT
    find "${BUILD_DIR}" -name '*.ko' -not -path '*/kconftest*' -exec cp {} "${KO_TMPDIR}/" \;
    KO_COUNT=$(ls "${KO_TMPDIR}"/*.ko 2>/dev/null | wc -l)
    echo "    ${KO_COUNT} modules"

    $SSHPASS ssh $SSH_OPTS ${REMOTE} "mkdir -p ${MODDIR}"
    $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" "${KO_TMPDIR}/" "${REMOTE}:${MODDIR}/"

    echo "--- Running depmod..."
    $SSHPASS ssh $SSH_OPTS ${REMOTE} "depmod -a ${KVER}"
fi

# --- Shared libraries and userspace binaries ---
if $DEPLOY_USERSPACE; then
    if $BUILD_IN_VM; then
	# EL8: host-built binaries use EL9 glibc -- build in-VM instead.
	# Rsync source (no .git, no build artifacts), configure, and
	# make install inside the VM.  Kernel modules are still taken from
	# the host build (they were compiled against the custom EL8 kernel).
	KVER_DEVEL=$($SSHPASS ssh $SSH_OPTS ${REMOTE} \
		'ls /usr/src/kernels/ 2>/dev/null | tail -1')
	[[ -z "${KVER_DEVEL}" ]] && {
		echo "ERROR: no kernel-devel found in VM; cannot build userspace"
		exit 1
	}
	echo "--- Syncing source for in-VM build..."
	$SSHPASS ssh $SSH_OPTS ${REMOTE} "mkdir -p ${BUILD_DIR}"
	$SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
		--exclude='.git' \
		--exclude='*.ko' --exclude='*.o' \
		--exclude='*.a' --exclude='*.lo' \
		"${BUILD_DIR}/" "${REMOTE}:${BUILD_DIR}/"
	echo "--- Building Lustre userspace in-VM (EL8, kernel-devel ${KVER_DEVEL})..."
	$SSHPASS ssh $SSH_OPTS ${REMOTE} "
		set -e
		cd ${BUILD_DIR}
		# Remove stale kconftest artifacts that poison configure
		rm -f kconftest.dir/conftest.* \
		      kconftest.dir/*.o kconftest.dir/*.ko \
		      kconftest.dir/*.mod* 2>/dev/null || true
		./configure \
			--with-linux=/usr/src/kernels/${KVER_DEVEL} \
			--disable-gss --disable-crypto --disable-server
		make -j\$(nproc)
		make install
		ldconfig
	"
    else
	LIBDIR="/usr/lib64"
	[[ "${VM_OS_ID}" == "ubuntu" ]] && LIBDIR="/usr/lib"
	echo "--- Syncing shared libraries..."
	$SSHPASS ssh $SSH_OPTS ${REMOTE} "mkdir -p ${LIBDIR}"

	$SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
		"${BUILD_DIR}/lustre/utils/.libs/liblustreapi.so"* \
		"${REMOTE}:${LIBDIR}/"
	$SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
		"${BUILD_DIR}/lnet/utils/lnetconfig/.libs/liblnetconfig.so"* \
		"${REMOTE}:${LIBDIR}/"
	$SSHPASS ssh $SSH_OPTS ${REMOTE} "ldconfig"

	echo "--- Syncing userspace binaries..."
	$SSHPASS ssh $SSH_OPTS ${REMOTE} "mkdir -p /usr/sbin /usr/bin"

	# Core binaries -> /usr/sbin
	for bin in lctl mkfs.lustre mount.lustre tunefs.lustre; do
		src="${BUILD_DIR}/lustre/utils/.libs/${bin}"
		[[ -f "${src}" ]] && $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
			"${src}" "${REMOTE}:/usr/sbin/"
	done

	# mount helper .so plugins
	for so in mount_osd_ldiskfs.so mount_osd_wbcfs.so; do
		src="${BUILD_DIR}/lustre/utils/.libs/${so}"
		[[ -f "${src}" ]] && $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
			"${src}" "${REMOTE}:${LIBDIR}/"
	done

	# User-facing binaries -> /usr/bin
	for bin in lfs llog_reader lustre_rsync lhsmtool_posix; do
		src="${BUILD_DIR}/lustre/utils/.libs/${bin}"
		[[ -f "${src}" ]] && $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
			"${src}" "${REMOTE}:/usr/bin/"
	done

	# lnetctl
	LNETCTL="${BUILD_DIR}/lnet/utils/.libs/lnetctl"
	[[ -f "${LNETCTL}" ]] && $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
		"${LNETCTL}" "${REMOTE}:/usr/sbin/"

	# Shell script utils -> /usr/bin
	for script in \
		llstat llobdstat ll_decode_filter_fid \
		ll_decode_linkea llverdev llverfs; do
		src="${BUILD_DIR}/lustre/utils/${script}"
		[[ -f "${src}" ]] && $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
			"${src}" "${REMOTE}:/usr/bin/"
	done
    fi
fi

# --- Test framework (needed for llmount.sh) ---
if $DEPLOY_TESTS; then
    echo "--- Syncing test framework..."
    $SSHPASS ssh $SSH_OPTS ${REMOTE} "mkdir -p /usr/lib64/lustre/tests"
    $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
        "${BUILD_DIR}/lustre/tests/" \
        "${REMOTE}:/usr/lib64/lustre/tests/" \
        --exclude='*.o' --exclude='*.lo' --exclude='.libs' --exclude='.deps'

    $SSHPASS ssh $SSH_OPTS ${REMOTE} "mkdir -p /usr/lib64/lustre/scripts"
    $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
        "${BUILD_DIR}/lustre/scripts/" \
        "${REMOTE}:/usr/lib64/lustre/scripts/" \
        --exclude='*.o' --exclude='*.lo' --exclude='.libs' --exclude='.deps'
fi

echo "=== Deploy complete ==="

# --- Configure Lustre devices in cfg/local.sh if VM has extra disks ---
MDT_DISKS="${MDT_DISKS:-0}"
OST_DISKS="${OST_DISKS:-0}"
TOTAL_DISKS=$((MDT_DISKS + OST_DISKS))
if [[ ${TOTAL_DISKS} -gt 0 ]]; then
    echo "--- Configuring Lustre devices (${MDT_DISKS} MDT, ${OST_DISKS} OST)..."
    # Drives are attached in order: MDT disks first, then OST disks
    # rootfs=/dev/vda, so disk1=/dev/vdb, disk2=/dev/vdc, ...
    # Letter for disk N: chr(97 + N) where 97='a', so disk1='b', disk2='c', ...
    LOCAL_SH_SNIPPET=""

    # MDT devices
    if [[ ${MDT_DISKS} -gt 0 ]]; then
        LOCAL_SH_SNIPPET+="MDSCOUNT=${MDT_DISKS}"$'\n'
        for n in $(seq 1 ${MDT_DISKS}); do
            LETTER=$(printf "\\x$(printf '%02x' $((97 + n)))")
            LOCAL_SH_SNIPPET+="MDSDEV${n}=/dev/vd${LETTER}"$'\n'
        done
    fi

    # OST devices (continue after MDT drives)
    if [[ ${OST_DISKS} -gt 0 ]]; then
        LOCAL_SH_SNIPPET+="OSTCOUNT=${OST_DISKS}"$'\n'
        for n in $(seq 1 ${OST_DISKS}); do
            LETTER=$(printf "\\x$(printf '%02x' $((97 + MDT_DISKS + n)))")
            LOCAL_SH_SNIPPET+="OSTDEV${n}=/dev/vd${LETTER}"$'\n'
        done
    fi

    # Write into the VM's cfg/local.sh (idempotent: replace block)
    $SSHPASS ssh $SSH_OPTS ${REMOTE} "
	sed -i '/^# --- VM disk configuration/,/^# --- END VM disk/d' \
		${TESTDIR}/cfg/local.sh 2>/dev/null || true
	cat >> ${TESTDIR}/cfg/local.sh
    " <<LOCALEOF

# --- VM disk configuration (generated by deploy-lustre.sh) ---
${LOCAL_SH_SNIPPET}# --- END VM disk configuration ---
LOCALEOF

    # Log what was configured
    MDT_START_LETTER=$(printf "\\x$(printf '%02x' 98)")
    MDT_END_LETTER=$(printf "\\x$(printf '%02x' $((97 + MDT_DISKS)))")
    OST_START_LETTER=$(printf "\\x$(printf '%02x' $((98 + MDT_DISKS)))")
    OST_END_LETTER=$(printf "\\x$(printf '%02x' $((97 + MDT_DISKS + OST_DISKS)))")
    [[ ${MDT_DISKS} -gt 0 ]] && echo "    MDTs: /dev/vd${MDT_START_LETTER}../dev/vd${MDT_END_LETTER} (${MDT_DISKS} disks)"
    [[ ${OST_DISKS} -gt 0 ]] && echo "    OSTs: /dev/vd${OST_START_LETTER}../dev/vd${OST_END_LETTER} (${OST_DISKS} disks)"
fi

# --- Optional: mount Lustre ---
if $DO_MOUNT; then
    echo "=== Running llmount.sh ==="
    LLMOUNT_ARGS=""
    $SERVER_ONLY && LLMOUNT_ARGS="--server-only"

    LUSTRE_DIR="/usr/lib64/lustre"
    [[ "${VM_OS_ID}" == "ubuntu" ]] && LUSTRE_DIR="/usr/lib/lustre"
    MOUNT_CMD="cd ${TESTDIR} && LUSTRE=${LUSTRE_DIR} bash llmount.sh ${LLMOUNT_ARGS}"

    if ! $SSHPASS ssh $SSH_OPTS ${REMOTE} "${MOUNT_CMD}" 2>&1; then
        echo "--- llmount.sh failed, retrying after dmsetup remove_all..."
        $SSHPASS ssh $SSH_OPTS ${REMOTE} "dmsetup remove_all 2>/dev/null" || true
        $SSHPASS ssh $SSH_OPTS ${REMOTE} "${MOUNT_CMD}"
    fi

    echo "=== Lustre mounted ==="
    $SSHPASS ssh $SSH_OPTS ${REMOTE} "mount -t lustre" || true
    $SSHPASS ssh $SSH_OPTS ${REMOTE} "lctl dl" || true
fi
