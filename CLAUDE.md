# lustre-test-vms-v2 -- Agent and Developer Reference

Build infrastructure for Lustre development/testing using
QEMU microVMs. Produces four cacheable artifacts per
target OS: build container, kernel, VM base image, and
Lustre staging (userland + modules per kernel), plus an
optional fifth -- ZFS -- when a build asks for it.

## LLM: Getting the User Set Up

If the user has just opened this repo, walk them through
installation proactively:

```bash
ltvm doctor                    # already installed?
sudo ./ltvm install            # if not: installs QEMU + bridge + dnsmasq + SSH
ltvm target fetch rocky9       # pre-built artifacts (fastest)
# or: ltvm build all rocky9 --lustre-tree ~/lustre-release
```

On macOS run `./ltvm install` *without* sudo: it refuses to run
as root there (Homebrew will not) and elevates only the steps
that need it -- see "Running on macOS" in README.md.

Ask: **"Where is your Lustre source checkout?"**  The usage
guidance an agent needs is the `ltvm` skill, which `ltvm
install` links into their skill directories -- there is
nothing to copy into a workspace CLAUDE.md.

## Versioning and git hooks

The version comes from git, not from a file that every commit edits:
`git describe` against the newest annotated `vMAJOR.MINOR` tag, so
`v0.5-114-g71992dd888ab` is reported as `0.5.114+71992dd888ab` -- commits
since the tag, plus the hash ([ltvm_pkg/version_info.py](ltvm_pkg/version_info.py)).
Only `v*` tags count; the artifact and QEMU release tags share the repo.
Without the tag (a shallow clone) it is `BASE_VERSION+<hash>`, and
without git just `BASE_VERSION`.

A new minor or major version is a new tag on master, with `BASE_VERSION`
in `ltvm_pkg/version_info.py` and `version` in `pyproject.toml` moved to
match:

```bash
git tag -a v0.6 -m "ltvm 0.6" && git push origin v0.6
```

The tracked hooks in `.githooks/` are enabled per clone:

```bash
make hooks        # git config core.hooksPath .githooks
```

`pre-commit` runs ruff and mypy.  `post-commit` bakes the version into
`ltvm_pkg/_build_info.py` (gitignored), as `ltvm update` does after a
pull, so `ltvm --version` does not run git on every invocation.

## Agent Skills

`skills/ltvm/` is the skill that teaches an agent to use this
tool: VM and cluster lifecycle, deploying a Lustre tree, the root rules,
crash collection. `ltvm install` links it into `~/.claude/skills` (and
`~/.codex/skills` when Codex is installed) for the invoking user -- under
sudo that is `$SUDO_USER`, not root. `ltvm skills` does only the linking
and `ltvm skills --uninstall` removes it. `ltvm doctor` reports links
that are missing and makes them with `--fix`, which is what catches a
host installed before the skills existed. A skill directory that is not
a symlink is someone else's: doctor names it and `--fix` leaves it
alone.

Links, not copies: `git pull` or `ltvm update` updates the skill with the
ltvm it describes. It covers *using* ltvm; target configuration, artifact
internals and release mechanics stay in this file.

## Tab Completion

Two modules, split along the line between *what* to offer and
*how a shell gets asked*:

- `ltvm_pkg/completion.py` -- the argcomplete completers.  Every one is
  wrapped in `_safe`, because an exception here lands as a traceback in
  the user's prompt.  They read live state (targets.yaml, VM and
  cluster files, snapshot tags via `qemu-img snapshot -l -U`), so they
  stay right without a word list to maintain.
- `ltvm_pkg/shell_completion.py` -- generating and installing the
  per-shell registration, via `argcomplete.shellcode()` in-process (not
  the `register-python-argcomplete` script, which lives in the venv's
  `bin/` that `sudo ltvm install` has no PATH for).

`ltvm install` installs for every shell on the host; `ltvm completion`
prints or installs it by hand, and `ltvm doctor` reports missing or
stale files and writes them with `--fix`.  Writes go through
`priv.atomic_write`, so only the write elevates -- the command itself
does not need root.

`ltvm install --verify` reports it too (`host_setup.verify`), but
deliberately *not* as part of `all_ok`: a file gone stale across a
version bump is normal and self-heals on the next install, so failing
that command's exit code over it would cry wolf.  `ltvm doctor` is the
one that exits non-zero and fixes it.

Wiring lives in `ltvm`: `_COMPLETERS_BY_OPTION` is attached across the
whole subparser tree by `_attach_completers(p)` at the end of
`build_parser`, so an option that means the same thing everywhere
(`--arch`, `--lustre-tree`) is covered once and a new subcommand is
covered for free.  It only fills arguments with no completer, so a
per-command assignment always wins.

Three traps worth knowing:

- **`create --kernel` is a version name**, despite reading like a
  path: `_resolve_os_and_kernel` hands it to `resolve_os_artifacts`,
  which matches it against artifact *directory* names, so a real path
  gets "No kernel matching".  It is in the by-option table with every
  other `--kernel` for that reason.  `create --image` genuinely is a
  path and stays out, where argcomplete's default `FilesCompleter` is
  the right answer -- as it is for `--ssh-key`, `--tarball`, `--output`.
