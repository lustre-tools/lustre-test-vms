"""The ``--nic TYPE[:key=value,...]`` spec of one extra NIC.

One parser turns the CLI text into a :class:`NicSpec`, and ``str()`` turns
it back into the canonical text that the ``.info`` file stores, so a spec
reads back as the value it was saved from.

A key is one entry in ``_KEYS``: the field it fills, the NIC types that
accept it, and a converter that validates the value.  A new per-NIC
option is a new field on ``NicSpec`` plus one entry there.

A type can name one key as positional, so the first item may leave out
``key=``.  ``passthrough:0000:85:00.1`` is ``passthrough:bdf=0000:85:00.1``,
which keeps the spelling ltvm has always accepted, and the one older
``.info`` files hold.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass, fields

# The extra-NIC types, in the order the CLI help lists them.
NIC_TYPES = ("tcp", "softroce", "passthrough", "shmnet")


class NicSpecError(ValueError):
    """A --nic value that does not parse or validate."""


@dataclass(frozen=True)
class NicSpec:
    """One extra NIC (eth1, eth2, ...) as given to ``--nic``."""

    type: str
    # PCI address of the host device a passthrough NIC hands to the guest.
    bdf: str = ""
    # Guest NUMA node the NIC's PCI device reports (node=N), or None for
    # the default: no node (-1), as every virtio NIC on ltvm's PCI root.
    node: int | None = None
    # Empty values keep the existing automatic LNet names and subnets.
    net: str = ""
    subnet: str = ""
    subnet6: str = ""
    # vhost-user socket of a shmnet NIC's switch; empty is the daemon
    # ltvm runs for this user.
    socket: str = ""

    @classmethod
    def parse(cls, raw: str) -> NicSpec:
        """Parse ``TYPE[:key=value,...]``; raise NicSpecError if invalid."""
        if not raw or not raw.strip():
            raise NicSpecError("--nic value is empty")
        # A NIC has a fixed set of short keys; bound parser work before split.
        if len(raw) > 512:
            raise NicSpecError("--nic value is longer than 512 characters")
        type_part, sep, rest = raw.partition(":")
        nic_type = type_part.strip().lower()
        if nic_type not in NIC_TYPES:
            raise NicSpecError(
                f"unknown --nic type {nic_type!r}: valid types are: "
                f"{', '.join(NIC_TYPES)}"
            )
        values: dict[str, object] = {}
        if sep:
            if not rest.strip():
                raise NicSpecError(f"--nic {raw}: nothing after ':'")
            for pos, item in enumerate(rest.split(",")):
                key, eq, val = item.partition("=")
                if not eq:
                    positional = _POSITIONAL.get(nic_type)
                    if pos != 0 or positional is None:
                        raise NicSpecError(
                            f"--nic {raw}: {item!r} is not key=value"
                            f"{_keys_hint(nic_type)}"
                        )
                    key, val = positional, item
                key = key.strip().lower()
                k = _KEYS.get(key)
                if k is None or nic_type not in k.types:
                    raise NicSpecError(
                        f"--nic {raw}: unknown key {key!r} for type "
                        f"{nic_type!r}{_keys_hint(nic_type)}"
                    )
                if k.field in values:
                    raise NicSpecError(f"--nic {raw}: {key} given twice")
                values[k.field] = k.convert(val.strip())
        if nic_type == "passthrough" and "bdf" not in values:
            raise NicSpecError(
                "--nic passthrough requires a PCIe BDF arg, "
                "e.g. --nic passthrough:0000:85:00.1"
            )
        spec = cls(type=nic_type, **values)  # type: ignore[arg-type]
        expected = "tcp" if nic_type in ("tcp", "shmnet") else "o2ib"
        # The LND must agree with the NIC provider used by the guest.
        if spec.net and not spec.net.startswith(expected):
            raise NicSpecError(
                f"--nic {raw}: net must use {expected} for type {nic_type}"
            )
        return spec

    def __str__(self) -> str:
        """The canonical spelling, which parse() reads back unchanged."""
        items: list[str] = []
        positional = _POSITIONAL.get(self.type)
        for key, k in _KEYS.items():
            value = getattr(self, k.field)
            if value == _DEFAULTS[k.field]:
                continue
            if key == positional:
                # parse() takes the positional value only first.
                items.insert(0, str(value))
            else:
                items.append(f"{key}={value}")
        return f"{self.type}:{','.join(items)}" if items else self.type

    def cmdline_entry(self) -> str:
        """This NIC's entry in the guest's ``fc_nics=`` list.

        The guest knows only ``TYPE[;ARG]``, ARG being a passthrough BDF
        with its ':' turned into ';' so the list stays comma-separated.
        Every other key reaches the guest in a parameter of its own, or
        not at all, so an image older than the key reads the list as
        before.
        """
        if self.bdf:
            return f"{self.type};{self.bdf.replace(':', ';')}"
        # A shmnet NIC is a plain virtio-net to the guest.
        if self.type == "shmnet":
            return "tcp"
        return self.type


_BDF_RE = re.compile(
    r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$"
)


def _bdf(value: str) -> str:
    """Check the existing PCI BDF spelling before it reaches sysfs."""
    if not _BDF_RE.match(value):
        raise NicSpecError(
            f"--nic passthrough arg {value!r} is not a valid PCIe BDF "
            f"(expected domain:bus:device.function like 0000:85:00.1)"
        )
    return value


def socket_path_problem(value: str) -> str | None:
    """Why *value* cannot be a shmrdmad socket path, or None.

    ',' would split QEMU's -chardev options, '|' the NICS= list in the
    .info file, ':' a --nic spec, and a control character could start
    a new .info line.
    """
    if not value.startswith("/") or any(
        c in ",|: " or not c.isprintable() for c in value
    ):
        return (
            "absolute path of a shmrdmad socket, without ',', '|', ':', "
            "blanks or control characters"
        )
    return None


def _socket(value: str) -> str:
    """An absolute path to a shmrdmad switch socket."""
    problem = socket_path_problem(value)
    if problem:
        raise NicSpecError(
            f"--nic shmnet socket {value!r}: expected the {problem}"
        )
    return value


def _node(value: str) -> int:
    """Read a bounded decimal NUMA node; topology checks run at create."""
    if not value.isdigit():
        raise NicSpecError(
            f"--nic node={value!r}: expected a NUMA node number (0, 1, ...)"
        )
    return int(value)


def _net(value: str) -> str:
    """Accept one canonical TCP or o2ib network name with a 16-bit ID."""
    match = re.fullmatch(r"(tcp|o2ib)(0|[1-9][0-9]{0,4})", value)
    if match is None or int(match[2]) > 65535:
        raise NicSpecError(
            f"--nic net={value!r}: expected tcp0..tcp65535 or o2ib0..o2ib65535"
        )
    return value


def _subnet(value: str, version: int) -> str:
    """Check a strict unicast CIDR and leave its canonical spelling."""
    # Scope IDs belong to link-local addresses, not reusable data subnets.
    if "%" in value:
        raise NicSpecError(
            f"--nic subnet={value!r}: scope IDs are not supported"
        )
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise NicSpecError(f"--nic subnet={value!r}: {exc}") from None
    if network.version != version:
        raise NicSpecError(f"--nic subnet={value!r}: expected IPv{version}")
    # Ordinary data subnets need at least two distinct host addresses.
    maximum = 30 if version == 4 else 126
    if network.prefixlen > maximum:
        raise NicSpecError(
            f"--nic subnet={value!r}: IPv{version} prefix must be /{maximum} or shorter"
        )
    forbidden = (
        (
            "0.0.0.0/8",
            "127.0.0.0/8",
            "169.254.0.0/16",
            "224.0.0.0/4",
            "240.0.0.0/4",
        )
        if version == 4
        else ("::/128", "::1/128", "fe80::/10", "ff00::/8")
    )
    if any(
        network.overlaps(ipaddress.ip_network(block)) for block in forbidden
    ):
        raise NicSpecError(
            f"--nic subnet={value!r}: expected a unicast network"
        )
    return str(network)


def _subnet4(value: str) -> str:
    """Read a data NIC's IPv4 subnet."""
    return _subnet(value, 4)


