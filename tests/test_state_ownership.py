"""State-file ownership and locking across privileged/unprivileged runs.

`sudo ltvm cluster create` writes into /opt/qemu-vms/sockets, which is
root-owned and not user-writable.  Everything it leaves behind must
still be usable by the next unprivileged `ltvm cluster deploy`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ltvm_pkg import priv
from ltvm_pkg.vm_state import VMInfo


@pytest.fixture()
def sockets(tmp_path: Path):
    d = tmp_path / "sockets"
    d.mkdir()
    with patch("ltvm_pkg.vm_state.SOCKETS", d):
        yield d


class TestInfoLock:
    def test_lock_created_when_absent(self, sockets: Path) -> None:
        vm = VMInfo(name="co1-lock", ip="10.0.0.9", os_id="rocky9")
        vm.save()
        vm.update_pid(4242)
        lock = sockets / ".co1-lock.info.lock"
        assert lock.exists()
        assert VMInfo.load("co1-lock").pid == 4242

    def test_lock_is_group_and_world_writable(self, sockets: Path) -> None:
        """Both a root ltvm and an unprivileged one must be able to
        open it; 0644 is what made cluster deploy fail."""
        vm = VMInfo(name="co1-lock", ip="10.0.0.9", os_id="rocky9")
        vm.save()
        vm.update_pid(1)
        mode = (sockets / ".co1-lock.info.lock").stat().st_mode & 0o666
        assert mode == 0o666

    def test_read_only_lock_still_locks(self, sockets: Path) -> None:
        """A lock left behind root-owned by an earlier `sudo` run can
        only be opened read-only.  flock() needs a descriptor, not
        write access, so that must be enough -- previously this raised
        PermissionError and aborted `ltvm cluster deploy`."""
        vm = VMInfo(name="co1-lock", ip="10.0.0.9", os_id="rocky9")
        vm.save()
        lock = sockets / ".co1-lock.info.lock"
        lock.write_text("")
        lock.chmod(0o444)

        real_open = open

        def no_write_open(path, mode="r", *a, **kw):
            if str(path) == str(lock) and ("a" in mode or "w" in mode):
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, mode, *a, **kw)

        with patch("builtins.open", side_effect=no_write_open):
            vm.update_pid(777)

        assert VMInfo.load("co1-lock").pid == 777


class TestInvokingUser:
    def test_none_when_plain_root(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(os, "geteuid", return_value=0),
        ):
            assert priv.invoking_user() is None

    def test_prefers_sudo_user(self) -> None:
        with (
            patch.dict(os.environ, {"SUDO_USER": "root"}, clear=True),
            patch.object(os, "geteuid", return_value=0),
        ):
            assert priv.invoking_user() is None

    def test_reports_current_user_unprivileged(self) -> None:
        import pwd

        me = pwd.getpwuid(os.getuid()).pw_name
        if me == "root":
            pytest.skip("test suite running as root")
        with patch.dict(os.environ, {}, clear=True):
            owner = priv.invoking_user()
        assert owner is not None and owner[0] == me

    def test_chown_noop_when_not_root(self, tmp_path: Path) -> None:
        f = tmp_path / "x"
        f.write_text("")
        before = f.stat()
        priv.chown_to_invoking_user(f)
        assert f.stat().st_uid == before.st_uid


class TestAtomicWriteOwnership:
    def test_sudo_fallback_installs_as_invoking_user(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """When the destination dir isn't user-writable, the install
        must name an owner -- otherwise every .info/.cluster file ends
        up root-owned and the next unprivileged ltvm can't rewrite it."""
        vm_dir = tmp_path / "qemu-vms"
        dest = vm_dir / "sockets" / "co1.info"
        dest.parent.mkdir(parents=True)
        monkeypatch.setenv("LTVM_VM_DIR", str(vm_dir))
        calls: list[list[str]] = []

        def fake_sudo(cmd, **kw):
            calls.append([str(c) for c in cmd])

            class R:
                returncode = 0

            return R()

        with (
            patch.object(priv, "sudo_run", side_effect=fake_sudo),
            patch.object(os, "access", return_value=False),
            patch.object(priv, "invoking_user", return_value=("paf", "paf")),
        ):
            priv.atomic_write(dest, "K=v\n")

        install = next(c for c in calls if c[0] == "install")
        assert "-o" in install and install[install.index("-o") + 1] == "paf"
        assert "-g" in install and install[install.index("-g") + 1] == "paf"

    def test_sudo_fallback_omits_owner_for_real_root(
        self, tmp_path: Path
    ) -> None:
        dest = tmp_path / "root-owned" / "co1.info"
        dest.parent.mkdir()
        calls: list[list[str]] = []

        def fake_sudo(cmd, **kw):
            calls.append([str(c) for c in cmd])

            class R:
                returncode = 0

            return R()

        with (
            patch.object(priv, "sudo_run", side_effect=fake_sudo),
            patch.object(os, "access", return_value=False),
            patch.object(priv, "invoking_user", return_value=None),
        ):
            priv.atomic_write(dest, "K=v\n")

        install = next(c for c in calls if c[0] == "install")
        assert "-o" not in install
