"""Check profile configuration at the real staging transfer boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from ltvm_pkg import deploy
from ltvm_pkg.rail_topology import resolve_topology
from ltvm_pkg.vm_state import VMInfo


def _staged_guest(tmp_path: Path, mode: str | None) -> tuple[Path, Path, str]:
    """Create isolated guest and staging roots with different LNet settings."""
    try:
        tar = subprocess.run(
            ["tar", "--version"], capture_output=True, text=True, check=False
        )
    except OSError:
        pytest.skip("needs GNU tar for supported guest extraction options")
    if "GNU tar" not in tar.stdout:
        pytest.skip("needs GNU tar for supported guest extraction options")
    root, stage = tmp_path / "guest", tmp_path / "staging"
    guest_conf = root / "etc/modprobe.d/lnet.conf"
    staged_conf = stage / "etc/modprobe.d/lnet.conf"
    guest_conf.parent.mkdir(parents=True)
    staged_conf.parent.mkdir(parents=True)
    networks = (
        "tcp0(eth1,eth2)" if mode == "shared" else "tcp0(eth1),tcp1(eth2)"
    )
    original = f'options lnet networks="{networks}"\n'
    guest_conf.write_text(original)
    staged_conf.write_text('options lnet networks="tcp0(eth0)"\n')
    (staged_conf.parent / "diagnostic.conf").write_text(
        "unrelated module options\n"
    )
    (stage / ".ltvm-staging-meta.json").write_text("{}")
    module = stage / "lib/modules/test/lustre.ko"
    module.parent.mkdir(parents=True)
    module.write_bytes(b"module content")
    return root, stage, original


@pytest.mark.parametrize("mode", [None, "shared", "isolated"])
@pytest.mark.parametrize("exclude_modules", [False, True])
def test_staging_cannot_replace_profile_lnet_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str | None,
    exclude_modules: bool,
) -> None:
    """Real archives preserve profile LNet config while shipping other staged files."""
    root, stage, original = _staged_guest(tmp_path, mode)
    vm = VMInfo(name="profile-node", ip="192.0.2.1", nics=["tcp", "tcp"])
    if mode is not None:
        vm.rail_topology = resolve_topology(vm.nics, mode)
    requests: list[list[str]] = []
    real_popen = subprocess.Popen

    def record_popen(argv: list[str], **options: Any) -> Any:
        """Run real transport processes and record their complete requests."""
        requests.append(list(argv))
        return real_popen(argv, **options)

    def local_ssh(ip: str, command: str, **options: Any) -> list[str]:
        """Replace SSH with actual extraction into the isolated guest directory."""
        assert ip == vm.ip
        assert (
            command == "tar xzf - -C / --keep-directory-symlink --no-same-owner"
        )
        return [
            "tar",
            "xzf",
            "-",
            "-C",
            str(root),
            "--keep-directory-symlink",
            "--no-same-owner",
        ]

    monkeypatch.setattr(deploy, "sshpass_ssh_argv", local_ssh)
    monkeypatch.setattr(deploy.subprocess, "Popen", record_popen)
    deploy._stream_tree_once(vm, stage, exclude_modules=exclude_modules)
    expected_tar = ["tar", "cf", "-", "-C", str(stage)]
    if exclude_modules:
        expected_tar.append("--exclude=./lib/modules")
    if mode is not None:
        expected_tar.append("--exclude=./etc/modprobe.d/lnet.conf")
    expected_tar += ["--exclude=./.ltvm-*", "."]
    assert requests[0] == expected_tar
    expected = (
        (stage / "etc/modprobe.d/lnet.conf").read_text()
        if mode is None
        else original
    )
    assert (root / "etc/modprobe.d/lnet.conf").read_text() == expected
    assert (
        root / "etc/modprobe.d/diagnostic.conf"
    ).read_text() == "unrelated module options\n"
    assert (root / "lib/modules/test/lustre.ko").exists() is not exclude_modules
    assert not (root / ".ltvm-staging-meta.json").exists()
