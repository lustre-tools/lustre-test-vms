# Suggested Agent Instructions for ltvm-based Lustre Development

Copy/adapt these sections into your project's CLAUDE.md
or AGENTS.md.  Replace paths like `~/lustre-release` and
`~/lustre-test-vms-v2` with your actual locations.

## What ltvm is

`ltvm` builds and runs QEMU microVMs for Lustre development.
It produces four cacheable per-target artifacts (container,
kernel, VM image, Lustre), each keyed by input hash so only
stale pieces get rebuilt.  Artifacts can be packaged and
published to GitHub releases for use on other hosts.

Images are **per-kernel**: for a target with two kernels
(e.g. rocky9 5.14-rhel9.7 and 5.14-rhel9.5), each gets its
own `output/<target>/images/<kernel-full-name>/base.ext4`
with `/lib/modules/<kver>/` and kdump pre-baked.

## Quick Start (download pre-built artifacts)

```bash
cd ~/lustre-test-vms-v2

# One-time host setup (QEMU, bridge, dnsmasq, SSH, ltvm symlink)
sudo ./ltvm install

# Download pre-built kernel + image + Lustre (~1.3 GB)
ltvm fetch rocky9

# Create and boot a single-node VM (root needed for QEMU + tap)
sudo ltvm ensure co1-single \
    --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3

# Mount Lustre inside the VM (Lustre is baked into fetched images)
sudo ltvm llmount co1-single

# Verify
sudo ltvm exec co1-single 'mount | grep lustre; lfs df -h /mnt/lustre'
```

No building required.  Four commands from download to
mounted Lustre filesystem.

## Building from Scratch

```bash
cd ~/lustre-test-vms-v2

# Build container + kernel + image for the target's default kernel
ltvm build-all rocky9 --lustre-tree ~/lustre-release

# Or a non-default kernel (must be in targets/targets.yaml
# kernels.available).  SRPM auto-falls-back to Rocky vault
# for older minors.
ltvm build-kernel rocky9 --kernel 5.14-rhel9.5 \
    --lustre-tree ~/lustre-release
sudo ltvm build-image rocky9 --kernel 5.14-rhel9.5

# Build Lustre against a specific kernel (staging lives at
# <tree>/.ltvm-staging/<target>/<arch>/).
ltvm build-lustre rocky9 ~/lustre-release --kernel 5.14-rhel9.5

# See what's built (one image row per built kernel)
ltvm build-status
```

## Compatibility Gate

Every build / package / deploy command consults
`lustre/kernel_patches/which_patch` (for `server_ldiskfs`)
and `lustre/ChangeLog` (for `server_zfs`) in the given
`--lustre-tree` and refuses combinations Lustre upstream
doesn't declare supported.

```bash
# Standalone check -- exit 0 ok/best_effort, 1 refuse, 2 error
ltvm validate rocky9 --lustre-tree ~/lustre-release

# Override a refusal for a one-off build
ltvm build-lustre rocky9 ~/lustre-release --force-compat
```

`--force-compat` is wired into `build-all`, `build-kernel`,
`build-lustre`, `package`, and `deploy`.

## Packaging and Publishing

```bash
# Bundle kernel + image + Lustre into a tarball (per kernel,
# named <target>-<kver>_lustre.tar.zst)
ltvm package rocky9

# Publish to GitHub releases (tag derived from tarball name)
ltvm publish rocky9
```

`ltvm fetch <target>` discovers the latest release tag for
that target and downloads it.  Running again when the local
tree is current finishes in well under a second.

## VM Management

```bash
# Create / ensure (ensure is idempotent)
sudo ltvm ensure co1-single \
    --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3

# Non-default kernel -- requires matching per-kernel image
sudo ltvm ensure co1-single --kernel 5.14-rhel9.5

# Observe
ltvm list
ltvm console-log co1-single
ltvm dmesg co1-single

# Execute commands inside
sudo ltvm exec --timeout 30 co1-single 'lctl dl'

# Lifecycle
sudo ltvm start|stop|restart|destroy co1-single
sudo ltvm snapshot co1-single before-test
sudo ltvm restore co1-single before-test

# Host-level infrastructure health check
sudo ltvm doctor            # or doctor --fix
```

VM names MUST encode the checkout number and role to
avoid collisions: `co<N>-<descriptive-role>` (e.g.
`co1-single`, `co2-mds`, `co2-oss`, `co5-sanity-ec`).
Never bare names like `testvm`.

## Deploying a Locally-Built Lustre

