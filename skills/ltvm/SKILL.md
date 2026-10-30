---
name: ltvm
description: This skill should be used to get a Lustre test VM and put a build on it with ltvm - "create a test VM", "spin up a Lustre VM", "deploy my tree to a VM", "build Lustre for the VM", "mount Lustre and run sanity", "make a two-node cluster", "my VM is hung", "collect the crash from that VM", "the VM kernel does not match my build". Covers VM and cluster lifecycle, deploying a Lustre tree, artifacts, and the root rules.
version: 0.1.0
---

# ltvm: Lustre test VMs

`ltvm` builds and runs QEMU microVMs for Lustre development. Four
cacheable artifacts per target OS -- build container, kernel, VM base
image, and Lustre staging -- each rebuilt only when its inputs change.

This is a test environment built for the purpose. Creating, destroying,
building and mounting are ordinary operations: do them rather than asking
whether to.

## First use on a host

```bash
ltvm doctor                     # is the host set up?
sudo ./ltvm install             # QEMU, bridge, dnsmasq, SSH, ltvm on PATH
ltvm target fetch rocky9        # pre-built artifacts -- much faster than building
ltvm build status               # what is stale
```

On macOS it is `./ltvm install` with no `sudo`: it refuses to run as
root there and prompts for sudo itself where it must (README.md,
"Running on macOS").

`install` also sets up tab completion for the human's shell (bash, zsh,
fish), which needs a new shell before it works -- worth saying when you
walk someone through setup, since nothing else announces it. `ltvm
doctor --fix` installs it on a host that predates it.

`ltvm target fetch` is the fast path, and a fetched image already has
Lustre baked in -- `ltvm create` then `ltvm llmount` gives a mounted
filesystem with no build at all. Build locally only when a target has no
release, or when the kernel or image genuinely needs to change:
`ltvm build all rocky9 --lustre-tree <tree>` rebuilds only what is stale,
`--force` rebuilds everything, and `ltvm build status` shows what is.

When a local container, kernel or image turns stale after `ltvm update`
or a pull, check for a newer release before rebuilding: an update that
moves an input often comes with republished artifacts, and `ltvm target
fetch <target>` takes them in a minute or two where a kernel build takes
from 20 minutes to over an hour. Rebuild locally when no release has
caught up (`ltvm target fetch <target> --dry-run` says what it would
take). A release republished under the same tag (same kernel version,
new config or image) makes fetch stop and ask for `--replace`.

Images are per-kernel. A target with two kernels has an image for each,
so `--kernel <name>` on `build image` and on `create` picks which one,
and creating against a kernel with no matching image fails rather than
booting the wrong modules. A Lustre build lands in the tree's
`.ltvm-staging/<target>/<arch>/<kernel>/`.

## A single VM, from nothing to a mounted filesystem

```bash
ltvm create co1-single --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
ltvm deploy-lustre co1-single --lustre-tree <tree> --mount
ssh co1-single 'lctl dl'
```

```bash
ltvm list                       # running and stopped
ltvm start|stop|destroy co1-single
ltvm doctor [--fix]             # host infrastructure health
```

Every VM command also answers under `vm` -- `ltvm vm create` is `ltvm
create`, and the same for `destroy`, `start`, `stop`, `list`,
`deploy-lustre`, `llmount`, `llumount` and `doctor`.

`stop` (and `cluster stop`) flushes each guest's root filesystem over
ssh before cutting its power, so files written just before survive;
`--force` skips that for a guest that no longer answers.

`--disk-size` sizes the MDT/OST scratch disks; `--root-size` sizes the
VM's own OS disk (default 8G). Both are fixed at create time, so a VM
that needs room for a debug build or a vmcore wants `--root-size 20G`
when it is created, not after.

The MDT/OST disks are sparse files in the VM directory, which QEMU
opens through the host page cache. To measure a real host device (an
HDD, say), put them on it and take the cache out:

```bash
ltvm create co1-hdd --disk-dir /mnt/hdd/ltvm --disk-cache none --disk-prealloc
```

