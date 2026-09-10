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


# --------------------------------------------------------------------------
# Unprivileged commands must never block on a sudo password prompt.
# --------------------------------------------------------------------------


class _Ok:
    returncode = 0


def _perform(cmd: list[str]) -> None:
    """Carry out what an atomic_write sudo fallback asked for, without
    privilege: the test dir is really writable, only os.access lies."""
    import shutil

    op = [str(c) for c in cmd]
    if op[0] == "install":
        shutil.copy(op[-2], op[-1])
    elif op[0] == "mv":
        os.replace(op[-2], op[-1])
    elif op[0] == "rm":
        for p in op[2:]:
            Path(p).unlink(missing_ok=True)
    elif op[0] == "mkdir":
        Path(op[-1]).mkdir(parents=True, exist_ok=True)


def _recording_sudo(calls: list[tuple[list[str], dict[str, Any]]]):
    def fake_sudo(cmd, **kw):
        calls.append(([str(c) for c in cmd], kw))
        _perform(cmd)
        return _Ok()

    return fake_sudo


class TestAtomicWriteNoninteractive:
    def test_raises_permissionerror_when_sudo_needs_password(
        self, tmp_path: Path
    ) -> None:
        """No credential cached: fail before touching sudo at all, so
        the caller can warn -- never a prompt."""
        dest = tmp_path / "root-owned" / "co1.info"
        dest.parent.mkdir()
        with (
            patch.object(os, "access", return_value=False),
            patch.object(priv, "sudo_ready", return_value=False),
            patch.object(priv, "sudo_run") as sr,
        ):
            with pytest.raises(PermissionError):
                priv.atomic_write(dest, "K=v\n", noninteractive=True)
        sr.assert_not_called()
        assert not dest.exists()

    def test_uses_sudo_n_when_credentials_are_cached(
        self, tmp_path: Path
    ) -> None:
        dest = tmp_path / "root-owned" / "co1.info"
        dest.parent.mkdir()
        calls: list[tuple[list[str], dict[str, Any]]] = []
        with (
            patch.object(os, "access", return_value=False),
            patch.object(priv, "sudo_ready", return_value=True),
            patch.object(priv, "sudo_run", side_effect=_recording_sudo(calls)),
        ):
            priv.atomic_write(dest, "K=v\n", noninteractive=True)
        assert dest.read_text() == "K=v\n"
        assert calls
        assert all(kw.get("noninteractive") for _, kw in calls)

    def test_default_stays_interactive(self, tmp_path: Path) -> None:
        """create/start/stop prime sudo and may prompt; the default
        fallback is unchanged and never consults sudo_ready."""
        dest = tmp_path / "root-owned" / "co1.info"
        dest.parent.mkdir()
        calls: list[tuple[list[str], dict[str, Any]]] = []
        with (
            patch.object(os, "access", return_value=False),
            patch.object(priv, "sudo_ready") as ready,
            patch.object(priv, "sudo_run", side_effect=_recording_sudo(calls)),
        ):
            priv.atomic_write(dest, "K=v\n")
        ready.assert_not_called()
        assert calls
        assert not any(kw.get("noninteractive") for _, kw in calls)

    def test_sudo_run_noninteractive_adds_dash_n(self) -> None:
        with (
            patch.object(os, "geteuid", return_value=1000),
            patch.object(priv, "_run") as run,
        ):
            priv.sudo_run(["true"], noninteractive=True)
        run.assert_called_once_with(
            ["sudo", "-n", "true"], check=True, quiet=False
        )


