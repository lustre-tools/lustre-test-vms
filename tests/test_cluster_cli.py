"""Behavioral tests for cmd_cluster CLI dispatch and vm_cluster handlers.

Pin down the dispatch surface of ``cli.cmd_cluster`` and the cluster
handlers in ``vm_cluster`` so a refactor that splits the dispatcher into
``cmd_cluster_create`` / ``cmd_cluster_deploy`` / etc. wrappers cannot
silently drop, swap, or mis-parse a sub-action.

Coverage focus (per the cli.py refactor plan):
  - Each ``cluster <sub>`` action invokes the matching vm_cluster handler
    with the exact namespace the current dispatcher builds.
  - Argument parsing inside each ``if action == "<sub>":`` branch
    (positional vs --target, repeatable --nic, unknown flags, conflicts).
  - Error paths return EXIT_ERROR with helpful messages.
  - Root requirement on create / destroy.
  - JSON vs text error envelopes.
  - cmd_cluster_destroy / cmd_cluster_list / cmd_cluster_status / etc.
    behavior on real on-disk ClusterInfo state.
"""

from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import vm_cluster
from ltvm_pkg.cli import EXIT_ERROR, EXIT_OK, cmd_cluster
from ltvm_pkg.vm_state import (
    ClusterInfo,
    ClusterNotFound,
    VMNotFound,
)


# ── helpers ──────────────────────────────────────────────


def _ns(action: str, *cargs: str, json_out: bool = False) -> argparse.Namespace:
    """Build the namespace cmd_cluster expects (parser-level)."""
    return argparse.Namespace(
        action=action,
        cluster_args=list(cargs),
        json=json_out,
    )


@pytest.fixture
def tmp_sockets(tmp_path: Path) -> Path:
    """Redirect SOCKETS so ClusterInfo round-trips on a tmp dir."""
    with patch("ltvm_pkg.vm_state.SOCKETS", tmp_path):
        yield tmp_path


@pytest.fixture
def as_root() -> Any:
    """Bypass _require_root so create/destroy don't reject in CI."""
    with patch("ltvm_pkg.cli._require_root", return_value=None):
        yield


def _save_cluster(
    name: str = "co1",
    nodes: list[dict] | None = None,
) -> ClusterInfo:
    """Save a small cluster on disk (caller patches SOCKETS first)."""
    if nodes is None:
        nodes = [
            {
                "name": "co1-mds",
                "roles": ["mgs", "mds"],
                "mdt_disks": 1,
                "ost_disks": 0,
                "ip": "10.0.0.10",
            },
            {
                "name": "co1-oss",
                "roles": ["oss"],
                "mdt_disks": 0,
                "ost_disks": 3,
                "ip": "10.0.0.11",
            },
        ]
    c = ClusterInfo(name=name, nodes=nodes)
    c.save()
    return c


# ─────────────────────────────────────────────────────────
# Dispatch correctness: each action routes to the right handler
# ─────────────────────────────────────────────────────────


