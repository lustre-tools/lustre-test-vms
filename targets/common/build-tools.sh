#!/usr/bin/env bash
# Build and install source-built tools used in VM images:
#   IOR + mdtest, simul, metabench, iozone, pjdfstest, dbench loadfile,
#   FlameGraph, drgn
#
# Expects gcc, make, autoconf, automake, libtool, curl, pip3
# to already be installed (via the package list install step).
# git is installed here as a build-time dep (not in the VM image packages).
#
# Cross-compilation support:
#   Set TARGET_ARCH to cross-compile (e.g. TARGET_ARCH=aarch64).
#   The script detects the host arch and sets up CC/--host
#   accordingly. When not cross-compiling, builds natively.
#
# Output:
#   By default, installs to /usr/local/bin.
#   Set DESTDIR to redirect (e.g. DESTDIR=/output for staged builds).
set -euo pipefail

# Pinned versions of source-built tools.  Bump in one place.
IOR_VERSION="${IOR_VERSION:-4.0.0}"
IOZONE_VERSION="${IOZONE_VERSION:-3_506}"
SIMUL_VERSION="${SIMUL_VERSION:-1.16}"
COMPILEBENCH_REF="${COMPILEBENCH_REF:-28ad3d580d84428beeda5c857f7c4b42bd90f9eb}"
CTHON04_REF="${CTHON04_REF:-86a4501a6e1e415844dc632894a85a5253cc1505}"
TOOLKIT_REF="${TOOLKIT_REF:-3a62943ca32ddc63cac90679f2f43a38b608ea68}"
DBENCH_LOADFILE_REF="${DBENCH_LOADFILE_REF:-a8e1c0fbb8bdb23ee22c0cc2e3f9b1049e537fff}"
VMTOUCH_REF="${VMTOUCH_REF:-af86e27675843b3c7e4ddfee66ddbaf44eff43c4}"

TARGET_ARCH="${TARGET_ARCH:-$(uname -m)}"
HOST_ARCH="$(uname -m)"
DESTDIR="${DESTDIR:-}"
PREFIX="${DESTDIR}/usr/local"

# Cross-compilation setup (only set CC/CXX when cross-compiling;
# leave them unset for native builds so mpicc wrappers work)
CONFIGURE_HOST=""

CROSS_TRIPLE=""
if [[ "$TARGET_ARCH" == "aarch64" && "$HOST_ARCH" != "aarch64" ]]; then
	CROSS_TRIPLE="aarch64-linux-gnu"
elif [[ "$TARGET_ARCH" == "x86_64" && "$HOST_ARCH" != "x86_64" ]]; then
	CROSS_TRIPLE="x86_64-linux-gnu"
fi
if [[ -n "$CROSS_TRIPLE" ]]; then
	CONFIGURE_HOST="--host=$CROSS_TRIPLE"
	# RHEL cross gccs ship without a sysroot or default include path;
	# point them at one if the caller provides it.  The Debian path
	# uses multiarch, so SYSROOT stays unset there.
	if [[ -n "${SYSROOT:-}" ]]; then
		export CC="${CROSS_TRIPLE}-gcc --sysroot=${SYSROOT} -isystem ${SYSROOT}/usr/include"
		export CXX="${CROSS_TRIPLE}-g++ --sysroot=${SYSROOT} -isystem ${SYSROOT}/usr/include"
	else
		export CC="${CROSS_TRIPLE}-gcc"
		export CXX="${CROSS_TRIPLE}-g++"
	fi
	echo "--- Cross-compiling tools: ${HOST_ARCH} -> ${TARGET_ARCH}"
fi

# Ensure build deps are present (may have been skipped by --skip-broken)
if command -v dnf &>/dev/null; then
	dnf -y install gcc gcc-c++ make autoconf automake libtool git curl patch \
		python3-pip 2>/dev/null || true
elif command -v apt-get &>/dev/null; then
	apt-get update && apt-get install -y gcc g++ make autoconf automake patch \
		libtool git curl python3-pip 2>/dev/null || true
fi

# Install cross-compiler if cross-compiling.  Cross direction is
# implied by TARGET_ARCH vs HOST_ARCH (already captured in
# CONFIGURE_HOST above).  The triple in CONFIGURE_HOST is
# --host=<triple>; derive the package-name stem from it so we support
# either direction (aarch64 target from x86 host, x86 target from
# aarch64 host).
if [[ -n "$CONFIGURE_HOST" ]]; then
	CROSS_TRIPLE="${CONFIGURE_HOST#--host=}"
	if command -v dnf &>/dev/null; then
		dnf -y install "gcc-${CROSS_TRIPLE}" "binutils-${CROSS_TRIPLE}" 2>/dev/null || true
	elif command -v apt-get &>/dev/null; then
		# Debian's cross package naming uses x86-64-linux-gnu (hyphen)
		# rather than the RHEL x86_64-linux-gnu (underscore).
		APT_TRIPLE="${CROSS_TRIPLE//x86_64/x86-64}"
		apt-get install -y "gcc-${APT_TRIPLE}" "g++-${APT_TRIPLE}" 2>/dev/null || true
	fi
