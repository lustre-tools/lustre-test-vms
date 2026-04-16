"""Behavioral tests for build commands in ltvm_pkg/cli.py.

Covers cmd_build_all, cmd_build_container, cmd_build_kernel,
cmd_build_image, cmd_build_lustre, cmd_build_shell,
cmd_build_mofed_kmods, cmd_status, cmd_clean, and _do_build_container.

These exist to lock down behavior before splitting cli.py into
submodules: each test asserts an externally-visible behavior
(exit code, output, what gets called with what args) rather than
implementation details.
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
import yaml

# Load the ltvm CLI entry point (no .py extension).
_LTVM_PATH = str(Path(__file__).parent.parent / "ltvm")


def _load_ltvm() -> Any:
    loader = importlib.machinery.SourceFileLoader("ltvm", _LTVM_PATH)
    spec = importlib.util.spec_from_loader("ltvm", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ltvm = _load_ltvm()

from ltvm_pkg.cli import EXIT_ERROR, EXIT_OK  # noqa: E402


def _run_main(argv: list[str]) -> int:
    with patch.object(sys, "argv", ["ltvm"] + argv):
        return ltvm.main()


def _make_tc(tmp_targets: Path, *, variant: str = "base") -> Any:
    """Create a TargetConfig pinned to tmp_targets."""
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
        return cfg.TargetConfig("rocky9", variant=variant)


def _add_mofed_variant(tmp_targets: Path) -> None:
    """Mutate the rocky9 fixture to declare a mofed-24 variant pinned
    to a non-default kernel.  Mirrors targets.yaml's real declaration so
    we can exercise variant kernel-pin propagation end-to-end."""
    yaml_path = tmp_targets / "targets" / "targets.yaml"
    data = yaml.safe_load(yaml_path.read_text())
    data["targets"]["rocky9"]["variants"] = {
        "mofed-24": {
            # Pin to a non-default kernel; the fixture declares
            # 5.14-rhel9.7 as default and 5.14-rhel9.5 as available.
            "kernel": "5.14-rhel9.5",
            "params": {
                "mofed_version": "24.10-2.1.8.0",
                "mofed_distro": "rhel9.5",
            },
        }
    }
    yaml_path.write_text(yaml.dump(data, default_flow_style=False))


def _ok_proc() -> Any:
    p = MagicMock()
    p.returncode = 0
    return p


# ---------------------------------------------------------------------------
# cmd_build_container -- happy path + error path
# ---------------------------------------------------------------------------


class TestCmdBuildContainer:
    def test_happy_path_text(
        self, capsys: pytest.CaptureFixture[str], tmp_targets: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = _make_tc(tmp_targets)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod, "_do_build_container", return_value="ltvm-build-rocky9"
            ) as dc,
        ):
            rc = _run_main(["build", "container", "rocky9"])
        assert rc == EXIT_OK
        dc.assert_called_once_with(tc)
        out = capsys.readouterr().out
        assert "rocky9" in out
        assert "ltvm-build-rocky9" in out

    def test_happy_path_json(
        self, capsys: pytest.CaptureFixture[str], tmp_targets: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = _make_tc(tmp_targets)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod, "_do_build_container", return_value="ltvm-build-rocky9"
            ),
        ):
            rc = _run_main(["build", "container", "rocky9", "--json"])
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"target": "rocky9", "image_tag": "ltvm-build-rocky9"}

    def test_build_failure_returns_error(
        self, capsys: pytest.CaptureFixture[str], tmp_targets: Path
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = _make_tc(tmp_targets)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod,
                "_do_build_container",
                side_effect=RuntimeError("podman exploded"),
            ),
        ):
            rc = _run_main(["build", "container", "rocky9"])
        assert rc == EXIT_ERROR
        assert "Container build failed" in capsys.readouterr().err

    def test_unknown_target_returns_not_found(
        self, capsys: pytest.CaptureFixture[str], tmp_targets: Path
    ) -> None:
        # _load_target_args raises ValueError for unknown targets;
        # cmd_build_container must propagate that as EXIT_NOT_FOUND.
        from ltvm_pkg.cli import EXIT_NOT_FOUND

        rc = _run_main(["build", "container", "no_such_target"])
        assert rc == EXIT_NOT_FOUND


# ---------------------------------------------------------------------------
# _do_build_container helper
# ---------------------------------------------------------------------------


class TestDoBuildContainer:
    def test_writes_meta_with_tag(self, tmp_targets: Path) -> None:
        """_do_build_container delegates tag creation and persists meta."""
        from ltvm_pkg import cli as cli_mod

        tc = _make_tc(tmp_targets)
        with patch(
            "ltvm_pkg.kernel_build._ensure_container_image",
            return_value="ltvm-build-rocky9",
        ):
            tag = cli_mod._do_build_container(tc)
        assert tag == "ltvm-build-rocky9"
        meta_path = tc.container_output_dir() / "meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["image_tag"] == "ltvm-build-rocky9"
        # write_meta seeds target + input_hash
        assert meta["target"] == "rocky9"
        assert "input_hash" in meta


# ---------------------------------------------------------------------------
# cmd_build_kernel -- happy path + missing-tree error
# ---------------------------------------------------------------------------


class TestCmdBuildKernelExtras:
    def test_missing_lustre_tree_errors(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        """cmd_build_kernel demands a Lustre tree for non-deb targets."""
        from ltvm_pkg import cli as cli_mod

        tc = _make_tc(tmp_targets)
        bogus = tmp_path / "no_such_dir"
        with patch.object(cli_mod, "TargetConfig", return_value=tc):
            rc = _run_main(
                ["build", "kernel", "rocky9", "--lustre-tree", str(bogus)]
            )
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "Not a directory" in err

    def test_build_failure_returns_error(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_compat import ValidationResult

        tc = _make_tc(tmp_targets)
        vr = ValidationResult(
            status="ok", mode=None, kernel_version=None,
            matched_in=None, message="ok",
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "validate_target", return_value=vr),
            patch.object(
                cli_mod, "build_kernel",
                side_effect=RuntimeError("kernel boom"),
            ),
        ):
            rc = _run_main(
                ["build", "kernel", "rocky9", "--lustre-tree", str(lustre_tree)]
            )
        assert rc == EXIT_ERROR
        assert "Kernel build failed" in capsys.readouterr().err

    def test_force_propagates_to_build_kernel(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_compat import ValidationResult

        tc = _make_tc(tmp_targets)
        vr = ValidationResult(
            status="ok", mode=None, kernel_version=None,
            matched_in=None, message="ok",
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "validate_target", return_value=vr),
            patch.object(
                cli_mod, "build_kernel", return_value={"ok": True}
            ) as bk,
        ):
            rc = _run_main(
                ["build", "kernel", "rocky9",
                 "--lustre-tree", str(lustre_tree),
                 "--force"]
            )
        assert rc == EXIT_OK
        _, kwargs = bk.call_args
        assert kwargs.get("force") is True


# ---------------------------------------------------------------------------
# cmd_build_image -- exception path + force propagation
# ---------------------------------------------------------------------------


class TestCmdBuildImageExtras:
    def test_build_image_failure_returns_error(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = _make_tc(tmp_targets)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod, "build_image",
                side_effect=RuntimeError("image boom"),
            ),
        ):
            rc = _run_main(["build", "image", "rocky9", "--no-lustre"])
        assert rc == EXIT_ERROR
        assert "Image build failed" in capsys.readouterr().err

    def test_force_propagates_to_build_image(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = _make_tc(tmp_targets)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "build_image") as mock_bi,
        ):
            mock_bi.return_value = Path("/fake/base.ext4")
            rc = _run_main(
                ["build", "image", "rocky9", "--no-lustre", "--force"]
            )
        assert rc == EXIT_OK
        _, kwargs = mock_bi.call_args
        assert kwargs.get("force") is True

    def test_relative_lustre_tree_is_resolved(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--lustre-tree ./rel`` must resolve to an absolute path
        before computing the staging candidate; otherwise cmd_build_image
        diverges from cmd_build_lustre (which resolves via
        _resolve_lustre_tree) and refuses to find the staging that
        lustre-build just produced.
        """
        from ltvm_pkg import cli as cli_mod

        tc = _make_tc(tmp_targets)
        # Create a fake Lustre tree + pre-existing staging so the
        # candidate.exists() check passes.
        tree_abs = (tmp_path / "mylustre").resolve()
        tree_abs.mkdir()
        (tree_abs / "configure.ac").write_text("")
        # Pre-create staging at the path _staging_path will compute.
        from ltvm_pkg.lustre_build import staging_path as _sp

        staging = _sp(
            tree_abs, "rocky9", arch="x86_64",
            kernel=tc.resolve_kernel(None), variant="base",
        )
        staging.mkdir(parents=True)
        (staging / ".ltvm-staging-stamp").write_text("ok\n")

        # Run from tmp_path with a RELATIVE --lustre-tree argument.
        monkeypatch.chdir(tmp_path)

        captured_with_lustre: dict[str, str] = {}

        def _fake_build(target_config, **kw):  # type: ignore[no-untyped-def]
            captured_with_lustre["v"] = kw.get("with_lustre", "")
            return Path("/fake/base.ext4")

        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "_gate_lustre_validation"),
            patch.object(cli_mod, "build_image", side_effect=_fake_build),
        ):
            rc = _run_main(
                ["build", "image", "rocky9", "--lustre-tree", "mylustre"]
            )
        assert rc == EXIT_OK
        # Absolute, matches the same resolved path cmd_build_lustre
        # would have written staging under.
        assert captured_with_lustre["v"] == str(tree_abs)


