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

        build_path = tmp_path / "lustre-release"
        self._setup_lustre_tree(build_path)
        legacy = build_path / ".ltvm-staging" / "rocky9" / "x86_64"
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
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "_gate_lustre_validation"),
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


# --------------------------------------------------------------------------
# cmd_deploy: full decision-tree coverage for refactor safety
# --------------------------------------------------------------------------


def _setup_lustre_tree(build_path: Path) -> None:
    (build_path / "lustre").mkdir(parents=True)
    (build_path / "lnet").mkdir()
    (build_path / "configure.ac").write_text("")


def _deploy_args(
    vm: str = "co-test",
    build: str | None = None,
    *,
    mount: bool = False,
    target: str | None = None,
    kernel: str | None = None,
    json: bool = False,
    userspace_only: bool = False,
    force_compat: bool = False,
) -> "argparse.Namespace":
    import argparse as ap

    return ap.Namespace(
        vm=vm,
        build=build,
        mount=mount,
        target=target,
        kernel=kernel,
        json=json,
        userspace_only=userspace_only,
        force_compat=force_compat,
    )


def _stub_tc() -> MagicMock:
    """Standard TargetConfig stub used by cmd_deploy tests."""
    tc = MagicMock()
    tc.os_family = "rhel"
    tc.arch = "x86_64"
    tc.resolve_kernel.side_effect = lambda k: k or "5.14-rhel9.7"
    tc.kernel_output_dir.side_effect = lambda kernel=None: Path(
        f"/fake/kernels/{kernel or '5.14-rhel9.7'}"
    )
    return tc


