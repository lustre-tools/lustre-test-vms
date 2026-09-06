"""The build preflights must stay wired to the build commands.

conftest.py neutralises `_preflight_container` and
`check_podman_machine_macos` with autouse fixtures, so that every unit
test can run on a host with no podman.  The cost is that deleting a
preflight call from a build command would be invisible to all ~1700
of them: the fixture patches the name, and a command that no longer
calls it passes just the same.

These tests close that hole structurally -- they assert the call is
still made, and that a preflight failure still short-circuits the
command.
"""

from __future__ import annotations

import argparse
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg.cli import build as build_cli
from ltvm_pkg.cli.util import EXIT_ERROR

# Every build command that needs a build container present.
_CONTAINER_PREFLIGHT_COMMANDS = (
    "cmd_build_kernel",
    "cmd_build_mofed_kmods",
    "cmd_build_image",
    "cmd_build_lustre",
    "cmd_build_shell",
)


def _args(**kw: Any) -> argparse.Namespace:
    base = dict(
        target="rocky9",
        arch=None,
        variant="base",
        kernel=None,
        json=False,
        force=False,
        lustre_tree=None,
        mofed_version=None,
        all_arches=False,
        # cmd_build_shell resolves its mount path before the preflight.
        path=".",
    )
    base.update(kw)
    return argparse.Namespace(**base)


class TestContainerPreflightStillCalled:
    @pytest.mark.parametrize("cmd_name", _CONTAINER_PREFLIGHT_COMMANDS)
    def test_preflight_failure_short_circuits(self, cmd_name: str) -> None:
        """A failing container preflight must stop the command.

        If the call were dropped, the command would sail past a
        MagicMock returning EXIT_ERROR and do real work instead.
        """
        cmd = getattr(build_cli, cmd_name)
        tc = MagicMock()
        tc.name = "rocky9"
        tc.arch = "x86_64"

        with (
            patch.object(
                build_cli, "_preflight_container", return_value=EXIT_ERROR
            ) as pre,
            patch.object(
                build_cli, "_load_target_args", return_value=(tc, None)
            ),
            patch.object(build_cli, "_preflight_podman", return_value=None),
        ):
            rc = cmd(_args())

        assert pre.called, (
            f"{cmd_name} no longer calls _preflight_container -- the "
            f"autouse fixture in conftest.py hides this from every "
            f"other test"
        )
        assert rc == EXIT_ERROR, (
            f"{cmd_name} ignored a failing container preflight"
        )


class TestPreflightHelpersBehave:
    def test_preflight_podman_maps_error_to_exit_code(self) -> None:
        from ltvm_pkg.host_setup import PodmanMachineError

        with patch.object(
            build_cli,
            "check_podman_machine_macos",
            side_effect=PodmanMachineError("no machine"),
        ):
            assert build_cli._preflight_podman(False) == EXIT_ERROR

    def test_preflight_podman_passes_when_healthy(self) -> None:
        with patch.object(
            build_cli, "check_podman_machine_macos", return_value=None
        ):
            assert build_cli._preflight_podman(False) is None
