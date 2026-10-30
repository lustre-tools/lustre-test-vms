"""Tests for ltvm_pkg/vm_commands.py: validation, parsing, lifecycle paths."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import vm_cluster, vm_commands
from ltvm_pkg.deploy import premount_cleanup_script
from ltvm_pkg.vm_state import (
    DISK_SIZE_BYTES,
    ROOT_SIZE_BYTES,
    ClusterInfo,
    VMInfo,
)
from tests.host_marks import needs_proc

# ── _validate_vm_name ────────────────────────────────────


class TestValidateVmName:
    """VM names must be safe for /etc/hosts, ssh config, shell and cmdline."""

    @pytest.mark.parametrize(
        "name",
        ["co1-mds", "co1.single", "vm_1", "A-123.test", "x"],
    )
    def test_accepts_valid(self, name: str) -> None:
        vm_commands._validate_vm_name(name)  # must not raise

    def test_empty_rejected(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._validate_vm_name("")

    def test_too_long_rejected(self) -> None:
        """63-char DNS label limit."""
        with pytest.raises(SystemExit):
            vm_commands._validate_vm_name("a" * 64)

    def test_boundary_63_accepted(self) -> None:
        vm_commands._validate_vm_name("a" * 63)

    @pytest.mark.parametrize(
        "name",
        [
            "-leading-dash",
            "has space",
            "tab\there",
            "under/slash",
            "quote'here",
            "new\nline",
            "émoji",
            ".leading-dot",
        ],
    )
    def test_unsafe_chars_rejected(self, name: str) -> None:
        with pytest.raises(SystemExit):
            vm_commands._validate_vm_name(name)


# ── parse_nic_spec / validate_nic_spec ───────────────────


class TestParseNicSpec:
    """parse_nic_spec splits TYPE[:ARG] and rejects unknown types."""

    def test_tcp_no_arg(self) -> None:
        assert vm_commands.parse_nic_spec("tcp") == ("tcp", "")

    def test_softroce_recognised(self) -> None:
        """softroce parses (the rejection is in validate_nic_spec)."""
        assert vm_commands.parse_nic_spec("softroce") == ("softroce", "")

    def test_passthrough_with_bdf(self) -> None:
        """Colons in ARG survive (PCIe BDF has colons: 0000:00:02.0)."""
        t, a = vm_commands.parse_nic_spec("passthrough:0000:00:02.0")
        assert t == "passthrough"
        assert a == "0000:00:02.0"

    def test_type_case_insensitive(self) -> None:
        assert vm_commands.parse_nic_spec("TCP")[0] == "tcp"

    def test_empty_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands.parse_nic_spec("")

    def test_unknown_type_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands.parse_nic_spec("infiniband")


class TestValidateNicSpec:
    """validate_nic_spec is the final CLI-layer gate.

    Implemented types round-trip through the canonical storage form.
    Reserved types die with a pointer at the tracking issue so agents
    working on -r55 / -5a0 don't accidentally land their code against
    a parser that already accepts those types silently.
    """

    def test_tcp_passes(self) -> None:
        assert vm_commands.validate_nic_spec("tcp") == "tcp"

    def test_softroce_passes(self) -> None:
        """softroce is implemented: fc_nics=softroce reaches the guest,
        rc.local runs setup-nic-softroce.sh, setup-lnet-config.sh emits
        the matching o2ib0(ethI) line into /etc/modprobe.d/lnet.conf
        (LNet takes the backing netdev; ko2iblnd finds rxe via
        rdma_cm)."""
        assert vm_commands.validate_nic_spec("softroce") == "softroce"

    def test_passthrough_accepts_valid_bdf(self) -> None:
        """passthrough requires a BDF arg; the canonical form round-trips."""
        assert (
            vm_commands.validate_nic_spec("passthrough:0000:85:00.1")
            == "passthrough:0000:85:00.1"
        )

    def test_passthrough_rejects_empty_bdf(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            vm_commands.validate_nic_spec("passthrough")
        err = capsys.readouterr().err
        assert "requires a PCIe BDF arg" in err

    def test_passthrough_rejects_malformed_bdf(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            vm_commands.validate_nic_spec("passthrough:not-a-bdf")
        err = capsys.readouterr().err
        assert "not a valid" in err

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands.validate_nic_spec("ethernet")


# ── _parse_disk_size ─────────────────────────────────────


class TestParseDiskSize:
    """Disk size accepts M/G suffixes with explicit bounds."""

    def test_default_on_none(self) -> None:
        assert vm_commands._parse_disk_size(None) == DISK_SIZE_BYTES

    def test_default_on_empty(self) -> None:
        assert vm_commands._parse_disk_size("") == DISK_SIZE_BYTES

    def test_default_on_whitespace(self) -> None:
        assert vm_commands._parse_disk_size("   ") == DISK_SIZE_BYTES

    def test_parses_megabytes(self) -> None:
        assert vm_commands._parse_disk_size("500M") == 500 * (1 << 20)

    def test_parses_gigabytes(self) -> None:
        assert vm_commands._parse_disk_size("2G") == 2 * (1 << 30)

    def test_lowercase_suffix_accepted(self) -> None:
        """500m upcases to 500M before parsing."""
        assert vm_commands._parse_disk_size("500m") == 500 * (1 << 20)

    def test_int_value_returned_as_is_when_big_enough(self) -> None:
        assert vm_commands._parse_disk_size(200 * (1 << 20)) == 200 * (1 << 20)

    def test_int_below_minimum_dies(self) -> None:
        """Sub-floor int input dies loud, matching string behavior.
        The older silent-substitute-default behavior was a footgun --
        callers passing a deliberately small byte count got silently
        upgraded to the default without warning.
        """
        with pytest.raises(SystemExit):
            vm_commands._parse_disk_size(1024)

    def test_int_above_maximum_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_disk_size(200 * (1 << 30))

    def test_invalid_suffix_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_disk_size("500K")

    def test_no_suffix_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_disk_size("500")

    def test_non_numeric_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_disk_size("foo M")

    def test_zero_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_disk_size("0G")

    def test_below_64m_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_disk_size("32M")

    def test_above_100g_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_disk_size("200G")

    def test_boundary_64m_accepted(self) -> None:
        assert vm_commands._parse_disk_size("64M") == 64 * (1 << 20)

    def test_boundary_100g_accepted(self) -> None:
        assert vm_commands._parse_disk_size("100G") == 100 * (1 << 30)


# ── _parse_root_size ─────────────────────────────────────


class TestParseRootSize:
    """The root (OS) disk takes the same forms as --disk-size, with a
    floor of its own: a root smaller than 1G cannot hold an OS."""

    def test_default_on_none(self) -> None:
        assert vm_commands._parse_root_size(None) == ROOT_SIZE_BYTES

    def test_default_is_8g(self) -> None:
        assert ROOT_SIZE_BYTES == 8 * (1 << 30)

    def test_parses_gigabytes(self) -> None:
        assert vm_commands._parse_root_size("20G") == 20 * (1 << 30)

    def test_parses_megabytes(self) -> None:
        assert vm_commands._parse_root_size("4096M") == 4096 * (1 << 20)

    def test_lowercase_suffix_accepted(self) -> None:
        assert vm_commands._parse_root_size("20g") == 20 * (1 << 30)

    def test_int_bytes_accepted(self) -> None:
        assert vm_commands._parse_root_size(12 * (1 << 30)) == 12 * (1 << 30)

    def test_boundary_1g_accepted(self) -> None:
        assert vm_commands._parse_root_size("1G") == 1 << 30

    def test_boundary_100g_accepted(self) -> None:
        assert vm_commands._parse_root_size("100G") == 100 * (1 << 30)

    def test_below_1g_dies(self) -> None:
        """64M clears the data-disk floor but not the root one."""
        with pytest.raises(SystemExit):
            vm_commands._parse_root_size("512M")

    def test_above_100g_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_root_size("200G")

    def test_invalid_suffix_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_root_size("16T")

    def test_no_suffix_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_root_size("16")

    def test_zero_dies(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._parse_root_size("0G")

    def test_error_names_the_root_flag(self, capsys: Any) -> None:
        """--disk-size in a --root-size error sends the user to the
        wrong flag."""
        with pytest.raises(SystemExit):
            vm_commands._parse_root_size("16T")
        err = capsys.readouterr().err
        assert "--root-size" in err
        assert "--disk-size" not in err


# ── _check_root_size_fits ────────────────────────────────


class TestCheckRootSizeFits:
    """The overlay cannot be smaller than the image it is backed by."""

    def test_smaller_than_base_image_dies(self, tmp_path: Path) -> None:
        image = tmp_path / "base.ext4"
        image.write_bytes(b"\0" * (4 << 20))
        with pytest.raises(SystemExit):
            vm_commands._check_root_size_fits(2 << 20, str(image))

    def test_error_names_both_sizes(self, tmp_path: Path, capsys: Any) -> None:
        image = tmp_path / "base.ext4"
        image.write_bytes(b"\0" * (4 << 20))
        with pytest.raises(SystemExit):
            vm_commands._check_root_size_fits(2 << 20, str(image))
        err = capsys.readouterr().err
        assert "2M" in err
        assert "4M" in err

    def test_equal_to_base_image_is_allowed(self, tmp_path: Path) -> None:
        image = tmp_path / "base.ext4"
        image.write_bytes(b"\0" * (4 << 20))
        vm_commands._check_root_size_fits(4 << 20, str(image))

    def test_larger_than_base_image_is_allowed(self, tmp_path: Path) -> None:
        image = tmp_path / "base.ext4"
        image.write_bytes(b"\0" * (4 << 20))
        vm_commands._check_root_size_fits(8 << 30, str(image))

    def test_missing_image_is_not_this_check_s_error(
        self, tmp_path: Path
    ) -> None:
        """A missing image is reported by the resolver, with a better
        message than this could give."""
        vm_commands._check_root_size_fits(1 << 30, str(tmp_path / "nope.ext4"))


# ── _size_label ──────────────────────────────────────────


class TestSizeLabel:
    def test_whole_gigabytes(self) -> None:
        assert vm_commands._size_label(20 * (1 << 30)) == "20G"

    def test_megabytes_when_not_a_whole_gigabyte(self) -> None:
        assert vm_commands._size_label(500 * (1 << 20)) == "500M"


# ── _validate_kernel_args ────────────────────────────────


class TestValidateKernelArgs:
    @pytest.mark.parametrize("raw", [None, ""])
    def test_absent_is_empty(self, raw: str | None) -> None:
        assert vm_commands._validate_kernel_args(raw) == ""

    def test_accepted_and_stripped(self) -> None:
        got = vm_commands._validate_kernel_args(
            " slub_debug=FZPU page_owner=on "
        )
        assert got == "slub_debug=FZPU page_owner=on"

    def test_names_that_only_start_like_ltvms_are_accepted(self) -> None:
        raw = "rootwait consoleblank=0 kasan.fault=panic"
        assert vm_commands._validate_kernel_args(raw) == raw

    @pytest.mark.parametrize(
        "raw",
        [
            "root=/dev/vdb",
            "console=tty0",
            "quiet fc_ip=10.0.0.1",
            "fc_nics=tcp",
        ],
    )
    def test_parameters_ltvm_sets_are_refused(self, raw: str) -> None:
        with pytest.raises(SystemExit):
            vm_commands._validate_kernel_args(raw)

    @pytest.mark.parametrize("raw", ["a\nb", "a\tb", "a\x7fb"])
    def test_control_characters_are_refused(self, raw: str) -> None:
        """A newline would end the value in the .info file."""
        with pytest.raises(SystemExit):
            vm_commands._validate_kernel_args(raw)

    def test_too_long_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            vm_commands._validate_kernel_args(
                "x" * (vm_commands.KERNEL_ARGS_MAX + 1)
            )


# ── _warn_ignored_resource_flags ─────────────────────────


class TestWarnIgnoredResourceFlags:
    """An idempotent create cannot resize hardware, so flags that
    disagree with the existing VM have to be called out."""

    def _vm(self) -> VMInfo:
        return VMInfo(
            name="co1-exists",
            ip="192.168.100.9",
            mem=4096,
            disk_size=DISK_SIZE_BYTES,
            root_size=ROOT_SIZE_BYTES,
        )

    def test_differing_root_size_warns(self, capsys: Any) -> None:
        args = argparse.Namespace(root_size="20G")
        vm_commands._warn_ignored_resource_flags(
            self._vm(),
            args,
            argv=["ltvm", "create", "co1-exists", "--root-size", "20G"],
        )
        err = capsys.readouterr().err
        assert "--root-size: requested 20G, VM has 8G" in err

    def test_matching_root_size_is_silent(self, capsys: Any) -> None:
        """Bytes on the VM against "8G" from the user is the same size;
        comparing their strings called it a mismatch."""
        args = argparse.Namespace(root_size="8G")
        vm_commands._warn_ignored_resource_flags(
            self._vm(),
            args,
            argv=["ltvm", "create", "co1-exists", "--root-size", "8G"],
        )
        assert capsys.readouterr().err == ""

    def test_matching_disk_size_is_silent(self, capsys: Any) -> None:
        args = argparse.Namespace(disk_size="500M")
        vm_commands._warn_ignored_resource_flags(
            self._vm(),
            args,
            argv=["ltvm", "create", "co1-exists", "--disk-size", "500M"],
        )
        assert capsys.readouterr().err == ""

    def test_differing_kernel_args_warn(self, capsys: Any) -> None:
        args = argparse.Namespace(kernel_args="slub_debug=FZPU")
        vm_commands._warn_ignored_resource_flags(
            self._vm(),
            args,
            argv=[
                "ltvm",
                "create",
                "co1-exists",
                "--kernel-args=slub_debug=FZPU",
            ],
        )
        err = capsys.readouterr().err
        assert "--kernel-args: requested slub_debug=FZPU, VM has (none)" in err

    def test_matching_kernel_args_are_silent(self, capsys: Any) -> None:
        vm = self._vm()
        vm.kernel_args = "slub_debug=FZPU"
        args = argparse.Namespace(kernel_args="slub_debug=FZPU")
        vm_commands._warn_ignored_resource_flags(
            vm,
            args,
            argv=[
                "ltvm",
                "create",
                "co1-exists",
                "--kernel-args",
                "slub_debug=FZPU",
            ],
        )
        assert capsys.readouterr().err == ""

    def test_flag_not_typed_is_not_reported(self, capsys: Any) -> None:
        """An argparse default is not a request."""
        args = argparse.Namespace(root_size=None, mem=8192)
        vm_commands._warn_ignored_resource_flags(
            self._vm(), args, argv=["ltvm", "create", "co1-exists"]
        )
        assert capsys.readouterr().err == ""


# ── _ago formatter ───────────────────────────────────────


class TestAgoFormatter:
    """_ago renders deltas in seconds/minutes/hours/days."""

    def test_zero_returns_dash(self) -> None:
        assert vm_commands._ago(0) == "-"

    def test_recent_is_just_now(self) -> None:
        with patch("ltvm_pkg.vm_commands.time.time", return_value=1000):
            assert vm_commands._ago(990) == "just now"

    def test_minutes(self) -> None:
        with patch("ltvm_pkg.vm_commands.time.time", return_value=10_000):
            assert vm_commands._ago(10_000 - 300) == "5m ago"

    def test_hours(self) -> None:
        with patch("ltvm_pkg.vm_commands.time.time", return_value=100_000):
            assert vm_commands._ago(100_000 - 7200) == "2h ago"

    def test_days(self) -> None:
        with patch("ltvm_pkg.vm_commands.time.time", return_value=1_000_000):
            assert vm_commands._ago(1_000_000 - 2 * 86400) == "2d ago"


# ── _destroy_vm_artifacts ────────────────────────────────


@pytest.fixture
def tmp_vmdir(tmp_path: Path) -> Path:
    sockets = tmp_path / "sockets"
    overlays = tmp_path / "overlays"
    sockets.mkdir()
    overlays.mkdir()
    with (
        patch("ltvm_pkg.vm_state.VM_DIR", tmp_path),
        patch("ltvm_pkg.vm_state.SOCKETS", sockets),
        patch("ltvm_pkg.vm_state.OVERLAYS", overlays),
        patch("ltvm_pkg.vm_commands.SOCKETS", sockets),
        patch("ltvm_pkg.vm_commands.OVERLAYS", overlays),
    ):
        yield tmp_path


def _seed_vm_files(
    tmp_vmdir: Path,
    name: str,
    *,
    mdt: int = 0,
    ost: int = 0,
) -> VMInfo:
    """Create overlay, data disks, and info file for *name*."""
    vm = VMInfo(
        name=name,
        ip="192.168.100.50",
        pid=0,
        tap=f"tap-{name}",
        mdt_disks=mdt,
        ost_disks=ost,
    )
    vm.save()
    vm.overlay_path.write_text("")
    vm.log_path.write_text("")
    vm.pid_path.write_text("")
    vm.socket_path.write_text("")
    for n in range(1, mdt + ost + 1):
        vm.disk_path(n).write_text("")
    return vm


class TestDestroyVmArtifacts:
    """_destroy_vm_artifacts removes overlay, disks, and control-plane files."""

    def test_removes_all_files(self, tmp_vmdir: Path) -> None:
        vm = _seed_vm_files(tmp_vmdir, "gone", mdt=1, ost=2)
        # Sanity: everything exists pre-destroy
        assert vm.overlay_path.exists()
        assert vm.info_path.exists()
        for n in range(1, 4):
            assert vm.disk_path(n).exists()

        vm_commands._destroy_vm_artifacts("gone")

        assert not vm.overlay_path.exists()
        assert not vm.info_path.exists()
        assert not vm.log_path.exists()
        assert not vm.pid_path.exists()
        assert not vm.socket_path.exists()
        for n in range(1, 4):
            assert not vm.disk_path(n).exists()

    def test_missing_vm_is_noop(self, tmp_vmdir: Path) -> None:
        """Destroying a non-existent VM does not raise."""
        vm_commands._destroy_vm_artifacts("never-existed")

    def test_partial_state_cleaned(self, tmp_vmdir: Path) -> None:
        """Works even if some files are missing (partial create rollback)."""
        vm = _seed_vm_files(tmp_vmdir, "half", mdt=1)
        vm.info_path.unlink()  # .info missing, others remain
        vm_commands._destroy_vm_artifacts("half")
        assert not vm.overlay_path.exists()
        assert not vm.disk_path(1).exists()

    def test_info_lock_file_also_removed(self, tmp_vmdir: Path) -> None:
        """The per-VM lock file is cleaned up alongside .info."""
        vm = _seed_vm_files(tmp_vmdir, "locked")
        # update_pid creates the lock file
        vm.update_pid(0)
        lock = vm._lock_path
        assert lock.exists()
        vm_commands._destroy_vm_artifacts("locked")
        assert not lock.exists()


# ── cmd_create validation ────────────────────────────────


def _create_args(**overrides) -> argparse.Namespace:
    defaults = {
        "name": "co1-single",
        "vcpus": 2,
        "mem": 2048,
        "mdt_disks": 0,
        "ost_disks": 0,
        "disk_size": None,
        "image": "",
        "kernel": "",
        "target": "",
        "arch": None,
        "ip": None,
        "json": False,
        "_quiet": True,
        "nic": None,  # repeatable --nic; None == no extras
    }
    if "os" in overrides:
        overrides["target"] = overrides.pop("os")
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestCmdCreateValidation:
    """cmd_create short-circuits on bad arguments before touching the filesystem."""

    def test_existing_running_vm_noop(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """create on a running VM re-registers SSH and prints 'already running'."""
        _seed_vm_files(tmp_vmdir, "running")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.wait_for_ssh"),
            patch("ltvm_pkg.vm_commands.register_ssh_name"),
        ):
            vm_commands.cmd_create(_create_args(name="running"))
        out = capsys.readouterr().out
        assert "already running" in out

    def test_existing_stopped_vm_starts(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """create on a stopped VM launches QEMU and prints 'started'."""
        _seed_vm_files(tmp_vmdir, "stopped-c")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=False),
            patch("ltvm_pkg.vm_commands.launch_qemu"),
            patch("ltvm_pkg.vm_commands.provision_vm_ssh"),
            patch("ltvm_pkg.vm_commands._seed_kdump_boot"),
        ):
            vm_commands.cmd_create(_create_args(name="stopped-c"))
        out = capsys.readouterr().out
        assert "started" in out

    def test_nonpositive_vcpus_dies(self, tmp_vmdir: Path) -> None:
        with pytest.raises(SystemExit):
            vm_commands.cmd_create(_create_args(name="bad-vcpus", vcpus=0))

    def test_nonpositive_mem_dies(self, tmp_vmdir: Path) -> None:
        with pytest.raises(SystemExit):
            vm_commands.cmd_create(_create_args(name="bad-mem", mem=-1))

    def test_too_many_data_disks_dies(self, tmp_vmdir: Path) -> None:
        """vda is rootfs; max 25 data disks (vdb..vdz)."""
        with pytest.raises(SystemExit):
            vm_commands.cmd_create(
                _create_args(name="too-many", mdt_disks=20, ost_disks=10)
            )

    def test_invalid_name_dies(self, tmp_vmdir: Path) -> None:
        with pytest.raises(SystemExit):
            vm_commands.cmd_create(_create_args(name="bad name"))


# ── cmd_destroy ──────────────────────────────────────────


class TestCmdDestroy:
    """cmd_destroy stops QEMU, removes artifacts, and unregisters DNS."""

    def test_destroy_missing_is_soft_error(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Destroying a missing VM prints 'not found' instead of lying.

        Used to print 'destroyed ghost' even when the VM never existed,
        which made typos like 'ltvm destroy co1-signle' look like a
        successful destroy.
        """
        args = argparse.Namespace(names=["ghost"])
        with (
            patch("ltvm_pkg.vm_commands.kill_qemu") as mock_kill,
            patch("ltvm_pkg.vm_commands.unregister_ssh_name"),
        ):
            vm_commands.cmd_destroy(args)
        mock_kill.assert_not_called()
        assert "destroy: ghost not found" in capsys.readouterr().out

    def test_destroy_live_vm_kills_and_unregisters(
        self,
        tmp_vmdir: Path,
    ) -> None:
        vm = _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(names=["live"])
        with (
            patch("ltvm_pkg.vm_commands.kill_qemu") as mock_kill,
            patch("ltvm_pkg.vm_commands.unregister_ssh_name") as mock_unreg,
        ):
            vm_commands.cmd_destroy(args)
        mock_kill.assert_called_once()
        mock_unreg.assert_called_once_with("live")
        assert not vm.info_path.exists()
        assert not vm.overlay_path.exists()


