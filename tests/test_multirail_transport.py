"""Check transport selection at the live suite's real CLI and state boundary."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from ltvm_pkg.rail_topology import resolve_topology
from tests.test_e2e.multirail_live import ROOT, Guest, Lab, make_lab
from tests.test_parser_coverage import ltvm


def _lab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, transport: str
) -> Lab:
    """Select a transport explicitly and keep saved VM state in test files."""
    monkeypatch.setenv("LTVM_MULTIRAIL_TRANSPORT", transport)
    lab = make_lab(mode, "ipv4", tmp_path)
    lab.env["LTVM_VM_DIR"] = str(tmp_path / "vms")
    lab.sockets.mkdir(parents=True)
    return lab


def _write_state(lab: Lab, name: str, nics: list[str]) -> None:
    """Model only the saved result of a parsed create request, not a VM launch."""
    topology = resolve_topology(nics, lab.mode)
    assert topology is not None
    ips = tuple(
        rail.subnet4.rsplit(".", 1)[0] + f".{10 + index}"
        for index, rail in enumerate(topology.rails)
    )
    values = {
        "NAME": name,
        "NICS": "|".join(nics),
        "NIC_IPS": "|".join(ips),
        "NIC_IP6S": "|".join(
            rail.ipv6_for(ip) for rail, ip in zip(topology.rails, ips)
        ),
        "RAIL_TOPOLOGY": json.dumps(topology.to_dict()),
    }
    (lab.sockets / f"{name}.info").write_text(
        "".join(f"{key}={value}\n" for key, value in values.items())
    )


def _create_runner(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Parse the actual outgoing request before returning a saved test record."""

    def run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        """No command executes; malformed real-parser arguments fail the unit test."""
        parsed = ltvm.build_parser().parse_args(command[2:])
        assert parsed.func.__name__ == "cmd_create"
        assert parsed.network_topology == lab.mode
        _write_state(lab, parsed.name, parsed.nic)
        return subprocess.CompletedProcess(command, 0, "created", "")

    runner = Mock(side_effect=run)
    monkeypatch.setattr(subprocess, "run", runner)
    return runner


def _expected_create(
    lab: Lab, guest: Guest, image: Path, server: bool
) -> list[str]:
    """Specify the complete consumer request, including roles and two data NICs."""
    return [
        sys.executable,
        str(ROOT / "ltvm"),
        "create",
        guest.name,
        "rocky9",
        "--kernel",
        "test-kernel",
        "--image",
        str(image),
        "--mem",
        "4096",
        "--vcpus",
        "2",
        "--mdt-disks",
        "1" if server else "0",
        "--ost-disks",
        "1" if server else "0",
        "--disk-size",
        "2G",
        "--nic",
        lab.transport,
        "--nic",
        lab.transport,
        "-T",
        lab.mode,
    ]


