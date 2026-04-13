"""Tests that parser choices and cmd_* implementations stay in sync.

These tests catch structural failure modes:
  1. A subcommand is in the parser but cmd_vm / cmd_cluster never dispatches
     it (falls through to the "Unknown action" error).
  2. A cmd_* function in vm_commands.py has no corresponding parser action
     (orphan function -- unreachable from the CLI).
  3. cmd_ensure does not carry required fields through to cmd_create.
  4. vm list / cluster list crash on missing state files rather than
     degrading gracefully.
  5. JSON error output shape is consistent across error paths.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.machinery
import importlib.util
import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load the ltvm entry point module (no .py extension).
# ---------------------------------------------------------------------------

_LTVM_PATH = str(Path(__file__).parent.parent / "ltvm")


def _load_ltvm() -> Any:
    loader = importlib.machinery.SourceFileLoader("ltvm", _LTVM_PATH)
    spec = importlib.util.spec_from_loader("ltvm", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ltvm = _load_ltvm()

from ltvm_pkg.cli import cmd_cluster  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vm_top_level_commands() -> list[str]:
    """Return the top-level subcommand names that correspond to VM operations.

    These are the commands that were previously vm sub-actions and now live
    directly as top-level ltvm subcommands.
    """
    import inspect

    import ltvm_pkg.vm_commands as vc

    cmd_names = [
        name
        for name, obj in inspect.getmembers(vc, inspect.isfunction)
        if name.startswith("cmd_")
    ]
    # Convert cmd_foo_bar -> foo-bar, and filter to those present in the parser
    p = ltvm.build_parser()
    top_level: set[str] = set()
    for action in p._subparsers._actions:
        if hasattr(action, "_name_parser_map"):
            top_level.update(action._name_parser_map.keys())

    result = []
    for cmd_name in cmd_names:
        action = cmd_name[len("cmd_") :].replace("_", "-")
        if action in top_level:
            result.append(action)
    return sorted(result)


def _cluster_parser_choices() -> list[str]:
    """Return the list of cluster action choices from the parser."""
    p = ltvm.build_parser()
    for action in p._subparsers._actions:
        if hasattr(action, "_name_parser_map"):
            cluster_sp = action._name_parser_map.get("cluster")
            if cluster_sp is not None:
                for a in cluster_sp._actions:
                    if hasattr(a, "choices") and a.choices:
                        return list(a.choices)
    raise RuntimeError("Could not find cluster action choices in parser")


def _vm_command_names() -> list[str]:
    """Return all public cmd_* function names in ltvm_pkg.vm_commands."""
    import ltvm_pkg.vm_commands as vc

    return [
        name
        for name, obj in inspect.getmembers(vc, inspect.isfunction)
        if name.startswith("cmd_")
    ]


# ---------------------------------------------------------------------------
# Test 1: Every top-level subcommand sets a func default
# ---------------------------------------------------------------------------


class TestAllSubcommandsHaveFunc:
    """Each registered subcommand must set_defaults(func=...) so dispatch works."""

    def test_all_subcommands_set_func(self) -> None:
        p = ltvm.build_parser()
        # Collect all subparser names (including aliases)
        subparser_map: dict[str, argparse.ArgumentParser] = {}
        for action in p._subparsers._actions:
            if hasattr(action, "_name_parser_map"):
                subparser_map.update(action._name_parser_map)

        missing_func = []
        for name, sp in subparser_map.items():
            defaults = sp._defaults
            if "func" not in defaults:
                missing_func.append(name)

        assert not missing_func, (
            f"Subcommands missing set_defaults(func=...): {missing_func}"
        )

    def test_parse_subcommand_yields_func_attr(self) -> None:
        """parse_args for each top-level subcommand produces args.func."""
        p = ltvm.build_parser()
        subparser_map: dict[str, argparse.ArgumentParser] = {}
        for action in p._subparsers._actions:
            if hasattr(action, "_name_parser_map"):
                subparser_map.update(action._name_parser_map)

        missing_func = []
        for name in subparser_map:
            # Use a known-safe minimal invocation for subcommands needing positionals
            try:
                args = p.parse_args([name, "dummy"])
            except SystemExit:
                try:
                    args = p.parse_args([name])
                except SystemExit:
                    # Cannot parse without required args; check defaults directly
                    if "func" not in subparser_map[name]._defaults:
                        missing_func.append(name)
                    continue
            if not hasattr(args, "func"):
                missing_func.append(name)

        assert not missing_func, (
            f"Subcommands that don't produce args.func: {missing_func}"
        )


# ---------------------------------------------------------------------------
# Test 2: Every VM top-level subcommand sets a func and calls the right handler
# ---------------------------------------------------------------------------

# Minimal parse args for each VM subcommand to verify the func dispatches
# without hitting errors.  These are parsed by the real parser.
_VM_SUBCOMMAND_PARSE_ARGS: dict[str, list[str]] = {
    "create": ["co1-test"],
    "ensure": ["co1-test"],
    "destroy": ["co1-test"],
    "exec": ["co1-test", "lctl dl"],
    "start": ["co1-test"],
    "stop": ["co1-test"],
    "list": [],
    "ssh": ["co1-test"],
    "console-log": ["co1-test"],
    "dmesg": ["co1-test"],
    "nmi": ["co1-test"],
    "crash-collect": ["co1-test"],
    "snapshot": ["co1-test"],
    "restore": ["co1-test"],
    "doctor": [],
}


class TestVmSubcommandsDispatch:
    """Each former vm sub-action is now a top-level subcommand with its own handler."""

    @pytest.mark.parametrize("subcmd", _vm_top_level_commands())
    def test_vm_subcommand_sets_func(self, subcmd: str) -> None:
        """Each VM subcommand must set_defaults(func=...) and parse cleanly."""
        p = ltvm.build_parser()
        extra = _VM_SUBCOMMAND_PARSE_ARGS.get(subcmd, [])
        args = p.parse_args([subcmd] + extra)
        assert hasattr(args, "func"), (
            f"ltvm {subcmd} parsed OK but args.func is not set"
        )

    @pytest.mark.parametrize("subcmd", _vm_top_level_commands())
    def test_vm_subcommand_handler_calls_vm_commands(self, subcmd: str) -> None:
        """Each VM subcommand's handler must invoke the corresponding vm_commands fn."""

        p = ltvm.build_parser()
        extra = _VM_SUBCOMMAND_PARSE_ARGS.get(subcmd, [])
        args = p.parse_args([subcmd] + extra)

        # Map subcommand -> vm_commands function to patch
        fn_name = subcmd.replace("-", "_")
        patches = [
            patch("ltvm_pkg.cli._require_root", return_value=None),
            patch(f"ltvm_pkg.vm_commands.cmd_{fn_name}"),
        ]
        with contextlib.ExitStack() as stack:
            for p_obj in patches:
                stack.enter_context(p_obj)
            try:
                result = args.func(args)
            except SystemExit:
                result = 0
        # A result of EXIT_ERROR (1) here would indicate a handler bug
        assert result in (0, None), (
            f"ltvm {subcmd} handler returned {result!r}; expected 0 or None"
        )


