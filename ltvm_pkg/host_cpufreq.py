"""The host's cpufreq governor, which sets how fast a vCPU runs.

A vCPU thread runs in short bursts between halts, on whichever CPU the
host scheduler picks, so a governor that follows per-CPU load
(schedutil, ondemand, conservative) leaves the CPUs it lands on at their
lowest clock.  On Sookie (acpi-cpufreq, EPYC 7551) the performance
governor took the sanity subset in docs/VM_EXIT_OVERHEAD.md from 190 s
to 152 s and doubled small LNet RPC rates.

``ltvm doctor`` reports such a governor and ``--fix`` sets performance,
now and at boot through a tmpfiles.d line.  ``[host] cpufreq = leave``
in /etc/ltvm.conf keeps a host's own choice (a laptop, say) and turns
the report into a note.  Drivers that pick the frequency in hardware
(intel_pstate and amd-pstate-epp in active mode, where "powersave" is
the normal setting) are left alone: they were not measured.
"""

from __future__ import annotations

import configparser
import os
import subprocess
from collections import Counter
from pathlib import Path

from . import site_config
from .priv import atomic_write

SYSFS_CPU = Path("/sys/devices/system/cpu")
TMPFILES = Path("/etc/tmpfiles.d/ltvm-cpufreq.conf")
WANT = "performance"
LOAD_FOLLOWING = ("schedutil", "ondemand", "conservative")
HARDWARE_DRIVERS = ("intel_pstate", "amd-pstate-epp")
TMPFILES_TEXT = (
    "# ltvm: vCPUs run in bursts that load-following governors never\n"
    "# clock up for; see docs/VM_EXIT_OVERHEAD.md in ltvm.\n"
    f"w /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor - - - - {WANT}\n"
)


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def slow_cpus() -> dict[Path, tuple[str, str]]:
    """{scaling_governor file: (governor, driver)} for the CPUs to change."""
    found: dict[Path, tuple[str, str]] = {}
    for gov_file in sorted(
        SYSFS_CPU.glob("cpu[0-9]*/cpufreq/scaling_governor")
    ):
        gov = _read(gov_file)
        driver = _read(gov_file.with_name("scaling_driver")) or ""
        if gov is None or driver in HARDWARE_DRIVERS:
            continue
        if gov in LOAD_FOLLOWING or gov == "powersave":
            found[gov_file] = (gov, driver)
    return found


def left_alone() -> bool:
    """Whether /etc/ltvm.conf says ``[host] cpufreq = leave``."""
    parser = configparser.ConfigParser()
    try:
        parser.read(site_config.path())
        return parser.get("host", "cpufreq", fallback="").strip() == "leave"
    except configparser.Error:
        return False


def _set(files: list[Path]) -> bool:
    data = f"{WANT}\n"
    if os.geteuid() == 0:
        try:
            for f in files:
                f.write_text(data)
        except OSError:
            return False
        return True
    r = subprocess.run(
        ["sudo", "tee", *map(str, files)],
        input=data,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return r.returncode == 0


def apply() -> tuple[bool, str]:
    """Set the governor now and at boot.  Returns (ok, what was done)."""
    files = list(slow_cpus())
    if files and not _set(files):
        return False, f"could not write {WANT} to {len(files)} CPU(s)"
    try:
        atomic_write(TMPFILES, TMPFILES_TEXT)
    except (OSError, subprocess.CalledProcessError) as e:
        return False, f"could not write {TMPFILES}: {e}"
    return True, f"{WANT} on {len(files)} CPU(s), and at boot ({TMPFILES})"


def doctor_report(fix: bool) -> tuple[list[str], list[str], int]:
    """(issue lines, note lines, failed repairs) for ``ltvm doctor``."""
    slow = slow_cpus()
    if not slow:
        return [], [], 0
    counts = Counter(slow.values())
    what = ", ".join(
        f"{gov} on {n} CPU(s) ({driver})" for (gov, driver), n in counts.items()
    )
    line = (
        f"cpufreq governor {what}: vCPUs run at a low clock between "
        f"bursts (docs/VM_EXIT_OVERHEAD.md)"
    )
    if left_alone():
        return [], [f"note: {line}; left as is ([host] cpufreq = leave)"], 0
    if not fix:
        return [f"{line}; --fix sets {WANT}"], [], 0
    ok, done = apply()
    if ok:
        return [line], [f"  fixed: {done}"], 0
    return [line], [f"  NOT fixed: {done}"], 1
