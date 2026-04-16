"""Phase-scoped tests for cmd_create in ltvm_pkg/vm_commands.py.

cmd_create is ~350 lines spanning disk allocation, networking, kernel
resolution, QEMU launch, SSH provisioning, VMInfo persistence, and
rollback.  The user plans to split it into phase-named helpers.  These
tests pin the observable I/O contract of each phase so a refactor can
reshape the internals without regressions:

  * disk allocation -- overlay + N data disks are created via qemu-img
    / truncate, and disk_size propagates to VMInfo.
  * kernel resolution -- --kernel name threads into resolve_os_artifacts
    (not interpreted as a path), and kver lands in VMInfo from
    meta.json.
  * NIC validation -- passthrough IOMMU/BDF checks fire before any VM
    state is written, and accepted NIC specs are persisted on VMInfo.
  * VMInfo persistence -- .info file carries all the user-visible
    fields (creator, variant, nics, disk_size).
  * idempotence -- already-running -> no-op; stopped -> launch path.
  * rollback -- launch-time failure preserves QEMU log as .log.failed
    and unwinds overlay/disks/IP.

Tests avoid binding themselves to the phase *ordering*: they observe
what subprocess/run calls were made and what ended up on disk, not
when during cmd_create it happened.
"""

from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import vm_commands
from ltvm_pkg.vm_state import DISK_SIZE_BYTES, VMInfo


# ────────────────────────────────────────────────────────
# Fixtures mirroring test_vm_commands.py's tmp_vmdir.
# Duplicated rather than shared via conftest because the
# SOCKETS/OVERLAYS patching must be hermetic to this module.
# ────────────────────────────────────────────────────────


@pytest.fixture
def tmp_vmdir(tmp_path: Path) -> Iterator[Path]:
    sockets = tmp_path / "sockets"
    overlays = tmp_path / "overlays"
    sockets.mkdir()
    overlays.mkdir()
    with (
        patch("ltvm_pkg.vm_state.VM_DIR", tmp_path),
        patch("ltvm_pkg.vm_state.SOCKETS", sockets),
        patch("ltvm_pkg.vm_state.OVERLAYS", overlays),
        patch("ltvm_pkg.vm_commands.SOCKETS", sockets),
        patch("ltvm_pkg.vm_commands.OVERLAYS", overlays),
    ):
        yield tmp_path


def _create_args(**overrides: Any) -> argparse.Namespace:
    defaults = {
        "name": "co1-phase",
        "vcpus": 2,
        "mem": 2048,
        "mdt_disks": 0,
        "ost_disks": 0,
        "disk_size": None,
        "image": "",
        "kernel": "",
        "os": "",
        "arch": None,
        "variant": None,
        "ip": None,
        "json": False,
        "_quiet": True,
        "nic": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _make_arts(tmp_vmdir: Path, kver_name: str = "5.14") -> MagicMock:
    """Build the MagicMock returned from resolve_os_artifacts."""
    kdir = tmp_vmdir / "kernels" / kver_name
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "vmlinuz").write_bytes(b"kernel")
    (kdir / "vmlinux").write_bytes(b"debug-kernel")
    arts = MagicMock()
    arts.image = tmp_vmdir / "base.ext4"
    arts.kernel = kdir / "vmlinuz"
    arts.arch = "x86_64"
    arts.default_mem = 2048
    return arts


@contextmanager
def _create_env(tmp_vmdir: Path, *, run_rc: int = 0, meta: dict | None = None,
                run_side_effect: Any = None) -> Iterator[dict]:
    """Full mock scaffold for driving cmd_create through disk alloc
    and launch without touching real QEMU / SSH / IP alloc.

    Yields a dict of MagicMocks the test can introspect:
      run, launch, provision, seed, alloc_ip, tc (target_config)
    """
    arts = _make_arts(tmp_vmdir)

    @contextmanager
    def fake_alloc(
        name: str,
        count: int = 1,
        explicit_ip: str | None = None,
    ) -> Iterator[list[str]]:
        # Mgmt IP honours explicit_ip; extras are synthesised per index.
        mgmt = explicit_ip or "192.168.100.50"
        extras = [f"192.168.100.{60 + i}" for i in range(count - 1)]
        yield [mgmt, *extras]

    run_rc_mock = MagicMock(returncode=run_rc, stdout="", stderr="")

    with (
        patch("ltvm_pkg.vm_commands.resolve_os_artifacts", return_value=arts),
        patch("ltvm_pkg.vm_commands.alloc_ip", side_effect=fake_alloc) as m_alloc,
        patch("ltvm_pkg.vm_commands.tap_for_name", return_value="tap-x"),
        patch(
            "ltvm_pkg.vm_commands.mac_for_name",
            return_value="52:54:00:00:00:01",
        ),
        patch(
            "ltvm_pkg.vm_commands.run",
            return_value=run_rc_mock,
            side_effect=run_side_effect,
        ) as m_run,
        patch("ltvm_pkg.vm_commands.launch_qemu") as m_launch,
        patch("ltvm_pkg.vm_commands.provision_vm_ssh") as m_prov,
        patch("ltvm_pkg.vm_commands._seed_kdump_boot") as m_seed,
        patch(
            "ltvm_pkg.vm_commands.load_meta_safe",
            return_value=(
                meta if meta is not None else {"kernel_version": "5.14.0-test"}
            ),
        ),
        patch.dict("os.environ", {}, clear=False),
    ):
        yield {
            "run": m_run,
            "launch": m_launch,
            "provision": m_prov,
            "seed": m_seed,
            "alloc_ip": m_alloc,
            "arts": arts,
        }


