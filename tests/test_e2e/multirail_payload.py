"""Checked cross-VM payloads and bounded faults for the live rail suite."""

from __future__ import annotations

import json
import re
import shlex
import time
import traceback
import uuid
from dataclasses import asdict

import pytest

from tests.multirail_metrics import (
    batch_stopped,
    bulk_progress,
    selftest_group_has_node,
    selftest_ipv6_group_rejected,
)
from tests.test_e2e.multirail_diagnostics import capture_failure
from tests.test_e2e.multirail_live import Guest, Lab, unavailable, wait_healthy


def _mount(lab: Lab, guest: Guest, command: str) -> None:
    """Expose the known upstream IPv6 UUID limit without hiding other errors."""
    result = lab.run(guest, command, timeout=120, check=False)
    if result.returncode == 0:
        return
    kernel = lab.run(guest, "dmesg | tail -200", check=False).stdout
    if (
        lab.family == "ipv6"
        and "target UUID must be 40 characters or less" in kernel
    ):
        # LU-18041 is a Lustre mount limit; LNet IPv6 tests remain independent.
        pytest.xfail(
            "Lustre IPv6 mount unsupported by this build: LU-18041 UUID length limit"
        )
    pytest.fail(
        f"Lustre mount failed; evidence {lab.evidence}: {result.stdout} {result.stderr}\n{kernel}"
    )


def mount_filesystem(lab: Lab) -> None:
    """Format only the newly created server disks and mount a separate client."""
    server, client = lab.guests
    nid = shlex.quote(server.nids[0])
    lab.run(
        server,
        "mkdir -p /mnt/mr-mdt /mnt/mr-ost && test -b /dev/vdb && test -b /dev/vdc",
    )
    lab.run(
        server,
        "mkfs.lustre --fsname=mrtest --backfstype=ldiskfs --mgs --mdt --index=0 /dev/vdb",
        timeout=120,
    )
    _mount(lab, server, "mount -t lustre /dev/vdb /mnt/mr-mdt")
    lab.run(
        server,
        "mkfs.lustre --fsname=mrtest --backfstype=ldiskfs --ost --index=0 "
        f"--mgsnode={nid} /dev/vdc",
        timeout=120,
    )
    _mount(lab, server, "mount -t lustre /dev/vdc /mnt/mr-ost")
    lab.run(client, "mkdir -p /mnt/mr-client")
    _mount(
        lab,
        client,
        f"mount -t lustre {shlex.quote(server.nids[0] + ':/mrtest')} /mnt/mr-client",
    )
    lab.run(
        client,
        'test "$(stat -f -c %T /mnt/mr-client)" = lustre && '
        "dd if=/dev/urandom of=/root/mr-input bs=1M count=128 status=none",
        timeout=60,
    )


def checked_copy(lab: Lab, phase: str) -> None:
    """Measure writes and reads separately and compare the complete payload."""
    client = lab.guests[1]
    commands = {
        "write": "dd if=/root/mr-input of=/mnt/mr-client/payload "
        "bs=1M oflag=direct conv=fsync status=none",
        "read": "dd if=/mnt/mr-client/payload of=/root/mr-read "
        "bs=1M iflag=direct status=none && cmp /root/mr-input /root/mr-read",
    }
    for direction, command in commands.items():
        started = time.monotonic()
        before = [lab.snapshot(guest) for guest in lab.guests]
        lab.run(client, f"timeout 60s sh -c {shlex.quote(command)}", timeout=70)
        after = [lab.snapshot(guest) for guest in lab.guests]
        measurements = [
            bulk_progress(start, end) for start, end in zip(before, after)
        ]
        (lab.evidence / f"{phase}-{direction}-shares.json").write_text(
            json.dumps(
                {
                    "metric": "fraction of data-interface rx_bytes + tx_bytes; "
                    "includes protocol overhead",
                    "payload_bytes": 128 * 1024 * 1024,
                    "clock": "controller time.monotonic; includes counter collection",
                    "started": started,
                    "finished": time.monotonic(),
                    "before": [asdict(sample) for sample in before],
                    "after": [asdict(sample) for sample in after],
                    "guests": {
                        guest.name: share
                        for guest, share in zip(lab.guests, measurements)
                    },
                },
                indent=2,
            )
        )


def start_worker(lab: Lab) -> None:
    """Start a finite checked I/O loop; management can stop it between copies."""
    client = lab.guests[1]
    work = """set -e
for ((i=1; i<=4096; i++)); do
    if test -e /root/mr-stop; then exit 0; fi
    touch /root/mr-active
    dd if=/root/mr-input of=/mnt/mr-client/payload bs=1M oflag=direct conv=fsync status=none
    dd if=/mnt/mr-client/payload of=/root/mr-read bs=1M iflag=direct status=none
    cmp /root/mr-input /root/mr-read
    rm /root/mr-active
    printf '%s\\n' "$i" > /root/mr-progress
done
"""
    wrapped = (
        f"timeout 600s bash -c {shlex.quote(work)}; rc=$?; "
        "printf '%s\\n' \"$rc\" > /root/mr-worker-exit"
    )
    lab.run(
        client,
        "rm -f /root/mr-stop /root/mr-progress /root/mr-worker-exit "
        "/root/mr-active /root/mr-worker-pid; "
        f"( {wrapped} ) </dev/null >/root/mr-worker.log 2>&1 & "
        "printf '%s\\n' \"$!\" > /root/mr-worker-pid",
    )


