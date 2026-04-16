"""Tests for ltvm_pkg/vm_net.py: TAP/MAC, IP allocation, hosts/ssh registry."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import vm_net
from ltvm_pkg.vm_state import VMInfo

# ── deterministic name → tap / mac ───────────────────────


class TestTapForName:
    """tap_for_name is deterministic and obeys the 15-char ifname limit."""

    def test_short_name_passthrough(self) -> None:
        assert vm_net.tap_for_name("co1-mds") == "tap-co1-mds"

    def test_long_name_is_hashed(self) -> None:
        """Names longer than 11 chars fall back to an md5-truncated suffix."""
        tap = vm_net.tap_for_name("co123-very-long-name-indeed")
        # Linux interface names cap at 15 (IFNAMSIZ=16 incl. nul)
        assert len(tap) <= 15
        assert tap.startswith("tap-")
        # Deterministic: the same input always yields the same hash
        assert vm_net.tap_for_name("co123-very-long-name-indeed") == tap

    def test_different_long_names_get_different_taps(self) -> None:
        a = vm_net.tap_for_name("x" * 30)
        b = vm_net.tap_for_name("y" * 30)
        assert a != b


class TestExtraTapForName:
    """extra_tap_for_name builds ifnames for extra NICs under the 15-char limit."""

    def test_appends_index_for_short_names(self) -> None:
        """Short VM names: tap-<name>-<idx>."""
        assert vm_net.extra_tap_for_name("co1-mds", 1) == "tap-co1-mds-1"
        assert vm_net.extra_tap_for_name("co1-mds", 2) == "tap-co1-mds-2"

    def test_stays_under_15_chars(self) -> None:
        """Long names fall back to a hash so the full ifname fits IFNAMSIZ."""
        long = "x" * 30
        for idx in (1, 9, 10):
            name = vm_net.extra_tap_for_name(long, idx)
            assert len(name) <= 15, name
            assert name.startswith("tap-")
            assert name.endswith(f"-{idx}")

    def test_different_index_different_name(self) -> None:
        assert (
            vm_net.extra_tap_for_name("co1-mds", 1)
            != vm_net.extra_tap_for_name("co1-mds", 2)
        )

    def test_distinct_from_mgmt_tap(self) -> None:
        """The mgmt TAP (tap_for_name) must not collide with any extras."""
        mgmt = vm_net.tap_for_name("co1-mds")
        for idx in (1, 2, 3):
            assert vm_net.extra_tap_for_name("co1-mds", idx) != mgmt

    def test_index_zero_is_rejected(self) -> None:
        """Index 0 is the mgmt NIC; use tap_for_name instead."""
        with pytest.raises(ValueError):
            vm_net.extra_tap_for_name("co1-mds", 0)


class TestExtraMacForName:
    """extra_mac_for_name produces deterministic, non-colliding MACs."""

    def test_prefix_is_locally_administered(self) -> None:
        mac = vm_net.extra_mac_for_name("co1-mds", 1)
        assert mac.startswith("AA:FC:00:")

    def test_distinct_from_mgmt_mac(self) -> None:
        """Must not collide with the mgmt NIC MAC for the same VM."""
        assert (
            vm_net.extra_mac_for_name("co1-mds", 1)
            != vm_net.mac_for_name("co1-mds")
        )

    def test_different_index_different_mac(self) -> None:
        assert (
            vm_net.extra_mac_for_name("co1-mds", 1)
            != vm_net.extra_mac_for_name("co1-mds", 2)
        )

    def test_deterministic(self) -> None:
        assert (
            vm_net.extra_mac_for_name("co1-mds", 1)
            == vm_net.extra_mac_for_name("co1-mds", 1)
        )

    def test_index_zero_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            vm_net.extra_mac_for_name("co1-mds", 0)


class TestMacForName:
    """mac_for_name is deterministic and stays in the AA:FC:00 locally-adminned range."""

    def test_format_and_prefix(self) -> None:
        mac = vm_net.mac_for_name("co1-mds")
        parts = mac.split(":")
        assert len(parts) == 6
        assert parts[0] == "AA"
        assert parts[1] == "FC"
        assert parts[2] == "00"
        # Remaining octets are hex chars
        for p in parts[3:]:
            assert len(p) == 2
            int(p, 16)  # raises on non-hex

    def test_deterministic(self) -> None:
        assert vm_net.mac_for_name("co2-oss") == vm_net.mac_for_name("co2-oss")

    def test_distinct_names_distinct_macs(self) -> None:
        assert vm_net.mac_for_name("a") != vm_net.mac_for_name("b")


# ── alloc_ip ─────────────────────────────────────────────


@pytest.fixture
def tmp_vmdir(tmp_path: Path) -> Path:
    """Redirect VM_DIR, SOCKETS, and the lock path into tmp_path.

    alloc_ip looks up existing VMs via VMInfo.all_names()/load(), which
    read from SOCKETS.  The lock file is stored relative to VM_DIR.
    """
    sockets = tmp_path / "sockets"
    sockets.mkdir()
    lock_path = tmp_path / ".ip-alloc.lock"
    hosts_lock = tmp_path / ".hosts.lock"
    with (
        patch("ltvm_pkg.vm_state.VM_DIR", tmp_path),
        patch("ltvm_pkg.vm_state.SOCKETS", sockets),
        patch("ltvm_pkg.vm_net._IP_LOCK_PATH", lock_path),
        patch("ltvm_pkg.vm_net._HOSTS_LOCK_PATH", hosts_lock),
    ):
        yield tmp_path


class TestAllocIp:
    """alloc_ip avoids collisions and respects explicit IPs."""

    def test_first_vm_gets_an_ip(self, tmp_vmdir: Path) -> None:
        """With no peers, alloc_ip picks a hash-derived IP in the subnet."""
        with vm_net.alloc_ip("first-vm") as ip:
            assert ip.startswith("192.168.100.")
            octet = int(ip.rsplit(".", 1)[1])
            assert 10 <= octet < 254

    def test_collision_skips_to_next_free(self, tmp_vmdir: Path) -> None:
        """A peer holding the hash-derived IP forces alloc_ip to walk."""
        # Pre-seed a VM holding the IP alloc_ip would normally give "repeat"
        with vm_net.alloc_ip("repeat") as first:
            blocker = VMInfo(name="blocker", ip=first)
            blocker.save()
        with vm_net.alloc_ip("repeat") as second:
            assert second != first

    def test_explicit_ip_respected(self, tmp_vmdir: Path) -> None:
        with vm_net.alloc_ip("explicit", explicit_ip="192.168.100.77") as ip:
            assert ip == "192.168.100.77"

    def test_explicit_ip_in_use_dies(self, tmp_vmdir: Path) -> None:
        """Explicit IP conflict with a live VM errors loudly."""
        other = VMInfo(name="existing", ip="192.168.100.42")
        other.save()
        with pytest.raises(SystemExit):
            with vm_net.alloc_ip("new", explicit_ip="192.168.100.42"):
                pass

    def test_explicit_ip_same_name_allowed(self, tmp_vmdir: Path) -> None:
        """Re-allocating the same IP to the same VM name is a no-op, not error."""
        existing = VMInfo(name="me", ip="192.168.100.99")
        existing.save()
        with vm_net.alloc_ip("me", explicit_ip="192.168.100.99") as ip:
            assert ip == "192.168.100.99"

    def test_returns_unique_ips_for_many_names(self, tmp_vmdir: Path) -> None:
        """Allocate 10 VMs; all IPs should be distinct and in-subnet."""
        ips = []
        for i in range(10):
            with vm_net.alloc_ip(f"vm{i}") as ip:
                ips.append(ip)
                VMInfo(name=f"vm{i}", ip=ip).save()
        assert len(set(ips)) == 10
        for ip in ips:
            assert ip.startswith("192.168.100.")


# ── _atomic_write ────────────────────────────────────────


class TestAtomicWrite:
    """_atomic_write writes via tmpfile + rename and preserves mode."""

    def test_creates_file(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        vm_net._atomic_write(target, "hello\n")
        assert target.read_text() == "hello\n"

    def test_overwrites_atomically(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        target.write_text("old\n")
        vm_net._atomic_write(target, "new\n")
        assert target.read_text() == "new\n"

    def test_preserves_existing_mode(self, tmp_path: Path) -> None:
        """If the target exists, chmod copies its mode onto the tmpfile."""
        target = tmp_path / "out.txt"
        target.write_text("old")
        target.chmod(0o600)
        vm_net._atomic_write(target, "new")
        mode = target.stat().st_mode & 0o777
        assert mode == 0o600

    def test_no_leftover_tmp_on_success(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        vm_net._atomic_write(target, "x")
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".")]
        assert leftovers == []


# ── register / unregister ssh name ───────────────────────


class TestSshRegistry:
    """register_ssh_name and unregister_ssh_name manage /etc/hosts + ssh config."""

    @pytest.fixture
    def fake_paths(self, tmp_vmdir: Path, tmp_path: Path):
        """Redirect /etc/hosts and ~/.ssh/config into the tmp tree."""
        fake_hosts = tmp_path / "etc-hosts"
        fake_hosts.write_text("127.0.0.1 localhost\n")
        fake_ssh = tmp_path / "home" / ".ssh"
        fake_ssh.mkdir(parents=True)

        # Patch Path("/etc/hosts") + _real_user_ssh_dir.
        real_path = vm_net.Path

        class _PathRouter(type(real_path)):
            def __call__(cls, *a, **k):
                if a and a[0] == "/etc/hosts":
                    return fake_hosts
                if a and a[0] == "/run/dnsmasq.pid":
                    return tmp_path / "dnsmasq.pid"
                return real_path(*a, **k)

        with (
            patch(
                "ltvm_pkg.vm_net._real_user_ssh_dir",
                return_value=("nobody", fake_ssh),
            ),
            patch("ltvm_pkg.vm_net.reload_dns"),
            patch(
                "ltvm_pkg.vm_net.Path",
                new=_PathRouter("Path", (real_path,), {}),
            ),
            # Tests run unprivileged; swallow chown so the non-root
            # fallback path doesn't leak a PermissionError.
            patch("ltvm_pkg.vm_net.os.chown"),
        ):
            yield fake_hosts, fake_ssh

    def test_register_adds_hosts_entry(self, fake_paths) -> None:
        fake_hosts, fake_ssh = fake_paths
        vm_net.register_ssh_name("co1-single", "192.168.100.50")
        hosts_text = fake_hosts.read_text()
        assert "192.168.100.50" in hosts_text
        assert "co1-single" in hosts_text
        assert "# qemu-vm:co1-single" in hosts_text
        assert "127.0.0.1 localhost" in hosts_text

    def test_register_is_idempotent(self, fake_paths) -> None:
        """Registering the same name twice leaves only one entry."""
        fake_hosts, _ = fake_paths
        vm_net.register_ssh_name("co1-single", "192.168.100.50")
        vm_net.register_ssh_name("co1-single", "192.168.100.50")
        matching = [
            ln
            for ln in fake_hosts.read_text().splitlines()
            if "# qemu-vm:co1-single" in ln
        ]
        assert len(matching) == 1

    def test_re_register_updates_ip(self, fake_paths) -> None:
        """A second register with a new IP replaces the old entry."""
        fake_hosts, _ = fake_paths
        vm_net.register_ssh_name("co1-single", "192.168.100.50")
        vm_net.register_ssh_name("co1-single", "192.168.100.77")
        hosts = fake_hosts.read_text()
        assert "192.168.100.77" in hosts
        assert "192.168.100.50" not in hosts

    def test_register_writes_ssh_config_block(self, fake_paths) -> None:
        """The ~/.ssh/config block has HostName, User, and key options."""
        _, fake_ssh = fake_paths
        vm_net.register_ssh_name("co1-mds", "10.0.0.5")
        cfg = (fake_ssh / "config").read_text()
        assert "Host co1-mds # qemu-vm:co1-mds" in cfg
        assert "HostName 10.0.0.5" in cfg
        assert "User root" in cfg
        assert "StrictHostKeyChecking no" in cfg
        assert "ConnectTimeout 5" in cfg

    def test_register_ssh_config_0600(self, fake_paths) -> None:
        """The ssh config file mode is locked down to 0600."""
        _, fake_ssh = fake_paths
        vm_net.register_ssh_name("co1-mds", "10.0.0.5")
        mode = (fake_ssh / "config").stat().st_mode & 0o777
        assert mode == 0o600

    def test_unregister_removes_hosts_entry(self, fake_paths) -> None:
        fake_hosts, _ = fake_paths
        vm_net.register_ssh_name("to-rm", "10.0.0.10")
        assert "to-rm" in fake_hosts.read_text()
        vm_net.unregister_ssh_name("to-rm")
        assert "to-rm" not in fake_hosts.read_text()
        # localhost is preserved
        assert "127.0.0.1 localhost" in fake_hosts.read_text()

    def test_unregister_removes_ssh_config_block(self, fake_paths) -> None:
        _, fake_ssh = fake_paths
        vm_net.register_ssh_name("to-rm", "10.0.0.10")
        vm_net.unregister_ssh_name("to-rm")
        cfg = (fake_ssh / "config").read_text()
        assert "to-rm" not in cfg
        assert "10.0.0.10" not in cfg

    def test_unregister_missing_is_noop(self, fake_paths) -> None:
        """Unregistering a VM that was never registered is a no-op."""
        vm_net.unregister_ssh_name("never-existed")  # must not raise

    def test_register_two_vms_keeps_both(self, fake_paths) -> None:
        """Two distinct VMs get two distinct ssh config blocks."""
        _, fake_ssh = fake_paths
        vm_net.register_ssh_name("co1-mds", "10.0.0.1")
        vm_net.register_ssh_name("co1-oss", "10.0.0.2")
        cfg = (fake_ssh / "config").read_text()
        assert "Host co1-mds" in cfg
        assert "Host co1-oss" in cfg
        assert "HostName 10.0.0.1" in cfg
        assert "HostName 10.0.0.2" in cfg


# ── run_ssh command structure ────────────────────────────


class TestRunSsh:
    """run_ssh passes the right sshpass/ssh args and respects timeout."""

    def test_includes_sshpass_and_options(self) -> None:
        captured: list[str] = []

        def fake_run(cmd, timeout=None):
            captured.extend(cmd)
            r = MagicMock()
            r.returncode = 0
            return r

        with patch("ltvm_pkg.vm_net.run", side_effect=fake_run):
            vm_net.run_ssh("10.0.0.5", "ls /tmp", timeout=30)
        assert captured[0] == "sshpass"
        assert "ssh" in captured
        assert "root@10.0.0.5" in captured
        assert "ls /tmp" in captured
        # Options we care about
        joined = " ".join(captured)
        assert "StrictHostKeyChecking=no" in joined
        assert "UserKnownHostsFile=/dev/null" in joined
        assert "ConnectTimeout=5" in joined

    def test_timeout_forwarded(self) -> None:
        seen = {}

        def fake_run(cmd, timeout=None):
            seen["timeout"] = timeout
            r = MagicMock()
            r.returncode = 0
            return r

        with patch("ltvm_pkg.vm_net.run", side_effect=fake_run):
            vm_net.run_ssh("10.0.0.5", "whoami", timeout=42)
        assert seen["timeout"] == 42
