#!/bin/bash
# Setup script for lustre-test-vms-v2.
#
# Prepares a Linux host for running Lustre QEMU microVMs:
# builds QEMU, configures networking, and installs scripts.
#
# Usage:
#   sudo ./setup.sh              # full host setup
#   sudo ./setup.sh --qemu       # QEMU only
#   sudo ./setup.sh --network    # bridge + dnsmasq only
#   sudo ./setup.sh --install    # install scripts only
#   sudo ./setup.sh --ssh        # SSH config only
#   sudo ./setup.sh --verify     # check existing setup
#
# Image and kernel builds are handled by ltvm:
#   ./ltvm init rocky9 --lustre-tree /path/to/lustre
#
# Requires: Linux host, root, internet, KVM,
#           podman (for ltvm), basic build tools.
#
# Supported host OSes: Rocky/RHEL/CentOS 8+,
#                      Fedora 38+, Ubuntu 22.04+,
#                      Debian 12+.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QEMU_VERSION="9.2.2"
QEMU_PREFIX="/opt/qemu"
VM_DIR="/opt/qemu-vms"

# Network subnet -- override for nested VMs to avoid
# collision with the outer bridge.
# Default: 192.168.100.0/24, gateway .1
BRIDGE_SUBNET="${BRIDGE_SUBNET:-192.168.100}"

# ── Helpers ─────────────────────────────────────────

info()  { echo "==> $*"; }
warn()  { echo "WARNING: $*" >&2; }
die()   { echo "ERROR: $*" >&2; exit 1; }

# ── Host OS detection ───────────────────────────────

detect_host_os() {
	if [[ -f /etc/os-release ]]; then
		# shellcheck disable=SC1091
		. /etc/os-release
	else
		die "Cannot detect host OS (/etc/os-release missing)"
	fi

	HOST_ID="${ID:-unknown}"
	HOST_VERSION="${VERSION_ID:-0}"
	HOST_MAJOR="${HOST_VERSION%%.*}"

	# Determine package manager
	if command -v dnf &>/dev/null; then
		PKG_MGR="dnf"
	elif command -v apt-get &>/dev/null; then
		PKG_MGR="apt"
	else
		die "No supported package manager found (need dnf or apt-get)"
	fi

	info "Host: ${PRETTY_NAME:-${HOST_ID} ${HOST_VERSION}} (${PKG_MGR})"
}

# Install packages portably.  Accepts RHEL package
# names; translates known differences for apt.
pkg_install() {
	local pkgs=("$@")

	if [[ "${PKG_MGR}" == "dnf" ]]; then
		dnf install -y "${pkgs[@]}" 2>&1 \
			| tail -1 || true
	elif [[ "${PKG_MGR}" == "apt" ]]; then
		local apt_pkgs=()
		local pkg
		for pkg in "${pkgs[@]}"; do
			apt_pkgs+=("$(_translate_pkg "${pkg}")")
		done
		DEBIAN_FRONTEND=noninteractive \
			apt-get install -y "${apt_pkgs[@]}" 2>&1 \
			| tail -1 || true
	fi
}

# Map RHEL package names to Debian/Ubuntu equivalents
# for the small set of host packages we need.
_translate_pkg() {
	local pkg="$1"
	case "${pkg}" in
		glib2-devel)       echo "libglib2.0-dev" ;;
		pixman-devel)      echo "libpixman-1-dev" ;;
		iptables-nft)      echo "iptables" ;;
		pdsh-rcmd-ssh)     echo "pdsh" ;;
		python3-pip)       echo "python3-pip" ;;
		ninja-build)       echo "ninja-build" ;;
		*)                 echo "${pkg}" ;;
	esac
}

# ── Prerequisite checks ────────────────────────────

check_root() {
	[[ $EUID -eq 0 ]] || die "Must run as root"
}

check_kvm() {
	if [[ ! -e /dev/kvm ]]; then
		warn "/dev/kvm not found"
		warn "VMs require KVM.  Check that:"
		warn "  - CPU supports virtualization (grep -c vmx /proc/cpuinfo)"
		warn "  - Nested virt is enabled if this is itself a VM"
		die "KVM required"
	fi
}