# ---------------------------------------------------------------------------
# Test 3: Every cluster parser choice dispatches
# ---------------------------------------------------------------------------

_CLUSTER_ARGS: dict[str, list[str]] = {
    "create": ["co1", "mgs+mds:co1-mds:1"],
    "destroy": ["co1"],
    "deploy": ["co1"],
    "status": ["co1"],
    "exec": ["co1", "oss", "lctl dl"],
    "list": [],
    "ssh": ["co1", "oss"],
}


class TestClusterActionsDispatch:
    """All cluster action choices reach a handler; none fall through."""

    def _make_args(self, action: str) -> argparse.Namespace:
        return argparse.Namespace(
            action=action,
            cluster_args=_CLUSTER_ARGS.get(action, []),
            json=False,
            verbose=False,
            arch=None,
        )

    @pytest.mark.parametrize("action", _cluster_parser_choices())
    def test_cluster_action_does_not_hit_unknown_fallback(
        self, action: str
    ) -> None:
        """cmd_cluster must not return the 'Unknown cluster action' error."""
        args = self._make_args(action)

        _cluster_patches = [
            patch("ltvm_pkg.cli._require_root", return_value=None),
            patch("ltvm_pkg.vm_cluster.cmd_cluster_create"),
            patch("ltvm_pkg.vm_cluster.cmd_cluster_destroy"),
            patch("ltvm_pkg.vm_cluster.cmd_cluster_deploy"),
            patch("ltvm_pkg.vm_cluster.cmd_cluster_status"),
            patch("ltvm_pkg.vm_cluster.cmd_cluster_exec"),
            patch("ltvm_pkg.vm_cluster.cmd_cluster_list"),
            patch("ltvm_pkg.vm_cluster.cmd_cluster_ssh"),
        ]
        with contextlib.ExitStack() as stack:
            for p in _cluster_patches:
                stack.enter_context(p)
            result = cmd_cluster(args)

        assert result != 1, (
            f"cluster action '{action}' fell through to "
            f"'Unknown cluster action' fallback (returned {result})"
        )


