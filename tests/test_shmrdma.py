"""shmrdma: shared guest RAM, the RDMA device, shmnet NICs and the daemon."""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import qemu_run, shmrdma
from ltvm_pkg.nic_spec import NicSpec, NicSpecError
from ltvm_pkg.vm_state import VMInfo
from tests.test_qemu_run import (  # noqa: F401
    _force_linux_host,
    _LaunchHarness,
    _make_vm,
    _run_launch,
    tmp_vmdir,
)


@pytest.fixture
def daemon_socket(tmp_path: Path):
    """A listening unix socket standing in for a hand-run shmrdmad."""
    path = tmp_path / "shmrdma.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.listen(1)
    yield path
    s.close()


@pytest.fixture
def own_daemon(tmp_vmdir: Path):  # noqa: F811
    """ltvm's own daemon, already up: its sockets exist, nothing runs."""
    d = shmrdma.Daemon("tester", os.geteuid(), tmp_vmdir / "shmrdma")
    d.dir.mkdir()
    socks = []
    for path in (d.rdma_socket, d.net_socket):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(path))
        socks.append(s)
    with (
        patch("ltvm_pkg.shmrdma.daemon", return_value=d),
        patch("ltvm_pkg.shmrdma.ensure_daemon", return_value=d) as ensure,
    ):
        yield d, ensure
    for s in socks:
        s.close()


class TestSharedRam:
    def test_plain(self) -> None:
        args = qemu_run.smp_args(4, 4096, 0, shared=True)
        assert args[:4] == ["-smp", "4", "-m", "4096"]
        assert "memory-backend-memfd,id=ram0,size=4096M,share=on" in args

    def test_numa_nodes_are_shared(self) -> None:
        joined = " ".join(qemu_run.smp_args(4, 4096, 2, shared=True))
        assert "memory-backend-memfd,id=numa0,size=2048M,share=on" in joined
        assert "memory-backend-ram" not in joined

    def test_unshared_is_unchanged(self) -> None:
        assert qemu_run.smp_args(4, 4096, 0) == ["-smp", "4", "-m", "4096"]


class TestShmnetSpec:
    def test_plain(self) -> None:
        spec = NicSpec.parse("shmnet")
        assert str(spec) == "shmnet"
        assert spec.socket == ""
        # the guest sets it up as an ordinary tcp NIC
        assert spec.cmdline_entry() == "tcp"

    def test_socket_round_trips(self) -> None:
        spec = NicSpec.parse("shmnet:socket=/run/x.sock,net=tcp3")
        assert spec.socket == "/run/x.sock"
        assert str(spec) == "shmnet:/run/x.sock,net=tcp3"
        assert NicSpec.parse(str(spec)) == spec

    def test_net_must_be_tcp(self) -> None:
        with pytest.raises(NicSpecError):
            NicSpec.parse("shmnet:net=o2ib1")

    def test_relative_socket(self) -> None:
        with pytest.raises(NicSpecError):
            NicSpec.parse("shmnet:x.sock")


def test_info_round_trip(tmp_vmdir: Path) -> None:  # noqa: F811
    vm = _make_vm(tmp_vmdir)
    vm.shmrdma = "auto"
    vm.nics = [NicSpec.parse("shmnet")]
    vm.save()
    back = VMInfo.load(vm.name)
    assert back.shmrdma == "auto"
    assert back.nics == [NicSpec(type="shmnet")]
    assert back.shared_ram


def test_launch_hand_run_daemon(
    tmp_vmdir: Path,  # noqa: F811
    daemon_socket: Path,
) -> None:
    vm = _make_vm(tmp_vmdir)
    vm.shmrdma = str(daemon_socket)
    h = _LaunchHarness()
    vm.save()
    _run_launch(vm, h)
    argv = h.qemu_argv or []
    joined = " ".join(argv)
    assert argv[argv.index("-machine") + 1].endswith(",memory-backend=ram0")
    assert f"socket,id=shmrdma0,path={daemon_socket}" in joined
    assert "vhost-user-fs-pci,chardev=shmrdma0,tag=shmrdma" in joined
    assert "fc_shmrdma=1" in argv[argv.index("-append") + 1]


