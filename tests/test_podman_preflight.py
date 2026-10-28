"""Tests for the macOS podman-machine preflight check.

Covers ``check_podman_machine_macos`` in ``ltvm_pkg.host_setup`` and its
wiring into the build CLI commands via ``_preflight_podman``.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg.cli import EXIT_ERROR, EXIT_OK
from ltvm_pkg.cli.build import (
    _preflight_container as _real_preflight_container,
)
from ltvm_pkg.host_setup import (
    PodmanMachineError,
    check_podman_machine_macos,
)

_LTVM_PATH = str(Path(__file__).parent.parent / "ltvm")


def _load_ltvm() -> Any:
    loader = importlib.machinery.SourceFileLoader("ltvm", _LTVM_PATH)
    spec = importlib.util.spec_from_loader("ltvm", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ltvm = _load_ltvm()


def _run_main(argv: list[str]) -> int:
    with patch.object(sys, "argv", ["ltvm"] + argv):
        return ltvm.main()


class TestCheckPodmanMachineMacos:
    def test_noop_on_non_macos(self) -> None:
        with patch("ltvm_pkg.host_setup.is_macos", return_value=False):
            check_podman_machine_macos()

    def test_missing_podman_binary(self) -> None:
        with (
            patch("ltvm_pkg.host_setup.is_macos", return_value=True),
            patch("ltvm_pkg.host_setup.shutil.which", return_value=None),
        ):
            with pytest.raises(PodmanMachineError) as exc:
                check_podman_machine_macos()
        assert "brew install podman" in str(exc.value)

    def test_no_machines_defined(self) -> None:
        fake = MagicMock(returncode=0, stdout="[]")
        with (
            patch("ltvm_pkg.host_setup.is_macos", return_value=True),
            patch(
                "ltvm_pkg.host_setup.shutil.which",
                return_value="/opt/homebrew/bin/podman",
            ),
            patch("ltvm_pkg.host_setup.subprocess.run", return_value=fake),
        ):
            with pytest.raises(PodmanMachineError) as exc:
                check_podman_machine_macos()
        assert "podman machine init" in str(exc.value)

    def test_machine_defined_but_not_running_auto_starts(self) -> None:
        list_result = MagicMock(
            returncode=0,
            stdout='[{"Name": "podman-machine-default", "Running": false}]',
        )
        start_result = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("ltvm_pkg.host_setup.is_macos", return_value=True),
            patch(
                "ltvm_pkg.host_setup.shutil.which",
                return_value="/opt/homebrew/bin/podman",
            ),
            patch(
                "ltvm_pkg.host_setup.subprocess.run",
                side_effect=[list_result, start_result],
            ) as run_mock,
        ):
            check_podman_machine_macos()
        # Second call is `podman machine start`.
        second_call_args = run_mock.call_args_list[1].args[0]
        assert second_call_args[:3] == ["podman", "machine", "start"]

    def test_running_machine_passes(self) -> None:
        fake = MagicMock(
            returncode=0,
            stdout='[{"Name": "podman-machine-default", "Running": true}]',
        )
        with (
            patch("ltvm_pkg.host_setup.is_macos", return_value=True),
            patch(
                "ltvm_pkg.host_setup.shutil.which",
                return_value="/opt/homebrew/bin/podman",
            ),
            patch("ltvm_pkg.host_setup.subprocess.run", return_value=fake),
        ):
            check_podman_machine_macos()

    def test_query_failure_surfaces_message(self) -> None:
        with (
            patch("ltvm_pkg.host_setup.is_macos", return_value=True),
            patch(
                "ltvm_pkg.host_setup.shutil.which",
                return_value="/opt/homebrew/bin/podman",
            ),
            patch(
                "ltvm_pkg.host_setup.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="podman", timeout=10),
            ),
        ):
            with pytest.raises(PodmanMachineError) as exc:
                check_podman_machine_macos()
        assert "podman machine" in str(exc.value)


class TestCliPreflight:
    """The build commands should refuse to proceed when the macOS preflight fails."""

    def test_build_container_fails_fast(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ltvm_pkg.cli import build as build_mod

        monkeypatch.setattr(
            build_mod,
            "check_podman_machine_macos",
            MagicMock(
                side_effect=PodmanMachineError(
                    "On macOS, container builds require a running podman machine."
                )
            ),
        )
        rc = _run_main(["build", "container", "rocky9"])
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "running podman machine" in err

    def test_build_kernel_fails_fast(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ltvm_pkg.cli import build as build_mod

        monkeypatch.setattr(
            build_mod,
            "check_podman_machine_macos",
            MagicMock(side_effect=PodmanMachineError("need podman machine")),
        )
        rc = _run_main(["build", "kernel", "rocky9"])
        assert rc == EXIT_ERROR
        assert "need podman machine" in capsys.readouterr().err

    def test_build_image_fails_fast(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ltvm_pkg.cli import build as build_mod

        monkeypatch.setattr(
            build_mod,
            "check_podman_machine_macos",
            MagicMock(side_effect=PodmanMachineError("need podman machine")),
        )
        rc = _run_main(["build", "image", "rocky9"])
        assert rc == EXIT_ERROR
        assert "need podman machine" in capsys.readouterr().err

    def test_build_lustre_fails_fast(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ltvm_pkg.cli import build as build_mod

        monkeypatch.setattr(
            build_mod,
            "check_podman_machine_macos",
            MagicMock(side_effect=PodmanMachineError("need podman machine")),
        )
        rc = _run_main(["build", "lustre", "rocky9"])
        assert rc == EXIT_ERROR
        assert "need podman machine" in capsys.readouterr().err

    def test_build_all_fails_fast(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ltvm_pkg.cli import build as build_mod

        monkeypatch.setattr(
            build_mod,
            "check_podman_machine_macos",
            MagicMock(side_effect=PodmanMachineError("need podman machine")),
        )
        rc = _run_main(["build", "all", "rocky9"])
        assert rc == EXIT_ERROR
        assert "need podman machine" in capsys.readouterr().err

    def test_build_shell_fails_fast(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from ltvm_pkg.cli import build as build_mod

        monkeypatch.setattr(
            build_mod,
            "check_podman_machine_macos",
            MagicMock(side_effect=PodmanMachineError("need podman machine")),
        )
        rc = _run_main(["build", "shell", "rocky9", str(tmp_path)])
        assert rc == EXIT_ERROR
        assert "need podman machine" in capsys.readouterr().err

    def test_build_mofed_kmods_fails_fast(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ltvm_pkg.cli import build as build_mod

        monkeypatch.setattr(
            build_mod,
            "check_podman_machine_macos",
            MagicMock(side_effect=PodmanMachineError("need podman machine")),
        )
        rc = _run_main(
            ["build", "mofed-kmods", "rocky9", "--variant", "mofed-24"]
        )
        # Either the preflight fires (EXIT_ERROR) or an earlier arg
        # validation path does; in both cases we should NOT reach EXIT_OK.
        assert rc != EXIT_OK


class TestPreflightContainerHelper:
    """``_preflight_container`` validates the cached image before use."""

    @pytest.fixture(autouse=True)
    def _container_dir(self, tmp_path: Path) -> None:
        self.container_dir = tmp_path

    def _tc(self, tag: str = "ltvm-build-rocky9", arch: str = "x86_64") -> Any:
        tc = MagicMock()
        tc.container_tag = tag
        tc.name = "rocky9"
        tc.arch = arch
        tc.container_output_dir.return_value = self.container_dir
        return tc

    def test_saved_container_is_loaded_when_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another user fetched it: the tarball is on disk, not in our store."""
        from ltvm_pkg.cli import build as build_mod

        (self.container_dir / "image.tar").write_text("")
        run = MagicMock(
            side_effect=[
                MagicMock(returncode=1),
                MagicMock(returncode=0, stdout="Loaded image: x\n"),
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout="x86_64\n"),
            ]
        )
        monkeypatch.setattr(build_mod.subprocess, "run", run)
        monkeypatch.setattr(build_mod.platform, "machine", lambda: "x86_64")
        assert _real_preflight_container(self._tc(), False) is None
        assert run.call_args_list[1].args[0] == [
            "podman",
            "load",
            "-i",
            str(self.container_dir / "image.tar"),
        ]

    def test_failed_load_still_reports_missing(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ltvm_pkg.cli import build as build_mod

        (self.container_dir / "image.tar").write_text("")
        monkeypatch.setattr(
            build_mod.subprocess,
            "run",
            MagicMock(
                side_effect=[
                    MagicMock(returncode=1),
                    MagicMock(returncode=125, stderr="bad tarball"),
                ]
            ),
        )
        assert _real_preflight_container(self._tc(), False) == EXIT_ERROR
        err = capsys.readouterr().err
        assert "podman load failed (rc=125): bad tarball" in err
        assert "ltvm-build-rocky9 not found" in err

    def test_pass_when_image_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg.cli import build as build_mod

        monkeypatch.setattr(
            build_mod.subprocess,
            "run",
            MagicMock(
                side_effect=[
                    MagicMock(returncode=0),
                    MagicMock(returncode=0, stdout="x86_64\n"),
                ]
            ),
        )
        monkeypatch.setattr(build_mod.platform, "machine", lambda: "x86_64")
        assert _real_preflight_container(self._tc(), False) is None

    def test_error_when_image_missing(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ltvm_pkg.cli import build as build_mod

        monkeypatch.setattr(
            build_mod.subprocess,
            "run",
            MagicMock(return_value=MagicMock(returncode=1)),
        )
        rc = _real_preflight_container(self._tc(), False)
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "ltvm-build-rocky9 not found" in err
        assert "ltvm build container rocky9" in err

    def test_error_when_podman_missing(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ltvm_pkg.cli import build as build_mod

        monkeypatch.setattr(
            build_mod.subprocess,
            "run",
            MagicMock(side_effect=FileNotFoundError()),
        )
        rc = _real_preflight_container(self._tc(), False)
        assert rc == EXIT_ERROR
        assert "podman not found" in capsys.readouterr().err

    def test_error_when_cached_image_is_not_host_native(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reject a fetched AMD64 builder on an Apple Silicon host early."""
        from ltvm_pkg.cli import build as build_mod

        monkeypatch.setattr(
            build_mod.subprocess,
            "run",
            MagicMock(
                side_effect=[
                    MagicMock(returncode=0),
                    MagicMock(returncode=0, stdout="amd64\n"),
                ]
            ),
        )
        monkeypatch.setattr(build_mod.platform, "machine", lambda: "arm64")

        rc = _real_preflight_container(self._tc(), False)

        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "ltvm-build-rocky9 is x86_64" in err
        assert "host is aarch64" in err
        assert "ltvm build container rocky9 --arch x86_64" in err

    def test_error_when_image_architecture_cannot_be_inspected(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ltvm_pkg.cli import build as build_mod

        monkeypatch.setattr(
            build_mod.subprocess,
            "run",
            MagicMock(
                side_effect=[
                    MagicMock(returncode=0),
                    MagicMock(returncode=125, stdout=""),
                ]
            ),
        )

        rc = _real_preflight_container(self._tc(), False)

        assert rc == EXIT_ERROR
        assert "could not determine architecture" in capsys.readouterr().err


class TestCliContainerPreflight:
    """Container preflight fires on kernel/image/lustre/shell/mofed-kmods."""

    def _install_missing_container(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ltvm_pkg.cli import build as build_mod

        def fake_run(*a: Any, **kw: Any) -> Any:
            return MagicMock(returncode=1)

        monkeypatch.setattr(build_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(
            build_mod, "_preflight_container", _real_preflight_container
        )

    def test_build_kernel_fails_fast(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._install_missing_container(monkeypatch)
        rc = _run_main(["build", "kernel", "rocky9"])
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "not found" in err
        assert "ltvm build container rocky9" in err

    def test_build_image_fails_fast(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._install_missing_container(monkeypatch)
        rc = _run_main(["build", "image", "rocky9"])
        assert rc == EXIT_ERROR
        assert "ltvm build container rocky9" in capsys.readouterr().err

    def test_build_lustre_fails_fast(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._install_missing_container(monkeypatch)
        rc = _run_main(["build", "lustre", "rocky9"])
        assert rc == EXIT_ERROR
        assert "ltvm build container rocky9" in capsys.readouterr().err

    def test_build_lustre_rejects_wrong_arch_before_running(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An AMD64 cache cannot be used by an Apple Silicon build."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.cli import build as build_mod

        monkeypatch.setattr(
            build_mod.subprocess,
            "run",
            MagicMock(
                side_effect=[
                    MagicMock(returncode=0),
                    MagicMock(returncode=0, stdout="amd64\n"),
                ]
            ),
        )
        monkeypatch.setattr(build_mod.platform, "machine", lambda: "arm64")
        monkeypatch.setattr(
            build_mod, "_preflight_container", _real_preflight_container
        )
        build_lustre = MagicMock()
        monkeypatch.setattr(cli_mod, "build_lustre", build_lustre)

        rc = _run_main(["build", "lustre", "rocky9", "--arch", "x86_64"])

        assert rc == EXIT_ERROR
        assert not build_lustre.called
        assert "Rebuild it: ltvm build container rocky9 --arch x86_64" in (
            capsys.readouterr().err
        )

    def test_build_shell_fails_fast(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self._install_missing_container(monkeypatch)
        rc = _run_main(["build", "shell", "rocky9", str(tmp_path)])
        assert rc == EXIT_ERROR
        assert "ltvm build container rocky9" in capsys.readouterr().err

    def test_build_container_skips_preflight(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`ltvm build container` must not preflight its own output."""
        from ltvm_pkg import cli as cli_mod

        self._install_missing_container(monkeypatch)
        monkeypatch.setattr(
            cli_mod,
            "_do_build_container",
            MagicMock(return_value="ltvm-build-rocky9"),
        )
        rc = _run_main(["build", "container", "rocky9"])
        assert rc == EXIT_OK

    def test_build_all_skips_preflight(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """`ltvm build all` builds the container first; no preflight."""
        from ltvm_pkg import cli as cli_mod

        self._install_missing_container(monkeypatch)
        monkeypatch.setattr(
            cli_mod,
            "_do_build_container",
            MagicMock(return_value="ltvm-build-rocky9"),
        )
        monkeypatch.setattr(
            cli_mod,
            "_resolve_lustre_tree",
            MagicMock(return_value=(tmp_path, None)),
        )
        monkeypatch.setattr(
            cli_mod,
            "_gate_lustre_validation",
            MagicMock(return_value=None),
        )
        monkeypatch.setattr(
            cli_mod,
            "build_kernel",
            MagicMock(return_value={"ok": True}),
        )
        monkeypatch.setattr(
            cli_mod,
            "build_lustre",
            MagicMock(return_value={"ok": True}),
        )
        monkeypatch.setattr(
            cli_mod,
            "snapshot_lustre",
            MagicMock(),
        )
        monkeypatch.setattr(
            cli_mod,
            "build_image",
            MagicMock(return_value=tmp_path),
        )
        rc = _run_main(["build", "all", "rocky9", "--yes"])
        err = capsys.readouterr().err
        assert "build container ltvm-build-rocky9 not found" not in err
        assert rc == EXIT_OK


class _OtherProcess:
    """Another process holding the usage lock, or probing it."""

    _HOLD = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_SH)\n"
        "print('held', flush=True)\n"
        "sys.stdin.read()\n"
    )
    _PROBE = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
        "op = fcntl.LOCK_EX if sys.argv[2] == 'ex' else fcntl.LOCK_SH\n"
        "try:\n"
        "    fcntl.flock(fd, op | fcntl.LOCK_NB)\n"
        "except OSError:\n"
        "    sys.exit(1)\n"
    )

    @classmethod
    def can_lock(cls, lock: Path, mode: str) -> bool:
        # Popen, not run: a test may have patched subprocess.run.
        p = subprocess.Popen(
            [sys.executable, "-c", cls._PROBE, str(lock), mode]
        )
        return p.wait(timeout=10) == 0

    def __init__(self, lock: Path) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", self._HOLD, str(lock)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        assert self.proc.stdout.readline().strip() == "held"

    def release(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


class TestPodmanUsageLock:
    """The advisory lock that keeps the macOS machine auto-stop from
    stopping the machine under a concurrent build (in-flight `podman
    build`/loads/pulls have no container for the ps guard to see)."""

    @pytest.fixture
    def lock(self, monkeypatch: pytest.MonkeyPatch) -> Path:
        import ltvm_pkg.host_setup as hs

        monkeypatch.setattr(hs, "is_macos", lambda: True)
        return hs._podman_lock_path()

    def test_hold_is_shared_and_kept(self, lock: Path) -> None:
        import ltvm_pkg.host_setup as hs

        hs.hold_podman_usage()
        hs.hold_podman_usage()  # idempotent
        with hs.podman_usage_lock():
            pass
        # Still held after the block: until exit, or the auto-stop.
        assert _OtherProcess.can_lock(lock, "sh")
        assert not _OtherProcess.can_lock(lock, "ex")

    def test_no_hold_off_macos(
        self, lock: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ltvm_pkg.host_setup as hs

        monkeypatch.setattr(hs, "is_macos", lambda: False)
        hs.hold_podman_usage()
        assert hs._usage_fd is None

    def test_idle_lock_refused_while_another_process_holds(
        self, lock: Path
    ) -> None:
        import ltvm_pkg.host_setup as hs

        hs.hold_podman_usage()
        other = _OtherProcess(lock)
        try:
            with hs.podman_idle_lock() as idle:
                assert idle is False
        finally:
            other.release()
        assert hs._usage_fd is None

    def test_idle_lock_is_exclusive_through_the_block(self, lock: Path) -> None:
        """A build starting during the stop waits for it, rather than
        finding the machine running and losing it a moment later."""
        import ltvm_pkg.host_setup as hs

        hs.hold_podman_usage()
        with hs.podman_idle_lock() as idle:
            assert idle is True
            assert not _OtherProcess.can_lock(lock, "sh")
        assert hs._usage_fd is None
        assert _OtherProcess.can_lock(lock, "ex")

    def test_preflight_holds_before_looking_at_the_machine(
        self, lock: Path
    ) -> None:
        """The machine is found running only under the hold, so no
        other build's auto-stop can stop it between the check and this
        build's first podman command."""
        import ltvm_pkg.host_setup as hs

        held: list[bool] = []

        def fake_run(cmd: list[str], **kw: Any) -> MagicMock:
            held.append(not _OtherProcess.can_lock(lock, "ex"))
            return MagicMock(
                returncode=0, stdout='[{"Name": "m", "Running": true}]'
            )

        with (
            patch.object(hs.shutil, "which", return_value="/bin/podman"),
            patch.object(hs.subprocess, "run", side_effect=fake_run),
        ):
            hs.check_podman_machine_macos()
        assert held == [True]


class TestAutostopConcurrency:
    def _run_cm(
        self, monkeypatch: pytest.MonkeyPatch, *, other_build: bool
    ) -> list[int]:
        import ltvm_pkg.host_setup as hs
        from ltvm_pkg.cli import build as bmod

        stops: list[int] = []
        monkeypatch.setattr(hs, "is_macos", lambda: True)
        monkeypatch.setattr(bmod, "is_macos", lambda: True)
        monkeypatch.setattr(
            bmod, "should_stop_podman_machine_macos", lambda: True
        )
        monkeypatch.setattr(
            bmod, "stop_podman_machine_macos", lambda: stops.append(1)
        )
        other = _OtherProcess(hs._podman_lock_path()) if other_build else None
        try:
            with bmod._podman_machine_autostop() as h:
                h.success = True
        finally:
            if other is not None:
                other.release()
        return stops

    def test_stop_skipped_while_other_build_in_flight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._run_cm(monkeypatch, other_build=True) == []

    def test_stop_fires_when_idle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._run_cm(monkeypatch, other_build=False) == [1]