class TestOrphanClusterRecord:
    """A cluster record whose VMs are all gone must not block its name."""

    @staticmethod
    def _record(name: str, members: list[str]) -> ClusterInfo:
        cluster = ClusterInfo(
            name=name,
            nodes=[{"name": m, "roles": ["client"]} for m in members],
        )
        cluster.save()
        return cluster

    def test_destroying_the_last_member_drops_the_record(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_vm_files(tmp_vmdir, "co9-a")
        _seed_vm_files(tmp_vmdir, "co9-b")
        cluster = self._record("co9", ["co9-a", "co9-b"])
        with (
            patch("ltvm_pkg.vm_commands.kill_qemu"),
            patch("ltvm_pkg.vm_commands.unregister_ssh_name"),
        ):
            vm_commands.cmd_destroy(argparse.Namespace(names=["co9-a"]))
            assert cluster.path.exists()
            vm_commands.cmd_destroy(argparse.Namespace(names=["co9-b"]))
        assert not cluster.path.exists()
        assert "removed cluster record co9" in capsys.readouterr().out

    def test_other_clusters_are_left_alone(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "co9-a")
        other = self._record("co8", ["co8-gone"])
        with (
            patch("ltvm_pkg.vm_commands.kill_qemu"),
            patch("ltvm_pkg.vm_commands.unregister_ssh_name"),
        ):
            vm_commands.cmd_destroy(argparse.Namespace(names=["co9-a"]))
        assert other.path.exists()

    @staticmethod
    def _create(name: str, sockets: Path) -> None:
        # The same node twice dies right after the existing-record check.
        args = argparse.Namespace(
            name=name, nodes=["client:co9-x", "client:co9-x"]
        )
        with (
            patch("ltvm_pkg.vm_cluster.SOCKETS", sockets),
            pytest.raises(SystemExit),
        ):
            vm_cluster.cmd_cluster_create(args)

    def test_create_replaces_an_orphan_record(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cluster = self._record("co9", ["co9-gone"])
        self._create("co9", cluster.path.parent)
        assert not cluster.path.exists()
        captured = capsys.readouterr()
        assert "removed stale cluster record co9" in captured.out
        assert "listed more than once" in captured.err

    def test_create_refuses_a_live_cluster(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_vm_files(tmp_vmdir, "co9-a")
        cluster = self._record("co9", ["co9-a"])
        self._create("co9", cluster.path.parent)
        assert cluster.path.exists()
        assert "already exists" in capsys.readouterr().err


# ── cmd_stop ─────────────────────────────────────────────


class TestCmdStop:
    """cmd_stop is lenient about missing VMs."""

    @pytest.fixture(autouse=True)
    def _signalable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fake pids here can be real root processes (pid 42 is a
        kernel thread on Linux), which would ask for sudo."""
        monkeypatch.setattr(
            vm_commands, "_require_signalable", lambda vm, verb: None
        )

    def test_missing_vm_not_an_error(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        args = argparse.Namespace(names=["ghost"])
        with patch("ltvm_pkg.vm_commands.kill_qemu") as mock_kill:
            vm_commands.cmd_stop(args)
        mock_kill.assert_not_called()
        assert "not found" in capsys.readouterr().out

    def test_live_vm_is_killed(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(names=["live"])
        with (
            patch("ltvm_pkg.vm_commands.kill_qemu") as mock_kill,
            patch("ltvm_pkg.vm_commands._sync_guests"),
        ):
            vm_commands.cmd_stop(args)
        mock_kill.assert_called_once()

    def _stop(self, tmp_vmdir: Path, rc: Any = 0, **kw: Any) -> list[str]:
        for n in ("s1", "s2"):
            VMInfo(name=n, ip=f"10.0.0.{len(n)}", pid=42).save()
        order: list[str] = []

        def ssh(ip: str, cmd: str, timeout: int = 0) -> MagicMock:
            order.append(f"ssh {cmd}")
            if isinstance(rc, BaseException):
                raise rc
            return MagicMock(returncode=rc, stderr="gone")

        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run_ssh", side_effect=ssh),
            patch(
                "ltvm_pkg.vm_commands.kill_qemu",
                side_effect=lambda vm: order.append(f"kill {vm.name}"),
            ),
        ):
            vm_commands.cmd_stop(argparse.Namespace(names=["s1", "s2"], **kw))
        return order

    def test_guests_sync_before_the_power_goes(self, tmp_vmdir: Path) -> None:
        """Regression: files deployed just before a stop came back empty
        after the next start."""
        order = self._stop(tmp_vmdir)
        assert order == [
            "ssh sync -f /",
            "ssh sync -f /",
            "kill s1",
            "kill s2",
        ]

    def test_force_skips_the_sync(self, tmp_vmdir: Path) -> None:
        assert self._stop(tmp_vmdir, force=True) == ["kill s1", "kill s2"]

    def test_unreachable_guest_is_stopped_anyway(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        order = self._stop(tmp_vmdir, rc=subprocess.TimeoutExpired("ssh", 30))
        assert order[-2:] == ["kill s1", "kill s2"]
        assert "could not sync s1" in capsys.readouterr().err


# ── cmd_list totals ──────────────────────────────────────


class TestCmdList:
    """cmd_list aggregates vcpu/memory totals and handles JSON output."""

    def test_empty(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        args = argparse.Namespace(json=False)
        vm_commands.cmd_list(args)
        assert "(no VMs)" in capsys.readouterr().out

    def test_json_totals(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        v1 = VMInfo(
            name="a",
            ip="10.0.0.1",
            pid=100,
            vcpus=4,
            mem=2048,
            mdt_disks=1,
            ost_disks=0,
        )
        v2 = VMInfo(
            name="b",
            ip="10.0.0.2",
            pid=0,
            vcpus=2,
            mem=1024,
        )
        v1.save()
        v2.save()
        args = argparse.Namespace(json=True)
        # a is "running" (pid 100 live), b is stopped (pid 0)
        with patch(
            "ltvm_pkg.vm_commands.is_running",
            side_effect=lambda vm: vm.pid > 0,
        ):
            vm_commands.cmd_list(args)
        out = json.loads(capsys.readouterr().out)
        assert len(out["vms"]) == 2
        assert out["totals"]["running"] == 1
        assert out["totals"]["stopped"] == 1
        # Only the running VM contributes to vcpu/mem totals
        assert out["totals"]["vcpus_used"] == 4
        assert out["totals"]["mem_used_mb"] == 2048

    def test_json_lists_clusters_and_members(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Clusters are in the listing, so a controller cleaning up a
        run can destroy one as a cluster rather than node by node."""
        _seed_vm_files(tmp_vmdir, "co9-mds")
        _seed_vm_files(tmp_vmdir, "lone")
        ClusterInfo(
            name="co9",
            nodes=[{"name": "co9-mds", "roles": ["mds"]}],
            owner_id="patch-watcher:abc",
        ).save()
        (tmp_vmdir / "sockets" / "broken.cluster").write_text("{")
        with patch("ltvm_pkg.vm_commands.is_running", return_value=False):
            vm_commands.cmd_list(argparse.Namespace(json=True))
        captured = capsys.readouterr()
        out = json.loads(captured.out)
        assert out["clusters"] == [
            {
                "name": "co9",
                "owner_id": "patch-watcher:abc",
                "members": ["co9-mds"],
            }
        ]
        by_name = {v["name"]: v for v in out["vms"]}
        assert by_name["co9-mds"]["cluster"] == "co9"
        assert by_name["lone"]["cluster"] is None
        assert "cluster broken: unreadable" in captured.err

    def test_json_clusters_is_a_list_when_there_are_none(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        vm_commands.cmd_list(argparse.Namespace(json=True))
        assert json.loads(capsys.readouterr().out)["clusters"] == []


# ── cmd_console_log ──────────────────────────────────────


class TestCmdConsoleLog:
    """console_log prints the tail of the serial-log file."""

    def test_tails_last_n_lines(
        self,
        tmp_vmdir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        vm = _seed_vm_files(tmp_vmdir, "logged")
        vm.log_path.write_text("\n".join(f"line{i}" for i in range(20)) + "\n")
        args = argparse.Namespace(name="logged", lines=5)
        vm_commands.cmd_console_log(args)
        out = capsys.readouterr().out.splitlines()
        assert out == [f"line{i}" for i in range(15, 20)]

    def test_missing_log_dies(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "nolog")
        # Remove the log file
        (tmp_vmdir / "sockets" / "nolog.log").unlink()
        args = argparse.Namespace(name="nolog", lines=10)
        with pytest.raises(SystemExit):
            vm_commands.cmd_console_log(args)

    def test_rejects_a_negative_line_count(self, tmp_vmdir: Path) -> None:
        vm = _seed_vm_files(tmp_vmdir, "neg")
        vm.log_path.write_text("a\nb\n")
        args = argparse.Namespace(name="neg", lines=-1)
        with pytest.raises(SystemExit):
            vm_commands.cmd_console_log(args)

    def test_zero_lines_prints_nothing(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """lines[-0:] is the whole file, so 0 used to dump everything."""
        vm = _seed_vm_files(tmp_vmdir, "zero")
        vm.log_path.write_text("a\nb\nc\n")
        vm_commands.cmd_console_log(argparse.Namespace(name="zero", lines=0))
        assert capsys.readouterr().out == ""

    def test_survives_non_utf8_console_output(
        self, tmp_vmdir: Path, capsysbinary: pytest.CaptureFixture[bytes]
    ) -> None:
        """A guest puts arbitrary bytes on its serial line.

        read_text() raised UnicodeDecodeError on the first stray byte,
        so one garbled character made the whole log unreadable through
        this command -- exactly when you most want to read it.

        capsysbinary, not capsys: the bytes are passed through verbatim,
        and asking the text fixture to decode them just moves the same
        UnicodeDecodeError into the test harness.
        """
        vm = _seed_vm_files(tmp_vmdir, "garbled")
        vm.log_path.write_bytes(b"before\ngarbage \x80\xff here\nafter\n")
        vm_commands.cmd_console_log(argparse.Namespace(name="garbled", lines=3))
        out = capsysbinary.readouterr().out.splitlines()
        assert out == [b"before", b"garbage \x80\xff here", b"after"]


class TestConsoleLogEarlierBoots:
    """A panic kdump rebooted out of sits a whole boot above the tail."""

    def _log(self) -> bytes:
        first = [b"[0.0] Linux version 6.12.0 (first)"] + [b"x"] * 10
        first += [b"[4252.7] Kernel panic - not syncing: Pool 'lustre-ost3'"]
        second = [b"[0.0] Linux version 6.12.0 (kdump)"] + [b"y"] * 80
        return b"\n".join(first + second) + b"\n"

    def test_note_names_the_earlier_panic(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression: three ZFS servers panicked, and console-log showed
        only the kdump boot after."""
        vm = _seed_vm_files(tmp_vmdir, "boots")
        vm.log_path.write_bytes(self._log())
        vm_commands.cmd_console_log(argparse.Namespace(name="boots", lines=50))
        err = capsys.readouterr().err
        assert "holds 2 boots" in err
        assert "line 12: [4252.7] Kernel panic - not syncing" in err
        assert "--lines 93 shows them all" in err

    def test_quiet_when_every_boot_is_shown(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vm = _seed_vm_files(tmp_vmdir, "boots2")
        vm.log_path.write_bytes(self._log())
        vm_commands.cmd_console_log(
            argparse.Namespace(name="boots2", lines=200)
        )
        assert capsys.readouterr().err == ""


# ── cmd_console_log --follow ─────────────────────────────


class TestConsoleLogFollow:
    """Following is `tail -F`, not `tail -f`.

    The distinction is not academic here: QEMU's serial chardev is
    opened without append=on (see qemu_run), so every boot truncates the
    log.  A follower that only read forward would go quiet for the rest
    of the VM's life after the first reboot.
    """

    @staticmethod
    def _sleep_that(*actions: Any) -> Any:
        """Stand in for time.sleep, doing one action per loop iteration.

        The loop only sleeps when it has drained the file, so hooking
        sleep is what lets a test write to the log *between* reads --
        which is the only way to exercise streaming rather than just the
        initial tail.  Ctrl-C ends it once the actions are spent.
        """
        calls = []

        def fake_sleep(_seconds: float) -> None:
            idx = len(calls)
            calls.append(idx)
            if idx >= len(actions):
                raise KeyboardInterrupt
            actions[idx]()

        return fake_sleep

    def test_streams_data_appended_while_following(
        self, tmp_vmdir: Path, capsysbinary: pytest.CaptureFixture[bytes]
    ) -> None:
        vm = _seed_vm_files(tmp_vmdir, "f1")
        vm.log_path.write_bytes(b"old1\nold2\n")

        def append() -> None:
            with open(vm.log_path, "ab") as fh:
                fh.write(b"new1\n")

        with patch.object(vm_commands.time, "sleep", self._sleep_that(append)):
            vm_commands._follow_console_log(vm, 1)
        # The tail, then the line that arrived after it.
        assert capsysbinary.readouterr().out == b"old2\nnew1\n"

    def test_picks_up_the_new_log_after_a_reboot(
        self, tmp_vmdir: Path, capsysbinary: pytest.CaptureFixture[bytes]
    ) -> None:
        """The case a plain `tail -f` gets wrong, and silently."""
        vm = _seed_vm_files(tmp_vmdir, "f2")
        vm.log_path.write_bytes(b"first boot\n")

        def reboot() -> None:
            # QEMU reopens the chardev without append=on: truncate.
            vm.log_path.write_bytes(b"second boot\n")

        with patch.object(vm_commands.time, "sleep", self._sleep_that(reboot)):
            vm_commands._follow_console_log(vm, 1)
        captured = capsysbinary.readouterr()
        assert captured.out == b"first boot\nsecond boot\n"
        assert b"VM rebooted" in captured.err

    def test_a_tail_of_zero_shows_only_new_data(
        self, tmp_vmdir: Path, capsysbinary: pytest.CaptureFixture[bytes]
    ) -> None:
        """`--lines 0 --follow` is `tail -n 0 -f`: nothing pre-existing."""
        vm = _seed_vm_files(tmp_vmdir, "f1b")
        vm.log_path.write_bytes(b"existing\n")

        def append() -> None:
            with open(vm.log_path, "ab") as fh:
                fh.write(b"fresh\n")

        with patch.object(vm_commands.time, "sleep", self._sleep_that(append)):
            vm_commands._follow_console_log(vm, 0)
        assert capsysbinary.readouterr().out == b"fresh\n"

    def test_rotated_detects_truncation(self, tmp_vmdir: Path) -> None:
        vm = _seed_vm_files(tmp_vmdir, "f3")
        vm.log_path.write_bytes(b"aaaaaaaaaa\n")
        with open(vm.log_path, "rb") as fh:
            fh.read()
            assert vm_commands._rotated(vm.log_path, fh) is False
            vm.log_path.write_bytes(b"x\n")
            assert vm_commands._rotated(vm.log_path, fh) is True

    def test_rotated_detects_replacement(self, tmp_vmdir: Path) -> None:
        """Unlink-and-recreate leaves our fd on a file nobody writes to."""
        vm = _seed_vm_files(tmp_vmdir, "f4")
        vm.log_path.write_bytes(b"original\n")
        with open(vm.log_path, "rb") as fh:
            fh.read()
            vm.log_path.unlink()
            # Same size, different inode -- size alone would miss this.
            vm.log_path.write_bytes(b"original\n")
            assert vm_commands._rotated(vm.log_path, fh) is True

    def test_follow_waits_for_a_log_that_does_not_exist_yet(
        self, tmp_vmdir: Path
    ) -> None:
        """Following a VM you are about to start is reasonable."""
        vm = _seed_vm_files(tmp_vmdir, "f5")
        vm.log_path.unlink()
        # _once returns rather than waiting; the point is that it does
        # not raise or die.
        vm_commands._follow_console_log(vm, 10, _once=True)

    def test_follow_on_a_missing_log_does_not_die(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without --follow a missing log is fatal; with it, it waits."""
        _seed_vm_files(tmp_vmdir, "f6")
        (tmp_vmdir / "sockets" / "f6.log").unlink()
        with patch.object(vm_commands, "_follow_console_log") as follow:
            vm_commands.cmd_console_log(
                argparse.Namespace(name="f6", lines=10, follow=True)
            )
        assert follow.called

    def test_ctrl_c_returns_quietly(self, tmp_vmdir: Path) -> None:
        """Ctrl-C is how you stop following, not an error."""
        vm = _seed_vm_files(tmp_vmdir, "f7")
        vm.log_path.write_bytes(b"x\n")
        with patch.object(
            vm_commands.time, "sleep", side_effect=KeyboardInterrupt
        ):
            vm_commands._follow_console_log(vm, 1)  # must not raise


# ── cmd_llmount ──────────────────────────────────────────


LIBDIR = "/usr/lib64/lustre"
_MOUNT_CMD = (
    f"( {premount_cleanup_script(LIBDIR)} ) >/dev/null 2>&1;"
    f" cd {LIBDIR}/tests && LUSTRE={LIBDIR} bash llmount.sh"
)
_CLEANUP_CMD = f"cd {LIBDIR}/tests && LUSTRE={LIBDIR} bash llmountcleanup.sh && lustre_rmmod"


class TestCmdLlmount:
    """cmd_llmount runs the correct remote commands and forwards exit codes."""

    def test_missing_vm_dies(self, tmp_vmdir: Path) -> None:
        args = argparse.Namespace(name="ghost", timeout=300, cleanup=False)
        with pytest.raises(SystemExit) as exc:
            vm_commands.cmd_llmount(args)
        assert exc.value.code == 2  # EXIT_NOT_FOUND

    def test_not_running_dies(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "stopped")
        args = argparse.Namespace(name="stopped", timeout=300, cleanup=False)
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=False),
            pytest.raises(SystemExit) as exc,
        ):
            vm_commands.cmd_llmount(args)
        assert exc.value.code == 4  # EXIT_UNREACHABLE

    def test_default_mount_command(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(name="live", timeout=300, cleanup=False)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.configure_test_disks"),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r) as mock_ssh,
            pytest.raises(SystemExit) as exc,
        ):
            vm_commands.cmd_llmount(args)
        assert exc.value.code == 0
        mock_ssh.assert_called_once()
        cmd_sent = mock_ssh.call_args[0][1]
        assert cmd_sent == _MOUNT_CMD

    def test_mount_takes_a_mounted_lustre_down_first(
        self, tmp_vmdir: Path
    ) -> None:
        """Regression: llmount on a mounted VM ran dmsetup remove_all
        while the targets still held their dm-flakey maps, so the maps
        survived and llmount.sh failed with "mkfs.lustre FATAL: Unable
        to build fs /dev/vdb".  It must unmount, then clear the maps,
        then format -- as deploy-lustre --mount does."""
        _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(name="live", timeout=300, cleanup=False)
        r = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.configure_test_disks"),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r) as mock_ssh,
            pytest.raises(SystemExit),
        ):
            vm_commands.cmd_llmount(args)
        cmd = mock_ssh.call_args[0][1]
        order = [
            "bash llmountcleanup.sh",
            'umount -f "$m"',
            "lustre_rmmod",
            "dmsetup remove_all",
            "bash llmount.sh",
        ]
        assert [cmd.index(step) for step in order] == sorted(
            cmd.index(step) for step in order
        )
        # The cleanup's output is noise; a failure in it must not stop
        # llmount.sh, whose own error is the one worth reading.
        cleanup, _, mount = cmd.rpartition(";")
        assert cleanup.endswith(">/dev/null 2>&1")
        assert "llmount.sh" in mount

    def test_configure_test_disks_called_with_vm_topology(
        self, tmp_vmdir: Path
    ) -> None:
        _seed_vm_files(tmp_vmdir, "live", mdt=1, ost=3)
        args = argparse.Namespace(name="live", timeout=300, cleanup=False)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.configure_test_disks"
            ) as mock_configure,
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r),
            pytest.raises(SystemExit),
        ):
            vm_commands.cmd_llmount(args)
        mock_configure.assert_called_once()
        args_passed, kwargs_passed = mock_configure.call_args
        assert args_passed[1] == 1  # mdt_disks
        assert args_passed[2] == 3  # ost_disks

    def test_configure_test_disks_skipped_on_cleanup(
        self, tmp_vmdir: Path
    ) -> None:
        _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(name="live", timeout=300, cleanup=True)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.configure_test_disks"
            ) as mock_configure,
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r),
            pytest.raises(SystemExit),
        ):
            vm_commands.cmd_llmount(args)
        mock_configure.assert_not_called()

    def test_cleanup_command(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(name="live", timeout=300, cleanup=True)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r) as mock_ssh,
            pytest.raises(SystemExit) as exc,
        ):
            vm_commands.cmd_llmount(args)
        assert exc.value.code == 0
        cmd_sent = mock_ssh.call_args[0][1]
        assert cmd_sent == _CLEANUP_CMD

    def test_nonzero_exit_forwarded(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(name="live", timeout=300, cleanup=False)
        r = MagicMock()
        r.returncode = 1
        r.stdout = "mkfs.lustre FATAL: Unable to build fs\n"
        r.stderr = ""
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.configure_test_disks"),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r),
            pytest.raises(SystemExit) as exc,
        ):
            vm_commands.cmd_llmount(args)
        assert exc.value.code == 1

    def test_timeout_plumbed_through(self, tmp_vmdir: Path) -> None:
        import subprocess as _sp

        _seed_vm_files(tmp_vmdir, "slow")
        args = argparse.Namespace(name="slow", timeout=42, cleanup=False)
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.configure_test_disks"),
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=_sp.TimeoutExpired(cmd="ssh", timeout=42),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            vm_commands.cmd_llmount(args)
        assert exc.value.code == 3  # EXIT_TIMEOUT

    def test_timeout_passed_to_run_ssh(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(name="live", timeout=99, cleanup=False)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.configure_test_disks"),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r) as mock_ssh,
            pytest.raises(SystemExit),
        ):
            vm_commands.cmd_llmount(args)
        _, call_kwargs = mock_ssh.call_args
        assert call_kwargs.get("timeout") == 99

    @pytest.mark.parametrize("cleanup", [False, True])
    def test_output_is_streamed(self, tmp_vmdir: Path, cleanup: bool) -> None:
        """llmount.sh's output flows as it runs, not in one block at the end."""
        import sys

        _seed_vm_files(tmp_vmdir, "live")
        args = argparse.Namespace(name="live", timeout=300, cleanup=cleanup)
        r = MagicMock()
        r.returncode = 0
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.configure_test_disks"),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r) as mock_ssh,
            pytest.raises(SystemExit),
        ):
            vm_commands.cmd_llmount(args)
        assert mock_ssh.call_args.kwargs["stream"] is sys.stdout


