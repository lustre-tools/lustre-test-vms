#!/bin/bash
# POWER_DOWN and POWER_UP for the test framework on an ltvm cluster,
# installed on every node by `ltvm cluster deploy`.
# Usage: ltvm-power off|on NODE
#
# A guest cannot cut its own power, and the framework runs these from a
# client, not from the host.  "off" crashes NODE with kdump unloaded, so
# it stops at once with no flush, as a power cut does.  The kernel then
# reboots it after LTVM_POWER_OFF_SECS: a fence that reboots the node.
# The time must be longer than the 10 s in which shutdown_node_hard
# expects the node to stop answering ping.  "on" waits for ssh.
#
# "off" logs NODE on the node that runs it, which drives the suite, so
# `ltvm suite status` can tell this reboot from a crash.
set -u

action=${1:-}
node=${2:-}
off_secs=${LTVM_POWER_OFF_SECS:-30}
power_log=/root/ltvm-suite/power
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no)

case $action in
off)
	mkdir -p "$(dirname "$power_log")"
	echo "$(date +%s) $node" >> "$power_log"
	# The crash runs in the background, so ssh returns before the
	# node stops.
	ssh "${ssh_opts[@]}" "$node" "kexec -p -u 2>/dev/null
		sysctl -qw kernel.sysrq=1 kernel.panic=$off_secs
		nohup sh -c 'sleep 1; echo c > /proc/sysrq-trigger' \
			> /dev/null 2>&1 < /dev/null &"
	;;
on)
	for ((i = 0; i < 180; i++)); do
		ssh "${ssh_opts[@]}" "$node" true 2> /dev/null && exit 0
		sleep 5
	done
	echo "ltvm-power: $node did not come back in 900 s" >&2
	exit 1
	;;
*)
	echo "usage: ltvm-power off|on NODE" >&2
	exit 2
	;;
esac
