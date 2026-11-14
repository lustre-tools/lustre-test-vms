#!/bin/bash
# Run the LNet selftest matrix between two ltvm VMs over each transport.
# usage: bench-matrix.sh VM_A VM_B OUTFILE LABEL TRANSPORT...
#   TRANSPORT: shm (o2ib over shmrdma0 on eth0), rxe (o2ib1 over rxe on
#   eth1), tcp (socklnd on eth0), lo (A to itself), snet (socklnd on
#   eth1, a shmnet NIC)
# CPP sets conns_per_peer for shm and snet.
# TESTS overrides the test list, SECS the measurement window.
set -u
A=$1 B=$2 out=$3 label=$4
shift 4
here=$(cd "$(dirname "$0")" && pwd)
PERF=${PERF:-/usr/lib/linux-tools-6.8.0-147/perf}
SECS=${SECS:-10}
TESTS=${TESTS:-"write:1M:1 write:1M:8 write:1M:32 read:1M:1 read:1M:8 read:1M:32 write:4K:1 write:4K:32 ping:0:1 ping:0:32"}
SOCK=/opt/qemu-vms/sockets

ip_of() { ssh $1 "ip -4 -o addr show $2" | awk '{print $4}' | cut -d/ -f1; }

# Configure LNet on $1 with only transport $2's net: with several nets
# the peers are discovered as multi-rail and traffic spreads over all.
setup() {
	local v=$1 t=$2 net
	case $t in
	shm) net="o2ib --if eth0${CPP:+ --conns-per-peer $CPP}" ;;
	rxe) net="o2ib1 --if eth1" ;;
	tcp|lo) net="tcp --if eth0" ;;
	snet) net="tcp --if eth1${CPP:+ --conns-per-peer $CPP}" ;;
	esac
	if [[ $t == rxe ]]; then
		# ltvm's softroce NIC sets MTU 4200 in the guest, but the host
		# bridge carries 1500: large RoCE frames are dropped.
		ssh $v 'if [[ $(cat /sys/class/net/eth1/mtu) != 1500 ]]; then
			rmmod lnet_selftest 2> /dev/null; lnetctl lnet unconfigure 2> /dev/null
			ip link set eth1 mtu 1500
			rdma link delete rxe0; rdma link add rxe0 type rxe netdev eth1
		fi'
	fi
	if [[ $t == shm ]]; then
		ssh $v 'lsmod | grep -q ^shmrdma || bash /root/guest-load.sh /root/shmrdma.ko eth0 > /dev/null'
	fi
	ssh $v "rmmod lnet_selftest 2> /dev/null; lnetctl lnet unconfigure 2> /dev/null;
		modprobe lnet && lnetctl lnet configure && lnetctl net add --net $net &&
		modprobe lnet_selftest"
}

TRANSPORTS=("$@")
for v in $A $B; do
	scp -q "$here/lst-bench.sh" "$here/guest-load.sh" $v:/root/
	[[ -n ${KO:-} ]] && scp -q "$KO" $v:/root/shmrdma.ko
done

pa=$(cat $SOCK/$A.pid) pb=$(cat $SOCK/$B.pid)
pd=$(pgrep -x shmrdmad || true)
[[ -s $out ]] || echo "label,transport,test,size,conc,MiB_s,RPC_s,cpu_vmA,cpu_vmB,cpu_daemon,errors" > $out

for t in "${TRANSPORTS[@]}"; do
	case $t in
	shm) fa="$(ip_of $A eth0)@o2ib" fb="$(ip_of $B eth0)@o2ib" ;;
	rxe) fa="$(ip_of $A eth1)@o2ib1" fb="$(ip_of $B eth1)@o2ib1" ;;
	tcp) fa="$(ip_of $A eth0)@tcp" fb="$(ip_of $B eth0)@tcp" ;;
	lo) fa="$(ip_of $A eth0)@tcp" fb=$fa ;;
	snet) fa="$(ip_of $A eth1)@tcp" fb="$(ip_of $B eth1)@tcp" ;;
	esac
	setup $A $t && setup $B $t || { echo "setup $t failed"; continue; }
	ssh $A "lnetctl ping $fb > /dev/null" || { echo "ping $fb failed"; continue; }
	for tst in $TESTS; do
		IFS=: read rw size conc <<< "$tst"
		tmp=$(mktemp)
		ssh $A "CHECK=${CHECK:-} /root/lst-bench.sh $fa $fb $rw ${size}B $conc $SECS" > $tmp 2>&1 &
		sshpid=$!
		sleep 3.3
		cpu=()
		for p in $pa $pb $pd; do
			sudo $PERF stat -x, -e task-clock -p $p -- sleep $SECS 2> $tmp.perf.$p &
		done
		wait $sshpid
		wait
		for p in $pa $pb $pd; do
			ms=$(awk -F, '/task-clock/ {print $1}' $tmp.perf.$p)
			cpu+=($(awk -v ms=${ms:-0} -v s=$SECS 'BEGIN {printf "%.2f", ms / 1000 / s}'))
			rm -f $tmp.perf.$p
		done
		[[ -z $pd ]] && cpu[2]=0
		bw=$(awk '/Bandwidth/ {f=1} f && /^\[[RW]\]/ {v=$3+0; if (v > m) m = v} END {printf "%.1f", m}' $tmp)
		rpc=$(awk '/Rates/ {f=1} f && /^\[R\]/ {print $3; exit}' $tmp)
		err=$(grep -c -E "error|FAILED" $tmp)
		echo "$label,$t,$rw,$size,$conc,$bw,$rpc,${cpu[0]},${cpu[1]},${cpu[2]},$err" | tee -a $out
		rm -f $tmp
	done
done
