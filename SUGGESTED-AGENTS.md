# Suggested Agent Instructions for ltvm-based Lustre Development

Copy/adapt these sections into your project's CLAUDE.md
or AGENTS.md. Replace paths like `~/lustre-release` and
`~/lustre-test-vms-v2` with your actual locations.

## Building

### Quick Start (download pre-built artifacts)

```bash
cd ~/lustre-test-vms-v2

# Download pre-built kernel + image + Lustre (~1.4 GB)
./ltvm fetch rocky9 --url <tarball-url>

# Install kernel + image to system paths
./ltvm install rocky9

# Create a VM and deploy the packaged Lustre
sudo vm.py create --name co1-single \
    --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
./ltvm deploy co1-single --mount
```

No building required. Four commands from download to
mounted Lustre filesystem.

### Building from Scratch

```bash
cd ~/lustre-test-vms-v2

# Build container + kernel + image (~45 min first time)
./ltvm build-all rocky9

# Install kernel + image to system paths
./ltvm install rocky9

# Check what's built
./ltvm status
```

### Kernel Build

ltvm builds kernels using Lustre's authoritative configs
from `lustre/kernel_patches/`. Each target supports
multiple kernel versions.

```bash
# Build default kernel for rocky9
./ltvm build-kernel rocky9

# Build a specific kernel version
./ltvm build-kernel rocky9 --kernel 5.14-rhel9.5

# Check available kernels
./ltvm status
```

Output lives at
`<ltvm-repo>/output/<target>/kernels/<kernel-name>/`.
Each kernel directory contains: vmlinux, vmlinuz,
modules/, and build-tree/ (kernel source for Lustre
module compilation, no object files).

### Lustre Build

`ltvm build-lustre` compiles Lustre inside the target's
build container (cross-OS capable). The container has
the correct GCC and Whamcloud-patched e2fsprogs.

```bash
cd ~/lustre-test-vms-v2

# Build Lustre against the default kernel
./ltvm build-lustre rocky9 \
    --lustre-tree ~/lustre-release

# Build against a specific kernel
./ltvm build-lustre rocky9 \
    --kernel 5.14-rhel9.5 \
    --lustre-tree ~/lustre-release

# Force clean rebuild
./ltvm build-lustre rocky9 --force \
    --lustre-tree ~/lustre-release
```

Incremental builds skip configure and go straight to
make. The container is retained by podman; ccache is
persisted in a named volume across runs.

### Packaging (for distribution)

```bash
# Package kernel + image + built Lustre
./ltvm package rocky9 \
    --lustre-tree ~/lustre-release

# Package a specific kernel version
./ltvm package rocky9 --kernel 5.14-rhel9.5 \
    --lustre-tree ~/lustre-release
```

Produces a `.tar.zst` (~1.4 GB) containing everything
needed to boot VMs and deploy Lustre.

### Deploy Workflow (QEMU microVMs)

QEMU microVMs provide persistent test environments.
Uses KVM acceleration with virtio-mmio devices.
Supports kdump for crash analysis. VMs survive
stop/start -- overlays and disks persist until
explicitly destroyed.

`ltvm` handles builds, packaging, and deployment.
VM lifecycle uses `vm.py` (Python, despite the name)
which ltvm wraps for deploy operations.

#### Single-node VMs

```bash
# Create a VM with Lustre backing disks
sudo vm.py create --name co1-single \
    --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3

# Idempotent: create if missing, start if stopped
sudo vm.py ensure co1-single \
    --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3

# List / status / stop / start / destroy
sudo vm.py list
sudo vm.py status co1-single
sudo vm.py stop co1-single
sudo vm.py start co1-single
sudo vm.py destroy co1-single
```

#### Execution and file transfer

```bash
# Interactive SSH (passwordless by name)
ssh co1-single

# Non-interactive exec with timeout + exit codes
# Exit codes: 0=ok, 1=error, 2=not-found,
#             3=timeout, 4=unreachable
sudo vm.py exec --timeout 30 co1-single 'lctl dl'

# Copy files to/from VM
sudo vm.py cp-to co1-single local.txt /tmp/
sudo vm.py cp-from co1-single /tmp/out.txt .

# Kernel log
sudo vm.py dmesg --tail 100 co1-single
```

#### Deploy and Mount

`ltvm deploy` is idempotent -- safe to redeploy over
existing mounts (unmounts, unloads, cleans dm devices
first). Auto-detects packaged Lustre and kernel
modules from the ltvm output directory.

