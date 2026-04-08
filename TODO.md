
## Pending Work

- [ ] aarch64 target support: add `--arch` flag for cross-platform builds
  - Container images are multiarch (same Dockerfile)
  - Kernel build needs arch-specific config
  - QEMU uses `qemu-system-aarch64` + `virt` machine type (not microvm)
  - Cross-compile or native build on arm host
  - Needed for Mac users running aarch64 Linux VMs

- [ ] Nested VM testing: `ltvm setup --network` breaks outer VM connectivity
  - The iptables/dnsmasq reconfiguration clobbers existing routes
  - Need to preserve the default route during bridge setup
  - Workaround: run setup steps individually, skip `--network`
  - Or: detect if running inside a VM and adjust network setup accordingly
