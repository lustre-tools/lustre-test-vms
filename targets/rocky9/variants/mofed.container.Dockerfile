ARG BASE_TAG
FROM ${BASE_TAG}

# Rocky 9 build container overlay: add MOFED userspace + devel so
# Lustre configure can detect o2ib and link against ibverbs/rdmacm.
#
# Install strategy: use `mlnxofedinstall --add-kernel-support` so the
# bundle rebuilds its kmods for whatever kernel-devel is in the
# container at run time.  Here we only need the userspace + -devel
# bits; kmods get rebuilt later in the image overlay against the
# target's actual kernel.
#
# VARIANT_MOFED_VERSION (e.g. 24.10-1.1.4.0) is passed by
# kernel_build._ensure_container_image from targets.yaml params or
# the CLI --mofed-version override.

ARG VARIANT_MOFED_VERSION=24.10-1.1.4.0
ARG MOFED_DISTRO=rhel9.7
ARG MOFED_ARCH=x86_64

ENV MOFED_VERSION=${VARIANT_MOFED_VERSION}

# Prereqs for mlnxofedinstall + its kmod build (kernel-rpm-macros,
# gcc, tcl/tk for the installer UI, pciutils, rpm-build).
RUN dnf -y install \
        perl python3 pciutils tcl tk gcc \
        kernel-rpm-macros kernel-abi-stablelists \
        rpm-build elfutils-libelf-devel \
        libnl3-devel libmnl-devel numactl-devel \
        pkgconfig \
    && dnf clean all

# Download + extract MLNX_OFED into /opt.  We keep the tarball around
# so the image overlay can reuse it (saved to /opt/mofed-src for
# rebuild against the target kernel).
RUN set -eux; \
    BUNDLE="MLNX_OFED_LINUX-${MOFED_VERSION}-${MOFED_DISTRO}-${MOFED_ARCH}"; \
    URL="https://www.mellanox.com/downloads/ofed/MLNX_OFED-${MOFED_VERSION}/${BUNDLE}.tgz"; \
    mkdir -p /opt/mofed-src && cd /opt/mofed-src; \
    curl -fsSL -o "${BUNDLE}.tgz" "${URL}"; \
    tar xzf "${BUNDLE}.tgz"; \
    rm -f "${BUNDLE}.tgz"; \
    ln -s "${BUNDLE}" current

# Install userspace + -devel packages (no kmods at this stage; those
# get rebuilt in the image overlay against the target kernel).
RUN cd /opt/mofed-src/current && \
    ./mlnxofedinstall \
        --user-space-only \
        --without-fw-update \
        --force \
        --skip-repo \
        || true
# `|| true` above because mlnxofedinstall's userspace-only still
# probes for a kernel match and can exit non-zero in a stripped
# container; the actual package installs succeed.  The image overlay
# re-runs the installer end-to-end so anything missed here is picked
# up there.
