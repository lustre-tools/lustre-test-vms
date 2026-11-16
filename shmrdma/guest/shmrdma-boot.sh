#!/bin/bash
# Boot setup of a VM with an ltvm shmrdma device (fc_shmrdma=1, run by
# rc.local): the device itself, then SMC sockets over it.  SMC-R runs
# over the device as over any RoCE HCA, but SMC v2 off s390 connects
# two peers only when they share a user EID: without one every
# connection falls back to TCP (the server declines with NOUEID).  A
# kernel without SMC skips it.
set -u
EID=LTVM-SHMRDMA

# A virtiofs built into the kernel (Debian's) binds the device before
# shmrdma.ko can load, which the modprobe.d softdep cannot order: hand
# it over.
for tag in /sys/fs/virtiofs/*/tag; do
	[[ -r $tag && $(< "$tag") == shmrdma ]] || continue
	dev=$(basename "$(readlink -f "${tag%/tag}/device")")
	[[ -e /sys/bus/virtio/drivers/virtiofs/$dev ]] || continue
	modprobe shmrdma || exit 1
	echo "$dev" > /sys/bus/virtio/drivers/virtiofs/unbind || exit 1
	echo "$dev" > /sys/bus/virtio/drivers/shmrdma/bind || exit 1
done

[[ -d /sys/module/smc ]] || modinfo smc &> /dev/null || exit 0
modprobe smc || exit 1
modprobe smc_diag 2> /dev/null
if command -v smcd > /dev/null; then
	smcd ueid add "$EID" || exit 1
else
	python3 /usr/local/sbin/smc_ueid.py "$EID" || exit 1
fi
# Each socket buffer is registered as one memory region, which the
# device takes up to 4096 pages (16 MB).  1 MB measured best for LNet
# bulk; 4 MB was slower.
sysctl -qw net.smc.wmem=1048576 net.smc.rmem=1048576
