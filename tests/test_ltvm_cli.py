"""Tests for the ltvm CLI entry point.

Covers argument parsing, output formatting, subcommand dispatch,
and error handling -- all without real build infra.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load the ltvm module (no .py extension -- must use SourceFileLoader)
# and the commands module where helpers now live after refactor.
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

from ltvm_pkg.cli import (  # noqa: E402, I001
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    _artifact_label,
    _error,
    _load_target,
    _not_found,
    _output,
    _resolve_lustre_tree,
    cmd_status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_main(argv: list[str], capsys: pytest.CaptureFixture[str]) -> int:
    """Patch sys.argv and call ltvm.main(); return exit code."""
    with patch.object(sys, "argv", ["ltvm"] + argv):
        return ltvm.main()


# ---------------------------------------------------------------------------
# Parser structure tests (no side effects)
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_returns_parser(self) -> None:
        p = ltvm.build_parser()
        assert p is not None
        assert p.prog == "ltvm"

    def test_json_flag_default_false(self) -> None:
        p = ltvm.build_parser()
        args = p.parse_args(["build-status"])
        assert args.json is False

    def test_json_flag_true_when_passed(self) -> None:
        # --json is defined on each subparser (via parents=[common]),
        # so it must appear after the subcommand name.
        p = ltvm.build_parser()
        args = p.parse_args(["build-status", "--json"])
        assert args.json is True

    def test_verbose_flag(self) -> None:
        p = ltvm.build_parser()
        args = p.parse_args(["build-status", "-v"])
        assert args.verbose is True

    def test_status_subcommand_sets_func(self) -> None:
        p = ltvm.build_parser()
        args = p.parse_args(["build-status"])
        assert args.func is cmd_status

    def test_build_all_target_positional(self) -> None:
        p = ltvm.build_parser()
        args = p.parse_args(["build-all", "rocky9"])
        assert args.target == "rocky9"
        assert args.force is False

    def test_build_all_force_flag(self) -> None:
        p = ltvm.build_parser()
        args = p.parse_args(["build-all", "rocky9", "--force"])
        assert args.force is True

    def test_list_subcommand_parsed(self) -> None:
        p = ltvm.build_parser()
        args = p.parse_args(["list"])
        assert args.command == "list"

    def test_deploy_subcommand(self) -> None:
        p = ltvm.build_parser()
        args = p.parse_args(["deploy", "myvm", "--mount"])
        assert args.vm == "myvm"
        assert args.mount is True


# ---------------------------------------------------------------------------
# --help exits 0
# ---------------------------------------------------------------------------


class TestHelp:
    def test_help_exits_zero(self) -> None:
        p = ltvm.build_parser()
        with pytest.raises(SystemExit) as exc_info:
            p.parse_args(["--help"])
        assert exc_info.value.code == 0

    def test_subcommand_help_exits_zero(self) -> None:
        p = ltvm.build_parser()
        with pytest.raises(SystemExit) as exc_info:
            p.parse_args(["build-status", "--help"])
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# No subcommand: main() prints help and returns EXIT_ERROR
# ---------------------------------------------------------------------------


class TestNoSubcommand:
    def test_no_command_returns_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = _run_main([], capsys)
        assert rc == EXIT_ERROR

    def test_no_command_prints_usage(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run_main([], capsys)
        out = capsys.readouterr().out
        assert "ltvm" in out


# ---------------------------------------------------------------------------
# _output helper: JSON vs human-readable
# ---------------------------------------------------------------------------


class TestOutputHelper:
    def test_string_human(self, capsys: pytest.CaptureFixture[str]) -> None:
        _output("hello world", use_json=False)
        assert capsys.readouterr().out.strip() == "hello world"

    def test_string_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        _output("hello world", use_json=True)
        out = capsys.readouterr().out
        assert json.loads(out) == "hello world"

    def test_dict_human(self, capsys: pytest.CaptureFixture[str]) -> None:
        _output({"key": "val"}, use_json=False)
        assert "key: val" in capsys.readouterr().out

    def test_dict_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        _output({"key": "val"}, use_json=True)
        out = capsys.readouterr().out
        assert json.loads(out) == {"key": "val"}

    def test_list_human(self, capsys: pytest.CaptureFixture[str]) -> None:
        _output(["a", "b"], use_json=False)
        out = capsys.readouterr().out
        assert "a" in out
        assert "b" in out

    def test_list_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        _output(["a", "b"], use_json=True)
        out = capsys.readouterr().out
        assert json.loads(out) == ["a", "b"]


# ---------------------------------------------------------------------------
# _error and _not_found helpers
# ---------------------------------------------------------------------------


class TestErrorHelpers:
    def test_error_human(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = _error("something went wrong", use_json=False)
        assert rc == EXIT_ERROR
        assert "something went wrong" in capsys.readouterr().err

    def test_error_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = _error("bad thing", use_json=True)
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        payload = json.loads(err)
        assert payload["error"] == "bad thing"

    def test_error_with_hint_json(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _error("oops", use_json=True, hint="try this")
        payload = json.loads(capsys.readouterr().err)
        assert payload["hint"] == "try this"

    def test_not_found_returns_exit_not_found(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = _not_found("no such target", use_json=False)
        assert rc == EXIT_NOT_FOUND

    def test_not_found_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        _not_found("missing", use_json=True)
        payload = json.loads(capsys.readouterr().err)
        assert "error" in payload


# ---------------------------------------------------------------------------
# _load_target: unknown target returns EXIT_NOT_FOUND
# ---------------------------------------------------------------------------


class TestLoadTarget:
    def test_unknown_target_returns_not_found(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # TargetConfig raises ValueError for unknown target names;
        # _load_target must catch it and return EXIT_NOT_FOUND.
        def _raise(name: str, arch: str = "x86_64") -> None:
            raise ValueError(f"Unknown target: {name}")

        with patch("ltvm_pkg.cli.TargetConfig", side_effect=_raise):
            tc, code = _load_target("no_such_target", use_json=False)
        assert tc is None
        assert code == EXIT_NOT_FOUND


# ---------------------------------------------------------------------------
# cmd_status: mocked list_targets + TargetConfig
# ---------------------------------------------------------------------------


class TestCmdStatus:
    def test_status_no_targets_human(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("ltvm_pkg.cli.list_targets", return_value=[]):
            rc = _run_main(["build-status"], capsys)
        assert rc == EXIT_OK
        assert "No targets" in capsys.readouterr().out

    def test_status_no_targets_json(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # --json must follow the subcommand name
        with patch("ltvm_pkg.cli.list_targets", return_value=[]):
            rc = _run_main(["build-status", "--json"], capsys)
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"targets": []}

    def test_status_with_target(
        self, capsys: pytest.CaptureFixture[str], tmp_targets: Path
    ) -> None:
        """With one configured target, status table includes its name."""
        import ltvm_pkg.target_config as cfg

        # Build a real TargetConfig against tmp_targets so it won't raise
        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            tc = cfg.TargetConfig("rocky9")

        with (
            patch("ltvm_pkg.cli.list_targets", return_value=["rocky9"]),
            patch("ltvm_pkg.cli.TargetConfig", return_value=tc),
            patch(
                "ltvm_pkg.cli.kernel_status",
                return_value={"built": False, "stale": True},
            ),
            patch(
                "ltvm_pkg.cli.image_status",
                return_value={"built": False, "stale": True},
            ),
        ):
            rc = _run_main(["build-status"], capsys)

        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "rocky9" in out


# ---------------------------------------------------------------------------
# cmd_status JSON output format
# ---------------------------------------------------------------------------


class TestCmdStatusJson:
    def test_json_output_is_valid(
        self, capsys: pytest.CaptureFixture[str], tmp_targets: Path
    ) -> None:
        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            tc = cfg.TargetConfig("rocky9")

        with (
            patch("ltvm_pkg.cli.list_targets", return_value=["rocky9"]),
            patch("ltvm_pkg.cli.TargetConfig", return_value=tc),
            patch(
                "ltvm_pkg.cli.kernel_status",
                return_value={"built": False, "stale": True},
            ),
            patch(
                "ltvm_pkg.cli.image_status",
                return_value={"built": False, "stale": True},
            ),
        ):
            rc = _run_main(["build-status", "--json"], capsys)

        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert "rocky9" in payload
        assert "container" in payload["rocky9"]
        assert "kernel" in payload["rocky9"]
        assert "images" in payload["rocky9"]

    def test_json_lists_built_kernels_separately(
        self, capsys: pytest.CaptureFixture[str], tmp_targets: Path
    ) -> None:
        """build-status enumerates one image entry per built kernel dir."""
        import ltvm_pkg.target_config as cfg

        # Pre-populate two built kernel dirs under output/rocky9/kernels/
        kernels = tmp_targets / "output" / "rocky9" / "kernels"
        (kernels / "5.14-rhel9.7-5.14.0-611.13.1").mkdir(parents=True)
        (kernels / "5.14-rhel9.5-5.14.0-503.26.1").mkdir(parents=True)

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            tc = cfg.TargetConfig("rocky9")

        with (
            patch("ltvm_pkg.cli.list_targets", return_value=["rocky9"]),
            patch("ltvm_pkg.cli.TargetConfig", return_value=tc),
            patch(
                "ltvm_pkg.cli.kernel_status",
                return_value={"built": False, "stale": True},
            ),
        ):
            rc = _run_main(["build-status", "--json"], capsys)

        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        images = payload["rocky9"]["images"]
        assert "5.14-rhel9.7-5.14.0-611.13.1" in images
        assert "5.14-rhel9.5-5.14.0-503.26.1" in images


# ---------------------------------------------------------------------------
# _artifact_label helper
# ---------------------------------------------------------------------------


class TestArtifactLabel:
    def test_not_built(self) -> None:
        assert _artifact_label({"built": False}) == "not built"

    def test_stale(self) -> None:
        assert _artifact_label({"built": True, "stale": True}) == "stale"

    def test_current(self) -> None:
        assert _artifact_label({"built": True, "stale": False}) == "current"


# ---------------------------------------------------------------------------
# cmd_validate
# ---------------------------------------------------------------------------


class TestCmdValidate:
    def _make_lustre_tree(
        self,
        tmp_path: Path,
        *,
        which_patch: str,
        changelog: str,
        target_in: str,
    ) -> Path:
        lt = tmp_path / "lustre-release"
        (lt / "lustre/kernel_patches/targets").mkdir(parents=True)
        (lt / "lustre/kernel_patches/which_patch").write_text(which_patch)
        (lt / "lustre/ChangeLog").write_text(changelog)
        (
            lt / "lustre/kernel_patches/targets/5.14-rhel9.7.target.in"
        ).write_text(target_in)
        return lt

    _TI = (
        'lnxmaj="5.14.0"\nlnxrel="611.13.1.el9_7"\nSERIES=5.14-rhel9.7.series\n'
    )
    _WP_OK = (
        "PATCH SERIES FOR SERVER KERNELS:\n"
        "5.14-rhel9.7.series    5.14.0-611.13.1.el9  (RHEL 9.7)\n\n"
    )
    _WP_ABSENT = (
        "PATCH SERIES FOR SERVER KERNELS:\n"
        "4.18-rhel8.10.series    4.18.0-553.89.1.el8  (RHEL 8.10)\n\n"
    )
    _CL = (
        "TBD Whamcloud\n\t* version 2.18.0\n"
        "\t* Server primary kernels built and tested during release cycle:\n"
        "\t  5.14.0-611.13.1.el9  (RHEL9.7)\n"
        "\t* Other server kernels known to build and work at some point:\n"
        "\t  vanilla linux 5.4.0\n"
        "\t* Client primary kernels built and tested during release cycle:\n"
        "\t  5.14.0-611.13.1.el9\n"
        "\t* Other clients known to build on these kernels at some point:\n"
        "\t  4.18.0-348.23.1.el8\n"
    )

    def _tc(self, tmp_targets: Path) -> Any:
        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            return cfg.TargetConfig("rocky9")

    def test_ok(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        tmp_targets: Path,
    ) -> None:
        lt = self._make_lustre_tree(
            tmp_path,
            which_patch=self._WP_OK,
            changelog=self._CL,
            target_in=self._TI,
        )
        tc = self._tc(tmp_targets)
        with patch("ltvm_pkg.cli.TargetConfig", return_value=tc):
            rc = _run_main(
                ["validate", "rocky9", "--lustre-tree", str(lt)], capsys
            )
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "[ok]" in out

    def test_refuse(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        tmp_targets: Path,
    ) -> None:
        lt = self._make_lustre_tree(
            tmp_path,
            which_patch=self._WP_ABSENT,
            changelog=self._CL,
            target_in=self._TI,
        )
        tc = self._tc(tmp_targets)
        with patch("ltvm_pkg.cli.TargetConfig", return_value=tc):
            rc = _run_main(
                ["validate", "rocky9", "--lustre-tree", str(lt)], capsys
            )
        assert rc == EXIT_ERROR
        out = capsys.readouterr().out
        assert "[refuse]" in out

    def test_refuse_with_force_exits_zero(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        tmp_targets: Path,
    ) -> None:
        lt = self._make_lustre_tree(
            tmp_path,
            which_patch=self._WP_ABSENT,
            changelog=self._CL,
            target_in=self._TI,
        )
        tc = self._tc(tmp_targets)
        with patch("ltvm_pkg.cli.TargetConfig", return_value=tc):
            rc = _run_main(
                ["validate", "rocky9", "--lustre-tree", str(lt), "--force"],
                capsys,
            )
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "--force:" in out
        assert "[refuse]" in out

    def test_json_output(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        tmp_targets: Path,
    ) -> None:
        lt = self._make_lustre_tree(
            tmp_path,
            which_patch=self._WP_OK,
            changelog=self._CL,
            target_in=self._TI,
        )
        tc = self._tc(tmp_targets)
        with patch("ltvm_pkg.cli.TargetConfig", return_value=tc):
            rc = _run_main(
                ["validate", "--json", "rocky9", "--lustre-tree", str(lt)],
                capsys,
            )
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["mode"] == "server_ldiskfs"
        assert payload["kernel_version"] == "5.14.0-611.13.1.el9_7"
        assert payload["matched_in"] == "which_patch_primary"
        assert payload["message"]


# ---------------------------------------------------------------------------
# update: missing target and --all
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _resolve_lustre_tree
# ---------------------------------------------------------------------------


class TestResolveLustreTree:
    def test_valid_tree(self, lustre_tree: Path) -> None:
        path, err = _resolve_lustre_tree(str(lustre_tree))
        assert err is None
        assert path == lustre_tree.resolve()

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "no_such_dir")
        path, err = _resolve_lustre_tree(missing)
        assert path is None
        assert err is not None
        assert "Not a directory" in err

    def test_dir_without_kernel_patches(self, tmp_path: Path) -> None:
        path, err = _resolve_lustre_tree(str(tmp_path))
        assert path is None
        assert err is not None
        assert "lustre/kernel_patches" in err

    def test_none_uses_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When arg is None, cwd is used; if cwd lacks kernel_patches, error."""
        monkeypatch.chdir("/tmp")
        path, err = _resolve_lustre_tree(None)
        # /tmp won't have lustre/kernel_patches, so we expect an error
        assert err is not None