`--disk-dir` must be an absolute directory that already exists;
`--disk-cache` is passed to QEMU as `cache=` (none, writeback,
writethrough, directsync, unsafe; unset is QEMU's writeback);
`--disk-prealloc` allocates the files up front (falloc, or
`--disk-prealloc=full` to write them out) and stops the guest's discards
from punching holes back in them. `cluster create` takes all three for
every node. `--disk-cache` alone can change later, with `ltvm vm set
<name> --disk-cache MODE` (or `default`) on the stopped VM. A VM made
with these needs an ltvm that knows them to start it: an older one looks
for the disks in the wrong place.

vCPUs and memory can change later without recreating the VM: stop it,
then `ltvm vm set <name> --vcpus 2 --mem 4096`, and the new size applies
at the next start. Use it to shrink idle VMs on a crowded host rather
than destroying them.

Running guests may commit at most the host's RAM less a reserve (in a
container, its cgroup's memory.max less the reserve). `ltvm list` shows
that budget (`mem_available_mb`, and `mem_free_mb` left of it, in
`--json`): size a cluster from it. `create` and `start` refuse a VM that
would go over it and list the guests using it, and `cluster create`
refuses a cluster whose nodes do not all fit before it makes any; with
`--wait SECONDS` they wait up to that long for the memory to free
instead, and `cluster create --wait` gives every node the same wait. On a host other sessions share, where their guests
are not yours to stop, that is how to wait for room -- not a retry loop:

```bash
ltvm create co1-single --mem 4096 --wait 1800
```

A host running KSM holds far less than that, since guests booted from
one image share most of their pages, so an admin may have set
`[memory] overcommit = N` in `/etc/ltvm.conf`. The running guests may
then commit N times the budget, though one VM must still fit the budget
on its own, and a refusal prints the ratio in force. It is off by
default and it is the user's setting for the whole host: do not raise
it to get past a refusal -- wait.

`--numa-nodes N` gives the guest N simulated NUMA nodes, for testing
Lustre CPTs (`cpu_npartitions`, `cpu_pattern`): the vCPUs, which N must
divide, and the memory are split evenly, one socket per node. Unlike the
disks it can change later, with `ltvm vm set <name> --numa-nodes N` on
the stopped VM (0 for none):

```bash
ltvm create co1-numa --vcpus 4 --mem 4096 --numa-nodes 4
ssh co1-numa numactl -H
```

`--kernel-args` adds kernel boot parameters, also fixed at create time.
The target kernels have no KASAN, but they build in SLUB debugging (and
the Rocky ones page owner tracking), off until booted with them:

```bash
ltvm create co1-single --kernel-args 'slub_debug=FZPU page_owner=on'
```

`slub_debug=FZPU` turns on sanity checks, red zones, poisoning and
allocation tracking in every slab cache. A use-after-free or overrun in
Lustre code is then reported when the object is freed or reused, with
its allocation and free stacks, rather than surfacing as unrelated
corruption.

Kernels built with the shared overlay also carry more debug facilities
that stay off until enabled: fault injection (failslab,
fail_page_alloc, fail_make_request, fail_function via debugfs),
`kmemleak=on`, `debug_pagealloc=on`, the `jbd2_debug` parameter, and the
dm-dust/dm-delay targets (`targets/common/kernel-config.overlay`).

ltvm refuses `root=`, `console=` and `fc_*`, which it sets itself.
`cluster create` takes the same flag for every node.

`create` is idempotent: it starts a stopped VM and no-ops on a running
one. Name VMs `co<N>-<role>` after the checkout they serve -- `co1-single`,
`co2-mds`, `co5-ec-dom`. Never a bare `testvm`: the number is what tells a
later session which tree the VM belongs to.

`deploy-lustre` is idempotent: it unmounts and unloads first, builds the
tree if the staging is stale, pushes it over ssh, and mounts with
`--mount`. The unload takes down every Lustre mount on the VM, however it
was made -- llmount.sh or a hand `mount -t lustre` -- and if Lustre will
not unload, the deploy stops before copying anything rather than leave
the old modules running; `cluster deploy` does the same on every node.
`--userspace-only` leaves a running Lustre alone and never rebuilds, but
edited test scripts, cfg files and other uncompiled sources are copied
into the staging before every deploy, so a script edit needs no rebuild;
it warns when compiled sources changed. `cluster deploy --userspace-only`
does the same on every node, from the staging an earlier `cluster deploy`
built. **The tree flag is never positional** -- `--lustre-tree <path>`
(`cluster deploy` also accepts `--build`).

On a loaded host the deploy's limits are about progress, not totals: the
copy stops only after 10 minutes without moving a byte, and each guest
step (depmod, ldconfig, writing cfg/local.sh) may take 10 minutes;
`LTVM_DEPLOY_TIMEOUT=<seconds>` changes both. llmount.sh under `--mount`
gets 3 minutes plus 1 per target, 3 per target on ZFS;
`--mount-timeout <seconds>` overrides it, on `cluster deploy` too, and
`ltvm llmount <vm> --timeout <seconds>` retries a mount that ran out.

Do not use host `make` or `fullbuild` to produce something for a VM. The
host kernel is not the VM kernel; `ltvm build lustre <target>
--lustre-tree <tree>` builds inside the target's build container, which is
what `deploy-lustre` invokes.

Because a deploy rebuilds a stale staging, it ships the tree as it is
when you deploy, not as it was when you built. Build a variant (debug
prints, a reverted fix), restore the source, then `deploy-lustre`, and
the VM gets the restored code. Deploy the variant before restoring the
source.

Configure flags belong to the command that builds. A deploy that rebuilds
uses the target's defaults, so a tree built with `build lustre
--configure=...` loses those flags at its next deploy: pass the same
`--configure` to `deploy-lustre` or `cluster deploy`.

ltvm's build path detects two staleness traps that a host build still
walks into, and both look like something else:

- `modpost: "ldiskfs_<sym>" [osd_ldiskfs.ko] undefined!` is stale ldiskfs
  staging. The tree's `sources` stamp depends on the series file but not
  on the individual patches under `ldiskfs/kernel_patches/`, so an edited
  patch never restages the generated `ldiskfs/*.c`. Fix it with
  `rm -f <tree>/ldiskfs/sources` and rebuild -- it is not a
  kernel/target mismatch, so do not bump the target or re-fetch
  artifacts over it.
- `configure: error: newly created file is older than distributed
  files!` is the host clock stepping backwards mid-build. Retry; any
  residual `make: Clock skew detected` warnings are harmless.

VMs are disposable. Destroying and recreating takes about 15-20 seconds
and is usually faster than unpicking a broken mount -- use
`ltvm llmount <vm> --cleanup` only when the logs or crash dumps on that VM
still matter.

## Update notices

ltvm checks weekly for a newer version and, when one is available,
prints one line to stderr on any command:

```
ltvm: update available (0.5.1.abc1234 -> def5678).  Run: sudo ltvm update
```

Surface it to the user and offer to run `sudo ltvm update`. Do not run
it mid-task on your own initiative: the update replaces `targets.yaml`,
the host-config templates and the kernel build scripts under a process
that has already imported the old code, so the rest of the task then
reads new files with old logic. Finish what you are doing, update
between tasks, and re-run.

The notice repeats at most once a day while an update is pending, so
seeing it once in a session is the expected behaviour -- not a signal
that a previous update attempt failed.

## Telemetry notice

On its first run ltvm prints a short notice to stderr saying it sends
an anonymous weekly check-in. That is expected output, not an error and
not something that failed -- do not try to fix it, and do not run
`ltvm telemetry off` on the user's behalf unless they ask. It prints
once per install, and never on a host whose admin set a shared
`[telemetry] install_id` in `/etc/ltvm.conf` (`ltvm telemetry status`
shows it).

If they ask what is collected, `ltvm telemetry show` prints the literal
payload: a random install ID, the ltvm version, a description of the
host (OS, arch, WSL, bucketed CPU/RAM), and counts of which commands
and targets were used. No paths, names or error text.

## Root

- **None, on a host set up for it.** Members of the `ltvm` group run
  `create`, `start`, `stop`, `destroy`, `doctor` and `cluster
  create/destroy` as themselves: QEMU runs as the user and joins the
  bridge through QEMU's bridge helper. `ltvm doctor` says whether the
  host is ready and, if not, what is missing. There, `sudo ltvm create`
  runs as the user who typed it, and `sudo ltvm start` as the VM's
  owner.
- **Otherwise** the single-VM lifecycle runs as the invoking user and
  prompts once for sudo, and `cluster create/destroy` need root.
- **Always root:** `install`, `update`, and any VM with a `passthrough`
  NIC.
- **Never:** `list`, `build *`, `target *`, `deploy-lustre`, `llmount`,
  `vm *`, `cluster deploy/exec/status/ssh`.

If `doctor` reports that this user is not in the `ltvm` group, that is
for the human to fix (`sudo usermod -aG ltvm <user>`, then a new login);
do not try to work around it.

**macOS** has the shared layout too, once `./ltvm install` has set it
up (`ltvm doctor` says whether it has): QEMU runs as the user and reaches
the network through socket_vmnet, and VM names go to `hosts.d/`, which
ltvm's dnsmasq is reloaded from by launchd. Without it, the lifecycle
commands elevate individual steps through sudo: `create` and `start`
launch QEMU as root, `stop` and `destroy` signal it as root, and each
`create` updates `/etc/hosts`. A VM started as root before the host was
set up still needs sudo to stop; once restarted it is the user's. You
cannot answer a sudo password prompt, so when one of these fails for want
of a password, ask the human to run that same command in their own
terminal -- where sudo can prompt and then remember the password for a
few minutes -- rather than working around it. Never edit sudoers.

## Talking to a VM

Every VM gets an entry in your `~/.ssh/config`, so plain `ssh` and
`scp` work by name:

```bash
ssh co1-single 'lctl dl'
scp co1-single:/tmp/out.txt .
ssh -o ConnectTimeout=10 -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=3 co1-single uptime   # when it may be hung
```

Bound anything that might hang. The ssh options above give up on a VM
that stops answering within about 15 seconds, on any host. For a long
command, set a timeout on your own command runner, or use `timeout` --
which macOS lacks; Homebrew's coreutils installs it as `gtimeout`. A VM
that stops answering is a candidate for `ltvm vm console-log`, not for a
longer wait.

Do not use `ltvm vm console-log -f`: it streams until Ctrl-C, which is
useful to a human watching a boot and a way to hang yourself. Take
another `console-log` snapshot instead -- the log is a file, and re-reading
it costs nothing.

## Clusters

```bash
ltvm cluster create co2 mgs+mds:co2-mds:1 oss:co2-oss:3 client:co2-client
ltvm cluster deploy co2 --build <tree> --mount
ltvm cluster exec co2 oss 'lctl dl'        # every node with the role
ltvm cluster exec co2 co2-oss2 'lctl dl'   # one node by name
ltvm cluster status co2
ltvm cluster llumount co2                  # llmount to mount it again
ltvm cluster stop co2                      # start to bring it back
ltvm cluster destroy co2
```

`cluster status` also shows the Lustre targets of each server, with
their recovery state.  For each client, it lists the imports that are
not FULL or IDLE.  `--json` gives the same data under `lustre`.  To wait
for recovery, add `--wait-recovery SECS`.  The command then exits 1
if a target still recovers after SECS.  A node that does not answer in
10 seconds shows as unreachable.

`cluster create` and `cluster destroy` need `sudo` in front on a host
that is not set up for unprivileged VMs (see Root).

The cluster commands take the single-VM flags that carry over.
`cluster create --kernel <name>` boots every node on that kernel, and
`cluster deploy` then builds Lustre for it -- the way to run an interop
cluster on a kernel an older branch still supports:

```bash
ltvm cluster create co2 --kernel 5.14-rhel9.3 mgs+mds:co2-mds:1 oss:co2-oss:2
```

`--variant` and `--wait` work the same way. `cluster start` takes
`--wait`, and `cluster destroy` accepts `--force`/`--yes` though it
never prompts.

`--node-target VM=TARGET` (repeatable) boots one node on its own
target, so one cluster can mix targets.  That node boots its target's
default kernel and base variant (`--kernel` and `--variant` name things
in the cluster-wide target); `--node-kernel VM=KERNEL` gives it another
of its own target's kernels.  `cluster deploy` builds Lustre once per
distinct target and kernel.  Targets that install Lustre in different
places (Debian and Ubuntu under /usr/lib, the rest under /usr/lib64)
mix too: deploy links each node's directory under the other name.

```bash
ltvm cluster create co7 rocky9 mgs+mds+oss:co7-srv:4 client:co7-c16k \
    --node-target co7-c16k=rocky10-16k
```

### Two Lustre trees on one cluster (interop)

`cluster deploy --server-only` builds for and deploys to the server
nodes alone, and `--client-only` to the pure clients alone; the other
nodes keep the Lustre they have, and every node still gets the
cluster's local.sh.  So older servers under a master client is two
deploys, with each tree built only for its own nodes' target:

```bash
ltvm cluster create co8 rocky8 mgs+mds:co8-mds:2 oss:co8-oss:4 client:co8-c1 \
    --node-target co8-c1=rocky9
ltvm cluster deploy co8 --build ~/worktrees/v2_15_8 --server-only
ltvm cluster deploy co8 --build $CO/1 --client-only --mount
```

`--server-only --mount` mounts the servers only, from the MGS node with
the servers' own scripts; a 2.15.x llmount.sh lacks `--server-only`,
and ltvm does what it does instead.  A node that is both a server and a
client counts as a server.  A mount that leaves a target, or the
client, unmounted fails even when llmount.sh exits 0.

### Page sizes larger than 4 KiB

`rocky10-16k` (aarch64, 6.12-rhel10.2) is rocky10 with
`CONFIG_ARM64_16K_PAGES`, for finding Lustre `PAGE_SIZE > 4096` bugs.
It runs at full speed under Apple Silicon HVF, whose CPUs have only the
4K and 16K translation granules; `rocky9-64k` cannot boot there except
under `LTVM_FORCE_TCG=1` (emulated, very slow).  Neither has a
published release: `ltvm build all rocky10-16k --lustre-tree <tree>`.
Start with a 16K client against 4K servers (as above); move the servers
to 16K once that passes.  Distro userspace needs nothing special: EL
aarch64 binaries are built with 64K segment alignment.

### SLES clients

`opensuse156` (x86_64, client only) is a SLES 15 kernel on an openSUSE
Leap 15.6 userspace: `6.4-sles15sp7` (the default, Lustre's SLES client
primary kernel) and `6.4-sles15sp6`, built from SUSE's release tags.
Both are published (`ltvm target fetch opensuse156`).  It has no
server, so put it in a cluster with rocky servers; `--node-kernel`
picks SP6:

```bash
ltvm cluster create co8 rocky9 mgs+mds+oss:co8-srv:4 client:co8-sles \
    --node-target co8-sles=opensuse156 --node-kernel co8-sles=6.4-sles15sp6
```

Create and start wait up to 120 seconds for a guest to answer ssh,
longer (up to 600) while its console shows it still booting, and stop
sooner if its QEMU exits. A host busy enough to run out of that ("SSH
not ready after ...") wants `LTVM_SSH_TIMEOUT=<seconds>` exported, not a
retry. That error ends with the last thing ssh said: read it
before blaming a slow boot. An ssh that fails on the host itself, such
as a config file with bad owner or permissions, stops the wait at once
with "fails on this host, not in the guest"; `ltvm doctor` checks the
ssh client config.

Options can go anywhere after `cluster create <name>`, between the
target and the node specs included.

Every node's `/etc/hosts` names every member of its cluster, written by
`cluster create` and again by each `cluster deploy`, so the nodes find
each other without the host's DNS. A cluster created by an older ltvm
gets it at its next `cluster deploy`.

A cluster record whose VMs are all gone, because they were destroyed one
by one, is removed with the last of them, and `cluster create` replaces
one it finds.

`cluster exec <role>` fans out and exits non-zero if any node did.
`cluster ssh <role>` is interactive and lands on the first node.

Security suites need a cluster, not one VM: on a single node every
connection is over `0@lo`, which Lustre never gives a security flavor
(LU-13343), so sanity-krb5 and sanity-sec's SSK runs (`SHARED_KEY=true`)
cannot pass there.  SSK needs nothing more.  sanity-krb5 needs a KDC and
keytabs, which one command sets up (KDC on the first MDS, realm
LTVM.TEST; rerunning keeps the keys, recreating the cluster loses them):

```bash
ltvm cluster krb5 co2
```

### Failover pairs

To move a target between 2 servers (`fail mds1`, replay and recovery
tests with a real failover), give a server a failover partner:

```bash
ltvm cluster create co2 mgs+mds:co2-mds1:1 oss:co2-oss:2 client:co2-c \
    --failover co2-mds2=co2-mds1
ltvm cluster deploy co2 --build <tree>
ltvm suite run co2 replay-single --only '0a 0b' --wait
```

The partner is a new node (not a node spec) with the role `failover`.
The partner has no disks of its own.  Its QEMU opens the primary's
disks, so `/dev/vdb` is the same MDT on both guests.  `cluster deploy`
writes the partner into `cfg/local.sh` as `mds1failover_HOST`.  When
the primary is the MGS, it also writes `mgsfailover_HOST` and puts both
NIDs in `MGSNID`.  The framework formats the target with `--failnode`,
which turns on ldiskfs MMP.  MMP stops a second Lustre mount on the
partner with EBUSY after about 11 seconds.  MMP does not stop a raw
`dd` to the device, so do not write to the device from the passive
node.  ZFS works too: `multihost=on` stops the second import.

To fail a server over by hand, use `cluster failover`, not a hand
mount.  It puts the fault on the host, then mounts the targets on the
partner through the test framework.  Thus the framework's record of
the active node stays correct for `cluster failback`:

```bash
ltvm cluster failover co2 co2-mds1 --how kill --json
ltvm cluster status co2 --json --wait-recovery 300
ltvm cluster failback co2 co2-mds1
```

`--how` is `umount`, `kill` (power loss), `panic` (vmcore, then a
reboot) or `freeze` (SIGSTOP, Linux hosts only).  The `split-brain`
method cuts the link of a primary that runs.  It then checks that MMP
refuses the takeover: exit 0 means MMP refused it.  Neither action waits for
recovery.  `cluster status --wait-recovery SECS` waits, and exits 1
if a target still recovers after SECS.  A suite deletes the framework's record of where each facet is.  Thus
`suite run` and `cluster llmount`/`llumount` refuse while a target is
on its partner, and name the failback to run.

For suites with hard failures, add `--env FAILURE_MODE=HARD`.  The
framework then crashes a node through `ltvm-power`, and the node
reboots after 30 seconds.

A primary has one partner at most.

## Running tests

For a whole suite, or a long `--only` list, use `ltvm suite`.  It starts
the suite the way CI does -- unmounted, `CLEANUP_DM_DEV=false`, `auster
-k -v -r -H`, detached under its own session keyring -- each of which
was a wrong result when left out (the paragraphs below say why):

```bash
id=$(ltvm suite run co1-single sanity)          # returns at once
ltvm suite run co1-single sanity --only '39r 42a'
ltvm suite run co2 sanity-sec --env SHARED_KEY=true --wait  # cluster
ltvm suite status co1-single [$id]   # running / done rc=N / died
ltvm suite collect co1-single $id    # logs to ./ltvm-suite/..., summary
ltvm suite collect co1-single $id --debug-logs   # the debug logs as well
```

A cluster runs it from its first client.  `--wait` collects at the end
and exits non-zero on any failure; `status` flags a node that rebooted
during the run (a crash: look in `/var/crash`).  One suite at a time per
VM or cluster; `run` refuses a second.

A suite that passes ends unmounted, on ldiskfs and ZFS alike: auster
mounted Lustre, so it cleans up after a clean run (a failed one is
usually left mounted).  For ad-hoc checks afterwards, `ltvm llmount <vm>`
(`ltvm cluster llmount <cluster>`) mounts it again, reformatted.

Collected logs go to `./ltvm-suite/<target>/<run-id>/` under the
directory you run from; `run` and `collect` print the full path, and
`status` shows `collected to <dir>` once a run has been collected.
`--results-dir DIR` on `collect` or `run --wait` puts them elsewhere:
use it when the current directory is a git tree that should not get
them.  The `*.debug_log.*` files stay in the guest unless `collect` or
`run --wait` is given `--debug-logs`; the summary says how many were
left.

For a single subtest by hand:

```bash
ssh co1-single 'sudo -E ONLY=42a bash /usr/lib64/lustre/tests/sanity.sh'
ssh co1-single 'cd /usr/lib64/lustre/tests && sudo -E ./auster sanity --only 42a'
```

auster's `-s` is `SLOW=yes`, not "suite": CI runs `SLOW=no`, so leave it off
unless the slow tests are the point.

Mount first. Only llmount.sh formats the targets, so on a VM that has
never been mounted a bare `sanity.sh` fails setup with "has not been
formatted with mkfs.lustre". `deploy-lustre --mount` or `ltvm llmount
<vm>` does it (`ltvm cluster llmount <cluster>` for a cluster), and
`deploy-lustre` without `--mount` says so when the targets are blank.
`REFORMAT=yes` on the suite, or `auster -r`, formats as well. Both
`llmount`s take down a Lustre that is already mounted and reformat, as
`deploy-lustre --mount` does.

For a whole suite, start unmounted, as CI does, and let auster format
and mount:

```bash
ssh co1-single 'cd /usr/lib64/lustre/tests && bash llmountcleanup.sh &&
    auster -k -r -H sanity'
```

A suite that finds Lustre already mounted -- by `deploy-lustre --mount`,
`llmount`, or an earlier suite in another shell -- takes the raw disks
(`/dev/vdb`...) as each target's device instead of the dm-flakey mapper
it is mounted through, so tests that read the device behind Lustre's back
see stale data: sanity 39r fails every time.  `-H` makes `--only` honour
the suite's `ALWAYS_EXCEPT` list; without it, a test named in `--only`
runs even when CI skips it as known-broken.

A suite left running after the ssh that started it has gone needs a
session keyring of its own:

```bash
ssh co1-single 'cd /usr/lib64/lustre/tests &&
    setsid nohup keyctl session - ./auster -k -r -H sanity-sec \
    > /tmp/sec.out 2>&1 < /dev/null &'
```

Without `keyctl session`, the job inherits the ssh login's keyring,
which is revoked at logout, and every encryption test then runs
without encryption: mount reports "could not insert dummy encryption
key into session keyring" and sanity-sec fails a dozen tests.

Auster logs land in `/tmp/test_logs/YYYY-MM-DD/HHMMSS/` inside the VM.
Redirect long runs to a file and grep the file afterwards rather than
piping a slow command through `grep`.

The VM's shape is in `cfg/local.sh`: deploy appends blocks setting
`MDSCOUNT`, `OSTCOUNT`, `MDSDEV*`/`OSTDEV*`, the sizes and `FSTYPE` (and
on a cluster which node holds what) after the stock `${VAR:-default}`
lines. The two counts and `FSTYPE` still take the environment's value,
as on a stock tree, so `MDSCOUNT=1` or `OSTCOUNT=1` runs fewer targets
than the VM has (a larger one puts the extra OSTs in files under /tmp),
and `FSTYPE=ldiskfs` runs ldiskfs on a VM deployed with `--zfs`. Reformat
with the same count:

```bash
ssh co1-single 'cd /usr/lib64/lustre/tests && MDSCOUNT=1 bash llmount.sh'
ssh co1-single 'MDSCOUNT=1 ONLY=42a bash /usr/lib64/lustre/tests/sanity.sh'
```

For more OSTs than disks -- sanity-ec wants 8 -- deploy with
`deploy-lustre --ost-count 8` (or `cluster deploy --ost-count 8`): the
OSTs past the disks are files in the root filesystem, sized to fit it,
and later deploys keep the count until `--ost-count 0`.  Later deploys,
`--userspace-only` included, keep the `FSTYPE` and the cluster block
too; `--fstype zfs` with `--userspace-only` pins ZFS on a VM that has it.

ZFS comes from the target's release (`--zfs`), another release
(`--zfs-version 2.4.3`, any OpenZFS release tag), or your own OpenZFS
tree with a fix in it (`--zfs-source ~/src/zfs`, a git checkout or an
unpacked tree), on `build zfs`, `build lustre`, `deploy-lustre` and
`cluster deploy` alike. A source build is versioned by its META version
and a digest of its files, so an edit rebuilds and an unchanged tree
does not; deploy ships the ZFS the Lustre build used. Where the shared
artifacts are not yours, ZFS builds into `~/.cache/ltvm`.

The rest -- devices and sizes -- are plain assignments and win over the
environment. A VM deployed by an older ltvm takes the counts and
`FSTYPE` from the environment only after its next deploy.

On a cluster the servers run parts of a suite (`rpc.sh`) from their own
`local.sh`, without the client's environment. There, point `CONFIG` at a
file that sources local.sh and then sets the count, on every node
(`cluster exec`):

```bash
printf '. /usr/lib64/lustre/tests/cfg/local.sh\nMDSCOUNT=1\n' > /root/one-mdt.sh
CONFIG=/root/one-mdt.sh ONLY=42a bash /usr/lib64/lustre/tests/sanity.sh
```

## Crashes and hangs

VMs boot with `crashkernel=512M`, and the image ships kexec-tools, crash
and drgn with the kernel and initramfs pre-baked. After a panic kdump
writes a vmcore to `/var/crash/<ip>-<date>/` and reboots, about 15
seconds. `ltvm vm nmi` triggers one from the host; `echo c >
/proc/sysrq-trigger` from inside the VM does the same.

```bash
ltvm vm console-log co1-single
ltvm vm snapshot co1-single before-test
ltvm vm restore co1-single [tag]
ltvm vm exec co1-single lctl dl               # run a command; exit = its rc
ltvm vm nmi co1-single                        # NMI -> panic + kdump
ltvm vm crash-collect co1-single --mod-dir <build>
ltvm vm crash-collect co1-single --trigger --mod-dir <build>
```

A command whose ssh drops mid-run (`vm exec` exits 255) may mean the
guest panicked: `vm exec` says so and names `console-log` and
`crash-collect` for it.

`crash-collect` resolves the matching vmlinux itself and verifies its ELF
build-id against the running kernel, exiting non-zero when it cannot find
one. Pass the `.ko` directory of the build that **crashed** -- a
`git checkout` does not rebuild, so the modules on disk are whatever was
built last. For reading the dump afterwards, see the crash triage skill.

## Compatibility gate

Before any Lustre-involving build, ltvm checks the tree against the
target's `lustre.mode`:

```bash
ltvm target validate rocky9 --lustre-tree <tree>   # 0 ok/warn, 1 refused, 2 error
ltvm build all rocky9 --lustre-tree <tree> --force-compat
```

The gate reads `lustre/kernel_patches/which_patch` for `server_ldiskfs`
and `lustre/ChangeLog` for `server_zfs`, and refuses combinations Lustre
upstream does not declare supported. `--force-compat` silences refusals,
not hard errors, and is for known work-in-progress branches only. It is
accepted by the `build` commands, `target build`, `target validate`,
`deploy-lustre`, `cluster deploy` and `make-install`/`make-reinstall`;
`target publish` runs no gate and does not take it.

## Sharing what was built

```bash
ltvm target publish rocky9                # bundle + upload a GitHub release
ltvm target publish rocky9 --no-upload    # build the tarballs only
```

`ltvm target fetch` finds the latest release for a target and downloads
it; re-running when the local tree is current finishes in under a second.

## Hot and cold storage

A shared host may split the artifacts in two, in `/etc/ltvm.conf`:

```ini
[storage]
hot = /var/cache/ltvm      # SSD: active targets -- images, kernel build trees, ccache
cold = /mnt/data/ltvm      # HDD: inactive targets, old releases
```

`hot` is the artifacts directory (`LTVM_ARTIFACTS_DIR` overrides it);
`ltvm doctor` shows both. Move a target you are done with, and bring it
back before using it heavily:

```bash
ltvm target archive rocky8 [--arch x86_64]   # leaves a symlink; paths keep working
ltvm target restore rocky8
```

Both refuse while a VM uses the target and name the VMs. A running VM
always blocks: stop it, or leave the target where it is. `--force`
moves under stopped VMs, which keep working (a restore repoints any that
named the cold path); ask before using it on VMs that are not yours.
`build status` and `target list` mark archived targets as cold (with
`storage`/`cold_path` in `--json`), and `target fetch` refuses one until
it is restored.

## Inside a VM rather than on the host

`deploy-lustre` pushes Lustre into a VM from the host. The `make-*`
commands are the other direction -- ltvm running inside a machine it
built, installing onto that machine's own root:

```bash
ltvm make-install --lustre-tree <tree>
ltvm make-uninstall
```

Both refuse unless the machine is one ltvm built and the working
directory is a Lustre tree. On EL8/EL9 images run them with
`python3.11`, and install podman first.

## For agent controllers

Export `LTVM_OWNER_ID=<durable-session-id>` before creating VMs so the
owner metadata identifies the session rather than a pid; read it back with
`ltvm list --json`. Its `clusters` list gives each cluster's owner and
member VMs, and each VM row names its `cluster`; destroy a cluster with
`ltvm cluster destroy`, not node by node.

Destroy the VMs a task made when it is done: a stopped VM keeps its
disks, a few GB each. A build warns when the disk is nearly full;
`ltvm clean` then previews what can go (superseded kernels and images,
dead ltvm podman images) and lists VMs idle for two weeks, and
`ltvm clean --apply` removes all but the VMs.

## Sharing VMs with other sessions

Other sessions on the host see the same VMs, and one deploying over
another's test run wrecks both.  Claim a VM before using it:

```bash
ltvm claim co1-single --tree ~/src/lustre-release   # or: ltvm claim (list)
ltvm release co1-single                              # when done with it
```

`deploy-lustre` claims an unclaimed VM for you.  `deploy-lustre`,
`llmount`, `start`/`stop`/`destroy`, `vm snapshot/restore/nmi/crash-collect/set`
and the cluster commands refuse a VM another live session has claimed, and
`ltvm list` shows `claimed=<owner>`.  When refused, use another VM or ask
the user -- do not `ltvm release --force` someone else's claim on your own.
Plain `ssh` is not gated, so claim before ssh-only work too.

A Claude Code session is recognised by itself (`claude:<session-id>`), and
its claims end when it exits.  Another agent exports `LTVM_OWNER_ID` and
`LTVM_OWNER_PID` (the agent's own long-lived process: each command it runs
is a fresh shell), or passes `--owner`/`--pid`; `--ttl 4h` ends a claim
after a while regardless.  A human's claims (`user:<name>`) last until
released, and a hand deploy never claims.

## Flags that apply everywhere

`--json` for machine-readable output, `--verbose`, `--arch <arch>` to
override the target's configured architecture, `--kernel <name>` on the
commands that act on one kernel, and `--force-compat` on the build,
validate and deploy commands.

`--json` is accepted everywhere but only some commands have anything
structured to say.  The ones worth parsing: `list`, `build status`,
`target show/validate/fetch/delete`, `create`, `deploy-lustre`, and
`cluster status/list/exec`.  The other `cluster` actions stream
human progress under `--json` too, and `cluster ssh` execs an
interactive session, so don't parse those.

## Where the detail lives

This skill covers using ltvm. For target configuration, package lists,
adding a new target OS, variants, artifact layout, the release manifest
schema and the export formats, read `CLAUDE.md` in the lustre-test-vms-v2
checkout and the operator notes under `docs/`.
