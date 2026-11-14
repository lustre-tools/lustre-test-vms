# shmrdma: RDMA and a switch between VMs through host memory

ltvm VMs on one host normally talk through a virtio-net NIC on a Linux
bridge, with QEMU moving every packet in userspace. shmrdma replaces
the wire with the host's memory: one daemon, `shmrdmad`, maps the RAM
of every VM attached to it and moves data between guests with a single
memcpy. On Sookie (dual EPYC 7551) LNet selftest moves 25 GB/s between
two VMs over it, against 1 GB/s over the bridge.

There are two ways in:

- **`--shmrdma`**, an RDMA device that looks like a RoCEv2 HCA.
  o2iblnd, rdma_cm and the kernel verbs run on it unchanged, and LNet
  comes up on `o2ib` over it. An RDMA WRITE is one memcpy from the
  sender's pages straight into the receiver's registered pages.
- **`--nic shmnet`**, a virtio-net NIC on a switch inside the daemon
  instead of the bridge. socklnd, TCP and ssh run unchanged; the guest
  configures it as a `tcp` NIC. TSO and checksum offload pass through,
  so 64 KB segments are never cut up or summed, but the guest's own TCP
  receive copy remains, so it is two copies per byte against RDMA's one.

Sources: `shmrdma/` (`daemon/`, `kmod/`, `include/`, `tools/`, and the
benchmark scripts in `bench/`).

## Using it

```bash
ltvm create co1-a --shmrdma
ltvm cluster create co2 --shmrdma mgs+mds+oss:co2-srv:3 client:co2-c1
ltvm cluster deploy co2 --build <tree> --mount     # Lustre over o2ib
ltvm create co1-b --nic shmnet                      # a NIC on the switch
ltvm vm set co1-a --shmrdma none                    # drop it (stopped VM)
ltvm create co1-c --shmrdma --host-node 2           # on host NUMA node 2
ltvm vm shmrdmad [status|start|stop]
```

- **The daemon.** ltvm builds `shmrdmad` with the host's C compiler
  (cached under `~/.cache/ltvm/shmrdmad/<source hash>/`) and runs one
  per user, started when a VM that needs it starts. Its sockets, pid
  file and log are in `/opt/qemu-vms/shmrdma-<uid>/`, a 0700 directory
  of the user's (or `~/.local/state/ltvm/shmrdma/` when that cannot be
  used), so it survives logout, and a QEMU running as the user or as
  root can reach it. The sockets are 0600: they hand over guest RAM,
  so no other user may connect, and ltvm only trusts a daemon that runs
  as the user from the binary its pid file names. It runs in a systemd
  user scope of its own, like the guests. Under `sudo ltvm` it uses the
  user's running daemon but never starts one as root.
  `LTVM_SHMRDMAD_ARGS` passes options (`-w` workers, `-c` copiers, `-p`
  idle spin in us).
- **Placement.** On a host with several NUMA nodes, where the VMs and
  the daemon run matters more than anything else: on Sookie (8 nodes),
  two VMs and the daemon on one node took a single shmnet TCP stream
  from 9 to 27-39 Gbit/s (vhost-net from 15 to 26).  `--host-node N`
  (create, cluster create, vm set) binds a VM's memory to host node N
  and runs its QEMU on that node's CPUs (the memory through QEMU's
  host-nodes= when its QEMU has libnuma, else through numactl, else by
  the pinned vCPUs touching it first); `[shmrdma] nodes = N[,M]` in
  /etc/ltvm.conf, or `LTVM_SHMRDMAD_NODES`, pins the daemon to those
  nodes' CPUs when ltvm starts it.  Nothing is placed automatically.
- **shmnet queues.** A shmnet NIC has one queue pair unless given
  `queues=N` (up to 8): `--nic shmnet:queues=4`.  More queues spread
  many TCP streams over the guest's vCPUs (the daemon picks a frame's
  RX queue by flow hash) and help multi-stream iperf3, but on Sookie
  they slowed socklnd with conns_per_peer=4 from 2.7-3.3 to 2.0-2.4
  GB/s, so the default stays one.
