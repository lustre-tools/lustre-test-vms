"""Wait for real NIC state under one bounded, monotonic startup budget."""

from __future__ import annotations

import json
import subprocess

import pytest

from ltvm_pkg import nic_guest
from ltvm_pkg.nic_spec import NicSpec
from ltvm_pkg.vm_state import VMInfo

_CONFIG = 'options lnet networks="tcp0(eth1,eth2)"\n'
_STALE = 'options lnet networks="tcp0(eth9)"\n'


class _Clock:
    """Control both time reads and sleeps without waiting in a test."""

    def __init__(self) -> None:
        """Start at a nonzero origin to catch absolute-deadline mistakes."""
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        """Return the same clock that fake SSH operations advance."""
        return self.now

    def sleep(self, duration: float) -> None:
        """Record each retry delay and optionally advance simulated time."""
        self.sleeps.append(duration)
        self.now += duration


class _Guest:
    """Return bounded SSH responses while preserving real layout checks."""

    def __init__(
        self, clock: _Clock, replies: list[tuple[str, int, float]]
    ) -> None:
        """Each response defines its body, exit status, and elapsed time."""
        self.clock = clock
        self.replies = replies
        self.requests: list[tuple[str, float, int]] = []

    def run(
        self, ip: str, command: str, *, timeout: int, retry_connect: bool
    ) -> subprocess.CompletedProcess[str]:
        """Capture actual read timeouts before consuming the next response."""
        assert ip == "203.0.113.50"
        assert retry_connect is False
        index = len(self.requests)
        assert index < len(self.replies), "unexpected extra SSH request"
        self.requests.append((command, self.clock.now, timeout))
        body, status, elapsed = self.replies[index]
        self.clock.now += elapsed
        return subprocess.CompletedProcess(
            [], status, body, "configuration not ready"
        )


def _vm(*specs: str) -> VMInfo:
    """Keep saved state generic and independent of VM directories."""
    return VMInfo(
        name="client",
        ip="203.0.113.50",
        tap="tap-client",
        mac="aa:fc:00:01:02:03",
        nics=[NicSpec.parse(spec) for spec in specs],
        nic_ips=["192.0.2.10", "192.0.2.11"],
    )