def wait_progress(lab: Lab, minimum: int, timeout: int = 45) -> int:
    """Wait for completed, validated copies, with a deadline and pass limit."""
    client = lab.guests[1]
    deadline = time.monotonic() + timeout
    for _ in range(timeout):
        result = lab.run(
            client,
            "if test -e /root/mr-worker-exit; then "
            "cat /root/mr-worker.log; exit 70; fi; "
            "if test -s /root/mr-progress; then cat /root/mr-progress; else echo 0; fi",
        )
        progress = int(result.stdout.strip())
        if progress >= minimum:
            return progress
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
    pytest.fail(
        f"checked I/O did not reach copy {minimum} in {timeout}s; evidence {lab.evidence}"
    )


def stop_worker(lab: Lab) -> None:
    """Let restored-link RPCs finish within 180 seconds; preserve failed I/O."""
    client = lab.guests[1]
    lab.run(client, ": > /root/mr-stop", timeout=10)
    deadline = time.monotonic() + 180
    for _ in range(180):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Graceful completion cannot extend the filesystem recovery limit.
            break
        result = lab.run(
            client,
            "if test -s /root/mr-worker-exit; then "
            "IFS= read -r status < /root/mr-worker-exit || exit 70; "
            "printf '%s\\n' \"$status\"; else printf 'running\\n'; fi",
            timeout=min(10, max(1, int(remaining))),
        )
        if result.stdout.strip() != "running":
            # A failed payload must fail at once, even when recovery time remains.
            assert result.stdout.strip() == "0", lab.run(
                client, "cat /root/mr-worker.log"
            ).stdout
            return
        time.sleep(1)
    pytest.fail(
        f"checked I/O worker failed to stop in 180s; evidence {lab.evidence}"
    )


def single_rail_fault(lab: Lab, index: int) -> None:
    """Fail one data link during real I/O and verify continued checked progress."""
    server = lab.guests[0]
    start_worker(lab)
    initial = wait_progress(lab, 1)
    before = [lab.snapshot(guest) for guest in lab.guests]
    lab.link(server, index, up=False)
    try:
        _wait_carrier(lab, server, index, up=False)
        lab.management()
        # A lost bulk RPC can wait for Lustre recovery even with one live NI.
        wait_progress(lab, initial + 2, timeout=180)
        after = [lab.snapshot(guest) for guest in lab.guests]
        survivor = (f"eth{3 - index}",)
        for start, end in zip(before, after):
            bulk_progress(start, end, survivor, balanced=False)
    except BaseException:
        # Capture stalled imports and worker stacks before restoring the link.
        capture_failure(
            lab, f"single-rail-{index}", traceback.format_exc(limit=32)
        )
        raise
    finally:
        # Restore the failed data path even if I/O or control assertions fail.
        lab.link(server, index, up=True)
    _wait_carrier(lab, server, index, up=True)
    stop_worker(lab)
    for guest in lab.guests:
        wait_healthy(lab, guest)
    checked_copy(lab, f"restored-rail-{index}")


def both_rails_fault(lab: Lab) -> None:
    """Require bounded LNet failure, live management, and resumed checked I/O."""
    server, client = lab.guests
    start_worker(lab)
    initial = wait_progress(lab, 1)
    lab.link(server, 1, up=False)
    lab.link(server, 2, up=False)
    try:
        for index in (1, 2):
            _wait_carrier(lab, server, index, up=False)
        lab.management()
        started = time.monotonic()
        result = lab.run(
            client,
            f"timeout 20s lnetctl ping --timeout 8 {shlex.quote(server.nids[0])}",
            timeout=25,
            check=False,
        )
        assert result.returncode not in (0, 124, 137), result
        assert "ping" in (result.stdout + result.stderr).lower(), result
        assert time.monotonic() - started < 25
        lab.management()
    except BaseException:
        # Both failed paths must remain visible in the diagnostic snapshot.
        capture_failure(lab, "both-rails", traceback.format_exc(limit=32))
        raise
    finally:
        # A filesystem can wait for an outage; restoring links releases its I/O.
        lab.link(server, 1, up=True)
        lab.link(server, 2, up=True)
    # Total loss can enter Lustre RPC recovery after LNet connectivity returns.
    wait_progress(lab, initial + 2, timeout=180)
    stop_worker(lab)
    for guest in lab.guests:
        wait_healthy(lab, guest)
    checked_copy(lab, "restored-both-rails")


