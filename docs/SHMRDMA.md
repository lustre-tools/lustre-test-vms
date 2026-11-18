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
- **`--nic shmrdma`**, both at once: a NIC on the switch with an RDMA
  device bound to it, so a VM can have several RDMA ports, each with
  its own address, the way a multi-rail o2ib node does.
- **`--nic shmnet`**, a virtio-net NIC on a switch inside the daemon
  instead of the bridge. socklnd, TCP and ssh run unchanged; the guest
  configures it as a `tcp` NIC. TSO and checksum offload pass through,
  so 64 KB segments are never cut up or summed (unless the receiving
  NIC has `tso=off`, below), but the guest's own TCP
  receive copy remains, so it is two copies per byte against RDMA's one.

Sources: `shmrdma/` (`daemon/`, `kmod/`, `include/`, `tools/`, and the
benchmark scripts in `bench/`).

## Using it

```bash
ltvm create co1-a --shmrdma
ltvm cluster create co2 mgs+mds+oss:co2-srv:3 client:co2-c1 --shmrdma
ltvm cluster deploy co2 --build <tree> --mount     # Lustre over o2ib
ltvm create co1-b --nic shmnet                      # a NIC on the switch
ltvm vm set co1-a --shmrdma none                    # drop it (stopped VM)
ltvm create co1-c --shmrdma --host-node 2           # on host NUMA node 2
ltvm vm shmrdmad [status|start|stop]
ltvm create co1-d --nic shmrdma:net=o2ib0 --nic shmrdma:net=o2ib0
ltvm vm link co1-d eth2 down                        # its switch port dies
ltvm vm link co1-d eth1 set --delay 2 --rate 10000  # 2 ms, 10 Gbit/s
ltvm vm link co1-d                                  # what each link has
ltvm vm link co1-d eth2 up
```

- **The daemon.** ltvm builds `shmrdmad` with the host's C compiler
  (cached under `~/.cache/ltvm/shmrdmad/<hash>/`, the hash of its
  sources, flags and the host's arch and libc, and rebuilt when the
  compiler found differs) and runs one
  per user, started when a VM that needs it starts. Its sockets, pid
  file and log are in `/opt/qemu-vms/shmrdma-<uid>/`, a 0700 directory
  of the user's (or `~/.local/state/ltvm/shmrdma-<host>/` when that
  cannot be used), so it survives logout, and a QEMU running as the user or as
  root can reach it. The sockets are 0600: they hand over guest RAM,
  so no other user may connect, and ltvm only trusts a daemon that runs
  as the user from the binary its pid file names. A QEMU run through
  sudo is not held back by the mode, so ltvm also refuses a socket that
  is not the VM owner's (root's only for a VM root owns), and on a
  classic host, where QEMU
  runs as root, a socket root cannot reach (a root-squashed NFS home
  holding the fallback directory). It runs in a systemd
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
- **Normal-sized packets.** By default a TSO frame crosses the switch
  whole and the receiving guest takes up to 64 KB as one frame, as if
  its NIC did perfect receive offload.  `--nic shmnet:tso=off` turns
  receive TSO off for that NIC (`guest_tso4`, `guest_tso6` and
  `guest_ecn` off on its virtio-net device), and the switch then cuts
  each TCP frame for it into MSS-sized segments, as a NIC without LRO
  delivers them; GRO in the guest merges them again unless `ethtool -K
  eth1 gro off`.  Use it to check that a socklnd change does not win
  only with 64 KB frames.  It costs throughput: on Sookie a TSO-off
  receiver took one iperf3 stream from 15-39 to 12-13 Gbit/s (7 with
  GRO off), and LNet bulk writes into it (1 MB, 8 in flight) from
  1.5-2.2 to 1.1 GB/s (0.5 GB/s with GRO off).
- **Switch counters.** The daemon rewrites `<net socket>.stats` about
  once a second: one line per port, `key=value`, with its MAC and
  negotiated offloads, packets and bytes each way, drops, and the RX
  starvation counters: `rx_holds` (sends that found no RX buffers on
  this port and held the sender), `rx_hold_ns` (time held),
  `rx_hold_drops` (frames dropped when a hold ran out), `tx_holds` and
  `tx_hold_ns` (this port as the held sender), `rx_segs` (segments cut
  for it), `rx_seg_drops` (GSO frames it could not be given) and
  `rx_drops` (frames for it that it was not given, flooded copies
  included).  A
  sender that looks idle in a profile while its peer's `rx_hold_ns`
  climbs is waiting for the peer to post RX buffers.
