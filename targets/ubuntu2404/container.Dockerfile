FROM ubuntu:24.04

# Ubuntu 24.04 build container for kernel and Lustre client builds.
# GCC 13 is the default on Noble Numbat.

ENV DEBIAN_FRONTEND=noninteractive

# Kernel build dependencies
RUN apt-get update && apt-get install -y \
        build-essential bc bison flex \
        libelf-dev libssl-dev \
        perl dwarves \
        net-tools hostname diffutils \
        python3 python3-dev \
        rsync tar gzip xz-utils bzip2 \
        kmod cpio \
        debhelper dpkg-dev \
        linux-source-6.8.0 \
    && rm -rf /var/lib/apt/lists/*

# Lustre build dependencies (autogen + configure + make)
RUN apt-get update && apt-get install -y \
        autoconf automake libtool git patch \
        libyaml-dev libnl-3-dev libnl-genl-3-dev libmount-dev \
        libselinux1-dev zlib1g-dev \
        texinfo pkg-config \
        libkeyutils-dev libfuse-dev \
        libjson-c-dev libaio-dev \
    && apt-get install -y nasm 2>/dev/null || true \
    && rm -rf /var/lib/apt/lists/*

# Whamcloud-patched e2fsprogs (needed for Lustre userspace tools)
RUN apt-get update && apt-get install -y git \
    && E2FS_REPO=https://review.whamcloud.com/tools/e2fsprogs \
    && TAG=$(git ls-remote --tags "$E2FS_REPO" 'refs/tags/v*wc*' \
        | grep -v '\^{}' \
        | awk '{print $2}' \
        | sed 's|refs/tags/||' \
        | sort -V \
        | tail -1) \
    && echo "e2fsprogs: using tag $TAG" \
    && git clone --depth 1 --branch "$TAG" \
        "$E2FS_REPO" /tmp/e2fsprogs \
    && cd /tmp/e2fsprogs \
    && ./configure --prefix=/usr --with-root-prefix="" \
        --enable-elf-shlibs --disable-uuidd \
    && make -j$(nproc) \
    && make install \
    && make install-libs \
    && ldconfig \
    && echo "e2fsprogs: $(pkg-config --modversion ext2fs)" \
    && cd / && rm -rf /tmp/e2fsprogs \
    && rm -rf /var/lib/apt/lists/*

# ccache for faster rebuilds
RUN apt-get update && apt-get install -y ccache \
    && rm -rf /var/lib/apt/lists/*
ENV PATH="/usr/lib/ccache:${PATH}"
ENV CCACHE_DIR="/ccache"

WORKDIR /build
ENTRYPOINT ["/bin/bash"]
