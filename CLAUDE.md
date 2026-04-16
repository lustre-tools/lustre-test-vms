# lustre-test-vms-v2 -- Agent and Developer Reference

Build infrastructure for Lustre development/testing using
QEMU microVMs. Produces three cacheable artifacts per
target OS: build container, kernel, and VM base image.

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

## Repository Layout

- `targets/` -- `targets.yaml` (source of truth), shared
  `common/` files (kernel fragment, package lists, setup
  scripts), and per-target dirs with `container.Dockerfile` +
  `image.Dockerfile` + `packages-os.txt`.
- `ltvm_pkg/` -- Python package; `cli.py` dispatches, rest
  is implementation.  `ltvm` script at repo root is the CLI.
- `output/<target>/<arch>/{container,kernels/<kver>,images/<kver>[/<variant>]}/`
  -- gitignored build artifacts with a `meta.json` each.
- `docs/` -- operator notes (e.g. releasing prebuilt QEMU).

## Quick Start

```bash
ltvm install
ltvm target fetch rocky9
ltvm build status
```

## Artifacts

Three cacheable artifacts per (target, arch, variant):
**build container**, **kernel**, **VM base image**.  Each
tracks an `input_hash` in its `meta.json`; `ltvm build
status` reports staleness.  Images are keyed per-kernel
(so multiple kernel minors can coexist).

```bash
ltvm build container rocky9
ltvm build kernel rocky9 --lustre-tree ~/lustre-release
ltvm build image rocky9                          # default kernel
ltvm build image rocky9 --kernel 5.14-rhel9.5    # specific kernel
ltvm build all rocky9 --lustre-tree ~/lustre-release  # stale only
ltvm build all rocky9 --force                    # everything
```

### Kernel build inputs (from Lustre tree)

`ltvm build kernel` parses the Lustre tree for the
target's slice: `lustre/kernel_patches/`
- `targets/<lustre_target>.target` -- SRPM version
- `kernel_configs/kernel-<ver>-<target>-<arch>.config`
- `series/<target>.series` + `patches/` -- patch series

then merges [targets/common/kernel-config.fragment](targets/common/kernel-config.fragment)
(plus the per-target `kernels.config` from `targets.yaml`)
and builds vmlinux/vmlinuz/modules/build-tree inside the
build container.  SRPMs cache under `output/<target>/<arch>/cache/`
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
ltvm deploy-lustre co1-single --build ~/lustre-release --mount
ssh co1-single 'lctl dl'
ltvm llmount co1-single [--cleanup]   # mount / unmount
ltvm vm console-log co1-single
ltvm vm nmi co1-single                # inject NMI -> kdump
ltvm vm crash-collect co1-single --mod-dir $CO/1
ltvm destroy co1-single
```

**Naming:** always include the checkout number: `co<N>-<role>`.

### Clusters

```bash
ltvm cluster create co2 mgs+mds:co2-mds:1 oss:co2-oss:3
ltvm cluster deploy co2 --mount
ltvm cluster exec co2 oss 'lctl dl'
ltvm cluster destroy co2
```

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

## Development

### Interactive container shell

```bash
ltvm build shell rocky9
```

### Cross-building Lustre

```bash
ltvm build lustre rocky9 ~/lustre-release
```

Builds inside the target's build container against the
target's kernel build tree.  Output goes to the Lustre
tree's `.ltvm-staging/<target>/<arch>/<kernel>[/<variant>]/`.

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
- **Root-required operations.** VM lifecycle touching host
  networking or QEMU launch (create, destroy, start, stop,
  `vm snapshot/restore/nmi`, doctor, cluster
  create/destroy) need root.  Read/observe (console-log,
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

- [docs/RELEASING.md](docs/RELEASING.md) -- rebuilding the
  pre-built QEMU tarballs.