- **It is never restarted behind a VM's back.** QEMU's vhost-user
  devices do not reconnect, so a daemon restart breaks every attached
  guest's device until that VM restarts. `ltvm vm shmrdmad stop`
  refuses while running VMs use it (`--force` overrides), and a daemon
  built from older sources keeps running until stopped; `status` says
  so. `ltvm doctor` reports running VMs whose daemon is gone or was
  replaced since they started.
- **A daemon of your own.** `--shmrdma SOCKET` and `--nic
  shmnet:SOCKET` connect to a daemon you run yourself
  (`shmrdmad -s RDMA_SOCKET -n NET_SOCKET`, with its link fault
  control socket at `RDMA_SOCKET.ctl` unless `-C` names another or
  `-C ''` turns it off); ltvm does not start it,
  and a start refuses while nothing listens there.  A socket path must
  be shorter than 108 bytes (104 on macOS) and hold no `,`, `|`, `:`,
  `=` or blanks.
  A start also refuses a socket another user could replace before QEMU
  connects: every directory on its path, symlinks followed, must be
  root's or the VM owner's and not writable by others unless sticky
  (`/tmp` itself is fine, a shared group-writable directory is not),
  and so must every symlink on it, since its owner can repoint it.
- **The guest module** is built per kernel in the target's build
  container and baked into the image (`/lib/modules/<kver>/extra/`,
  with `softdep virtiofs pre: shmrdma` in modprobe.d), so it is there
  at boot. It builds for every target's kernel, EL8's 4.18 through
  mainline. A kernel with virtiofs built in (Debian's) binds the device
  first, which the softdep cannot prevent, so the boot hook moves it
  to shmrdma. Where the module does not build, the image build warns
  and goes on, and `create --shmrdma` refuses that image. Fetched
  images predate it: rebuild with `ltvm build image <target>`.  An
  image cross-built for another arch (`--arch aarch64` on an x86_64
  host) never has it, since the module builds only in a native build
  container: build that image on a host of its own arch.
- **LNet.** `--shmrdma` adds `fc_shmrdma=1` to the guest's command
  line, and the boot-time lnet.conf emitter then puts eth0 on `o2ib`
  (the device takes its GIDs from eth0's addresses). An extra tcp or
  softroce NIC (a shmnet NIC included) joins LNet only when its spec
  names a net (`--nic shmnet:net=tcp1`): unnamed, it would reach the
  same peers as a second rail, and LNet then sends the traffic
  addressed to it over o2ib. `cluster deploy` reads the MGS's
  lnet.conf, so NETTYPE and the MGS NID follow; for a single VM,
  `deploy-lustre` and `ltvm llmount` read its own and write them in an
  LNet block of cfg/local.sh (the stock file says tcp; without
  `--mount`, a deploy that cannot work the net out keeps the stock file
  and warns). lnet.conf is
  written at first boot only, so after `vm set --shmrdma` on an
  existing VM, remove `/etc/modprobe.d/lnet.conf` in the guest and
  reboot it.
- **Memory.** Guest RAM is a shared memfd, which KSM cannot merge, so
  where `/etc/ltvm.conf` sets `[memory] overcommit`, a shmrdma or
  shmnet VM counts against the limit at overcommit times its size.
  Shared memfd memory also gets no transparent huge pages unless the
  host's `shmem_enabled` allows them.

## Several RDMA ports, and breaking links

### `--nic shmrdma`

Each `--nic shmrdma` adds a shmnet NIC (ethN) and an RDMA device
bound to it: the device is a vhost-user-fs with the tag
`shmrdma@ethN`, and the guest driver takes its GIDs from ethN rather
than eth0.  IP traffic to that address (ARP, ping, ssh) goes through
the daemon's switch and RDMA through the device, so both share the
link's faults below.  The guest puts each on o2ib; name one net for
all of a node's NICs (`net=o2ib0`) to make them rails of one LNet
network, as `--nic softroce` NICs are.  They can be mixed with
`--shmrdma`, which keeps eth0's device.

### `ltvm vm link`

shmrdmad is the wire between its VMs, so it can do to a link what a
switch port or a cable does.  `ltvm vm link VM NIC ACTION` sends a rule
to its control socket (`rdma.sock.ctl`, beside its other sockets):

| action | what it does |
|---|---|
| `down` | The link is gone.  RC sends over it wait as on a real HCA retrying and complete with `RETRY_EXC_ERR` once the QP's retries would have run out (4.096 us << `timeout`, `retry_cnt` + 1 times, the values the guest set on the QP; `--retry MS` overrides).  UD sends (the CM's MADs) and frames are dropped, so connects time out.  The VM's own NIC also loses carrier and its RDMA port goes down, as when its switch port is disabled, unless `--silent`: a fault further into the fabric leaves the carrier up. |
| `set` | Replace the link's faults with `--delay MS` (one way, added to each message), `--loss PCT` (frames and UD sends dropped; an RC message is sent again after the QP's ack timeout, and fails like a dead link once its retries run out), `--rate MBIT` (each way) and `--down`.  The carrier follows `--down`, as with `down` and `up`, unless `--silent`. |
| `up` | Clear every fault and raise the carrier. |
| `show` (no action) | The faults on each of the VM's links, with counts of what they dropped, lost and failed. |