- **zsh needs the `#compdef` wrapper.**  An autoloaded `_ltvm`'s body
  *is* the completion function, so the bare shellcode would define
  `_python_argcomplete` and return -- first TAB empty, second one works.
  `shellcode("zsh")` adds the header and a trailing call.
- **`completion` is excluded from telemetry and the update check** in
  `main()`.  The documented usage is `eval "$(ltvm completion)"` from a
  shell rc file, so it runs once per terminal opened.

`LTVM_COMPLETION_ROOT` prefixes every system path -- for staging into an
image, and for the test suite, which sets it in `tests/conftest.py` so
`ltvm doctor --fix` under pytest cannot rewrite the developer's real
`/etc/bash_completion.d`.

macOS takes its directories from the Homebrew prefix instead
(`_SEARCH_MACOS`): `/usr/share` there is on the SIP-sealed system
volume, which not even root can write.  The prefix is the user's, so
nothing elevates, and it is found without running `brew`.

## Repository Layout

- `targets/` -- `targets.yaml` (source of truth), shared
  `common/` files (kernel fragment, package lists, setup
  scripts), and per-target dirs with `container.Dockerfile` +
  `image.Dockerfile` + `packages-os.txt`.  Per-target
  `variants/` dirs hold optional overlay Dockerfiles.
- `ltvm_pkg/` -- Python package; `cli/` subpackage holds
  per-area dispatch (`build.py`, `clean.py`, `cluster.py`,
  `deploy.py`, `fetch.py`, `make.py`, `setup.py`,
  `targets.py`, `telemetry.py`, `vm.py`) plus shared
  `util.py`; rest is implementation.  `ltvm` script at repo
  root is the CLI.
- `artifacts/<target>/<arch>/{container,kernels/<kver>,images/<kver>[/<variant>]}/`
  -- gitignored build artifacts with a `meta.json` each.
  ZFS, when built, lands at `kernels/<kver>/zfs/<version>/`.
- `docs/` -- operator notes (getting started, releasing
  prebuilt QEMU, nested virtualization, SoftRoCE setup,
  system test plan, VM ownership, IPv6).

## Quick Start

```bash
sudo ./ltvm install                # macOS: no sudo (see above)
ltvm target fetch rocky9
ltvm build status
```

## Artifacts

