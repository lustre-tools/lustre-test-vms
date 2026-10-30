ARG BASE_IMAGE=registry.opensuse.org/opensuse/leap:15.6
FROM ${BASE_IMAGE}

# openSUSE Leap 15.6 VM base image.
# Built via podman, exported to raw ext4 for QEMU microvm use.
# No kernel installed -- QEMU passes it externally via -kernel.

# Copy shared package lists and the openSUSE name map.
COPY common/packages-base.txt    /tmp/packages-base.txt
COPY common/packages-test.txt    /tmp/packages-test.txt
COPY common/packages-debug.txt   /tmp/packages-debug.txt
COPY common/packages-server.txt  /tmp/packages-server.txt
COPY opensuse156/packages-os.txt /tmp/packages-os.txt
COPY opensuse156/package-map.txt /tmp/package-map.txt
COPY common/packages-skip-aarch64.txt /tmp/packages-skip-aarch64.txt

# Translate the shared lists to SUSE names and install.  kernel-devel
# is dropped (Lustre builds on the host).  --no-recommends keeps the
# distro kernel out: crash, perf and others recommend kernel-default.
# --force-resolution and the rpm check after it: see
# container.Dockerfile.  packages-skip-<arch>.txt drops the known
# per-arch gaps (numatop is x86_64-only).
RUN SKIP_ARCH=/tmp/packages-skip-$(uname -m).txt \
    && zypper -q --gpg-auto-import-keys refresh \
    && cat /tmp/packages-base.txt /tmp/packages-test.txt \
           /tmp/packages-debug.txt /tmp/packages-server.txt \
           /tmp/packages-os.txt \
        | grep -v '^\s*#' | grep -v '^\s*$' \
        | grep -v '^kernel-devel$' \
        | { [ -s "$SKIP_ARCH" ] && grep -vFxf "$SKIP_ARCH" || cat; } \
        | sort -u \
        | awk 'NR==FNR { \
                 if ($0 ~ /^[[:space:]]*#/ || $0 ~ /^[[:space:]]*$/) next; \
                 rhel=$1; \
                 sub(/^[^[:space:]]+[[:space:]]+/, ""); \
                 map[rhel]=$0; \
                 next \
               } \
               { if ($1 in map) { if (map[$1] != "-") print map[$1] } \
                 else print $1 }' \
            /tmp/package-map.txt - \
        | tr ' ' '\n' \
        | sort -u > /tmp/packages.txt \
    && xargs zypper -n install --no-recommends --force-resolution \
        < /tmp/packages.txt \
    && xargs rpm -q --whatprovides < /tmp/packages.txt > /dev/null \
    && zypper clean -a

# Copy shared setup scripts
COPY common/build-e2fsprogs.sh /tmp/build-e2fsprogs.sh
COPY common/build-tools.sh     /tmp/build-tools.sh
COPY common/setup-ssh.sh       /tmp/setup-ssh.sh
COPY common/setup-serial.sh    /tmp/setup-serial.sh
COPY common/rc.local           /etc/rc.d/rc.local
COPY common/setup-network.sh   /tmp/setup-network.sh
COPY common/setup-services.sh  /tmp/setup-services.sh
COPY common/lustre-tests-path.sh /etc/profile.d/lustre-tests-path.sh
COPY opensuse156/setup-suse.sh /tmp/setup-suse.sh

# Per-NIC hook scripts invoked by rc.local (fc_nics= dispatch).
COPY common/setup-nic-softroce.sh  /usr/local/sbin/setup-nic-softroce.sh
COPY common/setup-lnet-config.sh   /usr/local/sbin/setup-lnet-config.sh
COPY common/setup-lnet-passthrough-resolve.sh \
                                   /usr/local/sbin/setup-lnet-passthrough-resolve.sh
COPY common/setup-rail-routing.sh  /usr/local/sbin/setup-rail-routing.sh
COPY common/setup-lnet-rp-filter.sh /usr/local/sbin/setup-lnet-rp-filter.sh
RUN chmod 0755 /usr/local/sbin/setup-nic-softroce.sh \
               /usr/local/sbin/setup-lnet-config.sh \
               /usr/local/sbin/setup-lnet-passthrough-resolve.sh \
               /usr/local/sbin/setup-rail-routing.sh \
               /usr/local/sbin/setup-lnet-rp-filter.sh

# Source-built tools: IOR, mdtest, iozone, pjdfstest, FlameGraph.  It
# looks for EL's openmpi path; Leap's openmpi4 is elsewhere, and its
# libraries are off the loader path for what mpicc builds.  Its drgn
# install fails on Leap's Python 3.6; drgn goes in under 3.11.  PyPI
# has no aarch64 drgn wheel for it, and the source build needs headers
# the image lacks, so an aarch64 image goes without, as every other
# cross-built image does.
RUN echo /usr/lib64/mpi/gcc/openmpi4/lib64 > /etc/ld.so.conf.d/openmpi4.conf \
    && ldconfig
RUN PATH=/usr/lib64/mpi/gcc/openmpi4/bin:$PATH bash /tmp/build-tools.sh
RUN { python3.11 -m pip install drgn || [ "$(uname -m)" != x86_64 ]; } \
    && rm -rf /root/.cache/pip

# Lustre-patched e2fsprogs (pinned release)
RUN bash /tmp/build-e2fsprogs.sh

# System configuration
RUN bash /tmp/setup-ssh.sh
RUN bash /tmp/setup-serial.sh
RUN bash /tmp/setup-network.sh
RUN bash /tmp/setup-suse.sh
RUN bash /tmp/setup-services.sh

# Clean up
RUN zypper clean -a && rm -rf /tmp/*