# ---------------------------------------------------------------------------
# cmd_build_lustre -- preflight + error paths
# ---------------------------------------------------------------------------


class TestCmdBuildLustre:
    def test_missing_build_tree_errors(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        """When the kernel build-tree is missing, we get a distinctive
        error pointing at `ltvm build kernel ...`."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_compat import ValidationResult

        tc = _make_tc(tmp_targets)
        vr = ValidationResult(
            status="ok", mode=None, kernel_version=None,
            matched_in=None, message="ok",
        )
        # No build-tree on disk for tc.
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "validate_target", return_value=vr),
            patch.object(cli_mod, "build_lustre") as bl,
        ):
            rc = _run_main(
                ["build", "lustre", "rocky9", str(lustre_tree)]
            )
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "Kernel build-tree not found" in err
        assert "ltvm build kernel rocky9" in err
        assert not bl.called

    def test_missing_container_distinctive_error(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        """`ltvm build lustre` without a built container surfaces the
        hint to run `ltvm build container ...` rather than burying it in
        a 'Lustre build failed: ...' wrapper."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_compat import ValidationResult

        tc = _make_tc(tmp_targets)
        # Make build-tree exist so we get past that check.
        bt = tc.kernel_output_dir() / "build-tree"
        bt.mkdir(parents=True, exist_ok=True)
        vr = ValidationResult(
            status="ok", mode=None, kernel_version=None,
            matched_in=None, message="ok",
        )
        # podman image exists -> non-zero (image absent).
        miss_proc = MagicMock()
        miss_proc.returncode = 1
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "validate_target", return_value=vr),
            patch("subprocess.run", return_value=miss_proc),
            patch.object(cli_mod, "build_lustre") as bl,
        ):
            rc = _run_main(
                ["build", "lustre", "rocky9", str(lustre_tree)]
            )
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "not found in podman storage" in err
        assert "ltvm build container rocky9" in err
        assert not bl.called

    def test_build_failure_returns_error(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_compat import ValidationResult

        tc = _make_tc(tmp_targets)
        bt = tc.kernel_output_dir() / "build-tree"
        bt.mkdir(parents=True, exist_ok=True)
        vr = ValidationResult(
            status="ok", mode=None, kernel_version=None,
            matched_in=None, message="ok",
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "validate_target", return_value=vr),
            patch("subprocess.run", return_value=_ok_proc()),
            patch.object(
                cli_mod, "build_lustre",
                side_effect=RuntimeError("autogen failed"),
            ),
        ):
            rc = _run_main(
                ["build", "lustre", "rocky9", str(lustre_tree)]
            )
        assert rc == EXIT_ERROR
        assert "Lustre build failed" in capsys.readouterr().err

    def test_disable_server_overrides_mode(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        """--disable-server flips enable_server off even when the target
        defaults to server mode."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_compat import ValidationResult

        tc = _make_tc(tmp_targets)
        bt = tc.kernel_output_dir() / "build-tree"
        bt.mkdir(parents=True, exist_ok=True)
        vr = ValidationResult(
            status="ok", mode=None, kernel_version=None,
            matched_in=None, message="ok",
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "validate_target", return_value=vr),
            patch("subprocess.run", return_value=_ok_proc()),
            patch.object(
                cli_mod, "build_lustre", return_value={"ok": True}
            ) as bl,
        ):
            rc = _run_main(
                ["build", "lustre", "rocky9", str(lustre_tree),
                 "--disable-server"]
            )
        assert rc == EXIT_OK
        _, kwargs = bl.call_args
        assert kwargs.get("enable_server") is False

    def test_configure_extras_passed_through(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        """--configure 'a b c' is shlex-split onto extra_configure."""
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_compat import ValidationResult

        tc = _make_tc(tmp_targets)
        bt = tc.kernel_output_dir() / "build-tree"
        bt.mkdir(parents=True, exist_ok=True)
        vr = ValidationResult(
            status="ok", mode=None, kernel_version=None,
            matched_in=None, message="ok",
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "validate_target", return_value=vr),
            patch("subprocess.run", return_value=_ok_proc()),
            patch.object(
                cli_mod, "build_lustre", return_value={"ok": True}
            ) as bl,
        ):
            _run_main(
                ["build", "lustre", "rocky9", str(lustre_tree),
                 "--configure", "--enable-foo --with-bar=baz"]
            )
        _, kwargs = bl.call_args
        extra = kwargs.get("extra_configure", [])
        assert "--enable-foo" in extra
        assert "--with-bar=baz" in extra


# ---------------------------------------------------------------------------
# cmd_build_shell
# ---------------------------------------------------------------------------


class TestCmdBuildShell:
    def test_missing_path_errors(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = _make_tc(tmp_targets)
        with patch.object(cli_mod, "TargetConfig", return_value=tc):
            rc = _run_main(
                ["build", "shell", "rocky9", str(tmp_path / "no_such_dir")]
            )
        assert rc == EXIT_ERROR
        assert "Mount path not found" in capsys.readouterr().err

    def test_missing_container_errors(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        """When the container image is absent in podman storage, give a
        distinct error with a build-container hint."""
        from ltvm_pkg import cli as cli_mod

        tc = _make_tc(tmp_targets)
        miss = MagicMock()
        miss.returncode = 1
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch("subprocess.run", return_value=miss),
        ):
            rc = _run_main(
                ["build", "shell", "rocky9", str(tmp_path)]
            )
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "Container image" in err
        assert "ltvm build container rocky9" in err

    def test_podman_not_installed_errors(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        tc = _make_tc(tmp_targets)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch("subprocess.run", side_effect=FileNotFoundError("podman")),
        ):
            rc = _run_main(
                ["build", "shell", "rocky9", str(tmp_path)]
            )
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "podman" in err

    def test_happy_path_invokes_podman_run(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        """Container exists -> podman run is invoked with -v <path>:/src:Z."""
        from ltvm_pkg import cli as cli_mod

        tc = _make_tc(tmp_targets)
        calls: list[list[str]] = []

        def _run(cmd: Any, *a: Any, **kw: Any) -> Any:
            # First call: `podman image exists <tag>` -> success.
            # Subsequent: the actual `podman run ...` -- assert and exit.
            calls.append(list(cmd))
            p = MagicMock()
            p.returncode = 0
            return p

        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch("subprocess.run", side_effect=_run),
        ):
            rc = _run_main(["build", "shell", "rocky9", str(tmp_path)])
        assert rc == 0
        # Find the `podman run ...` call (not the `podman image exists`).
        run_calls = [c for c in calls if len(c) > 1 and c[1] == "run"]
        assert len(run_calls) == 1
        cmd = run_calls[0]
        assert "-v" in cmd
        # The mount string should reference the resolved tmp_path.
        mount_arg_idx = cmd.index("-v") + 1
        assert str(tmp_path.resolve()) in cmd[mount_arg_idx]
        assert cmd[mount_arg_idx].endswith(":/src:Z")
        assert "bash" in cmd


# ---------------------------------------------------------------------------
# cmd_build_mofed_kmods
# ---------------------------------------------------------------------------


class TestCmdBuildMofedKmods:
    def test_base_variant_refused(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        """Running mofed-kmods against the base variant errors with a
        hint to pass --variant mofed-*."""
        from ltvm_pkg import cli as cli_mod

        tc = _make_tc(tmp_targets)  # base variant
        with patch.object(cli_mod, "TargetConfig", return_value=tc):
            rc = _run_main(["build", "mofed-kmods", "rocky9"])
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "base variant" in err
        # Hint text mentions --variant mofed-...
        assert "--variant" in err

    def test_happy_path_emits_rpm_list(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        _add_mofed_variant(tmp_targets)
        tc = _make_tc(tmp_targets, variant="mofed-24")

        # build_mofed_kmods returns the dir containing produced RPMs.
        out_dir = tmp_targets / "mofed_out"
        out_dir.mkdir()
        (out_dir / "kmod-mlnx-ofa_kernel-24.10.x86_64.rpm").write_text("")
        (out_dir / "mlnx-ofa_kernel-24.10.x86_64.rpm").write_text("")

        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch(
                "ltvm_pkg.mofed_kmod_build.build_mofed_kmods",
                return_value=out_dir,
            ),
        ):
            rc = _run_main(
                ["build", "mofed-kmods", "rocky9", "--variant", "mofed-24",
                 "--json"]
            )
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["target"] == "rocky9"
        assert payload["variant"] == "mofed-24"
        # Variant pin should resolve kernel to 5.14-rhel9.5.
        assert payload["kernel"] == "5.14-rhel9.5"
        assert "kmod-mlnx-ofa_kernel-24.10.x86_64.rpm" in payload["rpms"]

    def test_build_failure_returns_error(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        _add_mofed_variant(tmp_targets)
        tc = _make_tc(tmp_targets, variant="mofed-24")
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch(
                "ltvm_pkg.mofed_kmod_build.build_mofed_kmods",
                side_effect=FileNotFoundError("no build-tree"),
            ),
        ):
            rc = _run_main(
                ["build", "mofed-kmods", "rocky9", "--variant", "mofed-24"]
            )
        assert rc == EXIT_ERROR
        assert "MOFED kmod build failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Variant kernel-pin propagation through build commands
# ---------------------------------------------------------------------------


class TestVariantKernelPinPropagation:
    """`ltvm build all rocky9 --variant mofed-24` must build against the
    pinned 5.14-rhel9.5 kernel, NOT the default 5.14-rhel9.7.

    Same applies to build-image and build-kernel.  This is the bug fixed
    in the commit referenced in cmd_build_all (resolve_kernel returns the
    pin when --kernel is omitted).  Locking down here so a refactor can't
    silently revert it.
    """

    def test_build_all_uses_variant_pinned_kernel(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod
        from ltvm_pkg.lustre_compat import ValidationResult

        _add_mofed_variant(tmp_targets)
        tc = _make_tc(tmp_targets, variant="mofed-24")
        vr = ValidationResult(
            status="ok", mode=None, kernel_version=None,
            matched_in=None, message="ok",
        )
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "validate_target", return_value=vr),
            patch.object(cli_mod, "_do_build_container"),
            patch.object(
                cli_mod, "build_kernel", return_value={"ok": True}
            ) as bk,
            patch.object(cli_mod, "build_image") as bi,
        ):
            rc = _run_main(
                ["build", "all", "rocky9",
                 "--variant", "mofed-24",
                 "--lustre-tree", str(lustre_tree)]
            )
        assert rc == EXIT_OK
        _, kwargs = bk.call_args
        # Pinned kernel must reach build_kernel and build_image.
        assert kwargs.get("kernel") == "5.14-rhel9.5"
        _, ikwargs = bi.call_args
        assert ikwargs.get("kernel") == "5.14-rhel9.5"

    def test_build_image_uses_variant_pinned_kernel(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        from ltvm_pkg import cli as cli_mod

        _add_mofed_variant(tmp_targets)
        tc = _make_tc(tmp_targets, variant="mofed-24")
        # Pre-populate matching staging dir under the pinned kernel.
        lt = tmp_path / "tree"
        staging = (
            lt / ".ltvm-staging" / "rocky9" / "x86_64"
            / "5.14-rhel9.5" / "mofed-24"
        )
        staging.mkdir(parents=True)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "_gate_lustre_validation"),
            patch.object(cli_mod, "build_image") as bi,
        ):
            bi.return_value = Path("/fake/base.ext4")
            rc = _run_main(
                ["build", "image", "rocky9", "--variant", "mofed-24",
                 "--lustre-tree", str(lt)]
            )
        assert rc == EXIT_OK
        _, ikwargs = bi.call_args
        # cmd_build_image passes kernel=None down (resolution happens in
        # build_image), but our resolved-kernel hint is used to find
        # staging -- verify staging lookup found the variant subdir.
        assert ikwargs.get("with_lustre") == str(lt)

    def test_build_image_missing_variant_staging_errors(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        """When --variant is set but staging is at the base (non-variant)
        path, build-image must report "no Lustre staging" rather than
        silently baking the wrong (or empty) staging."""
        from ltvm_pkg import cli as cli_mod

        _add_mofed_variant(tmp_targets)
        tc = _make_tc(tmp_targets, variant="mofed-24")
        lt = tmp_path / "tree"
        # Staging exists but at the BASE path, not the variant path.
        base_staging = (
            lt / ".ltvm-staging" / "rocky9" / "x86_64" / "5.14-rhel9.5"
        )
        base_staging.mkdir(parents=True)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "_gate_lustre_validation"),
            patch.object(cli_mod, "build_image") as bi,
        ):
            rc = _run_main(
                ["build", "image", "rocky9", "--variant", "mofed-24",
                 "--lustre-tree", str(lt)]
            )
        assert rc != EXIT_OK
        err = capsys.readouterr().err
        assert "no Lustre staging" in err
        bi.assert_not_called()


# ---------------------------------------------------------------------------
# cmd_status output formatting
# ---------------------------------------------------------------------------


class TestCmdStatusFormat:
    def test_text_table_has_expected_columns(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        """The text-mode status table includes Target/Container/Kernel/
        Image-Kernel/Variant/Image columns and one row per kernel."""
        import ltvm_pkg.target_config as cfg
        from ltvm_pkg import cli as cli_mod

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg, "TARGETS_YAML",
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
                return_value={
                    "built": False, "stale": True,
                    "kernel": "5.14-rhel9.7", "variant": "base",
                },
            ),
        ):
            rc = cli_mod.cmd_status(argparse.Namespace(json=False))
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        for col in ("Target", "Container", "Kernel",
                    "Image-Kernel", "Variant", "Image"):
            assert col in out
        # The single configured target shows up as a row.
        assert "rocky9" in out

    def test_status_emits_variant_image_rows(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        """When a non-base variant has a built image on disk, status emits
        an extra row for it under the same kernel."""
        import ltvm_pkg.target_config as cfg

        _add_mofed_variant(tmp_targets)
        # Build a real TargetConfig to drive image_output_dir math.
        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(
                cfg, "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            tc = cfg.TargetConfig("rocky9")

        # Pre-create a kernel dir + a mofed-24 variant base.ext4.
        kdir = (
            tmp_targets / "output" / "rocky9" / "x86_64" / "kernels"
            / "5.14-rhel9.5-5.14.0-503.26.1"
        )
        kdir.mkdir(parents=True)
        variant_image = (
            tmp_targets / "output" / "rocky9" / "x86_64" / "images"
            / "5.14-rhel9.5-5.14.0-503.26.1" / "mofed-24" / "base.ext4"
        )
        variant_image.parent.mkdir(parents=True)
        variant_image.write_bytes(b"")

        from ltvm_pkg import cli as cli_mod

        def _img_status(tcarg: Any, *, kernel: str, variant: str) -> Any:
            return {
                "built": variant != "base",
                "stale": False,
                "kernel": kernel,
                "variant": variant,
            }

        with (
            patch("ltvm_pkg.cli.list_targets", return_value=["rocky9"]),
            patch("ltvm_pkg.cli.TargetConfig", return_value=tc),
            patch(
                "ltvm_pkg.cli.kernel_status",
                return_value={"built": False, "stale": True},
            ),
            patch("ltvm_pkg.cli.image_status", side_effect=_img_status),
        ):
            rc = cli_mod.cmd_status(argparse.Namespace(json=True))
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        images = payload["rocky9"]["images"]
        variants = {(img["kernel"], img["variant"]) for img in images}
        # Variant row only appears for the kernel that has it on disk.
        assert ("5.14-rhel9.5-5.14.0-503.26.1", "mofed-24") in variants
        # Base row always appears for that same kernel.
        assert ("5.14-rhel9.5-5.14.0-503.26.1", "base") in variants

    def test_status_skips_planned_targets(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        """A target name that fails TargetConfig() (e.g. planned/disabled)
        is silently skipped instead of crashing the whole status."""
        from ltvm_pkg import cli as cli_mod

        def _tc_factory(name: str) -> Any:
            if name == "broken":
                raise ValueError("planned target")
            # Real one for rocky9
            import ltvm_pkg.target_config as cfg
            with (
                patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
                patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
                patch.object(
                    cfg, "TARGETS_YAML",
                    tmp_targets / "targets" / "targets.yaml",
                ),
            ):
                return cfg.TargetConfig("rocky9")

        with (
            patch("ltvm_pkg.cli.list_targets",
                  return_value=["broken", "rocky9"]),
            patch("ltvm_pkg.cli.TargetConfig", side_effect=_tc_factory),
            patch(
                "ltvm_pkg.cli.kernel_status",
                return_value={"built": False, "stale": True},
            ),
            patch(
                "ltvm_pkg.cli.image_status",
                return_value={"built": False, "stale": True,
                              "kernel": "k", "variant": "base"},
            ),
        ):
            rc = cli_mod.cmd_status(argparse.Namespace(json=True))
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert "rocky9" in payload
        assert "broken" not in payload


# ---------------------------------------------------------------------------
# cmd_clean -- additional scoping
# ---------------------------------------------------------------------------


class TestCmdCleanScoping:
    def test_arch_and_all_arches_mutually_exclusive(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        """Passing --arch with --all-arches is rejected even if the dirs
        themselves would otherwise be ambiguous."""
        import ltvm_pkg.cli as cli_mod
        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", tmp_targets / "output"),
            patch.object(cli_mod, "TargetConfig", cfg.TargetConfig),
            patch.object(
                cfg, "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            args = argparse.Namespace(
                target="rocky9", arch="aarch64",
                all_arches=True, json=False,
            )
            rc = cli_mod.cmd_clean(args)
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "mutually exclusive" in err

    def test_clean_with_arch_scopes_to_arch_dir(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        """`--arch aarch64` wipes only output/<target>/aarch64/."""
        import ltvm_pkg.cli as cli_mod
        import ltvm_pkg.target_config as cfg

        out = tmp_targets / "output"
        (out / "rocky9" / "x86_64").mkdir(parents=True, exist_ok=True)
        (out / "rocky9" / "x86_64" / "keep.txt").write_text("keep")
        (out / "rocky9" / "aarch64").mkdir(parents=True)
        (out / "rocky9" / "aarch64" / "drop.txt").write_text("drop")

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", out),
            patch.object(cli_mod, "TargetConfig", cfg.TargetConfig),
            patch.object(
                cfg, "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            args = argparse.Namespace(
                target="rocky9", arch="aarch64",
                all_arches=False, json=False,
            )
            rc = cli_mod.cmd_clean(args)
        assert rc == EXIT_OK
        # Only aarch64 wiped; x86_64 untouched.
        assert not (out / "rocky9" / "aarch64").exists()
        assert (out / "rocky9" / "x86_64" / "keep.txt").exists()

    def test_clean_unknown_target_returns_not_found(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        from ltvm_pkg.cli import EXIT_NOT_FOUND

        rc = _run_main(["target", "clean", "no_such_target"])
        assert rc == EXIT_NOT_FOUND

    def test_clean_json_text_format_consistent(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        """JSON output of cmd_clean carries target/arch/all_arches/wiped."""
        import ltvm_pkg.cli as cli_mod
        import ltvm_pkg.target_config as cfg

        out = tmp_targets / "output"
        (out / "rocky9" / "x86_64").mkdir(parents=True, exist_ok=True)
        (out / "rocky9" / "x86_64" / "f.txt").write_text("x")

        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "OUTPUT_DIR", out),
            patch.object(cli_mod, "TargetConfig", cfg.TargetConfig),
            patch.object(
                cfg, "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            args = argparse.Namespace(
                target="rocky9", arch=None,
                all_arches=False, json=True,
            )
            rc = cli_mod.cmd_clean(args)
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["target"] == "rocky9"
        assert payload["arch"] == "x86_64"
        assert payload["all_arches"] is False
        assert payload["wiped"][0]["removed"] is True
        assert payload["wiped"][0]["bytes"] >= 1
