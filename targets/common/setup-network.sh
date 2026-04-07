#!/usr/bin/env bash
# Configure networking for QEMU microvm guests.
#
# VMs receive their IP, gateway, and hostname via kernel cmdline
# (parsed by rc.local). NetworkManager is disabled so it doesn't
# fight with the cmdline-assigned addresses.
#
# Expects common/rc.local to already be present at /etc/rc.d/rc.local
# (COPY'd by the Dockerfile before this script runs).
set -euo pipefail

# rc.local: reads IP/GW/hostname from kernel cmdline at boot
mkdir -p /etc/rc.d
chmod +x /etc/rc.d/rc.local
ln -sf /etc/rc.d/rc.local /etc/rc.local

# fstab: root on /dev/vda (virtio block device)
cat > /etc/fstab <<'EOF'
/dev/vda  /  ext4  defaults,noatime  0 1
EOF

# resolv.conf: host bridge acts as DNS (dnsmasq on 192.168.100.1)
# Remove symlink first if it exists (Ubuntu uses systemd-resolved).
# In rootless podman, /etc/resolv.conf is bind-mounted -- truncate instead.
rm -f /etc/resolv.conf 2>/dev/null || truncate -s 0 /etc/resolv.conf
cat > /etc/resolv.conf <<'EOF'
nameserver 192.168.100.1
nameserver 8.8.8.8
EOF

# Disable NetworkManager auto-config (rc.local handles it)
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/00-ltvm.conf <<'EOF'
[main]
no-auto-default=*
EOF
