#!/bin/sh
# Per-NIC SoftRoCE setup.
#
# Called by rc.local (once the multi-NIC foundation lands) for each
# interface tagged as softroce:
#
#     /usr/local/sbin/setup-nic-softroce.sh <ifname>
#
# Creates an rxe rdma link on top of the given netdev and tunes the
# netdev for sane SoftRoCE performance.
#
# Link-name convention: rxe<N>, where N counts softroce interfaces
# from 0 in invocation order. This matches what setup-lnet-config.sh
# (the LNet emitter) expects -- it counts rxe entries within softroce
# transport blocks only, starting at 0. If this script is run twice
# for the same ifname the second call is a no-op (the existing rxe
# link is detected and preserved).
#
# Assumes rdma_rxe kernel module, rdma-core userspace (rdma, ibv_*),
# ip, and ethtool are all present -- image plumbing is handled
# elsewhere.

set -eu

PROG=$(basename "$0")

log() {
	# Log to stderr and, if available, to journald via logger.
	msg="$PROG: $*"
	printf '%s\n' "$msg" >&2
	if command -v logger >/dev/null 2>&1; then
		logger -t "$PROG" -- "$*" || true
	fi
}

die() {
	log "ERROR: $*"
	exit 1
}

if [ "$#" -ne 1 ]; then
	die "usage: $PROG <ifname>"
fi

IFNAME=$1

# Refuse cleanly if the interface doesn't exist.
if ! ip link show dev "$IFNAME" >/dev/null 2>&1; then
	die "interface $IFNAME does not exist"
fi

# 1. Load rdma_rxe (idempotent -- modprobe is a no-op if loaded).
modprobe rdma_rxe || die "modprobe rdma_rxe failed"

# 2. Idempotency check: if any rdma link already sits on this netdev,
# nothing to do. `rdma -j link show` is the most robust parser, but we
# avoid pulling in jq; parse the plain text output instead.
#
# Example line:
#   link rxe0/1 state ACTIVE physical_state LINK_UP netdev eth1
existing=$(rdma link show 2>/dev/null | \
	awk -v n="$IFNAME" '
		/^link / {
			for (i = 1; i <= NF; i++) {
				if ($i == "netdev" && $(i+1) == n) {
					# $2 is like "rxe0/1" -- strip /port
					sub("/.*", "", $2)
					print $2
					exit
				}
			}
		}
	')

if [ -n "$existing" ]; then
	log "rdma link $existing already present on $IFNAME; no-op"
	exit 0
fi

# 3. Compute next rxe<N>: count existing rxe* links. This matches the
# LNet emitter convention (counts softroce entries from 0). Since
# SoftRoCE is the only producer of rxe* links in this image, counting
# all of them is equivalent.
count=$(rdma link show 2>/dev/null | \
	awk '/^link rxe[0-9]+/ { n++ } END { print n+0 }')
linkname="rxe${count}"

# Create the rxe link. rdma-core accepts either `type rxe` or
# `type rxe netdev <ifname>`.
rdma link add "$linkname" type rxe netdev "$IFNAME" \
	|| die "rdma link add $linkname on $IFNAME failed"

# 4. Tune the underlying netdev.
# MTU >1500 avoids RoCEv2 fragmentation stalls. 4200 leaves headroom
# under a 4500-ish typical virtio ceiling and matches common RoCE
# lab settings.
ip link set dev "$IFNAME" mtu 4200 \
	|| log "warning: failed to set mtu 4200 on $IFNAME"

# Checksum / segmentation offloads are emulated for SoftRoCE and only
# add latency; turn them off. Each -K flag is best-effort (some
# drivers refuse individual knobs, which is fine).
for feat in tx rx tso gso gro; do
	ethtool -K "$IFNAME" "$feat" off >/dev/null 2>&1 || true
done

# 5. Summary.
log "configured $linkname on $IFNAME (mtu 4200, offloads off)"
