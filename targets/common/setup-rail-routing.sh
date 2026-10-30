#!/usr/bin/env bash
# Configure source routing for explicit ltvm rail profiles.
# Tables 201..208 and rule priorities 12002..12017 are reserved for rails.
# Runtime markers prove ownership before any existing routes or rules change.
# iproute2 is the routing fallback because Bash has no rtnetlink library.
# PROC_SYS_ROOT may name a fake proc/sys tree for tests.

rail_error() {
    # Return a checked failure with enough context to locate the failed setup.
    printf 'setup-rail-routing: %s\n' "$*" >&2
    return 1
}

rail_sysctl() {
    # Write a mandatory kernel setting directly; a missing knob is an error.
    local path=${PROC_SYS_ROOT:-/proc/sys}/$1
    if [[ ! -f $path ]]; then
        rail_error "required kernel setting is missing: $path"
        return 1
    fi
    printf '%s\n' "$2" > "$path" || rail_error "cannot set $path"
}

rail_ipv4_network() {
    # Validate a host address and prefix, then print its network address.
    local addr=$1 plen=$2 a b c d part ip mask=0 net
    [[ $addr =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    [[ $plen =~ ^[0-9]{1,2}$ ]] && ((10#$plen <= 32)) || return 1
    IFS=. read -r a b c d <<< "$addr"
    for part in "$a" "$b" "$c" "$d"; do
        # Reject ambiguous leading zeroes and values outside one byte.
        [[ $part == 0 || $part =~ ^[1-9][0-9]{0,2}$ ]] || return 1
        ((10#$part <= 255)) || return 1
    done
    ip=$(((10#$a << 24) | (10#$b << 16) | (10#$c << 8) | 10#$d))
    if ((10#$plen > 0)); then
        mask=$(((0xFFFFFFFF << (32 - 10#$plen)) & 0xFFFFFFFF))
    fi
    net=$((ip & mask))
    printf '%d.%d.%d.%d\n' "$(((net >> 24) & 255))" \
        "$(((net >> 16) & 255))" "$(((net >> 8) & 255))" "$((net & 255))"
}

rail_ipv6_network() {
    # Expand native IPv6 groups and clear host bits without an external tool.
    local addr=$1 plen=$2 left right count i bits value group joined=""
    local -a groups=() first=() last=()
    [[ $addr =~ ^[0-9a-fA-F:]{2,39}$ ]] || return 1
    [[ $addr != *:::* && $addr != :[!:]* && $addr != *[!:]: ]] || return 1
    [[ $plen =~ ^[0-9]{1,3}$ ]] && ((10#$plen <= 128)) || return 1
    if [[ $addr == *::* ]]; then
        # Only one compressed range is valid, and it must replace a group.
        left=${addr%%::*}; right=${addr#*::}
        [[ $right != *::* ]] || return 1
        count=0
        if [[ -n $left ]]; then
            IFS=: read -r -a first <<< "$left"
            groups=("${first[@]}"); count=${#first[@]}
        fi
        if [[ -n $right ]]; then
            IFS=: read -r -a last <<< "$right"
            count=$((count + ${#last[@]}))
        fi
        ((count < 8)) || return 1
        for ((i=count; i<8; i++)); do groups+=(0); done
        if [[ -n $right ]]; then groups+=("${last[@]}"); fi
    else
        IFS=: read -r -a groups <<< "$addr"
        ((${#groups[@]} == 8)) || return 1
    fi
    for ((i=0; i<8; i++)); do
        group=${groups[$i]}
        [[ $group =~ ^[0-9a-fA-F]{1,4}$ ]] || return 1
        value=$((16#$group)); bits=$((10#$plen - i * 16))
        if ((bits <= 0)); then
            value=0
        elif ((bits < 16)); then
            value=$((value & ((65535 << (16 - bits)) & 65535)))
        fi
        printf -v group '%x' "$value"
        joined+="${joined:+:}$group"
    done
    printf '%s\n' "$joined"
}

rail_validate_profile() {
    # Check the complete explicit input before any guest routing changes.
    local topology=$1 nics=$2 nets=$3 ips=$4 prefixes=$5 ip6s=$6 prefixes6=$7
    local i type net family previous="" value
    local -a types=() ids=() addresses=() lengths=() addresses6=() lengths6=()
    [[ $topology == shared || $topology == isolated ]] || \
        { rail_error "unknown rail topology '$topology'"; return 1; }
    for value in "$nics" "$nets" "$ips" "$prefixes" "$ip6s" "$prefixes6"; do
        [[ $value != ,* && $value != *, && $value != *,,* ]] || \
            { rail_error 'rail arrays cannot contain empty entries'; return 1; }
    done
    IFS=, read -r -a types <<< "$nics"; IFS=, read -r -a ids <<< "$nets"
    IFS=, read -r -a addresses <<< "$ips"; IFS=, read -r -a lengths <<< "$prefixes"
    IFS=, read -r -a addresses6 <<< "$ip6s"; IFS=, read -r -a lengths6 <<< "$prefixes6"
    ((${#types[@]} >= 2 && ${#types[@]} <= 8)) || \
        { rail_error 'an explicit profile needs 2..8 rails'; return 1; }
    ((${#ids[@]} == ${#types[@]} && ${#addresses[@]} == ${#types[@]} && \
        ${#lengths[@]} == ${#types[@]})) || \
        { rail_error 'rail ID and IPv4 arrays must match the NIC count'; return 1; }
    if [[ -n $ip6s || -n $prefixes6 ]]; then
        ((${#addresses6[@]} == ${#types[@]} && ${#lengths6[@]} == ${#types[@]})) || \
            { rail_error 'IPv6 arrays must match the NIC count'; return 1; }
    fi
    for ((i=0; i<${#types[@]}; i++)); do
        type=${types[$i]}; net=${ids[$i]}
        case $type in
            tcp) family=tcp ;;
            softroce) family=o2ib ;;
            *) rail_error "unsupported explicit rail type '$type'"; return 1 ;;
        esac
        if [[ ! $net =~ ^${family}(0|[1-9][0-9]{0,4})$ ]]; then
            rail_error "LNet ID '$net' does not match '$type'"; return 1
        fi
        ((10#${net#"$family"} <= 65535)) || \
            { rail_error "LNet ID '$net' exceeds 65535"; return 1; }
        if [[ $topology == shared && $i -gt 0 && $net != "${ids[0]}" ]]; then
            rail_error 'shared rails must use one LNet network'; return 1
        elif [[ $topology == isolated && ,$previous, == *,$net,* ]]; then
            rail_error 'isolated rails must use distinct LNet networks'; return 1
        fi
        previous+="${previous:+,}$net"
    done
    rail_validate_addresses "$topology" "$ips" "$prefixes" "$ip6s" "$prefixes6"
}

rail_validate_addresses() {
    # Require distinct addresses and the subnet relationship promised by the profile.
    local topology=$1 i j network host first_network="" first_prefix="" candidate other_network
    local -a addresses=() prefixes=() addresses6=() prefixes6=() networks=() hosts6=()
    IFS=, read -r -a addresses <<< "$2"; IFS=, read -r -a prefixes <<< "$3"
    IFS=, read -r -a addresses6 <<< "$4"; IFS=, read -r -a prefixes6 <<< "$5"
    for ((i=0; i<${#addresses[@]}; i++)); do
        network=$(rail_ipv4_network "${addresses[$i]}" "${prefixes[$i]}") || \
            { rail_error "invalid IPv4 address '${addresses[$i]}/${prefixes[$i]}'"; return 1; }
        networks+=("$network")
        if [[ $topology == shared && $i -gt 0 && \
            $network/${prefixes[$i]} != "${networks[0]}/${prefixes[0]}" ]]; then
            rail_error 'shared IPv4 rails must use one subnet'; return 1
        fi
        for ((j=0; j<i; j++)); do
            if [[ ${addresses[$i]} == "${addresses[$j]}" ]]; then
                rail_error 'each rail needs a distinct IPv4 address'; return 1
            fi
            if [[ $topology == isolated ]]; then
                candidate=$(rail_ipv4_network "${addresses[$i]}" "${prefixes[$j]}") || return 1
                host=$(rail_ipv4_network "${addresses[$j]}" "${prefixes[$i]}") || return 1
                if [[ $candidate == "${networks[$j]}" || $host == "$network" ]]; then
                    rail_error 'isolated IPv4 rail subnets overlap'; return 1
                fi
            fi
        done
        [[ -n $4 ]] || continue
        network=$(rail_ipv6_network "${addresses6[$i]}" "${prefixes6[$i]}") || \
            { rail_error "invalid IPv6 address '${addresses6[$i]}'"; return 1; }
        host=$(rail_ipv6_network "${addresses6[$i]}" 128) || return 1
        if [[ $topology == shared && $i -gt 0 && \
            $network/${prefixes6[$i]} != "$first_network/$first_prefix" ]]; then
            rail_error 'shared IPv6 rails must use one subnet'; return 1
        fi
        for ((j=0; j<i; j++)); do
            if [[ $host == "${hosts6[$j]}" ]]; then
                rail_error 'each rail needs a distinct IPv6 address'; return 1
            fi
            if [[ $topology == isolated ]]; then
                candidate=$(rail_ipv6_network "${addresses6[$i]}" "${prefixes6[$j]}") || return 1
                other_network=$(rail_ipv6_network "${addresses6[$j]}" "${prefixes6[$j]}") || return 1
                if [[ $candidate == "$other_network" ]]; then
                    rail_error 'isolated IPv6 rail subnets overlap'; return 1
                fi
                candidate=$(rail_ipv6_network "${addresses6[$j]}" "${prefixes6[$i]}") || return 1
                if [[ $candidate == "$network" ]]; then
                    rail_error 'isolated IPv6 rail subnets overlap'; return 1
                fi
            fi
        done
        hosts6+=("$host"); first_network=$network; first_prefix=${prefixes6[$i]}
    done
}

rail_read_routes() {
    # A missing FIB table is empty; any other query failure is a checked error.
    local family=$1 table=$2 body
    if body=$(LC_ALL=C ip -o "$family" route show table "$table" 2>&1); then
        ((${#body} <= 8192)) || return 1
        printf '%s' "$body"
    elif [[ $body == "Error: ipv${family#-}: FIB table does not exist."$'\n''Dump terminated' ]]; then
        return 0
    else
        rail_error "cannot inspect table $table: $body"
    fi
}

rail_table_matches() {
    # Accept only the connected route and terminal default made for this source.
    local family=$1 ifname=$2 addr=$3 network=$4 plen=$5 body=$6 entry candidate source
    local -a entries=() words=()
    [[ -n $body ]] || return 0
    IFS=$'\n' read -r -d '' -a entries <<< "$body" || true
    ((${#entries[@]} <= 2)) || return 1
    for entry in "${entries[@]}"; do
        read -r -a words <<< "$entry"
        entry=${words[*]}
        if [[ $entry == 'prohibit default'* ]]; then
            # IPv6 includes its loopback reject device and default metric.
            [[ $family == -4 && ${#words[@]} == 2 ]] && continue
            [[ $family == -6 && $entry == 'prohibit default dev lo metric 1024 pref medium' ]] && continue
            return 1
        fi
        [[ ${words[1]:-} == dev && ${words[2]:-} == "$ifname" ]] || return 1
        if [[ $family == -4 ]]; then
            [[ $entry == "$network/$plen dev $ifname scope link src $addr" || \
               $entry == "$network/$plen dev $ifname scope link src $addr linkdown" ]] || return 1
        else
            # Compare IPv6 values after expansion because iproute2 compresses zeroes.
            [[ ${words[3]:-} == src && ${words[5]:-} == metric && \
               ${words[6]:-} == 1024 && ${words[7]:-} == pref && ${words[8]:-} == medium ]] || return 1
            [[ ${words[0]} == */"$plen" && ( ${#words[@]} == 9 || \
               ( ${#words[@]} == 10 && ${words[9]} == linkdown ) ) ]] || return 1
            candidate=$(rail_ipv6_network "${words[0]%/*}" "$plen") || return 1
            source=$(rail_ipv6_network "${words[4]}" 128) || return 1
            [[ $candidate == "$network" && $source == "$addr" ]] || return 1
        fi
    done
}

rail_claim_table() {
    # Refuse unmarked state and source changes before claiming a reserved table.
    local family=$1 idx=$2 ifname=$3 addr=$4 network=$5 plen=$6
    local table=$((200 + idx)) pref=$((12000 + idx * 2)) routes rules marker expected
    local state_dir=${RAIL_STATE_DIR:-/run/ltvm-rail-routing}
    routes=$(rail_read_routes "$family" "$table") || return 1
    expected="v1|$family|$idx|$ifname|$addr|$network/$plen"
    marker=$state_dir/ipv${family#-}-$idx
    if [[ -e $marker || -L $marker ]]; then
        # Markers are data, never shell input; reconfiguration needs a new guest.
        [[ -f $marker && ! -L $marker && -O $marker ]] || return 1
        [[ $(<"$marker") == "$expected" ]] || \
            { rail_error "rail ownership marker differs: $marker"; return 1; }
        rail_table_matches "$family" "$ifname" "$addr" "$network" "$plen" "$routes" || \
            { rail_error "foreign routes in table $table"; return 1; }
        return 0
    fi
    [[ -z $routes ]] || { rail_error "unmanaged routes in table $table"; return 1; }
    for pref in "$pref" "$((pref + 1))"; do
        rules=$(ip -N "$family" rule show priority "$pref") || return 1
        [[ -z $rules ]] || { rail_error "unmanaged rules at priority $pref"; return 1; }
    done
    [[ ! -L $state_dir ]] || return 1
    (umask 077; mkdir -p "$state_dir") || return 1
    [[ -d $state_dir && -O $state_dir ]] || return 1
    # noclobber prevents a concurrent setup from replacing an ownership marker.
    (umask 077; set -o noclobber; printf '%s\n' "$expected" > "$marker") || return 1
}

rail_rule_matches() {
    # Match the complete rule, including source, before authorizing its deletion.
    local entry=$1 pref=$2 action=$3 table=$4 addr=$5 family=$6 source host_prefix=32
    local -a words=()
    read -r -a words <<< "$entry"
    [[ ${words[0]:-} == "$pref:" && ${words[1]:-} == from ]] || return 1
    [[ $family != -6 ]] || host_prefix=128
    source=${words[2]:-}
    [[ $source != */* || $source == */"$host_prefix" ]] || return 1
    if [[ $family == -4 ]]; then
        [[ ${source%/*} == "$addr" ]] || return 1
    else
        source=$(rail_ipv6_network "${source%/*}" 128) || return 1
        [[ $source == "$addr" ]] || return 1
    fi
    if [[ $action == lookup ]]; then
        [[ ${#words[@]} == 5 && ${words[3]} == lookup && ${words[4]} == "$table" ]]
    else
        # ip -N prints FR_ACT_PROHIBIT as 8 instead of its action name.
        [[ ${#words[@]} == 4 && ( ${words[3]} == prohibit || ${words[3]} == 8 ) ]]
    fi
}

rail_clear_rule() {
    # Validate all matching rules before deleting any of the bounded set.
    local family=$1 pref=$2 action=$3 table=$4 addr=$5 body entry
    local -a entries=()
    body=$(ip -N "$family" rule show priority "$pref") || return 1
    ((${#body} <= 8192)) || { rail_error 'owned rule output is too large'; return 1; }
    [[ -n $body ]] || return 0
    IFS=$'\n' read -r -d '' -a entries <<< "$body" || true
    ((${#entries[@]} <= 8)) || { rail_error 'too many rules at an owned priority'; return 1; }
    for entry in "${entries[@]}"; do
        rail_rule_matches "$entry" "$pref" "$action" "$table" "$addr" "$family" || \
            { rail_error "foreign rule at priority $pref"; return 1; }
    done
    for entry in "${entries[@]}"; do
        if [[ $action == lookup ]]; then
            ip "$family" rule del priority "$pref" from "$addr" lookup "$table" || return 1
        else
            ip "$family" rule del priority "$pref" from "$addr" prohibit || return 1
        fi
    done
}

rail_guard_source() {
    # Keep an exact source prohibit in place before addresses or routes can change.
    local family=$1 idx=$2 addr=$3 network=$4 plen=$5 ifname=$6 body entry
    local table=$((200 + idx)) pref=$((12001 + idx * 2)) host_prefix=32
    local -a entries=()
    rail_claim_table "$family" "$idx" "$ifname" "$addr" "$network" "$plen" || return 1
    body=$(ip -N "$family" rule show priority "$pref") || return 1
    ((${#body} <= 8192)) || { rail_error 'owned rule output is too large'; return 1; }
    if [[ -n $body ]]; then
        IFS=$'\n' read -r -d '' -a entries <<< "$body" || true
        ((${#entries[@]} <= 8)) || { rail_error 'too many rules at an owned priority'; return 1; }
        for entry in "${entries[@]}"; do
            rail_rule_matches "$entry" "$pref" prohibit "$table" "$addr" "$family" || \
                { rail_error "foreign rule at priority $pref"; return 1; }
        done
    else
        # Never remove this rule during setup; it blocks main after any later error.
        [[ $family != -6 ]] || host_prefix=128
        ip "$family" rule add priority "$pref" from "$addr/$host_prefix" prohibit || return 1
    fi
}

rail_clear_routes() {
    # Require source-specific ownership before changing a reserved rule or table.
    (($# == 6)) || { rail_error 'routing cleanup needs a complete rail profile'; return 1; }
    local family=$1 idx=$2 addr=$3 network=$4 plen=$5 ifname=$6
    local table=$((200 + idx)) pref=$((12000 + idx * 2))
    [[ $family == -4 || $family == -6 ]] && [[ $idx =~ ^[1-8]$ ]] || return 1
    rail_guard_source "$family" "$idx" "$addr" "$network" "$plen" "$ifname" || return 1
    rail_clear_rule "$family" "$pref" lookup "$table" "$addr" || return 1
    # Create the table first because a missing IPv6 table can make flush fail.
    ip "$family" route replace prohibit default table "$table" || return 1
    ip "$family" route flush table "$table" || \
        { rail_error "cannot clear routing table $table"; return 1; }
}

rail_install_routes() {
    # Pin the source to its NIC and stop lookup before main if that route fails.
    local family=$1 ifname=$2 addr=$3 plen=$4 idx=$5 network=$6
    local table=$((200 + idx)) pref=$((12000 + idx * 2)) host_prefix=32
    [[ $family != -6 ]] || host_prefix=128
    if [[ $family == -6 ]]; then
        addr=$(rail_ipv6_network "$addr" 128) || return 1
    fi
    rail_clear_routes "$family" "$idx" "$addr" "$network" "$plen" "$ifname" || return 1
    ip "$family" route replace "$network/$plen" dev "$ifname" \
        src "$addr" table "$table" || return 1
    ip "$family" route replace prohibit default table "$table" || return 1
    # The terminal rule remains installed across every reset and checked failure.
    ip "$family" rule add priority "$pref" from "$addr/$host_prefix" \
        lookup "$table" || return 1
}

rail_configure_nic() {
    # Configure one prevalidated rail; every required operation must succeed.
    local ifname=$1 addr=$2 plen=$3 addr6=$4 plen6=$5 idx=$6 network network6='' source6=''
    [[ $ifname == "eth$idx" && $idx =~ ^[1-8]$ ]] || \
        { rail_error 'invalid rail interface or index'; return 1; }
    network=$(rail_ipv4_network "$addr" "$plen") || return 1
    rail_guard_source -4 "$idx" "$addr" "$network" "$plen" "$ifname" || return 1
    if [[ -n $addr6 ]]; then
        # Both guards must exist before this call assigns either data address.
        network6=$(rail_ipv6_network "$addr6" "$plen6") || return 1
        source6=$(rail_ipv6_network "$addr6" 128) || return 1
        rail_guard_source -6 "$idx" "$source6" "$network6" "$plen6" "$ifname" || return 1
    fi
    rail_sysctl "net/ipv4/conf/$ifname/rp_filter" 0 || return 1
    rail_sysctl "net/ipv4/conf/$ifname/arp_ignore" 1 || return 1
    rail_sysctl "net/ipv4/conf/$ifname/arp_announce" 2 || return 1
    if [[ -n $addr6 ]]; then
        # Disable RA before link-up so no extra global IPv6 address can appear.
        rail_sysctl "net/ipv6/conf/$ifname/accept_ra" 0 || return 1
        rail_sysctl "net/ipv6/conf/$ifname/autoconf" 0 || return 1
        rail_sysctl "net/ipv6/conf/$ifname/accept_dad" 0 || return 1
    fi
    ip link set "$ifname" up || return 1
    ip -4 address replace "$addr/$plen" dev "$ifname" || return 1
    rail_install_routes -4 "$ifname" "$addr" "$plen" "$idx" "$network" || return 1
    if [[ -n $addr6 ]]; then
        # Static IPv6 addresses must be ready before LNet starts.
        ip -6 address replace "$addr6/$plen6" dev "$ifname" nodad || return 1
        rail_install_routes -6 "$ifname" "$addr6" "$plen6" "$idx" "$network6" || return 1
    fi
}

rail_configure_globals() {
    # Disable strict reverse lookup and ARP replies from the wrong interface.
    local scope
    for scope in all default; do
        rail_sysctl "net/ipv4/conf/$scope/rp_filter" 0 || return 1
        rail_sysctl "net/ipv4/conf/$scope/arp_ignore" 1 || return 1
        rail_sysctl "net/ipv4/conf/$scope/arp_announce" 2 || return 1
    done
}
