# Getting Started with ltvm

This guide walks through common workflows, from the simplest
path to more advanced setups.

## Prerequisites

- Linux host (WSL2 works)
- podman installed
- Root access (for VM lifecycle)

Run the one-time host setup:

```bash
sudo ltvm install
```

This installs QEMU (with microvm support), configures the
network bridge + dnsmasq, sets up SSH keys, and puts `ltvm`
on your PATH.

## Simple Flow: Pre-built Artifacts

The fastest path -- download pre-built kernel + image from
GitHub, optionally build Lustre from source, and run a VM.

### 1. Fetch pre-built artifacts

```bash
ltvm target fetch rocky9
```

This downloads a tarball containing the kernel (vmlinux,
vmlinuz, build-tree, modules) and VM base image (base.ext4)
into `artifacts/rocky9/x86_64/`.

Check what you have:

```bash
ltvm build status
```

### 2. Create a VM

```bash
sudo ltvm vm ensure co1-single \
    --vcpus 2 --mem 4096 \
    --mdt-disks 1 --ost-disks 3
```

The VM boots in ~2 seconds. It uses the pre-built kernel
(passed to QEMU via `-kernel`) and a copy-on-write overlay
of the base image.

### 3. Deploy and mount Lustre

If the fetched artifacts include a pre-built Lustre snapshot,
you can deploy directly:

```bash
sudo ltvm deploy-lustre co1-single --mount
```

### 4. Build and deploy your own Lustre (optional)

To test your own Lustre changes, build from source and deploy:

```bash
ltvm build lustre rocky9 ~/lustre-release
sudo ltvm deploy-lustre co1-single --build ~/lustre-release --mount
```

`build-lustre` runs inside the build container against the
pre-built kernel's build-tree. Incremental builds are fast --
only changed files recompile.

### 5. Run a test

```bash
ssh co1-single \
    'sudo -E ONLY=42a bash /usr/lib64/lustre/tests/sanity.sh'
```

### 6. Iterate

Edit Lustre source, then:

```bash
ltvm build lustre rocky9 ~/lustre-release
sudo ltvm deploy-lustre co1-single --mount
```

The build is incremental (make sees previous .o files).
Deploy is idempotent (cleans existing state, rsyncs, remounts).


## Intermediate Flow: Build Everything from Scratch

When you need to build the kernel and image yourself -- for
example, when working on kernel patches or testing a new OS
version.

### 1. Build the build container

```bash
ltvm build container rocky9
```

Creates a podman image (`ltvm-build-rocky9`) with GCC,
autotools, kernel build deps, and Lustre build deps. This
is the environment used for all subsequent builds.

### 2. Build the kernel

```bash
ltvm build kernel rocky9 --lustre-tree ~/lustre-release
```

The Lustre tree is needed because it contains the kernel
patches, patch series, base config, and SRPM version info
(in `lustre/kernel_patches/`). The SRPM is downloaded from
the Rocky mirror and cached in `artifacts/rocky9/x86_64/cache/`.

Output: `artifacts/rocky9/x86_64/kernels/<name>/` with vmlinux,
vmlinuz, modules, and a full build-tree for Lustre module
compilation.

### 3. Build the VM base image

```bash
ltvm build image rocky9
```

Builds a container image with all packages, exports it
to a raw ext4 filesystem via `mke2fs -d` under fakeroot
(no loop-mount, no root).  Takes ~10 minutes.

### 4. Build Lustre

```bash
ltvm build lustre rocky9 ~/lustre-release
```

### 5. Create VM, deploy, test

```bash
sudo ltvm vm ensure co1-single \
    --vcpus 2 --mem 4096 \
    --mdt-disks 1 --ost-disks 3
sudo ltvm deploy-lustre co1-single --build ~/lustre-release --mount
```

### Shortcut: build-all

Steps 1-3 can be combined:

```bash
ltvm build all rocky9 --lustre-tree ~/lustre-release
```


## Advanced Flow: Multiple Kernels

A single target supports multiple kernel versions. Rocky 9
ships with two:

```yaml
# targets/targets.yaml
rocky9:
  kernels:
    default: 5.14-rhel9.7
    available:
      - 5.14-rhel9.7
      - 5.14-rhel9.5
```

Build a non-default kernel:

```bash
ltvm build kernel rocky9 --lustre-tree ~/lustre-release \
    --kernel 5.14-rhel9.5
```

Build Lustre against it:

```bash
ltvm build lustre rocky9 ~/lustre-release --kernel 5.14-rhel9.5
```

Deploy with it:

```bash
sudo ltvm deploy-lustre co1-single --kernel 5.14-rhel9.5 --mount
```

Each kernel gets its own directory under
`artifacts/rocky9/x86_64/kernels/`, so they coexist without conflict.


## Advanced Flow: Multi-Node Clusters

For testing distributed Lustre (separate MDS, OSS, client):

```bash
# Create a cluster with named roles
sudo ltvm cluster create co2 \
    mgs+mds:co2-mds:1 \
    oss:co2-oss:3

# Deploy Lustre to all nodes and mount
sudo ltvm cluster deploy co2 \
    --build ~/lustre-release --mount

# Run a command on all OSS nodes
sudo ltvm cluster exec co2 oss 'lctl dl'

# Tear down
sudo ltvm cluster destroy co2
```

VM names must include the checkout number: `co<N>-<role>`.


## Advanced Flow: Interactive Container Shell

For debugging build issues:

```bash
ltvm build shell rocky9
```

Opens a shell inside the build container with the Lustre
source tree bind-mounted. You can run configure, make,
inspect the toolchain, etc.


## Key Concepts

### Artifacts and Staleness

Each artifact (container, kernel, image) stores an input hash
in `meta.json`. When you run a build command, ltvm hashes the
current inputs (Dockerfiles, package lists, kernel config) and
compares. If unchanged, the build is skipped.

```bash
ltvm build status          # shows current/stale for each artifact
```

### Incremental Lustre Builds

The Lustre source tree is bind-mounted into the container, so
.o files persist between builds. Configure is only re-run when
the kernel version, kernel path, or server flag changes. A
libtool version check prevents stale autotools state.

### VM Lifecycle

VMs are disposable. The base image is shared (read-only);
each VM gets a copy-on-write qcow2 overlay. Destroying and
recreating a VM takes ~15 seconds, which is often faster than
debugging cleanup issues.

### Deploy is Idempotent

`ltvm deploy` always:
1. Unmounts Lustre and unloads modules
2. Clears dm devices
3. Rsyncs the staging tree
4. Reconfigures disk mappings in cfg/local.sh
5. Optionally runs llmount.sh to format and mount

### One Staging Dir Per Target

`ltvm build lustre` installs to `artifacts/<target>/lustre/staging/`.
The last build wins. If you need two Lustre versions simultaneously,
use two source trees:

```bash
ltvm build lustre rocky9 ~/lustre-v1    # staging overwritten
ltvm build lustre rocky9 ~/lustre-v2    # staging overwritten again
```

In practice: build, deploy, test, iterate.
