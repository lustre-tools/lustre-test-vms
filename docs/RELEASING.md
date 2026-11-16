# Releasing

## Rebuilding the target releases

A change that moves kernel or image input hashes (the shared kernel
config, a guest module, a Dockerfile) leaves every published release
stale, so they are all rebuilt and republished together.  One command
per arch rebuilds everything published for it, from one Lustre commit:

```bash
ltvm build release --lustre-tree <lustre tree> --dry-run   # the plan
ltvm build release --lustre-tree <lustre tree>
```

The set to build comes from the GitHub releases: every base release
for the arch, mapped back to its kernel in targets.yaml.  `--only
rocky9` or `--only rocky9:5.14-rhel9.7` narrows it.  Releases whose
kernel cannot be mapped back are listed as unmatched and not built.
That includes every release of a target with moving kernels (mainline's
`stable`, `latest`, `longterm`), since a version cannot say which name
or series built it; build those with `ltvm build all`.  The Lustre tree
must have no uncommitted changes to tracked files, since the release
records its commit.

It runs in three passes, so what breaks shows early:

1. **Preflight**: each target's build container and image package
   layers (`ltvm build image --base-only`), in parallel.  A distro
   package that no longer installs fails here in minutes.  Podman
   caches the layers, so the full image builds reuse them.
2. **Builds**: `ltvm build all` for each target and kernel, every
   target's first kernel before any target's second, so a kernel
   problem common to a target shows on its first build.  Builds run
   in parallel slots (`--jobs`; one per 32 CPUs, at most four), each
   pinned to whole NUMA nodes (`--cpus` sets them by hand; the pinning
reaches local podman's containers, not those of a podman service reached
through `CONTAINER_HOST`) and given
   its own clone of the Lustre tree.  Two kernels of one target never
   build at once.
3. **Verify**: a build counts only when its kernel, its Lustre
   snapshot (from the commit asked for) and an image that is current
   against its inputs are all there; an exit status of 0 is not enough.

Each build logs to its own file, and the run ends with a table and a
`summary.json` beside the logs.  A build whose log stops growing for an
hour (`--stall-minutes`) is stopped and reported as stalled: a wedged
podman or fuse-overlayfs would otherwise hold the run for good.  The exit status is 0 only when every
build is good.  Before starting it checks that the artifacts and
podman storage, TMPDIR and the work directory have room for the run,
and refuses when they do not.  On macOS podman's storage is inside the
podman machine and is not checked.

Finished builds go in a ledger in the work directory
(`<artifacts>/.release-build/<user>`, with the Lustre clones).  Rerunning the
same command with the same ltvm and Lustre commit skips them, so after
a failure, fix the cause and run it again.  The ledger knows only those
two commits: an ltvm checkout with uncommitted changes skips nothing.

After anything else changes, `--fresh` runs every build with
`--force`, rebuilding each kernel, Lustre and image.  That includes a
change to ltvm's own build code that leaves the build inputs alone:
the caches key on the inputs, so without `--fresh` such a change does
not reach artifacts already built.  Kernels are chosen by the usual
rules either way: the release targets.yaml and the Lustre tree name,
unless that one can no longer be downloaded, when the newest available
one stands in; mainline's series names take the newest point release.
So a rebuild can move to a newer kernel.

Podman's cached package layers are not rebuilt by either.  Targets
with the same image Dockerfile share them, so to refresh them remove
every `ltvm-image-*` image (`podman images`), not just one target's.

The preflight runs for every target, finished ones too, so each still
has its build container for publishing.  A release that carries ZFS is
listed and not built: rebuild it with `ltvm build all --zfs`.

One run at a time uses an artifacts directory or a work directory, and
one per user runs at all (see below for why); a second is refused.

### Where to run it

- **x86_64** on a large Linux host.  One with 128 threads in 8 NUMA
  nodes runs four slots, each on two nodes.
- **aarch64** on an Apple Silicon Mac, one slot.  Give its podman
  machine 8 GiB of memory: BTF generation for the SLES 6.4 kernels
  (pahole) needs about 3.5 GiB, and is killed in a 4 GiB machine.
  The run holds the Mac out of idle sleep with `caffeinate` while it
  lasts; a closed lid still sleeps it, so leave the lid open.

