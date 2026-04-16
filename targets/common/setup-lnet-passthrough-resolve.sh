#!/bin/sh
# Resolve '@ib-of-eth<N>)' placeholders in /etc/modprobe.d/lnet.conf
# to real ibdev names once the PCI bus has enumerated.
#
# setup-lnet-config.sh (the pure emitter) writes a placeholder for
# each 'passthrough' NIC because at emit time it doesn't know what
# the vfio'd device will enumerate as inside the guest.  Typical IB
# VFs show up under /sys/class/infiniband/mlx5_N with no matching
# ethN, so the final name has to be resolved at runtime.
#
# Strategy:
#   1. Enumerate /sys/class/infiniband/ to list all ibdevs.
#   2. Subtract rxe* entries -- those are SoftRoCE links created
#      by setup-nic-softroce.sh and already named correctly in
#      lnet.conf.
#   3. Match the remainder positionally to the passthrough
#      entries in FC_NICS (Nth passthrough -> Nth non-rxe ibdev).
#   4. Rewrite lnet.conf in place, replacing each
#      '@ib-of-eth<N>)' with the resolved ibdev name.
#
# Guards:
#   - Only rewrite if the '@ib-of-eth' marker is present (proof
#     that our emitter produced the file -- operator-authored
#     content stays untouched).
#   - If the count of non-rxe ibdevs doesn't match the count of
#     passthrough entries in FC_NICS, log a warning and leave the
#     placeholder in place.  Visible breakage beats silently
#     wiring up the wrong device.
#
# Usage:
#     setup-lnet-passthrough-resolve.sh [<lnet.conf>]
# Defaults to /etc/modprobe.d/lnet.conf.
#
# Env:
#     FC_NICS            fc_nics= value (comma-separated NIC specs,
#                        extras only -- matches what rc.local
#                        exports).  If unset, falls back to
#                        /proc/cmdline.
#     SYSFS_IB_ROOT      override for /sys/class/infiniband (tests).

set -eu

PROG=$(basename "$0")

log() {
	msg="$PROG: $*"
	printf '%s\n' "$msg" >&2
	if command -v logger >/dev/null 2>&1; then
		logger -t "$PROG" -- "$*" || true
	fi
}

LNET_CONF=${1:-/etc/modprobe.d/lnet.conf}
SYSFS_IB_ROOT=${SYSFS_IB_ROOT:-/sys/class/infiniband}

# Pull fc_nics from env if set, else /proc/cmdline.  We parse the
# same way rc.local does, so behavior matches.
if [ -z "${FC_NICS+x}" ]; then
	if [ -r /proc/cmdline ]; then
		FC_NICS=$(tr ' ' '\n' < /proc/cmdline | \
			awk -F= '/^fc_nics=/ {sub(/^fc_nics=/, ""); \
				print; exit}')
	else
		FC_NICS=""
	fi
fi

# Count passthrough entries in FC_NICS.  Entries may be of the
# form TYPE or TYPE;ARG -- strip everything after the first ';'
# before comparing.
pt_count=0
if [ -n "${FC_NICS}" ]; then
	# POSIX: IFS-split on comma.
	oldifs=$IFS
	IFS=,
	# shellcheck disable=SC2086
	set -- ${FC_NICS}
	IFS=$oldifs
	for entry in "$@"; do
		nic_type=${entry%%;*}
		if [ "$nic_type" = "passthrough" ]; then
			pt_count=$((pt_count + 1))
		fi
	done
fi

if [ "$pt_count" -eq 0 ]; then
	# Nothing to resolve.  Don't log noisily -- this is the common
	# case for tcp-only and softroce-only VMs.
	exit 0
fi

if [ ! -s "$LNET_CONF" ]; then
	log "no $LNET_CONF (or empty); nothing to resolve"
	exit 0
fi

# Only touch the file if our emitter produced it (marker present).
if ! grep -q '@ib-of-eth' "$LNET_CONF"; then
	log "$LNET_CONF has no @ib-of-eth marker; leaving untouched"
	exit 0
fi