fi

mkdir -p "$PREFIX/bin"
cd /tmp

# IOR + mdtest
#
# IOR's configure auto-detects MPI via mpicc.  We have no cross-arch
# MPI in the build container (cross-building openmpi means cross-building
# libfabric + ucx + ...), so skip IOR/mdtest on cross builds; install
# them inside the VM via dnf if they're needed at test time.
#
# When the previous form of this section failed during `./configure`,
# `set -e` did *not* abort because a failing middle clause in a
# `cd && configure && make` chain is shielded by &&.  The chain has
# been split into separate commands so any failure is caught.
if [[ -z "$CROSS_TRIPLE" ]]; then
	# Add openmpi to PATH if available (EL installs to /usr/lib64/openmpi/bin)
	if [[ -d /usr/lib64/openmpi/bin ]]; then
		export PATH=/usr/lib64/openmpi/bin:$PATH
		export LD_LIBRARY_PATH="/usr/lib64/openmpi/lib:${LD_LIBRARY_PATH:-}"
	fi
	curl -fsSL "https://github.com/hpc/ior/releases/download/${IOR_VERSION}/ior-${IOR_VERSION}.tar.gz" | tar xz
	cd "ior-${IOR_VERSION}"
	./configure
	make -j"$(nproc)"
	cp src/ior src/mdtest "$PREFIX/bin/"
	cd /tmp && rm -rf "ior-${IOR_VERSION}"

	# simul, for parallel-scale
	curl -fsSL "https://github.com/LLNL/simul/archive/refs/tags/${SIMUL_VERSION}.tar.gz" | tar xz
	cd "simul-${SIMUL_VERSION}"
	# simul.c's inline begin() has no external definition, so it links
	# only under gnu89 inline semantics.
	mpicc -Wall -O2 -fgnu89-inline -o simul simul.c
	cp simul "$PREFIX/bin/"
	cd /tmp && rm -rf "simul-${SIMUL_VERSION}"

	# metabench, for parallel-scale and parallel-scale-nfs*: the 2005
	# NERSC release with Whamcloud's patches, as its toolkit RPM builds it
	mkdir /tmp/metabench && cd /tmp/metabench
	curl -fsSL "https://review.whamcloud.com/plugins/gitiles/build/toolkit/+archive/${TOOLKIT_REF}/benchmark/metabench.tar.gz" | tar xz
	sha256sum -c - <<-EOF
	e4e1efefe65912d295964844e41055dea1bde05cfe2614ad63ec2b809730e73c  metabench.tgz
	b5f9f60ca8d4a025477eab16164c2e61f83875321aeb88d4dc202b15d2f2bedd  wc-custom.patch
	bf535fc08918fae9ee1f2d46ef0191e7c9106bc6fc7334b6c3db44bdea44545c  fix-gather-rcv-datatype.patch
	EOF
	tar xzf metabench.tgz
	patch -d metabench -p1 < wc-custom.patch
	patch -d metabench -p1 < fix-gather-rcv-datatype.patch
	make -C metabench CC=mpicc
	cp metabench/metabench "$PREFIX/bin/"
	cd /tmp && rm -rf /tmp/metabench

	# No RDMA in the VMs, and UCX costs ~90 MB per rank: the 32 mdsrate
	# ranks per client of parallel-scale statahead OOM a 4 GB client.
	for f in "${DESTDIR}"/etc/openmpi*/openmpi-mca-params.conf; do
		[[ -f "$f" ]] || continue
		cat >> "$f" <<-EOF
		pml = ob1
		btl = self,vader,tcp
		osc = ^ucx
		EOF
	done
else
	echo "--- Skipping IOR/mdtest (cross-compile; no cross-arch MPI toolchain)"
fi

# iozone (needs -Wno-error=implicit-* for GCC 14+)
#
# Skipped on cross builds: the linux-AMD64 target's Makefile has a
# literal `x86_64` token (TARGET-substitution) that the cross gcc
# treats as an input filename and aborts; the linux-arm target works
# native but we'd need to special-case the cross side anyway.  Drop
# iozone in cross images; install via dnf inside the VM.
if [[ -z "$CROSS_TRIPLE" ]]; then
	curl -fsSL "http://www.iozone.org/src/current/iozone${IOZONE_VERSION}.tar" | tar xf -
	cd "iozone${IOZONE_VERSION}/src/current"
	EXTRA_CFLAGS="-Wno-error=implicit-int -Wno-error=implicit-function-declaration"
	# iozone uses arch-specific make targets
	case "$TARGET_ARCH" in
		aarch64) IOZONE_TARGET="linux-arm" ;;
		*)       IOZONE_TARGET="linux-AMD64" ;;
	esac
	make -j"$(nproc)" "$IOZONE_TARGET" \
		CC="${CC:-cc}" \
		CFLAGS="-O3 $EXTRA_CFLAGS" \
		C_OPT="-O3 $EXTRA_CFLAGS"
	cp iozone "$PREFIX/bin/"
	cd /tmp && rm -rf "iozone${IOZONE_VERSION}"
