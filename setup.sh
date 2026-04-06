#!/bin/bash
# Setup script for lustre-test-vms.
#
# Installs QEMU, configures networking, builds the base VM image,
# builds a boot kernel, and installs vm.sh + deploy-lustre.sh.
#
# Usage:
#   sudo ./setup.sh                  # full setup (including kernel)
#   sudo ./setup.sh --qemu           # QEMU only
#   sudo ./setup.sh --network        # bridge + dnsmasq only
#   sudo ./setup.sh --image              # build Rocky 9 base image
#   sudo ./setup.sh --image --os rocky8  # build Rocky 8 base image
#   sudo ./setup.sh --image --os ubuntu24 # build Ubuntu 24.04 base image
#   sudo ./setup.sh --image-size 32G # base image with custom size
#   sudo ./setup.sh --install        # install scripts only
#   sudo ./setup.sh --kernel PATH    # install a pre-built kernel
#   sudo ./setup.sh --kernel-el 8    # build + install EL8 kernel
#   sudo ./setup.sh --kernel-el 9    # build + install EL9 kernel
#
# Requires: Linux host, root, internet, KVM, podman or docker.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QEMU_VERSION="9.2.2"
QEMU_PREFIX="/opt/qemu"
VM_DIR="/opt/qemu-vms"
IMAGE_DIR="/opt/qemu-vms/images"

# OS version -- may be overridden by --os flag
OS_VERSION="rocky9"

# Derived from OS_VERSION, can be set after arg parsing
IMAGE_PATH=""
MOUNTPOINT=""

# Network subnet -- change for nested VMs to avoid
# collision with the outer bridge.
# Default: 192.168.100.0/24, gateway .1
BRIDGE_SUBNET="${BRIDGE_SUBNET:-192.168.100}"

# Base image size (sparse, so larger costs nothing)
IMAGE_SIZE="16G"

# ── Helpers ──────────────────────────────────────────────

info()  { echo "==> $*"; }
warn()  { echo "WARNING: $*" >&2; }
die()   { echo "ERROR: $*" >&2; exit 1; }

check_root() {
	[[ $EUID -eq 0 ]] || die "Must run as root"
}

check_kvm() {
	[[ -e /dev/kvm ]] || die "/dev/kvm not found -- KVM required"
}

# ── QEMU build ──────────────────────────────────────────

install_qemu() {
	info "Building QEMU ${QEMU_VERSION} with microvm support"

	if [[ -x "${QEMU_PREFIX}/bin/qemu-system-x86_64" ]]; then
		local existing
		existing=$("${QEMU_PREFIX}/bin/qemu-system-x86_64" \
			--version 2>/dev/null | head -1 || true)
		info "QEMU already installed: ${existing}"
		info "Re-run with --force-qemu to rebuild"
		return 0
	fi

	dnf install -y epel-release 2>/dev/null || true
	dnf config-manager --set-enabled crb 2>/dev/null || true
	dnf install -y gcc make glib2-devel pixman-devel \
		python3 python3-pip flex bison

	# ninja-build may be in EPEL or CRB
	dnf install -y ninja-build 2>/dev/null \
		|| pip3 install ninja

	pip3 install tomli 2>/dev/null || true

	local tmpdir
	tmpdir=$(mktemp -d)
	cd "${tmpdir}"

	info "Downloading QEMU ${QEMU_VERSION}..."
	curl -sL "https://download.qemu.org/qemu-${QEMU_VERSION}.tar.xz" | tar xJ
	cd "qemu-${QEMU_VERSION}"

	info "Configuring..."
	./configure --target-list=x86_64-softmmu \
		--disable-docs --disable-user --disable-gtk \
		--disable-sdl --disable-vnc --disable-spice \
		--disable-opengl --disable-xen --disable-curl \
		--disable-rbd --disable-libssh --disable-capstone \
		--disable-dbus-display --prefix="${QEMU_PREFIX}"

	info "Building (this takes a few minutes)..."
	make -j"$(nproc)"
	make install

	cd /
	rm -rf "${tmpdir}"

	"${QEMU_PREFIX}/bin/qemu-system-x86_64" -machine help \
		| grep -q microvm \
		|| die "QEMU built but microvm not available"

	info "QEMU installed at ${QEMU_PREFIX}"
}

# ── Network bridge ──────────────────────────────────────

