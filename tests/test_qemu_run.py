"""Tests for ltvm_pkg/qemu_run.py: command construction, is_running, kill."""

from __future__ import annotations

import fcntl
import itertools
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import qemu_run
from ltvm_pkg.nic_spec import NicSpec
from ltvm_pkg.qemu_run import NMI_PANIC_ARGS
from ltvm_pkg.vm_state import VMInfo


@pytest.fixture(autouse=True)
def _force_linux_host() -> Any:
    """Existing tests assert Linux /proc behaviour; pin is_macos() off.

    The qemu_run module branches on platform so host memory / is_running
    checks can run on macOS.  These tests drive the Linux code paths;
    without this pin they'd fail when the test runner is Darwin.
    """

    def routed_sudo_run(cmd, *, check=True, quiet=False):
        return qemu_run.run(cmd, check=check, capture_output=quiet)

    with (
        patch("ltvm_pkg.qemu_run.is_macos", return_value=False),
        patch("ltvm_pkg.qemu_run.sudo_run", side_effect=routed_sudo_run),
    ):
        yield


@pytest.fixture
def tmp_vmdir(tmp_path: Path) -> Path:
    """Patch VM_DIR, SOCKETS, OVERLAYS so VMInfo paths land in tmp_path."""
    sockets = tmp_path / "sockets"
    overlays = tmp_path / "overlays"
    sockets.mkdir()
    overlays.mkdir()
    with (
        patch("ltvm_pkg.vm_state.VM_DIR", tmp_path),
        patch("ltvm_pkg.vm_state.SOCKETS", sockets),
        patch("ltvm_pkg.vm_state.OVERLAYS", overlays),
    ):
        yield tmp_path


def _make_vm(
    tmp_vmdir: Path,
    *,
    name: str = "co1-single",
    arch: str = "x86_64",
    mem: int = 2048,
    mdt_disks: int = 0,
    ost_disks: int = 0,
    kernel: str = "",
) -> VMInfo:
    """Build a VMInfo and materialise overlay + data disk files."""
    if not kernel:
        kernel = str(tmp_vmdir / "vmlinuz")
        Path(kernel).write_text("")  # real file so Path(vm.kernel) resolves
    vm = VMInfo(
        name=name,
        ip="192.168.100.50",
        tap=f"tap-{name}",
        mac="AA:FC:00:01:02:03",
        vcpus=2,
        mem=mem,
        mdt_disks=mdt_disks,
        ost_disks=ost_disks,
        kernel=kernel,
        arch=arch,
    )
    vm.overlay_path.write_text("")
    for n in range(1, mdt_disks + ost_disks + 1):
        vm.disk_path(n).write_text("")
    return vm


# ── is_running ───────────────────────────────────────────


class TestIsRunning:
    """is_running reads /proc/<pid>/comm to confirm a live qemu process.

    The probe is deliberately os.kill-free: kill(0) returns EPERM when
    the target pid is owned by another user (commonly root, because
    qemu runs as root), which would make an unprivileged `ltvm list`
    misreport every VM as stopped.  /proc/<pid>/comm is world-readable
    on standard Linux and doubles as the PID-reuse guard.
    """

    def test_zero_pid_is_not_running(self) -> None:
        vm = VMInfo(name="x", ip="1.2.3.4", pid=0)
        assert qemu_run.is_running(vm) is False

    def test_negative_pid_is_not_running(self) -> None:
        vm = VMInfo(name="x", ip="1.2.3.4", pid=-1)
        assert qemu_run.is_running(vm) is False

    def test_live_qemu_pid_is_running(self) -> None:
        """A live PID whose /proc/<pid>/comm starts with qemu-system is running."""
        vm = VMInfo(name="x", ip="1.2.3.4", pid=42)
        with patch("ltvm_pkg.qemu_run.Path") as mock_path:
            mock_path.return_value.read_text.return_value = "qemu-system-x86\n"
            assert qemu_run.is_running(vm) is True

    def test_live_qemu_pid_owned_by_other_user_is_running(self) -> None:
        """Regression: `ltvm list` as non-root against a root-owned qemu.

        Before the fix, is_running() called os.kill(pid, 0) first, got
        EPERM (OSError), returned False, and the caller saw "stopped"
        for a clearly-running VM.  The new implementation doesn't touch
        os.kill at all -- only /proc/<pid>/comm.  This test asserts
        that's still true by failing loudly if anyone re-adds os.kill
        back in without handling EPERM.
        """
        vm = VMInfo(name="x", ip="1.2.3.4", pid=42)
        with (
            patch("ltvm_pkg.qemu_run.Path") as mock_path,
            patch(
                "ltvm_pkg.qemu_run.os.kill",
                side_effect=PermissionError("EPERM"),
            ),
        ):
            mock_path.return_value.read_text.return_value = "qemu-system-x86\n"
            assert qemu_run.is_running(vm) is True

    def test_live_pid_but_not_qemu_is_not_running(self) -> None:
        """PID reuse: the pid is live but the process isn't qemu."""
        vm = VMInfo(name="x", ip="1.2.3.4", pid=42)
        with patch("ltvm_pkg.qemu_run.Path") as mock_path:
            mock_path.return_value.read_text.return_value = "bash\n"
            assert qemu_run.is_running(vm) is False

    def test_dead_pid_is_not_running(self) -> None:
        """A dead PID's /proc/<pid>/comm read raises FileNotFoundError."""
        vm = VMInfo(name="x", ip="1.2.3.4", pid=42)
        with patch("ltvm_pkg.qemu_run.Path") as mock_path:
            mock_path.return_value.read_text.side_effect = FileNotFoundError
            assert qemu_run.is_running(vm) is False

    def test_proc_comm_permission_error_is_not_running(self) -> None:
        """Edge case: hardened /proc mounts (hidepid=2) may make comm
        unreadable to non-root even when the process exists.  Treat that
        as 'can't tell, assume not running' rather than crash."""
        vm = VMInfo(name="x", ip="1.2.3.4", pid=42)
        with patch("ltvm_pkg.qemu_run.Path") as mock_path:
            mock_path.return_value.read_text.side_effect = PermissionError
            assert qemu_run.is_running(vm) is False


# ── launch_qemu: command construction ────────────────────


class _LaunchHarness:
    """Intercept subprocess.run / open during launch_qemu.

    Captures the QEMU invocation (the final `subprocess.run(qemu_args, ...)`
    call) so tests can assert on the arg list and the console log path.
    """

    def __init__(self) -> None:
        self.qemu_args: list[str] | None = None
        self.run_calls: list[list[str]] = []

    @property
    def qemu_argv(self) -> list[str] | None:
        """The QEMU command with any sudo prefix removed.

        launch_qemu elevates the launch when it is not already root --
        QEMU writes its pidfile and QMP socket into a root-owned
        directory -- so the captured argv starts with sudo for an
        unprivileged run.
        """
        if self.qemu_args is None:
            return None
        args = self.qemu_args
        return args[1:] if args and args[0] == "sudo" else args

    def run(self, cmd, **kwargs):
        """Stand-in for qemu_run.run used by launch_qemu."""
        if isinstance(cmd, list):
            self.run_calls.append(cmd)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r

    def subprocess_run(self, qemu_args, **kwargs):
        """The direct subprocess.run call that actually launches QEMU."""
        self.qemu_args = list(qemu_args)
        r = MagicMock()
        r.returncode = 0
        return r


def _run_launch(vm: VMInfo, harness: _LaunchHarness) -> None:
    """Run launch_qemu with all side effects mocked."""
    # Ensure the pidfile write/read round-trip works.
    vm.pid_path.write_text("12345\n")
    with (
        patch("ltvm_pkg.qemu_run.run", side_effect=harness.run),
        patch(
            "ltvm_pkg.qemu_run.subprocess.run",
            side_effect=harness.subprocess_run,
        ),
        patch("ltvm_pkg.qemu_run.is_running", return_value=False),
        # Skip the host-memory budget check so command-construction
        # tests don't depend on the test host's RAM.  See
        # TestMemoryBudgetCheck for direct coverage of the check.
        patch("ltvm_pkg.qemu_run._memory_shortfall", return_value=None),
        patch("ltvm_pkg.qemu_run.time.time", return_value=1700000000),
        # Command construction, not where the guest's cgroup comes from;
        # TestGuestScope covers that.
        patch("ltvm_pkg.qemu_run._guest_scope", return_value=[]),
        patch.object(VMInfo, "update_pid"),
        patch.object(VMInfo, "update_last_boot"),
    ):
        qemu_run.launch_qemu(vm)