Neither needs the other: run both at once.

### A shared host whose root filesystem is small

Rootless podman keeps its storage under `~/.local/share/containers`,
and a kernel build needs about 25 GiB there.  Put it, the artifacts
and podman's temporary files on a large filesystem, and keep them
there between rebuilds: the build containers, the kernel source
packages and the image package layers are what make the next rebuild
start warm.

```bash
mkdir -p /mnt/data/$USER-ltvm/{containers,tmp,artifacts}
cat > /mnt/data/$USER-ltvm/storage.conf <<EOF2
[storage]
driver = "overlay"
runroot = "/run/user/$(id -u)/$USER-ltvm-containers"
rootless_storage_path = "/mnt/data/$USER-ltvm/containers"
EOF2
export CONTAINERS_STORAGE_CONF=/mnt/data/$USER-ltvm/storage.conf
export TMPDIR=/mnt/data/$USER-ltvm/tmp
export LTVM_ARTIFACTS_DIR=/mnt/data/$USER-ltvm/artifacts
```

`graphroot` does not move rootless storage (podman 4.9 ignores it);
`rootless_storage_path` does.  `runroot` is ignored too: every rootless
podman of one user keeps its locks and mount state in
`$XDG_RUNTIME_DIR/containers`, whatever storage it uses.  So a second
storage does not keep two sets of builds by one user apart, and
running two at once (two storages, or a release run beside other podman
builds) can wedge fuse-overlayfs.  `build release` therefore runs one
at a time per user.

### Publishing

Publish from the artifacts the build left:
`ltvm target publish <target> --kernel <kernel_dir> --arch <arch>` for
each build in `summary.json`, with the same `LTVM_ARTIFACTS_DIR`.  Use
its `kernel_dir`, the full directory name, rather than the short kernel
name: the short name picks the newest kernel cached, which need not be
the one built.  From a pull request's branch this publishes drafts,
which `ltvm target promote` makes public after the merge; see the next
section.

## Release tags, drafts and promote

The workflow for a change that needs new artifacts:

1. Build on the pull request's branch (`ltvm build release`, or `ltvm
   build all` per target).
2. `ltvm target publish` from that branch.  Its HEAD is not on the
   default branch of the repository releases go to, so each release is
   uploaded as a draft, which nothing fetches.
3. Merge the pull request.
4. On the default branch, after pulling: `ltvm target promote <target>
   ...` (or `--all`) makes public the drafts built from what the default
   branch now builds, and prunes what they supersede.

Publishing from the default branch itself skips the draft stage: the
release goes public as soon as its upload is complete.

"The default branch" is always that of the repository releases go to
(`LTVM_GITHUB_REPO`, normally `lustre-tools/lustre-test-vms`), fetched
from its own URL, never this checkout's `origin`: in a fork's clone,
the fork's default branch can hold work the release repository has not
merged.  Publish and promote fetch it to check, into a ref of their
own that they delete afterwards, not `FETCH_HEAD`, which any other
fetch in the checkout meanwhile would rewrite.

### Tags and keys

A release is tagged `rel-<target>-<arch>-<kver>[-<variant>]-<key>`.
The key is a short digest of the container, kernel and image inputs of
the checkout that published it, the same inputs `ltvm build status`
compares, leaving out Lustre; of the release manifest's schema version;
of `BUILD_GENERATION` in `ltvm_pkg/release_package.py`; and of the ZFS
the target declares (its targets.yaml `zfs` section and the version
`--zfs` resolves to, which no input hash covers, so that a ZFS bump
rebuilds no container, kernel or image).  Any checkout computes its own
key without a Lustre tree or anything built.  A rebuild whose inputs
or release format changed gets a new tag even when the kernel version
did not.

What a release carries, and what covers it:

- container, kernel, image: their input hashes, which take in this
  target's whole targets.yaml entry but for `variants` and `zfs`, the
  variant's own settings (overlays, packages, params such as the MOFED
  version), the package lists, the Dockerfiles and what they copy, the
  kernel config fragments and the kernel build scripts;
- ZFS: the declared ZFS, above; publish refuses a Lustre built against
  another version (`--zfs-version`);
- MOFED kmods, baked into a variant's image: the variant's MOFED
  version and distro, through the image's input hash;
- the release layout: the manifest schema;
- Lustre: not covered, by design.  It is built from a Lustre tree,
  and the release records its commit.  Its configure flags from
  targets.yaml are in the target's entry above; flags a build is
  given by hand (`--configure`) are not.

Not covered by any input hash, and so by `BUILD_GENERATION` alone:
ltvm's Python, and the build scripts that the input hashes do not
read: the MOFED kmod and ZFS build scripts in `ltvm_pkg/`.  Distro
packages are installed as the mirrors serve them when a container or
image is built; the key, like the input hashes, names the package
lists, not their versions.

The input hashes cover files ltvm reads (targets.yaml, Dockerfiles,
config fragments, the inner build scripts), not its Python.  A change
to the Python that changes what the artifacts contain (how a kernel
config is assembled, how an image is laid out) moves no input hash, so
replace `BUILD_GENERATION` with it, by a new value of the form
`"YYYY-MM-DD-<short-reason>"`.  Two pull requests replacing it then
conflict on that line in git instead of merging to the same value, and
the resolved value matches neither one's artifacts, so promote leaves
them for a rebuild.  Resolve such a conflict by choosing a third, new
value, never either side's: either side's would match that branch's
artifacts, which lack the other's change.  Kernels and images record the generation that
built them: after a change they read as stale (`ltvm build status
--why` says why) and are rebuilt, and publish refuses ones built by
another generation, or before generations were recorded.  A new value
moves every release key, and no input hash.

Releases published before keys existed are tagged
`<target>-<arch>-<kver>[-<variant>]`.  Fetch reads both kinds, and
those old releases are never pruned or replaced: they are what an ltvm
older than keyed tags fetches.  An ecosystem release's tag is always
derived from its kernel and key; `--tag` names `--image` releases
only, and must start with `bootable-` and name no release holding an
ecosystem manifest.  A tag given by hand could carry a key the
artifacts were not built with, or be an unprefixed one, which older
ltvm would fetch and could not load the container archive of.

### What publish checks

Publish refuses:

- a checkout with uncommitted changes to tracked files, or with
  untracked or ignored files the release key reads (an optional kernel
  config fragment, say): the release records the commit it was
  published from, and its key would describe files that commit does
  not hold.  Publish and promote compute the key's inputs again from
  HEAD exported with `git archive` and name each one that differs, so
  whatever the hashes read is covered; untracked files they do not
  read (artifacts, virtual environments) do not matter;
- anything it packages that the builds themselves would call stale,
  by the same functions they use: the container's and the image's
  staleness, the image's against its MOFED kmods and, when Lustre is
  baked into it, the Lustre being packaged (an image built without
  Lustre, beside a separate Lustre asset, does not depend on it); the kernel's status and its per-input digests (it
  cannot recompute a kernel's Lustre patch inputs without the tree);
  `ltvm build release`'s check that the Lustre was built against this
  kernel's Module.symvers; and the MOFED kmods' and the ZFS's own
  staleness, which see a kernel rebuilt from other inputs or for
  another build generation, and an incomplete ZFS.  Each refusal names
  what to rebuild.  With `--lustre-tree`, the Lustre checked is that
  tree's staging, which the publish will snapshot, and the snapshot is
  checked again once taken;
- what only a release needs on top of that: a kernel and image that
  record this build generation (one recording none is current for
  builds, not for a release), and a ZFS that is the one targets.yaml
  declares, which the key covers;
- with `--no-lustre`, which ships no Lustre asset and so no ZFS, an
  image with Lustre baked into it: rebuild the image without Lustre,
  or publish with Lustre.  The image's MOFED kmods are still checked;
- a kernel or image built in another build container than the one it
  belongs to.  They record, when built, the container's input hash
  (and its podman image id, when known); a changed container rebuilds
  while a cached kernel stays, and the release key would name the new
  container.  A kernel is shared by every variant and always built in
  the base variant's container, so it is checked against that one; an
  image against the published variant's.  One built before this was
  recorded is refused too.  Ordinary builds never go stale over it.  The record is of the
  container ltvm built for the kernel, not of the one podman ran under
  the shared tag, so do not rebuild a target's container from another
  checkout while a release build or publish of that target is running;
- a build container that is not the one the artifacts directory
  built.  The podman tag is shared by every checkout of a user, so a
  container build records in its meta.json the image id its own
  `podman build --iidfile` wrote, publish checks the tag still names
  that image, and it saves the image by its id (`podman save -o`,
  which works with a remote podman such as a Mac's podman machine).
  An archive saved by id carries no tag, so fetch, and a build
  reloading a container another user fetched, tag what `podman load`
  reports.  A container built before ltvm recorded the id has to be
  rebuilt once (`ltvm build container <target>`);
- from a branch, replacing a release that is already public;
- a draft another commit published: one maintainer publishes a given
  target and arch at a time, and a draft from another commit is
  someone else's publish in progress, or an older one.
  `--replace-unmerged` takes it over.  A draft this commit published
  is resumed, so publishing again finishes an interrupted upload;
- a release that would hold two manifests (one per release).

`--dry-run` shows the tag, runs these checks, including that the
Lustre snapshot, or the `--lustre-tree` staging, is there, and says
whether the release would be public or a draft, and what a public one would prune,
without packaging, uploading or deleting anything; with `--image` it
shows the bootable release's tag.

Before publish changes any asset of a release, new or existing, draft
or public, it rewrites the release's notes without their last line,
`ltvm-complete: yes`, and makes it a draft.  Only once every asset is
uploaded does it write the full notes back, mark included, and, from
the default branch, make the release public.  If anything fails on the
way, the release is left a draft without the mark, and the publish
fails.  Nothing selects or promotes a keyed release without the mark,
so a half-replaced set of assets is never fetched; publishing again
finishes it.

### What a release's notes say

Each release's notes name what it holds and which ltvm published it,
one `ltvm-<field>: <value>` line each:

- `ltvm-target`, `ltvm-arch`, `ltvm-kernel` and `ltvm-variant`: the
  kernel as targets.yaml declares it (`5.14-rhel9.7`, or `stable` for
  mainline), which a kernel version cannot always be mapped back to.
- `ltvm-schema`: the manifest's schema.
- `ltvm-version` (`ltvm --version`, for example `0.5.391+d8b1d5b`) and
  `ltvm-commit`, the full commit id.  The manifest carries these too,
  as `ltvm_version` and `ltvm_commit`.
- `ltvm-promoted-version`: on a promoted draft, the version of the
  default-branch ltvm that promoted it.  A branch's commit count does
  not order against the default branch's (a squash merge counts
  differently), so the update prompt compares this one.
- `ltvm-built-at` (UTC, `2026-11-16T12:30:05Z`) and `ltvm-complete:
  yes`, last: when the publish that wrote these notes finished, written
  with the mark only then.

Fetch, `target list`, promote, pruning and `ltvm build release` take a
release's kernel and variant from these lines; asset names are read
only for releases published before them.  So a fetch of mainline's
`stable` takes a `stable` release, never a newer `latest` one.  A
field that is not a plain name (letters, digits, `.`, `_`, `+`, `-`,
no `..`) is ignored, and `build release` takes only kernels
targets.yaml declares.

### Choosing a release

Nothing selects a draft, or a keyed release without the completion
mark.  The rest are taken newest published first, by their publication
date: the releases list is in creation order, which a draft promoted
late does not follow.  Among those for a target, arch, declared kernel
and variant, fetch takes the one keyed with this checkout's inputs.
Without one, it takes the newest, of either tag scheme, and says in
one line that it was built from other inputs, so `ltvm build status`
may show it stale.  If that release was published, or promoted, by a
newer ltvm than this one, fetch also asks for an update: `sudo ltvm
update` (or pull the checkout) and fetch again.  `--list`, `target
list` and `show`, `build release` and pruning read releases the same
way.  A release whose
manifest schema this ltvm does not know gets the same request.

### promote

`ltvm target promote [TARGET ...] [--all] [--arch A] [--dry-run]`
runs from a clean checkout, as publish defines it, whose HEAD is in
the history of the default branch.  For each draft `rel-`
release whose notes name a target given (and the arch, with `--arch`),
it makes the draft public when its upload finished (the completion
mark) and its key is the one this checkout computes for that target,
arch, kernel and variant: the release is built from what the default
branch now builds.  It adds its own version to the notes
(`ltvm-promoted-version`), then prunes what the promoted release
supersedes.  Every other draft of those targets is left, with the
reason: an unfinished upload, built from other inputs (a branch not
merged, or merged and changed since), notes that do not name it, or
more than one manifest.

Of the drafts it would promote, it takes only the newest build of
each target, arch, kernel, variant and key, among those drafts and that
key's published releases.  Newest is by `ltvm-built-at`, not GitHub's
`created_at`, which is the tag commit's date and the same for every
build uploaded between two commits.  A tie goes to the published
build, never to a draft, then to the tag name; a release without the
time sorts oldest.  Two builds of
a moving kernel (rebuilt after a point release) share a key; published
after the newer one, the older would be what fetch takes.  So older
drafts are left as superseded, a retry after a partial failure
included.  It promotes oldest first.

A draft left is not a failure; a promotion or deletion GitHub refused
is, and so is a draft gh could not read, or a published list it could
not get, for ordering or for a prune; promote exits with an error once
it has done the rest.  Publish only warns when it cannot list releases
to prune.
`--dry-run` lists what it would promote and prune, and fails as the
real run would when it cannot plan a prune.

### Pruning

A public publish and each promotion from the tip of the default branch
delete older published `rel-`
releases whose notes name the same target, arch, declared kernel and
variant, together with their git tags.  They keep the release just
published or promoted and the newest other one, whatever its age, and
of the rest delete those published 14 or more days ago.  A kernel
whose version moved (a new minor update of the same declared kernel)
is the same declared kernel.  Drafts are never pruned, nor are
old-scheme releases, releases whose notes do not name their contents
or that hold more than one manifest, or anything outside the target's
releases (bootable images, QEMU builds).  Each release is listed as
deleted or kept, with the reason.  A failed upload prunes nothing, and
a failed delete is a warning.  `--no-prune` skips it.

A checkout further back on the default branch may still publish and
promote, but does not prune, and says so: its releases are not what
the branch builds now, and pruning by them could delete the release
the tip fetches.  Whether it is at the tip is asked again just before
each prune, as the branch may have moved on while an upload or a
promote ran.

## Rebuilding Pre-built QEMU Binaries

Rocky Linux ships QEMU without microvm support, so we publish
pre-built binaries to GitHub.  `ltvm install` downloads these
automatically.  The tarball must contain `bin/qemu-system-x86_64`,
`bin/qemu-img`, and `share/qemu/<firmware files>` (bios-microvm.bin,
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

# Publish a sha256 next to each tarball.  `ltvm install` fetches
# <asset>.sha256 and refuses to unpack a tarball whose digest does not
# match -- it extracts into /opt as root, so this is the only thing
# standing between a corrupted or substituted asset and the host.
for suffix in el9 el10; do
    ( cd /tmp && sha256sum "qemu-9.2.2-${suffix}.tar.gz" \
        > "qemu-9.2.2-${suffix}.tar.gz.sha256" )
done

gh release upload qemu-9.2.2 /tmp/qemu-9.2.2-el9.tar.gz --clobber
gh release upload qemu-9.2.2 /tmp/qemu-9.2.2-el9.tar.gz.sha256 --clobber
gh release upload qemu-9.2.2 /tmp/qemu-9.2.2-el10.tar.gz --clobber
gh release upload qemu-9.2.2 /tmp/qemu-9.2.2-el10.tar.gz.sha256 --clobber
```

Notes:
- The `.sha256` companion is required for new uploads.  Assets
  published before it existed still install, with a warning.
- Rocky 8 needs `dnf install python38` (system python too old)
- Ubuntu uses system QEMU package (has microvm)
- Bump `QEMU_VERSION` in `ltvm_pkg/host_setup.py` when updating
