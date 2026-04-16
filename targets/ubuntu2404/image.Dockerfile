ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}

# Ubuntu 24.04 VM base image.
# Built via podman, exported to raw ext4 for QEMU microvm use.
# No kernel installed -- QEMU passes it externally via -kernel.

ENV DEBIAN_FRONTEND=noninteractive

# Copy shared package lists and the ubuntu-specific name map.
COPY common/packages-base.txt   /tmp/packages-base.txt
COPY common/packages-test.txt   /tmp/packages-test.txt
COPY common/packages-debug.txt  /tmp/packages-debug.txt
COPY common/packages-server.txt /tmp/packages-server.txt
COPY ubuntu2404/packages-os.txt /tmp/packages-os.txt
COPY ubuntu2404/package-map.txt /tmp/package-map.txt

# Parse the shared lists, translate RHEL names to Debian names via
# package-map.txt, drop "skip" entries (mapped to "-"), and install.
# Packages without a mapping pass through unchanged (assumes the
# name is identical on both distros).
RUN apt-get update \
    && cat /tmp/packages-base.txt /tmp/packages-test.txt \
           /tmp/packages-debug.txt /tmp/packages-server.txt \
           /tmp/packages-os.txt \
        | grep -v '^\s*#' | grep -v '^\s*$' \
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
        | sort -u \
        | xargs apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copy shared setup scripts
COPY common/build-e2fsprogs.sh /tmp/build-e2fsprogs.sh
COPY common/build-tools.sh     /tmp/build-tools.sh
COPY common/setup-ssh.sh       /tmp/setup-ssh.sh
COPY common/setup-serial.sh    /tmp/setup-serial.sh
COPY common/rc.local           /etc/rc.d/rc.local
COPY common/setup-network.sh   /tmp/setup-network.sh
COPY common/setup-kdump.sh     /tmp/setup-kdump.sh
COPY common/setup-services.sh  /tmp/setup-services.sh
COPY common/lustre-tests-path.sh /etc/profile.d/lustre-tests-path.sh
RUN cat /etc/profile.d/lustre-tests-path.sh >> /etc/bash.bashrc

# Per-NIC hook scripts invoked by rc.local (fc_nics= dispatch).
COPY common/setup-nic-softroce.sh  /usr/local/sbin/setup-nic-softroce.sh
COPY common/setup-lnet-config.sh   /usr/local/sbin/setup-lnet-config.sh
COPY common/setup-lnet-passthrough-resolve.sh \
                                   /usr/local/sbin/setup-lnet-passthrough-resolve.sh
RUN chmod 0755 /usr/local/sbin/setup-nic-softroce.sh \
               /usr/local/sbin/setup-lnet-config.sh \
               /usr/local/sbin/setup-lnet-passthrough-resolve.sh

# Source-built tools: IOR, mdtest, iozone, pjdfstest, FlameGraph, drgn
RUN bash /tmp/build-tools.sh

# Lustre-patched e2fsprogs (pinned release)
RUN bash /tmp/build-e2fsprogs.sh

# System configuration
RUN bash /tmp/setup-ssh.sh
RUN bash /tmp/setup-serial.sh
RUN bash /tmp/setup-network.sh
RUN bash /tmp/setup-kdump.sh
RUN bash /tmp/setup-services.sh unattended-upgrades.service snapd.service snapd.socket

# Clean up
RUN apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/*
