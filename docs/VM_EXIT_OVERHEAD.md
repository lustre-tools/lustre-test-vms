# KVM exit overhead of ltvm guests

What a guest costs the host beyond its own work, and the settings that
change it.  Measured 2026-11-14 on Sookie (dual EPYC 7551, Zen 1,
Ubuntu 24.04, kernel 6.8, QEMU 8.2, `kvm_amd avic=N`) and on a WSL2
desktop (i7-14700KF, nested KVM under Hyper-V, QEMU 9.2).

## Result

An x86_64 KVM guest now boots with `nohz=off` (`qemu_run.KVM_TICK_ARGS`).
The tickless default reprograms the guest's TSC deadline timer on every
idle entry and exit, and each of those is an MSR write KVM traps, so a
guest that sleeps and wakes per RPC pays two exits on every wake-up.
With a periodic tick the timer is written once per tick instead.

On Sookie, pairs of 4-vCPU rocky10 guests, means of two alternating
rounds (the default first):

| guest setting | sanity subset | host CPU-s for it | LNet 4K write x1 | ping x1 | 1M write x8 | idle host CPU per VM |
|---|---|---|---|---|---|---|
| default (tickless) | 251 s | 375 | 1,324 RPC/s | 1,227/s | 1,562 MiB/s | 0.006 |
| `nohz=off` | 196 s | 303 | 1,708 | 1,634 | 1,623 | 0.146 |

The desktop agrees (2-vCPU guests): sanity 211 s to 189 s, 4K x1 4,576
to 7,469 RPC/s.  The cost is idle: 4,000 timer exits and 4,000 halt
wake-ups a second for a 4-vCPU guest, 0.15 of a host CPU where a
tickless guest takes 0.01.  `--kernel-args nohz=on` turns it back off.

On the host, the cpufreq governor matters as much.  A vCPU thread runs
in short bursts between halts, on whichever CPU the scheduler picks, so
schedutil never sees enough load on any one CPU to clock it up.  With
`nohz=off` guests, unpinned, on Sookie's node 6 only:

| node 6 CPUs | sanity subset | LNet 4K write x1 | ping x1 |
|---|---|---|---|
| schedutil (Sookie's default) | 185 s, 298 host CPU-s | 1,858 RPC/s | 1,560/s |
| performance | 152 s, 260 | 3,328 | 3,162 |
| schedutil, C2 disabled | 191 s, 305 | 1,890 | 1,605 |

So `ltvm doctor` reports a load-following governor (schedutil, ondemand,
conservative, or powersave on a driver that uses the generic governors)
and `--fix` sets performance, now and at boot through
`/etc/tmpfiles.d/ltvm-cpufreq.conf`, and `ltvm install` does the same.
`[host] cpufreq = leave` in /etc/ltvm.conf keeps a host's choice and
turns the report into a note.
intel_pstate and amd-pstate-epp in active mode are left alone; they
were not measured.  The C-state made no difference: C2's exit latency
is not what a woken vCPU waits for.

## Where the host CPU goes

The receiving guest of a 1 MB LNet write over TCP (default settings):
3.35 host CPUs, of which

- 1.29 is guest mode, the guest's own work;
- 0.95 is QEMU's userspace I/O threads (virtio-net without vhost);
- 0.67 is KVM halt polling (`halt_poll_ns` 200000);
- 0.42 is exit handling, time from a VM exit to the next entry;
- the rest is the host scheduler putting vCPU threads to sleep and
  waking them.

About 107,000 exits a second: 33,000 TSC deadline writes, 12,000 x2APIC
ICR writes (single-target IPIs, which KVM's PV IPI hypercall does not
cover), 27,000 HLT, 13,000 host interrupts, 7,000 interrupt windows.
An idle tickless guest costs almost nothing (250 exits a second).

## What else was tried

- **Host halt polling.**  KVM's per-VM cap (`KVM_CAP_HALT_POLL`, which
  QEMU does not expose; set here from inside QEMU with gdb) at 0 or
  50 us saves 0.5 to 0.7 host CPU per busy guest, but the sanity subset
  took 298 s instead of 261 s: polling is what keeps a woken vCPU's
  latency down.  Keep the module default.
- **Guest `cpuidle-haltpoll`** (`cpuidle_haltpoll.force=1`): polls in
  the guest and turns host polling off through `MSR_KVM_POLL_CONTROL`.
  Sanity 231 s, but polling now counts as guest time and the host pays
  more for it (393 CPU-s).  With `nohz=off` too, 173 s for 322 CPU-s.
  Not a default: it costs CPU on every busy guest, and x86 KVM only.
- **One host core per vCPU** (vCPU threads pinned, QEMU's other threads
  on the SMT siblings): sanity 223 s for 339 CPU-s, 4K x1 1,354 RPC/s.
  With `nohz=off` as well, 135 s for 231 CPU-s and 3,338 RPC/s: the two
  compound.  The largest gain measured, and what a host-node placement
  option should do.
- **`-overcommit cpu-pm=on`** on top: guest HLT and PAUSE no longer
  exit, and with them the reschedule IPIs.  Sanity 125 s, 4K x1 5,740,
  but each vCPU is a fully busy host CPU, idle or not (4.0 per idle
  4-vCPU guest).  Only for dedicated cores.
- **AVIC** (`kvm_amd avic=1`, a module reload): not tried, since
  Sookie's other VMs hold the module.  Zen 1 has no x2AVIC, so an
  x2APIC guest gets only hybrid AVIC: it would remove the interrupt
  window exits and the IPIs that kick a running vCPU, at most a fifth
  of the exits and about 0.1 host CPU per busy guest.  The TSC deadline
  and ICR writes stay.  It also needs `-global
  kvm-pit.lost_tick_policy=discard`, or the PIT's reinjection inhibits
  it.

## Method

Per QEMU: `perf stat -e task-clock --per-thread -p PID`; vCPU threads
(from QMP `query-cpus-fast`) split into guest mode and host side from
`/proc/PID/task/TID/stat`; halt polling and exit counts from KVM's
per-VM debugfs (`/sys/kernel/debug/kvm/PID-FD/`); exits by reason, MSR
by index and exit-to-entry time from `kvm:kvm_exit`, `kvm:kvm_entry`
and `kvm:kvm_msr` with bpftrace, in a separate window so its overhead
stays out of the CPU numbers.  Workloads: lnet_selftest over socklnd
on eth0 (1 MB write and read at 8 in flight, 4 KB write and ping at
1), the sanity subset `0a` to `27f` (72 tests) on a local llmount, and
two minutes idle.  Settings alternated within one session.
