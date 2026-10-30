"""Check explicit guest routing without touching the host network or procfs."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "targets/common/setup-rail-routing.sh"


def run_helper(
    body: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a sourceable helper with only test-owned routing and procfs paths."""
    return subprocess.run(
        [
            "bash",
            "-eu",
            "-c",
            'source "$1"\n' + body,
            "guest-test",
            str(HELPER),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


@pytest.fixture
def guest_env(tmp_path: Path) -> dict[str, str]:
    """Provide fake iproute2 and all mandatory kernel settings for two rails."""
    binary = tmp_path / "bin"
    binary.mkdir()
    ip = binary / "ip"
    ip.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$IP_LOG"\n'
        '[[ "$*" != "${IP_FAIL:-}" ]] || exit 1\n'
        '[[ "$*" != *"rule del"* ]] || exit 2\n'
    )
    ip.chmod(0o755)
    proc = tmp_path / "proc"
    for family, names, keys in (
        (
            "ipv4",
            ("all", "default", "eth1", "eth2"),
            ("rp_filter", "arp_ignore", "arp_announce"),
        ),
        ("ipv6", ("eth1", "eth2"), ("accept_ra", "autoconf", "accept_dad")),
    ):
        for name in names:
            folder = proc / "net" / family / "conf" / name
            folder.mkdir(parents=True)
            for key in keys:
                (folder / key).write_text("1\n")
    return {
        "PATH": f"{binary}:{os.environ['PATH']}",
        "PROC_SYS_ROOT": str(proc),
        "IP_LOG": str(tmp_path / "ip.log"),
        "RAIL_STATE_DIR": str(tmp_path / "rail-state"),
    }


@pytest.mark.parametrize(
    ("address", "prefix", "want"),
    [
        ("10.50.9.17", "24", "10.50.9.0"),
        ("10.50.9.17", "0", "0.0.0.0"),
        ("10.50.9.17", "32", "10.50.9.17"),
        ("2001:db8:42::11", "64", "2001:db8:42:0:0:0:0:0"),
        ("2001:db8:42::11", "65", "2001:db8:42:0:0:0:0:0"),
        ("::1", "128", "0:0:0:0:0:0:0:1"),
        ("::", "0", "0:0:0:0:0:0:0:0"),
    ],
)
def test_network_address(address: str, prefix: str, want: str) -> None:
    """Network routes must clear the host bits, including compressed IPv6."""
    family = "ipv6" if ":" in address else "ipv4"
    result = run_helper(f"rail_{family}_network '{address}' '{prefix}'")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == want


@pytest.mark.parametrize(
    ("address", "prefix"),
    [
        ("300.1.1.1", "24"),
        ("010.1.1.1", "24"),
        ("1.2.3.4", "33"),
        ("2001::db8::1", "64"),
        ("2001:db8::1", "129"),
        ("1:2:3:4:5:6:7:8:", "64"),
        (":1:2:3:4:5:6:7:8", "64"),
        ("2001:::1", "64"),
    ],
)
def test_bad_network_address(address: str, prefix: str) -> None:
    """Malformed address inputs fail before an iproute2 call can occur."""
    family = "ipv6" if ":" in address else "ipv4"
    result = run_helper(f"rail_{family}_network '{address}' '{prefix}'")
    assert result.returncode != 0


@pytest.mark.parametrize("topology", ["shared", "isolated"])
def test_profile_validates_both_families(topology: str) -> None:
    """The same explicit profile controls IPv4 and IPv6 on each rail."""
    ids = "tcp0,tcp0" if topology == "shared" else "tcp0,tcp1"
    ips = (
        "10.50.0.11,10.50.0.12"
        if topology == "shared"
        else "10.50.0.11,10.51.0.11"
    )
    ip6s = (
        "2001:db8:50::11,2001:db8:50::12"
        if topology == "shared"
        else "2001:db8:50::11,2001:db8:51::11"
    )
    result = run_helper(
        f"rail_validate_profile {topology} tcp,tcp {ids} {ips} 24,24 {ip6s} 64,64"
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        "shared tcp,tcp tcp0,tcp1 10.50.0.11,10.50.0.12 24,24 '' ''",
        "isolated tcp,tcp tcp0,tcp0 10.50.0.11,10.51.0.11 24,24 '' ''",
        "shared tcp,tcp o2ib0,o2ib0 10.50.0.11,10.50.0.12 24,24 '' ''",
        "shared tcp tcp0 10.50.0.11 24 '' ''",
        "shared tcp,tcp tcp0,tcp0 10.50.0.11 24,24 '' ''",
        "shared tcp,tcp tcp0,tcp0 10.50.0.11,10.50.0.12 24,24 2001:db8::11 64,64",
        "shared tcp,tcp tcp0,tcp0 10.50.0.11,10.51.0.12 24,24 '' ''",
        "shared tcp,tcp tcp0,tcp0 10.50.0.11,10.50.0.11 24,24 '' ''",
        "isolated tcp,tcp tcp0,tcp1 10.50.0.11,10.50.1.12 16,24 '' ''",
        "shared tcp,tcp tcp0,tcp0 10.50.0.11,10.50.0.12 24,24 "
        "2001:db8::11,2001:db9::12 64,64",
        "shared tcp,tcp tcp0,tcp0 10.50.0.11,10.50.0.12 24,24 "
        "2001:db8::11,2001:db8:0:0:0:0:0:11 64,64",
        "isolated tcp,tcp tcp0,tcp1 10.50.0.11,10.51.0.12 24,24 "
        "2001:db8::11,2001:db8:1::12 32,64",
        "shared passthrough,passthrough o2ib0,o2ib0 10.50.0.11,10.50.0.12 24,24 '' ''",
    ],
)
def test_profile_rejects_invalid_mapping(arguments: str) -> None:
    """Partial or incompatible LNet profiles cannot silently use legacy setup."""
    result = run_helper("rail_validate_profile " + arguments)
    assert result.returncode != 0
    assert "setup-rail-routing:" in result.stderr


