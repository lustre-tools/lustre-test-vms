"""Two deploys of one Lustre tree must not race on its staging dir.

Seen with two `ltvm cluster deploy --build <tree>` started together: the
second one's build cleared and reinstalled the staging dir while the
first was still tarring it into its nodes ("tar: ./lib: file changed as
we read it", "./etc: File removed before we read it").
"""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import lustre_build as lb
from ltvm_pkg import vm_cluster
from ltvm_pkg.vm_cluster import ClusterInfo
from tests.test_deploy import _mark_staging_fresh, _stub_tc
from tests.test_staging_sources import (
    _TESTS,
    _edit_keeping_old_mtime,
    _tree_and_staging,
)

_KERNEL = "5.14-rhel9.7"


def _lock_state(staging: Path) -> str:
    """How another process would find the staging lock right now."""
    path = lb.staging_lock_path(staging)
    if not path.exists():
        return "free"
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return "exclusive"
            return "shared"
        return "free"
    finally:
        os.close(fd)


def _hold(staging: Path, exclusive: bool, entered, release) -> None:
    with lb.staging_lock(staging, exclusive=exclusive):
        entered.set()
        release.wait(10)


class TestStagingLock:
    def test_lock_file_sits_beside_the_staging_dir(
        self, tmp_path: Path
    ) -> None:
        staging = tmp_path / ".ltvm-staging" / "rocky9" / "x86_64" / _KERNEL
        with lb.staging_lock(staging, exclusive=False):
            pass
        lock = lb.staging_lock_path(staging)
        assert lock.parent == staging.parent
        assert lock.is_file()
        assert not staging.exists()

    def test_shared_holders_coexist(self, tmp_path: Path, capsys) -> None:
        staging = tmp_path / "staging"
        with lb.staging_lock(staging, exclusive=False):
            with lb.staging_lock(staging, exclusive=False):
                assert _lock_state(staging) == "shared"
        assert _lock_state(staging) == "free"
        assert "waiting" not in capsys.readouterr().err

    @pytest.mark.parametrize("first_exclusive", [False, True])
    def test_exclusive_excludes_the_other_kind(
        self, tmp_path: Path, capsys, first_exclusive: bool
    ) -> None:
        staging = tmp_path / "staging"
        held, release = threading.Event(), threading.Event()
        holder = threading.Thread(
            target=_hold, args=(staging, first_exclusive, held, release)
        )
        holder.start()
        assert held.wait(10)

        entered, done = threading.Event(), threading.Event()
        waiter = threading.Thread(
            target=_hold,
            args=(staging, not first_exclusive, entered, done),
        )
        waiter.start()
        try:
            assert not entered.wait(0.3)
        finally:
            release.set()
            holder.join(10)
        assert entered.wait(10)
        done.set()
        waiter.join(10)
        assert "waiting for another ltvm deploy or build" in (
            capsys.readouterr().err
        )

    def test_exclusive_waits_for_another_process(self, tmp_path: Path) -> None:
        staging = tmp_path / "staging"
        lock = lb.staging_lock_path(staging)
        lock.touch()
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, sys\n"
                f"f = open({str(lock)!r})\n"
                "fcntl.flock(f, fcntl.LOCK_SH)\n"
                "print('held', flush=True)\n"
                "sys.stdin.readline()\n",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert holder.stdout is not None and holder.stdin is not None
        try:
            assert holder.stdout.readline().strip() == "held"
            entered, done = threading.Event(), threading.Event()
            waiter = threading.Thread(
                target=_hold, args=(staging, True, entered, done)
            )
            waiter.start()
            assert not entered.wait(0.3)
            holder.stdin.write("\n")
            holder.stdin.flush()
            assert entered.wait(10)
            done.set()
            waiter.join(10)
        finally:
            holder.kill()
            holder.wait()

    def test_a_read_only_lock_file_still_locks(self, tmp_path: Path) -> None:
        """A lock file root left behind opens read-only for the user."""
        staging = tmp_path / "staging"
        lb.staging_lock_path(staging).touch()
        real_open = os.open

        def no_write(path, flags, *a):
            if flags & os.O_RDWR:
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *a)

        with patch.object(lb.os, "open", side_effect=no_write):
            with lb.staging_lock(staging, exclusive=True):
                assert _lock_state(staging) == "exclusive"


class TestBuildTakesItExclusive:
    def test_build_lustre(self, tmp_path: Path) -> None:
        lustre_tree = tmp_path / "lustre"
        (lustre_tree / "lustre" / "kernel_patches").mkdir(parents=True)
        build_tree = tmp_path / "kernel"
        (build_tree / "include" / "config").mkdir(parents=True)
        (build_tree / "include" / "config" / "kernel.release").write_text(
            "5.14.0-fake"
        )
        (build_tree / "Module.symvers").write_text("")
        seen: dict = {}

        def fake_build(tree, _bt, _tag, kver, *a, **kw):
            _, staging = lb._resolve_staging(
                tree, kw["target"], kw["arch"], kw["kernel"], kver, "base"
            )
            seen["state"] = _lock_state(staging)
            return {}

        with (
            patch.object(lb, "_container_exists", return_value=True),
            patch.object(lb, "_build_in_container", side_effect=fake_build),
        ):
            lb.build_lustre(
                lustre_tree,
                build_tree,
                container_tag="ltvm-build-rocky9",
                target="rocky9",
                kernel=_KERNEL,
            )
        assert seen["state"] == "exclusive"


