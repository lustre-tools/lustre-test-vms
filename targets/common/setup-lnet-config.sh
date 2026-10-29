#!/usr/bin/env bash
# Emit /etc/modprobe.d/lnet.conf body from an fc_nics= cmdline value.
#
# Explicit profiles add fc_rail_topology=shared|isolated and one
# fc_lnet_nets= entry per extra NIC. Repeated IDs group the interfaces.
# Without those parameters the existing per-type numbering is preserved.
#
# Usage:
#     setup-lnet-config.sh                      # reads /proc/cmdline, stdout
#     setup-lnet-config.sh /path/to/lnet.conf   # writes to path
#     echo "fc_nics=tcp,softroce" | \
#         setup-lnet-config.sh --stdin          # reads cmdline from stdin
#
# fc_nics= is a comma-separated list of NIC types.  The first element
# is always the mgmt NIC type: 'tcp' (mgmt doubles as tcp0) or 'none'
# (mgmt is SSH-only, excluded from LNet -- set when the user passed
# --nic, so LNet is exactly the --nic list).  Subsequent elements
# apply to eth1, eth2, ...
#
# Net-name assignment:
#   none        -> (skipped; keeps eth slot for later entries)
#   tcp         -> tcpK(ethI)              I = eth index (slot), K =
#                                          tcp index (per-type counter)
#   softroce    -> o2ibK(ethI)             K = o2ib index, I = eth
#                                          index.  Lustre's ko2iblnd
#                                          takes a NETDEV name here and
#                                          finds the rxe ibdev via
#                                          rdma_cm -- the rxe link
#                                          itself is not a netdev and
#                                          cannot appear in lnet.conf.
#   passthrough -> o2ibK(@ib-of-ethI))     K = o2ib index, I = eth
#                                          index; '@' marker replaced
#                                          at runtime by -5a0.
set -euo pipefail

emit_lnet_conf() {
	# Args: NIC type strings, one per positional argument.  Position I
	# corresponds to ethI.  'none' skips the slot (mgmt SSH-only).
	# Writes the full lnet.conf body (one line + trailing newline) to
	# stdout.
	local -a nics=("$@")
	local -a parts=()
	local i
	local nic
	local o2ib_idx=0
	local tcp_idx=0

	for ((i=0; i < ${#nics[@]}; i++)); do
		nic=${nics[$i]}
		case "$nic" in
		none)
			# Placeholder: keep eth slot (ethI) reserved but
			# don't emit an LNet entry.  Used for mgmt (eth0)
			# when --nic is set so it's SSH-only.
			;;
		tcp)
			parts+=("tcp${tcp_idx}(eth${i})")
			tcp_idx=$((tcp_idx + 1))
			;;
		softroce)
			parts+=("o2ib${o2ib_idx}(eth${i})")
			o2ib_idx=$((o2ib_idx + 1))
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

cmdline_value() {
	# Read one bounded kernel argument; duplicate values are not accepted.
	local cmdline=$1 key=$2 token value="" found=false
	local -a tokens=()
	((${#cmdline} <= 131072)) || return 1
	read -r -a tokens <<< "$cmdline"
	((${#tokens[@]} <= 1024)) || return 1
	for token in "${tokens[@]}"; do
		if [[ $token == "$key="* ]]; then
			[[ $found == false ]] || return 1
			value=${token#*=}; found=true
		fi
	done
	printf '%s' "$value"
}

parse_fc_nics() {
	# Preserve the sourceable parser interface used by existing tests.
	local cmdline
	IFS= read -r cmdline || [[ -n $cmdline ]]
	cmdline_value "$cmdline" fc_nics
}

emit_explicit_lnet_conf() {
	# Validate and group up to eight tcp or SoftRoCE rails by their LNet ID.
	local topology=$1 nets=$2 type family net i j seen body="" interfaces
	shift 2
	local -a nics=("$@") ids=() groups=()
	[[ $topology == shared || $topology == isolated ]] || return 1
	((${#nics[@]} >= 2 && ${#nics[@]} <= 8)) || return 1
	[[ $nets != ,* && $nets != *, && $nets != *,,* ]] || return 1
	IFS=, read -r -a ids <<< "$nets"
	((${#ids[@]} == ${#nics[@]})) || return 1
	for ((i=0; i<${#nics[@]}; i++)); do
		type=${nics[$i]}; net=${ids[$i]}
		case $type in
			tcp) family=tcp ;;
			softroce) family=o2ib ;;
			*) return 1 ;;
		esac
		[[ $net =~ ^${family}(0|[1-9][0-9]{0,4})$ ]] || return 1
		((10#${net#"$family"} <= 65535)) || return 1
		if [[ $topology == shared && $net != "${ids[0]}" ]]; then
			# One shared LNet network must contain all local interfaces.
			return 1
		fi
		seen=false
		for ((j=0; j<${#groups[@]}; j++)); do
			if [[ ${groups[$j]} == "$net" ]]; then seen=true; fi
		done
		if [[ $seen == true && $topology == isolated ]]; then return 1; fi
		if [[ $seen == false ]]; then groups+=("$net"); fi
	done
	for net in "${groups[@]}"; do
		interfaces=""
		for ((i=0; i<${#ids[@]}; i++)); do
			if [[ ${ids[$i]} == "$net" ]]; then
				interfaces+="${interfaces:+,}eth$((i + 1))"
			fi
		done
		body+="${body:+,}$net($interfaces)"
	done
	printf 'options lnet networks="%s"\n' "$body"
}

main() {
	local cmdline
	local out_path=""
	local from_stdin=false

	while (( $# > 0 )); do
		case "$1" in
		-s|--stdin)
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

	local fc_nics topology nets
	fc_nics=$(cmdline_value "$cmdline" fc_nics) || return 1
	topology=$(cmdline_value "$cmdline" fc_rail_topology) || return 1
	nets=$(cmdline_value "$cmdline" fc_lnet_nets) || return 1

	# fc_nics on the cmdline carries the *extras* (eth1+) only -- the
	# mgmt NIC (eth0) is implicit.  Semantics:
	#   - No --nic flag (empty fc_nics)  => mgmt doubles as tcp0
	#     (status quo).  Prepend 'tcp' so mgmt appears in LNet.
	#   - Any --nic flag (non-empty fc_nics)  => mgmt is SSH-only
	#     and must drop out of LNet.  Prepend 'none' so the eth0
	#     slot is reserved (positional eth indices preserved) but
	#     no LNet entry is emitted for it.
	local -a nic_array
	if [[ -z $fc_nics ]]; then
		nic_array=(tcp)
	else
		nic_array=(none)
		local -a _extras
		IFS=',' read -r -a _extras <<<"$fc_nics"
		nic_array+=("${_extras[@]}")
	fi

	local body
	if [[ -n $topology || -n $nets ]]; then
		# Explicit profiles must fail rather than silently use legacy numbering.
		body=$(emit_explicit_lnet_conf "$topology" "$nets" "${nic_array[@]:1}") || {
			echo 'setup-lnet-config: invalid explicit rail profile' >&2
			return 1
		}
	else
		body=$(emit_lnet_conf "${nic_array[@]}") || return 1
	fi

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
