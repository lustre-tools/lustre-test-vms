# lustre-test-vms

Build infrastructure for Lustre development and testing using QEMU microVMs.

Produces four independent, cacheable artifacts per target OS:

1. **Build container** -- cross-compilation environment (GCC, e2fsprogs-wc, etc.)
2. **Kernel** -- custom-built kernel + full source build tree for Lustre module builds
3. **VM base image** -- minimal root filesystem for QEMU microvm boot (Lustre baked in)
4. **Lustre staging** -- userland + modules installed to a per-kernel DESTDIR

Multiple kernel versions per target are supported (e.g., Rocky 9.5 and 9.7).

## Quick start

**Download pre-built artifacts (no building):**

```bash
sudo ./ltvm install                              # one-time host setup
./ltvm target fetch rocky9                       # container + kernel + image + Lustre
ltvm create co1-single --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
ltvm llmount co1-single                          # mount Lustre inside the VM
```

**Build everything from scratch:**

```bash
sudo ./ltvm install
ltvm build all rocky9 --lustre-tree ~/lustre-release
ltvm create co1-single --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
ltvm llmount co1-single
```

**Day-to-day iteration (change Lustre, redeploy into a running VM):**

```bash
ltvm build lustre rocky9 --lustre-tree ~/lustre-release   # incremental, fast
ltvm deploy-lustre co1-single --lustre-tree ~/lustre-release --mount
```

## Running on macOS (Apple Silicon)

ltvm runs on an Apple Silicon Mac, building/running aarch64 artifacts in
a podman-managed Linux VM + QEMU microvms. A few things differ from Linux:

- **Do not use `sudo`** for `./ltvm install` on macOS -- run it as your
  user; it prompts for sudo internally only where it needs it (e.g.
  `/opt/qemu`, `/usr/local/bin/ltvm`).
- **VMs need no sudo** after `./ltvm install`, which puts `/opt/qemu-vms`
  in an `ltvm` group and adds you to it (a new terminal may be needed
  before that takes). QEMU runs as you on socket_vmnet, whose socket is
  that group's too -- not `staff`, which an account from a directory
  service is not in -- and a launchd job reloads ltvm's dnsmasq when a VM
  name is added. `ltvm doctor` reports anything missing; an install from
  before 2026-10-21 needs `./ltvm install` once more for the socket.