# ── disk allocation ────────────────────────────────────


class TestCreateDiskAllocation:
    """Overlay + N data disks are created with qemu-img / truncate,
    and disk_size is persisted on VMInfo."""

    def test_no_data_disks_creates_overlay_only(self, tmp_vmdir: Path) -> None:
        with _create_env(tmp_vmdir) as env:
            vm_commands.cmd_create(_create_args(name="co1-nodisks"))
        # Expect at least: qemu-img create, qemu-img resize
        cmds = [c.args[0] for c in env["run"].call_args_list]
        create_cmds = [c for c in cmds if "create" in c]
        assert len(create_cmds) == 1
        # No truncate calls (data-disk phase skipped)
        truncs = [c for c in cmds if c and c[0] == "truncate"]
        assert truncs == []

    def test_disk_count_matches_mdt_plus_ost(self, tmp_vmdir: Path) -> None:
        with _create_env(tmp_vmdir) as env:
            vm_commands.cmd_create(
                _create_args(name="co1-5disks", mdt_disks=2, ost_disks=3)
            )
        truncs = [
            c.args[0]
            for c in env["run"].call_args_list
            if c.args[0] and c.args[0][0] == "truncate"
        ]
        assert len(truncs) == 5

    def test_disk_size_default_persisted_on_vm(self, tmp_vmdir: Path) -> None:
        with _create_env(tmp_vmdir):
            vm_commands.cmd_create(
                _create_args(name="co1-def-size", mdt_disks=1)
            )
        vm = VMInfo.load("co1-def-size")
        assert vm.disk_size == DISK_SIZE_BYTES

    def test_disk_size_explicit_megs_persisted(self, tmp_vmdir: Path) -> None:
        """--disk-size 200M threads to VMInfo.disk_size (208 MiB)."""
        with _create_env(tmp_vmdir) as env:
            vm_commands.cmd_create(
                _create_args(
                    name="co1-200m", mdt_disks=1, disk_size="200M"
                )
            )
        vm = VMInfo.load("co1-200m")
        assert vm.disk_size == 200 * (1 << 20)
        # The truncate call receives the byte count as a string
        truncs = [
            c.args[0]
            for c in env["run"].call_args_list
            if c.args[0] and c.args[0][0] == "truncate"
        ]
        assert str(200 * (1 << 20)) in truncs[0]


# ── kernel resolution ──────────────────────────────────