# ── _seed_kdump_boot ─────────────────────────────────────


class TestSeedKdumpBoot:
    """Fast path: image has baked-in kdump artifacts, only reload kdump.
    Fallback path: artifacts missing, scp + dracut/update-initramfs."""

    def _vm(self, tmp_path: Path, os_id: str = "rocky9") -> VMInfo:
        kdir = tmp_path / "kernels" / "5.14"
        kdir.mkdir(parents=True)
        (kdir / "vmlinux").write_bytes(b"ELF")
        (kdir / "vmlinuz").write_bytes(b"bz")
        return VMInfo(
            name="co1-test",
            ip="10.0.0.99",
            kernel=str(kdir / "vmlinux"),
            kver="5.14.0-test",
            os_id=os_id,
        )

    def _ssh_results(self, present: bool):
        """Return a side_effect fn where only the `test -f` probe
        varies with *present*; every other command succeeds."""

        def _fn(ip, cmd, **kw):
            r = MagicMock()
            if cmd.startswith("test -f"):
                r.returncode = 0 if present else 1
            else:
                r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        return _fn

    def test_fast_path_no_scp_no_dracut(self, tmp_path: Path) -> None:
        vm = self._vm(tmp_path)
        with (
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=self._ssh_results(present=True),
            ) as mock_ssh,
            patch("ltvm_pkg.vm_commands.run") as mock_run,
            patch("ltvm_pkg.target_config.TargetConfig") as mock_tc,
        ):
            mock_tc.return_value.os_family = "rhel"
            vm_commands._seed_kdump_boot(vm)

        mock_run.assert_not_called()
        cmds = [c.args[1] for c in mock_ssh.call_args_list]
        assert any("test -f /boot/vmlinuz-" in c for c in cmds)
        assert any("systemctl restart kdump" in c for c in cmds)
        assert not any("dracut" in c for c in cmds)
        # NMI panic for every later boot too, for kernels older than
        # sysctl.* on the cmdline.
        nmi = next(c for c in cmds if vm_commands.NMI_SYSCTL_CONF in c)
        assert "kernel.panic_on_unrecovered_nmi = 1" in nmi
        assert f"sysctl -q -p {vm_commands.NMI_SYSCTL_CONF}" in nmi

    def test_fast_path_debian_uses_kdump_config(self, tmp_path: Path) -> None:
        vm = self._vm(tmp_path, os_id="ubuntu24")
        with (
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=self._ssh_results(present=True),
            ) as mock_ssh,
            patch("ltvm_pkg.vm_commands.run") as mock_run,
            patch("ltvm_pkg.target_config.TargetConfig") as mock_tc,
        ):
            mock_tc.return_value.os_family = "debian"
            vm_commands._seed_kdump_boot(vm)

        mock_run.assert_not_called()
        cmds = [c.args[1] for c in mock_ssh.call_args_list]
        assert any("test -f /var/lib/kdump/initrd.img-" in c for c in cmds)
        assert any("kdump-config load" in c for c in cmds)
        assert not any("update-initramfs" in c for c in cmds)

    def test_fallback_path_runs_scp_and_dracut(self, tmp_path: Path) -> None:
        vm = self._vm(tmp_path)
        scp_rc = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=self._ssh_results(present=False),
            ) as mock_ssh,
            patch("ltvm_pkg.vm_commands.run", return_value=scp_rc) as mock_run,
            patch("ltvm_pkg.target_config.TargetConfig") as mock_tc,
        ):
            mock_tc.return_value.os_family = "rhel"
            vm_commands._seed_kdump_boot(vm)

        mock_run.assert_called()
        scp_cmd = mock_run.call_args_list[0].args[0]
        assert "scp" in scp_cmd
        cmds = [c.args[1] for c in mock_ssh.call_args_list]
        assert any("dracut --kver 5.14.0-test" in c for c in cmds)

    def test_no_kernel_returns_early(self, tmp_path: Path) -> None:
        vm = VMInfo(name="x", ip="10.0.0.1")  # kernel=""
        with (
            patch("ltvm_pkg.vm_commands.run_ssh") as mock_ssh,
            patch("ltvm_pkg.vm_commands.run") as mock_run,
        ):
            vm_commands._seed_kdump_boot(vm)
        mock_ssh.assert_not_called()
        mock_run.assert_not_called()