Four cacheable artifacts per (target, arch, variant):
**build container**, **kernel**, **VM base image**, and
**Lustre staging** (userland + modules per kernel,
written into the Lustre tree's `.ltvm-staging/`).  The
first three each track an `input_hash` in their
`meta.json`; `ltvm build status` reports staleness, and
`--why` names the input that moved.
Images are keyed per-kernel (so multiple kernel minors
can coexist).

### input_hash is load-bearing -- do not perturb it

`input_hash` *is* the staleness decision, so producing a
different value for unchanged inputs invalidates every
artifact on every machine at once, costing a kernel rebuild
per target.  Nothing warns you; builds just start running.

`TargetConfig._hash_parts` feeds the bytes through
`_HashParts`, which records them under labels while
concatenating them unchanged -- so `input_hash()` (the
digest) and `input_components()` (the per-input digests
`--why` diffs, also stored in `meta.json`) come from one
code path.  Keep it that way: an explanation that disagrees
with the rebuild decision is worse than none.

[tests/test_input_hash_stability.py](tests/test_input_hash_stability.py)
pins the digest for every target and artifact.  If it fails,
assume you changed the hash by accident.  When the change is
deliberate, update the goldens in the same commit -- that
test failing is the one signal anybody gets before the
rebuilds start.

Three cases `--why` answers honestly rather than
plausibly, all worth preserving: an artifact built before
the per-input digests existed reports the cause as unknown
(not "nothing changed"); the kernel's `lustre-tree-inputs`
component is never blamed, because `build status` has no
Lustre tree to recompute it from; and a hash that moved with
no component accounting for it says exactly that.

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

### Image files are never replaced

A VM's root disk is a qcow2 overlay backed by an image file
*by path*, holding only the blocks the VM wrote.  Renaming a
rebuilt image over that path left running VMs fine (QEMU
holds the old inode) and every stopped one booting its blocks
over a different filesystem: "No working init found".  So
([ltvm_pkg/image_store.py](ltvm_pkg/image_store.py)):

- Each build, and each fetch, lands as
  `base-<UTC stamp>-<hex>.ext4`; `current.ext4`, a relative
  symlink beside it, names the one new VMs get, and is
  swapped atomically.  `current_image(dir)` is the only way
  to find "the image" -- status, export, publish, doctor and
  create all go through it.
- `create` hands qemu-img the **resolved** versioned file
  (and resolves a symlinked `--image`), so no later build can
  move an overlay's base.
- `base.ext4` is the legacy name, the plain file pre-scheme
  overlays name.  Nothing writes it again: it is current only
  in a directory with no `current.ext4`.  Do not turn it into
  a symlink or a pointer -- old overlays would follow it.
- Publish still ships the current image as `base.ext4`
  (tar `-h` through a tree of symlinks), so the release
  format is unchanged; fetch extracts the image asset into a
  staging dir and installs it versioned.
- `create` records the image's `size:mtime_ns` as `IMAGE_ID`
  in the `.info`; `launch_qemu` reads the backing file from
  the overlay's qcow2 header and refuses to boot when it is
  gone or its identity moved.  A VM without `IMAGE_ID` is
  warned about when the image's mtime/ctime is after its
  `CREATED`, and otherwise adopts the record.
- `ltvm clean` removes image files that are neither current
  nor any overlay's backing file (qcow2 headers in
  `overlays/`, plus every `.info`'s `IMAGE`), and keeps an
  orphan image dir that one still uses -- `--force`
  included.  One overlay whose backing file it cannot read
  keeps every image.

## Exporting Images

`ltvm target export` repackages a built image + its
kernel + GRUB2 into one self-contained bootable disk -- for
people (or clouds) that don't have the ltvm runtime.

The disk is GPT and boots under both BIOS and UEFI: a BIOS
boot partition holding GRUB's core.img, an EFI system
partition holding a `BOOTX64.EFI` at the removable-media
path (so no NVRAM entry is needed), then root, last so it
can grow.  Both loaders read the same `grub.cfg`.  UEFI is
not optional on GCE's H4D: it falls back to legacy BIOS, and
that BIOS cannot read its NVMe boot disk.  The UEFI loader is
built with `grub-mkimage`, not `grub-install`, whose EFI mode
is distro-patched both ways (Ubuntu's switches to a Secure
Boot shim layout when `grub-efi-amd64-signed` is installed;
RHEL's refuses without `--force`).  Host needs `dosfstools`
and GRUB's x86_64-efi modules (`grub-efi-amd64-bin` /
`grub2-efi-x64-modules`); `ltvm install` and `ltvm doctor`
cover both.  Export needs `losetup` and `mount`, so it is
Linux-only: on macOS doctor prints a note instead of counting
the missing tools as issues.

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

The printed `gcloud compute images create` line carries
`--guest-os-features=UEFI_COMPATIBLE,GVNIC` (GVNIC only when
the image's kernel has `gve`).  Leave `UEFI_COMPATIBLE` off
and GCE boots the image through legacy BIOS; GVNIC is the NIC
the newer families (C4D, H4D) use.

Every format also gets `ltvm-growroot.service`, which grows
the root partition and its ext4 at boot to fill the disk.
The export sizes the partition to the image, and the disk it
boots from -- a GCE boot disk sized at instance creation, a
`qemu-img resize`d qcow2 -- is usually bigger.  It needs
`sfdisk`, which Debian and Ubuntu ship in the separate
`fdisk` package; the export warns when the image lacks it.

A base image ships no Google guest agent, so GCE cannot
inject SSH keys: export rocky10's `gce` variant
(`--variant gce`), which carries it, or pass `--ssh-key` to
bake a key in.  `--format gce` turns off password SSH and
locks root, but the image is otherwise ltvm's lab build --
don't open port 22 to the world.

## Lustre/Kernel Compatibility Gate

`ltvm` checks Lustre tree compatibility with the target's
`lustre.mode` (`server_ldiskfs` / `server_zfs` / `client`)
before any Lustre-involving build.

```bash
ltvm target validate rocky9 --lustre-tree ~/lustre-release
# Exit: 0 compatible (or warning), 1 refused, 2 could not tell

# Bypass a refusal (not hard errors):
ltvm build all rocky9 --lustre-tree ~/lustre-release --force-compat
```

## ZFS

ZFS is a per-build option, not a property of a target.  It
is entirely out-of-tree, so it sits between the kernel and
Lustre: it *reads* the kernel build-tree and changes
nothing about it, and it is installed into a VM at deploy
time rather than baked into the image.  Nothing about a
target's container, kernel or image artifacts depends on
it -- which is what lets it be a flag.

```bash
ltvm build zfs rocky9                                  # standalone
ltvm build lustre rocky9 --lustre-tree ~/lustre-release --zfs
ltvm deploy-lustre co1-zfs --lustre-tree ~/lustre-release --zfs --mount
ltvm cluster deploy co2 --build ~/lustre-release --zfs --mount
```

`--zfs` on a **build** means "build the ZFS OSD".  The
result has *both* backends: `--enable-server --with-zfs`
produces osd-ldiskfs and osd-zfs from one build, and
`FSTYPE` picks between them at test time.

`--zfs` on a **deploy** additionally means "run this VM on
ZFS", so it implies `--fstype zfs`.  Pass `--fstype
ldiskfs` alongside it to stage ZFS on a VM you want to
keep running ldiskfs.  `--fstype` writes the setting into
the VM's `cfg/local.sh`, which is what `llmount.sh`,
`auster` and a bare `sanity.sh` all read.

For ZFS the test framework takes `MDSDEV*`/`OSTDEV*` as
the **vdevs** to build pools on and derives the dataset
names itself (`$FSNAME-mdt1/mdt1`), so the `/dev/vd*`
mapping deploy already writes is what ZFS wants too --
nothing else in cfg/local.sh changes.

### Version

`--zfs-version VER` (implies `--zfs`) overrides the
target's `zfs.version` in targets.yaml, which in turn
overrides `DEFAULT_ZFS_VERSION` in
[ltvm_pkg/zfs_build.py](ltvm_pkg/zfs_build.py).  Release
tarballs come from openzfs/zfs and cache globally under
`artifacts/cache/zfs/`.  That cache is group-writable on a
shared host, so every tarball is checked against a sha256
before use -- on download, on a cache hit, and again from the
same open file as it is unpacked.  The sums are pinned in
`_PINNED_SHA256` (from the release assets' published digests);
**add one there when a target moves to a new ZFS version**.  An
unpinned version takes its sum from the release's
`zfs-<ver>.sha256.asc` and keeps it in the user's own cache.

rocky8 pins 2.3.4 rather than 2.4.0: 2.4 dropped support
for the 4.18 EL8 kernel.

### Artifact

`kernels/<kver>/zfs/<version>/` holds `src/` (configured
and built in place, for Lustre's `--with-zfs`) and
`staging/` (a `make install DESTDIR=` tree, for
deploy-lustre to stream into a VM).  It is keyed on the
kernel's release string **and** the kernel artifact's
`input_hash`, because zfs.ko links against Module.symvers
-- which moves when a kernel patch changes without
kernel.release changing.

The ZFS a VM receives is never chosen by the command line:
`build lustre` records the version and the artifact directory
(`zfs_dir`) in the staging meta and deploy ships exactly that
one, since osd_zfs.ko is linked against one specific ZFS build.

**Shared artifacts on a multi-user host.**  Only the kernel
artifact's owner (or root) builds ZFS into it, and only over a
`zfs/` it owns: ownership decides, not access(), because a
default ACL can make `zfs/` group-writable and a group member
building there would own what every other user loads.  Anyone
else builds into
`$XDG_CACHE_HOME/ltvm/zfs/<target>/<arch>/<kver>/<version>/`
(default `~/.cache/ltvm/...`), same layout, and downloads the
tarball to `~/.cache/ltvm/cache/zfs/` when the shared cache is
not writable.  After every owner's build, failed ones included,
`zfs/` loses the group/other write bits the kernel dir lacks.

A shared build is used only when it has `meta.json`, belongs
to the kernel dir's owner, and nothing in it is writable beyond
what the kernel dir allows; otherwise it is ignored with a
warning and the user's own is built.  A fresh trusted shared
build wins over the user's, so the owner saves every user the
build (1-5 minutes) and its ~900 MB (700 MB `src/`, 180 MB
`staging/`) by prebuilding: `ltvm build zfs <target> --kernel
<k>` as the account that owns the artifacts.  Builds into one
dir are serialized by a `.<version>.lock` beside it.

Deploy ships the ZFS dir the Lustre staging recorded, and fails
naming it when that dir has no complete build (no `meta.json`,
another version, no zfs.ko) rather than substitute another.

Per-user builds are never pruned with their kernel: `ltvm
clean --user-zfs` previews those whose kernel is gone from
artifacts (~900 MB each) and `--apply` removes them.  It only
touches the user's own cache.

Both rhel and debian build containers work; the inner
script picks dnf or apt for its extra build deps and takes
the library directory from `rpm --eval %{_libdir}` or the
Debian multiarch triplet, which is where the VM's ldconfig
will look for libzfs.  Any other os_family is refused up
front rather than inside the container.

`ltvm build zfs` works on a **client** target too -- ZFS is
a filesystem, not a server component.  What a client target
cannot do is `--with-zfs`, which needs `--enable-server` to
have an OSD to build; nothing in ltvm consumes a ZFS
artifact built for a client target, so it is only useful if
you are going to install it yourself.

Built per (target, arch, kernel, version):

| target | kernel | ZFS |
|---|---|---|
| rocky8 | 4.18 | 2.3.4 |
| rocky9 | 5.14 | 2.4.0, 2.3.4 |
| rocky10 | 6.12 | 2.4.0 |
| ubuntu2404 | 6.8 | 2.4.0 |

### Publishing

`ltvm target publish` emits a `zfs` asset only when the
Lustre being published was itself built with ZFS.  It
carries `staging/` and not `src/`: staging is what a
fetcher installs into a VM (~48 MB compressed), while
`src/` is only needed to *build* Lustre `--with-zfs`, adds
~180 MB, and `ltvm build zfs` reproduces it in well under
two minutes.  A fetched ZFS therefore reads as stale to
`ltvm build zfs`, which is correct -- it has no source to
configure against.

The kernel asset excludes `zfs/` for the same reason it
excludes `mofed-kmods/`: a fetcher who never passes
`--zfs` should not pay for it.

## VM Management

```bash
ltvm create co1-single --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
ltvm create co1-single --root-size 20G   # OS disk (default 8G)
ltvm create co1-single rocky9 --dry-run  # resolve + validate, write nothing
ltvm deploy-lustre co1-single --lustre-tree ~/lustre-release --mount
ssh co1-single 'lctl dl'
ltvm llmount co1-single               # mount
ltvm llumount co1-single              # unmount (= llmount --cleanup)
ltvm vm console-log co1-single
ltvm vm console-log co1-single -f     # keep streaming (tail -F semantics)
ltvm vm nmi co1-single                # inject NMI -> kdump
ltvm vm snapshot co1-single [tag]     # snapshot the overlay disk
ltvm vm snapshot co1-single --delete tag
ltvm vm restore co1-single [tag]      # restore (no tag: list them)
ltvm vm set co1-single --vcpus 2 --mem 4096   # resize a stopped VM
ltvm vm crash-collect co1-single --mod-dir $CO/1
ltvm destroy co1-single
```

The VM commands answer under `vm` too: `ltvm vm create` is `ltvm
create`, and likewise for `destroy`, `start`, `stop`, `list`,
`deploy-lustre`, `llmount`, `llumount` and `doctor`.  One parser
registered under both groups, so there is nothing to keep in sync.

**Owner/session metadata:** New VMs persist an advisory opaque `owner_id`.
Agent controllers should export `LTVM_OWNER_ID=<durable-session-id>` before
running normal create commands.  (If you do run create under sudo
anyway, use `sudo -E`: plain sudo drops the variable and the VM silently
takes the `pid:` fallback.) `--owner ID` / `--owner-id ID` override the
environment; otherwise LTVM uses `pid:<invoking-ltvm-pid>`. Cluster create
resolves once and applies the same owner to every member. Discover it through
`ltvm list --json`; legacy VMs report `owner_id: null`. See
[docs/VM_OWNERSHIP.md](docs/VM_OWNERSHIP.md).

**Claims:** `ltvm_pkg/vm_claim.py` records which session is using a VM
(`ltvm claim`/`release`, files in `VM_DIR/claims/`, mode 1777, rewritten
in place under flock).  `vm_claim.require()`/`check()` gate deploy,
llmount, the lifecycle commands (in `_require_manageable`, and in the CLI
wrappers ahead of `_vm_privileges` so a refusal never prompts for sudo),
the `vm` actions and the cluster commands; `auto_claim()` claims for a
session owner on deploy.  Tests: `tests/conftest.py` points `CLAIMS_DIR`
at a tmpdir and drops the session variables, since the suite often runs
under an agent.  Contract: docs/VM_OWNERSHIP.md#claims.

**Disks:** `--disk-size` sizes the MDT/OST scratch disks; `--root-size`
sizes the VM's own OS disk (default 8G, floor 1G, and never smaller than
the base image it overlays).  The guest's rc.local grows the root
filesystem into whatever the overlay is, so the flag is the whole
mechanism.  Both are fixed at create time -- `ltvm create` against an
existing VM warns that a differing value was ignored.  `cluster create`
takes `--root-size` too and applies it to every node.

**Resizing:** vCPUs and memory are not fixed: `ltvm vm set <name>
--vcpus N --mem MB` rewrites them in the stopped VM's `.info`, which
`launch_qemu` reads on every start, so the VM keeps its disks and
installed state.  It refuses a running VM, whose QEMU would keep the
old size while `list` showed the new one.  The host memory check runs
at the next `start`, as for any VM.

**Host memory:** `create` and `start` admit a VM only when the running
VMs' `-m` plus its own fit MemTotal less a reserve
(`qemu_run._memory_shortfall`).  `[memory] overcommit` in
`/etc/ltvm.conf` (`site_config.path()`; `LTVM_SITE_CONFIG` moves it, and
the tests point it at a file that does not exist) multiplies that budget
for the sum, never for one VM.  It is 1.0 unless set, for hosts running
KSM; a value below 1.0 or not a number warns once per process and counts
as 1.0.

**Kernel arguments:** `--kernel-args 'slub_debug=FZPU page_owner=on'`
is stored in the `.info` file as `KERNEL_ARGS` and appended after
ltvm's own parameters on every boot, so a parameter whose last
occurrence wins (`crashkernel=`) takes the user's value.  `root=`,
`console=` and `fc_*` are refused: the VM boots and logs through
ltvm's values, and rc.local configures the guest from `fc_*`.  Fixed at
create time like the disks; `cluster create` applies it to every node.

**Naming:** always include the checkout number: `co<N>-<role>`.

**Root:** a host has one of two layouts, and
[ltvm_pkg/rootless.py](ltvm_pkg/rootless.py) says which applies to the
current user.

*Shared* (what `ltvm install` sets up now): `/opt/qemu-vms`, its
`overlays/`, `sockets/` and `hosts.d/` are `root:ltvm` 3775, QEMU's
bridge helper is setuid root (a `dpkg-statoverride` on apt hosts, so
package upgrades keep it) and `/etc/qemu/bridge.conf` allows `fcbr0`.
Members of the `ltvm` group then run the whole VM lifecycle, clusters
included, with no root: QEMU runs as the user with `-netdev bridge`,
disks and state files are created directly, and VM names go to
`hosts.d/`, which dnsmasq watches (`hostsdir=`), instead of
`/etc/hosts`.  `/etc/hosts` is still updated when that needs no password.
Root must not create files in a directory every group member can plant
symlinks in, so under `sudo` a `create` continues as `$SUDO_USER` and a
`start` as the VM's owner, whose QEMU it is; `stop` and `destroy` only
signal and unlink, and stay root.  Root code that does write there opens
files `O_NOFOLLOW` (`priv.chmod_regular`, `priv.ensure_lock_file`,
`host_setup._write_root_file`).  A VM with a
`passthrough` NIC still needs a root QEMU, so it takes the classic path
below; on a shared host that trusts the group with root.

*Shared* on macOS: `ltvm install` makes the same directories and the
`ltvm` group (`dseditgroup`), but there is no helper -- QEMU connects
to socket_vmnet's socket, which launchd creates `root:staff` 0770.
Homebrew's dnsmasq has no inotify for `hostsdir=`, so it reads
`hosts.d/` as `addn-hosts=`, and the `io.github.ltvm.dnsmasq-reload`
LaunchDaemon, with `WatchPaths` on the directory, runs `launchctl kill
SIGHUP` on the dnsmasq job when a file comes or goes.  It runs a fixed
command and reads nothing a user wrote.  macOS caps `setgroups()` at 16
groups, so dropping from root there goes through `initgroups()`.

*Classic* (no bridge helper, or the user is not in the group): only
`update`, `cluster create` and `cluster destroy` need the whole command
under root -- and not `cluster create --dry-run`, which only reads.
Single-VM lifecycle -- `create`, `start`, `stop`, `destroy`, `doctor` --
runs as the invoking user and elevates the individual operations that
need it.  QEMU itself is launched under sudo, because it writes its
pidfile and QMP socket into the root-owned `/opt/qemu-vms/sockets`
(0755); the log is created there as root once and handed to the user,
and the pidfile and QMP socket are handed over after launch.
Everything the user then touches is theirs: `.info` and `.log` 0644,
`.pid` and `.qmp` 0600.

`doctor` reports which layout applies and why the shared one is
unavailable.  Once sudo has refused, later elevations in the same
process use `sudo -n`.

`build *`, `target *`, `deploy-lustre`, `llmount`, `list`, `vm *` and
the remaining `cluster` actions need nothing.

Verified 2026-09-11 by running the whole lifecycle as a non-root user.

### Host network (Linux)

`install --network` makes `fcbr0` (`qemu-bridge.service`, which also adds
the iptables NAT and FORWARD rules) and runs ltvm's **own** dnsmasq,
`ltvm-dnsmasq.service`, on it: `/etc/ltvm/dnsmasq.conf`,
`/run/ltvm-dnsmasq.pid`, `leasefile-ro`.  It reads nothing of the host's
`/etc/dnsmasq.conf` or `/etc/dnsmasq.d`, as on macOS.  apt hosts get
only `dnsmasq-base` (the binary), not Debian's `dnsmasq`, which would
enable a host-wide service.

The host's own dnsmasq is left as it is, apart from one drop-in,
`/etc/dnsmasq.d/ltvm-fcbr0.conf` (`except-interface=fcbr0`), written
when that directory exists.  Without it a host instance running
`bind-interfaces` takes the bridge address when it starts after the
bridge, and one running `bind-dynamic` takes it the moment it appears;
libvirt's Debian drop-in does the same for `virbr0`.  The host service
is restarted only when it holds the bridge's sockets.

An older ltvm instead wrote `/etc/dnsmasq.d/qemu-vms.conf` into the
host's dnsmasq, with process-wide options (`interface=`, `no-resolv`,
`except-interface=lo`) that took over everything else it served.
Re-running `install --network` migrates it
(`_hand_over_from_host_dnsmasq`): the file goes, and a host
`dnsmasq.service` that was serving the bridge is stopped if it was not
enabled (EL: ltvm started it, and it never came back after a reboot) or
restarted otherwise.  On Ubuntu that restart fails -- systemd-resolved
holds port 53, so the service only ever ran with ltvm's settings -- and
install says so and names `systemctl disable dnsmasq` rather than
disabling a host service itself.  Until then `reload_dns` and `install
--verify` still recognise that layout; otherwise `reload_dns` signals
only ltvm-dnsmasq, never whatever `pgrep dnsmasq` finds.

Under SELinux, dnsmasq_t may not set an inotify watch on `usr_t`
(anything under `/opt`), and the denial is dontaudit: no AVC, just
"failed to create inotify ... Permission denied" in dnsmasq's log, and
no `hosts.d/` name ever resolves.  Install labels `hosts.d`
`dnsmasq_etc_t` with `semanage fcontext`.

With **ufw or firewalld** running, the plain iptables rules are not
enough: ufw's INPUT policy drops the guests' DNS and DHCP to the host,
and firewalld filters from its own nftables table, which an iptables
ACCEPT cannot overrule.  `ltvm_pkg/host_firewall.py` has the active one
trust fcbr0 in its own persistent configuration (ufw `allow in on` and
`route allow in/out on`; firewalld's `trusted` zone), and `install
--verify` reports it.  ufw is detected from `ENABLED=` in
`/etc/ufw/ufw.conf`, not `systemctl is-active ufw`, which is "active"
whenever the oneshot unit has run.

Verified 2026-10-09 on stock Rocky 9.8 (SELinux enforcing) and Ubuntu
24.04 cloud images: fresh install, migration from the old layout, a
host dnsmasq in real use (and libvirt's), firewalld and ufw, each
across a reboot.

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

`ltvm cluster krb5 <name>` sets up Kerberos for sanity-krb5
([ltvm_pkg/cluster_krb5.py](ltvm_pkg/cluster_krb5.py)): a KDC on the
first MDS node, each node's lustre_{mgs,mds,oss,root} and host keys in
its own /etc/krb5.keytab, password principals for the test users.  The
guest-side work is `krb5-node.sh` and `krb5-kdc.sh` beside it, piped to
`bash -s`.  rhel-family targets only (dnf, sssd-kcm's drop-in).

`cluster exec <role>` fans out across every node holding the role and
exits non-zero if any node did; `cluster ssh <role>` opens a session on
the first, since it execs a single interactive ssh.

Each action is a real subparser, so `ltvm cluster <action> --help`
works and every action's flags validate and tab-complete.  Two
consequences worth knowing:

- **`cluster exec` takes its command as a REMAINDER**, so everything
  after the role is passed through untouched (`lctl dl -t` keeps its
  `-t`).  The price is that ltvm's own flags must come *before* the
  role: `cluster exec co2 --timeout 30 oss uptime`, not after it.
- **`create`'s specs are one `nargs="+"` positional** -- they have to
  be, or argparse would assign a bare positional TARGET the first spec.
  argparse matches positionals in contiguous runs, so an option between
  two specs ends the run and leaves the rest unrecognized; `parse_cli()`
  in `ltvm` appends those leftovers to `specs` for any subcommand that
  sets `_EXTRA_POSITIONALS_DEST`, so options go anywhere.  An unknown
  option among them is still an error.

A malformed cluster command line now exits 2 with a usage message rather
than ltvm's own error (a JSON envelope under `--json`), which is what
every other subcommand already did.

**Names inside a cluster.**  `cluster create`, and every `cluster
deploy`, write a `# --- ltvm cluster <name>` block naming every member
into each member's `/etc/hosts`.  pdsh, ssh and the `*_HOST` lines of
the cluster's local.sh all use names, and the guests' first nameserver
is the host's bridge address, which only ltvm's own dnsmasq is sure to
answer at once: a dnsmasq that forwards a bare name upstream (Patch
Watcher's run containers did, without `domain-needed`) makes each lookup
wait out the resolver's 5 s timeout, and its AAAA query for a name it
holds only an A record for does the same.  With the block, A lookups
and `getaddrinfo` never reach DNS; a lookup for IPv6 only (`getent
hosts`) still does.

### Running a suite

`ltvm suite run|status|collect`
([ltvm_pkg/suite_run.py](ltvm_pkg/suite_run.py)) starts one Lustre test
suite detached in the guest and reads it back.  The value is the recipe
in the generated `run.sh`, each line of which a burndown run got wrong
without: `CLEANUP_DM_DEV=false` in cfg/local.sh, `llmountcleanup.sh`
first, `auster -k -v -r -H`, launched under `setsid nohup keyctl session
-`.  The module docstring says why each one.  A run lives in
`/root/ltvm-suite/<run-id>/` on the VM, or on a cluster's
`ClusterInfo.local_node()` (first client, else the MGS -- where
`cluster llmount` runs too).  `status` is "running" while run.sh's pid
is alive, "done" once it wrote `done`, "died" otherwise; a node whose
uptime is shorter than the run's age crashed under it.  `collect`
rsyncs the run dir back without `*.debug_log.*` and parses results.yml
line by line (a killed suite leaves invalid YAML).  Out of scope on
purpose: splitting a suite across VMs, queues, and rebuilding a crashed
VM.

## Target Configuration

Targets live in [targets/targets.yaml](targets/targets.yaml).
Per-target keys:

| Key | Example | Description |
|---|---|---|
| os_family | rhel | Package manager family |
| os_name / os_version | rocky / 9.7 | Distro + version |
| container_image | rockylinux:9 | Build-container base |
| lustre.mode | server_ldiskfs | Compat gate mode |
| zfs.version | 2.4.0 | ZFS release `--zfs` builds (does not enable it) |
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

### Test suite

`uv run pytest` passes with no failures on two quite different
machines, and keeping both green is the standard:

- a **provisioned host** (`sudo ltvm install` has run): everything runs;
- a **bare checkout** with no `zstd`, `rsync`, `fakeroot`, `mke2fs` and
  no `ltvm` on PATH: the tests that genuinely drive those tools skip,
  naming what is missing, and nothing fails.

Two rules keep that true, and both came from tests that quietly wanted a
configured host:

- A **unit** test must not depend on a host tool.  When the code under
  test sits behind a presence preflight (`image_build._check_mke2fs`,
  `release_package._check_zstd`), neutralize the preflight -- otherwise
  the test fails on the preflight having never reached its subject, and
  what it reports is "fakeroot missing" rather than anything about the
  behaviour it covers.
- An **integration** test that really builds and unpacks assets needs
  the real binaries, since mocking tar and zstd would leave it asserting
  nothing about the tarballs it exists to check.  Those carry
  `@needs_host_tools` (tests/test_package.py) and skip with a reason
  naming the tool and the remedy.

Never let a test shell out to `ltvm` itself for real.  One did, and
passed only because the child failed; on a host where it would have
succeeded the suite was one check away from starting an actual Lustre
build (tests/test_deploy.py::test_legacy_staging_triggers_clear_error).

To assert that something is **absent** -- a hostname, a username, a path
-- substitute a sentinel and look for that, rather than searching for the
machine's real value.  The telemetry leak test did the latter and was
wrong in both directions: it failed on a host named `vm` because "vm" is
inside the key `ltvm_version`, and it would have failed on one named
`ubuntu` or `x86_64` by colliding with a legitimate value, while
narrowing the match enough to dodge that left it blind to a real leak on
the short-named host.  A sentinel collides with nothing, so the whole
payload can be searched and the answer is the same on every machine.
Better still where it fits: assert the output does not *change* when the
identity does (`test_payload_does_not_depend_on_the_machine_name`).

`tests/conftest.py` holds the autouse isolation that keeps the suite out
of the developer's real state: XDG config and state, `LTVM_TELEMETRY=0`,
and `LTVM_COMPLETION_ROOT`.  Add to it rather than patching per test
whenever a new code path writes outside the repo.

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

The container runs under `podman run --timeout`, as the ZFS and
MOFED-kmod builds do ([ltvm_pkg/build_timeout.py](ltvm_pkg/build_timeout.py)):
3600s for Lustre, 1800s for the other two.  `LTVM_BUILD_TIMEOUT`,
then `[build] timeout` in `/etc/ltvm.conf`, override all three, and
`0` drops the flag.  podman reports a stop only as rc=255, so a build
that failed after running for the limit names it and both settings
instead of dumping config.log.  It was 600s for Lustre, which a first
build overran on a macOS podman machine shared by several builds.

Uncompiled installs -- test scripts, cfg files, man pages,
headers -- are also kept in step without a rebuild: the build
records which source each verbatim-installed staged file came
from (`.ltvm-staging-sources.json`, matched by identical
content), and every `deploy-lustre` compares the pairs by
content and copies across what changed, `--userspace-only`
included (`ltvm_pkg/staging_sources.py`).  Sources come from
`git ls-files --cached --others --exclude-standard` in a git
tree, so gitignored build output (files generated from `.in`)
is never a source; ELF, `!<arch>` archives, `.a`/`.la`/`.pc`
and anything under `.libs/` are left out either way.  A staging
from before the manifest still gets `lustre/tests/` checked by
path.  Refresh writes its temp files in a
`.ltvm-refresh-*` dir beside the staging tree, never inside the
one deploy streams.  Mtimes are not trusted for this: an
mtime-preserving copy slips past the `find -newer` fast path.
`--userspace-only`'s warning about sources it cannot ship
counts only files git tracks.

Each staging dir has a flock beside it (`.<kernel>[__<variant>].lock`,
`lustre_build.staging_lock`): `build lustre`, the refresh and the
bundled-snapshot mirror take it exclusive, and streaming into VMs
(`deploy-lustre`, `cluster deploy`) takes it shared, so deploys of one
tree stream side by side but never while another rewrites the staging.
A deploy holds no lock while its child `build lustre` runs -- that
child's exclusive request would wait on it forever.

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

The Lustre version is decided on the host
(`ltvm_pkg/lustre_version.py`): `git describe` fails in the
container for a git worktree (its gitdir is not mounted) and
for a rootful build (safe.directory), and LUSTRE-VERSION-GEN
then falls back to DEFAULT_VERSION, which skips every
version-gated test.  The host writes LUSTRE-VERSION-FILE,
which LUSTRE-VERSION-GEN reads when its own describe fails.
autoconf cannot see a version change (it arrives through
`m4_esyscmd`), so a reconfigure whose `configure` carries
another version removes it and `autom4te.cache` first.  A
version nothing can determine (no tags, no version file) is a
WARNING at the start and the end of the build.

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
- **Root-required operations.** On a shared host nothing in the VM
  lifecycle needs root; a new host operation must keep that true or fall
  back to the classic path through `rootless.readiness()`.  On a classic
  host, single-VM lifecycle commands elevate the individual host
  operations that need it, so do not require users to invoke the whole
  command through sudo; cluster create/destroy still require root.
  Read/observe (console-log, deploy-lustre, llmount, crash-collect,
  cluster deploy/exec/status, list) don't.  Build commands don't.
- **Root in the shared VM directories.** Any root write there must not
  follow a symlink a group member planted.
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
- [docs/IPV6.md](docs/IPV6.md) -- a cluster's NIDs on IPv6,
  with `cluster deploy --ip-family`.
- [docs/RELEASING.md](docs/RELEASING.md) -- rebuilding the
  pre-built QEMU tarballs.
- [docs/NESTED_VIRTUALIZATION.md](docs/NESTED_VIRTUALIZATION.md)
  -- running ltvm under a nested hypervisor.
- [docs/SOFTROCE_SETUP.md](docs/SOFTROCE_SETUP.md) -- LNet
  o2iblnd over SoftRoCE.
- [docs/SYSTEM_TEST_PLAN.md](docs/SYSTEM_TEST_PLAN.md) --
  end-to-end test matrix.
- [docs/VM_OWNERSHIP.md](docs/VM_OWNERSHIP.md) -- the
  advisory `owner_id` a VM records, and who sets it.