class TestCreateKernelResolution:
    """--kernel is a name fed into resolve_os_artifacts (not a path)."""

    def test_explicit_kernel_flag_threaded_through(
        self, tmp_vmdir: Path
    ) -> None:
        """resolve_os_artifacts receives kernel=<user's --kernel>."""
        arts = _make_arts(tmp_vmdir)

        @contextmanager
        def fake_alloc(
            name: str,
            count: int = 1,
            explicit_ip: str | None = None,
        ) -> Iterator[list[str]]:
            yield [
                explicit_ip or "192.168.100.51",
                *[f"192.168.100.{60 + i}" for i in range(count - 1)],
            ]

        run_ok = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch(
                "ltvm_pkg.vm_commands.resolve_os_artifacts", return_value=arts
            ) as m_resolve,
            patch("ltvm_pkg.vm_commands.alloc_ip", side_effect=fake_alloc),
            patch("ltvm_pkg.vm_commands.tap_for_name", return_value="tap-x"),
            patch(
                "ltvm_pkg.vm_commands.mac_for_name",
                return_value="52:54:00:00:00:02",
            ),
            patch("ltvm_pkg.vm_commands.run", return_value=run_ok),
            patch("ltvm_pkg.vm_commands.launch_qemu"),
            patch("ltvm_pkg.vm_commands.provision_vm_ssh"),
            patch("ltvm_pkg.vm_commands._seed_kdump_boot"),
            patch(
                "ltvm_pkg.vm_commands.load_meta_safe",
                return_value={"kernel_version": "5.14.0-test"},
            ),
        ):
            vm_commands.cmd_create(
                _create_args(
                    name="co1-kver", os="rocky9", kernel="5.14-rhel9.5"
                )
            )
        # kernel kwarg made it to resolve_os_artifacts
        call_kwargs = m_resolve.call_args.kwargs
        assert call_kwargs.get("kernel") == "5.14-rhel9.5"

    def test_missing_kernel_version_in_meta_raises(
        self, tmp_vmdir: Path
    ) -> None:
        """meta.json with no kernel_version is a hard error -- we
        must not silently fall back to an unknown kver that would
        break crash-collect later."""
        with (
            _create_env(tmp_vmdir, meta={}),
            pytest.raises(RuntimeError, match="kernel_version"),
        ):
            vm_commands.cmd_create(_create_args(name="co1-bad-meta"))

    def test_kver_persisted_on_vm(self, tmp_vmdir: Path) -> None:
        with _create_env(tmp_vmdir, meta={"kernel_version": "5.14.0-999.el9"}):
            vm_commands.cmd_create(_create_args(name="co1-kver2"))
        vm = VMInfo.load("co1-kver2")
        assert vm.kver == "5.14.0-999.el9"


# ── NIC validation (phase: network) ─────────────────────


class TestCreateNicValidation:
    """--nic specs are validated before any VM state is written."""

    def test_invalid_nic_dies_before_allocation(
        self, tmp_vmdir: Path
    ) -> None:
        """An unknown --nic type must die before alloc_ip / disks / .info."""
        arts = _make_arts(tmp_vmdir)
        with (
            patch("ltvm_pkg.vm_commands.resolve_os_artifacts", return_value=arts),
            patch("ltvm_pkg.vm_commands.alloc_ip") as m_alloc,
            patch("ltvm_pkg.vm_commands.run") as m_run,
            pytest.raises(SystemExit),
        ):
            vm_commands.cmd_create(
                _create_args(name="co1-bad-nic", nic=["bogus"])
            )
        m_alloc.assert_not_called()
        m_run.assert_not_called()
        assert not (tmp_vmdir / "sockets" / "co1-bad-nic.info").exists()

    def test_tcp_nic_persisted_on_vminfo(self, tmp_vmdir: Path) -> None:
        with _create_env(tmp_vmdir):
            vm_commands.cmd_create(
                _create_args(name="co1-tcp", nic=["tcp"])
            )
        vm = VMInfo.load("co1-tcp")
        assert vm.nics == ["tcp"]

    def test_passthrough_without_iommu_dies(self, tmp_vmdir: Path) -> None:
        """passthrough NIC requires host IOMMU; missing IOMMU dies
        before alloc_ip."""
        arts = _make_arts(tmp_vmdir)
        # Create a fake BDF directory so the path-exists check
        # wouldn't die for a different reason than IOMMU.
        bdf = "0000:85:00.1"
        with (
            patch("ltvm_pkg.vm_commands.resolve_os_artifacts", return_value=arts),
            patch("ltvm_pkg.vm_commands.alloc_ip") as m_alloc,
            patch("ltvm_pkg.vfio.iommu_enabled", return_value=False),
            pytest.raises(SystemExit),
        ):
            vm_commands.cmd_create(
                _create_args(
                    name="co1-pt-iommu", nic=[f"passthrough:{bdf}"]
                )
            )
        m_alloc.assert_not_called()

    def test_passthrough_missing_device_dies(self, tmp_vmdir: Path) -> None:
        """passthrough NIC with a non-existent BDF dies before alloc."""
        arts = _make_arts(tmp_vmdir)
        with (
            patch("ltvm_pkg.vm_commands.resolve_os_artifacts", return_value=arts),
            patch("ltvm_pkg.vm_commands.alloc_ip") as m_alloc,
            patch("ltvm_pkg.vfio.iommu_enabled", return_value=True),
            pytest.raises(SystemExit),
        ):
            vm_commands.cmd_create(
                _create_args(
                    name="co1-pt-nodev",
                    nic=["passthrough:ffff:ff:ff.f"],
                )
            )
        m_alloc.assert_not_called()


