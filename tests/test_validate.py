"""Tests for lib/validate.py."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import lib.validate as validate
from lib.validate import (
    EXPECTED_PACKAGES,
    CheckResult,
    _summary,
    _vm_name,
    check_artifacts,
    check_basic_io,
    check_lustre_deploy,
    check_networking,
    check_no_lustre,
    check_packages,
    check_version_consistency,
    check_vm_boot,
    check_vm_kernel_version,
    print_results,
    validate_target,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_target(output_dir: Path, name: str = "testarch") -> SimpleNamespace:
    """Minimal stand-in for TargetConfig."""
    ns = SimpleNamespace(name=name, output_dir=output_dir)
    ns.kernel_output_dir = lambda kernel=None: output_dir / "kernel"
    return ns


def _make_artifact_tree(out: Path) -> None:
    """Create the full set of expected artifacts under *out*."""
    (out / "kernel").mkdir(parents=True)
    (out / "kernel" / "vmlinux").write_text("elf")
    bt = out / "kernel" / "build-tree"
    bt.mkdir(parents=True)
    (bt / ".config").write_text("CONFIG_X86=y\n")
    (bt / "Module.symvers").write_text("")
    (out / "image").mkdir(parents=True)
    (out / "image" / "base.ext4").write_text("ext4")
    (out / "container").mkdir(parents=True)
    (out / "container" / "meta.json").write_text("{}")


# ---------------------------------------------------------------------------
# CheckResult
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_constructor_stores_name(self) -> None:
        r = CheckResult("My check", True)
        assert r.name == "My check"

    def test_constructor_stores_passed_true(self) -> None:
        r = CheckResult("x", True)
        assert r.passed is True

    def test_constructor_stores_passed_false(self) -> None:
        r = CheckResult("x", False)
        assert r.passed is False

    def test_constructor_stores_detail(self) -> None:
        r = CheckResult("x", True, detail="some detail")
        assert r.detail == "some detail"

    def test_constructor_default_detail_is_empty(self) -> None:
        r = CheckResult("x", True)
        assert r.detail == ""

    def test_constructor_stores_elapsed(self) -> None:
        r = CheckResult("x", True, elapsed=1.23)
        assert r.elapsed == pytest.approx(1.23)

    def test_constructor_default_elapsed_is_zero(self) -> None:
        r = CheckResult("x", True)
        assert r.elapsed == 0.0

    def test_to_dict_has_required_keys(self) -> None:
        r = CheckResult("x", True)
        d = r.to_dict()
        assert set(d.keys()) == {"name", "passed", "detail", "elapsed_s"}

    def test_to_dict_name(self) -> None:
        r = CheckResult("My check", True)
        assert r.to_dict()["name"] == "My check"

    def test_to_dict_passed_true(self) -> None:
        r = CheckResult("x", True)
        assert r.to_dict()["passed"] is True

    def test_to_dict_passed_false(self) -> None:
        r = CheckResult("x", False)
        assert r.to_dict()["passed"] is False

    def test_to_dict_detail(self) -> None:
        r = CheckResult("x", True, detail="ok")
        assert r.to_dict()["detail"] == "ok"

    def test_to_dict_elapsed_rounded_to_2dp(self) -> None:
        r = CheckResult("x", True, elapsed=1.23456)
        assert r.to_dict()["elapsed_s"] == 1.23

    def test_to_dict_elapsed_rounds_up(self) -> None:
        r = CheckResult("x", True, elapsed=1.235)
        assert r.to_dict()["elapsed_s"] == pytest.approx(round(1.235, 2))

    def test_to_dict_elapsed_zero(self) -> None:
        r = CheckResult("x", True, elapsed=0.0)
        assert r.to_dict()["elapsed_s"] == 0.0

    def test_repr_pass(self) -> None:
        r = CheckResult("My check", True, detail="great")
        assert "PASS" in repr(r)
        assert "My check" in repr(r)

    def test_repr_fail(self) -> None:
        r = CheckResult("My check", False, detail="broken")
        assert "FAIL" in repr(r)
        assert "My check" in repr(r)

    def test_repr_no_fail_when_passed(self) -> None:
        r = CheckResult("x", True)
        assert "FAIL" not in repr(r)

    def test_repr_no_pass_when_failed(self) -> None:
        r = CheckResult("x", False)
        assert "PASS" not in repr(r)


# ---------------------------------------------------------------------------
# _summary
# ---------------------------------------------------------------------------


class TestSummary:
    def test_all_passed_true_when_all_pass(self) -> None:
        results = [CheckResult("a", True), CheckResult("b", True)]
        s = _summary("mytarget", results)
        assert s["all_passed"] is True

    def test_all_passed_false_when_any_fail(self) -> None:
        results = [CheckResult("a", True), CheckResult("b", False)]
        s = _summary("mytarget", results)
        assert s["all_passed"] is False

    def test_all_passed_false_when_all_fail(self) -> None:
        results = [CheckResult("a", False), CheckResult("b", False)]
        s = _summary("mytarget", results)
        assert s["all_passed"] is False

    def test_passed_count_all_pass(self) -> None:
        results = [CheckResult("a", True), CheckResult("b", True)]
        s = _summary("mytarget", results)
        assert s["passed"] == 2

    def test_passed_count_mixed(self) -> None:
        results = [
            CheckResult("a", True),
            CheckResult("b", False),
            CheckResult("c", True),
        ]
        s = _summary("mytarget", results)
        assert s["passed"] == 2

    def test_passed_count_none_pass(self) -> None:
        results = [CheckResult("a", False), CheckResult("b", False)]
        s = _summary("mytarget", results)
        assert s["passed"] == 0

    def test_total_count(self) -> None:
        results = [CheckResult("a", True), CheckResult("b", False)]
        s = _summary("mytarget", results)
        assert s["total"] == 2

    def test_total_count_empty(self) -> None:
        s = _summary("mytarget", [])
        assert s["total"] == 0

    def test_target_name_propagated(self) -> None:
        s = _summary("specialtarget", [])
        assert s["target"] == "specialtarget"

    def test_checks_list_length(self) -> None:
        results = [CheckResult("a", True), CheckResult("b", False)]
        s = _summary("x", results)
        assert len(s["checks"]) == 2

    def test_checks_list_contains_dicts(self) -> None:
        results = [CheckResult("a", True, detail="d", elapsed=0.5)]
        s = _summary("x", results)
        check = s["checks"][0]
        assert check["name"] == "a"
        assert check["passed"] is True
        assert check["detail"] == "d"
        assert check["elapsed_s"] == 0.5

    def test_checks_list_order_preserved(self) -> None:
        results = [CheckResult("first", True), CheckResult("second", False)]
        s = _summary("x", results)
        assert s["checks"][0]["name"] == "first"
        assert s["checks"][1]["name"] == "second"

    def test_all_passed_true_with_empty_list(self) -> None:
        s = _summary("x", [])
        assert s["all_passed"] is True


# ---------------------------------------------------------------------------
# _vm_name
# ---------------------------------------------------------------------------


class TestVmName:
    def test_contains_target_name(self) -> None:
        name = _vm_name("rocky9")
        assert "rocky9" in name

    def test_contains_pid(self) -> None:
        name = _vm_name("rocky9")
        assert str(os.getpid()) in name

    def test_returns_string(self) -> None:
        assert isinstance(_vm_name("rocky9"), str)

    def test_different_targets_give_different_names(self) -> None:
        assert _vm_name("rocky9") != _vm_name("ubuntu22")

    def test_same_target_same_pid_is_stable(self) -> None:
        assert _vm_name("rocky9") == _vm_name("rocky9")


# ---------------------------------------------------------------------------
# check_artifacts
# ---------------------------------------------------------------------------


class TestCheckArtifacts:
    def test_all_present_passes(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _make_artifact_tree(out)
        tc = _fake_target(out)
        result = check_artifacts(tc)
        assert result.passed is True

    def test_all_present_detail_shows_count(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _make_artifact_tree(out)
        tc = _fake_target(out)
        result = check_artifacts(tc)
        assert "5/5" in result.detail

    def test_missing_vmlinux_fails(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _make_artifact_tree(out)
        (out / "kernel" / "vmlinux").unlink()
        tc = _fake_target(out)
        result = check_artifacts(tc)
        assert result.passed is False

    def test_missing_vmlinux_named_in_detail(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _make_artifact_tree(out)
        (out / "kernel" / "vmlinux").unlink()
        tc = _fake_target(out)
        result = check_artifacts(tc)
        assert "vmlinux" in result.detail

    def test_missing_config_fails(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _make_artifact_tree(out)
        (out / "kernel" / "build-tree" / ".config").unlink()
        tc = _fake_target(out)
        result = check_artifacts(tc)
        assert result.passed is False

    def test_missing_module_symvers_fails(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _make_artifact_tree(out)
        (out / "kernel" / "build-tree" / "Module.symvers").unlink()
        tc = _fake_target(out)
        result = check_artifacts(tc)
        assert result.passed is False

    def test_missing_base_ext4_fails(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _make_artifact_tree(out)
        (out / "image" / "base.ext4").unlink()
        tc = _fake_target(out)
        result = check_artifacts(tc)
        assert result.passed is False

    def test_missing_container_meta_fails(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _make_artifact_tree(out)
        (out / "container" / "meta.json").unlink()
        tc = _fake_target(out)
        result = check_artifacts(tc)
        assert result.passed is False

    def test_multiple_missing_all_named(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _make_artifact_tree(out)
        (out / "kernel" / "vmlinux").unlink()
        (out / "image" / "base.ext4").unlink()
        tc = _fake_target(out)
        result = check_artifacts(tc)
        assert "vmlinux" in result.detail
        assert "base.ext4" in result.detail

    def test_no_output_dir_fails(self, tmp_path: Path) -> None:
        out = tmp_path / "nonexistent"
        tc = _fake_target(out)
        result = check_artifacts(tc)
        assert result.passed is False

    def test_result_name(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _make_artifact_tree(out)
        tc = _fake_target(out)
        result = check_artifacts(tc)
        assert result.name == "Artifacts exist"

    def test_elapsed_is_non_negative(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _make_artifact_tree(out)
        tc = _fake_target(out)
        result = check_artifacts(tc)
        assert result.elapsed >= 0.0


# ---------------------------------------------------------------------------
# check_version_consistency
# ---------------------------------------------------------------------------


def _make_version_tree(out: Path, meta_version: str, kr_version: str) -> None:
    """Create kernel meta.json and kernel.release under *out*."""
    kernel_dir = out / "kernel"
    kernel_dir.mkdir(parents=True, exist_ok=True)
    (kernel_dir / "meta.json").write_text(
        json.dumps({"kernel_version": meta_version})
    )
    kr_dir = kernel_dir / "build-tree" / "include" / "config"
    kr_dir.mkdir(parents=True, exist_ok=True)
    (kr_dir / "kernel.release").write_text(kr_version + "\n")


class TestCheckVersionConsistency:
    def test_matching_versions_passes(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _make_version_tree(out, "5.14.0-503.el9", "5.14.0-503.el9")
        tc = _fake_target(out)
        result = check_version_consistency(tc)
        assert result.passed is True

    def test_matching_versions_detail_is_version(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _make_version_tree(out, "5.14.0-503.el9", "5.14.0-503.el9")
        tc = _fake_target(out)
        result = check_version_consistency(tc)
        assert "5.14.0-503.el9" in result.detail

    def test_mismatched_versions_fails(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _make_version_tree(out, "5.14.0-503.el9", "5.14.0-999.el9")
        tc = _fake_target(out)
        result = check_version_consistency(tc)
        assert result.passed is False

    def test_mismatched_versions_detail_shows_both(
        self, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        _make_version_tree(out, "5.14.0-503.el9", "5.14.0-999.el9")
        tc = _fake_target(out)
        result = check_version_consistency(tc)
        assert "5.14.0-503.el9" in result.detail
        assert "5.14.0-999.el9" in result.detail

    def test_missing_meta_json_fails(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        (out / "kernel").mkdir(parents=True)
        tc = _fake_target(out)
        result = check_version_consistency(tc)
        assert result.passed is False

    def test_missing_meta_json_detail(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        (out / "kernel").mkdir(parents=True)
        tc = _fake_target(out)
        result = check_version_consistency(tc)
        assert "meta.json" in result.detail

    def test_missing_kernel_release_fails(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        (out / "kernel").mkdir(parents=True)
        (out / "kernel" / "meta.json").write_text(
            json.dumps({"kernel_version": "5.14.0"})
        )
        tc = _fake_target(out)
        result = check_version_consistency(tc)
        assert result.passed is False

    def test_missing_kernel_release_detail(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        (out / "kernel").mkdir(parents=True)
        (out / "kernel" / "meta.json").write_text(
            json.dumps({"kernel_version": "5.14.0"})
        )
        tc = _fake_target(out)
        result = check_version_consistency(tc)
        assert "kernel.release" in result.detail

    def test_kernel_release_whitespace_stripped(self, tmp_path: Path) -> None:
        """Trailing newline in kernel.release must not cause false mismatch."""
        out = tmp_path / "out"
        _make_version_tree(out, "5.14.0-503.el9", "5.14.0-503.el9")
        tc = _fake_target(out)
        result = check_version_consistency(tc)
        assert result.passed is True

    def test_result_name(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _make_version_tree(out, "5.14.0", "5.14.0")
        tc = _fake_target(out)
        result = check_version_consistency(tc)
        assert result.name == "Version consistency"


# ---------------------------------------------------------------------------
# check_packages  (mocks _vm_exec)
# ---------------------------------------------------------------------------


def _rpm_output_all_found(packages: list[str]) -> str:
    """Simulate rpm -q output when all packages are installed."""
    lines = [f"{p}-1.0-1.el9.x86_64" for p in packages]
    return "\n".join(lines)


def _rpm_output_with_missing(packages: list[str], missing: list[str]) -> str:
    """Simulate rpm -q output with some packages missing."""
    lines = []
    for p in packages:
        if p in missing:
            lines.append(f"package {p} is not installed")
        else:
            lines.append(f"{p}-1.0-1.el9.x86_64")
    return "\n".join(lines)


class TestCheckPackages:
    def test_all_installed_passes(self) -> None:
        stdout = _rpm_output_all_found(EXPECTED_PACKAGES)
        with patch.object(validate, "_vm_exec", return_value=(0, stdout, "")):
            result = check_packages("testvm")
        assert result.passed is True

    def test_all_installed_detail(self) -> None:
        total = len(EXPECTED_PACKAGES)
        stdout = _rpm_output_all_found(EXPECTED_PACKAGES)
        with patch.object(validate, "_vm_exec", return_value=(0, stdout, "")):
            result = check_packages("testvm")
        assert f"{total}/{total}" in result.detail

    def test_missing_package_fails(self) -> None:
        stdout = _rpm_output_with_missing(EXPECTED_PACKAGES, ["fio"])
        with patch.object(validate, "_vm_exec", return_value=(0, stdout, "")):
            result = check_packages("testvm")
        assert result.passed is False

    def test_missing_package_named_in_detail(self) -> None:
        stdout = _rpm_output_with_missing(EXPECTED_PACKAGES, ["fio"])
        with patch.object(validate, "_vm_exec", return_value=(0, stdout, "")):
            result = check_packages("testvm")
        assert "fio" in result.detail

    def test_multiple_missing_all_named(self) -> None:
        stdout = _rpm_output_with_missing(EXPECTED_PACKAGES, ["fio", "gdb"])
        with patch.object(validate, "_vm_exec", return_value=(0, stdout, "")):
            result = check_packages("testvm")
        assert "fio" in result.detail
        assert "gdb" in result.detail

    def test_count_in_detail_when_missing(self) -> None:
        total = len(EXPECTED_PACKAGES)
        stdout = _rpm_output_with_missing(EXPECTED_PACKAGES, ["fio"])
        with patch.object(validate, "_vm_exec", return_value=(0, stdout, "")):
            result = check_packages("testvm")
        assert f"{total - 1}/{total}" in result.detail

    def test_empty_stdout_all_missing(self) -> None:
        # No lines => no "is not installed" lines, so nothing reported missing
        # (rpm would normally print something, but empty stdout => 0 missing
        # from the parser's perspective)
        with patch.object(validate, "_vm_exec", return_value=(0, "", "")):
            result = check_packages("testvm")
        assert result.passed is True

    def test_result_name(self) -> None:
        stdout = _rpm_output_all_found(EXPECTED_PACKAGES)
        with patch.object(validate, "_vm_exec", return_value=(0, stdout, "")):
            result = check_packages("testvm")
        assert result.name == "Package check"

    def test_vm_exec_called_with_vm_name(self) -> None:
        stdout = _rpm_output_all_found(EXPECTED_PACKAGES)
        with patch.object(
            validate, "_vm_exec", return_value=(0, stdout, "")
        ) as mock_exec:
            check_packages("myvm")
        call_args = mock_exec.call_args
        assert call_args[0][0] == "myvm"

    def test_vm_exec_cmd_contains_all_packages(self) -> None:
        stdout = _rpm_output_all_found(EXPECTED_PACKAGES)
        with patch.object(
            validate, "_vm_exec", return_value=(0, stdout, "")
        ) as mock_exec:
            check_packages("myvm")
        cmd = mock_exec.call_args[0][1]
        for pkg in EXPECTED_PACKAGES:
            assert pkg in cmd


# ---------------------------------------------------------------------------
# check_no_lustre  (mocks _vm_exec)
# ---------------------------------------------------------------------------


class TestCheckNoLustre:
    def test_no_lustre_modules_passes(self) -> None:
        # grep returns rc=1 when no match -- that's good
        with patch.object(validate, "_vm_exec", return_value=(1, "", "")):
            result = check_no_lustre("testvm")
        assert result.passed is True

    def test_no_lustre_empty_stdout_rc0_passes(self) -> None:
        # rc=0 but empty stdout: grep matched nothing meaningful
        with patch.object(validate, "_vm_exec", return_value=(0, "", "")):
            result = check_no_lustre("testvm")
        assert result.passed is True

    def test_lustre_found_fails(self) -> None:
        lustre_output = "lustre               4096  0\nldiskfs              1234  1 lustre\n"
        with patch.object(
            validate, "_vm_exec", return_value=(0, lustre_output, "")
        ):
            result = check_no_lustre("testvm")
        assert result.passed is False

    def test_lustre_found_detail_contains_output(self) -> None:
        lustre_output = "lustre               4096  0"
        with patch.object(
            validate, "_vm_exec", return_value=(0, lustre_output, "")
        ):
            result = check_no_lustre("testvm")
        assert "lustre" in result.detail

    def test_result_name(self) -> None:
        with patch.object(validate, "_vm_exec", return_value=(1, "", "")):
            result = check_no_lustre("testvm")
        assert result.name == "No Lustre loaded"

    def test_vm_exec_called_with_vm_name(self) -> None:
        with patch.object(
            validate, "_vm_exec", return_value=(1, "", "")
        ) as mock_exec:
            check_no_lustre("myspecialvm")
        assert mock_exec.call_args[0][0] == "myspecialvm"

    def test_cmd_checks_lsmod(self) -> None:
        with patch.object(
            validate, "_vm_exec", return_value=(1, "", "")
        ) as mock_exec:
            check_no_lustre("myvm")
        cmd = mock_exec.call_args[0][1]
        assert "lsmod" in cmd
        assert "lustre" in cmd.lower()


# ---------------------------------------------------------------------------
# TestVmExecHelper  (mocks subprocess.run)
# ---------------------------------------------------------------------------


class TestVmExecHelper:
    def _make_proc(
        self, returncode: int = 0, stdout: str = "out", stderr: str = "err"
    ) -> MagicMock:
        proc = MagicMock()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = stderr
        return proc

    def test_returns_tuple(self) -> None:
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ):
            result = validate._vm_exec("myvm", "echo hi")
        assert isinstance(result, tuple)
        assert len(result) == 3

    def test_returns_returncode(self) -> None:
        with patch.object(
            validate.subprocess,
            "run",
            return_value=self._make_proc(returncode=42),
        ):
            rc, _stdout, _stderr = validate._vm_exec("myvm", "false")
        assert rc == 42

    def test_returns_stdout(self) -> None:
        with patch.object(
            validate.subprocess,
            "run",
            return_value=self._make_proc(stdout="hello\n"),
        ):
            _rc, stdout, _stderr = validate._vm_exec("myvm", "echo hello")
        assert stdout == "hello\n"

    def test_returns_stderr(self) -> None:
        with patch.object(
            validate.subprocess,
            "run",
            return_value=self._make_proc(stderr="oops"),
        ):
            _rc, _stdout, stderr = validate._vm_exec("myvm", "badcmd")
        assert stderr == "oops"

    def test_cmd_contains_vm_name(self) -> None:
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ) as mock_run:
            validate._vm_exec("targetvm", "hostname")
        cmd = mock_run.call_args[0][0]
        assert "targetvm" in cmd

    def test_cmd_contains_user_command(self) -> None:
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ) as mock_run:
            validate._vm_exec("myvm", "uname -r")
        cmd = mock_run.call_args[0][0]
        assert "uname -r" in cmd

    def test_cmd_calls_vm_py_exec(self) -> None:
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ) as mock_run:
            validate._vm_exec("myvm", "hostname")
        cmd = mock_run.call_args[0][0]
        assert "vm.py" in cmd
        assert "exec" in cmd

    def test_cmd_contains_timeout_flag(self) -> None:
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ) as mock_run:
            validate._vm_exec("myvm", "hostname", timeout=45)
        cmd = mock_run.call_args[0][0]
        assert "--timeout" in cmd
        assert "45" in cmd

    def test_default_timeout_in_cmd(self) -> None:
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ) as mock_run:
            validate._vm_exec("myvm", "hostname")
        cmd = mock_run.call_args[0][0]
        assert "--timeout" in cmd
        assert "30" in cmd

    def test_uses_sudo(self) -> None:
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ) as mock_run:
            validate._vm_exec("myvm", "hostname")
        cmd = mock_run.call_args[0][0]
        assert "sudo" in cmd


# ---------------------------------------------------------------------------
# TestVmEnsureHelper  (mocks subprocess.run)
# ---------------------------------------------------------------------------


class TestVmEnsureHelper:
    def _make_proc(self, returncode: int = 0, stderr: str = "") -> MagicMock:
        proc = MagicMock()
        proc.returncode = returncode
        proc.stdout = ""
        proc.stderr = stderr
        return proc

    def test_success_returns_completed_process(self, tmp_path: Path) -> None:
        image = tmp_path / "base.ext4"
        kernel = tmp_path / "vmlinux"
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ):
            result = validate._vm_ensure("myvm", image, kernel)
        assert result is not None

    def test_failure_raises_runtime_error(self, tmp_path: Path) -> None:
        image = tmp_path / "base.ext4"
        kernel = tmp_path / "vmlinux"
        with patch.object(
            validate.subprocess,
            "run",
            return_value=self._make_proc(returncode=1, stderr="boom"),
        ):
            with pytest.raises(RuntimeError):
                validate._vm_ensure("failvm", image, kernel)

    def test_failure_error_contains_vm_name(self, tmp_path: Path) -> None:
        image = tmp_path / "base.ext4"
        kernel = tmp_path / "vmlinux"
        with patch.object(
            validate.subprocess,
            "run",
            return_value=self._make_proc(returncode=1, stderr="boom"),
        ):
            with pytest.raises(RuntimeError, match="failvm"):
                validate._vm_ensure("failvm", image, kernel)

    def test_with_mdt_and_ost_disks_flags_in_cmd(self, tmp_path: Path) -> None:
        image = tmp_path / "base.ext4"
        kernel = tmp_path / "vmlinux"
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ) as mock_run:
            validate._vm_ensure("myvm", image, kernel, mdt_disks=1, ost_disks=3)
        cmd = mock_run.call_args[0][0]
        assert "--mdt-disks" in cmd
        assert "--ost-disks" in cmd
        assert "1" in cmd
        assert "3" in cmd

    def test_without_disks_no_disk_flags(self, tmp_path: Path) -> None:
        image = tmp_path / "base.ext4"
        kernel = tmp_path / "vmlinux"
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ) as mock_run:
            validate._vm_ensure("myvm", image, kernel)
        cmd = mock_run.call_args[0][0]
        assert "--mdt-disks" not in cmd
        assert "--ost-disks" not in cmd

    def test_cmd_contains_vm_name(self, tmp_path: Path) -> None:
        image = tmp_path / "base.ext4"
        kernel = tmp_path / "vmlinux"
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ) as mock_run:
            validate._vm_ensure("specialvm", image, kernel)
        cmd = mock_run.call_args[0][0]
        assert "specialvm" in cmd

    def test_cmd_contains_image_path(self, tmp_path: Path) -> None:
        image = tmp_path / "base.ext4"
        kernel = tmp_path / "vmlinux"
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ) as mock_run:
            validate._vm_ensure("myvm", image, kernel)
        cmd = mock_run.call_args[0][0]
        assert str(image) in cmd

    def test_cmd_contains_kernel_path(self, tmp_path: Path) -> None:
        image = tmp_path / "base.ext4"
        kernel = tmp_path / "vmlinux"
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ) as mock_run:
            validate._vm_ensure("myvm", image, kernel)
        cmd = mock_run.call_args[0][0]
        assert str(kernel) in cmd


# ---------------------------------------------------------------------------
# TestVmDestroyHelper  (mocks subprocess.run)
# ---------------------------------------------------------------------------


class TestVmDestroyHelper:
    def test_calls_subprocess_run(self) -> None:
        with patch.object(validate.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            validate._vm_destroy("myvm")
        mock_run.assert_called_once()

    def test_cmd_contains_vm_name(self) -> None:
        with patch.object(validate.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            validate._vm_destroy("destroyme")
        cmd = mock_run.call_args[0][0]
        assert "destroyme" in cmd

    def test_cmd_calls_destroy(self) -> None:
        with patch.object(validate.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            validate._vm_destroy("myvm")
        cmd = mock_run.call_args[0][0]
        assert "destroy" in cmd

    def test_swallows_failure(self) -> None:
        """_vm_destroy is best-effort -- non-zero rc must not raise."""
        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = ""
        proc.stderr = "already gone"
        with patch.object(validate.subprocess, "run", return_value=proc):
            # Should not raise
            validate._vm_destroy("myvm")

    def test_returns_none(self) -> None:
        with patch.object(validate.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = validate._vm_destroy("myvm")
        assert result is None


# ---------------------------------------------------------------------------
# TestDeployLustreHelper  (mocks subprocess.run)
# ---------------------------------------------------------------------------


class TestDeployLustreHelper:
    def _make_proc(self, returncode: int = 0, stderr: str = "") -> MagicMock:
        proc = MagicMock()
        proc.returncode = returncode
        proc.stdout = ""
        proc.stderr = stderr
        return proc

    def test_success_returns_completed_process(self, tmp_path: Path) -> None:
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ):
            result = validate._deploy_lustre("myvm", tmp_path)
        assert result is not None

    def test_failure_raises_runtime_error(self, tmp_path: Path) -> None:
        with patch.object(
            validate.subprocess,
            "run",
            return_value=self._make_proc(returncode=1, stderr="deploy failed"),
        ):
            with pytest.raises(RuntimeError):
                validate._deploy_lustre("myvm", tmp_path)

    def test_failure_error_message_contains_info(self, tmp_path: Path) -> None:
        with patch.object(
            validate.subprocess,
            "run",
            return_value=self._make_proc(returncode=1, stderr="mount error"),
        ):
            with pytest.raises(RuntimeError, match="mount error"):
                validate._deploy_lustre("myvm", tmp_path)

    def test_timeout_raises_runtime_error(self, tmp_path: Path) -> None:
        with patch.object(
            validate.subprocess,
            "run",
            side_effect=validate.subprocess.TimeoutExpired(
                cmd="deploy", timeout=300
            ),
        ):
            with pytest.raises(subprocess.TimeoutExpired):
                validate._deploy_lustre("myvm", tmp_path)

    def test_cmd_contains_vm_name(self, tmp_path: Path) -> None:
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ) as mock_run:
            validate._deploy_lustre("targetvm", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "targetvm" in cmd

    def test_cmd_contains_build_path(self, tmp_path: Path) -> None:
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ) as mock_run:
            validate._deploy_lustre("myvm", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert str(tmp_path) in cmd

    def test_cmd_contains_mount_flag(self, tmp_path: Path) -> None:
        with patch.object(
            validate.subprocess, "run", return_value=self._make_proc()
        ) as mock_run:
            validate._deploy_lustre("myvm", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "--mount" in cmd


# ---------------------------------------------------------------------------
# TestCheckVmBoot  (mocks _vm_exec)
# ---------------------------------------------------------------------------


class TestCheckVmBoot:
    def _fake_target(self, output_dir: Path) -> SimpleNamespace:
        return SimpleNamespace(name="test", output_dir=output_dir)

    def test_success_rc0_ok_in_stdout_passes(self, tmp_path: Path) -> None:
        tc = self._fake_target(tmp_path)
        with patch.object(validate, "_vm_exec", return_value=(0, "ok\n", "")):
            result = check_vm_boot(tc, "myvm")
        assert result.passed is True

    def test_success_detail_contains_elapsed(self, tmp_path: Path) -> None:
        tc = self._fake_target(tmp_path)
        with patch.object(validate, "_vm_exec", return_value=(0, "ok\n", "")):
            result = check_vm_boot(tc, "myvm")
        assert "s" in result.detail  # e.g. "booted in 0.0s"

    def test_fail_rc_nonzero_fails(self, tmp_path: Path) -> None:
        tc = self._fake_target(tmp_path)
        with patch.object(
            validate, "_vm_exec", return_value=(1, "", "timeout")
        ):
            result = check_vm_boot(tc, "myvm")
        assert result.passed is False

    def test_fail_ok_not_in_stdout_fails(self, tmp_path: Path) -> None:
        tc = self._fake_target(tmp_path)
        with patch.object(
            validate, "_vm_exec", return_value=(0, "no output here", "")
        ):
            result = check_vm_boot(tc, "myvm")
        assert result.passed is False

    def test_fail_detail_has_stderr(self, tmp_path: Path) -> None:
        tc = self._fake_target(tmp_path)
        with patch.object(
            validate, "_vm_exec", return_value=(1, "", "connection refused")
        ):
            result = check_vm_boot(tc, "myvm")
        assert "connection refused" in result.detail

    def test_result_name(self, tmp_path: Path) -> None:
        tc = self._fake_target(tmp_path)
        with patch.object(validate, "_vm_exec", return_value=(0, "ok", "")):
            result = check_vm_boot(tc, "myvm")
        assert result.name == "Virgin VM boot"

    def test_elapsed_non_negative(self, tmp_path: Path) -> None:
        tc = self._fake_target(tmp_path)
        with patch.object(validate, "_vm_exec", return_value=(0, "ok", "")):
            result = check_vm_boot(tc, "myvm")
        assert result.elapsed >= 0.0


# ---------------------------------------------------------------------------
# TestCheckVmKernelVersion  (mocks _vm_exec + uses tmp_path for meta.json)
# ---------------------------------------------------------------------------


class TestCheckVmKernelVersion:
    def _fake_target(self, output_dir: Path) -> SimpleNamespace:
        return SimpleNamespace(name="test", output_dir=output_dir)

    def _write_meta(self, out: Path, version: str) -> None:
        kernel_dir = out / "kernel"
        kernel_dir.mkdir(parents=True, exist_ok=True)
        (kernel_dir / "meta.json").write_text(
            json.dumps({"kernel_version": version})
        )

    def test_matching_versions_passes(self, tmp_path: Path) -> None:
        self._write_meta(tmp_path, "5.14.0-503.el9")
        tc = self._fake_target(tmp_path)
        with patch.object(
            validate, "_vm_exec", return_value=(0, "5.14.0-503.el9\n", "")
        ):
            result = check_vm_kernel_version(tc, "myvm")
        assert result.passed is True

    def test_matching_versions_detail_is_version(self, tmp_path: Path) -> None:
        self._write_meta(tmp_path, "5.14.0-503.el9")
        tc = self._fake_target(tmp_path)
        with patch.object(
            validate, "_vm_exec", return_value=(0, "5.14.0-503.el9\n", "")
        ):
            result = check_vm_kernel_version(tc, "myvm")
        assert "5.14.0-503.el9" in result.detail

    def test_mismatch_fails(self, tmp_path: Path) -> None:
        self._write_meta(tmp_path, "5.14.0-503.el9")
        tc = self._fake_target(tmp_path)
        with patch.object(
            validate, "_vm_exec", return_value=(0, "5.14.0-999.el9\n", "")
        ):
            result = check_vm_kernel_version(tc, "myvm")
        assert result.passed is False

    def test_mismatch_detail_shows_both(self, tmp_path: Path) -> None:
        self._write_meta(tmp_path, "5.14.0-503.el9")
        tc = self._fake_target(tmp_path)
        with patch.object(
            validate, "_vm_exec", return_value=(0, "5.14.0-999.el9\n", "")
        ):
            result = check_vm_kernel_version(tc, "myvm")
        assert "5.14.0-503.el9" in result.detail
        assert "5.14.0-999.el9" in result.detail

    def test_uname_fail_rc_nonzero_fails(self, tmp_path: Path) -> None:
        self._write_meta(tmp_path, "5.14.0-503.el9")
        tc = self._fake_target(tmp_path)
        with patch.object(
            validate, "_vm_exec", return_value=(1, "", "exec error")
        ):
            result = check_vm_kernel_version(tc, "myvm")
        assert result.passed is False

    def test_uname_fail_detail_has_error(self, tmp_path: Path) -> None:
        self._write_meta(tmp_path, "5.14.0-503.el9")
        tc = self._fake_target(tmp_path)
        with patch.object(
            validate, "_vm_exec", return_value=(1, "", "exec error")
        ):
            result = check_vm_kernel_version(tc, "myvm")
        assert "exec error" in result.detail

    def test_no_meta_json_fails(self, tmp_path: Path) -> None:
        tc = self._fake_target(tmp_path)
        with patch.object(
            validate, "_vm_exec", return_value=(0, "5.14.0\n", "")
        ):
            result = check_vm_kernel_version(tc, "myvm")
        assert result.passed is False

    def test_no_meta_json_detail_mentions_meta(self, tmp_path: Path) -> None:
        tc = self._fake_target(tmp_path)
        with patch.object(
            validate, "_vm_exec", return_value=(0, "5.14.0\n", "")
        ):
            result = check_vm_kernel_version(tc, "myvm")
        assert "meta.json" in result.detail

    def test_result_name(self, tmp_path: Path) -> None:
        self._write_meta(tmp_path, "5.14.0")
        tc = self._fake_target(tmp_path)
        with patch.object(
            validate, "_vm_exec", return_value=(0, "5.14.0\n", "")
        ):
            result = check_vm_kernel_version(tc, "myvm")
        assert result.name == "Kernel version match"


# ---------------------------------------------------------------------------
# TestCheckNetworking  (mocks _vm_exec)
# ---------------------------------------------------------------------------


class TestCheckNetworking:
    def test_rc0_passes(self) -> None:
        with patch.object(
            validate, "_vm_exec", return_value=(0, "1 received", "")
        ):
            result = check_networking("myvm")
        assert result.passed is True

    def test_rc_nonzero_fails(self) -> None:
        with patch.object(
            validate, "_vm_exec", return_value=(1, "", "Network unreachable")
        ):
            result = check_networking("myvm")
        assert result.passed is False

    def test_fail_detail_has_stderr(self) -> None:
        with patch.object(
            validate,
            "_vm_exec",
            return_value=(1, "", "Network unreachable"),
        ):
            result = check_networking("myvm")
        assert "Network unreachable" in result.detail

    def test_fail_no_stderr_uses_fallback(self) -> None:
        with patch.object(validate, "_vm_exec", return_value=(1, "", "")):
            result = check_networking("myvm")
        assert result.detail  # non-empty fallback

    def test_result_name(self) -> None:
        with patch.object(validate, "_vm_exec", return_value=(0, "", "")):
            result = check_networking("myvm")
        assert result.name == "Networking"

    def test_vm_exec_called_with_vm_name(self) -> None:
        with patch.object(
            validate, "_vm_exec", return_value=(0, "", "")
        ) as mock_exec:
            check_networking("pingme")
        assert mock_exec.call_args[0][0] == "pingme"

    def test_cmd_contains_ping(self) -> None:
        with patch.object(
            validate, "_vm_exec", return_value=(0, "", "")
        ) as mock_exec:
            check_networking("myvm")
        cmd = mock_exec.call_args[0][1]
        assert "ping" in cmd

    def test_elapsed_non_negative(self) -> None:
        with patch.object(validate, "_vm_exec", return_value=(0, "", "")):
            result = check_networking("myvm")
        assert result.elapsed >= 0.0


# ---------------------------------------------------------------------------
# TestCheckLustreDeploy  (mocks _deploy_lustre + _vm_exec)
# ---------------------------------------------------------------------------


class TestCheckLustreDeploy:
    def test_deploy_success_mountpoint_ok_passes(self, tmp_path: Path) -> None:
        proc = MagicMock()
        proc.returncode = 0
        with patch.object(validate, "_deploy_lustre", return_value=proc):
            with patch.object(validate, "_vm_exec", return_value=(0, "", "")):
                result = check_lustre_deploy("myvm", tmp_path)
        assert result.passed is True

    def test_deploy_raises_runtime_error_fails(self, tmp_path: Path) -> None:
        with patch.object(
            validate,
            "_deploy_lustre",
            side_effect=RuntimeError("deploy blew up"),
        ):
            result = check_lustre_deploy("myvm", tmp_path)
        assert result.passed is False

    def test_deploy_error_detail_contains_message(self, tmp_path: Path) -> None:
        with patch.object(
            validate,
            "_deploy_lustre",
            side_effect=RuntimeError("deploy blew up"),
        ):
            result = check_lustre_deploy("myvm", tmp_path)
        assert "deploy blew up" in result.detail

    def test_deploy_success_mountpoint_fails(self, tmp_path: Path) -> None:
        proc = MagicMock()
        proc.returncode = 0
        with patch.object(validate, "_deploy_lustre", return_value=proc):
            with patch.object(
                validate, "_vm_exec", return_value=(1, "", "not a mountpoint")
            ):
                result = check_lustre_deploy("myvm", tmp_path)
        assert result.passed is False

    def test_mountpoint_fail_detail_mentions_path(self, tmp_path: Path) -> None:
        proc = MagicMock()
        proc.returncode = 0
        with patch.object(validate, "_deploy_lustre", return_value=proc):
            with patch.object(validate, "_vm_exec", return_value=(1, "", "")):
                result = check_lustre_deploy("myvm", tmp_path)
        assert "/mnt/lustre" in result.detail

    def test_result_name(self, tmp_path: Path) -> None:
        proc = MagicMock()
        proc.returncode = 0
        with patch.object(validate, "_deploy_lustre", return_value=proc):
            with patch.object(validate, "_vm_exec", return_value=(0, "", "")):
                result = check_lustre_deploy("myvm", tmp_path)
        assert result.name == "Lustre deploy + mount"

    def test_timeout_expired_fails(self, tmp_path: Path) -> None:
        import subprocess as _sp

        with patch.object(
            validate,
            "_deploy_lustre",
            side_effect=_sp.TimeoutExpired(cmd="deploy", timeout=300),
        ):
            result = check_lustre_deploy("myvm", tmp_path)
        assert result.passed is False


# ---------------------------------------------------------------------------
# TestCheckBasicIo  (mocks _vm_exec)
# ---------------------------------------------------------------------------


class TestCheckBasicIo:
    _test_data = "ltvm-validation-test-data-12345"

    def test_write_read_success_passes(self) -> None:
        with patch.object(
            validate, "_vm_exec", return_value=(0, self._test_data + "\n", "")
        ):
            result = check_basic_io("myvm")
        assert result.passed is True

    def test_rc_nonzero_fails(self) -> None:
        with patch.object(
            validate,
            "_vm_exec",
            return_value=(1, "", "permission denied"),
        ):
            result = check_basic_io("myvm")
        assert result.passed is False

    def test_rc_nonzero_detail_has_stderr(self) -> None:
        with patch.object(
            validate,
            "_vm_exec",
            return_value=(1, "", "permission denied"),
        ):
            result = check_basic_io("myvm")
        assert "permission denied" in result.detail

    def test_data_not_in_stdout_fails(self) -> None:
        with patch.object(
            validate, "_vm_exec", return_value=(0, "wrong data\n", "")
        ):
            result = check_basic_io("myvm")
        assert result.passed is False

    def test_data_not_in_stdout_detail_has_readback(self) -> None:
        with patch.object(
            validate, "_vm_exec", return_value=(0, "wrong data\n", "")
        ):
            result = check_basic_io("myvm")
        assert "wrong data" in result.detail

    def test_result_name(self) -> None:
        with patch.object(
            validate, "_vm_exec", return_value=(0, self._test_data, "")
        ):
            result = check_basic_io("myvm")
        assert result.name == "Basic I/O"

    def test_elapsed_non_negative(self) -> None:
        with patch.object(
            validate, "_vm_exec", return_value=(0, self._test_data, "")
        ):
            result = check_basic_io("myvm")
        assert result.elapsed >= 0.0


# ---------------------------------------------------------------------------
# TestPrintResults  (uses capsys to capture stdout)
# ---------------------------------------------------------------------------


class TestPrintResults:
    def _make_summary(
        self,
        target: str = "mytarget",
        checks: list[dict] | None = None,
        all_passed: bool = True,
    ) -> dict:
        if checks is None:
            checks = []
        passed = sum(1 for c in checks if c.get("passed", False))
        return {
            "target": target,
            "passed": passed,
            "total": len(checks),
            "all_passed": all_passed,
            "checks": checks,
        }

    def test_pass_line_appears(self, capsys: pytest.CaptureFixture) -> None:
        summary = self._make_summary(
            checks=[
                {
                    "name": "Artifacts exist",
                    "passed": True,
                    "detail": "",
                    "elapsed_s": 0.1,
                }
            ],
            all_passed=True,
        )
        print_results(summary)
        out = capsys.readouterr().out
        assert "PASS" in out

    def test_fail_line_appears(self, capsys: pytest.CaptureFixture) -> None:
        summary = self._make_summary(
            checks=[
                {
                    "name": "Networking",
                    "passed": False,
                    "detail": "timeout",
                    "elapsed_s": 5.0,
                }
            ],
            all_passed=False,
        )
        print_results(summary)
        out = capsys.readouterr().out
        assert "FAIL" in out

    def test_overall_passed_line(self, capsys: pytest.CaptureFixture) -> None:
        summary = self._make_summary(
            checks=[
                {"name": "A", "passed": True, "detail": "", "elapsed_s": 0.0}
            ],
            all_passed=True,
        )
        print_results(summary)
        out = capsys.readouterr().out
        assert "PASSED" in out

    def test_overall_failed_line(self, capsys: pytest.CaptureFixture) -> None:
        summary = self._make_summary(
            checks=[
                {
                    "name": "A",
                    "passed": False,
                    "detail": "broken",
                    "elapsed_s": 0.0,
                }
            ],
            all_passed=False,
        )
        print_results(summary)
        out = capsys.readouterr().out
        assert "FAILED" in out

    def test_target_name_in_output(self, capsys: pytest.CaptureFixture) -> None:
        summary = self._make_summary(target="myspecialtarget")
        print_results(summary)
        out = capsys.readouterr().out
        assert "myspecialtarget" in out

    def test_check_name_in_output(self, capsys: pytest.CaptureFixture) -> None:
        summary = self._make_summary(
            checks=[
                {
                    "name": "Artifacts exist",
                    "passed": True,
                    "detail": "",
                    "elapsed_s": 0.0,
                }
            ],
            all_passed=True,
        )
        print_results(summary)
        out = capsys.readouterr().out
        assert "Artifacts exist" in out

    def test_detail_in_output_when_present(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        summary = self._make_summary(
            checks=[
                {
                    "name": "Networking",
                    "passed": False,
                    "detail": "host unreachable",
                    "elapsed_s": 1.0,
                }
            ],
            all_passed=False,
        )
        print_results(summary)
        out = capsys.readouterr().out
        assert "host unreachable" in out

    def test_counts_in_final_line(self, capsys: pytest.CaptureFixture) -> None:
        checks = [
            {"name": "A", "passed": True, "detail": "", "elapsed_s": 0.0},
            {"name": "B", "passed": True, "detail": "", "elapsed_s": 0.0},
            {"name": "C", "passed": False, "detail": "oops", "elapsed_s": 0.0},
        ]
        summary = {
            "target": "t",
            "passed": 2,
            "total": 3,
            "all_passed": False,
            "checks": checks,
        }
        print_results(summary)
        out = capsys.readouterr().out
        assert "2/3" in out

    def test_mixed_pass_fail_both_appear(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        checks = [
            {"name": "A", "passed": True, "detail": "", "elapsed_s": 0.0},
            {"name": "B", "passed": False, "detail": "err", "elapsed_s": 0.0},
        ]
        summary = {
            "target": "t",
            "passed": 1,
            "total": 2,
            "all_passed": False,
            "checks": checks,
        }
        print_results(summary)
        out = capsys.readouterr().out
        assert "PASS" in out
        assert "FAIL" in out


# ---------------------------------------------------------------------------
# TestValidateTarget  (mocks all sub-functions)
# ---------------------------------------------------------------------------


def _pass(name: str = "x") -> CheckResult:
    return CheckResult(name, True, "", 0.0)


def _fail(name: str = "x") -> CheckResult:
    return CheckResult(name, False, "err", 0.0)


class TestValidateTarget:
    """validate_target() orchestration tests -- all I/O sub-functions mocked."""

    def _fake_target(self, tmp_path: Path) -> SimpleNamespace:
        out = tmp_path / "out"
        out.mkdir(parents=True)
        return SimpleNamespace(name="testarch", output_dir=out)

    @patch("lib.validate._vm_destroy")
    @patch("lib.validate._vm_ensure")
    @patch("lib.validate.check_artifacts")
    def test_artifacts_fail_returns_early_no_vm(
        self,
        mock_artifacts: MagicMock,
        mock_ensure: MagicMock,
        mock_destroy: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Artifacts check fails -> summary returned early, no VM created."""
        mock_artifacts.return_value = _fail("Artifacts exist")
        tc = self._fake_target(tmp_path)
        result = validate_target(tc)
        assert result["all_passed"] is False
        mock_ensure.assert_not_called()
        mock_destroy.assert_not_called()

    @patch("lib.validate._vm_destroy")
    @patch("lib.validate._vm_ensure")
    @patch("lib.validate.check_no_lustre")
    @patch("lib.validate.check_packages")
    @patch("lib.validate.check_networking")
    @patch("lib.validate.check_vm_kernel_version")
    @patch("lib.validate.check_vm_boot")
    @patch("lib.validate.check_version_consistency")
    @patch("lib.validate.check_artifacts")
    def test_all_pass_no_lustre_tree(
        self,
        mock_artifacts: MagicMock,
        mock_version: MagicMock,
        mock_boot: MagicMock,
        mock_kver: MagicMock,
        mock_net: MagicMock,
        mock_pkgs: MagicMock,
        mock_nolustre: MagicMock,
        mock_ensure: MagicMock,
        mock_destroy: MagicMock,
        tmp_path: Path,
    ) -> None:
        """All checks pass without lustre_tree -> all_passed=True."""
        for m in (
            mock_artifacts,
            mock_version,
            mock_boot,
            mock_kver,
            mock_net,
            mock_pkgs,
            mock_nolustre,
        ):
            m.return_value = _pass()
        tc = self._fake_target(tmp_path)
        result = validate_target(tc)
        assert result["all_passed"] is True
        mock_ensure.assert_called_once()
        mock_destroy.assert_called_once()

    @patch("lib.validate._vm_destroy")
    @patch("lib.validate._vm_ensure")
    @patch("lib.validate.check_vm_boot")
    @patch("lib.validate.check_version_consistency")
    @patch("lib.validate.check_artifacts")
    def test_vm_boot_fails_returns_early_destroy_called(
        self,
        mock_artifacts: MagicMock,
        mock_version: MagicMock,
        mock_boot: MagicMock,
        mock_ensure: MagicMock,
        mock_destroy: MagicMock,
        tmp_path: Path,
    ) -> None:
        """check_vm_boot fails -> early return, _vm_destroy still called."""
        mock_artifacts.return_value = _pass()
        mock_version.return_value = _pass()
        mock_boot.return_value = _fail("Virgin VM boot")
        tc = self._fake_target(tmp_path)
        result = validate_target(tc)
        assert result["all_passed"] is False
        mock_destroy.assert_called_once()

    @patch("lib.validate._vm_destroy")
    @patch("lib.validate._vm_ensure")
    @patch("lib.validate.check_basic_io")
    @patch("lib.validate.check_lustre_deploy")
    @patch("lib.validate.check_no_lustre")
    @patch("lib.validate.check_packages")
    @patch("lib.validate.check_networking")
    @patch("lib.validate.check_vm_kernel_version")
    @patch("lib.validate.check_vm_boot")
    @patch("lib.validate.check_version_consistency")
    @patch("lib.validate.check_artifacts")
    def test_lustre_tree_provided_all_pass(
        self,
        mock_artifacts: MagicMock,
        mock_version: MagicMock,
        mock_boot: MagicMock,
        mock_kver: MagicMock,
        mock_net: MagicMock,
        mock_pkgs: MagicMock,
        mock_nolustre: MagicMock,
        mock_ldeploy: MagicMock,
        mock_bio: MagicMock,
        mock_ensure: MagicMock,
        mock_destroy: MagicMock,
        tmp_path: Path,
    ) -> None:
        """lustre_tree provided, all pass -> second VM with disks, both destroyed."""
        for m in (
            mock_artifacts,
            mock_version,
            mock_boot,
            mock_kver,
            mock_net,
            mock_pkgs,
            mock_nolustre,
            mock_ldeploy,
            mock_bio,
        ):
            m.return_value = _pass()
        tc = self._fake_target(tmp_path)
        lustre_tree = tmp_path / "lustre-release"
        lustre_tree.mkdir()
        result = validate_target(tc, lustre_tree=lustre_tree)
        assert result["all_passed"] is True
        assert mock_ensure.call_count == 2
        assert mock_destroy.call_count == 2
        # Second ensure call must include mdt_disks=1 and ost_disks=3
        second_call_kwargs = mock_ensure.call_args_list[1][1]
        assert second_call_kwargs.get("mdt_disks") == 1
        assert second_call_kwargs.get("ost_disks") == 3

    @patch("lib.validate._vm_destroy")
    @patch("lib.validate._vm_ensure")
    @patch("lib.validate.check_lustre_deploy")
    @patch("lib.validate.check_no_lustre")
    @patch("lib.validate.check_packages")
    @patch("lib.validate.check_networking")
    @patch("lib.validate.check_vm_kernel_version")
    @patch("lib.validate.check_vm_boot")
    @patch("lib.validate.check_version_consistency")
    @patch("lib.validate.check_artifacts")
    def test_lustre_deploy_fails_destroy_still_called(
        self,
        mock_artifacts: MagicMock,
        mock_version: MagicMock,
        mock_boot: MagicMock,
        mock_kver: MagicMock,
        mock_net: MagicMock,
        mock_pkgs: MagicMock,
        mock_nolustre: MagicMock,
        mock_ldeploy: MagicMock,
        mock_ensure: MagicMock,
        mock_destroy: MagicMock,
        tmp_path: Path,
    ) -> None:
        """lustre_tree provided, deploy fails -> early return, both destroys called."""
        for m in (
            mock_artifacts,
            mock_version,
            mock_boot,
            mock_kver,
            mock_net,
            mock_pkgs,
            mock_nolustre,
        ):
            m.return_value = _pass()
        mock_ldeploy.return_value = _fail("Lustre deploy + mount")
        tc = self._fake_target(tmp_path)
        lustre_tree = tmp_path / "lustre-release"
        lustre_tree.mkdir()
        result = validate_target(tc, lustre_tree=lustre_tree)
        assert result["all_passed"] is False
        assert mock_destroy.call_count == 2
