#!/usr/bin/env bash
# Emit /etc/modprobe.d/lnet.conf from the guest kernel command line.
# fc_nics= lists extra NIC types, in eth1, eth2, ... order. Without
# extras, eth0 is tcp0. With extras, eth0 is for management only.
# fc_nic_nets= optionally names the LNet network of each extra NIC.
# Equal explicit names group interfaces on one LNet network. Empty
# entries get separate automatic names, skipping explicit names.
#
# Usage: setup-lnet-config.sh [-s|--stdin] [output-path]
set -euo pipefail

config_error() {
	# Report an invalid boot parameter before any config file is written.
	printf 'setup-lnet-config: %s\n' "$1" >&2
	return 1
}

nic_transport() {
	# Return the LNet transport for a NIC type; retain passthrough args.
	case "${1%%;*}" in
	tcp) printf 'tcp\n' ;;
	softroce|passthrough) printf 'o2ib\n' ;;
	none) printf '\n' ;;
	*) config_error "unknown NIC type '${1%%;*}'" ;;
	esac
}

explicit_networks() {
	# Validate the parallel extra-NIC names and print one name per slot.
	# A dash marks an automatic name, including the implicit eth0 slot.
	local csv=$1
	shift
	local -a nics=("$@") names=()
	local i name transport number

	# A 64 KiB command line holds at most 16,384 shortest NIC entries.
	(( ${#nics[@]} > 0 && ${#nics[@]} <= 16385 )) ||
		{ config_error 'too many NIC entries'; return 1; }
	(( ${#csv} <= 128 )) ||
		{ config_error 'fc_nic_nets is too long'; return 1; }
	if [[ -n $csv ]]; then
		# Only the new explicit layout has the eight-extra-NIC limit.
		(( ${#nics[@]} <= 9 )) ||
			{ config_error 'expected at most eight explicit extra NICs'; return 1; }
		# The sentinel retains a trailing empty CSV entry in Bash read.
		IFS=',' read -r -a names <<<"${csv},end"
		(( ${#names[@]} == ${#nics[@]} )) ||
			{ config_error 'fc_nic_nets count does not match fc_nics'; return 1; }
		unset 'names[${#names[@]} - 1]'
	fi
	for ((i=0; i < ${#nics[@]}; i++)); do
		transport=$(nic_transport "${nics[$i]}") || return 1
		name=''
		(( i == 0 )) || name=${names[$((i - 1))]:-}
		if [[ -n $name ]]; then
			# Only software NICs take net=; passthrough keeps its resolver.
			[[ ${nics[$i]} == tcp || ${nics[$i]} == softroce ]] ||
				{ config_error 'explicit network needs tcp or softroce'; return 1; }
			[[ $name =~ ^${transport}(0|[1-9][0-9]{0,4})$ ]] ||
				{ config_error "invalid LNet network '$name'"; return 1; }
			number=${name#"$transport"}
			(( 10#$number <= 65535 )) ||
				{ config_error "LNet network number is too large: '$name'"; return 1; }
		fi
		printf '%s\n' "${name:--}"
	done
}

assign_networks() {
	# Reserve explicit names before choosing unused automatic names.
	local csv=$1 validated name transport candidate
	shift
	local -a nics=("$@") names=()
	local -A used=() next_number=([tcp]=0 [o2ib]=0)
	local i number scan

	validated=$(explicit_networks "$csv" "${nics[@]}") || return 1
	mapfile -t names <<<"$validated" || return 1
	for name in "${names[@]}"; do
		[[ $name == - ]] || used[$name]=1
	done
	for ((i=0; i < ${#nics[@]}; i++)); do
		name=${names[$i]}
		transport=$(nic_transport "${nics[$i]}") || return 1
		if [[ -n $transport && $name == - ]]; then
			# Each transport's index advances; earlier names are not rescanned.
			number=${next_number[$transport]}
			for ((scan=0; scan < ${#nics[@]}; scan++, number++)); do
				candidate=${transport}${number}
				if [[ ! ${used[$candidate]+set} ]]; then
					name=$candidate
					used[$name]=1
					next_number[$transport]=$((number + 1))
					break
				fi
			done
			[[ $name != - ]] ||
				{ config_error 'no free LNet network number'; return 1; }
		fi
		printf '%s\n' "$name"
	done
}

emit_lnet_conf_nets() {
	# Group validated names in NIC order and write one complete config.
	local csv=$1 assigned name iface joined
	shift
	local -a nics=("$@") names=() order=() parts=()
	local -A interfaces=()
	local i

	assigned=$(assign_networks "$csv" "${nics[@]}") || return 1
	mapfile -t names <<<"$assigned" || return 1
	for ((i=0; i < ${#nics[@]}; i++)); do
		name=${names[$i]}
		[[ $name != - ]] || continue
		iface=eth${i}
		if [[ ${nics[$i]%%;*} == passthrough ]]; then
			# Keep the marker expected by setup-lnet-passthrough-resolve.sh.
			iface=@ib-of-eth${i}')'
		fi
		if [[ ${interfaces[$name]+set} ]]; then
			interfaces[$name]+=",${iface}"
		else
			order+=("$name")
			interfaces[$name]=$iface
		fi
	done
	for name in "${order[@]}"; do
		parts+=("${name}(${interfaces[$name]})")
	done
	joined=$(IFS=','; printf '%s' "${parts[*]}")
	printf 'options lnet networks="%s"\n' "$joined"
}

emit_lnet_conf() {
	# Keep the existing sourceable API for callers without explicit names.
	emit_lnet_conf_nets '' "$@"
}

cmdline_parameter() {
	# Return key=value or empty if absent; reject duplicate parameters.
	local key=$1 cmdline=$2 item value='' found=false
	local -a words=()
	IFS=$' \t' read -r -a words <<<"${cmdline//$'\n'/ }"
	for item in "${words[@]}"; do
		if [[ $item == "$key="* ]]; then
			[[ $found == false ]] ||
				{ config_error "duplicate $key parameter"; return 1; }
			value=$item
			found=true
		fi
	done
	printf '%s' "$value"
}

main() {
	# Validate all inputs before opening the optional output file.
	local out_path='' from_stdin=false cmdline fc_nics fc_nic_nets body nets
	local -a nic_array=(tcp) extras=()
	(( $# <= 3 )) ||
		{ config_error 'too many arguments'; return 1; }
	while (( $# > 0 )); do
		case "$1" in
		-s|--stdin) from_stdin=true ;;
		-h|--help)
			printf 'Usage: %s [-s|--stdin] [output-path]\n' "$0" || return 1
			return 0 ;;
		-*) config_error "unknown option '$1'"; return 1 ;;
		*)
			[[ -z $out_path ]] ||
				{ config_error 'expected one output path'; return 1; }
			out_path=$1 ;;
		esac
		shift
	done
	if [[ $from_stdin == true ]]; then
		# EOF is expected; NUL or an overlong input makes read succeed.
		if IFS= read -r -d '' -n 65537 cmdline; then
			config_error 'command line is too long or contains NUL'; return 1
		fi
	else
		IFS= read -r cmdline < /proc/cmdline || return 1
	fi
	(( ${#cmdline} <= 65536 )) ||
		{ config_error 'command line is too long'; return 1; }
	fc_nics=$(cmdline_parameter fc_nics "$cmdline") || return 1
	fc_nics=${fc_nics#*=}
	nets=$(cmdline_parameter fc_nic_nets "$cmdline") || return 1
	fc_nic_nets=${nets#*=}
	if [[ -n $fc_nics ]]; then
		# Append a sentinel so a trailing empty NIC remains an error.
		IFS=',' read -r -a extras <<<"${fc_nics},end"
		unset 'extras[${#extras[@]} - 1]'
		nic_array=(none "${extras[@]}")
		if [[ -n $nets && -z $fc_nic_nets && ${#extras[@]} != 1 ]]; then
			config_error 'fc_nic_nets count does not match fc_nics'; return 1
		fi
	elif [[ -n $nets ]]; then
		config_error 'fc_nic_nets requires extra NICs'; return 1
	fi
	body=$(emit_lnet_conf_nets "$fc_nic_nets" "${nic_array[@]}") || return 1
	if [[ -n $out_path ]]; then
		printf '%s\n' "$body" > "$out_path"
	else
		printf '%s\n' "$body"
	fi
}

# Sourcing exposes the emitter without reading the caller's boot parameters.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
	main "$@"
fi