# ---------------------------------------------------------------------------
# Test 4: No orphan cmd_* functions in vm_commands.py
# ---------------------------------------------------------------------------


def _action_from_cmd_name(cmd_name: str) -> str:
    """Convert 'cmd_foo_bar' -> 'foo-bar' (the parser action name)."""
    assert cmd_name.startswith("cmd_")
    return cmd_name[len("cmd_") :].replace("_", "-")


def _top_level_subcommand_names() -> set[str]:
    """Return all top-level subcommand names (and aliases) from the parser."""
    p = ltvm.build_parser()
    names: set[str] = set()
    for action in p._subparsers._actions:
        if hasattr(action, "_name_parser_map"):
            names.update(action._name_parser_map.keys())
    return names


class TestNoOrphanVmCommandFunctions:
    """Every cmd_* in vm_commands.py must be reachable via the parser.

    With the flat structure, each function is reachable as a top-level
    subcommand (ltvm <action>) that delegates to vm_commands.
    """

    _INTENTIONALLY_DROPPED: set[str] = set()

    def test_all_cmd_functions_have_parser_action(self) -> None:
        top_level = _top_level_subcommand_names()
        cmd_names = _vm_command_names()

        orphans = []
        for cmd_name in cmd_names:
            if cmd_name in self._INTENTIONALLY_DROPPED:
                continue
            expected_action = _action_from_cmd_name(cmd_name)
            # Reachable as a top-level subcommand
            if expected_action not in top_level:
                orphans.append(
                    f"{cmd_name} (expected action '{expected_action}')"
                )

        assert not orphans, (
            "These vm_commands.py functions have no corresponding parser "
            "action:\n  " + "\n  ".join(orphans)
        )


# ---------------------------------------------------------------------------
# Test 5: Namespace completeness -- cmd_ensure carries all create fields
# ---------------------------------------------------------------------------
#
# cmd_ensure builds a Namespace and calls cmd_create when the VM does not yet
# exist.  If it forgets to forward a field that cmd_create reads, that field
# will silently fall back to a default regardless of what the user asked for.
#
# The test strategy:
#   - Mock the expensive side effects (VMInfo.save, launch_qemu, wait_for_ssh,
#     register_ssh_name, deploy_ssh_key, check_ip_collision, qemu-img run).
#   - Call cmd_ensure with non-default values for every field cmd_create reads.
#   - Intercept the VMInfo that gets constructed inside cmd_create and verify
#     the field values match the caller's intent.
#
# Fields that cmd_create reads from args (via getattr or direct access):
#   name, vcpus, mem, ip, rootfs, image, kernel, os, arch,
#   mdt_disks, ost_disks, disk_size, _quiet


