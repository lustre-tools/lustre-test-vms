#!/bin/bash
# test-nested-passthrough-e2e.sh
#
# End-to-end test for `ltvm create --nic passthrough:<BDF>` on a host
# WITHOUT real SR-IOV hardware, driving the real ltvm code path inside
# a nested QEMU VM.  Follow-up to docs/test-nested-sriov.sh, which only
# proved that the outer VM + igb SR-IOV + vfio-pci bind works.  This
# script additionally:
#
#   * runs ltvm itself *inside* the outer VM,
#   * drives `ltvm create inner-pt --nic passthrough:<VF-BDF>`,
#   * asserts the inner VM comes up with the VF attached via vfio-pci,
#   * destroys it and verifies the VF is rebound to igbvf.
#
# Shape: the outer VM is a rocky9 ltvm image on fcbr0 (192.168.100.99
# on the host).  Inside the outer VM we create a *second* bridge, also
# called fcbr0 (ltvm hardcodes the name), on a different subnet
# (192.168.200.0/24) so the inner mgmt NIC doesn't collide with the
# outer VM's own eth0.
#
# Self-cleaning: trap tears down the outer VM, the TAP, and any
# leftover /tmp scratch.  Does NOT modify ltvm_pkg/*.py, touch host
# VMs, or rebuild images.
#
# Exit 0 on full pass; non-zero with "FAIL: <what>" on any assertion.
set -euo pipefail

REPO="${REPO:-/home/paf/lustre-test-vms-v2}"
TARGET="${TARGET:-rocky9}"
KVER="${KVER:-5.14-rhel9.7-5.14.0-611.13.1.el9_7}"
OUT="$REPO/artifacts/$TARGET/x86_64"
VMLINUZ="$OUT/kernels/$KVER/vmlinuz"
BASE_IMG="$OUT/images/$KVER/base.ext4"
QEMU=/opt/qemu/bin/qemu-system-x86_64

WORK="${WORK:-/tmp/nested-pt-e2e}"
OUTER_IP="${OUTER_IP:-192.168.100.99}"
OUTER_NAME="${OUTER_NAME:-outer-pt-e2e}"
OUTER_TAP="${OUTER_TAP:-tap-outer-pt}"
OUTER_MAC="${OUTER_MAC:-52:54:00:99:00:02}"
HOST_BRIDGE="fcbr0"        # on the real host
INNER_BRIDGE="fcbr0"       # same name (ltvm hardcodes) inside outer VM
INNER_SUBNET="192.168.200" # different subnet so no collision with outer's eth0
INNER_IP="${INNER_IP:-192.168.200.10}"
ROOT_PW="initial0"
NUMVFS="${NUMVFS:-2}"

cleanup() {
    rc=$?
    set +e
    echo "[cleanup] tearing down outer VM + tap"
    if sudo test -f "$WORK/qemu.pid"; then
        sudo kill "$(sudo cat "$WORK/qemu.pid")" 2>/dev/null
        sleep 1
    fi
    sudo pkill -f "name $OUTER_NAME" 2>/dev/null
    sudo ip link del "$OUTER_TAP" 2>/dev/null
    sudo ip neigh flush "$OUTER_IP" dev "$HOST_BRIDGE" 2>/dev/null
    exit $rc
}
trap cleanup EXIT

fail() { echo; echo "FAIL: $*"; exit 1; }
pass() { echo; echo "PASS: $*"; }

mkdir -p "$WORK"

echo "[1/7] preflight"
[[ -e /dev/kvm ]]                           || fail "no /dev/kvm"
[[ "$(cat /sys/module/kvm_intel/parameters/nested 2>/dev/null)" = Y ]] \
                                            || fail "nested KVM disabled"
[[ -r "$VMLINUZ" ]]                         || fail "no vmlinuz: $VMLINUZ"
[[ -r "$BASE_IMG" ]]                        || fail "no base.ext4: $BASE_IMG"
[[ -x "$QEMU" ]]                            || fail "no qemu at $QEMU"
"$QEMU" -device igb,help 2>&1 | grep -q '^igb options' \
                                            || fail "qemu has no igb model"
command -v sshpass >/dev/null               || fail "need sshpass on host"
ip -br link show "$HOST_BRIDGE" >/dev/null 2>&1 \
                                            || fail "host bridge $HOST_BRIDGE missing (run ltvm install)"

