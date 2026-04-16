
## Pending Work

- [x] aarch64 target support: arch-aware build and VM infrastructure
  - Config: arch mapping helpers in lib/config.py, per-target arch in targets.yaml
  - Kernel: SRPM/deb build scripts parameterized by TARGET_ARCH env var
  - Kernel config: arch-specific fragment (kernel-config-aarch64.fragment)
  - QEMU: qemu-system-aarch64 + virt machine type, virtio-*-pci devices
  - Setup: builds both x86_64 + aarch64 QEMU targets from source
  - VM info: arch persisted per-VM for correct QEMU binary selection
  - Remaining: add concrete aarch64 target entries to targets.yaml, test on arm host

- [ ] Nested VM testing: `ltvm install --network` breaks outer VM connectivity
  - The iptables/dnsmasq reconfiguration clobbers existing routes
  - Need to preserve the default route during bridge setup
  - Workaround: run setup steps individually, skip `--network`
  - Or: detect if running inside a VM and adjust network setup accordingly