- **It is never restarted behind a VM's back.** QEMU's vhost-user
  devices do not reconnect, so a daemon restart breaks every attached
  guest's device until that VM restarts. `ltvm vm shmrdmad stop`
  refuses while running VMs use it (`--force` overrides), and a daemon
  built from older sources keeps running until stopped; `status` says
  so. `ltvm doctor` reports running VMs whose daemon is gone.
- **A daemon of your own.** `--shmrdma SOCKET` and `--nic
  shmnet:SOCKET` connect to a daemon you run yourself
  (`shmrdmad -s RDMA_SOCKET -n NET_SOCKET`); ltvm does not start it,
  and a start refuses while nothing listens there.
- **The guest module** is built per kernel in the target's build
  container and baked into the image (`/lib/modules/<kver>/extra/`,
  with `softdep virtiofs pre: shmrdma` in modprobe.d), so it is there
  at boot. It builds for the rocky9 (5.14-rhel9.x) and rocky10
  (6.12-rhel10.x) kernels. Where it does not build, the image build
  warns and goes on, and `create --shmrdma` refuses that image. Fetched
  images predate it: rebuild with `ltvm build image <target>`.
- **LNet.** `--shmrdma` adds `fc_shmrdma=1` to the guest's command
  line, and the boot-time lnet.conf emitter then puts eth0 on `o2ib`
  (the device takes its GIDs from eth0's addresses), extra NICs keeping
  their own nets. `cluster deploy` reads the MGS's lnet.conf, so
  NETTYPE and the MGS NID follow. lnet.conf is written at first boot
  only, so after `vm set --shmrdma` on an existing VM, remove
  `/etc/modprobe.d/lnet.conf` in the guest and reboot it.
  Don't put shmrdma and a shmnet NIC in one LNet config by hand:
  LNet treats the two nets on one peer as multi-rail and spreads
  traffic over both, which runs at the slower one's pace.
- **Memory.** Guest RAM is a shared memfd, which KSM cannot merge, so
  where `/etc/ltvm.conf` sets `[memory] overcommit`, a shmrdma or
  shmnet VM counts against the limit at overcommit times its size.
  Shared memfd memory also gets no transparent huge pages unless the
  host's `shmem_enabled` allows them.

## Requirements

`ltvm doctor --shmrdma [TARGET] [--kernel K]` checks everything these
features need and `--fix` sets up what it can.  `create` and `start`
refuse a VM whose features lack one, naming what is missing and that
command; they never build anything themselves.

| requirement | needed by | fixed by `doctor --fix` |
|---|---|---|
| a C compiler (shmrdmad is built on the host) | shmrdma, shmnet | installs gcc |
| QEMU with vhost-user-fs-pci | shmrdma | no: `sudo ltvm setup --qemu --force` |
| QEMU with memory-backend-memfd | shmrdma, shmnet | no: as above |
| QEMU with the vhost-user netdev | shmnet | no: as above |
| a private runtime directory for the daemon | shmrdma, shmnet | creates it |
| taskset | --host-node | installs util-linux |
| QEMU with libnuma, or numactl (optional: pinned vCPUs place most memory anyway) | --host-node | installs numactl |
| TARGET's image built with the guest module (a fetched one is not) | shmrdma | `ltvm build image` |
| TARGET's kernel built with SMC (optional: without it SMC falls back to TCP) | SMC | `ltvm build kernel` with `--lustre-tree`, then the image |

`ltvm install` installs gcc and numactl with its other prerequisites.
The daemon's node list (`[shmrdma] nodes`), when set, is checked too.

## SMC sockets over shmrdma

SMC-R moves a TCP connection's data over RDMA once both ends agree on
it in the TCP handshake, and it runs over the shmrdma device unchanged,
as over any RoCE HCA.  So any TCP program can use shmrdma, with no
change to it.  ltvm kernels are built with `CONFIG_SMC`, images carry
smc-tools, and a `--shmrdma` VM's boot loads `smc`, adds the SMC user
EID `LTVM-SHMRDMA`, and sets `net.smc.wmem` and `net.smc.rmem` to 1 MB
(`shmrdma/guest/shmrdma-boot.sh`).  SMC v2 off s390 connects two peers
only when they share a user EID; without one every connection falls
back to TCP.

```bash
smc_run iperf3 -s                       # smc-tools: TCP sockets become SMC
LD_PRELOAD=./smc_preload.so iperf3 -c HOST
smcss                                   # SMC connections, and which fell back
smcr -d stats                           # counts, including fallbacks
```

`smc_preload.so` is built from `shmrdma/tools/smc_preload.c` (the
build line is in its header), for images without smc-tools (Debian
13 has none; the boot hook adds the EID with `tools/smc_ueid.py`
there).  Socket buffers stay at or below 16 MB, because SMC-R
registers each as one memory region and the device takes up to 4096
pages; 1 MB measured best for LNet bulk, 4 MB was slower.

On Sookie, between two VMs:

| | TCP (bridge) | SMC over shmrdma |
|---|---|---|
| iperf3, 1 stream | 18.5 Gbit/s | 34 to 56 Gbit/s |
| iperf3, 4 streams | 15.8 Gbit/s | 70 to 91 Gbit/s |
| LNet bulk, conns_per_peer=4 | 1.2 to 1.5 GB/s | 4.1 to 4.6 GB/s |
| LNet ping, 1 in flight | 1,100/s | 6,300 to 8,200/s |

No connection fell back to TCP.  Two caveats:

- **LNet over SMC needs a Lustre patch that is not in Lustre**: an
  lnet `socket_smc=1` parameter that makes socklnd open its sockets
  as SMC.  It is a local commit (branch socklnd-smc in the shmrdma
  Lustre worktree), not posted.  o2iblnd straight over the device is
  faster still (above), so this matters for testing socklnd.
- **A kernel bug.** When an SMC connection falls back to TCP, an
  in-kernel `kernel_accept()` of it dereferences NULL in
  `smc_accept_dequeue` (upstream too).  socklnd's acceptor hits it;
  userspace programs do not.  A fix exists but has not been sent
  anywhere, and ltvm kernels do not carry it, so make sure peers share
  the EID (the boot hook does) before running socklnd over SMC.

## Architecture

```
 guest A                          host                          guest B
 o2iblnd / rdma_cm                shmrdmad                      o2iblnd
 shmrdma.ko ---- vhost-user ----> maps A's RAM and B's RAM <--- shmrdma.ko
   SQ, RQ, CQ rings in A's RAM    worker threads poll rings      rings in B's RAM
   doorbell: virtqueue kick       copier threads split big copies
   interrupt: event virtqueue     memcpy A's pages -> B's pages
```

**Guest memory.** QEMU backs guest RAM with `memory-backend-memfd,
share=on` and passes the memfd to the daemon in vhost-user
`SET_MEM_TABLE`. The daemon translates guest physical addresses itself.

**The device.** A vhost-user virtio device with three virtqueues:
control commands (create QP, modify QP, ...), events (one interrupt per
batch of completion events), and doorbells (kicks only). QEMU 8.2 and
9.2 both have the generic `vhost-user-device-pci`, but refuse to create
it from the command line, so the device is a `vhost-user-fs-pci` with
the tag `shmrdma`. The guest driver claims only that tag, and loads
before virtiofs so that it sees the device first.

**Data path.** Send and receive queues and completion queues are rings
in guest memory, as on a real HCA. The guest writes a work request and
bumps a producer index. The daemon's worker threads poll those indices
while busy. When a worker has been idle for 200 us (`-p`) it sets
`need_kick` in each of its QPs and sleeps, and the guest then rings the
doorbell virtqueue after posting (the usual Dekker handshake with full
fences on both sides). Each worker has the doorbell eventfds of the VMs
it serves in its own epoll, edge-triggered, so a kick wakes it
directly. Completions are written straight into the CQ ring. When a CQ
is armed, the daemon appends the CQ number to an event queue in guest
memory and sends one interrupt through the event virtqueue.

**Connection management is plain RoCEv2.** The device is bound to a
netdev (eth0 by default), so the RDMA core builds the GID table from
its IP addresses and reports each GID to the daemon through `add_gid`.
rdma_cm's MADs go over QP1 as UD sends. The daemon routes them by
destination GID and writes the 40-byte RoCEv2 network header (an IPv4
header with a valid checksum) in front of the payload, which is what
the CM needs to build its reply path. When rdma_cm moves an RC QP to
RTR it supplies the peer's GID and QPN, and that pair is the connection
from then on.

**Memory registration.** FastReg (`IB_WR_REG_MR`) is executed in send
queue order: the daemon copies the page list at that point. The local
DMA lkey is 0 and means "this address is a guest physical address".
o2iblnd only uses RDMA WRITE, SEND and FastReg, but RDMA READ, SEND
with immediate/invalidate and WRITE with immediate are implemented too.

**Copies.** A single 1 MB copy on one thread runs at that thread's
memcpy speed: about 10 GB/s on an i7-14700 and only 3 to 5 GB/s on an
EPYC 7551. Copies of 256 KB or more are therefore split into parts that
copier threads run in parallel. Copies of 64 KB or more use
non-temporal stores, since the destination is another guest's buffer
that will not be in cache.

**Failure handling.** A send to a QP that is gone or not connected
completes with `RETRY_EXC_ERR` and moves the QP to error, which is how
o2iblnd notices a dead peer. A send that finds no receive posted waits
(RNR) and fails after 2 s unless the QP asked for infinite RNR retry.
Moving a QP to error flushes its outstanding work requests, which
o2iblnd waits for before destroying the connection. A guest reset or VM
exit tears down that VM's state in the daemon, with all worker threads
stopped.

**shmnet** is the same daemon on a second socket. QEMU uses `-netdev
vhost-user` with a standard `virtio-net-pci` (mergeable RX buffers,
TSO4/6, checksum offload). The daemon learns MACs from transmitted
frames, floods broadcasts and unknown destinations, and copies each
frame with its virtio-net header into the destination's RX buffers.
When the destination has no RX buffers it holds the sender's TX queue
for up to 2 ms before dropping, which keeps TCP from losing segments to
a guest that is merely slow to refill.

### Why these choices

- **RoCE, not iWARP or a new LND.** rdma_cm on RoCE needs a GSI QP and
  UD sends with a synthesized header, which is a few hundred lines, and
  in return o2iblnd, rdma_cm and ib_cm run exactly as they do on real
  RoCE hardware. A custom LND would be less code but would test nothing
  that runs in production.
- **Rings in guest memory, not virtqueues for work requests.**
  Virtqueues would cost a descriptor chain per work request and could
  not be polled by the daemon without the guest's cooperation. Rings
  the daemon polls make the busy path syscall-free and exit-free.
- **shmnet and SMC-R, not SMC-D, for sockets.** SMC-R needs no new
  device: it runs over shmrdma.  SMC-D would need a virtual ISM
  device, and in these kernels an ISM device can only register from
  the s390 driver.  shmnet needs no guest change at all and speeds up
  every protocol, at the cost of the guest TCP stack.

### Limits

- One host; no live migration.
- The daemon can read and write all guest memory of the VMs attached
  to it. That is acceptable only because every VM is the user's own.
- Kernel verbs only (no userspace verbs, so no perftest); no SRQ,
  atomics or memory windows.
- x86_64 hosts: the daemon uses AVX2 non-temporal stores when the CPU
  has them.

## Results

### LNet selftest between two VMs (Sookie)

Sookie is a dual EPYC 7551 (2.0 GHz Zen 1), nearly idle during the
runs. Two rocky10 VMs (6.12-rhel10.2), 4 vCPUs and 4 GB each. Bulk
tests are `brw` with 1 MB transfers and N RPCs in flight; small tests
are 4 KB `brw` and `ping` (RPCs per second).

| test | TCP today | soft-RoCE | shmnet (TCP) | shmrdma, 1 conn | shmrdma, 4 conns |
|---|---|---|---|---|---|
| write 1M, 1 in flight | 0.36 GB/s | 0.10 GB/s | 0.60 GB/s | 2.0 GB/s | 1.8 GB/s |
| write 1M, 8 in flight | 0.97 GB/s | 0.11 GB/s | 1.4 GB/s | 16.6 GB/s | 17.5 GB/s |
| write 1M, 32 in flight | 0.98 GB/s | 0.12 GB/s | 1.2 GB/s | 17.8 GB/s | **24.5 GB/s** |
| read 1M, 1 in flight | 0.31 GB/s | 0.10 GB/s | 0.48 GB/s | 1.7 GB/s | 1.6 GB/s |
| read 1M, 8 in flight | 1.0 GB/s | 0.11 GB/s | 1.9 GB/s | 10.9 GB/s | 15.9 GB/s |
| read 1M, 32 in flight | 1.1 GB/s | 0.11 GB/s | 1.5 GB/s | 12.2 GB/s | **21.2 GB/s** |
| write 4K, 1 in flight | 1,125/s | 1,301/s | 1,676/s | 9,293/s | 7,340/s |
| write 4K, 32 in flight | 33,223/s | 1,951/s | 40,047/s | 79,761/s | 65,660/s |
| ping, 1 in flight | 1,163/s | 1,299/s | 1,413/s | 8,058/s | 6,754/s |
| ping, 32 in flight | 26,904/s | 1,904/s | 36,490/s | 66,551/s | 72,492/s |

"4 conns" is o2iblnd `conns_per_peer=4`, which spreads a peer's RPCs
over four QPs and so over four daemon workers. Two clients into one
server (16 RPCs in flight each) move 23 GB/s over shmrdma against 1.3
to 1.7 GB/s over TCP. A single stream stays near 2 GB/s whatever the
copy speed, because it is bound by per-message latency: the device adds
about 35 us per one-way message.

The ceiling is how fast the host copies between two guests' memory:
with one worker plus four copiers the daemon has five copy threads,
which on Sookie copy 27 GB/s with non-temporal stores, so 24.5 GB/s is
91% of it. Per byte the daemon is far cheaper in host CPU than the
bridge: 1.6 GB/s per host core against 0.18 for TCP today.

### Lustre (Sookie)

One server (MGS, MDS and OSS) and two clients, a 768 MB file read from
the OSS cache with `dd bs=4M`:

| transport | 1 client | 2 clients at once |
|---|---|---|
| TCP today | 0.93 GB/s | 1.6 GB/s total |
| shmnet | 0.98 GB/s | 1.4 GB/s total |
| shmrdma (o2ib) | 1.55 GB/s | 2.9 GB/s total |

The limit there is the Lustre stack in small guests on 2 GHz cores,
not the transport.

### iperf3 (i7-14700KF, loaded host)

| path | 1 stream | 4 streams |
|---|---|---|
| bridge, as ltvm runs it today | 21 Gbit/s | - |
| bridge with vhost-net | 27 to 37 Gbit/s | 36 to 40 Gbit/s |
| shmnet | 51 to 56 Gbit/s | 61 to 81 Gbit/s |

### Through ltvm (i7-14700KF, loaded host)

`cluster create --shmrdma` with a server and a client (2 vCPUs each),
then `cluster deploy --mount`: the module took its device at boot,
lnet.conf came up as `o2ib0(eth0)`, the client mounted
`192.168.100.230@o2ib:/lustre`, and lnet_selftest wrote 15.7 GB/s (1 MB,
8 in flight) between the two.  Two 2-vCPU shmnet VMs on `--host-node 0`
with two queue pairs each ran iperf3 at 37.7 Gbit/s with two streams.

### Correctness

- lnet_selftest with `check=full` in both directions: no errors.
- A 256 MB file written on one client and read on the other over o2ib:
  checksums match.
- `sanity` on a three-node o2ib cluster: 396 passed, 3 failed from the
  cluster's shape (small OSTs, too few MDTs), none from the transport.
- Guest reboot (the device is reset under a running QEMU) and VM
  restart: the daemon drops the old state and re-initializes, and o2ib
  connections come back.

### Benchmark scripts

`shmrdma/bench/bench-matrix.sh A B OUT LABEL TRANSPORT...` runs the
LNet selftest matrix between two VMs (`shm`, `rxe`, `tcp`, `lo`,
`snet`), `lustre-bench.sh` the Lustre reads, `tables.py` turns the CSVs
into the tables above, and `shmrdma/tools/memcpy_bw.c` measures the
host's copy ceiling.