def _wait_carrier(lab: Lab, guest: Guest, index: int, *, up: bool) -> None:
    """Require the QMP fault to reach the intended guest data NIC."""
    assert index in (1, 2)
    deadline = time.monotonic() + 8
    for _ in range(8):
        result = lab.run(guest, f"cat /sys/class/net/eth{index}/carrier")
        if result.stdout.strip() == str(int(up)):
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    pytest.fail(
        f"eth{index} did not change carrier to {up}; evidence {lab.evidence}"
    )


def _lst(lab: Lab, key: str, arguments: list[str]) -> str:
    """Use one session key and inspect errors instead of trusting query's rc."""
    result = lab.run(
        lab.guests[1],
        shlex.join(["env", f"LST_SESSION={key}", "lst", *arguments]),
        timeout=20,
        check=False,
    )
    text = result.stdout + result.stderr
    adding_group = len(arguments) == 3 and arguments[0] == "add_group"
    if (
        adding_group
        and lab.family == "ipv6"
        and selftest_ipv6_group_rejected(
            result.stdout, result.stderr, arguments[1], arguments[2]
        )
    ):
        # Successful empty-group deletion hides this parser failure with rc0.
        pytest.xfail(f"this lst build rejects IPv6 group NIDs: {text[-1000:]}")
    if result.returncode or result.stderr.strip():
        # LST can print an error and report successful cleanup as its status.
        pytest.fail(f"self-test setup failed; evidence {lab.evidence}: {text}")
    if adding_group and (
        f"{arguments[2]} are added to session" not in result.stdout.splitlines()
        or result.stderr.strip()
    ):
        # add_group can return success after failing to add every requested node.
        pytest.fail(
            f"self-test group was not accepted; evidence {lab.evidence}: {text}"
        )
    return result.stdout


def _add_selftest_group(lab: Lab, key: str, group: str, nid: str) -> None:
    """Check actual group membership before allowing a payload batch to start."""
    _lst(lab, key, ["add_group", group, nid])
    report = _lst(lab, key, ["list_group", "--all", group])
    assert selftest_group_has_node(report, group, nid), (
        f"self-test group lacks its expected active node; evidence {lab.evidence}: {report}"
    )


def lnet_selftest(lab: Lab) -> None:
    """Run finite checked BRW reads/writes and measure both rails at both ends."""
    for guest in lab.guests:
        probe = lab.run(
            guest,
            "command -v lst >/dev/null || exit 69; "
            "command -v modinfo >/dev/null || exit 69; "
            "modinfo lnet_selftest >/dev/null || exit 69",
            check=False,
        )
        if probe.returncode == 69:
            # Only a checked missing guest dependency may skip ordinary runs.
            unavailable(
                "LNet self-test tool or module absent from this guest image"
            )
        if probe.returncode != 0:
            # An invalid CLI request or lost control path is a test failure.
            pytest.fail(
                f"self-test prerequisite probe failed; evidence {lab.evidence}: "
                f"rc={probe.returncode} {probe.stdout} {probe.stderr}"
            )
        lab.run(guest, "modprobe lnet_selftest")
    key = str(uuid.uuid4().int % 2147483646 + 1)
    _lst(lab, key, ["new_session", f"mr-{key}"])
    try:
        # Validate the real data NID before local control on large-NID builds.
        _add_selftest_group(lab, key, "sink", lab.guests[0].nids[0])
        # The controller is the source node; local control uses LNet loopback.
        # Data still crosses to the remote sink and must use both physical NIs.
        _add_selftest_group(lab, key, "source", "0@lo")
        for direction in ("write", "read"):
            _run_batch(lab, key, direction)
    finally:
        _lst(lab, key, ["end_session"])


def _run_batch(lab: Lab, key: str, direction: str) -> None:
    """Do not accept an empty, failed, or unfinished self-test batch."""
    batch = f"bulk-{direction}"
    _lst(lab, key, ["add_batch", batch])
    _lst(
        lab,
        key,
        [
            "add_test",
            "--batch",
            batch,
            "--loop",
            "64",
            "--concurrency",
            "16",
            "--from",
            "source",
            "--to",
            "sink",
            "brw",
            direction,
            "check=full",
            "size=1M",
        ],
    )
    before = [lab.snapshot(guest) for guest in lab.guests]
    _lst(lab, key, ["run", batch])
    deadline = time.monotonic() + 90
    stopped = False
    for _ in range(90):
        stopped = batch_stopped(
            _lst(lab, key, ["query", "--timeout", "5", batch])
        )
        if stopped or time.monotonic() >= deadline:
            break
        time.sleep(1)
    assert stopped, (
        f"self-test batch exceeded its deadline; evidence {lab.evidence}"
    )
    errors = _lst(lab, key, ["show_error", "--session", "source", "sink"])
    assert re.findall(r"Total (\d+) error nodes in", errors) == ["0", "0"], (
        errors
    )
    after = [lab.snapshot(guest) for guest in lab.guests]
    for start, end in zip(before, after):
        bulk_progress(start, end)