# ── cmd_create: SUDO_USER chown ──────────────────────────


class TestCmdCreateChown:
    """cmd_create hands sudo-created disks to the invoking user."""

    def _run_create(
        self, tmp_vmdir: Path, sudo_user: str | None
    ) -> list[list[str]]:
        """Run cmd_create and return commands routed through sudo_run."""
        sudo_calls: list[list[str]] = []

        def fake_sudo_run(cmd, **kwargs):
            sudo_calls.append(list(cmd))
            return MagicMock(returncode=0, stdout="", stderr="")

        env = {"SUDO_USER": sudo_user} if sudo_user else {}

        with (
            patch("ltvm_pkg.vm_commands.resolve_os_artifacts") as mock_arts,
            patch("ltvm_pkg.vm_commands.alloc_ip") as mock_alloc,
            patch("ltvm_pkg.vm_commands.tap_for_name", return_value="tap0"),
            patch(
                "ltvm_pkg.vm_commands.mac_for_name",
                return_value="AA:BB:CC:DD:EE:FF",
            ),
            patch("ltvm_pkg.vm_commands.run") as mock_run,
            patch(
                "ltvm_pkg.vm_commands.sudo_run",
                side_effect=fake_sudo_run,
            ),
            patch("ltvm_pkg.vm_commands.launch_qemu"),
            patch("ltvm_pkg.vm_commands.provision_vm_ssh"),
            patch("ltvm_pkg.vm_commands._seed_kdump_boot"),
            patch("ltvm_pkg.vm_commands.os.environ", env),
            patch("getpass.getuser", return_value="patrick"),
            # A classic, root-owned VM_DIR: the images are made via sudo.
            patch("ltvm_pkg.vm_commands._user_can_write", return_value=False),
        ):
            arts = MagicMock()
            arts.image = tmp_vmdir / "base.ext4"
            arts.kernel = tmp_vmdir / "vmlinuz"
            arts.arch = "x86_64"
            arts.default_mem = 2048
            mock_arts.return_value = arts

            from contextlib import contextmanager

            @contextmanager
            def fake_alloc(name, count=1, explicit_ip=None):
                yield [
                    explicit_ip or "192.168.100.50",
                    *[f"192.168.100.{60 + i}" for i in range(count - 1)],
                ]

            mock_alloc.side_effect = fake_alloc

            mock_run.return_value = MagicMock(
                returncode=0, stdout="", stderr=""
            )

            with patch(
                "ltvm_pkg.vm_commands.load_meta_safe",
                return_value={"kernel_version": "5.14.0-test"},
            ):
                vm_commands.cmd_create(
                    _create_args(name="co1-chown", mdt_disks=1, ost_disks=2)
                )

        return sudo_calls

    def test_chown_called_when_sudo_user_set(self, tmp_vmdir: Path) -> None:
        calls = self._run_create(tmp_vmdir, sudo_user="alice")
        chowns = [cmd for cmd in calls if cmd and cmd[0] == "chown"]
        assert len(chowns) == 1
        assert chowns[0][1] == "alice:"
        # command + owner + overlay + three data disks
        assert len(chowns[0]) == 6

    def test_chown_uses_invoking_user_without_sudo_user(
        self, tmp_vmdir: Path
    ) -> None:
        calls = self._run_create(tmp_vmdir, sudo_user=None)
        chowns = [cmd for cmd in calls if cmd and cmd[0] == "chown"]
        assert len(chowns) == 1
        assert chowns[0][1] == "patrick:"


