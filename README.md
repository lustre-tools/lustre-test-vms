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
ltvm build all rocky9 --lustre-tree ~/lustre-release --lustre-build
sudo ltvm create co1-single --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
ltvm llmount co1-single
```

**Day-to-day iteration (change Lustre, redeploy into a running VM):**

```bash
ltvm build lustre rocky9 ~/lustre-release        # incremental, fast
ltvm deploy-lustre co1-single --build ~/lustre-release --mount
```

## Target OS support

| Target | Server | Client | Status |
|--------|--------|--------|--------|
| Rocky 9 | yes | yes | working |
| Rocky 8 | no | yes | working |
| Rocky 10 | yes | yes | working |
| Ubuntu 24.04 | no | yes | working |

## Repository layout

```
targets/
  common/                   # shared package lists + config
    packages-base.txt       # packages for all targets
    packages-server.txt     # server-only packages
    packages-dev.txt        # build-time deps (in container)
    packages-test.txt       # test runtime deps (IOR, dbench, etc.)
    packages-debug.txt      # debug/profiling tools
    kernel-config.fragment  # config overrides for all kernels
    rc.local                # VM first-boot setup
  targets.yaml              # all target metadata + kernel defaults
  rocky9/
    container.Dockerfile    # build container (GCC, e2fsprogs-wc)
    image.Dockerfile        # VM rootfs image
    packages-os.txt         # OS-specific packages

artifacts/                  # persistent build artifacts (gitignored)
  rocky9/x86_64/            # arch always nested (even for x86_64)
    container/
      image.tar
      meta.json
    kernels/
      5.14-rhel9.7-5.14.0-611...   # resolved kernel dirs (per kver)
        vmlinux
        vmlinuz
        modules/
        build-tree/
        meta.json
    images/
      5.14-rhel9.7-5.14.0-611...   # per-kernel image
        base.ext4
        meta.json
    cache/                  # downloaded SRPMs

ltvm_pkg/                   # Python package (CLI + implementation)
  cli.py                    # cmd_* handlers
  target_config.py          # targets.yaml parsing, staleness detection
  kernel_build.py           # kernel build (SRPM + patches + config)
  image_build.py            # VM image builder (rootless mke2fs -d)
  lustre_build.py           # containerized Lustre build + staging
  lustre_compat.py          # Lustre/kernel compatibility gate
  release_package.py        # package / fetch / publish to GitHub
  host_setup.py             # sudo ltvm install flow
  vm_state.py               # VMInfo, ClusterInfo, paths, constants
  vm_net.py                 # TAP, bridge, DNS, SSH registry
  vm_commands.py            # single-VM CLI handlers
  vm_cluster.py             # multi-node cluster management
  qemu_run.py               # QEMU launch / lifecycle

ltvm                        # main CLI entry point (argparse dispatch only)
```

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
ltvm build all <target>         Container + kernel + image (+ --lustre-build)
ltvm build container <target>   Rebuild the build container
ltvm build kernel <target>      Kernel (+ --kernel, --lustre-tree)
ltvm build image <target>       Per-kernel VM image (+ --kernel)
ltvm build lustre <t> <tree>    Lustre against target kernel (+ --kernel)
ltvm build mofed-kmods <t>      Per-kernel MOFED kernel modules
ltvm build shell <target>       Interactive shell in build container
ltvm build status               Staleness table (one row per built kernel)
```

`target` sub-actions:

```
ltvm target list                List configured targets + local/remote status
ltvm target show <target>       Detailed view of one target
ltvm target clean <target>      Remove built artifacts
ltvm target validate <target>   Read-only Lustre/kernel compat check
ltvm target fetch <target>      Download latest release tarballs
ltvm target export <target>     Bake a bootable qcow2/raw (no ltvm runtime)
ltvm target package <target>    Bundle artifacts into release tarballs
ltvm target publish <target>    Upload to GitHub release
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
