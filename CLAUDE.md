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

`pre-commit` runs ruff, mypy and the input hash goldens
(`tests/test_input_hash_stability.py`).  `post-commit` bakes the
version into `ltvm_pkg/_build_info.py` (gitignored), as `ltvm update`
does after a pull, so `ltvm --version` does not run git on every
invocation.

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
  `LTVM_ARTIFACTS_DIR` or `[storage] hot` moves the whole tree
  (see "Hot and cold storage").
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
kernel and any variant-pinned kernel.  Preserved even with
`--force`: any kernel a VM on this host boots (its `.info`
`KERNEL`), and any image file an overlay is backed by.
Distinct from `ltvm target clean`, which wipes a single
target's whole arch dir in one shot.

A scan of every target also offers the dead ltvm podman
images (`podman_gc.dead_tags`): an image build's `-final`
stage, and tags no target, arch or variant in targets.yaml
builds.  It lists VMs stopped and untouched for
`--idle-days` (14) with the `ltvm destroy` / `ltvm cluster
destroy` lines that would remove them, and never removes
one itself.  A tag another ltvm checkout's targets.yaml
still builds looks dead here; removing it costs that
checkout a rebuild, nothing more.

What a build makes dead it removes itself, so none of this
piles up between cleans:

- **Superseded podman images.**  A rebuild moves the tag;
  `podman_gc.Superseded` notes each tag's image before the
  `podman build` and `podman rmi`s the old one (no `-f`)
  once the build succeeds, variant before base.
- **The `-final` image.**  An image build's kernel-module
  stage is tagged `-final-<hex>`, one per build -- the other
  tags are per target, so two builds of one target for
  different kernels share them -- and is removed after the
  export and package manifest have read it.