class TestLaunchQemuCommand:
    """launch_qemu builds the expected QEMU args for each arch."""

    def test_x86_64_q35_with_virtio_pci(self, tmp_vmdir: Path) -> None:
        """x86_64 uses our custom binary + q35 machine + PCI devices.

        q35 replaced microvm; see qemu_machine_for_arch docstring for
        the rationale (~300 ms boot cost, enables vfio / hotplug /
        older QEMU compat).
        """
        vm = _make_vm(tmp_vmdir, arch="x86_64", mem=2048)
        h = _LaunchHarness()
        _run_launch(vm, h)
        args = h.qemu_args
        assert args is not None
        assert h.qemu_argv[0].endswith("qemu-system-x86_64")
        assert "-machine" in args
        machine = args[args.index("-machine") + 1]
        assert machine.startswith("q35")
        # virtio-*-pci on the PCI bus
        joined = " ".join(args)
        assert "virtio-blk-pci" in joined
        assert "virtio-net-pci" in joined
        assert "virtio-rng-pci" in joined
        assert "virtio-blk-device" not in joined

    def test_aarch64_virt_with_pci(self, tmp_vmdir: Path) -> None:
        """aarch64 uses virt + virtio-*-pci devices."""
        vm = _make_vm(tmp_vmdir, arch="aarch64", mem=4096)
        h = _LaunchHarness()
        _run_launch(vm, h)
        args = h.qemu_args
        assert args is not None
        assert h.qemu_argv[0].endswith("qemu-system-aarch64")
        machine = args[args.index("-machine") + 1]
        assert machine.startswith("virt")
        joined = " ".join(args)
        assert "virtio-blk-pci" in joined
        assert "virtio-net-pci" in joined
        assert "virtio-rng-pci" in joined
        assert "virtio-blk-device" not in joined

    def test_boot_args_contains_ip_name_gateway(self, tmp_vmdir: Path) -> None:
        """The -append arg wires IP/gateway/name into the kernel cmdline."""
        vm = _make_vm(tmp_vmdir)
        vm.ip = "192.168.100.77"
        h = _LaunchHarness()
        _run_launch(vm, h)
        args = h.qemu_args
        append = args[args.index("-append") + 1]
        assert "fc_ip=192.168.100.77" in append
        assert "fc_name=co1-single" in append
        assert "fc_gw=" in append
        assert "root=/dev/vda" in append

    def test_crashkernel_scales_with_memory(self, tmp_vmdir: Path) -> None:
        """Low-memory VMs reserve 256M; normal VMs reserve 512M."""
        small = _make_vm(tmp_vmdir, name="small", mem=1024)
        h1 = _LaunchHarness()
        _run_launch(small, h1)
        append_small = h1.qemu_args[h1.qemu_args.index("-append") + 1]
        assert "crashkernel=256M" in append_small

        big = _make_vm(tmp_vmdir, name="big", mem=4096)
        h2 = _LaunchHarness()
        _run_launch(big, h2)
        append_big = h2.qemu_args[h2.qemu_args.index("-append") + 1]
        assert "crashkernel=512M" in append_big

    def test_console_ttyAMA0_on_aarch64(self, tmp_vmdir: Path) -> None:
        """aarch64 uses ttyAMA0; x86 uses ttyS0."""
        arm = _make_vm(tmp_vmdir, name="arm", arch="aarch64")
        h = _LaunchHarness()
        _run_launch(arm, h)
        append = h.qemu_args[h.qemu_args.index("-append") + 1]
        assert "console=ttyAMA0" in append

        x86 = _make_vm(tmp_vmdir, name="x86", arch="x86_64")
        h2 = _LaunchHarness()
        _run_launch(x86, h2)
        append2 = h2.qemu_args[h2.qemu_args.index("-append") + 1]
        assert "console=ttyS0" in append2

    def test_single_nic_baseline_unchanged(self, tmp_vmdir: Path) -> None:
        """A VM with no --nic produces a single virtio-net-pci device,
        exactly as before the feature.  Regression guard against
        inadvertently changing the default single-NIC wiring.
        """
        vm = _make_vm(tmp_vmdir)  # vm.nics == []
        h = _LaunchHarness()
        _run_launch(vm, h)
        joined = " ".join(h.qemu_args)
        # Exactly one netdev and one virtio-net-pci -- the mgmt NIC.
        assert joined.count("-netdev") == 1
        assert joined.count("virtio-net-pci") == 1
        # No fc_nics= on the cmdline when no extras are requested.
        append = h.qemu_args[h.qemu_args.index("-append") + 1]
        assert "fc_nics=" not in append

    def test_extra_nics_emit_one_device_each(self, tmp_vmdir: Path) -> None:
        """Each --nic tcp adds one tap netdev + one virtio-net-pci.

        IDs are net1, net2, ... and correspond to eth1, eth2 in the
        guest.  MAC and ifname are distinct per NIC.
        """
        vm = _make_vm(tmp_vmdir, name="co1-two")
        vm.nics = [NicSpec.parse("tcp"), NicSpec.parse("tcp")]
        h = _LaunchHarness()
        _run_launch(vm, h)
        joined = " ".join(h.qemu_args)
        # 1 mgmt + 2 extras = 3 netdevs and 3 virtio-net-pci devices.
        assert joined.count("-netdev") == 3
        assert joined.count("virtio-net-pci") == 3
        # net1 and net2 appear with matching id/netdev pairs.
        assert "id=net1" in joined
        assert "id=net2" in joined
        assert "netdev=net1" in joined
        assert "netdev=net2" in joined

    def test_extra_nic_taps_created(self, tmp_vmdir: Path) -> None:
        """Pre-launch: each extra NIC gets an ip-tuntap-add + master + up."""
        vm = _make_vm(tmp_vmdir, name="co1-two")
        vm.nics = [NicSpec.parse("tcp"), NicSpec.parse("tcp")]
        h = _LaunchHarness()
        _run_launch(vm, h)
        # Collect the set of TAP names that were created
        added = set()
        for call in h.run_calls:
            if len(call) >= 5 and call[1:4] == ["tuntap", "add", "dev"]:
                added.add(call[4])
        # mgmt + 2 extras
        assert len(added) == 3

    def test_fc_nics_cmdline_single_extra(self, tmp_vmdir: Path) -> None:
        """--nic tcp threads through to fc_nics=tcp on the kernel cmdline."""
        vm = _make_vm(tmp_vmdir)
        vm.nics = [NicSpec.parse("tcp")]
        h = _LaunchHarness()
        _run_launch(vm, h)
        append = h.qemu_args[h.qemu_args.index("-append") + 1]
        assert "fc_nics=tcp" in append

    def test_fc_nics_cmdline_two_extras(self, tmp_vmdir: Path) -> None:
        """Two --nic tcp yields fc_nics=tcp,tcp in order."""
        vm = _make_vm(tmp_vmdir)
        vm.nics = [NicSpec.parse("tcp"), NicSpec.parse("tcp")]
        h = _LaunchHarness()
        _run_launch(vm, h)
        append = h.qemu_args[h.qemu_args.index("-append") + 1]
        assert "fc_nics=tcp,tcp" in append

    def test_kernel_args_come_last(self, tmp_vmdir: Path) -> None:
        """After ltvm's own, so crashkernel= and the like take the
        user's value."""
        vm = _make_vm(tmp_vmdir)
        vm.nics = [NicSpec.parse("tcp")]
        vm.kernel_args = "crashkernel=1G kasan_multi_shot"
        h = _LaunchHarness()
        _run_launch(vm, h)
        append = h.qemu_args[h.qemu_args.index("-append") + 1]
        assert append.endswith(
            f" fc_nics=tcp{NMI_PANIC_ARGS} crashkernel=1G kasan_multi_shot"
        )

    def test_no_kernel_args_leaves_cmdline_as_before(
        self, tmp_vmdir: Path
    ) -> None:
        vm = _make_vm(tmp_vmdir)
        h = _LaunchHarness()
        _run_launch(vm, h)
        append = h.qemu_args[h.qemu_args.index("-append") + 1]
        assert append.endswith(f" fc_name=co1-single{NMI_PANIC_ARGS}")

    def test_x86_boots_with_nmi_panic(self, tmp_vmdir: Path) -> None:
        """An NMI from the host must panic a guest that no longer
        answers ssh, so the knobs are set from boot."""
        vm = _make_vm(tmp_vmdir)
        h = _LaunchHarness()
        _run_launch(vm, h)
        append = h.qemu_args[h.qemu_args.index("-append") + 1]
        assert " unknown_nmi_panic " in append
        assert "sysctl.kernel.panic_on_unrecovered_nmi=1" in append

    def test_fc_nic_ips_carry_no_prefix(self, tmp_vmdir: Path) -> None:
        """fc_nic_ips= stays bare dotted quads.  An image older than
        fc_nic_prefixes appends its own '/24'; a '/24' already in the
        value would make its `ip addr add` fail silently."""
        vm = _make_vm(tmp_vmdir)
        vm.nics = [NicSpec.parse("softroce"), NicSpec.parse("softroce")]
        vm.nic_ips = ["172.16.100.33", "172.16.100.34"]
        h = _LaunchHarness()
        _run_launch(vm, h)
        append = h.qemu_args[h.qemu_args.index("-append") + 1]
        assert "fc_nic_ips=172.16.100.33,172.16.100.34" in append
        assert "/24," not in append

    def test_fc_nic_prefixes_parallel_to_fc_nic_ips(
        self, tmp_vmdir: Path
    ) -> None:
        """The prefix length travels in its own index-matched array."""
        vm = _make_vm(tmp_vmdir)
        vm.nics = [NicSpec.parse("tcp"), NicSpec.parse("tcp")]
        vm.nic_ips = ["172.16.100.33", "172.16.100.34"]
        h = _LaunchHarness()
        _run_launch(vm, h)
        append = h.qemu_args[h.qemu_args.index("-append") + 1]
        assert "fc_nic_prefixes=24,24" in append

    def test_fc_nic_ip6s_carries_the_full_addresses(
        self, tmp_vmdir: Path
    ) -> None:
        """The extras' IPv6 addresses ride their own parallel array,
        at their full 39-character width."""
        vm = _make_vm(tmp_vmdir)
        vm.nics = [NicSpec.parse("softroce"), NicSpec.parse("softroce")]
        vm.nic_ips = ["172.16.100.33", "172.16.100.34"]
        vm.nic_ip6s = [
            "fd17:2016:1000:f100:f172:f016:f100:f033",
            "fd17:2016:1000:f100:f172:f016:f100:f034",
        ]
        h = _LaunchHarness()
        _run_launch(vm, h)
        append = h.qemu_args[h.qemu_args.index("-append") + 1]
        assert (
            "fc_nic_ip6s=fd17:2016:1000:f100:f172:f016:f100:f033,"
            "fd17:2016:1000:f100:f172:f016:f100:f034" in append
        )
        assert "fc_nic_ip6_prefixes=64,64" in append

    def test_no_ip6_arrays_without_nic_ip6s(self, tmp_vmdir: Path) -> None:
        """A VM created before the extras carried IPv6 gets neither
        parameter, so its cmdline is unchanged."""
        vm = _make_vm(tmp_vmdir)
        vm.nics = [NicSpec.parse("tcp")]
        vm.nic_ips = ["172.16.100.33"]
        vm.nic_ip6s = []
        h = _LaunchHarness()
        _run_launch(vm, h)
        append = h.qemu_args[h.qemu_args.index("-append") + 1]
        assert "fc_nic_ip6s" not in append
        assert "fc_nic_ip6_prefixes" not in append

    def test_cmdline_stays_inside_command_line_size(
        self, tmp_vmdir: Path
    ) -> None:
        """Three extra NICs add about 40 characters each; the whole
        cmdline must stay well inside COMMAND_LINE_SIZE (2048)."""
        vm = _make_vm(tmp_vmdir)
        vm.nics = [
            NicSpec.parse("softroce"),
            NicSpec.parse("softroce"),
            NicSpec.parse("softroce"),
        ]
        vm.nic_ips = [
            "172.16.100.233",
            "172.16.100.234",
            "172.16.100.235",
        ]
        vm.nic_ip6s = [
            "fd17:2016:1000:f100:f172:f016:f100:f233",
            "fd17:2016:1000:f100:f172:f016:f100:f234",
            "fd17:2016:1000:f100:f172:f016:f100:f235",
        ]
        h = _LaunchHarness()
        _run_launch(vm, h)
        append = h.qemu_args[h.qemu_args.index("-append") + 1]
        assert len(append) < 2048

    def test_no_prefixes_without_nic_ips(self, tmp_vmdir: Path) -> None:
        """An old .info with NICS but no NIC_IPS emits neither array,
        so rc.local just brings the interfaces up as it always did."""
        vm = _make_vm(tmp_vmdir)
        vm.nics = [NicSpec.parse("tcp")]
        vm.nic_ips = []
        h = _LaunchHarness()
        _run_launch(vm, h)
        append = h.qemu_args[h.qemu_args.index("-append") + 1]
        assert "fc_nics=tcp" in append
        assert "fc_nic_ips=" not in append
        assert "fc_nic_prefixes=" not in append

    def test_no_nic_arrays_on_single_nic_cmdline(self, tmp_vmdir: Path) -> None:
        """Regression bar: a VM with no --nic gets the same cmdline as
        before this change."""
        vm = _make_vm(tmp_vmdir)
        h = _LaunchHarness()
        _run_launch(vm, h)
        append = h.qemu_args[h.qemu_args.index("-append") + 1]
        assert "fc_nic_ips=" not in append
        assert "fc_nic_prefixes=" not in append

    def test_data_disks_emit_device_and_drive(self, tmp_vmdir: Path) -> None:
        """mdt+ost disks produce paired -device/-drive args, 1-indexed."""
        vm = _make_vm(tmp_vmdir, mdt_disks=1, ost_disks=2)
        h = _LaunchHarness()
        _run_launch(vm, h)
        joined = " ".join(h.qemu_args)
        for n in (1, 2, 3):
            assert f"drive=disk{n}" in joined
            assert f"id=disk{n}" in joined
        # Exactly 3 data disks plus the rootfs drive
        drive_count = joined.count("-drive")
        assert drive_count == 4

    def test_missing_data_disk_errors(self, tmp_vmdir: Path) -> None:
        """launch_qemu die()s (SystemExit) if a backing disk is missing."""
        vm = _make_vm(tmp_vmdir, mdt_disks=1)
        vm.disk_path(1).unlink()
        h = _LaunchHarness()
        with (
            patch("ltvm_pkg.qemu_run.run", side_effect=h.run),
            patch(
                "ltvm_pkg.qemu_run.subprocess.run", side_effect=h.subprocess_run
            ),
            patch("ltvm_pkg.qemu_run.is_running", return_value=False),
            patch("ltvm_pkg.qemu_run._memory_shortfall", return_value=None),
        ):
            with pytest.raises(SystemExit):
                qemu_run.launch_qemu(vm)

    def test_missing_overlay_errors(self, tmp_vmdir: Path) -> None:
        """No overlay -> die() before spawning qemu."""
        vm = _make_vm(tmp_vmdir)
        vm.overlay_path.unlink()
        with patch("ltvm_pkg.qemu_run.is_running", return_value=False):
            with pytest.raises(SystemExit):
                qemu_run.launch_qemu(vm)

    def test_missing_kernel_errors(self, tmp_vmdir: Path) -> None:
        """Empty vm.kernel -> die() with a helpful message."""
        vm = _make_vm(tmp_vmdir)
        vm.kernel = ""
        h = _LaunchHarness()
        with (
            patch("ltvm_pkg.qemu_run.run", side_effect=h.run),
            patch(
                "ltvm_pkg.qemu_run.subprocess.run", side_effect=h.subprocess_run
            ),
            patch("ltvm_pkg.qemu_run.is_running", return_value=False),
            patch("ltvm_pkg.qemu_run._memory_shortfall", return_value=None),
        ):
            with pytest.raises(SystemExit):
                qemu_run.launch_qemu(vm)

    def test_running_vm_short_circuits(self, tmp_vmdir: Path) -> None:
        """launch_qemu on an already-running VM is a no-op (no qemu spawn)."""
        vm = _make_vm(tmp_vmdir)
        h = _LaunchHarness()
        with (
            patch("ltvm_pkg.qemu_run.run", side_effect=h.run),
            patch(
                "ltvm_pkg.qemu_run.subprocess.run", side_effect=h.subprocess_run
            ),
            patch("ltvm_pkg.qemu_run.is_running", return_value=True),
        ):
            qemu_run.launch_qemu(vm)
        assert h.qemu_args is None

    def test_tap_setup_sequence(self, tmp_vmdir: Path) -> None:
        """Before qemu launch: del-old-tap, flush-arp, add-tap, master, up."""
        vm = _make_vm(tmp_vmdir)
        h = _LaunchHarness()
        _run_launch(vm, h)
        # Each element is the full argv; look for the expected verbs in order
        verbs = [c[1:4] for c in h.run_calls if len(c) >= 4]
        # del + neigh flush are the first two
        assert ["link", "del", vm.tap] in verbs
        # tuntap add + link set master + link set up all present
        flat = [tuple(c) for c in h.run_calls]
        assert any("tuntap" in c and "add" in c for c in flat)
        assert any("master" in c for c in flat)

    def test_tap_rollback_on_failure(self, tmp_vmdir: Path) -> None:
        """If launch fails after TAP creation, the TAP is torn back down."""
        vm = _make_vm(tmp_vmdir)
        h = _LaunchHarness()

        # Simulate qemu-system-x86_64 failing (returncode != 0 -> die()).
        def bad_qemu(args, **kwargs):
            r = MagicMock()
            r.returncode = 1
            return r

        with (
            patch("ltvm_pkg.qemu_run.run", side_effect=h.run),
            patch("ltvm_pkg.qemu_run.subprocess.run", side_effect=bad_qemu),
            patch("ltvm_pkg.qemu_run.is_running", return_value=False),
            patch("ltvm_pkg.qemu_run._memory_shortfall", return_value=None),
        ):
            with pytest.raises(SystemExit):
                qemu_run.launch_qemu(vm)
        # Two "link del <tap>" calls: initial stale cleanup + rollback
        del_calls = [
            c
            for c in h.run_calls
            if len(c) >= 3 and c[:3] == ["ip", "link", "del"] and c[3] == vm.tap
        ]
        assert len(del_calls) >= 2, (
            f"expected rollback to tear down TAP, saw: {h.run_calls}"
        )

    def test_pidfile_appears_late_no_rollback(self, tmp_vmdir: Path) -> None:
        """-daemonize may return before child writes pidfile; we poll briefly."""
        vm = _make_vm(tmp_vmdir)
        # Pidfile is *not* present when subprocess.run returns; only after
        # one or two poll iterations does the daemonized child create it.
        vm.pid_path.unlink(missing_ok=True)
        h = _LaunchHarness()

        sleep_calls = {"n": 0}

        def fake_sleep(_):
            sleep_calls["n"] += 1
            if sleep_calls["n"] == 2:
                vm.pid_path.write_text("4242\n")

        with (
            patch("ltvm_pkg.qemu_run.run", side_effect=h.run),
            patch(
                "ltvm_pkg.qemu_run.subprocess.run", side_effect=h.subprocess_run
            ),
            patch("ltvm_pkg.qemu_run.is_running", return_value=False),
            patch("ltvm_pkg.qemu_run._memory_shortfall", return_value=None),
            patch("ltvm_pkg.qemu_run.time.sleep", side_effect=fake_sleep),
            patch("ltvm_pkg.qemu_run.time.time", return_value=1700000000),
            patch.object(VMInfo, "update_pid"),
            patch.object(VMInfo, "update_last_boot"),
        ):
            qemu_run.launch_qemu(vm)
        assert sleep_calls["n"] >= 1