A transfer sees the faults of both its ends: down at either, the
delays and losses added, and the sender's and receiver's rates.  A
flooded frame (broadcast, multicast, unknown unicast) is the
exception: each copy is dropped for its receiver's down, loss or rate,
but not held for the receiver's delay.  A shmnet frame held for its
receiver's delay or rate holds the sender's frames behind it in that
queue, to every receiver.  A shmnet port is known by its MAC once it
has sent a frame, so a rule by MAC reaches frames for it only after
that.  A rule names a link by IP,
MAC or both, and replaces any rule naming the same IP or MAC.  A
link is the VM's NIC: an `--nic shmrdma` or shmnet NIC by its MAC (the
RDMA device by its link-local GID too), eth0's `--shmrdma` device by
its address.  eth0's carrier never changes, since ssh runs over it.
`--carrier-only` drops just the carrier of any extra NIC, bridge NICs
included, where the daemon carries nothing; passthrough NICs have no
carrier QEMU controls.

Rules live in the daemon: they survive a guest reboot and go when the
daemon stops.  A daemon from before link faults has no control socket;
`ltvm vm shmrdmad status` says when it predates the sources.

The driver tells the daemon a QP's `timeout` and `retry_cnt` (a mask
bit older daemons ignore); with an older guest module the daemon
assumes rdma_cm's RoCE default, timeout 17 (0.54 s) and 7 retries.
Rate limits are token buckets refilled continuously, so a big message
may overdraw one and the next waits; at rates near the copy speed the
limit is loose.  Delay is per message, not per byte.

## Requirements

`ltvm doctor --shmrdma [TARGET] [--kernel K]` checks everything these
features need and `--fix` sets up what it can.  `create` and `start`
refuse a VM whose features lack one, naming what is missing and that
command; they never build anything themselves.

| requirement | needed by | fixed by `doctor --fix` |
|---|---|---|
| a C compiler (shmrdmad is built on the host) | shmrdma, shmnet | installs gcc |
| QEMU with vhost-user-fs-pci | shmrdma | Linux: no, `sudo ltvm setup --qemu --force`; macOS: installs ltvm's QEMU |
| QEMU with memory-backend-memfd (memory-backend-shm on macOS) | shmrdma, shmnet | as above |
| QEMU with the vhost-user netdev | shmnet | as above |
| a private runtime directory for the daemon | shmrdma, shmnet | creates it |
| taskset (Linux) | --host-node | installs util-linux |
| QEMU with libnuma, or numactl (Linux; optional: pinned vCPUs place most memory anyway) | --host-node | installs numactl |
| TARGET's image built with the guest module (a fetched one is not) | shmrdma | `ltvm build image`; not a cross-arch image, which never has it |
| TARGET's kernel built with SMC (optional: without it SMC falls back to TCP) | SMC | `ltvm build kernel` with `--lustre-tree`, then the image |

`ltvm install` installs gcc and numactl with its other prerequisites.
The daemon's node list (`[shmrdma] nodes`), when set, is checked too.

### macOS (Apple Silicon)