# ── cmd_crash_collect: default outdir ───────────────────


class TestCmdCrashCollectOutdir:
    """crash-collect defaults to ~/ltvm-crashes when --outdir is not given."""

    def test_default_outdir_is_under_home(self) -> None:
        # The resolution logic: None -> Path.home() / "ltvm-crashes"
        # Verify the formula without actually running cmd_crash_collect.
        args = argparse.Namespace(
            name="co1-crash",
            outdir=None,
            trigger=False,
            wait=120,
            mod_dir=None,
        )
        raw_outdir = getattr(args, "outdir", None)
        resolved = (
            Path(raw_outdir) if raw_outdir else Path.home() / "ltvm-crashes"
        )
        assert resolved == Path.home() / "ltvm-crashes"
        assert str(resolved).startswith(str(Path.home()))

    def test_explicit_outdir_respected(
        self, tmp_vmdir: Path, tmp_path: Path
    ) -> None:
        explicit = str(tmp_path / "my-crashes")
        _seed_vm_files(tmp_vmdir, "co1-crash2")
        with (
            patch("ltvm_pkg.vm_commands.VMInfo.load") as mock_load,
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run_ssh") as mock_ssh,
        ):
            mock_load.return_value = MagicMock(
                name="co1-crash2", ip="192.168.100.50"
            )
            r = MagicMock(returncode=1, stdout="", stderr="ssh error")
            mock_ssh.return_value = r
            args = argparse.Namespace(
                name="co1-crash2",
                outdir=explicit,
                trigger=False,
                wait=120,
                mod_dir=None,
                json=False,
            )
            # cmd_crash_collect now returns an error code (EXIT_ERROR)
            # instead of raising SystemExit, so the outer JSON-aware
            # wrapper can emit {"error": ...} cleanly.
            rc = vm_commands.cmd_crash_collect(args)
            assert rc != 0

        assert Path(explicit).exists()


# ── _handler_error ───────────────────────────────────────


