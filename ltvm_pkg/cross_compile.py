"""Cross-compilation arch mapping.

Single source of truth for arch -> (GNU triple, kbuild ARCH, Debian
arch tag, etc.) used across kernel_build, lustre_build, image_build,
and the shell helpers in targets/common/cross-compile-env.sh.

Both sides must agree on the same mapping; the shell helper lives in
``targets/common/cross-compile-env.sh`` and mirrors this file.  When
adding a new arch, update both.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass


@dataclass(frozen=True)
class CrossInfo:
    """Resolved cross-compile parameters for a (target, host) pair.

    Attributes:
        target_arch:        Target CPU arch (x86_64, aarch64).
        host_arch:          Host CPU arch (from platform.machine()).
        crossing:           True when target != host.
        triple:             GNU triple (aarch64-linux-gnu, x86_64-linux-gnu).
                            Also the RHEL cross-package suffix (gcc-<triple>).
        apt_triple:         Debian cross-package suffix (hyphens not
                            underscores: gcc-x86-64-linux-gnu).
        kbuild_arch:        ARCH=<value> for the Linux kbuild system
                            (arm64, x86_64).
        deb_arch:           Debian architecture tag (amd64, arm64).
        multiarch_triple:   Debian multiarch triple -- same as ``triple``
                            today (aarch64-linux-gnu, x86_64-linux-gnu).
                            Kept separate from ``triple`` so a future
                            arch that distinguishes them (e.g. musl vs
                            gnu) can diverge without churning callers.
        apt_sources_url:    Suitable URL for an apt sources entry for
                            ``deb_arch`` on Ubuntu Noble.  amd64 uses
                            archive.ubuntu.com; other archs use ports.
    """

    target_arch: str
    host_arch: str
    crossing: bool
    triple: str
    apt_triple: str
    kbuild_arch: str
    deb_arch: str
    multiarch_triple: str
    apt_sources_url: str


_ARCH_TABLE: dict[str, dict[str, str]] = {
    "x86_64": {
        "triple": "x86_64-linux-gnu",
        "apt_triple": "x86-64-linux-gnu",
        "kbuild_arch": "x86_64",
        "deb_arch": "amd64",
        "multiarch_triple": "x86_64-linux-gnu",
        "apt_sources_url": "http://archive.ubuntu.com/ubuntu",
    },
    "aarch64": {
        "triple": "aarch64-linux-gnu",
        "apt_triple": "aarch64-linux-gnu",
        "kbuild_arch": "arm64",
        "deb_arch": "arm64",
        "multiarch_triple": "aarch64-linux-gnu",
        "apt_sources_url": "http://ports.ubuntu.com/ubuntu-ports",
    },
}


_ARCH_ALIASES: dict[str, str] = {
    "arm64": "aarch64",   # macOS platform.machine() reports arm64
    "amd64": "x86_64",
}


def normalize_arch(arch: str) -> str:
    """Normalize arch aliases (arm64→aarch64, amd64→x86_64)."""
    return _ARCH_ALIASES.get(arch, arch)


def cross_info(target_arch: str, host_arch: str) -> CrossInfo:
    """Return resolved cross-compile parameters for target + host arch."""
    target_arch = normalize_arch(target_arch)
    host_arch = normalize_arch(host_arch)
    if target_arch not in _ARCH_TABLE:
        raise ValueError(
            f"unknown target_arch={target_arch!r}; "
            f"known: {sorted(_ARCH_TABLE)}"
        )
    t = _ARCH_TABLE[target_arch]
    return CrossInfo(
        target_arch=target_arch,
        host_arch=host_arch,
        crossing=target_arch != host_arch,
        triple=t["triple"],
        apt_triple=t["apt_triple"],
        kbuild_arch=t["kbuild_arch"],
        deb_arch=t["deb_arch"],
        multiarch_triple=t["multiarch_triple"],
        apt_sources_url=t["apt_sources_url"],
    )


def host_deb_arch(host_arch: str) -> str:
    """Return the Debian arch tag for a given host arch."""
    host_arch = normalize_arch(host_arch)
    if host_arch in _ARCH_TABLE:
        return _ARCH_TABLE[host_arch]["deb_arch"]
    # Unknown host arch -- fall back to amd64 so existing defaults
    # (which predate the cross-compile refactor) keep working.
    return "amd64"


_PODMAN_PLATFORM: dict[str, str] = {
    "x86_64": "linux/amd64",
    "aarch64": "linux/arm64",
    "amd64": "linux/amd64",
    "arm64": "linux/arm64",
}


def podman_platform_for(arch: str) -> str:
    """Return the ``--platform`` value for an arch (e.g. linux/amd64)."""
    return _PODMAN_PLATFORM.get(arch, "linux/amd64")


def host_podman_platform() -> str:
    """Return the ``--platform`` value for the current host arch.

    Cross-arch builds run the container as the HOST so the toolchain
    executes natively; inside the container the shared
    ``cross-compile-env.sh`` reads ``TARGET_ARCH`` and wires up
    ``ARCH=`` / ``CROSS_COMPILE=`` make flags against the pre-installed
    cross toolchain.  Forcing the target platform would instead pull an
    emulated (qemu-user) image, which is ~10x slower and masks the
    cross-compile path entirely because ``uname -m`` inside the emulated
    container reports the target arch.
    """
    return podman_platform_for(platform.machine())