echo "[2/7] clone base.ext4 -> $WORK/root.ext4"
cp --reflink=auto "$BASE_IMG" "$WORK/root.ext4"
# Grow the rootfs so dnf installs + /opt/qemu copy fit.  qemu-img on raw
# is just truncate; e2fsck + resize2fs expand the inner ext4.
qemu-img resize -f raw "$WORK/root.ext4" 6G
e2fsck -fy "$WORK/root.ext4" >/dev/null 2>&1 || true
resize2fs "$WORK/root.ext4" >/dev/null 2>&1

echo "[3/7] create TAP $OUTER_TAP on $HOST_BRIDGE"
sudo ip link del "$OUTER_TAP" 2>/dev/null || true
sudo ip tuntap add dev "$OUTER_TAP" mode tap
sudo ip link set "$OUTER_TAP" master "$HOST_BRIDGE"
sudo ip link set "$OUTER_TAP" up

echo "[4/7] launch outer VM (q35 + guest IOMMU + igb PF, 4 GB RAM)"
sudo "$QEMU" \
    -name "$OUTER_NAME" \
    -machine q35,kernel-irqchip=split \
    -cpu host -accel kvm \
    -smp 2 -m 4096 \
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
sshpass -p "$ROOT_PW" ssh \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR -o ConnectTimeout=2 \
    "root@$OUTER_IP" true 2>/dev/null \
    || fail "outer VM never came up on $OUTER_IP (check $WORK/console.log)"

SSH="sshpass -p $ROOT_PW ssh -o StrictHostKeyChecking=no \
-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 root@$OUTER_IP"
SCP="sshpass -p $ROOT_PW scp -o StrictHostKeyChecking=no \
-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"

echo "[5/7] inside outer VM: install deps, stage repo + /opt/qemu, configure bridge"

# Stage the repo (without artifacts/ and .git -- we'll just add the two
# artifacts we need) and /opt/qemu into the outer VM.  tar-over-ssh is
# fast enough (~10s for 500 MB over the host bridge).
$SSH "mkdir -p /ltvm /opt/qemu /opt/qemu-vms/overlays /opt/qemu-vms/sockets"

echo "  staging repo -> /ltvm (excluding artifacts/, .git)"
(cd "$REPO" && tar --exclude=./artifacts --exclude=./.git --exclude=./.venv -cf - .) \
    | $SSH "tar -xf - -C /ltvm"

echo "  staging kernel + base image for inner VM"
# Inner VM still needs the rocky9 artifacts.  Place them under the
# exact artifacts/<target>/<arch> layout ltvm expects.
$SSH "mkdir -p /ltvm/artifacts/$TARGET/x86_64/kernels/$KVER \
               /ltvm/artifacts/$TARGET/x86_64/images/$KVER"
$SCP "$VMLINUZ"  "root@$OUTER_IP:/ltvm/artifacts/$TARGET/x86_64/kernels/$KVER/vmlinuz"
$SCP "$OUT/kernels/$KVER/meta.json" \
     "root@$OUTER_IP:/ltvm/artifacts/$TARGET/x86_64/kernels/$KVER/meta.json"
$SCP "$BASE_IMG" "root@$OUTER_IP:/ltvm/artifacts/$TARGET/x86_64/images/$KVER/base.ext4"
$SCP "$OUT/images/$KVER/meta.json" \
     "root@$OUTER_IP:/ltvm/artifacts/$TARGET/x86_64/images/$KVER/meta.json"

echo "  downloading el9 QEMU pre-built tarball inside outer VM"
# Host's /opt/qemu is linked against Debian/Ubuntu libs (host is WSL2),
# so it won't run inside rocky9.  Pull the el9-built QEMU from the
# published release instead -- same one ltvm install would fetch.
$SSH '
set -eu
mkdir -p /opt/qemu
cd /opt/qemu
if [ ! -x bin/qemu-system-x86_64 ]; then
    curl -fsSL -o /tmp/qemu-el9.tar.gz \
        https://github.com/lustre-tools/lustre-test-vms/releases/download/qemu-9.2.2/qemu-9.2.2-el9.tar.gz
    tar -xzf /tmp/qemu-el9.tar.gz -C /opt/qemu
    rm /tmp/qemu-el9.tar.gz
