"""Tests for ltvm_pkg/deploy.py: tar streaming, disk topology, mount."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import deploy
from ltvm_pkg.vm_state import VMInfo
from tests.host_marks import needs_gnu_sed


def _ok(stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock()
    r.returncode = 0
    r.stdout = stdout
    r.stderr = stderr
    return r


def _fail(rc: int = 1, stdout: str = "", stderr: str = "boom") -> MagicMock:
    r = MagicMock()
    r.returncode = rc
    r.stdout = stdout
    r.stderr = stderr
    return r


@pytest.fixture
def tmp_sockets(tmp_path: Path) -> Path:
    """Redirect SOCKETS so VMInfo.save writes under tmp_path."""
    with patch("ltvm_pkg.vm_state.SOCKETS", tmp_path):
        yield tmp_path


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    """Create a minimal staging tree."""
    s = tmp_path / "staging"
    (s / "usr/lib64/lustre/tests").mkdir(parents=True)
    (s / "lib/modules/5.14").mkdir(parents=True)
    (s / "lib/modules/5.14/lustre.ko").write_text("")
    return s


def _make_vm(
    name: str = "co1-single",
    ip: str = "192.168.100.50",
    mdt_disks: int = 0,
    ost_disks: int = 0,
    disk_size: int = 500 * 1024 * 1024,
) -> VMInfo:
    return VMInfo(
        name=name,
        ip=ip,
        mdt_disks=mdt_disks,
        ost_disks=ost_disks,
        disk_size=disk_size,
    )


# ── configure_test_disks ─────────────────────────────────


class TestConfigureTestDisks:
    """configure_test_disks writes a correct disk topology into local.sh."""

    def _capture_script(
        self,
        mdt: int,
        ost: int,
        disk_size: int = 0,
        os_family: str = "rhel",
    ) -> str:
        captured: dict = {}

        def fake_run_ssh(ip, script, timeout=30):
            captured["ip"] = ip
            captured["script"] = script
            return _ok()

        with patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_run_ssh):
            deploy.configure_test_disks(
                "10.0.0.1",
                mdt,
                ost,
                disk_size,
                os_family=os_family,
            )
        return captured["script"]

    def test_mdt_only_starts_at_vdb(self) -> None:
        """Single MDT disk maps to /dev/vdb (vda is rootfs)."""
        script = self._capture_script(mdt=1, ost=0)
        assert "MDSCOUNT=${_ltvm_env_MDSCOUNT:-1}" in script
        assert "MDSDEV1=/dev/vdb" in script
        assert "OSTCOUNT=${_ltvm_env_OSTCOUNT" not in script

    def test_mdt_and_ost_ordering(self) -> None:
        """MDT disks come first, then OST disks in allocation order."""
        # mdt=2, ost=3: MDSDEV1=vdb, MDSDEV2=vdc, OSTDEV1=vdd, vde, vdf
        script = self._capture_script(mdt=2, ost=3)
        assert "MDSCOUNT=${_ltvm_env_MDSCOUNT:-2}" in script
        assert "MDSDEV1=/dev/vdb" in script
        assert "MDSDEV2=/dev/vdc" in script
        assert "OSTCOUNT=${_ltvm_env_OSTCOUNT:-3}" in script
        assert "OSTDEV1=/dev/vdd" in script
        assert "OSTDEV2=/dev/vde" in script
        assert "OSTDEV3=/dev/vdf" in script

    def test_ost_only(self) -> None:
        """OST-only VMs get OSTDEV1=vdb (skipping the MDT range)."""
        script = self._capture_script(mdt=0, ost=2)
        assert "OSTCOUNT=${_ltvm_env_OSTCOUNT:-2}" in script
        assert "OSTDEV1=/dev/vdb" in script
        assert "OSTDEV2=/dev/vdc" in script
        assert "MDSCOUNT=${_ltvm_env_MDSCOUNT" not in script

    def test_disk_size_in_kb(self) -> None:
        """disk_size_bytes emits MDSSIZE/OSTSIZE in kilobytes."""
        # 500 MiB
        script = self._capture_script(
            mdt=1,
            ost=1,
            disk_size=500 * 1024 * 1024,
        )
        assert "MDSSIZE=512000" in script
        assert "OSTSIZE=512000" in script

    def test_no_size_when_zero(self) -> None:
        """disk_size=0 omits MDSSIZE/OSTSIZE entirely."""
        script = self._capture_script(mdt=1, ost=1, disk_size=0)
        assert "MDSSIZE" not in script
        assert "OSTSIZE" not in script

    def test_size_only_for_present_roles(self) -> None:
        """OSTSIZE is only written when there are OST disks."""
        script = self._capture_script(mdt=1, ost=0, disk_size=1024 * 1024)
        assert "MDSSIZE=1024" in script
        assert "OSTSIZE" not in script

    def test_stop_keeps_dm_flakey_mappers(self) -> None:
        """CLEANUP_DM_DEV stays the framework's false, as in CI: with true,
        every stop() drops the mapper while ${facet}_dev can still name
        it, and the next start fails (sanity 160h, 278)."""
        assert "CLEANUP_DM_DEV" not in self._capture_script(mdt=1, ost=1)

    def test_client_defaults_for_setup(self) -> None:
        """init_gss() runs before init_clients_lists() and needs both."""
        script = self._capture_script(mdt=1, ost=1)
        assert "CLIENTS=${CLIENTS:-$HOSTNAME}" in script
        assert "CLIENTCOUNT=${CLIENTCOUNT:-1}" in script

    def test_markers_wrap_generated_block(self) -> None:
        """The generated snippet is wrapped in VM-disk sentinel markers."""
        script = self._capture_script(mdt=1, ost=1)
        assert "# --- VM disk configuration" in script
        assert "# --- END VM disk configuration" in script

    def test_script_rewrites_block_in_place(self) -> None:
        """Regenerating keeps the block where it is instead of appending."""
        script = self._capture_script(mdt=1, ost=1)
        # Appending would move the block past anything set after it, and
        # cfg/local.sh is sourced, so the last assignment wins.
        assert "VM disk configuration" in script
        assert "awk" in script
        assert not script.rstrip().endswith(">> {}".format("cfg/local.sh"))

    def test_regenerating_does_not_outrank_a_later_block(self) -> None:
        """A block written after ours keeps winning when we regenerate.

        This is the llmount-reverts-RAM-OSTs bug: configure_test_disks
        used to delete its block and re-append, which moved it past the
        RAM OST block and silently put the OSTs back on virtio disks.
        """
        script = self._capture_script(mdt=1, ost=1)
        with tempfile.TemporaryDirectory() as d:
            cfgdir = Path(d) / "usr/lib64/lustre/tests/cfg"
            cfgdir.mkdir(parents=True)
            cfg = cfgdir / "local.sh"
            cfg.write_text(
                "# --- VM disk configuration (generated by ltvm deploy) ---\n"
                "OSTCOUNT=1\n"
                "OSTDEV1=/dev/vdz\n"
                "# --- END VM disk configuration ---\n"
                "\n"
                "# --- RAM OST configuration (generated by ltvm deploy) ---\n"
                "OSTCOUNT=2\n"
                "OSTDEV1=/dev/ram0\n"
                "# --- END RAM OST configuration ---\n"
            )
            # run the generated script against the sandbox copy
            local = script.replace(
                "/usr/lib64/lustre/tests/cfg/local.sh", str(cfg)
            )
            subprocess.run(["bash", "-c", local], check=True)

            body = cfg.read_text()
            assert body.index("VM disk configuration") < body.index(
                "RAM OST configuration"
            ), "the regenerated block jumped past the RAM OST block"
            # and the virtio block really was refreshed in place
            assert "OSTDEV1=/dev/vdc" in body
            assert "/dev/vdz" not in body
            # sourcing it must still land on the ram device
            out = subprocess.run(
                ["bash", "-c", f". {cfg}; echo $OSTDEV1"],
                capture_output=True,
                text=True,
                check=True,
            )
            assert out.stdout.strip() == "/dev/ram0"

    def test_debian_libdir(self) -> None:
        """debian os_family writes to /usr/lib/lustre (not /usr/lib64)."""
        script = self._capture_script(
            mdt=1,
            ost=1,
            os_family="debian",
        )
        assert "/usr/lib/lustre/tests/cfg/local.sh" in script
        assert "/usr/lib64/lustre" not in script

    def test_rhel_libdir(self) -> None:
        """rhel os_family writes to /usr/lib64/lustre."""
        script = self._capture_script(
            mdt=1,
            ost=1,
            os_family="rhel",
        )
        assert "/usr/lib64/lustre/tests/cfg/local.sh" in script

    def test_failure_raises_runtimeerror(self) -> None:
        """A non-zero ssh result surfaces as RuntimeError with stderr."""
        with patch(
            "ltvm_pkg.deploy.run_ssh",
            return_value=_fail(rc=1, stderr="no such dir"),
        ):
            with pytest.raises(RuntimeError, match="local.sh"):
                deploy.configure_test_disks("10.0.0.1", 1, 1)


# ── deploy_to_vm ─────────────────────────────────────────


class TestDeployToVm:
    """deploy_to_vm streams tar over ssh, runs depmod, and configures disks."""

    def test_missing_staging_raises(self, tmp_path: Path) -> None:
        """Nonexistent staging dir raises before spawning subprocesses."""
        vm = _make_vm()
        with pytest.raises(RuntimeError, match="Staging directory not found"):
            deploy.deploy_to_vm(vm, tmp_path / "nope")

    def test_tar_failure_raises(self, staging: Path) -> None:
        """A failed transfer stops the deploy before depmod."""
        vm = _make_vm()
        ssh_cmds: list[str] = []
        with (
            patch(
                "ltvm_pkg.deploy._stream_tree",
                side_effect=RuntimeError("tar deploy failed (rc=2): boom"),
            ),
            patch(
                "ltvm_pkg.deploy.run_ssh",
                side_effect=lambda ip, cmd, **kw: ssh_cmds.append(cmd) or _ok(),
            ),
        ):
            with pytest.raises(RuntimeError, match="tar deploy failed"):
                deploy.deploy_to_vm(vm, staging)
        # The unload, then the read of cfg/local.sh's kept blocks.
        assert ssh_cmds[0] == deploy._UNLOAD_SCRIPT
        assert ssh_cmds[1:] == [
            "cat /usr/lib64/lustre/tests/cfg/local.sh 2>/dev/null; true"
        ]

    def test_depmod_timeout_is_an_error_not_a_traceback(
        self, staging: Path
    ) -> None:
        """A guest step past its limit raises RuntimeError, which
        cmd_deploy and cluster deploy report, and names the knob."""
        from ltvm_pkg.vm_net import SshTimeout

        def fake_ssh(ip: str, cmd: str, **kw: Any) -> MagicMock:
            if cmd.startswith("depmod"):
                raise SshTimeout(ip, cmd, kw["timeout"])
            return _ok()

        with (
            patch("ltvm_pkg.deploy._stream_tree"),
            patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_ssh),
        ):
            with pytest.raises(RuntimeError) as exc:
                deploy.deploy_to_vm(_make_vm(name="co1-slow"), staging)
        msg = str(exc.value)
        assert "co1-slow" in msg
        assert "depmod -a && ldconfig" in msg
        assert "timed out after 600s" in msg
        assert "LTVM_DEPLOY_TIMEOUT" in msg

    def test_guest_steps_take_the_deploy_timeout(
        self, staging: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_DEPLOY_TIMEOUT", "1500")
        timeouts: dict[str, int] = {}

        def fake_ssh(ip: str, cmd: str, **kw: Any) -> MagicMock:
            timeouts[cmd] = kw["timeout"]
            return _ok()

        with (
            patch("ltvm_pkg.deploy._stream_tree"),
            patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_ssh),
        ):
            deploy.deploy_to_vm(_make_vm(), staging)
        assert timeouts["depmod -a && ldconfig"] == 1500

    def test_depmod_failure_raises(self, staging: Path) -> None:
        """A failed depmod after successful tar propagates with rc."""
        vm = _make_vm()
        with (
            patch("ltvm_pkg.deploy._stream_tree"),
            patch(
                "ltvm_pkg.deploy.run_ssh",
                return_value=_fail(rc=7, stderr="depmod: boom"),
            ),
        ):
            with pytest.raises(RuntimeError, match="depmod"):
                deploy.deploy_to_vm(vm, staging)

    def test_no_disks_skips_configure(self, staging: Path) -> None:
        """A VM with no data disks never calls configure_test_disks."""
        vm = _make_vm(mdt_disks=0, ost_disks=0)
        with (
            patch("ltvm_pkg.deploy._stream_tree"),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch("ltvm_pkg.deploy.configure_test_disks") as mock_cfg,
        ):
            deploy.deploy_to_vm(vm, staging)
            mock_cfg.assert_not_called()

    def test_disks_trigger_configure(self, staging: Path) -> None:
        """VMs with data disks forward topology to configure_test_disks."""
        vm = _make_vm(mdt_disks=1, ost_disks=2, disk_size=12345)
        with (
            patch("ltvm_pkg.deploy._stream_tree"),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch("ltvm_pkg.deploy.configure_test_disks") as mock_cfg,
        ):
            deploy.deploy_to_vm(vm, staging, os_family="rhel")
            mock_cfg.assert_called_once_with(
                vm.ip,
                1,
                2,
                12345,
                os_family="rhel",
            )

    def test_userspace_only_skips_modules_and_depmod(
        self, staging: Path
    ) -> None:
        """userspace_only excludes lib/modules and runs ldconfig only."""
        vm = _make_vm()
        ssh_cmds = []

        def fake_ssh(ip, cmd, timeout=60):
            ssh_cmds.append(cmd)
            return _ok()

        with (
            patch("ltvm_pkg.deploy._stream_tree") as stream,
            patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_ssh),
        ):
            deploy.deploy_to_vm(vm, staging, userspace_only=True)
        stream.assert_called_once_with(vm, staging, exclude_modules=True)
        # post-deploy should be ldconfig only (no depmod); the first
        # reads cfg/local.sh, whose blocks the stream replaces
        assert ssh_cmds[1:] == ["ldconfig", deploy.AUSTER_WRAPPER_SCRIPT]
        assert ssh_cmds[0].startswith("cat ")

    def test_full_deploy_runs_depmod(self, staging: Path) -> None:
        """Normal (kernel-module) deploy runs depmod -a && ldconfig."""
        vm = _make_vm()
        ssh_cmds = []

        def fake_ssh(ip, cmd, timeout=60):
            ssh_cmds.append(cmd)
            return _ok()

        with (
            patch("ltvm_pkg.deploy._stream_tree"),
            patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_ssh),
        ):
            deploy.deploy_to_vm(vm, staging)
        assert ssh_cmds[0] == deploy._UNLOAD_SCRIPT
        assert ssh_cmds[2] == deploy.unstaged_modules_script(staging)
        assert ssh_cmds[3:] == [
            "depmod -a && ldconfig",
            deploy.AUSTER_WRAPPER_SCRIPT,
        ]

    def test_a_lustre_that_will_not_unload_stops_the_deploy(
        self, staging: Path
    ) -> None:
        """Nothing is streamed: the new files would not take effect."""
        vm = _make_vm()
        with (
            patch("ltvm_pkg.deploy._stream_tree") as tar,
            patch(
                "ltvm_pkg.deploy.run_ssh",
                return_value=_ok(stdout="unmounted\nmounted\nloaded libcfs\n"),
            ),
        ):
            with pytest.raises(RuntimeError, match="still loaded"):
                deploy.deploy_to_vm(vm, staging)
        tar.assert_not_called()


def _gnu_tar() -> bool:
    try:
        r = subprocess.run(
            ["tar", "--version"], capture_output=True, text=True, check=False
        )
    except OSError:
        return False
    return "GNU tar" in r.stdout


class TestStreamTree:
    """The transfer is limited by progress, not by its total time.

    A fixed 120s on the whole tar|ssh failed every deploy of a 380 MB
    staging on a host at load 60.  Here "ssh" is a local shell running
    the remote command against a scratch root.
    """

    @pytest.fixture(autouse=True)
    def _need_gnu_tar(self) -> None:
        if not _gnu_tar():
            pytest.skip("needs GNU tar (--keep-directory-symlink)")

    def _remote(self, root: Path, *, script: str | None = None) -> Any:
        def argv(ip: str, command: str, **kw: Any) -> list[str]:
            assert ip == "10.9.8.7"
            cmd = command.replace(" -C / ", f" -C {root} ")
            return ["bash", "-c", script if script is not None else cmd]

        return patch("ltvm_pkg.deploy.sshpass_ssh_argv", side_effect=argv)

    def test_tree_arrives(self, staging: Path, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        (staging / ".ltvm-staging-meta.json").write_text("{}")
        (staging / "usr/lib64/lustre/tests/sanity.sh").write_text("x" * 5000)
        with self._remote(root):
            deploy._stream_tree(_make_vm(ip="10.9.8.7"), staging)
        assert (root / "usr/lib64/lustre/tests/sanity.sh").read_text() == (
            "x" * 5000
        )
        assert (root / "lib/modules/5.14/lustre.ko").exists()
        assert not (root / ".ltvm-staging-meta.json").exists()

    def test_plain_gzip_without_pigz(
        self, staging: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(deploy.shutil, "which", lambda name: None)
        assert deploy._compressor()[0] == "gzip"
        root = tmp_path / "root"
        root.mkdir()
        with self._remote(root):
            deploy._stream_tree(_make_vm(ip="10.9.8.7"), staging)
        assert (root / "lib/modules/5.14/lustre.ko").exists()

    def test_exclude_modules(self, staging: Path, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        with self._remote(root):
            deploy._stream_tree(
                _make_vm(ip="10.9.8.7"), staging, exclude_modules=True
            )
        assert (root / "usr/lib64/lustre/tests").is_dir()
        assert not (root / "lib/modules").exists()

    def test_slow_but_moving_is_not_stopped(
        self,
        staging: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Takes longer in total than the limit, but never stalls."""
        monkeypatch.setenv("LTVM_DEPLOY_TIMEOUT", "2")
        monkeypatch.setattr(deploy, "_CHUNK", 64 * 1024)
        (staging / "big").write_bytes(os.urandom(1 << 20))
        root = tmp_path / "root"
        root.mkdir()
        # Reads 64 KiB every 0.2s: about 3s for the 1 MiB.
        slow = (
            "while dd bs=64k count=1 iflag=fullblock status=none "
            "| grep -qc ''; do sleep 0.2; done"
        )
        start = time.monotonic()
        with self._remote(root, script=slow):
            deploy._stream_tree(_make_vm(ip="10.9.8.7"), staging)
        assert time.monotonic() - start > 2

    def test_stall_is_stopped_and_named(
        self,
        staging: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LTVM_DEPLOY_TIMEOUT", "1")
        (staging / "big").write_bytes(os.urandom(4 << 20))
        with self._remote(tmp_path, script="sleep 60"):
            start = time.monotonic()
            with pytest.raises(RuntimeError) as exc:
                deploy._stream_tree(_make_vm(ip="10.9.8.7"), staging)
        assert time.monotonic() - start < 30
        assert "no progress for 1s" in str(exc.value)
        assert "LTVM_DEPLOY_TIMEOUT" in str(exc.value)

    def test_remote_failure_carries_its_output(
        self, staging: Path, tmp_path: Path
    ) -> None:
        with self._remote(
            tmp_path, script="cat >/dev/null; echo 'disk full' >&2; exit 2"
        ):
            with pytest.raises(RuntimeError) as exc:
                deploy._stream_tree(_make_vm(ip="10.9.8.7"), staging)
        assert "tar deploy failed (rc=2)" in str(exc.value)
        assert "disk full" in str(exc.value)

    def test_unreachable_guest_does_not_hang(
        self, staging: Path, tmp_path: Path
    ) -> None:
        """ssh exiting before reading must not leave tar blocked."""
        (staging / "big").write_bytes(os.urandom(4 << 20))
        with self._remote(tmp_path, script="echo 'no route' >&2; exit 255"):
            with pytest.raises(RuntimeError, match="no route"):
                deploy._stream_tree(_make_vm(ip="10.9.8.7"), staging)

    def test_a_connection_that_never_got_in_is_tried_again(
        self,
        staging: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A guest slow to send its banner after a cold boot gets the
        tree on a later try instead of failing the node."""
        root = tmp_path / "root"
        root.mkdir()
        flag = tmp_path / "tried"
        sleeps: list[float] = []
        monkeypatch.setattr(deploy.time, "sleep", sleeps.append)
        script = (
            f"if [ ! -e {flag} ]; then touch {flag}; "
            "echo 'Connection timed out during banner exchange' >&2; "
            f"exit 255; fi; tar xzf - -C {root} --no-same-owner"
        )
        with self._remote(root, script=script):
            deploy._stream_tree(_make_vm(ip="10.9.8.7"), staging)
        assert (root / "lib/modules/5.14/lustre.ko").exists()
        assert sleeps == [deploy.SSH_CONNECT_RETRY_DELAYS[0]]

    def test_a_guest_that_never_answers_still_fails(
        self,
        staging: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(deploy.time, "sleep", lambda s: None)
        script = (
            "cat >/dev/null; "
            "echo 'Connection timed out during banner exchange' >&2; exit 255"
        )
        with self._remote(tmp_path, script=script):
            with pytest.raises(RuntimeError, match="banner exchange"):
                deploy._stream_tree(_make_vm(ip="10.9.8.7"), staging)


class TestStepTimeout:
    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LTVM_DEPLOY_TIMEOUT", raising=False)
        assert deploy.step_timeout() == 600

    def test_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LTVM_DEPLOY_TIMEOUT", "1800")
        assert deploy.step_timeout() == 1800

    @pytest.mark.parametrize("bad", ["0", "-5", "lots"])
    def test_bad_value_warns_and_keeps_the_default(
        self,
        bad: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("LTVM_DEPLOY_TIMEOUT", bad)
        assert deploy.step_timeout() == 600
        assert "LTVM_DEPLOY_TIMEOUT" in capsys.readouterr().err


class TestLlmountTimeout:
    def test_grows_with_targets(self) -> None:
        assert deploy.llmount_timeout(8) > deploy.llmount_timeout(3)

    def test_zfs_gets_more(self) -> None:
        """1 MDT + 4 OSTs on ZFS took about 10 minutes."""
        assert deploy.llmount_timeout(5, zfs=True) >= 900
        assert deploy.llmount_timeout(5, zfs=True) > deploy.llmount_timeout(5)

    def test_no_disks_is_the_framework_default(self) -> None:
        assert deploy.llmount_timeout(0) == deploy.llmount_timeout(3)


class TestUnloadLustre:
    """deploy unloads whatever Lustre is running, however it was mounted."""

    def _unload(self, **ssh: Any) -> None:
        with patch("ltvm_pkg.deploy.run_ssh", **ssh):
            deploy.unload_lustre(_make_vm(name="co9-a"))

    def test_nothing_loaded_is_quiet(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._unload(return_value=_ok(stdout=""))
        assert capsys.readouterr().err == ""

    def test_says_what_it_took_down(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._unload(
            return_value=_ok(
                stdout="unmounted /mnt/lustre /mnt/ost /mnt/mdt\n"
                "mounted\nloaded\n"
            )
        )
        err = capsys.readouterr().err
        assert "Unloaded the running Lustre on co9-a" in err
        assert "/mnt/lustre, /mnt/ost, /mnt/mdt" in err

    def test_still_mounted_is_an_error(self) -> None:
        with pytest.raises(RuntimeError) as exc:
            self._unload(
                return_value=_ok(
                    stdout="unmounted /mnt/mdt\nmounted /mnt/mdt\n"
                    "loaded libcfs\n"
                )
            )
        assert "still mounted on /mnt/mdt" in str(exc.value)
        assert "ltvm stop co9-a && ltvm start co9-a" in str(exc.value)

    def test_timeout_is_an_error(self) -> None:
        with pytest.raises(RuntimeError, match="timed out"):
            self._unload(side_effect=subprocess.TimeoutExpired("ssh", 180))

    def test_unreachable_vm_is_an_error(self) -> None:
        with pytest.raises(RuntimeError, match="rc=255"):
            self._unload(return_value=_fail(rc=255, stderr="no route"))

    def test_script_unmounts_by_hand_mounts_too(self) -> None:
        """Every lustre mount in /proc/mounts, not llmount.sh's own."""
        script = deploy._UNLOAD_SCRIPT
        assert "/proc/mounts" in script
        # llmount.sh's own targets, which a filter on "lustre" alone missed.
        assert '"lustre_tgt"' in script
        assert "llmountcleanup" not in script
        assert script.index("umount -f") < script.index("lustre_rmmod")


# ── lustre_mount_vm ──────────────────────────────────────


class TestLustreMountVm:
    """lustre_mount_vm cleans up state then runs llmount.sh."""

    def test_vm_not_found_returns_not_found_exit(
        self,
        tmp_sockets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ltvm_pkg.vm_state import EXIT_NOT_FOUND

        rc = deploy.lustre_mount_vm("ghost", os_family="rhel")
        assert rc == EXIT_NOT_FOUND

    def test_mount_success_returns_zero(self, tmp_sockets: Path) -> None:
        """Happy path: cleanup + llmount.sh both return 0."""
        vm = _make_vm(name="mount-ok", ip="10.0.0.5")
        vm.save()
        with patch("ltvm_pkg.deploy.run_ssh", return_value=_ok(stdout="")):
            rc = deploy.lustre_mount_vm("mount-ok", os_family="rhel")
        assert rc == 0

    def test_mount_calls_cleanup_then_llmount(self, tmp_sockets: Path) -> None:
        """lustre_mount_vm runs llmountcleanup first, then llmount."""
        vm = _make_vm(name="mount-order", ip="10.0.0.6")
        vm.save()
        calls = []

        def fake_ssh(ip, cmd, timeout=0, stream=None):
            calls.append(cmd)
            return _ok()

        with patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_ssh):
            deploy.lustre_mount_vm("mount-order", os_family="rhel")
        assert len(calls) == 2
        assert "llmountcleanup.sh" in calls[0]
        assert "dmsetup remove_all" in calls[0]
        assert "llmount.sh" in calls[1]
        assert "llmountcleanup.sh" not in calls[1]

    @pytest.mark.parametrize("quiet", [False, True])
    def test_llmount_output_is_streamed(
        self, tmp_sockets: Path, quiet: bool
    ) -> None:
        """llmount.sh's output flows as it runs rather than all at once
        at the end -- to stderr under quiet, where stdout is --json's."""
        import sys

        vm = _make_vm(name="mount-stream", ip="10.0.0.10")
        vm.save()
        with patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()) as ssh:
            rc = deploy.lustre_mount_vm(
                "mount-stream", os_family="rhel", quiet=quiet
            )
        assert rc == 0
        cleanup, mount = ssh.call_args_list
        # The cleanup's output is noise; only llmount.sh streams.
        assert "stream" not in cleanup.kwargs
        assert mount.kwargs["stream"] is (sys.stderr if quiet else sys.stdout)

    def test_llmount_limit_scales_with_targets(self, tmp_sockets: Path) -> None:
        """A fixed 180s killed llmount.sh on ZFS with 4 MDTs and 4 OSTs."""
        vm = _make_vm(
            name="mount-zfs", ip="10.0.0.11", mdt_disks=4, ost_disks=4
        )
        vm.save()
        with patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()) as ssh:
            deploy.lustre_mount_vm("mount-zfs", os_family="rhel", zfs=True)
        assert ssh.call_args_list[1].kwargs["timeout"] == (
            deploy.llmount_timeout(8, zfs=True)
        )
        with patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()) as ssh:
            deploy.lustre_mount_vm("mount-zfs", os_family="rhel", ost_count=10)
        assert ssh.call_args_list[1].kwargs["timeout"] == (
            deploy.llmount_timeout(14)
        )
        with patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()) as ssh:
            deploy.lustre_mount_vm("mount-zfs", os_family="rhel", timeout=77)
        assert ssh.call_args_list[1].kwargs["timeout"] == 77

    def test_llmount_timeout_says_what_to_do(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from ltvm_pkg.vm_net import SshTimeout

        vm = _make_vm(name="mount-slow", ip="10.0.0.12")
        vm.save()
        results: list[Any] = [
            _ok(),
            SshTimeout(vm.ip, "cd /x && bash llmount.sh", 360),
        ]
        with patch("ltvm_pkg.deploy.run_ssh", side_effect=results):
            rc = deploy.lustre_mount_vm("mount-slow", os_family="rhel")
        assert rc != 0
        err = capsys.readouterr().err
        assert "timed out after 360s" in err
        assert "partly mounted" in err
        assert "--timeout" in err

    def test_mount_uses_debian_libdir(self, tmp_sockets: Path) -> None:
        """debian os_family passes /usr/lib/lustre into the mount commands."""
        vm = _make_vm(name="mount-deb", ip="10.0.0.7")
        vm.save()
        calls = []

        def fake_ssh(ip, cmd, timeout=0, stream=None):
            calls.append(cmd)
            return _ok()

        with patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_ssh):
            deploy.lustre_mount_vm("mount-deb", os_family="debian")
        assert all("/usr/lib/lustre" in c for c in calls)
        assert all("/usr/lib64/lustre" not in c for c in calls)

    def test_mount_failure_returns_rc(
        self,
        tmp_sockets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A failing llmount.sh surfaces its rc from lustre_mount_vm."""
        vm = _make_vm(name="mount-fail", ip="10.0.0.8")
        vm.save()
        results = iter([_ok(), _fail(rc=5, stderr="mount broken")])
        with patch(
            "ltvm_pkg.deploy.run_ssh",
            side_effect=lambda *a, **k: next(results),
        ):
            rc = deploy.lustre_mount_vm("mount-fail", os_family="rhel")
        assert rc == 5

    def test_mount_ssh_exception_returns_error(self, tmp_sockets: Path) -> None:
        """A run_ssh exception becomes EXIT_ERROR (not a traceback)."""
        from ltvm_pkg.vm_state import EXIT_ERROR

        vm = _make_vm(name="mount-exc", ip="10.0.0.9")
        vm.save()
        with patch(
            "ltvm_pkg.deploy.run_ssh",
            side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=60),
        ):
            rc = deploy.lustre_mount_vm("mount-exc", os_family="rhel")
        assert rc == EXIT_ERROR


# --------------------------------------------------------------------------
# cmd_deploy: per-kernel staging resolution (lustre_test_vms_v2-eh9)
# --------------------------------------------------------------------------


class TestCmdDeployPerKernelStaging:
    """cmd_deploy resolves staging per-kernel and refuses when the
    legacy per-target staging exists but the per-kernel one doesn't.
    """

    def _setup_lustre_tree(self, build_path: Path) -> None:
        (build_path / "lustre").mkdir(parents=True)
        (build_path / "lnet").mkdir()
        (build_path / "configure.ac").write_text("")

    def test_resolves_under_kernel_subdir(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """The staging path used by cmd_deploy is keyed by kernel."""
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        self._setup_lustre_tree(build_path)
        # Seed a fresh per-kernel staging dir with a .ko so the build
        # step is skipped and we can assert the path was used.
        from ltvm_pkg.lustre_build import staging_path

        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("5.14.0-foo\n")
        _mark_staging_fresh(staging, build_path, _stub_tc())

        vm = _make_vm(name="co1-eh9", ip="192.168.100.60")
        vm.os_id = "rocky9"
        vm.save()

        # Full stub: cmd_deploy's freshness check reads the kernel
        # build-tree's Module.symvers, so kernel_output_dir must be a
        # real path rather than an auto-created MagicMock attribute.
        tc = _stub_tc()

        captured: dict = {}

        def fake_deploy_to_vm(vm_arg, staging_arg, **kwargs):
            captured["staging"] = Path(staging_arg)

        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch("ltvm_pkg.cli.deploy_to_vm", side_effect=fake_deploy_to_vm),
            patch("subprocess.run") as run_mock,
        ):
            # If _staging_is_fresh does get invoked it calls `find`,
            # which we short-circuit to return "fresh" (empty stdout).
            run_mock.return_value = MagicMock(returncode=0, stdout="")
            args = ap.Namespace(
                vm="co1-eh9",
                lustre_tree=str(build_path),
                mount=False,
                target=None,
                kernel=None,
                json=False,
                userspace_only=False,
                force_compat=False,
            )
            cli_mod.cmd_deploy(args)

        assert "staging" in captured
        assert captured["staging"] == staging
        assert captured["staging"].name == "5.14-rhel9.7"

    def test_legacy_staging_triggers_clear_error(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """Legacy per-target staging present + per-kernel missing must
        not silently ship the legacy modules.

        cmd_deploy reaches that by rebuilding, so the `ltvm build lustre`
        child is mocked to fail.  Without the mock this test ran a real
        `ltvm build lustre` and passed only because that failed -- on a
        host with no ltvm on PATH it died with FileNotFoundError, and on
        a host with one it was one missing-artifact check away from
        starting an actual build from the test suite.
        """
        import argparse as ap

        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.cli import deploy as cli_deploy

        build_path = tmp_path / "lustre-release"
        self._setup_lustre_tree(build_path)
        legacy = build_path / ".ltvm-staging" / "rocky9" / "x86_64"
        legacy.mkdir(parents=True)
        (legacy / "old.ko").write_text("")

        vm = _make_vm(name="co1-eh9-legacy", ip="192.168.100.61")
        vm.os_id = "rocky9"
        vm.save()

        tc = MagicMock()
        tc.os_family = "rhel"
        tc.arch = "x86_64"
        tc.resolve_kernel.side_effect = lambda k: k or "5.14-rhel9.7"

        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "_gate_lustre_validation"),
            patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock,
            patch.object(
                cli_deploy.subprocess,
                "run",
                return_value=MagicMock(returncode=1, stdout="", stderr=""),
            ) as run_mock,
        ):
            args = ap.Namespace(
                vm="co1-eh9-legacy",
                lustre_tree=str(build_path),
                mount=False,
                target=None,
                kernel=None,
                json=False,
                userspace_only=False,
                force_compat=False,
            )
            rc = cli_mod.cmd_deploy(args)

        from ltvm_pkg.cli import EXIT_ERROR

        assert rc == EXIT_ERROR
        assert not deploy_mock.called
        # It failed at the rebuild, not somewhere earlier for an
        # unrelated reason -- which is what makes the assertion above
        # evidence that legacy modules are not shipped.
        assert any(
            call.args[0][:3] == ["ltvm", "build", "lustre"]
            for call in run_mock.call_args_list
        )


# --------------------------------------------------------------------------
# cmd_deploy: full decision-tree coverage for refactor safety
# --------------------------------------------------------------------------


def _setup_lustre_tree(build_path: Path) -> None:
    (build_path / "lustre").mkdir(parents=True)
    (build_path / "lnet").mkdir()
    (build_path / "configure.ac").write_text("")


def _deploy_args(
    vm: str = "co-test",
    lustre_tree: str | None = None,
    *,
    mount: bool = False,
    target: str | None = None,
    kernel: str | None = None,
    json: bool = False,
    userspace_only: bool = False,
    force_compat: bool = False,
) -> argparse.Namespace:
    # Post-merge, cmd_deploy reads the tree from args.lustre_tree
    # (unified with other subcommands); ``build`` here is kept as the
    # test-side kwarg for readability.
    return argparse.Namespace(
        vm=vm,
        lustre_tree=lustre_tree,
        mount=mount,
        target=target,
        kernel=kernel,
        json=json,
        userspace_only=userspace_only,
        force_compat=force_compat,
    )


def _mark_staging_fresh(
    staging: Path,
    build_path: Path,
    tc: Any,
    *,
    kernel: str = "5.14-rhel9.7",
    target: str = "rocky9",
) -> None:
    """Make a staging dir pass cmd_deploy's fast-path freshness check.

    The check now verifies that the staging was built against the
    kernel ABI and configure flags currently in play, not just that no
    source file is newer than the stamp -- so a test that wants "this
    staging is up to date" has to record those too.  Creates the
    kernel build-tree's Module.symvers and the tree's configure stamp
    to match what .ltvm-staging-meta.json claims.
    """
    from ltvm_pkg.lustre_build import _hash_file, _stamp_suffix

    build_tree = Path(tc.kernel_output_dir(kernel=kernel)) / "build-tree"
    build_tree.mkdir(parents=True, exist_ok=True)
    symvers = build_tree / "Module.symvers"
    if not symvers.exists():
        symvers.write_text("dummy symvers\n")
    cfg_hash = "deadbeef" * 8

    meta_file = staging / ".ltvm-staging-meta.json"
    meta = {}
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text())
        except ValueError:
            meta = {}
    meta.setdefault("kernel_version", "5.14.0-fake")
    meta["module_symvers_sha256"] = _hash_file(symvers)
    meta["configure_sha256"] = cfg_hash
    meta_file.write_text(json.dumps(meta))

    (
        build_path / f".ltvm-configure-{_stamp_suffix(target, tc.arch)}"
    ).write_text(cfg_hash + "\n")
    # Writing into the tree root bumps its mtime, and the fast path's
    # `find -newer` compares against the staging stamp -- so re-touch
    # the stamp last or the setup makes itself look stale.
    (staging / ".ltvm-staging-stamp").touch()


# cmd_deploy's freshness check reads the kernel build-tree's
# Module.symvers, so the stub's kernel_output_dir has to be a path
# tests can actually write to -- "/fake/kernels/..." cannot be created.
_STUB_KERNELS_ROOT = Path(tempfile.mkdtemp(prefix="ltvm-test-kernels-"))


def _stub_tc() -> MagicMock:
    """Standard TargetConfig stub used by cmd_deploy tests."""
    tc = MagicMock()
    tc.os_family = "rhel"
    tc.arch = "x86_64"
    tc.resolve_kernel.side_effect = lambda k: k or "5.14-rhel9.7"
    tc.kernel_output_dir.side_effect = lambda kernel=None: (
        _STUB_KERNELS_ROOT / f"{kernel or '5.14-rhel9.7'}"
    )
    return tc


class TestCmdDeployErrorPaths:
    """Error-path branches of cmd_deploy that gate downstream work."""

    def test_vm_not_found_returns_error(self, tmp_sockets: Path) -> None:
        """A missing VM exits with EXIT_ERROR, never touches build."""
        from ltvm_pkg import cli as cli_mod

        args = _deploy_args(vm="ghost", lustre_tree=None)
        with patch("ltvm_pkg.cli.TargetConfig") as tc_mock:
            rc = cli_mod.cmd_deploy(args)
        assert rc == 1
        # TargetConfig is never even instantiated when VM lookup fails.
        tc_mock.assert_not_called()

    def test_no_target_and_no_os_id_errors(self, tmp_sockets: Path) -> None:
        """VM with no os_id and no --target gives a clear error."""
        from ltvm_pkg import cli as cli_mod

        vm = _make_vm(name="co1-no-target", ip="10.0.0.10")
        vm.os_id = ""  # no recorded OS
        vm.save()

        args = _deploy_args(vm="co1-no-target", lustre_tree=None)
        rc = cli_mod.cmd_deploy(args)
        assert rc == 1

    def test_unknown_target_yields_targetconfig_error(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """A ValueError out of TargetConfig is surfaced as a friendly error."""
        from ltvm_pkg import cli as cli_mod

        vm = _make_vm(name="co1-unknown", ip="10.0.0.11")
        vm.os_id = "bogusos"
        vm.save()

        args = _deploy_args(vm="co1-unknown", lustre_tree=str(tmp_path))
        with patch.object(
            cli_mod, "TargetConfig", side_effect=ValueError("nope")
        ):
            rc = cli_mod.cmd_deploy(args)
        assert rc == 1

    def test_build_path_missing_errors(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """An explicit --build that points at a nonexistent dir errors."""
        from ltvm_pkg import cli as cli_mod

        vm = _make_vm(name="co1-nodir", ip="10.0.0.12")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(
            vm="co1-nodir", lustre_tree=str(tmp_path / "does-not-exist")
        )
        with patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()):
            rc = cli_mod.cmd_deploy(args)
        assert rc == 1

    def test_build_path_not_lustre_tree_errors(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """A --build dir that lacks configure.ac/lustre/lnet fails fast."""
        from ltvm_pkg import cli as cli_mod

        bad = tmp_path / "not-a-lustre-tree"
        bad.mkdir()

        vm = _make_vm(name="co1-bad", ip="10.0.0.13")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(vm="co1-bad", lustre_tree=str(bad))
        with patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()):
            rc = cli_mod.cmd_deploy(args)
        assert rc == 1

    def test_userspace_only_no_staging_errors(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """--userspace-only with no pre-existing staging exits with error."""
        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)

        vm = _make_vm(name="co1-uspace", ip="10.0.0.14")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(
            vm="co1-uspace", lustre_tree=str(build_path), userspace_only=True
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock,
        ):
            rc = cli_mod.cmd_deploy(args)
        assert rc == 1
        deploy_mock.assert_not_called()

    def test_deploy_to_vm_runtimeerror_returns_error(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """deploy_to_vm raising RuntimeError flows back as EXIT_ERROR."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")
        _mark_staging_fresh(staging, build_path, _stub_tc())

        vm = _make_vm(name="co1-rterr", ip="10.0.0.15")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(vm="co1-rterr", lustre_tree=str(build_path))
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch(
                "ltvm_pkg.cli.deploy_to_vm",
                side_effect=RuntimeError("ssh died"),
            ),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
        ):
            rc = cli_mod.cmd_deploy(args)
        assert rc == 1