class TestCmdClusterDispatch:
    """Each ``cluster <sub>`` action invokes exactly the matching handler.

    The existing parser_coverage test only checks "doesn't return 1";
    these tests pin the routing one-to-one so a typo or swap during the
    cli.py split is caught instantly.
    """

    @pytest.fixture(autouse=True)
    def _mock_handlers(self, as_root: Any) -> Any:
        """Replace every vm_cluster handler with a MagicMock and yield them."""
        names = [
            "cmd_cluster_create",
            "cmd_cluster_destroy",
            "cmd_cluster_deploy",
            "cmd_cluster_status",
            "cmd_cluster_exec",
            "cmd_cluster_list",
            "cmd_cluster_ssh",
        ]
        with contextlib.ExitStack() as stack:
            mocks = {
                n: stack.enter_context(patch(f"ltvm_pkg.vm_cluster.{n}"))
                for n in names
            }
            self.mocks = mocks
            yield mocks

    def _assert_only(self, expected: str) -> None:
        """Exactly one handler was called, and it was `expected`."""
        called = [n for n, m in self.mocks.items() if m.called]
        assert called == [expected], (
            f"expected only {expected!r} to be called, got {called!r}"
        )

    def test_create_routes_to_create(self) -> None:
        rc = cmd_cluster(_ns("create", "co1", "mgs+mds:co1-mds:1"))
        assert rc == EXIT_OK
        self._assert_only("cmd_cluster_create")

    def test_destroy_routes_to_destroy(self) -> None:
        rc = cmd_cluster(_ns("destroy", "co1"))
        assert rc == EXIT_OK
        self._assert_only("cmd_cluster_destroy")

    def test_deploy_routes_to_deploy(self) -> None:
        rc = cmd_cluster(_ns("deploy", "co1"))
        assert rc == EXIT_OK
        self._assert_only("cmd_cluster_deploy")

    def test_status_routes_to_status(self) -> None:
        rc = cmd_cluster(_ns("status", "co1"))
        assert rc == EXIT_OK
        self._assert_only("cmd_cluster_status")

    def test_exec_routes_to_exec(self) -> None:
        rc = cmd_cluster(_ns("exec", "co1", "oss", "lctl dl"))
        assert rc == EXIT_OK
        self._assert_only("cmd_cluster_exec")

    def test_list_routes_to_list(self) -> None:
        rc = cmd_cluster(_ns("list"))
        assert rc == EXIT_OK
        self._assert_only("cmd_cluster_list")

    def test_ssh_routes_to_ssh(self) -> None:
        rc = cmd_cluster(_ns("ssh", "co1", "oss"))
        assert rc == EXIT_OK
        self._assert_only("cmd_cluster_ssh")

    def test_unknown_action_returns_error(self) -> None:
        rc = cmd_cluster(_ns("bogus"))
        assert rc == EXIT_ERROR
        for m in self.mocks.values():
            assert not m.called

    def test_handler_systemexit_propagates_returncode(self) -> None:
        """A handler that calls die(...) (SystemExit) must yield its rc."""
        self.mocks["cmd_cluster_status"].side_effect = SystemExit(7)
        rc = cmd_cluster(_ns("status", "co1"))
        assert rc == 7

    def test_handler_systemexit_none_becomes_exit_error(self) -> None:
        """SystemExit(None) (sys.exit() with no arg) maps to EXIT_ERROR."""
        self.mocks["cmd_cluster_status"].side_effect = SystemExit()
        rc = cmd_cluster(_ns("status", "co1"))
        assert rc == EXIT_ERROR


# ─────────────────────────────────────────────────────────
# cmd_cluster create: argument parsing pins the namespace fields
# ─────────────────────────────────────────────────────────


