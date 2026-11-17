# Enabling Nested Virtualization

ltvm runs QEMU/KVM virtual machines. If your host is itself a VM
(cloud instance, Hyper-V, VMware, etc.), you need **nested
virtualization** enabled so the inner VMs can use hardware
acceleration.

Without KVM, QEMU falls back to software emulation which is
~100x slower and not supported by ltvm.

## Quick Diagnosis

```bash
ls /dev/kvm          # should exist
lscpu | grep -i virt # should show VT-x or AMD-V
```

If `/dev/kvm` is missing, follow the instructions for your
platform below.

---

## Linux on Bare Metal

KVM should work out of the box. If `/dev/kvm` is missing:

```bash
# Check BIOS/UEFI settings
grep -E 'vmx|svm' /proc/cpuinfo

# Load the KVM module
sudo modprobe kvm_intel   # Intel
sudo modprobe kvm_amd     # AMD

# If modprobe fails, enable VT-x/AMD-V in BIOS
```

## WSL2 (Windows)

From an **elevated PowerShell** on the Windows host:

```powershell
# Find your WSL VM
wsl -l -v

# Enable nested virt (requires Hyper-V)
Set-VMProcessor -VMName "WSL" -ExposeVirtualizationExtensions $true

# Restart WSL
wsl --shutdown
```

Then verify inside WSL: `ls /dev/kvm`

**Note:** WSL2 nested virt requires Windows 11 or Windows 10
21H2+ with Hyper-V enabled. Older Windows versions do not
support this.

## VMware (ESXi / Workstation / Fusion)

### ESXi (vSphere)

Edit the VM settings:
1. VM > Edit Settings > CPU
2. Check "Expose hardware-assisted virtualization to the guest OS"
3. Power cycle the VM (not just reboot)

Or via CLI:
```
vhv.enable = "TRUE"
```

### VMware Workstation / Fusion

VM Settings > Processors > "Virtualize Intel VT-x/EPT or AMD-V/RVI"

## KVM (libvirt / virsh)

Nested virt must be enabled in the host's KVM module:

```bash
# Check current setting on the HOST
cat /sys/module/kvm_intel/parameters/nested    # Intel
cat /sys/module/kvm_amd/parameters/nested      # AMD

# Enable (temporary)
sudo modprobe -r kvm_intel
sudo modprobe kvm_intel nested=1

# Enable (permanent)
echo 'options kvm_intel nested=1' | sudo tee /etc/modprobe.d/kvm-nested.conf
```

Then ensure the guest VM's XML has:
```xml
<cpu mode='host-passthrough'/>
```

Or via virsh:
```bash
virsh edit <vm-name>
# Change <cpu> to: <cpu mode='host-passthrough'/>
# Then: virsh destroy <vm> && virsh start <vm>
```

## Hyper-V (Azure / on-premises)

### Azure VMs

Use a **v5 or newer** VM size that supports nested virt:
- Dv5, Dsv5, Ev5, etc.
- **Not supported:** Dv2, Dv3, Dv4, Bv1, Fsv2

Check: `az vm list-skus --location <region> --query "[?capabilities[?name=='HyperVGenerations' && value=='V2']]"`

Nested virt is automatic on supported sizes -- no config needed.

### On-premises Hyper-V

From **elevated PowerShell** on the Hyper-V host:

```powershell
Set-VMProcessor -VMName <name> -ExposeVirtualizationExtensions $true
# Power cycle the VM
```

## AWS EC2

Use a **metal** instance type (e.g. `m5.metal`, `c5.metal`).
Regular EC2 instances do not support nested KVM.

Alternatively, `.metal` instances have direct hardware access
and KVM works natively (not technically "nested").

## Google Cloud (GCE)

Nested virt is supported on Haswell+ CPUs. Enable it when
creating the VM:

```bash
gcloud compute instances create <name> \
    --enable-nested-virtualization \
    --min-cpu-platform="Intel Haswell"
```

Or for an existing instance (must be stopped):
```bash
gcloud compute instances update <name> \
    --enable-nested-virtualization
```

## macOS (Apple Silicon / Intel)

### Apple Silicon -- aarch64

#### Expose KVM in a VM created by ltvm

ltvm exposes Arm EL2 to native aarch64 guests by default on compatible
Apple Silicon (M3 or later) with macOS 15+ and QEMU 11.1+. A Linux guest
kernel with KVM enabled then provides `/dev/kvm`. `./ltvm install`
supplies a signed QEMU bundle for Apple Silicon.

```bash
ltvm create nested-host --target rocky10 --arch aarch64 \
    --mem 6144 --vcpus 4 --root-size 16G --mdt-disks 0 --ost-disks 0
ltvm vm exec nested-host -- ls -l /dev/kvm
ltvm vm exec nested-host -- dmesg | grep -E 'started at EL2|kvm \['
```

