#!/usr/bin/env bash
# Tests for targets/common/setup-lnet-config.sh
#
# Verifies the five canonical cases from the design spec by sourcing
# the script (so we can call emit_lnet_conf directly) and also by
# driving the script end-to-end via --stdin with fc_nics=... cmdlines.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
UUT="$REPO_ROOT/targets/common/setup-lnet-config.sh"

if [[ ! -x $UUT ]]; then
	echo "FAIL: $UUT is not executable" >&2
	exit 1
fi

# Source for direct function access.
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

# --- Direct emit_lnet_conf cases -----------------------------------

check "tcp only" \
	'options lnet networks="tcp0(eth0)"' \
	"$(emit_lnet_conf tcp)"

check "tcp + tcp" \
	'options lnet networks="tcp0(eth0),tcp1(eth1)"' \
	"$(emit_lnet_conf tcp tcp)"

check "tcp + softroce" \
	'options lnet networks="tcp0(eth0),o2ib0(rxe0)"' \
	"$(emit_lnet_conf tcp softroce)"

check "tcp + softroce + softroce" \
	'options lnet networks="tcp0(eth0),o2ib0(rxe0),o2ib1(rxe1)"' \
	"$(emit_lnet_conf tcp softroce softroce)"

check "tcp + passthrough" \
	'options lnet networks="tcp0(eth0),o2ib0(@ib-of-eth1))"' \
	"$(emit_lnet_conf tcp passthrough)"

# --- End-to-end via --stdin ----------------------------------------

run_cli() {
	# Pipe a synthetic cmdline into the script in --stdin mode.
	printf '%s\n' "$1" | "$UUT" --stdin
}

# fc_nics on the cmdline carries *extras* only; the mgmt NIC (eth0=tcp)
# is implicit and prepended by main() before calling emit_lnet_conf.
check "cli: fc_nics=tcp (one extra)" \
	'options lnet networks="tcp0(eth0),tcp1(eth1)"' \
	"$(run_cli 'ro fc_ip=1.2.3.4 fc_nics=tcp console=ttyS0')"

check "cli: fc_nics=softroce" \
	'options lnet networks="tcp0(eth0),o2ib0(rxe0)"' \
	"$(run_cli 'fc_nics=softroce quiet')"

check "cli: fc_nics missing -> mgmt tcp only" \
	'options lnet networks="tcp0(eth0)"' \
	"$(run_cli 'ro quiet console=ttyS0')"

check "cli: fc_nics=passthrough" \
	'options lnet networks="tcp0(eth0),o2ib0(@ib-of-eth1))"' \
	"$(run_cli 'fc_nics=passthrough')"

# --- Write-to-file mode --------------------------------------------

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
printf '%s\n' 'fc_nics=softroce,softroce' | "$UUT" --stdin "$tmp"
got_file=$(cat "$tmp")
check "cli: writes to path arg" \
	'options lnet networks="tcp0(eth0),o2ib0(rxe0),o2ib1(rxe1)"' \
	"$got_file"

# --- Unknown type fails --------------------------------------------

if ( emit_lnet_conf tcp bogus ) >/dev/null 2>&1; then
	fail=$((fail + 1))
	printf 'FAIL %s\n' "unknown NIC type should error"
else
	pass=$((pass + 1))
	printf 'ok   %s\n' "unknown NIC type rejected"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