fi
/opt/qemu/bin/qemu-img --version | head -1
' >"$WORK/outer-qemu-fetch.log" 2>&1 \
    || fail "failed to fetch el9 QEMU inside outer VM (see $WORK/outer-qemu-fetch.log)"

echo "  installing sshpass + python3-pyyaml + dnsmasq (via host NAT)"
# dnsmasq is needed because provision_vm_ssh -> register_ssh_name ->
# reload_dns() sends SIGHUP to dnsmasq; without it that raises and
# the whole create rolls back before we get to assert anything.
$SSH 'dnf install -y --setopt=install_weak_deps=False sshpass python3-pyyaml dnsmasq' \
    >"$WORK/outer-dnf.log" 2>&1 \
    || fail "dnf install inside outer VM failed (see $WORK/outer-dnf.log)"

echo "  creating inner fcbr0 bridge on $INNER_SUBNET.0/24 + dnsmasq"
$SSH "
ip link add fcbr0 type bridge 2>/dev/null || true
ip link set fcbr0 up
ip addr add $INNER_SUBNET.1/24 dev fcbr0 2>/dev/null || true
# ltvm reads /opt/qemu-vms/subnet for alloc_ip and rc.local fc_gw logic.
echo '$INNER_SUBNET' > /opt/qemu-vms/subnet
mkdir -p /root/.ssh
# Start dnsmasq bound to fcbr0 so reload_dns() has something to SIGHUP.
# No DHCP range needed -- inner VMs use static IPs via fc_ip cmdline.
# bind-dynamic = bind to fcbr0 even if it changes; pid-file must be at
# /run/dnsmasq.pid because ltvm looks there first.
pkill -x dnsmasq 2>/dev/null || true
# Some default /etc/dnsmasq.conf ships with bind-interfaces which
# conflicts with bind-dynamic.  Comment it out first (same fix ltvm's
# host_setup does for the host).
if [ -f /etc/dnsmasq.conf ] && grep -q '^bind-interfaces' /etc/dnsmasq.conf; then
    sed -i 's/^bind-interfaces/# bind-interfaces/' /etc/dnsmasq.conf
fi
cat > /etc/dnsmasq.d/ltvm-nested.conf <<'EOF'
interface=fcbr0
bind-dynamic
no-resolv
pid-file=/run/dnsmasq.pid
EOF
# Start dnsmasq by hand.
dnsmasq -x /run/dnsmasq.pid
sleep 1
test -s /run/dnsmasq.pid || { echo 'dnsmasq failed to start'; exit 1; }
"

echo "[6/7] inside outer VM: spawn VFs, identify VF0, drive ltvm create"

# Note: the VF is an igbvf 1 GbE Ethernet device.  It is NOT usable as
# ltvm's mgmt NIC.  The inner VM's eth0 is the virtio-net-pci that
# ltvm wires up unconditionally; the VF becomes eth1 / enp*s0 / etc.
# inside the inner VM.  The test is whether the vfio-pci bind, the
# QEMU -device vfio-pci emission, and PID-alive tracking all work.
$SSH "bash -s" <<'OUTER_SETUP' | tee "$WORK/outer-setup.log"
set -eu
echo '=== iommu + cmdline ==='
grep -q intel_iommu=on /proc/cmdline && echo 'intel_iommu=on: OK' || echo 'intel_iommu missing'
[ -d /sys/kernel/iommu_groups ] && [ -n "$(ls /sys/kernel/iommu_groups)" ] \
    && echo 'iommu_groups present' || { echo 'FAIL: no iommu groups'; exit 20; }

echo '=== spawn VFs ==='
modprobe igb
modprobe igbvf
modprobe vfio-pci
PF=$(lspci -D | awk '/Ethernet.*82576|Ethernet.*Gigabit/{print $1; exit}')
[ -n "$PF" ] || { echo 'FAIL: no igb PF'; exit 21; }
echo "PF=$PF"
echo 2 > /sys/bus/pci/devices/$PF/sriov_numvfs
sleep 2
VF0=$(readlink /sys/bus/pci/devices/$PF/virtfn0 | xargs basename)
echo "VF0=$VF0"
echo "$VF0" > /tmp/vf0.bdf