```bash
# Build Lustre, then copy modules + userland into a running VM
ltvm build-lustre rocky9 ~/lustre-release
sudo ltvm deploy co1-single --build ~/lustre-release
sudo ltvm llmount co1-single
```

`deploy` is idempotent (unmounts/unloads before re-copy).
Destroy + recreate (~15-20 s) is often faster than an
in-place redeploy unless you want to preserve logs.

## Multi-node Clusters

```bash
# Roles: mgs, mds, oss, client; combine with +
ltvm cluster create co2 mgs+mds:co2-mds:1 oss:co2-oss:3
ltvm cluster deploy co2 --build ~/lustre-release --mount
ltvm cluster exec co2 oss 'lctl dl'
ltvm cluster status co2
ltvm cluster destroy co2
```

## Loading and Unloading Lustre (inside VM)

```bash
sudo ltvm llmount co1-single               # mount
sudo ltvm llmount co1-single --cleanup     # unmount + lustre_rmmod
```

These replace the older manual
`dmsetup remove_all; bash /usr/lib64/lustre/tests/llmount.sh`
ritual; `--cleanup` runs `llmountcleanup.sh` + `lustre_rmmod`.

## kdump / Crash Analysis

VMs boot with `crashkernel=512M`.  The image ships
kexec-tools, crash, and drgn, and has `/boot/vmlinuz-<kver>` +
`/boot/initramfs-<kver>.img` pre-baked by `build-image`.
After a panic, kdump writes a vmcore to `/var/crash/` and
reboots (~15 s).

```bash
# Trigger a panic
sudo ltvm nmi co1-single
# or from inside the VM: echo c > /proc/sysrq-trigger

# Collect vmcore from the VM and run the lustre crash recipe
sudo ltvm crash-collect co1-single --mod-dir ~/lustre-release

crash-tool recipes lustre \
    --vmcore /path/to/vmcore \
    --vmlinux output/rocky9/kernels/<kernel-full-name>/vmlinux \
    --mod-dir ~/lustre-release
```

## Debugging

Lustre uses an in-kernel ring buffer (per-CPU, default
~5 MB/CPU).  All CDEBUG/CERROR output lands there.

```bash
sudo ltvm exec co1-single '
    lctl set_param debug=-1
    lctl set_param debug_mb=10000
    lctl clear
    lctl mark "before repro"
    # ... reproduce ...
    lctl dk /tmp/dk.log
'
```

`lctl` clamps `debug_mb` to its max, so setting it very
large is fine.

## ltvm Command Reference

```
# Build
ltvm install                    One-time host setup (sudo)
ltvm update                     git fast-forward ltvm itself
ltvm build-all <target>         Container + kernel + image (+ --kernel)
ltvm build-container <target>   Rebuild the build container
ltvm build-kernel <target>      Kernel (+ --kernel, --lustre-tree)
ltvm build-image <target>       Per-kernel VM image (+ --kernel)
ltvm build-lustre <t> <tree>    Lustre against target kernel (+ --kernel)
ltvm build-status               Staleness table (one image row per kernel)
ltvm build-shell <target>       Interactive shell in build container

# Compat
ltvm validate <target>          Lustre/kernel compat check (read-only)
                                --force-compat overrides refusal

# Package / distribute
ltvm package <target>           Bundle into tarball (+ --kernel)
ltvm fetch <target>             Download latest release tarball
ltvm publish <target>           Upload tarball to GitHub release

# VM lifecycle
ltvm create|ensure <name>       Create / idempotent-create
ltvm start|stop|restart <name>  Power control
ltvm destroy <name>             Remove VM and its overlay
ltvm list                       Running + stopped VMs
ltvm ssh|exec <name> [cmd]      Shell / run command
ltvm console-log|dmesg <name>   Observe
ltvm snapshot|restore <name>    Overlay snapshots
ltvm nmi <name>                 Inject NMI (panic + kdump)
ltvm crash-collect <name>       Pull vmcore + run lustre_triage
ltvm doctor [--fix]             Host-infra health check

# Lustre in a VM
ltvm deploy <vm> --build <tree>   Copy Lustre build into running VM
ltvm llmount <vm> [--cleanup]     Mount (or unmount) Lustre

# Clusters
ltvm cluster create|deploy|exec|status|destroy <cluster> ...
```

Global flags: `--json`, `--verbose`, `--arch <a>`.
`--kernel <name-or-path>` on commands that operate on a
specific kernel.  `--force-compat` on build / package /
deploy to override the compat gate.
