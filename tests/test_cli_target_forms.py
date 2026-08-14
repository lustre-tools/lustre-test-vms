"""Tests for the dual-form target argument (positional + --target).

Every target-taking subcommand must accept BOTH a positional target
AND a --target flag.  Conflicting specs (different values supplied
in both) must fail cleanly.  Neither form is deprecated.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

_LTVM_PATH = str(Path(__file__).parent.parent / "ltvm")


def _load_ltvm() -> Any:
    loader = importlib.machinery.SourceFileLoader("ltvm", _LTVM_PATH)
    spec = importlib.util.spec_from_loader("ltvm", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ltvm = _load_ltvm()


def _parse(argv: list[str]) -> Any:
    """Parse argv and run the target reconciler.

    Returns the final Namespace that dispatch would see.
    """
    p = ltvm.build_parser()
    args = p.parse_args(argv)
    rc = ltvm._reconcile_target_args(args)
    if rc is not None:
        raise SystemExit(rc)
    return args


def _parse_expect_conflict(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Assert that the reconciler rejects argv with a conflict error."""
    with pytest.raises(SystemExit) as exc_info:
        _parse(argv)
    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "conflict" in err.lower(), f"Expected 'conflict' in stderr: {err!r}"


# ---------------------------------------------------------------------------
# build *: currently positional; must accept --target too
# ---------------------------------------------------------------------------