Homebrew's QEMU has no vhost-user, so ltvm publishes its own
qemu-system-aarch64 for Apple Silicon (GitHub release
`qemu-macos-<version>`, built by `shmrdma/macos/build-qemu.sh`, which
lists the configure flags and the one QEMU patch).  `ltvm install` and
`ltvm doctor --fix --shmrdma` download it, check it against the sha256
pinned beside `QEMU_MACOS_VERSION` in `ltvm_pkg/host_setup.py`, unpack it
under `/opt/qemu/macos-<version>` and point
`/opt/qemu/bin/qemu-system-aarch64` at it; Homebrew's QEMU stays for
x86_64 guests and `qemu-img`.  It carries the few Homebrew libraries
it loads, so it runs without them.  Guest RAM is POSIX shared memory
(`memory-backend-shm`) instead of a memfd, and the daemon runs on
kqueue and pipes (`shmrdma/daemon/plat_macos.h`).  `--host-node` does
not apply: macOS has no NUMA nodes.  Intel Macs keep Homebrew's QEMU
and cannot use these features.

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
the tag `shmrdma` (eth0's `--shmrdma` device) or `shmrdma@IFNAME` (an
`--nic shmrdma` device, bound to that NIC). The guest driver claims
only those tags, and loads before virtiofs so that it sees the device
first. A device whose NIC has not appeared yet defers its probe; if
virtiofs takes it meanwhile, the boot hook gives it back.

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

A work request is retired in three stores, in this order: its consumer
index (`sq_cons` or `rq_cons` in the QP record) frees its slot, then
its completion, if it has one, becomes visible in the CQ, then the
record's `sq_done` or `rq_done` catches up.  So a ULP that refills
from its completion handler always finds the slot free.  Before the
first store the daemon writes the request's `wr_id`, its opcode and
the CQ index its completion goes to into the record as well, since the
slot may hold a new request once it is freed.  A guest whose daemon
died between those stores finds `done` one behind `cons`, and flushes
that request from the record unless the CQ's producer already covers
its completion.  The guest and the daemon must have the same
`SHMRDMA_ABI_VERSION`: the daemon refuses INIT from any other.

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
that will not be in cache.  shmnet copies frames of 16 KB or more the
same way, and a 64 KB frame with more queued behind it on its TX queue
is split between the worker and one copier, then published once both
halves are done, so a flow's frames stay in order.  On Sookie that
took socklnd with conns_per_peer=4 from 2.6-3.8 to 3.6-4.1 GB/s and
four iperf3 streams from 28 to 42 Gbit/s.

**Failure handling.** A send to a QP that is gone or not connected
completes with `RETRY_EXC_ERR` and moves the QP to error, which is how
o2iblnd notices a dead peer. A send that finds no receive posted waits
(RNR) and fails after 2 s unless the QP asked for infinite RNR retry.
Moving a QP to error flushes its outstanding work requests, which
o2iblnd waits for before destroying the connection. A guest reset or VM
exit tears down that VM's state in the daemon, with all worker threads
stopped.  Pausing the VM (QMP `stop`) only stops its device, and `cont`
resumes it with every QP, CQ and MR as it was; a peer sending to it
meanwhile waits as on a receiver not ready, so a pause longer than 2 s
fails the peer's QPs unless their `rnr_retry` is 7.  A CQ that
overflows raises a CQ error event and moves its QPs to error, and
every later completion it loses fails the QPs on it again.

**When the daemon stops answering.** A control command with no reply
after 30 s makes the guest reset its own device.  QEMU stops the
device in the daemon and waits for its reply, which comes only once the
daemon has stopped using that VM's memory, then forwards the reset, on
which the daemon drops every QP, CQ and MR of the VM.  So once the
reset returns the daemon no longer touches the guest's memory.  The
price is that the reset holds QEMU until the daemon answers: a daemon
that is stopped or stuck freezes the whole VM, every vCPU and its ssh
included, until it answers again, and on x86 a freeze that long trips
the guest's watchdog, which panics it into kdump once the daemon comes
back.  A daemon that has died has closed its socket, and QEMU fails the
reset at once instead of waiting.  The guest then completes everything outstanding, and
anything posted later, with a flush error in software (as mlx5 does
after an internal error), reports the port down, and raises a fatal
device event, so ULP teardown such as `lnetctl net del` finishes.  The
device stays dead until the module is reloaded or the VM reboots.  A
daemon that is merely paused costs nothing until a control command
times out: data traffic needs none.