class TestCmdDeployErrorPaths:
    """Error-path branches of cmd_deploy that gate downstream work."""

    def test_vm_not_found_returns_error(self, tmp_sockets: Path) -> None:
        """A missing VM exits with EXIT_ERROR, never touches build."""
        from ltvm_pkg import cli as cli_mod

        args = _deploy_args(vm="ghost", build=None)
        with patch("ltvm_pkg.cli.TargetConfig") as tc_mock:
            rc = cli_mod.cmd_deploy(args)
        assert rc == 1
        # TargetConfig is never even instantiated when VM lookup fails.
        tc_mock.assert_not_called()

    def test_no_target_and_no_os_id_errors(
        self, tmp_sockets: Path
    ) -> None:
        """VM with no os_id and no --target gives a clear error."""
        from ltvm_pkg import cli as cli_mod

        vm = _make_vm(name="co1-no-target", ip="10.0.0.10")
        vm.os_id = ""  # no recorded OS
        vm.save()

        args = _deploy_args(vm="co1-no-target", build=None)
        rc = cli_mod.cmd_deploy(args)
        assert rc == 1

    def test_unknown_target_yields_targetconfig_error(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """A ValueError out of TargetConfig is surfaced as a friendly error."""
        from ltvm_pkg import cli as cli_mod

        vm = _make_vm(name="co1-unknown", ip="10.0.0.11")
        vm.os_id = "bogusos"
        vm.save()

        args = _deploy_args(vm="co1-unknown", build=str(tmp_path))
        with patch.object(
            cli_mod, "TargetConfig", side_effect=ValueError("nope")
        ):
            rc = cli_mod.cmd_deploy(args)
        assert rc == 1

    def test_build_path_missing_errors(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """An explicit --build that points at a nonexistent dir errors."""
        from ltvm_pkg import cli as cli_mod

        vm = _make_vm(name="co1-nodir", ip="10.0.0.12")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(
            vm="co1-nodir", build=str(tmp_path / "does-not-exist")
        )
        with patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()):
            rc = cli_mod.cmd_deploy(args)
        assert rc == 1

    def test_build_path_not_lustre_tree_errors(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """A --build dir that lacks configure.ac/lustre/lnet fails fast."""
        from ltvm_pkg import cli as cli_mod

        bad = tmp_path / "not-a-lustre-tree"
        bad.mkdir()

        vm = _make_vm(name="co1-bad", ip="10.0.0.13")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(vm="co1-bad", build=str(bad))
        with patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()):
            rc = cli_mod.cmd_deploy(args)
        assert rc == 1

    def test_userspace_only_no_staging_errors(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """--userspace-only with no pre-existing staging exits with error."""
        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)

        vm = _make_vm(name="co1-uspace", ip="10.0.0.14")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(
            vm="co1-uspace", build=str(build_path), userspace_only=True
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock,
        ):
            rc = cli_mod.cmd_deploy(args)
        assert rc == 1
        deploy_mock.assert_not_called()

    def test_deploy_to_vm_runtimeerror_returns_error(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """deploy_to_vm raising RuntimeError flows back as EXIT_ERROR."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")

        vm = _make_vm(name="co1-rterr", ip="10.0.0.15")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(vm="co1-rterr", build=str(build_path))
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch(
                "ltvm_pkg.cli.deploy_to_vm",
                side_effect=RuntimeError("ssh died"),
            ),
            patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
        ):
            rc = cli_mod.cmd_deploy(args)
        assert rc == 1


class TestCmdDeployUserspaceOnly:
    """--userspace-only happy path skips kernel modules and forwards flag."""

    def test_userspace_only_forwards_flag_to_deploy(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")

        vm = _make_vm(name="co1-uspace-ok", ip="10.0.0.16")
        vm.os_id = "rocky9"
        vm.save()

        captured: dict = {}

        def fake_deploy_to_vm(vm_arg, staging_arg, **kwargs):
            captured.update(kwargs)
            captured["staging"] = Path(staging_arg)

        args = _deploy_args(
            vm="co1-uspace-ok",
            build=str(build_path),
            userspace_only=True,
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch(
                "ltvm_pkg.cli.deploy_to_vm",
                side_effect=fake_deploy_to_vm,
            ),
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 0
        assert captured.get("userspace_only") is True
        assert captured["staging"] == staging


class TestCmdDeployBundledSnapshot:
    """Bundled-snapshot detection and rsync mirroring path."""

    def _make_snapshot(
        self, tc_output_dir: Path, kernel: str = "5.14-rhel9.7"
    ) -> Path:
        """Lay down a snapshot dir with the marker file."""
        snap = tc_output_dir / "kernels" / kernel / "lustre-artifacts"
        snap.mkdir(parents=True)
        (snap / ".ltvm-snapshot.json").write_text("{}")
        # Real snapshot would contain usr/, lib/modules/, etc.
        (snap / "usr").mkdir()
        (snap / "lib").mkdir()
        (snap / "marker.ko").write_text("from-snapshot")
        return snap

    def test_bundled_snapshot_used_when_no_build_arg(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """No --build + bundled snapshot present -> snapshot is the source."""
        from ltvm_pkg import cli as cli_mod

        tc_out = tmp_path / "tc-out"
        snap = self._make_snapshot(tc_out)

        vm = _make_vm(name="co1-bundled", ip="10.0.0.17")
        vm.os_id = "rocky9"
        vm.save()

        tc = _stub_tc()
        tc.output_dir = tc_out

        captured: dict = {}

        def fake_deploy_to_vm(vm_arg, staging_arg, **kwargs):
            captured["staging"] = Path(staging_arg)

        rsync_calls: list = []

        def fake_run(cmd, *args, **kwargs):
            rsync_calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        args = _deploy_args(vm="co1-bundled", build=None)
        # Run from snapshot dir so cwd-fallback wouldn't trigger; but
        # since the snapshot marker exists, snapshot path wins regardless.
        import os as _os

        old = _os.getcwd()
        try:
            _os.chdir(tmp_path)
            with (
                patch.object(cli_mod, "TargetConfig", return_value=tc),
                patch(
                    "ltvm_pkg.cli.deploy_to_vm",
                    side_effect=fake_deploy_to_vm,
                ),
                patch("subprocess.run", side_effect=fake_run),
            ):
                rc = cli_mod.cmd_deploy(args)
        finally:
            _os.chdir(old)

        assert rc == 0
        # First subprocess.run is the rsync mirror
        assert rsync_calls, "expected rsync to be invoked"
        rsync = rsync_calls[0]
        assert rsync[0] == "rsync"
        assert "--delete" in rsync
        assert str(snap) + "/" in rsync
        # Staging should be in build_path tree, not under tc.output_dir.
        # build_path was set to the snapshot itself, so staging lives
        # under snapshot/.ltvm-staging/.
        staging = captured["staging"]
        assert ".ltvm-staging" in str(staging)
        assert staging.name == "5.14-rhel9.7"

    def test_bundled_snapshot_rsync_failure_returns_error(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc_out = tmp_path / "tc-out"
        self._make_snapshot(tc_out)

        vm = _make_vm(name="co1-rsync-fail", ip="10.0.0.18")
        vm.os_id = "rocky9"
        vm.save()

        tc = _stub_tc()
        tc.output_dir = tc_out

        def fake_run(cmd, *args, **kwargs):
            return MagicMock(returncode=23, stdout="", stderr="rsync: nope")

        args = _deploy_args(vm="co1-rsync-fail", build=None)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock,
            patch("subprocess.run", side_effect=fake_run),
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 1
        deploy_mock.assert_not_called()

    def test_bundled_snapshot_skips_lustre_tree_validation(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """Snapshot DESTDIR layout has no configure.ac -- must not error."""
        from ltvm_pkg import cli as cli_mod

        tc_out = tmp_path / "tc-out"
        snap = self._make_snapshot(tc_out)

        # Sanity: the snapshot dir really does NOT look like a lustre tree.
        assert not (snap / "configure.ac").exists()
        assert not (snap / "lustre").exists()

        vm = _make_vm(name="co1-snap-ok", ip="10.0.0.19")
        vm.os_id = "rocky9"
        vm.save()

        tc = _stub_tc()
        tc.output_dir = tc_out

        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ),
        ):
            args = _deploy_args(vm="co1-snap-ok", build=None)
            rc = cli_mod.cmd_deploy(args)

        # Should succeed (rc=0), not bail with "not a Lustre source tree".
        assert rc == 0


class TestCmdDeployVariantPropagation:
    """Variant-aware staging path resolution."""

    def test_mofed_variant_routes_to_mofed_staging(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """A VM with variant=mofed-24 deploys from the mofed-24 staging dir."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path,
            "rocky9",
            arch="x86_64",
            kernel="5.14-rhel9.7",
            variant="mofed-24",
        )
        staging.mkdir(parents=True)
        (staging / "ko2iblnd.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")

        vm = _make_vm(name="co1-mofed", ip="10.0.0.20")
        vm.os_id = "rocky9"
        vm.variant = "mofed-24"
        vm.save()

        captured: dict = {}

        def fake_deploy_to_vm(vm_arg, staging_arg, **kwargs):
            captured["staging"] = Path(staging_arg)

        args = _deploy_args(vm="co1-mofed", build=str(build_path))
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()) as tc_mock,
            patch(
                "ltvm_pkg.cli.deploy_to_vm",
                side_effect=fake_deploy_to_vm,
            ),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 0
        # TargetConfig must be invoked with variant=mofed-24
        kwargs = tc_mock.call_args.kwargs
        assert kwargs.get("variant") == "mofed-24"
        # Staging path is .../mofed-24/ (variant trailing dir).
        assert captured["staging"].name == "mofed-24"
        assert captured["staging"].parent.name == "5.14-rhel9.7"


