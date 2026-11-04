"""Check that a guest applied the explicitly requested NIC layout."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from ipaddress import ip_network

from .vm_net import run_ssh
from .vm_state import VMInfo

_MAX_CONFIG = 65536
_CONFIG_READ = (
    "ltvm_conf_bytes=$(stat -Lc %s -- /etc/modprobe.d/lnet.conf) && "
    f'if [ "$ltvm_conf_bytes" -le {_MAX_CONFIG} ]; then '
    f"head -c {_MAX_CONFIG + 1} -- /etc/modprobe.d/lnet.conf; "
    "else printf 'LNet configuration exceeds 64 KiB\\n' >&2; exit 1; fi"
)
_NETWORK = re.compile(
    r"(tcp(?:\d+)?|o2ib(?:\d+)?)\s*\(([^()]*)\)\s*(?:\[[^\[\]]*\])?"
)


def _read(vm: VMInfo, command: str, deadline: float | None = None) -> str:
    """Use the existing SSH transport; no remote file API is available."""
    timeout = 30
    if deadline is not None:
        # Every request shares one monotonic startup deadline.
        remaining = deadline - time.monotonic()
        if remaining < 1:
            raise TimeoutError(f"{vm.name}: timed out waiting for NIC setup")
        timeout = min(timeout, int(remaining))
    result = run_ssh(vm.ip, command, timeout=timeout, retry_connect=False)
    if result.returncode:
        # An unreadable guest state must not look like a valid layout.
        raise ValueError(
            f"cannot read NIC configuration on {vm.name}: {result.stderr.strip()}"
        )
    body = result.stdout
    if not isinstance(body, str):
        raise ValueError(
            f"cannot read NIC configuration on {vm.name}: no text output"
        )
    if len(body.encode("utf-8")) > _MAX_CONFIG:
        raise ValueError(f"NIC configuration on {vm.name} exceeds 64 KiB")
    return body


def _interfaces(value: str) -> set[str]:
    """Ignore balanced CPT expressions before splitting interface commas."""
    names = re.sub(r"\[[^\[\]]*\]", "", value)
    # CPT commas are not interface separators. Unbalanced brackets are errors.
    if "[" in names or "]" in names:
        raise ValueError("cannot read the guest LNet interface CPT setting")
    members = {name.strip() for name in names.split(",")}
    if "" in members:
        raise ValueError("cannot read an empty guest LNet interface")
    return members


def _members(body: str) -> dict[str, set[str]]:
    """Read network/interface mappings while allowing other LNet options."""
    value = ""
    for line in body.replace("\\\n", "").splitlines():
        words = shlex.split(line, comments=True)
        if words[:2] != ["options", "lnet"]:
            # Other modules do not set LNet's networks parameter.
            continue
        for word in words[2:]:
            if word.startswith("networks="):
                value = word.removeprefix("networks=")
    value = value.strip()
    groups: dict[str, set[str]] = {}
    end = 0
    for match in _NETWORK.finditer(value):
        if value[end : match.start()].strip() != ("," if end else ""):
            raise ValueError("cannot read the guest LNet networks mapping")
        net = match[1]
        if net in ("tcp", "o2ib"):
            # LNet's bare transport name is network zero.
            net += "0"
        if net in groups:
            raise ValueError(f"duplicate LNet network {net}")
        groups[net] = _interfaces(match[2])
        end = match.end()
    if end != len(value):
        raise ValueError("cannot read the guest LNet networks mapping")
    return groups


def _check_networks(vm: VMInfo, deadline: float | None = None) -> None:
    """Reject an old image or saved config that ignored explicit LNet names."""
    wanted: dict[str, set[str]] = {}
    for index, nic in enumerate(vm.nics, start=1):
        if nic.net:
            wanted.setdefault(nic.net, set()).add(f"eth{index}")
    if not wanted:
        return
    # stat follows symlinks and checks bytes before text capture can fold CRLF.
    body = _read(vm, _CONFIG_READ, deadline)
    got = _members(body)
    for net, interfaces in wanted.items():
        if got.get(net) != interfaces:
            raise ValueError(
                f"{vm.name}: LNet network {net} does not use "
                f"{','.join(sorted(interfaces))}; check lnet.conf or rebuild "
                "the image with `ltvm build image`"
            )


def _check_addresses(vm: VMInfo, deadline: float | None = None) -> None:
    """Check explicit prefixes as well as addresses, including old images."""
    for index, nic in enumerate(vm.nics):
        if not (nic.subnet or nic.subnet6):
            continue
        # iproute2 reads netlink state that procfs/sysfs do not expose.
        probe = (
            f"ip -j address show dev eth{index + 1} | head -c {_MAX_CONFIG + 1}"
        )
        # Keep JSON reads bounded and preserve an iproute2 failure status.
        command = "bash -o pipefail -c " + shlex.quote(probe)
        body = _read(vm, command, deadline)
        entries = json.loads(body)
        if (
            not isinstance(entries, list)
            or len(entries) != 1
            or not isinstance(entries[0], dict)
            or not isinstance(entries[0].get("addr_info"), list)
            or not all(
                isinstance(item, dict) for item in entries[0]["addr_info"]
            )
        ):
            raise ValueError(f"{vm.name}: cannot read eth{index + 1} addresses")
        if not all(
            isinstance(item.get("local"), str)
            and type(item.get("prefixlen")) is int
            for item in entries[0]["addr_info"]
        ):
            # Reject malformed tool output before building a hashable set.
            raise ValueError(
                f"{vm.name}: invalid eth{index + 1} address record"
            )
        actual = {
            (entry.get("local"), entry.get("prefixlen"))
            for entry in entries[0].get("addr_info", [])
        }
        for subnet, addresses in (
            (nic.subnet, vm.nic_ips),
            (nic.subnet6, vm.nic_ip6s),
        ):
            if not subnet:
                continue
            if index >= len(addresses):
                raise ValueError(
                    f"{vm.name}: missing saved eth{index + 1} address"
                )
            wanted = (addresses[index], ip_network(subnet).prefixlen)
            if wanted not in actual:
                raise ValueError(
                    f"{vm.name}: eth{index + 1} did not apply {wanted[0]}/{wanted[1]}; "
                    "check guest networking or rebuild the image"
                )


def check_layout(vm: VMInfo, *, deadline: float | None = None) -> None:
    """Fail create/start when a requested NIC option was not applied."""
    _check_networks(vm, deadline)
    _check_addresses(vm, deadline)


def wait_for_layout(vm: VMInfo, timeout: int = 30) -> None:
    """Wait for data setup after SSH is ready; never accept a partial layout."""
    if type(timeout) is not int or not 1 <= timeout <= 30:
        raise ValueError("NIC setup timeout must be 1..30 seconds")
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    for attempt in range(61):
        try:
            check_layout(vm, deadline=deadline)
            return
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            if isinstance(error, TimeoutError) and last_error is not None:
                # Keep the useful state error when the shared time budget ends.
                raise last_error from None
            last_error = error
            remaining = deadline - time.monotonic()
            if remaining <= 1 or attempt == 60:
                raise
            # Boot helpers can still be assigning addresses after SSH starts.
            time.sleep(min(0.5, remaining))
    raise TimeoutError(f"{vm.name}: timed out waiting for NIC setup")