Nothing tells the guest that the daemon has gone: QEMU sends no notice
of a closed socket, so the guest finds out only at its next control
command.  Traffic in flight just stops, and with o2iblnd that command
usually comes when its transmit timeout (60 s) tears the connection
down, so the device is reported dead about 90 s after the daemon went.
Unload LNet before shmrdma, as `lustre_rmmod` does: o2iblnd cannot let
a device go under it, and removing shmrdma first leaves both stuck in
`cma_remove_one`.

**shmnet** is the same daemon on a second socket. QEMU uses `-netdev
vhost-user` with a standard `virtio-net-pci` (mergeable RX buffers,
TSO4/6, checksum offload). The daemon learns MACs from transmitted
frames, floods broadcasts and unknown destinations, and copies each
frame with its virtio-net header into the destination's RX buffers.
A GSO frame passed on whole must be TCP over the IP version its header
names, with a partial checksum at the TCP header, or it is dropped,
since the receiving kernel trusts that header.  RX buffers must be 256
bytes or more (Linux posts 1518 or more); a smaller one breaks the
queue.
A destination without receive TSO gets a TCP GSO frame as MSS-sized
segments instead: headers copied per segment, IPv4 length, id and
header checksum, IPv6 payload length and TCP sequence fixed up, FIN
and PSH on the last segment only, CWR on the first only, and the TCP
checksum left to the guest as a partial checksum when it took
`GUEST_CSUM`, computed in full otherwise.  Every segment's RX buffers
are taken before any is handed over, so a frame is delivered whole or
not at all.  Other GSO frames for such a port are dropped and counted.
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
- The VMs on one daemon trust each other as hosts on one Ethernet
  segment do.  A GID goes to the first device that adds it, so a guest
  can take another VM's address-based GID before that VM boots or
  while it resets; the other VM's add then fails, and RDMA CM traffic
  for that address reaches the first guest.  No memory is exposed by
  it: remote access still needs a QP the victim connected and an MR it
  registered.
- A worker copies one work request whole while holding its lock, and
  the control plane (every VM's start, stop, reset and memory table
  change) waits for every worker.  A single multi-GB SEND or RDMA
  WRITE/READ therefore stalls the control plane for all VMs on the
  daemon for the length of its copy, 0.1 to 0.7 s at 2 GB.
- Kernel verbs only (no userspace verbs, so no perftest); no SRQ,
  atomics or memory windows.  A move to SQD (send queue drain) is
  refused; `ib_drain_qp` and the ULPs ltvm runs go through ERR.
- The module claims virtio-fs devices and gives back every one whose
  tag is neither `shmrdma` nor `shmrdma@IFNAME` only after virtio has negotiated its features
  and marked the device failed, so virtiofs then starts on a failed
  device.  ltvm VMs have no real virtiofs share, so this does not
  arise in them.
- x86_64 hosts: the daemon uses AVX2 non-temporal stores when the CPU
  has them.  aarch64 hosts use memcpy: on an Apple M5 no NEON or
  non-temporal variant beat it (`tools/memcpy_bw.c`).

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

### LNet selftest between two VMs (Mac, Apple M5)

Two rocky10-16k guests (2 vCPUs, 3 GB) under HVF, ltvm's macOS QEMU,
`lst-bench.sh`, 10 s per test, no errors:

| test | shmrdma (o2ib) | shmnet (tcp) |
|---|---|---|
| ping, 1 in flight | 11,100/s | 9,500/s |
| 4 KB write, 1 in flight | 11,900/s | 10,200/s |
| 1 MB write, 8 in flight | 36 GB/s; 13.9 with check=full | 9.3 GB/s (check=full) |
| 1 MB read, 8 in flight | | 11 GB/s (check=full) |

Per-message rates match Sookie's, and bulk is higher: the M5 copies
about 50 GB/s per thread, and with both of the daemon's workers busy
the o2ib run is bound by the daemon and the guests, not memory.  With
check=full the guests' checksums are the limit.  shmnet is several
times faster than on Sookie because the guests' TCP stack is not the
bottleneck on these cores.

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
