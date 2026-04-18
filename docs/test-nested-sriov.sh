#!/bin/bash
# test-nested-sriov.sh
#
# End-to-end feasibility test for ltvm `--nic passthrough:<BDF>` on a
# host WITHOUT real SR-IOV hardware, using nested QEMU + emulated SR-IOV.
#
# Recipe:
#   1. Boot an outer VM (q35 + guest IOMMU) with an emulated igb PF.
#   2. Inside the outer VM, spawn VFs via sriov_numvfs.
#   3. Verify VFs appear in lspci and land in distinct IOMMU groups.
#   4. Bind one VF to vfio-pci as the ltvm `--nic passthrough` prep.
#
# Requires: /dev/kvm (nested), passwordless sudo, the rocky9 ltvm
# artifacts in artifacts/ already built, and fcbr0 bridge up from
# `ltvm install`.  Uses 192.168.100.99 -- pick another free IP if it
# clashes with an active VM (check `ltvm list`).
#
# Self-contained: no ltvm modifications, no image rebuild.
# Cleans up on exit.
set -euo pipefail

REPO="${REPO:-/home/paf/lustre-test-vms-v2}"
TARGET="${TARGET:-rocky9}"
KVER="${KVER:-5.14-rhel9.7-5.14.0-611.13.1.el9_7}"
OUT="$REPO/artifacts/$TARGET/x86_64"
VMLINUZ="$OUT/kernels/$KVER/vmlinuz"
BASE_IMG="$OUT/images/$KVER/base.ext4"
QEMU=/opt/qemu/bin/qemu-system-x86_64

WORK="${WORK:-/tmp/nested-sriov}"
OUTER_IP="${OUTER_IP:-192.168.100.99}"
OUTER_NAME="${OUTER_NAME:-outer-sriov}"
OUTER_TAP="${OUTER_TAP:-tap-outer-sriov}"
OUTER_MAC="${OUTER_MAC:-52:54:00:99:00:01}"
BRIDGE="fcbr0"
ROOT_PW="initial0"          # matches ltvm_pkg/vm_state.py
NUMVFS="${NUMVFS:-4}"

cleanup() {
    set +e
    echo "[cleanup] tearing down outer VM + tap"
    if sudo test -f "$WORK/qemu.pid"; then
        sudo kill "$(sudo cat "$WORK/qemu.pid")" 2>/dev/null
    fi
    sudo pkill -f "name $OUTER_NAME" 2>/dev/null
    sudo ip link del "$OUTER_TAP" 2>/dev/null
    sudo ip neigh flush "$OUTER_IP" dev "$BRIDGE" 2>/dev/null
}
trap cleanup EXIT

mkdir -p "$WORK"
echo "[1/5] preflight"
[[ -e /dev/kvm ]]                            || { echo "no /dev/kvm"; exit 1; }
[[ "$(cat /sys/module/kvm_intel/parameters/nested 2>/dev/null)" = Y ]] \
    || { echo "nested KVM disabled"; exit 1; }
[[ -r "$VMLINUZ" ]]                          || { echo "no vmlinuz: $VMLINUZ"; exit 1; }
[[ -r "$BASE_IMG" ]]                         || { echo "no base.ext4: $BASE_IMG"; exit 1; }
"$QEMU" -device igb,help 2>&1 | grep -q '^igb options' \
    || { echo "qemu has no igb model"; exit 1; }
command -v sshpass >/dev/null                || { echo "need sshpass"; exit 1; }
ip -br link show "$BRIDGE" >/dev/null        || { echo "need bridge $BRIDGE (run ltvm install)"; exit 1; }

echo "[2/5] clone base.ext4 -> $WORK/root.ext4"
cp --reflink=auto "$BASE_IMG" "$WORK/root.ext4"

echo "[3/5] create TAP $OUTER_TAP on $BRIDGE"
sudo ip link del "$OUTER_TAP" 2>/dev/null || true
sudo ip tuntap add dev "$OUTER_TAP" mode tap
sudo ip link set "$OUTER_TAP" master "$BRIDGE"
sudo ip link set "$OUTER_TAP" up

echo "[4/5] launch outer VM (q35 + guest IOMMU + igb PF)"
# --- QEMU flags of interest ---
# -machine q35,kernel-irqchip=split   required for intel-iommu intremap
# -device intel-iommu,caching-mode=on,intremap=on
#                                     guest-visible IOMMU (enables vfio in guest)
# -device igb                         emulated 82576; upstream QEMU 8.2+ auto-
#                                     exposes SR-IOV cap with 8 VFs per PF
# -cpu host -accel kvm                nested hw accel
sudo "$QEMU" \
    -name "$OUTER_NAME" \
    -machine q35,kernel-irqchip=split \
    -cpu host -accel kvm \
    -smp 2 -m 2048 \
    -kernel "$VMLINUZ" \
    -append "console=ttyS0 reboot=k panic=1 root=/dev/vda rw \
intel_iommu=on iommu=pt net.ifnames=0 biosdevname=0 \
fc_ip=$OUTER_IP fc_gw=192.168.100.1 fc_name=$OUTER_NAME" \
    -nodefaults -no-user-config -nographic \
    -serial "file:$WORK/console.log" \
    -device intel-iommu,caching-mode=on,intremap=on \
    -device virtio-blk-pci,drive=rootfs \
    -drive "id=rootfs,file=$WORK/root.ext4,format=raw,if=none" \
    -netdev "tap,id=net0,ifname=$OUTER_TAP,script=no,downscript=no" \
    -device "virtio-net-pci,netdev=net0,mac=$OUTER_MAC" \
    -device pcie-root-port,id=rp1,chassis=1 \
    -device "igb,bus=rp1,id=igb0" \
    -daemonize -pidfile "$WORK/qemu.pid" \
    -monitor none

