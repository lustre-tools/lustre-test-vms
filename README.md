# lustre-test-vms-v2

Build infrastructure for Lustre development and testing using QEMU microVMs.

Produces three independent, cacheable artifacts per target OS:

1. **Build container** -- cross-compilation environment (GCC, e2fsprogs-wc, etc.)
2. **Kernel** -- custom-built kernel + full source build tree for Lustre module builds
3. **VM base image** -- minimal root filesystem for QEMU microvm boot

Multiple kernel versions are supported per target (e.g., Rocky 9.5 and 9.7).

## Quick start

**Download pre-built artifacts (no building):**

```bash
./ltvm fetch rocky9 --url <tarball-url>
./ltvm install rocky9
sudo vm.py create --name co1-single \
    --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
./ltvm deploy co1-single --mount
```

**Build everything from scratch:**

```bash
./ltvm build-all rocky9
./ltvm install rocky9
./ltvm build-lustre rocky9 --lustre-tree ~/lustre-release
sudo vm.py create --name co1-single \
    --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
./ltvm deploy co1-single --build ~/lustre-release --mount
```

**Day-to-day iteration:**

```bash
./ltvm build-lustre rocky9          # incremental, fast
./ltvm deploy co1-single --mount    # redeploy
```

## Target OS support

| Target | Server | Client | Status |
|--------|--------|--------|--------|
| Rocky 9 | yes | yes | working |
| Rocky 8 | yes | yes | planned |
| Rocky 10 | yes | yes | planned |
| Ubuntu 24.04 | no | yes | planned |

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
  rocky9/
    target.conf             # OS metadata, capabilities
    kernel.conf             # default kernel version + config overrides
    container.Dockerfile    # build container (GCC, e2fsprogs-wc)
    image.Dockerfile        # VM rootfs image
    packages-os.txt         # OS-specific packages

output/                     # persistent build artifacts (gitignored)
  rocky9/
    container/
      meta.json
    kernels/
      5.14-rhel9.7/         # default kernel
        vmlinux
        vmlinuz
        modules/
        build-tree/
        meta.json
        lustre/             # optional: pre-built Lustre snapshot
      5.14-rhel9.5/         # additional kernels built on demand
        ...
    image/
      base.ext4
      meta.json
    cache/                  # downloaded SRPMs

lib/                        # Python library modules
  config.py                 # target config parsing, staleness detection
  kernel.py                 # kernel build system
  lustre.py                 # Lustre container build
  image.py                  # VM image builder (rootless)
  package.py                # packaging, fetch, install
  runtime.py                # vm.py / deploy wrappers
  validate.py               # end-to-end validation
  setup.py                  # host setup (QEMU, network)
  kernel-build-inner.sh     # runs inside kernel build container

ltvm                        # main CLI entry point
```

## ltvm commands

```
ltvm build-all <target>         Build container + kernel + image
ltvm build-container <target>   Build the build container
ltvm build-kernel <target>      Build a kernel (--kernel <ver>)
ltvm build-image <target>       Build the VM base image
ltvm build-lustre [target]      Build Lustre in container (--kernel <ver>)

ltvm package <target>           Create distributable tarball
ltvm fetch <target> --url URL   Download pre-built package
ltvm install <target>           Install kernel + image to system paths

ltvm deploy <vm>                Deploy Lustre to a VM
ltvm exec <vm> <cmd>            Execute command in a VM
ltvm vm <action> [args]         VM lifecycle (create/destroy/etc.)
ltvm cluster <action> [args]    Cluster management

ltvm status                     Show build status of all targets
ltvm shell <target>             Enter build container interactively
ltvm setup                      Set up host (QEMU, network, SSH)
```