def _addresses(index: int, prefix: int = 27) -> str:
    """Match iproute2 output for the corresponding saved data address."""
    return json.dumps(
        [
            {
                "addr_info": [
                    {"local": f"192.0.2.{index + 9}", "prefixlen": prefix}
                ]
            }
        ]
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    replies: list[tuple[str, int, float]],
) -> _Guest:
    """Replace only time and transport, leaving layout validation active."""
    guest = _Guest(clock, replies)
    monkeypatch.setattr(nic_guest.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(nic_guest.time, "sleep", clock.sleep)
    monkeypatch.setattr(nic_guest, "run_ssh", guest.run)
    return guest


@pytest.mark.parametrize("first", [(_STALE, 0, 0.0), ("", 1, 0.0)])
def test_waits_for_stale_or_missing_config_and_late_addresses(
    monkeypatch: pytest.MonkeyPatch, first: tuple[str, int, float]
) -> None:
    """SSH readiness cannot accept an old config or incomplete addresses."""
    clock = _Clock()
    guest = _install(
        monkeypatch,
        clock,
        [
            first,
            (_CONFIG, 0, 0.0),
            (_addresses(1, 24), 0, 0.0),
            (_CONFIG, 0, 0.0),
            (_addresses(1), 0, 0.0),
        ],
    )
    vm = _vm("tcp:net=tcp0,subnet=192.0.2.0/27", "tcp:net=tcp0")

    nic_guest.wait_for_layout(vm)

    assert clock.sleeps == [0.5, 0.5]
    assert [request[2] for request in guest.requests] == [30, 29, 29, 29, 29]
    commands = [request[0] for request in guest.requests]
    assert commands.count(nic_guest._CONFIG_READ) == 3


@pytest.mark.parametrize("timeout", [1, 30])
def test_ready_first_layout_has_no_retry_sleep(
    monkeypatch: pytest.MonkeyPatch, timeout: int
) -> None:
    """A ready guest completes after the required config and address reads."""
    clock = _Clock()
    guest = _install(
        monkeypatch, clock, [(_CONFIG, 0, 0.0), (_addresses(1), 0, 0.0)]
    )

    nic_guest.wait_for_layout(
        _vm("tcp:net=tcp0,subnet=192.0.2.0/27", "tcp:net=tcp0"),
        timeout=timeout,
    )

    assert clock.sleeps == []
    assert len(guest.requests) == 2
    assert [request[2] for request in guest.requests] == [timeout, timeout]


def test_wrong_layout_stops_at_budget_with_useful_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permanent wrong state reports the actual mapping failure on timeout."""
    clock = _Clock()
    guest = _install(monkeypatch, clock, [(_STALE, 0, 0.0)] * 61)

    with pytest.raises(ValueError, match="LNet network tcp0 does not use eth1"):
        nic_guest.wait_for_layout(_vm("tcp:net=tcp0"), timeout=3)

    assert len(guest.requests) == 5
    assert clock.sleeps == [0.5] * 4
    assert [request[2] for request in guest.requests] == [3, 2, 2, 1, 1]
    assert all(start < 103.0 for _, start, _ in guest.requests)


@pytest.mark.parametrize(
    "body,error,match",
    [
        (_STALE, ValueError, "LNet network tcp0"),
        (_CONFIG, TimeoutError, "timed out waiting for NIC setup"),
    ],
)
def test_read_that_uses_budget_cannot_start_another_request(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    error: type[Exception],
    match: str,
) -> None:
    """An SSH call that reaches the deadline prevents every later read."""
    clock = _Clock()
    guest = _install(monkeypatch, clock, [(body, 0, 3.0)])

    with pytest.raises(error, match=match):
        nic_guest.wait_for_layout(
            _vm("tcp:net=tcp0,subnet=192.0.2.0/27", "tcp:net=tcp0"),
            timeout=3,
        )

    assert len(guest.requests) == 1
    assert guest.requests[0][1:] == (100.0, 3)
    assert clock.sleeps == []
    assert clock.now == 103.0


def test_multiple_nic_reads_share_the_original_ssh_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network and NIC reads cannot each start a new full timeout."""
    clock = _Clock()
    guest = _install(
        monkeypatch, clock, [(_CONFIG, 0, 1.25), (_addresses(1), 0, 1.25)]
    )
    vm = _vm(
        "tcp:net=tcp0,subnet=192.0.2.0/27",
        "tcp:net=tcp0,subnet=192.0.2.0/27",
    )

    with pytest.raises(TimeoutError, match="timed out waiting for NIC setup"):
        nic_guest.wait_for_layout(vm, timeout=3)

    assert [request[2] for request in guest.requests] == [3, 1]
    assert clock.sleeps == []
    assert all("eth2" not in command for command, _, _ in guest.requests[1:])
    assert all(
        1 <= timeout <= int(103.0 - start)
        for _, start, timeout in guest.requests
    )


def test_budget_exhaustion_keeps_the_earlier_layout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final read deadline must not hide the last useful guest diagnosis."""
    clock = _Clock()
    guest = _install(
        monkeypatch,
        clock,
        [(_STALE, 0, 0.25), (_CONFIG, 0, 1.0), (_addresses(1), 0, 1.0)],
    )
    vm = _vm(
        "tcp:net=tcp0,subnet=192.0.2.0/27",
        "tcp:net=tcp0,subnet=192.0.2.0/27",
    )

    with pytest.raises(
        ValueError, match="LNet network tcp0 does not use eth1,eth2"
    ):
        nic_guest.wait_for_layout(vm, timeout=3)

    assert [request[2] for request in guest.requests] == [3, 2, 1]
    assert clock.sleeps == [0.5]
    assert clock.now == 102.75
