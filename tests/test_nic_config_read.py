"""Exercise the guest config byte limit through its real Linux shell read."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from ltvm_pkg import nic_guest
from ltvm_pkg.nic_spec import NicSpec
from ltvm_pkg.vm_state import VMInfo

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="guest config reads require native Linux"
)

_GUEST_CONFIG = "/etc/modprobe.d/lnet.conf"
_PREFIX = b'options lnet networks="tcp0(eth1)"\n'


def _vm() -> VMInfo:
    """Request one named network without touching a real guest."""
    return VMInfo(
        name="client",
        ip="203.0.113.50",
        tap="tap-client",
        mac="aa:fc:00:01:02:03",
        nics=[NicSpec.parse("tcp:net=tcp0")],
    )


def _content(size: int, *, crlf: bool = False) -> bytes:
    """Pad a valid config with one comment to an exact physical byte size."""
    prefix = _PREFIX.replace(b"\n", b"\r\n") if crlf else _PREFIX
    return prefix + b"#" + b"x" * (size - len(prefix) - 1)


@pytest.fixture
def config_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, list[subprocess.CompletedProcess[str]]]:
    """Run only the shipped read command against a private replacement file."""
    config = tmp_path / "lnet.conf"
    results: list[subprocess.CompletedProcess[str]] = []

    def read_ssh(
        ip: str, command: str, *, timeout: int, retry_connect: bool
    ) -> subprocess.CompletedProcess[str]:
        """Keep shell behavior real while replacing only the fixed file path."""
        assert ip == "203.0.113.50"
        assert retry_connect is False
        assert command == nic_guest._CONFIG_READ
        local_command = command.replace(_GUEST_CONFIG, shlex.quote(str(config)))
        result = subprocess.run(
            ["bash", "-c", local_command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        results.append(result)
        return result

    monkeypatch.setattr(nic_guest, "run_ssh", read_ssh)
    return config, results


def test_exact_64_kib_config_is_accepted_and_unchanged(
    config_probe: tuple[Path, list[subprocess.CompletedProcess[str]]],
) -> None:
    """The limit is inclusive and a successful probe never rewrites config."""
    config, results = config_probe
    body = _content(65536)
    config.write_bytes(body)

    nic_guest._check_networks(_vm())

    assert len(results) == 1
    assert results[0].returncode == 0
    assert len(results[0].stdout.encode("utf-8")) == 65536
    assert config.read_bytes() == body


@pytest.mark.parametrize("crlf", [False, True])
def test_overlimit_config_is_rejected_before_text_normalization(
    config_probe: tuple[Path, list[subprocess.CompletedProcess[str]]],
    crlf: bool,
) -> None:
    """CRLF conversion cannot hide a physical byte above the file limit."""
    config, results = config_probe
    body = _content(65537, crlf=crlf)
    config.write_bytes(body)

    with pytest.raises(ValueError, match="LNet configuration exceeds 64 KiB"):
        nic_guest._check_networks(_vm())

    assert len(results) == 1
    assert results[0].returncode != 0
    assert results[0].stdout == ""
    assert "LNet configuration exceeds 64 KiB" in results[0].stderr
    assert config.read_bytes() == body


@pytest.mark.parametrize("size", [65536, 65537])
def test_symlink_is_checked_against_target_size(
    config_probe: tuple[Path, list[subprocess.CompletedProcess[str]]],
    size: int,
) -> None:
    """A small symlink must not bypass the bound on its config target."""
    config, results = config_probe
    target = config.parent / "target.conf"
    body = _content(size)
    target.write_bytes(body)
    config.symlink_to(target.name)

    if size == 65536:
        # A symlink to a supported file remains a valid operator setup.
        nic_guest._check_networks(_vm())
        assert results[0].returncode == 0
    else:
        # Check the target's bytes rather than the link path's length.
        with pytest.raises(ValueError, match="exceeds 64 KiB"):
            nic_guest._check_networks(_vm())
        assert results[0].returncode != 0

    assert len(results) == 1
    assert config.is_symlink()
    assert config.readlink() == Path(target.name)
    assert target.read_bytes() == body


def test_missing_config_retains_the_shell_failure_status(
    config_probe: tuple[Path, list[subprocess.CompletedProcess[str]]],
) -> None:
    """A failed stat cannot become a successful empty config read."""
    config, results = config_probe

    with pytest.raises(ValueError, match="cannot read NIC configuration"):
        nic_guest._check_networks(_vm())

    assert len(results) == 1
    assert results[0].returncode != 0
    assert results[0].stdout == ""
    assert "stat:" in results[0].stderr
    assert not config.exists()
