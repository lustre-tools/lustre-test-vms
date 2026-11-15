"""host_cpufreq: report and set a load-following cpufreq governor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ltvm_pkg import host_cpufreq


def _cpus(root: Path, govs: list[str], driver: str = "acpi-cpufreq") -> None:
    for i, gov in enumerate(govs):
        d = root / f"cpu{i}" / "cpufreq"
        d.mkdir(parents=True)
        (d / "scaling_governor").write_text(f"{gov}\n")
        (d / "scaling_driver").write_text(f"{driver}\n")
    # Not CPUs: must not be globbed.
    (root / "cpufreq").mkdir(exist_ok=True)
    (root / "cpuidle").mkdir(exist_ok=True)


@pytest.fixture
def sysfs(tmp_path: Path) -> Path:
    root = tmp_path / "cpu"
    root.mkdir()
    with (
        patch.object(host_cpufreq, "SYSFS_CPU", root),
        patch.object(host_cpufreq, "TMPFILES", tmp_path / "ltvm-cpufreq.conf"),
    ):
        yield root


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    conf = tmp_path / "ltvm.conf"
    monkeypatch.setenv("LTVM_SITE_CONFIG", str(conf))
    return conf


def test_no_cpufreq_is_no_issue(sysfs: Path) -> None:
    """WSL2, VMs, macOS: no cpufreq directory at all."""
    assert host_cpufreq.doctor_report(fix=False) == ([], [], 0)


def test_performance_is_no_issue(sysfs: Path) -> None:
    _cpus(sysfs, ["performance", "performance"])
    assert host_cpufreq.doctor_report(fix=False) == ([], [], 0)


@pytest.mark.parametrize(
    "gov", ["schedutil", "ondemand", "conservative", "powersave"]
)
def test_load_following_governor_is_an_issue(sysfs: Path, gov: str) -> None:
    _cpus(sysfs, [gov, gov, "performance"])
    issues, notes, failures = host_cpufreq.doctor_report(fix=False)
    assert len(issues) == 1 and not notes and failures == 0
    assert f"{gov} on 2 CPU(s) (acpi-cpufreq)" in issues[0]
    assert "--fix" in issues[0]


@pytest.mark.parametrize("driver", ["intel_pstate", "amd-pstate-epp"])
def test_hardware_drivers_are_left_alone(sysfs: Path, driver: str) -> None:
    """Their powersave is the normal mode, and they were not measured."""
    _cpus(sysfs, ["powersave", "powersave"], driver=driver)
    assert host_cpufreq.doctor_report(fix=False) == ([], [], 0)


def test_site_config_leave_makes_it_a_note(sysfs: Path, site: Path) -> None:
    _cpus(sysfs, ["schedutil"])
    site.write_text("[host]\ncpufreq = leave\n")
    with patch.object(host_cpufreq, "_set") as setter:
        issues, notes, failures = host_cpufreq.doctor_report(fix=True)
    assert issues == [] and failures == 0
    assert notes[0].startswith("note: ") and "leave" in notes[0]
    setter.assert_not_called()


def test_fix_sets_governor_and_persists(sysfs: Path) -> None:
    _cpus(sysfs, ["schedutil", "performance", "schedutil"])
    with patch.object(host_cpufreq, "_set", return_value=True) as setter:
        issues, notes, failures = host_cpufreq.doctor_report(fix=True)
    assert len(issues) == 1 and failures == 0
    assert "fixed" in notes[0]
    files = setter.call_args.args[0]
    assert sorted(f.parent.parent.name for f in files) == ["cpu0", "cpu2"]
    text = host_cpufreq.TMPFILES.read_text()
    assert (
        "w /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"
        " - - - - performance\n"
    ) in text


def test_failed_write_is_a_failure(sysfs: Path) -> None:
    _cpus(sysfs, ["schedutil"])
    with patch.object(host_cpufreq, "_set", return_value=False):
        issues, notes, failures = host_cpufreq.doctor_report(fix=True)
    assert failures == 1 and "NOT fixed" in notes[0]
    assert not host_cpufreq.TMPFILES.exists()


def test_set_as_root_writes_the_files(
    sysfs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cpus(sysfs, ["schedutil"])
    monkeypatch.setattr(host_cpufreq.os, "geteuid", lambda: 0)
    files = list(host_cpufreq.slow_cpus())
    assert host_cpufreq._set(files)
    assert files[0].read_text() == "performance\n"
    assert host_cpufreq.slow_cpus() == {}