class TestCmdDeployUserspaceOnly:
    """--userspace-only happy path skips kernel modules and forwards flag."""

    def test_userspace_only_forwards_flag_to_deploy(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")
        _mark_staging_fresh(staging, build_path, _stub_tc())

        vm = _make_vm(name="co1-uspace-ok", ip="10.0.0.16")
        vm.os_id = "rocky9"
        vm.save()

        captured: dict = {}

        def fake_deploy_to_vm(vm_arg, staging_arg, **kwargs):
            captured.update(kwargs)
            captured["staging"] = Path(staging_arg)

        args = _deploy_args(
            vm="co1-uspace-ok",
            lustre_tree=str(build_path),
            userspace_only=True,
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch(
                "ltvm_pkg.cli.deploy_to_vm",
                side_effect=fake_deploy_to_vm,
            ),
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 0
        assert captured.get("userspace_only") is True
        assert captured["staging"] == staging


class TestCmdDeployBundledSnapshot:
    """Bundled-snapshot detection and rsync mirroring path."""

    def _make_snapshot(
        self, tc_output_dir: Path, kernel: str = "5.14-rhel9.7"
    ) -> Path:
        """Lay down a snapshot dir with the marker file."""
        snap = tc_output_dir / "kernels" / kernel / "lustre-artifacts"
        snap.mkdir(parents=True)
        (snap / ".ltvm-snapshot.json").write_text("{}")
        # Real snapshot would contain usr/, lib/modules/, etc.
        (snap / "usr").mkdir()
        (snap / "lib").mkdir()
        (snap / "marker.ko").write_text("from-snapshot")
        return snap

    def test_bundled_snapshot_used_when_no_build_arg(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """No --build + bundled snapshot present -> the snapshot is
        deployed as it stands, with nothing written under it."""
        from ltvm_pkg import cli as cli_mod

        tc_out = tmp_path / "tc-out"
        snap = self._make_snapshot(tc_out)

        vm = _make_vm(name="co1-bundled", ip="10.0.0.17")
        vm.os_id = "rocky9"
        vm.save()

        tc = _stub_tc()
        tc.output_dir = tc_out

        captured: dict = {}

        def fake_deploy_to_vm(vm_arg, staging_arg, **kwargs):
            captured["staging"] = Path(staging_arg)

        args = _deploy_args(vm="co1-bundled", lustre_tree=None)
        import os as _os

        before = sorted(p for p in tc_out.rglob("*"))
        # The artifacts dir is read-only on a shared host.
        for p in [tc_out, *before]:
            p.chmod(p.stat().st_mode & ~0o222)
        old = _os.getcwd()
        try:
            _os.chdir(tmp_path)
            with (
                patch.object(cli_mod, "TargetConfig", return_value=tc),
                patch(
                    "ltvm_pkg.cli.deploy_to_vm",
                    side_effect=fake_deploy_to_vm,
                ),
                patch("subprocess.run") as run_mock,
            ):
                rc = cli_mod.cmd_deploy(args)
        finally:
            _os.chdir(old)
            for p in [tc_out, *before]:
                p.chmod(p.stat().st_mode | 0o200)

        assert rc == 0
        assert captured["staging"] == snap
        run_mock.assert_not_called()
        assert sorted(p for p in tc_out.rglob("*")) == before

    def test_bundled_snapshot_for_the_vms_kernel_not_the_default(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """A VM booting a non-default kernel gets that kernel's bundle,
        never the default kernel's."""
        from ltvm_pkg import cli as cli_mod

        tc_out = tmp_path / "tc-out"
        self._make_snapshot(tc_out, kernel="5.14-rhel9.7")
        snap = self._make_snapshot(tc_out, kernel="5.14-rhel9.8")

        vm = _make_vm(name="co1-bundled98", ip="10.0.0.20")
        vm.os_id = "rocky9"
        vm.kernel = str(tc_out / "kernels" / "5.14-rhel9.8" / "vmlinuz")
        vm.save()

        tc = _stub_tc()
        tc.output_dir = tc_out
        captured: dict = {}

        def fake_deploy_to_vm(vm_arg, staging_arg, **kwargs):
            captured["staging"] = Path(staging_arg)

        args = _deploy_args(vm="co1-bundled98", lustre_tree=None)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch("ltvm_pkg.cli.deploy_to_vm", side_effect=fake_deploy_to_vm),
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 0
        assert captured["staging"] == snap

    def test_no_bundle_for_the_vms_kernel_names_the_ones_there_are(
        self,
        tmp_sockets: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc_out = tmp_path / "tc-out"
        self._make_snapshot(tc_out, kernel="5.14-rhel9.7")
        (tc_out / "kernels" / "5.14-rhel9.8").mkdir()

        vm = _make_vm(name="co1-nobundle", ip="10.0.0.21")
        vm.os_id = "rocky9"
        vm.kernel = str(tc_out / "kernels" / "5.14-rhel9.8" / "vmlinuz")
        vm.save()

        tc = _stub_tc()
        tc.output_dir = tc_out
        args = _deploy_args(vm="co1-nobundle", lustre_tree=None)
        import os as _os

        old = _os.getcwd()
        try:
            _os.chdir(tmp_path)
            with (
                patch.object(cli_mod, "TargetConfig", return_value=tc),
                patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock,
            ):
                rc = cli_mod.cmd_deploy(args)
        finally:
            _os.chdir(old)

        assert rc == 1
        deploy_mock.assert_not_called()
        err = capsys.readouterr().err
        assert "boots 5.14-rhel9.8" in err
        assert "for 5.14-rhel9.7 only" in err

    def test_bundle_recording_another_kernel_is_refused(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc_out = tmp_path / "tc-out"
        snap = self._make_snapshot(tc_out)
        (snap / ".ltvm-snapshot.json").write_text(
            '{"kernel": "5.14-rhel9.5-5.14.0-503.40.1.el9_5"}'
        )

        vm = _make_vm(name="co1-snapkern", ip="10.0.0.22")
        vm.os_id = "rocky9"
        vm.save()

        tc = _stub_tc()
        tc.output_dir = tc_out
        args = _deploy_args(vm="co1-snapkern", lustre_tree=None)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock,
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 1
        deploy_mock.assert_not_called()

    def test_bundled_snapshot_skips_lustre_tree_validation(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """Snapshot DESTDIR layout has no configure.ac -- must not error."""
        from ltvm_pkg import cli as cli_mod

        tc_out = tmp_path / "tc-out"
        snap = self._make_snapshot(tc_out)

        # Sanity: the snapshot dir really does NOT look like a lustre tree.
        assert not (snap / "configure.ac").exists()
        assert not (snap / "lustre").exists()

        vm = _make_vm(name="co1-snap-ok", ip="10.0.0.19")
        vm.os_id = "rocky9"
        vm.save()

        tc = _stub_tc()
        tc.output_dir = tc_out

        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ),
        ):
            args = _deploy_args(vm="co1-snap-ok", lustre_tree=None)
            rc = cli_mod.cmd_deploy(args)

        # Should succeed (rc=0), not bail with "not a Lustre source tree".
        assert rc == 0


class TestCmdDeployVariantPropagation:
    """Variant-aware staging path resolution."""

    def test_mofed_variant_routes_to_mofed_staging(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """A VM with variant=mofed-24 deploys from the mofed-24 staging dir."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path,
            "rocky9",
            arch="x86_64",
            kernel="5.14-rhel9.7",
            variant="mofed-24",
        )
        staging.mkdir(parents=True)
        (staging / "ko2iblnd.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")
        _mark_staging_fresh(staging, build_path, _stub_tc())

        vm = _make_vm(name="co1-mofed", ip="10.0.0.20")
        vm.os_id = "rocky9"
        vm.variant = "mofed-24"
        vm.save()

        captured: dict = {}

        def fake_deploy_to_vm(vm_arg, staging_arg, **kwargs):
            captured["staging"] = Path(staging_arg)

        args = _deploy_args(vm="co1-mofed", lustre_tree=str(build_path))
        with (
            patch.object(
                cli_mod, "TargetConfig", return_value=_stub_tc()
            ) as tc_mock,
            patch(
                "ltvm_pkg.cli.deploy_to_vm",
                side_effect=fake_deploy_to_vm,
            ),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 0
        # TargetConfig must be invoked with variant=mofed-24
        kwargs = tc_mock.call_args.kwargs
        assert kwargs.get("variant") == "mofed-24"
        # Staging is the variant's own dir, a sibling of the base
        # kernel dir rather than nested inside it.
        assert captured["staging"].name == "5.14-rhel9.7__mofed-24"
        assert captured["staging"].parent.name == "x86_64"


class TestCmdDeployKernelMismatch:
    """VM kernel vs target default kernel routing."""

    def test_vm_kernel_overrides_target_default_for_staging(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """A VM booted on a non-default kernel gets staging keyed to that kernel."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        # Note: target default is 5.14-rhel9.7; VM is on 5.14-rhel9.5.
        staging = staging_path(
            build_path,
            "rocky9",
            arch="x86_64",
            kernel="5.14-rhel9.5",
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")
        _mark_staging_fresh(
            staging, build_path, _stub_tc(), kernel="5.14-rhel9.5"
        )

        vm = _make_vm(name="co1-altkern", ip="10.0.0.21")
        vm.os_id = "rocky9"
        # vm.kernel is a path-like value -- the parent dir name is the
        # kernel key. Mirror what create writes: <kerndir>/vmlinux.
        vm.kernel = "/fake/artifacts/rocky9/x86_64/kernels/5.14-rhel9.5/vmlinux"
        vm.save()

        captured: dict = {}

        def fake_deploy_to_vm(vm_arg, staging_arg, **kwargs):
            captured["staging"] = Path(staging_arg)

        args = _deploy_args(vm="co1-altkern", lustre_tree=str(build_path))
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch(
                "ltvm_pkg.cli.deploy_to_vm",
                side_effect=fake_deploy_to_vm,
            ),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 0
        # Staging dir is keyed by the VM's kernel (rhel9.5), not the
        # target default (rhel9.7).
        assert captured["staging"].name == "5.14-rhel9.5"

    def test_vm_kernel_forwarded_to_build_lustre_subprocess(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """When build is invoked, --kernel <vm_kernel> is forwarded."""
        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        # No fresh staging -> build_lustre is invoked.

        vm = _make_vm(name="co1-fwd", ip="10.0.0.22")
        vm.os_id = "rocky9"
        vm.kernel = "/fake/artifacts/rocky9/x86_64/kernels/5.14-rhel9.5/vmlinux"
        vm.save()

        run_calls: list = []

        def fake_run(cmd, *args, **kwargs):
            run_calls.append(cmd)
            # Build returns 1 so we abort cleanly after capturing the cmd.
            return MagicMock(returncode=1, stdout="", stderr="")

        args = _deploy_args(vm="co1-fwd", lustre_tree=str(build_path))
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch.object(cli_mod, "_gate_lustre_validation"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 1
        # find the ltvm build lustre subprocess
        build_calls = [
            c
            for c in run_calls
            if isinstance(c, list)
            and len(c) >= 3
            and c[0:3] == ["ltvm", "build", "lustre"]
        ]
        # Could also be sudo-prefixed
        if not build_calls:
            build_calls = [
                c
                for c in run_calls
                if isinstance(c, list)
                and "ltvm" in c
                and "build" in c
                and "lustre" in c
            ]
        assert build_calls, f"build subprocess not found in {run_calls}"
        cmd = build_calls[0]
        assert "--kernel" in cmd
        idx = cmd.index("--kernel")
        assert cmd[idx + 1] == "5.14-rhel9.5"
        assert "--arch" in cmd
        assert cmd[cmd.index("--arch") + 1] == "x86_64"

    def test_configure_forwarded_even_when_staging_is_fresh(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """--configure reaches build lustre, and skips the fresh fast path.

        The fast path compares the staging with the tree's last configure,
        not with the flags asked for, so it would ship the old build.
        """
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("5.14.0-foo\n")
        _mark_staging_fresh(staging, build_path, _stub_tc())

        vm = _make_vm(name="co1-cfg", ip="10.0.0.23")
        vm.os_id = "rocky9"
        vm.save()

        run_calls: list = []

        def fake_run(cmd, *args, **kwargs):
            run_calls.append(cmd)
            return MagicMock(returncode=1, stdout="", stderr="")

        args = _deploy_args(vm="co1-cfg", lustre_tree=str(build_path))
        args.configure = "--enable-crypto"
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch.object(cli_mod, "_gate_lustre_validation"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 1
        build_calls = [
            c
            for c in run_calls
            if isinstance(c, list) and "build" in c and "lustre" in c
        ]
        assert build_calls, f"build subprocess not found in {run_calls}"
        assert "--configure=--enable-crypto" in build_calls[0]


class TestCmdDeployForceCompat:
    """--force-compat silences refuse but not hard error in the deploy path."""

    def test_force_compat_threaded_into_gate(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """force_compat=True is forwarded to _gate_lustre_validation."""
        from ltvm_pkg import cli as cli_mod

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)

        vm = _make_vm(name="co1-fc", ip="10.0.0.23")
        vm.os_id = "rocky9"
        vm.save()

        gate_calls: list = []

        def fake_gate(
            tc, lustre_tree, *, force, kernel_build_tree=None, kernel=None
        ):
            gate_calls.append({"force": force, "kernel": kernel})

        args = _deploy_args(
            vm="co1-fc", lustre_tree=str(build_path), force_compat=True
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch.object(
                cli_mod, "_gate_lustre_validation", side_effect=fake_gate
            ),
            # Build subprocess fails -> stops before deploy.
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=1, stdout=""),
            ),
        ):
            cli_mod.cmd_deploy(args)

        assert gate_calls, "_gate_lustre_validation was not called"
        assert gate_calls[0]["force"] is True
        # The gate must be told which kernel is being deployed, not left
        # to fall back to the target's default.
        assert gate_calls[0]["kernel"] == "5.14-rhel9.7"

    def test_force_compat_does_not_silence_hard_error(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """--force-compat overrides 'refuse' but NOT 'error' (hard parse fail)."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_compat import ValidationResult

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)

        vm = _make_vm(name="co1-fc-hard", ip="10.0.0.24")
        vm.os_id = "rocky9"
        vm.save()

        hard_err = ValidationResult(
            status="error",
            mode=None,
            kernel_version=None,
            matched_in=None,
            message="parse failure",
        )

        args = _deploy_args(
            vm="co1-fc-hard", lustre_tree=str(build_path), force_compat=True
        )
        # _gate_lustre_validation raises SystemExit on "error" even with
        # force.  cmd_deploy is dispatched directly (not through _vm_call)
        # so SystemExit propagates -- this is intentional, since the
        # caller is the top-level dispatch table which converts it to rc.
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch.object(cli_mod, "validate_target", return_value=hard_err),
            patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock,
        ):
            with pytest.raises(SystemExit) as exc:
                cli_mod.cmd_deploy(args)

        assert exc.value.code == 1
        deploy_mock.assert_not_called()


class TestCmdDeployMountAndKver:
    """--mount triggers lustre_mount_vm; staging meta drives kver recording."""

    def test_mount_invokes_lustre_mount_vm(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")
        _mark_staging_fresh(staging, build_path, _stub_tc())

        vm = _make_vm(name="co1-mount", ip="10.0.0.25")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(
            vm="co1-mount", lustre_tree=str(build_path), mount=True
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
            patch("ltvm_pkg.cli.lustre_mount_vm", return_value=0) as mount_mock,
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 0
        # quiet= is how --json keeps llmount's stdout out of the JSON
        # document; this is the human path, so it is False.
        mount_mock.assert_called_once_with(
            "co1-mount",
            "rhel",
            quiet=False,
            timeout=None,
            zfs=False,
            ost_count=None,
        )

    def test_mount_timeout_reaches_llmount(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")
        _mark_staging_fresh(staging, build_path, _stub_tc())

        vm = _make_vm(name="co1-mount", ip="10.0.0.25")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(
            vm="co1-mount", lustre_tree=str(build_path), mount=True
        )
        args.mount_timeout = 1234
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
            patch("ltvm_pkg.cli.lustre_mount_vm", return_value=0) as mount_mock,
        ):
            cli_mod.cmd_deploy(args)
        assert mount_mock.call_args.kwargs["timeout"] == 1234

    def test_json_emits_an_envelope_on_success(
        self, tmp_sockets: Path, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Every print in cmd_deploy is `if not use_json`-guarded and
        there was no final _output, so `deploy-lustre --json` wrote
        nothing at all to stdout on success -- an empty document for
        exactly the consumers --json exists for."""
        import json as _json

        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")
        _mark_staging_fresh(staging, build_path, _stub_tc())

        vm = _make_vm(name="co1-json", ip="10.0.0.26")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(
            vm="co1-json", lustre_tree=str(build_path), json=True
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 0
        payload = _json.loads(capsys.readouterr().out)
        assert payload["action"] == "deploy-lustre"
        assert payload["vm"] == "co1-json"
        assert payload["target"] == "rocky9"
        assert payload["mounted"] is False
        assert payload["staging"] == str(staging)

    def test_json_mount_keeps_llmount_output_off_stdout(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """llmount's own stdout was printed unconditionally, landing in
        the middle of the JSON document under --mount --json."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")
        _mark_staging_fresh(staging, build_path, _stub_tc())

        vm = _make_vm(name="co1-jmount", ip="10.0.0.27")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(
            vm="co1-jmount",
            lustre_tree=str(build_path),
            mount=True,
            json=True,
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
            patch("ltvm_pkg.cli.lustre_mount_vm", return_value=0) as mount_mock,
        ):
            rc = cli_mod.cmd_deploy(args)

        assert rc == 0
        mount_mock.assert_called_once_with(
            "co1-jmount",
            "rhel",
            quiet=True,
            timeout=None,
            zfs=False,
            ost_count=None,
        )

    def test_mount_failure_propagates(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")
        _mark_staging_fresh(staging, build_path, _stub_tc())

        vm = _make_vm(name="co1-mount-fail", ip="10.0.0.26")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(
            vm="co1-mount-fail", lustre_tree=str(build_path), mount=True
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
            patch("ltvm_pkg.cli.lustre_mount_vm", return_value=7),
        ):
            rc = cli_mod.cmd_deploy(args)
        assert rc == 7

    def test_kver_from_staging_meta_recorded(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """The kver from .ltvm-staging-meta.json is recorded on the VM."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")
        (staging / ".ltvm-staging-meta.json").write_text(
            '{"kernel_version": "5.14.0-from-staging"}'
        )
        # After the meta write: _mark_staging_fresh merges the
        # freshness fields into whatever meta is already there.
        _mark_staging_fresh(staging, build_path, _stub_tc())

        vm = _make_vm(name="co1-kver", ip="10.0.0.27")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(vm="co1-kver", lustre_tree=str(build_path))
        update_calls: list = []
        from ltvm_pkg.vm_state import VMInfo as _VMI

        orig = _VMI.update_deploy

        def capture(self, epoch, build_path, kver):
            update_calls.append({"kver": kver, "build_path": build_path})
            return orig(self, epoch, build_path, kver)

        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
            patch.object(_VMI, "update_deploy", capture),
        ):
            cli_mod.cmd_deploy(args)

        assert update_calls
        assert update_calls[0]["kver"] == "5.14.0-from-staging"

    def test_update_deploy_permissionerror_warns_not_fail(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """PermissionError on metadata save is a warning, not failure."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path
        from ltvm_pkg.vm_state import VMInfo as _VMI

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")
        _mark_staging_fresh(staging, build_path, _stub_tc())

        vm = _make_vm(name="co1-perm", ip="10.0.0.28")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(vm="co1-perm", lustre_tree=str(build_path))
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
            patch.object(
                _VMI, "update_deploy", side_effect=PermissionError("nope")
            ),
        ):
            rc = cli_mod.cmd_deploy(args)
        # Still successful: deploy itself worked, only the bookkeeping failed.
        assert rc == 0


# --------------------------------------------------------------------------
# cmd_llmount: thin wrapper over vm_commands.cmd_llmount
# --------------------------------------------------------------------------


class TestCmdLlmount:
    """cmd_llmount returns SystemExit codes from the underlying handler."""

    def test_no_cleanup_passes_flag_through(self, tmp_sockets: Path) -> None:
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        captured: dict = {}

        def fake(args):
            captured["cleanup"] = getattr(args, "cleanup", None)
            captured["vm"] = args.vm
            # mimic vm_commands.cmd_llmount calling sys.exit on success
            raise SystemExit(0)

        with patch("ltvm_pkg.vm_commands.cmd_llmount", side_effect=fake):
            args = ap.Namespace(
                vm="co1-mnt", json=False, timeout=300, cleanup=False
            )
            rc = cli_mod.cmd_llmount(args)

        assert rc == 0
        assert captured["cleanup"] is False
        assert captured["vm"] == "co1-mnt"

    def test_cleanup_flag_propagates(self, tmp_sockets: Path) -> None:
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        captured: dict = {}

        def fake(args):
            captured["cleanup"] = getattr(args, "cleanup", None)
            raise SystemExit(0)

        with patch("ltvm_pkg.vm_commands.cmd_llmount", side_effect=fake):
            args = ap.Namespace(
                vm="co1-clean", json=False, timeout=300, cleanup=True
            )
            rc = cli_mod.cmd_llmount(args)
        assert rc == 0
        assert captured["cleanup"] is True

    def test_systemexit_nonzero_propagates_rc(self, tmp_sockets: Path) -> None:
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        with patch(
            "ltvm_pkg.vm_commands.cmd_llmount",
            side_effect=SystemExit(5),
        ):
            args = ap.Namespace(
                vm="co1-fail", json=False, timeout=300, cleanup=False
            )
            rc = cli_mod.cmd_llmount(args)
        assert rc == 5

    def test_systemexit_none_code_maps_to_exit_error(
        self, tmp_sockets: Path
    ) -> None:
        """A bare `sys.exit()` (no code) maps to EXIT_ERROR == 1."""
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        with patch(
            "ltvm_pkg.vm_commands.cmd_llmount",
            side_effect=SystemExit(None),
        ):
            args = ap.Namespace(
                vm="co1-bare", json=False, timeout=300, cleanup=False
            )
            rc = cli_mod.cmd_llmount(args)
        assert rc == 1

    def test_normal_return_yields_exit_ok(self, tmp_sockets: Path) -> None:
        """If the underlying handler returns normally (no SystemExit), rc=0."""
        import argparse as ap

        from ltvm_pkg import cli as cli_mod

        with patch("ltvm_pkg.vm_commands.cmd_llmount", return_value=None):
            args = ap.Namespace(
                vm="co1-ret", json=False, timeout=300, cleanup=False
            )
            rc = cli_mod.cmd_llmount(args)
        assert rc == 0


class TestVerifyDeployedModules:
    """A deploy that ships stale modules must not pass silently."""

    def _staging(self, tmp_path, mods):
        d = tmp_path / "staging"
        d.mkdir()
        for name in mods:
            (d / name).write_bytes(b"\x7fELF fake")
        return d

    def _vm(self):
        class V:
            ip = "10.0.0.1"
            name = "co1-test"

        return V()

    def test_warns_on_mismatch(self, tmp_path, capsys):
        from unittest.mock import MagicMock, patch

        from ltvm_pkg.deploy import verify_deployed_modules

        staging = self._staging(tmp_path, ["osc.ko", "lov.ko"])
        with (
            patch(
                "ltvm_pkg.deploy.read_modinfo_field",
                side_effect=lambda p, f: "STAGED_" + p.name,
            ),
            patch("ltvm_pkg.deploy.run_ssh") as ssh,
        ):
            ssh.return_value = MagicMock(
                returncode=0, stdout="osc STAGED_osc.ko\nlov OLDBUILD\n"
            )
            verify_deployed_modules(self._vm(), staging)
        err = capsys.readouterr().err
        assert "do not match" in err
        assert "lov.ko" in err
        assert "osc.ko" not in err

    def test_silent_when_all_match(self, tmp_path, capsys):
        from unittest.mock import MagicMock, patch

        from ltvm_pkg.deploy import verify_deployed_modules

        staging = self._staging(tmp_path, ["osc.ko"])
        with (
            patch(
                "ltvm_pkg.deploy.read_modinfo_field",
                side_effect=lambda p, f: "SAME",
            ),
            patch("ltvm_pkg.deploy.run_ssh") as ssh,
        ):
            ssh.return_value = MagicMock(returncode=0, stdout="osc SAME\n")
            verify_deployed_modules(self._vm(), staging)
        assert capsys.readouterr().err == ""

    def test_module_absent_on_vm_is_not_stale(self, tmp_path, capsys):
        """A staged module the VM does not have is not evidence of staleness."""
        from unittest.mock import MagicMock, patch

        from ltvm_pkg.deploy import verify_deployed_modules

        staging = self._staging(tmp_path, ["osc.ko"])
        with (
            patch(
                "ltvm_pkg.deploy.read_modinfo_field",
                side_effect=lambda p, f: "SAME",
            ),
            patch("ltvm_pkg.deploy.run_ssh") as ssh,
        ):
            ssh.return_value = MagicMock(returncode=0, stdout="osc \n")
            verify_deployed_modules(self._vm(), staging)
        assert capsys.readouterr().err == ""

    def test_ssh_failure_warns_but_does_not_raise(self, tmp_path, capsys):
        from unittest.mock import MagicMock, patch

        from ltvm_pkg.deploy import verify_deployed_modules

        staging = self._staging(tmp_path, ["osc.ko"])
        with (
            patch(
                "ltvm_pkg.deploy.read_modinfo_field",
                side_effect=lambda p, f: "SAME",
            ),
            patch("ltvm_pkg.deploy.run_ssh") as ssh,
        ):
            ssh.return_value = MagicMock(returncode=255, stdout="")
            verify_deployed_modules(self._vm(), staging)
        assert "could not verify" in capsys.readouterr().err


# ── configure_ram_osts ───────────────────────────────────


class TestConfigureRamOsts:
    """Ram-backed OSTs take the OST's backing store out of a benchmark."""

    def _capture_script(
        self,
        count: int,
        size_gb: int = 32,
        ram_mdt: bool = False,
        os_family: str = "rhel",
    ) -> str:
        captured: dict = {}

        def fake_run_ssh(ip, script, timeout=30):
            captured["script"] = script
            return _ok()

        with patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_run_ssh):
            deploy.configure_ram_osts(
                "10.0.0.1",
                count,
                size_gb,
                ram_mdt=ram_mdt,
                os_family=os_family,
            )
        return captured["script"]

    def test_osts_map_to_ram_devices_from_zero(self) -> None:
        script = self._capture_script(count=4)
        for n, dev in enumerate(("ram0", "ram1", "ram2", "ram3"), start=1):
            assert f"OSTDEV{n}=/dev/{dev}" in script
        assert "OSTCOUNT=${_ltvm_env_OSTCOUNT:-4}" in script

    def test_mdt_untouched_by_default(self) -> None:
        """Without --ram-mdt the MDT keeps whatever it had; emitting an
        MDSDEV here would silently override the virtio mapping."""
        script = self._capture_script(count=4)
        assert "MDSDEV" not in script
        assert "MDSCOUNT=${_ltvm_env_MDSCOUNT" not in script

    def test_ram_mdt_takes_the_device_after_the_osts(self) -> None:
        script = self._capture_script(count=4, ram_mdt=True)
        assert "MDSDEV1=/dev/ram4" in script  # 0-3 are the OSTs
        assert "rd_nr=$want_nr" in script

    def test_size_is_converted_to_kib_for_brd(self) -> None:
        """brd's rd_size is KiB; OSTSIZE is KB for the test framework."""
        script = self._capture_script(count=2, size_gb=32)
        assert "want_kb=33554432" in script  # 32 GiB in KiB
        assert "OSTSIZE=33554432" in script

    def test_stale_signatures_are_wiped(self) -> None:
        """A ram device reloaded with a new geometry can still carry an
        ldiskfs superblock, which mkfs.lustre then refuses."""
        script = self._capture_script(count=2)
        assert "wipefs -a /dev/ram$i" in script

    def test_brd_only_reloaded_when_geometry_differs(self) -> None:
        """An unconditional rmmod would fail on a mounted filesystem for
        no reason."""
        script = self._capture_script(count=2)
        assert "have_nr" in script and "have_kb" in script
        assert "-lt" in script  # count compare
        assert "-ne" in script  # size compare

    def test_in_use_brd_gives_an_actionable_message(self) -> None:
        script = self._capture_script(count=2)
        assert "unmount Lustre first" in script

    def test_block_is_appended_not_replacing_the_virtio_one(self) -> None:
        """cfg/local.sh is sourced, so later wins.  Appending keeps both
        blocks visible, which matters when working out why an OST is not
        where it was expected."""
        script = self._capture_script(count=2)
        assert ">> " in script  # append
        # Only our own block is removed first, never ltvm's virtio block.
        assert "RAM OST configuration" in script
        assert "VM disk configuration" not in script

    def test_failure_is_raised_with_output(self) -> None:
        with patch(
            "ltvm_pkg.deploy.run_ssh",
            return_value=_fail(stderr="brd is in use; unmount Lustre first"),
        ):
            with pytest.raises(RuntimeError, match="unmount Lustre first"):
                deploy.configure_ram_osts("10.0.0.1", 4, 32)


class TestDeployRamOstWiring:
    """deploy_to_vm must apply ram OSTs after the virtio block."""

    def test_not_called_when_zero(self, staging: Path) -> None:
        vm = _make_vm(mdt_disks=1, ost_disks=2)
        with (
            patch("ltvm_pkg.deploy._stream_tree"),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch("ltvm_pkg.deploy.configure_test_disks"),
            patch("ltvm_pkg.deploy.configure_ram_osts") as mock_ram,
        ):
            deploy.deploy_to_vm(vm, staging)
            mock_ram.assert_not_called()

    def test_forwarded_when_requested(self, staging: Path) -> None:
        vm = _make_vm(mdt_disks=1, ost_disks=0)
        with (
            patch("ltvm_pkg.deploy._stream_tree"),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch("ltvm_pkg.deploy.configure_test_disks"),
            patch("ltvm_pkg.deploy.configure_ram_osts") as mock_ram,
        ):
            deploy.deploy_to_vm(
                vm, staging, ram_osts=8, ram_ost_size_gb=16, ram_mdt=True
            )
            mock_ram.assert_called_once_with(
                vm.ip, 8, 16, ram_mdt=True, os_family="rhel"
            )

    def test_ram_runs_after_virtio(self, staging: Path) -> None:
        """Order is load-bearing: the ram block must be appended after
        the virtio one or `source cfg/local.sh` takes the wrong devices."""
        calls: list[str] = []
        vm = _make_vm(mdt_disks=1, ost_disks=2)
        with (
            patch("ltvm_pkg.deploy._stream_tree"),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch(
                "ltvm_pkg.deploy.configure_test_disks",
                side_effect=lambda *a, **k: calls.append("virtio"),
            ),
            patch(
                "ltvm_pkg.deploy.configure_ram_osts",
                side_effect=lambda *a, **k: calls.append("ram"),
            ),
        ):
            deploy.deploy_to_vm(vm, staging, ram_osts=4)
        assert calls == ["virtio", "ram"], calls


# ── --ost-count ──────────────────────────────────────────


class _Sandbox:
    """Runs what configure_ost_count sends over ssh in a local tree."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cfg = root / "usr/lib64/lustre/tests/cfg/local.sh"
        self.cfg.parent.mkdir(parents=True)
        self.files = root / "ost-files"
        self.scripts: list[str] = []
        self.stdout: list[str] = []

    def local(self, script: str) -> str:
        return (
            script.replace(deploy.OST_FILE_DIR, str(self.files))
            .replace("/usr/lib64/lustre", str(self.root / "usr/lib64/lustre"))
            .replace("/tmp/.ltvm-", str(self.root / ".ltvm-"))
        )

    def run_ssh(self, ip, script, timeout=30):
        self.scripts.append(script)
        r = subprocess.run(
            ["bash", "-c", self.local(script)], capture_output=True, text=True
        )
        self.stdout.append(r.stdout)
        return r

    def source(self, expr: str, env: dict[str, str] | None = None) -> str:
        return subprocess.run(
            ["bash", "-c", f". {self.cfg}; echo {expr}"],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout.strip()


@pytest.fixture
def sandbox(tmp_path: Path) -> Any:
    box = _Sandbox(tmp_path)
    with (
        patch("ltvm_pkg.deploy.run_ssh", side_effect=box.run_ssh),
        # The host's free space must not decide these tests.
        patch.object(deploy, "OST_FILE_RESERVE_KB", 0),
        patch.object(deploy, "OST_FILE_MIN_KB", 1024),
    ):
        yield box


class TestOstFileKb:
    """The size of each file-backed OST."""

    def test_capped_at_the_disk_size(self) -> None:
        free = 100 * 1024 * 1024
        assert deploy.ost_file_kb(free, 6, 512000) == 512000

    def test_shares_the_space_after_the_reserve(self) -> None:
        space = deploy.OST_FILE_RESERVE_KB + 6 * 300 * 1024
        assert deploy.ost_file_kb(space, 6, 512000) == 300 * 1024

    def test_rounds_down_to_a_mib(self) -> None:
        space = deploy.OST_FILE_RESERVE_KB + 300 * 1024 + 7
        assert deploy.ost_file_kb(space, 1, 0) == 300 * 1024

    def test_too_small_is_zero(self) -> None:
        space = deploy.OST_FILE_RESERVE_KB + 6 * 100 * 1024
        assert deploy.ost_file_kb(space, 6, 512000) == 0


class TestConfigureOstCount:
    """configure_ost_count writes the OST count block into cfg/local.sh."""

    def _scripts(
        self, count: int, ost_disks: int = 2, free_kb: int = 8 << 20, **kw
    ) -> list[str]:
        scripts: list[str] = []

        def fake_run_ssh(ip, script, timeout=30):
            scripts.append(script)
            return _ok(stdout=f"{free_kb} 0\n")

        with patch("ltvm_pkg.deploy.run_ssh", side_effect=fake_run_ssh):
            deploy.configure_ost_count(
                "10.0.0.1", count, ost_disks, 500 * 1024 * 1024, **kw
            )
        return scripts

    def test_osts_past_the_disks_are_files(self) -> None:
        block = self._scripts(8)[-1]
        assert "OSTCOUNT=${_ltvm_env_OSTCOUNT:-8}" in block
        for n in range(3, 9):
            assert f"OSTDEV{n}={deploy.OST_FILE_DIR}/ost{n}" in block
        # The disk block keeps OSTDEV1 and OSTDEV2.
        assert "OSTDEV1=" not in block and "OSTDEV2=" not in block
        assert "OSTSIZE=512000" in block

    def test_size_shrinks_to_fit_the_root_disk(self) -> None:
        free = deploy.OST_FILE_RESERVE_KB + 6 * 400 * 1024
        block = self._scripts(8, free_kb=free)[-1]
        assert f"OSTSIZE={400 * 1024}" in block

    def test_too_little_space_is_refused(self) -> None:
        free = deploy.OST_FILE_RESERVE_KB + 6 * 10 * 1024
        with pytest.raises(RuntimeError, match="--root-size"):
            self._scripts(8, free_kb=free)

    def test_no_files_within_the_disks(self) -> None:
        block = self._scripts(1)[-1]
        assert "OSTCOUNT=${_ltvm_env_OSTCOUNT:-1}" in block
        assert "OSTDEV" not in block and "OSTSIZE" not in block

    def test_userspace_only_removes_no_files(self) -> None:
        """Lustre may still be running on them."""
        probe = self._scripts(8, prune=False)[0]
        assert "rm -f" not in probe

    def test_zero_removes_the_block(self) -> None:
        scripts = self._scripts(0)
        assert "rm -f" in scripts[0]
        assert "OSTCOUNT" not in scripts[-1]
        assert (
            f"/^{deploy.OST_COUNT_BEGIN}/,/^{deploy.OST_COUNT_END}/d"
            in (scripts[-1])
        )

    def test_debian_libdir(self) -> None:
        block = self._scripts(4, os_family="debian")[-1]
        assert "/usr/lib/lustre/tests/cfg/local.sh" in block
        assert "/usr/lib64/" not in block


@needs_gnu_sed
class TestOstCountInLocalSh:
    """The block, run for real against a cfg/local.sh."""

    _DISKS = (
        "OSTCOUNT=${OSTCOUNT:-2}\n"
        "# --- VM disk configuration (generated by ltvm deploy) ---\n"
        "OSTSIZE=512000\n"
        "OSTCOUNT=${_ltvm_env_OSTCOUNT:-2}\n"
        "OSTDEV1=/dev/vdc\n"
        "OSTDEV2=/dev/vdd\n"
        "# --- END VM disk configuration ---\n"
    )

    def _configure(self, box: _Sandbox, count: int, **kw: Any) -> None:
        deploy.configure_ost_count(
            "10.0.0.1", count, 2, 500 * 1024 * 1024, **kw
        )

    def test_sourcing_gives_the_count_and_the_files(
        self, sandbox: _Sandbox
    ) -> None:
        sandbox.cfg.write_text(self._DISKS)
        self._configure(sandbox, 8)
        assert sandbox.source("$OSTCOUNT") == "8"
        assert sandbox.source("$OSTDEV2") == "/dev/vdd"
        assert sandbox.source("$OSTDEV8") == str(sandbox.files / "ost8")
        assert sandbox.source("$OSTSIZE") == "512000"
        assert sandbox.files.is_dir()

    def test_environment_count_still_wins(self, sandbox: _Sandbox) -> None:
        sandbox.cfg.write_text(self._DISKS)
        self._configure(sandbox, 8)
        env = {"PATH": "/usr/bin:/bin", "OSTCOUNT": "3"}
        assert sandbox.source("$OSTCOUNT", env) == "3"

    def test_llmount_rewriting_the_disk_block_keeps_it(
        self, sandbox: _Sandbox
    ) -> None:
        """llmount and `suite run` rewrite the disk block; the count
        must survive them, as it survives a plain redeploy."""
        sandbox.cfg.write_text(self._DISKS)
        self._configure(sandbox, 8)
        deploy.configure_test_disks("10.0.0.1", 1, 2, 500 * 1024 * 1024)
        assert sandbox.source("$OSTCOUNT") == "8"
        body = sandbox.cfg.read_text()
        assert body.count(deploy.OST_COUNT_BEGIN) == 1

    def test_a_new_count_replaces_the_old(self, sandbox: _Sandbox) -> None:
        sandbox.cfg.write_text(self._DISKS)
        self._configure(sandbox, 8)
        (sandbox.files / "ost8").write_text("x")
        self._configure(sandbox, 4)
        assert sandbox.source("$OSTCOUNT") == "4"
        assert sandbox.source("${OSTDEV8:-unset}") == "unset"
        assert sandbox.cfg.read_text().count(deploy.OST_COUNT_BEGIN) == 1
        assert not (sandbox.files / "ost8").exists()

    def test_zero_goes_back_to_the_disks(self, sandbox: _Sandbox) -> None:
        sandbox.cfg.write_text(self._DISKS)
        self._configure(sandbox, 8)
        (sandbox.files / "ost3").write_text("x")
        self._configure(sandbox, 0)
        assert sandbox.source("$OSTCOUNT") == "2"
        assert deploy.OST_COUNT_BEGIN not in sandbox.cfg.read_text()
        assert not (sandbox.files / "ost3").exists()

    def test_kept_files_survive_userspace_only(self, sandbox: _Sandbox) -> None:
        sandbox.cfg.write_text(self._DISKS)
        self._configure(sandbox, 8)
        (sandbox.files / "ost8").write_text("x")
        self._configure(sandbox, 4, prune=False)
        assert (sandbox.files / "ost8").exists()

    def test_a_ram_ost_block_is_dropped(self, sandbox: _Sandbox) -> None:
        """Appended after ours, it would otherwise keep winning."""
        sandbox.cfg.write_text(
            self._DISKS
            + "\n# --- RAM OST configuration (generated by ltvm deploy) ---\n"
            "OSTCOUNT=4\nOSTDEV1=/dev/ram0\n"
            "# --- END RAM OST configuration ---\n"
        )
        self._configure(sandbox, 8)
        assert "RAM OST" not in sandbox.cfg.read_text()
        assert sandbox.source("$OSTDEV1") == "/dev/vdc"
        assert sandbox.source("$OSTCOUNT") == "8"

    def test_space_counts_files_about_to_be_reformatted(
        self, sandbox: _Sandbox
    ) -> None:
        """mkfs.lustre recreates a file-backed target empty, so what a
        kept file holds now is space it may use again."""
        sandbox.files.mkdir()
        (sandbox.files / "ost3").write_bytes(b"x" * (64 * 1024))
        (sandbox.files / "ost9").write_bytes(b"x" * (64 * 1024))
        free = deploy.prepare_ost_files("10.0.0.1", [3], prune=True)
        avail, used = (int(x) for x in sandbox.stdout[-1].split())
        assert used >= 64
        assert free == avail + used
        assert (sandbox.files / "ost3").exists()
        assert not (sandbox.files / "ost9").exists()


class TestDeployOstCountWiring:
    """deploy_to_vm applies --ost-count after the disk block."""

    def test_not_called_when_unset(self, staging: Path) -> None:
        vm = _make_vm(mdt_disks=1, ost_disks=2)
        with (
            patch("ltvm_pkg.deploy._stream_tree"),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch("ltvm_pkg.deploy.configure_test_disks"),
            patch("ltvm_pkg.deploy.configure_ost_count") as mock_cnt,
        ):
            deploy.deploy_to_vm(vm, staging)
        mock_cnt.assert_not_called()

    def test_zero_is_passed_on(self, staging: Path) -> None:
        """0 is a request (back to the disks), not "unset"."""
        vm = _make_vm(mdt_disks=1, ost_disks=2)
        with (
            patch("ltvm_pkg.deploy._stream_tree"),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch("ltvm_pkg.deploy.configure_test_disks"),
            patch("ltvm_pkg.deploy.configure_ost_count") as mock_cnt,
        ):
            deploy.deploy_to_vm(vm, staging, ost_count=0)
        assert mock_cnt.call_args.args[1] == 0

    def test_after_the_disks_and_pruning_only_with_modules(
        self, staging: Path
    ) -> None:
        calls: list[str] = []
        vm = _make_vm(mdt_disks=1, ost_disks=2, disk_size=12345)
        with (
            patch("ltvm_pkg.deploy._stream_tree"),
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch(
                "ltvm_pkg.deploy.configure_test_disks",
                side_effect=lambda *a, **k: calls.append("disks"),
            ),
            patch(
                "ltvm_pkg.deploy.configure_ost_count",
                side_effect=lambda *a, **k: calls.append("count"),
            ) as mock_cnt,
        ):
            deploy.deploy_to_vm(vm, staging, ost_count=8)
            assert mock_cnt.call_args.kwargs["prune"] is True
            deploy.deploy_to_vm(vm, staging, ost_count=8, userspace_only=True)
            assert mock_cnt.call_args.kwargs["prune"] is False
        assert calls[:2] == ["disks", "count"]
        assert mock_cnt.call_args.args == (vm.ip, 8, 2, 12345)


class TestCmdDeployOstCount:
    """deploy-lustre --ost-count reaches deploy_to_vm, or is refused."""

    def _run(self, tmp_path: Path, **flags: Any) -> tuple[int, MagicMock]:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_build import staging_path

        build_path = tmp_path / "lustre-release"
        _setup_lustre_tree(build_path)
        staging = staging_path(
            build_path, "rocky9", arch="x86_64", kernel="5.14-rhel9.7"
        )
        staging.mkdir(parents=True)
        (staging / "lustre.ko").write_text("")
        (staging / ".ltvm-staging-stamp").write_text("")
        _mark_staging_fresh(staging, build_path, _stub_tc())

        vm = _make_vm(name="co1-osts", ip="10.0.0.16")
        vm.os_id = "rocky9"
        vm.save()

        args = _deploy_args(vm="co1-osts", lustre_tree=str(build_path))
        for k, v in flags.items():
            setattr(args, k, v)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch("ltvm_pkg.cli.deploy_to_vm") as deploy_mock,
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=""),
            ),
        ):
            rc = cli_mod.cmd_deploy(args)
        return rc, deploy_mock

    def test_forwarded(self, tmp_sockets: Path, tmp_path: Path) -> None:
        rc, deploy_mock = self._run(tmp_path, ost_count=8)
        assert rc == 0
        assert deploy_mock.call_args.kwargs["ost_count"] == 8

    def test_absent_is_none(self, tmp_sockets: Path, tmp_path: Path) -> None:
        """A plain redeploy leaves the VM's count as it is."""
        rc, deploy_mock = self._run(tmp_path)
        assert rc == 0
        assert deploy_mock.call_args.kwargs["ost_count"] is None

    def test_negative_refused(self, tmp_sockets: Path, tmp_path: Path) -> None:
        rc, deploy_mock = self._run(tmp_path, ost_count=-1)
        assert rc == 1
        deploy_mock.assert_not_called()

    def test_with_ram_osts_refused(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        rc, deploy_mock = self._run(tmp_path, ost_count=8, ram_osts=4)
        assert rc == 1
        deploy_mock.assert_not_called()


class TestTargetsUnformatted:
    """deploy-lustre's hint that a suite needs llmount.sh first."""

    def _check(self, rc: int, **disks: int) -> tuple[bool, MagicMock]:
        vm = VMInfo(name="co9-x", ip="192.168.100.9", **disks)
        with patch.object(
            deploy, "run_ssh", return_value=MagicMock(returncode=rc)
        ) as ssh:
            return deploy.targets_unformatted(vm), ssh

    def test_blank_disk_is_unformatted(self) -> None:
        unformatted, ssh = self._check(2, mdt_disks=1, ost_disks=2)
        assert unformatted
        assert "/dev/vdb" in ssh.call_args.args[1]

    def test_formatted_disk_is_not(self) -> None:
        assert not self._check(0, mdt_disks=1)[0]

    def test_blkid_error_is_not_taken_as_blank(self) -> None:
        assert not self._check(4, ost_disks=1)[0]

    def test_no_disks_asks_nothing(self) -> None:
        unformatted, ssh = self._check(2)
        assert not unformatted
        assert not ssh.called

    def test_hung_ssh_is_not_taken_as_blank(self) -> None:
        vm = VMInfo(name="co9-x", ip="192.168.100.9", mdt_disks=1)
        with patch.object(
            deploy, "run_ssh", side_effect=subprocess.TimeoutExpired("ssh", 15)
        ):
            assert not deploy.targets_unformatted(vm)


class TestKeptBlocks:
    """A deploy streams the staging's stock cfg/local.sh over the VM's;
    the OST count, FSTYPE and cluster blocks come back unless the deploy
    set them again.  Run for real against a cfg/local.sh."""

    STOCK = "OSTCOUNT=${OSTCOUNT:-2}\nFSTYPE=${FSTYPE:-ldiskfs}\n"
    IP = "10.0.0.1"
    SIZE = 500 * 1024 * 1024

    def _deployed(self, box: _Sandbox) -> None:
        box.cfg.write_text(self.STOCK)
        deploy.configure_test_disks(self.IP, 1, 2, self.SIZE)
        deploy.configure_ost_count(self.IP, 8, 2, self.SIZE)
        deploy.configure_fstype(self.IP, "zfs")

    def _redeploy(self, box: _Sandbox, skip: set[str] | None = None) -> None:
        kept = deploy.read_kept_blocks(self.IP)
        box.cfg.write_text(self.STOCK)
        deploy.configure_test_disks(self.IP, 1, 2, self.SIZE)
        deploy.restore_kept_blocks(self.IP, kept, skip or set())

    def test_a_plain_redeploy_keeps_count_and_fstype(
        self, sandbox: _Sandbox
    ) -> None:
        """Regression: --userspace-only after --ost-count 8 --zfs left
        OSTCOUNT 4 and FSTYPE ldiskfs on a mounted 8-OST ZFS fs."""
        self._deployed(sandbox)
        self._redeploy(sandbox)
        assert sandbox.source("$OSTCOUNT") == "8"
        assert sandbox.source("$OSTDEV8") == str(sandbox.files / "ost8")
        assert sandbox.source("$FSTYPE") == "zfs"
        body = sandbox.cfg.read_text()
        assert body.count(deploy.OST_COUNT_BEGIN) == 1
        assert body.count(deploy.FSTYPE_BEGIN) == 1

    def test_what_the_deploy_set_is_not_put_back(
        self, sandbox: _Sandbox
    ) -> None:
        self._deployed(sandbox)
        kept = deploy.read_kept_blocks(self.IP)
        sandbox.cfg.write_text(self.STOCK)
        deploy.configure_fstype(self.IP, "ldiskfs")
        deploy.restore_kept_blocks(self.IP, kept, {deploy.FSTYPE_BEGIN})
        assert sandbox.source("$FSTYPE") == "ldiskfs"
        assert sandbox.source("$OSTCOUNT") == "8"

    def test_the_environment_picks_the_backend(self, sandbox: _Sandbox) -> None:
        """Regression: `FSTYPE=ldiskfs conf-sanity.sh` formatted ZFS."""
        self._deployed(sandbox)
        env = {"PATH": "/usr/bin:/bin", "FSTYPE": "ldiskfs"}
        assert sandbox.source("$FSTYPE", env) == "ldiskfs"
        assert sandbox.source("$FSTYPE") == "zfs"

    def test_an_older_pinned_fstype_comes_back_overridable(
        self, sandbox: _Sandbox
    ) -> None:
        sandbox.cfg.write_text(
            self.STOCK + "\n# --- FSTYPE (generated by ltvm deploy) ---\n"
            "FSTYPE=zfs\n# --- END FSTYPE ---\n"
        )
        self._redeploy(sandbox)
        env = {"PATH": "/usr/bin:/bin", "FSTYPE": "ldiskfs"}
        assert sandbox.source("$FSTYPE") == "zfs"
        assert sandbox.source("$FSTYPE", env) == "ldiskfs"

    def test_an_older_environment_block_is_replaced(
        self, sandbox: _Sandbox
    ) -> None:
        sandbox.cfg.write_text(
            f"{deploy.ENV_SAVE_BEGIN} ---\n"
            "_ltvm_env_MDSCOUNT=${MDSCOUNT-}\n"
            "_ltvm_env_OSTCOUNT=${OSTCOUNT-}\n"
            f"{deploy.ENV_SAVE_END} ---\n" + self.STOCK
        )
        deploy.configure_fstype(self.IP, "zfs")
        body = sandbox.cfg.read_text()
        assert body.startswith(deploy.ENV_SAVE_BEGIN)
        assert body.count(deploy.ENV_SAVE_BEGIN) == 1
        env = {"PATH": "/usr/bin:/bin", "FSTYPE": "ldiskfs"}
        assert sandbox.source("$FSTYPE", env) == "ldiskfs"

    def test_the_cluster_block_is_kept(self, sandbox: _Sandbox) -> None:
        """Regression: a single-node deploy to a cluster member dropped
        its cluster block, and cluster llmount then formatted a
        single-node filesystem on the first client."""
        from ltvm_pkg.vm_cluster import CLUSTER_BLOCK_BEGIN, CLUSTER_BLOCK_END

        sandbox.cfg.write_text(
            self.STOCK + f"{CLUSTER_BLOCK_BEGIN} (generated) ---\n"
            "mds_HOST=co1-mds\nPDSH='pdsh -S -Rssh -w'\n"
            f"{CLUSTER_BLOCK_END} ---\n"
        )
        self._redeploy(sandbox)
        assert sandbox.source("$mds_HOST") == "co1-mds"
        assert sandbox.source('"$PDSH"') == "pdsh -S -Rssh -w"

    def test_nothing_to_keep_sends_nothing(self) -> None:
        with patch("ltvm_pkg.deploy.run_ssh") as ssh:
            deploy.restore_kept_blocks(self.IP, {}, set())
        ssh.assert_not_called()


class TestDeployKeepsBlocks:
    """deploy_to_vm reads the blocks before the stream and restores
    those it did not write."""

    def _deploy(self, staging: Path, **kw: Any) -> tuple[list[str], Any]:
        calls: list[str] = []
        vm = _make_vm(mdt_disks=1, ost_disks=2)
        with (
            patch("ltvm_pkg.deploy.run_ssh", return_value=_ok()),
            patch(
                "ltvm_pkg.deploy._stream_tree",
                side_effect=lambda *a, **k: calls.append("stream"),
            ),
            patch(
                "ltvm_pkg.deploy.read_kept_blocks",
                side_effect=lambda *a: calls.append("read") or {"b": "x"},
            ),
            patch("ltvm_pkg.deploy.configure_test_disks"),
            patch("ltvm_pkg.deploy.configure_ost_count"),
            patch("ltvm_pkg.deploy.configure_fstype"),
            patch("ltvm_pkg.deploy.verify_deployed_modules"),
            patch("ltvm_pkg.deploy.restore_kept_blocks") as restore,
        ):
            deploy.deploy_to_vm(vm, staging, **kw)
        return calls, restore

    def test_read_before_the_stream(self, staging: Path) -> None:
        calls, restore = self._deploy(staging, userspace_only=True)
        assert calls == ["read", "stream"]
        assert restore.call_args.args[1:] == ({"b": "x"}, set())

    def test_what_was_set_is_skipped(self, staging: Path) -> None:
        _, restore = self._deploy(staging, ost_count=4, fstype="ldiskfs")
        assert restore.call_args.args[2] == {
            deploy.OST_COUNT_BEGIN,
            deploy.FSTYPE_BEGIN,
        }

    def test_ram_osts_outrank_a_kept_count(self, staging: Path) -> None:
        with patch("ltvm_pkg.deploy.configure_ram_osts"):
            _, restore = self._deploy(staging, ram_osts=2)
        assert restore.call_args.args[2] == {deploy.OST_COUNT_BEGIN}


class TestAusterWrapper:
    """Images from before the wrapper get it at deploy."""

    def test_written_only_when_absent(self, tmp_path: Path) -> None:
        script = deploy.AUSTER_WRAPPER_SCRIPT.replace(
            "/usr/local/bin", str(tmp_path / "bin")
        )
        subprocess.run(["bash", "-c", script], check=True)
        wrapper = tmp_path / "bin/auster"
        body = wrapper.read_text()
        assert body.startswith("#!/bin/sh\n")
        assert '[ -x "$d/auster" ] && exec "$d/auster" "$@"' in body
        assert wrapper.stat().st_mode & 0o111
        wrapper.write_text("mine")
        subprocess.run(["bash", "-c", script], check=True)
        assert wrapper.read_text() == "mine"

    def test_matches_the_images(self) -> None:
        tools = Path("targets/common/build-tools.sh").read_text()
        assert deploy._AUSTER_WRAPPER in tools


# -- stale modules the build did not produce --------------------------


class TestUnstagedModules:
    """A client-only build over an image with a server build must not
    leave the old server modules (lfsck, mdt, ...) in the same
    directory, where modprobe fails with "Unknown symbol"."""

    def _tree(self, base: Path, files: list[str]) -> None:
        for f in files:
            p = base / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("")

    def test_removes_only_unstaged_modules_in_staged_dirs(
        self, tmp_path: Path
    ) -> None:
        lus = "lib/modules/5.14/extra/kernel/fs/lustre"
        lnet = "lib/modules/5.14/extra/kernel/net/lustre"
        staging = tmp_path / "staging"
        self._tree(
            staging, [f"{lus}/lustre.ko", f"{lus}/osc.ko", f"{lnet}/lnet.ko"]
        )
        root = tmp_path / "vm"
        self._tree(
            root,
            [
                f"{lus}/lustre.ko",
                f"{lus}/osc.ko.xz",
                f"{lus}/lfsck.ko",
                f"{lus}/mdt.ko",
                f"{lus}/README",
                f"{lnet}/lnet.ko",
                "lib/modules/5.14/extra/zfs/zfs.ko",
            ],
        )
        script = deploy.unstaged_modules_script(staging, root=str(root))
        r = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=True
        )
        left = sorted(
            str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
        )
        assert left == sorted(
            [
                f"{lus}/README",
                f"{lus}/lustre.ko",
                f"{lus}/osc.ko.xz",
                f"{lnet}/lnet.ko",
                "lib/modules/5.14/extra/zfs/zfs.ko",
            ]
        )
        assert r.stdout.count("removed ") == 2

    def test_no_staged_modules_no_script(self, tmp_path: Path) -> None:
        (tmp_path / "usr/bin").mkdir(parents=True)
        assert deploy.unstaged_modules_script(tmp_path) == ""

    def test_userspace_only_does_not_prune(self, staging: Path) -> None:
        ssh_cmds: list[str] = []
        with (
            patch("ltvm_pkg.deploy._stream_tree"),
            patch(
                "ltvm_pkg.deploy.run_ssh",
                side_effect=lambda ip, cmd, **kw: ssh_cmds.append(cmd) or _ok(),
            ),
        ):
            deploy.deploy_to_vm(_make_vm(), staging, userspace_only=True)
        assert not any("removed" in c for c in ssh_cmds)
