"""ltvm's own QEMU for macOS: install_qemu_macos_vhost and doctor's use.

The installer runs for real against a temporary QEMU_PREFIX; only the
curl download is faked, from a tarball made here whose
qemu-system-aarch64 is a shell script, with its sum pinned in place of
the real one.
"""

from __future__ import annotations

import hashlib
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from ltvm_pkg import host_setup as hs

FAKE_QEMU = """#!/bin/sh
case "$*" in
*"-device help"*) echo 'name "vhost-user-fs-pci", bus PCI' ;;
esac
"""


def _tarball(tmp: Path, script: str = FAKE_QEMU) -> Path:
    tree = tmp / "tree"
    (tree / "bin").mkdir(parents=True)
    (tree / "lib").mkdir()
    (tree / "share" / "qemu").mkdir(parents=True)
    q = tree / "bin" / "qemu-system-aarch64"
    q.write_text(script)
    q.chmod(0o755)
    out = tmp / "pkg.tar.gz"
    with tarfile.open(out, "w:gz") as t:
        for part in ("bin", "lib", "share"):
            t.add(tree / part, arcname=part)
    return out


@pytest.fixture
def prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "opt-qemu"
    p.mkdir()
    monkeypatch.setattr(hs, "QEMU_PREFIX", p)
    monkeypatch.setattr(hs.platform, "machine", lambda: "arm64")
    return p


def _downloads(pkg: Path, monkeypatch: pytest.MonkeyPatch, sha: str = ""):
    """A _run that serves curl from *pkg* and runs everything else; the
    pinned sum is *pkg*'s, or *sha*."""
    real = hs._run
    digest = sha or hashlib.sha256(pkg.read_bytes()).hexdigest()
    monkeypatch.setattr(hs, "_QEMU_MACOS_SHA256", digest)
    fetched: list[str] = []

    def run(cmd, check=True, quiet=False, cwd=None, env=None):
        if cmd[0] == "curl":
            url, dest = cmd[2], Path(cmd[cmd.index("-o") + 1])
            fetched.append(url)
            dest.write_bytes(pkg.read_bytes())
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real(cmd, check=check, quiet=quiet, cwd=cwd, env=env)

    return run, fetched


def test_installs_and_links(
    prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, fetched = _downloads(_tarball(tmp_path), monkeypatch)
    stale = prefix / "macos-0.0.0-1"
    stale.mkdir()
    with patch.object(hs, "_run", side_effect=run):
        hs.install_qemu_macos_vhost()
    target = hs.qemu_macos_dir() / "bin" / "qemu-system-aarch64"
    link = prefix / "bin" / "qemu-system-aarch64"
    assert link.is_symlink() and link.resolve() == target.resolve()
    assert hs.qemu_macos_installed()
    # the sum is pinned, not fetched from the same release
    assert len(fetched) == 1 and fetched[0].endswith(hs._QEMU_MACOS_ASSET)
    assert hs._QEMU_MACOS_TAG in fetched[0]
    assert not stale.exists()

    # installed: nothing is fetched again
    with patch.object(hs, "_run", side_effect=run):
        hs.install_qemu_macos_vhost()
    assert len(fetched) == 1


def test_relinks_after_brew(
    prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Homebrew's link replaced ours: relink without downloading."""
    run, fetched = _downloads(_tarball(tmp_path), monkeypatch)
    with patch.object(hs, "_run", side_effect=run):
        hs.install_qemu_macos_vhost()
        link = prefix / "bin" / "qemu-system-aarch64"
        link.unlink()
        link.symlink_to("/opt/homebrew/bin/qemu-system-aarch64")
        assert not hs.qemu_macos_installed()
        hs.install_qemu_macos_vhost()
    assert hs.qemu_macos_installed() and len(fetched) == 1


def test_checksum_mismatch_refused(
    prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, _ = _downloads(_tarball(tmp_path), monkeypatch, sha="0" * 64)
    with patch.object(hs, "_run", side_effect=run):
        with pytest.raises(RuntimeError, match="Checksum mismatch"):
            hs.install_qemu_macos_vhost()
    assert not (prefix / "bin" / "qemu-system-aarch64").exists()


def test_binary_without_vhost_user_refused(
    prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, _ = _downloads(_tarball(tmp_path, "#!/bin/sh\nexit 0\n"), monkeypatch)
    with patch.object(hs, "_run", side_effect=run):
        with pytest.raises(RuntimeError, match="vhost-user-fs-pci"):
            hs.install_qemu_macos_vhost()
    assert not (prefix / "bin" / "qemu-system-aarch64").exists()


def test_intel_mac_keeps_homebrew(
    prefix: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hs.platform, "machine", lambda: "x86_64")
    with patch.object(hs, "_run") as run:
        hs.install_qemu_macos_vhost()
    run.assert_not_called()


def test_install_qemu_macos_always_adds_ours() -> None:
    """An existing Homebrew QEMU no longer ends the macOS install early."""
    with (
        patch.object(hs, "_qemu_installed_version", return_value="10.2.2"),
        patch.object(hs, "_install_brew_qemu_macos") as brew,
        patch.object(hs, "install_qemu_macos_vhost") as ours,
    ):
        hs.install_qemu_macos()
    brew.assert_not_called()
    ours.assert_called_once_with(force=False)


def test_doctor_fix_on_macos() -> None:
    from ltvm_pkg import shmrdma_req as sr

    with (
        patch("ltvm_pkg.host_setup.is_macos", return_value=True),
        patch("ltvm_pkg.shmrdma_req._qemu_help", return_value=""),
        patch(
            "ltvm_pkg.shmrdma_req._qemu_has_vhost_user_netdev",
            return_value=False,
        ),
    ):
        reqs = {r.name: r for r in sr.host_checks()}
    for name in ("QEMU vhost-user-fs-pci", "QEMU vhost-user netdev"):
        assert reqs[name].fix is hs.install_qemu_macos_vhost
    numa = reqs["host NUMA nodes"]
    assert not numa.ok and not numa.hard
    assert "taskset" not in reqs


def test_doctor_on_linux_unchanged() -> None:
    from ltvm_pkg import shmrdma_req as sr

    with (
        patch("ltvm_pkg.host_setup.is_macos", return_value=False),
        patch("ltvm_pkg.shmrdma_req._qemu_help", return_value=""),
        patch(
            "ltvm_pkg.shmrdma_req._qemu_has_vhost_user_netdev",
            return_value=False,
        ),
        patch("ltvm_pkg.qemu_run.qemu_binds_numa", return_value=False),
    ):
        reqs = {r.name: r for r in sr.host_checks()}
    assert reqs["QEMU vhost-user-fs-pci"].fix is None
    assert "taskset" in reqs and "host NUMA nodes" not in reqs
