# lustre-test-vms-v2 -- Agent and Developer Reference

Build infrastructure for Lustre development/testing using
QEMU microVMs. Produces three cacheable artifacts per
target OS: build container, kernel, and VM base image.

## LLM: Getting the User Set Up

If the user has just opened this repo, walk them through
installation proactively. Don't wait for them to ask.

### Step 1 -- Install ltvm

Check whether it's already installed:

```bash
ltvm doctor
```

If not found, run from the repo directory (requires sudo --
installs QEMU, configures host bridge and dnsmasq, SSH,
symlinks `ltvm` into `/usr/local/bin`):

```bash
sudo ./ltvm install
```

### Step 2 -- Fetch pre-built artifacts

Unless the user wants to build from source, fetch is faster:

```bash
ltvm fetch rocky9
ltvm build-status
```

To build from source instead (e.g. custom kernel config):

```bash
ltvm build-all rocky9 --lustre-tree ~/lustre-release
```

### Step 3 -- Find out where their Lustre tree lives

Ask the user: **"Where is your Lustre source checkout?"**

### Step 4 -- Set up their workspace CLAUDE.md

This repo ships `SUGGESTED-AGENTS.md` with ready-made agent
config. Offer to append it to the user's Lustre workspace
CLAUDE.md (or AGENTS.md), replacing placeholder paths:

```bash
# Replace ~/lustre-test-vms and ~/lustre-release with actuals,
# then append:
cat SUGGESTED-AGENTS.md >> ~/lustre-release/CLAUDE.md
```

---

## Repository Layout

```
targets/
  targets.yaml            All target OS definitions (schema source of truth)
  common/                 Shared across all targets
    kernel-config.fragment  Microvm kernel config (all targets)
    packages-base.txt     Core OS packages
    packages-server.txt   Lustre server deps
    packages-dev.txt      Build container deps
    packages-test.txt     Test runtime deps (IOR, dbench, etc.)
    packages-debug.txt    Profiling/tracing tools
    rc.local              VM networking init script
  rocky9/                 Per-target directory (legacy per-dir files)
    container.Dockerfile  Build container definition
    image.Dockerfile      VM base image definition
    packages-os.txt       OS-specific packages
ltvm_pkg/                 Python package (CLI + all implementation)
  cli.py                  CLI command implementations (cmd_* functions)
  target_config.py        Target config parsing, staleness detection
  kernel_build.py         Kernel build (SRPM + patches + config)
  kernel-build-inner.sh   Runs inside build container
  image_build.py          VM image builder (Dockerfile -> ext4)
  lustre_build.py         Lustre build (containerized)
  lustre_compat.py        Lustre/kernel compatibility gate
  release_package.py      Package and fetch GitHub release artifacts
  host_setup.py           Host setup, verify, WSL2 helpers
  download.py             Robust file downloader
  vm_state.py             VMInfo, ClusterInfo, paths, constants
  vm_net.py               TAP, bridge, DNS, SSH registry
  vm_commands.py          Single-VM CLI handlers
  vm_cluster.py           Multi-node cluster management
output/                   Build artifacts (gitignored)
  <target>/
    <arch>/               Always present, even for x86_64
      container/
        image.tar
        meta.json
      kernels/
        <kernel-full-name>/
          vmlinux         Unstripped ELF (crash/drgn + boot)
          vmlinuz         Compressed bzImage (kdump)
          build-tree/     For Lustre module builds
          modules/
          meta.json
      images/
        <kernel-full-name>/
          base.ext4       Raw ext4 root filesystem
          meta.json
```

## Quick Start

```bash
ltvm install
ltvm fetch rocky9
ltvm build-status
```

## Artifacts

### Build Container

Cross-compilation environment with GCC, rpm-build,
autotools, kernel build deps, and Lustre build deps.
Uses ccache via a persistent podman volume.

```bash
ltvm build-container rocky9
```

- Dockerfile: `targets/<target>/container.Dockerfile`
- Package list: `targets/common/packages-dev.txt`
- Image tag: `ltvm-<target>-builder`