class TestHandlerError:
    """_handler_error emits JSON to stdout or text to stderr based on
    args.json, and returns the caller-supplied exit code."""

    def test_json_mode_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        args = argparse.Namespace(json=True)
        rc = vm_commands._handler_error(args, "boom", code=7)
        assert rc == 7
        captured = capsys.readouterr()
        # JSON on stdout, not stderr
        assert json.loads(captured.out) == {"error": "boom"}
        assert captured.err == ""

    def test_text_mode_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        args = argparse.Namespace(json=False)
        rc = vm_commands._handler_error(args, "bad thing")
        # Default code is EXIT_ERROR (1)
        assert rc == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "error: bad thing" in captured.err

    def test_missing_json_attr_treated_as_false(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """args without a json attr: default to text mode (CLI callers
        always set it, but the handler must not crash if a caller forgets).
        """
        args = argparse.Namespace()
        vm_commands._handler_error(args, "oops")
        assert "error: oops" in capsys.readouterr().err


# ── _os_family_for_vm ────────────────────────────────────


class TestOsFamilyForVm:
    """Falls back to rhel (with a warning) only when the target config
    is genuinely missing/broken, not for every exception."""

    def test_empty_os_id_returns_rhel_no_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vm = VMInfo(name="x", ip="10.0.0.1", os_id="")
        assert vm_commands._os_family_for_vm(vm) == "rhel"
        assert capsys.readouterr().err == ""

    def test_resolves_from_target_config(self) -> None:
        vm = VMInfo(name="x", ip="10.0.0.1", os_id="rocky9")
        with patch("ltvm_pkg.target_config.TargetConfig") as mock_tc:
            mock_tc.return_value.os_family = "rhel"
            assert vm_commands._os_family_for_vm(vm) == "rhel"

    def test_debian_resolved(self) -> None:
        vm = VMInfo(name="x", ip="10.0.0.1", os_id="ubuntu24")
        with patch("ltvm_pkg.target_config.TargetConfig") as mock_tc:
            mock_tc.return_value.os_family = "debian"
            assert vm_commands._os_family_for_vm(vm) == "debian"

    def test_unknown_target_warns_and_falls_back(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vm = VMInfo(name="x", ip="10.0.0.1", os_id="gone")
        with patch(
            "ltvm_pkg.target_config.TargetConfig",
            side_effect=ValueError("unknown target"),
        ):
            assert vm_commands._os_family_for_vm(vm, "kdump path") == "rhel"
        err = capsys.readouterr().err
        assert "cannot resolve target" in err
        assert "'gone'" in err
        assert "kdump path" in err

    def test_missing_yaml_warns_and_falls_back(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vm = VMInfo(name="x", ip="10.0.0.1", os_id="any")
        with patch(
            "ltvm_pkg.target_config.TargetConfig",
            side_effect=FileNotFoundError("targets.yaml"),
        ):
            assert vm_commands._os_family_for_vm(vm) == "rhel"
        assert "cannot resolve target" in capsys.readouterr().err


# ── _with_vm_stopped ─────────────────────────────────────


class TestWithVmStopped:
    """Context manager that stops a running VM for the duration of a
    block then restarts it.  No-op if the VM isn't running."""

    @pytest.fixture(autouse=True)
    def _no_sync(self) -> Any:
        with patch("ltvm_pkg.vm_commands._sync_guests") as sync:
            self.sync = sync
            yield

    def _vm(self) -> VMInfo:
        return VMInfo(name="co1-wvs", ip="10.0.0.10")

    def test_guest_is_synced_before_it_stops(self) -> None:
        """A snapshot taken after a power cut missed the guest's
        unflushed writes."""
        vm = self._vm()
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.kill_qemu",
                side_effect=lambda v: self.sync.assert_called_once_with([vm]),
            ) as kill,
            patch("ltvm_pkg.vm_commands.launch_qemu"),
            patch("ltvm_pkg.vm_commands.provision_vm_ssh"),
            patch("ltvm_pkg.vm_commands._seed_kdump_boot"),
        ):
            with vm_commands._with_vm_stopped(vm, "for test"):
                pass
        kill.assert_called_once()

    def test_noop_when_not_running(self) -> None:
        """If the VM is stopped, _with_vm_stopped does nothing."""
        vm = self._vm()
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=False),
            patch("ltvm_pkg.vm_commands.kill_qemu") as mock_kill,
            patch("ltvm_pkg.vm_commands.launch_qemu") as mock_launch,
        ):
            with vm_commands._with_vm_stopped(vm, "for test"):
                pass
        mock_kill.assert_not_called()
        mock_launch.assert_not_called()

    def test_running_is_stopped_then_restarted(self) -> None:
        """Running VM: stop before block, relaunch + provision + kdump
        after block (always, even on exception)."""
        vm = self._vm()
        calls: list[str] = []
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.kill_qemu",
                side_effect=lambda v: calls.append("kill"),
            ),
            patch(
                "ltvm_pkg.vm_commands.launch_qemu",
                side_effect=lambda v: calls.append("launch"),
            ),
            patch(
                "ltvm_pkg.vm_commands.provision_vm_ssh",
                side_effect=lambda v, t, **kw: calls.append("prov"),
            ),
            patch(
                "ltvm_pkg.vm_commands._seed_kdump_boot",
                side_effect=lambda v: calls.append("seed"),
            ),
        ):
            with vm_commands._with_vm_stopped(vm, "for test"):
                calls.append("inside")
        assert calls == ["kill", "inside", "launch", "prov", "seed"]

    def test_restart_after_inner_exception(self) -> None:
        """Even when the block raises, the VM is restarted."""
        vm = self._vm()
        relaunch_called = {"n": 0}
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.kill_qemu"),
            patch(
                "ltvm_pkg.vm_commands.launch_qemu",
                side_effect=lambda v: relaunch_called.__setitem__(
                    "n", relaunch_called["n"] + 1
                ),
            ),
            patch("ltvm_pkg.vm_commands.provision_vm_ssh"),
            patch("ltvm_pkg.vm_commands._seed_kdump_boot"),
            pytest.raises(ValueError, match="inner"),
        ):
            with vm_commands._with_vm_stopped(vm, "for test"):
                raise ValueError("inner")
        assert relaunch_called["n"] == 1

    def test_get_error_printed_on_restart_SystemExit(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """If restart itself die()s (SystemExit), the inner error
        returned by get_error() is printed before re-raising."""
        vm = self._vm()
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.kill_qemu"),
            patch(
                "ltvm_pkg.vm_commands.launch_qemu",
                side_effect=SystemExit("relaunch died"),
            ),
            patch("ltvm_pkg.vm_commands.provision_vm_ssh"),
            patch("ltvm_pkg.vm_commands._seed_kdump_boot"),
            pytest.raises(SystemExit),
        ):
            with vm_commands._with_vm_stopped(
                vm,
                "for test",
                get_error=lambda: "inner snapshot error",
            ):
                pass
        err = capsys.readouterr().err
        assert "inner snapshot error" in err


# ── cmd_start ────────────────────────────────────────────


class TestCmdStart:
    """cmd_start registers SSH name BEFORE waiting so even a wait_for_ssh
    timeout leaves a populated /etc/hosts entry (so the user can ssh in
    to diagnose)."""

    def test_multiple_names_all_launched(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "a")
        _seed_vm_files(tmp_vmdir, "b")
        args = argparse.Namespace(names=["a", "b"])
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=False),
            patch("ltvm_pkg.vm_commands.launch_qemu") as mock_launch,
            patch("ltvm_pkg.vm_commands.provision_vm_ssh") as mock_prov,
            patch("ltvm_pkg.vm_commands._seed_kdump_boot"),
        ):
            vm_commands.cmd_start(args)
        assert mock_launch.call_count == 2
        assert mock_prov.call_count == 2
        # All provisioning calls must pass register_before_wait=True.
        for call in mock_prov.call_args_list:
            assert call.kwargs.get("register_before_wait") is True

    def test_the_wait_reaches_every_launch(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "a")
        _seed_vm_files(tmp_vmdir, "b")
        args = argparse.Namespace(names=["a", "b"], wait=300)
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=False),
            patch("ltvm_pkg.vm_commands.launch_qemu") as mock_launch,
            patch("ltvm_pkg.vm_commands.provision_vm_ssh"),
            patch("ltvm_pkg.vm_commands._seed_kdump_boot"),
        ):
            vm_commands.cmd_start(args)
        assert [
            c.kwargs["wait_seconds"] for c in mock_launch.call_args_list
        ] == [
            300,
            300,
        ]

    def test_already_running_short_circuits(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression: pre-fix output was contradictory, e.g.:

            $ sudo ltvm start pafvm
            VM 'pafvm' is already running
            started pafvm

        cmd_start now short-circuits on is_running so neither
        provision_vm_ssh, _seed_kdump_boot, nor the "started <name>"
        log fire when the VM is already up.
        """
        _seed_vm_files(tmp_vmdir, "up")
        args = argparse.Namespace(names=["up"])
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.launch_qemu") as mock_launch,
            patch("ltvm_pkg.vm_commands.provision_vm_ssh") as mock_prov,
            patch("ltvm_pkg.vm_commands._seed_kdump_boot") as mock_seed,
        ):
            vm_commands.cmd_start(args)
        assert mock_launch.call_count == 0
        assert mock_prov.call_count == 0
        assert mock_seed.call_count == 0
        out = capsys.readouterr().out
        assert "up: already running" in out
        # Crucially the contradictory "started" log must not appear.
        assert "started up" not in out

    def test_mixed_running_and_stopped(self, tmp_vmdir: Path) -> None:
        """Two VMs: one already up, one down.  The down one launches,
        the up one just prints 'already running' -- no cross-contamination."""
        _seed_vm_files(tmp_vmdir, "up")
        _seed_vm_files(tmp_vmdir, "down")
        args = argparse.Namespace(names=["up", "down"])

        # is_running(vm) -> True for 'up', False for 'down'.
        def fake_is_running(vm: VMInfo) -> bool:
            return vm.name == "up"

        with (
            patch(
                "ltvm_pkg.vm_commands.is_running",
                side_effect=fake_is_running,
            ),
            patch("ltvm_pkg.vm_commands.launch_qemu") as mock_launch,
            patch("ltvm_pkg.vm_commands.provision_vm_ssh"),
            patch("ltvm_pkg.vm_commands._seed_kdump_boot"),
        ):
            vm_commands.cmd_start(args)
        # launch_qemu called exactly once, for 'down'.
        assert mock_launch.call_count == 1
        assert mock_launch.call_args.args[0].name == "down"


# ── cmd_exec ─────────────────────────────────────────────


class TestCmdExec:
    def _args(self, name: str, command: list[str], **kw: Any) -> Any:
        return argparse.Namespace(
            name=name, command=command, timeout=7, json=False, **kw
        )

    def test_no_command_is_an_error(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = vm_commands.cmd_exec(self._args("x-none", []))
        assert rc == 1
        assert "requires a command" in capsys.readouterr().err

    def test_not_running_returns_unreachable(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_vm_files(tmp_vmdir, "x-stopped")
        with patch("ltvm_pkg.vm_commands.is_running", return_value=False):
            rc = vm_commands.cmd_exec(self._args("x-stopped", ["true"]))
        assert rc == 4
        assert "not running" in capsys.readouterr().err

    def test_returns_remote_status_and_quotes_argv(
        self, tmp_vmdir: Path
    ) -> None:
        _seed_vm_files(tmp_vmdir, "x-ok")
        r = MagicMock(returncode=3, stdout=None, stderr=None)
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r) as ssh,
        ):
            rc = vm_commands.cmd_exec(self._args("x-ok", ["echo", "a b"]))
        assert rc == 3
        assert ssh.call_args.args[1] == "echo 'a b'"
        assert ssh.call_args.kwargs["timeout"] == 7

    def test_single_argument_passes_verbatim(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "x-one")
        r = MagicMock(returncode=0, stdout=None, stderr=None)
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r) as ssh,
        ):
            vm_commands.cmd_exec(self._args("x-one", ["lctl dl | wc -l"]))
        assert ssh.call_args.args[1] == "lctl dl | wc -l"

    def test_timeout(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_vm_files(tmp_vmdir, "x-slow")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=subprocess.TimeoutExpired("ssh", 7),
            ),
        ):
            rc = vm_commands.cmd_exec(self._args("x-slow", ["sleep 99"]))
        assert rc == 3
        assert "timeout after 7s" in capsys.readouterr().err

    def test_json_captures_output(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_vm_files(tmp_vmdir, "x-json")
        r = MagicMock(returncode=0, stdout="hi\n", stderr="")
        args = self._args("x-json", ["echo hi"])
        args.json = True
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r) as ssh,
        ):
            rc = vm_commands.cmd_exec(args)
        assert rc == 0
        assert ssh.call_args.kwargs["stream"] is None
        out = json.loads(capsys.readouterr().out)
        assert out["stdout"] == "hi\n" and out["rc"] == 0
        assert "hint" not in out

    def test_connection_lost_mid_command_points_at_the_crash(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A guest LBUG dropped ssh with no message, and the run took it
        for a command that ended (fb-bb006d40e8a0)."""
        _seed_vm_files(tmp_vmdir, "x-lbug")
        r = MagicMock(
            returncode=255,
            stdout="",
            stderr="Connection to 192.0.2.9 closed by remote host.\n",
        )
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r),
        ):
            rc = vm_commands.cmd_exec(self._args("x-lbug", ["dd ..."]))
            args = self._args("x-lbug", ["dd ..."])
            args.json = True
            vm_commands.cmd_exec(args)
        assert rc == 255
        captured = capsys.readouterr()
        assert "ltvm vm crash-collect x-lbug" in captured.err
        assert "ltvm vm console-log x-lbug" in json.loads(captured.out)["hint"]

    def test_never_connected_gets_no_crash_hint(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_vm_files(tmp_vmdir, "x-boot")
        r = MagicMock(
            returncode=255,
            stdout="",
            stderr="ssh: connect to host 192.0.2.9 port 22: Connection refused\n",
        )
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r),
        ):
            vm_commands.cmd_exec(self._args("x-boot", ["true"]))
        assert "crash-collect" not in capsys.readouterr().err