ltvm checks support at each cold boot, including for existing VMs.
On an older host or QEMU, the VM boots with ordinary HVF. To opt out,
pass `--disable-nesting` to `ltvm create` or `ltvm cluster create`.
ltvm saves the opt-out for each VM. QEMU suppresses Arm SME when it
exposes EL2 through HVF, so opt out for workloads that need SME.

The example allocates space for inner VMs and their images.
For an inner QEMU, use
`-accel kvm -cpu host -machine virt,gic-version=max,its=off`.

#### Run ltvm inside a Linux VM on the Mac

Use the Linux guest above, or use UTM to create the outer VM. For the
UTM path, use:

- an **M3 or later** chip (M1 and M2 have no nested virtualization);
- **macOS 15** or later;
- **UTM 4.6** or later, with the VM on the **Apple Virtualization**
  backend (not QEMU) and its nested virtualization option turned on;
- an **aarch64** Linux guest, running **aarch64** ltvm targets.  An
  x86_64 target can only be emulated (TCG) on Apple Silicon.

Check from inside the Linux VM:

```bash
sudo dmesg | grep -e 'started at EL2' -e 'kvm \['   # "All CPU(s) started at EL2"
ls -l /dev/kvm                                      # present
sudo usermod -aG kvm $USER                          # if you are not in the kvm group
```

`ltvm doctor` then reports KVM as available, and `ltvm create` uses
it: `accel=kvm` and `-cpu host`.  `ps -o args -C qemu-system-aarch64`
shows what a running VM got.

**ltvm turns off the GIC ITS for the inner VM.** On an Apple CPU (`CPU
implementer : 0x61` in `/proc/cpuinfo`), ltvm launches KVM guests with
`virt,...,its=off`. This avoids a hang observed under UTM's Apple
Virtualization backend. With the ITS, virtio-pci MSIs lose wakeups and
the guest sits in `cpu_do_idle` while its vCPU thread shows 100% CPU.
Without the ITS, virtio falls back to wired INTx interrupts.
Trapping WFI in the L1 KVM does not help.
`LTVM_FORCE_TCG=1` still forces emulation, if you ever need it.

Parallels Desktop and VMware Fusion have not been tested with ltvm.
If the Mac cannot nest, run ltvm on macOS directly, or on a remote
x86_64 or aarch64 Linux machine.

#### Benchmarks

An M3 Pro (5 performance and 6 efficiency cores, 36 GB), macOS 26.5,
UTM 4.7.5 with an Ubuntu 26.04 VM (kernel 7.0, QEMU 10.2.1, 5 vCPUs,
8 GB).  Guests: rocky9 aarch64 with the 5.14-rhel9.7 kernel, 2 vCPUs.
Measured on 2026-10-19 and 2026-10-21; a range spans both days.

**Nested KVM against emulation**, both in the UTM VM:

| | TCG | nested KVM |
|---|---|---|
| boot to root prompt | 21-25 s | 8.3-9 s |
| openssl sha256, 1 process | 0.17 GB/s | 2.69-2.78 GB/s |
| 2000 fork+exec | 20-23 s | 0.5-0.6 s |
| pipe between processes, two vCPUs | 8.5 s | 0.39-0.47 s |
| `xz -3` of 64 MiB | 62-70 s | 20-22 s |
| `find /usr` | 7.4-12 s | 2.0-2.9 s |
| 4 KiB direct reads x 20000 | 7.0-7.9 s | 4.8-5.1 s |

**The UTM VM against ltvm on the Mac itself**, the same image:

| | nested KVM | HVF | |
|---|---|---|---|
| boot to root prompt | 8.3-8.8 s | 10.4-11 s | |
| openssl sha256, 1 / 2 processes | 2.69-2.78 / 2.57-2.68 GB/s | 2.90-2.98 / 2.60-2.83 GB/s | ~1x |
| pipe between processes, one vCPU | 0.31 s | 0.28-0.29 s | ~1x |
| pipe between processes, two vCPUs | 0.39-0.47 s | 0.20 s | 2-2.3x |
| 2000 fork+exec | 0.5-0.8 s | 0.39-0.5 s | 1.1-2x |
| `xz -3` of 64 MiB | 20-22 s | 15-16 s | 1.3x |
| `find /usr` | 2.0-2.9 s | 1.5-1.6 s | 1.5-1.9x |
| 256 MiB direct write / read | 1-1.9 / 1-1.7 s | 0.7-0.8 / 0.5 s | ~2x |
| 4 KiB direct reads x 20000 | 4.8-5.1 s | 2.5-2.8 s | 1.9x |

- **Compute and syscalls cost nothing extra.**  Code that does not
  leave the guest runs at native speed at any nesting depth.
