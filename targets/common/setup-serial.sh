#!/usr/bin/env bash
# Configure serial console for automatic root login.
# Required for QEMU microvm direct kernel boot (no display, no VNC).
#
# x86_64 microvm uses ttyS0 (8250 UART).
# aarch64 virt machine uses ttyAMA0 (PL011 UART).
# We configure both so the same image works on either arch.
set -euo pipefail

for tty in ttyS0 ttyAMA0; do
    mkdir -p "/etc/systemd/system/serial-getty@${tty}.service.d"
    cat > "/etc/systemd/system/serial-getty@${tty}.service.d/autologin.conf" <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root -o '-p -f root' --keep-baud 115200,38400,9600 %I $TERM
EOF
done
