#!/usr/bin/env bash
# Configure SSH for passwordless root access and inter-VM connectivity.
#
# - Enables sshd
# - Allows root login with empty password
# - Generates persistent SSH host keys
# - Creates a shared ed25519 key so VMs can SSH to each other without prompts
set -euo pipefail

# Enable sshd (service name differs: sshd on RHEL, ssh on Debian)
if systemctl list-unit-files sshd.service &>/dev/null; then
	systemctl enable sshd
else
	systemctl enable ssh
fi

# Allow root login with empty password, and raise the connection
# concurrency limits for the Lustre test framework.
#
# MaxStartups: the framework fans out over pdsh and opens a burst of
# short-lived connections on nearly every helper -- do_nodes,
# load_modules_remote, per-facet stop/start, the client-load drivers.
# sshd's default (10:30:100) starts randomly refusing unauthenticated
# connections past 10 in flight, which surfaces mid-run as
# "ssh_exchange_identification: Connection closed by remote host"
# rather than as a clean test failure.  100:30:200 keeps the same
# shape (drop probability ramps from 30% at the low mark to 100% at
# the high mark) with headroom for a fan-out across every node.
#
# MaxSessions: pdsh opens one connection per node, but the framework
# also multiplexes several exec channels over a single connection
# when ControlMaster is in play.  The default of 10 is low enough to
# stall a wide do_nodes; 100 costs nothing on an idle VM.
SSHD_LIMITS='MaxStartups 100:30:200
MaxSessions 100'

# Use sshd_config.d if available (Ubuntu 24.04+), else append to main config
if [[ -d /etc/ssh/sshd_config.d ]]; then
	cat > /etc/ssh/sshd_config.d/99-ltvm.conf <<SSHEOF
PermitRootLogin yes
PermitEmptyPasswords yes
${SSHD_LIMITS}
SSHEOF
else
	echo "PermitRootLogin yes"      >> /etc/ssh/sshd_config
	echo "PermitEmptyPasswords yes" >> /etc/ssh/sshd_config
	echo "${SSHD_LIMITS}"           >> /etc/ssh/sshd_config
fi

# Clear root password
passwd -d root

# Generate host keys so they persist across boots
ssh-keygen -A

# Shared inter-VM key: all VMs share the same ed25519 keypair so any
# VM can SSH to any other without needing to exchange keys at runtime.
mkdir -p /root/.ssh
chmod 700 /root/.ssh
ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N "" -q
cp /root/.ssh/id_ed25519.pub /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

cat > /root/.ssh/config <<'SSHCFG'
Host *
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
SSHCFG
chmod 600 /root/.ssh/config

# Users required by the Lustre test framework
groupadd -g 500 runas 2>/dev/null || true
useradd -u 500 -g 500 -m -s /bin/bash runas 2>/dev/null || true
useradd -m -s /bin/bash sanityusr  2>/dev/null || true
useradd -m -s /bin/bash sanityusr1 2>/dev/null || true
useradd -m -s /bin/bash quota_usr  2>/dev/null || true
useradd -m -s /bin/bash quota_2usr 2>/dev/null || true
useradd -m -s /bin/bash mpiuser    2>/dev/null || true
