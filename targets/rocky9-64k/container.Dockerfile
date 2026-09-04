ARG BASE_IMAGE=quay.io/rockylinux/rockylinux:9
FROM ${BASE_IMAGE}

# Rocky 9 build container for kernel and Lustre builds.
# GCC 11 matches the EL9 5.14.0 kernel build environment.

# Enable CRB + EPEL repos
RUN dnf -y install dnf-plugins-core epel-release \
    && dnf config-manager --set-enabled crb

# Install build packages from the canonical shared list.  Adding a
# package once in common/packages-dev.txt picks it up across every
# build container.
COPY common/packages-dev.txt /tmp/packages-dev.txt
RUN cat /tmp/packages-dev.txt \
        | grep -v '^\s*#' | grep -v '^\s*$' \
        | sort -u \
        | xargs dnf -y --allowerasing install \
    && dnf clean all && rm -f /tmp/packages-dev.txt

# Whamcloud-patched e2fsprogs (required for server builds).
# ldiskfs/mkfs.lustre needs ext2fs >= 1.47.3-wc2.
COPY common/build-e2fsprogs.sh /tmp/build-e2fsprogs.sh
RUN bash /tmp/build-e2fsprogs.sh && rm /tmp/build-e2fsprogs.sh

# Cross-compilers for the opposite arch (best-effort: EPEL ships
# gcc-aarch64-linux-gnu and gcc-x86_64-linux-gnu on el9, but the
# package set depends on the building host's arch -- we install
# whatever dnf finds and let the inner build script fall back to
# a runtime install if the cross toolchain is missing for the
# container's host arch).
RUN dnf -y install gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu 2>/dev/null || true \
    && dnf -y install gcc-x86_64-linux-gnu binutils-x86_64-linux-gnu 2>/dev/null || true \
    && dnf clean all

ENV PATH="/usr/lib64/ccache:${PATH}"
ENV CCACHE_DIR="/ccache"

WORKDIR /build
ENTRYPOINT ["/bin/bash"]
