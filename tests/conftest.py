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


def _make_config(
    tmp_targets: Path, arch: str | None = None, variant: str | None = None
) -> TargetConfig:
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
        if variant is None:
            return cfg.TargetConfig("rocky9", arch=arch)
        return cfg.TargetConfig("rocky9", arch=arch, variant=variant)


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
    monkeypatch.setenv("XDG_CACHE_HOME", str(root / "cache"))
    monkeypatch.setenv("LTVM_TELEMETRY", "0")
    monkeypatch.setenv("LTVM_SITE_CONFIG", str(root / "no-such-ltvm.conf"))
    # The developer's own build time limit would change podman argv.
    monkeypatch.delenv("LTVM_BUILD_TIMEOUT", raising=False)
    # On a Mac, build_lustre, clean and doctor would otherwise create,
    # probe and list real podman volumes from tests that mock only the
    # build's own podman run.  Telling build_volumes it is not on a Mac
    # covers all of them; tests/test_build_volumes.py turns it back on.
    from ltvm_pkg import build_volumes

    monkeypatch.setattr(build_volumes, "is_macos", lambda: False)
    monkeypatch.delenv("LTVM_MACOS_BUILD_VOLUMES", raising=False)
    monkeypatch.delenv("LTVM_PODMAN_AUTOSTOP", raising=False)
    # Tab completion installs into /etc/bash_completion.d and the zsh
    # and fish equivalents, and `ltvm doctor --fix` installs any that
    # are missing -- so without this a doctor test rewrote the
    # developer's real system completion files.  Same front-door
    # principle as LTVM_TELEMETRY above: the real install path runs,
    # just under a tmpdir.
    monkeypatch.setenv("LTVM_COMPLETION_ROOT", str(root / "completion"))
    # VM claims live in the real VM_DIR and key on the session running
    # the suite -- often an agent's -- so a test deploy would claim the
    # developer's VMs, or be refused by a claim on them.
    for var in (
        "LTVM_OWNER_ID",
        "LTVM_OWNER_PID",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_PID",
    ):
        monkeypatch.delenv(var, raising=False)
    claims = root / "claims"
    claims.mkdir()
    claims.chmod(0o1777)
    monkeypatch.setattr("ltvm_pkg.vm_claim.CLAIMS_DIR", claims)
    # The update check has no kill switch and would otherwise `git
    # ls-remote` once per test, so it is patched out.  Telemetry needs
    # no patch: the env above disables it through its own front door,
    # so the tests exercise the real disabled path.
    with patch(
        "ltvm_pkg.update_check.maybe_check_for_updates", return_value=None
    ):
        yield