# ── VMInfo persistence (phase: commit) ─────────────────


class TestCreateVMInfoPersistence:
    """The .info file after cmd_create carries all user-visible fields."""

    def test_creator_from_sudo_user(self, tmp_vmdir: Path) -> None:
        with (
            _create_env(tmp_vmdir),
            patch.dict("os.environ", {"SUDO_USER": "alice"}, clear=False),
            patch("pwd.getpwnam") as m_pw,
        ):
            m_pw.return_value = MagicMock(pw_uid=1001, pw_gid=1001)
            vm_commands.cmd_create(_create_args(name="co1-alice"))
        vm = VMInfo.load("co1-alice")
        assert vm.creator == "alice"

    def test_creator_falls_back_to_root(self, tmp_vmdir: Path) -> None:
        with (
            _create_env(tmp_vmdir),
            patch.dict("os.environ", {}, clear=True),
        ):
            vm_commands.cmd_create(_create_args(name="co1-asroot"))
        vm = VMInfo.load("co1-asroot")
        assert vm.creator == "root"

    def test_default_variant_is_base(self, tmp_vmdir: Path) -> None:
        with _create_env(tmp_vmdir):
            vm_commands.cmd_create(_create_args(name="co1-var", variant=None))
        vm = VMInfo.load("co1-var")
        assert vm.variant == "base"

    def test_arch_recorded_from_arts(self, tmp_vmdir: Path) -> None:
        """arch comes from resolve_os_artifacts, not args."""
        with _create_env(tmp_vmdir) as env:
            env["arts"].arch = "aarch64"
            vm_commands.cmd_create(_create_args(name="co1-arm"))
        vm = VMInfo.load("co1-arm")
        assert vm.arch == "aarch64"

    def test_default_mem_from_target_when_mem_none(
        self, tmp_vmdir: Path
    ) -> None:
        """When --mem isn't passed (None), target's default_mem wins."""
        with _create_env(tmp_vmdir) as env:
            env["arts"].default_mem = 4096
            vm_commands.cmd_create(_create_args(name="co1-dmem", mem=None))
        vm = VMInfo.load("co1-dmem")
        assert vm.mem == 4096


# ── idempotence ───────────────────────────────────────


