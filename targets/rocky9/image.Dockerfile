FROM rockylinux:9

# Rocky 9 VM base image.
# Built via podman, exported to raw ext4 for QEMU microvm use.
# No kernel installed -- QEMU passes it externally via -kernel.

# Enable EPEL and CRB repos
RUN dnf -y install epel-release dnf-plugins-core \
    && dnf config-manager --set-enabled crb \
    && dnf clean all

# Copy package lists
COPY common/packages-base.txt /tmp/packages-base.txt
COPY common/packages-test.txt /tmp/packages-test.txt
COPY common/packages-debug.txt /tmp/packages-debug.txt
COPY common/packages-server.txt /tmp/packages-server.txt
COPY rocky9/packages-os.txt /tmp/packages-os.txt

# Parse package lists (strip comments and blank lines) and install.
# kernel-devel is excluded -- Lustre builds on the host, not in VM.
RUN cat /tmp/packages-base.txt \
        /tmp/packages-test.txt \
        /tmp/packages-debug.txt \
        /tmp/packages-server.txt \
        /tmp/packages-os.txt \
    | grep -v '^\s*#' | grep -v '^\s*$' \
    | grep -v '^kernel-devel$' \
    | sort -u \
    | xargs dnf -y --allowerasing install \
    && dnf clean all

# Source-built tools: IOR, mdtest, iozone, pjdfstest, FlameGraph
RUN dnf -y --allowerasing install gcc gcc-c++ make autoconf automake libtool git curl \
    && export PATH=/usr/lib64/openmpi/bin:$PATH \
    && cd /tmp \
    && curl -sL https://github.com/hpc/ior/releases/download/4.0.0/ior-4.0.0.tar.gz | tar xz \
    && cd ior-4.0.0 && ./configure && make -j$(nproc) \
    && cp src/ior src/mdtest /usr/local/bin/ \
    && cd /tmp && rm -rf ior-4.0.0 \
    && curl -sL http://www.iozone.org/src/current/iozone3_506.tar | tar xf - \
    && cd iozone3_506/src/current && make -j$(nproc) linux-AMD64 \
    && cp iozone /usr/local/bin/ \
    && cd /tmp && rm -rf iozone3_506 \
    && git clone https://github.com/pjd/pjdfstest.git \
    && cd pjdfstest && autoreconf -ifs && ./configure \
    && make -j$(nproc) && cp pjdfstest /usr/local/bin/ \
    && cd /tmp && rm -rf pjdfstest \
    && git clone --depth 1 https://github.com/brendangregg/FlameGraph.git /usr/local/FlameGraph \
    && for f in flamegraph.pl stackcollapse-perf.pl stackcollapse.pl difffolded.pl; do \
         ln -sf /usr/local/FlameGraph/$f /usr/local/bin/$f; \
       done \
    && dnf clean all

# Install drgn
RUN pip3 install drgn && rm -rf /root/.cache/pip

# Install Lustre-patched e2fsprogs
RUN cd /tmp \
    && git clone https://review.whamcloud.com/tools/e2fsprogs \
    && cd e2fsprogs && git checkout v1.47.3-wc2 \
    && ./configure --enable-elf-shlibs \
    && make -j$(nproc) && make install && make install-libs \
    && ldconfig \
    && cd /tmp && rm -rf e2fsprogs

# ── System configuration ──

# Enable sshd, allow passwordless root login
RUN systemctl enable sshd \
    && echo "PermitRootLogin yes" >> /etc/ssh/sshd_config \
    && echo "PermitEmptyPasswords yes" >> /etc/ssh/sshd_config

# Set empty root password
RUN passwd -d root

# Generate SSH host keys (so they persist across boots)
RUN ssh-keygen -A

# Shared inter-VM SSH key
RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh \
    && ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N "" -q \
    && cp /root/.ssh/id_ed25519.pub /root/.ssh/authorized_keys \
    && chmod 600 /root/.ssh/authorized_keys
COPY <<'EOF' /root/.ssh/config
Host *
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
EOF
RUN chmod 600 /root/.ssh/config

# Serial console auto-login
COPY <<'EOF' /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root -o '-p -f root' --keep-baud 115200,38400,9600 %I $TERM
EOF

# Networking init: read IP/GW/hostname from kernel cmdline
RUN mkdir -p /etc/rc.d
COPY common/rc.local /etc/rc.d/rc.local
RUN chmod +x /etc/rc.d/rc.local \
    && ln -sf /etc/rc.d/rc.local /etc/rc.local

# fstab -- root on /dev/vda
COPY <<'EOF' /etc/fstab
/dev/vda  /  ext4  defaults,noatime  0 1
EOF

# resolv.conf -- point at host bridge DNS
COPY <<'EOF' /etc/resolv.conf
nameserver 192.168.100.1
nameserver 8.8.8.8
EOF

# Disable NetworkManager auto-config (we use rc.local)
COPY <<'EOF' /etc/NetworkManager/conf.d/00-fc.conf
[main]
no-auto-default=*
EOF

# kdump configuration -- near-complete dumps
RUN mkdir -p /var/crash
COPY <<'EOF' /etc/kdump.conf
path /var/crash
core_collector makedumpfile -l --message-level 7 -d 1
EOF
COPY <<'EOF' /etc/sysconfig/kdump
KDUMP_KERNELVER=""
KDUMP_COMMANDLINE_REMOVE="hugepages hugepagesz slub_debug quiet log_buf_len swiotlb"
KDUMP_COMMANDLINE_APPEND="irqpoll nr_cpus=1 reset_devices cgroup_disable=memory mce=off numa=off udev.children-max=2 panic=10 rootflags=nofail acpi_no_memhotplug transparent_hugepage=never nokaslr"
KEXEC_ARGS=""
KDUMP_IMG=vmlinuz
EOF
RUN systemctl enable kdump

# Disable slow/unnecessary services
RUN systemctl mask systemd-hwdb-update firewalld dnf-makecache.timer 2>/dev/null || true

# Blacklist drm
COPY <<'EOF' /etc/modprobe.d/no-drm.conf
blacklist drm
EOF

# Clean up caches
RUN dnf clean all && rm -rf /var/cache/dnf /tmp/*
