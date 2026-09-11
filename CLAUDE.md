# lustre-test-vms-v2 -- Agent and Developer Reference

Build infrastructure for Lustre development/testing using
QEMU microVMs. Produces four cacheable artifacts per
target OS: build container, kernel, VM base image, and
Lustre staging (userland + modules per kernel).

## LLM: Getting the User Set Up

If the user has just opened this repo, walk them through
installation proactively:

```bash
ltvm doctor                    # already installed?
sudo ./ltvm install            # if not: installs QEMU + bridge + dnsmasq + SSH
ltvm target fetch rocky9       # pre-built artifacts (fastest)
# or: ltvm build all rocky9 --lustre-tree ~/lustre-release
```

Ask: **"Where is your Lustre source checkout?"**  Offer to
append `SUGGESTED-AGENTS.md` to their workspace CLAUDE.md:

```bash
cat SUGGESTED-AGENTS.md >> ~/lustre-release/CLAUDE.md
```

## Versioning and git hooks

`pyproject.toml` carries the full version; `BASE_VERSION` in
`ltvm_pkg/__init__.py` carries its major.minor, and `ltvm --version`
reports that plus the short commit hash.

The tracked hooks in `.githooks/` keep both honest, and are enabled per
clone:

```bash
make hooks        # git config core.hooksPath .githooks
```

`pre-commit` runs ruff and mypy, then `.githooks/bump-version` bumps the
patch version when the staged commit touches `ltvm`, `ltvm_pkg/` or
`targets/` -- docs-, test- and hook-only commits do not move it, and a
version edited by hand in the same commit is left alone. It refuses the
commit when `BASE_VERSION` and `pyproject.toml` disagree. `post-commit`
bakes the new hash into `ltvm_pkg/_build_info.py`.

## Agent Skills

`skills/ltvm/` is the skill that teaches an agent to use this
tool: VM and cluster lifecycle, deploying a Lustre tree, the root rules,
crash collection. `ltvm install` links it into `~/.claude/skills` (and
`~/.codex/skills` when Codex is installed) for the invoking user -- under
sudo that is `$SUDO_USER`, not root. `ltvm skills` does only the linking
and `ltvm skills --uninstall` removes it.

Links, not copies: `git pull` or `ltvm update` updates the skill with the
ltvm it describes. It covers *using* ltvm; target configuration, artifact
internals and release mechanics stay in this file.

## Repository Layout

- `targets/` -- `targets.yaml` (source of truth), shared
  `common/` files (kernel fragment, package lists, setup
  scripts), and per-target dirs with `container.Dockerfile` +
  `image.Dockerfile` + `packages-os.txt`.  Per-target
  `variants/` dirs hold optional overlay Dockerfiles.
- `ltvm_pkg/` -- Python package; `cli/` subpackage holds
  per-area dispatch (`build.py`, `targets.py`, `vm.py`,
  `cluster.py`, `deploy.py`, `fetch.py`, `setup.py`), rest
  is implementation.  `ltvm` script at repo root is the CLI.
- `artifacts/<target>/<arch>/{container,kernels/<kver>,images/<kver>[/<variant>]}/`
  -- gitignored build artifacts with a `meta.json` each.
- `docs/` -- operator notes (getting started, releasing
  prebuilt QEMU, nested virtualization, SoftRoCE setup,
  system test plan).

## Quick Start

```bash
sudo ./ltvm install
ltvm target fetch rocky9
ltvm build status
```

## Artifacts

