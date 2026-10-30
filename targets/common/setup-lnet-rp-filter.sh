#!/usr/bin/env bash
# Disable reverse-path filtering on the interfaces LNet is configured
# to use, and persist that as a sysctl drop-in.
#
# Why this is needed:
#
# rc.local puts every extra NIC (--nic) on the *same* /24 as the mgmt
# NIC -- there's one subnet and one default route, no source-based
# routing from the OS side.  With two or more interfaces on a shared
# subnet, a reply can legitimately arrive on an interface that isn't
# the one the kernel's reverse-path lookup would pick for that source
# address.  Under the RHEL/Rocky default of rp_filter=1 (strict, per
# RFC 3704) the kernel silently drops those packets.
#
# The failure mode is nasty precisely because it is silent: LNet peers
# discover each other, `lctl ping` may even work over one rail, and
# then bulk traffic stalls until the RPC times out.  It reads as a
# Lustre bug rather than a host networking setting.
#
# ksocklnd does install its own MR routes by default
# (skip_mr_route_setup=0), which helps traffic leave via the right
# interface -- but it does not change how the *receive* path is
# filtered.  rp_filter still has to be off.
#
# Note the kernel evaluates rp_filter as
#     max(conf.all.rp_filter, conf.<iface>.rp_filter)
# so clearing the per-interface knob alone is not enough -- 'all' has
# to be cleared too.  'default' is set so interfaces that appear later
# (softroce rxe links, hotplugged NICs) inherit 0.
#
# Usage:
#     setup-lnet-rp-filter.sh                      # /etc/modprobe.d/lnet.conf
#     setup-lnet-rp-filter.sh /path/to/lnet.conf
#     echo 'options lnet networks="tcp0(eth0)"' | \
#         setup-lnet-rp-filter.sh --stdin          # parse only, no writes
#
# Env:
#     SYSCTL_CONF_DIR    override for /etc/sysctl.d (tests)
#     PROC_NET_CONF      override for /proc/sys/net/ipv4/conf (tests)
#     APPLY              set to 'no' to skip live procfs writes
#     STRICT_RAILS       set to 'true' to require every configured netdev

set -euo pipefail

PROG=${0##*/}

log() {
	# Report a setup result to the console and the guest system log.
	printf '%s: %s\n' "$PROG" "$*" >&2
	if command -v logger >/dev/null 2>&1; then
		logger -t "$PROG" -- "$*" || true
	fi
}

lnet_ifaces() {
	# Parse complete network groups before splitting their interface lists.
	# The legacy passthrough marker has an extra closing parenthesis.
	local body rest group iface suffix parsed="" entry
	local -a interfaces=()
	IFS= read -r -d '' -n 65537 body || true
	((${#body} <= 65536)) || return 1
	[[ $body == *networks=* ]] || return 0
	[[ $body =~ networks=\"([^\"]*)\" ]] || return 1
	rest=${BASH_REMATCH[1]}
	for ((entry=0; entry<64; entry++)); do
		[[ -n $rest ]] || { printf '%s' "$parsed"; return 0; }
		[[ $rest =~ ^[a-z][a-z0-9]*\(([^\(\)]*)\) ]] || return 1
		group=${BASH_REMATCH[1]}; suffix=${rest:${#BASH_REMATCH[0]}}
		[[ -n $group && $group != ,* && $group != *, && $group != *,,* ]] || return 1
		IFS=, read -r -a interfaces <<< "$group"
		((${#interfaces[@]} <= 64)) || return 1
		for iface in "${interfaces[@]}"; do
			[[ $iface =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.:-]{0,63}$ || \
			   $iface =~ ^@ib-of-eth[0-9]+$ ]] || return 1
			parsed+="$iface"$'\n'
		done
		if [[ $group == @ib-of-eth* && $suffix == \)* ]]; then
			# Keep compatibility with unresolved passthrough placeholders.
			suffix=${suffix:1}
		fi
		if [[ -z $suffix ]]; then
			rest=""
		elif [[ $suffix == ,* && $suffix != , ]]; then
			rest=${suffix:1}
		else
			return 1
		fi
	done
	[[ -z $rest ]] || return 1
	printf '%s' "$parsed"
}

apply_rp_filter() {
	# Write procfs directly, with checked failure for an explicit rail profile.
	local path=$1
	if [[ -f $path ]] && printf '0\n' > "$path"; then return 0; fi
	log "cannot disable rp_filter at $path"
	[[ ${STRICT_RAILS:-false} != true ]]
}

main() {
	# Persist and apply the receive-path settings for all configured interfaces.
	local conf=""
	local from_stdin=false

	while (($# > 0)); do
		case "$1" in
		-s | --stdin)
			from_stdin=true
			shift
			;;
		-h | --help)
			sed -n '2,44p' "$0"
			return 0
			;;
		*)
			conf=$1
			shift
			;;
		esac
	done

	local body
	if [[ $from_stdin == true ]]; then
		body=$(cat)
		# Parse-only mode for tests: emit the interface list and stop.
		printf '%s\n' "$body" | lnet_ifaces
		return 0
	fi

	conf=${conf:-/etc/modprobe.d/lnet.conf}
	if [[ ! -s $conf ]]; then
		# No LNet config -- nothing is using multiple rails yet.  Quiet
		# exit; this is the normal case on a single-NIC VM before
		# deploy-lustre has run.
		return 0
	fi
	body=$(<"$conf")

	local sysctl_dir=${SYSCTL_CONF_DIR:-/etc/sysctl.d}
	local proc_conf=${PROC_NET_CONF:-/proc/sys/net/ipv4/conf}
	local out=$sysctl_dir/99-lustre-lnet-rp-filter.conf

	# Parse before writing so malformed configuration never gets a partial result.
	local parsed
	parsed=$(printf '%s\n' "$body" | lnet_ifaces) || {
		log 'invalid LNet interface configuration'; return 1
	}
	local -a ifaces=()
	local i
	while IFS= read -r i; do
		[[ -n $i ]] || continue
		if [[ -e $proc_conf/$i/rp_filter ]]; then
			ifaces+=("$i")
		elif [[ ${STRICT_RAILS:-false} == true ]]; then
			log "required interface has no rp_filter setting: $i"; return 1
		fi
	done <<< "$parsed"

	mkdir -p "$sysctl_dir"
	{
		printf '# Generated by %s -- do not edit.\n' "$PROG"
		printf '# Lustre LNet needs rp_filter off on multi-homed\n'
		printf '# interfaces; see the script header for why.\n'
		printf 'net.ipv4.conf.all.rp_filter = 0\n'
		printf 'net.ipv4.conf.default.rp_filter = 0\n'
		for i in "${ifaces[@]:-}"; do
			[[ -n $i ]] || continue
			printf 'net.ipv4.conf.%s.rp_filter = 0\n' "$i"
		done
	} >"$out"

	# Apply the same settings now because rc.local runs after link creation.
	if [[ ${APPLY:-yes} != no ]]; then
		apply_rp_filter "$proc_conf/all/rp_filter" || return 1
		apply_rp_filter "$proc_conf/default/rp_filter" || return 1
		for i in "${ifaces[@]:-}"; do
			[[ -n $i ]] || continue
			apply_rp_filter "$proc_conf/$i/rp_filter" || return 1
		done
	fi

	log "rp_filter disabled for [${ifaces[*]:-none} all default] via $out"
}

# Only invoke main when run as a script (not when sourced).
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
	main "$@"
fi
