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


@pytest.fixture(autouse=True)
def _requirements_met(request):
    """Launch tests build QEMU args; TestRequirements checks the gate."""
    if request.node.get_closest_marker("requirements"):
        yield
        return
    with patch("ltvm_pkg.shmrdma_req.refusal", return_value=None):
        yield


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
        assert (
            f"{shmrdma.SHARED_RAM_BACKEND},id=ram0,size=4096M,share=on" in args
        )

    def test_numa_nodes_are_shared(self) -> None:
        joined = " ".join(qemu_run.smp_args(4, 4096, 2, shared=True))
        assert (
            f"{shmrdma.SHARED_RAM_BACKEND},id=numa0,size=2048M,share=on"
            in joined
        )
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
    assert f"{shmrdma.SHARED_RAM_BACKEND},id=ram0" in joined
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
    EXE = "/opt/ltvm/cache/shmrdmad"

    def _daemon(
        self, tmp_path: Path, rec: str, uid: int = -1
    ) -> shmrdma.Daemon:
        d = shmrdma.Daemon("tester", os.geteuid() if uid < 0 else uid, tmp_path)
        d.pid_path.write_text(rec)
        return d

    def _proc(self, uid: int, exe: str):
        return patch(
            "ltvm_pkg.shmrdma._proc_owner_exe", return_value=(uid, exe)
        )

    def test_own_process_with_its_binary(self, tmp_path: Path) -> None:
        d = self._daemon(tmp_path, f"{os.getpid()} abc {self.EXE}\n")
        with self._proc(os.geteuid(), self.EXE):
            assert d.pid() == os.getpid()

    def test_other_binary_refused(self, tmp_path: Path) -> None:
        d = self._daemon(tmp_path, f"{os.getpid()} abc /tmp/evil\n")
        with self._proc(os.geteuid(), self.EXE):
            assert d.pid() == 0

    def test_not_shmrdmad_refused(self, tmp_path: Path) -> None:
        d = self._daemon(tmp_path, f"{os.getpid()} abc /usr/bin/python3\n")
        with self._proc(os.geteuid(), "/usr/bin/python3"):
            assert d.pid() == 0

    def test_other_uid_refused(self, tmp_path: Path) -> None:
        d = self._daemon(
            tmp_path, f"{os.getpid()} abc {self.EXE}\n", os.geteuid() + 1
        )
        with self._proc(os.geteuid(), self.EXE):
            assert d.pid() == 0

    def test_gone_process(self, tmp_path: Path) -> None:
        d = self._daemon(tmp_path, f"{os.getpid()} abc {self.EXE}\n")
        with self._proc(-1, ""):
            assert d.pid() == 0

    def test_proc_owner_exe_self(self) -> None:
        import sys

        uid, exe = shmrdma._proc_owner_exe(os.getpid())
        assert uid == os.geteuid()
        assert Path(exe).resolve() == Path(sys.executable).resolve()

    def test_proc_owner_exe_ps(self) -> None:
        r = subprocess.CompletedProcess(
            [], 0, stdout="  501 /Users/x/My Cache/shmrdmad\n", stderr=""
        )
        with (
            patch("ltvm_pkg.shmrdma._LINUX", False),
            patch("ltvm_pkg.shmrdma.subprocess.run", return_value=r),
        ):
            assert shmrdma._proc_owner_exe(1) == (
                501,
                "/Users/x/My Cache/shmrdmad",
            )
        gone = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with (
            patch("ltvm_pkg.shmrdma._LINUX", False),
            patch("ltvm_pkg.shmrdma.subprocess.run", return_value=gone),
        ):
            assert shmrdma._proc_owner_exe(1) == (-1, "")

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
            f"{shmrdma.SHARED_RAM_BACKEND},id=ram0,size=2048M,share=on,"
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
    def _argv(self, vmdir: Path, sock: Path, spec: str) -> str:
        vm = _make_vm(vmdir)
        vm.vcpus = 4
        vm.nics = [
            NicSpec.parse(f"shmnet:{sock},{spec}" if spec else f"shmnet:{sock}")
        ]
        vm.nic_ips = ["172.16.100.50"]
        h = _LaunchHarness()
        vm.save()
        _run_launch(vm, h)
        return " ".join(h.qemu_argv or [])

    def test_single_by_default(self, tmp_vmdir, daemon_socket) -> None:  # noqa: F811
        joined = self._argv(tmp_vmdir, daemon_socket, "")
        assert "queues=" not in joined and "mq=on" not in joined

    def test_opt_in(self, tmp_vmdir, daemon_socket) -> None:  # noqa: F811
        joined = self._argv(tmp_vmdir, daemon_socket, "queues=4")
        assert "chardev=charnet1,queues=4" in joined
        assert "mrg_rxbuf=on,mq=on,vectors=10" in joined

    @pytest.mark.parametrize("bad", ["0", "9", "x"])
    def test_bounds(self, bad: str) -> None:
        with pytest.raises(NicSpecError):
            NicSpec.parse(f"shmnet:queues={bad}")

    def test_round_trip(self) -> None:
        spec = NicSpec.parse("shmnet:queues=8")
        assert str(spec) == "shmnet:queues=8"
        assert NicSpec.parse(str(spec)).queues == 8
        with pytest.raises(NicSpecError):
            NicSpec.parse("tcp:queues=2")


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


