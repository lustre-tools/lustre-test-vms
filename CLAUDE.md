# lustre-test-vms -- Agent and Developer Reference

Build infrastructure for Lustre development/testing using
QEMU microVMs. Produces three cacheable artifacts per
target OS: build container, kernel, and VM base image.

## LLM: Getting the User Set Up

If the user has just opened this repo, walk them through
installation proactively. Don't wait for them to ask.

### Step 1 — Install ltvm

Check whether it's already installed:

```bash
ltvm doctor
```

If not found, run this from the repo directory (requires sudo — it
installs QEMU, configures the host bridge and dnsmasq, sets up SSH,
and symlinks `ltvm` into `/usr/local/bin`):

```bash
sudo ./ltvm install
```

### Step 2 — Fetch pre-built artifacts

Unless the user wants to build from source, fetch is
faster (downloads ~1.4 GB: kernel + VM image):

```bash
ltvm fetch rocky9
```

Verify everything landed:

```bash
ltvm build-status
```

If they want to build from source instead (e.g. they have
a custom kernel config), skip fetch and do:

```bash
ltvm build-all rocky9 --lustre-tree ~/lustre-release
```

### Step 3 — Find out where their Lustre tree lives

Ask the user: **"Where is your Lustre source checkout?"**
You'll need that path in the next step.

### Step 4 — Set up their workspace CLAUDE.md

This repo ships a ready-made agent config template at
`SUGGESTED-AGENTS.md`. Offer to append it to the user's
Lustre workspace CLAUDE.md (or AGENTS.md), with the
placeholder paths replaced with their actual locations:

```bash
# Preview what will be added:
cat SUGGESTED-AGENTS.md
```

Before writing, replace:
- `~/lustre-test-vms` → actual path to this repo
- `~/lustre-release`  → actual path to their Lustre tree

Then append to their workspace config:

```bash
cat SUGGESTED-AGENTS.md >> ~/lustre-release/CLAUDE.md
# or AGENTS.md, whichever they use
```

Once that's done, when the user opens their Lustre
workspace, any LLM reading their CLAUDE.md will know
exactly how to build, deploy, and test Lustre using ltvm.

---

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
ltvm_pkg/                   Python package (CLI + all implementation)
  cli.py                    CLI command implementations (cmd_* functions)
  target_config.py          Target config parsing, staleness detection
  kernel_build.py           Kernel build (SRPM + patches + config)
  kernel-build-inner.sh     Runs inside build container
  image_build.py            VM image builder (Dockerfile → ext4)
  lustre_build.py           Lustre build (containerized)
  release_package.py        Package and fetch GitHub release artifacts
  build_validate.py         Pipeline validation suite
  host_setup.py             Host setup, verify, WSL2 helpers
  download.py               Robust file downloader
  vm_state.py               VMInfo, ClusterInfo, paths, constants
  vm_net.py                 TAP, bridge, DNS, SSH registry
  vm_commands.py            Single-VM CLI handlers
  vm_cluster.py             Multi-node cluster management
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

### Build Status and Staleness

```bash
ltvm build-status
ltvm build-all rocky9              # rebuild stale artifacts
ltvm build-all rocky9 --force      # rebuild everything
```

Each artifact tracks an input hash in `meta.json`.
Staleness is detected by hashing Dockerfiles, package
lists, kernel config fragments, and the Lustre target
name. Changed inputs trigger a rebuild; unchanged
inputs skip.

## VM Management

```bash
# Create / ensure a VM
ltvm create co1-single --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
ltvm ensure co1-single --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3

# Deploy Lustre and mount (--build defaults to cwd)
ltvm deploy co1-single --mount
ltvm deploy co1-single --build ~/lustre-release --mount

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
   - `container.Dockerfile` -- build environment (copy from `targets/rocky9/`)
   - `image.Dockerfile` -- VM root filesystem (copy from `targets/rocky9/`)
2. Add `packages-os.txt` for OS-specific packages
3. Add `package-map.txt` if non-RHEL (translates
   common package names to distro-specific names)
4. Test: `ltvm build-all <name> --lustre-tree <path>`

## Development

### Interactive Container Shell

```bash
ltvm build-shell rocky9
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