@pytest.fixture
def tmp_sockets(tmp_path: Path):
    with patch("ltvm_pkg.vm_state.SOCKETS", tmp_path):
        yield tmp_path


class TestDeploy:
    """deploy-lustre refreshes under the exclusive lock, streams under
    the shared one, and holds neither while its build runs."""

    def _vm(self, kernel: str | None = None):
        from tests.test_deploy import _make_vm

        vm = _make_vm(name="co1-lock", ip="10.0.0.41")
        vm.os_id = "rocky9"
        if kernel:
            vm.kernel = kernel
        vm.save()
        return vm

    def test_refresh_then_stream(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg import staging_sources as ss
        from tests.test_deploy import _deploy_args

        tree, staging = _tree_and_staging(tmp_path)
        ss.write_manifest(staging, tree)
        _mark_staging_fresh(staging, tree, _stub_tc())
        _edit_keeping_old_mtime(
            tree / "lustre/tests/sanity-quota.sh", "echo v2\n"
        )
        self._vm()
        seen: dict = {}
        real_refresh = ss.refresh

        def refresh(staging_arg, tree_arg):
            seen["refresh"] = _lock_state(staging_arg)
            return real_refresh(staging_arg, tree_arg)

        def deploy(_vm, staging_arg, **_kw):
            seen["stream"] = _lock_state(staging_arg)
            seen["script"] = (
                staging_arg / _TESTS / "sanity-quota.sh"
            ).read_text()

        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch.object(ss, "refresh", side_effect=refresh),
            patch("ltvm_pkg.cli.deploy_to_vm", side_effect=deploy),
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(vm="co1-lock", lustre_tree=str(tree))
            )
        assert rc == 0
        assert seen == {
            "refresh": "exclusive",
            "stream": "shared",
            "script": "echo v2\n",
        }
        assert _lock_state(staging) == "free"

    def test_no_lock_is_held_across_the_build(
        self, tmp_sockets: Path, tmp_path: Path
    ) -> None:
        """The child `ltvm build lustre` takes the lock exclusive, so
        the parent holding it in any mode would deadlock."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.cli import deploy as cli_deploy
        from tests.test_deploy import _deploy_args

        tree, staging = _tree_and_staging(tmp_path)
        (staging / ".ltvm-staging-stamp").unlink()
        self._vm()
        seen: dict = {}

        def run(cmd, *a, **kw):
            if cmd[:3] == ["ltvm", "build", "lustre"]:
                seen["build"] = _lock_state(staging)
                return MagicMock(returncode=0)
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch.object(cli_mod, "TargetConfig", return_value=_stub_tc()),
            patch.object(cli_mod, "_gate_lustre_validation"),
            patch.object(cli_deploy.subprocess, "run", side_effect=run),
            patch("ltvm_pkg.cli.deploy_to_vm"),
        ):
            rc = cli_mod.cmd_deploy(
                _deploy_args(vm="co1-lock", lustre_tree=str(tree))
            )
        assert rc == 0
        assert seen["build"] == "free"


class TestClusterDeploy:
    def test_build_unlocked_streams_shared(self, tmp_path: Path) -> None:
        class _TC:
            os_family = "rhel"

        kernel_dir = "5.14-rhel9.7-5.14.0-611.55.1.el9_7"
        staging = lb.staging_path(
            tmp_path, "rocky9", arch="x86_64", kernel=kernel_dir
        )
        cluster = ClusterInfo(
            name="co3",
            nodes=[
                {"name": "co3-mds", "roles": ["mgs", "mds"]},
                {"name": "co3-oss1", "roles": ["oss"]},
            ],
        )
        node = MagicMock(
            rail_topology=None,
            os_id="rocky9",
            arch="x86_64",
            variant="base",
            kernel=f"/a/kernels/{kernel_dir}/vmlinuz",
            ip="10.0.0.5",
        )
        seen: dict = {"streams": []}

        def run(cmd, *a, **kw):
            seen["build"] = _lock_state(staging)
            return MagicMock(returncode=0)

        def deploy_one(name, *a, **kw):
            seen["streams"].append(_lock_state(staging))
            return name, 0, ""

        with (
            patch.object(ClusterInfo, "load", return_value=cluster),
            patch.object(vm_cluster.VMInfo, "load", return_value=node),
            patch.object(vm_cluster, "_validate_lustre_source"),
            patch("ltvm_pkg.target_config.TargetConfig", return_value=_TC()),
            patch.object(vm_cluster.subprocess, "run", side_effect=run),
            patch.object(
                vm_cluster, "_deploy_one_node", side_effect=deploy_one
            ),
            patch.object(vm_cluster, "probe_mgs_lnet"),
            patch.object(vm_cluster, "generate_local_sh", return_value=""),
            patch.object(
                vm_cluster,
                "_write_cluster_local_sh",
                side_effect=lambda name, *a, **k: (name, 0, ""),
            ),
            patch.object(
                vm_cluster, "_distribute_cluster_hosts", return_value=[]
            ),
        ):
            vm_cluster.cmd_cluster_deploy(
                argparse.Namespace(
                    name="co3", lustre_source=str(tmp_path), mount=False
                )
            )
        assert seen == {"build": "free", "streams": ["shared", "shared"]}
        assert _lock_state(staging) == "free"
