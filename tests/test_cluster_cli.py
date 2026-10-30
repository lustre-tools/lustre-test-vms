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
import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import vm_cluster
from ltvm_pkg.cli import EXIT_ERROR, EXIT_OK
from ltvm_pkg.vm_state import (
    ClusterInfo,
    ClusterNotFound,
    VMNotFound,
)

# ── helpers ──────────────────────────────────────────────


def _cluster_parser() -> argparse.ArgumentParser:
    """The real ltvm parser, loaded once."""
    from tests.test_parser_coverage import ltvm as ltvm_mod

    return ltvm_mod.build_parser()


def _ns(action: str, *cargs: str, json_out: bool = False) -> argparse.Namespace:
    """Parse a real `ltvm cluster <action> ...` command line.

    These used to hand-build the namespace the old dispatcher expected
    (an action string plus an opaque REMAINDER list), which meant none of
    them exercised any parsing -- the parsing lived in cmd_cluster and
    was tested only through its results.  Now that each action is a real
    subparser, going through the parser is both more honest and what
    catches a misdeclared argument.

    --json goes immediately after the action, not at the end: `exec` and
    `ssh` take their command as REMAINDER, which would otherwise swallow
    it as part of the command.
    """
    argv = ["cluster", action]
    if json_out:
        argv.append("--json")
    argv.extend(cargs)
    return _cluster_parser().parse_args(argv)


def cmd_cluster(args: argparse.Namespace) -> int:
    """Dispatch a parsed namespace the way main() does."""
    rc = args.func(args)
    return int(rc) if isinstance(rc, int) else EXIT_OK


def _expect_usage_error(action: str, *cargs: str) -> str:
    """Assert argparse rejects this command line, and return its stderr.

    A malformed cluster command line used to be ltvm's own EXIT_ERROR,
    because `cluster` was one REMAINDER parsed by hand.  Each action is a
    real subparser now, so argparse rejects it with a usage message and
    exit 2 -- which is what `ltvm create` with no name already did, so
    cluster is the consistent one now rather than the exception.
    """
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        with pytest.raises(SystemExit) as exc:
            _ns(action, *cargs)
    assert exc.value.code == 2
    return err.getvalue()


