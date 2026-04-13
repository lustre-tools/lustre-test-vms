"""Tests for ltvm_pkg/deploy.py: tar streaming, disk topology, mount."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import deploy
from ltvm_pkg.vm_state import VMInfo


def _ok(stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock()
    r.returncode = 0
    r.stdout = stdout
    r.stderr = stderr
    return r


def _fail(rc: int = 1, stdout: str = "", stderr: str = "boom") -> MagicMock:
    r = MagicMock()
    r.returncode = rc
    r.stdout = stdout
    r.stderr = stderr
    return r


@pytest.fixture
def tmp_sockets(tmp_path: Path) -> Path:
    """Redirect SOCKETS so VMInfo.save writes under tmp_path."""
    with patch("ltvm_pkg.vm_state.SOCKETS", tmp_path):
        yield tmp_path


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    """Create a minimal staging tree."""
    s = tmp_path / "staging"
    (s / "usr/lib64/lustre/tests").mkdir(parents=True)
    (s / "lib/modules/5.14").mkdir(parents=True)
    (s / "lib/modules/5.14/lustre.ko").write_text("")
    return s


def _make_vm(
    name: str = "co1-single",
    ip: str = "192.168.100.50",
    mdt_disks: int = 0,
    ost_disks: int = 0,
    disk_size: int = 500 * 1024 * 1024,
) -> VMInfo:
    return VMInfo(
        name=name,
        ip=ip,
        mdt_disks=mdt_disks,
        ost_disks=ost_disks,
        disk_size=disk_size,
    )


# ── configure_test_disks ─────────────────────────────────


class TestConfigureTestDisks:
    """configure_test_disks writes a correct disk topology into local.sh."""

    def _capture_script(
        self,
        mdt: int,
        ost: int,
        disk_size: int = 0,
        os_family: str = "rhel",
    ) -> str:
        captured: dict = {}

        def fake_run_ssh(ip, script, timeout=30):
            captured["ip"] = ip
            captured["script"] = script
            return _ok()

        with patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_run_ssh):
            deploy.configure_test_disks(
                "10.0.0.1",
                mdt,
                ost,
                disk_size,
                os_family=os_family,
            )
        return captured["script"]

    def test_mdt_only_starts_at_vdb(self) -> None:
        """Single MDT disk maps to /dev/vdb (vda is rootfs)."""
        script = self._capture_script(mdt=1, ost=0)
        assert "MDSCOUNT=1" in script
        assert "MDSDEV1=/dev/vdb" in script
        assert "OSTCOUNT" not in script

    def test_mdt_and_ost_ordering(self) -> None:
        """MDT disks come first, then OST disks in allocation order."""
        # mdt=2, ost=3: MDSDEV1=vdb, MDSDEV2=vdc, OSTDEV1=vdd, vde, vdf
        script = self._capture_script(mdt=2, ost=3)
        assert "MDSCOUNT=2" in script
        assert "MDSDEV1=/dev/vdb" in script
        assert "MDSDEV2=/dev/vdc" in script
        assert "OSTCOUNT=3" in script
        assert "OSTDEV1=/dev/vdd" in script
        assert "OSTDEV2=/dev/vde" in script
        assert "OSTDEV3=/dev/vdf" in script

    def test_ost_only(self) -> None:
        """OST-only VMs get OSTDEV1=vdb (skipping the MDT range)."""
        script = self._capture_script(mdt=0, ost=2)
        assert "OSTCOUNT=2" in script
        assert "OSTDEV1=/dev/vdb" in script
        assert "OSTDEV2=/dev/vdc" in script
        assert "MDSCOUNT" not in script

    def test_disk_size_in_kb(self) -> None:
        """disk_size_bytes emits MDSSIZE/OSTSIZE in kilobytes."""
        # 500 MiB
        script = self._capture_script(
            mdt=1,
            ost=1,
            disk_size=500 * 1024 * 1024,
        )
        assert "MDSSIZE=512000" in script
        assert "OSTSIZE=512000" in script

    def test_no_size_when_zero(self) -> None:
        """disk_size=0 omits MDSSIZE/OSTSIZE entirely."""
        script = self._capture_script(mdt=1, ost=1, disk_size=0)
        assert "MDSSIZE" not in script
        assert "OSTSIZE" not in script

    def test_size_only_for_present_roles(self) -> None:
        """OSTSIZE is only written when there are OST disks."""
        script = self._capture_script(mdt=1, ost=0, disk_size=1024 * 1024)
        assert "MDSSIZE=1024" in script
        assert "OSTSIZE" not in script

    def test_markers_wrap_generated_block(self) -> None:
        """The generated snippet is wrapped in VM-disk sentinel markers."""
        script = self._capture_script(mdt=1, ost=1)
        assert "# --- VM disk configuration" in script
        assert "# --- END VM disk configuration" in script

    def test_script_deletes_old_block_before_append(self) -> None:
        """Regenerating idempotently: script sed-deletes the old block."""
        script = self._capture_script(mdt=1, ost=1)
        # Must sed out the old sentinel range before appending.
        assert "sed -i" in script
        assert "VM disk configuration" in script

    def test_debian_libdir(self) -> None:
        """debian os_family writes to /usr/lib/lustre (not /usr/lib64)."""
        script = self._capture_script(
            mdt=1,
            ost=1,
            os_family="debian",
        )
        assert "/usr/lib/lustre/tests/cfg/local.sh" in script
        assert "/usr/lib64/lustre" not in script

    def test_rhel_libdir(self) -> None:
        """rhel os_family writes to /usr/lib64/lustre."""
        script = self._capture_script(
            mdt=1,
            ost=1,
            os_family="rhel",
        )
        assert "/usr/lib64/lustre/tests/cfg/local.sh" in script

    def test_failure_raises_runtimeerror(self) -> None:
        """A non-zero ssh result surfaces as RuntimeError with stderr."""
        with patch(
            "ltvm_pkg.deploy.run_ssh",
            return_value=_fail(rc=1, stderr="no such dir"),
        ):
            with pytest.raises(RuntimeError, match="local.sh"):
                deploy.configure_test_disks("10.0.0.1", 1, 1)


# ── deploy_to_vm ─────────────────────────────────────────


class TestDeployToVm:
    """deploy_to_vm streams tar over ssh, runs depmod, and configures disks."""

    def test_missing_staging_raises(self, tmp_path: Path) -> None:
        """Nonexistent staging dir raises before spawning subprocesses."""
        vm = _make_vm()
        with pytest.raises(RuntimeError, match="Staging directory not found"):
            deploy.deploy_to_vm(vm, tmp_path / "nope")

    def test_tar_failure_raises(self, staging: Path) -> None:
        """subprocess nonzero rc surfaces as RuntimeError with output."""
        vm = _make_vm()
        with patch(
            "ltvm_pkg.deploy.subprocess.run",
            return_value=MagicMock(returncode=2, stdout="", stderr="tar: boom"),
        ):
            with pytest.raises(RuntimeError, match="tar deploy failed"):
                deploy.deploy_to_vm(vm, staging)

    def test_depmod_failure_raises(self, staging: Path) -> None:
        """A failed depmod after successful tar propagates with rc."""
        vm = _make_vm()
        with (
            patch(
                "ltvm_pkg.deploy.subprocess.run",
                return_value=_ok(),
            ),
            patch(
                "ltvm_pkg.deploy.run_ssh",
                return_value=_fail(rc=7, stderr="depmod: boom"),
            ),
        ):
            with pytest.raises(RuntimeError, match="depmod"):
                deploy.deploy_to_vm(vm, staging)

    def test_no_disks_skips_configure(self, staging: Path) -> None:
        """A VM with no data disks never calls configure_test_disks."""
        vm = _make_vm(mdt_disks=0, ost_disks=0)
        with (
            patch(
                "ltvm_pkg.deploy.subprocess.run",
                return_value=_ok(),
            ),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch("ltvm_pkg.deploy.configure_test_disks") as mock_cfg,
        ):
            deploy.deploy_to_vm(vm, staging)
            mock_cfg.assert_not_called()

    def test_disks_trigger_configure(self, staging: Path) -> None:
        """VMs with data disks forward topology to configure_test_disks."""
        vm = _make_vm(mdt_disks=1, ost_disks=2, disk_size=12345)
        with (
            patch(
                "ltvm_pkg.deploy.subprocess.run",
                return_value=_ok(),
            ),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch("ltvm_pkg.deploy.configure_test_disks") as mock_cfg,
        ):
            deploy.deploy_to_vm(vm, staging, os_family="rhel")
            mock_cfg.assert_called_once_with(
                vm.ip,
                1,
                2,
                12345,
                os_family="rhel",
            )

    def test_tar_command_targets_staging_and_vm_ip(self, staging: Path) -> None:
        """The bash tar|ssh pipeline references the staging dir and VM IP."""
        vm = _make_vm(ip="10.11.12.13")
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return _ok()

        with (
            patch("ltvm_pkg.deploy.subprocess.run", side_effect=fake_run),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
        ):
            deploy.deploy_to_vm(vm, staging)
        # bash -c "... tar cf - -C <staging> . | ... ssh ... root@<ip> ..."
        assert captured["args"][0] == "bash"
        assert captured["args"][1] == "-c"
        pipeline = captured["args"][2]
        assert f"-C {staging}" in pipeline or str(staging) in pipeline
        assert "root@10.11.12.13" in pipeline
        assert "tar cf -" in pipeline
        assert "tar xf -" in pipeline

    def test_userspace_only_skips_modules_and_depmod(
        self, staging: Path
    ) -> None:
        """userspace_only excludes lib/modules and runs ldconfig only."""
        vm = _make_vm()
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return _ok()

        ssh_cmds = []

        def fake_ssh(ip, cmd, timeout=60):
            ssh_cmds.append(cmd)
            return _ok()

        with (
            patch("ltvm_pkg.deploy.subprocess.run", side_effect=fake_run),
            patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_ssh),
        ):
            deploy.deploy_to_vm(vm, staging, userspace_only=True)
        assert "--exclude=./lib/modules" in captured["args"][2]
        # post-deploy should be ldconfig only (no depmod)
        assert ssh_cmds == ["ldconfig"]

    def test_full_deploy_runs_depmod(self, staging: Path) -> None:
        """Normal (kernel-module) deploy runs depmod -a && ldconfig."""
        vm = _make_vm()
        ssh_cmds = []

        def fake_ssh(ip, cmd, timeout=60):
            ssh_cmds.append(cmd)
            return _ok()

        with (
            patch("ltvm_pkg.deploy.subprocess.run", return_value=_ok()),
            patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_ssh),
        ):
            deploy.deploy_to_vm(vm, staging)
        assert ssh_cmds == ["depmod -a && ldconfig"]


# ── lustre_mount_vm ──────────────────────────────────────


class TestLustreMountVm:
    """lustre_mount_vm cleans up state then runs llmount.sh."""

    def test_vm_not_found_returns_not_found_exit(
        self,
        tmp_sockets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ltvm_pkg.vm_state import EXIT_NOT_FOUND

        rc = deploy.lustre_mount_vm("ghost", os_family="rhel")
        assert rc == EXIT_NOT_FOUND

    def test_mount_success_returns_zero(self, tmp_sockets: Path) -> None:
        """Happy path: cleanup + llmount.sh both return 0."""
        vm = _make_vm(name="mount-ok", ip="10.0.0.5")
        vm.save()
        with patch("ltvm_pkg.deploy.run_ssh", return_value=_ok(stdout="")):
            rc = deploy.lustre_mount_vm("mount-ok", os_family="rhel")
        assert rc == 0

    def test_mount_calls_cleanup_then_llmount(self, tmp_sockets: Path) -> None:
        """lustre_mount_vm runs llmountcleanup first, then llmount."""
        vm = _make_vm(name="mount-order", ip="10.0.0.6")
        vm.save()
        calls = []

        def fake_ssh(ip, cmd, timeout=0):
            calls.append(cmd)
            return _ok()

        with patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_ssh):
            deploy.lustre_mount_vm("mount-order", os_family="rhel")
        assert len(calls) == 2
        assert "llmountcleanup.sh" in calls[0]
        assert "dmsetup remove_all" in calls[0]
        assert "llmount.sh" in calls[1]
        assert "llmountcleanup.sh" not in calls[1]

    def test_mount_uses_debian_libdir(self, tmp_sockets: Path) -> None:
        """debian os_family passes /usr/lib/lustre into the mount commands."""
        vm = _make_vm(name="mount-deb", ip="10.0.0.7")
        vm.save()
        calls = []

        def fake_ssh(ip, cmd, timeout=0):
            calls.append(cmd)
            return _ok()

        with patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_ssh):
            deploy.lustre_mount_vm("mount-deb", os_family="debian")
        assert all("/usr/lib/lustre" in c for c in calls)
        assert all("/usr/lib64/lustre" not in c for c in calls)

    def test_mount_failure_returns_rc(
        self,
        tmp_sockets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A failing llmount.sh surfaces its rc from lustre_mount_vm."""
        vm = _make_vm(name="mount-fail", ip="10.0.0.8")
        vm.save()
        results = iter([_ok(), _fail(rc=5, stderr="mount broken")])
        with patch(
            "ltvm_pkg.deploy.run_ssh",
            side_effect=lambda *a, **k: next(results),
        ):
            rc = deploy.lustre_mount_vm("mount-fail", os_family="rhel")
        assert rc == 5

    def test_mount_ssh_exception_returns_error(self, tmp_sockets: Path) -> None:
        """A run_ssh exception becomes EXIT_ERROR (not a traceback)."""
        from ltvm_pkg.vm_state import EXIT_ERROR

        vm = _make_vm(name="mount-exc", ip="10.0.0.9")
        vm.save()
        with patch(
            "ltvm_pkg.deploy.run_ssh",
            side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=60),
        ):
            rc = deploy.lustre_mount_vm("mount-exc", os_family="rhel")
        assert rc == EXIT_ERROR