def test_source_routes_and_terminal_rules(guest_env: dict[str, str]) -> None:
    """Both address families must use their NIC and block fallback to main."""
    result = run_helper(
        "rail_configure_globals\n"
        "rail_configure_nic eth2 10.50.0.12 24 2001:db8:50::12 64 2",
        guest_env,
    )
    assert result.returncode == 0, result.stderr
    commands = Path(guest_env["IP_LOG"]).read_text().splitlines()
    assert (
        "-4 route replace 10.50.0.0/24 dev eth2 src 10.50.0.12 table 202"
        in commands
    )
    assert (
        "-6 route replace 2001:db8:50:0:0:0:0:0/64 dev eth2 "
        "src 2001:db8:50:0:0:0:0:12 table 202" in commands
    )
    for family, source, mask in (
        ("-4", "10.50.0.12", 32),
        ("-6", "2001:db8:50:0:0:0:0:12", 128),
    ):
        terminal = (
            f"{family} rule add priority 12005 from {source}/{mask} prohibit"
        )
        lookup = (
            f"{family} rule add priority 12004 from {source}/{mask} lookup 202"
        )
        assert terminal in commands
        assert lookup in commands
        assert commands.index(terminal) < commands.index(lookup)
        address = next(
            item
            for item in commands
            if item.startswith(f"{family} address replace")
        )
        assert commands.index(terminal) < commands.index(address)
        assert f"{family} route replace prohibit default table 202" in commands
    assert not any(
        "rule del" in item and "prohibit" in item for item in commands
    )
    proc = Path(guest_env["PROC_SYS_ROOT"])
    assert (proc / "net/ipv4/conf/all/rp_filter").read_text() == "0\n"
    assert (proc / "net/ipv4/conf/eth2/arp_ignore").read_text() == "1\n"
    assert (proc / "net/ipv4/conf/eth2/arp_announce").read_text() == "2\n"
    assert (proc / "net/ipv6/conf/eth2/accept_ra").read_text() == "0\n"
    assert (proc / "net/ipv6/conf/eth2/autoconf").read_text() == "0\n"
    assert (proc / "net/ipv6/conf/eth2/accept_dad").read_text() == "0\n"


@pytest.mark.parametrize(
    "command",
    [
        "link set eth1 up",
        "-4 address replace 10.50.0.11/24 dev eth1",
        "-4 route flush table 201",
        "-4 route replace 10.50.0.0/24 dev eth1 src 10.50.0.11 table 201",
        "-4 route replace prohibit default table 201",
        "-4 rule add priority 12003 from 10.50.0.11/32 prohibit",
        "-4 rule add priority 12002 from 10.50.0.11/32 lookup 201",
        "-6 address replace 2001:db8::11/64 dev eth1 nodad",
        "-6 route flush table 201",
        "-6 route replace 2001:db8:0:0:0:0:0:0/64 dev eth1 "
        "src 2001:db8:0:0:0:0:0:11 table 201",
        "-6 route replace prohibit default table 201",
        "-6 rule add priority 12002 from 2001:db8:0:0:0:0:0:11/128 lookup 201",
    ],
)
def test_required_command_failure_is_fatal(
    command: str, guest_env: dict[str, str]
) -> None:
    """Missing devices and failed route or IPv6 operations are explicit failures."""
    result = run_helper(
        "rail_configure_nic eth1 10.50.0.11 24 2001:db8::11 64 1",
        {**guest_env, "IP_FAIL": command},
    )
    assert result.returncode != 0
    commands = Path(guest_env["IP_LOG"]).read_text().splitlines()
    if "route " in command or "lookup 201" in command:
        # Every route mutation follows both guards, which setup never removes.
        assert commands.index(
            "-4 rule add priority 12003 from 10.50.0.11/32 prohibit"
        ) < commands.index(command)
        assert commands.index(
            "-6 rule add priority 12003 from 2001:db8:0:0:0:0:0:11/128 prohibit"
        ) < commands.index(command)
        assert not any(
            "rule del" in item and "prohibit" in item for item in commands
        )