def test_guest_files_parse() -> None:
    """The boot hook and the EID tool baked into images are well formed."""
    for rel in shmrdma.GUEST_FILES:
        path = shmrdma.SRC_DIR / rel
        assert os.access(path, os.X_OK), rel
    r = subprocess.run(
        ["bash", "-n", str(shmrdma.SRC_DIR / "guest/shmrdma-boot.sh")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    compile(
        (shmrdma.SRC_DIR / "tools/smc_ueid.py").read_text(),
        "smc_ueid.py",
        "exec",
    )


def test_guest_files_feed_image_hash(tmp_path: Path) -> None:
    from ltvm_pkg.target_config import TargetConfig

    tc = TargetConfig("rocky10")
    before = tc.input_hash("image")
    src = tmp_path / "shmrdma"
    shutil.copytree(shmrdma.SRC_DIR, src)
    with (src / "guest" / "shmrdma-boot.sh").open("a") as f:
        f.write("# changed\n")
    with patch("ltvm_pkg.shmrdma.SRC_DIR", src):
        assert tc.input_hash("image") != before


@pytest.mark.requirements
class TestRequirements:
    def _req(self, name, feats, ok, hard=True, fix=None):
        from ltvm_pkg import shmrdma_req as sr

        return sr.Req(name, frozenset(feats), ok, "detail", fix=fix, hard=hard)

    def test_nothing_asked(self) -> None:
        from ltvm_pkg import shmrdma_req as sr

        with patch("ltvm_pkg.shmrdma_req.host_checks") as hc:
            assert sr.refusal(set()) is None
            hc.assert_not_called()

    def test_only_the_asked_features_refuse(self) -> None:
        from ltvm_pkg import shmrdma_req as sr

        reqs = [
            self._req("cc", {sr.SHMRDMA, sr.SHMNET}, False),
            self._req("numactl", {sr.HOST_NODE}, False, hard=False),
            self._req("taskset", {sr.HOST_NODE}, True),
        ]
        with patch("ltvm_pkg.shmrdma_req.host_checks", return_value=reqs):
            assert sr.refusal({sr.HOST_NODE}) is None
            why = sr.refusal({sr.SHMNET})
        assert why is not None
        assert "--nic shmnet" in why and "cc: detail" in why
        assert "numactl" not in why
        assert "ltvm doctor --fix --shmrdma" in why

    def test_target_in_the_fix_command(self, tmp_path: Path) -> None:
        from ltvm_pkg import shmrdma_req as sr

        t = sr.Target(
            "rocky10",
            "6.12-rhel10.2",
            image=tmp_path / "x.ext4",
            kernel_dir=tmp_path,
        )
        with patch("ltvm_pkg.shmrdma_req.host_checks", return_value=[]):
            why = sr.refusal({sr.SHMRDMA, sr.SMC}, t)
        assert why is not None
        assert "image has the shmrdma guest module" in why
        # SMC is never a refusal
        assert "SMC" not in why
        assert (
            "ltvm doctor --fix --shmrdma rocky10 --kernel 6.12-rhel10.2" in why
        )

    def test_image_and_smc_checks(self, tmp_path: Path) -> None:
        from ltvm_pkg import shmrdma_req as sr

        (tmp_path / "meta.json").write_text(json.dumps({"shmrdma_kmod": True}))
        (tmp_path / "build-tree").mkdir()
        (tmp_path / "build-tree" / ".config").write_text("#\nCONFIG_SMC=m\n")
        t = sr.Target("rocky10", image=tmp_path / "x.ext4", kernel_dir=tmp_path)
        assert all(r.ok for r in sr.target_checks(t))
        (tmp_path / "build-tree" / ".config").write_text(
            "# CONFIG_SMC is not set\n"
        )
        smc = [r for r in sr.target_checks(t) if "SMC" in r.name][0]
        assert not smc.ok and not smc.hard

    def test_unbuilt_target(self) -> None:
        from ltvm_pkg import shmrdma_req as sr

        t = sr.Target("rocky10", problem="No kernel matching 'x'\nRun: ...")
        (r,) = sr.target_checks(t)
        assert not r.ok and r.detail == "No kernel matching 'x'"

    def test_host_checks_read_qemu(self) -> None:
        from ltvm_pkg import shmrdma_req as sr

        helps = {
            "device": 'name "vhost-user-fs-pci", bus PCI',
            "object": f"{shmrdma.SHARED_RAM_BACKEND}\n",
        }
        with (
            patch("ltvm_pkg.shmrdma_req._qemu_help", side_effect=helps.get),
            patch(
                "ltvm_pkg.shmrdma_req._qemu_has_vhost_user_netdev",
                return_value=True,
            ),
            patch("ltvm_pkg.qemu_run.qemu_binds_numa", return_value=False),
        ):
            reqs = {r.name: r for r in sr.host_checks()}
        assert reqs["QEMU vhost-user-fs-pci"].ok
        assert reqs[f"QEMU {shmrdma.SHARED_RAM_BACKEND}"].ok
        assert reqs["QEMU vhost-user netdev"].ok
        with (
            patch("ltvm_pkg.shmrdma_req._qemu_help", return_value=""),
            patch("ltvm_pkg.qemu_run.qemu_binds_numa", return_value=True),
        ):
            reqs = {r.name: r for r in sr.host_checks()}
        assert not reqs["QEMU vhost-user-fs-pci"].ok
        assert not reqs["QEMU vhost-user netdev"].ok
        assert reqs["NUMA memory binding"].ok

    def test_vhost_user_netdev_probe(self) -> None:
        """Help lists vhost-user even where it is not built in."""
        from ltvm_pkg import shmrdma_req as sr

        def run(err: str) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess([], 1, stdout="", stderr=err)

        built = run('-netdev vhost-user,...: chardev "ltvm-probe" not found')
        missing = run(
            "network backend 'vhost-user' is not compiled into this binary"
        )
        with patch(
            "ltvm_pkg.shmrdma_req._qemu_help", return_value="vhost-user\n"
        ):
            with patch(
                "ltvm_pkg.shmrdma_req.subprocess.run", return_value=built
            ):
                assert sr._qemu_has_vhost_user_netdev()
            with patch(
                "ltvm_pkg.shmrdma_req.subprocess.run", return_value=missing
            ):
                assert not sr._qemu_has_vhost_user_netdev()

    def test_doctor_fixes(self, capsys) -> None:
        from ltvm_pkg import shmrdma_req as sr

        fixed = []
        reqs = [
            self._req(
                "cc", {sr.SHMRDMA}, False, fix=lambda: fixed.append("cc")
            ),
            self._req("qemu", {sr.SHMRDMA}, False),
            self._req("numa", {sr.HOST_NODE}, False, hard=False),
            self._req("ok", {sr.SHMRDMA}, True),
        ]
        t = sr.Target("rocky10")
        with (
            patch("ltvm_pkg.shmrdma_req.host_checks", return_value=reqs),
            patch("ltvm_pkg.shmrdma_req.target_checks", return_value=[]),
        ):
            assert sr.doctor(t, fix=False) == (2, 0)
            assert fixed == []
            assert sr.doctor(t, fix=True) == (2, 1)
        assert fixed == ["cc"]
        out = capsys.readouterr().out
        assert "cannot fix automatically" in out

    def test_launch_refuses(self, tmp_vmdir: Path) -> None:  # noqa: F811
        vm = _make_vm(tmp_vmdir)
        vm.host_node = 0
        vm.save()
        with (
            patch("ltvm_pkg.shmrdma_req.refusal", return_value="needs numactl"),
            pytest.raises(SystemExit),
        ):
            _run_launch(vm, _LaunchHarness())


def test_install_wants_numactl() -> None:
    import inspect

    from ltvm_pkg import host_setup

    assert '"numactl": "numactl"' in inspect.getsource(
        host_setup.check_prerequisites
    )
