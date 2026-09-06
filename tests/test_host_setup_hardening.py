"""ltvm install must not lie about success or clobber the host's sudo."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import host_setup


class TestSudoersFragment:
    """The fragment used to replace secure_path for every user."""

    def _install(self, tmp_path: Path, current: list[str]) -> str:
        target = tmp_path / "ltvm"
        with (
            patch.object(
                host_setup, "_current_secure_path", return_value=current
            ),
            patch.object(host_setup.subprocess, "run") as run,
        ):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            host_setup._install_sudoers_fragment(target)
        return target.read_text()

    def test_preserves_existing_entries(self, tmp_path: Path) -> None:
        """Ubuntu's secure_path ends in /snap/bin.

        The old hardcoded value dropped it, so `sudo snap install ...`
        started failing with "snap: command not found" after
        `ltvm install`.
        """
        body = self._install(
            tmp_path, ["/usr/sbin", "/usr/bin", "/sbin", "/bin", "/snap/bin"]
        )
        assert "/snap/bin" in body
        assert "/usr/local/bin" in body

    def test_omits_directive_when_nothing_missing(self, tmp_path: Path) -> None:
        """No reason to set secure_path at all when it already works."""
        body = self._install(
            tmp_path,
            [
                "/sbin",
                "/bin",
                "/usr/sbin",
                "/usr/bin",
                "/usr/local/sbin",
                "/usr/local/bin",
            ],
        )
        assert "secure_path" not in body
        assert "LTVM_OWNER_ID" in body

    def test_always_sets_env_keep(self, tmp_path: Path) -> None:
        body = self._install(tmp_path, ["/usr/bin"])
        assert 'env_keep += "LTVM_OWNER_ID"' in body

    def test_visudo_rejection_leaves_no_file(self, tmp_path: Path) -> None:
        """A parse error in /etc/sudoers.d disables sudo entirely."""
        target = tmp_path / "ltvm"
        with (
            patch.object(
                host_setup, "_current_secure_path", return_value=["/usr/bin"]
            ),
            patch.object(host_setup.subprocess, "run") as run,
        ):
            run.return_value = MagicMock(
                returncode=1, stdout="", stderr="syntax error"
            )
            host_setup._install_sudoers_fragment(target)
        assert not target.exists()
        assert not list(tmp_path.glob(".*.tmp"))


class TestQemuAssetVerification:
    def _write(self, tmp_path: Path, data: bytes) -> Path:
        p = tmp_path / "qemu-9.2.2-el9.tar.gz"
        p.write_bytes(data)
        return p

    def test_matching_digest_passes(self, tmp_path: Path) -> None:
        local = self._write(tmp_path, b"tarball contents")
        digest = hashlib.sha256(b"tarball contents").hexdigest()

        def fake_run(cmd, **kw):
            Path(cmd[cmd.index("-o") + 1]).write_text(
                f"{digest}  {local.name}\n"
            )
            return MagicMock(returncode=0)

        with patch.object(host_setup, "_run", side_effect=fake_run):
            host_setup._verify_qemu_asset("https://x/a", local, local.name)

    def test_mismatched_digest_refuses(self, tmp_path: Path) -> None:
        """The check that stands between a substituted asset and /opt."""
        local = self._write(tmp_path, b"tarball contents")

        def fake_run(cmd, **kw):
            Path(cmd[cmd.index("-o") + 1]).write_text(f"{'0' * 64}  x\n")
            return MagicMock(returncode=0)

        with patch.object(host_setup, "_run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="Checksum mismatch"):
                host_setup._verify_qemu_asset("https://x/a", local, local.name)

    def test_absent_digest_warns_but_proceeds(self, tmp_path: Path) -> None:
        """Assets published before .sha256 existed must still install."""
        local = self._write(tmp_path, b"tarball contents")

        with patch.object(
            host_setup, "_run", return_value=MagicMock(returncode=22)
        ):
            host_setup._verify_qemu_asset("https://x/a", local, local.name)


class TestPrerequisitesReportFailure:
    def _host(self) -> MagicMock:
        h = MagicMock()
        h.pkg_mgr = "dnf"
        return h

    def test_still_missing_after_install_raises(self) -> None:
        """`ltvm install` printed "Install complete." and exited 0 with
        an unreachable mirror; the user found out much later when
        `target fetch` died in _check_zstd()."""

        def which(name: str) -> str | None:
            return None if name == "zstd" else "/usr/bin/" + name

        with (
            patch.object(host_setup.shutil, "which", side_effect=which),
            patch.object(host_setup, "_pkg_install"),
        ):
            with pytest.raises(RuntimeError, match="still missing"):
                host_setup.check_prerequisites(self._host())

    def test_successful_install_does_not_raise(self) -> None:
        state: dict = {}

        def which(name: str) -> str | None:
            if name == "zstd" and not state.get("done"):
                return None
            return "/usr/bin/" + name

        with (
            patch.object(host_setup.shutil, "which", side_effect=which),
            patch.object(host_setup, "_pkg_install") as inst,
        ):
            inst.side_effect = lambda *a, **k: state.update(done=True)
            host_setup.check_prerequisites(self._host())