class TestClusterCreateArgs:
    """``cluster create`` parses optional flags and forwards to handler."""

    @pytest.fixture(autouse=True)
    def _patch(self, as_root: Any) -> Any:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_create") as m:
            self.handler = m
            yield m

    def _captured_ns(self) -> argparse.Namespace:
        assert self.handler.called, "cmd_cluster_create not called"
        return self.handler.call_args.args[0]

    def test_minimum_invocation_passes_defaults(self) -> None:
        rc = cmd_cluster(_ns("create", "co1", "mgs+mds:co1-mds:1"))
        assert rc == EXIT_OK
        ns = self._captured_ns()
        assert ns.name == "co1"
        assert ns.nodes == ["mgs+mds:co1-mds:1"]
        assert ns.vcpus == 2  # parser default
        # mem=None means "let target default win" -- pinning this is
        # critical: the previous bug hardcoded 4096 here.
        assert ns.mem is None
        assert ns.os is None
        assert ns.arch is None
        assert ns.disk_size is None
        assert ns.nic == []

    def test_vcpus_and_mem_flags_parsed(self) -> None:
        cmd_cluster(
            _ns("create", "co1", "--vcpus", "8", "--mem", "8192",
                "mgs+mds:co1-mds:1")
        )
        ns = self._captured_ns()
        assert ns.vcpus == 8
        assert ns.mem == 8192

    def test_target_flag_form(self) -> None:
        cmd_cluster(
            _ns("create", "co1", "--target", "rocky10",
                "mgs+mds:co1-mds:1")
        )
        ns = self._captured_ns()
        assert ns.os == "rocky10"

    def test_positional_target_form(self) -> None:
        """Bare token between cluster name and first spec is the target."""
        cmd_cluster(
            _ns("create", "co1", "rocky10", "mgs+mds:co1-mds:1")
        )
        ns = self._captured_ns()
        assert ns.os == "rocky10"
        # Positional target must be stripped from `nodes` -- otherwise
        # parse_node_spec would later choke on "rocky10".
        assert "rocky10" not in ns.nodes
        assert ns.nodes == ["mgs+mds:co1-mds:1"]

    def test_positional_and_flag_target_must_agree(self) -> None:
        """Conflicting positional + --target is an error."""
        rc = cmd_cluster(
            _ns(
                "create",
                "co1",
                "rocky10",
                "--target",
                "rocky9",
                "mgs+mds:co1-mds:1",
            )
        )
        assert rc == EXIT_ERROR
        assert not self.handler.called

    def test_positional_and_flag_target_agree_ok(self) -> None:
        """Same value via both means "fine"."""
        cmd_cluster(
            _ns(
                "create",
                "co1",
                "rocky10",
                "--target",
                "rocky10",
                "mgs+mds:co1-mds:1",
            )
        )
        ns = self._captured_ns()
        assert ns.os == "rocky10"

    def test_arch_disk_size_and_repeatable_nic(self) -> None:
        cmd_cluster(
            _ns(
                "create",
                "co1",
                "--arch",
                "aarch64",
                "--disk-size",
                "5G",
                "--nic",
                "nat",
                "--nic",
                "softroce",
                "mgs+mds:co1-mds:1",
            )
        )
        ns = self._captured_ns()
        assert ns.arch == "aarch64"
        assert ns.disk_size == "5G"
        # --nic is repeatable; both values land in the list in order.
        assert ns.nic == ["nat", "softroce"]

    def test_unknown_flag_errors(self) -> None:
        rc = cmd_cluster(
            _ns("create", "co1", "--frobnicate", "mgs+mds:co1-mds:1")
        )
        assert rc == EXIT_ERROR
        assert not self.handler.called

    def test_missing_node_spec_errors(self) -> None:
        rc = cmd_cluster(_ns("create", "co1"))
        assert rc == EXIT_ERROR
        assert not self.handler.called

    def test_missing_node_spec_after_positional_target_errors(self) -> None:
        """Name + positional target but no node spec -> error."""
        rc = cmd_cluster(_ns("create", "co1", "rocky10"))
        assert rc == EXIT_ERROR
        assert not self.handler.called

    def test_no_args_errors(self) -> None:
        rc = cmd_cluster(_ns("create"))
        assert rc == EXIT_ERROR
        assert not self.handler.called

    def test_only_flags_no_positionals_errors(self) -> None:
        """``cluster create --vcpus 8 --mem 4096`` with no name/spec fails.

        This is the second-pass positional check (after flag parsing
        consumes everything).  The pre-parse `len(cargs) < 2` check
        passes (we have 4 args), so the secondary check at line 2846
        must catch it.
        """
        rc = cmd_cluster(
            _ns("create", "--vcpus", "8", "--mem", "4096")
        )
        assert rc == EXIT_ERROR
        assert not self.handler.called

    def test_only_name_after_flag_parsing_errors(self) -> None:
        """``cluster create --vcpus 8 co1`` -- name but no spec."""
        rc = cmd_cluster(_ns("create", "--vcpus", "8", "co1"))
        assert rc == EXIT_ERROR
        assert not self.handler.called

    def test_multiple_node_specs_pass_through(self) -> None:
        cmd_cluster(
            _ns(
                "create",
                "co2",
                "mgs+mds:co2-mds:1",
                "oss:co2-oss:3",
                "client:co2-c:0",
            )
        )
        ns = self._captured_ns()
        assert ns.nodes == [
            "mgs+mds:co2-mds:1",
            "oss:co2-oss:3",
            "client:co2-c:0",
        ]


# ─────────────────────────────────────────────────────────
# cmd_cluster create: root requirement
# ─────────────────────────────────────────────────────────


