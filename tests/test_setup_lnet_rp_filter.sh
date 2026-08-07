#!/usr/bin/env bash
# Tests for targets/common/setup-lnet-rp-filter.sh
#
# Two layers, matching test_setup_lnet_config.sh:
#   - lnet_ifaces() sourced directly, to pin the parser
#   - the script driven end-to-end against a fake /proc and /etc/sysctl.d
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
UUT="$REPO_ROOT/targets/common/setup-lnet-rp-filter.sh"

if [[ ! -x $UUT ]]; then
	echo "FAIL: $UUT is not executable" >&2
	exit 1
fi

# shellcheck disable=SC1090
source "$UUT"

pass=0
fail=0

check() {
	local name=$1
	local want=$2
	local got=$3
	if [[ $want == "$got" ]]; then
		pass=$((pass + 1))
		printf 'ok   %s\n' "$name"
	else
		fail=$((fail + 1))
		printf 'FAIL %s\n' "$name"
		printf '  want: %q\n' "$want"
		printf '  got:  %q\n' "$got"
	fi
}

ifaces_of() {
	printf '%s\n' "$1" | lnet_ifaces | tr '\n' ' ' | sed 's/ $//'
}

# --- Parser --------------------------------------------------------

check "single tcp NI" \
	'eth0' \
	"$(ifaces_of 'options lnet networks="tcp0(eth0)"')"

check "two rails" \
	'eth0 eth1' \
	"$(ifaces_of 'options lnet networks="tcp0(eth0),tcp1(eth1)"')"

check "mixed tcp + o2ib" \
	'eth0 eth1' \
	"$(ifaces_of 'options lnet networks="tcp0(eth0),o2ib0(eth1)"')"

check "mgmt SSH-only (no eth0 entry)" \
	'eth1 eth2' \
	"$(ifaces_of 'options lnet networks="tcp0(eth1),o2ib0(eth2)"')"

check "resolved passthrough ibdev name survives the parser" \
	'eth1 mlx5_0' \
	"$(ifaces_of 'options lnet networks="tcp0(eth1),o2ib0(mlx5_0)"')"

check "unresolved passthrough placeholder survives the parser" \
	'@ib-of-eth1' \
	"$(ifaces_of 'options lnet networks="o2ib0(@ib-of-eth1))"')"

check "no networks= line" \
	'' \
	"$(ifaces_of '# operator notes, nothing to see')"

# --- End-to-end ----------------------------------------------------

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Fake /proc/sys/net/ipv4/conf: eth0 and eth1 are netdevs, mlx5_0 is
# not (an ibdev has no rp_filter knob).
mkdir -p "$tmp/proc/eth0" "$tmp/proc/eth1" "$tmp/proc/all" "$tmp/proc/default"
touch "$tmp/proc/eth0/rp_filter" "$tmp/proc/eth1/rp_filter"
mkdir -p "$tmp/sysctl.d"

run_uut() {
	SYSCTL_CONF_DIR="$tmp/sysctl.d" \
		PROC_NET_CONF="$tmp/proc" \
		APPLY=no \
		"$UUT" "$1" 2>/dev/null
}

conf="$tmp/lnet.conf"
out="$tmp/sysctl.d/99-lustre-lnet-rp-filter.conf"

printf '%s\n' 'options lnet networks="tcp0(eth0),tcp1(eth1)"' >"$conf"
run_uut "$conf"
check "e2e: both rails get a per-iface knob" \
	'net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
net.ipv4.conf.eth0.rp_filter = 0
net.ipv4.conf.eth1.rp_filter = 0' \
	"$(grep '^net\.' "$out")"

# An ibdev name parses out of lnet.conf but has no rp_filter knob, so
# it must be filtered rather than emitted as a bogus sysctl key.
printf '%s\n' 'options lnet networks="tcp0(eth0),o2ib0(mlx5_0)"' >"$conf"
run_uut "$conf"
check "e2e: non-netdev (ibdev) filtered out" \
	'net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
net.ipv4.conf.eth0.rp_filter = 0' \
	"$(grep '^net\.' "$out")"

# Even with nothing to name per-interface, all/default must still be
# cleared -- the kernel takes max(all, iface).
printf '%s\n' 'options lnet networks="o2ib0(mlx5_0)"' >"$conf"
run_uut "$conf"
check "e2e: all/default cleared even with no matching netdev" \
	'net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0' \
	"$(grep '^net\.' "$out")"

# Missing lnet.conf is the normal single-NIC case: quiet no-op, and in
# particular no stale drop-in left behind from a previous run.
rm -f "$out"
run_uut "$tmp/does-not-exist.conf"
if [[ -e $out ]]; then
	fail=$((fail + 1))
	printf 'FAIL %s\n' "missing lnet.conf should not write a drop-in"
else
	pass=$((pass + 1))
	printf 'ok   %s\n' "missing lnet.conf is a quiet no-op"
fi

# --stdin is parse-only: it must never touch the filesystem.
got=$(printf '%s\n' 'options lnet networks="tcp0(eth0),tcp1(eth1)"' |
	SYSCTL_CONF_DIR="$tmp/sysctl.d" "$UUT" --stdin | tr '\n' ' ' | sed 's/ $//')
check "cli: --stdin prints the iface list" 'eth0 eth1' "$got"
if [[ -e $out ]]; then
	fail=$((fail + 1))
	printf 'FAIL %s\n' "--stdin should not write a drop-in"
else
	pass=$((pass + 1))
	printf 'ok   %s\n' "--stdin writes nothing"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