@pytest.mark.parametrize(
    "command",
    [
        "-4 rule add priority 12003 from 10.50.0.11/32 prohibit",
        "-6 rule add priority 12003 from 2001:db8:0:0:0:0:0:11/128 prohibit",
    ],
)
def test_guard_failure_does_not_assign_either_address(
    command: str, guest_env: dict[str, str]
) -> None:
    """A missing IPv4 or IPv6 guard must fail before the NIC gains a source."""
    result = run_helper(
        "rail_configure_nic eth1 10.50.0.11 24 2001:db8::11 64 1",
        {**guest_env, "IP_FAIL": command},
    )
    assert result.returncode != 0
    commands = Path(guest_env["IP_LOG"]).read_text()
    assert command in commands
    assert "address replace" not in commands


@pytest.mark.parametrize("action", ["8", "prohibit"])
def test_existing_source_guard_is_never_removed_or_duplicated(
    action: str, guest_env: dict[str, str]
) -> None:
    """Repeat setup must retain the terminal rule while replacing owned routes."""
    marker = Path(guest_env["RAIL_STATE_DIR"]) / "ipv4-1"
    marker.parent.mkdir()
    marker.write_text("v1|-4|1|eth1|10.50.0.11|10.50.0.0/24\n")
    ip = Path(guest_env["PATH"].split(":")[0]) / "ip"
    ip.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$IP_LOG"\n'
        'if [[ "$*" == "-N -4 rule show priority 12003" ]]; then\n'
        '    printf "12003: from 10.50.0.11 %s\\n" "$RULE_ACTION"\n'
        "fi\n"
    )
    result = run_helper(
        "rail_configure_nic eth1 10.50.0.11 24 '' '' 1",
        {**guest_env, "RULE_ACTION": action},
    )
    assert result.returncode == 0, result.stderr
    commands = Path(guest_env["IP_LOG"]).read_text()
    assert "-4 route flush table 201" in commands
    assert "rule add priority 12003" not in commands
    assert "rule del priority 12003" not in commands


@pytest.mark.parametrize(
    "knob", ["net/ipv4/conf/eth1/arp_ignore", "net/ipv6/conf/eth1/autoconf"]
)
def test_missing_kernel_setting_is_fatal(
    knob: str, guest_env: dict[str, str]
) -> None:
    """A guest without a required kernel setting cannot claim healthy rails."""
    setting = Path(guest_env["PROC_SYS_ROOT"]) / knob
    setting.unlink()
    result = run_helper(
        "rail_configure_nic eth1 10.50.0.11 24 2001:db8::11 64 1", guest_env
    )
    assert result.returncode != 0
    assert "required kernel setting is missing" in result.stderr
    assert "address replace" not in Path(guest_env["IP_LOG"]).read_text()
    assert "link set eth1 up" not in Path(guest_env["IP_LOG"]).read_text()


@pytest.mark.parametrize("count", [1, 8, 9])
def test_rule_cleanup_is_bounded(count: int, guest_env: dict[str, str]) -> None:
    """Only bounded matching rules can be deleted from an owned priority."""
    ip = Path(guest_env["PATH"].split(":")[0]) / "ip"
    rules = "12002: from 10.50.0.11 lookup 201\n" * count
    ip.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$IP_LOG"\n'
        'if [[ "$*" == "-N -4 rule show priority 12002" ]]; then\n'
        '    printf "%s" "$RULES"\n'
        "fi\n"
    )
    result = run_helper(
        "rail_clear_rule -4 12002 lookup 201 10.50.0.11",
        {**guest_env, "RULES": rules},
    )
    commands = Path(guest_env["IP_LOG"]).read_text().splitlines()
    if count > 8:
        assert result.returncode != 0
        assert "too many rules" in result.stderr
        assert commands == ["-N -4 rule show priority 12002"]
    else:
        assert result.returncode == 0, result.stderr
        assert commands == (
            ["-N -4 rule show priority 12002"]
            + ["-4 rule del priority 12002 from 10.50.0.11 lookup 201"] * count
        )


