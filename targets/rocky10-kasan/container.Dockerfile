ARG BASE_IMAGE=quay.io/rockylinux/rockylinux:10.1
FROM ${BASE_IMAGE}

# Rocky 10 build container for kernel and Lustre builds.
# GCC 14 matches the EL10 6.12.0 kernel build environment.

# Enable CRB + EPEL repos
RUN dnf -y install dnf-plugins-core epel-release \
    && dnf config-manager --set-enabled crb

# Install build packages from the canonical shared list.
# Match rocky8/rocky9 (no --skip-broken) so dependency resolution
# failures fail loud instead of silently dropping packages.
COPY common/packages-dev.txt /tmp/packages-dev.txt
RUN cat /tmp/packages-dev.txt \
        | grep -v '^\s*#' | grep -v '^\s*$' \
        | sort -u \
        | xargs dnf -y --allowerasing install \
    && dnf clean all && rm -f /tmp/packages-dev.txt

# Whamcloud-patched e2fsprogs (required for server builds).
COPY common/build-e2fsprogs.sh /tmp/build-e2fsprogs.sh
RUN bash /tmp/build-e2fsprogs.sh && rm /tmp/build-e2fsprogs.sh

# Cross-compilers for the opposite arch (best-effort; see rocky9).
RUN dnf -y install gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu 2>/dev/null || true \
    && dnf -y install gcc-x86_64-linux-gnu binutils-x86_64-linux-gnu 2>/dev/null || true \
    && dnf clean all

ENV PATH="/usr/lib64/ccache:${PATH}"
ENV CCACHE_DIR="/ccache"

WORKDIR /build
ENTRYPOINT ["/bin/bash"]