### Kernel

Custom kernel built from a distro SRPM with Lustre
patches applied and microvm config merged.

```bash
ltvm build-kernel rocky9 --lustre-tree /path/to/lustre-release
```

**How it works:**

1. Reads `targets.yaml` to find the default Lustre target
   (e.g., `5.14-rhel9.7`)
2. Parses the Lustre tree's `.target` file
   (`lustre/kernel_patches/targets/<target>.target`)
   for SRPM version, patch series
3. Downloads the kernel SRPM (cached in
   `output/<target>/<arch>/cache/`); falls back to Rocky vault
   for older minor versions
4. Resolves the kernel config from the Lustre tree
   (`lustre/kernel_patches/kernel_configs/`)
5. Merges microvm config fragments:
   - `targets/common/kernel-config.fragment` (all targets)
   - per-target `[config]` section from `targets.yaml`
6. Builds vmlinux, vmlinuz, modules, and a build tree
   inside the build container

**Outputs:** `output/<target>/<arch>/kernels/<kernel>/vmlinux`,
`vmlinuz`, `build-tree/` (for Lustre module builds).

To build a non-default kernel minor (e.g., for compat
testing), pass `--kernel`:

```bash
ltvm build-kernel rocky9 --kernel 5.14-rhel9.5 \
    --lustre-tree ~/lustre-release
```

### VM Base Image

Minimal root filesystem for QEMU microvm boot.
Built as a container image, then exported to raw ext4.
**Images are keyed per kernel** -- each kernel version
gets its own image under `output/<target>/<arch>/images/<kernel>/`.

```bash
ltvm build-image rocky9                         # default kernel
ltvm build-image rocky9 --kernel 5.14-rhel9.5  # specific kernel
```

Requires root (mount, losetup). The image includes:
- All packages from `packages-{base,test,debug,server}.txt`
  plus OS-specific `packages-os.txt`
- Source-built tools: IOR, mdtest, iozone, pjdfstest,
  FlameGraph
- drgn (via pip), Lustre-patched e2fsprogs
- Passwordless root SSH, inter-VM SSH key, serial
  console autologin
- kdump pre-configured: `/boot/vmlinuz-<kver>` and
  `/boot/initramfs-<kver>.img` baked in at image build time
- Networking via kernel cmdline (fc_ip, fc_gw, fc_name)

No kernel in image -- QEMU passes it via `-kernel`.

### Build Status and Staleness

```bash
ltvm build-status
ltvm build-all rocky9              # rebuild stale artifacts
ltvm build-all rocky9 --force      # rebuild everything
```

Each artifact tracks an input hash in `meta.json`.
`build-status` shows one image row per built kernel.

## Lustre/Kernel Compatibility Gate

Before any build that touches Lustre, ltvm checks that
the Lustre tree is compatible with the target's kernel
mode. The `lustre.mode` field in `targets.yaml` is
authoritative (values: `server_ldiskfs`, `server_zfs`).

```bash
# Standalone check (read-only, exit 0/1/2)
ltvm validate rocky9 --lustre-tree ~/lustre-release

# Override a compatibility refusal (not hard errors)
ltvm build-all rocky9 --lustre-tree ~/lustre-release --force-compat
ltvm build-kernel rocky9 --lustre-tree ~/lustre-release --force-compat
ltvm build-lustre rocky9 ~/lustre-release --force-compat
ltvm deploy co1-single --build ~/lustre-release --force-compat
```

Exit codes from `ltvm validate`: 0=compatible, 1=warning
(proceed with care), 2=refused (incompatible).

## VM Management

```bash
# Create a VM (idempotent: starts it if stopped, no-ops if running)
ltvm create co1-single --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3

# Deploy Lustre and mount (--build defaults to cwd)
ltvm deploy co1-single --mount
ltvm deploy co1-single --build ~/lustre-release --mount

# Mount / unmount Lustre (clears stale dm targets first)
ltvm llmount co1-single            # dmsetup remove_all + llmount.sh
ltvm llmount co1-single --cleanup  # llmountcleanup.sh + lustre_rmmod

# Execute commands in VM
ltvm exec co1-single 'lctl dl'

# Observe
ltvm console-log co1-single
ltvm dmesg co1-single

# Crash / kdump
ltvm nmi co1-single              # inject NMI -> panic + kdump
ltvm crash-collect co1-single --mod-dir $CO/1

# Destroy
ltvm destroy co1-single
```