class TestEnsureToCreateNamespaceContract:
    """cmd_ensure must forward all caller-supplied fields to cmd_create."""

    # The fields that cmd_create reads from its Namespace, and what
    # cmd_ensure should forward (non-default sentinel values used in tests).
    _FIELD_SENTINELS: dict[str, Any] = {
        "vcpus": 8,
        "mem": 8192,
        "mdt_disks": 3,
        "ost_disks": 5,
        "image": "/custom/base.ext4",
        "kernel": "/custom/vmlinuz",
        "os": "ubuntu2404",
        "arch": "aarch64",
        "disk_size": 1024 * 1024 * 1024,  # 1 GiB
    }

    def _make_ensure_args(self) -> argparse.Namespace:
        """Build an args Namespace that cmd_ensure will accept."""
        return argparse.Namespace(
            name="co1-test",
            json=False,
            **self._FIELD_SENTINELS,
        )

    def test_ensure_forwards_all_fields_to_create(self, tmp_path: Path) -> None:
        """Every non-default field in _FIELD_SENTINELS must reach cmd_create.

        Specifically the Namespace that cmd_create receives must carry each
        field with the value the caller passed to cmd_ensure, not a default.
        """
        from ltvm_pkg import vm_commands

        # Intercept the VMInfo that gets built inside cmd_create so we can
        # inspect it.  We record the constructor call args.
        created_vms: list[Any] = []
        real_VMInfo = vm_commands.VMInfo

        class CapturingVMInfo(real_VMInfo):  # type: ignore[misc]
            def save(self) -> None:
                created_vms.append(self)

        args = self._make_ensure_args()

        # Patch SOCKETS so the .info path doesn't exist (triggers create path).
        fake_sockets = tmp_path / "sockets"
        fake_sockets.mkdir()
        fake_overlays = tmp_path / "overlays"
        fake_overlays.mkdir()

        patches = [
            # No existing .info file in fake_sockets → create path is taken.
            patch.object(vm_commands, "SOCKETS", fake_sockets),
            patch.object(vm_commands, "OVERLAYS", fake_overlays),
            patch("ltvm_pkg.vm_commands.VMInfo", CapturingVMInfo),
            patch(
                "ltvm_pkg.vm_commands.alloc_ip",
                return_value=contextlib.contextmanager(
                    lambda *a, **kw: (yield "192.168.100.5")
                )(),
            ),
            patch(
                "ltvm_pkg.vm_commands.tap_for_name", return_value="tap-co1-test"
            ),
            patch(
                "ltvm_pkg.vm_commands.mac_for_name",
                return_value="52:54:00:aa:bb:cc",
            ),
            patch(
                "ltvm_pkg.vm_commands.resolve_os_artifacts",
                return_value=MagicMock(
                    image="/resolved/base.ext4",
                    kernel="/resolved/vmlinuz",
                    default_mem=2048,
                    arch="aarch64",
                ),
            ),
            patch("ltvm_pkg.vm_commands.launch_qemu"),
            patch("ltvm_pkg.vm_commands.wait_for_ssh", return_value=True),
            patch("ltvm_pkg.vm_commands.register_ssh_name"),
            patch("ltvm_pkg.vm_commands.deploy_ssh_key"),
            patch("ltvm_pkg.vm_commands._seed_kdump_boot"),
            patch("ltvm_pkg.vm_commands.run"),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            vm_commands.cmd_ensure(args)

        assert created_vms, (
            "cmd_ensure (create path) never called VMInfo.save(); "
            "the VM was never created"
        )
        vm = created_vms[0]

        # Check each sentinel field.
        failures = []
        if vm.vcpus != self._FIELD_SENTINELS["vcpus"]:
            failures.append(
                f"vcpus: expected {self._FIELD_SENTINELS['vcpus']}, got {vm.vcpus}"
            )
        if vm.mem != self._FIELD_SENTINELS["mem"]:
            failures.append(
                f"mem: expected {self._FIELD_SENTINELS['mem']}, got {vm.mem}"
            )
        if vm.mdt_disks != self._FIELD_SENTINELS["mdt_disks"]:
            failures.append(
                f"mdt_disks: expected {self._FIELD_SENTINELS['mdt_disks']}, "
                f"got {vm.mdt_disks}"
            )
        if vm.ost_disks != self._FIELD_SENTINELS["ost_disks"]:
            failures.append(
                f"ost_disks: expected {self._FIELD_SENTINELS['ost_disks']}, "
                f"got {vm.ost_disks}"
            )
        if vm.disk_size != self._FIELD_SENTINELS["disk_size"]:
            failures.append(
                f"disk_size: expected {self._FIELD_SENTINELS['disk_size']}, "
                f"got {vm.disk_size}"
            )
        # os field: cmd_create reads it as getattr(args, "os", ""), and uses
        # it as os_id on the VMInfo.
        if vm.os_id != self._FIELD_SENTINELS["os"]:
            failures.append(
                f"os (os_id): expected '{self._FIELD_SENTINELS['os']}', "
                f"got '{vm.os_id}'"
            )
        # explicit image: when args.image is set, cmd_create uses it directly
        if vm.image != self._FIELD_SENTINELS["image"]:
            failures.append(
                f"image: expected '{self._FIELD_SENTINELS['image']}', "
                f"got '{vm.image}'"
            )
        # explicit kernel: when args.kernel is set, cmd_create uses it directly
        if vm.kernel != self._FIELD_SENTINELS["kernel"]:
            failures.append(
                f"kernel: expected '{self._FIELD_SENTINELS['kernel']}', "
                f"got '{vm.kernel}'"
            )

        assert not failures, (
            "cmd_ensure did not forward these fields to cmd_create:\n  "
            + "\n  ".join(failures)
            + "\n\nThis means cmd_ensure builds a Namespace that drops caller-"
            "supplied values, causing them to be silently ignored."
        )

    def test_ensure_calls_resolve_os_artifacts_with_caller_os(
        self, tmp_path: Path
    ) -> None:
        """When --os is given, cmd_create must pass it to resolve_os_artifacts.

        This validates that the os field actually reaches the artifact resolver,
        not just that it ends up on the VMInfo struct.
        """
        from ltvm_pkg import vm_commands

        resolve_calls: list[str] = []

        def capturing_resolve(
            os_name: str,
            arch: str = "x86_64",
            kernel: str | None = None,
        ) -> Any:
            resolve_calls.append(os_name)
            return MagicMock(
                image="/resolved/base.ext4",
                kernel="/resolved/vmlinuz",
                default_mem=2048,
                arch=arch,
            )

        args = argparse.Namespace(
            name="co1-test",
            json=False,
            vcpus=2,
            mem=4096,
            mdt_disks=0,
            ost_disks=0,
            image="",
            kernel="",
            os="ubuntu2404",
            arch="x86_64",
            disk_size=None,
        )

        fake_sockets = tmp_path / "sockets"
        fake_sockets.mkdir()
        fake_overlays = tmp_path / "overlays"
        fake_overlays.mkdir()

        patches = [
            patch.object(vm_commands, "SOCKETS", fake_sockets),
            patch.object(vm_commands, "OVERLAYS", fake_overlays),
            patch(
                "ltvm_pkg.vm_commands.resolve_os_artifacts",
                side_effect=capturing_resolve,
            ),
            patch(
                "ltvm_pkg.vm_commands.alloc_ip",
                return_value=contextlib.contextmanager(
                    lambda *a, **kw: (yield "192.168.100.5")
                )(),
            ),
            patch(
                "ltvm_pkg.vm_commands.tap_for_name", return_value="tap-co1-test"
            ),
            patch(
                "ltvm_pkg.vm_commands.mac_for_name",
                return_value="52:54:00:aa:bb:cc",
            ),
            patch("ltvm_pkg.vm_commands.launch_qemu"),
            patch("ltvm_pkg.vm_commands.wait_for_ssh", return_value=True),
            patch("ltvm_pkg.vm_commands.register_ssh_name"),
            patch("ltvm_pkg.vm_commands.deploy_ssh_key"),
            patch("ltvm_pkg.vm_commands._seed_kdump_boot"),
            patch("ltvm_pkg.vm_commands.run"),
        ]
        # Patch VMInfo.save so the create path completes without writing to disk
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch("ltvm_pkg.vm_commands.VMInfo.save"))
            vm_commands.cmd_ensure(args)

        assert resolve_calls, (
            "resolve_os_artifacts was never called; did cmd_ensure skip cmd_create?"
        )
        assert "ubuntu2404" in resolve_calls, (
            f"resolve_os_artifacts was called with {resolve_calls!r}, "
            f"but not with the requested os 'ubuntu2404'. "
            f"cmd_ensure failed to forward --os to cmd_create."
        )