echo "  waiting for SSH on $OUTER_IP ..."
for i in {1..60}; do
    if sshpass -p "$ROOT_PW" ssh \
          -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
          -o LogLevel=ERROR -o ConnectTimeout=2 \
          "root@$OUTER_IP" true 2>/dev/null; then
        break
    fi
    sleep 2
done

SSH="sshpass -p $ROOT_PW ssh -o StrictHostKeyChecking=no \
-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR root@$OUTER_IP"

echo "[5/5] inside outer VM: probe igb PF, spawn $NUMVFS VFs, bind one to vfio-pci"
$SSH "bash -s" <<EOF | tee "$WORK/inner.log"
set -eu
echo '=== uname + iommu ==='
uname -r
grep -q intel_iommu=on /proc/cmdline && echo 'intel_iommu=on: OK'

echo '=== lspci (before VFs) ==='
lspci -nn | grep -iE 'igb|ethernet' || true

PF=\$(lspci -D | awk '/Ethernet.*82576|Ethernet.*Gigabit/{print \$1; exit}')
echo "PF BDF: \$PF"

echo '=== sriov sysfs ==='
ls /sys/bus/pci/devices/\$PF/ | grep -iE 'sriov|vf' || true
TOTAL=\$(cat /sys/bus/pci/devices/\$PF/sriov_totalvfs 2>/dev/null || echo 0)
echo "sriov_totalvfs=\$TOTAL"
if [ "\$TOTAL" -eq 0 ]; then
    echo 'FAIL: PF does not expose SR-IOV capability'
    exit 10
fi

echo '=== spawn VFs ==='
modprobe vfio-pci || true
echo $NUMVFS > /sys/bus/pci/devices/\$PF/sriov_numvfs
sleep 2

echo '=== lspci (after VFs) ==='
lspci -D | grep -iE 'virtual function|ethernet' || true

VFS=\$(ls /sys/bus/pci/devices/\$PF/ | grep '^virtfn' | sort)
echo "vfs: \$VFS"
VF0_BDF=\$(readlink /sys/bus/pci/devices/\$PF/virtfn0 | xargs basename)
echo "VF0 BDF: \$VF0_BDF"

echo '=== IOMMU groups ==='
for v in \$VFS; do
    bdf=\$(readlink /sys/bus/pci/devices/\$PF/\$v | xargs basename)
    grp=\$(readlink /sys/bus/pci/devices/\$bdf/iommu_group 2>/dev/null | xargs basename || echo 'NONE')
    echo "  \$v -> \$bdf -> iommu_group \$grp"
done

echo '=== bind VF0 to vfio-pci ==='
VENDOR=\$(cat /sys/bus/pci/devices/\$VF0_BDF/vendor)
DEVICE=\$(cat /sys/bus/pci/devices/\$VF0_BDF/device)
echo "\$VF0_BDF: vendor=\$VENDOR device=\$DEVICE"
FROM=\$(basename \$(readlink /sys/bus/pci/devices/\$VF0_BDF/driver 2>/dev/null) 2>/dev/null || echo none)
echo "  current driver: \$FROM"
if [ "\$FROM" != none ] && [ "\$FROM" != vfio-pci ]; then
    echo \$VF0_BDF > /sys/bus/pci/drivers/\$FROM/unbind
fi
echo "\${VENDOR#0x} \${DEVICE#0x}" > /sys/bus/pci/drivers/vfio-pci/new_id 2>/dev/null || true
echo \$VF0_BDF > /sys/bus/pci/drivers/vfio-pci/bind 2>/dev/null || true
sleep 1
NEW=\$(basename \$(readlink /sys/bus/pci/devices/\$VF0_BDF/driver 2>/dev/null) 2>/dev/null || echo none)
echo "  new driver: \$NEW"
if [ "\$NEW" = vfio-pci ]; then
    echo "SUCCESS: VF0 bound to vfio-pci; --nic passthrough:\$VF0_BDF would work here"
else
    echo "FAIL: could not bind VF0 to vfio-pci"
    exit 11
fi
EOF

echo
echo "===== VERDICT ====="
if grep -q '^SUCCESS: VF0 bound to vfio-pci' "$WORK/inner.log"; then
    echo "PASS: nested SR-IOV works on this host."
    echo "  Artifacts: $WORK/{console.log,inner.log}"
    exit 0
else
    echo "FAIL: see $WORK/inner.log"
    exit 1
fi
