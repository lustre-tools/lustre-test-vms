
## Pending Work

- [ ] aarch64 target support: add `--arch` flag for cross-platform builds
  - Container images are multiarch (same Dockerfile)
  - Kernel build needs arch-specific config
  - QEMU uses `qemu-system-aarch64` + `virt` machine type (not microvm)
  - Cross-compile or native build on arm host
  - Needed for Mac users running aarch64 Linux VMs
