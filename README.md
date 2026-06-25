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
sudo ltvm create co1-single --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
ltvm llmount co1-single                          # mount Lustre inside the VM
```

**Build everything from scratch:**

```bash
sudo ./ltvm install
ltvm build all rocky9 --lustre-tree ~/lustre-release
sudo ltvm create co1-single --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
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
sudo ltvm create co1-single --kernel 5.14-rhel9.5   # VM lifecycle needs sudo
ltvm llmount co1-single                 # mount Lustre inside the VM
```

## Target OS support

| Target | Server | Client | Status |
|--------|--------|--------|--------|
| Rocky 9 | yes | yes | working |
| Rocky 8 | no  | yes | working |
| Rocky 10 | no | yes | working |
| Ubuntu 24.04 | no | yes | working |

## ltvm commands

Top-level:

```
ltvm install                    One-time host setup (sudo)
ltvm update                     git fast-forward ltvm itself
ltvm build      <action> ...    Build artifacts (see below)
ltvm target     <action> ...    Target OS management (see below)
ltvm vm         <action> ...    VM inspection / crash / snapshot (see below)
ltvm cluster    <action> ...    Multi-node cluster management
ltvm create     <name>          Create a VM (idempotent)
ltvm start|stop|destroy <name>  VM power / removal
ltvm list                       Show all VMs
ltvm deploy-lustre <vm>         Deploy Lustre into a running VM
ltvm llmount <vm>               Mount (or --cleanup unmount) Lustre in a VM
ltvm doctor                     Host health check (--fix on request)
```

`build` sub-actions:

```
ltvm build all <target>         Container + kernel + Lustre + image
ltvm build container <target>   Rebuild the build container
ltvm build kernel <target>      Kernel (+ --kernel, --lustre-tree)
ltvm build image <target>       Per-kernel VM image (+ --kernel)
ltvm build lustre <target>      Lustre against target kernel (+ --lustre-tree, --kernel)
ltvm build mofed-kmods <t>      Per-kernel MOFED kernel modules
ltvm build shell <target>       Interactive shell in build container
ltvm build status               Staleness table (one row per built kernel)
```

`target` sub-actions:

```
ltvm target list                List configured targets + local/remote status
ltvm target show <target>       Detailed view of one target
ltvm target build <target>      Container + kernel + Lustre + image (alias for build all)
ltvm target clean <target>      Remove built artifacts
ltvm target delete <target>     Delete artifacts (local; --remote for GitHub release)
ltvm target validate <target>   Read-only Lustre/kernel compat check
ltvm target fetch <target>      Download latest release tarballs
ltvm target export <target>     Bake a bootable qcow2/raw (no ltvm runtime)
ltvm target publish <target>    Bundle artifacts and upload to GitHub release
                                (use --no-upload to produce tarballs locally)
```

`vm` sub-actions:

```
ltvm vm console-log   <name>    Show QEMU serial log
ltvm vm crash-collect <name>    Pull vmcore + run lustre_triage
ltvm vm nmi           <name>    Inject NMI (panic + kdump)
ltvm vm snapshot      <name>    Snapshot overlay disk
ltvm vm restore       <name>    Restore to a snapshot
```

VM names MUST include the checkout number and a descriptive role:
`co<N>-<role>` (e.g. `co1-single`, `co2-mds`, `co2-oss`). Never bare
names like `testvm`.

## More

See [CLAUDE.md](CLAUDE.md) for the full developer reference, and
[SUGGESTED-AGENTS.md](SUGGESTED-AGENTS.md) for ready-made AGENTS.md /
CLAUDE.md snippets you can drop into a Lustre workspace.