class TestClusterCreateRoot:
    def test_create_requires_root(self) -> None:
        """Without root, create errors before invoking the handler."""
        with (
            patch("ltvm_pkg.vm_cluster.cmd_cluster_create") as m,
            patch("os.getuid", return_value=1000),
        ):
            rc = cmd_cluster(_ns("create", "co1", "mgs+mds:co1-mds:1"))
        assert rc == EXIT_ERROR
        assert not m.called

    def test_destroy_requires_root(self) -> None:
        with (
            patch("ltvm_pkg.vm_cluster.cmd_cluster_destroy") as m,
            patch("os.getuid", return_value=1000),
        ):
            rc = cmd_cluster(_ns("destroy", "co1"))
        assert rc == EXIT_ERROR
        assert not m.called

    def test_deploy_does_not_require_root(self) -> None:
        with (
            patch("ltvm_pkg.vm_cluster.cmd_cluster_deploy") as m,
            patch("os.getuid", return_value=1000),
        ):
            rc = cmd_cluster(_ns("deploy", "co1"))
        assert rc == EXIT_OK
        assert m.called

    def test_list_does_not_require_root(self) -> None:
        with (
            patch("ltvm_pkg.vm_cluster.cmd_cluster_list") as m,
            patch("os.getuid", return_value=1000),
        ):
            rc = cmd_cluster(_ns("list"))
        assert rc == EXIT_OK
        assert m.called


# ─────────────────────────────────────────────────────────
# cmd_cluster deploy: argument parsing
# ─────────────────────────────────────────────────────────


class TestClusterDeployArgs:
    """``cluster deploy`` parses --build/--mount/--server-only/--force-compat."""

    @pytest.fixture(autouse=True)
    def _patch(self) -> Any:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_deploy") as m:
            self.handler = m
            yield m

    def _ns_call(self) -> argparse.Namespace:
        assert self.handler.called
        return self.handler.call_args.args[0]

    def test_defaults(self) -> None:
        rc = cmd_cluster(_ns("deploy", "co1"))
        assert rc == EXIT_OK
        ns = self._ns_call()
        assert ns.name == "co1"
        # Default --build is "." (cwd).
        assert ns.lustre_source == "."
        assert ns.mount is False
        assert ns.server_only is False
        assert ns.force_compat is False

    def test_build_flag(self) -> None:
        cmd_cluster(_ns("deploy", "co1", "--build", "/path/to/lustre"))
        ns = self._ns_call()
        assert ns.lustre_source == "/path/to/lustre"

    def test_mount_flag(self) -> None:
        cmd_cluster(_ns("deploy", "co1", "--mount"))
        ns = self._ns_call()
        assert ns.mount is True

    def test_server_only_flag(self) -> None:
        cmd_cluster(_ns("deploy", "co1", "--server-only"))
        ns = self._ns_call()
        assert ns.server_only is True

    def test_force_compat_flag(self) -> None:
        cmd_cluster(_ns("deploy", "co1", "--force-compat"))
        ns = self._ns_call()
        assert ns.force_compat is True

    def test_all_flags_combined(self) -> None:
        cmd_cluster(
            _ns(
                "deploy",
                "co1",
                "--build",
                "/x",
                "--mount",
                "--server-only",
                "--force-compat",
            )
        )
        ns = self._ns_call()
        assert ns.lustre_source == "/x"
        assert ns.mount and ns.server_only and ns.force_compat

    def test_unknown_flag_errors(self) -> None:
        rc = cmd_cluster(_ns("deploy", "co1", "--frob"))
        assert rc == EXIT_ERROR
        assert not self.handler.called

    def test_missing_name_errors(self) -> None:
        rc = cmd_cluster(_ns("deploy"))
        assert rc == EXIT_ERROR
        assert not self.handler.called


# ─────────────────────────────────────────────────────────
# cmd_cluster exec / ssh / status / destroy / list arg shape
# ─────────────────────────────────────────────────────────


class TestClusterExecArgs:
    @pytest.fixture(autouse=True)
    def _patch(self) -> Any:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_exec") as m:
            self.handler = m
            yield m

    def test_exec_passes_command_list(self) -> None:
        rc = cmd_cluster(_ns("exec", "co1", "oss", "lctl", "dl"))
        assert rc == EXIT_OK
        ns = self.handler.call_args.args[0]
        assert ns.name == "co1"
        assert ns.target == "oss"
        # All trailing tokens become the command list (preserving multi-arg).
        assert ns.command == ["lctl", "dl"]
        # Default timeout pinned to 120s.
        assert ns.timeout == 120

    def test_exec_too_few_args_errors(self) -> None:
        rc = cmd_cluster(_ns("exec", "co1"))
        assert rc == EXIT_ERROR
        assert not self.handler.called

        rc = cmd_cluster(_ns("exec", "co1", "oss"))
        assert rc == EXIT_ERROR


