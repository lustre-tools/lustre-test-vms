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
- **Install Python deps first** with [uv](https://docs.astral.sh/uv/);
  the system Python lacks PyYAML. `./ltvm` auto-uses `.venv/` when present.
- **podman backend is pinned to `applehv`** (Apple's Hypervisor.framework).
  `./ltvm install` forces the applehv machine provider; ltvm has no use for
  the GPU-only libkrun backend.

```bash
git clone git@github.com:lustre-tools/lustre-test-vms.git
cd lustre-test-vms

brew install uv && uv sync              # Python deps into .venv/
./ltvm install                          # NO sudo -- prompts internally
ltvm doctor                             # sanity-check the host

ltvm target fetch rocky9 --arch aarch64 --kernel 5.14-rhel9.5
ltvm create co1-single --kernel 5.14-rhel9.5   # prompts for sudo internally
ltvm llmount co1-single                 # mount Lustre inside the VM
```

## Target OS support

Fetchable = a prebuilt release exists (`ltvm target fetch <target>`).
More kernels can be built locally with `ltvm build all`; see them
with `ltvm target list --all-kernels`.

| Target | Arch | Lustre | Default kernel | Fetchable |
|--------|------|--------|----------------|-----------|
| rocky8 | aarch64, x86_64 | server (ldiskfs) + client | 4.18-rhel8.10 | rhel8.10 |
| rocky9 | aarch64 | server (ldiskfs) + client | 5.14-rhel9.7 | rhel9.5, 9.7, 9.8 |
| rocky9 | x86_64 | server (ldiskfs) + client | 5.14-rhel9.7 | rhel9.7, 9.8 |
| rocky10 | aarch64, x86_64 | server (ldiskfs) + client | 6.12-rhel10.0 | rhel10.0, 10.1 |
| ubuntu2404 | x86_64 | client only | 6.8-ubuntu2404 | 6.8-ubuntu2404 |
| rocky9-64k | aarch64 | server (ldiskfs) + client | 5.14-rhel9.7 | no -- build locally |
| mainline* | x86_64 | server (ldiskfs) + client | see below | no -- build locally |

`rocky9-64k` is rocky9 with `CONFIG_ARM64_64K_PAGES`, for testing
Lustre against a 64K page size.  It is aarch64-only: on an x86_64 host
it cross-builds, and `ltvm target list` hides it behind
`--all-arches` along with every other non-native target.

`mainline` (experimental) builds Lustre against vanilla kernel.org
kernels rather than a distro SRPM.  Its `--kernel` takes a release
series (`6.18`), an exact release (`7.2.3`), or one of the moving
aliases `latest` / `stable` / `longterm`.  See
[ltvm_pkg/upstream_kernel.py](ltvm_pkg/upstream_kernel.py).

## ltvm commands

Top-level:

```
ltvm install                    One-time host setup (sudo)
ltvm update                     git fast-forward ltvm itself
ltvm build      <action> ...    Build artifacts (see below)
ltvm target     <action> ...    Target OS management (see below)
ltvm vm         <action> ...    VM inspection / crash / snapshot (see below);
                                also accepts every VM command below, so
                                `ltvm vm create` == `ltvm create`
ltvm cluster    <action> ...    Multi-node cluster management (see below)
ltvm create     <name>          Create a VM (idempotent; --root-size sets
                                the OS disk, --disk-size the MDT/OST ones,
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
ltvm clean                      Prune superseded artifacts (dry-run by default)
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

`target` sub-actions:

```
ltvm target list                List configured targets + local/remote status
ltvm target show <target>       Detailed view of one target
ltvm target build <target>      Container + kernel + Lustre + image (alias for build all)
ltvm target clean <target>      Remove built artifacts
ltvm target delete <target>     Delete artifacts (local; --remote for GitHub release)
ltvm target validate <target>   Read-only Lustre/kernel compat check
ltvm target fetch <target>      Download latest release tarballs
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
ltvm cluster status <name>      Nodes and their state
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

`vm` sub-actions:

```
ltvm vm console-log   <name>    Show QEMU serial log (-f to keep streaming;
                                picks up the new log when the VM reboots)
ltvm vm crash-collect <name>    Pull vmcore + run lustre_triage
ltvm vm nmi           <name>    Inject NMI (panic + kdump)
ltvm vm snapshot      <name>    Snapshot overlay disk
ltvm vm restore       <name>    Restore to a snapshot
ltvm vm set           <name>    Change --vcpus/--mem of a stopped VM
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

Nothing is sent on the first run: ltvm prints the notice, starts the
clock, and the first check-in is a week later. That week is the window
in which opting out means nothing was ever sent.

## More

See [CLAUDE.md](CLAUDE.md) for the full developer reference. Agents get
what they need from the `ltvm` skill above, which `ltvm install` links
into their skill directories.

## License

BSD 3-Clause.  See [LICENSE](LICENSE).
