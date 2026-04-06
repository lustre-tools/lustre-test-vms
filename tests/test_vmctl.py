"""Tests for lib/vmctl.py -- subprocess client for qemu/vm.py."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.vmctl import (
    RunResult,
    _deploy_kernel_modules,
    _run,
    _run_impl,
    cluster_create,
    cluster_deploy,
    cluster_destroy,
    cluster_exec,
    cluster_status,
    deploy,
    vm_create,
    vm_destroy,
    vm_dmesg,
    vm_ensure,
    vm_exec,
    vm_list,
    vm_log,
    vm_restart,
    vm_start,
    vm_status,
    vm_stop,
)


class TestRunImpl:
    def test_success(self) -> None:
        result = _run_impl(["echo", "hello"])
        assert result["ok"] is True
        assert result["returncode"] == 0
        assert "hello" in result["output"]

    def test_failure(self) -> None:
        result = _run_impl(["false"])
        assert result["ok"] is False
        assert result["returncode"] != 0

    def test_timeout(self) -> None:
        result = _run_impl(["sleep", "10"], timeout=1)
        assert result["ok"] is False
        assert result["returncode"] == 3
        assert "timed out" in result["output"]

    def test_stderr_combined(self) -> None:
        result = _run_impl(["bash", "-c", "echo out; echo err >&2"])
        assert "out" in result["output"]
        assert "err" in result["output"]

    def test_stderr_only(self) -> None:
        result = _run_impl(["bash", "-c", "echo err >&2"])
        assert "err" in result["output"]

    def test_empty_output(self) -> None:
        result = _run_impl(["true"])
        assert result["ok"] is True
        assert result["output"] == ""

    def test_strips_trailing_newlines(self) -> None:
        result = _run_impl(["echo", "hello"])
        assert not result["output"].endswith("\n")


class TestRunSudo:
    @patch("lib.vmctl.subprocess.run")
    def test_prepends_sudo(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        _run(["vm.py", "list"])
        args = mock_run.call_args[0][0]
        assert args[0] == "sudo"
        assert args[1] == "vm.py"


class TestVmCreate:
    @patch("lib.vmctl._run")
    def test_basic_create(self, mock: MagicMock) -> None:
        mock.return_value: RunResult = {
            "ok": True,
            "output": "",
            "returncode": 0,
        }
        vm_create("test-vm")
        cmd = mock.call_args[0][0]
        assert "create" in cmd
        assert "--name" in cmd
        assert "test-vm" in cmd

    @patch("lib.vmctl._run")
    def test_with_disks(self, mock: MagicMock) -> None:
        mock.return_value: RunResult = {
            "ok": True,
            "output": "",
            "returncode": 0,
        }
        vm_create("test-vm", mdt_disks=1, ost_disks=3)
        cmd = mock.call_args[0][0]
        assert "--mdt-disks" in cmd
        assert "1" in cmd
        assert "--ost-disks" in cmd
        assert "3" in cmd

    @patch("lib.vmctl._run")
    def test_no_disks_flags_omitted(self, mock: MagicMock) -> None:
        mock.return_value: RunResult = {
            "ok": True,
            "output": "",
            "returncode": 0,
        }
        vm_create("test-vm")
        cmd = mock.call_args[0][0]
        assert "--mdt-disks" not in cmd
        assert "--ost-disks" not in cmd


class TestVmEnsure:
    @patch("lib.vmctl._run")
    def test_ensure_cmd(self, mock: MagicMock) -> None:
        mock.return_value: RunResult = {
            "ok": True,
            "output": "",
            "returncode": 0,
        }
        vm_ensure("test-vm", vcpus=4, mem=8192)
        cmd = mock.call_args[0][0]
        assert "ensure" in cmd
        assert "test-vm" in cmd
        assert "4" in cmd
        assert "8192" in cmd

    @patch("lib.vmctl._run")
    def test_ensure_with_disks(self, mock: MagicMock) -> None:
        mock.return_value: RunResult = {
            "ok": True,
            "output": "",
            "returncode": 0,
        }
        vm_ensure("test-vm", mdt_disks=1, ost_disks=3)
        cmd = mock.call_args[0][0]
        assert "--mdt-disks" in cmd
        assert "1" in cmd
        assert "--ost-disks" in cmd
        assert "3" in cmd


class TestVmExec:
    @patch("lib.vmctl._run")
    def test_exec_with_timeout(self, mock: MagicMock) -> None:
        mock.return_value: RunResult = {
            "ok": True,
            "output": "result",
            "returncode": 0,
        }
        vm_exec("test-vm", "echo hello", timeout=60)
        cmd = mock.call_args[0][0]
        assert "exec" in cmd
        assert "--timeout" in cmd
        assert "60" in cmd
        assert "test-vm" in cmd
        assert "echo hello" in cmd
        # Outer timeout should be timeout+30
        assert mock.call_args[1]["timeout"] == 90


class TestVmExecDefaults:
    @patch("lib.vmctl._run")
    def test_default_timeout_120(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_exec("test-vm", "echo hello")
        cmd = mock.call_args[0][0]
        assert "120" in cmd
        assert mock.call_args[1]["timeout"] == 150


class TestVmCreateDefaults:
    @patch("lib.vmctl._run")
    def test_default_vcpus_and_mem(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_create("test-vm")
        cmd = mock.call_args[0][0]
        assert "2" in cmd
        assert "4096" in cmd


class TestVmEnsureDefaults:
    @patch("lib.vmctl._run")
    def test_default_vcpus_and_mem(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_ensure("test-vm")
        cmd = mock.call_args[0][0]
        assert "2" in cmd
        assert "4096" in cmd

    @patch("lib.vmctl._run")
    def test_no_disks_flags_omitted(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_ensure("test-vm")
        cmd = mock.call_args[0][0]
        assert "--mdt-disks" not in cmd
        assert "--ost-disks" not in cmd


class TestVmCreateNonDefaultDisks:
    @patch("lib.vmctl._run")
    def test_mdt_only(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_create("test-vm", mdt_disks=2)
        cmd = mock.call_args[0][0]
        assert "--mdt-disks" in cmd
        idx = cmd.index("--mdt-disks")
        assert cmd[idx + 1] == "2"
        assert "--ost-disks" not in cmd

    @patch("lib.vmctl._run")
    def test_ost_only(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_create("test-vm", ost_disks=5)
        cmd = mock.call_args[0][0]
        assert "--ost-disks" in cmd
        idx = cmd.index("--ost-disks")
        assert cmd[idx + 1] == "5"
        assert "--mdt-disks" not in cmd

    @patch("lib.vmctl._run")
    def test_both_mdt_and_ost(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_create("test-vm", mdt_disks=2, ost_disks=4)
        cmd = mock.call_args[0][0]
        mdt_idx = cmd.index("--mdt-disks")
        ost_idx = cmd.index("--ost-disks")
        assert cmd[mdt_idx + 1] == "2"
        assert cmd[ost_idx + 1] == "4"


class TestVmEnsureNonDefaultDisks:
    @patch("lib.vmctl._run")
    def test_mdt_only(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_ensure("test-vm", mdt_disks=3)
        cmd = mock.call_args[0][0]
        idx = cmd.index("--mdt-disks")
        assert cmd[idx + 1] == "3"
        assert "--ost-disks" not in cmd

    @patch("lib.vmctl._run")
    def test_ost_only(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_ensure("test-vm", ost_disks=6)
        cmd = mock.call_args[0][0]
        idx = cmd.index("--ost-disks")
        assert cmd[idx + 1] == "6"
        assert "--mdt-disks" not in cmd

    @patch("lib.vmctl._run")
    def test_both_mdt_and_ost(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_ensure("test-vm", mdt_disks=2, ost_disks=4)
        cmd = mock.call_args[0][0]
        mdt_idx = cmd.index("--mdt-disks")
        ost_idx = cmd.index("--ost-disks")
        assert cmd[mdt_idx + 1] == "2"
        assert cmd[ost_idx + 1] == "4"

    @patch("lib.vmctl._run")
    def test_custom_vcpus_and_mem(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_ensure("test-vm", vcpus=8, mem=16384)
        cmd = mock.call_args[0][0]
        assert "8" in cmd
        assert "16384" in cmd


class TestVmList:
    @patch("lib.vmctl._run")
    def test_plain_list(self, mock: MagicMock) -> None:
        mock.return_value: RunResult = {
            "ok": True,
            "output": "",
            "returncode": 0,
        }
        vm_list()
        cmd = mock.call_args[0][0]
        assert "list" in cmd
        assert "--json" not in cmd

    @patch("lib.vmctl._run")
    def test_json_list(self, mock: MagicMock) -> None:
        mock.return_value: RunResult = {
            "ok": True,
            "output": "[]",
            "returncode": 0,
        }
        vm_list(json_output=True)
        cmd = mock.call_args[0][0]
        assert "--json" in cmd


class TestVmStatus:
    @patch("lib.vmctl._run")
    def test_status_with_json(self, mock: MagicMock) -> None:
        mock.return_value: RunResult = {
            "ok": True,
            "output": "{}",
            "returncode": 0,
        }
        vm_status("test-vm", json_output=True)
        cmd = mock.call_args[0][0]
        assert "status" in cmd
        assert "--json" in cmd
        assert "test-vm" in cmd


_OK: RunResult = {"ok": True, "output": "", "returncode": 0}
_FAIL: RunResult = {"ok": False, "output": "error", "returncode": 1}


class TestVmDestroy:
    @patch("lib.vmctl._run")
    def test_destroy(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_destroy("my-vm")
        cmd = mock.call_args[0][0]
        assert "destroy" in cmd
        assert "my-vm" in cmd


class TestVmStart:
    @patch("lib.vmctl._run")
    def test_start(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_start("my-vm")
        cmd = mock.call_args[0][0]
        assert "start" in cmd
        assert "my-vm" in cmd


class TestVmStop:
    @patch("lib.vmctl._run")
    def test_stop(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_stop("my-vm")
        cmd = mock.call_args[0][0]
        assert "stop" in cmd
        assert "my-vm" in cmd


class TestVmRestart:
    @patch("lib.vmctl._run")
    def test_restart(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_restart("my-vm")
        cmd = mock.call_args[0][0]
        assert "restart" in cmd
        assert "my-vm" in cmd


class TestVmLog:
    @patch("lib.vmctl._run")
    def test_log_default_lines(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_log("my-vm")
        cmd = mock.call_args[0][0]
        assert "log" in cmd
        assert "my-vm" in cmd
        assert "50" in cmd

    @patch("lib.vmctl._run")
    def test_log_custom_lines(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_log("my-vm", lines=200)
        cmd = mock.call_args[0][0]
        assert "200" in cmd


class TestVmDmesg:
    @patch("lib.vmctl._run")
    def test_dmesg_default_tail(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_dmesg("my-vm")
        cmd = mock.call_args[0][0]
        assert "dmesg" in cmd
        assert "--tail" in cmd
        assert "100" in cmd
        assert "my-vm" in cmd

    @patch("lib.vmctl._run")
    def test_dmesg_custom_tail(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        vm_dmesg("my-vm", tail=50)
        cmd = mock.call_args[0][0]
        assert "50" in cmd


class TestDeploy:
    @patch("lib.vmctl._run")
    def test_plain_deploy(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        deploy("my-vm", build_path="/some/build")
        cmd = mock.call_args[0][0]
        assert "--vm" in cmd
        assert "my-vm" in cmd
        assert "--build" in cmd
        assert "--mount" not in cmd

    @patch("lib.vmctl._run")
    def test_deploy_with_mount(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        deploy("my-vm", build_path="/some/build", mount=True)
        cmd = mock.call_args[0][0]
        assert "--mount" in cmd

    @patch("lib.vmctl._run")
    def test_deploy_kernel_modules_no_lib_modules(
        self, mock: MagicMock
    ) -> None:
        """kernel_modules path has no lib/modules subdir -> skips module deploy."""
        mock.return_value = _OK
        with tempfile.TemporaryDirectory() as tmpdir:
            # tmpdir exists but has no lib/modules subdir
            deploy("my-vm", build_path="/some/build", kernel_modules=tmpdir)
        # _run should be called once for the main deploy only
        assert mock.call_count == 1
        cmd = mock.call_args[0][0]
        assert "--vm" in cmd

    @patch("lib.vmctl._deploy_kernel_modules")
    @patch("lib.vmctl._run")
    def test_deploy_kernel_modules_failure_returns_early(
        self, mock_run: MagicMock, mock_deploy_mods: MagicMock
    ) -> None:
        """If _deploy_kernel_modules fails, deploy returns early."""
        mock_run.return_value = _OK
        mock_deploy_mods.return_value = _FAIL
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_mods = Path(tmpdir) / "lib" / "modules"
            lib_mods.mkdir(parents=True)
            result = deploy(
                "my-vm",
                build_path="/some/build",
                kernel_modules=tmpdir,
            )
        assert result["ok"] is False
        # main _run (deploy-lustre.sh) must not have been called
        mock_run.assert_not_called()


class TestDeployKernelModules:
    @patch("lib.vmctl._run")
    def test_empty_versions_dir(self, mock_run: MagicMock) -> None:
        """No version subdirs -> return failure without calling _run."""
        mock_run.return_value = _OK
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_mods = Path(tmpdir)
            result = _deploy_kernel_modules("my-vm", lib_mods)
        assert result["ok"] is False
        assert "No version dirs" in result["output"]
        mock_run.assert_not_called()

    @patch("lib.vmctl._run")
    def test_with_version_dir_success(self, mock_run: MagicMock) -> None:
        """With a version subdir, calls cp-to then depmod."""
        mock_run.return_value = _OK
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_mods = Path(tmpdir)
            ver_dir = lib_mods / "5.14.0-1"
            ver_dir.mkdir()
            result = _deploy_kernel_modules("my-vm", lib_mods)
        assert result["ok"] is True
        assert mock_run.call_count == 2
        # First call: cp-to
        cp_cmd = mock_run.call_args_list[0][0][0]
        assert "cp-to" in cp_cmd
        assert "my-vm" in cp_cmd
        assert str(ver_dir) in cp_cmd
        assert "/lib/modules/" in cp_cmd
        # Second call: depmod
        depmod_cmd = mock_run.call_args_list[1][0][0]
        assert "depmod" in " ".join(depmod_cmd)

    @patch("lib.vmctl._run")
    def test_multiple_version_dirs(self, mock_run: MagicMock) -> None:
        """Multiple version dirs each get a cp-to call."""
        mock_run.return_value = _OK
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_mods = Path(tmpdir)
            (lib_mods / "5.14.0-1").mkdir()
            (lib_mods / "5.14.0-2").mkdir()
            result = _deploy_kernel_modules("my-vm", lib_mods)
        assert result["ok"] is True
        # 2 cp-to + 1 depmod = 3
        assert mock_run.call_count == 3
        for call in mock_run.call_args_list[:2]:
            assert "cp-to" in call[0][0]

    @patch("lib.vmctl._run")
    def test_cp_failure_returns_early(self, mock_run: MagicMock) -> None:
        """If cp-to fails, return early without calling depmod."""
        mock_run.return_value = _FAIL
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_mods = Path(tmpdir)
            (lib_mods / "5.14.0-1").mkdir()
            result = _deploy_kernel_modules("my-vm", lib_mods)
        assert result["ok"] is False
        assert mock_run.call_count == 1

    @patch("lib.vmctl._run")
    def test_second_cp_failure_skips_rest(self, mock_run: MagicMock) -> None:
        """If second cp-to fails, depmod is not called."""
        mock_run.side_effect = [_OK, _FAIL]
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_mods = Path(tmpdir)
            (lib_mods / "5.14.0-1").mkdir()
            (lib_mods / "5.14.0-2").mkdir()
            result = _deploy_kernel_modules("my-vm", lib_mods)
        assert result["ok"] is False
        assert mock_run.call_count == 2


class TestLustreMount:
    @patch("lib.vmctl._run")
    def test_mount_calls_vm_exec(self, mock_run: MagicMock) -> None:
        """lustre_mount calls vm_exec with llmount.sh at standard path."""
        from lib.vmctl import lustre_mount

        mock_run.return_value = _OK
        result = lustre_mount("my-vm")
        assert result["ok"] is True
        cmd = mock_run.call_args[0][0]
        assert "exec" in cmd
        assert "my-vm" in cmd
        shell_cmd = " ".join(cmd)
        assert "llmount.sh" in shell_cmd
        assert "/usr/lib64/lustre/tests" in shell_cmd

    @patch("lib.vmctl._run")
    def test_mount_timeout_180(self, mock_run: MagicMock) -> None:
        """lustre_mount uses 180s inner timeout (210s outer)."""
        from lib.vmctl import lustre_mount

        mock_run.return_value = _OK
        lustre_mount("my-vm")
        cmd = mock_run.call_args[0][0]
        assert "180" in cmd
        assert mock_run.call_args[1]["timeout"] == 210


class TestClusterCreate:
    @patch("lib.vmctl._run")
    def test_create_with_node_specs(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        cluster_create("c1", "mgs+mds:c1-srv:1", "oss:c1-oss:3")
        cmd = mock.call_args[0][0]
        assert "cluster" in cmd
        assert "create" in cmd
        assert "c1" in cmd
        assert "mgs+mds:c1-srv:1" in cmd
        assert "oss:c1-oss:3" in cmd


class TestClusterDestroy:
    @patch("lib.vmctl._run")
    def test_destroy(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        cluster_destroy("c1")
        cmd = mock.call_args[0][0]
        assert "cluster" in cmd
        assert "destroy" in cmd
        assert "c1" in cmd


class TestClusterDeploy:
    @patch("lib.vmctl._run")
    def test_deploy_no_mount(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        cluster_deploy("c1", build_path="/some/build")
        cmd = mock.call_args[0][0]
        assert "cluster" in cmd
        assert "deploy" in cmd
        assert "c1" in cmd
        assert "--lustre-source" in cmd
        assert "--mount" not in cmd

    @patch("lib.vmctl._run")
    def test_deploy_with_mount(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        cluster_deploy("c1", build_path="/some/build", mount=True)
        cmd = mock.call_args[0][0]
        assert "--mount" in cmd


class TestClusterStatus:
    @patch("lib.vmctl._run")
    def test_status(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        cluster_status("c1")
        cmd = mock.call_args[0][0]
        assert "cluster" in cmd
        assert "status" in cmd
        assert "c1" in cmd


class TestClusterExec:
    @patch("lib.vmctl._run")
    def test_exec_forwards_role_and_cmd(self, mock: MagicMock) -> None:
        mock.return_value = _OK
        cluster_exec("c1", "oss", "lctl dl")
        cmd = mock.call_args[0][0]
        assert "cluster" in cmd
        assert "exec" in cmd
        assert "c1" in cmd
        assert "oss" in cmd
        assert "lctl dl" in cmd
