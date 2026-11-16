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

Publish once the change has merged, from the artifacts the build left:
`ltvm target publish <target> --kernel <kernel_dir> --arch <arch>` for
each build in `summary.json`, with the same `LTVM_ARTIFACTS_DIR`.  Use
its `kernel_dir`, the full directory name, rather than the short kernel
name: the short name picks the newest kernel cached, which need not be
the one built.

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