def test_rule_cleanup_preserves_foreign_rule(guest_env: dict[str, str]) -> None:
    """A conflicting rule must stop setup without deleting another owner's rule."""
    ip = Path(guest_env["PATH"].split(":")[0]) / "ip"
    ip.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$IP_LOG"\n'
        'printf "%s\\n" "12002: from all lookup 99"\n'
    )
    result = run_helper(
        "rail_clear_rule -4 12002 lookup 201 10.50.0.11", guest_env
    )
    assert result.returncode != 0
    assert "foreign rule" in result.stderr
    assert Path(guest_env["IP_LOG"]).read_text().splitlines() == [
        "-N -4 rule show priority 12002"
    ]


@pytest.mark.parametrize("action", ["8", "prohibit"])
def test_rule_cleanup_accepts_prohibit_action(
    action: str, guest_env: dict[str, str]
) -> None:
    """iproute2 numeric output represents the terminal prohibit action as 8."""
    ip = Path(guest_env["PATH"].split(":")[0]) / "ip"
    ip.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$IP_LOG"\n'
        'if [[ "$*" == "-N -4 rule show priority 12003" ]]; then\n'
        '    printf "12003: from 10.50.0.11 %s\\n" "$RULE_ACTION"\n'
        "fi\n"
    )
    result = run_helper(
        "rail_clear_rule -4 12003 prohibit 201 10.50.0.11",
        {**guest_env, "RULE_ACTION": action},
    )
    assert result.returncode == 0, result.stderr
    assert (
        "-4 rule del priority 12003 from 10.50.0.11 prohibit"
        in Path(guest_env["IP_LOG"]).read_text()
    )


@pytest.mark.parametrize(
    "entry",
    [
        "12002: from 10.50.0.99 lookup 201",
        "12002: from 10.50.0.11 to 192.0.2.0/24 lookup 201",
    ],
)
def test_rule_cleanup_preserves_other_sources(
    entry: str, guest_env: dict[str, str]
) -> None:
    """A matching priority and table do not prove that a rule belongs to this rail."""
    ip = Path(guest_env["PATH"].split(":")[0]) / "ip"
    ip.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$IP_LOG"\n'
        'printf "%s\\n" "$RULE_ENTRY"\n'
    )
    result = run_helper(
        "rail_clear_rule -4 12002 lookup 201 10.50.0.11",
        {**guest_env, "RULE_ENTRY": entry},
    )
    assert result.returncode != 0
    assert "foreign rule" in result.stderr
    assert "rule del" not in Path(guest_env["IP_LOG"]).read_text()


def test_unmarked_populated_table_is_preserved(
    guest_env: dict[str, str],
) -> None:
    """An existing table without a runtime ownership marker cannot be claimed."""
    ip = Path(guest_env["PATH"].split(":")[0]) / "ip"
    ip.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$IP_LOG"\n'
        'if [[ "$*" == "-o -4 route show table 201" ]]; then\n'
        '    printf "%s\\n" "192.0.2.0/24 dev eth9 scope link"\n'
        "fi\n"
    )
    result = run_helper(
        "rail_clear_routes -4 1 10.50.0.11 10.50.0.0 24 eth1", guest_env
    )
    assert result.returncode != 0
    assert "unmanaged routes" in result.stderr
    commands = Path(guest_env["IP_LOG"]).read_text()
    assert "route flush" not in commands
    assert "route replace" not in commands
    assert "rule del" not in commands


def test_marked_table_rejects_foreign_routes(guest_env: dict[str, str]) -> None:
    """A marker cannot authorize deletion of a new foreign route in the table."""
    marker = Path(guest_env["RAIL_STATE_DIR"]) / "ipv4-1"
    marker.parent.mkdir()
    marker.write_text("v1|-4|1|eth1|10.50.0.11|10.50.0.0/24\n")
    ip = Path(guest_env["PATH"].split(":")[0]) / "ip"
    ip.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$IP_LOG"\n'
        'if [[ "$*" == "-o -4 route show table 201" ]]; then\n'
        '    printf "%s\\n" "192.0.2.0/24 dev eth9 scope link"\n'
        "fi\n"
    )
    result = run_helper(
        "rail_clear_routes -4 1 10.50.0.11 10.50.0.0 24 eth1", guest_env
    )
    assert result.returncode != 0
    assert "foreign routes" in result.stderr
    assert "route flush" not in Path(guest_env["IP_LOG"]).read_text()