- `ltvm_pkg/target_config.py` -- `TargetConfig` class: parses
  `target.conf` + `kernel.conf`, computes input hashes
  for staleness detection, manages output directories.
  `list_targets()` scans for all configured targets.

- `ltvm_pkg/kernel_build.py` -- `build_kernel()`: orchestrates
  SRPM download, Lustre patch resolution, config
  fragment assembly, and containerized build.
  `parse_lustre_target()` reads the `.target` file
  for SRPM version info.

- `ltvm_pkg/image_build.py` -- `build_image()`: builds the
  container image via podman, exports to raw ext4
  (dd + mkfs + mount + tar extract + resize2fs).
  Requires root.

- `ltvm_pkg/kernel-build-inner.sh` -- runs inside the
  build container. Extracts SRPM, applies patches,
  merges config, builds vmlinux + bzImage + modules,
  and populates the build tree for external module
  builds.

- `ltvm_pkg/vm_state.py` -- `VMInfo`, `ClusterInfo`: on-disk
  state for running VMs and clusters. Path constants
  and `resolve_os_artifacts()` for locating build outputs.

- `ltvm_pkg/vm_commands.py` + `ltvm_pkg/vm_cluster.py` --
  VM and cluster lifecycle handlers called by `ltvm_pkg/cli.py`.

## Code Review Guidance

When reviewing or auditing this codebase, watch for:

- **Subprocess command building.** Never interpolate
  variables into shell strings (`bash -c f"...{x}"`).
  Always use argument lists so subprocess handles
  quoting. This applies to `ltvm_pkg/` and test helpers.

- **Root-required operations.** VM lifecycle commands
  (`vm_commands.py`, `vm_cluster.py`, `vm_net.py`)
  require root. `cli.py` calls `_require_root()` before
  dispatching to them. Build commands do not need root.

## Rebuilding Pre-built QEMU Binaries

Rocky Linux ships QEMU without microvm support, so we publish
pre-built binaries to GitHub.  `ltvm install` downloads these
automatically and extracts them as a `/opt/qemu` overlay.

The tarball must contain `bin/qemu-system-x86_64`, `bin/qemu-img`,
and `share/qemu/<firmware files>` (bios-microvm.bin, linuxboot_dma.bin,
etc.).  `_fetch_prebuilt_qemu` validates that the firmware files are
present after extract; if they're missing, microvm boots fail at
runtime with cryptic "could not load PC BIOS" errors.

To rebuild:

```bash
# Build in each target's container
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
        # `make install DESTDIR=...` lays out bin/, share/qemu/<firmware>,
        # libexec/, etc. under DESTDIR/opt/qemu/.  We then tar from there
        # so the resulting archive is a /opt/qemu overlay.
        make install DESTDIR=/output/install
    '
    tar czf "/tmp/qemu-9.2.2-${suffix}.tar.gz" \
        -C /tmp/qemu-out/install/opt/qemu bin share
done

# Publish (updates existing release)
gh release upload qemu-9.2.2 /tmp/qemu-9.2.2-el9.tar.gz --clobber
gh release upload qemu-9.2.2 /tmp/qemu-9.2.2-el10.tar.gz --clobber
```

Notes:
- Rocky 8 needs `dnf install python38` (system python is too old)
- Ubuntu uses the system QEMU package (has microvm)
- Bump `QEMU_VERSION` in `ltvm_pkg/host_setup.py` when updating
- The tarball MUST include the `share/qemu/` firmware files; just
  copying the binaries (the old approach) leaves microvm boots
  broken at runtime.

## Issue Tracking

This project uses `bd` (beads) for task tracking.

**Sync model:** we do NOT use Dolt remotes. Beads state is
shared across machines by exporting to JSONL and committing
it to git — not by `dolt push`/`dolt pull`. If `.beads/` has
no `issues.jsonl` after a `git pull`, the other machine
forgot to export before pushing; ask the user rather than
assuming the tracker is empty.

```bash
bd prime          # session start
bd ready          # find available work
bd show <id>      # view issue
bd update <id> --claim
bd close <id>
```

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
