#!/usr/bin/env bash
# Emit /etc/modprobe.d/lnet.conf body from an fc_nics= cmdline value.
#
# Consumed by the multi-NIC foundation (-oc8) and softroce backend
# (-r55).
#
# Usage:
#     setup-lnet-config.sh                      # reads /proc/cmdline, stdout
#     setup-lnet-config.sh /path/to/lnet.conf   # writes to path
#     echo "fc_nics=tcp,softroce" | \
#         setup-lnet-config.sh --stdin          # reads cmdline from stdin; -s also works
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
#
# fc_nic_nets= (--nic net=) names the LNet network of each extra NIC,
# empty for the automatic name above.  NICs with one name share one
# network: tcp0(eth1,eth2).  An automatic name skips the explicit ones.
# ltvm validates the names, so this script does not.
set -euo pipefail

emit_lnet_conf() {
	# Args: one TYPE[=NET] entry per NIC.  Position I corresponds to
	# ethI.  'none' skips the slot (mgmt SSH-only).  Writes the full
	# lnet.conf body (one line + trailing newline) to stdout.
	local -a nics=("$@")
	local -a order=()
	local -A ifaces=() taken=()
	local -A next=([tcp]=0 [o2ib]=0)
	local i entry nic net lnd iface idx joined

	for entry in "${nics[@]}"; do
		if [[ $entry == *=* ]]; then
			taken[${entry#*=}]=1
		fi
	done

	for ((i=0; i < ${#nics[@]}; i++)); do
		entry=${nics[$i]}
		nic=${entry%%[=;]*}
		net=""
		if [[ $entry == *=* ]]; then
			net=${entry#*=}
		fi
		case "$nic" in
		none)
			# Placeholder: keep eth slot (ethI) reserved but
			# don't emit an LNet entry.  Used for mgmt (eth0)
			# when --nic is set so it's SSH-only.
			continue
			;;
		tcp)
			lnd=tcp
			iface=eth${i}
			;;
		softroce)
			lnd=o2ib
			iface=eth${i}
			;;
		passthrough)
			lnd=o2ib
			iface="@ib-of-eth${i})"
			;;
		*)
			echo "setup-lnet-config: unknown NIC type '$nic'" >&2
			return 1
			;;
		esac
		if [[ -z $net ]]; then
			idx=${next[$lnd]}
			while [[ ${taken[$lnd$idx]+set} ]]; do
				idx=$((idx + 1))
			done
			net=$lnd$idx
			next[$lnd]=$((idx + 1))
		fi
		if [[ ${ifaces[$net]+set} ]]; then
			ifaces[$net]+=",$iface"
		else
			order+=("$net")
			ifaces[$net]=$iface
		fi
	done

	joined=""
	for net in "${order[@]}"; do
		joined+="${joined:+,}${net}(${ifaces[$net]})"
	done
	printf 'options lnet networks="%s"\n' "$joined"
}

cmdline_param() {
	# Extract the value of parameter $1 from a kernel-cmdline-style
	# string on stdin.  Prints the value (or empty).
	# We deliberately avoid pipefail propagation: grep returning 1
	# when the parameter is absent is not an error.
	tr ' ' '\n' | awk -v key="$1=" \
		'index($0, key) == 1 {print substr($0, length(key) + 1); exit}'
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
			sed -n '2,37p' "$0"
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

	local fc_nics fc_nic_nets
	fc_nics=$(printf '%s' "$cmdline" | cmdline_param fc_nics)
	fc_nic_nets=$(printf '%s' "$cmdline" | cmdline_param fc_nic_nets)

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
		local -a _extras _nets
		local i
		IFS=',' read -r -a _extras <<<"$fc_nics"
		IFS=',' read -r -a _nets <<<"$fc_nic_nets"
		for ((i=0; i < ${#_extras[@]}; i++)); do
			nic_array+=("${_extras[$i]}${_nets[$i]:+=${_nets[$i]}}")
		done
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