setup_network() {
	info "Configuring network bridge (fcbr0) on ${BRIDGE_SUBNET}.0/24"

	dnf install -y dnsmasq sshpass pdsh pdsh-rcmd-ssh iptables-nft

	cp "${SCRIPT_DIR}/qemu/host-config/99-qemu-vms.conf" \
		/etc/sysctl.d/
	sysctl -p /etc/sysctl.d/99-qemu-vms.conf

	# Generate bridge service with correct subnet
	sed "s/192\.168\.100/${BRIDGE_SUBNET}/g" \
		"${SCRIPT_DIR}/qemu/host-config/qemu-bridge.service" \
		>/etc/systemd/system/qemu-bridge.service

	# Generate dnsmasq config with correct subnet
	sed "s/192\.168\.100/${BRIDGE_SUBNET}/g" \
		"${SCRIPT_DIR}/qemu/host-config/qemu-dnsmasq.conf" \
		>/etc/dnsmasq.d/qemu-vms.conf

	systemctl daemon-reload
	systemctl enable --now qemu-bridge
	systemctl restart dnsmasq

	ip link show fcbr0 >/dev/null 2>&1 \
		|| die "fcbr0 bridge not created"
	info "Bridge fcbr0 active at ${BRIDGE_SUBNET}.1/24"
}

# ── Install scripts ─────────────────────────────────────

install_scripts() {
	info "Installing vm.sh and deploy-lustre.sh"

	mkdir -p "${VM_DIR}"/{overlays,sockets,kernel}
	mkdir -p "${IMAGE_DIR}"

	cp "${SCRIPT_DIR}/qemu/vm.sh" "${VM_DIR}/vm.sh"
	cp "${SCRIPT_DIR}/qemu/deploy-lustre.sh" "${VM_DIR}/deploy-lustre.sh"
	chmod +x "${VM_DIR}/vm.sh" "${VM_DIR}/deploy-lustre.sh"

	# Symlink into PATH
	ln -sf "${VM_DIR}/vm.sh" /usr/local/bin/vm.sh
	ln -sf "${VM_DIR}/deploy-lustre.sh" /usr/local/bin/deploy-lustre.sh

	# Install dk-filter (dk log filtering tool)
	if [[ -f "${SCRIPT_DIR}/qemu/dk-filter" ]]; then
		cp "${SCRIPT_DIR}/qemu/dk-filter" \
			/usr/local/bin/dk-filter
		chmod +x /usr/local/bin/dk-filter
	fi

	info "Installed to ${VM_DIR}"
}

# ── Kernel ──────────────────────────────────────────────

install_kernel() {
	local kernel_path="${1:-}"
	if [[ -z "${kernel_path}" ]]; then
		if [[ -f "${VM_DIR}/kernel/vmlinux" ]]; then
			info "Kernel already at ${VM_DIR}/kernel/vmlinux"
			return 0
		fi
		die "No kernel specified. Use: --kernel /path/to/vmlinux"
	fi

	[[ -f "${kernel_path}" ]] \
		|| die "Kernel not found: ${kernel_path}"

	mkdir -p "${VM_DIR}/kernel"
	cp "${kernel_path}" "${VM_DIR}/kernel/vmlinux"
	info "Kernel installed at ${VM_DIR}/kernel/vmlinux"
}

