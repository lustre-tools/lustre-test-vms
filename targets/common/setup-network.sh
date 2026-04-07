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

# resolv.conf: written at boot by rc.local (reads fc_ip/fc_gw from
# kernel cmdline). No need to set it here -- during container builds
# /etc/resolv.conf is a bind mount that can't be replaced anyway.

# Disable NetworkManager auto-config (rc.local handles it)
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/00-ltvm.conf <<'EOF'
[main]
no-auto-default=*
EOF
