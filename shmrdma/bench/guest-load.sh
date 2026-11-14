#!/bin/bash
# Load shmrdma in a guest: take the "shmrdma"-tagged virtio-fs device
# away from virtiofs if it grabbed it, then insmod.
# usage: guest-load.sh /path/to/shmrdma.ko [netdev]
set -eu
ko=$1
nd=${2:-eth0}
modprobe ib_core
modprobe rdma_cm
modprobe ib_cm
for d in /sys/bus/virtio/devices/virtio*; do
	[[ $(cat $d/device) == 0x001a ]] || continue
	drv=$(readlink $d/driver 2>/dev/null || true)
	if [[ ${drv##*/} == virtiofs ]]; then
		tag=$(cat /sys/fs/virtiofs/*/tag 2>/dev/null | head -1 || true)
		echo "${d##*/}" > /sys/bus/virtio/drivers/virtiofs/unbind
	fi
done
lsmod | grep -q '^shmrdma ' && rmmod shmrdma
insmod "$ko" netdev="$nd"
sleep 1
rdma link show 2>/dev/null || true