else
	echo "--- Skipping iozone (cross-compile; linux-AMD64 Makefile mishandles cross gcc)"
fi

# pjdfstest
git clone https://github.com/pjd/pjdfstest.git
cd pjdfstest
autoreconf -ifs
./configure ${CONFIGURE_HOST:+"$CONFIGURE_HOST"}
make -j"$(nproc)"
cp pjdfstest "$PREFIX/bin/"
# pjdfstest.sh runs $PJDFSTEST_DIR/**/*.t, default /usr/share/pjdfstest,
# and the .t files find the binary beside their tests/ directory.
mkdir -p "${DESTDIR}/usr/share/pjdfstest"
cp -a tests pjdfstest "${DESTDIR}/usr/share/pjdfstest/"
cd /tmp && rm -rf pjdfstest

# dbench's loadfile: the distro package ships only the binary, and
# rundbench skips without client.txt.
mkdir -p "${DESTDIR}/usr/share/dbench"
curl -fsSL "https://raw.githubusercontent.com/sahlberg/dbench/${DBENCH_LOADFILE_REF}/loadfiles/client.load" \
    -o "${DESTDIR}/usr/share/dbench/client.txt"

# compilebench (parallel-scale): Josef Bacik's python3 port.  It reads its
# dataset files from the directory it is run in, which the suite cd's to
# ($cbench_DIR, exported by lustre-tests-path.sh).
mkdir -p "${DESTDIR}/opt/compilebench"
for f in compilebench dataset-patched dataset-patched-compiled \
	dataset-unpatched dataset-unpatched-compiled; do
	curl -fsSL "https://raw.githubusercontent.com/josefbacik/compilebench/${COMPILEBENCH_REF}/$f" \
	    -o "${DESTDIR}/opt/compilebench/$f"
done
sed -i '1s|.*|#!/usr/bin/python3|' "${DESTDIR}/opt/compilebench/compilebench"
chmod 755 "${DESTDIR}/opt/compilebench/compilebench"

# vmtouch (sanity 600a-d, sanity-sec): EPEL10 has no package, so build
# the one C file here for every target; where the distro packages it too,
# both are the same tool.
curl -fsSL "https://raw.githubusercontent.com/hoytech/vmtouch/${VMTOUCH_REF}/vmtouch.c" \
    -o /tmp/vmtouch.c
mkdir -p "$PREFIX/bin"
${CC:-gcc} -Wall -O2 -std=c99 -o "$PREFIX/bin/vmtouch" /tmp/vmtouch.c
rm -f /tmp/vmtouch.c

# connectathon (parallel-scale, parallel-scale-nfs*): the suite runs
# $cnt_DIR/runtests, from a built tree.
if [[ -z "$CROSS_TRIPLE" ]]; then
	git clone git://git.linux-nfs.org/projects/steved/cthon04.git /tmp/cthon04
	git -C /tmp/cthon04 checkout -q "$CTHON04_REF"
	# tests.init is tracked, but make's built-in %: %.sh rule overwrites
	# it with tests.init.sh whenever checkout left that one newer.
	touch /tmp/cthon04/tests.init
	# runtests needs these four; tools/ wants libtirpc, which the image lacks.
	for d in basic general special lock; do
		make -C /tmp/cthon04/$d
	done
	rm -rf /tmp/cthon04/.git
	mkdir -p "${DESTDIR}/opt"
	cp -a /tmp/cthon04 "${DESTDIR}/opt/connectathon"
	rm -rf /tmp/cthon04
fi

# auster by name, without the tests directory on PATH (lustre-tests-path.sh).
# exec by full path: auster finds the tree from its own $0.
cat > "$PREFIX/bin/auster" <<'AUSTER'
#!/bin/sh
for d in /usr/lib64/lustre/tests /usr/lib/lustre/tests; do
	[ -x "$d/auster" ] && exec "$d/auster" "$@"
done
echo "auster: Lustre tests are not installed" >&2
exit 127
AUSTER
chmod 755 "$PREFIX/bin/auster"

# FlameGraph (pure perl scripts -- no compilation needed)
git clone --depth 1 https://github.com/brendangregg/FlameGraph.git \
    "$PREFIX/FlameGraph"
for f in flamegraph.pl stackcollapse-perf.pl stackcollapse.pl difffolded.pl; do
    ln -sf "$PREFIX/FlameGraph/$f" "$PREFIX/bin/$f"
done

# drgn (Python crash analysis) -- skip when cross-compiling
# (needs target-arch Python headers + C extensions)
if [[ -z "$CONFIGURE_HOST" ]]; then
	pip3 install --break-system-packages drgn 2>/dev/null \
	    || pip3 install drgn 2>/dev/null \
	    || echo "WARNING: drgn install failed (non-fatal)"
	rm -rf /root/.cache/pip
else
	echo "--- Skipping drgn (cross-compile; install on target instead)"
fi
