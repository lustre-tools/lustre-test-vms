#!/usr/bin/env bash
# Build and install source-built tools used in VM images:
#   IOR + mdtest, iozone, pjdfstest, FlameGraph, drgn
#
# Expects gcc, make, autoconf, automake, libtool, curl, pip3
# to already be installed (via the package list install step).
# git is installed here as a build-time dep (not in the VM image packages).
set -euo pipefail

# Ensure build-time deps are available (not in VM image packages)
_missing=()
command -v git &>/dev/null || _missing+=(git)
command -v autoreconf &>/dev/null || _missing+=(autoconf automake libtool)
command -v pip3 &>/dev/null || _missing+=(python3-pip)
if [ ${#_missing[@]} -gt 0 ]; then
    dnf -y install "${_missing[@]}" || apt-get -y install "${_missing[@]}"
fi

cd /tmp

# IOR + mdtest (requires MPI -- load openmpi module env on RHEL)
if [ -d /usr/lib64/openmpi/bin ]; then
    export PATH="/usr/lib64/openmpi/bin:$PATH"
    export LD_LIBRARY_PATH="/usr/lib64/openmpi/lib:${LD_LIBRARY_PATH:-}"
fi
curl -sL https://github.com/hpc/ior/releases/download/4.0.0/ior-4.0.0.tar.gz | tar xz
cd ior-4.0.0 && ./configure && make -j"$(nproc)"
cp src/ior src/mdtest /usr/local/bin/
cd /tmp && rm -rf ior-4.0.0

# iozone
curl -sL http://www.iozone.org/src/current/iozone3_506.tar | tar xf -
cd iozone3_506/src/current && make -j"$(nproc)" linux-AMD64
cp iozone /usr/local/bin/
cd /tmp && rm -rf iozone3_506

# pjdfstest
git clone https://github.com/pjd/pjdfstest.git
cd pjdfstest && autoreconf -ifs && ./configure && make -j"$(nproc)"
cp pjdfstest /usr/local/bin/
cd /tmp && rm -rf pjdfstest

# FlameGraph
git clone --depth 1 https://github.com/brendangregg/FlameGraph.git \
    /usr/local/FlameGraph
for f in flamegraph.pl stackcollapse-perf.pl stackcollapse.pl difffolded.pl; do
    ln -sf /usr/local/FlameGraph/"$f" /usr/local/bin/"$f"
done

# drgn (Python crash analysis)
# --break-system-packages needed on Ubuntu 24.04+ (PEP 668)
pip3 install --break-system-packages drgn 2>/dev/null \
    || pip3 install drgn
rm -rf /root/.cache/pip
