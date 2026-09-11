"""Shared fixtures for ltvm tests."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import yaml

if TYPE_CHECKING:
    from ltvm_pkg.target_config import TargetConfig

_ROCKY9_YAML: dict = {
    "defaults": {"arch": "x86_64", "os_family": "rhel"},
    "targets": {
        "rocky9": {
            "os_name": "rocky",
            "os_version": "9.7",
            "container_image": "rockylinux:9.7",
            "srpm_url": "https://dl.rockylinux.org/pub/rocky/9/BaseOS/source/tree/Packages/k",
            "status": "working",
            "kernels": {
                "default": "5.14-rhel9.7",
                "available": ["5.14-rhel9.7", "5.14-rhel9.5"],
                "config": {"CONFIG_XEN_PVH": "y"},
            },
            "lustre": {"mode": "server_ldiskfs"},
        }
    },
}


def _write_targets_yaml(targets_dir: Path, data: dict | None = None) -> None:
    """Write targets.yaml into targets_dir."""
    (targets_dir / "targets.yaml").write_text(
        yaml.dump(data or _ROCKY9_YAML, default_flow_style=False)
    )


def _make_config(tmp_targets: Path, arch: str | None = None) -> TargetConfig:
    """Instantiate a TargetConfig with patched paths."""
    import ltvm_pkg.target_config as cfg

    with (
        patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
        patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"),
        patch.object(
            cfg,
            "TARGETS_YAML",
            tmp_targets / "targets" / "targets.yaml",
        ),
    ):
        return cfg.TargetConfig("rocky9", arch=arch)


def _make_kernel_outputs(tc, kernel: str | None = None) -> Path:
    """Lay down the files a successful kernel build leaves behind.

    is_stale() checks that an artifact's outputs actually exist, not
    just that its meta.json hash matches -- so a test that writes meta
    without outputs is describing a *failed* build.  Use this whenever
    a test means "a good kernel build is cached here".
    """
    out = tc.kernel_output_dir(kernel)
    (out / "build-tree").mkdir(parents=True, exist_ok=True)
    (out / "modules" / "lib" / "modules").mkdir(parents=True, exist_ok=True)
    (out / "vmlinux").write_bytes(b"\x7fELF")
    (out / "vmlinuz").write_bytes(b"kernel")
    (out / "build-tree" / ".config").write_text("CONFIG_X=y\n")
    (out / "modules" / "lib" / "modules" / "dummy.ko").write_bytes(b"ko")
    return out


def _make_image_outputs(tc, kernel: str | None = None, variant=None) -> Path:
    """Lay down the file a successful image build leaves behind."""
    out = tc.image_output_dir(kernel, variant=variant)
    out.mkdir(parents=True, exist_ok=True)
    img = out / "base.ext4"
    img.write_bytes(b"ext4")
    return img


@pytest.fixture(autouse=True)
def _isolate_user_state(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> object:
    """Keep the suite out of the developer's real config and state.

    ltvm's main() records telemetry counters and runs the update check,
    and the CLI tests drive main() directly -- so without this they
    accumulate into ~/.local/state/ltvm.  That is not merely untidy:
    those counters get *sent*, so a test run arrives at the server as
    somebody's usage.  It happened, and the giveaway was `build_lustre`
    failing 21 times on a machine where nobody had run it.

    LTVM_TELEMETRY rather than patching record(): it disables telemetry
    through its own front door, so the tests exercise the real disabled
    path, and it works regardless of the module-level paths having been
    resolved at import time by an earlier test.

    The update check has no such switch and would otherwise `git
    ls-remote` once per test, so it is patched out.  Both have their
    own tests, which set up their own isolation.
    """
    root = tmp_path_factory.mktemp("xdg")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    monkeypatch.setenv("LTVM_TELEMETRY", "0")
    monkeypatch.setenv("LTVM_SITE_CONFIG", str(root / "no-such-ltvm.conf"))
    # The update check has no kill switch and would otherwise `git
    # ls-remote` once per test, so it is patched out.  Telemetry needs
    # no patch: the env above disables it through its own front door,
    # so the tests exercise the real disabled path.
    with patch(
        "ltvm_pkg.update_check.maybe_check_for_updates", return_value=None
    ):
        yield


@pytest.fixture(autouse=True)
def _neutralize_podman_preflight() -> object:
    """Suppress the macOS podman-machine preflight for unit tests.

    On Darwin, build commands call ``check_podman_machine_macos`` which
    would fail tests that don't mock podman. The tests already stub the
    actual build functions, so the preflight is orthogonal.
    """
    with patch(
        "ltvm_pkg.cli.build.check_podman_machine_macos", return_value=None
    ):
        yield


@pytest.fixture(autouse=True)
def _neutralize_container_preflight() -> object:
    """Suppress the build-container-exists preflight for unit tests.

    Build commands short-circuit when `podman image exists <tag>` fails.
    Most tests mock the actual build functions and don't care about
    podman state, so treat the preflight as a pass unless a test opts
    out by re-patching ``_preflight_container``.
    """
    with patch("ltvm_pkg.cli.build._preflight_container", return_value=None):
        yield


@pytest.fixture
def tmp_targets(tmp_path: Path) -> Path:
    """Create a minimal targets/ tree for TargetConfig tests."""
    common = tmp_path / "targets" / "common"
    common.mkdir(parents=True)
    (common / "packages-base.txt").write_text("bash\ncoreutils\n")
    (common / "packages-dev.txt").write_text("gcc\nmake\n")
    (common / "packages-test.txt").write_text("fio\nattr\n")
    (common / "packages-debug.txt").write_text("gdb\nstrace\n")
    (common / "packages-server.txt").write_text("nfs-utils\n")
    (common / "kernel-config.fragment").write_text(
        "CONFIG_VIRTIO=y\nCONFIG_9P_FS=y\n"
    )

    rocky9 = tmp_path / "targets" / "rocky9"
    rocky9.mkdir(parents=True)

    # Populate rocky9 with real Dockerfiles for tests that read them.
    # Fall back to stubs if the real files aren't present (e.g. CI).
    _real_targets = Path(__file__).parent.parent / "targets"
    for df in ("container.Dockerfile", "image.Dockerfile"):
        real = _real_targets / "rocky9" / df
        if real.exists():
            (rocky9 / df).write_text(real.read_text())
        else:
            (rocky9 / df).write_text("FROM rockylinux:9.7\n# stub\n")

    _write_targets_yaml(tmp_path / "targets")

    # Also create output dir
    (tmp_path / "artifacts" / "rocky9").mkdir(parents=True)

    return tmp_path


@pytest.fixture
def lustre_tree(tmp_path: Path) -> Path:
    """Create a minimal mock Lustre source tree."""
    lt = tmp_path / "lustre-release"
    targets_dir = lt / "lustre" / "kernel_patches" / "targets"
    targets_dir.mkdir(parents=True)

    configs_dir = lt / "lustre" / "kernel_patches" / "kernel_configs"
    configs_dir.mkdir(parents=True)

    series_dir = lt / "lustre" / "kernel_patches" / "series"
    series_dir.mkdir(parents=True)

    patches_dir = lt / "lustre" / "kernel_patches" / "patches"
    patches_dir.mkdir(parents=True)

    # Write a .target file
    (targets_dir / "5.14-rhel9.7.target").write_text(
        textwrap.dedent("""\
            lnxmaj=5.14.0
            lnxrel=503.26.1.el9_7
            SERIES=5.14-rhel9.7.series
        """)
    )

    # Kernel config
    (configs_dir / "kernel-5.14.0-5.14-rhel9.7-x86_64.config").write_text(
        "# kernel config\nCONFIG_X86=y\n"
    )

    # Series file with patches
    (series_dir / "5.14-rhel9.7.series").write_text(
        "patch1.patch\npatch2.patch\n"
    )

    # Patch files
    (patches_dir / "patch1.patch").write_text("--- a/foo\n+++ b/foo\n")
    (patches_dir / "patch2.patch").write_text("--- a/bar\n+++ b/bar\n")

    return lt


def make_fake_ko(modinfo: dict[str, str]) -> bytes:
    """Build a minimal ELF64 .ko carrying a .modinfo section.

    read_modinfo_field parses the real .modinfo section rather than
    scanning the whole file, because a whole-file scan matches the
    needle inside longer keys ("version=" within "rhelversion=") and
    inside ordinary string constants.  So tests need a genuine ELF, not
    a blob with key=value bytes in it.
    """
    import struct

    entries = b"".join(
        f"{k}={v}".encode() + b"\x00" for k, v in modinfo.items()
    )
    shstrtab = b"\x00.modinfo\x00.shstrtab\x00"
    ehsize, shentsize, shnum = 64, 64, 3
    shoff = ehsize
    body_off = shoff + shentsize * shnum
    modinfo_off = body_off
    shstrtab_off = modinfo_off + len(entries)

    eh = bytearray(ehsize)
    eh[0:4] = b"\x7fELF"
    eh[4] = 2  # ELFCLASS64
    eh[5] = 1  # little endian
    eh[6] = 1  # EV_CURRENT
    struct.pack_into("<H", eh, 0x10, 1)  # e_type = ET_REL
    struct.pack_into("<Q", eh, 0x28, shoff)  # e_shoff
    struct.pack_into("<HHH", eh, 0x3A, shentsize, shnum, 2)

    def sh(name_off: int, off: int, size: int) -> bytes:
        s = bytearray(shentsize)
        struct.pack_into("<I", s, 0x00, name_off)
        struct.pack_into("<I", s, 0x04, 1)  # SHT_PROGBITS
        struct.pack_into("<QQ", s, 0x18, off, size)
        return bytes(s)

    return b"".join(
        [
            bytes(eh),
            sh(0, 0, 0),  # SHN_UNDEF
            sh(1, modinfo_off, len(entries)),  # ".modinfo"
            sh(10, shstrtab_off, len(shstrtab)),  # ".shstrtab"
            entries,
            shstrtab,
        ]
    )