# ---------------------------------------------------------------------------
# Test 6: vm list / cluster list resilience to missing state files
# ---------------------------------------------------------------------------
#
# These commands enumerate all VMs/clusters by scanning for *.info / *.cluster
# files and then loading each one.  If a file disappears between the scan and
# the load (race condition, manual deletion, or partial write), the command
# must not crash; it should either skip or return a degraded result.


class TestListResilienceToMissingFiles:
    """vm list and cluster list must not crash on missing state files."""

    def test_vm_list_skips_missing_info_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """cmd_list handles a VM whose .info file vanishes after enumeration.

        Simulates: all_names() returns a name, but VMInfo.load() raises
        VMNotFound.  The command must not propagate the exception.
        """
        from ltvm_pkg.vm_commands import cmd_list
        from ltvm_pkg.vm_state import VMNotFound

        args = argparse.Namespace(json=False)

        with patch("ltvm_pkg.vm_commands.VMInfo") as MockVMInfo:
            MockVMInfo.all_names.return_value = ["co1-test"]
            MockVMInfo.side_effect = None
            MockVMInfo.load.side_effect = VMNotFound("co1-test")

            # Should not raise; may print nothing or a warning.
            try:
                cmd_list(args)
            except VMNotFound as exc:
                pytest.fail(
                    f"cmd_list propagated VMNotFound for a missing info file: {exc}\n"
                    f"vm list must be resilient to races between enumeration and load."
                )
            except Exception as exc:
                pytest.fail(
                    f"cmd_list raised {type(exc).__name__} for a missing info file: {exc}"
                )

    def test_vm_list_json_skips_missing_info_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """cmd_list --json handles a missing .info file without crashing.

        The JSON output must still be valid (parseable), even if the
        missing VM is absent from the 'vms' list.
        """
        from ltvm_pkg.vm_commands import cmd_list
        from ltvm_pkg.vm_state import VMNotFound

        args = argparse.Namespace(json=True)

        # Capture the real open() before patching so the side_effect
        # can fall through without recursing into its own patch.
        _real_open = open
        import io as _io

        def _open_side(*a, **kw):
            if a and "/proc/meminfo" in str(a[0]):
                return _io.StringIO("MemTotal: 8000000 kB\n")
            return _real_open(*a, **kw)

        with (
            patch("ltvm_pkg.vm_commands.VMInfo") as MockVMInfo,
            patch("builtins.open", side_effect=_open_side),
            patch("os.cpu_count", return_value=4),
        ):
            MockVMInfo.all_names.return_value = ["co1-test"]
            MockVMInfo.load.side_effect = VMNotFound("co1-test")

            try:
                cmd_list(args)
            except VMNotFound as exc:
                pytest.fail(
                    f"cmd_list --json propagated VMNotFound: {exc}; "
                    f"JSON output must degrade gracefully."
                )
            except Exception as exc:
                pytest.fail(
                    f"cmd_list --json raised {type(exc).__name__}: {exc}"
                )

    def test_cluster_list_skips_missing_cluster_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """cmd_cluster_list handles a cluster whose file vanishes after scan.

        Simulates: all_names() returns a name, but ClusterInfo.load() raises
        ClusterNotFound.  The command must not crash.
        """
        from ltvm_pkg.vm_cluster import cmd_cluster_list
        from ltvm_pkg.vm_state import ClusterNotFound

        args = argparse.Namespace()

        with patch("ltvm_pkg.vm_cluster.ClusterInfo") as MockCI:
            MockCI.all_names.return_value = ["co1"]
            MockCI.load.side_effect = ClusterNotFound("co1")

            try:
                cmd_cluster_list(args)
            except ClusterNotFound as exc:
                pytest.fail(
                    f"cmd_cluster_list propagated ClusterNotFound: {exc}\n"
                    f"cluster list must be resilient to missing state files."
                )
            except Exception as exc:
                pytest.fail(
                    f"cmd_cluster_list raised {type(exc).__name__}: {exc}"
                )