@pytest.fixture
def tmp_sockets(tmp_path: Path) -> Path:
    """Redirect SOCKETS so ClusterInfo round-trips on a tmp dir.

    vm_cluster holds its own binding, which the create path's "already
    exists" checks read -- unpatched, a host with a real cluster of the
    same name failed the dry-run tests.
    """
    with (
        patch("ltvm_pkg.vm_state.SOCKETS", tmp_path),
        patch("ltvm_pkg.vm_cluster.SOCKETS", tmp_path),
    ):
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
            "cmd_cluster_llmount",
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

    def test_llmount_routes_to_llmount(self) -> None:
        rc = cmd_cluster(_ns("llmount", "co1"))
        assert rc == EXIT_OK
        self._assert_only("cmd_cluster_llmount")
        assert not self.mocks["cmd_cluster_llmount"].call_args.args[0].cleanup

    def test_llumount_routes_to_llmount_with_cleanup(self) -> None:
        rc = cmd_cluster(_ns("llumount", "co1"))
        assert rc == EXIT_OK
        self._assert_only("cmd_cluster_llmount")
        assert self.mocks["cmd_cluster_llmount"].call_args.args[0].cleanup

    def test_unknown_action_is_rejected_by_the_parser(self) -> None:
        err = _expect_usage_error("bogus")
        assert "invalid choice" in err
        for m in self.mocks.values():
            assert not m.called

    @pytest.mark.parametrize(
        ("argv", "handler"),
        [
            (("status", "co1"), "cmd_cluster_status"),
            (("list",), "cmd_cluster_list"),
            (("destroy", "co1"), "cmd_cluster_destroy"),
            (("exec", "co1", "oss", "lctl dl"), "cmd_cluster_exec"),
        ],
    )
    def test_json_reaches_the_handler(
        self, argv: tuple[str, ...], handler: str
    ) -> None:
        """--json must reach the handler, which decides its output on it."""
        assert cmd_cluster(_ns(*argv, json_out=True)) == EXIT_OK
        assert self.mocks[handler].call_args.args[0].json is True

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
        assert ns.root_size is None
        assert ns.nic == []
        assert ns.kernel_args is None
        assert ns.kernel is None
        assert ns.variant == "base"
        assert ns.wait == 0

    def test_kernel_variant_and_wait_reach_the_handler(self) -> None:
        cmd_cluster(
            _ns(
                "create",
                "co1",
                "--kernel",
                "5.14-rhel9.3",
                "--variant",
                "mofed",
                "--wait",
                "600",
                "mgs+mds:co1-mds:1",
            )
        )
        ns = self._captured_ns()
        assert ns.kernel == "5.14-rhel9.3"
        assert ns.variant == "mofed"
        assert ns.wait == 600

    def test_vcpus_and_mem_flags_parsed(self) -> None:
        cmd_cluster(
            _ns(
                "create",
                "co1",
                "--vcpus",
                "8",
                "--mem",
                "8192",
                "mgs+mds:co1-mds:1",
            )
        )
        ns = self._captured_ns()
        assert ns.vcpus == 8
        assert ns.mem == 8192

    def test_target_flag_form(self) -> None:
        cmd_cluster(
            _ns("create", "co1", "--target", "rocky10", "mgs+mds:co1-mds:1")
        )
        ns = self._captured_ns()
        assert ns.os == "rocky10"

    def test_positional_target_form(self) -> None:
        """Bare token between cluster name and first spec is the target."""
        cmd_cluster(_ns("create", "co1", "rocky10", "mgs+mds:co1-mds:1"))
        ns = self._captured_ns()
        assert ns.os == "rocky10"
        # Positional target must be stripped from `nodes` -- otherwise
        # parse_node_spec would later choke on "rocky10".
        assert "rocky10" not in ns.nodes
        assert ns.nodes == ["mgs+mds:co1-mds:1"]

    def test_positional_and_flag_target_must_agree(self) -> None:
        """Conflicting positional + --target is an error.

        Options go after the run of specs, not between them -- see
        test_cli_target_forms.test_option_between_specs_is_rejected.
        """
        rc = cmd_cluster(
            _ns(
                "create",
                "co1",
                "rocky10",
                "mgs+mds:co1-mds:1",
                "--target",
                "rocky9",
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
                "mgs+mds:co1-mds:1",
                "--target",
                "rocky10",
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
                "--root-size",
                "16G",
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
        assert ns.root_size == "16G"
        # --nic is repeatable; both values land in the list in order.
        assert ns.nic == ["nat", "softroce"]

    def test_kernel_args_parsed(self) -> None:
        cmd_cluster(
            _ns(
                "create",
                "co1",
                "--kernel-args",
                "slub_debug=FZPU page_owner=on",
                "mgs+mds:co1-mds:1",
            )
        )
        assert (
            self._captured_ns().kernel_args == "slub_debug=FZPU page_owner=on"
        )

    def test_unknown_flag_errors(self) -> None:
        err = _expect_usage_error(
            "create", "co1", "mgs+mds:co1-mds:1", "--frobnicate"
        )
        assert "--frobnicate" in err
        assert not self.handler.called

    def test_missing_node_spec_errors(self) -> None:
        _expect_usage_error("create", "co1")
        assert not self.handler.called

    def test_missing_node_spec_after_positional_target_errors(self) -> None:
        """Name + positional target but no node spec -> error."""
        rc = cmd_cluster(_ns("create", "co1", "rocky10"))
        assert rc == EXIT_ERROR
        assert not self.handler.called

    def test_no_args_errors(self) -> None:
        _expect_usage_error("create")
        assert not self.handler.called

    def test_only_flags_no_positionals_errors(self) -> None:
        """``cluster create --vcpus 8 --mem 4096`` with no name/spec fails.

        This is the second-pass positional check (after flag parsing
        consumes everything).  The pre-parse `len(cargs) < 2` check
        passes (we have 4 args), so the secondary check at line 2846
        must catch it.
        """
        _expect_usage_error("create", "--vcpus", "8", "--mem", "4096")
        assert not self.handler.called

    def test_only_name_after_flag_parsing_errors(self) -> None:
        """``cluster create --vcpus 8 co1`` -- name but no spec."""
        _expect_usage_error("create", "--vcpus", "8", "co1")
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
        assert ns.node_targets == [None, None, None]

    def test_node_target_mixes_targets(self) -> None:
        """--node-target mixes targets in one cluster, e.g. 4 KiB-page
        servers with 16 KiB-page clients."""
        cmd_cluster(
            _ns(
                "create",
                "co7",
                "rocky9",
                "mgs+mds:co7-mds:1",
                "oss:co7-oss:2",
                "client:co7-c1",
                "client:co7-c2",
                "--node-target",
                "co7-c1=rocky10-16k",
                "--node-target=co7-c2=rocky10-16k",
            )
        )
        ns = self._captured_ns()
        assert ns.os == "rocky9"
        assert ns.nodes == [
            "mgs+mds:co7-mds:1",
            "oss:co7-oss:2",
            "client:co7-c1",
            "client:co7-c2",
        ]
        assert ns.node_targets == [None, None, "rocky10-16k", "rocky10-16k"]

    def test_node_kernel_picks_one_nodes_kernel(self) -> None:
        """--node-kernel names a kernel of the node's own target, which
        --kernel (the cluster-wide target's) cannot."""
        cmd_cluster(
            _ns(
                "create",
                "co8",
                "rocky9",
                "mgs+mds+oss:co8-srv:4",
                "client:co8-c",
                "--node-target",
                "co8-c=opensuse156",
                "--node-kernel",
                "co8-c=6.4-sles15sp6",
            )
        )
        ns = self._captured_ns()
        assert ns.node_targets == [None, "opensuse156"]
        assert ns.node_kernels == [None, "6.4-sles15sp6"]

    @pytest.mark.parametrize(
        "extra",
        [
            ["client:co7-c1", "--node-kernel", "co7-c1"],
            ["client:co7-c1", "--node-kernel", "co7-c9=5.14-rhel9.5"],
            [
                "client:co7-c1",
                "--node-kernel",
                "co7-c1=5.14-rhel9.5",
                "--node-kernel",
                "co7-c1=5.14-rhel9.4",
            ],
        ],
    )
    def test_bad_node_kernel_errors(self, extra: list[str]) -> None:
        rc = cmd_cluster(
            _ns("create", "co7", "rocky9", "mgs+mds:co7-mds:1", *extra)
        )
        assert rc == EXIT_ERROR
        assert not self.handler.called

    @pytest.mark.parametrize(
        "extra",
        [
            # A target is not a node spec anywhere but first.
            ["client:co7-c1", "rocky10-16k"],
            ["client:co7-c1", "--node-target", "co7-c1"],
            ["client:co7-c1", "--node-target", "co7-c9=rocky10-16k"],
            [
                "client:co7-c1",
                "--node-target",
                "co7-c1=rocky10-16k",
                "--node-target",
                "co7-c1=rocky9-64k",
            ],
        ],
    )
    def test_bad_node_target_errors(self, extra: list[str]) -> None:
        rc = cmd_cluster(
            _ns("create", "co7", "rocky9", "mgs+mds:co7-mds:1", *extra)
        )
        assert rc == EXIT_ERROR
        assert not self.handler.called

    def test_failover_passes_partner_and_primary(self) -> None:
        cmd_cluster(
            _ns(
                "create",
                "co9",
                "mgs+mds:co9-mds1:1",
                "client:co9-c",
                "--failover",
                "co9-mds2=co9-mds1",
            )
        )
        ns = self._captured_ns()
        assert ns.failover == {"co9-mds2": "co9-mds1"}
        # The partner is a new node, not a spec.
        assert ns.nodes == ["mgs+mds:co9-mds1:1", "client:co9-c"]

    def test_no_failover_passes_an_empty_map(self) -> None:
        cmd_cluster(_ns("create", "co9", "mgs+mds:co9-mds1:1"))
        assert self._captured_ns().failover == {}

    @pytest.mark.parametrize(
        "extra",
        [
            ["--failover", "co9-mds2"],
            ["--failover", "=co9-mds1"],
            ["--failover", "co9-x=co9-mds1", "--failover", "co9-x=co9-mds1"],
        ],
    )
    def test_bad_failover_errors(self, extra: list[str]) -> None:
        rc = cmd_cluster(_ns("create", "co9", "mgs+mds:co9-mds1:1", *extra))
        assert rc == EXIT_ERROR
        assert not self.handler.called


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
        assert ns.mount_timeout is None

    def test_mount_timeout(self) -> None:
        cmd_cluster(_ns("deploy", "co1", "--mount", "--mount-timeout", "2100"))
        assert self._ns_call().mount_timeout == 2100

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

    def test_configure_reaches_the_handler(self) -> None:
        cmd_cluster(_ns("deploy", "co1", "--configure=--enable-crypto"))
        assert self._ns_call().configure == "--enable-crypto"

    def test_configure_defaults_to_none(self) -> None:
        cmd_cluster(_ns("deploy", "co1"))
        assert self._ns_call().configure is None

    def test_ip_family_flag(self) -> None:
        cmd_cluster(_ns("deploy", "co1", "--ip-family", "ipv6"))
        assert self._ns_call().ip_family == "ipv6"

    def test_ip_family_default_is_none(self) -> None:
        # None, not ipv4: a bare deploy keeps the cluster's recorded family.
        cmd_cluster(_ns("deploy", "co1"))
        assert self._ns_call().ip_family is None

    def test_ost_count_flag(self) -> None:
        cmd_cluster(_ns("deploy", "co1", "--ost-count", "8"))
        assert self._ns_call().ost_count == 8

    def test_ost_count_default_is_none(self) -> None:
        # None, not 0: a bare deploy keeps the cluster's recorded count.
        cmd_cluster(_ns("deploy", "co1"))
        assert self._ns_call().ost_count is None

    def test_invalid_ip_family_errors(self) -> None:
        err = _expect_usage_error("deploy", "co1", "--ip-family", "ipv5")
        assert "invalid choice" in err
        assert not self.handler.called

    def test_unknown_flag_errors(self) -> None:
        err = _expect_usage_error("deploy", "co1", "--frob")
        assert "--frob" in err
        assert not self.handler.called

    def test_invalid_fstype_errors(self) -> None:
        """--fstype was validated by hand; argparse choices do it now."""
        err = _expect_usage_error("deploy", "co1", "--fstype", "btrfs")
        assert "invalid choice" in err
        assert not self.handler.called

    def test_missing_name_errors(self) -> None:
        _expect_usage_error("deploy")
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

    def test_exec_without_a_role_is_a_usage_error(self) -> None:
        _expect_usage_error("exec", "co1")
        assert not self.handler.called

    def test_exec_without_a_command_is_ltvms_own_error(self) -> None:
        """The role parses; the missing command is ours to report."""
        rc = cmd_cluster(_ns("exec", "co1", "oss"))
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
        _expect_usage_error("ssh", "co1")
        assert not self.handler.called


class TestClusterStatusArgs:
    def test_status_requires_name(self) -> None:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_status") as m:
            _expect_usage_error("status")
        assert not m.called

    def test_status_passes_name(self) -> None:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_status") as m:
            cmd_cluster(_ns("status", "co7"))
        ns = m.call_args.args[0]
        assert ns.name == "co7"
        assert ns.wait_recovery is None

    def test_status_passes_wait_recovery(self) -> None:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_status") as m:
            cmd_cluster(_ns("status", "co7", "--wait-recovery", "90"))
        assert m.call_args.args[0].wait_recovery == 90


class TestClusterDestroyArgs:
    def test_destroy_requires_name(self, as_root: Any) -> None:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_destroy") as m:
            _expect_usage_error("destroy")
        assert not m.called

    @pytest.mark.parametrize("flag", ["--force", "-f", "--yes", "-y"])
    def test_accepts_a_confirmation_flag_it_does_not_need(
        self, as_root: Any, flag: str
    ) -> None:
        """As `ltvm destroy` does: destroy never prompts, and a habitual
        --force must not cost a retry."""
        for argv in (("co2", flag), (flag, "co2")):
            with patch("ltvm_pkg.vm_cluster.cmd_cluster_destroy") as m:
                rc = cmd_cluster(_ns("destroy", *argv))
            assert rc == EXIT_OK
            assert m.call_args.args[0].names == ["co2"]

    def test_takes_several_names(self, as_root: Any) -> None:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_destroy") as m:
            cmd_cluster(_ns("destroy", "co2", "co3"))
        assert m.call_args.args[0].names == ["co2", "co3"]


class TestClusterStartStop:
    """start/stop hand the cluster's nodes to `ltvm start` / `ltvm stop`."""

    def test_start_passes_every_node_and_wait(self, tmp_sockets: Path) -> None:
        _save_cluster(name="co1")
        with patch("ltvm_pkg.cli.vm.cmd_vm_start", return_value=0) as start:
            rc = cmd_cluster(_ns("start", "co1", "--wait", "60"))
        assert rc == EXIT_OK
        ns = start.call_args.args[0]
        assert ns.names == ["co1-mds", "co1-oss"]
        assert ns.wait == 60

    def test_stop_passes_the_nodes_of_every_cluster_named(
        self, tmp_sockets: Path
    ) -> None:
        _save_cluster(name="co1")
        _save_cluster(
            name="co2",
            nodes=[
                {"name": "co2-a", "roles": ["mgs", "mds"], "ip": "10.0.0.2"}
            ],
        )
        with patch("ltvm_pkg.cli.vm.cmd_vm_stop", return_value=0) as stop:
            rc = cmd_cluster(_ns("stop", "co1", "co2"))
        assert rc == EXIT_OK
        assert stop.call_args.args[0].names == ["co1-mds", "co1-oss", "co2-a"]

    @pytest.mark.parametrize("action", ["start", "stop"])
    def test_missing_cluster_is_an_error_and_touches_nothing(
        self, tmp_sockets: Path, action: str
    ) -> None:
        with (
            patch("ltvm_pkg.cli.vm.cmd_vm_start") as start,
            patch("ltvm_pkg.cli.vm.cmd_vm_stop") as stop,
        ):
            rc = cmd_cluster(_ns(action, "phantom"))
        assert rc == EXIT_ERROR
        assert not start.called and not stop.called

    def test_start_rc_is_the_single_vm_starts(self, tmp_sockets: Path) -> None:
        _save_cluster(name="co1")
        with patch("ltvm_pkg.cli.vm.cmd_vm_start", return_value=4):
            assert cmd_cluster(_ns("start", "co1")) == 4


class TestClusterLlmountArgs:
    def test_server_only_with_cleanup_is_refused(self) -> None:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_llmount") as m:
            rc = cmd_cluster(
                _ns("llmount", "co1", "--cleanup", "--server-only")
            )
        assert rc == EXIT_ERROR
        assert not m.called

    def test_flags_reach_the_handler(self) -> None:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_llmount") as m:
            cmd_cluster(
                _ns("llmount", "co1", "--server-only", "--timeout", "9")
            )
        ns = m.call_args.args[0]
        assert ns.name == "co1"
        assert ns.server_only and not ns.cleanup
        assert ns.timeout == 9


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

    def test_create_target_without_specs_json(
        self, as_root: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("ltvm_pkg.vm_cluster.cmd_cluster_create"):
            # name + a bare target, so argparse is satisfied and the
            # "no node specs" check is ltvm's own.
            rc = cmd_cluster(_ns("create", "co1", "rocky9", json_out=True))
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        payload = json.loads(err)
        assert "error" in payload
        assert "node spec" in payload["error"]

    def test_exec_without_a_command_json(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cmd_cluster(_ns("exec", "co1", "oss", json_out=True))
        assert rc == EXIT_ERROR
        payload = json.loads(capsys.readouterr().err)
        assert "error" in payload

    def test_target_conflict_json(
        self, as_root: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cmd_cluster(
            _ns(
                "create",
                "co1",
                "rocky9",
                "mgs+mds:co1-mds:1",
                "--target",
                "rocky10",
                json_out=True,
            )
        )
        assert rc == EXIT_ERROR
        payload = json.loads(capsys.readouterr().err)
        assert "error" in payload

    def test_a_malformed_command_line_is_argparses_to_report(self) -> None:
        """Not a JSON envelope: argparse owns the command line now, and
        reports it the same way for every ltvm subcommand."""
        assert "--bad" in _expect_usage_error("deploy", "co1", "--bad")


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
        with patch.object(vm_cluster, "VMInfo") as mock_vm:
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
            patch.object(ClusterInfo, "all_names", return_value=["phantom"]),
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
                ClusterInfo,
                "load",
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
            patch.object(ClusterInfo, "all_names", return_value=["bad"]),
            patch.object(
                ClusterInfo,
                "load",
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
            vm_cluster.cmd_cluster_status(argparse.Namespace(name="phantom"))

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
    """cmd_cluster_destroy removes the .cluster file and its nodes."""

    def test_missing_cluster_is_not_an_error(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """As `ltvm destroy` treats a missing VM, so a cleanup script can
        run it unconditionally."""
        with patch("ltvm_pkg.vm_commands.cmd_destroy") as destroy:
            vm_cluster.cmd_cluster_destroy(
                argparse.Namespace(names=["phantom"])
            )
        assert not destroy.called
        assert "destroy: cluster phantom not found" in capsys.readouterr().out

    def test_nodes_go_through_the_single_vm_destroy(
        self, tmp_sockets: Path
    ) -> None:
        _save_cluster(name="co1")
        cluster_file = tmp_sockets / "co1.cluster"
        assert cluster_file.exists()
        with patch("ltvm_pkg.vm_commands.cmd_destroy") as destroy:
            vm_cluster.cmd_cluster_destroy(argparse.Namespace(names=["co1"]))
        assert destroy.call_args.args[0].names == ["co1-mds", "co1-oss"]
        assert not cluster_file.exists()

    def test_several_clusters_and_a_missing_one(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _save_cluster(name="co1")
        _save_cluster(
            name="co2",
            nodes=[
                {"name": "co2-a", "roles": ["mgs", "mds"], "ip": "10.0.0.2"}
            ],
        )
        with patch("ltvm_pkg.vm_commands.cmd_destroy") as destroy:
            vm_cluster.cmd_cluster_destroy(
                argparse.Namespace(names=["co1", "gone", "co2"])
            )
        assert [c.args[0].names for c in destroy.call_args_list] == [
            ["co1-mds", "co1-oss"],
            ["co2-a"],
        ]
        assert "cluster gone not found" in capsys.readouterr().out
        assert not (tmp_sockets / "co1.cluster").exists()
        assert not (tmp_sockets / "co2.cluster").exists()

    def test_a_node_already_gone_does_not_stop_the_rest(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Real single-VM destroy, with neither node's VM on disk."""
        _save_cluster(name="co1")
        with (
            patch("ltvm_pkg.vm_commands.SOCKETS", tmp_sockets),
            patch("ltvm_pkg.vm_commands._destroy_vm_artifacts") as artifacts,
            patch("ltvm_pkg.vm_commands.unregister_ssh_name"),
        ):
            vm_cluster.cmd_cluster_destroy(argparse.Namespace(names=["co1"]))
        assert artifacts.call_count == 2
        out = capsys.readouterr().out
        assert "destroy: co1-mds not found" in out
        assert "destroy: co1-oss not found" in out
        assert not (tmp_sockets / "co1.cluster").exists()


class TestCmdClusterLlmountBehavior:
    """llmount runs from the first client, and only on a running cluster."""

    _IPS = {"co1-mds": "10.0.0.10", "co1-cli": "10.0.0.12", "co1-cli2": "x"}

    @pytest.fixture(autouse=True)
    def _cluster(self, tmp_sockets: Path) -> Any:
        _save_cluster(
            name="co1",
            nodes=[
                {"name": "co1-mds", "roles": ["mgs", "mds", "oss"]},
                {"name": "co1-cli", "roles": ["client"]},
                {"name": "co1-cli2", "roles": ["client"]},
            ],
        )
        with (
            patch.object(
                vm_cluster.VMInfo,
                "load",
                side_effect=lambda n: MagicMock(
                    rail_topology=None, ip=self._IPS[n], os_id="rocky9"
                ),
            ),
            patch.object(vm_cluster, "run") as run,
            patch(
                "ltvm_pkg.deploy.read_kept_blocks",
                return_value={vm_cluster.CLUSTER_BLOCK_BEGIN: "x"},
            ) as kept,
        ):
            run.return_value = MagicMock(returncode=0)
            self.run = run
            self.kept = kept
            with patch.object(vm_cluster, "_check_mounted") as checked:
                self.checked = checked
                yield

    def test_refuses_without_the_cluster_block(self) -> None:
        """Regression: with no cluster block in the first client's
        cfg/local.sh, llmount.sh formatted a single-node filesystem on
        that client and exited 0."""
        self.kept.return_value = {}
        with patch.object(vm_cluster, "_node_state", return_value="up"):
            with pytest.raises(SystemExit) as exc:
                vm_cluster.cmd_cluster_llmount(self._ns())
        assert exc.value.code == EXIT_ERROR
        assert self.kept.call_args.args[0] == "10.0.0.12"
        assert not self.run.called

    def test_cleanup_needs_no_cluster_block(self) -> None:
        self.kept.return_value = {}
        with patch.object(vm_cluster, "_node_state", return_value="up"):
            vm_cluster.cmd_cluster_llmount(self._ns(cleanup=True))
        assert "llmountcleanup.sh" in self._remote()

    def _ns(self, **kw: Any) -> argparse.Namespace:
        base = {"name": "co1", "cleanup": False, "server_only": False}
        base.update(kw)
        return argparse.Namespace(timeout=300, **base)

    def _remote(self) -> str:
        return self.run.call_args.args[0][-1]

    def test_refuses_while_a_node_is_down(self) -> None:
        from ltvm_pkg.vm_state import EXIT_UNREACHABLE

        state = {"co1-mds": "up", "co1-cli": "down", "co1-cli2": "up"}
        with patch.object(vm_cluster, "_node_state", side_effect=state.get):
            with pytest.raises(SystemExit) as exc:
                vm_cluster.cmd_cluster_llmount(self._ns())
        assert exc.value.code == EXIT_UNREACHABLE
        assert not self.run.called

    @pytest.mark.parametrize(
        ("kw", "want", "unwanted"),
        [
            ({}, "bash llmount.sh", "--server-only"),
            ({"server_only": True}, "bash llmount.sh --server-only", "cleanup"),
            (
                {"cleanup": True},
                "llmountcleanup.sh && lustre_rmmod",
                "--server",
            ),
        ],
    )
    def test_runs_the_script_on_the_mgs(
        self, kw: dict[str, bool], want: str, unwanted: str
    ) -> None:
        with patch.object(vm_cluster, "_node_state", return_value="up"):
            vm_cluster.cmd_cluster_llmount(self._ns(**kw))
        assert want in self._remote()
        assert unwanted not in self._remote()

    @pytest.mark.parametrize("cleanup", [False, True])
    def test_runs_on_the_first_client(self, cleanup: bool) -> None:
        """Mount and cleanup must run where local.sh expects: from any other
        node, llmount.sh mounts a client there that cleanup never removes."""
        with patch.object(vm_cluster, "_node_state", return_value="up"):
            vm_cluster.cmd_cluster_llmount(self._ns(cleanup=cleanup))
        assert "root@10.0.0.12" in self.run.call_args.args[0]

    @pytest.mark.parametrize("cleanup", [False, True])
    def test_mount_sweeps_dm_maps_on_every_node(self, cleanup: bool) -> None:
        """A test framework run leaves dm-flakey maps on the disks, and
        llmount.sh cannot format a disk one still holds."""
        with patch.object(vm_cluster, "_node_state", return_value="up"):
            vm_cluster.cmd_cluster_llmount(self._ns(cleanup=cleanup))
        from ltvm_pkg.deploy import premount_sweep_script

        swept = {
            argv[-2]
            for argv in (c.args[0] for c in self.run.call_args_list)
            if argv[-1] == premount_sweep_script()
        }
        if cleanup:
            assert not swept
        else:
            assert swept == {f"root@{ip}" for ip in self._IPS.values()}
            assert "llmount.sh" in self._remote()

    def test_mount_takes_a_mounted_cluster_down_first(self) -> None:
        """Regression: on a mounted cluster the dm sweep ran while the
        targets still held their dm-flakey maps, the maps survived, and
        llmount.sh failed with "Unable to build fs /dev/vdb".
        llmountcleanup.sh runs first, from the node llumount uses, then
        the sweep, then llmount.sh."""
        with patch.object(vm_cluster, "_node_state", return_value="up"):
            vm_cluster.cmd_cluster_llmount(self._ns())
        calls = [c.args[0] for c in self.run.call_args_list]
        first, *sweeps, last = calls
        assert first[-2] == "root@10.0.0.12"
        assert "bash llmountcleanup.sh" in first[-1]
        assert len(sweeps) == len(self._IPS)
        assert all("dmsetup remove_all" in argv[-1] for argv in sweeps)
        assert "bash llmount.sh" in last[-1]

    def test_runs_on_the_mgs_when_there_is_no_client(
        self, tmp_sockets: Path
    ) -> None:
        (tmp_sockets / "co1.cluster").unlink()
        _save_cluster(
            name="co1", nodes=[{"name": "co1-mds", "roles": ["mgs", "mds"]}]
        )
        with patch.object(vm_cluster, "_node_state", return_value="up"):
            vm_cluster.cmd_cluster_llmount(self._ns())
        assert "root@10.0.0.10" in self.run.call_args.args[0]

    def test_script_failure_is_an_error(self) -> None:
        self.run.return_value = MagicMock(returncode=5)
        with patch.object(vm_cluster, "_node_state", return_value="up"):
            with pytest.raises(SystemExit) as exc:
                vm_cluster.cmd_cluster_llmount(self._ns())
        assert exc.value.code == EXIT_ERROR
        assert not self.checked.called

    @pytest.mark.parametrize("server_only", [False, True])
    def test_a_mount_is_checked(self, server_only: bool) -> None:
        with patch.object(vm_cluster, "_node_state", return_value="up"):
            vm_cluster.cmd_cluster_llmount(self._ns(server_only=server_only))
        assert self.checked.call_args.kwargs["server_only"] is server_only

    def test_cleanup_is_not_checked(self) -> None:
        with patch.object(vm_cluster, "_node_state", return_value="up"):
            vm_cluster.cmd_cluster_llmount(self._ns(cleanup=True))
        assert not self.checked.called

    def test_server_only_mounts_from_the_mgs(self) -> None:
        """The clients of an interop cluster may hold another Lustre, or
        none, so the servers mount with their own scripts.  The cluster
        still comes down first from the node llumount uses."""
        with patch.object(vm_cluster, "_node_state", return_value="up"):
            vm_cluster.cmd_cluster_llmount(self._ns(server_only=True))
        calls = [c.args[0] for c in self.run.call_args_list]
        assert calls[0][-2] == "root@10.0.0.12"
        assert "llmountcleanup.sh" in calls[0][-1]
        assert calls[-1][-2] == "root@10.0.0.10"
        assert "setupall server_only" in calls[-1][-1]


class TestServerOnlyMountScript:
    """What `cluster llmount --server-only` runs in the guest, run here
    against stand-in scripts."""

    _FRAMEWORK = (
        "init_test_env() { echo init; }\n"
        "formatall() { echo formatall; }\n"
        'setupall() { echo "setupall $*"; }\n'
    )

    def _run(self, tmp_path: Path, llmount: str) -> str:
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "llmount.sh").write_text(llmount)
        (tests / "test-framework.sh").write_text(self._FRAMEWORK)
        r = subprocess.run(
            ["bash", "-c", vm_cluster._SERVER_ONLY_MOUNT, "llmount.sh"],
            cwd=tests,
            env={"LUSTRE": str(tmp_path), "PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=True,
        )
        return r.stdout

    def test_a_llmount_that_knows_the_option_gets_it(
        self, tmp_path: Path
    ) -> None:
        out = self._run(
            tmp_path,
            '# --server-only) set -- "$@" -S;;\necho "llmount $*"\n',
        )
        assert out == "llmount --server-only\n"

    def test_an_older_llmount_is_done_without_it(self, tmp_path: Path) -> None:
        """v2_15_8's llmount.sh passes --server-only to init_test_env,
        which prints its usage and exits 0 having mounted nothing."""
        out = self._run(tmp_path, "echo 'never run'\n")
        assert "never run" not in out
        assert out.splitlines()[1:] == [
            "init",
            "formatall",
            "setupall server_only",
        ]


class TestCheckMounted:
    """llmount.sh exiting 0 is not proof: v2_15_8's ignored --server-only,
    and master's prints its usage and exits 0 on an option it lacks."""

    @pytest.fixture(autouse=True)
    def _cluster(self) -> Any:
        self.cluster = ClusterInfo(
            name="co1",
            nodes=[
                {"name": "co1-mds", "roles": ["mgs", "mds"], "mdt_disks": 1},
                {"name": "co1-oss", "roles": ["oss"], "ost_disks": 2},
                {"name": "co1-cli", "roles": ["client"]},
            ],
        )
        self.mounts = {
            "co1-mds": "1 0",
            "co1-oss": "2 0",
            "co1-cli": "0 1",
        }
        ips = {"co1-mds": "10.0.0.1", "co1-oss": "10.0.0.2", "co1-cli": "c"}
        names = {v: k for k, v in ips.items()}

        def fake_run(argv: list[str], **kw: Any) -> MagicMock:
            name = names[argv[-2].removeprefix("root@")]
            return MagicMock(returncode=0, stdout=self.mounts[name] + "\n")

        with (
            patch.object(
                vm_cluster.VMInfo,
                "load",
                side_effect=lambda n: MagicMock(ip=ips[n], rail_topology=None),
            ),
            patch.object(vm_cluster, "run", side_effect=fake_run),
        ):
            yield

    def _check(self, server_only: bool = False, ost_count: int = 0) -> None:
        vm_cluster._check_mounted(
            self.cluster,
            self.cluster.local_node(),
            server_only=server_only,
            ost_count=ost_count,
        )

    def test_all_mounted(self) -> None:
        self._check()

    def test_a_missing_target(self, capsys: pytest.CaptureFixture[str]) -> None:
        self.mounts["co1-oss"] = "1 0"
        with pytest.raises(SystemExit):
            self._check()
        assert "2 of 3 targets" in capsys.readouterr().err

    def test_no_client(self, capsys: pytest.CaptureFixture[str]) -> None:
        self.mounts["co1-cli"] = "0 0"
        with pytest.raises(SystemExit):
            self._check()
        assert "no client is mounted on co1-cli" in capsys.readouterr().err

    def test_server_only_needs_no_client(self) -> None:
        self.mounts["co1-cli"] = "0 0"
        self._check(server_only=True)

    def test_expected_targets(self) -> None:
        assert vm_cluster.expected_targets(self.cluster) == 3
        assert vm_cluster.expected_targets(self.cluster, ost_count=5) == 6
        apart = ClusterInfo(
            name="co2",
            nodes=[
                {"name": "co2-mgs", "roles": ["mgs"]},
                {"name": "co2-mds", "roles": ["mds", "oss"], "mdt_disks": 2},
                {"name": "co2-oss", "roles": ["oss"], "ost_disks": 3},
            ],
        )
        assert vm_cluster.expected_targets(apart) == 6


class TestCmdClusterDeployMountLimit:
    """llmount.sh under `cluster deploy --mount` had a fixed 300s, which
    a four-MDT cluster on a loaded host overran mid-format."""

    def _deploy(self, tmp_path: Path, **args: Any) -> int:
        class _TC:
            os_family = "rhel"

        cluster = ClusterInfo(
            name="co4",
            nodes=[
                {"name": "co4-mds", "roles": ["mgs", "mds"], "mdt_disks": 4},
                {"name": "co4-oss", "roles": ["oss"], "ost_disks": 2},
                {"name": "co4-c1", "roles": ["client"]},
            ],
        )
        node = MagicMock(
            rail_topology=None,
            os_id="rocky9",
            arch="x86_64",
            variant="base",
            kernel="/a/kernels/5.14-rhel9.7-5.14.0-611.55.1.el9_7/vmlinuz",
            ip="10.0.0.5",
        )
        with (
            patch.object(ClusterInfo, "load", return_value=cluster),
            patch.object(vm_cluster.VMInfo, "load", return_value=node),
            patch.object(vm_cluster, "_validate_lustre_source"),
            patch("ltvm_pkg.target_config.TargetConfig", return_value=_TC()),
            patch.object(vm_cluster.subprocess, "run") as run,
            patch.object(
                vm_cluster,
                "_deploy_one_node",
                side_effect=lambda name, *a, **k: (name, 0, ""),
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
            patch.object(vm_cluster, "_run_llmount") as llmount,
        ):
            run.return_value = MagicMock(returncode=0)
            vm_cluster.cmd_cluster_deploy(
                argparse.Namespace(
                    name="co4",
                    lustre_source=str(tmp_path),
                    mount=True,
                    server_only=False,
                    **args,
                )
            )
        return int(llmount.call_args.kwargs["timeout"])

    def test_scales_with_the_targets(self, tmp_path: Path) -> None:
        from ltvm_pkg.deploy import llmount_timeout

        assert self._deploy(tmp_path) == llmount_timeout(6)

    def test_zfs(self, tmp_path: Path) -> None:
        from ltvm_pkg.deploy import llmount_timeout

        with patch.object(vm_cluster, "_staged_zfs", return_value=None):
            got = self._deploy(tmp_path, fstype="zfs")
        assert got == llmount_timeout(6, zfs=True)

    def test_flag_wins(self, tmp_path: Path) -> None:
        assert self._deploy(tmp_path, mount_timeout=4000) == 4000


class TestCmdClusterDeployBuildsForTheNodes:
    """The Lustre build targets the kernel and variant the nodes boot."""

    def test_kernel_and_variant_reach_the_build(self, tmp_path: Path) -> None:
        class _TC:
            os_family = "rhel"

            def container_variant(self) -> str:
                return "mofed"

        cluster = ClusterInfo(
            name="co3", nodes=[{"name": "co3-mds", "roles": ["mgs", "mds"]}]
        )
        node = MagicMock(
            rail_topology=None,
            os_id="rocky9",
            arch="x86_64",
            variant="mofed",
            kernel="/a/kernels/5.14-rhel9.3-5.14.0-362.18.1.el9_3/vmlinuz",
            ip="10.0.0.5",
        )
        with (
            patch.object(ClusterInfo, "load", return_value=cluster),
            patch.object(vm_cluster.VMInfo, "load", return_value=node),
            patch.object(vm_cluster, "_validate_lustre_source"),
            patch("ltvm_pkg.target_config.TargetConfig", return_value=_TC()),
            patch.object(vm_cluster.subprocess, "run") as run,
            patch.object(
                vm_cluster,
                "_deploy_one_node",
                side_effect=lambda name, *a, **k: (name, 0, ""),
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
            run.return_value = MagicMock(returncode=0)
            vm_cluster.cmd_cluster_deploy(
                argparse.Namespace(
                    name="co3", lustre_source=str(tmp_path), mount=False
                )
            )
        build = run.call_args_list[0].args[0]
        assert build[build.index("--kernel") + 1] == (
            "5.14-rhel9.3-5.14.0-362.18.1.el9_3"
        )
        assert build[build.index("--variant") + 1] == "mofed"

    def test_mixed_targets_build_once_per_target(self, tmp_path: Path) -> None:
        """4 KiB-page servers with 16 KiB-page clients: each target gets
        its own build, and nodes sharing one share the build."""

        class _TC:
            os_family = "rhel"

        cluster = ClusterInfo(
            name="co7",
            nodes=[
                {"name": "co7-srv", "roles": ["mgs", "mds", "oss"]},
                {"name": "co7-c1", "roles": ["client"]},
                {"name": "co7-c2", "roles": ["client"]},
            ],
        )
        srv = MagicMock(
            rail_topology=None,
            os_id="rocky9",
            arch="aarch64",
            variant="base",
            kernel="/a/kernels/5.14-rhel9.7-5.14.0-611.55.1.el9_7/vmlinuz",
            ip="10.0.0.5",
        )
        cli = MagicMock(
            rail_topology=None,
            os_id="rocky10-16k",
            arch="aarch64",
            variant="base",
            kernel="/a/kernels/6.12-rhel10.2-6.12.0-211.47.1.el10_2/vmlinuz",
            ip="10.0.0.6",
        )
        vms = {"co7-srv": srv, "co7-c1": cli, "co7-c2": cli}
        with (
            patch.object(ClusterInfo, "load", return_value=cluster),
            patch.object(
                vm_cluster.VMInfo, "load", side_effect=vms.__getitem__
            ),
            patch.object(vm_cluster, "_validate_lustre_source"),
            patch("ltvm_pkg.target_config.TargetConfig", return_value=_TC()),
            patch.object(vm_cluster.subprocess, "run") as run,
            patch.object(
                vm_cluster,
                "_deploy_one_node",
                side_effect=lambda name, *a, **k: (name, 0, ""),
            ) as deploy,
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
            run.return_value = MagicMock(returncode=0)
            vm_cluster.cmd_cluster_deploy(
                argparse.Namespace(
                    name="co7", lustre_source=str(tmp_path), mount=False
                )
            )
        builds = [c.args[0] for c in run.call_args_list]
        assert [
            (b[b.index("lustre") + 1], b[b.index("--kernel") + 1])
            for b in builds
        ] == [
            ("rocky9", "5.14-rhel9.7-5.14.0-611.55.1.el9_7"),
            ("rocky10-16k", "6.12-rhel10.2-6.12.0-211.47.1.el10_2"),
        ]
        assert sorted(c.args[0] for c in deploy.call_args_list) == [
            "co7-c1",
            "co7-c2",
            "co7-srv",
        ]


class TestCmdClusterDeployIpFamily:
    """--ip-family reaches the MGS probe and outlives the deploy."""

    def _deploy(
        self, cluster: ClusterInfo, ip_family: str | None, tree: Path
    ) -> MagicMock:
        class _TC:
            os_family = "rhel"

        node = MagicMock(
            rail_topology=None,
            os_id="rocky9",
            arch="x86_64",
            variant="base",
            kernel="/a/kernels/k/vmlinuz",
            ip="10.0.0.5",
        )
        with (
            patch.object(ClusterInfo, "load", return_value=cluster),
            patch.object(vm_cluster.VMInfo, "load", return_value=node),
            patch.object(vm_cluster, "_validate_lustre_source"),
            patch("ltvm_pkg.target_config.TargetConfig", return_value=_TC()),
            patch.object(
                vm_cluster.subprocess,
                "run",
                return_value=MagicMock(returncode=0),
            ),
            patch.object(
                vm_cluster,
                "_deploy_one_node",
                side_effect=lambda name, *a, **k: (name, 0, ""),
            ),
            patch.object(vm_cluster, "probe_mgs_lnet") as probe,
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
                    name="co3",
                    lustre_source=str(tree),
                    mount=False,
                    ip_family=ip_family,
                )
            )
        return probe

    def test_flag_is_recorded(self, tmp_path: Path) -> None:
        cluster = ClusterInfo(
            name="co3", nodes=[{"name": "co3-mds", "roles": ["mgs", "mds"]}]
        )
        probe = self._deploy(cluster, "ipv6", tmp_path)
        assert probe.call_args.args[1] == "ipv6"
        assert cluster.ip_family == "ipv6"

    def test_bare_redeploy_keeps_the_family(self, tmp_path: Path) -> None:
        cluster = ClusterInfo(
            name="co3",
            nodes=[{"name": "co3-mds", "roles": ["mgs", "mds"]}],
            ip_family="ipv6",
        )
        probe = self._deploy(cluster, None, tmp_path)
        assert probe.call_args.args[1] == "ipv6"


class TestCmdClusterDeployOstCount:
    """--ost-count reaches the cluster block and outlives the deploy."""

    def _deploy(
        self, cluster: ClusterInfo, tree: Path, **flags: object
    ) -> tuple[MagicMock, MagicMock]:
        class _TC:
            os_family = "rhel"

        node = MagicMock(
            rail_topology=None,
            os_id="rocky9",
            arch="x86_64",
            variant="base",
            kernel="/a/kernels/k/vmlinuz",
            ip="10.0.0.5",
            disk_size=500 << 20,
        )
        with (
            patch.object(ClusterInfo, "load", return_value=cluster),
            patch.object(ClusterInfo, "save"),
            patch.object(vm_cluster.VMInfo, "load", return_value=node),
            patch.object(vm_cluster, "_validate_lustre_source"),
            patch("ltvm_pkg.target_config.TargetConfig", return_value=_TC()),
            patch.object(
                vm_cluster.subprocess,
                "run",
                return_value=MagicMock(returncode=0),
            ),
            patch.object(
                vm_cluster,
                "_deploy_one_node",
                side_effect=lambda name, *a, **k: (name, 0, ""),
            ),
            patch.object(vm_cluster, "probe_mgs_lnet"),
            patch.object(vm_cluster, "plan_ost_files") as plan,
            patch.object(
                vm_cluster, "generate_local_sh", return_value=""
            ) as gen,
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
                    name="co3", lustre_source=str(tree), mount=False, **flags
                )
            )
        return plan, gen

    def _cluster(self, ost_count: int = 0) -> ClusterInfo:
        return ClusterInfo(
            name="co3",
            nodes=[
                {"name": "co3-mds", "roles": ["mgs", "mds"], "mdt_disks": 1},
                {"name": "co3-oss", "roles": ["oss"], "ost_disks": 2},
            ],
            ost_count=ost_count,
        )

    def test_flag_is_planned_and_recorded(self, tmp_path: Path) -> None:
        cluster = self._cluster()
        plan, gen = self._deploy(cluster, tmp_path, ost_count=8)
        assert plan.call_args.args[1] == 8
        assert plan.call_args.kwargs["prune"] is True
        assert gen.call_args.kwargs["ost_count"] == 8
        assert cluster.ost_count == 8

    def test_bare_redeploy_keeps_the_count(self, tmp_path: Path) -> None:
        plan, gen = self._deploy(self._cluster(8), tmp_path)
        assert plan.call_args.args[1] == 8
        assert gen.call_args.kwargs["ost_count"] == 8

    def test_zero_removes_the_files_and_the_record(
        self, tmp_path: Path
    ) -> None:
        cluster = self._cluster(8)
        plan, gen = self._deploy(cluster, tmp_path, ost_count=0)
        assert plan.call_args.args[1] == 0
        assert gen.call_args.kwargs["ost_count"] == 0
        assert cluster.ost_count == 0

    def test_never_asked_touches_nothing(self, tmp_path: Path) -> None:
        plan, gen = self._deploy(self._cluster(), tmp_path)
        plan.assert_not_called()
        assert gen.call_args.kwargs["ost_files"] is None

    def test_userspace_only_removes_no_files(self, tmp_path: Path) -> None:
        from ltvm_pkg.lustre_build import staging_path

        staging_path(
            str(tmp_path), "rocky9", arch="x86_64", kernel="k", variant="base"
        ).mkdir(parents=True)
        with patch(
            "ltvm_pkg.cli.deploy._refresh_staged_sources", return_value=None
        ):
            plan, _ = self._deploy(
                self._cluster(), tmp_path, ost_count=8, userspace_only=True
            )
        assert plan.call_args.kwargs["prune"] is False

    def test_negative_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            self._deploy(self._cluster(), tmp_path, ost_count=-1)


class TestCmdClusterDeployFstype:
    """The backend outlives the deploy, as --ost-count does."""

    def _deploy(
        self, cluster: ClusterInfo, tree: Path, **flags: object
    ) -> tuple[MagicMock, MagicMock, MagicMock]:
        from ltvm_pkg.lustre_build import staging_path

        class _TC:
            os_family = "rhel"

        staging_path(
            str(tree), "rocky9", arch="x86_64", kernel="k", variant="base"
        ).mkdir(parents=True, exist_ok=True)
        node = MagicMock(
            rail_topology=None,
            os_id="rocky9",
            arch="x86_64",
            variant="base",
            kernel="/a/kernels/k/vmlinuz",
            ip="10.0.0.5",
            disk_size=500 << 20,
        )
        with (
            patch.object(ClusterInfo, "load", return_value=cluster),
            patch.object(ClusterInfo, "save"),
            patch.object(vm_cluster.VMInfo, "load", return_value=node),
            patch.object(vm_cluster, "_validate_lustre_source"),
            patch("ltvm_pkg.target_config.TargetConfig", return_value=_TC()),
            patch(
                "ltvm_pkg.cli.deploy._refresh_staged_sources",
                return_value=None,
            ),
            patch.object(
                vm_cluster.subprocess,
                "run",
                return_value=MagicMock(returncode=0),
            ) as run,
            patch.object(
                vm_cluster,
                "_deploy_one_node",
                side_effect=lambda name, *a, **k: (name, 0, ""),
            ) as one,
            patch.object(vm_cluster, "_staged_zfs", return_value=None),
            patch.object(vm_cluster, "probe_mgs_lnet"),
            patch.object(
                vm_cluster, "generate_local_sh", return_value=""
            ) as gen,
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
                    name="co3", lustre_source=str(tree), mount=False, **flags
                )
            )
        return gen, one, run

    def _cluster(self, fstype: str = "") -> ClusterInfo:
        return ClusterInfo(
            name="co3",
            nodes=[
                {"name": "co3-mds", "roles": ["mgs", "mds"], "mdt_disks": 1},
                {"name": "co3-oss", "roles": ["oss"], "ost_disks": 2},
                {"name": "co3-c1", "roles": ["client"]},
            ],
            fstype=fstype,
        )

    def test_zfs_is_recorded(self, tmp_path: Path) -> None:
        cluster = self._cluster()
        gen, _, _ = self._deploy(cluster, tmp_path, zfs=True)
        assert gen.call_args.kwargs["fstype"] == "zfs"
        assert cluster.fstype == "zfs"

    def test_userspace_only_keeps_zfs(self, tmp_path: Path) -> None:
        """Regression: a userspace-only redeploy of a ZFS cluster wrote
        FSTYPE=ldiskfs into its cluster block."""
        gen, one, run = self._deploy(
            self._cluster("zfs"), tmp_path, userspace_only=True
        )
        assert gen.call_args.kwargs["fstype"] == "zfs"
        run.assert_not_called()
        need = {c.args[0]: c.kwargs["need_zfs"] for c in one.call_args_list}
        assert need == {"co3-mds": True, "co3-oss": True, "co3-c1": False}

    def test_a_full_redeploy_builds_zfs_again(self, tmp_path: Path) -> None:
        _, one, run = self._deploy(self._cluster("zfs"), tmp_path)
        assert "--zfs" in run.call_args.args[0]
        assert not any(c.kwargs["need_zfs"] for c in one.call_args_list)

    def test_fstype_overrides_the_record(self, tmp_path: Path) -> None:
        cluster = self._cluster("zfs")
        gen, _, run = self._deploy(cluster, tmp_path, fstype="ldiskfs")
        assert gen.call_args.kwargs["fstype"] == "ldiskfs"
        assert "--zfs" not in run.call_args.args[0]
        assert cluster.fstype == "ldiskfs"

    def test_unrecorded_asks_the_nodes(self, tmp_path: Path) -> None:
        with patch.object(
            vm_cluster, "_deployed_fstype", return_value="zfs"
        ) as asked:
            gen, _, _ = self._deploy(
                self._cluster(), tmp_path, userspace_only=True
            )
        assert gen.call_args.kwargs["fstype"] == "zfs"
        asked.assert_called_once()

    def test_never_deployed_is_ldiskfs(self, tmp_path: Path) -> None:
        gen, _, _ = self._deploy(self._cluster(), tmp_path)
        assert gen.call_args.kwargs["fstype"] == "ldiskfs"

    def test_zfs_flag_with_userspace_only_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            self._deploy(
                self._cluster(), tmp_path, zfs=True, userspace_only=True
            )


class TestCmdClusterDeployUserspaceOnly:
    """--userspace-only ships the existing staging, without a build."""

    def _deploy(self, tree: Path, **flags: object) -> tuple[MagicMock, ...]:
        class _TC:
            os_family = "rhel"

        cluster = ClusterInfo(
            name="co3", nodes=[{"name": "co3-mds", "roles": ["mgs", "mds"]}]
        )
        node = MagicMock(
            rail_topology=None,
            os_id="rocky9",
            arch="x86_64",
            variant="base",
            kernel="/a/kernels/k/vmlinuz",
            ip="10.0.0.5",
        )
        with (
            patch.object(ClusterInfo, "load", return_value=cluster),
            patch.object(vm_cluster.VMInfo, "load", return_value=node),
            patch.object(vm_cluster, "_validate_lustre_source"),
            patch("ltvm_pkg.target_config.TargetConfig", return_value=_TC()),
            patch.object(
                vm_cluster.subprocess,
                "run",
                return_value=MagicMock(returncode=0),
            ) as run,
            patch.object(
                vm_cluster,
                "_deploy_one_node",
                side_effect=lambda name, *a, **k: (name, 0, ""),
            ) as deploy_one,
            patch(
                "ltvm_pkg.cli.deploy._refresh_staged_sources",
                return_value=None,
            ) as refresh,
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
                    name="co3",
                    lustre_source=str(tree),
                    mount=False,
                    userspace_only=True,
                    **flags,
                )
            )
        return run, deploy_one, refresh

    def test_no_build_and_userspace_reaches_each_node(
        self, tmp_path: Path
    ) -> None:
        from ltvm_pkg.lustre_build import staging_path

        staging = staging_path(
            str(tmp_path), "rocky9", arch="x86_64", kernel="k", variant="base"
        )
        staging.mkdir(parents=True)
        run, deploy_one, refresh = self._deploy(tmp_path)
        run.assert_not_called()
        assert deploy_one.call_args.kwargs["userspace_only"] is True
        assert refresh.call_args.args[0] == staging

    def test_no_staging_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            self._deploy(tmp_path)

    def test_zfs_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            self._deploy(tmp_path, zfs=True)


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

    def test_quoted_single_arg_passed_verbatim(self, tmp_sockets: Path) -> None:
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

    def test_multi_arg_still_quoted_safely(self, tmp_sockets: Path) -> None:
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

    def test_exec_propagates_remote_returncode(self, tmp_sockets: Path) -> None:
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


class TestClusterNodeDiskArgs:
    """`cluster create` must state disk counts explicitly."""

    def test_kernel_and_variant_reach_each_node(self) -> None:
        node = vm_cluster.parse_node_spec("oss:co9-oss:3")
        with patch.object(vm_cluster, "_run_prefixed") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._create_one_node(
                node, vcpus=2, mem=None, kernel="5.14-rhel9.3", variant="mofed"
            )
        argv = run.call_args.args[0]
        assert argv[argv.index("--kernel") + 1] == "5.14-rhel9.3"
        assert argv[argv.index("--variant") + 1] == "mofed"

    def test_defaults_leave_kernel_and_variant_to_create(self) -> None:
        node = vm_cluster.parse_node_spec("oss:co9-oss:3")
        with patch.object(vm_cluster, "_run_prefixed") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._create_one_node(node, vcpus=2, mem=None)
        argv = run.call_args.args[0]
        assert "--kernel" not in argv
        assert "--variant" not in argv

    def test_zero_counts_are_passed_not_omitted(self) -> None:
        """An oss-only node must not inherit `ltvm create`'s defaults.

        Omitting --mdt-disks when the count is 0 lets create's default
        of 1 apply, putting an unrequested disk ahead of the OSTs.
        Every /dev/vd* letter then shifts past what generate_local_sh
        computed, so OSTDEV1 names the spurious disk and the last real
        OST disk is never used -- silently, since the backing files are
        identical scratch images that mount fine.
        """
        from ltvm_pkg import vm_cluster

        node = vm_cluster.parse_node_spec("oss:co9-oss:3")
        with patch.object(vm_cluster, "_run_prefixed") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._create_one_node(node, vcpus=2, mem=None)
        argv = run.call_args.args[0]
        assert "--mdt-disks" in argv
        assert argv[argv.index("--mdt-disks") + 1] == "0"
        assert argv[argv.index("--ost-disks") + 1] == "3"

    def test_root_size_reaches_each_node(self) -> None:
        from ltvm_pkg import vm_cluster

        node = vm_cluster.parse_node_spec("oss:co9-oss:3")
        with patch.object(vm_cluster, "_run_prefixed") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._create_one_node(
                node, vcpus=2, mem=None, root_size="16G"
            )
        argv = run.call_args.args[0]
        assert argv[argv.index("--root-size") + 1] == "16G"

    def test_kernel_args_reach_each_node_as_one_word(self) -> None:
        from ltvm_pkg import vm_cluster

        node = vm_cluster.parse_node_spec("oss:co9-oss:3")
        with patch.object(vm_cluster, "_run_prefixed") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._create_one_node(
                node, vcpus=2, mem=None, kernel_args="slub_debug=FZPU quiet"
            )
        assert "--kernel-args=slub_debug=FZPU quiet" in run.call_args.args[0]

    def test_kernel_args_omitted_when_not_asked_for(self) -> None:
        from ltvm_pkg import vm_cluster

        node = vm_cluster.parse_node_spec("oss:co9-oss:3")
        with patch.object(vm_cluster, "_run_prefixed") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._create_one_node(node, vcpus=2, mem=None)
        assert not any(
            a.startswith("--kernel-args") for a in run.call_args.args[0]
        )

    def test_wait_reaches_each_node_and_extends_its_budget(self) -> None:
        from ltvm_pkg import vm_cluster

        node = vm_cluster.parse_node_spec("oss:co9-oss:3")
        with patch.object(vm_cluster, "_run_prefixed") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._create_one_node(
                node, vcpus=2, mem=None, wait_seconds=600
            )
        argv = run.call_args.args[0]
        assert argv[argv.index("--wait") + 1] == "600"
        # A node that waits for memory must not be killed for waiting.
        assert run.call_args.kwargs["timeout"] == (
            vm_cluster._node_create_timeout() + 600
        )

    def test_wait_omitted_when_not_asked_for(self) -> None:
        from ltvm_pkg import vm_cluster

        node = vm_cluster.parse_node_spec("oss:co9-oss:3")
        with patch.object(vm_cluster, "_run_prefixed") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._create_one_node(node, vcpus=2, mem=None)
        assert "--wait" not in run.call_args.args[0]
        assert (
            run.call_args.kwargs["timeout"] == vm_cluster._node_create_timeout()
        )

    def test_refused_kernel_args_create_no_node(self, tmp_path: Path) -> None:
        from ltvm_pkg import vm_cluster

        args = argparse.Namespace(
            name="co9k",
            nodes=["mgs+mds:co9k-mds:1"],
            vcpus=2,
            mem=None,
            kernel_args="console=tty0",
        )
        with (
            patch.object(vm_cluster, "SOCKETS", tmp_path),
            patch.object(vm_cluster, "_create_one_node") as create,
            pytest.raises(SystemExit),
        ):
            vm_cluster.cmd_cluster_create(args)
        assert not create.called

    def test_root_size_omitted_when_not_asked_for(self) -> None:
        from ltvm_pkg import vm_cluster

        node = vm_cluster.parse_node_spec("oss:co9-oss:3")
        with patch.object(vm_cluster, "_run_prefixed") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            vm_cluster._create_one_node(node, vcpus=2, mem=None)
        assert "--root-size" not in run.call_args.args[0]


class TestClusterCreateRefusesExistingVMs:
    def test_existing_node_vm_aborts_before_creating(
        self, tmp_path: Path
    ) -> None:
        """Never adopt a VM this command did not create.

        `ltvm create` is idempotent, so an existing VM is silently
        adopted -- and the failure path destroys every node in the
        spec, deleting a VM (and its data disks) the cluster never
        created.
        """
        from ltvm_pkg import vm_cluster

        (tmp_path / "co9-oss.info").write_text("NAME=co9-oss\n")
        with (
            patch.object(vm_cluster, "SOCKETS", tmp_path),
            patch.object(vm_cluster.subprocess, "run") as run,
            pytest.raises(SystemExit),
        ):
            vm_cluster.cmd_cluster_create(
                argparse.Namespace(
                    name="co9",
                    nodes=["mgs+mds:co9-mds:1", "oss:co9-oss:3"],
                    vcpus=2,
                    mem=None,
                    os=None,
                    arch=None,
                    disk_size=None,
                    nic=None,
                )
            )
        # Aborted before spawning any `ltvm create`/`ltvm destroy`.
        assert not run.called


# ─────────────────────────────────────────────────────────
# cluster create --dry-run
# ─────────────────────────────────────────────────────────


class TestClusterCreateDryRun:
    """--dry-run validates the whole spec, prints the node plan, and
    creates nothing.

    It stops at the thread pool that runs the per-node `ltvm create`:
    everything before it -- name rules, duplicate node names, VMs that
    already exist, the one-MGS and at-least-one-MDS rules, owner
    resolution -- is a read.
    """

    def test_creates_nothing_and_prints_the_plan(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch.object(vm_cluster, "_create_one_node") as one:
            rc = cmd_cluster(
                _ns(
                    "create",
                    "co7",
                    "rocky9",
                    "mgs+mds:co7-mds:1",
                    "oss:co7-oss:3",
                    "--dry-run",
                )
            )
        assert rc == EXIT_OK
        assert not one.called
        assert not (tmp_sockets / "co7.cluster").exists()
        out = capsys.readouterr().out
        assert "Would create cluster 'co7' with 2 nodes" in out
        assert "co7-mds" in out and "1 MDT" in out
        assert "co7-oss" in out and "3 OST" in out
        assert "Nothing was written" in out

    def test_plan_names_the_kernel_and_variant(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch.object(vm_cluster, "_create_one_node"):
            cmd_cluster(
                _ns(
                    "create",
                    "co7",
                    "mgs+mds:co7-mds:1",
                    "--kernel",
                    "5.14-rhel9.3",
                    "--variant",
                    "mofed",
                    "--dry-run",
                )
            )
        out = capsys.readouterr().out
        assert "kernel:  5.14-rhel9.3" in out
        assert "variant=mofed" in out

    def test_does_not_require_root(self, tmp_sockets: Path) -> None:
        """A dry run only reads, so demanding a password for it would
        just stop people using it.  Note this test does NOT use the
        as_root fixture."""
        with patch("ltvm_pkg.cli._require_root") as req:
            with patch.object(vm_cluster, "_create_one_node"):
                rc = cmd_cluster(
                    _ns("create", "co8", "mgs+mds:co8-mds:1", "--dry-run")
                )
        assert rc == EXIT_OK
        assert not req.called

    def test_short_flag_also_skips_root(self, tmp_sockets: Path) -> None:
        with patch("ltvm_pkg.cli._require_root") as req:
            with patch.object(vm_cluster, "_create_one_node"):
                cmd_cluster(_ns("create", "co8b", "mgs+mds:co8b-mds:1", "-n"))
        assert not req.called

    def test_a_real_create_still_requires_root(self, tmp_sockets: Path) -> None:
        """The bypass must be scoped to --dry-run and nothing else."""
        with patch(
            "ltvm_pkg.cli._require_root", return_value=EXIT_ERROR
        ) as req:
            rc = cmd_cluster(_ns("create", "co8c", "mgs+mds:co8c-mds:1"))
        assert req.called
        assert rc == EXIT_ERROR

    def test_reports_the_shared_per_node_settings(
        self, tmp_sockets: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch.object(vm_cluster, "_create_one_node"):
            cmd_cluster(
                _ns(
                    "create",
                    "co9",
                    "mgs+mds:co9-mds:1",
                    "--vcpus",
                    "8",
                    "--mem",
                    "4096",
                    "--target",
                    "rocky10",
                    "--nic",
                    "softroce",
                    "--dry-run",
                )
            )
        out = capsys.readouterr().out
        assert "8 vcpus, 4096 MB" in out
        assert "rocky10" in out
        assert "softroce" in out

    @pytest.mark.parametrize(
        "specs,expected",
        [
            # No MGS.
            (["oss:coX-oss:2"], "at least one node with mgs"),
            # Two MGS nodes.
            (
                ["mgs+mds:coX-a:1", "mgs:coX-b"],
                "only have one MGS",
            ),
            # MGS but no MDS.
            (["mgs:coX-a"], "at least one node with mds"),
            # Same node named twice.
            (
                ["mgs+mds:coX-a:1", "oss:coX-a:3"],
                "more than once",
            ),
            # Unknown role.
            (["mgs+wat:coX-a:1"], "unknown role"),
        ],
    )
    def test_validation_still_fires(
        self, tmp_sockets: Path, specs: list[str], expected: str
    ) -> None:
        """A dry run that printed a plan for an invalid cluster would be
        worse than no dry run at all."""
        with patch.object(vm_cluster, "_create_one_node") as one:
            rc = cmd_cluster(_ns("create", "coX", *specs, "--dry-run"))
        assert rc == EXIT_ERROR
        assert not one.called

    def test_an_unknown_flag_is_still_rejected(self, tmp_sockets: Path) -> None:
        err = _expect_usage_error(
            "create", "coY", "mgs+mds:coY-a:1", "--dry-run", "--nope"
        )
        assert "--nope" in err


# Taken before conftest's autouse patch refuses every call.
_RUN_PREFIXED = vm_cluster._run_prefixed


class TestNodeCreateProgress:
    """Each node's create output is shown as it comes, not at the end."""

    def test_lines_are_echoed_with_the_node_name(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import sys

        r = _RUN_PREFIXED(
            [sys.executable, "-c", "print('creating'); print('up')"],
            "co9-mds",
            timeout=60,
        )
        assert r.returncode == 0
        assert r.stdout == "creating\nup\n"
        assert capsys.readouterr().out == (
            "  co9-mds: creating\n  co9-mds: up\n"
        )

    def test_child_is_unbuffered(self) -> None:
        import sys

        r = _RUN_PREFIXED(
            [
                sys.executable,
                "-c",
                "import os; print(os.environ.get('PYTHONUNBUFFERED'))",
            ],
            "n",
            timeout=60,
        )
        assert r.stdout.strip() == "1"

    def test_timeout_kills_the_child(self) -> None:
        import subprocess
        import sys

        with pytest.raises(subprocess.TimeoutExpired):
            _RUN_PREFIXED(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                "n",
                timeout=0.5,
            )

    def test_budget_covers_the_childs_own_limits(self) -> None:
        """Regression: SSH_TIMEOUT + 270s killed a node that was up and
        seeding kdump, whose restart alone may take 300s."""
        from ltvm_pkg.vm_commands import KDUMP_SEED_BUDGET
        from ltvm_pkg.vm_net import ssh_wait_cap
        from ltvm_pkg.vm_state import SSH_TIMEOUT

        assert vm_cluster._node_create_timeout() > (
            ssh_wait_cap(SSH_TIMEOUT) + KDUMP_SEED_BUDGET
        )

    def test_an_emulated_node_gets_the_longer_wait(self, monkeypatch) -> None:
        """The child waits TCG_SSH_WAIT_FACTOR times longer for an
        emulated guest; the backstop must not cut it short."""
        from ltvm_pkg.vm_commands import KDUMP_SEED_BUDGET
        from ltvm_pkg.vm_net import TCG_SSH_WAIT_FACTOR, ssh_wait_cap
        from ltvm_pkg.vm_state import SSH_TIMEOUT

        monkeypatch.delenv("LTVM_SSH_TIMEOUT", raising=False)
        monkeypatch.setattr("ltvm_pkg.vm_net.emulated", lambda arch: True)
        assert vm_cluster._node_create_timeout("aarch64") > (
            ssh_wait_cap(SSH_TIMEOUT * TCG_SSH_WAIT_FACTOR) + KDUMP_SEED_BUDGET
        )


class TestClusterMemoryPreflight:
    """A cluster that cannot fit is refused before any node is made."""

    _BUDGET = None

    def _check(
        self, mem: int | None, running: list, wait: int = 0, nodes: int = 5
    ) -> None:
        from ltvm_pkg.qemu_run import MemoryBudget

        specs = [
            vm_cluster.parse_node_spec(f"oss:co9-n{i}:1") for i in range(nodes)
        ]
        budget = MemoryBudget(27107, "cgroup memory.max", 2710, 1.0)
        with (
            patch("ltvm_pkg.qemu_run.guest_memory_budget", return_value=budget),
            patch("ltvm_pkg.qemu_run.running_vm_memory", return_value=running),
        ):
            vm_cluster._require_cluster_memory(
                specs, mem, {s.name: None for s in specs}, "rocky10", wait
            )

    def test_fits(self) -> None:
        self._check(4096, [])

    def test_running_vms_leave_too_little(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression: five 4096 MiB nodes beside a 6144 MiB guest made
        four nodes, failed the fifth and rolled them all back."""
        with pytest.raises(SystemExit):
            self._check(4096, [("co3-zil-test", 6144)])
        err = capsys.readouterr().err
        assert "20480 MiB" in err
        assert "24397 MiB (cgroup memory.max 27107 MiB" in err
        assert "co3-zil-test 6144" in err

    def test_wait_counts_only_the_cluster(self) -> None:
        self._check(4096, [("co3-zil-test", 6144)], wait=600)

    def test_too_big_even_idle(self) -> None:
        with pytest.raises(SystemExit):
            self._check(4096, [], wait=600, nodes=7)

    def test_target_default_memory(self) -> None:
        """rocky10's default_mem is 4096."""
        with pytest.raises(SystemExit):
            self._check(None, [("co3-zil-test", 6144)])


class TestCmdClusterDeployServersOrClients:
    """--server-only and --client-only build for and deploy to part of the
    cluster, so it can run two trees: 2.15 servers under a master client."""

    def _deploy(
        self, tmp_path: Path, **flags: object
    ) -> tuple[list[list[str]], list[str], list[str]]:
        cluster = ClusterInfo(
            name="co3",
            nodes=[
                {"name": "co3-mds", "roles": ["mgs", "mds"], "mdt_disks": 1},
                {"name": "co3-oss", "roles": ["oss"], "ost_disks": 2},
                {"name": "co3-c1", "roles": ["client"]},
            ],
        )

        class _TC:
            os_family = "rhel"

        def vm(name: str) -> MagicMock:
            client = name.endswith("c1")
            return MagicMock(
                rail_topology=None,
                os_id="rocky9" if client else "rocky8",
                arch="x86_64",
                variant="base",
                kernel=f"/a/kernels/{'k9' if client else 'k8'}/vmlinuz",
                ip="10.0.0.5",
                disk_size=500 << 20,
            )

        builds: list[list[str]] = []

        def run(cmd: list[str], *a: object, **k: object) -> MagicMock:
            if cmd[2:4] == ["build", "lustre"]:
                builds.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        deployed: list[str] = []
        written: list[str] = []
        with (
            patch.object(ClusterInfo, "load", return_value=cluster),
            patch.object(ClusterInfo, "save"),
            patch.object(vm_cluster.VMInfo, "load", side_effect=vm),
            patch.object(vm_cluster, "_validate_lustre_source"),
            patch("ltvm_pkg.target_config.TargetConfig", return_value=_TC()),
            patch.object(vm_cluster.subprocess, "run", side_effect=run),
            patch.object(
                vm_cluster,
                "_deploy_one_node",
                side_effect=lambda name, *a, **k: (
                    deployed.append(name) or (name, 0, "")
                ),
            ),
            patch.object(vm_cluster, "probe_mgs_lnet"),
            patch.object(vm_cluster, "generate_local_sh", return_value=""),
            patch.object(
                vm_cluster,
                "_write_cluster_local_sh",
                side_effect=lambda name, *a, **k: (
                    written.append(name) or (name, 0, "")
                ),
            ),
            patch.object(
                vm_cluster, "_distribute_cluster_hosts", return_value=[]
            ),
        ):
            vm_cluster.cmd_cluster_deploy(
                argparse.Namespace(
                    name="co3",
                    lustre_source=str(tmp_path),
                    mount=False,
                    **flags,
                )
            )
        return builds, deployed, written

    def test_server_only_skips_the_client_and_its_target(
        self, tmp_path: Path
    ) -> None:
        builds, deployed, written = self._deploy(tmp_path, server_only=True)
        assert [b[b.index("lustre") + 1] for b in builds] == ["rocky8"]
        assert sorted(deployed) == ["co3-mds", "co3-oss"]
        # Every node still gets the cluster's configuration.
        assert sorted(written) == ["co3-c1", "co3-mds", "co3-oss"]

    def test_client_only_skips_the_servers(self, tmp_path: Path) -> None:
        builds, deployed, written = self._deploy(tmp_path, client_only=True)
        assert [b[b.index("lustre") + 1] for b in builds] == ["rocky9"]
        assert deployed == ["co3-c1"]
        assert sorted(written) == ["co3-c1", "co3-mds", "co3-oss"]

    def test_no_mount_needed_and_a_full_deploy_is_unchanged(
        self, tmp_path: Path
    ) -> None:
        builds, deployed, _ = self._deploy(tmp_path)
        assert sorted(b[b.index("lustre") + 1] for b in builds) == [
            "rocky8",
            "rocky9",
        ]
        assert sorted(deployed) == ["co3-c1", "co3-mds", "co3-oss"]

    def test_both_or_zfs_for_clients_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            self._deploy(tmp_path, server_only=True, client_only=True)
        with pytest.raises(SystemExit):
            self._deploy(tmp_path, client_only=True, zfs=True)

    def test_the_cli_refuses_both(self) -> None:
        rc = cmd_cluster(_ns("deploy", "co1", "--server-only", "--client-only"))
        assert rc != EXIT_OK


def test_child_ltvm_is_this_checkout() -> None:
    argv = vm_cluster._ltvm()
    assert argv[1] == str(
        Path(vm_cluster.__file__).resolve().parent.parent / "ltvm"
    )