class TestDeployNeverPrompts:
    """`ltvm deploy-lustre` and `cluster deploy` run unprivileged.  Their
    only write into the root-owned sockets dir is the LAST_DEPLOY /
    BUILD_PATH / KVER bookkeeping, and that must never turn into a
    password prompt: an agent driving deploy+test unattended hangs on
    it.  3c476c7 routed the lock file through atomic_write's sudo
    fallback and did exactly that."""

    def test_update_deploy_raises_instead_of_prompting(
        self, sockets: Path
    ) -> None:
        vm = VMInfo(name="co1-dep", ip="10.0.0.9", os_id="rocky9")
        vm.save()
        with (
            patch.object(os, "access", return_value=False),
            patch.object(priv, "sudo_ready", return_value=False),
            patch.object(priv, "sudo_run") as sr,
        ):
            with pytest.raises(PermissionError):
                vm.update_deploy(1, "/src", "5.14")
        sr.assert_not_called()
        assert VMInfo.load("co1-dep").last_deploy == 0

    def test_update_deploy_records_through_sudo_n(self, sockets: Path) -> None:
        """A cached timestamp or NOPASSWD rule is used silently."""
        vm = VMInfo(name="co1-dep", ip="10.0.0.9", os_id="rocky9")
        vm.save()
        calls: list[tuple[list[str], dict[str, Any]]] = []
        with (
            patch.object(os, "access", return_value=False),
            patch.object(priv, "sudo_ready", return_value=True),
            patch.object(priv, "sudo_run", side_effect=_recording_sudo(calls)),
        ):
            vm.update_deploy(7, "/src", "5.14-x")
        loaded = VMInfo.load("co1-dep")
        assert (loaded.last_deploy, loaded.build_path, loaded.kver) == (
            7,
            "/src",
            "5.14-x",
        )
        assert calls
        assert all(kw.get("noninteractive") for _, kw in calls)

    def test_update_deploy_needs_no_sudo_in_writable_dir(
        self, sockets: Path
    ) -> None:
        vm = VMInfo(name="co1-dep", ip="10.0.0.9", os_id="rocky9")
        vm.save()
        with (
            patch.object(priv, "sudo_ready") as ready,
            patch.object(priv, "sudo_run") as sr,
        ):
            vm.update_deploy(3, "/src", "5.14")
        ready.assert_not_called()
        sr.assert_not_called()
        assert VMInfo.load("co1-dep").last_deploy == 3

    def test_root_owned_lock_from_old_sudo_create(self, sockets: Path) -> None:
        """The scenario 3c476c7 fixed: a 0644 root-owned lock left by
        `sudo ltvm cluster create` in a root-owned sockets dir.  That
        used to traceback on open(lock, "w").  Now the lock is taken
        read-only and the .info write is what decides: PermissionError
        for the caller to warn about when sudo would need a password,
        or recorded through `sudo -n` when a credential is cached."""
        vm = VMInfo(name="co1-old", ip="10.0.0.9", os_id="rocky9")
        vm.save()
        lock = sockets / ".co1-old.info.lock"
        lock.write_text("")
        lock.chmod(0o444)
        real_open = open

        def no_write_open(path, mode="r", *a, **kw):
            if str(path) == str(lock) and ("a" in mode or "w" in mode):
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, mode, *a, **kw)

        with (
            patch("builtins.open", side_effect=no_write_open),
            patch.object(os, "access", return_value=False),
            patch.object(priv, "sudo_ready", return_value=False),
            patch.object(priv, "sudo_run") as sr,
        ):
            with pytest.raises(PermissionError) as ei:
                vm.update_deploy(1, "/src", "5.14")
        sr.assert_not_called()
        # It got past the lock; the .info write is what refused.
        assert str(ei.value.filename).endswith("co1-old.info")

        calls: list[tuple[list[str], dict[str, Any]]] = []
        with (
            patch("builtins.open", side_effect=no_write_open),
            patch.object(os, "access", return_value=False),
            patch.object(priv, "sudo_ready", return_value=True),
            patch.object(priv, "sudo_run", side_effect=_recording_sudo(calls)),
        ):
            vm.update_deploy(9, "/src", "5.14")
        assert VMInfo.load("co1-old").last_deploy == 9
        assert calls
        assert all(kw.get("noninteractive") for _, kw in calls)

    def test_update_pid_keeps_interactive_fallback(self, sockets: Path) -> None:
        """start/stop prime sudo up front; their writes may still go
        through a plain `sudo`."""
        vm = VMInfo(name="co1-pid", ip="10.0.0.9", os_id="rocky9")
        vm.save()
        calls: list[tuple[list[str], dict[str, Any]]] = []
        with (
            patch.object(os, "access", return_value=False),
            patch.object(priv, "sudo_ready") as ready,
            patch.object(priv, "sudo_run", side_effect=_recording_sudo(calls)),
        ):
            vm.update_pid(5)
        ready.assert_not_called()
        assert calls
        assert not any(kw.get("noninteractive") for _, kw in calls)
        assert VMInfo.load("co1-pid").pid == 5


class TestClusterDeployNeverPrompts:
    def test_permissionerror_on_bookkeeping_is_a_warning(
        self, sockets: Path, tmp_path: Path, capsys: Any
    ) -> None:
        """The deploy itself succeeded on every node; an unrecordable
        LAST_DEPLOY must not abort the command (or prompt)."""
        import argparse

        from ltvm_pkg import vm_cluster
        from ltvm_pkg.vm_state import ClusterInfo

        for n in ("co3-mds", "co3-oss1"):
            VMInfo(name=n, ip="10.0.0.5", os_id="rocky9", arch="x86_64").save()
        cluster = ClusterInfo(
            name="co3",
            nodes=[
                {"name": "co3-mds", "roles": ["mgs", "mds"]},
                {"name": "co3-oss1", "roles": ["oss"]},
            ],
        )
        args = argparse.Namespace(
            name="co3", lustre_source=str(tmp_path), mount=False
        )

        class _TC:
            os_family = "rhel"

        with (
            patch.object(ClusterInfo, "load", return_value=cluster),
            patch.object(vm_cluster, "_validate_lustre_source"),
            patch("ltvm_pkg.target_config.TargetConfig", return_value=_TC()),
            patch.object(vm_cluster.subprocess, "run", return_value=_Ok()),
            patch.object(
                vm_cluster,
                "_deploy_one_node",
                side_effect=lambda name, *a, **k: (name, 0, ""),
            ),
            patch.object(vm_cluster, "generate_local_sh", return_value=""),
            patch.object(
                vm_cluster,
                "_write_cluster_local_sh",
                side_effect=lambda name, *a, **k: (name, 0, ""),
            ),
            patch.object(
                VMInfo, "update_deploy", side_effect=PermissionError("nope")
            ),
        ):
            vm_cluster.cmd_cluster_deploy(args)

        err = capsys.readouterr().err
        assert "not recorded" in err
        assert "co3-mds" in err and "co3-oss1" in err