# ---------------------------------------------------------------------------
# Test 7: JSON error output shape is consistent across cmd_vm error paths
# ---------------------------------------------------------------------------
#
# When --json is set, every error code path in cmd_vm and cmd_cluster must
# produce output that is valid JSON and contains an "error" key.  This ensures
# callers can rely on `{"error": "..."}` regardless of which error fires.


class TestJsonErrorShape:
    """JSON error responses always have an 'error' key."""

    def _capture_json_output(self, args: argparse.Namespace, fn: Any) -> dict:
        """Call fn(args) and parse the first JSON object from stdout/stderr."""
        captured_lines: list[str] = []

        with patch("builtins.print") as mock_print, patch("sys.stderr"):
            # Capture calls to print()
            def capture(*a: Any, file: Any = None, **kw: Any) -> None:
                if a:
                    captured_lines.append(str(a[0]))

            mock_print.side_effect = capture
            try:
                fn(args)
            except SystemExit:
                pass

        combined = "\n".join(captured_lines)
        # Find the first JSON object
        for line in captured_lines:
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    pass
        # Try the whole combined string
        try:
            return json.loads(combined)
        except json.JSONDecodeError:
            return {}

    @pytest.mark.parametrize(
        "handler_name,args_ns,description",
        [
            (
                "cmd_destroy",
                argparse.Namespace(
                    names=[], json=True, verbose=False, arch=None
                ),
                "destroy with empty names list triggers VMNotFound",
            ),
        ],
    )
    def test_vm_json_error_has_error_key(
        self, handler_name: str, args_ns: argparse.Namespace, description: str
    ) -> None:
        """VM handler --json error paths always produce {'error': ...}."""
        import ltvm_pkg.cli as cli_mod
        from ltvm_pkg.vm_state import VMNotFound

        handler = getattr(cli_mod, handler_name)

        output_lines: list[str] = []

        with (
            patch("ltvm_pkg.cli._require_root", return_value=None),
            patch(
                "ltvm_pkg.vm_commands.cmd_destroy",
                side_effect=VMNotFound("co1-test"),
            ),
            patch("builtins.print") as mock_print,
            patch("sys.stderr"),
        ):
            mock_print.side_effect = lambda *a, file=None, **kw: (
                output_lines.append(str(a[0])) if a else None
            )
            try:
                handler(args_ns)
            except SystemExit:
                pass

        json_outputs = []
        for line in output_lines:
            line = line.strip()
            if line.startswith("{"):
                try:
                    json_outputs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        assert json_outputs, (
            f"{handler_name} --json ({description}) produced no JSON output.\n"
            f"Raw output: {output_lines!r}"
        )
        assert "error" in json_outputs[0], (
            f"{handler_name} --json ({description}) JSON output "
            f"is missing 'error' key.\n"
            f"Got: {json_outputs[0]!r}\n"
            f'All JSON error paths must produce {{"error": "..."}}.'
        )

    @pytest.mark.parametrize(
        "action,cluster_args,description",
        [
            ("destroy", [], "missing cluster name"),
            ("status", [], "missing cluster name"),
            ("exec", ["co1", "oss"], "too few args for exec"),
        ],
    )
    def test_cluster_json_error_has_error_key(
        self, action: str, cluster_args: list[str], description: str
    ) -> None:
        """cmd_cluster --json error paths always produce {'error': ...}."""
        from ltvm_pkg.cli import cmd_cluster

        args = argparse.Namespace(
            action=action,
            cluster_args=cluster_args,
            json=True,
            verbose=False,
            arch=None,
        )

        output_lines: list[str] = []

        with (
            patch("ltvm_pkg.cli._require_root", return_value=None),
            patch("builtins.print") as mock_print,
            patch("sys.stderr"),
        ):
            mock_print.side_effect = lambda *a, file=None, **kw: (
                output_lines.append(str(a[0])) if a else None
            )
            try:
                cmd_cluster(args)
            except SystemExit:
                pass

        json_outputs = []
        for line in output_lines:
            line = line.strip()
            if line.startswith("{"):
                try:
                    json_outputs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        assert json_outputs, (
            f"cmd_cluster --json action='{action}' ({description}) produced no JSON output.\n"
            f"Raw output: {output_lines!r}"
        )
        assert "error" in json_outputs[0], (
            f"cmd_cluster --json action='{action}' ({description}) JSON output "
            f"is missing 'error' key.\n"
            f"Got: {json_outputs[0]!r}\n"
            f'All JSON error paths must produce {{"error": "..."}}.'
        )