# Enumerate ibdevs (sorted) and subtract rxe* (softroce entries
# already named correctly by the emitter).
if [ ! -d "$SYSFS_IB_ROOT" ]; then
	log "WARNING: $SYSFS_IB_ROOT does not exist; leaving $pt_count" \
		"placeholder(s) in $LNET_CONF"
	exit 0
fi

# Collect non-rxe ibdev names into a newline-separated list.
# Using ls is ugly but portable; /sys entries are safe (no spaces).
# We sort for a stable ordering.
non_rxe_list=""
# shellcheck disable=SC2012
for dev in $(ls "$SYSFS_IB_ROOT" 2>/dev/null | sort); do
	case "$dev" in
	rxe*)
		continue
		;;
	*)
		if [ -z "$non_rxe_list" ]; then
			non_rxe_list=$dev
		else
			non_rxe_list="${non_rxe_list}
${dev}"
		fi
		;;
	esac
done

non_rxe_count=0
if [ -n "$non_rxe_list" ]; then
	non_rxe_count=$(printf '%s\n' "$non_rxe_list" | wc -l)
fi

if [ "$non_rxe_count" -ne "$pt_count" ]; then
	log "WARNING: found $non_rxe_count non-rxe ibdev(s) under" \
		"$SYSFS_IB_ROOT but FC_NICS declares $pt_count" \
		"passthrough entry/entries; leaving placeholder(s)" \
		"in $LNET_CONF for visibility"
	exit 0
fi

# We have a match.  Rewrite the file in place.  The emitter produces
# placeholders in the form:
#     o2ibK(@ib-of-ethN))
# where N is the eth index.  We don't care about N here -- we just
# replace each placeholder in document order with the next ibdev
# from $non_rxe_list.  The emitter guarantees placeholder order
# matches passthrough order in FC_NICS, which matches the positional
# assignment we're doing.
#
# Use awk so we can walk occurrences in order (sed's s///g would
# substitute all with the same replacement).
tmp=$(mktemp "${LNET_CONF}.XXXXXX")
# Clean up on any exit path.
trap 'rm -f "$tmp"' EXIT

# Feed the ibdev list as a newline record, then the file contents.
# Awk splits the first record into an array and walks placeholders
# in each subsequent line.
printf '%s\n' "$non_rxe_list" | \
	awk -v conf="$LNET_CONF" '
		NR == FNR {
			ibs[NR] = $0
			n = NR
			next
		}
		{
			line = $0
			out = ""
			i = 1
			while ((p = index(line, "@ib-of-eth")) > 0) {
				# Find the closing ")" after the marker.
				rest = substr(line, p)
				q = index(rest, ")")
				if (q == 0) {
					# Malformed -- bail, leave line as-is.
					out = out line
					line = ""
					break
				}
				# Emit prefix up to just before "@".
				out = out substr(line, 1, p - 1)
				# Replace the "@ib-of-eth...)"  (length q in
				# rest) with the next ibdev name.  Note the
				# placeholder ends with ")" but the emitter
				# also wrote the *real* closing ")" after it,
				# producing "...))".  We consume only the
				# inner "@...)" here; the outer ")" is kept.
				if (i > n) {
					# Shouldnt happen given the pre-check,
					# but guard anyway.
					out = out substr(rest, 1, q)
				} else {
					out = out ibs[i]
					i++
				}
				line = substr(rest, q + 1)
			}
			print out line
		}
	' - "$LNET_CONF" > "$tmp"

# Preserve perms from the original file; chmod --reference is GNU
# but present on all our targets.  Fall back to 0644 otherwise.
chmod --reference="$LNET_CONF" "$tmp" 2>/dev/null || chmod 0644 "$tmp"
mv -f "$tmp" "$LNET_CONF"
# trap still holds a removed path -- harmless, rm -f.

# Summarize what we did.  Read back the rewritten file for the log.
resolved=$(printf '%s\n' "$non_rxe_list" | tr '\n' ' ')
log "resolved $pt_count passthrough placeholder(s) in $LNET_CONF" \
	"-> [${resolved% }]"

exit 0
