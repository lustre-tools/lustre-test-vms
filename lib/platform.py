"""Platform detection helpers."""

from __future__ import annotations

from pathlib import Path


def is_wsl2() -> bool:
    try:
        v = Path("/proc/version").read_text().lower()
        return "microsoft" in v or "wsl" in v
    except OSError:
        return False


def wsl2_has_kvm() -> bool:
    return Path("/dev/kvm").exists()


def wsl2_has_systemd() -> bool:
    return Path("/run/systemd/private").exists()