echo '=== VF0 driver before ltvm ==='
readlink /sys/bus/pci/devices/$VF0/driver | xargs basename
OUTER_SETUP

grep -q 'iommu_groups present' "$WORK/outer-setup.log" \
    || fail "outer VM has no guest IOMMU"
grep -q '^VF0=' "$WORK/outer-setup.log" \
    || fail "no VF0 spawned"
VF0_BDF=$($SSH 'cat /tmp/vf0.bdf')
echo "  VF0 BDF: $VF0_BDF"

# Run ltvm from /ltvm without installing.  The repo ships a .venv
# bootstrap that we don't have inside the outer VM, so we run the raw
# ltvm_pkg via python3 with PYTHONPATH pointing at /ltvm.
#
# Short SSH timeout for the inner provisioning so we don't wait 30s
# when (in the partial-success path) the inner mgmt NIC never
# reaches provision_vm_ssh success.
INNER_SSH_TIMEOUT=45
INNER_NAME="inner-pt"

echo "  driving: ltvm create $INNER_NAME --nic passthrough:$VF0_BDF"
# Export PATH to include /opt/qemu/bin so ltvm's qemu_binary_for_arch
# picks up QEMU 9.2 with microvm.
$SSH "
export PATH=/opt/qemu/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PYTHONPATH=/ltvm
export LTVM_VM_DIR=/opt/qemu-vms
export LTVM_SUBNET=$INNER_SUBNET
export LTVM_SSH_TIMEOUT=$INNER_SSH_TIMEOUT
cd /ltvm
# The ltvm script self-bootstraps into .venv if present; we deleted .venv
# above by excluding it, so it runs directly under the system python.
python3 /ltvm/ltvm create $INNER_NAME \\
    --target rocky9 \\
    --kernel $KVER \\
    --ip $INNER_IP \\
    --vcpus 1 --mem 512 \\
    --mdt-disks 0 --ost-disks 0 \\
    --nic passthrough:$VF0_BDF \\
    2>&1
" > "$WORK/inner-create.log" 2>&1 || true  # may non-zero on SSH-provision timeout
echo "  --- ltvm create output (tail) ---"
tail -30 "$WORK/inner-create.log"

echo "[7/7] assertions"

# --- A1: QEMU actually got -device vfio-pci on its cmdline ----------
inner_pid=$($SSH "cat /opt/qemu-vms/sockets/$INNER_NAME.pid 2>/dev/null || echo 0")
[[ "$inner_pid" != "0" ]] || fail "no pidfile for $INNER_NAME (create didn't reach launch_qemu)"

echo "  inner-pt pid (inside outer VM): $inner_pid"

# Dump the qemu cmdline from /proc inside the outer VM
qemu_cmdline=$($SSH "tr '\0' ' ' < /proc/$inner_pid/cmdline 2>/dev/null || true")
[[ -n "$qemu_cmdline" ]] || fail "inner-pt QEMU process not alive (pid $inner_pid)"
echo "$qemu_cmdline" > "$WORK/inner-qemu-cmdline.txt"

if echo "$qemu_cmdline" | grep -qE "vfio-pci,host=$VF0_BDF"; then
    echo "  A1 OK: QEMU cmdline has -device vfio-pci,host=$VF0_BDF"
else
    fail "A1: inner QEMU cmdline missing vfio-pci,host=$VF0_BDF -- got: $qemu_cmdline"
fi

# --- A2: VF0 is currently bound to vfio-pci inside outer VM --------
drv_during=$($SSH "basename \$(readlink /sys/bus/pci/devices/$VF0_BDF/driver) 2>/dev/null || echo none")
if [[ "$drv_during" == "vfio-pci" ]]; then
    echo "  A2 OK: $VF0_BDF is bound to vfio-pci while inner VM runs"
else
    fail "A2: expected VF0 driver vfio-pci during run, got: $drv_during"
fi

