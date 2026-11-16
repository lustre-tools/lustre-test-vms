#!/bin/bash
# Run one lnet_selftest test from this node and print the server stats.
# usage: lst-bench.sh FROM_NIDS TO_NIDS read|write|ping SIZE CONC SECS
set -u
from=$1 to=$2 rw=$3 size=$4 conc=$5 secs=${6:-10}
export LST_SESSION=$$
lst new_session --timeout 120 bench > /dev/null
lst add_group c $from > /dev/null
lst add_group s $to > /dev/null
lst add_batch b > /dev/null
if [[ $rw == ping ]]; then
	lst add_test --batch b --concurrency $conc --distribute 1:1 \
		--from c --to s ping > /dev/null
else
	lst add_test --batch b --concurrency $conc --distribute 1:1 \
		--from c --to s brw $rw ${CHECK:+check=$CHECK} size=$size \
		> /dev/null || echo "ADD_TEST_FAILED"
fi
lst run b > /dev/null
sleep 3
echo STAT_START
lst stat --delay $secs --count 1 s 2>&1
lst stop b > /dev/null
lst show_error c s 2>&1 | grep -E "Total|error" | grep -v "Total 0 error"
lst end_session > /dev/null