```bash
# Deploy packaged Lustre (from fetch or build)
./ltvm deploy co1-single --mount

# Deploy from a specific Lustre build tree
./ltvm deploy co1-single \
    --build ~/lustre-release --mount

# Deploy using a specific kernel's modules
./ltvm deploy co1-single \
    --kernel 5.14-rhel9.5 \
    --build ~/lustre-release --mount
```

#### Multi-node Clusters

Clusters create multiple VMs with Lustre role
assignments. Node specs: `roles:vmname[:disks]`.
Roles: `mgs`, `mds`, `oss`, `client` (join with `+`).

```bash
# Combined MGS/MDS + OSS
sudo vm.py cluster create co2 \
    mgs+mds:co2-mds:1 oss:co2-oss:3

# Deploy + mount
sudo vm.py cluster deploy co2 \
    --build ~/lustre-release --mount

# Cluster info / exec / destroy
sudo vm.py cluster status co2
sudo vm.py cluster exec co2 oss 'lctl dl'
sudo vm.py cluster destroy co2
```

**Cluster deploy details:** rsyncs the full build
tree to the same absolute path on all VMs. Installs
modules via depmod, libraries via ldconfig, key
binaries to /usr/sbin. Generates cfg/local.sh with
correct host, device, NID, and PDSH configuration
for the test framework's multi-node support.

#### VM Naming Convention

VM names MUST include the checkout number to avoid
conflicts between parallel sessions.
Format: `co<N>-<role>`.

Never use bare names like `testvm` or `myvm` --
these collide when multiple checkouts are in use.

#### VM Networking and Discovery

- VMs resolve each other by name via dnsmasq on
  the host bridge (`192.168.100.1`)
- `/etc/hosts` entries added on create, removed on
  destroy; dnsmasq SIGHUPed automatically
- Shared inter-VM SSH key baked into base image --
  `ssh othervmname` works from inside any VM
- Host SSH config: `ServerAliveInterval=1`,
  `ServerAliveCountMax=2`, `ConnectTimeout=5` --
  dead VMs detected in seconds

#### VM Internals

- QEMU binary: `/opt/qemu/bin/qemu-system-x86_64`
- Base image: `<ltvm-repo>/output/<target>/image/base.ext4`
- Kernel: `<ltvm-repo>/output/<target>/kernels/<name>/vmlinux`
- Kernel build tree:
  `<ltvm-repo>/output/<target>/kernels/<name>/build-tree/`
- qcow2 overlays in `/opt/qemu-vms/overlays/`
- TAP devices on `fcbr0` bridge, `192.168.100.0/24`
- IPs derived from VM name (md5 hash -> last octet)
- After host reboot: `sudo vm.py start-all`
  (no auto-start)
- Requires root/sudo for TAP and QEMU operations

#### Base Image Tools

Pre-installed in the base image:

- **Profiling:** perf, bcc-tools, bpftrace, trace-cmd,
  systemtap, valgrind, sysstat, kernel-tools
- **I/O:** fio, blktrace, ioprofile, iotop
- **Benchmarks:** IOR, mdtest, iozone, dbench, bonnie++,
  pjdfstest
- **MPI:** openmpi, openmpi-devel
- **Tracing:** strace, ltrace
- **Flamegraphs:** `/usr/local/FlameGraph/`
- **Dev:** vim, git, gcc, gdb
- **Test deps:** attr, acl, bc, rsync, perl, pdsh,
  pdsh-rcmd-ssh, nfs-utils, sg3_utils

## Day-to-Day Iteration

```bash
# Edit Lustre code, then:
./ltvm build-lustre rocky9            # incremental, fast
./ltvm deploy co1-single --mount      # redeploy

# Image recipe iteration:
# Edit targets/rocky9/image.Dockerfile or
# targets/common/packages-*.txt
./ltvm build-image rocky9
./ltvm install rocky9
sudo vm.py destroy co1-single
sudo vm.py create --name co1-single \
    --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
./ltvm deploy co1-single --mount
```

## Testing

### Test Environment

Testing is done inside QEMU microVMs. Building is done
on the host (or in containers via ltvm); loading/
unloading Lustre and running tests happens inside the VM.

Single-node (checkout 1):
```bash
sudo vm.py ensure co1-single \
    --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
./ltvm deploy co1-single \
    --build ~/lustre-release --mount
```

Multi-node cluster (checkout 2):
```bash
sudo vm.py cluster create co2 \
    mgs+mds:co2-mds:1 oss:co2-oss:3
sudo vm.py cluster deploy co2 \
    --build ~/lustre-release --mount
```

