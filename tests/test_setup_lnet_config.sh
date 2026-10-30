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
#
# Semantics: the first positional arg is the mgmt slot (eth0).  'tcp'
# there means mgmt doubles as LNet tcp0 (status quo, no --nic flag).
# 'none' means mgmt is SSH-only and drops out of LNet (the new
# --nic-present behaviour).  tcp indices renumber per-type, so
# tcp(none, tcp, tcp) -> tcp0(eth1),tcp1(eth2).

check "tcp only (mgmt doubles as tcp0)" \
	'options lnet networks="tcp0(eth0)"' \
	"$(emit_lnet_conf tcp)"

check "tcp + tcp (mgmt + extra tcp)" \
	'options lnet networks="tcp0(eth0),tcp1(eth1)"' \
	"$(emit_lnet_conf tcp tcp)"

check "tcp + softroce (mgmt + softroce)" \
	'options lnet networks="tcp0(eth0),o2ib0(eth1)"' \
	"$(emit_lnet_conf tcp softroce)"

check "tcp + softroce + softroce" \
	'options lnet networks="tcp0(eth0),o2ib0(eth1),o2ib1(eth2)"' \
	"$(emit_lnet_conf tcp softroce softroce)"

check "tcp + passthrough" \
	'options lnet networks="tcp0(eth0),o2ib0(@ib-of-eth1))"' \
	"$(emit_lnet_conf tcp passthrough)"

# 'none' sentinel: mgmt slot reserved but excluded from LNet.

check "none only -> empty LNet" \
	'options lnet networks=""' \
	"$(emit_lnet_conf none)"

check "none + softroce (mgmt SSH-only, rxe on eth1)" \
	'options lnet networks="o2ib0(eth1)"' \
	"$(emit_lnet_conf none softroce)"

check "none + tcp (mgmt SSH-only, extra tcp on eth1)" \
	'options lnet networks="tcp0(eth1)"' \
	"$(emit_lnet_conf none tcp)"

check "none + tcp + softroce (mgmt + tcp + softroce)" \
	'options lnet networks="tcp0(eth1),o2ib0(eth2)"' \
	"$(emit_lnet_conf none tcp softroce)"

check "none + tcp + tcp (multi-rail tcp without mgmt)" \
	'options lnet networks="tcp0(eth1),tcp1(eth2)"' \
	"$(emit_lnet_conf none tcp tcp)"

check "none + softroce + softroce" \
	'options lnet networks="o2ib0(eth1),o2ib1(eth2)"' \
	"$(emit_lnet_conf none softroce softroce)"

check "none + passthrough" \
	'options lnet networks="o2ib0(@ib-of-eth1))"' \
	"$(emit_lnet_conf none passthrough)"

# --- End-to-end via --stdin ----------------------------------------

run_cli() {
	# Pipe a synthetic cmdline into the script in --stdin mode.
	printf '%s\n' "$1" | "$UUT" --stdin
}

# fc_nics on the cmdline carries *extras* only (eth1+).  Semantics:
#   - fc_nics missing   => mgmt doubles as tcp0 (main() prepends 'tcp')
#   - fc_nics non-empty => mgmt is SSH-only   (main() prepends 'none')

check "cli: fc_nics missing -> mgmt tcp only" \
	'options lnet networks="tcp0(eth0)"' \
	"$(run_cli 'ro quiet console=ttyS0')"

check "cli: fc_nics=tcp (extra tcp on eth1, mgmt SSH-only)" \
	'options lnet networks="tcp0(eth1)"' \
	"$(run_cli 'ro fc_ip=1.2.3.4 fc_nics=tcp console=ttyS0')"

check "cli: fc_nics=tcp,tcp (multi-rail tcp without mgmt)" \
	'options lnet networks="tcp0(eth1),tcp1(eth2)"' \
	"$(run_cli 'fc_nics=tcp,tcp')"

check "cli: fc_nics=softroce (mgmt SSH-only)" \
	'options lnet networks="o2ib0(eth1)"' \
	"$(run_cli 'fc_nics=softroce quiet')"

check "cli: fc_nics=softroce,softroce" \
	'options lnet networks="o2ib0(eth1),o2ib1(eth2)"' \
	"$(run_cli 'fc_nics=softroce,softroce')"

check "cli: fc_nics=tcp,softroce" \
	'options lnet networks="tcp0(eth1),o2ib0(eth2)"' \
	"$(run_cli 'fc_nics=tcp,softroce')"

check "cli: fc_nics=passthrough (mgmt SSH-only)" \
	'options lnet networks="o2ib0(@ib-of-eth1))"' \
	"$(run_cli 'fc_nics=passthrough')"

# --- Explicit rail profiles ----------------------------------------

check "shared tcp profile groups local interfaces" \
    'options lnet networks="tcp0(eth1,eth2)"' \
    "$(run_cli 'fc_nics=tcp,tcp fc_lnet_nets=tcp0,tcp0 fc_rail_topology=shared')"
check "shared SoftRoCE profile groups local interfaces" \
    'options lnet networks="o2ib0(eth1,eth2)"' \
    "$(run_cli 'fc_nics=softroce,softroce fc_lnet_nets=o2ib0,o2ib0 fc_rail_topology=shared')"
check "isolated profile keeps distinct networks" \
    'options lnet networks="tcp2(eth1),tcp9(eth2)"' \
    "$(run_cli 'fc_nics=tcp,tcp fc_lnet_nets=tcp2,tcp9 fc_rail_topology=isolated')"
check "stdin has short form" \
    'options lnet networks="tcp0(eth0)"' \
    "$(printf '%s' quiet | "$UUT" -s)"

# Reject a partial, mixed, oversized, or malformed topology instead of guessing.
for invalid in \
    'fc_nics=tcp,tcp fc_lnet_nets=tcp0,tcp0' \
    'fc_nics=tcp,tcp fc_rail_topology=shared' \
    'fc_nics=tcp,tcp fc_lnet_nets=tcp0,tcp1 fc_rail_topology=shared' \
    'fc_nics=tcp,tcp fc_lnet_nets=tcp0,tcp0 fc_rail_topology=isolated' \
    'fc_nics=tcp,tcp fc_lnet_nets=o2ib0,o2ib0 fc_rail_topology=shared' \
    'fc_nics=tcp,tcp fc_lnet_nets=tcp0,tcp0, fc_rail_topology=shared' \
    'fc_nics=tcp fc_lnet_nets=tcp0 fc_rail_topology=shared' \
    'fc_nics=tcp,tcp fc_lnet_nets=tcp65536,tcp65536 fc_rail_topology=shared' \
    'fc_nics=tcp,tcp fc_lnet_nets=tcp0,tcp0 fc_rail_topology=shared fc_rail_topology=isolated' \
    'fc_nics=tcp,tcp,tcp,tcp,tcp,tcp,tcp,tcp,tcp fc_lnet_nets=tcp0,tcp0,tcp0,tcp0,tcp0,tcp0,tcp0,tcp0,tcp0 fc_rail_topology=shared'; do
    if run_cli "$invalid" >/dev/null 2>&1; then
        fail=$((fail + 1))
        printf 'FAIL invalid profile was accepted: %s\n' "$invalid"
    else
        pass=$((pass + 1))
    fi
done

# --- Write-to-file mode --------------------------------------------

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
printf '%s\n' 'fc_nics=softroce,softroce' | "$UUT" --stdin "$tmp"
got_file=$(cat "$tmp")
check "cli: writes to path arg" \
	'options lnet networks="o2ib0(eth1),o2ib1(eth2)"' \
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