**VM naming convention:** always include the checkout
number to avoid collisions: `co<N>-<role>`.

### Clusters

```bash
ltvm cluster create co2 \
    mgs+mds:co2-mds:1 oss:co2-oss:3
ltvm cluster deploy co2 --mount
ltvm cluster exec co2 oss 'lctl dl'
ltvm cluster destroy co2
```

## Target Configuration

Target metadata is centralised in `targets/targets.yaml`.
The legacy per-target `target.conf` / `kernel.conf` files
are superseded by this file.

### targets.yaml schema (per target)

| Key                       | Example            | Description                        |
|---------------------------|--------------------|------------------------------------|
| os_family                 | rhel               | Package manager family             |
| os_name                   | rocky              | Distro name                        |
| os_version                | 9.7                | Full version                       |
| server                    | true               | Include server packages            |
| container_image           | rockylinux:9       | Base container for builds          |
| lustre.mode               | server_ldiskfs     | Compat gate mode                   |
| kernels.default           | 5.14-rhel9.7       | Default kernel version             |
| kernels.available         | [5.14-rhel9.7, ...]| All buildable kernels              |
| kernels.config            | {CONFIG_XEN_PVH: y}| Per-target config overrides        |

The `kernels.default` value maps to files in the Lustre
tree under `lustre/kernel_patches/`:
- `targets/<lustre_target>.target` -- SRPM version
- `kernel_configs/kernel-<ver>-<target>-<arch>.config`
- `series/<target>.series` -- patch series
- `patches/` -- individual patches

### Package Lists

Shared lists in `targets/common/`:
- `packages-base.txt` -- every image
- `packages-server.txt` -- when `server=yes`
- `packages-test.txt` -- test runtime
- `packages-debug.txt` -- profiling and tracing
- `packages-dev.txt` -- build container only

Per-target lists in `targets/<name>/`:
- `packages-os.txt` -- OS-specific packages

Format: one package per line, `#` comments, blank
lines ignored.

For non-RHEL targets, add `package-map.txt` to
translate RHEL package names to distro-specific ones.

## Adding a New Target OS

1. Add an entry in `targets/targets.yaml` with the
   required keys (os_family, os_name, os_version,
   container_image, lustre.mode, kernels.default,
   kernels.available)
2. Create `targets/<name>/` with:
   - `container.Dockerfile` -- build environment
   - `image.Dockerfile` -- VM root filesystem
3. Add `packages-os.txt` for OS-specific packages
4. Add `package-map.txt` if non-RHEL
5. Test: `ltvm build-all <name> --lustre-tree <path>`

## Development

### Interactive Container Shell

```bash
ltvm build-shell rocky9
```

Opens a shell in the build container with the Lustre
source tree bind-mounted.

### Cross-building Lustre

```bash
ltvm build-lustre rocky9 ~/lustre-release
```

Builds Lustre inside the target's build container
against the target's kernel build tree. Output goes
to the Lustre source tree as usual.

### Architecture

- `ltvm_pkg/target_config.py` -- `TargetConfig`: parses
  `targets.yaml`, computes input hashes for staleness,
  manages output directories. `list_targets()` scans
  for all configured targets.

- `ltvm_pkg/lustre_compat.py` -- `validate_target()`:
  checks Lustre tree against target's `lustre.mode`.
  Called automatically by build/deploy commands;
  also exposed as `ltvm validate`.

- `ltvm_pkg/kernel_build.py` -- `build_kernel()`:
  SRPM download (with vault fallback), Lustre patch
  resolution, config fragment assembly, containerized
  build. `parse_lustre_target()` reads the `.target`
  file for SRPM version info.