class TestCmdDeployKernelMismatch:
    """VM kernel vs target default kernel routing."""

    def test_vm_kernel_overrides_target_default_for_staging(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """A VM booted on a non-default kernel gets staging keyed to that kernel."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        # Note: target default is 5.14-rhel9.7; VM is on 5.14-rhel9.5.
        staging = staging_path(
            build_path,
            "rocky9",
            arch="x86_64",
            kernel="5.14-rhel9.5",
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")

        vm = _make_vm(name="co1-altkern", ip="10.0.0.21")
        vm.os_id = "rocky9"
        # vm.kernel is a path-like value -- the parent dir name is the
        # kernel key. Mirror what create writes: <kerndir>/vmlinux.
        vm.kernel = "/fake/artifacts/rocky9/x86_64/kernels/5.14-rhel9.5/vmlinux"
        vm.save()

        captured: dict = {}

        def fake_deploy_to_vm(vm_arg, staging_arg, **kwargs):
            captured["staging"] = Path(staging_arg)

        args = _deploy_args(vm="co1-altkern", build=str(build_path))
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch(
                "ltvm_pkg.cli.deploy_to_vm",
                side_effect=fake_deploy_to_vm,
            ),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 0
        # Staging dir is keyed by the VM's kernel (rhel9.5), not the
        # target default (rhel9.7).
        assert captured["staging"].name == "5.14-rhel9.5"

    def test_vm_kernel_forwarded_to_build_lustre_subprocess(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """When build is invoked, --kernel <vm_kernel> is forwarded."""
        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        # No fresh staging -> build_lustre is invoked.

        vm = _make_vm(name="co1-fwd", ip="10.0.0.22")
        vm.os_id = "rocky9"
        vm.kernel = "/fake/artifacts/rocky9/x86_64/kernels/5.14-rhel9.5/vmlinux"
        vm.save()

        run_calls: list = []

        def fake_run(cmd, *args, **kwargs):
            run_calls.append(cmd)
            # Build returns 1 so we abort cleanly after capturing the cmd.
            return MagicMock(returncode=1, stdout="", stderr="")

        args = _deploy_args(vm="co1-fwd", build=str(build_path))
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch.object(cli_mod, "_gate_lustre_validation"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 1
        # find the ltvm build lustre subprocess
        build_calls = [c for c in run_calls if isinstance(c, list)
                       and len(c) >= 3 and c[0:3] == ["ltvm", "build", "lustre"]]
        # Could also be sudo-prefixed
        if not build_calls:
            build_calls = [c for c in run_calls if isinstance(c, list)
                           and "ltvm" in c and "build" in c
                           and "lustre" in c]
        assert build_calls, f"build subprocess not found in {run_calls}"
        cmd = build_calls[0]
        assert "--kernel" in cmd
        idx = cmd.index("--kernel")
        assert cmd[idx + 1] == "5.14-rhel9.5"
        assert "--arch" in cmd
        assert cmd[cmd.index("--arch") + 1] == "x86_64"


class TestCmdDeployForceCompat:
    """--force-compat silences refuse but not hard error in the deploy path."""

    def test_force_compat_threaded_into_gate(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """force_compat=True is forwarded to _gate_lustre_validation."""
        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)

        vm = _make_vm(name="co1-fc", ip="10.0.0.23")
        vm.os_id = "rocky9"
        vm.save()

        gate_calls: list = []

        def fake_gate(tc, lustre_tree, *, force, kernel_build_tree=None):
            gate_calls.append({"force": force})

        args = _deploy_args(
            vm="co1-fc", build=str(build_path), force_compat=True
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch.object(cli_mod, "_gate_lustre_validation", side_effect=fake_gate),
            # Build subprocess fails -> stops before deploy.
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=1, stdout=""),
            ),
        ):
            cli_mod.cmd_deploy(args)

        assert gate_calls, "_gate_lustre_validation was not called"
        assert gate_calls[0]["force"] is True

    def test_force_compat_does_not_silence_hard_error(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """--force-compat overrides 'refuse' but NOT 'error' (hard parse fail)."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_compat import ValidationResult

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)

        vm = _make_vm(name="co1-fc-hard", ip="10.0.0.24")
        vm.os_id = "rocky9"
        vm.save()

        hard_err = ValidationResult(
            status="error",
            mode=None,
            kernel_version=None,
            matched_in=None,
            message="parse failure",
        )

        args = _deploy_args(
            vm="co1-fc-hard", build=str(build_path), force_compat=True
        )
        # _gate_lustre_validation raises SystemExit on "error" even with
        # force.  cmd_deploy is dispatched directly (not through _vm_call)
        # so SystemExit propagates -- this is intentional, since the
        # caller is the top-level dispatch table which converts it to rc.
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch.object(
                cli_mod, "validate_target", return_value=hard_err
            ),
            patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock,
        ):
            with pytest.raises(SystemExit) as exc:
                cli_mod.cmd_deploy(args)

        assert exc.value.code == 1
        deploy_mock.assert_not_called()