### Loading and Unloading Lustre

**Inside the VM** (client mounts at `/mnt/lustre`):
```bash
sudo bash lustre/tests/llmount.sh
sudo bash lustre/tests/llmountcleanup.sh && \
    sudo lustre_rmmod
```

If `llmount.sh` mkfs fails, try
`sudo dmsetup remove_all` first.

**Destroy/recreate is often faster than cleanup.**
`llmountcleanup.sh` can be slow or hang. If you
don't need to preserve logs or other VM state:
```bash
sudo vm.py destroy co1-single
sudo vm.py ensure co1-single \
    --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
./ltvm deploy co1-single \
    --build ~/lustre-release --mount
```

### Running Tests

**Inside the VM:**
```bash
sudo -E ONLY=42a bash \
    lustre/tests/sanity.sh
```
**Auster:** `sudo -E auster -s sanity --only 42a`

Auster logs: `/tmp/test_logs/YYYY-MM-DD/HHMMSS/`

### Testing Notes

- When running something that might hang, use
  `timeout <seconds> <command>` or run in the
  background with a timeout.
- If a test VM is stuck (hung mount, blocked cleanup),
  just destroy and recreate it -- VMs are ephemeral.
- Print output directly / save to bash variables
  rather than redirecting to temp files.
- Use stack_trap to reset configuration after test.

## Debugging

### Debug Log Quick Reference

Lustre uses an in-kernel ring buffer (per-CPU,
default ~5 MB/CPU). All CDEBUG/CERROR output goes
here.

**Basic capture workflow:**
```bash
lctl set_param debug=-1
lctl set_param debug_mb=10000
lctl clear
lctl mark "before repro"
# ... reproduce ...
lctl dk /tmp/dk.log
```

Set debug_mb extremely large -- kernel clamps to max.

**From the host:**
```bash
sudo vm.py exec --timeout 30 co1-single \
    'lctl set_param debug=-1 && lctl clear'
# ... reproduce ...
sudo vm.py exec --timeout 30 co1-single \
    'lctl dk /tmp/dk.log'
sudo vm.py cp-from co1-single /tmp/dk.log .
```

### kdump / Crash Analysis

VMs boot with `crashkernel=512M`. The base image has
kexec-tools, crash, drgn, and vmlinux with debug
symbols. After a kernel panic, kdump saves a vmcore
to `/var/crash/` and reboots.

**Triggering a crash:**
```bash
sudo vm.py exec --timeout 30 co1-single \
    'echo c > /proc/sysrq-trigger'
```
VM reboots after kdump completes (~15s).

**Collecting and analyzing:**
```bash
sudo vm.py cp-from co1-single \
    /var/crash/latest/vmcore /tmp/

crash-tool recipes lustre \
    --vmcore /tmp/vmcore \
    --vmlinux <ltvm>/output/rocky9/kernels/5.14-rhel9.7/vmlinux \
    --mod-dir ~/lustre-release
```

**Use drgn, not crash.** drgn provides typed Lustre
struct traversal, local variable extraction, dk log,
kernel log, LDLM lock enumeration, OBD device listing,
and structured JSON output.

**Key notes:**
- `--mod-dir <build-tree>` loads Lustre module debug
  symbols. Build tree .ko files have debug_info.
- drgn runs from the host -- copy vmcore out with
  `vm.py cp-from`.
- vmlinux is at
  `<ltvm>/output/<target>/kernels/<name>/vmlinux`

**Parallel testing:** use separate numbered checkouts
and separate VMs (or separate clusters).

## ltvm Command Reference

```
ltvm build-all <target>         Build container + kernel + image
ltvm build-container <target>   Build the build container
ltvm build-kernel <target>      Build a kernel (--kernel <ver>)
ltvm build-image <target>       Build the VM base image
ltvm build-lustre [target]      Build Lustre in container (--kernel <ver>)

ltvm package <target>           Create distributable tarball
ltvm fetch <target> --url URL   Download pre-built package
ltvm install <target>           Install kernel + image to system paths

ltvm deploy <vm>                Deploy Lustre to a VM
ltvm exec <vm> <cmd>            Execute command in a VM
ltvm vm <action> [args]         VM lifecycle (create/destroy/etc.)
ltvm cluster <action> [args]    Cluster management

ltvm status                     Show build status of all targets
ltvm shell <target>             Enter build container interactively
ltvm setup                      Set up host (QEMU, network, SSH)
```

Global flags: `--json`, `--verbose`, `--kernel <ver>`
(on commands that operate on a specific kernel).