- `ltvm_pkg/image_build.py` -- `build_image()`: builds
  the container image via podman, exports to raw ext4.
  Output path is `output/<target>/<arch>/images/<kernel>/`.
  Requires root.

- `ltvm_pkg/kernel-build-inner.sh` -- runs inside the
  build container. Extracts SRPM, applies patches,
  merges config, builds vmlinux + bzImage + modules,
  populates build tree.

- `ltvm_pkg/vm_state.py` -- `VMInfo`, `ClusterInfo`:
  on-disk state for VMs/clusters. Path constants and
  `resolve_os_artifacts()` for locating build outputs.

- `ltvm_pkg/vm_commands.py` + `ltvm_pkg/vm_cluster.py` --
  VM and cluster lifecycle handlers called by `cli.py`.

## Code Review Guidance

When reviewing or auditing this codebase, watch for:

- **Subprocess command building.** Never interpolate
  variables into shell strings (`bash -c f"...{x}"`).
  Always use argument lists so subprocess handles
  quoting.

- **Root-required operations.** VM lifecycle commands
  that touch host networking or QEMU launch (create,
  destroy, start, stop, restart, snapshot,
  restore, nmi, doctor, cluster create, cluster destroy)
  require root. Read/observe/SSH commands (exec, ssh,
  console-log, dmesg, deploy, llmount, crash-collect,
  cluster deploy, cluster exec, cluster status, list)
  do not. Build commands do not need root.

- **Compat gate bypass.** `--force-compat` silences
  refusals but not hard errors. Use only when you know
  what you're doing (e.g. testing a WIP Lustre branch
  against a slightly mismatched kernel).

## Rebuilding Pre-built QEMU Binaries

Rocky Linux ships QEMU without microvm support, so we
publish pre-built binaries to GitHub. `ltvm install`
downloads these automatically. The tarball must contain
`bin/qemu-system-x86_64`, `bin/qemu-img`, and
`share/qemu/<firmware files>` (bios-microvm.bin,
linuxboot_dma.bin, etc.).

To rebuild:

```bash
for target in rocky9 rocky10; do
    suffix="el${target#rocky}"
    rm -rf /tmp/qemu-out && mkdir -p /tmp/qemu-out
    podman run --rm -v /tmp/qemu-out:/output:Z ltvm-build-${target} -c '
        dnf -y install glib2-devel pixman-devel flex bison ninja-build \
            python3-pip xz pkg-config
        pip3 install tomli
        curl -fsSL https://download.qemu.org/qemu-9.2.2.tar.xz | tar xJ -C /tmp
        cd /tmp/qemu-9.2.2
        ./configure --target-list=x86_64-softmmu --disable-docs --disable-user \
            --disable-gtk --disable-sdl --disable-vnc --disable-spice \
            --disable-opengl --disable-xen --disable-curl --disable-rbd \
            --disable-libssh --disable-capstone --disable-dbus-display \
            --prefix=/opt/qemu
        make -j$(nproc)
        make install DESTDIR=/output/install
    '
    tar czf "/tmp/qemu-9.2.2-${suffix}.tar.gz" \
        -C /tmp/qemu-out/install/opt/qemu bin share
done

gh release upload qemu-9.2.2 /tmp/qemu-9.2.2-el9.tar.gz --clobber
gh release upload qemu-9.2.2 /tmp/qemu-9.2.2-el10.tar.gz --clobber
```

Notes:
- Rocky 8 needs `dnf install python38` (system python too old)
- Ubuntu uses system QEMU package (has microvm)
- Bump `QEMU_VERSION` in `ltvm_pkg/host_setup.py` when updating

## Issue Tracking

This project uses `bd` (beads) for task tracking.

**Sync model:** beads state is shared by exporting to
JSONL and committing it to git -- not by `dolt push`/
`dolt pull`. If `.beads/` has no `issues.jsonl` after
a `git pull`, the other machine forgot to export.

```bash
bd prime          # session start
bd ready          # find available work
bd show <id>      # view issue
bd update <id> --claim
bd close <id>
```