- **Exits cost about 2x.**  A cross-vCPU wakeup (an IPI and a WFI), an
  interrupt or an I/O request exits from the guest to Apple's
  hypervisor, which hands it to the L1's KVM.  The L1's KVM runs in
  nVHE mode (Apple's virtual EL2 offers no VHE), whose world switch is
  the expensive kind, and `its=off` adds the exits that wired
  interrupts take to deassert.  ltvm can change neither.
- **Memory-heavy work loses ~22% in the L1 itself, not in the
  nesting.**  `xz` takes 16.2 s on the Mac, 15.9 s in an HVF guest,
  19.8 s in the UTM VM and 20 s in a guest nested in it.

**A Lustre run**: the rocky9 rhel9.7 bundle, a 3 GB guest, 1 MDT +
2 OSTs of 2 GB.

| step | UTM VM (nested KVM) | macOS (HVF) |
|---|---|---|
| `ltvm create` to ssh | 20 s | 7 s |
| `deploy-lustre --mount` | 31-35 s | 31 s |
| sanity 0a-24b (34 tests) | 4-5 s, all pass | 3 s, all pass |
| createmany / `ls -l` / unlinkmany, 5000 files | 2 s / 1 s / 1 s | 1 s / 1 s / 0 s |
| 1 GiB write (striped, fsync) / read | 5-9 s / 2 s | 2 s / 1 s |
| `llumount` | 37-42 s | 47 s |

About 20% slower end to end: setup and teardown dominate and cost about
the same, and the nesting shows in the I/O.

**Building Lustre.**  Builds in the UTM VM are not nested: `ltvm
build` runs podman directly in it, against a Lustre tree on its own
disk, and only the test VMs nest.  On the Mac podman runs in a VM of
its own (`podman machine`) and reaches its bind mounts over virtiofs.
`ltvm build lustre rocky9 --arch aarch64 --kernel 5.14-rhel9.7
--configure=--with-o2ib=no`, a fresh clone of one commit, an empty
ccache, the same build container image, 5 CPUs on every side (the
podman machine with 4 GB):

| | UTM VM | macOS | macOS, `LTVM_MACOS_BUILD_VOLUMES=0` |
|---|---|---|---|
| full build (configure + 34 modules) | 137-143 s | 160-170 s | 332-340 s |
| rebuild, nothing changed | 3 s | 19-20 s (7-8 s) | 23-24 s (10-10.5 s) |
| rebuild, `llite/file.c` edited | 3-4 s | 22 s (8.3-8.8 s) | 23-24 s (11-11.6 s) |

The first build against a kernel also copies its build-tree into a
podman volume, once: 115 s.  The times in brackets keep the podman
machine up between builds (`LTVM_PODMAN_AUTOSTOP=0`).  An edit
recompiles that one file everywhere; the rest is overhead.

- **virtiofs is what the Mac paid.**  Each compile opens and stats
  thousands of kernel headers, and over virtiofs every one is a round
  trip to macOS: in the podman machine an up-to-date `make` takes 0.8
  s on the machine's own disk and 3.0 s (10 s cold) over virtiofs.  So
  on macOS the kernel build-tree and ccache come from podman volumes
  (CLAUDE.md, "Building on macOS").  The Lustre tree stays a bind
  mount, since it is edited on macOS; moving it too would take the
  full build to 139 s.
- **The podman machine's lifecycle.**  ltvm stops the machine after a
  build to give its memory back and starts it for the next one: about
  12 s per build command, and the machine's page cache.
- **Not CPUs, not `-j`.**  Over virtiofs 10 CPUs build no faster than
  5 (325 s), and ltvm's `make -j<the Mac's cores>` beats `-j5` there
  by hiding the I/O wait (make 235 s against 295 s); with the volumes,
  `-j5` to `-j16` are within noise.

### Intel Mac

Use VMware Fusion, Parallels, or UTM with an x86_64 Linux guest.
Enable nested virtualization in the VM settings (same as VMware
Workstation instructions above).

## Troubleshooting

### `/dev/kvm` exists but permission denied

```bash
sudo chmod 666 /dev/kvm
# Or add your user to the kvm group:
sudo usermod -aG kvm $USER
# Then log out and back in
```

### Module loads but `/dev/kvm` not created

```bash
# Check for errors
dmesg | grep -i kvm
# Common: BIOS has VT-x disabled even though CPU supports it
```

### Everything looks right but VMs won't start

```bash
# Test KVM directly
/opt/qemu/bin/qemu-system-x86_64 -machine accel=kvm -nographic -no-reboot \
    -kernel /boot/vmlinuz-$(uname -r) -append "console=ttyS0 panic=1" &
# Should boot briefly and panic (no rootfs) -- proves KVM works
# Kill it: kill %1
```

If this fails with "Could not access KVM kernel module", the
issue is in the hypervisor configuration, not the guest.