class TestClusterSshArgs:
    @pytest.fixture(autouse=True)
    def _patch(self) -> Any:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_ssh") as m:
            self.handler = m
            yield m

    def test_ssh_with_command(self) -> None:
        cmd_cluster(_ns("ssh", "co1", "mds", "uptime"))
        ns = self.handler.call_args.args[0]
        assert ns.name == "co1"
        assert ns.target == "mds"
        assert ns.command == ["uptime"]

    def test_ssh_no_command_passes_empty_list(self) -> None:
        cmd_cluster(_ns("ssh", "co1", "mds"))
        ns = self.handler.call_args.args[0]
        assert ns.command == []

    def test_ssh_too_few_args_errors(self) -> None:
        rc = cmd_cluster(_ns("ssh", "co1"))
        assert rc == EXIT_ERROR
        assert not self.handler.called


class TestClusterStatusArgs:
    def test_status_requires_name(self) -> None:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_status") as m:
            rc = cmd_cluster(_ns("status"))
        assert rc == EXIT_ERROR
        assert not m.called

    def test_status_passes_name(self) -> None:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_status") as m:
            cmd_cluster(_ns("status", "co7"))
        ns = m.call_args.args[0]
        assert ns.name == "co7"


class TestClusterDestroyArgs:
    def test_destroy_requires_name(self, as_root: Any) -> None:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_destroy") as m:
            rc = cmd_cluster(_ns("destroy"))
        assert rc == EXIT_ERROR
        assert not m.called


class TestClusterListArgs:
    def test_list_takes_no_args(self) -> None:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_list") as m:
            cmd_cluster(_ns("list"))
        # cmd_cluster_list receives an empty namespace -- pin that.
        ns = m.call_args.args[0]
        assert isinstance(ns, argparse.Namespace)


# ─────────────────────────────────────────────────────────
# JSON error envelope
# ─────────────────────────────────────────────────────────


