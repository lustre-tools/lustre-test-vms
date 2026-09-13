# Releasing

Two unrelated things are released from this repo, and this file used to
cover only the first:

- **Pre-built QEMU binaries** -- the tarballs `ltvm install` downloads,
  rebuilt by hand when QEMU is bumped.  Below.
- **Target releases** -- the per-(target, arch, kernel, variant) asset
  sets `ltvm target publish` produces and `ltvm target fetch` consumes.
  Republishing those is a different procedure, at the end of this file.

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


## Republishing Target Releases

Needed when `SCHEMA_VERSION` moves (see CLAUDE.md's *Release Manifest
Schema*), because fetch compares the manifest schema for equality and
refuses anything else.  Until a release is republished, clients on the
new ltvm cannot fetch it at all.

```bash
# For every (target, arch, kernel, variant) that should stay fetchable:
ltvm build all <target> --lustre-tree ~/lustre-release   # see below
ltvm target publish <target>
```

The rebuild is not optional, and `target publish` enforces it: it
refuses artifacts whose `meta.json` records an older `HASH_SCHEME`,
because publishing those wraps old-formula hashes in a new-schema
manifest -- which fetches cleanly and then reads stale on every client.
`--allow-stale` overrides if you know what you are doing.

Two things worth knowing:

- **Publish clobbers into the existing tag**, so a republished release
  keeps its name.  Clients notice by comparing the manifest's content
  fingerprint, not the tag, and record it beside the tag file.  A client
  that fetched with an older ltvm has no fingerprint recorded, so its
  next fetch re-downloads once and then has one.
- **The republish is the rebuild.**  Whoever publishes holds the
  artifacts locally and pays the one-time rebuild; every fetcher then
  gets matching hashes for free.  So republish before people next fetch,
  not after.

To check what is out there: `ltvm target fetch --list`.