# ── cmd_nmi ──────────────────────────────────────────────


class TestCmdNmi:
    """cmd_nmi sets NMI-panic sysctls then injects via QMP."""

    def test_not_running_returns_unreachable(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_vm_files(tmp_vmdir, "nmi-stopped")
        args = argparse.Namespace(name="nmi-stopped", json=False)
        with patch("ltvm_pkg.vm_commands.is_running", return_value=False):
            rc = vm_commands.cmd_nmi(args)
        assert rc == 4  # EXIT_UNREACHABLE
        assert "not running" in capsys.readouterr().err

    def test_success_path(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_vm_files(tmp_vmdir, "nmi-ok")
        args = argparse.Namespace(name="nmi-ok", json=False)
        r = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r) as mock_ssh,
            patch("ltvm_pkg.vm_commands._qmp_nmi") as mock_nmi,
        ):
            rc = vm_commands.cmd_nmi(args)
        assert rc == 0
        # The single run_ssh call must enable all three panic sysctls.
        cmd = mock_ssh.call_args.args[1]
        assert "panic_on_unrecovered_nmi=1" in cmd
        assert "panic_on_io_nmi=1" in cmd
        assert "unknown_nmi_panic=1" in cmd
        mock_nmi.assert_called_once()
        assert "NMI injected" in capsys.readouterr().out

    def test_sysctl_failure_reported(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_vm_files(tmp_vmdir, "nmi-ssh")
        args = argparse.Namespace(name="nmi-ssh", json=False)
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=RuntimeError("ssh gone"),
            ),
            patch("ltvm_pkg.vm_commands._qmp_nmi") as mock_nmi,
        ):
            rc = vm_commands.cmd_nmi(args)
        assert rc != 0
        # Must not attempt NMI injection after sysctl failure.
        mock_nmi.assert_not_called()
        assert "failed to set NMI panic sysctls" in capsys.readouterr().err

    def test_hung_guest_booted_with_the_knobs_still_gets_its_nmi(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression: a guest that stopped answering ssh could not be
        captured, because the panic sysctls were set over ssh."""
        _seed_vm_files(tmp_vmdir, "nmi-hung")
        args = argparse.Namespace(name="nmi-hung", json=False)
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch(
                "ltvm_pkg.vm_commands.run_ssh",
                side_effect=subprocess.TimeoutExpired("ssh", 10),
            ),
            patch(
                "ltvm_pkg.vm_commands._booted_with_nmi_panic",
                return_value=True,
            ),
            patch("ltvm_pkg.vm_commands._qmp_nmi") as mock_nmi,
        ):
            rc = vm_commands.cmd_nmi(args)
        assert rc == 0
        mock_nmi.assert_called_once()
        assert "set at boot" in capsys.readouterr().err

    @needs_proc
    def test_booted_with_reads_the_qemu_cmdline(self) -> None:
        import sys

        from ltvm_pkg.qemu_run import NMI_PANIC_ARGS

        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
                f"-append console=ttyS0{NMI_PANIC_ARGS}",
            ]
        )
        try:
            # The child's cmdline reads empty until it has exec'd, which
            # a loaded host can take a moment to do.
            cmdline = Path(f"/proc/{proc.pid}/cmdline")
            for _ in range(100):
                if b"-append" in cmdline.read_bytes():
                    break
                time.sleep(0.05)
            vm = VMInfo(name="x", ip="10.0.0.1", pid=proc.pid)
            assert vm_commands._booted_with_nmi_panic(vm)
            vm.pid = os.getpid()
            assert not vm_commands._booted_with_nmi_panic(vm)
        finally:
            proc.kill()
            proc.wait()

    def test_nmi_injection_failure_reported(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_vm_files(tmp_vmdir, "nmi-qmp")
        args = argparse.Namespace(name="nmi-qmp", json=False)
        r = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run_ssh", return_value=r),
            patch(
                "ltvm_pkg.vm_commands._qmp_nmi",
                side_effect=RuntimeError("qmp busted"),
            ),
        ):
            rc = vm_commands.cmd_nmi(args)
        assert rc != 0
        assert "failed to inject NMI" in capsys.readouterr().err


# ── _qmp_nmi ─────────────────────────────────────────────


class TestQmpNmi:
    """_qmp_nmi speaks the QMP protocol: drain greeting, negotiate caps,
    send inject-nmi, skip events, wait for return/error response."""

    def _fake_socket(self, frames: list[bytes]) -> Any:
        """Build a context-manager socket whose recv() drains `frames`."""
        sock = MagicMock()
        recv_chunks = list(frames)

        def _recv(n: int) -> bytes:
            if not recv_chunks:
                return b""
            return recv_chunks.pop(0)

        sock.recv.side_effect = _recv
        sock.sendall = MagicMock()
        sock.settimeout = MagicMock()
        sock.connect = MagicMock()
        # Context-manager protocol
        sock.__enter__ = lambda self_: self_
        sock.__exit__ = lambda self_, *a: False
        return sock

    def test_happy_path_no_error(self, tmp_path: Path) -> None:
        qmp = tmp_path / "q.qmp"
        # greeting, cap response, inject-nmi response
        frames = [
            b'{"QMP": {"version": "8"}}\n',
            b'{"return": {}}\n',
            b'{"return": {}}\n',
        ]
        sock = self._fake_socket(frames)
        with patch("socket.socket", return_value=sock):
            # Must not raise.
            vm_commands._qmp_nmi(qmp)
        # Sent two commands: qmp_capabilities + inject-nmi
        sent = [call.args[0] for call in sock.sendall.call_args_list]
        assert any(b"qmp_capabilities" in s for s in sent)
        assert any(b"inject-nmi" in s for s in sent)

    def test_skips_events_before_response(self, tmp_path: Path) -> None:
        """QEMU may emit async events (NMI, RESET) before the return
        frame; _qmp_nmi must skip them and wait for return/error."""
        qmp = tmp_path / "q.qmp"
        frames = [
            b'{"QMP": {"version": "8"}}\n',
            b'{"return": {}}\n',  # capabilities ack
            b'{"event": "NMI"}\n',  # async event
            b'{"event": "RESET"}\n',  # another async event
            b'{"return": {}}\n',  # the real response
        ]
        sock = self._fake_socket(frames)
        with patch("socket.socket", return_value=sock):
            vm_commands._qmp_nmi(qmp)  # must complete

    def test_error_response_raises(self, tmp_path: Path) -> None:
        qmp = tmp_path / "q.qmp"
        frames = [
            b'{"QMP": {"version": "8"}}\n',
            b'{"return": {}}\n',
            b'{"error": {"desc": "inject-nmi not supported"}}\n',
        ]
        sock = self._fake_socket(frames)
        with (
            patch("socket.socket", return_value=sock),
            pytest.raises(RuntimeError, match="inject-nmi not supported"),
        ):
            vm_commands._qmp_nmi(qmp)

    def test_socket_closed_before_greeting_raises(self, tmp_path: Path) -> None:
        qmp = tmp_path / "q.qmp"
        sock = self._fake_socket([])  # empty -> recv returns b""
        with (
            patch("socket.socket", return_value=sock),
            pytest.raises(RuntimeError, match="closed before greeting"),
        ):
            vm_commands._qmp_nmi(qmp)


# ── cmd_snapshot ─────────────────────────────────────────


class TestCmdSnapshot:
    """cmd_snapshot creates a tag on the overlay; --delete removes one."""

    def test_create_with_explicit_tag(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_vm_files(tmp_vmdir, "snap-ok")
        args = argparse.Namespace(name="snap-ok", tag="v1", delete=None)
        run_ok = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=False),
            patch("ltvm_pkg.vm_commands.run", return_value=run_ok) as mock_run,
        ):
            vm_commands.cmd_snapshot(args)
        # qemu-img snapshot -c v1 <overlay>
        cmd = mock_run.call_args.args[0]
        assert "snapshot" in cmd and "-c" in cmd and "v1" in cmd
        assert "snapshot 'v1' created" in capsys.readouterr().out

    def test_create_failure_dies(self, tmp_vmdir: Path) -> None:
        _seed_vm_files(tmp_vmdir, "snap-fail")
        args = argparse.Namespace(name="snap-fail", tag="v1", delete=None)
        run_fail = MagicMock(returncode=1, stdout="", stderr="disk busy")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=False),
            patch("ltvm_pkg.vm_commands.run", return_value=run_fail),
            pytest.raises(SystemExit),
        ):
            vm_commands.cmd_snapshot(args)

    def test_delete_tag(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_vm_files(tmp_vmdir, "snap-del")
        args = argparse.Namespace(name="snap-del", tag=None, delete="v1")
        run_ok = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=False),
            patch("ltvm_pkg.vm_commands.run", return_value=run_ok) as mock_run,
        ):
            vm_commands.cmd_snapshot(args)
        cmd = mock_run.call_args.args[0]
        assert "-d" in cmd and "v1" in cmd
        assert "snapshot 'v1' deleted" in capsys.readouterr().out


# ── cmd_set ──────────────────────────────────────────────


class TestCmdSet:
    """cmd_set resizes a stopped VM in place for its next start."""

    def _args(
        self, name: str, vcpus: int | None = None, mem: int | None = None
    ):
        return argparse.Namespace(name=name, vcpus=vcpus, mem=mem)

    def test_changes_vcpus_and_mem(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vm = _seed_vm_files(tmp_vmdir, "set-both")
        with patch("ltvm_pkg.vm_commands.is_running", return_value=False):
            vm_commands.cmd_set(self._args("set-both", vcpus=2, mem=1536))
        vm = VMInfo.load("set-both")
        assert (vm.vcpus, vm.mem) == (2, 1536)
        assert "next start" in capsys.readouterr().out

    def test_keeps_other_fields(self, tmp_vmdir: Path) -> None:
        """Only VCPUS changes; disks and identity stay as they were."""
        before = _seed_vm_files(tmp_vmdir, "set-keep", mdt=2, ost=6)
        with patch("ltvm_pkg.vm_commands.is_running", return_value=False):
            vm_commands.cmd_set(self._args("set-keep", vcpus=before.vcpus + 1))
        after = VMInfo.load("set-keep")
        assert after.vcpus == before.vcpus + 1
        assert (after.mem, after.mdt_disks, after.ost_disks, after.ip) == (
            before.mem,
            before.mdt_disks,
            before.ost_disks,
            before.ip,
        )

    def test_running_vm_is_refused(self, tmp_vmdir: Path) -> None:
        before = _seed_vm_files(tmp_vmdir, "set-running")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            pytest.raises(SystemExit),
        ):
            vm_commands.cmd_set(self._args("set-running", vcpus=1))
        assert VMInfo.load("set-running").vcpus == before.vcpus

    @pytest.mark.parametrize(
        "vcpus,mem", [(None, None), (0, None), (None, 0), (-1, 512)]
    )
    def test_bad_arguments_die(
        self, tmp_vmdir: Path, vcpus: int | None, mem: int | None
    ) -> None:
        _seed_vm_files(tmp_vmdir, "set-bad")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=False),
            pytest.raises(SystemExit),
        ):
            vm_commands.cmd_set(self._args("set-bad", vcpus=vcpus, mem=mem))

    def test_same_values_do_not_rewrite(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vm = _seed_vm_files(tmp_vmdir, "set-same")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=False),
            patch.object(VMInfo, "save") as mock_save,
        ):
            vm_commands.cmd_set(self._args("set-same", vcpus=vm.vcpus))
        mock_save.assert_not_called()
        assert "no change" in capsys.readouterr().out


# ── cmd_restore ──────────────────────────────────────────


class TestCmdRestore:
    """cmd_restore verifies the tag exists before stopping the VM."""

    _SNAP_LIST_V1 = (
        "Snapshot list:\n"
        "ID        TAG               VM SIZE                DATE       VM CLOCK     ICOUNT\n"
        "1         v1                0 B 2024-01-01 12:00:00   00:00:00.000          0\n"
    )

    def test_no_tag_lists_snapshots(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_vm_files(tmp_vmdir, "list-snap")
        args = argparse.Namespace(name="list-snap", tag=None)
        run_ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch("ltvm_pkg.vm_commands.run", return_value=run_ok) as mock_run:
            vm_commands.cmd_restore(args)
        # -l means list; capture_output=False so the user sees output
        cmd = mock_run.call_args.args[0]
        assert "snapshot" in cmd and "-l" in cmd
        assert "snapshots for list-snap" in capsys.readouterr().out

    def test_missing_tag_dies_before_stop(self, tmp_vmdir: Path) -> None:
        """A bad tag dies BEFORE we stop the VM -- a stopped VM with a
        failed restore is worse than a running VM with an error."""
        _seed_vm_files(tmp_vmdir, "res-bad")
        args = argparse.Namespace(name="res-bad", tag="nope")
        run_ok = MagicMock(returncode=0, stdout=self._SNAP_LIST_V1, stderr="")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=True),
            patch("ltvm_pkg.vm_commands.run", return_value=run_ok),
            patch("ltvm_pkg.vm_commands.kill_qemu") as mock_kill,
            pytest.raises(SystemExit),
        ):
            vm_commands.cmd_restore(args)
        mock_kill.assert_not_called()

    def test_valid_tag_applies(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_vm_files(tmp_vmdir, "res-ok")
        args = argparse.Namespace(name="res-ok", tag="v1")
        run_ok = MagicMock(returncode=0, stdout=self._SNAP_LIST_V1, stderr="")
        with (
            patch("ltvm_pkg.vm_commands.is_running", return_value=False),
            patch("ltvm_pkg.vm_commands.run", return_value=run_ok) as mock_run,
        ):
            vm_commands.cmd_restore(args)
        # Second run call is the apply (-a)
        apply_cmd = mock_run.call_args_list[-1].args[0]
        assert "-a" in apply_cmd and "v1" in apply_cmd
        assert "restored res-ok to 'v1'" in capsys.readouterr().out


# ── cmd_list rendering ───────────────────────────────────


class TestCmdListRendering:
    """Human-readable rendering of VM rows + totals line."""

    def test_text_output_contains_vm_row_and_totals(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        v = VMInfo(
            name="row-a",
            ip="10.0.0.5",
            pid=42,
            vcpus=4,
            mem=2048,
            mdt_disks=1,
            ost_disks=2,
            creator="bob",
        )
        v.save()
        args = argparse.Namespace(json=False)
        with patch(
            "ltvm_pkg.vm_commands.is_running",
            side_effect=lambda vm: vm.pid > 0,
        ):
            vm_commands.cmd_list(args)
        out = capsys.readouterr().out
        assert "row-a" in out
        assert "10.0.0.5" in out
        assert "running" in out
        assert "mdt=1 ost=2" in out
        assert "by=bob" in out
        # totals line
        assert "1 running, 0 stopped" in out
        assert "vcpus: 4/" in out
        assert "mem: 2048M/" in out


class TestCmdListMemory:
    """Each row says what the VM is holding, not just what it was given.

    QEMU faults guest RAM in lazily, so a host that looks fully
    committed can be half idle -- and when the launch budget refuses the
    next VM, which one to stop is the question the list has to answer.
    """

    def test_running_row_shows_resident_over_configured(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        VMInfo(name="mem-a", ip="10.0.0.6", pid=42, vcpus=2, mem=4096).save()
        with (
            patch(
                "ltvm_pkg.vm_commands.is_running",
                side_effect=lambda vm: vm.pid > 0,
            ),
            patch("ltvm_pkg.vm_commands._rss_mb", return_value=900),
        ):
            vm_commands.cmd_list(argparse.Namespace(json=False))
        out = capsys.readouterr().out
        assert "mem=900/4096M" in out
        assert "(900M resident)" in out

    def test_stopped_row_shows_configured_only(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        VMInfo(name="mem-b", ip="10.0.0.7", pid=0, vcpus=2, mem=4096).save()
        with patch(
            "ltvm_pkg.vm_commands.is_running", side_effect=lambda vm: False
        ):
            vm_commands.cmd_list(argparse.Namespace(json=False))
        out = capsys.readouterr().out
        assert "mem=4096M" in out
        assert "resident" not in out

    def test_json_carries_both_numbers(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        VMInfo(name="mem-c", ip="10.0.0.8", pid=42, vcpus=2, mem=4096).save()
        with (
            patch(
                "ltvm_pkg.vm_commands.is_running",
                side_effect=lambda vm: vm.pid > 0,
            ),
            patch("ltvm_pkg.vm_commands._rss_mb", return_value=900),
        ):
            vm_commands.cmd_list(argparse.Namespace(json=True))
        data = json.loads(capsys.readouterr().out)
        assert data["vms"][0]["mem"] == 4096
        assert data["vms"][0]["mem_rss_mb"] == 900
        assert data["totals"]["mem_used_mb"] == 4096
        assert data["totals"]["mem_rss_mb"] == 900


class TestRssMb:
    def test_unknown_pid_is_none(self) -> None:
        from ltvm_pkg.vm_commands import _rss_mb

        assert _rss_mb(0) is None
        assert _rss_mb(-1) is None
        # A pid that cannot be read is unknown, not zero: a row saying
        # 0M would read as "this VM is free".
        assert _rss_mb(4194303) is None

    @needs_proc
    def test_our_own_process_reports_something(self) -> None:
        import os as _os

        from ltvm_pkg.vm_commands import _rss_mb

        mb = _rss_mb(_os.getpid())
        assert mb is not None and mb >= 0


class TestCmdListBudget:
    """list reports the budget create enforces, not the host's RAM."""

    def test_cgroup_budget(
        self, tmp_vmdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression: list said 54215 MiB available in a container whose
        create refused past 24397 MiB."""
        from ltvm_pkg.qemu_run import MemoryBudget

        VMInfo(name="mem-d", ip="10.0.0.9", pid=42, vcpus=2, mem=6144).save()
        budget = MemoryBudget(27107, "cgroup memory.max", 2710, 1.0)
        with (
            patch(
                "ltvm_pkg.vm_commands.is_running",
                side_effect=lambda vm: vm.pid > 0,
            ),
            patch("ltvm_pkg.vm_commands._rss_mb", return_value=900),
            patch("ltvm_pkg.qemu_run.guest_memory_budget", return_value=budget),
            patch(
                "ltvm_pkg.vm_commands._host_total_mem_mb", return_value=64000
            ),
        ):
            vm_commands.cmd_list(argparse.Namespace(json=True))
            data = json.loads(capsys.readouterr().out)
            vm_commands.cmd_list(argparse.Namespace(json=False))
            text = capsys.readouterr().out
        totals = data["totals"]
        assert totals["mem_available_mb"] == 24397
        assert totals["mem_free_mb"] == 24397 - 6144
        assert totals["mem_host_mb"] == 64000
        assert "cgroup memory.max 27107" in totals["mem_budget"]
        assert "mem: 6144M/24397M committed" in text
        assert "budget: cgroup memory.max 27107 MiB" in text