- **Install Python deps first** with [uv](https://docs.astral.sh/uv/);
  the system Python lacks PyYAML. `./ltvm` auto-uses `.venv/` when present.
- **podman backend is pinned to `applehv`** (Apple's Hypervisor.framework).
  `./ltvm install` forces the applehv machine provider; ltvm has no use for
  the GPU-only libkrun backend.
- **Lustre builds read the kernel build-tree and ccache from podman
  volumes**, not over virtiofs, which halves a build.  The first build
  against a kernel copies its build-tree in (~2 min, once);
  `LTVM_MACOS_BUILD_VOLUMES=0` goes back to bind mounts.  A kernel's
  volume goes with the kernel (`ltvm clean`, `ltvm target clean`), and
  `ltvm doctor` shows what they take inside the podman machine.
- **The podman machine stops after a build** to give its memory
  back, unless another ltvm command is still using it, which costs
  ~12 s and its page cache on the next one.
  `export LTVM_PODMAN_AUTOSTOP=0` keeps it up: a rebuild then takes
  ~8 s instead of ~22 s.
- **To confine an agent to a VM**, run ltvm inside an aarch64 Linux VM
  on the Mac instead: UTM on the Apple Virtualization backend with
  nested virtualization on, which needs an M3 or later and macOS 15+.
  ltvm then gets KVM there; see
  [docs/NESTED_VIRTUALIZATION.md](docs/NESTED_VIRTUALIZATION.md#apple-silicon----aarch64).

```bash
git clone git@github.com:lustre-tools/lustre-test-vms.git
cd lustre-test-vms

brew install uv && uv sync              # Python deps into .venv/
./ltvm install                          # NO sudo -- prompts internally
ltvm doctor                             # sanity-check the host

ltvm target fetch rocky9 --arch aarch64 --kernel 5.14-rhel9.5
ltvm create co1-single --kernel 5.14-rhel9.5   # no sudo once installed
ltvm llmount co1-single                 # mount Lustre inside the VM
```

## Uninstalling

```bash
./ltvm uninstall --dry-run     # what it would remove; changes nothing
./ltvm uninstall               # NO sudo, on Linux too -- prompts internally
```

`ltvm uninstall` takes off the host what `ltvm install` put there: it
destroys the VMs, stops and removes the bridge and dnsmasq (systemd units
on Linux, LaunchDaemons on macOS) with their iptables and ufw/firewalld
rules, ltvm's files in `/etc`, `/opt/qemu`, `/opt/qemu-vms` and the
`ltvm` group, tab completion, the agent skill links, ltvm's config and
state, and last the `ltvm` launcher.  Run it as yourself: the podman
store, artifacts and crash dumps are yours, so it refuses to run under
`sudo` and elevates only the steps that need root.

It asks about the things that hold your data, and keeps each unless you
say otherwise:

| What | Delete | Keep |
|---|---|---|
| `artifacts/` and `~/.cache/ltvm` | `--delete-artifacts` | `--keep-artifacts` |
| `~/ltvm-crashes` | `--delete-crashes` | `--keep-crashes` |
| the podman machine (macOS), and everything in it | `--delete-podman-machine` | `--keep-podman-machine` |
| ltvm's podman images and volumes | `--delete-podman-images` | `--keep-podman-images` |

`--yes` asks nothing and keeps what no `--delete-*` names; without a
terminal, `--yes` is required.  A step that fails is reported and the
rest still run, so running it again finishes the job.  Packages ltvm
installed (Homebrew formulae, distro packages), the kvm group, a setuid
`qemu-bridge-helper` and the checkout itself stay; it lists them at the
end.

## Target OS support

Fetchable = a prebuilt release exists (`ltvm target fetch <target>`).
More kernels can be built locally with `ltvm build all`; see them
with `ltvm target list --all-kernels`.

| Target | Arch | Lustre | Default kernel | Fetchable |
|--------|------|--------|----------------|-----------|
| rocky8 | aarch64, x86_64 | server (ldiskfs) + client | 4.18-rhel8.10 | rhel8.10 (aarch64: built before the kernel config overlay) |
| rocky9 | aarch64 | server (ldiskfs) + client | 5.14-rhel9.7 | rhel9.5, 9.7, 9.8 |
| rocky9 | x86_64 | server (ldiskfs) + client | 5.14-rhel9.7 | rhel9.7, 9.8 |
| rocky10 | aarch64 | server (ldiskfs) + client | 6.12-rhel10.0 | rhel10.0, 10.1 |
| rocky10 | x86_64 | server (ldiskfs) + client | 6.12-rhel10.0 | rhel10.0, 10.1, 10.2 |
| ubuntu2404 | aarch64, x86_64 | client only | 6.8-ubuntu2404 | 6.8-ubuntu2404 |
| rocky9-64k | aarch64 | server (ldiskfs) + client | 5.14-rhel9.7 | no -- build locally |
| rocky10-16k* | aarch64 | server (ldiskfs) + client | 6.12-rhel10.2 | no -- build locally |
| mainline* | x86_64 | server (ldiskfs) + client | see below | no -- build locally |
| ubuntu2604* | aarch64, x86_64 | client only | 7.0-ubuntu2604 | 7.0-ubuntu2604 |
| opensuse156* | aarch64, x86_64 | client only | 6.4-sles15sp7 | 6.4-sles15sp7, sles15sp6 |
| ubuntu2604-server* | aarch64, x86_64 | server (ldiskfs) + client | 7.0.0-14-ubuntu26 | 7.0.0-14-ubuntu26 |
| debian13* | aarch64, x86_64 | server (ldiskfs) + client | 6.12-debian13 | 6.12-debian13 |

`ltvm target fetch` takes the host's arch, or the one a target
declares in targets.yaml (rocky9-64k and rocky10-16k declare aarch64).
The aarch64 releases, all but rocky8's, are cross-built on x86_64,
and a cross-built image carries no IOR, mdtest, iozone or drgn.  On
an x86_64 host an aarch64 VM runs emulated, which `ltvm create` waits
out (a Debian or Ubuntu guest spends minutes in kdump-tools at boot).

`rocky9-64k` is rocky9 with `CONFIG_ARM64_64K_PAGES`, for testing
Lustre against a 64K page size.  It is aarch64-only: on an x86_64 host
it cross-builds, and `ltvm target list` hides it behind
`--all-arches` along with every other non-native target.

`rocky10-16k` (experimental) is rocky10 with `CONFIG_ARM64_16K_PAGES`.
Apple Silicon has only the 4K and 16K translation granules, so
`rocky9-64k` boots there only under `LTVM_FORCE_TCG=1`, while
`rocky10-16k` runs natively under HVF and still exercises Lustre with
`PAGE_SIZE > 4096`.  Mix it with 4K servers in one cluster with
`--node-target`:
`ltvm cluster create co7 rocky9 mgs+mds+oss:co7-srv:4 client:co7-c16k --node-target co7-c16k=rocky10-16k`.

`ubuntu2604` (experimental) is Ubuntu 26.04 with a kernel configured
for nested KVM, nftables, cgroup-BPF and ext4/XFS project quota: a
guest for testing services that run their own VMs and firewalls.

`opensuse156` (experimental) runs SLES 15 kernels (`6.4-sles15sp7`,
`6.4-sles15sp6`) on an openSUSE Leap 15.6 userspace, as a Lustre
client.  Public SLES kernel SRPMs are scarce (Leap's mirrors keep only
SP6's newest few, and SP7 has none), but SUSE tags every released
kernel (`rpm-6.4.0-150700.53.66`) in
[SUSE/kernel](https://github.com/SUSE/kernel), the patched source, and
[SUSE/kernel-source](https://github.com/SUSE/kernel-source), its
patches and configs.  ltvm builds the tag Lustre's `.target` pins with
that tag's `config/x86_64/default`, as SUSE's own kernel spec does.
Leap 15.6 ships SLE 15 SP6's packages for most of its userspace.
Lustre ships no ldiskfs series for SLES past SP5, so run these as
clients against rocky servers, picking SP6 with `--node-kernel`:
`ltvm cluster create co8 rocky9 mgs+mds+oss:co8-srv:4 client:co8-sles --node-target co8-sles=opensuse156 --node-kernel co8-sles=6.4-sles15sp6`.
A cluster can mix Debian-family targets with the others too, though
they install Lustre in different places: `cluster deploy` links one
place to the other on each node.

`ubuntu2604-server` (experimental) is Ubuntu 26.04 as an ldiskfs
server, on the kernel Lustre's `ldiskfs-7.0.0-14-ubuntu26` series is
for: the source is pinned to linux-source 7.0.0-14.14, the kernel is
named 7.0.0-14-lustre, and its build-tree carries the
`UTS_UBUNTU_RELEASE_ABI` line Ubuntu's packaging writes.  Lustre's
configure goes by that line and the ABI to pick an Ubuntu series;
without them it would build the vanilla `ldiskfs-7.0-ml` instead.

`debian13` (experimental) is Debian 13 as an ldiskfs server, built
from Debian's patched kernel source (linux-source 6.12.107, pinned:
the ldiskfs series stops applying at 6.12.111) with Debian's own
kernel config, taken from the linux-image packages of the same upload.
Lustre's configure has no Debian case, so it builds the vanilla
`ldiskfs-6.12-ml` series.

`mainline` (experimental) builds Lustre against vanilla kernel.org
kernels rather than a distro SRPM.  Its `--kernel` takes a release
series (`6.18`), an exact release (`7.2.3`), or one of the moving
aliases `latest` / `stable` / `longterm`.  See
[ltvm_pkg/upstream_kernel.py](ltvm_pkg/upstream_kernel.py).

## ltvm commands

Top-level:

```
ltvm install                    One-time host setup (sudo)
ltvm uninstall                  Undo install (no sudo; asks first)
ltvm update                     git fast-forward ltvm itself
ltvm build      <action> ...    Build artifacts (see below)
ltvm target     <action> ...    Target OS management (see below)
ltvm vm         <action> ...    VM inspection / crash / snapshot (see below);
                                also accepts every VM command below, so
                                `ltvm vm create` == `ltvm create`
ltvm cluster    <action> ...    Multi-node cluster management (see below)
ltvm create     <name>          Create a VM (idempotent; --root-size sets
                                the OS disk, --disk-size the MDT/OST ones,
                                --disk-dir/--disk-cache/--disk-prealloc
                                where and how those are kept,
                                --kernel-args adds boot parameters,
                                --wait SECONDS waits for host memory)
ltvm start|stop|destroy <name>  VM power / removal (start takes --wait)
ltvm list                       Show all VMs
ltvm deploy-lustre <vm>         Deploy Lustre into a running VM
ltvm make-install               Build + install Lustre onto THIS machine
                                (run inside an ltvm VM or cloud node)
ltvm make-uninstall             Remove what make-install put here
ltvm make-reinstall             make-uninstall + make-install
ltvm llmount <vm>               Mount Lustre in a VM
ltvm llumount <vm>              Unmount (same as llmount --cleanup)
ltvm clean                      Prune superseded artifacts and dead podman
                                images, list idle VMs (dry-run by default)
ltvm completion                 Print/install shell tab completion
ltvm doctor                     Host health check (--fix on request)
ltvm skills                     Link the agent skill into ~/.claude/skills
ltvm telemetry  <action> ...    Anonymous check-in: status/show/on/off/send
```

`build` sub-actions:

```
ltvm build all <target>         Container + kernel + Lustre + image
ltvm build container <target>   Rebuild the build container
ltvm build kernel <target>      Kernel (+ --kernel, --lustre-tree)
ltvm build image <target>       Per-kernel VM image (+ --kernel)
ltvm build lustre <target>      Lustre against target kernel (+ --lustre-tree, --kernel)
ltvm build mofed-kmods <t>      Per-kernel MOFED kernel modules
ltvm build zfs <target>         ZFS against the target kernel (see --zfs)
ltvm build shell <target>       Interactive shell in build container
ltvm build status               Staleness table (one row per built kernel)
                                --why names the input that went stale
```

Long builds report where the time went, and ring the terminal bell
when they finish (over a minute, on a TTY):

```
build all rocky9 finished in 41m 51s
  container   1m 03s
  kernel     32m 40s
  lustre      6m 21s
  snapshot       12s
  image       1m 56s
```

`LTVM_NO_BELL=1` silences the bell; `LTVM_NOTIFY_COMMAND` (e.g.
`notify-send ltvm`) is run with the summary as its last argument.

The Lustre, ZFS and MOFED-kmod builds run in a container that podman
stops at a time limit, so a wedged configure fails rather than hanging:
an hour for Lustre, 30 minutes for ZFS and MOFED kmods.  A build
stopped there says so.  A slow or busy host -- such as a Mac whose
podman machine several builds share -- can raise it for one command, or
for every user on the host; `0` means no limit, and the variable wins
over the file:

```bash
LTVM_BUILD_TIMEOUT=7200 ltvm build lustre rocky9 --lustre-tree ~/lustre-release
```

```ini
# /etc/ltvm.conf
[build]
timeout = 7200
```

Kernel and image builds have no limit.

Lustre is configured `--with-o2ib=yes`, building ko2iblnd against the
kernel's own InfiniBand stack, which every kernel gets from the shared
config overlay.  A kernel whose target turns InfiniBand off on purpose
(`CONFIG_INFINIBAND: n` in its `kernels.config`) gets `--with-o2ib=no`,
with a note.  Any other kernel without it -- built before the overlay,
or one the overlay could not change -- is refused, with what to do:
rebuild the kernel, or pass `--configure --with-o2ib=no`.  That flag, or
the same in a target's `configure_args`, always overrides the choice.

`target` sub-actions:

```
ltvm target list                List configured targets + local/remote status
ltvm target show <target>       Detailed view of one target
ltvm target build <target>      Container + kernel + Lustre + image (alias for build all)
ltvm target clean <target>      Remove built artifacts
ltvm target delete <target>     Delete artifacts (local; --remote for GitHub release)
ltvm target validate <target>   Read-only Lustre/kernel compat check
ltvm target fetch <target>      Download latest release tarballs
ltvm target archive <target>    Move to cold storage, leaving a symlink
ltvm target restore <target>    Move it back from cold storage
ltvm target export <target>     Bake a bootable qcow2/raw, or a Google Cloud
                                image (--format gce); no ltvm runtime needed
ltvm target publish <target>    Bundle artifacts and upload to GitHub release
                                (use --no-upload to produce tarballs locally)
```

A fetch pulls several hundred-MB tarballs, so it draws two lines: the
asset in flight, and one bar for the whole set with its rate and ETA.

```
  [3/4] image-rocky9-x86_64-5.14.0-611.55.1.el9_7.tar.zst  190/260 MB   73%
  total [#################################---------------]  69%  320/460 MB   38.9 MB/s  eta     4s
```

Off a TTY (CI, a pipe, a log file) it prints one line per asset instead.

`cluster` sub-actions (each takes `--help`):

```
ltvm cluster create <name> [TARGET] <roles:vm[:disks]> ...   (needs root)
ltvm cluster destroy <name>...  Destroy clusters and every node (root)
ltvm cluster start <name>...    Start every node, as `ltvm start` does
ltvm cluster stop <name>...     Stop every node
ltvm cluster deploy <name>      Build + deploy Lustre to every node
ltvm cluster llmount <name>     Mount Lustre across the cluster
ltvm cluster llumount <name>    Unmount it and unload the modules
ltvm cluster status <name>      Nodes, targets, recovery and client imports
                                (--wait-recovery SECS waits for recovery)
ltvm cluster failover <name> <node> [--how H]   Fault a server, mount on partner
ltvm cluster failback <name> <node>             Restore it, move targets back
ltvm cluster exec <name> <role> <cmd>...   Run on every node with that role
ltvm cluster ssh  <name> <role> Interactive ssh to one node
ltvm cluster list               List all clusters
```

A cluster command takes its single-VM counterpart's flags where they
mean the same thing: `cluster create` takes `--kernel`, `--variant` and
`--wait` as `create` does, and applies them to every node; `cluster
start` takes `--wait`; `cluster destroy`, like `destroy`, never prompts
and accepts `--force`/`--yes` anyway. `cluster llmount` and `cluster
deploy --mount` run llmount.sh from the first client node (the MGS node
when there is no client), the node the generated `cfg/local.sh` treats
as local.

`cluster create --failover PARTNER=PRIMARY` adds a failover partner for
a server node. ltvm creates PARTNER with no data disks of its own. The
QEMU of PARTNER opens the data disks of PRIMARY in the same order. Thus
both guests see each target under one name (`/dev/vdb`, `/dev/vdc`).
Both QEMUs open these disks with `share-rw=on` and `cache=none`.
`cluster deploy` writes PARTNER into `cfg/local.sh` as the
`mds<N>failover_HOST` or `ost<N>failover_HOST` of each target on
PRIMARY. When PRIMARY is the MGS, it also writes `mgsfailover_HOST` and
adds the NID of PARTNER to `MGSNID`. The test framework then formats
these targets with `--failnode`, which turns on ldiskfs MMP, and `fail
mds1` moves MDT0 to PARTNER:

```bash
ltvm cluster create co2 mgs+mds:co2-mds1:1 oss:co2-oss:2 client:co2-c \
    --failover co2-mds2=co2-mds1
ltvm cluster deploy co2 --build ~/lustre-release
ltvm suite run co2 replay-single --only '0a 0b' --wait
```

The single-VM form is `ltvm create A --share-disks`, then `ltvm create
B --disks-from A`. `ltvm destroy A` refuses while B exists, because B
would lose its disks: destroy both together. A primary has at most one
partner. MMP stops a second Lustre mount, but it does not stop a raw
write to the device. On Linux, the VM directory must be on a filesystem
that supports `O_DIRECT`.

On ZFS, mkfs.lustre partitions each disk after the partner booted, and
the partner's kernel does not see the new partitions. Thus `local.sh`
sets `ZPOOL=/usr/sbin/ltvm-zpool`, which rereads the partition table of
a data disk that shows none before a `zpool import`.

A cluster can run `FAILURE_MODE=HARD`. Its `local.sh` sets `POWER_DOWN` and
`POWER_UP` to `/usr/sbin/ltvm-power`. `off` crashes the node with kdump
unloaded, and the kernel reboots it after `LTVM_POWER_OFF_SECS` (30 by
default). `on` waits for ssh:

```bash
ltvm suite run co2 replay-single --only '0a 0b' --env FAILURE_MODE=HARD --wait
```

The crash is close to a power loss, but it is not the same. The guest
loses its page cache, MMP stops, and the node stops answering, as in a
power loss. But the kernel runs its panic path (panic notifiers and
console output) before it stops. A real power off needs the QEMU
process on the host, and the framework calls `POWER_DOWN` from inside
the client guest, which cannot reach QEMU. For a real power loss, use
`ltvm cluster failover --how kill` from the host.

`cluster failover` takes a primary down by one of 5 methods, then
mounts its targets on its partner. It moves the targets with the test
framework on the first client. Thus the framework's record of the
active node stays correct for `cluster failback` and the next failover.
It returns when the mounts finish or fail. It does not wait for
recovery, because in a test recovery may never end. To wait, use
`cluster status --wait-recovery SECS`:

| `--how` | The primary | After the takeover |
|---------|-------------|--------------------|
| `umount` | Unmounts its targets | Runs |
| `kill` | QEMU gets SIGKILL: power loss | Is down |
| `panic` | Crashes; kdump saves a vmcore | Reboots |
| `freeze` | QEMU gets SIGSTOP: a hung node (Linux hosts only) | Resumes (`--hold`: stays frozen) |
| `split-brain` | QMP `set_link off`: still runs, and MMP must refuse the takeover | Gets its link back (`--hold`: stays cut off) |

```bash
ltvm cluster failover co2 co2-mds1 --how kill --json
ltvm cluster status co2 --json --wait-recovery 300
ltvm cluster failback co2 co2-mds1
```

`split-brain` is a check of MMP, not a failover. It exits 0 when MMP
refused every mount on the partner. It exits 1, and names the targets,
when the partner mounted a target that the running primary also holds.

On macOS, `cluster failover` refuses `freeze`. A stopped QEMU reads no
packets, and socket_vmnet then stalls every VM on the host. QMP `stop`
keeps the network up, but it also stops the guest's clock. In a test on
2026-10-19, the resumed node saw no gap, and its MMP did not find the
takeover. Both nodes then kept the targets, on ldiskfs and on ZFS.

`cluster failback` starts, resumes or reconnects the primary as
failover left it. It then moves the targets back from the partner, and
it mounts a target that a failed move left mounted nowhere.

A suite, `llmount.sh` and `llmountcleanup.sh` delete the framework's
record of the active node when they start. They then act on each
primary, while the partner still holds the target. Thus `suite run`,
`cluster llmount`, `cluster llumount` and `cluster deploy --mount`
refuse while a target is on its partner, and they name the `cluster
failback` command to run.

`vm` sub-actions:

```
ltvm vm console-log   <name>    Show QEMU serial log (-f to keep streaming;
                                picks up the new log when the VM reboots)
ltvm vm crash-collect <name>    Pull vmcore + run lustre_triage
ltvm vm exec          <name> CMD  Run a command in the VM (ssh; its exit status)
ltvm vm nmi           <name>    Inject NMI (panic + kdump)
ltvm vm snapshot      <name>    Snapshot overlay disk
ltvm vm restore       <name>    Restore to a snapshot
ltvm vm set           <name>    Change --vcpus/--mem/--numa-nodes/
                                --disk-cache of a stopped VM
```

`vm` also answers for the top-level VM commands -- `create`, `destroy`,
`start`, `stop`, `list`, `deploy-lustre`, `llmount`, `llumount` and
`doctor`.  They were `vm` sub-actions before they were promoted, and
both spellings reach the same parser: `ltvm vm create co1-single` and
`ltvm create co1-single` are one command, flags, help and tab
completion included.

`ltvm create` and `ltvm cluster create` take `--dry-run` (`-n`): they
resolve and validate everything, print what they would make, and write
nothing. Needs no root, so it never prompts for a password.

```
$ ltvm create co1-single rocky9 --dry-run --ost-disks 3
Would create VM: co1-single
  target:  rocky9 (variant=base)
  kernel:  5.14.0-611.13.1.el9_7_lustre
  cpu/mem: 2 vcpus, 2048 MB
  disks:   1 MDT + 3 OST @ 500M each
  root:    8G
  ip:      next free (auto)
Nothing was written.  Re-run without --dry-run to create it.
```

VM names MUST include the checkout number and a descriptive role:
`co<N>-<role>` (e.g. `co1-single`, `co2-mds`, `co2-oss`). Never bare
names like `testvm`.

### Host memory

`create` and `start` refuse a VM when the running guests' memory (each
one's `--mem`) plus the new one would exceed the host's RAM less a
reserve of 1 GiB or 10%, whichever is larger. `--wait SECONDS` waits
for room instead.

That counts every guest at its full size, which overstates what a host
running KSM holds: guests booted from the same image share most of
their pages. An admin can let the running guests commit a multiple of
the budget, for every user on the host:

```ini
# /etc/ltvm.conf
[memory]
overcommit = 2
```

It is 1.0 unless set. A single VM must still fit the budget, and the
host wants swap behind it, because merged pages are copied apart again
as soon as guests write different data to them. The kernel boots with
KSM off; `w /sys/kernel/mm/ksm/run - - - - 1` in a file under
`/etc/tmpfiles.d/` turns it on at every boot.

### Shared host: hot and cold artifact storage

On a host several people use, keep one artifacts tree for everybody,
in two parts: **hot** on fast (SSD) storage for the targets in use --
the base images VM overlays read as backing files, the kernel build
trees every Lustre build reads, the kernel ccache -- and **cold** on
big, slow (HDD) storage for inactive targets and old releases.

```bash
# both owned by the ltvm group, setgid, with a default ACL so that
# everything created inside stays writable by the whole group
sudo install -d -o root -g ltvm -m 2775 /var/cache/ltvm /mnt/data/ltvm
sudo setfacl -m g:ltvm:rwX -m d:g:ltvm:rwX /var/cache/ltvm /mnt/data/ltvm
```

```ini
# /etc/ltvm.conf
[storage]
hot = /var/cache/ltvm
cold = /mnt/data/ltvm
```

`hot` replaces the default `<repo>/artifacts` for every user;
`LTVM_ARTIFACTS_DIR` still wins over it, for one user or one command.
`cold` is optional.  A relative path is ignored with a warning.  `ltvm
doctor` reports both: where each comes from, whether it exists, is
writable by you and is group-shared, and its free space.  A cold
directory that is configured but missing is an issue -- usually an
unmounted disk, so ltvm never creates it.

Archiving is invisible to everything else:

```bash
ltvm target archive rocky8              # every arch; or --arch x86_64
ltvm target restore rocky8
```

`archive` moves `<hot>/rocky8/<arch>` to the same path under `cold` and
leaves a symlink in its place, so builds, `create` and existing VMs keep
finding it (more slowly); `restore` moves it back.  `ltvm build status`
and `ltvm target list` mark archived targets cold and say where they
are (`storage` and `cold_path` under `--json`).  A move between filesystems copies to a
temporary name, checks the copy (names, sizes, nanosecond mtimes, link
targets), switches over, and only then deletes the original; any failure
before that -- a full disk, say -- removes the partial copy and leaves
the original as it was.  Free space and permission to delete the
original are checked before anything is copied.

Both refuse while a VM uses the target -- its `.info` names an `IMAGE`
or `KERNEL` there, or its overlay is backed by an image there -- and
list the VMs.  `--force` goes ahead for **stopped** VMs: on archive they
keep their paths, which now lead through the symlink; on restore any VM
that names the cold path (a `create --image` through the symlink) has
its `.info` rewritten and its overlay rebased (`qemu-img rebase -u`).  A
**running** VM always blocks the move, `--force` or not: stop it first.
`target fetch` refuses an archived target (tar would replace the
symlink); restore it first.  Do not build into a target while it is
being archived.

Why setgid and group-writable, and not the sticky bit that
`/opt/qemu-vms` uses: rebuilding a kernel or image, pruning with `ltvm
clean`, fetching a newer release and archiving all replace or delete
files another member made, which the sticky bit forbids.  Commands that
write artifacts (`build all/container/kernel/image/zfs/mofed-kmods`,
`target build/fetch/clean/delete/archive/restore`, `clean`) leave the group bits out of their umask when the hot or cold
directory is shared, and run build containers with `--umask 0002` and
`CCACHE_UMASK=002`, so this holds even without the default ACL.  VM
commands keep your own umask: an overlay must not be group-writable.

### Running VMs without sudo

`ltvm install` sets a Linux host up so that members of the `ltvm` group
create, start, stop and destroy VMs -- clusters included -- with no root
at all.  QEMU runs as the user and attaches to the VM bridge through
QEMU's setuid `qemu-bridge-helper`; the VM directories under
`/opt/qemu-vms` belong to the group.  The installing user is added to
the group (log in again afterwards); add others with
`sudo usermod -aG ltvm,kvm <user>`.

`ltvm doctor` says whether this works for you.  Without a bridge helper,
or for a VM with a `passthrough` NIC, ltvm falls back to elevating the
individual host operations through sudo.  The sticky bit on the VM
directories stops one member from deleting another's VM files.  A
passthrough VM still runs QEMU as root, so only trusted users belong in
the group on a host that uses passthrough.

An unprivileged QEMU starts in a systemd user scope of its own,
`ltvm-<name>-<time>-<random>.scope` in `ltvm-guests.slice`, when the
user has a systemd manager that outlives the login: linger is on, or
ltvm already runs under the manager.  Without linger the manager stops
at logout, so a guest started from a login stays in the login's
session.  QEMU daemonizes but keeps its caller's cgroup, so a guest
started from a service, or from a tool that runs in a transient scope,
would otherwise be killed whenever that unit is stopped.
`LTVM_GUEST_SCOPE=0` turns it off; a manager that refuses the scope
gets the plain launch.

### Agent/session ownership

Every new VM records an advisory `owner_id` for lifecycle reconciliation.
Controllers should export a durable session ID; normal commands need no new
arguments and fall back to a typed per-invocation process ID:

```bash
export LTVM_OWNER_ID=patch-watcher:session-7f9c
ltvm create co1-single
ltvm list --json                    # each VM has owner_id (or null for legacy)
```

`--owner ID` and `--owner-id ID` override the environment for both `create`
and `cluster create`. See [VM ownership metadata](docs/VM_OWNERSHIP.md) for the
precedence, persistence, cluster propagation, and JSON contracts.

### Claiming VMs

Sessions sharing a host claim the VMs they are using, so one does not
deploy over another's test run:

```bash
ltvm claim co1-single --ttl 4h      # `ltvm claim` alone lists claims
ltvm release co1-single
```

`deploy-lustre`, `llmount`, `start`/`stop`/`destroy`, `vm
snapshot/restore/nmi/crash-collect/set` and the cluster commands refuse a VM
that another live session claimed; `ltvm release --force` breaks a claim.
An agent session's `deploy-lustre` claims an unclaimed VM.  Claims live in
`/opt/qemu-vms/claims` (mode 1777), which `ltvm install` and `ltvm doctor
--fix` create.  See [VM ownership metadata](docs/VM_OWNERSHIP.md#claims).

## Tab completion

`ltvm install` installs tab completion for every shell it finds on the
host -- bash, zsh and fish -- into that shell's system completion
directory. Open a new shell afterwards to pick it up.

```bash
ltvm completion                      # print the code for $SHELL
ltvm completion --shell zsh          # ...or for a named shell
sudo ltvm completion --install       # (re)install system-wide
sudo ltvm completion --uninstall     # remove it
ltvm doctor                          # reports missing/stale; --fix installs
```

To keep it in your own dotfiles rather than system-wide, add
`eval "$(ltvm completion)"` to `~/.bashrc`; for zsh, save
`ltvm completion --shell zsh` as `_ltvm` somewhere on your `fpath`.

Completion is dynamic, not a static word list -- it reads the same
sources the commands do, so it offers your actual targets, VMs,
clusters, kernels and variants:

```bash
ltvm build kernel roc<TAB>              # -> rocky8 rocky9 rocky9-64k rocky10
ltvm build kernel rocky8 --kernel <TAB> # -> only rocky8's kernels
ltvm deploy-lustre co<TAB>              # -> your VMs
ltvm cluster exec co2 <TAB>             # -> that cluster's roles, then nodes
ltvm vm restore co1-single <TAB>        # -> that VM's snapshot tags
```

## Agent skills

`ltvm install` links this repo's agent skill (`skills/ltvm/`) into
`~/.claude/skills`, and into `~/.codex/skills` when Codex is installed, so
Claude Code and Codex know how to drive ltvm. Under `sudo` the links are
made for the invoking user, not root.

```bash
ltvm skills              # link them (also done by `ltvm install`)
ltvm skills --uninstall  # remove the links this checkout made
ltvm doctor              # reports missing links; `--fix` makes them
```

They are symlinks into the checkout, so `git pull` or `ltvm update` keeps
them current. A skill directory of the same name that you wrote yourself
is never replaced.

## Telemetry

ltvm sends an anonymous check-in once a week so we can tell whether
anyone is using it, and which parts. It carries:

| | |
|---|---|
| a random install ID | minted once, not derived from anything about the host |
| the ltvm version | so we know when an old code path can go |
| host facts | OS + version, arch, WSL or not, Python, QEMU, and RAM as a *bucket* rather than an exact number |
| usage counts | which commands ran, against which targets, with which options, and how many of each succeeded or failed |
| who ran them | how many runs came from a terminal (`human`), an AI coding agent (`agent`), or neither (`script`) |

No hostnames, usernames, paths, VM or cluster names, Lustre tree
identity, git branches, environment variables, command lines, or IP
addresses. No error messages and no failure *reasons* -- only counts,
because a reason is a string built where the paths live. Target and
variant names outside the shipped set arrive as `other`, so a target
you added yourself does not name your site. An agent is recognised by
the variables Claude Code, Codex and Gemini CLI set for the commands
they run; only whether they are present is used, never their values.

The server records a *hash* of the source address so distinct networks
can be counted; the address itself is never stored.

```bash
ltvm telemetry show      # the exact payload a check-in would send
ltvm telemetry status    # on/off, install ID, when it last sent
ltvm telemetry off       # opt out
```

`ltvm telemetry show` prints the literal JSON, so what leaves your
machine is something you can check rather than something we assert.

Three ways to turn it off, in precedence order:

| | scope |
|---|---|
| `LTVM_TELEMETRY=0` in the environment | one command or one CI job |
| `/etc/ltvm.conf` with `[telemetry]` / `enabled = false` | every user on the host |
| `ltvm telemetry off` | you |

The site-wide file can only ever disable, and a user cannot override
it -- an opt-out someone could silently undo would not be one.

The first check-in goes out on the first run, alongside the notice.

A host whose ltvm runs each start from an empty home (an agent service
that gives every run a fresh container, say) would otherwise be a new
install on every run. Its admin can give the host one install ID and
one shared schedule in `/etc/ltvm.conf`:

```ini
[telemetry]
# python3 -c 'import uuid; print(uuid.uuid4())', once
install_id = <that uuid>
# optional: last send time and counters, writable by every user here
state_dir = /var/lib/ltvm-telemetry
```

The ID is used as it stands and never copied into a user's config, and
with it set no notice is printed. Only a random (version 4) UUID is
accepted. `state_dir` takes effect only alongside `install_id`; without
it each user keeps their own schedule, as before.

## More

See [CLAUDE.md](CLAUDE.md) for the full developer reference. Agents get
what they need from the `ltvm` skill above, which `ltvm install` links
into their skill directories.

## License

BSD 3-Clause.  See [LICENSE](LICENSE).
