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
VM_ROOT_PASSWORD="${VM_ROOT_PASSWORD:-initial0}"
SSHPASS="sshpass -p $VM_ROOT_PASSWORD"
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

# Check that Lustre modules match the VM kernel
if ${DEPLOY_MODULES}; then
	SAMPLE_KO=$(find "${BUILD_DIR}/lustre" -name 'lustre.ko' -type f 2>/dev/null | head -1)
	if [[ -n "${SAMPLE_KO}" ]]; then
		MOD_VER=$(modinfo -F vermagic "${SAMPLE_KO}" | awk '{print $1}')
		if [[ "${MOD_VER}" != "${KVER}" ]]; then
			echo "ERROR: kernel mismatch"
			echo "  Lustre modules built for: ${MOD_VER}"
			echo "  VM running kernel:        ${KVER}"
			echo "  Rebuild Lustre or fix the VM kernel."
			exit 1
		fi
	fi
fi

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

echo "    VM OS: ${VM_OS_ID} ${VM_OS_VER}"

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

    # Install kernel base modules if missing in VM.
    # The ltvm kernel build places them under output/<target>/kernels/*/modules/.
    # Find the right directory by matching the VM's kernel version.
    # Find ltvm output directory.  Try LTVM_DIR env, then common locations.
    _ltvm_dir="${LTVM_DIR:-}"
    if [[ -z "${_ltvm_dir}" ]]; then
        for _d in \
            "$(dirname "$(dirname "$(readlink -f "$0")")")" \
            "${HOME}/lustre-test-vms-v2" \
            /home/*/lustre-test-vms-v2; do
            if [[ -d "${_d}/output" ]]; then
                _ltvm_dir="${_d}"
                break
            fi
        done
    fi
    HAS_KERNEL_DIR=$($SSHPASS ssh $SSH_OPTS ${REMOTE} \
        "test -d /lib/modules/${KVER}/kernel && echo yes || echo no")
    if [[ "${HAS_KERNEL_DIR}" == "no" ]]; then
        KMOD_SRC=""
        for kdir in "${_ltvm_dir}"/output/*/kernels/*/modules/lib/modules/"${KVER}"; do
            if [[ -d "${kdir}/kernel" ]]; then
                KMOD_SRC="${kdir}"
                break
            fi
        done
        if [[ -n "${KMOD_SRC}" ]]; then
            echo "--- Installing kernel base modules from ltvm build..."
            $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" --exclude='build' \
                "${KMOD_SRC}/" "${REMOTE}:/lib/modules/${KVER}/"
        else
            echo "    WARNING: No kernel base modules found for ${KVER}"
            echo "    depmod may report warnings about missing modules.order"
        fi
    fi

    # Install vmlinuz for kdump if missing
    HAS_VMLINUZ=$($SSHPASS ssh $SSH_OPTS ${REMOTE} \
        "test -f /boot/vmlinuz-${KVER} && echo yes || echo no")
    if [[ "${HAS_VMLINUZ}" == "no" && -n "${_ltvm_dir}" ]]; then
        for _vz in "${_ltvm_dir}"/output/*/kernels/*/vmlinuz; do
            # Match by checking the kernel version in the same dir
            _kdir="$(dirname "${_vz}")"
            if [[ -f "${_kdir}/build-tree/kernel-version" ]]; then
                _bkver=$(cat "${_kdir}/build-tree/kernel-version")
                if [[ "${_bkver}" == "${KVER}" ]]; then
                    echo "--- Installing vmlinuz for kdump..."
                    $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
                        "${_vz}" "${REMOTE}:/boot/vmlinuz-${KVER}"
                    # Pre-load kdump kernel (skip initrd -- direct root mount)
                    $SSHPASS ssh $SSH_OPTS ${REMOTE} \
                        "kexec -p /boot/vmlinuz-${KVER} --reuse-cmdline 2>/dev/null" || true
                    break
                fi
            fi
        done
    fi

    # Remove autoconf probe .ko files (invalid, confuse depmod)
    $SSHPASS ssh $SSH_OPTS ${REMOTE} \
        "find ${MODDIR} -name '*_pc.ko' -delete 2>/dev/null" || true

    echo "--- Running depmod..."
    $SSHPASS ssh $SSH_OPTS ${REMOTE} "depmod -a ${KVER}"
fi

# --- Userspace, libraries, and test framework ---
# If a staging tree exists (from `make install DESTDIR=.staging`),
# rsync it wholesale -- this is the preferred path because make
# install already knows the correct layout.  Otherwise fall back
# to file-by-file rsync from the build tree.
STAGING="${BUILD_DIR}/.staging"

if [[ -d "${STAGING}/usr" ]]; then
    # Staging tree exists -- rsync the entire installed layout.
    if $DEPLOY_USERSPACE; then
	echo "--- Syncing userspace from staging tree..."
	$SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
		"${STAGING}/usr/" "${REMOTE}:/usr/" \
		--exclude='*.la' --exclude='*.a' \
		--exclude='include/' --exclude='share/man/'
	$SSHPASS ssh $SSH_OPTS ${REMOTE} "ldconfig"
    fi
    if $DEPLOY_TESTS; then
	echo "--- Syncing test framework from staging tree..."
	LUSTRE_DIR="$(dirname "${TESTDIR}")"
	# Tests and scripts are installed under pkglibdir (e.g.
	# /usr/lib64/lustre/tests/).  The staging tree includes
	# them, but the main rsync above excludes .o/.la files
	# which is sufficient.  Sync scripts separately since
	# make install may place them under share/ or libexec/.
	$SSHPASS ssh $SSH_OPTS ${REMOTE} "mkdir -p ${TESTDIR}"
	if [[ -d "${STAGING}${TESTDIR}" ]]; then
		$SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
			"${STAGING}${TESTDIR}/" "${REMOTE}:${TESTDIR}/" \
			--exclude='*.o' --exclude='*.lo'
	fi
	$SSHPASS ssh $SSH_OPTS ${REMOTE} "mkdir -p ${LUSTRE_DIR}/scripts"
	if [[ -d "${STAGING}${LUSTRE_DIR}/scripts" ]]; then
		$SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
			"${STAGING}${LUSTRE_DIR}/scripts/" \
			"${REMOTE}:${LUSTRE_DIR}/scripts/"
	fi
    fi
else
    # No staging tree -- fall back to file-by-file rsync.
    if $DEPLOY_USERSPACE; then
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

	for bin in lctl mkfs.lustre mount.lustre tunefs.lustre; do
		src="${BUILD_DIR}/lustre/utils/.libs/${bin}"
		[[ -f "${src}" ]] && $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
			"${src}" "${REMOTE}:/usr/sbin/"
	done

	# mount helper .so plugins -> /usr/lib64/lustre/ (where mkfs.lustre dlopen()s)
	$SSHPASS ssh $SSH_OPTS ${REMOTE} "mkdir -p ${LIBDIR}/lustre"
	for so in mount_osd_ldiskfs.so mount_osd_wbcfs.so; do
		src="${BUILD_DIR}/lustre/utils/.libs/${so}"
		[[ -f "${src}" ]] || src="${BUILD_DIR}/lustre/utils/${so}"
		[[ -f "${src}" ]] && $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
			"${src}" "${REMOTE}:${LIBDIR}/lustre/"
	done

	for bin in lfs llog_reader lustre_rsync lhsmtool_posix; do
		src="${BUILD_DIR}/lustre/utils/.libs/${bin}"
		[[ -f "${src}" ]] && $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
			"${src}" "${REMOTE}:/usr/bin/"
	done

	LNETCTL="${BUILD_DIR}/lnet/utils/.libs/lnetctl"
	[[ -f "${LNETCTL}" ]] && $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
		"${LNETCTL}" "${REMOTE}:/usr/sbin/"

	for script in \
		llstat llobdstat ll_decode_filter_fid \
		ll_decode_linkea llverdev llverfs; do
		src="${BUILD_DIR}/lustre/utils/${script}"
		[[ -f "${src}" ]] && $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
			"${src}" "${REMOTE}:/usr/bin/"
	done
    fi

    if $DEPLOY_TESTS; then
	LUSTRE_DIR="$(dirname "${TESTDIR}")"
	echo "--- Syncing test framework..."
	$SSHPASS ssh $SSH_OPTS ${REMOTE} "mkdir -p ${TESTDIR}"
	$SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
		"${BUILD_DIR}/lustre/tests/" \
		"${REMOTE}:${TESTDIR}/" \
		--exclude='*.o' --exclude='*.lo' --exclude='.libs' --exclude='.deps'
	$SSHPASS ssh $SSH_OPTS ${REMOTE} "mkdir -p ${LUSTRE_DIR}/scripts"
	$SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
		"${BUILD_DIR}/lustre/scripts/" \
		"${REMOTE}:${LUSTRE_DIR}/scripts/" \
		--exclude='*.o' --exclude='*.lo' --exclude='.libs' --exclude='.deps'
    fi
fi

echo "=== Deploy complete ==="

# Update VM metadata with deploy info
_update_info_field() {
	local key="$1" val="$2"
	if grep -q "^${key}=" "${INFO}" 2>/dev/null; then
		sed -i "s|^${key}=.*|${key}=${val}|" "${INFO}"
	else
		echo "${key}=${val}" >> "${INFO}"
	fi
}
_update_info_field "LAST_DEPLOY" "$(date +%s)"
_update_info_field "BUILD_PATH" "${BUILD_DIR}"
_update_info_field "KVER" "${KVER}"

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
    # Check if OSD plugin is present -- client-only builds have no
    # osd_ldiskfs.ko and cannot run llmount.sh (no server support).
    HAS_OSD=$($SSHPASS ssh $SSH_OPTS ${REMOTE} \
        "test -f /lib/modules/${KVER}/extra/lustre/osd_ldiskfs.ko \
         && echo yes || echo no" 2>/dev/null || echo no)
    if [[ "${HAS_OSD}" != "yes" ]]; then
        echo "--- Skipping llmount.sh: client-only build (no osd_ldiskfs.ko)"
        echo "    To test, mount against a remote Lustre server."
    else
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
fi
