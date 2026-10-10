"""Build container time limits: LTVM_BUILD_TIMEOUT, [build] timeout."""

from __future__ import annotations

import platform
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ltvm_pkg import build_timeout


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_timeout, "_warned", set())


def _site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    site = tmp_path / "ltvm.conf"
    site.write_text(text)
    monkeypatch.setenv("LTVM_SITE_CONFIG", str(site))
    return site


class TestSeconds:
    def test_default_without_settings(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """conftest points LTVM_SITE_CONFIG at a missing file."""
        assert build_timeout.seconds(1234) == 1234
        assert capsys.readouterr().err == ""

    def test_site_file_sets_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _site(tmp_path, monkeypatch, "[build]\ntimeout = 7200\n")
        assert build_timeout.seconds(1234) == 7200

    def test_site_file_without_build_section(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _site(tmp_path, monkeypatch, "[memory]\novercommit = 2\n")
        assert build_timeout.seconds(1234) == 1234
        assert capsys.readouterr().err == ""

    def test_env_beats_site_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _site(tmp_path, monkeypatch, "[build]\ntimeout = 7200\n")
        monkeypatch.setenv("LTVM_BUILD_TIMEOUT", "900")
        assert build_timeout.seconds(1234) == 900

    def test_zero_is_no_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LTVM_BUILD_TIMEOUT", "0")
        assert build_timeout.seconds(1234) == 0
        assert build_timeout.podman_args(0) == []

    @pytest.mark.parametrize("value", ["-1", "1h", "10.5", "lots"])
    def test_bad_env_falls_through_and_warns_once(
        self,
        value: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A typo in the variable must not cancel the host's setting."""
        _site(tmp_path, monkeypatch, "[build]\ntimeout = 7200\n")
        monkeypatch.setenv("LTVM_BUILD_TIMEOUT", value)
        assert build_timeout.seconds(1234) == 7200
        assert build_timeout.seconds(1234) == 7200
        err = capsys.readouterr().err
        assert err.count("warning: ignoring LTVM_BUILD_TIMEOUT") == 1

    def test_bad_site_value_falls_back_to_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        site = _site(tmp_path, monkeypatch, "[build]\ntimeout = forever\n")
        assert build_timeout.seconds(1234) == 1234
        err = capsys.readouterr().err
        assert f"ignoring [build] timeout in {site}" in err

    def test_unparsable_site_file_falls_back_to_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _site(tmp_path, monkeypatch, "this is not ini [[[\n")
        assert build_timeout.seconds(1234) == 1234
        assert "warning: ignoring [build]" in capsys.readouterr().err


class TestPodmanArgs:
    def test_limit(self) -> None:
        assert build_timeout.podman_args(3600) == ["--timeout", "3600"]

    def test_no_limit(self) -> None:
        assert build_timeout.podman_args(0) == []


class TestExplain:
    def test_failure_before_the_limit_is_not_blamed_on_it(self) -> None:
        assert build_timeout.explain(3600, 3599.0) == ""

    def test_failure_at_the_limit_names_both_knobs(self) -> None:
        why = build_timeout.explain(600, 601.5)
        assert "stopped at its 600s limit" in why
        assert "LTVM_BUILD_TIMEOUT=<seconds>" in why
        assert "[build] timeout" in why

    def test_no_limit_never_explains(self) -> None:
        assert build_timeout.explain(0, 99999.0) == ""


def _clock(step: float):
    """A monotonic() whose every read is *step* seconds after the last."""
    now = [0.0]

    def monotonic() -> float:
        now[0] += step
        return now[0]

    return SimpleNamespace(monotonic=monotonic)


class TestZfsWiring:
    """The ZFS container takes its limit, and its failure text, from here."""

    def _run(
        self, tmp_targets: Path, returncode: int = 0, step: float = 0.0
    ) -> tuple[list[str], Exception | None]:
        from ltvm_pkg import zfs_build as zb
        from ltvm_pkg.cross_compile import normalize_arch
        from tests.test_zfs import (
            _KVER,
            _seed_kernel,
            _sha,
            _tarball_bytes,
            _zfs_tc,
        )

        tc = _zfs_tc(tmp_targets)
        cmds: list[list[str]] = []

        def fake_podman(cmd, **kw):
            cmds.append(list(cmd))
            staging = zb.zfs_staging_dir(tc, None, "2.4.0")
            mod = staging / "lib" / "modules" / _KVER / "extra"
            mod.mkdir(parents=True, exist_ok=True)
            (mod / "zfs.ko").write_bytes(b"\x7fELF")
            return SimpleNamespace(returncode=returncode)

        # ZFS refuses a cross-arch build, so build for this host's arch.
        native = normalize_arch(platform.machine())
        err: Exception | None = None
        with patch.object(type(tc), "arch", property(lambda self: native)):
            _seed_kernel(tc)
            tb = tmp_targets / "zfs-2.4.0.tar.gz"
            tb.write_bytes(_tarball_bytes("zfs-2.4.0"))
            with (
                patch.object(
                    zb.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0),
                ),
                patch.object(zb, "fetch_tarball", return_value=tb),
                # test_zfs pins its fake tarball's sum the same way.
                patch.object(
                    zb,
                    "expected_sha256",
                    lambda v: _sha(_tarball_bytes(f"zfs-{v}")),
                ),
                patch.object(
                    zb, "run_podman_with_cleanup", side_effect=fake_podman
                ),
                patch.object(zb, "time", _clock(step)),
            ):
                try:
                    zb.build_zfs(tc, version="2.4.0")
                except zb.ZfsBuildError as e:
                    err = e
        assert cmds, "podman run was not called"
        return cmds[0], err

    def test_default(self, tmp_targets: Path) -> None:
        cmd, err = self._run(tmp_targets)
        assert err is None
        assert cmd[cmd.index("--timeout") + 1] == str(build_timeout.ZFS)

    def test_env_overrides(
        self, tmp_targets: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_BUILD_TIMEOUT", "0")
        cmd, _ = self._run(tmp_targets)
        assert "--timeout" not in cmd

    def test_stopped_at_the_limit_says_so(
        self, tmp_targets: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_BUILD_TIMEOUT", "60")
        _, err = self._run(tmp_targets, returncode=255, step=61.0)
        assert err is not None
        assert "rc=255" in str(err)
        assert "stopped at its 60s limit" in str(err)


class TestMofedWiring:
    """The MOFED kmod container takes its limit from here too."""

    def _run(
        self, tmp_targets: Path, returncode: int = 0, step: float = 0.0
    ) -> tuple[list[str], Exception | None]:
        from ltvm_pkg import mofed_kmod_build as mk
        from tests.test_mofed_kmod_staleness import _mofed_tc, _seed_kernel

        tc = _mofed_tc(tmp_targets)
        _seed_kernel(tc, None, "kernelhash1")
        cmds: list[list[str]] = []

        def fake_podman(cmd, **kw):
            cmds.append(list(cmd))
            if returncode == 0:
                out = mk.mofed_kmod_dir(tc, None)
                (out / "kmod-mlnx-ofa.rpm").write_bytes(b"rpm")
            return SimpleNamespace(returncode=returncode)

        err: Exception | None = None
        with (
            patch.object(
                mk.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ),
            patch.object(
                mk, "run_podman_with_cleanup", side_effect=fake_podman
            ),
            patch.object(mk, "time", _clock(step)),
        ):
            try:
                mk.build_mofed_kmods(tc, force=True)
            except RuntimeError as e:
                err = e
        assert cmds, "podman run was not called"
        return cmds[0], err

    def test_default(self, tmp_targets: Path) -> None:
        cmd, err = self._run(tmp_targets)
        assert err is None
        assert cmd[cmd.index("--timeout") + 1] == str(build_timeout.MOFED_KMODS)

    def test_site_file_overrides(
        self, tmp_targets: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _site(tmp_targets, monkeypatch, "[build]\ntimeout = 5000\n")
        cmd, _ = self._run(tmp_targets)
        assert cmd[cmd.index("--timeout") + 1] == "5000"

    def test_stopped_at_the_limit_says_so(
        self, tmp_targets: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_BUILD_TIMEOUT", "60")
        _, err = self._run(tmp_targets, returncode=255, step=61.0)
        assert err is not None
        assert "stopped at its 60s limit" in str(err)