class TestBuildTargetForms:
    def test_build_all_positional(self) -> None:
        args = _parse(["build", "all", "rocky9"])
        assert args.target == "rocky9"

    def test_build_all_flag(self) -> None:
        args = _parse(["build", "all", "--target", "rocky9"])
        assert args.target == "rocky9"

    def test_build_all_both_same(self) -> None:
        args = _parse(["build", "all", "rocky9", "--target", "rocky9"])
        assert args.target == "rocky9"

    def test_build_all_both_conflict(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _parse_expect_conflict(
            ["build", "all", "rocky9", "--target", "rocky10"], capsys
        )

    def test_build_container_flag(self) -> None:
        args = _parse(["build", "container", "--target", "rocky9"])
        assert args.target == "rocky9"

    def test_build_kernel_flag(self) -> None:
        args = _parse(["build", "kernel", "--target", "rocky9"])
        assert args.target == "rocky9"

    def test_build_image_flag(self) -> None:
        args = _parse(["build", "image", "--target", "rocky9"])
        assert args.target == "rocky9"

    def test_build_shell_flag(self) -> None:
        args = _parse(["build", "shell", "--target", "rocky9"])
        assert args.target == "rocky9"

    def test_build_mofed_kmods_flag(self) -> None:
        args = _parse(["build", "mofed-kmods", "--target", "rocky9"])
        assert args.target == "rocky9"

    def test_build_lustre_flag(self, tmp_path: Path) -> None:
        args = _parse(
            ["build", "lustre", "--target", "rocky9",
             "--lustre-tree", str(tmp_path)]
        )
        assert args.target == "rocky9"

    def test_build_lustre_positional(self, tmp_path: Path) -> None:
        args = _parse(
            ["build", "lustre", "rocky9", "--lustre-tree", str(tmp_path)]
        )
        assert args.target == "rocky9"

    def test_build_lustre_no_second_positional(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The old `build lustre <target> <lustre-tree>` form is gone;
        # --lustre-tree is the only way to pass the tree.
        with pytest.raises(SystemExit):
            _parse(["build", "lustre", "rocky9", str(tmp_path)])


# ---------------------------------------------------------------------------
# target *: currently positional; must accept --target too
# ---------------------------------------------------------------------------


class TestTargetSubcommandForms:
    def test_target_show_positional(self) -> None:
        args = _parse(["target", "show", "rocky9"])
        assert args.target == "rocky9"

    def test_target_show_flag(self) -> None:
        args = _parse(["target", "show", "--target", "rocky9"])
        assert args.target == "rocky9"

    def test_target_clean_flag(self) -> None:
        args = _parse(["target", "clean", "--target", "rocky9"])
        assert args.target == "rocky9"

    def test_target_validate_flag(self) -> None:
        args = _parse(["target", "validate", "--target", "rocky9"])
        assert args.target == "rocky9"

    def test_target_export_flag(self) -> None:
        args = _parse(["target", "export", "--target", "rocky9"])
        assert args.target == "rocky9"

    def test_target_publish_positional(self) -> None:
        args = _parse(["target", "publish", "rocky9"])
        assert args.target == "rocky9"

    def test_target_publish_flag(self) -> None:
        args = _parse(["target", "publish", "--target", "rocky9"])
        assert args.target == "rocky9"

    def test_target_publish_no_upload_flag(self) -> None:
        args = _parse(["target", "publish", "rocky9", "--no-upload"])
        assert args.target == "rocky9"
        assert args.no_upload is True

    def test_target_publish_conflict(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _parse_expect_conflict(
            ["target", "publish", "rocky9", "--target", "rocky10"], capsys
        )


# ---------------------------------------------------------------------------
# create: currently --target flag; must accept positional too
# ---------------------------------------------------------------------------


class TestCreateTargetForms:
    def test_create_flag(self) -> None:
        args = _parse(["create", "vm1", "--target", "rocky9"])
        # create's downstream reads args.target (not args.os anymore).
        assert args.target == "rocky9"
        assert not hasattr(args, "os")

    def test_create_positional(self) -> None:
        args = _parse(["create", "vm1", "rocky9"])
        assert args.target == "rocky9"
        assert not hasattr(args, "os")

    def test_create_both_same(self) -> None:
        args = _parse(["create", "vm1", "rocky9", "--target", "rocky9"])
        assert args.target == "rocky9"

    def test_create_both_conflict(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _parse_expect_conflict(
            ["create", "vm1", "rocky9", "--target", "rocky10"], capsys
        )

    def test_create_no_target(self) -> None:
        # Omitting the target leaves args.target=None; vm_commands falls
        # back to DEFAULT_TARGET downstream.
        args = _parse(["create", "vm1"])
        assert args.target is None
        assert not hasattr(args, "os")


# ---------------------------------------------------------------------------
# deploy-lustre: currently --target flag; must accept positional too
# ---------------------------------------------------------------------------


class TestDeployLustreTargetForms:
    def test_deploy_flag(self) -> None:
        args = _parse(["deploy-lustre", "vm1", "--target", "rocky9"])
        assert args.target == "rocky9"
        assert args.vm == "vm1"

    def test_deploy_positional(self) -> None:
        args = _parse(["deploy-lustre", "vm1", "rocky9"])
        assert args.target == "rocky9"
        assert args.vm == "vm1"

    def test_deploy_no_target(self) -> None:
        # Omitting is allowed: cmd_deploy auto-detects from VM.
        args = _parse(["deploy-lustre", "vm1"])
        assert args.target is None

    def test_deploy_conflict(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _parse_expect_conflict(
            ["deploy-lustre", "vm1", "rocky9", "--target", "rocky10"], capsys
        )


# ---------------------------------------------------------------------------
# Sentinel dest is stripped before dispatch
# ---------------------------------------------------------------------------


class TestSentinelCleanup:
    def test_target_flag_sentinel_removed(self) -> None:
        args = _parse(["build", "all", "--target", "rocky9"])
        assert not hasattr(args, ltvm._TARGET_FLAG_DEST)

    def test_positional_form_sentinel_removed(self) -> None:
        args = _parse(["build", "all", "rocky9"])
        assert not hasattr(args, ltvm._TARGET_FLAG_DEST)


# ---------------------------------------------------------------------------
# Non-target subcommands: reconciler is a no-op
# ---------------------------------------------------------------------------


class TestNonTargetSubcommands:
    def test_list_untouched(self) -> None:
        # `list` doesn't take a target; reconciler must not invent one.
        args = _parse(["list"])
        assert not hasattr(args, "target") or args.target is None

    def test_destroy_untouched(self) -> None:
        args = _parse(["destroy", "vm1"])
        # destroy has no target attr at all
        assert getattr(args, "target", None) is None


# ---------------------------------------------------------------------------
# cluster create: --target and positional both accepted
# ---------------------------------------------------------------------------


class TestClusterCreateTargetForms:
    """cluster create uses REMAINDER parsing in cmd_cluster; verify
    the positional form works alongside the legacy --target flag."""

    def _run(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> tuple[int, Any]:
        """Invoke cmd_cluster with a captured _qc_create call.

        Returns (rc, captured-namespace-or-None).
        """
        captured: dict[str, Any] = {}

        def fake_qc_create(ns: Any) -> None:
            captured["ns"] = ns

        # cmd_cluster requires root for create; patch that plus the
        # _qc_create import target inside cmd_cluster's function body.
        import ltvm_pkg.cli as cli_mod
        import ltvm_pkg.vm_cluster as vmc

        with (
            patch.object(cli_mod, "_require_root", return_value=None),
            patch.object(vmc, "cmd_cluster_create", new=fake_qc_create),
        ):
            p = ltvm.build_parser()
            args = p.parse_args(argv)
            rc = ltvm._reconcile_target_args(args)
            if rc is not None:
                return rc, None
            rc = args.func(args)
        return rc, captured.get("ns")

    def test_cluster_create_flag(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, ns = self._run(
            ["cluster", "create", "co9", "--target", "rocky9",
             "mgs+mds:co9-mds:1"],
            capsys,
        )
        assert rc == 0, capsys.readouterr()
        assert ns is not None
        assert ns.os == "rocky9"
        assert ns.name == "co9"

    def test_cluster_create_positional(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, ns = self._run(
            ["cluster", "create", "co9", "rocky9", "mgs+mds:co9-mds:1"],
            capsys,
        )
        assert rc == 0, capsys.readouterr()
        assert ns is not None
        assert ns.os == "rocky9"
        assert ns.name == "co9"

    def test_cluster_create_both_conflict(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, ns = self._run(
            ["cluster", "create", "co9", "rocky9", "--target", "rocky10",
             "mgs+mds:co9-mds:1"],
            capsys,
        )
        assert rc != 0
        err = capsys.readouterr().err
        assert "conflict" in err.lower()


class TestLlumountAlias:
    """`ltvm llumount <vm>` is the unmount spelling (matching Lustre's
    llmount.sh / llumount.sh pair); `llmount --cleanup` remains
    supported for existing scripts."""

    def test_llumount_parses_as_cleanup(self) -> None:
        p = ltvm.build_parser()
        args = p.parse_args(["llumount", "co1-single"])
        assert args.vm == "co1-single"
        assert args.cleanup is True
        assert args.func is ltvm.cmd_llmount

    def test_llmount_cleanup_still_works(self) -> None:
        p = ltvm.build_parser()
        args = p.parse_args(["llmount", "co1-single", "--cleanup"])
        assert args.vm == "co1-single"
        assert args.cleanup is True
        assert args.func is ltvm.cmd_llmount

    def test_llmount_default_is_mount(self) -> None:
        p = ltvm.build_parser()
        args = p.parse_args(["llmount", "co1-single"])
        assert args.cleanup is False