class TestJsonErrorOutput:
    """``--json`` errors must emit a parseable JSON envelope."""

    def test_create_missing_args_json(
        self, as_root: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_create"):
            rc = cmd_cluster(_ns("create", json_out=True))
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        payload = json.loads(err)
        assert "error" in payload
        assert "node spec" in payload["error"]

    def test_unknown_action_json(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cmd_cluster(_ns("nosuch", json_out=True))
        assert rc == EXIT_ERROR
        payload = json.loads(capsys.readouterr().err)
        assert "error" in payload

    def test_deploy_unknown_flag_json(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cmd_cluster(_ns("deploy", "co1", "--bad", json_out=True))
        assert rc == EXIT_ERROR
        payload = json.loads(capsys.readouterr().err)
        assert "error" in payload
        assert "--bad" in payload["error"]


# ─────────────────────────────────────────────────────────
# vm_cluster handlers: behavior on real on-disk state
# ─────────────────────────────────────────────────────────


class TestCmdClusterListBehavior:
    """cmd_cluster_list reflects on-disk .cluster files."""

    def test_no_clusters_prints_placeholder(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vm_cluster.cmd_cluster_list(argparse.Namespace())
        out = capsys.readouterr().out
        assert "(no clusters)" in out

    def test_lists_each_cluster_with_node_summary(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _save_cluster(name="co1")

        # Force is_running -> False and VMInfo.load -> a stub.
        with (
            patch.object(vm_cluster, "is_running", return_value=False),
            patch.object(vm_cluster, "VMInfo") as mock_vm,
        ):
            mock_vm.load.return_value = MagicMock()
            vm_cluster.cmd_cluster_list(argparse.Namespace())

        out = capsys.readouterr().out
        assert "co1:" in out
        assert "co1-mds(mgs+mds,down)" in out
        assert "co1-oss(oss,down)" in out

    def test_marks_missing_vms(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _save_cluster(name="co1")
        with patch.object(
            vm_cluster, "VMInfo"
        ) as mock_vm:
            mock_vm.load.side_effect = VMNotFound("co1-mds")
            vm_cluster.cmd_cluster_list(argparse.Namespace())
        out = capsys.readouterr().out
        # Both nodes show as "missing" since VMInfo.load always raises.
        assert "missing" in out

    def test_skips_disappeared_cluster(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Race: .cluster file vanishes between all_names() and load()."""
        with (
            patch.object(
                ClusterInfo, "all_names", return_value=["phantom"]
            ),
            patch.object(
                ClusterInfo, "load", side_effect=ClusterNotFound("phantom")
            ),
        ):
            vm_cluster.cmd_cluster_list(argparse.Namespace())
        out = capsys.readouterr().out
        # No traceback; phantom not listed.
        assert "phantom" not in out

    def test_corrupt_cluster_flagged_not_crashed(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A corrupt .cluster file (invalid JSON) must not crash the
        whole list -- flag that one and keep listing the rest."""
        with (
            patch.object(
                ClusterInfo, "all_names", return_value=["broken", "ok"]
            ),
            patch.object(
                ClusterInfo, "load",
                side_effect=[
                    RuntimeError("invalid JSON in /tmp/broken.cluster"),
                    MagicMock(get_nodes=MagicMock(return_value=[])),
                ],
            ),
        ):
            vm_cluster.cmd_cluster_list(argparse.Namespace())
        out = capsys.readouterr().out
        assert "broken:" in out
        assert "<BROKEN" in out
        assert "ok:" in out

    def test_malformed_cluster_flagged_not_crashed(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """ValueError from cluster loader is also recovered from."""
        with (
            patch.object(
                ClusterInfo, "all_names", return_value=["bad"]
            ),
            patch.object(
                ClusterInfo, "load",
                side_effect=ValueError("missing required field: nodes"),
            ),
        ):
            vm_cluster.cmd_cluster_list(argparse.Namespace())
        out = capsys.readouterr().out
        assert "bad: <BROKEN" in out
        assert "missing required field" in out


class TestCmdClusterStatusBehavior:
    """cmd_cluster_status prints per-node table + raises when missing."""

    def test_missing_cluster_raises(self, tmp_sockets: Path) -> None:
        with pytest.raises(ClusterNotFound):
            vm_cluster.cmd_cluster_status(
                argparse.Namespace(name="phantom")
            )

    def test_status_output_format(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _save_cluster(name="co1")
        with (
            patch.object(vm_cluster, "is_running", return_value=True),
            patch.object(vm_cluster, "VMInfo") as mock_vm,
        ):
            mock_vm.load.return_value = MagicMock()
            vm_cluster.cmd_cluster_status(argparse.Namespace(name="co1"))

        out = capsys.readouterr().out
        assert "cluster: co1" in out
        assert "nodes:   2" in out
        # Per-node row contains name, ip, status, roles
        assert "co1-mds" in out
        assert "10.0.0.10" in out
        assert "running" in out
        assert "mgs+mds" in out
        assert "mdt=1" in out
        # OSS node disk count too
        assert "ost=3" in out

    def test_status_shows_stopped_when_vm_missing(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _save_cluster(name="co1")
        with patch.object(vm_cluster, "VMInfo") as mock_vm:
            mock_vm.load.side_effect = VMNotFound("co1-mds")
            vm_cluster.cmd_cluster_status(argparse.Namespace(name="co1"))
        out = capsys.readouterr().out
        assert "stopped" in out

    def test_status_standalone_mgs_shows_mgs_disk(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _save_cluster(
            name="split",
            nodes=[
                {
                    "name": "split-mgs",
                    "roles": ["mgs"],
                    "mdt_disks": 0,
                    "ost_disks": 0,
                    "ip": "10.0.0.1",
                },
                {
                    "name": "split-mds",
                    "roles": ["mds"],
                    "mdt_disks": 1,
                    "ost_disks": 0,
                    "ip": "10.0.0.2",
                },
                {
                    "name": "split-oss",
                    "roles": ["oss"],
                    "mdt_disks": 0,
                    "ost_disks": 1,
                    "ip": "10.0.0.3",
                },
            ],
        )
        with (
            patch.object(vm_cluster, "is_running", return_value=False),
            patch.object(vm_cluster, "VMInfo") as mock_vm,
        ):
            mock_vm.load.return_value = MagicMock()
            vm_cluster.cmd_cluster_status(argparse.Namespace(name="split"))
        out = capsys.readouterr().out
        # Standalone MGS gets the "mgs=1" disk annotation.
        assert "mgs=1" in out


class TestCmdClusterDestroyBehavior:
    """cmd_cluster_destroy removes the .cluster file and processes nodes."""

    def test_missing_cluster_raises(self, tmp_sockets: Path) -> None:
        with pytest.raises(ClusterNotFound):
            vm_cluster.cmd_cluster_destroy(
                argparse.Namespace(name="phantom")
            )

    def test_destroy_unlinks_cluster_file(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _save_cluster(name="co1")
        cluster_file = tmp_sockets / "co1.cluster"
        assert cluster_file.exists()

        with (
            patch.object(vm_cluster, "VMInfo") as mock_vm,
            patch.object(vm_cluster, "kill_qemu"),
            patch.object(vm_cluster, "unregister_ssh_name"),
            patch(
                "ltvm_pkg.vm_commands._destroy_vm_artifacts"
            ) as mock_destroy,
        ):
            mock_vm.load.return_value = MagicMock()
            vm_cluster.cmd_cluster_destroy(argparse.Namespace(name="co1"))

        assert not cluster_file.exists()
        # Per-node destroy invoked once per node (2 nodes).
        assert mock_destroy.call_count == 2

    def test_destroy_continues_when_vm_already_gone(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A missing VM during destroy must not abort cluster cleanup."""
        _save_cluster(name="co1")
        with (
            patch.object(vm_cluster, "VMInfo") as mock_vm,
            patch.object(vm_cluster, "kill_qemu"),
            patch.object(vm_cluster, "unregister_ssh_name"),
            patch(
                "ltvm_pkg.vm_commands._destroy_vm_artifacts"
            ) as mock_destroy,
        ):
            mock_vm.load.side_effect = VMNotFound("any")
            vm_cluster.cmd_cluster_destroy(argparse.Namespace(name="co1"))
        # Even with VMNotFound, _destroy_vm_artifacts still called per node.
        assert mock_destroy.call_count == 2
        assert not (tmp_sockets / "co1.cluster").exists()


class TestCmdClusterExecBehavior:
    """cmd_cluster_exec resolves the target by name OR role."""

    def test_target_by_role(self, tmp_sockets: Path) -> None:
        _save_cluster(name="co1")
        fake_vm = MagicMock(ip="10.0.0.11")
        completed = MagicMock(stdout="ok\n", stderr="", returncode=0)
        with (
            patch.object(vm_cluster, "VMInfo") as mock_vm,
            patch.object(vm_cluster, "run_ssh", return_value=completed) as ssh,
        ):
            mock_vm.load.return_value = fake_vm
            with pytest.raises(SystemExit) as exc:
                vm_cluster.cmd_cluster_exec(
                    argparse.Namespace(
                        name="co1",
                        target="oss",
                        command=["lctl", "dl"],
                        timeout=60,
                    )
                )
            assert exc.value.code == 0

        # The OSS node was matched -> VMInfo.load called with co1-oss.
        mock_vm.load.assert_called_with("co1-oss")
        # run_ssh called with the OSS IP and joined command.
        args, _ = ssh.call_args
        assert args[0] == "10.0.0.11"
        assert args[1] == "lctl dl"

    def test_target_by_name(self, tmp_sockets: Path) -> None:
        _save_cluster(name="co1")
        completed = MagicMock(stdout="", stderr="", returncode=0)
        with (
            patch.object(vm_cluster, "VMInfo") as mock_vm,
            patch.object(vm_cluster, "run_ssh", return_value=completed),
        ):
            mock_vm.load.return_value = MagicMock(ip="1.2.3.4")
            with pytest.raises(SystemExit):
                vm_cluster.cmd_cluster_exec(
                    argparse.Namespace(
                        name="co1",
                        target="co1-mds",
                        command=["uptime"],
                        timeout=10,
                    )
                )
        mock_vm.load.assert_called_with("co1-mds")

    def test_quoted_single_arg_passed_verbatim(
        self, tmp_sockets: Path
    ) -> None:
        """Regression for lustre_test_vms_v2-b0h.

        When the user types `ltvm cluster exec co1 oss 'lctl dl'`,
        the shell passes a SINGLE argv element 'lctl dl'.  Previously
        `shlex.join(['lctl dl'])` produced `"'lctl dl'"` and the
        remote bash read the quoted string as a command name with a
        space (rc=127, "command not found").  A one-element command
        list is now passed verbatim; shlex.join only engages when
        there are multiple argv elements to quote safely.
        """
        _save_cluster(name="co1")
        completed = MagicMock(stdout="", stderr="", returncode=0)
        with (
            patch.object(vm_cluster, "VMInfo") as mock_vm,
            patch.object(vm_cluster, "run_ssh", return_value=completed) as ssh,
        ):
            mock_vm.load.return_value = MagicMock(ip="10.0.0.11")
            with pytest.raises(SystemExit):
                vm_cluster.cmd_cluster_exec(
                    argparse.Namespace(
                        name="co1",
                        target="oss",
                        command=["lctl dl"],  # shell pre-joined
                        timeout=60,
                    )
                )
        # Exact command string reaching run_ssh must be the user's
        # literal, NOT the double-quoted form.
        assert ssh.call_args.args[1] == "lctl dl"
        assert "'" not in ssh.call_args.args[1]

    def test_multi_arg_still_quoted_safely(
        self, tmp_sockets: Path
    ) -> None:
        """The quoted-single-arg fix must not regress the splitting
        case where shlex.join protects args with spaces / globs.
        `command=['echo', 'hello world']` must transport as
        `echo 'hello world'` so the space survives the remote shell.
        """
        _save_cluster(name="co1")
        completed = MagicMock(stdout="", stderr="", returncode=0)
        with (
            patch.object(vm_cluster, "VMInfo") as mock_vm,
            patch.object(vm_cluster, "run_ssh", return_value=completed) as ssh,
        ):
            mock_vm.load.return_value = MagicMock(ip="10.0.0.11")
            with pytest.raises(SystemExit):
                vm_cluster.cmd_cluster_exec(
                    argparse.Namespace(
                        name="co1",
                        target="oss",
                        command=["echo", "hello world"],
                        timeout=60,
                    )
                )
        assert ssh.call_args.args[1] == "echo 'hello world'"

    def test_no_matching_target_dies(self, tmp_sockets: Path) -> None:
        _save_cluster(name="co1")
        with pytest.raises(SystemExit):
            vm_cluster.cmd_cluster_exec(
                argparse.Namespace(
                    name="co1",
                    target="nonexistent-role",
                    command=["x"],
                    timeout=10,
                )
            )

    def test_exec_propagates_remote_returncode(
        self, tmp_sockets: Path
    ) -> None:
        """Non-zero rc from run_ssh propagates via sys.exit(rc)."""
        _save_cluster(name="co1")
        completed = MagicMock(stdout="", stderr="boom", returncode=42)
        with (
            patch.object(vm_cluster, "VMInfo") as mock_vm,
            patch.object(vm_cluster, "run_ssh", return_value=completed),
        ):
            mock_vm.load.return_value = MagicMock(ip="1.2.3.4")
            with pytest.raises(SystemExit) as exc:
                vm_cluster.cmd_cluster_exec(
                    argparse.Namespace(
                        name="co1",
                        target="mds",
                        command=["false"],
                        timeout=10,
                    )
                )
            assert exc.value.code == 42


class TestCmdClusterSshBehavior:
    """cmd_cluster_ssh resolves target then execs sshpass."""

    def test_no_match_dies(self, tmp_sockets: Path) -> None:
        _save_cluster(name="co1")
        with pytest.raises(SystemExit):
            vm_cluster.cmd_cluster_ssh(
                argparse.Namespace(
                    name="co1",
                    target="nope",
                    command=[],
                )
            )

    def test_role_match_invokes_execvp(self, tmp_sockets: Path) -> None:
        _save_cluster(name="co1")
        with (
            patch.object(vm_cluster, "VMInfo") as mock_vm,
            patch.object(vm_cluster.os, "execvp") as ev,
        ):
            mock_vm.load.return_value = MagicMock(ip="10.0.0.11")
            vm_cluster.cmd_cluster_ssh(
                argparse.Namespace(
                    name="co1",
                    target="oss",
                    command=["uptime"],
                )
            )
        assert ev.called
        prog, argv = ev.call_args.args
        assert prog == "sshpass"
        assert argv[0] == "sshpass"
        # Command tail is appended after the SSH target.
        assert argv[-1] == "uptime"
        # And the SSH target is root@<ip>.
        assert any(a == "root@10.0.0.11" for a in argv)