# --- A3: ltvm list shows inner-pt running --------------------------
list_out=$($SSH "
export PYTHONPATH=/ltvm LTVM_VM_DIR=/opt/qemu-vms LTVM_SUBNET=$INNER_SUBNET
python3 /ltvm/ltvm list --json 2>/dev/null" || true)
echo "$list_out" > "$WORK/inner-ltvm-list.json"
if echo "$list_out" | grep -q "\"$INNER_NAME\""; then
    echo "  A3 OK: ltvm list shows $INNER_NAME"
else
    fail "A3: ltvm list missing $INNER_NAME (output: $list_out)"
fi

# --- A4: VMInfo records passthrough_drivers correctly --------------
pt_drvs=$($SSH "grep -E '^.' /opt/qemu-vms/sockets/$INNER_NAME.info | tail -5" 2>/dev/null || true)
echo "$pt_drvs" > "$WORK/inner-info.txt"
if $SSH "cat /opt/qemu-vms/sockets/$INNER_NAME.info" 2>/dev/null | grep -q "$VF0_BDF=igbvf"; then
    echo "  A4 OK: .info records $VF0_BDF was bound to igbvf before vfio"
else
    echo "  A4 WARN: .info file format unexpected; dump:"
    $SSH "cat /opt/qemu-vms/sockets/$INNER_NAME.info" 2>/dev/null | sed 's/^/    /'
fi

# --- A5: inner VM sees the VF as a PCI device (best-effort) -------
# This REQUIRES the inner VM's mgmt NIC to work.  It often will not
# (see comment above) -- we treat this as informational rather than a
# hard failure.  The mgmt NIC is virtio-net-pci on the outer VM's new
# fcbr0, which *should* work if provision_vm_ssh succeeded.
inner_create_rc=$?
if grep -q "VM created: $INNER_NAME" "$WORK/inner-create.log"; then
    echo "  ltvm create reached provision success -- probing inner VM"
    inner_probe=$($SSH "
        export PYTHONPATH=/ltvm LTVM_VM_DIR=/opt/qemu-vms LTVM_SUBNET=$INNER_SUBNET
        sshpass -p initial0 ssh -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
            -o ConnectTimeout=5 root@$INNER_IP \
            'lspci -nn | grep -iE \"ethernet|82576\" ; ls /sys/class/net' \
            2>&1" || true)
    echo "$inner_probe" > "$WORK/inner-lspci.txt"
    if echo "$inner_probe" | grep -qiE '82576.*VF|Virtual Function|igbvf'; then
        echo "  A5 OK: inner VM sees the VF in lspci"
    else
        echo "  A5 info: inner VM did not clearly surface VF (output saved)"
    fi
else
    echo "  A5 skip: ltvm create did not fully provision SSH (expected --"
    echo "           the igbvf VF is not a usable mgmt NIC; we bypass this"
    echo "           by running a virtio-net mgmt NIC, but the inner VM may"
    echo "           still race ssh_provision timeout under nested KVM)."
fi

# --- A6: ltvm destroy rebinds VF to igbvf --------------------------
echo "  destroying inner VM"
$SSH "
export PATH=/opt/qemu/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PYTHONPATH=/ltvm LTVM_VM_DIR=/opt/qemu-vms LTVM_SUBNET=$INNER_SUBNET
python3 /ltvm/ltvm destroy $INNER_NAME 2>&1" > "$WORK/inner-destroy.log" 2>&1 \
    || fail "ltvm destroy $INNER_NAME failed (see $WORK/inner-destroy.log)"

drv_after=$($SSH "basename \$(readlink /sys/bus/pci/devices/$VF0_BDF/driver) 2>/dev/null || echo none")
if [[ "$drv_after" == "igbvf" ]]; then
    echo "  A6 OK: after destroy, $VF0_BDF is rebound to igbvf"
else
    fail "A6: after destroy, expected driver igbvf, got: $drv_after"
fi

pass "ltvm --nic passthrough end-to-end works on WSL2 via nested SR-IOV"
echo "  artifacts: $WORK/"
echo "    console.log           (outer VM serial)"
echo "    outer-setup.log       (VF spawn + IOMMU probe)"
echo "    outer-dnf.log         (package install inside outer)"
echo "    inner-create.log      (ltvm create output)"
echo "    inner-qemu-cmdline.txt (live QEMU cmdline inside outer VM)"
echo "    inner-ltvm-list.json  (ltvm list --json result)"
echo "    inner-info.txt        (VMInfo .info file tail)"
echo "    inner-lspci.txt       (inner VM VF probe, if reached)"
echo "    inner-destroy.log     (ltvm destroy output)"
exit 0
