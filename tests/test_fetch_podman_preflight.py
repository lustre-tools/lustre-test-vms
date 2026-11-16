"""fetch and publish start a stopped macOS podman machine before using it.

They took only the usage lock, so once the idle stop had stopped the
machine every `target fetch` failed `podman load` with "Cannot connect to
Podman".
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import release_package

# Taken at import, before conftest's autouse stub replaces it.
real_ensure_podman = release_package._ensure_podman


def test_export_build_container_starts_podman_first(tmp_path: Path) -> None:
    calls: list[str] = []

    def run(cmd: list[str], **_kw: object) -> MagicMock:
        calls.append(" ".join(cmd[:2]))
        return MagicMock(returncode=1, stdout="", stderr="")

    with (
        patch.object(
            release_package,
            "_ensure_podman",
            side_effect=lambda: calls.append("preflight"),
        ),
        patch.object(release_package.subprocess, "run", side_effect=run),
        pytest.raises(RuntimeError, match="not found in podman storage"),
    ):
        release_package.export_build_container("rocky9", tmp_path)
    assert calls[:2] == ["preflight", "podman image"]


def test_ensure_podman_runs_the_machine_preflight() -> None:
    # The suite stubs _ensure_podman (conftest); this is the real one.
    with patch("ltvm_pkg.host_setup.check_podman_machine_macos") as check:
        real_ensure_podman()
    check.assert_called_once_with()
