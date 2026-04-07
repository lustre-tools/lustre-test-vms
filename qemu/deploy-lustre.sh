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
    VM_OS_ID=$($SSHPASS ssh $SSH_OPTS ${REMOTE} '. /etc/os-release; echo $ID')
    [[ -z "${VM_OS_ID}" ]] && { echo "ERROR: could not detect VM OS"; exit 1; }
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
VM_OS_ID=$($SSHPASS ssh $SSH_OPTS ${REMOTE} '. /etc/os-release; echo $ID')
[[ -z "${VM_OS_ID}" ]] && { echo "ERROR: could not detect VM OS"; exit 1; }
VM_OS_VER=$($SSHPASS ssh $SSH_OPTS ${REMOTE} '. /etc/os-release; echo ${VERSION_ID%%.*}')
[[ -z "${VM_OS_VER}" ]] && VM_OS_VER="unknown"

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
    find "${BUILD_DIR}" -name '*.ko' -not -path '*/kconftest*' -not -path '*/.staging/*' -exec cp {} "${KO_TMPDIR}/" \;
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
        # Try: script's own repo dir, then SUDO_USER's home
        for _d in \
            "$(dirname "$(dirname "$(readlink -f "$0")")")" \
            "${HOME}/lustre-test-vms-v2" \
            "$(getent passwd "${SUDO_USER:-}" 2>/dev/null | cut -d: -f6)/lustre-test-vms-v2"; do
            [[ -n "${_d}" ]] && [[ -d "${_d}/output" ]] && { _ltvm_dir="${_d}"; break; }
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
# Require staging tree from `make install DESTDIR=.staging`.
# ltvm build-lustre creates this automatically.
STAGING="${BUILD_DIR}/.staging"

if [[ ! -d "${STAGING}/usr" ]]; then
    echo "ERROR: No staging tree at ${STAGING}/usr"
    echo "  Run: ltvm build-lustre <target> --lustre-tree ${BUILD_DIR}"
    echo "  Or:  make install DESTDIR=${BUILD_DIR}/.staging"
    exit 1
fi

if $DEPLOY_USERSPACE || $DEPLOY_TESTS; then
    echo "--- Syncing from staging tree..."
    # Rsync only /usr/ -- not /, which would clobber FHS symlinks
    # (/lib -> /usr/lib, /sbin -> /usr/sbin) on EL systems.
    $SSHPASS rsync -az --force -e "ssh ${SSH_OPTS}" \
	    "${STAGING}/usr/" "${REMOTE}:/usr/" \
	    --exclude='*.la' --exclude='*.a' \
	    --exclude='include/' --exclude='share/man/' \
	    --exclude='lib/modules/'
    # mount.lustre goes to /sbin on the staging tree; copy to /usr/sbin
    # (which is /sbin via symlink on EL).
    if [[ -f "${STAGING}/sbin/mount.lustre" ]]; then
	    $SSHPASS rsync -az -e "ssh ${SSH_OPTS}" \
		    "${STAGING}/sbin/mount.lustre" "${REMOTE}:/usr/sbin/"
    fi
    # mount.lustre_tgt symlink (server mount helper, checks argv[0])
    $SSHPASS ssh $SSH_OPTS ${REMOTE} \
	    "ln -sf /usr/sbin/mount.lustre /usr/sbin/mount.lustre_tgt 2>/dev/null || true"
    $SSHPASS ssh $SSH_OPTS ${REMOTE} "ldconfig"
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
