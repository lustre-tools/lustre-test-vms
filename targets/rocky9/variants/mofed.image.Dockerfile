ARG BASE_IMAGE_TAG
FROM ${BASE_IMAGE_TAG}

# Rocky 9 VM image overlay: add MOFED userspace + kmods to the rootfs.
#
# mlnxofedinstall --add-kernel-support builds kmods for the kernel
# whose headers it finds in the container.  We install matching
# kernel-devel first so the kmods match the kernel that the image's
# /lib/modules/<kver>/ layer will end up with after the subsequent
# kernel-module inject stage.
#
# Note: for the proof-of-concept MOFED variant, we accept the small
# risk that our Lustre-patched kernel's ABI drifts from stock Rocky
# 9.7 kernel-devel.  In practice Lustre patches touch ext4/VFS, not
# the parts MOFED kmods link against.  A follow-up can copy the
# target's kernel build-tree into this overlay's context for an
# exact match (see lustre_test_vms_v2-stp).

ARG VARIANT_MOFED_VERSION=24.10-1.1.4.0
ARG MOFED_DISTRO=rhel9.7
ARG MOFED_ARCH=x86_64

ENV MOFED_VERSION=${VARIANT_MOFED_VERSION}

RUN dnf -y install \
        kernel-devel kernel-headers kernel-rpm-macros \
        perl python3 pciutils tcl tk gcc make \
        rpm-build elfutils-libelf-devel \
        libnl3-devel libmnl-devel numactl-devel \
        pkgconfig \
    && dnf clean all

RUN set -eux; \
    BUNDLE="MLNX_OFED_LINUX-${MOFED_VERSION}-${MOFED_DISTRO}-${MOFED_ARCH}"; \
    URL="https://www.mellanox.com/downloads/ofed/MLNX_OFED-${MOFED_VERSION}/${BUNDLE}.tgz"; \
    mkdir -p /opt/mofed-src && cd /opt/mofed-src; \
    curl -fsSL -o "${BUNDLE}.tgz" "${URL}"; \
    tar xzf "${BUNDLE}.tgz"; \
    rm -f "${BUNDLE}.tgz"; \
    ln -s "${BUNDLE}" current

RUN cd /opt/mofed-src/current && \
    ./mlnxofedinstall \
        --add-kernel-support \
        --without-fw-update \
        --force \
        --skip-repo

# Make sure mlx5_core / rdma_rxe / ib_uverbs etc. are loaded early.
RUN echo -e "mlx5_core\nib_uverbs\nrdma_cm\nrdma_ucm" \
        > /etc/modules-load.d/mofed.conf