@pytest.fixture(autouse=True)
def _isolate_vm_state(tmp_path_factory: pytest.TempPathFactory) -> object:
    """Keep VM and cluster records out of the host's real sockets dir.

    Tests name their fixtures after real clusters (co2, co3), so a save
    that no test patches overwrites a real record.  It happened: a new
    ClusterInfo.save() in cluster deploy rewrote co3.cluster.  A test
    that needs its own directory patches SOCKETS again inside this one.
    """
    with (
        patch("ltvm_pkg.vm_state.SOCKETS", tmp_path_factory.mktemp("sockets")),
        # `ltvm clean` reads every overlay's backing file
        # (image_store.referenced_images); the host's VMs must not
        # decide what a test may prune.
        patch(
            "ltvm_pkg.vm_state.OVERLAYS", tmp_path_factory.mktemp("overlays")
        ),
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
def _isolate_podman_usage_lock(
    tmp_path_factory: pytest.TempPathFactory,
) -> object:
    """Keep the podman usage lock off the real one in /tmp.

    A test that took the real lock would keep a real build's auto-stop
    from stopping the machine, and one that probed it would see a real
    build's hold.  Each test starts with no hold, and drops any it took.
    """
    import os

    import ltvm_pkg.host_setup as hs

    lock = tmp_path_factory.mktemp("podman-lock") / "podman.lock"
    hs._usage_fd = None
    with patch.object(hs, "_podman_lock_path", lambda: lock):
        yield
    if hs._usage_fd is not None:
        os.close(hs._usage_fd)
        hs._usage_fd = None


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


@pytest.fixture(autouse=True)
def _neutralize_ssh_config_check() -> object:
    """Keep `ltvm doctor` off the host's real ssh and its config.

    tests/test_vm_doctor.py::TestDoctorSshClientConfig opts back in.
    """
    with patch(
        "ltvm_pkg.vm_commands.ssh_client_config_error", return_value=None
    ):
        yield


@pytest.fixture(autouse=True)
def _classic_privileges() -> object:
    """Pin the sudo path, whatever the test host's own setup.

    A host installed for unprivileged VMs would otherwise switch launch,
    DNS and CLI code to it.  tests/test_rootless.py opts back in.
    """
    from ltvm_pkg import rootless

    with (
        patch.object(
            rootless,
            "readiness",
            return_value=rootless.Readiness(problems=["disabled in tests"]),
        ),
        patch.object(rootless, "hosts_dir_writable", return_value=False),
        patch.object(rootless, "hosts_entries", return_value=[]),
    ):
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


@pytest.fixture(autouse=True)
def _neutralize_deployed_fstype() -> object:
    """Keep cluster deploy from reading a node's cfg/local.sh over ssh.

    A cluster with no recorded fstype asks its first client which one
    it was deployed with; the tests' clusters have no real nodes.
    tests/test_cluster_logic.py::TestDeployedFstype covers the reader.
    """
    with patch("ltvm_pkg.vm_cluster._deployed_fstype", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _no_real_node_create() -> object:
    """Fail a test whose cluster create reaches a real `ltvm create`.

    cluster create runs each node's create as a child through
    vm_cluster._run_prefixed; a test that gets that far patches it.
    """

    def refuse(cmd: object, *_a: object, **_k: object) -> None:
        raise AssertionError(f"test ran a real child command: {cmd}")

    with patch("ltvm_pkg.vm_cluster._run_prefixed", side_effect=refuse):
        yield


@pytest.fixture(autouse=True)
def _neutralize_guest_memory_budget() -> object:
    """Keep the host's RAM out of `list` and the cluster create preflight.

    Tests that check either patch it with a budget of their own.
    """
    with patch("ltvm_pkg.qemu_run.guest_memory_budget", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _neutralize_ensure_pdsh() -> object:
    """Keep cluster deploy from installing pdsh over ssh on fake nodes.

    tests/test_cluster_logic.py::TestEnsurePdsh covers it.
    """
    with patch(
        "ltvm_pkg.vm_cluster._ensure_pdsh",
        side_effect=lambda name, *a, **k: (name, 0, ""),
    ):
        yield


@pytest.fixture(autouse=True)
def _neutralize_guest_helpers() -> object:
    """Keep cluster deploy from writing the guest helpers over ssh."""
    with patch(
        "ltvm_pkg.vm_cluster._install_guest_helpers",
        side_effect=lambda name, *a, **k: (name, 0, ""),
    ):
        yield


@pytest.fixture(autouse=True)
def _neutralize_lustre_state() -> object:
    """Keep cluster status from probing fake nodes over ssh.

    tests/test_cluster_lustre_state.py covers it.
    """
    with patch("ltvm_pkg.cluster_lustre_state.lustre_state", return_value=[]):
        yield


@pytest.fixture(autouse=True)
def _neutralize_podman_gc() -> object:
    """Keep the builds' and clean's podman image cleanup off real podman.

    Every podman call in podman_gc goes through _podman; with it
    returning None the store looks empty.  tests/test_podman_gc.py
    patches it again for its own cases.
    """
    with patch("ltvm_pkg.podman_gc._podman", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _neutralize_host_clean_state() -> object:
    """Keep the host's VMs, disk and image files out of clean and builds.

    `ltvm clean` lists the host's idle VMs and protects their kernels,
    builds warn on a nearly full disk, and an image build prunes the
    image files no overlay on the host uses.  Each has its own tests.
    """
    with (
        patch("ltvm_pkg.cli.clean._idle_vms", return_value=[]),
        patch("ltvm_pkg.cli.clean._vm_kernel_dirs", return_value=set()),
        patch("ltvm_pkg.cli.build._warn_low_disk"),
        patch("ltvm_pkg.image_build.prune_superseded", return_value=0),
    ):
        yield
