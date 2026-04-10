ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}

# Ubuntu 24.04 build container for kernel and Lustre client builds.
# GCC 13 is the default on Noble Numbat.
#
# KERNEL_DEB_SOURCE comes from targets.yaml (kernel_deb_source field)
# via --build-arg so the apt-installed kernel source package matches
# the kernel ltvm builds against.
ARG KERNEL_DEB_SOURCE=linux-source-6.8.0

ENV DEBIAN_FRONTEND=noninteractive

# Install build packages from the same shared common/packages-dev.txt
# the rocky containers use.  RHEL package names get translated to
# Debian via package-map.txt; "-" means skip.
COPY common/packages-dev.txt   /tmp/packages-dev.txt
COPY ubuntu2404/package-map.txt /tmp/package-map.txt
RUN apt-get update \
    && cat /tmp/packages-dev.txt \
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
    && apt-get install -y --no-install-recommends "${KERNEL_DEB_SOURCE}" \
    && rm -f /tmp/packages-dev.txt /tmp/package-map.txt \
    && rm -rf /var/lib/apt/lists/*

# Whamcloud-patched e2fsprogs (needed for Lustre userspace tools).
# Pinned via build-e2fsprogs.sh's DEFAULT_E2FS_TAG.
COPY common/build-e2fsprogs.sh /tmp/build-e2fsprogs.sh
RUN apt-get update && apt-get install -y git ca-certificates \
    && bash /tmp/build-e2fsprogs.sh \
    && rm -f /tmp/build-e2fsprogs.sh \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/lib/ccache:${PATH}"
ENV CCACHE_DIR="/ccache"

WORKDIR /build
ENTRYPOINT ["/bin/bash"]
