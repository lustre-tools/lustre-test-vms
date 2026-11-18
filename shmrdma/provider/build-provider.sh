#!/bin/bash
# Build and install the shmrdma libibverbs provider, inside the image
# (or a guest), against the rdma-core installed there.
#
#   build-provider.sh SRC_DIR
#
# A provider links against libibverbs' private ABI, which can change
# with any rdma-core release, so it is built in an rdma-core source tree
# of exactly the installed version, fetched from the upstream release.
# Without network, or for a version upstream does not have, it says so
# and installs nothing: the image then has the device for kernel users
# only, as before.
set -euo pipefail

src=$(cd "$1" && pwd)
log() { echo "shmrdma provider: $*" >&2; }

# awk reads all of it: an early exit could kill ldconfig with SIGPIPE
lib=$(ldconfig -p | awk '/libibverbs\.so\.1 / && /x86-64|AArch64|aarch64|ARM64/ && !f {print $NF; f = 1}')
[[ -n "$lib" ]] || lib=$(ldconfig -p | awk '/libibverbs\.so\.1 / && !f {print $NF; f = 1}')
[[ -n "$lib" ]] || { log "no libibverbs installed"; exit 0; }
lib=$(readlink -f "$lib")
if command -v rpm >/dev/null && rpm -qf "$lib" >/dev/null 2>&1; then
	ver=$(rpm -qf --qf '%{VERSION}\n' "$lib" | sed -n 1p)
else
	pkg=$(dpkg -S "$lib" | sed -n 1p | cut -d: -f1)
	ver=$(dpkg-query -W -f '${Version}' "$pkg")
	ver=${ver#*:}
	ver=${ver%%-*}
	ver=${ver%%+*}
fi
# beside the libibverbs found, which is the native one
pdir="$(dirname "$lib")/libibverbs"
[[ -d "$pdir" ]] || { log "no provider directory $pdir"; exit 0; }
log "rdma-core $ver, providers in $pdir"

# rdma-core's build needs cmake, pkg-config and python3
if ! command -v cmake >/dev/null || ! command -v pkg-config >/dev/null ||
   ! command -v python3 >/dev/null; then
	if command -v dnf >/dev/null; then
		dnf -y -q install cmake pkgconf-pkg-config python3
	elif command -v zypper >/dev/null; then
		zypper -n -q install cmake pkg-config python3
	else
		apt-get -q update && apt-get -y -q install cmake pkg-config python3
	fi
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
url="https://github.com/linux-rdma/rdma-core/releases/download/v$ver/rdma-core-$ver.tar.gz"
if ! curl -fsSL --retry 3 --connect-timeout 30 --max-time 300 \
	-o "$work/src.tar.gz" "$url" || ! tar xzf "$work/src.tar.gz" -C "$work"; then
	log "could not fetch $url; not installed"
	exit 0
fi
tree="$work/rdma-core-$ver"
mkdir "$tree/providers/shmrdma"
cp "$src"/shmrdma.c "$src"/shmrdma-abi.h "$src"/shmrdma_abi.h \
	"$src"/CMakeLists.txt "$tree/providers/shmrdma/"
sed -i 's|^add_subdirectory(providers/rxe)$|&\nadd_subdirectory(providers/shmrdma)|' \
	"$tree/CMakeLists.txt"
grep -q 'providers/shmrdma' "$tree/CMakeLists.txt"
pabi=$(sed -n 's/^set(IBVERBS_PABI_VERSION "\([0-9]*\)")/\1/p' "$tree/CMakeLists.txt")

mkdir "$tree/build"
cd "$tree/build"
cmake -DNO_MAN_PAGES=1 -DNO_PYVERBS=1 -DENABLE_STATIC=0 -DENABLE_RESOLVE_NEIGH=0 \
	-DCMAKE_BUILD_TYPE=RelWithDebInfo .. > "$work/cmake.log" 2>&1 ||
	{ tail -20 "$work/cmake.log" >&2; log "cmake failed"; exit 1; }
make -j"$(nproc)" "shmrdma-rdmav$pabi" > "$work/make.log" 2>&1 ||
	{ tail -30 "$work/make.log" >&2; log "build failed"; exit 1; }
grep -A4 "providers/shmrdma/.*warning:" "$work/make.log" >&2 || true
so="lib/libshmrdma-rdmav$pabi.so"

# what it was linked against must be what is installed
if ! grep -aq "IBVERBS_PRIVATE_$pabi" "$lib"; then
	log "installed $lib lacks IBVERBS_PRIVATE_$pabi; not installed"
	exit 1
fi
install -m 0755 "$so" "$pdir/"
mkdir -p /etc/libibverbs.d
echo "driver shmrdma" > /etc/libibverbs.d/shmrdma.driver
log "installed $pdir/libshmrdma-rdmav$pabi.so"