# --------------------------------------------------------------------------
# cmd_deploy: per-kernel staging resolution (lustre_test_vms_v2-eh9)
# --------------------------------------------------------------------------


class TestCmdDeployPerKernelStaging:
    """cmd_deploy resolves staging per-kernel and refuses when the
    legacy per-target staging exists but the per-kernel one doesn't.
    """

    def _setup_lustre_tree(self, build_path: Path) -> None:
        (build_path / "lustre").mkdir(parents=True)
        (build_path / "lnet").mkdir()
        (build_path / "configure.ac").write_text("")

    def test_resolves_under_kernel_subdir(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """The staging path used by cmd_deploy is keyed by kernel."""
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        self._setup_lustre_tree(build_path)
        # Seed a fresh per-kernel staging dir with a .ko so the build
        # step is skipped and we can assert the path was used.
        from ltvm_pkg.lustre_build import staging_path

        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("5.14.0-foo\n")

        vm = _make_vm(name="co1-eh9", ip="192.168.100.60")
        vm.os_id = "rocky9"
        vm.save()

        tc = MagicMock()
        tc.os_family = "rhel"
        tc.arch = "x86_64"
        tc.resolve_kernel.side_effect = lambda k: k or "5.14-rhel9.7"

        captured: dict = {}

        def fake_deploy_to_vm(vm_arg, staging_arg, **kwargs):
            captured["staging"] = Path(staging_arg)

        with (
            patch.object(cli_mod, "_require_root", return_value=None),
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch(
                "ltvm_pkg.cli.deploy_to_vm", side_effect=fake_deploy_to_vm
            ),
            patch("subprocess.run") as run_mock,
        ):
            # If _staging_is_fresh does get invoked it calls `find`,
            # which we short-circuit to return "fresh" (empty stdout).
            run_mock.return_value = MagicMock(returncode=0, stdout="")
            args = ap.Namespace(
                vm="co1-eh9",
                build=str(build_path),
                mount=False,
                target=None,
                kernel=None,
                json=False,
                userspace_only=False,
                force_compat=False,
            )
            cli_mod.cmd_deploy(args)

        assert "staging" in captured
        assert captured["staging"] == staging
        assert captured["staging"].name == "5.14-rhel9.7"

    def test_legacy_staging_triggers_clear_error(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """Legacy per-target staging present + per-kernel missing
        errors with the build-lustre hint instead of silently shipping
        the legacy modules."""
        import argparse as ap

        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        self._setup_lustre_tree(build_path)
        legacy = staging_path(
            build_path, "rocky9", arch="x86_64", kernel=None
        )
        legacy.mkdir(parents=True)
        (legacy / "old.ko").write_text("")

        vm = _make_vm(name="co1-eh9-legacy", ip="192.168.100.61")
        vm.os_id = "rocky9"
        vm.save()

        tc = MagicMock()
        tc.os_family = "rhel"
        tc.arch = "x86_64"
        tc.resolve_kernel.side_effect = lambda k: k or "5.14-rhel9.7"

        with (
            patch.object(cli_mod, "_require_root", return_value=None),
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock,
        ):
            args = ap.Namespace(
                vm="co1-eh9-legacy",
                build=str(build_path),
                mount=False,
                target=None,
                kernel=None,
                json=False,
                userspace_only=False,
                force_compat=False,
            )
            rc = cli_mod.cmd_deploy(args)

        from ltvm_pkg.cli import EXIT_ERROR

        assert rc == EXIT_ERROR
        assert not deploy_mock.called