def _subnet6(value: str) -> str:
    """Read a data NIC's IPv6 subnet."""
    return _subnet(value, 6)


@dataclass(frozen=True)
class _Key:
    field: str
    types: tuple[str, ...]
    convert: Callable[[str], object]


# key -> where it goes and who takes it.  Iteration order is the order
# str() writes the keys in.
_KEYS: dict[str, _Key] = {
    "bdf": _Key("bdf", ("passthrough",), _bdf),
    # Not passthrough: the guest sets the node of the NIC it finds as
    # ethN, and a passed-through device need not appear as ethN.
    "node": _Key("node", ("tcp", "softroce", "shmnet"), _node),
    "net": _Key("net", ("tcp", "softroce", "shmnet"), _net),
    "subnet": _Key("subnet", ("tcp", "softroce", "shmnet"), _subnet4),
    "subnet6": _Key("subnet6", ("tcp", "softroce", "shmnet"), _subnet6),
    "socket": _Key("socket", ("shmnet",), _socket),
}

# The key a type's first item may give without "key=".
_POSITIONAL: dict[str, str] = {"passthrough": "bdf", "shmnet": "socket"}

_DEFAULTS = {f.name: f.default for f in fields(NicSpec)}


def _keys_hint(nic_type: str) -> str:
    """List only the keys accepted by this NIC type."""
    keys = [key for key, k in _KEYS.items() if nic_type in k.types]
    if not keys:
        return f" (type {nic_type!r} takes no keys)"
    return f" (keys for {nic_type!r}: {', '.join(keys)})"


def load_stored(raw: str) -> NicSpec:
    """A spec read back from an ``.info`` file.

    ltvm before this parser stored whatever followed ``TYPE:`` and used
    it only for passthrough, so a stored ``tcp:<junk>`` loads as plain
    ``tcp`` rather than making the VM unreadable.
    """
    try:
        return NicSpec.parse(raw)
    except NicSpecError:
        nic_type = raw.partition(":")[0].strip().lower()
        # Only the old positional junk spelling is compatible. A broken
        # keyed spec must not silently discard its stored network layout.
        if (
            nic_type in NIC_TYPES
            and nic_type != "passthrough"
            and "=" not in raw
        ):
            return NicSpec(type=nic_type)
        raise


def numa_problem(nics: list[NicSpec], numa_nodes: int) -> str | None:
    """Why a NIC's node= does not fit *numa_nodes*, or None if all do."""
    for idx, nic in enumerate(nics, start=1):
        if nic.node is None:
            continue
        if numa_nodes < 2:
            return (
                f"--nic {nic} (eth{idx}) needs --numa-nodes 2 or more "
                f"(the VM has {numa_nodes})"
            )
        if nic.node >= numa_nodes:
            return (
                f"--nic {nic} (eth{idx}): node {nic.node} does not exist; "
                f"the VM's {numa_nodes} NUMA nodes are 0-{numa_nodes - 1}"
            )
    return None
