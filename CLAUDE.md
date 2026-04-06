# lustre-test-vms-v2 -- Agent and Developer Reference

Build infrastructure for Lustre development/testing
QEMU microVMs. Produces three cacheable artifacts per
target OS: build container, kernel, and VM base image.

## Repository Layout

```
targets/
  common/                   Shared across all targets
    kernel-config.fragment  Microvm kernel config (all targets)
    packages-base.txt       Core OS packages
    packages-server.txt     Lustre server deps
    packages-dev.txt        Build container deps
    packages-test.txt       Test runtime deps (IOR, dbench, etc.)
    packages-debug.txt      Profiling/tracing tools
    rc.local                VM networking init script
  rocky9/                   Per-target directory
    target.conf             OS metadata (family, arch, server)
    kernel.conf             Lustre target + config overrides
    container.Dockerfile    Build container definition
    image.Dockerfile        VM base image definition
    packages-os.txt         OS-specific packages
lib/
  config.py                 Target config parsing, staleness
  commands.py               CLI command implementations
  kernel.py                 Kernel build system
  kernel-build-inner.sh     Runs inside build container
  image.py                  VM image builder (ext4 export)
  vmctl.py                  Subprocess client for qemu/vm.py (sudo boundary)
output/                     Build artifacts (gitignored)
  <target>/
    container.tag
    kernel/
      vmlinux               Unstripped ELF (crash/drgn + boot)
      vmlinuz               Compressed bzImage (kdump)
      build-tree/           For Lustre module builds
      meta.json
    image/
      base.ext4             Raw ext4 root filesystem
      meta.json
```

## Quick Start

```bash
ltvm init rocky9 --lustre-tree /path/to/lustre-release
ltvm status
```

`init` builds all three artifacts (container, kernel,
image) in sequence. Each is independently cacheable --
rebuilds only happen when inputs change.

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

1. Reads `kernel.conf` to find the Lustre target
   (e.g., `5.14-rhel9.7`)
2. Parses the Lustre tree's `.target` file
   (`lustre/kernel_patches/targets/<target>.target`)
   for SRPM version, patch series
3. Downloads the kernel SRPM (cached in
   `output/<target>/cache/`)
4. Resolves the kernel config from the Lustre tree
   (`lustre/kernel_patches/kernel_configs/`)
5. Merges microvm config fragments:
   - `targets/common/kernel-config.fragment` (all targets)
   - `kernel.conf [config]` section (per-target)
6. Builds vmlinux, vmlinuz, modules, and a build tree
   inside the build container

**Outputs:** `output/<target>/kernel/vmlinux`,
`vmlinuz`, `build-tree/` (for Lustre module builds).

### VM Base Image

Minimal root filesystem for QEMU microvm boot.
Built as a container image, then exported to raw ext4.

```bash
ltvm build-image rocky9
```

Requires root (mount, losetup). The image includes:
- All packages from `packages-{base,test,debug,server}.txt`
  plus OS-specific `packages-os.txt`
- Source-built tools: IOR, mdtest, iozone, pjdfstest,
  FlameGraph
- drgn (via pip), Lustre-patched e2fsprogs
- Passwordless root SSH, inter-VM SSH key, serial
  console autologin, kdump configured
- Networking via kernel cmdline (fc_ip, fc_gw, fc_name)

No kernel in image -- QEMU passes it via `-kernel`.

### Status and Staleness

```bash
ltvm status
ltvm update rocky9   # rebuild stale artifacts only
```

Each artifact tracks an input hash in `meta.json`.
Staleness is detected by hashing Dockerfiles, package
lists, kernel config fragments, and the Lustre target
name. Changed inputs trigger a rebuild; unchanged
inputs skip.

## VM Management

These commands subsume the older `vm.sh` interface.

```bash
# Create / ensure a VM
ltvm vm create co1-single \
    --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
ltvm vm ensure co1-single \
    --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3

# Deploy Lustre and mount (--build defaults to cwd)
ltvm deploy co1-single --mount
ltvm deploy co1-single --build ~/lustre-release --mount

# Execute commands in VM
ltvm exec co1-single 'lctl dl'

# Destroy
ltvm vm destroy co1-single
```