# ---------------------------------------------------------------------------
# VM top-level subcommands: basic parse and dispatch checks
# ---------------------------------------------------------------------------


class TestVmSubcommands:
    def test_destroy_parses_names(self) -> None:
        p = ltvm.build_parser()
        args = p.parse_args(["destroy", "co1-single", "co1-other"])
        assert args.names == ["co1-single", "co1-other"]

    def test_ensure_parses_name_and_vcpus(self) -> None:
        p = ltvm.build_parser()
        args = p.parse_args(["ensure", "co1-single", "--vcpus", "4"])
        assert args.name == "co1-single"
        assert args.vcpus == 4

    def test_crash_collect_mod_dir(self) -> None:
        p = ltvm.build_parser()
        args = p.parse_args(
            ["crash-collect", "co1-single", "--mod-dir", "/path/to/build"]
        )
        assert args.name == "co1-single"
        assert args.mod_dir == "/path/to/build"

    def test_doctor_fix_flag(self) -> None:
        p = ltvm.build_parser()
        args = p.parse_args(["doctor", "--fix"])
        assert args.fix is True

    def test_vm_subcommand_not_present(self) -> None:
        """'ltvm vm' no longer exists as a subcommand."""
        p = ltvm.build_parser()
        with pytest.raises(SystemExit):
            p.parse_args(["vm", "list"])