@pytest.mark.parametrize("mode", ("shared", "isolated"))
@pytest.mark.parametrize("transport", ("tcp", "softroce"))
@pytest.mark.parametrize("server", (False, True))
def test_create_uses_selected_transport_at_consumer_boundary(
    mode: str,
    transport: str,
    server: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both roles must request and read back exactly the selected two NICs."""
    lab = _lab(tmp_path, monkeypatch, mode, transport)
    runner = _create_runner(lab, monkeypatch)
    image = tmp_path / "image.ext4"
    guest = lab.create("rocky9", "test-kernel", image, server=server)
    assert runner.call_args.args == (
        _expected_create(lab, guest, image, server),
    )
    assert runner.call_args.kwargs == {
        "cwd": ROOT,
        "env": lab.env,
        "capture_output": True,
        "text": True,
        "timeout": 300,
    }
    prefix = "tcp" if transport == "tcp" else "o2ib"
    expected = [f"{prefix}0", f"{prefix}{0 if mode == 'shared' else 1}"]
    assert [nid.rsplit("@", 1)[1] for nid in guest.nids] == expected
    assert [
        rail["lnet_network"] for rail in guest.topology["rails"]
    ] == expected


@pytest.mark.parametrize("transport", ("tcp", "softroce"))
def test_saved_transport_mismatch_is_a_failure(
    transport: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A VM with the other transport cannot masquerade as successful coverage."""
    lab = _lab(tmp_path, monkeypatch, "shared", transport)
    name = "e2e-mr-mismatch"
    lab.names.append(name)
    other = "softroce" if transport == "tcp" else "tcp"
    _write_state(lab, name, [other, other])
    with pytest.raises(AssertionError):
        lab.read_guest(name)


@pytest.mark.parametrize("value", ("", "TCP", "rdma", "tcp,softroce"))
@pytest.mark.parametrize("required", ("0", "1"))
def test_invalid_transport_selection_never_skips(
    value: str, required: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid selection is a test configuration error in every run mode."""
    monkeypatch.setenv("LTVM_MULTIRAIL_TRANSPORT", value)
    monkeypatch.setenv("LTVM_REQUIRE_MULTIRAIL_E2E", required)
    with pytest.raises(pytest.fail.Exception, match="must be tcp or softroce"):
        make_lab("shared", "ipv4", tmp_path)


def test_transport_defaults_to_tcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing invocations retain TCP when no transport is selected."""
    monkeypatch.delenv("LTVM_MULTIRAIL_TRANSPORT", raising=False)
    assert make_lab("shared", "ipv4", tmp_path).transport == "tcp"


@pytest.mark.parametrize(
    "transport,module,parameter",
    (("tcp", "ksocklnd", "sock_timeout"), ("softroce", "ko2iblnd", "timeout")),
)
def test_transport_timeout_uses_exact_kernel_parameter(
    transport: str,
    module: str,
    parameter: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use writable module parameters and checked reads before any connections."""
    lab = _lab(tmp_path, monkeypatch, "shared", transport)
    lab.names.append("e2e-mr-policy")
    _write_state(lab, lab.names[0], [transport, transport])
    guest = lab.read_guest(lab.names[0])
    lab.guests.append(guest)
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(lab, "run", runner)
    lab.configure_transport(guest)
    path = f"/sys/module/{module}/parameters/{parameter}"
    runner.assert_called_once_with(
        guest,
        f"modprobe lnet && modprobe {module} && printf '3\\n' > {path} && "
        f'IFS= read -r transport_timeout < {path} && test "$transport_timeout" = 3',
    )


@pytest.mark.parametrize("fault", ("", "global", "lnd"))
@pytest.mark.parametrize("transport", ("tcp", "softroce"))
def test_configuration_reads_back_effective_timeout_policy(
    fault: str, transport: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful set commands do not prove active LNet and transport values."""
    lab = _lab(tmp_path, monkeypatch, "shared", transport)
    lab.names.append("e2e-mr-policy")
    _write_state(lab, lab.names[0], [transport, transport])
    guest = lab.read_guest(lab.names[0])
    lab.guests.append(guest)
    globals = {
        "global": {
            "discovery": 1,
            "health_sensitivity": 100,
            "retry_count": 2,
            "transaction_timeout": 50 if fault == "global" else 10,
        }
    }
    networks = {
        "net": [
            {
                "local NI(s)": [
                    {
                        "nid": nid.replace("@tcp0", "@tcp").replace(
                            "@o2ib0", "@o2ib"
                        ),
                        "lnd tunables": {
                            "timeout": 50 if fault == "lnd" else 3
                        },
                    }
                    for nid in guest.nids
                ]
            }
        ]
    }

    def run(
        guest: Guest, command: str, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        """Return only the two checked reports; all mutations remain simulated."""
        values = {
            "lnetctl global show": globals,
            "lnetctl net show --verbose 3": networks,
        }
        return subprocess.CompletedProcess(
            [], 0, yaml.safe_dump(values.get(command, {})), ""
        )

    runner = Mock(side_effect=run)
    monkeypatch.setattr(lab, "run", runner)
    if fault:
        # Either stale effective timeout must fail instead of yielding a false pass.
        with pytest.raises(AssertionError):
            lab.configure(guest)
    else:
        lab.configure(guest)
        commands = [call.args[1] for call in runner.call_args_list]
        assert commands.index(
            next(command for command in commands if "printf '3\\n'" in command)
        ) < commands.index("lnetctl lnet configure --all")
