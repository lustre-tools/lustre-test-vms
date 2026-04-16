# Releasing

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

gh release upload qemu-9.2.2 /tmp/qemu-9.2.2-el9.tar.gz --clobber
gh release upload qemu-9.2.2 /tmp/qemu-9.2.2-el10.tar.gz --clobber
```

Notes:
- Rocky 8 needs `dnf install python38` (system python too old)
- Ubuntu uses system QEMU package (has microvm)
- Bump `QEMU_VERSION` in `ltvm_pkg/host_setup.py` when updating