def test_launch_refuses_without_daemon(tmp_vmdir: Path) -> None:  # noqa: F811
    vm = _make_vm(tmp_vmdir)
    vm.shmrdma = str(tmp_vmdir / "nothing.sock")
    with pytest.raises(SystemExit):
        vm.save()
        _run_launch(vm, _LaunchHarness())


def test_launch_own_daemon(tmp_vmdir: Path, own_daemon) -> None:  # noqa: F811
    d, ensure = own_daemon
    vm = _make_vm(tmp_vmdir)
    vm.shmrdma = "auto"
    vm.nics = [NicSpec.parse("shmnet")]
    vm.nic_ips = ["172.16.100.50"]
    h = _LaunchHarness()
    vm.save()
    _run_launch(vm, h)
    ensure.assert_called_once()
    argv = h.qemu_argv or []
    joined = " ".join(argv)
    assert f"socket,id=shmrdma0,path={d.rdma_socket}" in joined
    assert f"socket,id=charnet1,path={d.net_socket}" in joined
    assert "vhost-user,id=net1,chardev=charnet1" in joined
    append = argv[argv.index("-append") + 1]
    assert "fc_nics=tcp" in append
    assert "fc_shmrdma=1" in append
    # recorded, so whoever asks can tell which daemon it is attached to
    back = VMInfo.load(vm.name)
    assert back.shm_sockets == [str(d.rdma_socket), str(d.net_socket)]
    assert shmrdma.vm_uses(back, d)
    other = shmrdma.Daemon("other", 4321, tmp_vmdir / "other")
    assert not shmrdma.vm_uses(back, other)


def test_launch_shmnet_only(
    tmp_vmdir: Path,  # noqa: F811
    daemon_socket: Path,
) -> None:
    vm = _make_vm(tmp_vmdir)
    vm.nics = [NicSpec(type="shmnet", socket=str(daemon_socket))]
    vm.nic_ips = ["172.16.100.50"]
    h = _LaunchHarness()
    vm.save()
    _run_launch(vm, h)
    argv = h.qemu_argv or []
    joined = " ".join(argv)
    assert "memory-backend-memfd,id=ram0" in joined
    assert f"socket,id=charnet1,path={daemon_socket}" in joined
    assert "vhost-user-fs-pci" not in joined
    assert "fc_shmrdma" not in argv[argv.index("-append") + 1]


class TestMemoryCharge:
    def test_shared_counts_overcommit_times(self) -> None:
        assert qemu_run.charged_mb(4096, True, 2.0) == 8192
        assert qemu_run.charged_mb(4096, False, 2.0) == 4096
        assert qemu_run.charged_mb(4096, True, 1.0) == 4096


def test_image_has_kmod(tmp_path: Path) -> None:
    image = tmp_path / "rootfs.ext4"
    assert not shmrdma.image_has_kmod(image)
    (tmp_path / "meta.json").write_text(json.dumps({"shmrdma_kmod": True}))
    assert shmrdma.image_has_kmod(image)


def test_kmod_sources_feed_image_hash(tmp_path: Path) -> None:
    from ltvm_pkg.target_config import TargetConfig

    tc = TargetConfig("rocky10")
    before = tc.input_hash("image")
    src = tmp_path / "shmrdma"
    shutil.copytree(shmrdma.SRC_DIR, src)
    with (src / "kmod" / "shmrdma.c").open("a") as f:
        f.write("/* changed */\n")
    with patch("ltvm_pkg.shmrdma.SRC_DIR", src):
        assert tc.input_hash("image") != before


@pytest.mark.skipif(
    not (shutil.which("cc") or shutil.which("gcc")),
    reason="needs a C compiler",
)
def test_daemon_start_status_stop(tmp_vmdir: Path, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_vmdir / "cache"))
    with (
        patch("ltvm_pkg.qemu_run._guest_scope", return_value=[]),
    ):
        d = shmrdma.ensure_daemon()
        try:
            assert d.dir == tmp_vmdir / f"shmrdma-{os.geteuid()}"
            assert stat.S_IMODE(os.stat(d.dir).st_mode) == 0o700
            assert d.rdma_socket.is_socket() and d.net_socket.is_socket()
            assert stat.S_IMODE(os.stat(d.rdma_socket).st_mode) == 0o600
            st = shmrdma.status()
            assert st["running"] and st["current"] and st["vms"] == []
            pid = d.pid()
            # a second ensure keeps the running daemon
            assert shmrdma.ensure_daemon().pid() == pid
        finally:
            msg = shmrdma.stop_daemon()
        assert "stopped" in msg
        assert not d.pid()
        assert not d.rdma_socket.exists()
        assert shmrdma.stop_daemon() == "shmrdmad is not running"