class TestCmdDeployMountAndKver:
    """--mount triggers lustre_mount_vm; staging meta drives kver recording."""

    def test_mount_invokes_lustre_mount_vm(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")

        vm = _make_vm(name="co1-mount", ip="10.0.0.25")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(
            vm="co1-mount", build=str(build_path), mount=True
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
            patch("ltvm_pkg.cli.lustre_mount_vm", return_value=0) as mount_mock,
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 0
        mount_mock.assert_called_once_with("co1-mount", "rhel")

    def test_mount_failure_propagates(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")

        vm = _make_vm(name="co1-mount-fail", ip="10.0.0.26")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(
            vm="co1-mount-fail", build=str(build_path), mount=True
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
            patch("ltvm_pkg.cli.lustre_mount_vm", return_value=7),
        ):
            rc = cli_mod.cmd_deploy(args)
        assert rc == 7

    def test_kver_from_staging_meta_recorded(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """The kver from .ltvm-staging-meta.json is recorded on the VM."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")
        (staging / ".ltvm-staging-meta.json").write_text(
            '{"kernel_version": "5.14.0-from-staging"}'
        )

        vm = _make_vm(name="co1-kver", ip="10.0.0.27")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(vm="co1-kver", build=str(build_path))
        update_calls: list = []
        from ltvm_pkg.vm_state import VMInfo as _VMI

        orig = _VMI.update_deploy

        def capture(self, epoch, build_path, kver):
            update_calls.append({"kver": kver, "build_path": build_path})
            return orig(self, epoch, build_path, kver)

        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
            patch.object(_VMI, "update_deploy", capture),
        ):
            cli_mod.cmd_deploy(args)

        assert update_calls
        assert update_calls[0]["kver"] == "5.14.0-from-staging"

    def test_update_deploy_permissionerror_warns_not_fail(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """PermissionError on metadata save is a warning, not failure."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path
        from ltvm_pkg.vm_state import VMInfo as _VMI

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")

        vm = _make_vm(name="co1-perm", ip="10.0.0.28")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(vm="co1-perm", build=str(build_path))
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
            patch.object(
                _VMI, "update_deploy", side_effect=PermissionError("nope")
            ),
        ):
            rc = cli_mod.cmd_deploy(args)
        # Still successful: deploy itself worked, only the bookkeeping failed.
        assert rc == 0


# --------------------------------------------------------------------------
# cmd_llmount: thin wrapper over vm_commands.cmd_llmount
# --------------------------------------------------------------------------


class TestCmdLlmount:
    """cmd_llmount returns SystemExit codes from the underlying handler."""

    def test_no_cleanup_passes_flag_through(
        self, tmp_sockets: Path
    ) -> None:
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        captured: dict = {}

        def fake(args):
            captured["cleanup"] = getattr(args, "cleanup", None)
            captured["vm"] = args.vm
            # mimic vm_commands.cmd_llmount calling sys.exit on success
            raise SystemExit(0)

        with patch("ltvm_pkg.vm_commands.cmd_llmount", side_effect=fake):
            args = ap.Namespace(
                vm="co1-mnt", json=False, timeout=300, cleanup=False
            )
            rc = cli_mod.cmd_llmount(args)

        assert rc == 0
        assert captured["cleanup"] is False
        assert captured["vm"] == "co1-mnt"

    def test_cleanup_flag_propagates(self, tmp_sockets: Path) -> None:
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        captured: dict = {}

        def fake(args):
            captured["cleanup"] = getattr(args, "cleanup", None)
            raise SystemExit(0)

        with patch("ltvm_pkg.vm_commands.cmd_llmount", side_effect=fake):
            args = ap.Namespace(
                vm="co1-clean", json=False, timeout=300, cleanup=True
            )
            rc = cli_mod.cmd_llmount(args)
        assert rc == 0
        assert captured["cleanup"] is True

    def test_systemexit_nonzero_propagates_rc(
        self, tmp_sockets: Path
    ) -> None:
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        with patch(
            "ltvm_pkg.vm_commands.cmd_llmount",
            side_effect=SystemExit(5),
        ):
            args = ap.Namespace(
                vm="co1-fail", json=False, timeout=300, cleanup=False
            )
            rc = cli_mod.cmd_llmount(args)
        assert rc == 5

    def test_systemexit_none_code_maps_to_exit_error(
        self, tmp_sockets: Path
    ) -> None:
        """A bare `sys.exit()` (no code) maps to EXIT_ERROR == 1."""
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        with patch(
            "ltvm_pkg.vm_commands.cmd_llmount",
            side_effect=SystemExit(None),
        ):
            args = ap.Namespace(
                vm="co1-bare", json=False, timeout=300, cleanup=False
            )
            rc = cli_mod.cmd_llmount(args)
        assert rc == 1

    def test_normal_return_yields_exit_ok(self, tmp_sockets: Path) -> None:
        """If the underlying handler returns normally (no SystemExit), rc=0."""
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        with patch(
            "ltvm_pkg.vm_commands.cmd_llmount", return_value=None
        ):
            args = ap.Namespace(
                vm="co1-ret", json=False, timeout=300, cleanup=False
            )
            rc = cli_mod.cmd_llmount(args)
        assert rc == 0
