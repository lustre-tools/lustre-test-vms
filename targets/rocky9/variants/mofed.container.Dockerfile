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

ARG VARIANT_MOFED_VERSION=24.10-2.1.8.0
ARG VARIANT_MOFED_DISTRO=rhel9.5
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
    BUNDLE="MLNX_OFED_LINUX-${MOFED_VERSION}-${VARIANT_MOFED_DISTRO}-${MOFED_ARCH}"; \
    URL="https://content.mellanox.com/ofed/MLNX_OFED-${MOFED_VERSION}/${BUNDLE}.tgz"; \
    mkdir -p /opt/mofed-src && cd /opt/mofed-src; \
    curl -fsSL -o "${BUNDLE}.tgz" "${URL}"; \
    tar xzf "${BUNDLE}.tgz"; \
    rm -f "${BUNDLE}.tgz"; \
    ln -s "${BUNDLE}" current

# Install userspace + -devel packages (no kmods at this stage; those
# get rebuilt in the image overlay against the target kernel).
# Install userspace-only bundle + mlnx-ofa_kernel-devel.  The
# bundle ships `-user-only` metapackages specifically for the "no
# kmods" case, which is what we want in the BUILDER: Lustre
# configure just needs the -devel headers to link o2iblnd.
# mlnx-ofa_kernel-devel provides MOFED's ibverbs/rdma_cm kernel
# headers so `--with-o2ib=<path>` works when Lustre's configure
# scans for the MOFED-OFED kernel source tree.
RUN cd /opt/mofed-src/current/RPMS && \
    ls *.rpm \
      | grep -vE '^(kmod-|kernel-mft-mlnx-|mlnx-ofed-)' \
      | xargs dnf install -y --allowerasing --nogpgcheck --setopt=install_weak_deps=False \
    && dnf clean all

# Path hint for Lustre configure --with-o2ib; mlnx-ofa_kernel-devel
# installs headers under /usr/src/ofa_kernel/default.
ENV O2IB_PATH=/usr/src/ofa_kernel/default