Four cacheable artifacts per (target, arch, variant):
**build container**, **kernel**, **VM base image**, and
**Lustre staging** (userland + modules per kernel,
written into the Lustre tree's `.ltvm-staging/`).  The
first three each track an `input_hash` in their
`meta.json`; `ltvm build status` reports staleness.
Images are keyed per-kernel (so multiple kernel minors
can coexist).

```bash
ltvm build container rocky9
ltvm build kernel rocky9 --lustre-tree ~/lustre-release
ltvm build image rocky9                          # default kernel
ltvm build image rocky9 --kernel 5.14-rhel9.5    # specific kernel
ltvm build all rocky9 --lustre-tree ~/lustre-release  # stale only
ltvm build all rocky9 --lustre-tree ~/lustre-release --force  # everything
ltvm build mofed-kmods rocky9 --variant mofed-24 # MOFED kmods per variant
```

All build commands accept `--arch <arch>` to override
the target's configured architecture (e.g. `aarch64`).

### Pruning stale artifacts

`ltvm clean` walks `artifacts/` and previews superseded
kernel builds, off-list kernel groups (no longer in
`kernels.available`), and orphan images (no matching
kernel).  Default is dry-run; pass `--apply` to delete.

```bash
ltvm clean                      # dry-run, all targets/arches
ltvm clean rocky9               # one target
ltvm clean --older-than 30 --apply
ltvm clean --keep 2 --apply     # keep 2 most recent per group
```

Always preserved (unless `--force`): the target's default
kernel and any variant-pinned kernel.  Distinct from
`ltvm target clean`, which wipes a single target's whole
arch dir in one shot.

### Kernel build inputs (from Lustre tree)

`ltvm build kernel` parses the Lustre tree for the
target's slice: `lustre/kernel_patches/`
- `targets/<lustre_target>.target` -- SRPM version
- `kernel_configs/kernel-<ver>-<target>-<arch>.config`
- `series/<target>.series` + `patches/` -- patch series

then merges [targets/common/kernel-config.fragment](targets/common/kernel-config.fragment)
(plus the per-target `kernels.config` from `targets.yaml`)
and builds vmlinux/vmlinuz/modules/build-tree inside the
build container.  SRPMs cache under `artifacts/<target>/<arch>/cache/`
with a Rocky-vault fallback for older minors.

### Image

Built as a container via podman, exported to ext4 with
`mke2fs -d` under fakeroot (rootless).  Installs
`packages-{base,server,test,debug}.txt` +
`packages-os.txt`, source-built tools (IOR, mdtest,
iozone, pjdfstest, FlameGraph, drgn, Lustre-patched
e2fsprogs), passwordless root SSH, serial console
autologin, kdump pre-configured (vmlinuz + initramfs
baked in at build time).  No kernel inside the image --
QEMU passes it via `-kernel`.

## Exporting Images

`ltvm target export` repackages a built `base.ext4` + its
kernel + a BIOS GRUB2 bootloader into one self-contained
bootable disk -- for people (or clouds) that don't have
the ltvm runtime.

```bash
ltvm target export rocky9                      # bootable qcow2
ltvm target export rocky9 --format raw
ltvm target export rocky9 --format gce \
    --disk-size-gb 20 --ssh-key ~/.ssh/id_ed25519.pub
```

`--format gce` writes the `disk.raw` tar.gz (oldgnu format,
whole-GiB disk) that Google Compute Engine's custom-image
import requires, and fixes up the guest for it: an explicit
NetworkManager DHCP profile for eth0, because ltvm's own
images take their address from the `fc_ip=` kernel cmdline
that GCE never passes.  The fstab `/` entry is rewritten to
`UUID=` for *every* format -- the image ships `/dev/vda`,
which is right only for ltvm's unpartitioned microvm boot.

The image ships no Google guest agent, so GCE cannot inject
SSH keys: pass `--ssh-key` to bake one in.  It also keeps
ltvm's lab defaults (root login, empty password) -- don't
open port 22 to the world.

## Lustre/Kernel Compatibility Gate

`ltvm` checks Lustre tree compatibility with the target's
`lustre.mode` (`server_ldiskfs` / `server_zfs` / `client`)
before any Lustre-involving build.

```bash
ltvm target validate rocky9 --lustre-tree ~/lustre-release
# Exit: 0 compatible, 1 warning, 2 refused

# Bypass a refusal (not hard errors):
ltvm build all rocky9 --lustre-tree ~/lustre-release --force-compat
```

## VM Management

```bash
ltvm create co1-single --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
ltvm deploy-lustre co1-single --lustre-tree ~/lustre-release --mount
ssh co1-single 'lctl dl'
ltvm llmount co1-single               # mount
ltvm llumount co1-single              # unmount (= llmount --cleanup)
ltvm vm console-log co1-single
ltvm vm nmi co1-single                # inject NMI -> kdump
ltvm vm crash-collect co1-single --mod-dir $CO/1
ltvm destroy co1-single
```

**Owner/session metadata:** New VMs persist an advisory opaque `owner_id`.
Agent controllers should export `LTVM_OWNER_ID=<durable-session-id>` before
running normal create commands. `--owner ID` / `--owner-id ID` override the
environment; otherwise LTVM uses `pid:<invoking-ltvm-pid>`. Cluster create
resolves once and applies the same owner to every member. Discover it through
`ltvm list --json`; legacy VMs report `owner_id: null`. See
[docs/VM_OWNERSHIP.md](docs/VM_OWNERSHIP.md).

**Naming:** always include the checkout number: `co<N>-<role>`.

**Root:** Run `create`, `destroy`, `start`, `stop`, and `doctor` as
the invoking user; they prompt once and elevate only the individual host
operations that need it. `update`, `cluster create`, and `cluster destroy`
still require root. `build *`, `target *`, `deploy-lustre`, `llmount`,
`list`, `vm *`, and the remaining `cluster` actions do not.

### Running ltvm inside a VM it built

`deploy-lustre` runs on the build host and pushes Lustre
*into* a VM over ssh.  The `make-*` commands are the other
direction: ltvm running **inside** a machine it produced --
an ltvm VM, or a cloud node booted from `ltvm target export
--format gce` -- installing Lustre onto that machine's own
root filesystem.

```bash
# on the node, from a Lustre checkout:
ltvm make-install --lustre-tree ~/lustre-release
ltvm make-uninstall
ltvm make-reinstall --lustre-tree ~/lustre-release
```

The build still happens in the target's build container
(the VM image ships the runtime packages, not the
toolchain), so the node needs podman plus a
`ltvm target fetch <target>` first.

On an EL8/EL9 image the distro `python3` (3.6 / 3.9) is
below ltvm's 3.10 floor, so run ltvm with the 3.11 the
image now ships:

```bash
dnf install -y podman            # not in the image
python3.11 /path/to/ltvm make-install --lustre-tree .
```

Images built before this was added need
`dnf install -y python3.11 python3.11-pyyaml` too.

**Two preconditions**, both enforced for all three
commands: you must be on a machine ltvm built, and inside
a Lustre source tree.  They unpack a tree onto `/`, and
the machine where you'd most likely type one by accident
is your build host.  `--force` overrides the first.

Being an ltvm machine is established by any of three, in
order:

1. `/etc/ltvm-image.json`, baked in by `ltvm build image`
   -- authoritative, and the only one that names the
   variant and kernel;
2. ltvm's kernel cmdline (`fc_ip=` / `fc_name=`), which
   `qemu_run` passes -- this is what identifies every VM
   from an image built before the stamp existed;
3. ltvm's setup scripts under `/usr/local/sbin` -- what
   identifies a cloud node, which gets no ltvm cmdline.

Target resolution prefers the stamp, then `--target`, then
an `/etc/os-release` guess -- and an ambiguous guess
(rocky9 vs rocky9-64k) is an error asking for `--target`,
not a coin flip.  Without a stamp the kernel is matched to
whatever is **running** rather than the target's default,
since modules built for the wrong release won't load.

`make-uninstall` works from the manifest at
`/var/lib/ltvm/lustre-install.json` that install writes, not
from `make uninstall` (the node has no configured source
tree), so it removes exactly what ltvm put there.  It
unloads Lustre modules first and refuses if they won't
unload; `--no-unload` and `--force` override.

Verified end-to-end by the three scripts under `tests/e2e/`
(deliberately not named `test_*.py`, and excluded from pytest
collection -- they write to `/`, so run them only in a
throwaway machine):
[local_install_rootfs.py](tests/e2e/local_install_rootfs.py)
(any Linux rootfs),
[make_install_vm.py](tests/e2e/make_install_vm.py) (a real
Lustre DESTDIR in a real ltvm VM: depmod, `modprobe
lustre`, then unloading it again), and
[export_pipeline_rootfs.py](tests/e2e/export_pipeline_rootfs.py)
(the real `target export` pipeline).

### Clusters

```bash
sudo ltvm cluster create co2 mgs+mds:co2-mds:1 oss:co2-oss:3
ltvm cluster deploy co2 --mount
ltvm cluster exec co2 oss 'lctl dl'    # runs on EVERY oss node
ltvm cluster exec co2 co2-oss2 'lctl dl'   # or one node by name
ltvm cluster ssh co2 mds               # interactive; one node
ltvm cluster status co2
ltvm cluster list
sudo ltvm cluster destroy co2
```

`cluster exec <role>` fans out across every node holding the role and
exits non-zero if any node did; `cluster ssh <role>` opens a session on
the first, since it execs a single interactive ssh.

## Target Configuration

Targets live in [targets/targets.yaml](targets/targets.yaml).
Per-target keys:

| Key | Example | Description |
|---|---|---|
| os_family | rhel | Package manager family |
| os_name / os_version | rocky / 9.7 | Distro + version |
| container_image | rockylinux:9 | Build-container base |
| lustre.mode | server_ldiskfs | Compat gate mode |
| kernels.default | 5.14-rhel9.7 | Default kernel |
| kernels.available | [5.14-rhel9.7, ...] | Buildable kernels |
| kernels.config | {CONFIG_XEN_PVH: y} | Per-target config overrides |
| variants | {mofed-24: {...}} | Optional add-on variants |

### Package Lists

Shared in `targets/common/`:
`packages-base.txt` (every image),
`packages-server.txt` (when server-mode),
`packages-test.txt`, `packages-debug.txt`,
`packages-dev.txt` (build container only).
Per-target `packages-os.txt` adds OS-specific packages.
Non-RHEL targets add `package-map.txt` to translate names.

Format: one package per line, `#` comments, blanks OK.

## Adding a New Target OS

1. Add a `targets.yaml` entry (required keys above).
2. Create `targets/<name>/` with `container.Dockerfile`,
   `image.Dockerfile`, `packages-os.txt`.
3. `package-map.txt` if non-RHEL.
4. `ltvm build all <name> --lustre-tree <path>`.

For a new kernel minor on an **existing** OS, just add
the short name to `kernels.available` -- no Dockerfile
changes needed as long as the Lustre tree has the
`.target` / `.series` / `.config` for it.

### Variants

A target may declare `variants:` in `targets.yaml` --
overlay Dockerfiles (under `targets/<name>/variants/`)
that layer on top of the base container/image, pinned
to a specific kernel.  See rocky9's `mofed-24` for the
canonical example: an overlay container/image pair plus
a kernel pin and `params:` consumed by the Dockerfile.

## Development

### Interactive container shell

```bash
ltvm build shell rocky9
```

### Cross-building Lustre

```bash
ltvm build lustre rocky9 --lustre-tree ~/lustre-release
```

Builds inside the target's build container against the
target's kernel build tree.  Output goes to the Lustre
tree's `.ltvm-staging/<target>/<arch>/<kernel>[/<variant>]/`.

Incremental by default: repeating the command rebuilds
only what changed.  `make distclean` runs for `--force`,
or when `.ltvm-last-build` -- a claim stamp naming the
`<target> <arch> <variant> <kver>` the tree's shared
autoconf state (config.h, config.cache, `.deps`, staged
ldiskfs sources) currently belongs to -- names anything
other than the build about to run.  So switching targets
in one source tree distcleans on each switch, and staying
on one target never does.  autogen + configure re-run on
a narrower condition still (`_needs_reconfigure`).

## Release Manifest Schema

Each published release carries `"schema": "ltvm-release/<N>"`
in its `manifest-*.json`.  Fetch refuses any version it
doesn't explicitly recognize -- no forward/backward-compat
muddling.

Source of truth: `SCHEMA_VERSION` in
[ltvm_pkg/release_package.py](ltvm_pkg/release_package.py).
Writer and fetch-side check both read it, so they can't
drift.

**Bump when** an older ltvm couldn't consume the new
release: asset renames, content/compression changes,
manifest shape changes, per-variant scoping changes,
extraction-path changes, module-injection changes.

**Don't bump for** additive changes an old fetcher can
safely ignore (optional manifest fields, new target OSes,
new variants under existing scheme).

**Procedure:** edit `SCHEMA_VERSION`, add a one-line entry
to the bump-history comment above it, republish every
release that should stay fetchable.  Old clients get a
clear "upgrade ltvm" error and (interactive) an update
prompt via [ltvm_pkg/update_check.py](ltvm_pkg/update_check.py).

## Code Review Guidance

Watch for:

- **Subprocess command building.** Never interpolate into
  shell strings (`bash -c f"...{x}"`).  Use argument lists.
- **Root-required operations.** Single-VM lifecycle commands elevate
  individual host operations internally; do not require users to invoke
  the whole command through sudo. Cluster create/destroy still require
  root. Read/observe (console-log,
  deploy-lustre, llmount, crash-collect, cluster
  deploy/exec/status, list) don't.  Build commands don't.
- **`--force-compat`** silences compat *refusals* but not
  hard errors -- only for known WIP branches.

## Issue Tracking

Two trackers, by scope:

- **`bd` (beads)** -- local, session-scoped work (bugs
  mid-task, short-lived TODOs).  Fast, doesn't clutter
  the public tracker.  State syncs via JSONL committed
  to git.  Exports to `.beads/issues.jsonl`.
- **GitHub Issues** on `lustre-tools/lustre-test-vms` --
  longer-term work, feature requests, external-visible.

Rule of thumb: under a week → bead.  Month-plus → GH
issue.  Migrate beads to GH issues when they age out.

```bash
bd ready / bd show <id> / bd update <id> --claim / bd close <id>
gh issue list / view <n> / create --title ... --body ...
```

## See Also

- [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) --
  first-time setup walkthrough.
- [docs/RELEASING.md](docs/RELEASING.md) -- rebuilding the
  pre-built QEMU tarballs.
- [docs/NESTED_VIRTUALIZATION.md](docs/NESTED_VIRTUALIZATION.md)
  -- running ltvm under a nested hypervisor.
- [docs/SOFTROCE_SETUP.md](docs/SOFTROCE_SETUP.md) -- LNet
  o2iblnd over SoftRoCE.
- [docs/SYSTEM_TEST_PLAN.md](docs/SYSTEM_TEST_PLAN.md) --
  end-to-end test matrix.
