"""Check the real guest emitter with explicit per-NIC LNet networks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "targets"
    / "common"
    / "setup-lnet-config.sh"
)
pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="guest shell checks require native Linux"
)


def _run(
    cmdline: str,
    option: str = "--stdin",
    output: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the actual emitter with bounded input and an observed status."""
    args = ["bash", str(SCRIPT), option]
    if output is not None:
        # A file argument exercises the write boundary after validation.
        args.append(str(output))
    return subprocess.run(
        args,
        input=cmdline,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("option", ["-s", "--stdin"])
@pytest.mark.parametrize(
    ("cmdline", "networks"),
    [
        ("quiet", "tcp0(eth0)"),
        ("fc_nics=tcp", "tcp0(eth1)"),
        ("fc_nics=tcp,tcp", "tcp0(eth1),tcp1(eth2)"),
        (
            "fc_nics=" + ",".join(["tcp"] * 9),
            ",".join(f"tcp{idx}(eth{idx + 1})" for idx in range(9)),
        ),
        (
            "fc_nics=" + ",".join(["tcp", "softroce"] * 5),
            ",".join(
                f"{transport}{idx // 2}(eth{idx + 1})"
                for idx, transport in enumerate(["tcp", "o2ib"] * 5)
            ),
        ),
        ("fc_nics=softroce,softroce", "o2ib0(eth1),o2ib1(eth2)"),
        (
            "fc_nics=tcp,tcp fc_nic_nets=tcp0,tcp0",
            "tcp0(eth1,eth2)",
        ),
        (
            "fc_nics=softroce,softroce fc_nic_nets=o2ib4,o2ib4",
            "o2ib4(eth1,eth2)",
        ),
        (
            "fc_nics=tcp,tcp fc_nic_nets=tcp3,tcp8",
            "tcp3(eth1),tcp8(eth2)",
        ),
        (
            "fc_nics=tcp,softroce,tcp fc_nic_nets=tcp4,o2ib4,tcp4",
            "tcp4(eth1,eth3),o2ib4(eth2)",
        ),
        # Automatic names cannot join a later explicit network by accident.
        (
            "fc_nics=tcp,tcp fc_nic_nets=,tcp0",
            "tcp1(eth1),tcp0(eth2)",
        ),
        (
            "fc_nics=tcp,tcp fc_nic_nets=tcp0,",
            "tcp0(eth1),tcp1(eth2)",
        ),
        (
            "fc_nics=tcp,tcp,tcp fc_nic_nets=,tcp0,",
            "tcp1(eth1),tcp0(eth2),tcp2(eth3)",
        ),
        (
            "fc_nics=softroce,tcp,softroce fc_nic_nets=,tcp0,o2ib0",
            "o2ib1(eth1),tcp0(eth2),o2ib0(eth3)",
        ),
        ("fc_nics=tcp,tcp fc_nic_nets=,", "tcp0(eth1),tcp1(eth2)"),
        ("fc_nics=tcp fc_nic_nets=", "tcp0(eth1)"),
        ("fc_nics=tcp fc_nic_nets=tcp65535", "tcp65535(eth1)"),
        (
            "fc_nics=softroce fc_nic_nets=o2ib65535",
            "o2ib65535(eth1)",
        ),
        (
            "fc_nics=passthrough;0000;85;00.1",
            "o2ib0(@ib-of-eth1))",
        ),
        (
            "fc_nics=tcp,passthrough;0000;85;00.1 fc_nic_nets=tcp1,",
            "tcp1(eth1),o2ib0(@ib-of-eth2))",
        ),
        (
            "fc_nics="
            + ",".join(["tcp"] * 8)
            + " fc_nic_nets="
            + ",".join(["tcp0"] * 8),
            "tcp0(eth1,eth2,eth3,eth4,eth5,eth6,eth7,eth8)",
        ),
    ],
)
def test_network_mapping(option: str, cmdline: str, networks: str) -> None:
    """Check complete output for legacy, grouping, mixed and limit cases."""
    result = _run(cmdline, option)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == f'options lnet networks="{networks}"\n'


@pytest.mark.parametrize(
    "cmdline",
    [
        "fc_nics=tcp,tcp fc_nic_nets=tcp0",
        "fc_nics=tcp fc_nic_nets=tcp0,tcp0",
        "fc_nic_nets=tcp0",
        "fc_nic_nets=",
        "fc_nics=tcp,tcp fc_nic_nets=",
        "fc_nics=tcp fc_nic_nets=o2ib0",
        "fc_nics=softroce fc_nic_nets=tcp0",
        "fc_nics=tcp fc_nic_nets=tcp",
        "fc_nics=tcp fc_nic_nets=tcp01",
        "fc_nics=tcp fc_nic_nets=tcp-1",
        "fc_nics=tcp fc_nic_nets=tcp65536",
        "fc_nics=tcp fc_nic_nets=tcp9999999999999999999999",
        "fc_nics=softroce fc_nic_nets=o2ib65536",
        "fc_nics=tcp fc_nic_nets=TCP0",
        "fc_nics=tcp fc_nic_nets=tcp0(eth9)",
        "fc_nics=passthrough fc_nic_nets=o2ib0",
        "fc_nics=bad",
        "fc_nics=tcp,",
        "fc_nics=,tcp",
        "fc_nics=tcp,,tcp",
        "fc_nics="
        + ",".join(["tcp"] * 9)
        + " fc_nic_nets="
        + ",".join(["tcp0"] * 9),
        "fc_nics=tcp fc_nics=softroce",
        "fc_nics=tcp fc_nic_nets=tcp0 fc_nic_nets=tcp1",
        "fc_nics=tcp fc_nic_nets=" + "t" * 129,
        "x" * 65537,
        "fc_nics=tcp\0fc_nic_nets=tcp0",
    ],
)
def test_bad_input_preserves_existing_file(
    tmp_path: Path, cmdline: str
) -> None:
    """Reject malformed input without truncating an operator's config."""
    output = tmp_path / "lnet.conf"
    original = 'options lnet networks="tcp4(eth1)" lnet_retry_count=7\n'
    output.write_text(original)
    result = _run(cmdline, output=output)
    assert result.returncode != 0
    assert "setup-lnet-config:" in result.stderr
    assert result.stdout == ""
    assert output.read_text() == original


def test_writes_complete_config(tmp_path: Path) -> None:
    """Write a valid grouped mapping through the actual file interface."""
    output = tmp_path / "lnet.conf"
    result = _run("fc_nics=tcp,tcp fc_nic_nets=tcp0,tcp0", output=output)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert output.read_text() == 'options lnet networks="tcp0(eth1,eth2)"\n'


def test_command_line_limit() -> None:
    """Accept the documented input limit and reject the next character."""
    result = _run("x" * 65536)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'options lnet networks="tcp0(eth0)"\n'


@pytest.mark.parametrize("option", ["-h", "--help"])
def test_help(option: str) -> None:
    """Document both stdin option spellings through both help forms."""
    result = _run("", option)
    assert result.returncode == 0
    assert "-s|--stdin" in result.stdout


@pytest.mark.parametrize("network", ["o2ib0", "tcp65536", "tcp0,tcp1"])
def test_sourced_failure_in_if(network: str) -> None:
    """Keep error returns when Bash disables errexit inside an if call."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; if emit_lnet_conf_nets "$2" none tcp; '
            "then exit 0; else exit 42; fi",
            "emitter-check",
            str(SCRIPT),
            network,
        ],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 42
    assert result.stdout == ""
    assert "setup-lnet-config:" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [["--unknown"], ["-s", "--stdin", "-s", "--stdin"], ["one", "two"]],
)
def test_bad_cli_arguments(arguments: list[str]) -> None:
    """Reject unknown options and excess paths before any file write."""
    result = subprocess.run(
        ["bash", str(SCRIPT), *arguments],
        input="fc_nics=tcp",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "setup-lnet-config:" in result.stderr


@pytest.mark.parametrize("explicit", [False, True])
def test_nine_nics_in_if_preserve_file_on_failure(
    tmp_path: Path, explicit: bool
) -> None:
    """Keep legacy lists valid and new layout limits checked inside if."""
    output = tmp_path / "lnet.conf"
    original = 'options lnet networks="tcp4(eth1)" lnet_retry_count=7\n'
    output.write_text(original)
    cmdline = "fc_nics=" + ",".join(["tcp"] * 9)
    if explicit:
        # Nine explicit extra NICs exceed the new layout's stated limit.
        cmdline += " fc_nic_nets=" + ",".join(["tcp0"] * 9)
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; if main --stdin "$2"; then exit 0; else exit 42; fi',
            "emitter-check",
            str(SCRIPT),
            str(output),
        ],
        input=cmdline,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.stdout == ""
    if explicit:
        # A rejected layout must not truncate a previously valid file.
        assert result.returncode == 42
        assert "at most eight explicit extra NICs" in result.stderr
        assert output.read_text() == original
    else:
        # Legacy lists keep separate automatic networks beyond eight NICs.
        expected = ",".join(f"tcp{idx}(eth{idx + 1})" for idx in range(9))
        assert result.returncode == 0, result.stderr
        assert output.read_text() == f'options lnet networks="{expected}"\n'