# ---------------------------------------------------------------------------
# Lustre-tree validation gating in build/package/deploy commands
# ---------------------------------------------------------------------------


class TestValidationGating:
    """Stub validate_target and verify each gated command honors it."""

    def _vr(self, status: str, message: str = "stub-msg") -> Any:
        from ltvm_pkg.lustre_compat import ValidationResult

        return ValidationResult(
            status=status,  # type: ignore[arg-type]
            mode=None,
            kernel_version=None,
            matched_in=None,
            message=message,
        )

    def _tc(self, tmp_targets: Path) -> Any:
        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            return cfg.TargetConfig("rocky9")

    # --- gate helper directly ----------------------------------------

    def test_gate_ok_silent(
        self, capsys: pytest.CaptureFixture[str], tmp_targets: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = self._tc(tmp_targets)
        with patch.object(
            cli_mod, "validate_target", return_value=self._vr("ok")
        ):
            cli_mod._gate_lustre_validation(
                tc, Path("/x"), force=False
            )  # returns None
        cap = capsys.readouterr()
        assert cap.err == ""

    def test_gate_best_effort_warns(
        self, capsys: pytest.CaptureFixture[str], tmp_targets: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = self._tc(tmp_targets)
        with patch.object(
            cli_mod,
            "validate_target",
            return_value=self._vr("best_effort", "close but not exact"),
        ):
            cli_mod._gate_lustre_validation(tc, Path("/x"), force=False)
        err = capsys.readouterr().err
        assert "best_effort" in err
        assert "close but not exact" in err

    def test_gate_refuse_raises(
        self, capsys: pytest.CaptureFixture[str], tmp_targets: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = self._tc(tmp_targets)
        with patch.object(
            cli_mod,
            "validate_target",
            return_value=self._vr("refuse", "no match"),
        ):
            with pytest.raises(SystemExit) as exc:
                cli_mod._gate_lustre_validation(tc, Path("/x"), force=False)
        assert exc.value.code == EXIT_ERROR
        err = capsys.readouterr().err
        assert "refuse" in err

    def test_gate_refuse_force_passes(
        self, capsys: pytest.CaptureFixture[str], tmp_targets: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = self._tc(tmp_targets)
        with patch.object(
            cli_mod,
            "validate_target",
            return_value=self._vr("refuse", "no match"),
        ):
            cli_mod._gate_lustre_validation(tc, Path("/x"), force=True)
        err = capsys.readouterr().err
        assert "overriding refusal" in err

    def test_gate_error_raises_even_with_force(
        self, capsys: pytest.CaptureFixture[str], tmp_targets: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = self._tc(tmp_targets)
        with patch.object(
            cli_mod,
            "validate_target",
            return_value=self._vr("error", "parse failure"),
        ):
            with pytest.raises(SystemExit) as exc:
                cli_mod._gate_lustre_validation(tc, Path("/x"), force=True)
        assert exc.value.code == EXIT_ERROR
        err = capsys.readouterr().err
        assert "parse failure" in err

    # --- per-command gating ------------------------------------------

    def _common_patches(self, tmp_targets: Path, lustre_tree: Path) -> Any:
        """Patch context for running build/package commands without real work."""
        from ltvm_pkg import cli as cli_mod

        tc = self._tc(tmp_targets)
        return cli_mod, tc

    def test_build_all_ok_proceeds(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        cli_mod, tc = self._common_patches(tmp_targets, lustre_tree)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod, "validate_target", return_value=self._vr("ok")
            ),
            patch.object(cli_mod, "_do_build_container"),
            patch.object(
                cli_mod, "build_kernel", return_value={"ok": True}
            ) as bk,
            patch.object(cli_mod, "build_image"),
        ):
            rc = _run_main(
                [
                    "build-all",
                    "rocky9",
                    "--lustre-tree",
                    str(lustre_tree),
                ],
                capsys,
            )
        assert rc == EXIT_OK
        assert bk.called

    def test_build_all_refuse_aborts(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        cli_mod, tc = self._common_patches(tmp_targets, lustre_tree)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod,
                "validate_target",
                return_value=self._vr("refuse", "won't build"),
            ),
            patch.object(cli_mod, "_do_build_container") as dc,
            patch.object(cli_mod, "build_kernel") as bk,
        ):
            with pytest.raises(SystemExit) as exc:
                _run_main(
                    [
                        "build-all",
                        "rocky9",
                        "--lustre-tree",
                        str(lustre_tree),
                    ],
                    capsys,
                )
        assert exc.value.code == EXIT_ERROR
        assert not dc.called
        assert not bk.called
        assert "refuse" in capsys.readouterr().err

    def test_build_all_refuse_force_compat_proceeds(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        cli_mod, tc = self._common_patches(tmp_targets, lustre_tree)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod,
                "validate_target",
                return_value=self._vr("refuse", "won't build"),
            ),
            patch.object(cli_mod, "_do_build_container"),
            patch.object(cli_mod, "build_kernel", return_value={}),
            patch.object(cli_mod, "build_image"),
        ):
            rc = _run_main(
                [
                    "build-all",
                    "rocky9",
                    "--lustre-tree",
                    str(lustre_tree),
                    "--force-compat",
                ],
                capsys,
            )
        assert rc == EXIT_OK
        assert "overriding refusal" in capsys.readouterr().err

    def test_build_all_best_effort_proceeds_with_warning(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        cli_mod, tc = self._common_patches(tmp_targets, lustre_tree)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod,
                "validate_target",
                return_value=self._vr("best_effort", "close enough"),
            ),
            patch.object(cli_mod, "_do_build_container"),
            patch.object(cli_mod, "build_kernel", return_value={}),
            patch.object(cli_mod, "build_image"),
        ):
            rc = _run_main(
                [
                    "build-all",
                    "rocky9",
                    "--lustre-tree",
                    str(lustre_tree),
                ],
                capsys,
            )
        assert rc == EXIT_OK
        assert "best_effort" in capsys.readouterr().err

    def test_build_all_error_aborts_even_with_force(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        cli_mod, tc = self._common_patches(tmp_targets, lustre_tree)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod,
                "validate_target",
                return_value=self._vr("error", "parse bang"),
            ),
            patch.object(cli_mod, "_do_build_container") as dc,
        ):
            with pytest.raises(SystemExit) as exc:
                _run_main(
                    [
                        "build-all",
                        "rocky9",
                        "--lustre-tree",
                        str(lustre_tree),
                        "--force-compat",
                    ],
                    capsys,
                )
        assert exc.value.code == EXIT_ERROR
        assert not dc.called

    def test_build_kernel_refuse_aborts(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = self._tc(tmp_targets)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod,
                "validate_target",
                return_value=self._vr("refuse", "nope"),
            ),
            patch.object(cli_mod, "build_kernel") as bk,
        ):
            with pytest.raises(SystemExit) as exc:
                _run_main(
                    [
                        "build-kernel",
                        "rocky9",
                        "--lustre-tree",
                        str(lustre_tree),
                    ],
                    capsys,
                )
        assert exc.value.code == EXIT_ERROR
        assert not bk.called

    def test_build_kernel_ok_proceeds(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = self._tc(tmp_targets)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod, "validate_target", return_value=self._vr("ok")
            ),
            patch.object(
                cli_mod, "build_kernel", return_value={"ok": True}
            ) as bk,
        ):
            rc = _run_main(
                [
                    "build-kernel",
                    "rocky9",
                    "--lustre-tree",
                    str(lustre_tree),
                ],
                capsys,
            )
        assert rc == EXIT_OK
        assert bk.called

    def test_build_lustre_refuse_aborts(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = self._tc(tmp_targets)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod,
                "validate_target",
                return_value=self._vr("refuse", "no"),
            ),
            patch.object(cli_mod, "build_lustre") as bl,
        ):
            with pytest.raises(SystemExit) as exc:
                _run_main(
                    [
                        "build-lustre",
                        "rocky9",
                        str(lustre_tree),
                    ],
                    capsys,
                )
        assert exc.value.code == EXIT_ERROR
        assert not bl.called

    def test_build_lustre_refuse_force_compat_proceeds(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        """--force-compat overrides gating; existing --force is unrelated."""
        from ltvm_pkg import cli as cli_mod

        tc = self._tc(tmp_targets)
        # Create build-tree so the pre-flight check passes.
        bt = tc.kernel_output_dir() / "build-tree"
        bt.mkdir(parents=True, exist_ok=True)

        ok_proc = MagicMock()
        ok_proc.returncode = 0
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod,
                "validate_target",
                return_value=self._vr("refuse", "no"),
            ),
            patch("subprocess.run", return_value=ok_proc),
            patch.object(
                cli_mod, "build_lustre", return_value={"ok": True}
            ) as bl,
        ):
            rc = _run_main(
                [
                    "build-lustre",
                    "rocky9",
                    str(lustre_tree),
                    "--force-compat",
                ],
                capsys,
            )
        assert rc == EXIT_OK
        assert bl.called

    def test_package_refuse_aborts(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = self._tc(tmp_targets)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod,
                "validate_target",
                return_value=self._vr("refuse", "nope"),
            ),
            patch.object(cli_mod, "snapshot_lustre") as snap,
            patch.object(cli_mod, "package_target") as pt,
        ):
            with pytest.raises(SystemExit) as exc:
                _run_main(
                    [
                        "package",
                        "rocky9",
                        "--lustre-tree",
                        str(lustre_tree),
                    ],
                    capsys,
                )
        assert exc.value.code == EXIT_ERROR
        assert not snap.called
        assert not pt.called

    def test_package_ok_proceeds(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
        tmp_path: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = self._tc(tmp_targets)
        tarball = tmp_path / "out.tar.gz"
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod, "validate_target", return_value=self._vr("ok")
            ),
            patch.object(cli_mod, "snapshot_lustre") as snap,
            patch.object(
                cli_mod, "package_target", return_value=tarball
            ) as pt,
        ):
            rc = _run_main(
                [
                    "package",
                    "rocky9",
                    "--lustre-tree",
                    str(lustre_tree),
                ],
                capsys,
            )
        assert rc == EXIT_OK
        assert snap.called
        assert pt.called

    def test_deploy_refuse_aborts(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        """cmd_deploy gate fires when --build triggers a rebuild."""
        import argparse as ap

        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.vm_state import VMInfo

        sockets_dir = tmp_path / "sockets"
        sockets_dir.mkdir()
        build_path = tmp_path / "lustre-release"
        (build_path / "lustre").mkdir(parents=True)
        (build_path / "lnet").mkdir()
        (build_path / "configure.ac").write_text("")

        tc = self._tc(tmp_targets)

        with patch("ltvm_pkg.vm_state.SOCKETS", sockets_dir):
            vm = VMInfo(
                name="co1-gate-test",
                ip="192.168.100.51",
                os_id="rocky9",
            )
            vm.save()

            with (
                patch.object(cli_mod, "_require_root", return_value=None),
                patch("ltvm_pkg.vm_state.VMInfo.load", return_value=vm),
                patch.object(cli_mod, "TargetConfig", return_value=tc),
                patch.object(
                    cli_mod,
                    "validate_target",
                    return_value=self._vr("refuse", "nope"),
                ),
                patch("subprocess.run") as run_mock,
            ):
                args = ap.Namespace(
                    vm="co1-gate-test",
                    build=str(build_path),
                    mount=False,
                    target=None,
                    kernel=None,
                    json=False,
                    userspace_only=False,
                    force_compat=False,
                )
                with pytest.raises(SystemExit) as exc:
                    cli_mod.cmd_deploy(args)

        assert exc.value.code == EXIT_ERROR
        # subprocess.run must NOT have been called to spawn build-lustre.
        calls = [
            c
            for c in run_mock.call_args_list
            if c.args
            and isinstance(c.args[0], list)
            and len(c.args[0]) > 1
            and c.args[0][1] == "build-lustre"
        ]
        assert calls == []


# ---------------------------------------------------------------------------
# cmd_deploy: build gating
# ---------------------------------------------------------------------------


class TestCmdDeployBuildGating:
    """cmd_deploy must abort when build-lustre fails or staging has no .ko files."""

    def test_build_failure_returns_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When build-lustre returns non-zero, cmd_deploy returns EXIT_ERROR."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.vm_state import VMInfo

        sockets_dir = tmp_path / "sockets"
        sockets_dir.mkdir()
        build_path = tmp_path / "lustre-release"
        build_path.mkdir()

        with patch("ltvm_pkg.vm_state.SOCKETS", sockets_dir):
            vm = VMInfo(
                name="co1-deploy-test",
                ip="192.168.100.50",
                os_id="rocky9",
            )
            vm.save()

            def _load_vm(name: str) -> VMInfo:
                return VMInfo.load(name)

            fail_result = MagicMock()
            fail_result.returncode = 1

            with (
                patch("ltvm_pkg.cli._require_root", return_value=None),
                patch("ltvm_pkg.vm_state.VMInfo.load", return_value=vm),
                patch("ltvm_pkg.cli.TargetConfig") as mock_tc,
                patch("subprocess.run", return_value=fail_result),
            ):
                mock_tc.return_value.os_family = "rhel"
                mock_tc.return_value.resolve_kernel.return_value = (
                    "5.14-rhel9.7"
                )

                args = argparse.Namespace(
                    vm="co1-deploy-test",
                    build=str(build_path),
                    mount=False,
                    target=None,
                    kernel=None,
                    json=False,
                )
                rc = cli_mod.cmd_deploy(args)

        assert rc == EXIT_ERROR

    def test_build_success_no_ko_files_returns_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When build succeeds but staging has no .ko files, cmd_deploy errors."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.vm_state import VMInfo

        sockets_dir = tmp_path / "sockets"
        sockets_dir.mkdir()
        build_path = tmp_path / "lustre-release"
        build_path.mkdir()

        # Create staging dir (in-tree, the new layout) with NO .ko files.
        # build_path is the lustre tree; staging lives at
        # <build_path>/.ltvm-staging/<target>/<arch>/.
        staging = build_path / ".ltvm-staging" / "rocky9" / "x86_64"
        staging.mkdir(parents=True)
        (staging / "some-file.txt").write_text("not a kernel module")

        with patch("ltvm_pkg.vm_state.SOCKETS", sockets_dir):
            vm = VMInfo(
                name="co1-deploy-test",
                ip="192.168.100.50",
                os_id="rocky9",
            )
            vm.save()

            ok_result = MagicMock()
            ok_result.returncode = 0

            with (
                patch("ltvm_pkg.cli._require_root", return_value=None),
                patch("ltvm_pkg.vm_state.VMInfo.load", return_value=vm),
                patch("ltvm_pkg.cli.TargetConfig") as mock_tc,
                patch("subprocess.run", return_value=ok_result),
            ):
                mock_tc.return_value.os_family = "rhel"
                mock_tc.return_value.resolve_kernel.return_value = (
                    "5.14-rhel9.7"
                )

                args = argparse.Namespace(
                    vm="co1-deploy-test",
                    build=str(build_path),
                    mount=False,
                    target=None,
                    kernel=None,
                    json=False,
                )
                rc = cli_mod.cmd_deploy(args)

        assert rc == EXIT_ERROR

    def test_build_failure_does_not_reach_tar_ssh(self, tmp_path: Path) -> None:
        """When build fails, the tar/ssh deploy step is never executed."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.vm_state import VMInfo

        sockets_dir = tmp_path / "sockets"
        sockets_dir.mkdir()
        build_path = tmp_path / "lustre-release"
        build_path.mkdir()

        with patch("ltvm_pkg.vm_state.SOCKETS", sockets_dir):
            vm = VMInfo(
                name="co1-deploy-test",
                ip="192.168.100.50",
                os_id="rocky9",
            )
            vm.save()

            fail_result = MagicMock()
            fail_result.returncode = 1
            subprocess_calls: list = []

            def _track_run(cmd, *args, **kwargs):
                subprocess_calls.append(cmd)
                return fail_result

            with (
                patch("ltvm_pkg.cli._require_root", return_value=None),
                patch("ltvm_pkg.vm_state.VMInfo.load", return_value=vm),
                patch("ltvm_pkg.cli.TargetConfig") as mock_tc,
                patch("subprocess.run", side_effect=_track_run),
            ):
                mock_tc.return_value.os_family = "rhel"
                mock_tc.return_value.resolve_kernel.return_value = (
                    "5.14-rhel9.7"
                )

                args = argparse.Namespace(
                    vm="co1-deploy-test",
                    build=str(build_path),
                    mount=False,
                    target=None,
                    kernel=None,
                    json=False,
                )
                cli_mod.cmd_deploy(args)

        # Only the build command should have been called (via subprocess.run).
        # The tar/ssh deploy uses subprocess.run with ["bash", "-c", tar_cmd].
        bash_calls = [
            c
            for c in subprocess_calls
            if isinstance(c, list) and c[:1] == ["bash"]
        ]
        assert bash_calls == [], (
            "tar/ssh deploy must not be called after build failure"
        )


# ---------------------------------------------------------------------------
# --kernel argparse propagation for build-image and build-all
# ---------------------------------------------------------------------------


class TestKernelArgPropagation:
    """Verify --kernel is forwarded to the underlying build functions."""

    def _tc(self, tmp_targets: Path) -> Any:
        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            return cfg.TargetConfig("rocky9")

    def test_build_image_kernel_forwarded(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = self._tc(tmp_targets)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "build_image") as mock_bi,
        ):
            mock_bi.return_value = Path("/fake/base.ext4")
            rc = _run_main(
                ["build-image", "rocky9", "--kernel", "5.14-rhel9.5"],
                capsys,
            )

        assert rc == EXIT_OK
        mock_bi.assert_called_once()
        _, kwargs = mock_bi.call_args
        assert kwargs.get("kernel") == "5.14-rhel9.5"

    def test_build_image_with_lustre_forwarded(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = self._tc(tmp_targets)
        lt = tmp_path / "tree"
        lt.mkdir()
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "build_image") as mock_bi,
        ):
            mock_bi.return_value = Path("/fake/base.ext4")
            rc = _run_main(
                [
                    "build-image",
                    "rocky9",
                    "--kernel",
                    "5.14-rhel9.7",
                    "--with-lustre",
                    str(lt),
                ],
                capsys,
            )

        assert rc == EXIT_OK
        mock_bi.assert_called_once()
        _, kwargs = mock_bi.call_args
        assert kwargs.get("kernel") == "5.14-rhel9.7"
        assert kwargs.get("with_lustre") == str(lt)

    def test_build_image_without_with_lustre_is_none(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = self._tc(tmp_targets)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "build_image") as mock_bi,
        ):
            mock_bi.return_value = Path("/fake/base.ext4")
            rc = _run_main(["build-image", "rocky9"], capsys)

        assert rc == EXIT_OK
        _, kwargs = mock_bi.call_args
        assert kwargs.get("with_lustre") is None

    def test_build_all_kernel_reaches_image_builder(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_compat import ValidationResult

        tc = self._tc(tmp_targets)
        vr = ValidationResult(
            status="ok",
            mode=None,
            kernel_version=None,
            matched_in=None,
            message="stub",
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "validate_target", return_value=vr),
            patch.object(cli_mod, "_do_build_container"),
            patch.object(
                cli_mod, "build_kernel", return_value={"ok": True}
            ),
            patch.object(cli_mod, "build_image") as mock_bi,
        ):
            rc = _run_main(
                [
                    "build-all",
                    "rocky9",
                    "--kernel",
                    "5.14-rhel9.5",
                    "--lustre-tree",
                    str(lustre_tree),
                ],
                capsys,
            )

        assert rc == EXIT_OK
        mock_bi.assert_called_once()
        _, kwargs = mock_bi.call_args
        assert kwargs.get("kernel") == "5.14-rhel9.5"
