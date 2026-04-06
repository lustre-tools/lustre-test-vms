# Kernel and Lustre Build for QEMU microvm

Builds EL8 or EL9 kernels and Lustre inside containers so the host
GCC version doesn't matter.  Works on any Linux distro with podman
or docker.

## Quick start

```bash
# Download the latest Rocky 8 kernel SRPM and build it:
sudo ./build-kernel.sh --download --el 8

# Same for EL9:
sudo ./build-kernel.sh --download --el 9

# Boot a VM with the result:
sudo vm.sh ensure myvm --vcpus 2 --mem 4096 \
    --kernel /opt/qemu-vms/kernel/vmlinuz-el8
```

That's it.  The script downloads the SRPM from the Rocky Linux
vault, extracts the source and config, builds inside a container
with the matching GCC, and installs to `/opt/qemu-vms/kernel/`.

## Other ways to provide source

```bash
# From a local SRPM (EL version auto-detected from filename):
sudo ./build-kernel.sh --srpm kernel-4.18.0-553.97.1.el8_10.src.rpm

# From an extracted tarball + config:
sudo ./build-kernel.sh \
    --source linux-4.18.0-553.97.1.el8_10.tar.xz \
    --config kernel-x86_64.config \
    --el 8
```

## What it does

1. Downloads the kernel SRPM from Rocky Linux (if `--download`)
2. Builds a Rocky 8 or 9 container with the matching GCC
3. Extracts kernel source and applies the distro `.config`
4. Enables `CONFIG_XEN_PVH` (required for QEMU microvm boot)
5. Builds `vmlinux` + `bzImage` inside the container
6. Installs to `/opt/qemu-vms/kernel/`:
   - `vmlinuz-el{8,9}` -- compressed bzImage for `vm.sh --kernel`
   - `vmlinux-el{8,9}` -- unstripped ELF for crash/drgn analysis

## Options

```
--download          Download a kernel SRPM (requires --el)
--kernel-version V  Specific version (e.g., 553.97.1 or 611.36.1)
--keep              Keep build tree (for Lustre builds)
--srpm FILE         Use a local kernel source RPM
--source FILE       Use a kernel source tarball
--config FILE       Kernel .config (required with --source)
--el 8|9            EL version (auto-detected from filename)
--install-dir DIR   Output directory (default: /opt/qemu-vms/kernel)
--jobs N            Parallel make jobs (default: nproc)
```

## Requirements

- **podman** or **docker** -- `dnf install podman` / `apt install podman`
- **rpm2cpio** and **cpio** -- for SRPM extraction
  (`dnf install rpm` / `apt install rpm2cpio cpio`)
- **curl** -- for `--download`
- ~2 GB disk for the build (cleaned up automatically)

## Why containers?

RHEL8's 4.18 kernel must be built with GCC 8.  Building on an EL9
host (or any host with GCC 11+) causes:

- `-Werror=address-of-packed-member` in Hyper-V and Xen headers
- `-Werror=misleading-indentation` in kgdbts.c
- `-Werror=stringop-overflow` in mm/mempolicy.c
- `-Werror=missing-attributes` in lib/crc32.c
- **SMP crash**: GCC 11 tail-calls `cpu_startup_entry()` in
  `start_secondary()`, placing the stack canary check on the live
  path after `boot_init_stack_canary()` changed `%gs:0x28`.  Every
  secondary CPU panics on boot.

Using a container with the distro's native GCC avoids all of these.

## Building Lustre

`build-lustre.sh` compiles Lustre inside the same container,
against the kernel built by `build-kernel.sh`.

```bash
# Build Lustre against the kernel we just built
./build-lustre.sh \
    --lustre ~/lustre-release \
    --kernel-tree /tmp/kernel-build-el9-*/linux-*

# Or download + build the kernel automatically first
./build-lustre.sh \
    --lustre ~/lustre-release \
    --download-kernel --el 9

# Deploy to a VM
sudo deploy-lustre.sh \
    --vm myvm --build ~/lustre-release --mount
```

### Server vs client builds

By default, `build-lustre.sh` tries to build both client
and server.  Server requires Whamcloud-patched e2fsprogs
(`>= 1.47.3-wc2`), which is not in the standard Rocky repos.
If e2fsprogs is not found, the script automatically falls
back to a client-only build.

For server support, install Whamcloud e2fsprogs from
https://downloads.whamcloud.com/public/e2fsprogs/ before
building, or pass `--configure "--disable-server"` to skip
the fallback message.

### build-lustre.sh options

```
--lustre DIR        Lustre source tree (required)
--kernel-tree DIR   Kernel build tree (from build-kernel.sh)
--download-kernel   Build a kernel first (calls build-kernel.sh)
--el 8|9            EL version (default: 9)
--configure ARGS    Extra configure arguments
--jobs N            Parallel make jobs (default: nproc)
```
