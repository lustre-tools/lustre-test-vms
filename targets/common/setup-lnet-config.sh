#!/usr/bin/env bash
# Emit /etc/modprobe.d/lnet.conf body from an fc_nics= cmdline value.
#
# Consumed by the multi-NIC foundation (-oc8) and softroce backend
# (-r55).  This script is the source of truth for the emitter logic;
# ltvm_pkg/lnet_config.py mirrors it for unit testing convenience.
#
# Usage:
#     setup-lnet-config.sh                      # reads /proc/cmdline, stdout
#     setup-lnet-config.sh /path/to/lnet.conf   # writes to path
#     echo "fc_nics=tcp,softroce" | \
#         setup-lnet-config.sh --stdin          # reads cmdline from stdin
#
# fc_nics= is a comma-separated list of NIC types.  The first element
# is always the mgmt NIC type ('tcp' on eth0).  Subsequent elements
# apply to eth1, eth2, ...
#
# Net-name assignment:
#   tcp         -> tcpN(ethN)              N = tcp index (eth index)
#   softroce    -> o2ibN(rxeM)             N = o2ib index, M = softroce
#                                          index (rxe link on ethI)
#   passthrough -> o2ibN(@ib-of-ethI))     N = o2ib index, I = eth
#                                          index; '@' marker replaced
#                                          at runtime by -5a0.
set -euo pipefail

emit_lnet_conf() {
	# Args: NIC type strings, one per positional argument.
	# Writes the full lnet.conf body (one line + trailing newline) to
	# stdout.
	local -a nics=("$@")
	local -a parts=()
	local i
	local nic
	local rxe_idx=0
	local o2ib_idx=0

	for ((i=0; i < ${#nics[@]}; i++)); do
		nic=${nics[$i]}
		case "$nic" in
		tcp)
			parts+=("tcp${i}(eth${i})")
			;;
		softroce)
			parts+=("o2ib${o2ib_idx}(rxe${rxe_idx})")
			o2ib_idx=$((o2ib_idx + 1))
			rxe_idx=$((rxe_idx + 1))
			;;
		passthrough)
			parts+=("o2ib${o2ib_idx}(@ib-of-eth${i}))")
			o2ib_idx=$((o2ib_idx + 1))
			;;
		*)
			echo "setup-lnet-config: unknown NIC type '$nic'" >&2
			return 1
			;;
		esac
	done

	local joined=""
	local first=true
	local p
	for p in "${parts[@]}"; do
		if [[ $first == true ]]; then
			joined=$p
			first=false
		else
			joined="${joined},${p}"
		fi
	done

	printf 'options lnet networks="%s"\n' "$joined"
}

parse_fc_nics() {
	# Extract fc_nics= value from a kernel-cmdline-style string on
	# stdin.  Prints the comma-separated value (or empty).
	# We deliberately avoid pipefail propagation: grep returning 1
	# when fc_nics= is absent is not an error.
	tr ' ' '\n' | awk -F= '/^fc_nics=/ {sub(/^fc_nics=/, ""); \
		print; exit}'
}

main() {
	local cmdline
	local out_path=""
	local from_stdin=false

	while (( $# > 0 )); do
		case "$1" in
		--stdin)
			from_stdin=true
			shift
			;;
		-h|--help)
			sed -n '2,20p' "$0"
			return 0
			;;
		*)
			out_path=$1
			shift
			;;
		esac
	done

	if [[ $from_stdin == true ]]; then
		cmdline=$(cat)
	else
		cmdline=$(cat /proc/cmdline)
	fi

	local fc_nics
	fc_nics=$(printf '%s' "$cmdline" | parse_fc_nics)

	# fc_nics on the cmdline carries the *extras* (eth1+) only -- the
	# mgmt NIC (eth0 = tcp, configured from fc_ip/fc_gw) is implicit.
	# The emit_lnet_conf function expects every NIC listed, mgmt first,
	# so prepend 'tcp' here.  Empty fc_nics -> just the mgmt NIC.
	local -a nic_array=(tcp)
	if [[ -n $fc_nics ]]; then
		local -a _extras
		IFS=',' read -r -a _extras <<<"$fc_nics"
		nic_array+=("${_extras[@]}")
	fi

	local body
	body=$(emit_lnet_conf "${nic_array[@]}")

	if [[ -n $out_path ]]; then
		printf '%s\n' "$body" > "$out_path"
	else
		printf '%s\n' "$body"
	fi
}

# Only invoke main when run as a script (not when sourced).
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
	main "$@"
fi
