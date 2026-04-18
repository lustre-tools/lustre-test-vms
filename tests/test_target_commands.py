"""Coverage for the four target meta-commands in ltvm_pkg/cli.py:

  cmd_targets       (`ltvm target list`)
  cmd_target_show   (`ltvm target show`)
  cmd_target_export (`ltvm target export`)
  cmd_validate      (`ltvm target validate`)

These commands are mostly presentation/format logic.  Refactoring
``cli.py`` into smaller modules can silently break:

  * the JSON shape consumers script against
    (`is_default`, `built`, `local_release`, ...),
  * the kernel/variant header convention introduced by
    ``b458662`` and ``9097251`` (one Variants column, ``base`` is
    explicit, default mark only on the kernel header row),
  * the variant kernel-pin filter from ``fa38bac``
    (mofed-24 only surfaces under its pinned kernel),
  * the ``cmd_validate`` exit-code mapping
    (refuse=EXIT_ERROR, error=EXIT_NOT_FOUND, --force-compat
    flips refuse->EXIT_OK but not error),
  * the ``cmd_target_export`` argument-validation order
    (root check first, then target lookup, then format/overwrite).

The tests stub out network (``_gh_api``) and disk-touching
helpers so they stay fast and hermetic.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from ltvm_pkg import cli
from ltvm_pkg.cli import (
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    cmd_target_export,
    cmd_target_show,
    cmd_targets,
    cmd_validate,
)
from ltvm_pkg.lustre_compat import ValidationResult
from ltvm_pkg.target_config import LustreMode


# ---------------------------------------------------------------------------
# Targets-yaml fixture variants (rocky9 with a pinned mofed-24 variant)
# ---------------------------------------------------------------------------


def _yaml_with_variant() -> dict:
    """rocky9 with two declared kernels and a mofed-24 variant pinned
    to the second kernel."""
    return {
        "defaults": {"arch": "x86_64", "os_family": "rhel"},
        "targets": {
            "rocky9": {
                "os_name": "rocky",
                "os_version": "9.7",
                "container_image": "rockylinux:9.7",
                "srpm_url": "https://example.invalid/k",
                "status": "working",
                "kernels": {
                    "default": "5.14-rhel9.7",
                    "available": ["5.14-rhel9.7", "5.14-rhel9.5"],
                },
                "lustre": {"mode": "server_ldiskfs"},
                "variants": {
                    "mofed-24": {
                        # Overlay paths are validated lazily; using a
                        # nonexistent path here keeps the fixture
                        # minimal -- TargetConfig only stores the path.
                        "container_overlay": (
                            "rocky9/variants/mofed-24.container.Dockerfile"
                        ),
                        "image_overlay": (
                            "rocky9/variants/mofed-24.image.Dockerfile"
                        ),
                        "kernel": "5.14-rhel9.5",
                        "params": {"mofed_version": "24.10-2.1.8.0"},
                    },
                },
            },
        },
    }


def _yaml_experimental() -> dict:
    """rocky9 marked status=experimental.  cmd_targets must annotate
    the target with a ``*`` and emit the legend footer."""
    data = _yaml_with_variant()
    data["targets"]["rocky9"]["status"] = "experimental"
    return data


@pytest.fixture
def variant_targets(tmp_targets: Path) -> Path:
    """tmp_targets, but with mofed-24 declared as a pinned variant."""
    (tmp_targets / "targets" / "targets.yaml").write_text(
        yaml.dump(_yaml_with_variant(), default_flow_style=False)
    )
    # The variant overlay paths reference rocky9/variants/...; create
    # them as empty files so any code that probes them doesn't trip.
    variants_dir = tmp_targets / "targets" / "rocky9" / "variants"
    variants_dir.mkdir(parents=True, exist_ok=True)
    (variants_dir / "mofed-24.container.Dockerfile").write_text("# stub\n")
    (variants_dir / "mofed-24.image.Dockerfile").write_text("# stub\n")
    return tmp_targets


@pytest.fixture
def experimental_targets(tmp_targets: Path) -> Path:
    """tmp_targets with rocky9 set to status=experimental."""
    (tmp_targets / "targets" / "targets.yaml").write_text(
        yaml.dump(_yaml_experimental(), default_flow_style=False)
    )
    variants_dir = tmp_targets / "targets" / "rocky9" / "variants"
    variants_dir.mkdir(parents=True, exist_ok=True)
    (variants_dir / "mofed-24.container.Dockerfile").write_text("# stub\n")
    (variants_dir / "mofed-24.image.Dockerfile").write_text("# stub\n")
    return tmp_targets


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_cfg_paths(tmp_targets: Path) -> Any:
    """Return a context manager that retargets target_config's module
    constants at the fixture's tmp dir."""
    import ltvm_pkg.target_config as cfg
    from contextlib import ExitStack

    es = ExitStack()
    es.enter_context(patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"))
    es.enter_context(patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"))
    es.enter_context(
        patch.object(
            cfg, "TARGETS_YAML",
            tmp_targets / "targets" / "targets.yaml",
        )
    )
    return es


def _ns(**kw: Any) -> argparse.Namespace:
    """Minimal argparse.Namespace with the common fields these
    commands look for, plus any caller-supplied overrides."""
    base: dict[str, Any] = {
        "json": False,
        "target": "rocky9",
        "arch": None,
        "variant": "base",
        "kernel": None,
        "mofed_version": None,
        "force_compat": False,
        "lustre_tree": None,
        "force": False,
        "format": "qcow2",
        "output": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


# ===========================================================================
# cmd_targets -- `ltvm target list`
# ===========================================================================


class TestCmdTargetsJsonShape:
    """Refactor-safety: every JSON consumer expects this exact key
    set on every row.  Renaming any of these silently breaks
    scripts that scrape ``ltvm target list --json``."""

    def test_json_top_level_is_list(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("no net")):
            rc = cmd_targets(_ns(json=True))
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        # The historical contract: top-level is a flat list of rows,
        # not a dict keyed by target.  Splitting cli.py must not
        # turn this into {"targets": [...]} (cmd_status's shape).
        assert isinstance(payload, list)
        assert all(isinstance(r, dict) for r in payload)

    def test_json_row_keys(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("no net")):
            cmd_targets(_ns(json=True))
        payload = json.loads(capsys.readouterr().out)
        # Every row -- header or variant -- carries this contract.
        expected = {
            "name", "arch", "status", "kernel", "variant",
            "is_default", "server", "default_kernel", "lustre_mode",
            "available", "built", "local_release", "remote_release",
        }
        for row in payload:
            assert expected.issubset(row.keys()), (
                f"row missing keys: {expected - set(row)}; row={row}"
            )

    def test_one_default_per_target(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        """Exactly one row per target has ``is_default=True`` and it
        is the kernel-header row (variant=None) for the default
        kernel.  Variant rows under the default kernel must NOT
        carry the default mark."""
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("no net")):
            cmd_targets(_ns(json=True))
        rows = json.loads(capsys.readouterr().out)
        defaults = [r for r in rows if r["is_default"]]
        assert len(defaults) == 1, (
            f"expected exactly one is_default row, got {len(defaults)}"
        )
        d = defaults[0]
        assert d["variant"] is None, (
            "is_default should sit on the kernel-header row, not a "
            f"variant row; got variant={d['variant']!r}"
        )
        assert d["kernel"] == "5.14-rhel9.7"

    def test_pinned_variant_skipped_under_other_kernels(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        """mofed-24 is pinned to 5.14-rhel9.5, so it must NOT appear
        as a variant row under 5.14-rhel9.7.  Regression guard for
        applicable_kernels() integration."""
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("no net")):
            cmd_targets(_ns(json=True))
        rows = json.loads(capsys.readouterr().out)
        rhel97_variants = {
            r["variant"] for r in rows
            if r["kernel"] == "5.14-rhel9.7" and r["variant"] is not None
        }
        rhel95_variants = {
            r["variant"] for r in rows
            if r["kernel"] == "5.14-rhel9.5" and r["variant"] is not None
        }
        assert "mofed-24" not in rhel97_variants
        assert "mofed-24" in rhel95_variants
        # base must always be present alongside any pinned variant.
        assert "base" in rhel97_variants
        assert "base" in rhel95_variants

    def test_header_row_has_blank_local_remote(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        """Per-kernel header rows carry no local/remote info -- those
        belong to the variant rows below."""
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("no net")):
            cmd_targets(_ns(json=True))
        rows = json.loads(capsys.readouterr().out)
        headers = [r for r in rows if r["variant"] is None]
        assert headers, "expected at least one kernel-header row"
        for h in headers:
            assert h["local_release"] == "-"
            assert h["remote_release"] == "-"
            assert h["available"] == ""
            assert h["built"] is False

    def test_unreachable_remote_marks_question_mark(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        """When _gh_api raises, every variant row must report
        remote_release='?' (not '-' -- that would mean 'no release
        available' rather than 'we don't know').  Scripts use this
        to retry vs. give up."""
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("no net")):
            cmd_targets(_ns(json=True))
        rows = json.loads(capsys.readouterr().out)
        variant_rows = [r for r in rows if r["variant"] is not None]
        assert variant_rows
        assert all(r["remote_release"] == "?" for r in variant_rows)


def _seed_image_meta(
    tmp_targets: Path,
    target: str,
    kernel: str,
    *,
    variant: str | None = None,
    with_lustre: object = None,
    lustre_version: object = None,
) -> None:
    """Seed a minimal image meta.json on disk.

    Mirrors the real layout:
        artifacts/<target>/<arch>/images/<kernel>/[<variant>/]meta.json
    Callers control whether the image declares Lustre (`with_lustre`
    and `lustre_version` -- None mimics the --no-lustre build path).
    Also seeds the paired kernel meta so cmd_targets sees the kernel
    as built (its `built=` path for the base row reads the kernel
    meta, not the image meta).
    """
    kdir = (
        tmp_targets / "artifacts" / target / "x86_64" / "kernels" / kernel
    )
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "meta.json").write_text("{}")
    img_dir = (
        tmp_targets / "artifacts" / target / "x86_64" / "images" / kernel
    )
    if variant and variant != "base":
        img_dir = img_dir / variant
    img_dir.mkdir(parents=True, exist_ok=True)
    (img_dir / "meta.json").write_text(
        json.dumps({
            "target": target,
            "input_hash": "x" * 16,
            "with_lustre": with_lustre,
            "lustre_version": lustre_version,
        })
    )


class TestCmdTargetsLustreMissing:
    """A built image with no Lustre baked in (the --no-lustre build
    path or a pre-Lustre-staging fetch) produces VMs that can't mount
    Lustre.  cmd_targets flags these with `yes*` on the Local column
    and includes a legend footer explaining the marker."""

    def test_json_lustre_missing_flagged(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        # Seed a built image WITHOUT lustre, and a second one WITH.
        _seed_image_meta(
            variant_targets, "rocky9", "5.14-rhel9.7",
            with_lustre=None, lustre_version=None,  # --no-lustre build
        )
        _seed_image_meta(
            variant_targets, "rocky9", "5.14-rhel9.5",
            with_lustre="/some/tree",
            lustre_version="2.8.0",
        )
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("net")):
            cmd_targets(_ns(json=True))
        rows = json.loads(capsys.readouterr().out)
        by_key = {
            (r["kernel"], r["variant"]): r for r in rows
            if r["variant"] is not None
        }
        # rhel9.7 base row: lustre missing
        assert by_key[("5.14-rhel9.7", "base")]["lustre_missing"] is True
        # rhel9.5 base row: lustre present
        assert by_key[("5.14-rhel9.5", "base")]["lustre_missing"] is False

    def test_text_check_star_marker(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        """The Local column shows `\u2713*` for no-lustre images and the
        legend footer explains it.  `\u2713` (plain) for good images."""
        _seed_image_meta(
            variant_targets, "rocky9", "5.14-rhel9.7",
            with_lustre=None, lustre_version=None,
        )
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("net")):
            cmd_targets(_ns(json=False))
        out = capsys.readouterr().out
        # `✓*` (Local column) for the no-lustre row.
        assert "\u2713*" in out
        # Legend footer must explain the marker so the user can act.
        assert "\u2713* = image does NOT have Lustre baked in" in out

    def test_text_no_marker_when_lustre_present(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        _seed_image_meta(
            variant_targets, "rocky9", "5.14-rhel9.7",
            with_lustre="/x", lustre_version="2.8.0",
        )
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("net")):
            cmd_targets(_ns(json=False))
        out = capsys.readouterr().out
        assert "\u2713*" not in out  # no false positive


class TestCmdTargetsTextOutput:
    """Refactor-safety: the text table is the human-facing surface
    of `target list`.  Column order, presence of the legend, and
    the convention that ``yes`` appears once per target on the
    kernel header row are easy to break unintentionally."""

    def test_header_columns(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("no net")):
            cmd_targets(_ns(json=False))
        out = capsys.readouterr().out
        first_line = out.splitlines()[0]
        # b458662 folded Kernel into Variants and renamed the column.
        assert "Variants" in first_line
        assert "Default?" in first_line
        assert "Local" in first_line
        assert "Remote" in first_line
        # The old 'Kernel' header would mean somebody reverted the merge.
        assert "Kernel" not in first_line.split("Variants")[0]

    def test_default_check_only_once_per_target(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        """``\u2713`` in the Default column must appear once -- on the
        default kernel's header row, not on any variant row."""
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("no net")):
            cmd_targets(_ns(json=False))
        out = capsys.readouterr().out
        # Lines that end with "\u2713" (Default? column).  Variant rows
        # can carry a \u2713 in the Local column too, but the Default?
        # check sits at the end of the line.
        yes_lines = [
            ln for ln in out.splitlines() if ln.rstrip().endswith("\u2713")
        ]
        assert len(yes_lines) == 1, (
            f"expected exactly one '\u2713' default marker; got "
            f"{len(yes_lines)}: {yes_lines}"
        )
        # And it should be on the *kernel header* row -- which holds
        # the kernel name unindented in the Variants column, not a
        # leading "  base"/"  mofed-24" indent.
        line = yes_lines[0]
        assert "5.14-rhel9.7" in line
        # variant indent is two leading spaces inside the Variants
        # column; the kernel header has no such indent.
        assert "  base" not in line
        assert "  mofed-24" not in line

    def test_variants_indent_under_kernel(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        """``base`` and any declared variants render as indented rows
        under their kernel header (9097251)."""
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("no net")):
            cmd_targets(_ns(json=False))
        out = capsys.readouterr().out
        # The Variants column shows "  base" / "  mofed-24" with a
        # leading indent.  Search for those tokens with their indent
        # somewhere on the line.
        assert "  base" in out
        assert "  mofed-24" in out

    def test_no_targets_prints_friendly_message(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with patch.object(cli, "list_targets", return_value=[]), \
                patch.object(cli, "_gh_api", side_effect=Exception("net")):
            rc = cmd_targets(_ns(json=False))
        assert rc == EXIT_OK
        assert "No targets" in capsys.readouterr().out

    def test_no_targets_json_is_empty_list(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """JSON shape must stay [] (empty list), not {} or null --
        scripts iterate with ``for row in payload``."""
        with patch.object(cli, "list_targets", return_value=[]), \
                patch.object(cli, "_gh_api", side_effect=Exception("net")):
            rc = cmd_targets(_ns(json=True))
        assert rc == EXIT_OK
        assert json.loads(capsys.readouterr().out) == []

    def test_experimental_marker_and_legend(
        self,
        capsys: pytest.CaptureFixture[str],
        experimental_targets: Path,
    ) -> None:
        """An experimental target shows ``*`` next to its name and
        the legend footer is emitted."""
        with _patch_cfg_paths(experimental_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("no net")):
            cmd_targets(_ns(json=False))
        out = capsys.readouterr().out
        assert "rocky9*" in out
        assert "experimental" in out

    def test_unreachable_legend(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        """When the remote is unreachable, the ``?`` legend is shown."""
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("no net")):
            cmd_targets(_ns(json=False))
        out = capsys.readouterr().out
        assert "github unreachable" in out

    def test_target_load_error_surfaces_in_table(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """If TargetConfig raises ValueError for a listed target, its
        row contains an 'error: ...' message rather than aborting
        the whole listing."""
        with patch.object(cli, "list_targets", return_value=["bogus"]), \
                patch.object(
                    cli, "TargetConfig",
                    side_effect=ValueError("bad yaml"),
                ), \
                patch.object(cli, "_gh_api", side_effect=Exception("net")):
            rc = cmd_targets(_ns(json=False))
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "bogus" in out
        assert "error" in out


# ===========================================================================
# cmd_target_show -- `ltvm target show <name>`
# ===========================================================================


class TestCmdTargetShow:
    def test_json_payload_top_keys(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("net")):
            rc = cmd_target_show(_ns(json=True))
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        expected_top = {
            "name", "status", "arch", "os_family", "os_name",
            "os_version", "container_image", "lustre_mode",
            "default_mem", "default_kernel", "kernels", "output_dir",
        }
        assert expected_top.issubset(payload.keys())
        assert payload["name"] == "rocky9"
        assert payload["lustre_mode"] == "server_ldiskfs"
        assert payload["default_kernel"] == "5.14-rhel9.7"

    def test_json_kernels_list_shape(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("net")):
            cmd_target_show(_ns(json=True))
        payload = json.loads(capsys.readouterr().out)
        ks = payload["kernels"]
        assert isinstance(ks, list)
        assert len(ks) == 2  # rhel9.7 + rhel9.5
        for k in ks:
            assert {
                "kernel", "is_default", "available",
                "built", "local_release", "remote_release",
            }.issubset(k.keys())
        defaults = [k for k in ks if k["is_default"]]
        assert len(defaults) == 1
        assert defaults[0]["kernel"] == "5.14-rhel9.7"

    def test_json_built_reflects_meta_on_disk(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        """A kernel with a meta.json on disk reports built=True;
        an unbuilt kernel reports built=False."""
        # Pre-populate kernel meta for the default kernel only.
        kdir = (
            variant_targets / "artifacts" / "rocky9" / "x86_64"
            / "kernels" / "5.14-rhel9.7"
        )
        kdir.mkdir(parents=True)
        (kdir / "meta.json").write_text("{}")

        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("net")):
            cmd_target_show(_ns(json=True))
        payload = json.loads(capsys.readouterr().out)
        by_name = {k["kernel"]: k for k in payload["kernels"]}
        assert by_name["5.14-rhel9.7"]["built"] is True
        assert by_name["5.14-rhel9.7"]["available"] == "ready"
        assert by_name["5.14-rhel9.5"]["built"] is False
        # No remote data either, so available is "build".
        assert by_name["5.14-rhel9.5"]["available"] == "build"

    def test_text_output_contains_default_marker(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("net")):
            rc = cmd_target_show(_ns(json=False))
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        # Header lines.
        assert "target:" in out
        assert "rocky9" in out
        assert "arch:" in out
        assert "os:" in out
        assert "container image:" in out
        # The default kernel is annotated; the non-default isn't.
        assert "(default)" in out
        # Both kernels listed.
        assert "5.14-rhel9.7" in out
        assert "5.14-rhel9.5" in out

    def test_unknown_target_returns_not_found(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("net")):
            rc = cmd_target_show(_ns(target="no_such_target"))
        # _load_target maps ValueError -> EXIT_NOT_FOUND.
        assert rc == EXIT_NOT_FOUND

    def test_json_omits_local_remote_keys_when_unbuilt(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        """For an unbuilt kernel with no remote release, both
        local_release and remote_release should be the sentinel '-'
        (not missing -- consumers want a stable shape)."""
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli, "_gh_api", side_effect=Exception("net")):
            cmd_target_show(_ns(json=True))
        payload = json.loads(capsys.readouterr().out)
        for k in payload["kernels"]:
            # local default is '-' (no .ltvm-release-tag); remote
            # is '?' when _gh_api raised (network unreachable).
            assert k["local_release"] == "-"
            assert k["remote_release"] == "?"


# ===========================================================================
# cmd_validate -- `ltvm target validate`
# ===========================================================================


def _vresult(status: str, message: str = "msg") -> ValidationResult:
    return ValidationResult(
        status=status,  # type: ignore[arg-type]
        mode=LustreMode.SERVER_LDISKFS,
        kernel_version="5.14.0-test",
        matched_in=None,
        message=message,
    )


class TestCmdValidateExitCodes:
    """The exit-code mapping is part of the public CLI contract:
    scripts read $? to decide whether a build can proceed.  Any
    refactor that swaps these around silently breaks CI.

      ok           -> 0   (EXIT_OK)
      best_effort  -> 0   (EXIT_OK; warning-only)
      refuse       -> 1   (EXIT_ERROR; overridable with --force-compat)
      error        -> 2   (EXIT_NOT_FOUND; NOT overridable)
    """

    def _run(
        self,
        tmp_targets: Path,
        lustre_tree: Path,
        status: str,
        force: bool = False,
        use_json: bool = False,
    ) -> tuple[int, str]:
        with _patch_cfg_paths(tmp_targets), \
                patch.object(
                    cli, "validate_target",
                    return_value=_vresult(status, "msg"),
                ):
            ns = _ns(
                json=use_json,
                lustre_tree=str(lustre_tree),
                force_compat=force,
            )
            rc = cmd_validate(ns)
        return rc, ""

    def test_ok(self, tmp_targets: Path, lustre_tree: Path) -> None:
        rc, _ = self._run(tmp_targets, lustre_tree, "ok")
        assert rc == EXIT_OK

    def test_best_effort_is_ok(
        self, tmp_targets: Path, lustre_tree: Path,
    ) -> None:
        rc, _ = self._run(tmp_targets, lustre_tree, "best_effort")
        assert rc == EXIT_OK

    def test_refuse_returns_error(
        self, tmp_targets: Path, lustre_tree: Path,
    ) -> None:
        rc, _ = self._run(tmp_targets, lustre_tree, "refuse")
        assert rc == EXIT_ERROR

    def test_refuse_with_force_returns_ok(
        self, tmp_targets: Path, lustre_tree: Path,
    ) -> None:
        """--force-compat downgrades a refuse to EXIT_OK so the next
        build step can proceed."""
        rc, _ = self._run(tmp_targets, lustre_tree, "refuse", force=True)
        assert rc == EXIT_OK

    def test_error_returns_not_found(
        self, tmp_targets: Path, lustre_tree: Path,
    ) -> None:
        """``error`` (parse/IO failure) is intentionally distinct
        from ``refuse`` (Lustre says no): scripts retry the former,
        not the latter."""
        rc, _ = self._run(tmp_targets, lustre_tree, "error")
        assert rc == EXIT_NOT_FOUND

    def test_error_not_overridable_by_force(
        self, tmp_targets: Path, lustre_tree: Path,
    ) -> None:
        """--force-compat must NOT silence ``error`` -- those are
        IO/parse problems and a build attempt would fail anyway."""
        rc, _ = self._run(tmp_targets, lustre_tree, "error", force=True)
        assert rc == EXIT_NOT_FOUND


class TestCmdValidateOutput:
    def test_json_payload_keys(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        with _patch_cfg_paths(tmp_targets), \
                patch.object(
                    cli, "validate_target",
                    return_value=_vresult("ok", "all good"),
                ):
            cmd_validate(_ns(json=True, lustre_tree=str(lustre_tree)))
        payload = json.loads(capsys.readouterr().out)
        assert set(payload) == {
            "status", "mode", "kernel_version", "matched_in", "message",
        }
        assert payload["status"] == "ok"
        assert payload["message"] == "all good"
        # mode is serialised via .value, not the Enum repr.
        assert payload["mode"] == "server_ldiskfs"

    def test_text_status_tag(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        """Text output prefixes the message with [status]."""
        with _patch_cfg_paths(tmp_targets), \
                patch.object(
                    cli, "validate_target",
                    return_value=_vresult("ok", "compatible"),
                ):
            cmd_validate(_ns(lustre_tree=str(lustre_tree)))
        out = capsys.readouterr().out
        assert "[ok]" in out
        assert "compatible" in out

    def test_text_force_override_message(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        """When --force-compat overrides a refuse, the line is
        prefixed with ``--force-compat:`` so logs make the
        override obvious."""
        with _patch_cfg_paths(tmp_targets), \
                patch.object(
                    cli, "validate_target",
                    return_value=_vresult("refuse", "bad combo"),
                ):
            cmd_validate(
                _ns(lustre_tree=str(lustre_tree), force_compat=True)
            )
        out = capsys.readouterr().out
        assert "--force-compat" in out
        assert "bad combo" in out

    def test_invalid_lustre_tree_returns_error(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        """If --lustre-tree points at a directory without
        lustre/kernel_patches/, _resolve_lustre_tree returns an
        error message and cmd_validate maps that to EXIT_ERROR."""
        bogus = tmp_path / "not-a-lustre-tree"
        bogus.mkdir()
        with _patch_cfg_paths(tmp_targets):
            rc = cmd_validate(_ns(lustre_tree=str(bogus)))
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "lustre" in err.lower() or "kernel_patches" in err

    def test_unknown_target_returns_not_found(
        self,
        tmp_targets: Path,
        lustre_tree: Path,
    ) -> None:
        with _patch_cfg_paths(tmp_targets):
            rc = cmd_validate(
                _ns(target="bogus", lustre_tree=str(lustre_tree))
            )
        assert rc == EXIT_NOT_FOUND


# ===========================================================================
# cmd_target_export -- `ltvm target export`
# ===========================================================================


class TestCmdTargetExport:
    """cmd_target_export wraps image_export.export_image with the
    standard CLI surface (root check, target lookup, output-path
    defaulting, error mapping).  All disk work is mocked -- the
    image_export internals are exercised in test_image_export.py."""

    def test_requires_root_first(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
    ) -> None:
        """Non-root invocation must be rejected before any target
        lookup happens (so a typoed target name as non-root still
        prints the friendly 'needs sudo' hint)."""
        with patch.object(cli.os, "getuid", return_value=1000):
            rc = cmd_target_export(_ns(target="anything"))
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "root" in err.lower()

    def test_requires_root_json_envelope(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """JSON mode reports the root requirement as a JSON error."""
        with patch.object(cli.os, "getuid", return_value=1000):
            rc = cmd_target_export(_ns(target="anything", json=True))
        assert rc == EXIT_ERROR
        payload = json.loads(capsys.readouterr().err)
        assert "error" in payload

    def test_unknown_target_returns_not_found(
        self,
        variant_targets: Path,
    ) -> None:
        with _patch_cfg_paths(variant_targets), \
                patch.object(cli.os, "getuid", return_value=0):
            rc = cmd_target_export(_ns(target="no_such_target"))
        assert rc == EXIT_NOT_FOUND

    def test_overwrite_guard_maps_to_error_with_hint(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
        tmp_path: Path,
    ) -> None:
        """A FileExistsError from export_image becomes EXIT_ERROR
        with the ``--force`` hint in the user-visible output."""
        out_file = tmp_path / "exists.qcow2"
        out_file.write_text("preexisting")

        def boom(*a: Any, **kw: Any) -> None:
            raise FileExistsError(f"refuse to overwrite {out_file}")

        with _patch_cfg_paths(variant_targets), \
                patch.object(cli.os, "getuid", return_value=0), \
                patch(
                    "ltvm_pkg.image_export.export_image",
                    side_effect=boom,
                ):
            rc = cmd_target_export(
                _ns(output=str(out_file), force=False)
            )
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "--force" in err

    def test_runtime_error_from_export_returns_error(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
        tmp_path: Path,
    ) -> None:
        """RuntimeError / FileNotFoundError / ValueError from
        export_image are captured and re-emitted as EXIT_ERROR."""
        out_file = tmp_path / "out.qcow2"

        with _patch_cfg_paths(variant_targets), \
                patch.object(cli.os, "getuid", return_value=0), \
                patch(
                    "ltvm_pkg.image_export.export_image",
                    side_effect=RuntimeError("parted blew up"),
                ):
            rc = cmd_target_export(_ns(output=str(out_file)))
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "parted blew up" in err

    def test_default_output_path_uses_image_dir(
        self,
        variant_targets: Path,
        tmp_path: Path,
    ) -> None:
        """When --output is not passed, the export defaults to
        ``artifacts/<target>/<arch>/images/<kernel>/bootable-<kernel>.<ext>``.
        The path the CLI passes to export_image must match that
        layout, otherwise ``ltvm target list`` won't find the file
        for accounting."""
        # Create the image dir + a fake bootable file at the expected
        # path so the post-export stat() succeeds.
        kname = "5.14-rhel9.7"
        img_dir = (
            variant_targets / "artifacts" / "rocky9" / "x86_64"
            / "images" / kname
        )
        img_dir.mkdir(parents=True)
        expected = img_dir / f"bootable-{kname}.qcow2"
        expected.write_bytes(b"\0" * 1024)

        captured: dict[str, Path] = {}

        def fake_export(tc: Any, kernel: Any, out: Path, **kw: Any) -> Path:
            captured["out"] = out
            return out

        with _patch_cfg_paths(variant_targets), \
                patch.object(cli.os, "getuid", return_value=0), \
                patch(
                    "ltvm_pkg.image_export.export_image",
                    side_effect=fake_export,
                ):
            rc = cmd_target_export(_ns(json=True))
        assert rc == EXIT_OK
        assert captured["out"].name == f"bootable-{kname}.qcow2"
        assert captured["out"].parent == img_dir.resolve()

    def test_format_raw_changes_extension(
        self,
        variant_targets: Path,
    ) -> None:
        """--format raw must default the output extension to .raw,
        not .qcow2."""
        kname = "5.14-rhel9.7"
        img_dir = (
            variant_targets / "artifacts" / "rocky9" / "x86_64"
            / "images" / kname
        )
        img_dir.mkdir(parents=True)
        expected = img_dir / f"bootable-{kname}.raw"
        expected.write_bytes(b"\0" * 1024)

        captured: dict[str, Path] = {}

        def fake_export(tc: Any, kernel: Any, out: Path, **kw: Any) -> Path:
            captured["out"] = out
            captured["fmt"] = kw.get("image_format")
            return out

        with _patch_cfg_paths(variant_targets), \
                patch.object(cli.os, "getuid", return_value=0), \
                patch(
                    "ltvm_pkg.image_export.export_image",
                    side_effect=fake_export,
                ):
            cmd_target_export(_ns(format="raw"))
        assert captured["out"].name.endswith(".raw")
        assert captured["fmt"] == "raw"

    def test_success_json_payload(
        self,
        capsys: pytest.CaptureFixture[str],
        variant_targets: Path,
        tmp_path: Path,
    ) -> None:
        """Successful export emits the documented JSON payload."""
        out_file = tmp_path / "myout.qcow2"
        out_file.write_bytes(b"\0" * (2 * 1024 * 1024))

        with _patch_cfg_paths(variant_targets), \
                patch.object(cli.os, "getuid", return_value=0), \
                patch(
                    "ltvm_pkg.image_export.export_image",
                    return_value=out_file,
                ):
            rc = cmd_target_export(
                _ns(json=True, output=str(out_file))
            )
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["target"] == "rocky9"
        assert payload["format"] == "qcow2"
        assert payload["path"] == str(out_file)
        assert payload["size_mb"] == 2.0
        assert "kernel" in payload