def test_stop_refuses_with_vms(tmp_vmdir: Path) -> None:  # noqa: F811
    d = shmrdma.Daemon("tester", os.geteuid(), tmp_vmdir)
    with (
        patch("ltvm_pkg.qemu_run._launch_lock") as launch_lock,
        patch("ltvm_pkg.shmrdma.daemon", return_value=d),
        patch.object(shmrdma.Daemon, "pid", return_value=4242),
        patch("ltvm_pkg.shmrdma.attached_vms", return_value=["co1-a"]),
        patch("ltvm_pkg.shmrdma.os.kill") as kill,
    ):
        with pytest.raises(RuntimeError, match="co1-a"):
            shmrdma.stop_daemon()
        kill.assert_not_called()
        # the check runs under the launch lock
        launch_lock.assert_called_once()


class TestPrivateDir:
    def test_created_private(self, tmp_vmdir: Path) -> None:  # noqa: F811
        d = shmrdma.run_dir(os.geteuid())
        assert d == tmp_vmdir / f"shmrdma-{os.geteuid()}"
        assert stat.S_IMODE(os.lstat(d).st_mode) == 0o700

    def test_not_created_for_status(self, tmp_vmdir: Path) -> None:  # noqa: F811
        shmrdma.run_dir(os.geteuid(), create=False)
        assert not (tmp_vmdir / f"shmrdma-{os.geteuid()}").exists()

    def test_loose_mode_tightened(self, tmp_vmdir: Path) -> None:  # noqa: F811
        d = tmp_vmdir / f"shmrdma-{os.geteuid()}"
        d.mkdir(mode=0o777)
        os.chmod(d, 0o777)
        assert shmrdma.run_dir(os.geteuid()) == d
        assert stat.S_IMODE(os.lstat(d).st_mode) == 0o700

    def test_planted_symlink_refused(
        self,
        tmp_vmdir: Path,  # noqa: F811
        monkeypatch,
    ) -> None:
        elsewhere = tmp_vmdir / "elsewhere"
        elsewhere.mkdir()
        (tmp_vmdir / f"shmrdma-{os.geteuid()}").symlink_to(elsewhere)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_vmdir / "state"))
        d = shmrdma.run_dir(os.geteuid())
        assert d == tmp_vmdir / "state" / "ltvm" / "shmrdma"

    def test_foreign_owner_refused(self, tmp_vmdir: Path, monkeypatch) -> None:  # noqa: F811
        d = tmp_vmdir / f"shmrdma-{os.geteuid()}"
        d.mkdir(mode=0o700)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_vmdir / "state"))
        real = os.lstat

        def lstat(path, *a, **k):
            st = real(path, *a, **k)
            if Path(path) == d:
                fake = list(st)
                fake[stat.ST_UID] = st.st_uid + 1
                return os.stat_result(fake)
            return st

        with patch("ltvm_pkg.shmrdma.os.lstat", side_effect=lstat):
            assert shmrdma.run_dir(os.geteuid()) != d