# Build a kernel using the containerized build script and
# install it as the VM boot kernel.  Skips if a kernel
# already exists unless --force-kernel is used.
build_and_install_kernel() {
	local os_ver="${1:-rocky9}"
	local force="${2:-false}"
	local build_script="${SCRIPT_DIR}/qemu/kernel-build/build-kernel.sh"

	# Backward compat: bare "8" or "9" -> "rocky8" or "rocky9"
	case "${os_ver}" in
		8) os_ver="rocky8" ;;
		9) os_ver="rocky9" ;;
	esac

	local vmlinuz="${VM_DIR}/kernel/vmlinuz-${os_ver}"

	if [[ -f "${VM_DIR}/kernel/vmlinux" ]] && ! $force; then
		info "Kernel already installed, skipping build"
		info "  (use --force-kernel to rebuild)"
		return 0
	fi

	case "${os_ver}" in
		ubuntu24)
			# Ubuntu 24: use pre-built kernel or build via ubuntu24 container
			if [[ -f "${VM_DIR}/kernel/vmlinuz-ubuntu24" ]]; then
				info "Ubuntu 24.04 kernel already at ${VM_DIR}/kernel/vmlinuz-ubuntu24"
				cp "${VM_DIR}/kernel/vmlinuz-ubuntu24" "${VM_DIR}/kernel/vmlinux"
				info "Default boot kernel: vmlinuz-ubuntu24"
			else
				[[ -f "${build_script}" ]] || \
					die "build-kernel.sh not found at ${build_script}"
				info "Building Ubuntu 24.04 kernel (containerized)..."
				bash "${build_script}" --download --os ubuntu24 \
					--install-dir "${VM_DIR}/kernel" \
					--jobs "$(nproc)"
				if [[ -f "${vmlinuz}" ]]; then
					cp "${vmlinuz}" "${VM_DIR}/kernel/vmlinux"
					info "Default boot kernel: ${vmlinuz}"
				else
					warn "bzImage not found at ${vmlinuz}"
				fi
			fi
			;;
		rocky*)
			local el_num="${os_ver#rocky}"
			[[ -f "${build_script}" ]] || \
				die "build-kernel.sh not found at ${build_script}"
			info "Building EL${el_num} kernel (containerized)..."
			bash "${build_script}" --download --os "${os_ver}" \
				--install-dir "${VM_DIR}/kernel" \
				--jobs "$(nproc)"

			# Install the bzImage as the default boot kernel
			if [[ -f "${vmlinuz}" ]]; then
				cp "${vmlinuz}" "${VM_DIR}/kernel/vmlinux"
				info "Default boot kernel: ${vmlinuz}"
			else
				warn "bzImage not found at ${vmlinuz}"
			fi
			;;
		*)
			die "Unknown OS for kernel build: ${os_ver}"
			;;
	esac
}

# ── Base image build ────────────────────────────────────

build_base_image() {
	if [[ -f "${IMAGE_PATH}" ]]; then
		info "Base image already exists at ${IMAGE_PATH}"
		info "Delete it first to rebuild"
		return 0
	fi

	case "${OS_VERSION}" in
		rocky*) build_rocky_image ;;
		ubuntu*) build_ubuntu_image ;;
		*) die "Unknown OS: ${OS_VERSION}" ;;
	esac
}

