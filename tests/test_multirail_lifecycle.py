"""Isolated checks for live restart verification and failure evidence."""

from __future__ import annotations

import inspect
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock, call

import pytest
import yaml

from ltvm_pkg.rail_topology import resolve_topology
from ltvm_pkg.vm_net import tap_for_name
from tests.test_e2e import multirail_diagnostics as diagnostics
from tests.test_e2e import multirail_lifecycle as lifecycle
from tests.test_e2e import multirail_payload as payload
from tests.test_e2e.multirail_live import Guest, Lab, make_lab

BOOT_BEFORE = "11111111-1111-4111-8111-111111111111"
BOOT_AFTER = "22222222-2222-4222-8222-222222222222"


def _record(lab: Lab, name: str, address: int) -> Guest:
    """Write the real saved schema needed by the live state reader."""
    topology = resolve_topology(["tcp", "tcp"], "shared")
    assert topology is not None
    ips = (f"172.18.100.{address}", f"172.18.100.{address + 1}")
    ip6s = tuple(rail.ipv6_for(ip) for rail, ip in zip(topology.rails, ips))
    values = {
        "NAME": name,
        "IP": f"192.168.100.{address}",
        "MAC": f"52:54:00:00:00:{address:02x}",
        "TAP": tap_for_name(name),
        "NICS": "tcp|tcp",
        "NIC_IPS": "|".join(ips),
        "NIC_IP6S": "|".join(ip6s),
        "RAIL_TOPOLOGY": json.dumps(topology.to_dict()),
    }
    (lab.sockets / f"{name}.info").write_text(
        "".join(f"{key}={value}\n" for key, value in values.items())
    )
    return lab.read_guest(name)


def _taps(guest: Guest, index: int) -> None:
    """Create sysfs-shaped test files without creating any host interface."""
    bridges = ["fcbr0", *(rail["bridge"] for rail in guest.topology["rails"])]
    for offset, (name, bridge) in enumerate(
        zip((tap_for_name(guest.name), *guest.taps), bridges)
    ):
        directory = lifecycle.SYS_NET / name
        directory.mkdir()
        master = lifecycle.SYS_NET / bridge
        master.mkdir(exist_ok=True)
        (directory / "master").symlink_to(master)
        (directory / "ifindex").write_text(str(index + offset))


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    """Keep all state in temporary files and mock every command boundary."""
    monkeypatch.setenv("LTVM_MULTIRAIL_TRANSPORT", "tcp")
    network = tmp_path / "net"
    network.mkdir()
    monkeypatch.setattr(lifecycle, "SYS_NET", network)
    monkeypatch.setattr(diagnostics, "SYS_NET", network)
    lab = make_lab("shared", "ipv4", tmp_path)
    lab.env["LTVM_VM_DIR"] = str(tmp_path / "vms")
    lab.sockets.mkdir(parents=True)
    for number, address in enumerate((10, 20)):
        name = f"co1-mr-{number}"
        lab.names.append(name)
        lab.guests.append(_record(lab, name, address))
        _taps(lab.guests[-1], 10 + number * 10)
    monkeypatch.setattr(lab, "configure", Mock())
    monkeypatch.setattr(
        lab, "cli", Mock(side_effect=AssertionError("unmocked CLI"))
    )
    monkeypatch.setattr(
        lab, "run", Mock(side_effect=AssertionError("unmocked guest"))
    )
    return lab


