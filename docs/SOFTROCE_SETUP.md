# SoftRoCE (RXE) between ltvm Rocky VMs

How to bring up a working RoCEv2 link between two ltvm-managed Rocky 9 VMs
using the in-kernel `rdma_rxe` software RoCE driver. No HCA required —
RXE runs RDMA verbs over the regular Ethernet NIC.

## What this is good for

- Prototyping and correctness-testing kernel RDMA / o2iblnd code changes
  without needing real Mellanox hardware.
- Exercising the userspace verbs API (`libibverbs`, `librdmacm`) and
  perftest tools end-to-end.
- Bringing up Lustre over o2iblnd against a software RDMA stack for
  development.

## What this is NOT good for

- Measuring anything that depends on PCIe behavior (TLP ordering, root
  complex / IO die effects, RO bit handling, posted-write retirement,
  HCA DMA engines). SoftRoCE never touches the PCIe data path; the
  "RDMA" is implemented entirely in software on top of UDP. The whole
  PCIe Relaxed Ordering story (DDN-6698 etc.) cannot reproduce here.
- Throughput-sensitive benchmarking. Expect single-digit Gb/s, bounded
  by software packet processing through the host bridge and TUN/TAP.

## Steps

### 1. Create two VMs

```bash
sudo ltvm ensure co<N>-rdma-a --vcpus 2 --mem 2048 --os rocky9 \
    --mdt-disks 0 --ost-disks 0
sudo ltvm ensure co<N>-rdma-b --vcpus 2 --mem 2048 --os rocky9 \
    --mdt-disks 0 --ost-disks 0
sudo ltvm list
```

No MDT/OST disks needed unless you also intend to run Lustre on them.
Confirm L2 connectivity with a ping between the two assigned IPs before
going further.

### 2. Install perftest and verbs utilities (both VMs)

```bash
sudo ltvm exec co<N>-rdma-a 'dnf install -y perftest libibverbs-utils'
sudo ltvm exec co<N>-rdma-b 'dnf install -y perftest libibverbs-utils'
```

`rdma-core` (which provides the `rdma` link tool) is already installed
in the Rocky 9 base image.

### 3. Load `rdma_rxe` and create the RXE link (both VMs)

```bash
sudo ltvm exec co<N>-rdma-a 'modprobe rdma_rxe && \
    rdma link add rxe0 type rxe netdev eth0 && rdma link'
sudo ltvm exec co<N>-rdma-b 'modprobe rdma_rxe && \
    rdma link add rxe0 type rxe netdev eth0 && rdma link'
```

Expected:

```
link rxe0/1 state ACTIVE physical_state LINK_UP netdev eth0
```

Verify userspace can see it:

```bash
sudo ltvm exec co<N>-rdma-a 'ibv_devices; ibv_devinfo -d rxe0'
```

You should see `rxe0` listed, port state `PORT_ACTIVE`, link_layer
`Ethernet`. Ethernet link layer means RoCEv2 (encapsulated in UDP/IPv4).

### 4. Smoke-test with perftest

Server on B, client on A. Each test needs the server backgrounded
because `ltvm exec` is synchronous.

```bash
# Server (VM-B) — note `true;` prefix avoids pkill nonzero exit
# propagating; nohup + & detaches; -F skips CPU-frequency check
sudo ltvm exec co<N>-rdma-b 'true; \
    nohup ib_write_bw -d rxe0 -F -D 5 > /tmp/ib.log 2>&1 & echo started'

# Client (VM-A)
sudo ltvm exec co<N>-rdma-a 'ib_write_bw -d rxe0 -F -D 5 <VM-B-IP>'
```

Repeat for `ib_read_bw` and `ib_send_bw`. Typical result on default
1500-byte MTU eth0 (active RoCE MTU = 1024B): ~250 MiB/s for all three
verbs at default message size. The numbers are dominated by SoftRoCE
overhead, not the wire.

### 5. (Optional) Bigger MTU for less-bad numbers

```bash
sudo ltvm exec co<N>-rdma-a 'ip link set eth0 mtu 9000'
sudo ltvm exec co<N>-rdma-b 'ip link set eth0 mtu 9000'
```

Then re-create the rxe link to pick up the new MTU:

```bash
sudo ltvm exec co<N>-rdma-a 'rdma link delete rxe0 && \
    rdma link add rxe0 type rxe netdev eth0'
```

(Same on B.) Active RDMA MTU should now be 4096. The host bridge needs
to support the larger MTU end-to-end; if pings stop working, revert.

## Gotchas

- **`ltvm exec` propagates the last command's exit code.** A trailing
  `pkill -f foo` that finds no match returns 1 and the whole exec
  fails with `unreachable`. Prefix with `true;` or append `|| true`.
- **Background jobs need `nohup ... &`**. Plain `&` under `ltvm exec`
  may get reaped when the exec session closes.
- **One RXE device per VM is enough.** Don't try to add multiple rxe
  links over the same netdev — they'll conflict.
- **Link layer is Ethernet, not InfiniBand**, even though `ibv_devinfo`
  prints `transport: InfiniBand`. That's the verbs transport class;
  what's on the wire is RoCEv2 over UDP.
- **Don't expect RC behavior to match real HCAs at the edges.** SoftRoCE
  implements the verbs spec but performance characteristics, error
  paths, completion timing, and concurrency limits all differ from
  Mellanox/Broadcom silicon. Code that works against rxe may still
  break against real HCAs and vice versa.