class TestPidFile:
    def _daemon(self, tmp_path: Path, rec: str) -> shmrdma.Daemon:
        d = shmrdma.Daemon("tester", os.geteuid(), tmp_path)
        d.pid_path.write_text(rec)
        return d

    def test_own_process_with_its_binary(self, tmp_path: Path) -> None:
        exe = os.readlink("/proc/self/exe")
        d = self._daemon(tmp_path, f"{os.getpid()} abc {exe}\n")
        with patch(
            "ltvm_pkg.shmrdma.Path.read_text", return_value="shmrdmad\n"
        ):
            assert d.pid() == os.getpid()

    def test_other_binary_refused(self, tmp_path: Path) -> None:
        d = self._daemon(tmp_path, f"{os.getpid()} abc /tmp/evil\n")
        with patch(
            "ltvm_pkg.shmrdma.Path.read_text", return_value="shmrdmad\n"
        ):
            assert d.pid() == 0

    def test_other_uid_refused(self, tmp_path: Path) -> None:
        exe = os.readlink("/proc/self/exe")
        d = shmrdma.Daemon("tester", os.geteuid() + 1, tmp_path)
        d.pid_path.write_text(f"{os.getpid()} abc {exe}\n")
        with patch(
            "ltvm_pkg.shmrdma.Path.read_text", return_value="shmrdmad\n"
        ):
            assert d.pid() == 0

    def test_symlinked_pid_file_ignored(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.write_text(f"{os.getpid()} abc x\n")
        d = shmrdma.Daemon("tester", os.geteuid(), tmp_path)
        d.pid_path.symlink_to(target)
        assert d.pid() == 0


def test_root_does_not_start_a_daemon(tmp_vmdir: Path) -> None:  # noqa: F811
    d = shmrdma.Daemon("tester", os.geteuid() + 1, tmp_vmdir)
    with (
        patch("ltvm_pkg.shmrdma.daemon", return_value=d),
        patch.object(shmrdma.Daemon, "pid", return_value=0),
        patch("ltvm_pkg.shmrdma.subprocess.Popen") as popen,
    ):
        with pytest.raises(RuntimeError, match="as root"):
            shmrdma.ensure_daemon()
        popen.assert_not_called()


def test_start_timeout_kills(tmp_vmdir: Path) -> None:  # noqa: F811
    d = shmrdma.Daemon("tester", os.geteuid(), tmp_vmdir)
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 4242
    with (
        patch("ltvm_pkg.shmrdma.daemon", return_value=d),
        patch("ltvm_pkg.shmrdma.build_daemon", return_value=(Path("/x"), "h")),
        patch("ltvm_pkg.qemu_run._guest_scope", return_value=[]),
        patch("ltvm_pkg.shmrdma.subprocess.Popen", return_value=proc),
        patch("ltvm_pkg.shmrdma.time.monotonic", side_effect=[0, 11]),
    ):
        with pytest.raises(RuntimeError, match="did not come up"):
            shmrdma.ensure_daemon()
    proc.kill.assert_called_once()
    assert not d.pid_path.exists()


@pytest.mark.parametrize(
    "bad", ["/a,b", "/a|b", "/a\nNICS=x", "/a\tb", "rel/x", "/a:b"]
)
def test_socket_paths_rejected(bad: str) -> None:
    from ltvm_pkg import vm_commands

    with pytest.raises(NicSpecError):
        NicSpec.parse(f"shmnet:socket={bad}")
    with pytest.raises(SystemExit):
        vm_commands._check_shmrdma_socket(bad)


def test_daemon_builds_clean() -> None:
    """The daemon compiles without warnings under the flags ltvm uses."""
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        pytest.skip("needs a C compiler")
    r = subprocess.run(
        [cc, *shmrdma.DAEMON_CFLAGS, "-fsyntax-only", "shmrdmad.c"],
        cwd=shmrdma.SRC_DIR / "daemon",
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


class TestHostNode:
    def test_backend_bound(self) -> None:
        args = qemu_run.smp_args(2, 2048, 0, host_node=3)
        assert (
            "memory-backend-ram,id=ram0,size=2048M,host-nodes=3,policy=bind"
            in args
        )

    def test_numa_backends_bound(self) -> None:
        joined = " ".join(qemu_run.smp_args(4, 4096, 2, host_node=1))
        assert joined.count("host-nodes=1,policy=bind") == 2

    def test_shared_and_bound(self) -> None:
        args = qemu_run.smp_args(2, 2048, 0, shared=True, host_node=0)
        assert (
            "memory-backend-memfd,id=ram0,size=2048M,share=on,"
            "host-nodes=0,policy=bind" in args
        )

    def _launch(self, vmdir: Path, binds: bool, numactl: str | None):
        vm = _make_vm(vmdir)
        vm.host_node = 1
        h = _LaunchHarness()
        with (
            patch("ltvm_pkg.qemu_run.host_node_cpus", return_value="8-15"),
            patch("ltvm_pkg.qemu_run.qemu_binds_numa", return_value=binds),
            patch("ltvm_pkg.qemu_run.shutil.which", return_value=numactl),
        ):
            vm.save()
            _run_launch(vm, h)
        return h.qemu_argv or []

    def test_qemu_binds(self, tmp_vmdir: Path) -> None:  # noqa: F811
        argv = self._launch(tmp_vmdir, True, "/usr/bin/numactl")
        assert argv[:3] == ["taskset", "-c", "8-15"]
        assert argv[argv.index("-machine") + 1].endswith(",memory-backend=ram0")
        assert "host-nodes=1,policy=bind" in " ".join(argv)

    def test_numactl_binds(self, tmp_vmdir: Path) -> None:  # noqa: F811
        argv = self._launch(tmp_vmdir, False, "/usr/bin/numactl")
        assert argv[:3] == ["numactl", "--cpunodebind=1", "--membind=1"]
        assert "host-nodes" not in " ".join(argv)

    def test_cpus_only(self, tmp_vmdir: Path) -> None:  # noqa: F811
        argv = self._launch(tmp_vmdir, False, None)
        assert argv[:3] == ["taskset", "-c", "8-15"]
        assert "host-nodes" not in " ".join(argv)
        assert "memory-backend=ram0" not in " ".join(argv)

    def test_launch_refuses_missing_node(self, tmp_vmdir: Path) -> None:  # noqa: F811
        vm = _make_vm(tmp_vmdir)
        vm.host_node = 999
        with pytest.raises(SystemExit):
            vm.save()
            _run_launch(vm, _LaunchHarness())

    def test_info_round_trip(self, tmp_vmdir: Path) -> None:  # noqa: F811
        vm = _make_vm(tmp_vmdir)
        vm.save()
        assert VMInfo.load(vm.name).host_node == -1
        vm.host_node = 2
        vm.save()
        assert VMInfo.load(vm.name).host_node == 2


class TestShmnetQueues:
    def _argv(self, vmdir: Path, sock: Path, vcpus: int) -> str:
        vm = _make_vm(vmdir)
        vm.vcpus = vcpus
        vm.nics = [NicSpec(type="shmnet", socket=str(sock))]
        vm.nic_ips = ["172.16.100.50"]
        h = _LaunchHarness()
        vm.save()
        _run_launch(vm, h)
        return " ".join(h.qemu_argv or [])

    def test_one_pair_per_vcpu(self, tmp_vmdir, daemon_socket) -> None:  # noqa: F811
        joined = self._argv(tmp_vmdir, daemon_socket, 4)
        assert "chardev=charnet1,queues=4" in joined
        assert "mrg_rxbuf=on,mq=on,vectors=10" in joined

    def test_capped(self, tmp_vmdir, daemon_socket) -> None:  # noqa: F811
        joined = self._argv(tmp_vmdir, daemon_socket, 32)
        assert "queues=8" in joined and "vectors=18" in joined

    def test_single(self, tmp_vmdir, daemon_socket) -> None:  # noqa: F811
        joined = self._argv(tmp_vmdir, daemon_socket, 1)
        assert "queues=" not in joined and "mq=on" not in joined


def test_daemon_nodes(monkeypatch, tmp_path: Path) -> None:
    conf = tmp_path / "ltvm.conf"
    conf.write_text("[shmrdma]\nnodes = 0, 2\n")
    monkeypatch.setenv("LTVM_SITE_CONFIG", str(conf))
    monkeypatch.delenv("LTVM_SHMRDMAD_NODES", raising=False)
    assert shmrdma.daemon_nodes() == [0, 2]
    monkeypatch.setenv("LTVM_SHMRDMAD_NODES", "1")
    assert shmrdma.daemon_nodes() == [1]
    monkeypatch.setenv("LTVM_SHMRDMAD_NODES", "")
    assert shmrdma.daemon_nodes() == []
    with patch(
        "ltvm_pkg.qemu_run.host_node_cpus",
        side_effect=lambda n: {0: "0-7", 2: "16-23"}.get(n, ""),
    ):
        monkeypatch.setenv("LTVM_SHMRDMAD_NODES", "0,2")
        assert shmrdma.daemon_cpus() == "0-7,16-23"
        monkeypatch.setenv("LTVM_SHMRDMAD_NODES", "5")
        assert shmrdma.daemon_cpus() == ""
