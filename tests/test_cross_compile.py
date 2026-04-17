"""Tests for ltvm_pkg.cross_compile -- the single-source-of-truth
arch mapping shared between Python (kernel_build / lustre_build /
image_build) and the shell helper in targets/common/cross-compile-env.sh.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ltvm_pkg.cross_compile import (
    cross_info,
    host_deb_arch,
    host_podman_platform,
    podman_platform_for,
)


class TestCrossInfoNative:
    """When target == host, crossing is False and CROSS_COMPILE is unused."""

    def test_x86_64_native(self) -> None:
        i = cross_info("x86_64", "x86_64")
        assert i.crossing is False
        assert i.triple == "x86_64-linux-gnu"
        assert i.apt_triple == "x86-64-linux-gnu"
        assert i.kbuild_arch == "x86_64"
        assert i.deb_arch == "amd64"

    def test_aarch64_native(self) -> None:
        i = cross_info("aarch64", "aarch64")
        assert i.crossing is False
        assert i.triple == "aarch64-linux-gnu"
        assert i.apt_triple == "aarch64-linux-gnu"
        assert i.kbuild_arch == "arm64"
        assert i.deb_arch == "arm64"


class TestCrossInfoBothDirections:
    """Cross-compile is symmetric: either host arch can target either."""

    def test_x86_64_host_aarch64_target(self) -> None:
        i = cross_info("aarch64", "x86_64")
        assert i.crossing is True
        assert i.triple == "aarch64-linux-gnu"
        assert i.apt_triple == "aarch64-linux-gnu"
        assert i.kbuild_arch == "arm64"
        assert i.deb_arch == "arm64"

    def test_aarch64_host_x86_64_target(self) -> None:
        """The Apple Silicon case: ARM host, x86_64 target."""
        i = cross_info("x86_64", "aarch64")
        assert i.crossing is True
        assert i.triple == "x86_64-linux-gnu"
        assert i.apt_triple == "x86-64-linux-gnu"
        assert i.kbuild_arch == "x86_64"
        assert i.deb_arch == "amd64"


class TestAptSourcesUrl:
    """Ubuntu sources for the target arch: amd64 -> archive.ubuntu.com,
    everything else -> ports.ubuntu.com."""

    def test_amd64_uses_main_archive(self) -> None:
        assert (
            cross_info("x86_64", "aarch64").apt_sources_url
            == "http://archive.ubuntu.com/ubuntu"
        )

    def test_arm64_uses_ports(self) -> None:
        assert "ports.ubuntu.com" in cross_info(
            "aarch64", "x86_64"
        ).apt_sources_url


class TestUnknownArch:
    def test_unknown_target_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown target_arch"):
            cross_info("mips64", "x86_64")


class TestHostDebArch:
    def test_x86_64(self) -> None:
        assert host_deb_arch("x86_64") == "amd64"

    def test_aarch64(self) -> None:
        assert host_deb_arch("aarch64") == "arm64"

    def test_unknown_falls_back_to_amd64(self) -> None:
        """Safety net for odd uname -m values (e.g. arm64 alone on some
        Apple-native tooling) -- we don't want to crash downstream."""
        assert host_deb_arch("riscv64") == "amd64"


class TestPodmanPlatformFor:
    def test_x86_64(self) -> None:
        assert podman_platform_for("x86_64") == "linux/amd64"

    def test_aarch64(self) -> None:
        assert podman_platform_for("aarch64") == "linux/arm64"

    def test_apple_silicon_uname_form(self) -> None:
        """platform.machine() on Apple Silicon can report ``arm64``
        (Darwin) not ``aarch64`` (Linux).  Map it anyway."""
        assert podman_platform_for("arm64") == "linux/arm64"

    def test_amd64(self) -> None:
        assert podman_platform_for("amd64") == "linux/amd64"

    def test_unknown_defaults_to_amd64(self) -> None:
        assert podman_platform_for("riscv64") == "linux/amd64"


class TestHostPodmanPlatform:
    """The core of the s3f fix: container builds pick HOST arch, not
    target arch, so cross-compile actually fires."""

    def test_host_x86_64(self) -> None:
        with patch("ltvm_pkg.cross_compile.platform.machine",
                   return_value="x86_64"):
            assert host_podman_platform() == "linux/amd64"

    def test_host_aarch64(self) -> None:
        with patch("ltvm_pkg.cross_compile.platform.machine",
                   return_value="aarch64"):
            assert host_podman_platform() == "linux/arm64"

    def test_host_arm64_darwin(self) -> None:
        """Apple Silicon host targeting x86_64 Linux: the podman
        platform must be linux/arm64 (native on the Mac) so the
        container runs at full speed and the cross toolchain fires."""
        with patch("ltvm_pkg.cross_compile.platform.machine",
                   return_value="arm64"):
            assert host_podman_platform() == "linux/arm64"