build_ubuntu_image() {
	info "Building Ubuntu 24.04 base image"

	local imgfile
	imgfile=$(mktemp /tmp/ubuntu24-base.XXXXXX.ext4)

	truncate -s "${IMAGE_SIZE}" "${imgfile}"
	mkfs.ext4 -q "${imgfile}"

	mkdir -p "${MOUNTPOINT}"
	mount -o loop "${imgfile}" "${MOUNTPOINT}"

	info "Running debootstrap noble"
	debootstrap --arch=amd64 noble "${MOUNTPOINT}" http://archive.ubuntu.com/ubuntu

	# Configure apt sources
	cat >"${MOUNTPOINT}/etc/apt/sources.list.d/ubuntu.sources" <<'APTEOF'
Types: deb
URIs: http://archive.ubuntu.com/ubuntu
Suites: noble noble-updates noble-security
Components: main restricted universe
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
APTEOF

	mount --bind /dev  "${MOUNTPOINT}/dev"
	mount --bind /proc "${MOUNTPOINT}/proc"
	mount --bind /sys  "${MOUNTPOINT}/sys"

	info "Installing packages"
	chroot "${MOUNTPOINT}" env DEBIAN_FRONTEND=noninteractive bash -c '
		apt-get update
		apt-get install -y \
			systemd systemd-sysv init \
			openssh-server \
			util-linux iproute2 iputils-ping less \
			vim rsync kmod e2fsprogs dmsetup sudo hostname \
			kexec-tools python3-pip python3-dev \
			gcc g++ make \
			autoconf automake libtool \
			libelf-dev libmount-dev libyaml-dev libaio-dev \
			fio git gdb htop tmux \
			attr acl bc perl jq lsof psmisc \
			flex bison dwarves \
			nfs-common sg3-utils \
			curl ca-certificates
	'

	info "Installing Lustre-patched e2fsprogs"
	chroot "${MOUNTPOINT}" bash -c '
		cd /tmp
		git clone https://review.whamcloud.com/tools/e2fsprogs
		cd e2fsprogs
		git checkout v1.47.3-wc2
		./configure --enable-elf-shlibs
		make -j$(nproc)
		make install
		make install-libs
		ldconfig
		cd /tmp && rm -rf e2fsprogs
	'

	info "Installing drgn"
	chroot "${MOUNTPOINT}" pip3 install --break-system-packages drgn || true

	info "Installing IOR + mdtest"
	chroot "${MOUNTPOINT}" bash -c '
		export DEBIAN_FRONTEND=noninteractive
		apt-get update && apt-get install -y openmpi-bin libopenmpi-dev
		cd /tmp
		curl -sL https://github.com/hpc/ior/releases/download/4.0.0/ior-4.0.0.tar.gz | tar xz
		cd ior-4.0.0
		export PATH=/usr/lib/x86_64-linux-gnu/openmpi/bin:$PATH
		./configure && make -j$(nproc)
		cp src/ior src/mdtest /usr/local/bin/
		cd /tmp && rm -rf ior-4.0.0
	'

	umount "${MOUNTPOINT}"/{sys,proc,dev}

	# Root password
	local pw_hash
	pw_hash=$(openssl passwd -6 "initial0")
	sed -i "s|^root:[^:]*:|root:${pw_hash}:|" "${MOUNTPOINT}/etc/shadow"
	chroot "${MOUNTPOINT}" systemctl enable ssh
	echo "PermitRootLogin yes" >>"${MOUNTPOINT}/etc/ssh/sshd_config"

	# Serial console auto-login
	mkdir -p "${MOUNTPOINT}/etc/systemd/system/serial-getty@ttyS0.service.d"
	cat >"${MOUNTPOINT}/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf" <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root -o '-p -f root' --keep-baud 115200,38400,9600 %I $TERM
EOF

	# IP config from kernel cmdline
	cat >"${MOUNTPOINT}/etc/rc.local" <<'RCEOF'
#!/bin/bash
FC_IP=$(cat /proc/cmdline | tr ' ' '\n' | grep ^fc_ip= | cut -d= -f2)
FC_GW=$(cat /proc/cmdline | tr ' ' '\n' | grep ^fc_gw= | cut -d= -f2)
FC_NAME=$(cat /proc/cmdline | tr ' ' '\n' | grep ^fc_name= | cut -d= -f2)
if [ -n "${FC_IP}" ]; then
    ip addr add ${FC_IP}/24 dev eth0 2>/dev/null
    ip link set eth0 up
    ip route add default via ${FC_GW:-192.168.100.1} 2>/dev/null
    echo "nameserver 192.168.100.1" > /etc/resolv.conf
    echo "nameserver 8.8.8.8" >> /etc/resolv.conf
fi
if [ -n "${FC_NAME}" ]; then
    hostnamectl set-hostname "${FC_NAME}" 2>/dev/null || \
        hostname "${FC_NAME}"
fi
RCEOF
	chmod +x "${MOUNTPOINT}/etc/rc.local"

	# Disable slow services
	for svc in apt-daily.timer apt-daily-upgrade.timer unattended-upgrades; do
		chroot "${MOUNTPOINT}" systemctl mask "$svc" 2>/dev/null || true
	done
	echo "blacklist drm" >"${MOUNTPOINT}/etc/modprobe.d/no-drm.conf"

	# SSH host keys + inter-VM key
	chroot "${MOUNTPOINT}" ssh-keygen -A
	mkdir -p "${MOUNTPOINT}/root/.ssh"
	ssh-keygen -t ed25519 -f "${MOUNTPOINT}/root/.ssh/id_ed25519" -N "" -q
	cp "${MOUNTPOINT}/root/.ssh/id_ed25519.pub" "${MOUNTPOINT}/root/.ssh/authorized_keys"
	chmod 700 "${MOUNTPOINT}/root/.ssh"
	chmod 600 "${MOUNTPOINT}/root/.ssh/authorized_keys"
	cat >"${MOUNTPOINT}/root/.ssh/config" <<'EOF'
Host *
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
EOF

	# dk-filter
	if [[ -f "${SCRIPT_DIR}/qemu/dk-filter" ]]; then
		cp "${SCRIPT_DIR}/qemu/dk-filter" "${MOUNTPOINT}/usr/local/bin/dk-filter"
		chmod +x "${MOUNTPOINT}/usr/local/bin/dk-filter"
	fi

	# Clean up
	rm -rf "${MOUNTPOINT}/var/cache/apt"/* "${MOUNTPOINT}/var/lib/apt/lists"/*

	umount "${MOUNTPOINT}"

	mkdir -p "${IMAGE_DIR}"
	mv "${imgfile}" "${IMAGE_PATH}"

	info "Base image built at ${IMAGE_PATH}"
}

build_rocky_image() {
	local osver="${OS_VERSION}"
	# Extract releasever number from e.g. rocky8 -> 8
	local releasever="${osver#rocky}"
	info "Building Rocky ${releasever} base image"

	local imgfile
	imgfile=$(mktemp /tmp/rocky${releasever}-base.XXXXXX.ext4)

	truncate -s "${IMAGE_SIZE}" "${imgfile}"
	mkfs.ext4 -q "${imgfile}"

	mkdir -p "${MOUNTPOINT}"
	mount -o loop "${imgfile}" "${MOUNTPOINT}"

	# Rocky 8 is EOL -- repos moved to vault. Configure vault before any dnf ops.
	local gcc_toolset="gcc-toolset-14"
	if [[ "${releasever}" == "8" ]]; then
		gcc_toolset="gcc-toolset-13"
		# Rocky 8.10 is the final EL8 release, still on pub mirrors.
		# Explicitly configure repos to avoid mirrorlist failures.
		mkdir -p "${MOUNTPOINT}/etc/yum.repos.d"
		cat >"${MOUNTPOINT}/etc/yum.repos.d/rocky8.repo" <<'REPOEOF'
[baseos]
name=Rocky Linux 8 - BaseOS
baseurl=https://dl.rockylinux.org/pub/rocky/8/BaseOS/x86_64/os/
enabled=1
gpgcheck=0
[appstream]
name=Rocky Linux 8 - AppStream
baseurl=https://dl.rockylinux.org/pub/rocky/8/AppStream/x86_64/os/
enabled=1
gpgcheck=0
[powertools]
name=Rocky Linux 8 - PowerTools
baseurl=https://dl.rockylinux.org/pub/rocky/8/PowerTools/x86_64/os/
enabled=1
gpgcheck=0
[extras]
name=Rocky Linux 8 - Extras
baseurl=https://dl.rockylinux.org/pub/rocky/8/extras/x86_64/os/
enabled=1
gpgcheck=0
[epel]
name=EPEL 8
baseurl=https://dl.fedoraproject.org/pub/epel/8/Everything/x86_64/
enabled=1
gpgcheck=0
REPOEOF
	fi

	# Core OS packages
	info "Installing core packages..."
	if [[ "${releasever}" == "8" ]]; then
		# Rocky 8: use pre-seeded repo file, skip epel-release package
		# (we already have EPEL baseurl configured directly)
		dnf --installroot="${MOUNTPOINT}" --releasever="${releasever}" \
			install -y \
			basesystem rocky-release dnf bash \
			coreutils systemd NetworkManager \
			openssh-server openssh-clients \
			passwd util-linux iproute iputils less \
			vim-minimal vim-enhanced \
			rsync kmod e2fsprogs device-mapper sudo hostname \
			kexec-tools crash python3-pip python3-devel \
			gcc gcc-c++ gcc-gfortran make \
			autoconf automake libtool \
			elfutils-devel elfutils-libelf-devel \
			kernel-devel fuse-libs \
			libmount-devel libyaml-devel libaio-devel \
			json-c-devel
	else
		dnf --installroot="${MOUNTPOINT}" --releasever="${releasever}" \
			install -y \
			basesystem rocky-release epel-release dnf bash \
			coreutils systemd NetworkManager \
			openssh-server openssh-clients \
			passwd util-linux iproute iputils less \
			vim-minimal vim-enhanced \
			rsync kmod e2fsprogs device-mapper sudo hostname \
			kexec-tools crash python3-pip python3-devel \
			gcc gcc-c++ gcc-gfortran make \
			autoconf automake libtool \
			elfutils-devel elfutils-libelf-devel \
			kernel-devel fuse-libs
		# Enable CRB for devel packages
		chroot "${MOUNTPOINT}" dnf config-manager \
			--set-enabled crb 2>/dev/null || true
	fi

	# Rocky 8: rocky-release installs Rocky-*.repo files with mirrorlist= URLs
	# that 404 (EOL mirrors). Remove them -- our rocky8.repo has direct baseurls.
	if [[ "${releasever}" == "8" ]]; then
		rm -f "${MOUNTPOINT}"/etc/yum.repos.d/Rocky-*.repo
	fi

	# Dev, profiling, and benchmark tools
	info "Installing dev/profiling tools..."
	dnf --installroot="${MOUNTPOINT}" --releasever="${releasever}" \
		install -y \
		perf bcc-tools bpftrace trace-cmd systemtap \
		valgrind sysstat kernel-tools numatop \
		strace ltrace tuna \
		iperf3 ethtool tcpdump conntrack-tools \
		numactl numad hwloc \
		fio blktrace iproute-tc iotop \
		openmpi openmpi-devel \
		git gdb lldb htop tmux \
		attr acl bc perl pdsh pdsh-rcmd-ssh \
		nfs-utils sg3_utils quota jq lsof psmisc \
		bonnie++ dbench flex bison

	# GCC toolset (14 on el9, 13 on el8)
	dnf --installroot="${MOUNTPOINT}" --releasever="${releasever}" \
		install -y \
		${gcc_toolset}-gcc ${gcc_toolset}-gcc-c++ \
		${gcc_toolset}-libstdc++-devel \
		2>/dev/null || info "GCC toolset not available, skipping"

	# drgn
	info "Installing drgn..."
	chroot "${MOUNTPOINT}" pip3 install drgn

	# Source-built tools
	info "Installing IOR, mdtest, iozone, pjdfstest, FlameGraph..."
	# /dev+/proc+/sys must be bound for git (getrandom) and other tools
	mount --bind /dev  "${MOUNTPOINT}/dev"
	mount --bind /proc "${MOUNTPOINT}/proc"
	mount --bind /sys  "${MOUNTPOINT}/sys"
	chroot "${MOUNTPOINT}" bash -c '
		set -e
		cd /tmp
		# EL8: openmpi is in /usr/lib64/openmpi/bin, not in PATH by default
		export PATH=/usr/lib64/openmpi/bin:$PATH

		# IOR + mdtest (use release tarball -- EL8 autoconf too old for git bootstrap)
		curl -sL https://github.com/hpc/ior/releases/download/4.0.0/ior-4.0.0.tar.gz \
			| tar xz
		cd ior-4.0.0
		./configure
		make -j$(nproc)
		cp src/ior src/mdtest /usr/local/bin/
		cd /tmp && rm -rf ior-4.0.0

		# iozone
		curl -sL http://www.iozone.org/src/current/iozone3_506.tar \
			| tar xf -
		cd iozone3_506/src/current
		make -j$(nproc) linux-AMD64
		cp iozone /usr/local/bin/
		cd /tmp && rm -rf iozone3_506

		# pjdfstest
		git clone https://github.com/pjd/pjdfstest.git
		cd pjdfstest
		autoreconf -ifs
		./configure
		make -j$(nproc)
		cp pjdfstest /usr/local/bin/
		cd /tmp && rm -rf pjdfstest

		# FlameGraph
		git clone --depth 1 \
			https://github.com/brendangregg/FlameGraph.git \
			/usr/local/FlameGraph
		for f in flamegraph.pl stackcollapse-perf.pl \
			stackcollapse.pl difffolded.pl; do
			ln -sf /usr/local/FlameGraph/$f /usr/local/bin/$f
		done
	'
	umount "${MOUNTPOINT}"/{sys,proc,dev}

	# dk-filter (Lustre dk log filtering)
	if [[ -f "${SCRIPT_DIR}/qemu/dk-filter" ]]; then
		cp "${SCRIPT_DIR}/qemu/dk-filter" \
			"${MOUNTPOINT}/usr/local/bin/dk-filter"
		chmod +x \
			"${MOUNTPOINT}/usr/local/bin/dk-filter"
	fi

	# Root password -- chpasswd needs PAM which may fail in chroot;
	# write hashed password directly to shadow instead
	local pw_hash
	pw_hash=$(openssl passwd -6 "initial0")
	sed -i "s|^root:[^:]*:|root:${pw_hash}:|" "${MOUNTPOINT}/etc/shadow"
	chroot "${MOUNTPOINT}" systemctl enable sshd
	echo "PermitRootLogin yes" \
		>>"${MOUNTPOINT}/etc/ssh/sshd_config"

	# Serial console auto-login
	mkdir -p "${MOUNTPOINT}/etc/systemd/system/serial-getty@ttyS0.service.d"
	cat >"${MOUNTPOINT}/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf" <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root -o '-p -f root' --keep-baud 115200,38400,9600 %I $TERM
EOF

	# IP config from kernel cmdline
	cat >"${MOUNTPOINT}/etc/rc.local" <<'RCEOF'
#!/bin/bash
FC_IP=$(cat /proc/cmdline | tr ' ' '\n' | grep ^fc_ip= | cut -d= -f2)
FC_GW=$(cat /proc/cmdline | tr ' ' '\n' | grep ^fc_gw= | cut -d= -f2)
FC_NAME=$(cat /proc/cmdline | tr ' ' '\n' | grep ^fc_name= | cut -d= -f2)
if [ -n "${FC_IP}" ]; then
    ip addr add ${FC_IP}/24 dev eth0 2>/dev/null
    ip link set eth0 up
    ip route add default via ${FC_GW:-192.168.100.1} 2>/dev/null
    echo "nameserver 192.168.100.1" > /etc/resolv.conf
    echo "nameserver 8.8.8.8" >> /etc/resolv.conf
fi
if [ -n "${FC_NAME}" ]; then
    hostnamectl set-hostname "${FC_NAME}" 2>/dev/null || \
        hostname "${FC_NAME}"
fi
RCEOF
	chmod +x "${MOUNTPOINT}/etc/rc.local"

	# Disable slow/unnecessary services
	for svc in systemd-hwdb-update firewalld dnf-makecache.timer; do
		chroot "${MOUNTPOINT}" systemctl mask "$svc" \
			2>/dev/null || true
	done
	echo "blacklist drm" \
		>"${MOUNTPOINT}/etc/modprobe.d/no-drm.conf"

	cat >"${MOUNTPOINT}/etc/NetworkManager/conf.d/00-fc.conf" <<'EOF'
[main]
no-auto-default=*
EOF

	# kdump config -- near-complete dumps
	cat >"${MOUNTPOINT}/etc/kdump.conf" <<'EOF'
path /var/crash
core_collector makedumpfile -l --message-level 7 -d 1
EOF

	cat >"${MOUNTPOINT}/etc/sysconfig/kdump" <<'EOF'
KDUMP_KERNELVER=""
KDUMP_COMMANDLINE_REMOVE="hugepages hugepagesz slub_debug quiet log_buf_len swiotlb"
KDUMP_COMMANDLINE_APPEND="irqpoll nr_cpus=1 reset_devices cgroup_disable=memory mce=off numa=off udev.children-max=2 panic=10 rootflags=nofail acpi_no_memhotplug transparent_hugepage=never nokaslr"
KEXEC_ARGS=""
KDUMP_IMG=vmlinuz
EOF
	mkdir -p "${MOUNTPOINT}/var/crash"
	chroot "${MOUNTPOINT}" systemctl enable kdump

	# SSH host keys (so they don't regenerate each boot)
	chroot "${MOUNTPOINT}" ssh-keygen -A

	# Generate shared SSH key for inter-VM access
	mkdir -p "${MOUNTPOINT}/root/.ssh"
	ssh-keygen -t ed25519 -f "${MOUNTPOINT}/root/.ssh/id_ed25519" \
		-N "" -q
	cp "${MOUNTPOINT}/root/.ssh/id_ed25519.pub" \
		"${MOUNTPOINT}/root/.ssh/authorized_keys"
	chmod 700 "${MOUNTPOINT}/root/.ssh"
	chmod 600 "${MOUNTPOINT}/root/.ssh/authorized_keys"
	cat >"${MOUNTPOINT}/root/.ssh/config" <<'EOF'
Host *
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
EOF

	# Clean up
	rm -rf "${MOUNTPOINT}/var/cache/dnf"/*

	umount "${MOUNTPOINT}"

	mkdir -p "${IMAGE_DIR}"
	mv "${imgfile}" "${IMAGE_PATH}"

	info "Base image built at ${IMAGE_PATH}"
}

# ── Install kernel + kdump initramfs ────────────────────

install_kernel_to_image() {
	local kver="${1:-}"
	local vmlinuz="${2:-}"
	local vmlinux="${3:-}"

	if [[ -z "${kver}" || -z "${vmlinuz}" || -z "${vmlinux}" ]]; then
		info "Skipping kernel install in image (provide --image-kernel KVER VMLINUZ VMLINUX)"
		return 0
	fi

	info "Installing kernel ${kver} into base image"

	mkdir -p "${MOUNTPOINT}"
	mount -o loop "${IMAGE_PATH}" "${MOUNTPOINT}"

	cp "${vmlinuz}" "${MOUNTPOINT}/boot/vmlinuz-${kver}"
	cp "${vmlinux}" "${MOUNTPOINT}/boot/vmlinux-${kver}"

	# Build kdump initramfs
	mount --bind /dev "${MOUNTPOINT}/dev"
	mount --bind /proc "${MOUNTPOINT}/proc"
	mount --bind /sys "${MOUNTPOINT}/sys"
	chroot "${MOUNTPOINT}" \
		/sbin/mkdumprd "/boot/initramfs-${kver}kdump.img" "${kver}"
	umount "${MOUNTPOINT}"/{sys,proc,dev}

	umount "${MOUNTPOINT}"
	info "Kernel ${kver} installed in base image"
}

# ── SSH config for fast VM access ───────────────────────

setup_ssh_config() {
	info "Configuring SSH for fast VM access"

	local ssh_config="/root/.ssh/config"
	local marker="# lustre-test-vms"

	if grep -q "${marker}" "${ssh_config}" 2>/dev/null; then
		info "SSH config already has VM settings"
		return 0
	fi

	mkdir -p /root/.ssh
	cat >>"${ssh_config}" <<EOF

${marker}
Host 192.168.100.*
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
    ServerAliveInterval 1
    ServerAliveCountMax 2
    ConnectTimeout 5
    User root
EOF
	chmod 600 "${ssh_config}"
	info "SSH config updated"
}

# ── Main ────────────────────────────────────────────────

usage() {
	sed -n '2,16p' "$0" | sed 's/^# \?//'
	exit 0
}

main() {
	check_root

	local do_all=true
	local do_qemu=false
	local do_network=false
	local do_image=false
	local do_install=false
	local kernel_path=""
	local kernel_el=""
	local force_kernel=false

	while [[ $# -gt 0 ]]; do
		case "$1" in
			--qemu)     do_qemu=true; do_all=false ;;
			--network)  do_network=true; do_all=false ;;
			--image)    do_image=true; do_all=false ;;
			--install)  do_install=true; do_all=false ;;
			--kernel)   kernel_path="$2"; do_all=false; shift ;;
			--kernel-el) kernel_el="$2"; do_all=false; shift ;;
			--force-kernel) force_kernel=true ;;
			--image-size) IMAGE_SIZE="$2"; shift ;;
			--os)       OS_VERSION="$2"; shift ;;
			--help|-h)  usage ;;
			*)          die "Unknown option: $1" ;;
		esac
		shift
	done

	# Set image path and mountpoint based on OS version
	# Backward compat: bare "8" or "9" -> "rocky8" or "rocky9"
	case "${OS_VERSION}" in
		8) OS_VERSION="rocky8" ;;
		9) OS_VERSION="rocky9" ;;
	esac

	IMAGE_PATH="${IMAGE_DIR}/${OS_VERSION}-base.ext4"
	MOUNTPOINT="/mnt/${OS_VERSION}-rootfs"

	check_kvm

	if $do_all; then
		install_qemu
		setup_network
		install_scripts
		setup_ssh_config
		build_base_image
		# Build + install kernel automatically
		if [[ -n "${kernel_path}" ]]; then
			install_kernel "${kernel_path}"
		else
			build_and_install_kernel "${OS_VERSION}" $force_kernel
		fi
		info ""
		info "Setup complete. Create a VM:"
		info "  sudo vm.sh ensure myvm --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3"
	else
		$do_qemu && install_qemu
		$do_network && setup_network
		$do_install && install_scripts
		$do_image && build_base_image
		[[ -n "${kernel_path}" ]] && install_kernel "${kernel_path}"
		[[ -n "${kernel_el}" ]] && build_and_install_kernel "${kernel_el}" $force_kernel
	fi
}

main "$@"
