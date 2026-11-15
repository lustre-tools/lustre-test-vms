"""shmrdma: shared guest RAM, the RDMA device, shmnet NICs and the daemon."""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
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

_REAL_STATE_HOME = shmrdma._state_home

# sun_path holds 104 bytes on macOS, 108 on Linux; the tests' sockets
# go at most this far below tmp_path (shmrdma-<uid>/rdma.sock).
_SOCKET_SUFFIX_MAX = 32


@pytest.fixture
def tmp_path(tmp_path: Path) -> Iterator[Path]:
    """A short directory when pytest's own would leave no room for a
    unix socket path: on macOS always, on Linux in a deep workspace."""
    if len(str(tmp_path)) + _SOCKET_SUFFIX_MAX < 104:
        yield tmp_path
        return
    d = Path(tempfile.mkdtemp(prefix="ltvm-", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


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


def test_launch_refuses_a_dead_daemons_socket(
    tmp_vmdir: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The socket file a killed daemon left behind refuses connects."""
    path = tmp_path / "stale.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    try:
        vm = _make_vm(tmp_vmdir)
        vm.shmrdma = str(path)
        vm.save()
        with (
            patch("ltvm_pkg.qemu_run.die", side_effect=SystemExit) as die,
            pytest.raises(SystemExit),
        ):
            shmrdma.verify_sockets(vm)
        assert "nothing listens" in die.call_args[0][0]
    finally:
        s.close()


def test_own_sockets_checked_for_qemu(tmp_vmdir: Path) -> None:  # noqa: F811
    """A daemon directory QEMU's -chardev cannot name is refused by name."""
    d = shmrdma.Daemon("tester", os.geteuid(), tmp_vmdir / "a,b")
    vm = _make_vm(tmp_vmdir)
    vm.shmrdma = "auto"
    with (
        patch("ltvm_pkg.shmrdma.daemon_for", return_value=d),
        patch("ltvm_pkg.qemu_run.die", side_effect=SystemExit) as die,
        pytest.raises(SystemExit),
    ):
        shmrdma.verify_sockets(vm)
    msg = die.call_args[0][0]
    assert f"directory {d.dir} cannot hold its sockets" in msg


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


def test_daemon_starts_outside_the_launch_lock(
    tmp_vmdir: Path,  # noqa: F811
    own_daemon,
) -> None:
    """A daemon build must not stall every other user's VM starts."""
    from contextlib import contextmanager

    d, ensure = own_daemon
    held = []

    @contextmanager
    def lock():
        held.append(True)
        try:
            yield
        finally:
            held.pop()

    ensure.side_effect = lambda *_: (held and pytest.fail("lock held"), d)[1]
    vm = _make_vm(tmp_vmdir)
    vm.shmrdma = "auto"
    vm.save()
    with patch("ltvm_pkg.qemu_run._launch_lock", lock):
        _run_launch(vm, _LaunchHarness())
    ensure.assert_called_once()


def test_sockets_gone_by_the_lock_refuse(
    tmp_vmdir: Path,  # noqa: F811
    own_daemon,
) -> None:
    """A daemon stopped between the start and the lock is refused."""
    d, ensure = own_daemon
    ensure.side_effect = lambda *_: (d.rdma_socket.unlink(), d)[1]
    vm = _make_vm(tmp_vmdir)
    vm.shmrdma = "auto"
    vm.save()
    with pytest.raises(SystemExit):
        _run_launch(vm, _LaunchHarness())


def test_launch_refuses_another_users_socket(
    tmp_vmdir: Path,  # noqa: F811
    daemon_socket: Path,
) -> None:
    vm = _make_vm(tmp_vmdir)
    vm.shmrdma = str(daemon_socket)
    vm.save()
    real = os.lstat

    def lstat(p, *a, **kw):
        st = real(p, *a, **kw)
        if str(p) != str(daemon_socket):
            return st
        f = list(st[:10])
        f[4] = os.geteuid() + 1
        return os.stat_result(f)

    with patch("ltvm_pkg.shmrdma.os.lstat", side_effect=lstat):
        with pytest.raises(SystemExit):
            _run_launch(vm, _LaunchHarness())


def test_launch_refuses_a_root_socket_for_a_users_vm(
    tmp_vmdir: Path,  # noqa: F811
    daemon_socket: Path,
) -> None:
    vm = _make_vm(tmp_vmdir)
    vm.shmrdma = str(daemon_socket)
    vm.save()
    real = os.lstat

    def lstat(p, *a, **kw):
        st = real(p, *a, **kw)
        if str(p) != str(daemon_socket):
            return st
        f = list(st[:10])
        f[4] = 0
        return os.stat_result(f)

    with patch("ltvm_pkg.shmrdma.os.lstat", side_effect=lstat):
        with pytest.raises(SystemExit):
            _run_launch(vm, _LaunchHarness())


class TestReplaceable:
    """A socket is trusted only where nobody else can swap it."""

    def test_private_dirs_pass(self, tmp_path: Path) -> None:
        d = tmp_path / "d"
        d.mkdir(mode=0o700)
        assert shmrdma.replaceable(str(d / "s.sock"), {0, os.geteuid()}) is None

    def test_writable_dir(self, tmp_path: Path) -> None:
        d = tmp_path / "d"
        d.mkdir()
        d.chmod(0o777)
        why = shmrdma.replaceable(str(d / "s.sock"), {0, os.geteuid()})
        # paths are reported resolved; /tmp is a symlink on macOS
        assert why == f"{d.resolve()} is writable by other users"
        d.chmod(0o1777)
        assert shmrdma.replaceable(str(d / "s.sock"), {0, os.geteuid()}) is None

    def test_foreign_dir(self, tmp_path: Path) -> None:
        d = tmp_path / "d"
        d.mkdir(mode=0o700)
        why = shmrdma.replaceable(str(d / "s.sock"), {0})
        assert why is not None and "belongs to uid" in why

    def test_through_a_symlink(self, tmp_path: Path) -> None:
        """The directory holding a symlink counts, and its target's too."""
        open_dir = tmp_path / "open"
        open_dir.mkdir()
        safe = tmp_path / "safe"
        safe.mkdir(mode=0o700)
        (open_dir / "link").symlink_to(safe)
        open_dir.chmod(0o777)
        why = shmrdma.replaceable(
            str(open_dir / "link" / "s.sock"), {0, os.geteuid()}
        )
        assert why == f"{open_dir.resolve()} is writable by other users"
        open_dir.chmod(0o755)
        (safe / "in").mkdir()
        (safe / "in").chmod(0o777)
        (open_dir / "link2").symlink_to(safe / "in")
        why = shmrdma.replaceable(
            str(open_dir / "link2" / "s.sock"), {0, os.geteuid()}
        )
        assert why == f"{(safe / 'in').resolve()} is writable by other users"

    def test_foreign_symlink(self, tmp_path: Path) -> None:
        """A symlink another user owns can be repointed after the check."""
        safe = tmp_path / "safe"
        safe.mkdir(mode=0o700)
        link = tmp_path / "link"
        link.symlink_to(safe)
        real_lstat = os.lstat
        walked = str(tmp_path.resolve() / "link")

        def lstat(p, *a, **kw):
            st = real_lstat(p, *a, **kw)
            if str(p) == walked:
                return os.stat_result((st.st_mode, *st[1:4], 4242, *st[5:]))
            return st

        uids = {0, os.geteuid()}
        assert shmrdma.replaceable(str(link / "s.sock"), uids) is None
        with patch("ltvm_pkg.shmrdma.os.lstat", side_effect=lstat):
            why = shmrdma.replaceable(str(link / "s.sock"), uids)
        assert why is not None and "symlink belonging to uid 4242" in why

    def test_dotdot_after_symlink(self, tmp_path: Path) -> None:
        """'..' leaves the symlink's target, not the symlink's directory."""
        top = tmp_path / "top"
        (top / "in").mkdir(parents=True)
        top.chmod(0o777)
        safe = tmp_path / "safe"
        safe.mkdir(mode=0o700)
        (safe / "link").symlink_to(top / "in")
        try:
            why = shmrdma.replaceable(
                str(safe / "link" / ".." / "s.sock"), {0, os.geteuid()}
            )
        finally:
            top.chmod(0o755)
        assert why == f"{top.resolve()} is writable by other users"

    def test_relative_path(self, tmp_path: Path, monkeypatch) -> None:
        d = tmp_path / "d"
        d.mkdir()
        d.chmod(0o777)
        monkeypatch.chdir(tmp_path)
        try:
            why = shmrdma.replaceable("d/s.sock", {0, os.geteuid()})
        finally:
            d.chmod(0o755)
        assert why == f"{d.resolve()} is writable by other users"

    def test_launch_refuses(self, tmp_vmdir: Path, tmp_path: Path) -> None:  # noqa: F811
        d = tmp_path / "shared"
        d.mkdir()
        path = d / "rdma.sock"
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(path))
        d.chmod(0o777)
        try:
            vm = _make_vm(tmp_vmdir)
            vm.shmrdma = str(path)
            vm.save()
            with pytest.raises(SystemExit):
                _run_launch(vm, _LaunchHarness())
        finally:
            d.chmod(0o755)
            s.close()


def test_long_socket_path_refused(tmp_vmdir: Path) -> None:  # noqa: F811
    from ltvm_pkg.nic_spec import SUN_PATH_SIZE, socket_path_problem

    long = "/" + "x" * SUN_PATH_SIZE
    assert socket_path_problem(long)
    assert socket_path_problem("/" + "x" * (SUN_PATH_SIZE - 2)) is None
    d = shmrdma.Daemon("tester", os.geteuid(), Path(long))
    with pytest.raises(RuntimeError, match="shorter than"):
        shmrdma.ensure_daemon(d)


def test_root_reach_refused(tmp_vmdir: Path) -> None:  # noqa: F811
    vm = _make_vm(tmp_vmdir)
    denied = subprocess.CompletedProcess(
        [], 1, "", "stat: cannot statx '/h/s': Permission denied\n"
    )
    with (
        patch.object(shmrdma, "_LINUX", True),
        patch("ltvm_pkg.shmrdma.os.geteuid", return_value=1000),
        patch("ltvm_pkg.priv.sudo_run", return_value=denied),
    ):
        with pytest.raises(SystemExit):
            shmrdma.check_root_reach(vm, ["/h/s"])
    ok = subprocess.CompletedProcess([], 0, "socket\n", "")
    with (
        patch.object(shmrdma, "_LINUX", True),
        patch("ltvm_pkg.shmrdma.os.geteuid", return_value=1000),
        patch("ltvm_pkg.priv.sudo_run", return_value=ok),
    ):
        shmrdma.check_root_reach(vm, ["/h/s"])


def test_launch_records_the_daemon(tmp_vmdir: Path, own_daemon) -> None:  # noqa: F811
    d, _ensure = own_daemon
    vm = _make_vm(tmp_vmdir)
    vm.shmrdma = "auto"
    vm.save()
    with patch.object(shmrdma.Daemon, "identity", return_value="77:123"):
        _run_launch(vm, _LaunchHarness())
    assert VMInfo.load(vm.name).shm_daemon == "77:123"


def test_proc_start_is_stable() -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("needs /proc")
    start = shmrdma._proc_start(os.getpid())
    assert start and start == shmrdma._proc_start(os.getpid())
    assert shmrdma._proc_start(2**22 + 12345) == ""


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


OLD_IMG = "base-20261001T000000-aaaaaa.ext4"
NEW_IMG = "base-20261101T000000-bbbbbb.ext4"


def _image_dir(tmp_path: Path, meta: dict) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name in (OLD_IMG, NEW_IMG):
        (tmp_path / name).write_bytes(b"")
    (tmp_path / "current.ext4").symlink_to(NEW_IMG)
    (tmp_path / "meta.json").write_text(json.dumps(meta))
    return tmp_path


def test_image_has_kmod_flag_describes_current_only(tmp_path: Path) -> None:
    d = _image_dir(tmp_path, {"shmrdma_kmod": True})
    assert shmrdma.image_has_kmod(d / NEW_IMG)
    assert shmrdma.image_has_kmod(d / "current.ext4")
    # an older VM's base predates the build the flag describes
    assert not shmrdma.image_has_kmod(d / OLD_IMG)


def test_image_has_kmod_per_file(tmp_path: Path) -> None:
    d = _image_dir(
        tmp_path,
        {
            "shmrdma_kmod": False,
            "shmrdma_kmod_images": {OLD_IMG: True, NEW_IMG: False},
        },
    )
    assert shmrdma.image_has_kmod(d / OLD_IMG)
    assert not shmrdma.image_has_kmod(d / NEW_IMG)


def test_image_has_kmod_no_meta(tmp_path: Path) -> None:
    assert not shmrdma.image_has_kmod(tmp_path / NEW_IMG)


def test_kmod_images_carries_older_files(tmp_path: Path) -> None:
    from ltvm_pkg.image_build import _kmod_images

    for name in (OLD_IMG, NEW_IMG):
        (tmp_path / name).write_bytes(b"")
    prev = {"shmrdma_kmod": True, "shmrdma_kmod_images": {"gone.ext4": True}}
    got = _kmod_images(prev, tmp_path / OLD_IMG, tmp_path / NEW_IMG, False)
    assert got == {OLD_IMG: True, NEW_IMG: False}


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
def test_daemon_unpinned_without_taskset(tmp_vmdir: Path, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_vmdir / "cache"))
    which = shutil.which
    with (
        patch("ltvm_pkg.qemu_run._guest_scope", return_value=[]),
        patch("ltvm_pkg.shmrdma.daemon_cpus", return_value="0"),
        patch(
            "ltvm_pkg.shmrdma.shutil.which",
            side_effect=lambda n: None if n == "taskset" else which(n),
        ),
    ):
        d = shmrdma.ensure_daemon()
    try:
        assert d.pid()
    finally:
        shmrdma.stop_daemon()


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


@pytest.mark.skipif(
    not (shutil.which("cc") or shutil.which("gcc")),
    reason="needs a C compiler",
)
def test_daemon_starts_when_scope_refused(tmp_vmdir: Path, monkeypatch) -> None:  # noqa: F811
    """As QEMU's launch: a refused systemd-run scope is retried without."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_vmdir / "cache"))
    fake = tmp_vmdir / "systemd-run"
    fake.write_text(
        "#!/bin/sh\necho 'Failed to start transient scope unit: x' >&2\nexit 1\n"
    )
    fake.chmod(0o755)
    with patch("ltvm_pkg.qemu_run._guest_scope", return_value=[str(fake)]):
        d = shmrdma.ensure_daemon()
        try:
            assert d.pid() and d.rdma_socket.is_socket()
            assert "transient scope" in d.log_path.read_text()
        finally:
            shmrdma.stop_daemon()


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


def test_attached_vms_only_the_users(tmp_vmdir: Path) -> None:  # noqa: F811
    d = shmrdma.Daemon("tester", os.geteuid(), tmp_vmdir)
    for name in ("co1-mine", "co1-theirs"):
        vm = _make_vm(tmp_vmdir, name=name)
        vm.shmrdma = "auto"
        vm.shm_sockets = [str(d.rdma_socket)]
        vm.save()
    real = os.stat

    def stat_(path, *a, **k):
        st = real(path, *a, **k)
        if Path(path).name == "co1-theirs.info":
            fake = list(st)
            fake[stat.ST_UID] = os.geteuid() + 1
            return os.stat_result(fake)
        return st

    with (
        patch("ltvm_pkg.shmrdma.os.stat", side_effect=stat_),
        patch("ltvm_pkg.qemu_run.is_running", return_value=True),
    ):
        assert shmrdma.attached_vms(d) == ["co1-mine"]


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

    def test_loose_mode_tightened_without_lchmod(
        self,
        tmp_vmdir: Path,  # noqa: F811
    ) -> None:
        """glibc before 2.32 (EL8) has no chmod that refuses symlinks."""
        d = tmp_vmdir / f"shmrdma-{os.geteuid()}"
        d.mkdir()
        os.chmod(d, 0o755)
        with patch(
            "ltvm_pkg.shmrdma.os.chmod", side_effect=NotImplementedError
        ):
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
        monkeypatch.setattr(
            shmrdma, "_state_home", lambda uid: tmp_vmdir / "state"
        )
        d = shmrdma.run_dir(os.geteuid())
        assert d == shmrdma._candidate_dirs(os.geteuid())[1]
        assert d.parent == tmp_vmdir / "state" / "ltvm"

    def test_foreign_owner_refused(self, tmp_vmdir: Path, monkeypatch) -> None:  # noqa: F811
        d = tmp_vmdir / f"shmrdma-{os.geteuid()}"
        d.mkdir(mode=0o700)
        monkeypatch.setattr(
            shmrdma, "_state_home", lambda uid: tmp_vmdir / "state"
        )
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
            # status, stop and doctor look where create would put it
            assert shmrdma.run_dir(os.geteuid(), create=False) != d

    def test_live_daemon_wins(self, tmp_vmdir: Path, monkeypatch) -> None:  # noqa: F811
        monkeypatch.setattr(
            shmrdma, "_state_home", lambda uid: tmp_vmdir / "state"
        )
        first = tmp_vmdir / f"shmrdma-{os.geteuid()}"
        state = shmrdma._candidate_dirs(os.geteuid())[1]
        first.mkdir(mode=0o700)
        state.mkdir(mode=0o700, parents=True)

        def pid(self: shmrdma.Daemon) -> int:
            return 4242 if self.dir == state else 0

        with patch.object(shmrdma.Daemon, "pid", pid):
            assert shmrdma.run_dir(os.geteuid()) == state
            assert shmrdma.run_dir(os.geteuid(), create=False) == state

    def test_root_and_user_agree_on_the_state_dir(self, monkeypatch) -> None:
        monkeypatch.setattr(shmrdma, "_state_home", _REAL_STATE_HOME)
        monkeypatch.setenv("XDG_STATE_HOME", "/elsewhere")
        uid = os.geteuid()
        mine = shmrdma._candidate_dirs(uid)
        with patch("ltvm_pkg.shmrdma.os.geteuid", return_value=uid + 1):
            assert shmrdma._candidate_dirs(uid) == mine
        home = Path(shmrdma.pwd.getpwuid(uid).pw_dir)
        host = socket.gethostname().split(".")[0]
        assert mine[1] == home / f".local/state/ltvm/shmrdma-{host}"

    def test_hosts_sharing_a_home_do_not_share_it(self) -> None:
        uid = os.geteuid()
        with patch("ltvm_pkg.shmrdma.socket.gethostname", return_value="a.x"):
            a = shmrdma._candidate_dirs(uid)
        with patch("ltvm_pkg.shmrdma.socket.gethostname", return_value="b.x"):
            b = shmrdma._candidate_dirs(uid)
        assert a[1] != b[1] and a[1].name == "shmrdma-a"

    def test_root_never_creates(self, tmp_vmdir: Path) -> None:  # noqa: F811
        uid = os.geteuid()
        with patch("ltvm_pkg.shmrdma.os.geteuid", return_value=0):
            d = shmrdma.run_dir(uid if uid else 1000, create=True)
        assert not d.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="/proc")
def test_proc_info_deleted_binary() -> None:
    with patch(
        "ltvm_pkg.shmrdma.os.readlink",
        return_value="/c/ltvm/shmrdmad (deleted)",
    ):
        _, _, exe = shmrdma._proc_info(os.getpid())
    assert exe == "/c/ltvm/shmrdmad"


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
            "ltvm_pkg.shmrdma._proc_info",
            return_value=(uid, Path(exe).name, exe),
        )

    def test_own_process_with_its_binary(self, tmp_path: Path) -> None:
        d = self._daemon(tmp_path, f"{os.getpid()} abc {self.EXE}\n")
        with self._proc(os.geteuid(), self.EXE):
            assert d.pid() == os.getpid()

    def test_binary_path_with_spaces(self, tmp_path: Path) -> None:
        exe = "/home/test/My Cache/ltvm/shmrdmad"
        d = self._daemon(tmp_path, f"{os.getpid()} abc {exe}\n")
        with self._proc(os.geteuid(), exe):
            assert d.pid() == os.getpid()
        assert d.binary_hash() == "abc"

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
        with patch("ltvm_pkg.shmrdma._proc_info", return_value=(-1, "", "")):
            assert d.pid() == 0

    def test_proc_info_self(self) -> None:
        # comm is whatever this process was started as (pytest, python3)
        uid, comm, exe = shmrdma._proc_info(os.getpid())
        assert uid == os.geteuid() and comm
        if sys.platform == "darwin":
            # A framework build runs Python.app, not sys.executable.
            assert Path(exe).is_file()
        else:
            assert Path(exe).resolve() == Path(sys.executable).resolve()

    def test_proc_info_ps(self) -> None:
        r = subprocess.CompletedProcess(
            [], 0, stdout="  501 /Users/x/My Cache/shmrdmad\n", stderr=""
        )
        with (
            patch("ltvm_pkg.shmrdma._LINUX", False),
            patch("ltvm_pkg.shmrdma.subprocess.run", return_value=r),
        ):
            assert shmrdma._proc_info(1) == (
                501,
                "shmrdmad",
                "/Users/x/My Cache/shmrdmad",
            )
        gone = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with (
            patch("ltvm_pkg.shmrdma._LINUX", False),
            patch("ltvm_pkg.shmrdma.subprocess.run", return_value=gone),
        ):
            assert shmrdma._proc_info(1) == (-1, "", "")

    def test_symlinked_cache_dir(self, tmp_path: Path) -> None:
        """/proc names the resolved binary; a pid file may not."""
        real = tmp_path / "real"
        real.mkdir()
        (real / "shmrdmad").write_text("")
        (tmp_path / "link").symlink_to(real)
        exe = tmp_path / "link" / "shmrdmad"
        d = self._daemon(tmp_path, f"{os.getpid()} abc {exe}\n")
        with self._proc(os.geteuid(), str(real / "shmrdmad")):
            assert d.pid() == os.getpid()

    def test_symlinked_pid_file_ignored(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.write_text(f"{os.getpid()} abc x\n")
        d = shmrdma.Daemon("tester", os.geteuid(), tmp_path)
        d.pid_path.symlink_to(target)
        assert d.pid() == 0

    def test_fifo_pid_file_does_not_hang(self, tmp_path: Path) -> None:
        import threading

        d = shmrdma.Daemon("tester", os.geteuid(), tmp_path)
        os.mkfifo(d.pid_path)
        got = []
        t = threading.Thread(target=lambda: got.append(d.pid()), daemon=True)
        t.start()
        t.join(5)
        assert got == [0]
        os.mkfifo(d.lock_path)
        with pytest.raises(OSError, match="not a regular file"):
            with shmrdma._daemon_lock(d):
                pass


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


def test_pid_file_names_the_resolved_binary(
    tmp_vmdir: Path,  # noqa: F811
) -> None:
    real = tmp_vmdir / "real"
    real.mkdir()
    (real / "shmrdmad").write_text("")
    (tmp_vmdir / "cache").symlink_to(real)
    d = shmrdma.Daemon("tester", os.geteuid(), tmp_vmdir)
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 4242
    recorded = []

    def popen(*a, **k):
        return proc

    def sleep(_s: float) -> None:
        recorded.append(d.pid_path.read_text())
        raise RuntimeError("stop here")

    with (
        patch("ltvm_pkg.shmrdma.daemon", return_value=d),
        patch(
            "ltvm_pkg.shmrdma.build_daemon",
            return_value=(tmp_vmdir / "cache" / "shmrdmad", "h"),
        ),
        patch("ltvm_pkg.qemu_run._guest_scope", return_value=[]),
        patch("ltvm_pkg.shmrdma.subprocess.Popen", side_effect=popen),
        patch("ltvm_pkg.shmrdma.time.sleep", side_effect=sleep),
        pytest.raises(RuntimeError, match="stop here"),
    ):
        shmrdma.ensure_daemon()
    assert recorded == [f"4242 h {(real / 'shmrdmad').resolve()}\n"]


def test_stop_kills_only_the_daemon(tmp_vmdir: Path) -> None:  # noqa: F811
    """No SIGKILL for a pid the daemon gave up after the last look."""
    d = shmrdma.Daemon("tester", os.geteuid(), tmp_vmdir)
    with (
        patch.object(shmrdma.Daemon, "pid", side_effect=[4242] * 51 + [0]),
        patch("ltvm_pkg.shmrdma.attached_vms", return_value=[]),
        patch("ltvm_pkg.shmrdma.time.sleep"),
        patch("ltvm_pkg.shmrdma.os.kill") as kill,
    ):
        shmrdma._stop_locked(d, force=False)
    assert [c.args for c in kill.call_args_list] == [
        (4242, shmrdma.signal.SIGTERM)
    ]


def test_stop_holds_the_daemon_lock(tmp_vmdir: Path) -> None:  # noqa: F811
    """A `vm shmrdmad start` waits until stop has removed the files."""
    import fcntl

    d = shmrdma.Daemon("tester", os.geteuid(), tmp_vmdir)
    held = []

    def kill(pid: int, sig: int) -> None:
        fd = os.open(d.lock_path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(False)
        except BlockingIOError:
            held.append(True)
        finally:
            os.close(fd)

    with (
        patch("ltvm_pkg.qemu_run._launch_lock"),
        patch("ltvm_pkg.shmrdma.daemon", return_value=d),
        patch.object(shmrdma.Daemon, "pid", side_effect=[4242, 4242, 0]),
        patch("ltvm_pkg.shmrdma.attached_vms", return_value=[]),
        patch("ltvm_pkg.shmrdma.os.kill", side_effect=kill),
    ):
        assert shmrdma.stop_daemon() == "stopped shmrdmad (pid 4242)"
    assert held == [True]


@pytest.mark.parametrize(
    "bad", ["/a,b", "/a|b", "/a\nNICS=x", "/a\tb", "rel/x", "/a:b", "/a=b"]
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


def test_daemon_rebuilt_for_another_compiler(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    calls = []

    def cc(name: str) -> Path:
        p = tmp_path / name
        p.write_text("")
        return p

    def run(cmd, **kw):
        calls.append(cmd[0])
        Path(cmd[cmd.index("-o") + 1]).write_text(cmd[0])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("ltvm_pkg.shmrdma.subprocess.run", side_effect=run):
        monkeypatch.setenv("CC", str(cc("gcc")))
        out, digest = shmrdma.build_daemon()
        assert shmrdma.build_daemon() == (out, digest)
        monkeypatch.setenv("CC", str(cc("clang")))
        assert shmrdma.build_daemon() == (out, digest)
        monkeypatch.setenv("CC", "")
        with patch("ltvm_pkg.shmrdma.shutil.which", return_value=None):
            assert shmrdma.build_daemon() == (out, digest)
    assert calls == [str(tmp_path / "gcc"), str(tmp_path / "clang")]
    with patch("ltvm_pkg.shmrdma.DAEMON_CFLAGS", ("-O0",)):
        assert shmrdma.daemon_digest() != digest
    with patch("ltvm_pkg.shmrdma._host_abi", return_value="Linux riscv64"):
        assert shmrdma.daemon_digest() != digest


def test_cc_with_arguments(tmp_path: Path, monkeypatch) -> None:
    """CC is split as make splits it: "ccache gcc" runs ccache."""
    from ltvm_pkg import shmrdma_req as sr

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    wrapper = tmp_path / "ccache"
    wrapper.write_text("")
    wrapper.chmod(0o755)
    monkeypatch.setenv("CC", f"{wrapper} gcc -m64")
    assert shmrdma._compiler() == [str(wrapper), "gcc", "-m64"]
    calls = []

    def run(cmd, **kw):
        calls.append(cmd[:3])
        Path(cmd[cmd.index("-o") + 1]).write_text("")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("ltvm_pkg.shmrdma.subprocess.run", side_effect=run):
        shmrdma.build_daemon()
    assert calls == [[str(wrapper), "gcc", "-m64"]]
    with patch("ltvm_pkg.shmrdma_req._qemu_caps") as caps:
        caps.return_value.__getitem__.return_value = True
        r = {r.name: r for r in sr.host_checks()}["C compiler for shmrdmad"]
    assert r.ok
    monkeypatch.setenv("CC", "no-such-cc-ltvm gcc")
    with (
        patch("ltvm_pkg.shmrdma.daemon_needs_compiler", return_value=True),
        patch("ltvm_pkg.shmrdma_req._qemu_caps") as caps,
    ):
        caps.return_value.__getitem__.return_value = True
        r = {r.name: r for r in sr.host_checks()}["C compiler for shmrdmad"]
    assert not r.ok and "no-such-cc-ltvm is not on PATH" in r.detail


def test_uid_without_passwd_entry(monkeypatch) -> None:
    """A container uid with no passwd entry gets a refusal, not a
    traceback, and a --host-node VM is not refused for it."""
    from ltvm_pkg import shmrdma_req as sr

    def nobody(uid):
        raise KeyError(uid)

    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("SUDO_USER", raising=False)
    # conftest's stand-in for the passwd home
    monkeypatch.setattr(
        shmrdma,
        "_state_home",
        lambda uid: Path(shmrdma._passwd(uid).pw_dir) / ".local" / "state",
    )
    with patch("ltvm_pkg.shmrdma.pwd.getpwuid", side_effect=nobody):
        with pytest.raises(RuntimeError, match="no passwd entry"):
            shmrdma._cache_dir()
        with (
            patch("ltvm_pkg.shmrdma_req._qemu_caps") as caps,
            patch("ltvm_pkg.host_setup.is_macos", return_value=False),
        ):
            caps.return_value.__getitem__.return_value = True
            reqs = {r.name: r for r in sr.host_checks()}
        r = reqs["shmrdmad runtime directory"]
        assert not r.ok and "no passwd entry" in r.detail
        assert sr.HOST_NODE not in r.features


def test_daemon_builds_through_a_ccache_wrapper(
    tmp_path: Path, monkeypatch
) -> None:
    """ccache's masquerade link compiles only when run by its own name."""
    real = shutil.which("cc") or shutil.which("gcc")
    if not real:
        pytest.skip("needs a C compiler")
    wrapper = tmp_path / "ccache"
    wrapper.write_text(
        "#!/bin/sh\n"
        'case "${0##*/}" in\n'
        f'cc) exec {real} "$@" ;;\n'
        '*) echo "ccache: invalid option -- O" >&2; exit 1 ;;\n'
        "esac\n"
    )
    wrapper.chmod(0o755)
    masq = tmp_path / "masq"
    masq.mkdir()
    (masq / "cc").symlink_to(wrapper)
    monkeypatch.setenv("PATH", f"{masq}:{os.environ['PATH']}")
    monkeypatch.delenv("CC", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    out, _digest = shmrdma.build_daemon()
    assert out.is_file()
    # the stamp names the real compiler, so a relinked masquerade rebuilds
    assert out.with_name("cc").read_text().strip() == str(wrapper.resolve())
    with patch("ltvm_pkg.shmrdma.subprocess.run") as run:
        assert shmrdma.build_daemon()[0] == out
    run.assert_not_called()


_SHMNET_HARNESS = r"""
#define main shmrdmad_main
#include "shmrdmad.c"
#undef main

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL line %d\n", __LINE__); \
	fails++; } } while (0)

/* a partial UDP checksum, finished across odd segment splits */
static void test_fill_csum(void)
{
	srand(1);
	for (int it = 0; it < 500; it++) {
		int ulen = 8 + rand() % 1500, len = 34 + ulen, n = 0, off = 0;
		uint8_t pkt[2000];
		uint32_t s = 0, p;
		struct seg sg[8];

		for (int i = 0; i < len; i++)
			pkt[i] = rand();
		pkt[38] = ulen >> 8;
		pkt[39] = ulen;
		pkt[40] = pkt[41] = 0;
		for (int i = 26; i < 34; i += 2)
			s += pkt[i] << 8 | pkt[i + 1];
		s += 17 + ulen;
		p = s;
		for (int i = 0; i < ulen; i += 2)
			s += pkt[34 + i] << 8 | (i + 1 < ulen ? pkt[35 + i] : 0);
		while (s >> 16)
			s = (s & 0xffff) + (s >> 16);
		while (p >> 16)
			p = (p & 0xffff) + (p >> 16);
		uint16_t want = (uint16_t)~s ? (uint16_t)~s : 0xffff;

		pkt[40] = p >> 8;
		pkt[41] = p;
		while (off < len && n < 7) {
			int k = 1 + rand() % 300;

			if (off + k > len)
				k = len - off;
			sg[n].p = pkt + off;
			sg[n++].len = k;
			off += k;
		}
		if (off < len) {
			sg[n].p = pkt + off;
			sg[n++].len = len - off;
		}
		struct cursor c = { .s = sg, .n = n };
		struct vnet_hdr h = { .flags = 1, .csum_start = 34 };

		h.csum_offset = 6;

		net_fill_csum(c, len, &h);
		CHECK((pkt[40] << 8 | pkt[41]) == want);
	}
}

static int chain(struct vdev *d, struct vq *q, bool writable)
{
	struct seg segs[MAX_SEGS];
	uint64_t total;

	return vq_chain(d, q, 0, writable, segs, MAX_SEGS, &total);
}

static void test_vq_chain(void)
{
	static uint8_t mem[1 << 16];
	static struct vdev d;
	struct vq q = { .num = 8, .desc = (struct vring_desc *)mem };
	struct vring_desc *t = q.desc;

	d.nmap = 1;
	d.map[0].size = sizeof(mem);
	d.map[0].hva = mem;
	t[0] = (struct vring_desc){ 4096, 100, VRING_DESC_F_WRITE, 0 };
	CHECK(chain(&d, &q, true) == 1);
	/* RX: a zero-length or read-only descriptor refuses the chain */
	t[0] = (struct vring_desc){ 4096, 0,
		VRING_DESC_F_WRITE | VRING_DESC_F_NEXT, 1 };
	t[1] = (struct vring_desc){ 4096, 100, VRING_DESC_F_WRITE, 0 };
	CHECK(chain(&d, &q, true) == -1);
	t[0] = (struct vring_desc){ 4096, 10, VRING_DESC_F_NEXT, 1 };
	CHECK(chain(&d, &q, true) == -1);
	CHECK(chain(&d, &q, false) == 1);
	/* TX: a loop of empty descriptors ends at the step budget */
	t[0] = (struct vring_desc){ 4096, 0, VRING_DESC_F_NEXT, 0 };
	CHECK(chain(&d, &q, false) == -1);
	/* an indirect table needs only the 8-byte alignment kmalloc gives */
	struct vring_desc *it = (struct vring_desc *)(mem + 8200);

	it[0] = (struct vring_desc){ 4096, 10, VRING_DESC_F_NEXT, 1 };
	it[1] = (struct vring_desc){ 4196, 10, 0, 0 };
	t[0] = (struct vring_desc){ 8200, 2 * sizeof(*it),
		VRING_DESC_F_INDIRECT, 0 };
	CHECK(chain(&d, &q, false) == 2);
	memmove(mem + 8204, it, 2 * sizeof(*it));
	t[0].addr = 8204;
	CHECK(chain(&d, &q, false) == -1);
}

static void test_vq_map(void)
{
	static uint8_t mem[1 << 16];
	static struct vdev d;
	struct vq q = { .desc_ua = 0x1000, .avail_ua = 0x2000,
			.used_ua = 0x3000 };

	d.nmap = 1;
	d.map[0].size = sizeof(mem);
	d.map[0].uaddr = 0;
	d.map[0].hva = mem;
	/* an address with no size maps nothing a later table could leave */
	CHECK(vq_map(&d, &q) == -1);
	CHECK(!q.desc && !q.avail && !q.used);
	q.num = 8;
	CHECK(vq_map(&d, &q) == 0);
	CHECK(q.desc && q.avail && q.used);
	q.used_ua = sizeof(mem) - 8;
	CHECK(vq_map(&d, &q) == -1);
	CHECK(!q.desc && !q.avail && !q.used);
}

static void test_mac_claim(void)
{
	static struct vdev d1, d2;
	static struct netport p1 = { .dev = &d1, .attached = true };
	static struct netport p2 = { .dev = &d2, .attached = true };
	const uint8_t m[6] = { 2, 0, 0, 0, 0, 1 };

	plat_spin_init(&g_mac_lock);
	p1.next = &p2;
	g_ports = &p1;
	CHECK(net_claim_mac(&p1, m));
	CHECK(!net_claim_mac(&p2, m));
	CHECK(p1.mac == mac_key(m));
	/* the holder went quiet: the address moves */
	p1.mac_ns = now_ns() - MAC_HOLD_NS - 1;
	CHECK(net_claim_mac(&p2, m));
	CHECK(p1.mac == 0 && p2.mac == mac_key(m));
	CHECK(!net_claim_mac(&p1, m));
	/* the holder sent after the claimer read the clock */
	p2.mac_ns = now_ns() + 1000000;
	CHECK(!net_claim_mac(&p1, m));
	CHECK(p2.mac == mac_key(m));
}

int main(void)
{
	test_fill_csum();
	test_vq_chain();
	test_vq_map();
	test_mac_claim();
	printf("fails=%d\n", fails);
	return fails != 0;
}
"""


def test_shmnet_helpers(tmp_path: Path) -> None:
    """Checksum completion, RX chain rules and MAC takeover, in C."""
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        pytest.skip("needs a C compiler")
    src = tmp_path / "harness.c"
    src.write_text(_SHMNET_HARNESS)
    exe = tmp_path / "harness"
    r = subprocess.run(
        [
            cc,
            "-O1",
            "-pthread",
            "-Wno-unused-function",
            "-I",
            str(shmrdma.SRC_DIR / "daemon"),
            "-o",
            str(exe),
            str(src),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    r = subprocess.run([str(exe)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


_RETIRE_HARNESS = r"""
static void rel_hook(void *p);
#define REL(p, v) do { __atomic_store_n((p), (v), __ATOMIC_RELEASE); \
	rel_hook((void *)(p)); } while (0)
#define main shmrdmad_main
#include "shmrdmad.c"
#undef main

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL line %d\n", __LINE__); \
	fails++; } } while (0)

enum { N = 8, BASE = 1000 };

static uint8_t sqpage[4096], rqpage[4096], cqpage[4096];
static uint8_t *sqp[1] = { sqpage }, *rqp[1] = { rqpage }, *cqp[1] = { cqpage };
static struct shmrdma_qp_rec rec __attribute__((aligned(128)));
static struct shmrdma_cq_rec cqr __attribute__((aligned(128)));
static struct vdev dev;
static struct cq cq;
static struct qp qp;

/* what a guest would find after the backend died just after a store */
static struct snap {
	struct shmrdma_qp_rec r;
	uint32_t cq_prod;
} snaps[256];
static int nsnap;

static void take(void)
{
	if (nsnap < 256) {
		snaps[nsnap].r = rec;
		snaps[nsnap].cq_prod = cqr.prod;
		nsnap++;
	}
}

static void rel_hook(void *p)
{
	if ((p >= (void *)&rec && p < (void *)(&rec + 1)) ||
		(p >= (void *)&cqr && p < (void *)(&cqr + 1)))
		take();
}

static void setup(uint32_t state)
{
	memset(&rec, 0, sizeof(rec));
	memset(&cqr, 0, sizeof(cqr));
	memset(sqpage, 0, sizeof(sqpage));
	memset(rqpage, 0, sizeof(rqpage));
	memset(cqpage, 0, sizeof(cqpage));
	memset(&cq, 0, sizeof(cq));
	memset(&qp, 0, sizeof(qp));
	cq.dev = &dev;
	cq.rec = &cqr;
	cq.ring = (struct ring){ .entries = 64, .esz = 32, .npages = 1,
					.pages = cqp };
	plat_spin_init(&cq.lock);
	qp.dev = &dev;
	qp.type = SHMRDMA_QPT_UD;
	qp.state = state;
	qp.scq = qp.rcq = &cq;
	qp.rec = &rec;
	qp.sq = (struct ring){ .entries = N, .esz = 64, .npages = 1,
				.pages = sqp };
	qp.rq = (struct ring){ .entries = N, .esz = 32, .npages = 1,
				.pages = rqp };
	plat_spin_init(&qp.rq_lock);
	nsnap = 0;
	take();
}

/*
 * The guest's flush after a backend death at every snapshot: each WR
 * completes once, through a visible CQE, the limbo record or its slot;
 * an unsignaled one that ran may complete never.  A visible CQE always
 * means its slot is free.
 */
static void check_flush(bool sq, const bool *must)
{
	for (int k = 0; k < nsnap; k++) {
		const struct shmrdma_qp_rec *r = &snaps[k].r;
		uint32_t prod = snaps[k].cq_prod;
		uint32_t cons = sq ? r->sq_cons : r->rq_cons;
		uint32_t done = sq ? r->sq_done : r->rq_done;
		int count[N] = {0};

		for (uint32_t j = 0; j < prod; j++) {
			struct shmrdma_cqe *e = ring_entry(&cq.ring, j);
			uint32_t i = e->wr_id - BASE;

			CHECK(i < N);
			if (i >= N)
				continue;
			count[i]++;
			CHECK(cons > i);
		}
		if (done != cons &&
			shmrdma_limbo_owed(done, cons,
					sq ? r->sq_limbo_op : r->rq_limbo_op,
					sq ? r->sq_limbo_pos : r->rq_limbo_pos,
					prod)) {
			uint64_t id = sq ? r->sq_limbo_wr_id : r->rq_limbo_wr_id;

			CHECK(id - BASE < N);
			if (id - BASE < N)
				count[id - BASE]++;
		}
		for (uint32_t c = cons; c < N; c++)
			count[c]++;
		for (int i = 0; i < N; i++) {
			if (count[i] > 1 || (must[i] && count[i] != 1) ||
				(!count[i] && (uint32_t)i >= cons)) {
				printf("FAIL %s snapshot %d wr %d count %d\n",
					sq ? "sq" : "rq", k, i, count[i]);
				fails++;
			}
		}
	}
}

static void post_sends(void)
{
	for (int i = 0; i < N; i++) {
		struct shmrdma_sq_wqe *w = ring_entry(&qp.sq, i);

		w->wr_id = BASE + i;
		w->opcode = SHMRDMA_OP_SEND;
		w->flags = (i % 3) ? SHMRDMA_WQE_SIGNALED : 0;
	}
	rec.sq_prod = N;
}

/* sends a UD QP drops (no source GID), signaled or not */
static void test_send_retire(void)
{
	bool must[N];
	bool stalled = false;

	setup(QPS_RTS);
	post_sends();
	for (int i = 0; i < N; i++)
		must[i] = i % 3;
	CHECK(qp_process(&qp, 64, &stalled) == N);
	CHECK(rec.sq_cons == N && rec.sq_done == N);
	check_flush(true, must);
}

/* sends of a QP in error: every one completes */
static void test_send_flush(void)
{
	bool must[N];
	bool stalled = false;

	setup(QPS_ERR);
	post_sends();
	for (int i = 0; i < N; i++)
		must[i] = true;
	CHECK(qp_process(&qp, 64, &stalled) == N);
	CHECK(cqr.prod == N);
	check_flush(true, must);
}

static void test_recv_flush(void)
{
	bool must[N];

	setup(QPS_ERR);
	for (int i = 0; i < N; i++) {
		struct shmrdma_rq_wqe *w = ring_entry(&qp.rq, i);

		w->wr_id = BASE + i;
		must[i] = true;
	}
	rec.rq_prod = N;
	qp_flush_rq(&qp);
	CHECK(rec.rq_cons == N && rec.rq_done == N && cqr.prod == N);
	check_flush(false, must);
}

/* a CQ with no room: the WR's slot is freed with nothing owed */
static void test_overflow(void)
{
	bool must[N] = {0};
	bool stalled = false;

	setup(QPS_ERR);
	post_sends();
	cq.ring.entries = 4;
	CHECK(qp_process(&qp, 64, &stalled) == N);
	CHECK(cqr.prod == 4 && rec.sq_cons == N);
	CHECK(rec.sq_limbo_op == SHMRDMA_LIMBO_NONE);
	for (int i = 0; i < 4; i++)
		must[i] = true;
	check_flush(true, must);
}

int main(void)
{
	plat_spin_init(&dev.evq_lock);
	plat_spin_init(&dev.log_lock);
	test_send_retire();
	test_send_flush();
	test_recv_flush();
	test_overflow();
	printf("fails=%d\n", fails);
	return fails != 0;
}
"""


def test_completion_order(tmp_path: Path) -> None:
    """A CQE means a free slot, and a flush after any death is exact, in C."""
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        pytest.skip("needs a C compiler")
    src = tmp_path / "harness.c"
    src.write_text(_RETIRE_HARNESS)
    exe = tmp_path / "harness"
    r = subprocess.run(
        [
            cc,
            "-O1",
            "-pthread",
            "-Wno-unused-function",
            "-I",
            str(shmrdma.SRC_DIR / "daemon"),
            "-o",
            str(exe),
            str(src),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    r = subprocess.run([str(exe)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


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


class TestShmnetTso:
    def test_on_by_default(self, tmp_vmdir, daemon_socket) -> None:  # noqa: F811
        joined = TestShmnetQueues()._argv(tmp_vmdir, daemon_socket, "")
        assert "guest_tso" not in joined

    def test_off(self, tmp_vmdir, daemon_socket) -> None:  # noqa: F811
        joined = TestShmnetQueues()._argv(tmp_vmdir, daemon_socket, "tso=off")
        assert (
            "mrg_rxbuf=on,guest_tso4=off,guest_tso6=off,guest_ecn=off" in joined
        )

    def test_round_trip(self) -> None:
        spec = NicSpec.parse("shmnet:tso=off,queues=2")
        assert not spec.tso
        assert str(spec) == "shmnet:queues=2,tso=off"
        assert NicSpec.parse(str(spec)) == spec
        assert NicSpec.parse("shmnet:tso=on") == NicSpec.parse("shmnet")
        with pytest.raises(NicSpecError):
            NicSpec.parse("shmnet:tso=maybe")
        with pytest.raises(NicSpecError):
            NicSpec.parse("tcp:tso=off")


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
            self._req("cc", {sr.SHMRDMA, sr.SHMNET}, False, fix=lambda: None),
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

    def test_root_needs_the_users_daemon_running(self, tmp_path: Path) -> None:
        from ltvm_pkg import shmrdma_req as sr

        d = shmrdma.Daemon("alice", os.geteuid() + 1, tmp_path)
        with (
            patch("ltvm_pkg.shmrdma_req.host_checks", return_value=[]),
            patch("ltvm_pkg.shmrdma.daemon", return_value=d),
        ):
            why = sr.refusal({sr.SHMRDMA, sr.OWN_DAEMON})
            assert why is not None
            assert "ltvm vm shmrdmad start` as alice" in why
            assert "doctor --fix" not in why
            # a hand-run daemon's socket needs nothing of ltvm's
            assert sr.refusal({sr.SHMRDMA}) is None
            with patch.object(shmrdma.Daemon, "pid", return_value=42):
                assert sr.refusal({sr.SHMRDMA, sr.OWN_DAEMON}) is None
        own = shmrdma.Daemon("me", os.geteuid(), tmp_path)
        with (
            patch("ltvm_pkg.shmrdma_req.host_checks", return_value=[]),
            patch("ltvm_pkg.shmrdma.daemon", return_value=own),
        ):
            assert sr.refusal({sr.OWN_DAEMON}) is None

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

        _image_dir(tmp_path, {"shmrdma_kmod": True})
        (tmp_path / "build-tree").mkdir()
        (tmp_path / "build-tree" / ".config").write_text("#\nCONFIG_SMC=m\n")
        t = sr.Target("rocky10", image=tmp_path / NEW_IMG, kernel_dir=tmp_path)
        assert all(r.ok for r in sr.target_checks(t))
        (tmp_path / "build-tree" / ".config").write_text(
            "# CONFIG_SMC is not set\n"
        )
        smc = [r for r in sr.target_checks(t) if "SMC" in r.name][0]
        assert not smc.ok and not smc.hard

    def test_resolve_target_arch(self) -> None:
        """The target is looked up for --arch, else the host's arch."""
        from ltvm_pkg import shmrdma_req as sr

        arts = MagicMock(image="/a/img.ext4", kernel="/a/k/vmlinuz")
        with (
            patch(
                "ltvm_pkg.vm_state.resolve_os_artifacts", return_value=arts
            ) as roa,
            patch("ltvm_pkg.cli.util.host_arch", return_value="aarch64"),
        ):
            t = sr.resolve_target("rocky10-16k", None)
            assert roa.call_args.kwargs["arch"] == "aarch64"
            assert t.kernel_dir == Path("/a/k")
            sr.resolve_target("rocky10", None, arch="x86_64")
            assert roa.call_args.kwargs["arch"] == "x86_64"

    def test_unbuilt_target(self) -> None:
        from ltvm_pkg import shmrdma_req as sr

        t = sr.Target("rocky10", problem="No kernel matching 'x'\nRun: ...")
        (r,) = sr.target_checks(t)
        assert not r.ok and r.detail == "No kernel matching 'x'"

    def test_repairs_keep_the_arch(self, tmp_path: Path) -> None:
        """doctor --fix --arch builds the artifacts it was asked about."""
        from ltvm_pkg import shmrdma_req as sr

        arch = sr._native_arch()
        calls: list[tuple[str, ...]] = []
        with patch(
            "ltvm_pkg.shmrdma_req._ltvm", side_effect=lambda *a: calls.append(a)
        ):
            t = sr.Target("rocky10", "6.12", "/src", arch=arch)
            (r,) = sr.target_checks(t)
            assert r.fix is not None
            r.fix()
            assert f"--kernel 6.12 --arch {arch}" in r.how
            _image_dir(tmp_path, {"shmrdma_kmod": False})
            (tmp_path / "build-tree").mkdir()
            (tmp_path / "build-tree" / ".config").write_text("\n")
            t = sr.Target(
                "rocky10",
                "6.12",
                "/src",
                image=tmp_path / NEW_IMG,
                kernel_dir=tmp_path,
                arch=arch,
            )
            for r in sr.target_checks(t):
                assert r.fix is not None
                r.fix()
        assert len(calls) == 5
        for c in calls:
            assert c[c.index("--arch") + 1] == arch, c
            assert c[c.index("--kernel") + 1] == "6.12", c
        why = sr.refusal({sr.SHMRDMA}, t)
        assert why is not None
        assert f"--shmrdma rocky10 --kernel 6.12 --arch {arch}`" in why

    def test_cross_arch_image_offers_no_fix(self, tmp_path: Path) -> None:
        """A cross-built image never has the module: no rebuild is offered."""
        from ltvm_pkg import shmrdma_req as sr

        other = "x86_64" if sr._native_arch() != "x86_64" else "aarch64"
        _image_dir(tmp_path, {"shmrdma_kmod": False})
        t = sr.Target(
            "rocky10", image=tmp_path / NEW_IMG, kernel_dir=tmp_path, arch=other
        )
        kmod = [r for r in sr.target_checks(t) if "module" in r.name][0]
        assert not kmod.ok and kmod.fix is None
        assert f"on a {other} host" in kmod.how
        with patch("ltvm_pkg.shmrdma_req.host_checks", return_value=[]):
            why = sr.refusal({sr.SHMRDMA}, t)
        assert why is not None
        assert f"on a {other} host" in why
        assert "doctor --fix" not in why

    def test_host_checks_read_qemu(self) -> None:
        from ltvm_pkg import shmrdma_req as sr

        helps = {
            "device": 'name "vhost-user-fs-pci", bus PCI',
            "object": f"{shmrdma.SHARED_RAM_BACKEND}\n",
        }
        with (
            patch(
                "ltvm_pkg.shmrdma_req._qemu_help",
                side_effect=lambda what, qemu=None: helps[what],
            ),
            patch(
                "ltvm_pkg.shmrdma_req._qemu_has_vhost_user_netdev",
                return_value=True,
            ),
            patch("ltvm_pkg.qemu_run.qemu_binds_numa", return_value=False),
            patch("ltvm_pkg.host_setup.is_macos", return_value=False),
        ):
            reqs = {r.name: r for r in sr.host_checks()}
        assert reqs["QEMU vhost-user-fs-pci"].ok
        assert reqs[f"QEMU {shmrdma.SHARED_RAM_BACKEND}"].ok
        assert reqs["QEMU vhost-user netdev"].ok
        sr._caps_cache().unlink()  # another QEMU
        with (
            patch("ltvm_pkg.shmrdma_req._qemu_help", return_value=""),
            patch("ltvm_pkg.qemu_run.qemu_binds_numa", return_value=True),
            patch("ltvm_pkg.host_setup.is_macos", return_value=False),
        ):
            reqs = {r.name: r for r in sr.host_checks()}
        assert not reqs["QEMU vhost-user-fs-pci"].ok
        assert not reqs["QEMU vhost-user netdev"].ok
        assert reqs["NUMA memory binding"].ok

    def test_qemu_probed_once_per_binary(self, tmp_path: Path) -> None:
        from ltvm_pkg import shmrdma_req as sr

        qemu = tmp_path / "qemu"
        qemu.write_text("v1")
        helps = {"device": '"vhost-user-fs-pci"', "object": ""}
        with (
            patch(
                "ltvm_pkg.shmrdma_req._qemu_help",
                side_effect=lambda what, q=None: helps[what],
            ) as qh,
            patch(
                "ltvm_pkg.shmrdma_req._qemu_has_vhost_user_netdev",
                return_value=True,
            ),
            patch("ltvm_pkg.qemu_run.qemu_binds_numa", return_value=False),
        ):
            first = sr._qemu_caps(str(qemu))
            assert first["vhost-user-fs-pci"]
            assert qh.call_count == 2
            assert sr._qemu_caps(str(qemu)) == first
            assert qh.call_count == 2
            # a reinstalled QEMU is probed again
            qemu.write_text("v2, longer")
            sr._qemu_caps(str(qemu))
            assert qh.call_count == 4

    def test_failed_probe_not_cached(self, tmp_path: Path) -> None:
        from ltvm_pkg import shmrdma_req as sr

        qemu = tmp_path / "qemu"
        qemu.write_text("v1")
        with (
            patch("ltvm_pkg.shmrdma_req._qemu_help", return_value="") as qh,
            patch(
                "ltvm_pkg.shmrdma_req._qemu_has_vhost_user_netdev",
                return_value=False,
            ),
            patch("ltvm_pkg.qemu_run.qemu_binds_numa", return_value=False),
        ):
            sr._qemu_caps(str(qemu))
            sr._qemu_caps(str(qemu))
        assert qh.call_count == 4
        assert not sr._caps_cache().exists()

    def test_timed_out_probe_not_cached(self, tmp_path: Path) -> None:
        """A probe that timed out is not taken for a QEMU without it."""
        from ltvm_pkg import shmrdma_req as sr

        qemu = tmp_path / "qemu"
        qemu.write_text("v1")
        helps = {"device": '"vhost-user-fs-pci"', "object": None}
        with (
            patch(
                "ltvm_pkg.shmrdma_req._qemu_help",
                side_effect=lambda what, q=None: helps[what],
            ),
            patch(
                "ltvm_pkg.shmrdma_req._qemu_has_vhost_user_netdev",
                return_value=None,
            ),
            patch("ltvm_pkg.qemu_run.qemu_binds_numa", return_value=False),
        ):
            caps = sr._qemu_caps(str(qemu))
            assert not caps["shared-ram"] and not caps["vhost-user-netdev"]
            assert not sr._caps_cache().exists()
            helps["object"] = shmrdma.SHARED_RAM_BACKEND
            assert sr._qemu_caps(str(qemu))["shared-ram"]
        assert not sr._caps_cache().exists()
        with patch(
            "ltvm_pkg.shmrdma_req.subprocess.run",
            side_effect=subprocess.TimeoutExpired("qemu", 30),
        ):
            assert sr._qemu_help("object", str(qemu)) is None
            assert sr._qemu_has_vhost_user_netdev(str(qemu)) is None

    def test_compiler_not_needed_once_built(self, tmp_path: Path) -> None:
        from ltvm_pkg import shmrdma_req as sr

        def cc_check() -> sr.Req:
            with (
                patch("ltvm_pkg.shmrdma_req.shutil.which", return_value=None),
                patch.dict(os.environ, {"CC": ""}),
                patch("ltvm_pkg.shmrdma_req._qemu_caps") as caps,
            ):
                caps.return_value.__getitem__.return_value = True
                return {r.name: r for r in sr.host_checks()}[
                    "C compiler for shmrdmad"
                ]

        d = shmrdma.Daemon("tester", os.geteuid(), tmp_path / "d")
        with patch("ltvm_pkg.shmrdma.daemon", return_value=d):
            assert not cc_check().ok
            binary = shmrdma._cache_dir() / shmrdma.daemon_digest() / "shmrdmad"
            binary.parent.mkdir(parents=True)
            binary.write_text("")
            r = cc_check()
        assert r.ok and "none needed" in r.detail

    def test_compiler_not_needed_while_running(self, tmp_path: Path) -> None:
        from ltvm_pkg import shmrdma_req as sr

        d = MagicMock()
        d.pid.return_value = 4321
        with (
            patch("ltvm_pkg.shmrdma.daemon", return_value=d),
            patch("ltvm_pkg.shmrdma_req.shutil.which", return_value=None),
            patch.dict(os.environ, {"CC": ""}),
            patch("ltvm_pkg.shmrdma_req._qemu_caps") as caps,
        ):
            caps.return_value.__getitem__.return_value = True
            reqs = {r.name: r for r in sr.host_checks()}
        assert reqs["C compiler for shmrdmad"].ok

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

    def test_launch_refuses_before_taps(self, tmp_vmdir: Path) -> None:  # noqa: F811
        """On a classic host a refusal leaves no TAP behind."""
        vm = _make_vm(tmp_vmdir, arch="aarch64")
        vm.shmrdma = "auto"
        vm.save()
        h = _LaunchHarness()
        with (
            patch(
                "ltvm_pkg.qemu_run.rootless.readiness",
                return_value=MagicMock(ok=False),
            ),
            patch(
                "ltvm_pkg.shmrdma_req.refusal", return_value="needs gcc"
            ) as refusal,
            pytest.raises(SystemExit),
        ):
            _run_launch(vm, h)
        assert not any("tuntap" in c for c in h.run_calls)
        # the QEMU checked is the one this VM's arch runs on
        assert refusal.call_args.kwargs["arch"] == "aarch64"

    def test_checks_probe_the_guest_arch_qemu(self) -> None:
        from ltvm_pkg import shmrdma_req as sr

        probed = []

        def qemu_help(what: str, qemu: str | None = None) -> str:
            probed.append(qemu)
            return ""

        with (
            patch("ltvm_pkg.shmrdma_req._qemu_help", side_effect=qemu_help),
            patch(
                "ltvm_pkg.shmrdma_req._qemu_has_vhost_user_netdev",
                side_effect=lambda qemu=None: probed.append(qemu) or False,
            ),
            patch("ltvm_pkg.qemu_run.qemu_binds_numa", return_value=False),
            patch("ltvm_pkg.host_setup.is_macos", return_value=False),
            patch(
                "ltvm_pkg.vm_state.qemu_binary_for_arch",
                side_effect=lambda a: f"/q/qemu-system-{a}",
            ),
        ):
            reqs = {r.name: r for r in sr.host_checks("riscv64")}
        assert set(probed) == {"/q/qemu-system-riscv64"}
        assert reqs["QEMU vhost-user-fs-pci"].detail == "/q/qemu-system-riscv64"

    def test_cross_arch_on_macos_has_no_fix(self) -> None:
        from ltvm_pkg import shmrdma_req as sr

        other = "x86_64" if sr._native_arch() != "x86_64" else "aarch64"
        with (
            patch("ltvm_pkg.host_setup.is_macos", return_value=True),
            patch("ltvm_pkg.shmrdma_req._qemu_help", return_value=""),
            patch(
                "ltvm_pkg.shmrdma_req._qemu_has_vhost_user_netdev",
                return_value=False,
            ),
        ):
            reqs = {r.name: r for r in sr.host_checks(other)}
        fs = reqs["QEMU vhost-user-fs-pci"]
        assert fs.fix is None and "native guests only" in fs.how

    def test_hand_given_sockets_need_no_build(self) -> None:
        """A daemon run by hand needs no compiler or run dir here."""
        from ltvm_pkg import shmrdma_req as sr

        own = NicSpec.parse("shmnet")
        hand = NicSpec.parse("shmnet:socket=/run/x.sock")
        assert sr.OWN_DAEMON in sr.features_of("auto", [], -1)
        assert sr.OWN_DAEMON in sr.features_of("", [own], -1)
        assert sr.OWN_DAEMON not in sr.features_of("/run/r.sock", [hand], -1)
        reqs = [
            self._req("C compiler for shmrdmad", {sr.OWN_DAEMON}, False),
            self._req("QEMU vhost-user netdev", {sr.SHMNET}, True),
        ]
        with patch("ltvm_pkg.shmrdma_req.host_checks", return_value=reqs):
            assert sr.refusal(sr.features_of("", [hand], -1)) is None
            assert sr.refusal(sr.features_of("", [own], -1)) is not None


def test_install_wants_numactl() -> None:
    import inspect

    from ltvm_pkg import host_setup

    assert '"numactl": "numactl"' in inspect.getsource(
        host_setup.check_prerequisites
    )


def test_cluster_create_refuses_a_swallowed_spec(capsys) -> None:
    """--shmrdma takes an optional value, so a node spec after it would
    become its socket; cluster create refuses before making any node."""
    from tests.test_cluster_cli import _ns

    ns = _ns("create", "co2", "--shmrdma", "mgs+mds+oss:co2-srv:3", "client:c1")
    assert ns.shmrdma == "mgs+mds+oss:co2-srv:3"
    with patch("ltvm_pkg.vm_cluster.cmd_cluster_create") as create:
        rc = ns.func(ns)
    assert rc != 0
    create.assert_not_called()
    out = capsys.readouterr()
    assert "put --shmrdma after" in out.out + out.err


@pytest.mark.parametrize(
    "flags, refused",
    [
        (["--shmrdma"], "refusal"),
        (["--host-node", "0"], "host_node_problem"),
        (["--shmrdma", "--numa-nodes", "16"], "shmrdmad maps at most"),
    ],
)
def test_cluster_create_refuses_before_any_node(
    tmp_path: Path, capsys, flags: list[str], refused: str
) -> None:
    """What every node would refuse alike is refused once, up front."""
    from ltvm_pkg import vm_cluster
    from tests.test_cluster_cli import _ns

    ns = _ns("create", "co2", "mgs+mds+oss:co2-srv:3", *flags)
    with (
        patch("ltvm_pkg.rootless.ready", return_value=True),
        patch.object(vm_cluster, "SOCKETS", tmp_path),
        patch(
            "ltvm_pkg.vm_cluster.host_node_problem",
            side_effect=lambda n: "host_node_problem" if n >= 0 else None,
        ),
        patch(
            "ltvm_pkg.shmrdma_req.refusal",
            side_effect=lambda f, **k: (
                "refusal"
                if "--shmrdma" in flags and "--numa-nodes" not in flags
                else None
            ),
        ),
        patch.object(vm_cluster, "_create_one_node") as create,
        patch.object(vm_cluster, "_require_cluster_memory") as memory,
    ):
        rc = ns.func(ns)
    assert rc != 0
    create.assert_not_called()
    memory.assert_not_called()
    out = capsys.readouterr()
    assert refused in out.out + out.err


class TestImageLines:
    def _lines(self, tmp_path: Path, **kw) -> tuple[list[str], MagicMock]:
        from ltvm_pkg.image_build import _shmrdma_lines

        ko = tmp_path / "built.ko"
        ko.write_bytes(b"ko")
        inject = tmp_path / "inject"
        inject.mkdir()
        with patch("ltvm_pkg.shmrdma.build_kmod", return_value=ko) as build:
            args = {"cross": False, "mofed": False, **kw}
            return (
                _shmrdma_lines(MagicMock(), "k", "5.14.0", inject, **args),
                build,
            )

    def test_baked_in(self, tmp_path: Path) -> None:
        lines, _ = self._lines(tmp_path)
        assert "COPY shmrdma/shmrdma.ko /lib/modules/5.14.0/extra/" in lines
        assert (tmp_path / "inject" / "shmrdma" / "shmrdma.ko").is_file()

    def test_left_out_over_mofed_kmods(self, tmp_path: Path) -> None:
        lines, build = self._lines(tmp_path, mofed=True)
        assert lines == []
        build.assert_not_called()

    def test_left_out_of_a_cross_build(self, tmp_path: Path) -> None:
        lines, build = self._lines(tmp_path, cross=True)
        assert lines == []
        build.assert_not_called()


class TestVMKmodRecord:
    def test_round_trip(self, tmp_vmdir: Path) -> None:  # noqa: F811
        vm = _make_vm(tmp_vmdir)
        vm.save()
        assert VMInfo.load(vm.name).shmrdma_kmod is None
        for has in (True, False):
            vm.shmrdma_kmod = has
            vm.save()
            assert VMInfo.load(vm.name).shmrdma_kmod is has

    def test_record_wins_over_the_image_meta(
        self,
        tmp_vmdir: Path,  # noqa: F811
        tmp_path: Path,
    ) -> None:
        d = _image_dir(tmp_path / "img", {"shmrdma_kmod": True})
        vm = _make_vm(tmp_vmdir)
        vm.image = str(d / OLD_IMG)
        vm.shmrdma_kmod = True
        vm.save()
        # a fetch replaced meta.json: it no longer names OLD_IMG
        assert not shmrdma.image_has_kmod(vm.image)
        assert shmrdma.vm_has_kmod(vm)

    def test_older_vm_adopts_a_yes(
        self,
        tmp_vmdir: Path,  # noqa: F811
        tmp_path: Path,
    ) -> None:
        d = _image_dir(tmp_path / "img", {"shmrdma_kmod": True})
        vm = _make_vm(tmp_vmdir)
        vm.image = str(d / NEW_IMG)
        vm.save()
        assert shmrdma.vm_has_kmod(vm)
        assert VMInfo.load(vm.name).shmrdma_kmod is True

    def test_older_vm_never_records_a_no(
        self,
        tmp_vmdir: Path,  # noqa: F811
        tmp_path: Path,
    ) -> None:
        d = _image_dir(tmp_path / "img", {"shmrdma_kmod": True})
        vm = _make_vm(tmp_vmdir)
        vm.image = str(d / OLD_IMG)
        vm.save()
        assert not shmrdma.vm_has_kmod(vm)
        assert VMInfo.load(vm.name).shmrdma_kmod is None

    def test_target_takes_the_record(self, tmp_path: Path) -> None:
        from ltvm_pkg import shmrdma_req as sr

        _image_dir(tmp_path, {"shmrdma_kmod": True})
        t = sr.Target(
            "rocky10",
            image=tmp_path / OLD_IMG,
            kernel_dir=tmp_path,
            has_kmod=True,
        )
        kmod = [r for r in sr.target_checks(t) if "module" in r.name][0]
        assert kmod.ok
        t.has_kmod = None
        kmod = [r for r in sr.target_checks(t) if "module" in r.name][0]
        assert not kmod.ok


class TestFetchKeepsKmodImages:
    def _fetch(self, out: Path, fetched_meta: dict) -> Path:
        from ltvm_pkg.image_store import LEGACY
        from ltvm_pkg.release_package import _install_fetched_images

        staging = out / ".ltvm-fetch-image-x"
        sd = staging / "rocky9" / "x86_64" / "images" / "5.14"
        sd.mkdir(parents=True)
        (sd / LEGACY).write_bytes(b"fetched")
        (sd / "meta.json").write_text(json.dumps(fetched_meta))
        _install_fetched_images(staging, out)
        return out / "rocky9" / "x86_64" / "images" / "5.14"

    def test_older_files_carried(self, tmp_path: Path) -> None:
        from ltvm_pkg.image_store import current_image

        out = tmp_path / "artifacts"
        d = _image_dir(
            out / "rocky9" / "x86_64" / "images" / "5.14",
            {
                "shmrdma_kmod": False,
                "shmrdma_kmod_images": {OLD_IMG: True, NEW_IMG: False},
            },
        )
        self._fetch(out, {"shmrdma_kmod": True, "new": 1})
        cur = current_image(d)
        assert cur is not None and cur.name not in (OLD_IMG, NEW_IMG)
        meta = json.loads((d / "meta.json").read_text())
        assert meta["new"] == 1
        assert meta["shmrdma_kmod_images"] == {
            OLD_IMG: True,
            NEW_IMG: False,
            cur.name: True,
        }
        assert shmrdma.image_has_kmod(d / OLD_IMG)
        assert shmrdma.image_has_kmod(cur)

    def test_older_flag_names_the_old_current(self, tmp_path: Path) -> None:
        out = tmp_path / "artifacts"
        d = _image_dir(
            out / "rocky9" / "x86_64" / "images" / "5.14",
            {"shmrdma_kmod": True},
        )
        self._fetch(out, {"shmrdma_kmod": False})
        assert shmrdma.image_has_kmod(d / NEW_IMG)
        assert not shmrdma.image_has_kmod(d / "current.ext4")

    def test_publishers_map_replaced(self, tmp_path: Path) -> None:
        from ltvm_pkg.image_store import current_image

        out = tmp_path / "artifacts"
        d = self._fetch(
            out,
            {"shmrdma_kmod": True, "shmrdma_kmod_images": {"theirs": True}},
        )
        cur = current_image(d)
        assert cur is not None
        meta = json.loads((d / "meta.json").read_text())
        assert meta["shmrdma_kmod_images"] == {cur.name: True}


class TestKmodCache:
    @pytest.fixture
    def tc(self, tmp_path: Path) -> MagicMock:
        kdir = tmp_path / "kernels" / "6.12"
        (kdir / "build-tree").mkdir(parents=True)
        (kdir / "meta.json").write_text('{"input_hash": "k"}')
        tc = MagicMock()
        tc.name, tc.arch, tc.container_tag = "rocky10", "x86_64", "tag"
        tc.kernel_output_dir.return_value = kdir
        tc.meta_path.return_value = kdir / "meta.json"
        return tc

    @pytest.fixture
    def podman(self) -> Iterator[MagicMock]:
        def run(cmd, **kw):
            src = Path(cmd[cmd.index("-v", 6) + 1].split(":")[0])
            (src / "kmod" / "shmrdma.ko").write_bytes(b"ko")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("ltvm_pkg.shmrdma.subprocess.run", side_effect=run) as r:
            yield r

    def test_owner_builds_into_the_kernel_dir(self, tc, podman) -> None:
        ko = shmrdma.build_kmod(tc, None, "6.12.0")
        assert ko == shmrdma.kmod_dir(tc) / "shmrdma.ko"
        assert shmrdma.build_kmod(tc, None, "6.12.0") == ko
        assert podman.call_count == 1

    def test_others_build_their_own(self, tc, podman) -> None:
        with patch("ltvm_pkg.zfs_build._shared_is_ours", return_value=False):
            ko = shmrdma.build_kmod(tc, None, "6.12.0")
        assert ko == shmrdma.user_kmod_dir(tc) / "shmrdma.ko"
        assert not shmrdma.kmod_dir(tc).exists()

    def test_untrusted_shared_module_ignored(self, tc, podman) -> None:
        shmrdma.build_kmod(tc, None, "6.12.0")
        with (
            patch("ltvm_pkg.zfs_build._untrusted", return_value="planted"),
            patch("ltvm_pkg.zfs_build._shared_is_ours", return_value=False),
        ):
            ko = shmrdma.build_kmod(tc, None, "6.12.0")
        assert ko == shmrdma.user_kmod_dir(tc) / "shmrdma.ko"
        assert podman.call_count == 2

    def test_concurrent_builds_build_once(self, tc) -> None:
        import threading

        started = threading.Event()
        release = threading.Event()

        def run(cmd, **kw):
            started.set()
            release.wait(10)
            src = Path(cmd[cmd.index("-v", 6) + 1].split(":")[0])
            (src / "kmod" / "shmrdma.ko").write_bytes(b"ko" * 1000)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        got: list[Path | None] = []
        with patch("ltvm_pkg.shmrdma.subprocess.run", side_effect=run) as r:
            threads = [
                threading.Thread(
                    target=lambda: got.append(
                        shmrdma.build_kmod(tc, None, "6.12.0")
                    )
                )
                for _ in range(2)
            ]
            for t in threads:
                t.start()
            assert started.wait(10)
            time.sleep(0.2)
            release.set()
            for t in threads:
                t.join(10)
        assert r.call_count == 1
        ko = shmrdma.kmod_dir(tc) / "shmrdma.ko"
        assert got == [ko, ko]
        assert ko.read_bytes() == b"ko" * 1000

    def test_rebuild_keeps_the_old_module_until_replaced(self, tc) -> None:
        out = shmrdma.kmod_dir(tc)
        out.mkdir()
        (out / "shmrdma.ko").write_bytes(b"old")
        (out / "meta.json").write_text('{"input_hash": "stale"}')
        seen = []

        def run(cmd, **kw):
            seen.append((out / "shmrdma.ko").read_bytes())
            src = Path(cmd[cmd.index("-v", 6) + 1].split(":")[0])
            (src / "kmod" / "shmrdma.ko").write_bytes(b"new")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with open(out / "shmrdma.ko", "rb") as reader:
            with patch("ltvm_pkg.shmrdma.subprocess.run", side_effect=run):
                ko = shmrdma.build_kmod(tc, None, "6.12.0")
            assert reader.read() == b"old"
        assert seen == [b"old"]
        assert ko is not None and ko.read_bytes() == b"new"
        assert not [p for p in out.iterdir() if p.name.startswith(".")]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
    def test_unwritable_kernel_dir_warns(self, tc, podman) -> None:
        kdir = tc.kernel_output_dir.return_value
        kdir.chmod(0o555)
        try:
            with patch("ltvm_pkg.zfs_build._shared_is_ours", return_value=True):
                assert shmrdma.build_kmod(tc, None, "6.12.0") is None
        finally:
            kdir.chmod(0o755)
        podman.assert_not_called()


def test_vm_set_checks_the_vms_kernel_dir(tmp_path: Path) -> None:
    from ltvm_pkg import vm_commands

    vm = VMInfo(
        name="co1-a",
        ip="192.168.100.50",
        image=str(tmp_path / "images" / "base.ext4"),
        kernel=str(tmp_path / "kernels" / "k1" / "vmlinuz"),
    )
    with (
        patch("ltvm_pkg.shmrdma.vm_has_kmod", return_value=True),
        patch("ltvm_pkg.shmrdma_req.refusal", return_value=None) as refusal,
    ):
        vm_commands._check_shmrdma_image(vm, "auto")
    t = refusal.call_args.args[1]
    assert t.kernel_dir == tmp_path / "kernels" / "k1"