def _guest_commands(lab: Lab, fault: str) -> Mock:
    """Return a bounded guest command model with no external process calls."""
    boot_calls = 0

    def run(
        guest: Guest, command: str, *, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        """Return distinct boot IDs and real parser input for remote MR peers."""
        nonlocal boot_calls
        output = ""
        if command == lifecycle.BOOT_ID_COMMAND:
            # Returning the old ID models a start command that did no work.
            output = (
                BOOT_BEFORE
                if boot_calls == 0 or fault == "boot"
                else BOOT_AFTER
            )
            boot_calls += 1
        elif command == "lnetctl peer show --verbose 3":
            remote = next(other for other in lab.guests if other != guest)
            output = yaml.safe_dump(
                {
                    "peer": [
                        {
                            "Multi-Rail": True,
                            "peer ni": [{"nid": nid} for nid in remote.nids],
                        }
                    ]
                }
            )
        return subprocess.CompletedProcess([], 0, output, "")

    return Mock(side_effect=run)


def _restart_commands(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, fault: str = ""
) -> tuple[Mock, Mock]:
    """Model only stop/start effects and return checked command captures."""

    def cli(verb: str, name: str, *, timeout: int) -> None:
        """Change temporary TAP files; optional faults model real regressions."""
        guest = next(guest for guest in lab.guests if guest.name == name)
        if verb == "stop":
            # A lingering TAP must fail before start can overwrite the evidence.
            if fault != "lingering":
                for tap in (tap_for_name(name), *guest.taps):
                    shutil.rmtree(lifecycle.SYS_NET / tap)
        elif verb == "start":
            # A successful exit still requires fresh interfaces and saved state.
            _taps(guest, 20 if fault == "indices" else 30)
            if fault == "identity":
                # A management address change is as invalid as a data change.
                path = lab.sockets / f"{name}.info"
                path.write_text(
                    path.read_text().replace("192.168.100.20", "192.168.100.99")
                )
            if fault == "bridge":
                # The expected NIC name alone cannot prove fabric attachment.
                master = lifecycle.SYS_NET / guest.taps[0] / "master"
                master.unlink()
                master.symlink_to(lifecycle.SYS_NET / "fcbr0")
        else:
            raise AssertionError(f"unexpected lifecycle command {verb}")

    control, guest_run = Mock(side_effect=cli), _guest_commands(lab, fault)
    monkeypatch.setattr(lab, "cli", control)
    monkeypatch.setattr(lab, "run", guest_run)
    return control, guest_run


def test_restart_preserves_identity_and_reconfigures_peer(
    sandbox: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Require real saved fields, fresh attachments, boot change, and discovery."""
    control, guest_run = _restart_commands(sandbox, monkeypatch)
    client = sandbox.guests[1]
    lifecycle.restart_saved_profile(sandbox, client)
    assert control.call_args_list == [
        (("stop", client.name), {"timeout": 120}),
        (("start", client.name), {"timeout": 300}),
    ]
    sandbox.configure.assert_called_once_with(client)
    discoveries = [
        call
        for call in guest_run.call_args_list
        if call.args[1].startswith("lnetctl discover ")
    ]
    assert len(discoveries) == 4
    assert [call.args[0] for call in discoveries] == [
        client,
        sandbox.guests[0],
        client,
        sandbox.guests[0],
    ]
    record = json.loads(
        (sandbox.evidence / "restart-identity.json").read_text()
    )
    assert record["saved_identity"]["IP"] == "192.168.100.20"
    assert record["boot_id_before"] == BOOT_BEFORE
    assert record["boot_id_after"] == BOOT_AFTER


@pytest.mark.parametrize(
    "fault", ("identity", "bridge", "indices", "boot", "lingering")
)
def test_restart_rejects_identity_or_lifecycle_regressions(
    fault: str, sandbox: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful CLI exit cannot substitute for real stop/start behavior."""
    _restart_commands(sandbox, monkeypatch, fault)
    with pytest.raises(AssertionError):
        lifecycle.restart_saved_profile(sandbox, sandbox.guests[1])
    assert not (sandbox.evidence / "restart-identity.json").exists()


def test_failure_capture_keeps_logs_and_original_reason(
    sandbox: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounded snapshots preserve the failure before cleanup can remove it."""
    server = sandbox.guests[0]
    serial = b"x" * diagnostics.MAX_CAPTURE_BYTES + b"kernel failure"
    (sandbox.sockets / f"{server.name}.log").write_bytes(serial)
    runner = Mock(
        return_value=subprocess.CompletedProcess([], 0, "guest state", "")
    )
    monkeypatch.setattr(sandbox, "run", runner)
    diagnostics.capture_failure(sandbox, "test::failed", "original failure")
    captured = list(sandbox.evidence.glob("failure-*"))
    assert len(captured) == 1
    directory = captured[0]
    assert (directory / "reason.txt").read_text() == "original failure"
    assert (directory / f"{server.name}.log").read_bytes() == serial[
        -diagnostics.MAX_CAPTURE_BYTES :
    ]
    assert (directory / f"{server.name}.info").read_bytes() == (
        sandbox.sockets / f"{server.name}.info"
    ).read_bytes()
    assert runner.call_count == 2
    assert runner.call_args.kwargs == {"timeout": 10, "check": False}
    assert (
        "guest state"
        in (directory / f"{server.name}-guest-state.txt").read_text()
    )


def test_failure_capture_continues_after_unreachable_guest(
    sandbox: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One stopped guest must not hide the other guest's diagnostics."""
    runner = Mock(
        side_effect=[
            subprocess.TimeoutExpired("probe", 10),
            subprocess.CompletedProcess([], 0, "second state", ""),
        ]
    )
    monkeypatch.setattr(sandbox, "run", runner)
    diagnostics.capture_failure(sandbox, "restart", "start failed")
    directory = next(sandbox.evidence.glob("failure-*"))
    assert runner.call_count == 2
    assert (
        "second state"
        in (directory / f"{sandbox.guests[1].name}-guest-state.txt").read_text()
    )
    assert "timed out" in (directory / "capture-errors.txt").read_text()


def test_host_diagnostics_capture_mtu_tap_flags_and_bridge_port_state(
    sandbox: Lab,
) -> None:
    """Read fabric settings directly and expose unsupported sysfs data explicitly."""
    guest = sandbox.guests[0]
    tap = diagnostics.SYS_NET / guest.taps[0]
    values = {"mtu": "4200", "tun_flags": "0x5102", "brport/state": "3"}
    for name, value in values.items():
        target = tap / name
        target.parent.mkdir(exist_ok=True)
        target.write_text(value)
    (tap / "master" / "mtu").write_text("4200")
    report = json.loads(diagnostics._host_links(guest))
    entry = report[guest.taps[0]]
    assert all(entry[name] == value for name, value in values.items())
    assert entry["master_mtu"] == "4200"
    assert entry["brport/learning"].startswith("read failed:")
    assert "missing" not in entry


@pytest.mark.parametrize("both", (False, True))
def test_fault_capture_runs_before_failed_links_are_restored(
    both: bool, sandbox: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The saved failure must describe the stalled paths before cleanup repairs them."""
    events = []
    monkeypatch.setattr(payload, "start_worker", Mock())
    monkeypatch.setattr(payload, "_wait_carrier", Mock())
    monkeypatch.setattr(sandbox, "management", Mock())
    monkeypatch.setattr(sandbox, "snapshot", Mock())
    monkeypatch.setattr(
        sandbox, "link", lambda guest, index, *, up: events.append((index, up))
    )
    capture = Mock(side_effect=lambda *args: events.append("capture"))
    monkeypatch.setattr(payload, "capture_failure", capture)
    error = RuntimeError("fault probe failed")
    if both:
        # Both-rail failure reaches its control probe after the initial copy.
        monkeypatch.setattr(payload, "wait_progress", Mock(return_value=1))
        monkeypatch.setattr(sandbox, "run", Mock(side_effect=error))
        operation = payload.both_rails_fault
        args = (sandbox,)
        expected = [(1, False), (2, False), "capture", (1, True), (2, True)]
    else:
        # A single-rail stall fails while waiting for the next checked copies.
        monkeypatch.setattr(
            payload, "wait_progress", Mock(side_effect=[1, error])
        )
        operation = payload.single_rail_fault
        args = (sandbox, 2)
        expected = [(2, False), "capture", (2, True)]
    with pytest.raises(RuntimeError, match="fault probe failed"):
        operation(*args)
    assert events == expected
    assert "RuntimeError: fault probe failed" in capture.call_args.args[2]


@pytest.mark.parametrize("both", (False, True))
def test_faulted_io_has_a_separate_filesystem_recovery_bound(
    both: bool, sandbox: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound faulted RPC recovery separately from healthy initial progress."""
    default_timeout = (
        inspect.signature(payload.wait_progress).parameters["timeout"].default
    )
    progress = Mock(side_effect=[1, 3])
    for name in (
        "start_worker",
        "stop_worker",
        "_wait_carrier",
        "wait_healthy",
        "checked_copy",
        "bulk_progress",
    ):
        monkeypatch.setattr(payload, name, Mock())
    monkeypatch.setattr(payload, "wait_progress", progress)
    monkeypatch.setattr(sandbox, "snapshot", Mock())
    monkeypatch.setattr(sandbox, "link", Mock())
    monkeypatch.setattr(sandbox, "management", Mock())
    runner = Mock(
        return_value=subprocess.CompletedProcess([], 1, "ping failed", "")
    )
    monkeypatch.setattr(sandbox, "run", runner)
    if both:
        # Total loss can enter upper-layer RPC recovery after the links return.
        payload.both_rails_fault(sandbox)
        assert progress.call_args_list == [
            call(sandbox, 1),
            call(sandbox, 3, timeout=180),
        ]
        assert runner.call_args.kwargs == {"timeout": 25, "check": False}
        assert runner.call_args.args[1].startswith(
            "timeout 20s lnetctl ping --timeout 8 "
        )
    else:
        # A lost bulk RPC can enter filesystem recovery with one healthy rail.
        payload.single_rail_fault(sandbox, 2)
        assert progress.call_args_list == [
            call(sandbox, 1),
            call(sandbox, 3, timeout=180),
        ]
    assert default_timeout == 45


def test_worker_timeout_covers_the_bounded_total_outage_recovery(
    sandbox: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guest worker must outlive the total outage and still have a hard limit."""
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(sandbox, "run", runner)
    payload.start_worker(sandbox)
    assert runner.call_args.args[0] == sandbox.guests[1]
    command = runner.call_args.args[1]
    assert "timeout 600s bash -c " in command
    assert "timeout 120s" not in command
    assert "i<=4096" in command and "mr-worker-exit" in command