### Clusters

```bash
ltvm cluster create co2 \
    mgs+mds:co2-mds:1 oss:co2-oss:3
ltvm cluster deploy co2 --mount
ltvm cluster exec co2 oss 'lctl dl'
ltvm cluster destroy co2
```

**VM naming convention:** always include the checkout
number to avoid collisions: `co<N>-<role>`.

## Target Configuration

### target.conf

INI format under `[target]`:

| Key             | Example       | Description                |
|-----------------|---------------|----------------------------|
| os_family       | rhel          | Package manager family     |
| os_name         | rocky         | Distro name                |
| os_version      | 9             | Major version              |
| server          | yes           | Include server packages    |
| arch            | x86_64        | Build architecture         |
| container_image | rockylinux:9  | Base container for builds  |

### kernel.conf

INI format with two sections:

```ini
[kernel]
lustre_target = 5.14-rhel9.7

[config]
# Per-target kernel config overrides
CONFIG_XEN_PVH=y
```

The `lustre_target` value maps to files in the Lustre
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
translate RHEL package names to the target's names.

## Adding a New Target OS

1. Create `targets/<name>/` with:
   - `target.conf` -- OS metadata
   - `kernel.conf` -- Lustre target + config overrides
   - `container.Dockerfile` -- build environment
   - `image.Dockerfile` -- VM root filesystem
2. Add `packages-os.txt` for OS-specific packages
3. Add `package-map.txt` if non-RHEL (translates
   common package names to distro-specific names)
4. Test: `ltvm init <name> --lustre-tree <path>`

The config parser (`lib/config.py`) auto-discovers
targets by scanning for directories containing
`target.conf`.

## Development

### Interactive Container Shell

```bash
ltvm shell rocky9
```

Opens a shell in the build container with the Lustre
source tree bind-mounted. Useful for debugging build
issues interactively.

### Cross-building Lustre

```bash
ltvm build-lustre rocky9 ~/lustre-release
```

Builds Lustre inside the target's build container
against the target's kernel build tree. Output goes
to the Lustre source tree as usual.

### Architecture

- `lib/config.py` -- `TargetConfig` class: parses
  `target.conf` + `kernel.conf`, computes input hashes
  for staleness detection, manages output directories.
  `list_targets()` scans for all configured targets.

- `lib/kernel.py` -- `build_kernel()`: orchestrates
  SRPM download, Lustre patch resolution, config
  fragment assembly, and containerized build.
  `parse_lustre_target()` reads the `.target` file
  for SRPM version info.

- `lib/image.py` -- `build_image()`: builds the
  container image via podman, exports to raw ext4
  (dd + mkfs + mount + tar extract + resize2fs).
  Requires root.

- `lib/kernel-build-inner.sh` -- runs inside the
  build container. Extracts SRPM, applies patches,
  merges config, builds vmlinux + bzImage + modules,
  and populates the build tree for external module
  builds.

## Code Review Guidance

When reviewing or auditing this codebase, watch for:

- **Functionality duplication between layers.** The
  repo has two deployment paths: single-node
  (`deploy-lustre.sh` / `lib/vmctl.py:deploy`) and
  cluster (`qemu/cluster.py`). Changes to deploy
  logic must be reflected in both, or factored into
  shared code. Check that new features haven't been
  added to one path but not the other.

- **Subprocess command building.** Never interpolate
  variables into shell strings (`bash -c f"...{x}"`).
  Always use argument lists so subprocess handles
  quoting. This applies to `lib/`, `qemu/`, and
  test helpers.

- **sudo boundary.** `lib/vmctl.py` is the boundary
  between user-space code (lib/) and root operations
  (qemu/). Code in lib/ must not assume root. Code
  in qemu/ runs as root via sudo. Don't mix these.

## Issue Tracking

This project uses `bd` (beads) for task tracking.

```bash
bd prime          # session start
bd ready          # find available work
bd show <id>      # view issue
bd update <id> --claim
bd close <id>
```