check_prerequisites() {
	info "Checking prerequisites..."
	local missing=()

	command -v curl    &>/dev/null || missing+=(curl)
	command -v tar     &>/dev/null || missing+=(tar)
	command -v make    &>/dev/null || missing+=(make)
	command -v gcc     &>/dev/null || missing+=(gcc)
	command -v ip      &>/dev/null || missing+=(iproute2)
	command -v python3 &>/dev/null || missing+=(python3)

	if (( ${#missing[@]} > 0 )); then
		info "Installing missing prerequisites: ${missing[*]}"
		pkg_install "${missing[@]}"
	fi

	# podman is needed for ltvm (not strictly for setup.sh,
	# but warn early since the user will need it next)
	if ! command -v podman &>/dev/null; then
		warn "podman not found -- needed by ltvm for container/image builds"
		warn "Install it: ${PKG_MGR} install podman"
	fi
}

# ── Cleanup on failure ──────────────────────────────

CLEANUP_MOUNTS=()
CLEANUP_FILES=()

cleanup() {
	local rc=$?
	for mnt in "${CLEANUP_MOUNTS[@]}"; do
		mountpoint -q "${mnt}" 2>/dev/null \
			&& umount -l "${mnt}" 2>/dev/null || true
	done
	for f in "${CLEANUP_FILES[@]}"; do
		[[ -f "${f}" ]] && rm -f "${f}" 2>/dev/null || true
	done
	if (( rc != 0 )); then
		warn "Setup failed (exit code ${rc})"
		warn "Re-run with the failing step to retry"
	fi
}
trap cleanup EXIT

# ── QEMU build ──────────────────────────────────────

install_qemu() {
	info "Installing QEMU ${QEMU_VERSION}"

	# Check if already installed at correct version
	if [[ -x "${QEMU_PREFIX}/bin/qemu-system-x86_64" ]]; then
		local existing
		existing=$("${QEMU_PREFIX}/bin/qemu-system-x86_64" \
			--version 2>/dev/null \
			| grep -oP 'version \K[0-9.]+' || echo "unknown")
		if [[ "${existing}" == "${QEMU_VERSION}" ]] \
				&& ! $FORCE; then
			info "QEMU ${existing} already installed (use --force to rebuild)"
			return 0
		fi
		info "QEMU ${existing} installed, rebuilding to ${QEMU_VERSION}"
	fi

	# Build dependencies
	info "Installing QEMU build dependencies..."
	if [[ "${PKG_MGR}" == "dnf" ]]; then
		dnf install -y epel-release 2>/dev/null || true
		dnf config-manager --set-enabled crb \
			2>/dev/null || true
		pkg_install gcc make glib2-devel pixman-devel \
			python3 python3-pip flex bison ninja-build
	elif [[ "${PKG_MGR}" == "apt" ]]; then
		apt-get update -qq
		pkg_install gcc make libglib2.0-dev \
			libpixman-1-dev python3 python3-pip \
			flex bison ninja-build pkg-config
	fi

	pip3 install tomli 2>/dev/null || true

	local tmpdir
	tmpdir=$(mktemp -d /tmp/qemu-build.XXXXXX)
	CLEANUP_FILES+=("${tmpdir}")

	info "Downloading QEMU ${QEMU_VERSION}..."
	if ! curl -fsSL \
		"https://download.qemu.org/qemu-${QEMU_VERSION}.tar.xz" \
		| tar xJ -C "${tmpdir}"; then
		die "Failed to download QEMU ${QEMU_VERSION}"
	fi

	cd "${tmpdir}/qemu-${QEMU_VERSION}"

	info "Configuring..."
	./configure --target-list=x86_64-softmmu \
		--disable-docs --disable-user --disable-gtk \
		--disable-sdl --disable-vnc --disable-spice \
		--disable-opengl --disable-xen --disable-curl \
		--disable-rbd --disable-libssh \
		--disable-capstone --disable-dbus-display \
		--prefix="${QEMU_PREFIX}"

	info "Building (this takes a few minutes)..."
	make -j"$(nproc)"
	make install

	cd /
	rm -rf "${tmpdir}"

	# Verify
	if ! "${QEMU_PREFIX}/bin/qemu-system-x86_64" \
			-machine help 2>/dev/null \
			| grep -q microvm; then
		die "QEMU built but microvm machine type not available"
	fi

	info "QEMU ${QEMU_VERSION} installed at ${QEMU_PREFIX}"
}

# ── Network bridge ──────────────────────────────────

setup_network() {
	info "Configuring network bridge (fcbr0) on ${BRIDGE_SUBNET}.0/24"

	# Install networking dependencies
	if [[ "${PKG_MGR}" == "dnf" ]]; then
		pkg_install dnsmasq iptables-nft
	elif [[ "${PKG_MGR}" == "apt" ]]; then
		pkg_install dnsmasq iptables
	fi

	# Verify critical dependencies are present
	for cmd in dnsmasq iptables; do
		if ! command -v "${cmd}" &>/dev/null; then
			die "${cmd} not found after install -- check package manager errors above"
		fi
	done

	# sysctl: enable IP forwarding
	cp -f "${SCRIPT_DIR}/qemu/host-config/99-qemu-vms.conf" \
		/etc/sysctl.d/
	sysctl -p /etc/sysctl.d/99-qemu-vms.conf >/dev/null

	# Generate bridge service with correct subnet
	sed "s/192\.168\.100/${BRIDGE_SUBNET}/g" \
		"${SCRIPT_DIR}/qemu/host-config/qemu-bridge.service" \
		>/etc/systemd/system/qemu-bridge.service

	# Generate dnsmasq config with correct subnet
	mkdir -p /etc/dnsmasq.d
	sed "s/192\.168\.100/${BRIDGE_SUBNET}/g" \
		"${SCRIPT_DIR}/qemu/host-config/qemu-dnsmasq.conf" \
		>/etc/dnsmasq.d/qemu-vms.conf

	# On Ubuntu, dnsmasq may conflict with
	# systemd-resolved on port 53.  Bind only to the
	# bridge interface (already in our config) and ensure
	# the main dnsmasq.conf has bind-interfaces.
	if [[ "${PKG_MGR}" == "apt" ]]; then
		if ! grep -q '^bind-interfaces' \
				/etc/dnsmasq.conf 2>/dev/null; then
			echo "bind-interfaces" >> /etc/dnsmasq.conf
		fi
	fi

	systemctl daemon-reload
	systemctl enable --now qemu-bridge
	systemctl restart dnsmasq

	# Verify
	if ! ip link show fcbr0 &>/dev/null; then
		die "fcbr0 bridge not created -- check qemu-bridge service"
	fi

	info "Bridge fcbr0 active at ${BRIDGE_SUBNET}.1/24"
}

# ── Install scripts ─────────────────────────────────

install_scripts() {
	info "Installing vm.sh and deploy-lustre.sh"

	mkdir -p "${VM_DIR}"/{overlays,sockets,kernel,images}

	for script in vm.sh deploy-lustre.sh; do
		local src="${SCRIPT_DIR}/qemu/${script}"
		if [[ ! -f "${src}" ]]; then
			warn "${script} not found at ${src}, skipping"
			continue
		fi
		cp -f "${src}" "${VM_DIR}/${script}"
		chmod +x "${VM_DIR}/${script}"
		ln -sf "${VM_DIR}/${script}" "/usr/local/bin/${script}"
	done

	# Install dk-filter (dk log filtering tool)
	if [[ -f "${SCRIPT_DIR}/qemu/dk-filter" ]]; then
		cp -f "${SCRIPT_DIR}/qemu/dk-filter" \
			/usr/local/bin/dk-filter
		chmod +x /usr/local/bin/dk-filter
	fi

	# Install pdsh + sshpass if available (needed for
	# multi-node clusters)
	if [[ "${PKG_MGR}" == "dnf" ]]; then
		pkg_install pdsh pdsh-rcmd-ssh sshpass
	elif [[ "${PKG_MGR}" == "apt" ]]; then
		pkg_install pdsh sshpass
	fi

	info "Installed to ${VM_DIR}"
}

# ── SSH config ──────────────────────────────────────

setup_ssh_config() {
	info "Configuring SSH for fast VM access"

	local ssh_dir="/root/.ssh"
	local ssh_config="${ssh_dir}/config"
	local marker="# lustre-test-vms"

	# Check if already configured with correct subnet
	if grep -q "${marker}" "${ssh_config}" 2>/dev/null; then
		if grep -q "Host ${BRIDGE_SUBNET}" \
				"${ssh_config}" 2>/dev/null; then
			info "SSH config already current"
			return 0
		fi
		# Subnet changed -- remove old block and re-add
		info "Updating SSH config for new subnet"
		sed -i "/${marker}/,/^$/d" "${ssh_config}"
	fi

	mkdir -p "${ssh_dir}"
	chmod 700 "${ssh_dir}"

	cat >>"${ssh_config}" <<EOF

${marker}
Host ${BRIDGE_SUBNET}.*
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
    ServerAliveInterval 1
    ServerAliveCountMax 2
    ConnectTimeout 5
    User root
EOF
	chmod 600 "${ssh_config}"
	info "SSH config updated for ${BRIDGE_SUBNET}.*"
}

# ── Verify ──────────────────────────────────────────

verify_setup() {
	info "Verifying setup..."
	local ok=true

	# QEMU
	if [[ -x "${QEMU_PREFIX}/bin/qemu-system-x86_64" ]]; then
		local ver
		ver=$("${QEMU_PREFIX}/bin/qemu-system-x86_64" \
			--version 2>/dev/null \
			| grep -oP 'version \K[0-9.]+' || echo "?")
		info "  QEMU: ${ver} at ${QEMU_PREFIX}"
	else
		warn "  QEMU: not installed"
		ok=false
	fi

	# KVM
	if [[ -e /dev/kvm ]]; then
		info "  KVM: available"
	else
		warn "  KVM: /dev/kvm not found"
		ok=false
	fi

	# Bridge
	if ip link show fcbr0 &>/dev/null; then
		local addr
		addr=$(ip -4 addr show fcbr0 \
			| grep -oP 'inet \K[0-9./]+' || echo "?")
		info "  Bridge: fcbr0 at ${addr}"
	else
		warn "  Bridge: fcbr0 not found"
		ok=false
	fi

	# dnsmasq
	if systemctl is-active dnsmasq &>/dev/null; then
		info "  dnsmasq: running"
	else
		warn "  dnsmasq: not running"
		ok=false
	fi

	# Scripts
	for script in vm.sh deploy-lustre.sh; do
		if command -v "${script}" &>/dev/null; then
			info "  ${script}: $(command -v "${script}")"
		else
			warn "  ${script}: not in PATH"
			ok=false
		fi
	done

	# podman
	if command -v podman &>/dev/null; then
		local pver
		pver=$(podman --version 2>/dev/null \
			| grep -oP '[0-9.]+' || echo "?")
		info "  podman: ${pver}"
	else
		warn "  podman: not installed (needed by ltvm)"
		ok=false
	fi

	# SSH config
	if grep -q "lustre-test-vms" \
			/root/.ssh/config 2>/dev/null; then
		info "  SSH config: configured"
	else
		warn "  SSH config: not configured"
		ok=false
	fi

	echo ""
	if $ok; then
		info "All checks passed"
	else
		warn "Some checks failed -- re-run setup for missing components"
	fi
	return 0
}

# ── Main ────────────────────────────────────────────

usage() {
	cat <<'EOF'
Usage: sudo ./setup.sh [OPTIONS]

Host setup for Lustre QEMU test VMs.

Steps (all run by default):
  --qemu        Build and install QEMU with microvm support
  --network     Configure fcbr0 bridge, dnsmasq, NAT
  --install     Install vm.sh, deploy-lustre.sh, dk-filter
  --ssh         Configure host SSH for fast VM access

Other:
  --verify      Check existing setup (no changes)
  --force       Force rebuild of already-installed components
  --subnet N    Set bridge subnet (default: 192.168.100)
                Also settable via BRIDGE_SUBNET env var

After setup, build artifacts with ltvm:
  ./ltvm init rocky9 --lustre-tree /path/to/lustre
EOF
	exit 0
}

main() {
	check_root
	detect_host_os

	local do_all=true
	local do_qemu=false
	local do_network=false
	local do_install=false
	local do_ssh=false
	local do_verify=false
	FORCE=false

	while [[ $# -gt 0 ]]; do
		case "$1" in
			--qemu)    do_qemu=true; do_all=false ;;
			--network) do_network=true; do_all=false ;;
			--install) do_install=true; do_all=false ;;
			--ssh)     do_ssh=true; do_all=false ;;
			--verify)  do_verify=true; do_all=false ;;
			--force)   FORCE=true ;;
			--subnet)
				BRIDGE_SUBNET="$2"; shift ;;
			--help|-h) usage ;;
			*)         die "Unknown option: $1" ;;
		esac
		shift
	done

	if $do_verify; then
		verify_setup
		return $?
	fi

	check_prerequisites

	# KVM is needed to actually run VMs but not to build
	# QEMU or install scripts.  Warn instead of failing
	# for individual steps; only hard-fail on full setup.
	if $do_all; then
		check_kvm
	elif ! [[ -e /dev/kvm ]]; then
		warn "/dev/kvm not found -- VMs won't run without KVM"
	fi

	if $do_all; then
		install_qemu
		setup_network
		install_scripts
		setup_ssh_config

		echo ""
		info "Host setup complete."
		info ""
		info "Next: build VM artifacts with ltvm:"
		info "  ./ltvm init rocky9 --lustre-tree /path/to/lustre"
		info ""
		info "Then create a VM:"
		info "  sudo vm.sh ensure co1-single \\"
		info "      --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3"
	else
		if $do_qemu;    then install_qemu;    fi
		if $do_network; then setup_network;   fi
		if $do_install; then install_scripts; fi
		if $do_ssh;     then setup_ssh_config; fi
	fi
}

main "$@"
