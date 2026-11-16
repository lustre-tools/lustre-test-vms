#!/bin/bash
# Lustre reads over each transport on an ltvm cluster with one server
# (MGS+MDS+OSS) and two clients.  The file is read once to fill the OSS
# page cache; after that the clients drop their own caches and read it
# again, so the reads measure the network path through the whole Lustre
# stack (client readahead keeps many RPCs in flight).
#
# usage: lustre-bench.sh CLUSTER LUSTRE_TREE OUTFILE TRANSPORT...
#   TRANSPORT: o2ib (shmrdma on eth0), shmnet (tcp on eth1), tcp (eth0)
# LTVM overrides the ltvm command; SIZE_MB the file size.
set -u
cl=$1 tree=$2 out=$3
shift 3
LTVM=${LTVM:-ltvm}
SRV=$cl-srv C1=$cl-c1 C2=$cl-c2
SIZE_MB=${SIZE_MB:-768}

rate() { awk '{for (i = 1; i <= NF; i++) if ($i ~ /\/s$/) print $(i-1), $i}'; }

for t in "$@"; do
	case $t in
	o2ib) nets='o2ib(eth0)' ;;
	shmnet) nets='tcp(eth1)' ;;
	tcp) nets='tcp(eth0)' ;;
	esac
	for v in $SRV $C1 $C2; do
		ssh $v "echo 'options lnet networks=\"$nets\"' > /etc/modprobe.d/lnet.conf"
	done
	$LTVM cluster deploy $cl --build $tree --mount > /tmp/lb-deploy-$t.log 2>&1 ||
		{ echo "$t,deploy failed"; continue; }
	echo "$t,mount,$(ssh $C1 'mount -t lustre | cut -d" " -f1')"
	ssh $C1 "lfs setstripe -c -1 -S 4M /mnt/lustre/f1 &&
		dd if=/dev/zero of=/mnt/lustre/f1 bs=4M count=$((SIZE_MB / 4)) conv=fsync 2>&1 | tail -1" |
		sed "s/^/$t,write-fsync,/"
	ssh $C1 "echo 3 > /proc/sys/vm/drop_caches; dd if=/mnt/lustre/f1 of=/dev/null bs=4M 2>&1 | tail -1" |
		rate | sed "s/^/$t,read-fill-oss-cache,/"
	for rep in 1 2 3; do
		ssh $C1 "echo 3 > /proc/sys/vm/drop_caches; dd if=/mnt/lustre/f1 of=/dev/null bs=4M 2>&1 | tail -1" |
			rate | sed "s/^/$t,read-1client,/"
	done
	for rep in 1 2; do
		for c in $C1 $C2; do ssh $c "echo 3 > /proc/sys/vm/drop_caches"; done
		ssh $C1 "dd if=/mnt/lustre/f1 of=/dev/null bs=4M 2>&1 | tail -1" > /tmp/lb-r1 &
		ssh $C2 "dd if=/mnt/lustre/f1 of=/dev/null bs=4M 2>&1 | tail -1" > /tmp/lb-r2 &
		wait
		echo "$t,read-2clients,$(rate < /tmp/lb-r1) + $(rate < /tmp/lb-r2)"
	done
	ssh $C1 "rm -f /mnt/lustre/f1"
done | tee -a $out