class TestCreateIdempotence:
    """Re-creating an existing VM is a no-op or re-launch."""

    def test_json_already_running(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """JSON mode emits an action=none record for a running VM."""
        # seed a minimal VMInfo on disk
        vm = VMInfo(name="co1-idem", ip="10.0.0.1", pid=1234)
        vm.save()
        args = _create_args(name="co1-idem", json=True)
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.wait_for_ssh"),
            patch("ltvm_pkg.vm_commands.register_ssh_name"),
        ):
            vm_commands.cmd_create(args)
        out = json.loads(capsys.readouterr().out)
        assert out["action"] == "none"
        assert out["status"] == "already running"

    def test_json_started_when_stopped(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A stopped VM re-create returns action=started in JSON."""
        vm = VMInfo(name="co1-restart", ip="10.0.0.2", pid=0)
        vm.save()
        args = _create_args(name="co1-restart", json=True)
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=False),
            patch("ltvm_pkg.vm_commands.launch_qemu"),
            patch("ltvm_pkg.vm_commands.provision_vm_ssh"),
            patch("ltvm_pkg.vm_commands._seed_kdump_boot"),
        ):
            vm_commands.cmd_create(args)
        out = json.loads(capsys.readouterr().out)
        assert out["action"] == "started"


# ── rollback on launch failure ─────────────────────────


class TestCreateRollback:
    """A launch-time failure unwinds overlay/disks/.info and preserves
    the QEMU log so the user can diagnose what happened.
    """

    def test_launch_failure_removes_info_and_overlay(
        self, tmp_vmdir: Path
    ) -> None:
        # launch_qemu raises -> rollback must clean up .info + overlay
        arts = _make_arts(tmp_vmdir)

        @contextmanager
        def fake_alloc(
            name: str,
            count: int = 1,
            explicit_ip: str | None = None,
        ) -> Iterator[list[str]]:
            yield [
                explicit_ip or "192.168.100.60",
                *[f"192.168.100.{70 + i}" for i in range(count - 1)],
            ]

        run_ok = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("ltvm_pkg.vm_commands.resolve_os_artifacts", return_value=arts),
            patch("ltvm_pkg.vm_commands.alloc_ip", side_effect=fake_alloc),
            patch("ltvm_pkg.vm_commands.tap_for_name", return_value="tap-x"),
            patch(
                "ltvm_pkg.vm_commands.mac_for_name",
                return_value="52:54:00:00:00:03",
            ),
            patch("ltvm_pkg.vm_commands.run", return_value=run_ok),
            patch(
                "ltvm_pkg.vm_commands.launch_qemu",
                side_effect=RuntimeError("qemu boom"),
            ),
            patch("ltvm_pkg.vm_commands.provision_vm_ssh"),
            patch("ltvm_pkg.vm_commands._seed_kdump_boot"),
            patch("ltvm_pkg.vm_commands.kill_qemu"),
            patch("ltvm_pkg.vm_commands.unregister_ssh_name"),
            patch(
                "ltvm_pkg.vm_commands.load_meta_safe",
                return_value={"kernel_version": "5.14.0-test"},
            ),
            pytest.raises(RuntimeError, match="qemu boom"),
        ):
            vm_commands.cmd_create(
                _create_args(name="co1-fails", mdt_disks=1)
            )
        # .info gone, overlay gone, disk gone
        assert not (tmp_vmdir / "sockets" / "co1-fails.info").exists()
        assert not (tmp_vmdir / "overlays" / "co1-fails.qcow2").exists()
        assert not (
            tmp_vmdir / "overlays" / "co1-fails-disk1.img"
        ).exists()

    def test_launch_failure_preserves_qemu_log(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """On launch failure the .log is renamed to .log.failed so the
        user can read it after rollback wipes artifacts."""
        arts = _make_arts(tmp_vmdir)

        @contextmanager
        def fake_alloc(
            name: str,
            count: int = 1,
            explicit_ip: str | None = None,
        ) -> Iterator[list[str]]:
            yield [
                explicit_ip or "192.168.100.61",
                *[f"192.168.100.{80 + i}" for i in range(count - 1)],
            ]

        run_ok = MagicMock(returncode=0, stdout="", stderr="")

        def seed_log_then_fail(vm: Any) -> None:
            vm.log_path.write_text("fake qemu stderr\n")
            raise RuntimeError("launch failed")

        with (
            patch("ltvm_pkg.vm_commands.resolve_os_artifacts", return_value=arts),
            patch("ltvm_pkg.vm_commands.alloc_ip", side_effect=fake_alloc),
            patch("ltvm_pkg.vm_commands.tap_for_name", return_value="tap-y"),
            patch(
                "ltvm_pkg.vm_commands.mac_for_name",
                return_value="52:54:00:00:00:04",
            ),
            patch("ltvm_pkg.vm_commands.run", return_value=run_ok),
            patch(
                "ltvm_pkg.vm_commands.launch_qemu",
                side_effect=seed_log_then_fail,
            ),
            patch("ltvm_pkg.vm_commands.provision_vm_ssh"),
            patch("ltvm_pkg.vm_commands._seed_kdump_boot"),
            patch("ltvm_pkg.vm_commands.kill_qemu"),
            patch("ltvm_pkg.vm_commands.unregister_ssh_name"),
            patch(
                "ltvm_pkg.vm_commands.load_meta_safe",
                return_value={"kernel_version": "5.14.0-test"},
            ),
            pytest.raises(RuntimeError),
        ):
            vm_commands.cmd_create(_create_args(name="co1-logfail"))
        preserved = tmp_vmdir / "sockets" / "co1-logfail.log.failed"
        assert preserved.exists()
        assert "fake qemu stderr" in preserved.read_text()
        err = capsys.readouterr().err
        assert "QEMU log preserved" in err


# ── output formatting ─────────────────────────────────


class TestCreateOutput:
    """Non-JSON output includes the VM name, IP, and disk topology."""

    def test_human_output_has_name_ip_disks(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _create_args(
            name="co1-out", mdt_disks=1, ost_disks=2, _quiet=False
        )
        with _create_env(tmp_vmdir):
            vm_commands.cmd_create(args)
        out = capsys.readouterr().out
        assert "VM created: co1-out" in out
        assert "192.168.100.50" in out
        assert "1 MDT + 2 OST" in out

    def test_json_output_has_action_created(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # os="rocky9" suppresses the "using default target:" banner
        # that would pollute stdout when --os wasn't explicit.
        args = _create_args(name="co1-json", json=True, os="rocky9")
        with _create_env(tmp_vmdir):
            vm_commands.cmd_create(args)
        # The JSON line is the last non-empty line.
        lines = [
            ln for ln in capsys.readouterr().out.splitlines() if ln.strip()
        ]
        out = json.loads(lines[-1])
        assert out["action"] == "created"
        assert out["name"] == "co1-json"
        assert out["status"] == "running"
