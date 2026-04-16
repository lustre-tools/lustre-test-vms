ARG BASE_IMAGE=quay.io/rockylinux/rockylinux:10.1
FROM ${BASE_IMAGE}

# Rocky 10 VM base image.
# Built via podman, exported to raw ext4 for QEMU microvm use.
# No kernel installed -- QEMU passes it externally via -kernel.

# Enable EPEL and CRB repos
RUN dnf -y install epel-release dnf-plugins-core \
    && dnf config-manager --set-enabled crb \
    && dnf clean all

# Copy package lists and install
COPY common/packages-base.txt /tmp/packages-base.txt
COPY common/packages-test.txt /tmp/packages-test.txt
COPY common/packages-debug.txt /tmp/packages-debug.txt
COPY common/packages-server.txt /tmp/packages-server.txt
COPY rocky10/packages-os.txt /tmp/packages-os.txt

# Parse package lists (strip comments/blanks) and install.
# Exclude kernel-devel -- Lustre builds on the host, not in VM.
RUN cat /tmp/packages-base.txt \
        /tmp/packages-test.txt \
        /tmp/packages-debug.txt \
        /tmp/packages-server.txt \
        /tmp/packages-os.txt \
    | grep -v '^\s*#' | grep -v '^\s*$' \
    | grep -v '^kernel-devel$' \
    | sort -u \
    | xargs dnf -y --allowerasing --skip-broken install \
    && dnf clean all

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
RUN cat /etc/profile.d/lustre-tests-path.sh >> /etc/bashrc

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
RUN bash /tmp/setup-services.sh dnf-makecache.timer

# Clean up
RUN dnf clean all && rm -rf /var/cache/dnf /tmp/*
