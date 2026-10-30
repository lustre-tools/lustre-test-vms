"""Capture bounded failure evidence before live guest cleanup changes state."""

from __future__ import annotations

import json
import subprocess
import time
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

from ltvm_pkg.vm_net import tap_for_name

if TYPE_CHECKING:
    from tests.test_e2e.multirail_live import Guest, Lab

SYS_NET = Path("/sys/class/net")
MAX_CAPTURE_BYTES = 1024 * 1024
GUEST_STATE = """set +e
printf '\\n== checked worker files ==\\n'
for f in mr-worker-pid mr-progress mr-worker-exit mr-worker.log mr-active mr-stop; do
    printf '\\n-- %s --\\n' "$f"
    if test -e "/root/$f"; then tail -c 65536 "/root/$f"; else printf 'absent\\n'; fi
done
printf '\\n== worker process stacks and wait channels ==\\n'
scanned=0; captured=0
for p in /proc/[0-9]*; do
    scanned=$((scanned + 1)); if ((scanned > 512)); then break; fi
    read -r comm < "$p/comm" || continue
    case "$comm" in
        dd|cmp|timeout|bash)
            captured=$((captured + 1)); if ((captured > 32)); then break; fi
            printf '\\n-- %s %s --\\n' "$p" "$comm"
            cat "$p/status" "$p/wchan" "$p/stack"
            ;;
    esac
done
printf '\\n== addresses and links ==\\n'
ip -j address show; ip -j -d link show
printf '\\n== IPv4 rules routes neighbors ==\\n'
ip -4 rule show; ip -4 route show table all; ip -4 neigh show
printf '\\n== IPv6 rules routes neighbors ==\\n'
ip -6 rule show; ip -6 route show table all; ip -6 neigh show
printf '\\n== TCP connection timers and retransmissions ==\\n'
for f in /proc/net/tcp /proc/net/tcp6; do
    printf '\\n-- %s --\\n' "$f"
    count=0
    while IFS= read -r line; do
        count=$((count + 1)); if ((count > 256)); then printf 'truncated\\n'; break; fi
        printf '%s\\n' "$line"
    done < "$f"
done
printf '\\n== transport module timeout settings ==\\n'
for transport in ksocklnd:sock_timeout ko2iblnd:timeout; do
    f="/sys/module/${transport%%:*}/parameters/${transport#*:}"
    if IFS= read -r value < "$f"; then printf '%s=%s\\n' "$f" "$value"; fi
done
printf '\\n== LNet configuration and state ==\\n'
cat /etc/modprobe.d/lnet.conf
lnetctl net show --verbose 3; lnetctl peer show --verbose 3
lnetctl global show; lnetctl stats show
printf '\\n== Lustre imports timeouts and RPC state ==\\n'
lctl get_param timeout ldlm_timeout
lctl get_param 'osc.*.import' 'mdc.*.import' 'mgc.*.import'
lctl get_param 'osc.*.timeouts' 'mdc.*.timeouts' 'mgc.*.timeouts'
lctl get_param 'osc.*.rpc_stats' 'mdc.*.rpc_stats'
printf '\\n== kernel messages ==\\n'
dmesg | tail -200
"""


def _copy_tail(source: Path, target: Path) -> None:
    """Copy at most one MiB, including a serial log's latest failure messages."""
    with source.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - MAX_CAPTURE_BYTES))
        target.write_bytes(stream.read(MAX_CAPTURE_BYTES))


def _host_links(guest: Guest) -> str:
    """Read test-owned TAP state directly without invoking host network tools."""
    result = {}
    for name in (tap_for_name(guest.name), *guest.taps):
        directory = SYS_NET / name
        if not directory.exists():
            # Missing TAPs are evidence, especially when start fails after stop.
            result[name] = {"missing": True}
            continue
        entry = {}
        for field in (
            "ifindex",
            "ifalias",
            "flags",
            "address",
            "operstate",
            "mtu",
            "tun_flags",
            "brport/state",
            "brport/hairpin_mode",
            "brport/learning",
            "brport/unicast_flood",
            "brport/multicast_flood",
            "brport/broadcast_flood",
        ):
            try:
                entry[field] = (directory / field).read_text().strip()
            except OSError as error:
                entry[field] = f"read failed: {error}"
        entry["master"] = str((directory / "master").resolve())
        try:
            entry["master_mtu"] = (
                (directory / "master" / "mtu").read_text().strip()
            )
        except OSError as error:
            entry["master_mtu"] = f"read failed: {error}"
        result[name] = entry
    return json.dumps(result, indent=2)


def capture_failure(lab: Lab, phase: str, reason: str) -> None:
    """Preserve original failure details; diagnostic errors must not replace it."""
    name = "".join(c if c.isalnum() or c in "-_." else "_" for c in phase[:120])
    directory = lab.evidence / f"failure-{name}-{time.monotonic_ns()}"
    try:
        directory.mkdir()
        (directory / "reason.txt").write_text(reason[-MAX_CAPTURE_BYTES:])
    except OSError as error:
        # The original exception remains the primary failure when storage fails.
        warnings.warn(
            f"cannot save Multi-Rail failure evidence: {error}", stacklevel=2
        )
        return
    errors = []
    for vm_name in lab.names[:2]:
        for suffix in ("info", "pid", "log", "log.failed"):
            source = lab.sockets / f"{vm_name}.{suffix}"
            try:
                _copy_tail(source, directory / source.name)
            except OSError as error:
                errors.append(f"copy {source.name}: {error}")
    for guest in lab.guests[:2]:
        try:
            (directory / f"{guest.name}-host-links.json").write_text(
                _host_links(guest)
            )
            result = lab.run(guest, GUEST_STATE, timeout=10, check=False)
            (directory / f"{guest.name}-guest-state.txt").write_text(
                f"rc={result.returncode}\n{result.stdout[-MAX_CAPTURE_BYTES:]}"
                f"\n{result.stderr[-MAX_CAPTURE_BYTES:]}"
            )
        except (OSError, subprocess.SubprocessError) as error:
            # A stopped or unreachable guest must not block the other capture.
            errors.append(f"guest {guest.name}: {error}")
    try:
        (directory / "capture-errors.txt").write_text("\n".join(errors))
    except OSError as error:
        warnings.warn(
            f"cannot save Multi-Rail capture errors: {error}", stacklevel=2
        )
