#!/usr/bin/env bash
# Build and install source-built tools used in VM images:
#   IOR + mdtest, iozone, pjdfstest, FlameGraph, drgn
#
# Expects gcc, make, autoconf, automake, libtool, curl, pip3
# to already be installed (via the package list install step).
# git is installed here as a build-time dep (not in the VM image packages).
set -euo pipefail

# Ensure build deps are present (may have been skipped by --skip-broken)
if command -v dnf &>/dev/null; then
	dnf -y install gcc gcc-c++ make autoconf automake libtool git curl \
		python3-pip 2>/dev/null || true
elif command -v apt-get &>/dev/null; then
	apt-get update && apt-get install -y gcc g++ make autoconf automake \
		libtool git curl python3-pip 2>/dev/null || true
fi

cd /tmp

# IOR + mdtest
# Add openmpi to PATH if available (EL installs to /usr/lib64/openmpi/bin)
if [[ -d /usr/lib64/openmpi/bin ]]; then
	export PATH=/usr/lib64/openmpi/bin:$PATH
	export LD_LIBRARY_PATH="/usr/lib64/openmpi/lib:${LD_LIBRARY_PATH:-}"
fi
curl -sL https://github.com/hpc/ior/releases/download/4.0.0/ior-4.0.0.tar.gz | tar xz
cd ior-4.0.0 && ./configure && make -j"$(nproc)"
cp src/ior src/mdtest /usr/local/bin/
cd /tmp && rm -rf ior-4.0.0

# iozone (needs -Wno-error=implicit-* for GCC 14+)
curl -sL http://www.iozone.org/src/current/iozone3_506.tar | tar xf -
cd iozone3_506/src/current
EXTRA_CFLAGS="-Wno-error=implicit-int -Wno-error=implicit-function-declaration"
make -j"$(nproc)" linux-AMD64 \
	CFLAGS="-O3 $EXTRA_CFLAGS" \
	C_OPT="-O3 $EXTRA_CFLAGS"
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
