#!/usr/bin/env bash
# Configure kdump for near-complete crash dumps.
#
# Writes dumps to /var/crash. Use `ltvm crash-collect` to retrieve.
# crashkernel=... must be in QEMU kernel cmdline (set by qemu_run.py).
#
# Handles both RHEL (kdump service, /etc/kdump.conf + /etc/sysconfig/kdump)
# and Debian (kdump-tools, /etc/default/kdump-tools).
set -euo pipefail

mkdir -p /var/crash

# Panic on unknown NMI so that an injected NMI triggers kdump.
mkdir -p /etc/sysctl.d
echo 'kernel.unknown_nmi_panic = 1' > /etc/sysctl.d/90-ltvm-nmi.conf

if [[ -f /etc/os-release ]] && grep -qi 'debian\|ubuntu' /etc/os-release; then
	# Debian/Ubuntu: kdump-tools
	cat > /etc/default/kdump-tools <<'EOF'
USE_KDUMP=1
KDUMP_SYSCTL="kernel.panic_on_oops=1"
KDUMP_COREDIR="/var/crash"
EOF
	systemctl enable kdump-tools 2>/dev/null || true
else
	# RHEL/Rocky: kexec-tools kdump service
	cat > /etc/kdump.conf <<'EOF'
path /var/crash
core_collector makedumpfile -l --message-level 7 -d 1
EOF

	mkdir -p /etc/sysconfig
	cat > /etc/sysconfig/kdump <<'EOF'
KDUMP_KERNELVER=""
KDUMP_COMMANDLINE_REMOVE="hugepages hugepagesz slub_debug quiet log_buf_len swiotlb"
KDUMP_COMMANDLINE_APPEND="irqpoll nr_cpus=1 reset_devices cgroup_disable=memory mce=off numa=off udev.children-max=2 panic=10 rootflags=nofail acpi_no_memhotplug transparent_hugepage=never nokaslr"
KEXEC_ARGS=""
KDUMP_IMG=vmlinuz
EOF
	systemctl enable kdump
fi
