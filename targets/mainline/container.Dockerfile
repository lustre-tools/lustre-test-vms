ARG BASE_IMAGE=quay.io/rockylinux/rockylinux:10.1
FROM ${BASE_IMAGE}

# Build container for the `mainline` target: vanilla kernel.org
# kernels on a Rocky 10 userspace.
#
# Rocky 10 is the base because its GCC 14 / binutils / pahole 1.31 are
# the newest toolchain ltvm has that mainline is settled against --
# rocky9's GCC 11 sits much closer to the kernel's rising floor, and
# ubuntu2404's pahole 1.25 is already below mainline's 1.26 minimum,
# so BTF generation would fail there.
#
# Kept a separate target rather than a rocky10 variant: the kernel
# source is a different *kind* (kernel.org tarball, not a distro
# SRPM), and variants overlay a container, not a kernel source.

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