- **Superseded image files.**  After a build makes a new
  `base-*.ext4` current, the dir's other image files that no
  overlay is backed by go (`image_store.prune_superseded`;
  nothing while any overlay's backing file is unknown).

Kernels are left to `ltvm clean`: an older minor is a choice.
Every build warns when the artifacts or podman filesystem has
under 30 GiB free (`cli.build._warn_low_disk`), since a build
that runs out fails late with nothing naming the cause.

### Hot and cold storage

`target_config.ARTIFACTS_DIR` is `LTVM_ARTIFACTS_DIR`, else `[storage]
hot` in `/etc/ltvm.conf`, else `<repo>/artifacts`, resolved once at
import ([ltvm_pkg/storage.py](ltvm_pkg/storage.py), parsed the way
`[memory]` and `[build]` are: configparser, a bad value warns once and
is ignored).  `[storage] cold` is read at call time and used only by
`ltvm target archive`/`restore`; `storage.cold_root()` refuses one that
does not exist rather than create it, since an unmounted disk's mount
point is the usual reason.

Archiving keeps every path computation unchanged: `<hot>/<target>/<arch>`
moves to the same place under cold and becomes a symlink to it.  The
unit is always an arch dir, never `<hot>/<target>`, so a target with one
arch archived needs no special case.  Code that walks the tree follows
the link (`is_dir()`, `glob`), so `ltvm clean`, status, target list and
doctor see archived targets; what does not follow it was fixed where it mattered:
`target clean`/`delete` remove the cold tree and the link
(`storage.archived_links`, links into cold only), and `target fetch`
refuses an archived target because tar replaces a symlink to a
directory with a real one.  `build status` and `target list` report it
as `storage: cold` plus `cold_path` -- per target in status, on every row
of the arch in target list -- from one helper, `storage.storage_fields`,
so they cannot disagree; the human output lists it under "Cold" in
status and tags the block `[cold]` in target list.

`storage.relocate` is the move: same filesystem, rename and link;
across filesystems, copy to `.<arch>.ltvm-<pid>-copy` beside the
destination (sparse via SEEK_DATA/SEEK_HOLE, modes and ns mtimes kept,
since `IMAGE_ID` is `size:mtime_ns`), `_verify_copy`, then rename and
link, and remove the source last.  A failure before the switch removes
the partial copy.  `move_target` checks everything before moving
anything: VM references (`vm_references`: each `.info`'s IMAGE and
KERNEL and each overlay's backing file, orphan overlays included),
deletability of the source (`survey`: writable dirs, no foreign entries
in sticky dirs), free space plus 1 GiB.  Running VMs always refuse;
stopped ones refuse without `--force`.  `create` records the image via
`current_image`, which keeps the hot spelling, so references normally go
through the symlink and survive both directions; only a cold spelling
(`create --image` resolves symlinks) needs repointing on restore, which
`--force` does with `_update_fields` and `qemu-img rebase -u -F raw`.

Group sharing: a storage dir is "shared" when setgid, or group-writable
for a group that is not the user's own (`storage.sharing`).  Then the
artifact-writing commands (`_ARTIFACT_WRITERS` in `ltvm`) clear the
umask's group bits (`share_new_files`), `run_podman_with_cleanup` adds
`--umask 0002 -e CCACHE_UMASK=002` to a `podman run` with a read-write
bind mount under a shared root (`podman_share_args`), and archive's copy
gives the group the owner's bits and keeps setgid on directories.  VM
commands are left alone on purpose: a group-writable overlay would let
any member write another's VM disk.  The ZFS ownership rules above are
relative to the kernel dir, so they hold on a group-writable tree.
Tests: tests/test_storage.py (`_same_fs` patched to False exercises the
copy path on one filesystem).

### Kernel build inputs (from Lustre tree)

`ltvm build kernel` parses the Lustre tree for the
target's slice: `lustre/kernel_patches/`
- `targets/<lustre_target>.target` -- SRPM version
- `kernel_configs/kernel-<ver>-<target>-<arch>.config`
- `series/<target>.series` + `patches/` -- patch series

then merges [targets/common/kernel-config.fragment](targets/common/kernel-config.fragment)
(plus the per-target `kernels.config` from `targets.yaml`)
and builds vmlinux/vmlinuz/modules/build-tree inside the
build container.

Two layers of config go on top of the base config:

- **The shared overlay**,
  [targets/common/kernel-config.overlay](targets/common/kernel-config.overlay),
  is best effort.  It goes in first and is never forced: what the
  kernel will not take after `olddefconfig` (an unmet dependency, a
  select, a symbol it lacks) is listed in the build log and in the
  kernel's meta.json as `config_overlay_missed`, and the build goes on.
  It holds what every kernel should have but no single target needs to
  boot: InfiniBand for ko2iblnd, and the hardware a headless microvm
  never has (DRM, sound, media, wireless, staging) turned off.
- **The forced fragment** is `kernel-config.fragment`, the per-arch
  `kernel-config-<arch>.fragment`, then the target's `kernels.config`.
  Its values are forced in before and after `olddefconfig`.  A symbol a
  target sets drops out of the overlay; a symbol it sets to `null`
  (`CONFIG_DRM: null`) is whited out of both shared files, which leaves
  it to the kernel's own config.

The four inner scripts share the overlay logic through
`targets/common/kernel-config-overlay.sh`, staged beside them with
`cross-compile-env.sh`.  The overlay, its helper and the whiteouts are
all inputs to every kernel's hash.  SRPMs cache under `artifacts/<target>/<arch>/cache/`
with a Rocky-vault fallback for older minors.

The container's tar and `rsync -a` run as container root and keep a
source tarball's owners, so the Ubuntu trees' files (uid 2001 in the
tarball) landed in build-tree/ as one of the user's subuids, which the
user cannot delete.  `_run_kernel_podman` therefore hands them back
after every build, failed ones included, and `target clean` and `ltvm
clean` do the same when a removal is refused
(`podman_run.reclaim_ownership`, `podman_run.rmtree`).  This is done in
Python rather than in the inner scripts, whose bytes are in every
kernel's hash.

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

A server series comes from `which_patch` when it is listed there.
Otherwise it is chosen the way Lustre's configure chooses it: by
version for vanilla and Debian kernels (`vanilla_ldiskfs_series`;
configure has no Debian case), by base
version and ABI for Ubuntu ones (`ubuntu_ldiskfs_series`), by
filename for the rest.  When a kernel build-tree exists, the series is
then applied in order to a scratch copy of the files it touches, as
the ldiskfs build stages them -- each patch on top of the ones before,
with GNU patch, because BSD patch rejects hunks GNU patch's fuzz takes.
A host with no GNU patch (stock macOS) skips that step and says so.

**Ubuntu servers** (`ubuntu2604-server`).  configure treats a kernel
as Ubuntu's only when its utsrelease.h defines
`UTS_UBUNTU_RELEASE_ABI`, which Ubuntu's packaging writes and a kbuild
from linux-source does not, and reads the ABI from the release's
second field.  So the target pins the exact source upload
(`kernel_deb_source: linux-source-7.0.0=7.0.0-14.14`), sets
`CONFIG_LOCALVERSION="-14-lustre"`, and `kernel_build._mark_ubuntu_abi`
adds the line to the build-tree.  Miss either and Lustre quietly
builds the vanilla `ldiskfs-7.0-ml` series instead.  The kernel is
defconfig plus ubuntu2604's options plus the target's own.  It needs
`CONFIG_KPROBES`, without which every Lustre module fails to load with
-EOPNOTSUPP (libcfs finds `kallsyms_lookup_name` through a kprobe);
the shared overlay sets it for every target, since arm64's defconfig
leaves it off.

**Debian's kernel config** (`debian13`).  The container stages
Debian's own config for both arches, from the linux-image packages of
the upload the source is pinned to, in `$KERNEL_DEB_CONFIG_DIR`
(`config-amd64`, `config-arm64`); `kernel-build-inner-deb.sh` starts
from `config-<deb arch>` there instead of defconfig.  Containers that
do not stage one (the Ubuntu targets) keep defconfig.

**SLES kernels** (`opensuse156`).  A target with `kernel_suse_source`
(the GitHub organisation) builds a SLES kernel from SUSE's own release
tags, not an SRPM: `kernel_build._build_kernel_suse` reads the version
(`lnxmaj`+`lnxmin`, "6.4.0") and release from Lustre's SLES `.target`
(a kernels.available `srpm_version: <version>-<release>` picks another
tag) and downloads `rpm-<version>-<release>` from SUSE/kernel, the
expanded source, and that tag's `config/<arch>/default` from
SUSE/kernel-source.  The expanded tree matched applying the tag's
`series.conf` to the vanilla tarball, except one drm CI text file
(checked 2026-10-13).  It saves applying 60,000 patches: one `patch`
process each takes most of an hour, and one `patch` run for all of
them loses a file that a revert deletes and a later patch recreates.
`CONFIG_SUSE_KERNEL` is what makes Lustre's configure take it for SUSE's
(and install modules under `updates/`).  The kernel is named
`6.4.0-<release>_lustre-default`.  No module carries SUSE's "supported"
mark, so the image sets `allow_unsupported_modules 1`, as SLES sites
running Lustre do.  The userspace is Leap 15.6 (`os_family: suse`,
zypper; systemd runs `/etc/init.d/boot.local`, not `/etc/rc.local`).
SUSE paths stay in `targets/opensuse156/` (`setup-suse.sh` replaces
`common/setup-kdump.sh`) so no other target's image hash moves.  The
build container points `CONFIG_SITE` at SUSE's site-config, the
counterpart of EL's `/usr/share/config.site`, so Lustre installs under
`/usr/lib64` there too, the path SLES's own Lustre packages use.

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
`auster` and a bare `sanity.sh` all read, as
`FSTYPE=${_ltvm_env_FSTYPE:-zfs}`: FSTYPE in the
environment still picks the backend, as it does for the
counts.

Every deploy streams the staging's stock `cfg/local.sh`
over the VM's, so `deploy_to_vm` reads ltvm's OST count,
FSTYPE and cluster blocks first and puts back those it did
not rewrite (`read_kept_blocks`/`restore_kept_blocks`).
That is what makes `--ost-count` and `--fstype` sticky,
and keeps a cluster member's cluster block through a
single-node deploy.  A `cluster deploy` takes its backend
from the cluster record when no flag gives one, and
`cluster llmount` refuses a first client without the
cluster block rather than format a one-node filesystem.

A ZFS deploy also writes `/etc/modprobe.d/ltvm-zfs-mmp.conf`
(`zfs_multihost_fail_intervals=0`) and sets the loaded module's
parameter to match (`deploy.configure_zfs_mmp`).  mkfs.lustre turns
multihost on for every pool, and a guest stalled ten MMP intervals by
host load had its pool suspended and, with failmode=panic, panicked.
The setting keeps the MMP writes and import check and drops only the
suspend.

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

`--zfs-source DIR` (on `build zfs`, `build lustre`, `build
all`, `deploy-lustre`, `cluster deploy`; implies `--zfs`)
builds an OpenZFS tree of the user's instead of a release, for
a fix no release has yet.  Its version is
`zfs_build.source_label()`: the tree's META version and a
digest of its files (in a git checkout, what git would commit,
so build output left in the tree does not count), e.g.
`2.4.99-src-1a2b3c4d5e6f`.  The files are copied into the
artifact's `src/`, `./autogen.sh` runs first when there is no
`configure` (outside zfs-build-inner.sh, whose bytes are in
every ZFS artifact's input hash), and meta.json records
`zfs_source`.  Everything after that -- the Lustre build's
`zfs_dir`, deploy's `find_zfs_staging` -- is the release path
unchanged.

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

A deploy's guest steps (depmod, ldconfig, cfg/local.sh) may each take
`LTVM_DEPLOY_TIMEOUT` seconds (default 600), and the tar|gzip|ssh copy
stops only after that long without progress
([ltvm_pkg/deploy.py](ltvm_pkg/deploy.py) `step_timeout`).  A fixed 120s
on the whole copy failed every deploy of a 380 MB staging on a host at
load 60; ssh's keepalives catch a dead guest in 30s, so the limits only
bound a command that is still running.  llmount.sh under `--mount` gets
`llmount_timeout()`, which grows with the target count and triples per
target on ZFS; `--mount-timeout` overrides it.  ssh and scp get the
guest password through `SSHPASS` (`sshpass -e`), never argv, so no
exception or `ps` listing shows it.

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
as 1.0.  In a container MemTotal is the host's, so the budget is the
cgroup's `memory.max` when that is smaller (`qemu_run.memory_budget`).
`ltvm list` reports the same budget (`mem_available_mb`, `mem_free_mb`
in `--json`), and `cluster create` checks all its nodes against it
before it makes any (`_require_cluster_memory`).

**Telemetry site ID:** `[telemetry] install_id` in `/etc/ltvm.conf`, a
uuid4 the admin generates once, is the install ID for every user on the
host, ahead of the per-user one minted into `config.json`, and is never
written there (`telemetry._site_install_id`; anything but a uuid4 is
ignored, since a uuid1 carries the MAC).  With it set no notice prints,
and `[telemetry] state_dir`, if absolute, holds `telemetry.json` and
`counters.json` in place of `XDG_STATE_HOME`, written 0644 and with no
chown of the directory, so users who start from an empty home every time
(Patch Watcher's run containers) share one weekly schedule and one set
of counters.  `state_dir` is ignored without the ID.  `ltvm telemetry
status` names both.  `[telemetry] enabled = false` still overrides all of
it.

**Waiting for SSH:** create and start wait `LTVM_SSH_TIMEOUT` (120s)
for the guest's sshd, and longer, up to five times that, while the
guest's serial console is still growing: on a loaded host a boot runs
past two minutes and is fine.  `cluster create` echoes each node's
create output as it comes, prefixed with the node's name, and gives
each node a backstop of that cap plus the kdump seeding budget
(`KDUMP_SEED_BUDGET`) plus headroom, so it never kills a node that is
up and seeding kdump.

**Stopping:** `stop` ends QEMU (SIGTERM, then SIGKILL), which flushes
QEMU's own caches but not the guest's page cache, so it first runs
`sync -f /` in every running guest over ssh, in parallel
(`_sync_guests`; also before a snapshot or restore stops the VM).
Root's filesystem only: a whole `sync` on a Lustre client whose
servers are gone would hang.  An unreachable guest is warned about and
stopped anyway; `--force` skips the sync.

**NMI:** an x86 guest boots with the NMI panic knobs set
(`qemu_run.NMI_PANIC_ARGS`: `unknown_nmi_panic` and `sysctl.kernel.*`
on the cmdline, which needs 5.8; create and start also write
`/etc/sysctl.d/90-ltvm-nmi.conf` for rocky8's 4.18), so `ltvm vm nmi`
panics a guest into kdump with no ssh, which is when it is needed.
`vm nmi` still sets them over ssh for a guest booted before that, and
refuses only when that fails and the QEMU's cmdline lacks them.

**NUMA:** `--numa-nodes N` (`create`, `cluster create`, `vm set`) is
`NUMA_NODES` in the `.info` file, and `qemu_run.smp_args()` turns it into
`-smp V,sockets=N,cores=V/N`, one `memory-backend-ram` and one `-numa
node` per node at every start: equal vCPUs (N must divide them) and
memory in 2 MB units, the last node taking the remainder so the backends
sum to `-m`.  For Lustre CPT testing; works under KVM and HVF, x86_64 and
aarch64.

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

A cluster may mix targets (`--node-target VM=TARGET`, and
`--node-kernel VM=KERNEL` for one of that node's own target's kernels).
Its nodes share one cluster block naming Lustre's directory (LUSTRE,
RLUSTRE, RPWD), which the framework also uses on the other nodes, and
Debian-family targets install under /usr/lib/lustre while the rest use
/usr/lib64/lustre.  So when a cluster mixes them, `cluster deploy`
links each node's directory under the other name
(`_link_lustre_libdirs`), refusing to replace a real directory there;
each node is still deployed with its own family's paths.

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

`cluster deploy --server-only` and `--client-only` build for and deploy
to the MGS/MDS/OSS nodes, or the pure clients, alone
(`cmd_cluster_deploy`'s `deploy_nodes`): the rest keep the Lustre they
have, so two trees share a cluster (2.15 servers, a master client).
Every node still gets local.sh, /etc/hosts and pdsh.  `--server-only`
used to mean only `llmount.sh --server-only` and required `--mount`,
while still building the tree for every node's target, the client's
included; with `--mount` it still mounts the servers only.

A servers-only mount (`cluster deploy --server-only --mount`, `cluster
llmount --server-only`) runs on the MGS node with the servers' own test
scripts, since the clients may hold another tree or none
(`_SERVER_ONLY_MOUNT`).  llmount.sh took `--server-only` in 2.15.56; an
older one, every 2.15.x release included, hands it to init_test_env,
which prints its usage and exits 0 with nothing mounted.  So when the
node's llmount.sh lacks the option, ltvm sources test-framework.sh
and runs `formatall; setupall server_only` itself, which setupall has
long supported.  Every cluster mount is then checked
(`_check_mounted`): the targets mounted across the server nodes must
number `expected_targets()` and, unless servers-only, the node it ran
on must have a client, or the command fails.  llmount.sh's exit status
alone proves nothing: master's prints its usage and exits 0 on an
option it does not know.

`ltvm cluster krb5 <name>` sets up Kerberos for sanity-krb5
([ltvm_pkg/cluster_krb5.py](ltvm_pkg/cluster_krb5.py)): a KDC on the
first MDS node, each node's lustre_{mgs,mds,oss,root} and host keys in
its own /etc/krb5.keytab, password principals for the test users.  The
guest-side work is `krb5-node.sh` and `krb5-kdc.sh` beside it, piped to
`bash -s`.  rhel-family targets only (dnf, sssd-kcm's drop-in).

The framework runs every remote command through pdsh.  The rocky10
and mainline images take it from the epel-10 branch (their 10.1 base
pins an EPEL branch without it), and `cluster deploy` installs it on a
node whose older image lacks it (`_ensure_pdsh`).

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
line by line (a killed suite leaves invalid YAML).  `--debug-logs` (on
`collect` and on `run --wait`) copies the debug logs too; without it
the summary counts the ones left in the guest (`debug_logs_skipped`
under `--json`).

Collected results go to `./ltvm-suite/<target>/<run-id>/` under the
working directory (`suite_run.results_root()`), where people look for
them.  `--results-dir DIR` (on `collect`, where `--dest` is its old
spelling, and on `run --wait`) puts one run's results in DIR instead,
for a caller standing in a git tree that should not get them.  `run` names the place, as an absolute path, when it starts and
the summary names it again at the end; `run --wait` checks it is
writable before launching, not after the suite.  `status` prints
`collected to <dir>` (`results_dir` under `--json`) for a run found
under the working directory, or in `$XDG_STATE_HOME/ltvm/suite/`
(`suite_run.state_root()`), where collects went for a while on
2026-10-13; nothing migrates or prunes them.  Out of scope on
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
| kernels.config | {CONFIG_XEN_PVH: y} | Per-target config overrides (null whites out a shared entry) |
| kernels.arch_config | {aarch64: {CONFIG_NFP: n}} | Overrides for one arch's kernel, on top of kernels.config; moves only that arch's kernel hash |
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

The suite also runs on macOS on Apple Silicon, so a test that assumes a
Linux x86_64 host says so with
[tests/host_marks.py](tests/host_marks.py): the `x86_host` fixture for
the x86_64 default targets, `needs_gnu_sed` for guest-side shell run on
the host, `needs_proc` for /proc.  No test may reach the real `sudo`,
which prompts in a terminal and fails without one: stub
`_vm_privileges`, `sudo_prime` or `sudo_run` where the code under test
elevates.

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

configure gets `--with-o2ib=yes` (the in-kernel ko2iblnd, for
SoftRoCE), which fails outright on a kernel without InfiniBand.  Every
kernel gets IB from the shared overlay (above), so one without it is
either meant to be (its target sets `CONFIG_INFINIBAND` or
`CONFIG_INFINIBAND_ADDR_TRANS` to n, or whites one out with null, in
kernels.config or a shared fragment) or wrong: built before the
overlay, or the overlay missed it.  The kernel build records the first
as `infiniband_opt_out` in meta.json (`kernel_build.infiniband_opt_out`).
`lustre_build.o2ib_configure_flag` reads the build-tree -- rdma_cm's
`rdma_destroy_id` in Module.symvers, else `CONFIG_INFINIBAND` and
`CONFIG_INFINIBAND_ADDR_TRANS` in .config or autoconf.h -- and for a
kernel without IB configures `--with-o2ib=no` with a one-line note when
the opt-out is recorded, and otherwise refuses the build, naming the
cause (no record, predates the overlay, or the `config_overlay_missed`
entry) and the fixes.  A refusal rather than a quiet `=no`, because a
Lustre built without ko2iblnd surfaces much later, as an o2ib or
SoftRoCE LNet setup with no LND.  An o2ib setting in configure_args or
`--configure` skips all of it; it is appended after the default and
wins.  Every Lustre build goes through it: `build lustre`/`all`,
deploy-lustre, cluster deploy and make-install.

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
`lustre_build.staging_lock`): `build lustre` and the refresh take it
exclusive, and streaming into VMs (`deploy-lustre`, `cluster deploy`)
takes it shared, so deploys of one tree stream side by side but never
while another rewrites the staging.  A deploy holds no lock while its
child `build lustre` runs -- that child's exclusive request would wait
on it forever.

`deploy-lustre` without `--lustre-tree` deploys the bundled Lustre from
`ltvm fetch` (`kernels/<kver>/lustre-artifacts[/<variant>]/`, marked by
`.ltvm-snapshot.json`) for the kernel the VM boots, straight from the
artifacts dir, with no lock and nothing written there: on a shared host
or in a Patch Watcher run that dir is read-only.  It used to look under
the target's default kernel whatever the VM booted, and to mirror the
snapshot into a staging dir beneath itself, which failed with EROFS.
With no bundle for the VM's kernel it falls back to the current
directory, and if that is no Lustre tree, the error names the kernels
that do have one.

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
