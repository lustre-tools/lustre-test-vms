#!/usr/bin/env bash
# Tests for targets/common/setup-lnet-passthrough-resolve.sh
#
# Runs the resolver against a fake /sys/class/infiniband/ tree
# (via SYSFS_IB_ROOT) and a synthetic FC_NICS env.  Verifies:
#   - 0 passthrough: no-op, file untouched
#   - 1 passthrough + 0 softroce
#   - 1 passthrough + 1 softroce (mixed, rxe ignored)
#   - 2 passthrough
#   - mismatched counts: warn + leave placeholder
#   - operator-authored file (no marker): untouched
#
# A stub `logger` in PATH lets us capture log messages and assert
# on the warning path.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
UUT="$REPO_ROOT/targets/common/setup-lnet-passthrough-resolve.sh"

if [[ ! -x $UUT ]]; then
	echo "FAIL: $UUT is not executable" >&2
	exit 1
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# Stub `logger` on PATH so tests can inspect what the script logged.
# The stub appends each call's args to $WORK/logger.out.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/logger" <<'EOF'
#!/bin/sh
# Strip the "-t TAG --" prefix; record the rest.
tag=""
while [ $# -gt 0 ]; do
	case "$1" in
	-t) tag=$2; shift 2 ;;
	--) shift; break ;;
	*) break ;;
	esac
done
printf '%s: %s\n' "$tag" "$*" >> "$LOGGER_OUT"
EOF
chmod +x "$WORK/bin/logger"

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

# Runs the resolver in a fresh per-case sandbox.
# Args: case_name, fc_nics, ibdev_list (space-separated), initial_conf.
# Sets globals: RESULT (final conf contents), STDERR (stderr text),
# LOGGER (captured logger calls).
run_case() {
	local name=$1
	local fc_nics=$2
	local ibs=$3
	local initial=$4

	local casedir="$WORK/$name"
	rm -rf "$casedir"
	mkdir -p "$casedir/sys_ib"
	for dev in $ibs; do
		mkdir -p "$casedir/sys_ib/$dev"
	done

	local conf="$casedir/lnet.conf"
	printf '%s\n' "$initial" > "$conf"

	local stderr_file="$casedir/stderr"
	local logger_file="$casedir/logger.out"
	: > "$logger_file"

	PATH="$WORK/bin:$PATH" \
	LOGGER_OUT="$logger_file" \
	FC_NICS="$fc_nics" \
	SYSFS_IB_ROOT="$casedir/sys_ib" \
		"$UUT" "$conf" 2>"$stderr_file" || true

	RESULT=$(cat "$conf")
	STDERR=$(cat "$stderr_file")
	LOGGER=$(cat "$logger_file")
}

# --- Case: 0 passthrough (softroce only) ---------------------------
# Resolver must be a silent no-op: exits before any log on the
# "nothing to resolve" path.
run_case "0pt-softroce" "softroce" "rxe0" \
	'options lnet networks="tcp0(eth0),o2ib0(rxe0)"'
check "0pt: conf unchanged" \
	'options lnet networks="tcp0(eth0),o2ib0(rxe0)"' \
	"$RESULT"

# --- Case: 0 passthrough, no FC_NICS at all ------------------------
run_case "0pt-empty" "" "" \
	'options lnet networks="tcp0(eth0)"'
check "0pt empty fc_nics: conf unchanged" \
	'options lnet networks="tcp0(eth0)"' \
	"$RESULT"

# --- Case: 1 passthrough + 0 softroce ------------------------------
run_case "1pt-0sr" "passthrough" "mlx5_0" \
	'options lnet networks="tcp0(eth0),o2ib0(@ib-of-eth1))"'
check "1pt+0sr: placeholder resolved" \
	'options lnet networks="tcp0(eth0),o2ib0(mlx5_0)"' \
	"$RESULT"

# --- Case: 1 passthrough + 1 softroce (mixed) ----------------------
# rxe0 is softroce, already named correctly in conf; resolver must
# skip it and match mlx5_0 to the lone passthrough placeholder.
run_case "1pt-1sr" "softroce,passthrough" "mlx5_0 rxe0" \
	'options lnet networks="tcp0(eth0),o2ib0(rxe0),o2ib1(@ib-of-eth2))"'
check "1pt+1sr: only passthrough resolved" \
	'options lnet networks="tcp0(eth0),o2ib0(rxe0),o2ib1(mlx5_0)"' \
	"$RESULT"

# --- Case: 2 passthrough -------------------------------------------
# Emitter's canonical output for fc_nics=passthrough,passthrough:
#   tcp0(eth0),o2ib0(@ib-of-eth1)),o2ib1(@ib-of-eth2))
run_case "2pt" "passthrough,passthrough" "mlx5_0 mlx5_1" \
	'options lnet networks="tcp0(eth0),o2ib0(@ib-of-eth1)),o2ib1(@ib-of-eth2))"'
check "2pt: both resolved in order" \
	'options lnet networks="tcp0(eth0),o2ib0(mlx5_0),o2ib1(mlx5_1)"' \
	"$RESULT"

# --- Case: mismatched counts (warn, keep placeholder) --------------
# 2 passthrough declared, only 1 non-rxe ibdev present.
mismatch_initial='options lnet networks="tcp0(eth0),o2ib0(@ib-of-eth1)),o2ib1(@ib-of-eth2))"'
run_case "mismatch" "passthrough,passthrough" "mlx5_0" \
	"$mismatch_initial"
check "mismatch: conf unchanged" \
	"$mismatch_initial" \
	"$RESULT"
# Warning should mention "WARNING" and reach both stderr and logger.
if [[ $STDERR == *WARNING* ]]; then
	pass=$((pass + 1))
	printf 'ok   %s\n' "mismatch: stderr carries WARNING"
else
	fail=$((fail + 1))
	printf 'FAIL %s\n' "mismatch: stderr carries WARNING"
	printf '  stderr: %q\n' "$STDERR"
fi
if [[ $LOGGER == *WARNING* ]]; then
	pass=$((pass + 1))
	printf 'ok   %s\n' "mismatch: logger sees WARNING"
else
	fail=$((fail + 1))
	printf 'FAIL %s\n' "mismatch: logger sees WARNING"
	printf '  logger: %q\n' "$LOGGER"
fi

# --- Case: operator-authored lnet.conf (no marker) -----------------
# Even with a passthrough in FC_NICS, if the conf has no marker we
# must not touch it -- user/deploy-lustre owns the file.
operator_conf='options lnet networks="tcp0(eth0),o2ib0(ib0)"'
run_case "no-marker" "passthrough" "mlx5_0" \
	"$operator_conf"
check "no-marker: operator conf untouched" \
	"$operator_conf" \
	"$RESULT"

# --- Case: more non-rxe ibdevs than passthrough entries ------------
# Common real-world case: guest sees several ibdevs we didn't ask
# for.  We still refuse to guess -- mismatch policy applies both
# ways.
run_case "extra-ibs" "passthrough" "mlx5_0 mlx5_1" \
	'options lnet networks="tcp0(eth0),o2ib0(@ib-of-eth1))"'
check "extra-ibs: placeholder kept" \
	'options lnet networks="tcp0(eth0),o2ib0(@ib-of-eth1))"' \
	"$RESULT"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