class TestForceTcg:
    """LTVM_FORCE_TCG=1 selects software emulation and a concrete CPU.

    Some guest kernels need a translation granule the host hypervisor
    cannot provide -- Apple Silicon has no 64 KiB granule, so a
    CONFIG_ARM64_64K_PAGES guest dies in early MMU setup under HVF with
    no console output at all.  Falling back to TCG is the only way to
    boot such a guest.
    """

    def test_machine_uses_tcg_when_forced(self, monkeypatch: Any) -> None:
        """qemu_machine_for_arch honours the override on both arches."""
        from ltvm_pkg.vm_state import qemu_machine_for_arch

        monkeypatch.setenv("LTVM_FORCE_TCG", "1")
        assert "accel=tcg" in qemu_machine_for_arch("aarch64")
        assert "accel=tcg" in qemu_machine_for_arch("x86_64")

    def test_machine_native_accel_without_override(
        self, monkeypatch: Any
    ) -> None:
        """Unset (or not "1") leaves the existing behaviour alone."""
        from ltvm_pkg.vm_state import qemu_machine_for_arch

        monkeypatch.delenv("LTVM_FORCE_TCG", raising=False)
        machine = qemu_machine_for_arch("aarch64")
        assert machine.startswith("virt,")
        assert "gic-version=max" in machine

    def test_its_off_under_nested_kvm_on_apple(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """An Apple CPU on a Linux KVM host gets its=off; others keep the ITS.

        Nested KVM under Apple Virtualization loses MSIs routed through
        the ITS and the guest hangs in idle after init.
        """
        import platform

        from ltvm_pkg import vm_state

        monkeypatch.delenv("LTVM_FORCE_TCG", raising=False)
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(platform, "machine", lambda: "aarch64")
        cpuinfo = tmp_path / "cpuinfo"
        real = vm_state._nested_kvm_on_apple
        monkeypatch.setattr(
            vm_state, "_nested_kvm_on_apple", lambda: real(cpuinfo)
        )

        cpuinfo.write_text("processor\t: 0\nCPU implementer\t: 0x61\n")
        assert vm_state.qemu_machine_for_arch("aarch64") == (
            "virt,accel=kvm,gic-version=max,its=off"
        )
        cpuinfo.write_text("processor\t: 0\nCPU implementer\t: 0x41\n")
        assert vm_state.qemu_machine_for_arch("aarch64") == (
            "virt,accel=kvm,gic-version=max"
        )
        # Under TCG there is no nested GIC to work around.
        cpuinfo.write_text("CPU implementer\t: 0x61\n")
        monkeypatch.setenv("LTVM_FORCE_TCG", "1")
        assert "its=off" not in vm_state.qemu_machine_for_arch("aarch64")

    def test_cpu_model_is_concrete_when_forced(
        self, tmp_vmdir: Path, monkeypatch: Any
    ) -> None:
        """ "-cpu host" is invalid without a hardware accelerator.

        Under TCG the model must be a real one or QEMU refuses to start,
        so the arch-matches-host shortcut has to be skipped.
        """
        monkeypatch.setenv("LTVM_FORCE_TCG", "1")
        vm = _make_vm(tmp_vmdir, arch="aarch64", mem=2048)
        h = _LaunchHarness()
        # qemu_run does "import platform as _platform" inside the
        # function, so patch the module attribute itself.
        with patch("platform.machine", return_value="arm64"):
            _run_launch(vm, h)
        args = h.qemu_args
        assert args is not None
        assert args[args.index("-cpu") + 1] != "host"


class TestLaunchQemuMacos:
    """On macOS, launch_qemu replaces -netdev tap with -netdev stream pointed
    at the socket_vmnet Unix socket.  No host TAPs are created or torn down --
    socket_vmnet multiplexes every guest onto a single shared daemon.
    """

    _SOCK = "/opt/homebrew/var/run/socket_vmnet"

    def _launch_macos(self, vm: VMInfo, h: _LaunchHarness) -> None:
        vm.pid_path.write_text("12345\n")
        with (
            patch("ltvm_pkg.qemu_run.is_macos", return_value=True),
            patch(
                "ltvm_pkg.qemu_run.socket_vmnet_socket_path",
                return_value=Path(self._SOCK),
            ),
            patch(
                "ltvm_pkg.host_setup.ensure_socket_vmnet_running",
                return_value=None,
            ),
            patch("ltvm_pkg.qemu_run.run", side_effect=h.run),
            patch(
                "ltvm_pkg.qemu_run.subprocess.run",
                side_effect=h.subprocess_run,
            ),
            patch("ltvm_pkg.qemu_run.is_running", return_value=False),
            patch("ltvm_pkg.qemu_run._memory_shortfall", return_value=None),
            patch("ltvm_pkg.qemu_run.time.time", return_value=1700000000),
            patch.object(VMInfo, "update_pid"),
            patch.object(VMInfo, "update_last_boot"),
        ):
            qemu_run.launch_qemu(vm)

    def test_mgmt_nic_uses_stream_unix(self, tmp_vmdir: Path) -> None:
        """-netdev stream,addr.type=unix,addr.path=<socket>,server=off."""
        vm = _make_vm(tmp_vmdir)
        h = _LaunchHarness()
        self._launch_macos(vm, h)
        joined = " ".join(h.qemu_args or [])
        assert "tap,id=net0" not in joined
        assert (
            f"stream,id=net0,addr.type=unix,addr.path={self._SOCK},server=off"
            in joined
        )
        assert f"mac={vm.mac}" in joined

    def test_no_ip_commands_run(self, tmp_vmdir: Path) -> None:
        """macOS has no TAP/bridge; launch must not shell out to `ip`."""
        vm = _make_vm(tmp_vmdir, name="co1-two")
        vm.nics = [NicSpec.parse("tcp"), NicSpec.parse("tcp")]
        h = _LaunchHarness()
        self._launch_macos(vm, h)
        ip_calls = [c for c in h.run_calls if c and c[0] == "ip"]
        assert ip_calls == [], f"unexpected ip commands on macOS: {ip_calls}"

    def test_extra_nics_share_one_socket(self, tmp_vmdir: Path) -> None:
        """Each extra NIC gets its own -netdev stream entry, all pointed at
        the same socket_vmnet Unix socket (shared L2 fabric).
        """
        vm = _make_vm(tmp_vmdir, name="co1-two")
        vm.nics = [NicSpec.parse("tcp"), NicSpec.parse("tcp")]
        h = _LaunchHarness()
        self._launch_macos(vm, h)
        joined = " ".join(h.qemu_args or [])
        assert joined.count("-netdev") == 3
        assert joined.count(f"addr.path={self._SOCK}") == 3
        assert "id=net0" in joined
        assert "id=net1" in joined
        assert "id=net2" in joined
        assert "virtio-net-pci" in joined

    def test_kill_qemu_no_ip_commands(self, tmp_vmdir: Path) -> None:
        """kill_qemu on macOS must skip `ip link del` / `ip neigh flush`."""
        vm = _make_vm(tmp_vmdir)
        vm.pid = 0  # skip the signal path; exercise teardown only
        h = _LaunchHarness()
        with (
            patch("ltvm_pkg.qemu_run.is_macos", return_value=True),
            patch("ltvm_pkg.qemu_run.run", side_effect=h.run),
            patch.object(VMInfo, "update_pid"),
        ):
            qemu_run.kill_qemu(vm)
        ip_calls = [c for c in h.run_calls if c and c[0] == "ip"]
        assert ip_calls == [], f"unexpected ip commands on macOS: {ip_calls}"


class TestIsRunningMacos:
    """macOS has no /proc, so is_running asks ps for the command line."""

    def _is_running(self, vm: VMInfo, stdout: str, rc: int = 0) -> bool:
        done = subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")
        with (
            patch("ltvm_pkg.qemu_run.is_macos", return_value=True),
            patch("ltvm_pkg.qemu_run.subprocess.run", return_value=done) as run,
        ):
            result = qemu_run.is_running(vm)
        assert "comm=" not in run.call_args.args[0]
        return result

    def test_long_qemu_path_is_running(self, tmp_vmdir: Path) -> None:
        """ps truncates comm= to 16 chars ("/opt/qemu/bin/qe"), which read
        as "not QEMU" and listed every running VM as stopped."""
        vm = _make_vm(tmp_vmdir)
        vm.pid = 46513
        out = (
            "/opt/qemu/bin/qemu-system-aarch64 -name "
            f"{vm.name} -machine virt,accel=hvf\n"
        )
        assert self._is_running(vm, out)

    def test_other_vm_is_not_running(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        vm.pid = 46513
        out = "/opt/qemu/bin/qemu-system-aarch64 -name co9-other\n"
        assert not self._is_running(vm, out)

    def test_reused_pid_is_not_running(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        vm.pid = 46513
        assert not self._is_running(vm, "/usr/bin/python3 foo.py\n")

    def test_dead_pid_is_not_running(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        vm.pid = 46513
        assert not self._is_running(vm, "", rc=1)


# ── memory budget check ──────────────────────────────────


class TestCgroupMemoryLimit:
    def _tree(
        self, tmp_path: Path, limits: dict[str, str]
    ) -> tuple[Path, Path]:
        root = tmp_path / "cgroup"
        for relative, value in limits.items():
            (root / relative).mkdir(parents=True, exist_ok=True)
            (root / relative / "memory.max").write_text(value + "\n")
        proc = tmp_path / "self-cgroup"
        proc.write_text("0::/user.slice/run.scope\n")
        return proc, root

    def test_the_tightest_of_the_cgroup_and_its_parents(
        self, tmp_path: Path
    ) -> None:
        proc, root = self._tree(
            tmp_path,
            {"user.slice": str(8 << 30), "user.slice/run.scope": "max"},
        )
        assert qemu_run._cgroup_memory_limit_mb(proc, root) == 8192

    def test_no_limit_anywhere_is_none(self, tmp_path: Path) -> None:
        proc, root = self._tree(tmp_path, {"user.slice/run.scope": "max"})
        assert qemu_run._cgroup_memory_limit_mb(proc, root) == 0

    def test_a_container_sees_its_own_root(self, tmp_path: Path) -> None:
        root = tmp_path / "cgroup"
        root.mkdir()
        (root / "memory.max").write_text(f"{6 << 30}\n")
        proc = tmp_path / "self-cgroup"
        proc.write_text("0::/\n")
        assert qemu_run._cgroup_memory_limit_mb(proc, root) == 6144

    def test_unreadable_is_none(self, tmp_path: Path) -> None:
        assert qemu_run._cgroup_memory_limit_mb(tmp_path / "no", tmp_path) == 0

    def test_named_cgroup_instead_of_own(self, tmp_path: Path) -> None:
        """relative= reads that cgroup and its parents, not /proc/self's."""
        root = tmp_path / "cg"
        own = root / "app.slice" / "run-x.scope"
        own.mkdir(parents=True)
        (own / "memory.max").write_text(str(10 * 1024**3))
        other = root / "user.slice" / "app.slice"
        other.mkdir(parents=True)
        (other / "memory.max").write_text("max")
        proc = tmp_path / "cgroup"
        proc.write_text("0::/app.slice/run-x.scope\n")
        assert qemu_run._cgroup_memory_limit_mb(proc, root) == 10240
        assert (
            qemu_run._cgroup_memory_limit_mb(
                proc, root, relative="/user.slice/app.slice"
            )
            == 0
        )


class TestMemoryBudgetCheck:
    """_memory_shortfall says when a launch would exceed the host."""

    @pytest.fixture(autouse=True)
    def _no_cgroup_limit(self) -> Iterator[None]:
        with (
            patch("ltvm_pkg.qemu_run._cgroup_memory_limit_mb", return_value=0),
            patch("ltvm_pkg.qemu_run._guest_cgroup", return_value=None),
        ):
            yield

    def test_a_guest_in_its_own_scope_is_bounded_by_that_slice(
        self, tmp_vmdir: Path
    ) -> None:
        """A caller in a capped transient scope does not cap a guest that
        systemd-run moves out of it: the limit read is the app.slice's."""
        vm = _make_vm(tmp_vmdir, mem=2048)
        slice_path = "/user.slice/user-1000.slice/user@1000.service/app.slice"
        with (
            patch("ltvm_pkg.qemu_run._read_meminfo_mb", return_value=16384),
            patch("ltvm_pkg.qemu_run._guest_cgroup", return_value=slice_path),
            patch(
                "ltvm_pkg.qemu_run._cgroup_memory_limit_mb", return_value=0
            ) as limit,
            patch.object(VMInfo, "all_names", return_value=[]),
        ):
            assert qemu_run._memory_shortfall(vm) is None
        limit.assert_called_once_with(relative=slice_path)

    def test_a_cgroup_limit_below_memtotal_is_the_budget(
        self, tmp_vmdir: Path
    ) -> None:
        """In a container with half the host's memory, a VM that fits the
        host but not the container is refused, and the limit is named."""
        vm = _make_vm(tmp_vmdir, name="big", mem=15360)
        with (
            patch("ltvm_pkg.qemu_run._read_meminfo_mb", return_value=32768),
            patch(
                "ltvm_pkg.qemu_run._cgroup_memory_limit_mb", return_value=16384
            ),
            patch.object(VMInfo, "all_names", return_value=[]),
        ):
            shortfall = qemu_run._memory_shortfall(vm)
        assert shortfall is not None
        assert "cgroup memory.max 16384 MiB" in shortfall.message

    def test_a_cgroup_limit_above_memtotal_changes_nothing(
        self, tmp_vmdir: Path
    ) -> None:
        vm = _make_vm(tmp_vmdir, mem=2048)
        with (
            patch("ltvm_pkg.qemu_run._read_meminfo_mb", return_value=8192),
            patch(
                "ltvm_pkg.qemu_run._cgroup_memory_limit_mb", return_value=65536
            ),
            patch.object(VMInfo, "all_names", return_value=[]),
        ):
            assert qemu_run._memory_shortfall(vm) is None

    def test_passes_when_budget_has_room(self, tmp_vmdir: Path) -> None:
        """Plenty of host RAM, no other VMs -> check returns silently."""
        vm = _make_vm(tmp_vmdir, mem=2048)
        with (
            patch("ltvm_pkg.qemu_run._read_meminfo_mb", return_value=16384),
            patch.object(VMInfo, "all_names", return_value=[]),
        ):
            assert qemu_run._memory_shortfall(vm) is None

    def test_skips_when_meminfo_unreadable(self, tmp_vmdir: Path) -> None:
        """If MemTotal can't be read (e.g. non-Linux test host), don't block."""
        vm = _make_vm(tmp_vmdir, mem=999999)
        with patch("ltvm_pkg.qemu_run._read_meminfo_mb", return_value=0):
            assert qemu_run._memory_shortfall(vm) is None

    def test_refuses_when_request_exceeds_empty_host(
        self, tmp_vmdir: Path
    ) -> None:
        """No other VMs running, but the request alone busts the budget."""
        vm = _make_vm(tmp_vmdir, name="big", mem=8192)
        with (
            # 8 GiB host -> reserve = max(1024, 819) = 1024 -> budget 7168
            patch("ltvm_pkg.qemu_run._read_meminfo_mb", return_value=8192),
            patch.object(VMInfo, "all_names", return_value=[]),
        ):
            shortfall = qemu_run._memory_shortfall(vm)
        assert shortfall is not None
        # No amount of waiting makes room for it.
        assert not shortfall.fits_when_idle
        err = shortfall.message
        assert "not enough host memory" in err
        assert "big" in err
        # No running VMs -> guidance is the smaller-mem hint, not the stop list
        assert "no other VMs are running" in err
        assert "ltvm stop" not in err

    def test_refuses_and_lists_running_vms(self, tmp_vmdir: Path) -> None:
        """Other VMs are eating the budget; the error lists them largest-first."""
        # Build three sibling VMInfo files on disk so all_names() finds them.
        sib_a = _make_vm(tmp_vmdir, name="co1-mds", mem=4096)
        sib_a.save()
        sib_b = _make_vm(tmp_vmdir, name="co1-oss1", mem=2048)
        sib_b.save()
        sib_c = _make_vm(tmp_vmdir, name="co1-oss2", mem=2048)
        sib_c.save()

        new_vm = _make_vm(tmp_vmdir, name="co1-new", mem=4096)

        running_set = {"co1-mds", "co1-oss1", "co1-oss2"}

        def fake_is_running(other: VMInfo) -> bool:
            return other.name in running_set

        with (
            # 10 GiB host -> reserve max(1024, 1024) = 1024 -> budget 9216
            # Committed 4096+2048+2048 = 8192; new wants 4096 -> 12288 > 9216
            patch("ltvm_pkg.qemu_run._read_meminfo_mb", return_value=10240),
            patch("ltvm_pkg.qemu_run.is_running", side_effect=fake_is_running),
        ):
            shortfall = qemu_run._memory_shortfall(new_vm)

        assert shortfall is not None
        # Stopping the running VMs would make room, so it is worth waiting.
        assert shortfall.fits_when_idle
        err = shortfall.message
        assert "not enough host memory" in err
        assert "co1-new" in err
        # Running VMs are listed
        for name in ("co1-mds", "co1-oss1", "co1-oss2"):
            assert name in err
        # Suggests stopping VMs
        assert "ltvm stop" in err
        # Largest-first ordering: mds (4096) appears before oss1 (2048)
        assert err.index("co1-mds") < err.index("co1-oss1")

    def test_excludes_self_from_committed_total(self, tmp_vmdir: Path) -> None:
        """A re-launch of an existing VM doesn't double-count its own RAM.

        ``launch_qemu`` short-circuits when the VM is already running, but
        the budget check runs for VMs whose .info file exists with a stale
        PID -- in that case the VM's own ``mem`` must not be added on top
        of the requested ``mem``.
        """
        vm = _make_vm(tmp_vmdir, name="co1-self", mem=4096)
        vm.save()
        # Pretend the saved sibling IS running, but it's the same VM we're
        # launching -- the check should skip it.
        with (
            patch("ltvm_pkg.qemu_run._read_meminfo_mb", return_value=8192),
            patch("ltvm_pkg.qemu_run.is_running", return_value=True),
        ):
            assert qemu_run._memory_shortfall(vm) is None

    @staticmethod
    def _site(
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
        text: str,
    ) -> None:
        site = tmp_path_factory.mktemp("site") / "ltvm.conf"
        site.write_text(text)
        monkeypatch.setenv("LTVM_SITE_CONFIG", str(site))
        monkeypatch.setattr(qemu_run, "_site_config_warned", set())

    @staticmethod
    def _shortfall_with_running(
        tmp_vmdir: Path, running_mb: list[int], new_mb: int
    ) -> qemu_run._Shortfall | None:
        """Check a new VM on a 10 GiB host (budget 9216 MiB)."""
        for i, mb in enumerate(running_mb):
            _make_vm(tmp_vmdir, name=f"co1-run{i}", mem=mb).save()
        new_vm = _make_vm(tmp_vmdir, name="co1-new", mem=new_mb)
        with (
            patch("ltvm_pkg.qemu_run._read_meminfo_mb", return_value=10240),
            patch(
                "ltvm_pkg.qemu_run.is_running",
                side_effect=lambda o: o.name.startswith("co1-run"),
            ),
        ):
            return qemu_run._memory_shortfall(new_vm)

    def test_overcommit_admits_past_the_budget(
        self,
        tmp_vmdir: Path,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """12288 + 4096 MiB is over the 9216 budget but within 2x it."""
        self._site(tmp_path_factory, monkeypatch, "[memory]\novercommit = 2\n")
        assert self._shortfall_with_running(tmp_vmdir, [4096] * 3, 4096) is None

    def test_overcommit_refuses_past_its_limit(
        self,
        tmp_vmdir: Path,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._site(tmp_path_factory, monkeypatch, "[memory]\novercommit = 2\n")
        shortfall = self._shortfall_with_running(tmp_vmdir, [4096] * 3, 8192)
        assert shortfall is not None
        assert shortfall.fits_when_idle
        assert "overcommit:   2 " in shortfall.message
        assert "may commit 18432 MiB" in shortfall.message
        # 12288 + 8192 - 18432
        assert "shortfall:    2048 MiB" in shortfall.message

    def test_overcommit_does_not_stretch_one_vm(
        self,
        tmp_vmdir: Path,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A VM bigger than the budget never fits, whatever the ratio."""
        self._site(tmp_path_factory, monkeypatch, "[memory]\novercommit = 4\n")
        shortfall = self._shortfall_with_running(tmp_vmdir, [], 12288)
        assert shortfall is not None
        assert not shortfall.fits_when_idle
        assert "shortfall:    3072 MiB" in shortfall.message

    def test_overcommit_defaults_to_one(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A site file without [memory] changes nothing, and says nothing."""
        self._site(
            tmp_path_factory, monkeypatch, "[telemetry]\nenabled = false\n"
        )
        assert qemu_run._memory_overcommit() == 1.0
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("value", ["0.5", "lots", "nan", "inf"])
    def test_bad_overcommit_falls_back_and_warns_once(
        self,
        value: str,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._site(
            tmp_path_factory, monkeypatch, f"[memory]\novercommit = {value}\n"
        )
        assert qemu_run._memory_overcommit() == 1.0
        assert qemu_run._memory_overcommit() == 1.0
        err = capsys.readouterr().err
        assert err.count("warning: ignoring [memory]") == 1

    def test_read_meminfo_parses_memtotal(self, tmp_path: Path) -> None:
        """_read_meminfo_mb returns kB-from-meminfo // 1024 for the named key."""
        meminfo = tmp_path / "meminfo"
        meminfo.write_text(
            "MemTotal:       16777216 kB\n"
            "MemFree:         1048576 kB\n"
            "MemAvailable:    4194304 kB\n"
        )
        real_open = open

        def fake_open(path, *a, **kw):
            if path == "/proc/meminfo":
                return real_open(meminfo)
            return real_open(path, *a, **kw)

        with patch("builtins.open", side_effect=fake_open):
            assert qemu_run._read_meminfo_mb("MemTotal") == 16384
            assert qemu_run._read_meminfo_mb("MemAvailable") == 4096
            assert qemu_run._read_meminfo_mb("Bogus") == 0


class TestLaunchWaitsForMemory:
    """launch_qemu refuses a full host at once, or with a wait waits for it."""

    FULL = qemu_run._Shortfall(
        "not enough host memory to start VM 'co1-single'\n  requested: 2048 MiB",
        fits_when_idle=True,
    )

    def _launch(
        self, vm: VMInfo, answers: Any, *, wait_seconds: int = 0
    ) -> None:
        """Run launch_qemu against a run of budget answers, on a fake clock.

        What it started and how long it slept are left in ``self.started``
        and ``self.slept``, so a test can read them after a refusal too.
        """
        clock = [1000.0]
        self.slept: list[float] = []
        self.started: list[VMInfo] = []

        def sleep(seconds: float) -> None:
            self.slept.append(seconds)
            clock[0] += seconds

        answers = iter(answers)
        with (
            patch("ltvm_pkg.qemu_run.is_running", return_value=False),
            patch("ltvm_pkg.qemu_run.is_macos", return_value=False),
            patch(
                "ltvm_pkg.qemu_run._memory_shortfall",
                side_effect=lambda _vm: next(answers),
            ),
            patch(
                "ltvm_pkg.qemu_run._start_qemu", side_effect=self.started.append
            ),
            patch(
                "ltvm_pkg.qemu_run.time.monotonic", side_effect=lambda: clock[0]
            ),
            patch("ltvm_pkg.qemu_run.time.sleep", side_effect=sleep),
        ):
            qemu_run.launch_qemu(vm, wait_seconds=wait_seconds)

    def test_a_full_host_is_refused_at_once(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            self._launch(_make_vm(tmp_vmdir), [self.FULL])
        assert self.slept == []
        err = capsys.readouterr().err
        assert "not enough host memory" in err
        assert "--wait SECONDS" in err

    def test_a_wait_launches_once_the_memory_frees(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vm = _make_vm(tmp_vmdir)
        self._launch(vm, [self.FULL, self.FULL, None], wait_seconds=60)
        assert self.started == [vm]
        assert self.slept == [qemu_run._MEMORY_POLL_SECONDS] * 2
        err = capsys.readouterr().err
        assert "waiting up to 60s" in err
        assert err.count("waiting up to") == 1

    def test_a_wait_gives_up_at_its_deadline(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            self._launch(
                _make_vm(tmp_vmdir),
                itertools.repeat(self.FULL),
                wait_seconds=12,
            )
        assert self.started == []
        # The last sleep is cut short so the give-up lands on the deadline.
        assert self.slept == [5, 5, 2]
        err = capsys.readouterr().err
        assert "gave up after waiting 12s" in err
        assert "--wait SECONDS" not in err

    def test_a_vm_no_idle_host_could_hold_is_not_waited_for(
        self, tmp_vmdir: Path
    ) -> None:
        hopeless = qemu_run._Shortfall(
            "not enough host memory", fits_when_idle=False
        )
        with pytest.raises(SystemExit):
            self._launch(_make_vm(tmp_vmdir), [hopeless], wait_seconds=600)
        assert self.slept == []

    def test_the_check_and_launch_hold_the_lock_and_the_wait_does_not(
        self, tmp_vmdir: Path
    ) -> None:
        events: list[tuple[str, bool]] = []
        held = [False]

        @contextmanager
        def lock() -> Iterator[None]:
            held[0] = True
            try:
                yield
            finally:
                held[0] = False

        answers = iter([self.FULL, None])

        def shortfall(_vm: VMInfo) -> Any:
            events.append(("check", held[0]))
            return next(answers)

        with (
            patch("ltvm_pkg.qemu_run._launch_lock", lock),
            patch("ltvm_pkg.qemu_run.is_running", return_value=False),
            patch("ltvm_pkg.qemu_run.is_macos", return_value=False),
            patch("ltvm_pkg.qemu_run._memory_shortfall", side_effect=shortfall),
            patch(
                "ltvm_pkg.qemu_run._start_qemu",
                side_effect=lambda _vm: events.append(("start", held[0])),
            ),
            patch(
                "ltvm_pkg.qemu_run.time.sleep",
                side_effect=lambda _s: events.append(("sleep", held[0])),
            ),
        ):
            qemu_run.launch_qemu(_make_vm(tmp_vmdir), wait_seconds=60)
        assert events == [
            ("check", True),
            ("sleep", False),
            ("check", True),
            ("start", True),
        ]

    def test_the_launch_lock_is_one_file_in_the_vm_dir(
        self, tmp_vmdir: Path
    ) -> None:
        path = tmp_vmdir / ".launch.lock"
        with qemu_run._launch_lock():
            with open(path) as other:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with open(path) as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


# ── kill_qemu ────────────────────────────────────────────


class TestKillQemu:
    """kill_qemu sends SIGTERM, escalates to SIGKILL, cleans up TAP."""

    def test_no_pid_just_clears_tap(self, tmp_vmdir: Path) -> None:
        """pid=0 skips the signalling path but still tears down TAP."""
        vm = _make_vm(tmp_vmdir)
        vm.pid = 0
        run_calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list):
                run_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            return r

        kill_calls: list[int] = []

        def fake_kill(pid, sig):
            kill_calls.append(sig)

        with (
            patch("ltvm_pkg.qemu_run.run", side_effect=fake_run),
            patch("ltvm_pkg.qemu_run.os.kill", side_effect=fake_kill),
            patch.object(VMInfo, "update_pid"),
        ):
            qemu_run.kill_qemu(vm)
        # No signals sent
        assert kill_calls == []
        # TAP teardown + ARP flush still happen
        assert any(
            len(c) >= 4 and c[1:4] == ["link", "del", vm.tap] for c in run_calls
        )
        assert any("neigh" in c for c in run_calls)

    def test_clean_sigterm_shutdown(self, tmp_vmdir: Path) -> None:
        """A process that exits after SIGTERM is not escalated to SIGKILL."""
        import signal as _signal

        vm = _make_vm(tmp_vmdir)
        vm.pid = 555
        sent: list[int] = []
        # The kill_qemu flow:
        #   1. is_running(): os.kill(pid, 0) succeeds + comm reads qemu
        #   2. os.kill(pid, SIGTERM)
        #   3. loop: os.kill(pid, 0) raises OSError -> process gone
        # We simulate this by tracking how many kill(0) calls we've seen
        # and only raising on the 2nd onward.
        zero_calls = [0]

        def fake_kill(pid, sig):
            sent.append(sig)
            if sig == 0:
                zero_calls[0] += 1
                if zero_calls[0] >= 2:
                    raise OSError("gone")

        with (
            patch("ltvm_pkg.qemu_run.run"),
            patch("ltvm_pkg.qemu_run.os.kill", side_effect=fake_kill),
            patch("ltvm_pkg.qemu_run.time.sleep"),
            # is_running() reads /proc/<pid>/comm; pretend the PID is qemu.
            patch("ltvm_pkg.qemu_run.Path") as mock_path,
            patch.object(VMInfo, "update_pid") as mock_update,
        ):
            mock_path.return_value.read_text.return_value = "qemu-system-x86\n"
            qemu_run.kill_qemu(vm)
        assert _signal.SIGTERM in sent
        assert _signal.SIGKILL not in sent
        mock_update.assert_called_once_with(0)

    def test_sigkill_escalation(self, tmp_vmdir: Path) -> None:
        """A process that ignores SIGTERM gets SIGKILL after the grace period."""
        import signal as _signal

        vm = _make_vm(tmp_vmdir)
        vm.pid = 555
        sent: list[int] = []

        def fake_kill(pid, sig):
            sent.append(sig)
            # Alive through the SIGTERM grace period, gone after SIGKILL.
            if sig == 0 and _signal.SIGKILL in sent:
                raise ProcessLookupError

        with (
            patch("ltvm_pkg.qemu_run.run"),
            patch("ltvm_pkg.qemu_run.os.kill", side_effect=fake_kill),
            patch("ltvm_pkg.qemu_run.time.sleep"),
            patch("ltvm_pkg.qemu_run.Path") as mock_path,
            patch.object(VMInfo, "update_pid") as mock_update,
        ):
            mock_path.return_value.read_text.return_value = "qemu-system-x86\n"
            qemu_run.kill_qemu(vm)
        assert _signal.SIGTERM in sent
        assert _signal.SIGKILL in sent
        mock_update.assert_called_once_with(0)

    def test_unsignalable_qemu_is_not_marked_stopped(
        self, tmp_vmdir: Path
    ) -> None:
        """A root QEMU whose `sudo kill` fails keeps its PID.

        Recording PID=0 for it hid a live guest from list, stop and
        destroy, and the next start failed on its locked pidfile.
        """
        vm = _make_vm(tmp_vmdir)
        vm.pid = 555
        refused = MagicMock(returncode=1)

        def fake_kill(pid, sig):
            if sig != 0:
                raise PermissionError

        with (
            patch("ltvm_pkg.qemu_run.run"),
            patch("ltvm_pkg.qemu_run.os.kill", side_effect=fake_kill),
            patch("ltvm_pkg.qemu_run.sudo_run", return_value=refused) as sudo,
            patch("ltvm_pkg.qemu_run.time.sleep"),
            patch("ltvm_pkg.qemu_run.is_running", return_value=True),
            patch.object(VMInfo, "update_pid") as mock_update,
            pytest.raises(SystemExit),
        ):
            qemu_run.kill_qemu(vm)
        mock_update.assert_not_called()
        assert [c.args[0][1] for c in sudo.mock_calls] == ["-15", "-9"]


class TestPidfileFallback:
    """A live QEMU recorded as PID=0 is found through its pidfile."""

    def test_live_qemu_in_pidfile_is_adopted(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        vm.pid = 0
        vm.pid_path.write_text("51721\n")
        with patch(
            "ltvm_pkg.qemu_run._qemu_pid_is", return_value=True
        ) as check:
            assert qemu_run.is_running(vm)
        check.assert_called_once_with(51721, vm.name)
        assert vm.pid == 51721

    def test_stale_pidfile_is_ignored(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        vm.pid = 0
        vm.pid_path.write_text("51721\n")
        with patch("ltvm_pkg.qemu_run._qemu_pid_is", return_value=False):
            assert not qemu_run.is_running(vm)
        assert vm.pid == 0

    def test_no_pidfile(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        vm.pid = 0
        with patch("ltvm_pkg.qemu_run._qemu_pid_is") as check:
            assert not qemu_run.is_running(vm)
        check.assert_not_called()

    def test_kill_qemu_tears_down_extra_taps(self, tmp_vmdir: Path) -> None:
        """kill_qemu must delete every TAP the VM owns, including extras.

        Without this the next ``ltvm create`` (or ``ltvm start``) on the
        same VM name would collide with the leftover TAPs.  The mgmt
        TAP always gets torn down; extras need explicit iteration.
        """
        vm = _make_vm(tmp_vmdir, name="co1-two")
        vm.pid = 0  # skip the signalling path; we only care about TAP teardown
        vm.nics = [NicSpec.parse("tcp"), NicSpec.parse("tcp")]

        run_calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list):
                run_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            return r

        with (
            patch("ltvm_pkg.qemu_run.run", side_effect=fake_run),
            patch("ltvm_pkg.qemu_run.os.kill"),
            patch.object(VMInfo, "update_pid"),
        ):
            qemu_run.kill_qemu(vm)

        # Three TAPs should have been deleted: mgmt + 2 extras.
        deletions = {
            c[3]
            for c in run_calls
            if len(c) >= 4 and c[:3] == ["ip", "link", "del"]
        }
        # mgmt tap
        assert vm.tap in deletions
        # both extras
        for _idx, _type, _tap, _mac in vm.extra_nics():
            assert _tap in deletions

    def test_kill_qemu_skips_when_pid_is_not_qemu(
        self,
        tmp_vmdir: Path,
    ) -> None:
        """PID reuse: vm.pid points at a non-qemu process; we don't signal it."""
        import signal as _signal

        vm = _make_vm(tmp_vmdir)
        vm.pid = 555
        sent: list[int] = []

        def fake_kill(pid, sig):
            sent.append(sig)

        with (
            patch("ltvm_pkg.qemu_run.run"),
            patch("ltvm_pkg.qemu_run.os.kill", side_effect=fake_kill),
            patch("ltvm_pkg.qemu_run.Path") as mock_path,
            patch.object(VMInfo, "update_pid") as mock_update,
        ):
            # Live PID but the comm is "bash" -- not our qemu.
            mock_path.return_value.read_text.return_value = "bash\n"
            qemu_run.kill_qemu(vm)
        # is_running's kill(0) probe gets recorded, but no SIGTERM/SIGKILL.
        assert _signal.SIGTERM not in sent
        assert _signal.SIGKILL not in sent
        # PID still gets reset to 0 because we want to clear stale state
        mock_update.assert_called_once_with(0)


# ── launch_qemu: QMP socket permissions ──────────────────


class TestLaunchQemuSocketPerms:
    """launch_qemu hands the QMP socket to the invoking user at 0o600.

    It must NOT be world-accessible: QMP exposes human-monitor-command
    and `migrate exec:`, both of which run a shell as the QEMU process
    owner (root for a VM created under sudo), and SOCKETS is 0755.
    """

    def test_socket_chmoded_to_0o600_after_launch(
        self, tmp_vmdir: Path
    ) -> None:
        vm = _make_vm(tmp_vmdir)
        vm.pid_path.write_text("12345\n")

        chmod_calls: list[tuple] = []

        def fake_chmod(path, mode):
            chmod_calls.append((str(path), mode))

        h = _LaunchHarness()
        with (
            patch("ltvm_pkg.qemu_run.run", side_effect=h.run),
            patch(
                "ltvm_pkg.qemu_run.subprocess.run",
                side_effect=h.subprocess_run,
            ),
            patch("ltvm_pkg.qemu_run.is_running", return_value=False),
            patch("ltvm_pkg.qemu_run._memory_shortfall", return_value=None),
            patch("ltvm_pkg.qemu_run.time.time", return_value=1700000000),
            patch.object(VMInfo, "update_pid"),
            patch.object(VMInfo, "update_last_boot"),
            patch("ltvm_pkg.qemu_run.os.chmod", side_effect=fake_chmod),
        ):
            qemu_run.launch_qemu(vm)

        socket_str = str(vm.socket_path)
        chmod_for_socket = [(p, m) for p, m in chmod_calls if p == socket_str]
        assert chmod_for_socket, (
            f"expected os.chmod({socket_str!r}, 0o600); got {chmod_calls}"
        )
        assert chmod_for_socket[0][1] == 0o600
        assert chmod_for_socket[0][1] & 0o077 == 0, (
            "QMP socket must not be group/world accessible"
        )


class TestIsRunningPidIdentity:
    """is_running must confirm the pid is *this* VM's qemu."""

    def _vm(self, name: str, pid: int = 42) -> VMInfo:
        return VMInfo(name=name, ip="1.2.3.4", pid=pid)

    def _patch_proc(self, comm: str, cmdline: bytes) -> Any:
        mock_path = MagicMock()
        mock_path.return_value.read_text.return_value = comm
        mock_path.return_value.read_bytes.return_value = cmdline
        return patch("ltvm_pkg.qemu_run.Path", mock_path)

    def test_matching_name_is_running(self) -> None:
        with self._patch_proc(
            "qemu-system-x86\n",
            b"qemu-system-x86_64\x00-name\x00co1-single\x00-machine\x00microvm\x00",
        ):
            assert qemu_run.is_running(self._vm("co1-single")) is True

    def test_other_vms_qemu_is_not_running(self) -> None:
        """The PID-reuse case that could SIGKILL the wrong VM.

        After a host reboot without `ltvm stop`, PIDs restart low and
        so do the stale pids in .info files, so VM A's stale pid can
        land on VM B's live qemu.  comm says "qemu-system" for both.
        """
        with self._patch_proc(
            "qemu-system-x86\n",
            b"qemu-system-x86_64\x00-name\x00co2-oss\x00-machine\x00microvm\x00",
        ):
            assert qemu_run.is_running(self._vm("co1-single")) is False

    def test_unreadable_cmdline_keeps_prior_behavior(self) -> None:
        """Don't report a live VM as stopped just because /proc is coy."""
        mock_path = MagicMock()
        mock_path.return_value.read_text.return_value = "qemu-system-x86\n"
        mock_path.return_value.read_bytes.side_effect = PermissionError()
        with patch("ltvm_pkg.qemu_run.Path", mock_path):
            assert qemu_run.is_running(self._vm("co1-single")) is True

    def test_non_qemu_pid_reuse_still_rejected(self) -> None:
        with self._patch_proc("bash\n", b"bash\x00-c\x00sleep\x00"):
            assert qemu_run.is_running(self._vm("co1-single")) is False


class TestLaunchElevation:
    """QEMU is launched as root; ltvm itself need not be.

    It writes its pidfile and QMP socket into /opt/qemu-vms/sockets,
    which is root-owned 0755 and which `doctor` asserts that mode on, so
    the launch elevates rather than the whole command.
    """

    def test_unprivileged_launch_goes_through_sudo(
        self, tmp_vmdir: Path
    ) -> None:
        vm = _make_vm(tmp_vmdir)
        h = _LaunchHarness()
        with patch("ltvm_pkg.qemu_run.os.geteuid", return_value=1000):
            _run_launch(vm, h)
        assert h.qemu_args is not None
        assert h.qemu_args[0] == "sudo"
        assert h.qemu_argv[0].endswith("qemu-system-x86_64")

    def test_root_launches_qemu_directly(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        h = _LaunchHarness()
        with patch("ltvm_pkg.qemu_run.os.geteuid", return_value=0):
            _run_launch(vm, h)
        assert h.qemu_args is not None
        assert h.qemu_args[0] != "sudo"


# ── an unprivileged guest's own scope ────────────────────


class TestGuestScope:
    """QEMU keeps its caller's cgroup; a guest must not die with its caller."""

    ENV = {"XDG_RUNTIME_DIR": "/run/user/1001"}
    MANAGER = "0::/user.slice/user-1001.slice/user@1001.service/app.slice/x"
    SESSION = "0::/user.slice/user-1001.slice/session-4.scope"

    @pytest.fixture(autouse=True)
    def _host(self, tmp_path: Path) -> None:
        self.host = tmp_path / "host"
        self.host.mkdir()

    def _scope(
        self,
        vm: VMInfo,
        env: dict[str, str],
        euid: int = 1001,
        cgroup: str = MANAGER,
        linger: bool = False,
    ) -> list[str]:
        proc = self.host / "cgroup"
        proc.write_text(f"{cgroup}\n")
        lingering = self.host / "linger"
        lingering.mkdir(exist_ok=True)
        if linger:
            (lingering / "alice").touch()
        with (
            patch("ltvm_pkg.qemu_run._PROC_SELF_CGROUP", proc),
            patch("ltvm_pkg.qemu_run._LINGER_DIR", lingering),
            patch(
                "ltvm_pkg.qemu_run.pwd.getpwuid",
                return_value=MagicMock(pw_name="alice"),
            ),
            patch.dict("os.environ", env, clear=True),
            patch("ltvm_pkg.qemu_run.os.geteuid", return_value=euid),
            patch("ltvm_pkg.qemu_run.sys.platform", "linux"),
            patch(
                "ltvm_pkg.qemu_run.shutil.which",
                return_value="/usr/bin/systemd-run",
            ),
            patch("ltvm_pkg.qemu_run.time.time", return_value=1700000000),
        ):
            return qemu_run._guest_scope(vm)

    def test_a_user_with_a_manager_gets_a_scope_per_guest(
        self, tmp_vmdir: Path
    ) -> None:
        prefix = self._scope(_make_vm(tmp_vmdir, name="co23-lnet"), self.ENV)
        assert prefix[:3] == ["systemd-run", "--user", "--scope"]
        [unit] = [a for a in prefix if a.startswith("--unit=")]
        assert re.fullmatch(
            r"--unit=ltvm-co23-lnet-1700000000-[0-9a-f]{8}\.scope", unit
        )
        assert "--collect" in prefix
        assert prefix[-1] == "--"

    def test_two_guests_of_one_name_in_one_second_get_their_own_units(
        self, tmp_vmdir: Path
    ) -> None:
        vm = _make_vm(tmp_vmdir, name="co23-lnet")
        units = [
            [a for a in self._scope(vm, self.ENV) if a.startswith("--unit=")]
            for _ in range(2)
        ]
        assert units[0] != units[1]

    def test_a_session_guest_without_linger_stays_in_its_session(
        self, tmp_vmdir: Path
    ) -> None:
        # user@UID.service stops at logout when linger is off; the session
        # scope does not, with KillUserProcesses=no.
        vm = _make_vm(tmp_vmdir)
        assert self._scope(vm, self.ENV, cgroup=self.SESSION) == []

    def test_a_caller_under_its_manager_gets_a_scope_without_linger(
        self, tmp_vmdir: Path
    ) -> None:
        vm = _make_vm(tmp_vmdir)
        assert self._scope(vm, self.ENV, cgroup=self.MANAGER) != []
        other = "0::/user.slice/user-1002.slice/user@1002.service/x.scope"
        assert self._scope(vm, self.ENV, cgroup=other) == []

    def test_a_lingering_user_gets_a_scope_from_a_session(
        self, tmp_vmdir: Path
    ) -> None:
        vm = _make_vm(tmp_vmdir)
        prefix = self._scope(vm, self.ENV, cgroup=self.SESSION, linger=True)
        assert prefix[:3] == ["systemd-run", "--user", "--scope"]

    def test_no_scope_without_a_user_manager_as_root_or_when_off(
        self, tmp_vmdir: Path
    ) -> None:
        vm = _make_vm(tmp_vmdir)
        assert self._scope(vm, {}) == []
        assert self._scope(vm, self.ENV, euid=0) == []
        assert self._scope(vm, {**self.ENV, "LTVM_GUEST_SCOPE": "0"}) == []

    def _launch(
        self, tmp_vmdir: Path, results: list[int], refusal: str = ""
    ) -> list[list[str]]:
        vm = _make_vm(tmp_vmdir)
        vm.pid_path.write_text("12345\n")
        launched: list[list[str]] = []

        def subprocess_run(argv, **kwargs):
            launched.append(list(argv))
            if argv[0] == "systemd-run" and refusal:
                kwargs["stdout"].write(refusal + "\n")
                kwargs["stdout"].flush()
            r = MagicMock()
            r.returncode = results[len(launched) - 1]
            return r

        ready = MagicMock(
            ok=True, helper=Path("/usr/lib/qemu/qemu-bridge-helper")
        )
        with (
            patch(
                "ltvm_pkg.qemu_run.run", return_value=MagicMock(returncode=0)
            ),
            patch(
                "ltvm_pkg.qemu_run.subprocess.run", side_effect=subprocess_run
            ),
            patch("ltvm_pkg.qemu_run.is_running", return_value=False),
            patch("ltvm_pkg.qemu_run._memory_shortfall", return_value=None),
            patch("ltvm_pkg.qemu_run.os.geteuid", return_value=1001),
            patch("ltvm_pkg.qemu_run.is_macos", return_value=False),
            patch("ltvm_pkg.qemu_run.rootless.readiness", return_value=ready),
            patch(
                "ltvm_pkg.qemu_run._guest_scope",
                return_value=["systemd-run", "--user", "--scope", "--"],
            ),
            patch.object(VMInfo, "update_pid"),
            patch.object(VMInfo, "update_last_boot"),
        ):
            qemu_run.launch_qemu(vm)
        return launched

    def test_an_unprivileged_guest_starts_in_its_scope(
        self, tmp_vmdir: Path
    ) -> None:
        [argv] = self._launch(tmp_vmdir, [0])
        assert argv[:4] == ["systemd-run", "--user", "--scope", "--"]
        assert "-daemonize" in argv

    def test_a_scope_systemd_refuses_falls_back_to_the_old_launch(
        self, tmp_vmdir: Path
    ) -> None:
        first, second = self._launch(
            tmp_vmdir,
            [1, 0],
            refusal="Failed to start transient scope unit: Access denied",
        )
        assert first[0] == "systemd-run"
        assert second[0] != "systemd-run" and "-daemonize" in second

    def test_a_qemu_that_fails_inside_its_scope_is_not_retried(
        self, tmp_vmdir: Path
    ) -> None:
        with pytest.raises(SystemExit):
            self._launch(tmp_vmdir, [1], refusal="")

    def test_a_qemu_socket_it_cannot_reach_is_not_a_refused_scope(
        self, tmp_vmdir: Path
    ) -> None:
        with pytest.raises(SystemExit):
            self._launch(
                tmp_vmdir,
                [1, 0],
                refusal="qemu-system-x86_64: -chardev socket,id=c0: "
                "Failed to connect to socket /tmp/x: Connection refused",
            )

    def test_a_user_bus_it_cannot_reach_falls_back_to_the_old_launch(
        self, tmp_vmdir: Path
    ) -> None:
        first, second = self._launch(
            tmp_vmdir,
            [1, 0],
            refusal="Failed to connect to bus: No medium found",
        )
        assert first[0] == "systemd-run"
        assert second[0] != "systemd-run"


class TestGuestCgroup:
    """_guest_cgroup names where a launched QEMU's memory is bounded."""

    def test_own_scope_means_the_user_app_slice(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        ready = MagicMock(ok=True)
        with (
            patch("os.geteuid", return_value=1000),
            patch(
                "ltvm_pkg.qemu_run._guest_scope", return_value=["systemd-run"]
            ),
            patch("ltvm_pkg.rootless.readiness", return_value=ready),
        ):
            assert qemu_run._guest_cgroup(vm) == (
                "/user.slice/user-1000.slice/user@1000.service/app.slice"
            )

    def test_no_scope_means_the_callers(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        with (
            patch("os.geteuid", return_value=1000),
            patch("ltvm_pkg.qemu_run._guest_scope", return_value=[]),
        ):
            assert qemu_run._guest_cgroup(vm) is None

    def test_root_qemu_stays_in_the_callers(self, tmp_vmdir: Path) -> None:
        vm = _make_vm(tmp_vmdir)
        with patch("os.geteuid", return_value=0):
            assert qemu_run._guest_cgroup(vm) is None
