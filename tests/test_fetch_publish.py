"""Tests for cmd_fetch / cmd_publish and their helpers.

These commands form the maintainer publish + consumer fetch pipeline
and weren't well-covered behaviorally before this file existed.  The
tests aim to lock in observable behavior so the cli.py refactor can
move these into a submodule without silently breaking anything.

Conventions follow tests/test_ltvm_cli.py: monkeypatch
``ltvm_pkg.cli.TargetConfig``, mock subprocess where it would shell
out, never hit the network.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import cli as cli_mod
from ltvm_pkg.cli import (
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    _find_release_url,
    _gh_release_upload,
    _release_status,
    cmd_fetch,
    cmd_publish,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _tc(tmp_targets: Path) -> Any:
    """Build a real TargetConfig pointing at the tmp_targets fixture."""
    import ltvm_pkg.target_config as cfg

    with (
        patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
        patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"),
        patch.object(
            cfg,
            "TARGETS_YAML",
            tmp_targets / "targets" / "targets.yaml",
        ),
    ):
        return cfg.TargetConfig("rocky9")


def _ns(**kwargs: Any) -> argparse.Namespace:
    """Build an argparse.Namespace with sensible cli defaults."""
    defaults: dict[str, Any] = {
        "json": False,
        "target": "rocky9",
        "kernel": None,
        "variant": "base",
        "arch": None,
        "force_compat": False,
        "force": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# _find_release_url -- pin variant + kernel + bootable filtering rules
# ---------------------------------------------------------------------------


class TestFindReleaseUrl:
    def _rel(self, tag: str, *names: str) -> dict:
        """Build a minimal /releases JSON entry."""
        return {
            "tag_name": tag,
            "assets": [
                {"name": n, "browser_download_url": f"https://ex/{n}"}
                for n in names
            ],
        }

    def test_bootable_variant_asset_is_found(self) -> None:
        """A published variant bootable image must be fetchable.

        package_bootable names it
        bootable-<t>-<arch>-<kver>-<variant>.qcow2.zst, so the variant
        is not adjacent to the trailing .zst.  Matching a
        `-<variant>.zst` tail meant these could never be located --
        publish would succeed and fetch would report none published.
        """
        name = (
            "bootable-rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre"
            "-mofed-24.qcow2.zst"
        )
        releases = [
            self._rel("bootable-rocky9-x86_64-5.14.0-611.13.1.el9_7", name)
        ]
        with patch("ltvm_pkg.cli._gh_api", return_value=releases):
            url = _find_release_url(
                "rocky9", arch="x86_64", variant="mofed-24", mode="bootable"
            )
        assert url.endswith(name)

    def test_bootable_base_rejects_variant_asset(self) -> None:
        """mofed-24's trailing '24' must not read as a kver.

        The base-variant guard asked whether the last dashed segment
        held a digit; for `-mofed-24` that segment is `24`, so a base
        bootable lookup accepted the MOFED image.
        """
        releases = [
            self._rel(
                "bootable-rocky9-x86_64-5.14.0-611.13.1.el9_7",
                "bootable-rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre"
                "-mofed-24.qcow2.zst",
            )
        ]
        with patch("ltvm_pkg.cli._gh_api", return_value=releases):
            with pytest.raises(RuntimeError, match="No published"):
                _find_release_url("rocky9", arch="x86_64", mode="bootable")

    def test_base_picks_manifest_without_variant_suffix(self) -> None:
        """A base lookup must not silently grab a -mofed manifest."""
        releases = [
            self._rel(
                "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre-mofed",
                "manifest-rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre-mofed.json",
            ),
            self._rel(
                "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre",
                "manifest-rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre.json",
            ),
        ]
        with patch("ltvm_pkg.cli._gh_api", return_value=releases):
            url = _find_release_url("rocky9", arch="x86_64")
        assert url.endswith("611.13.1.el9_7_lustre.json")
        assert "mofed" not in url

    def test_variant_requires_exact_suffix(self) -> None:
        """--variant mofed must reject a base manifest."""
        releases = [
            self._rel(
                "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre",
                "manifest-rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre.json",
            ),
            self._rel(
                "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre-mofed",
                "manifest-rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre-mofed.json",
            ),
        ]
        with patch("ltvm_pkg.cli._gh_api", return_value=releases):
            url = _find_release_url(
                "rocky9", arch="x86_64", variant="mofed"
            )
        assert url.endswith("-mofed.json")

    def test_bootable_mode_uses_bootable_prefix(self) -> None:
        """bootable mode looks for ``bootable-<target>-<arch>-`` assets
        under a release tagged ``bootable-<target>-<arch>-<kver>[-<variant>]``.
        This matches what ``cmd_publish --image`` actually emits.
        """
        releases = [
            self._rel(
                "bootable-rocky9-x86_64-5.14.0-611",
                "bootable-rocky9-x86_64-5.14.0-611.qcow2.zst",
            ),
        ]
        with patch("ltvm_pkg.cli._gh_api", return_value=releases):
            url = _find_release_url(
                "rocky9", arch="x86_64", mode="bootable"
            )
        assert url.endswith(".qcow2.zst")

    def test_bootable_mode_rejects_ecosystem_tag(self) -> None:
        """Ecosystem tags (``<target>-<arch>-...``) must NOT match a
        bootable fetch or we'd grab a manifest when the user asked
        for a qcow2.
        """
        releases = [
            self._rel(
                "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre",
                "manifest-rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre.json",
            ),
        ]
        with patch("ltvm_pkg.cli._gh_api", return_value=releases):
            with pytest.raises(
                RuntimeError, match="No published bootable image"
            ):
                _find_release_url("rocky9", arch="x86_64", mode="bootable")

    def test_filter_string_filters_tag(self) -> None:
        releases = [
            self._rel(
                "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre",
                "manifest-rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre.json",
            ),
            self._rel(
                "rocky9-x86_64-5.14.0-503.26.1.el9_5_lustre",
                "manifest-rocky9-x86_64-5.14.0-503.26.1.el9_5_lustre.json",
            ),
        ]
        with patch("ltvm_pkg.cli._gh_api", return_value=releases):
            url = _find_release_url(
                "rocky9", filter_str="503", arch="x86_64"
            )
        assert "503" in url

    def test_no_match_raises_with_helpful_hint(self) -> None:
        releases = [
            self._rel(
                "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre",
                "manifest-rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre.json",
            ),
        ]
        with patch("ltvm_pkg.cli._gh_api", return_value=releases):
            with pytest.raises(RuntimeError) as exc:
                _find_release_url(
                    "rocky9",
                    arch="x86_64",
                    kernel_signature="el9_5",
                    variant="mofed",
                )
        msg = str(exc.value)
        assert "No published artifacts" in msg
        assert "el9_5" in msg
        assert "mofed" in msg
        assert "ltvm target fetch --list" in msg

    def test_dict_response_normalized_to_list(self) -> None:
        """When _gh_api returns a single dict, it's wrapped to a list."""
        single = self._rel(
            "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre",
            "manifest-rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre.json",
        )
        with patch("ltvm_pkg.cli._gh_api", return_value=single):
            url = _find_release_url("rocky9", arch="x86_64")
        assert url.endswith(".json")

    def test_unrelated_tag_skipped(self) -> None:
        """Tags from a different target must not be matched."""
        releases = [
            self._rel(
                "ubuntu2404-x86_64-6.8.0-100",
                "manifest-ubuntu2404-x86_64-6.8.0-100.json",
            ),
        ]
        with patch("ltvm_pkg.cli._gh_api", return_value=releases):
            with pytest.raises(RuntimeError):
                _find_release_url("rocky9", arch="x86_64")


# ---------------------------------------------------------------------------
# _release_status -- variant + signature filtering parity with _find_release_url
# ---------------------------------------------------------------------------


class TestReleaseStatus:
    def test_no_releases_yields_dashes(self, tmp_targets: Path) -> None:
        import ltvm_pkg.target_config as cfg

        with patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"):
            local, remote = _release_status("rocky9", "x86_64", [])
        assert local == "-"
        assert remote == "-"

    def test_remote_unreachable_returns_question_mark(
        self, tmp_targets: Path
    ) -> None:
        import ltvm_pkg.target_config as cfg

        with patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"):
            local, remote = _release_status("rocky9", "x86_64", None)
        assert remote == "?"

    def test_local_tag_is_trimmed(self, tmp_targets: Path) -> None:
        import ltvm_pkg.target_config as cfg

        out = tmp_targets / "artifacts"
        tag_dir = out / "rocky9" / "x86_64"
        tag_dir.mkdir(parents=True, exist_ok=True)
        (tag_dir / ".ltvm-release-tag").write_text(
            "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre\n"
        )

        with patch.object(cfg, "ARTIFACTS_DIR", out):
            local, remote = _release_status("rocky9", "x86_64", [])
        assert local == "5.14.0-611.13.1.el9_7_lustre"

    def test_local_base_rejects_variant_tag(self, tmp_targets: Path) -> None:
        """A locally-cached mofed tag must not satisfy a base query."""
        import ltvm_pkg.target_config as cfg

        out = tmp_targets / "artifacts"
        tag_dir = out / "rocky9" / "x86_64"
        tag_dir.mkdir(parents=True, exist_ok=True)
        (tag_dir / ".ltvm-release-tag").write_text(
            "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre-mofed\n"
        )

        with patch.object(cfg, "ARTIFACTS_DIR", out):
            local, _ = _release_status(
                "rocky9", "x86_64", [], variant="base"
            )
        assert local == "-"

    def test_local_variant_requires_matching_suffix(
        self, tmp_targets: Path
    ) -> None:
        import ltvm_pkg.target_config as cfg

        out = tmp_targets / "artifacts"
        tag_dir = out / "rocky9" / "x86_64"
        tag_dir.mkdir(parents=True, exist_ok=True)
        (tag_dir / ".ltvm-release-tag").write_text(
            "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre\n"
        )

        with patch.object(cfg, "ARTIFACTS_DIR", out):
            local, _ = _release_status(
                "rocky9", "x86_64", [], variant="mofed"
            )
        assert local == "-"

    def test_remote_variant_filter_rejects_base_manifest(
        self, tmp_targets: Path
    ) -> None:
        """A mofed query must not see a base manifest as remote."""
        import ltvm_pkg.target_config as cfg

        releases = [
            {
                "tag_name": "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre",
                "assets": [
                    {"name": "manifest-rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre.json"},
                ],
            },
        ]
        with patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"):
            _, remote = _release_status(
                "rocky9", "x86_64", releases, variant="mofed"
            )
        assert remote == "-"

    def test_remote_kernel_signature_filter(
        self, tmp_targets: Path
    ) -> None:
        """An el9_5 query must skip an el9_7 release."""
        import ltvm_pkg.target_config as cfg

        releases = [
            {
                "tag_name": "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre",
                "assets": [
                    {"name": "manifest-rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre.json"},
                ],
            },
        ]
        with patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"):
            _, remote = _release_status(
                "rocky9", "x86_64", releases, kernel_signature="el9_5"
            )
        assert remote == "-"


# ---------------------------------------------------------------------------
# _gh_release_upload -- gh CLI semantics (success, "already exists", failure)
# ---------------------------------------------------------------------------


class TestGhReleaseUpload:
    def _proc(self, rc: int, stdout: str = "", stderr: str = "") -> Any:
        m = MagicMock()
        m.returncode = rc
        m.stdout = stdout
        m.stderr = stderr
        return m

    def test_success_creates_and_uploads(self, tmp_path: Path) -> None:
        a = tmp_path / "asset.tar.zst"
        a.write_bytes(b"x" * 32)
        calls: list[list[str]] = []

        def _run(cmd: list[str], **kw: Any) -> Any:
            calls.append(cmd)
            return self._proc(0)

        with patch("ltvm_pkg.cli.subprocess.run", side_effect=_run):
            rc, err = _gh_release_upload(
                "rocky9-x86_64-5.14", [a], notes="hi", use_json=True
            )
        assert rc is None
        assert err is None
        # First call: gh release create.  Second: gh release upload.
        assert calls[0][:3] == ["gh", "release", "create"]
        assert calls[1][:3] == ["gh", "release", "upload"]
        # --clobber is required so re-runs overwrite prior uploads.
        assert "--clobber" in calls[1]

    def test_already_exists_is_not_fatal(self, tmp_path: Path) -> None:
        """A "release already exists" stderr is treated as success and
        upload still proceeds.  This is what makes `publish` re-runnable."""
        a = tmp_path / "asset.tar.zst"
        a.write_bytes(b"x")

        run_outputs = iter(
            [
                self._proc(1, stderr="release already exists"),
                self._proc(0),
            ]
        )

        with patch(
            "ltvm_pkg.cli.subprocess.run",
            side_effect=lambda *a, **k: next(run_outputs),
        ):
            rc, err = _gh_release_upload(
                "tag", [a], notes="n", use_json=True
            )
        assert rc is None
        assert err is None

    def test_create_failure_other_message_is_error(
        self, tmp_path: Path
    ) -> None:
        a = tmp_path / "asset.tar.zst"
        a.write_bytes(b"x")
        with patch(
            "ltvm_pkg.cli.subprocess.run",
            return_value=self._proc(1, stderr="permission denied"),
        ):
            rc, err = _gh_release_upload(
                "tag", [a], notes="n", use_json=True
            )
        assert rc == EXIT_ERROR
        assert err is not None
        assert "permission denied" in err
        assert "rc=1" in err

    def test_upload_failure_returns_error(self, tmp_path: Path) -> None:
        a = tmp_path / "asset.tar.zst"
        a.write_bytes(b"x")

        run_outputs = iter([self._proc(0), self._proc(1)])
        with patch(
            "ltvm_pkg.cli.subprocess.run",
            side_effect=lambda *a, **k: next(run_outputs),
        ):
            rc, err = _gh_release_upload(
                "tag", [a], notes="n", use_json=True
            )
        assert rc == EXIT_ERROR
        assert err is not None
        assert "asset.tar.zst" in err

    def test_missing_gh_cli_returns_helpful_error(
        self, tmp_path: Path
    ) -> None:
        a = tmp_path / "asset.tar.zst"
        a.write_bytes(b"x")
        with patch(
            "ltvm_pkg.cli.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            rc, err = _gh_release_upload(
                "tag", [a], notes="n", use_json=True
            )
        assert rc == EXIT_ERROR
        assert err is not None
        assert "gh CLI not found" in err


# ---------------------------------------------------------------------------
# cmd_publish --no-upload -- local-package-only paths (formerly cmd_package)
# ---------------------------------------------------------------------------


class TestCmdPublishNoUpload:
    """``publish --no-upload`` short-circuits after package_target --
    the local-package flow (formerly ``target package``).
    """

    def test_no_lustre_passes_include_lustre_false(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        """--no-lustre forces include_lustre=False into package_target."""
        tc = _tc(tmp_targets)
        assets = {
            "container": tmp_path / "c.tar.zst",
            "kernel": tmp_path / "k.tar.zst",
            "image": tmp_path / "i.tar.zst",
            "manifest": tmp_path / "m.json",
        }
        for p in assets.values():
            p.write_bytes(b"x")

        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod, "package_target", return_value=assets
            ) as pt,
            patch.object(cli_mod, "_gh_release_upload") as upl,
        ):
            args = _ns(
                target="rocky9",
                no_lustre=True,
                no_upload=True,
                output=None,
            )
            rc = cmd_publish(args)

        assert rc == EXIT_OK
        assert pt.called
        _, kwargs = pt.call_args
        assert kwargs.get("include_lustre") is False
        # --no-upload: must NOT hit GitHub.
        assert not upl.called

    def test_default_passes_include_lustre_true(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        """Without --no-lustre, include_lustre=True -- no tree touched."""
        tc = _tc(tmp_targets)
        assets = {"manifest": tmp_path / "m.json"}
        assets["manifest"].write_text("{}")

        # Seed a fake kernel + snapshot so cmd_publish's
        # _resolve_kernel + snapshot-presence check both pass without
        # touching a real build tree.
        kdir = tc.output_dir / "kernels" / "5.14-rhel9.7"
        snap_dir = kdir / "lustre-artifacts"
        snap_dir.mkdir(parents=True)
        (kdir / "vmlinux").write_text("")
        (snap_dir / ".ltvm-snapshot.json").write_text("{}")

        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod, "package_target", return_value=assets
            ) as pt,
            patch.object(cli_mod, "_resolve_lustre_tree") as rl,
            patch.object(cli_mod, "snapshot_lustre") as snap,
        ):
            args = _ns(
                target="rocky9",
                no_lustre=False,
                no_upload=True,
                output=None,
            )
            rc = cmd_publish(args)

        assert rc == EXIT_OK
        assert pt.called
        _, kwargs = pt.call_args
        assert kwargs.get("include_lustre") is True
        # Publish must never snapshot from / resolve the Lustre tree.
        assert not snap.called
        assert not rl.called

    def test_package_target_failure_surfaces(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        tc = _tc(tmp_targets)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod,
                "package_target",
                side_effect=ValueError("missing artifacts"),
            ),
        ):
            args = _ns(
                target="rocky9",
                no_lustre=True,
                no_upload=True,
                output=None,
            )
            rc = cmd_publish(args)

        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "Package failed" in err
        assert "missing artifacts" in err

    def test_variant_appears_in_human_output(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        tc = _tc(tmp_targets)
        assets = {"manifest": tmp_path / "m.json"}
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod, "package_target", return_value=assets
            ) as pt,
        ):
            args = _ns(
                target="rocky9",
                variant="mofed",
                no_lustre=True,
                no_upload=True,
                output=None,
            )
            rc = cmd_publish(args)

        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "variant=mofed" in out
        # Variant must reach package_target unchanged.
        _, kwargs = pt.call_args
        assert kwargs.get("variant") == "mofed"


# ---------------------------------------------------------------------------
# cmd_fetch -- main paths
# ---------------------------------------------------------------------------


class TestCmdFetch:
    def test_missing_target_when_not_listing_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _ns(
            target=None,
            url=None,
            filter=None,
            arch=None,
            kernel=None,
            variant="base",
            list=False,
            replace=False,
            force=False,
            image=False,
        )
        rc = cmd_fetch(args)
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "target required" in err

    def test_kernel_without_target_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _ns(
            target=None,
            url=None,
            filter=None,
            arch=None,
            kernel="5.14-rhel9.7",
            variant="base",
            list=False,
            replace=False,
            force=False,
            image=False,
        )
        rc = cmd_fetch(args)
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "--kernel requires a target" in err

    def test_list_mode_prints_releases(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        releases = [
            {
                "tag_name": "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre",
                "published_at": "2024-12-01T00:00:00Z",
                "assets": [
                    {"name": "container.tar.gz", "size": 1024 * 1024 * 50},
                ],
            },
        ]
        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cli_mod, "_gh_api", return_value=releases),
        ):
            args = _ns(
                target="rocky9",
                url=None,
                filter=None,
                arch=None,
                kernel=None,
                variant="base",
                list=True,
                replace=False,
                force=False,
                image=False,
            )
            rc = cmd_fetch(args)
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "rocky9-x86_64-5.14.0-611" in out
        assert "2024-12-01" in out

    def test_list_mode_empty_prints_no_releases(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cli_mod, "_gh_api", return_value=[]),
        ):
            args = _ns(
                target="rocky9",
                url=None,
                filter=None,
                arch=None,
                kernel=None,
                variant="base",
                list=True,
                replace=False,
                force=False,
                image=False,
            )
            rc = cmd_fetch(args)
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "no releases found" in out

    def test_list_mode_json(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cli_mod, "_gh_api", return_value=[]),
        ):
            args = _ns(
                target="rocky9",
                url=None,
                filter=None,
                arch=None,
                kernel=None,
                variant="base",
                list=True,
                replace=False,
                force=False,
                image=False,
                json=True,
            )
            rc = cmd_fetch(args)
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload == []

    def test_no_release_found_returns_error(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cli_mod, "_gh_api", return_value=[]),
        ):
            args = _ns(
                target="rocky9",
                url=None,
                filter=None,
                arch=None,
                kernel=None,
                variant="base",
                list=False,
                replace=False,
                force=False,
                image=False,
            )
            rc = cmd_fetch(args)
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "No published artifacts" in err

    def test_explicit_url_skips_lookup_and_fetches(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        import ltvm_pkg.target_config as cfg

        target_dir = tmp_targets / "artifacts" / "rocky9" / "x86_64"
        target_dir.mkdir(parents=True, exist_ok=True)
        url = "https://x/releases/download/rocky9-x86_64-foo/manifest.json"

        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"),
            patch.object(cli_mod, "fetch_target", return_value=target_dir) as ft,
            patch.object(cli_mod, "_gh_api") as ga,
        ):
            args = _ns(
                target="rocky9",
                url=url,
                filter=None,
                arch="x86_64",
                kernel=None,
                variant="base",
                list=False,
                replace=False,
                force=False,
                image=False,
            )
            rc = cmd_fetch(args)

        assert rc == EXIT_OK
        # Explicit URL must skip the GitHub API entirely.
        assert not ga.called
        assert ft.called
        # Tag was extracted and recorded for next-run idempotency.
        tag_file = target_dir / ".ltvm-release-tag"
        assert tag_file.exists()
        assert tag_file.read_text().strip() == "rocky9-x86_64-foo"

    def test_already_up_to_date_no_op(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        """Same release tag on disk: no fetch, no error, success."""
        import ltvm_pkg.target_config as cfg

        out = tmp_targets / "artifacts"
        target_dir = out / "rocky9" / "x86_64"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / ".ltvm-release-tag").write_text("rocky9-x86_64-cached\n")
        url = "https://x/releases/download/rocky9-x86_64-cached/manifest.json"

        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cfg, "ARTIFACTS_DIR", out),
            patch.object(cli_mod, "fetch_target") as ft,
        ):
            args = _ns(
                target="rocky9",
                url=url,
                filter=None,
                arch="x86_64",
                kernel=None,
                variant="base",
                list=False,
                replace=False,
                force=False,
                image=False,
            )
            rc = cmd_fetch(args)

        assert rc == EXIT_OK
        assert not ft.called
        assert "Already up to date" in capsys.readouterr().out

    def test_replace_without_force_refuses_when_same_tag(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        """--replace + same tag refuses unless --force overrides."""
        import ltvm_pkg.target_config as cfg

        out = tmp_targets / "artifacts"
        target_dir = out / "rocky9" / "x86_64"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / ".ltvm-release-tag").write_text("rocky9-x86_64-same\n")
        url = "https://x/releases/download/rocky9-x86_64-same/manifest.json"

        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cfg, "ARTIFACTS_DIR", out),
            patch.object(cli_mod, "fetch_target") as ft,
        ):
            args = _ns(
                target="rocky9",
                url=url,
                filter=None,
                arch="x86_64",
                kernel=None,
                variant="base",
                list=False,
                replace=True,
                force=False,
                image=False,
            )
            rc = cmd_fetch(args)

        assert rc == EXIT_ERROR
        assert not ft.called
        err = capsys.readouterr().err
        assert "identical bytes" in err
        assert "--force" in err

    def test_replace_with_force_wipes_and_refetches(
        self,
        tmp_targets: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import ltvm_pkg.target_config as cfg

        out = tmp_targets / "artifacts"
        target_dir = out / "rocky9" / "x86_64"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / ".ltvm-release-tag").write_text("rocky9-x86_64-same\n")
        leftover = target_dir / "stale.txt"
        leftover.write_text("old")

        url = "https://x/releases/download/rocky9-x86_64-same/manifest.json"

        def _fake_fetch(target, url, base, **kw):  # type: ignore[no-untyped-def]
            # Recreate the dir as fetch_target would after extraction.
            (Path(base) / target / kw["arch"]).mkdir(
                parents=True, exist_ok=True
            )
            return Path(base) / target / kw["arch"]

        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cfg, "ARTIFACTS_DIR", out),
            patch.object(
                cli_mod, "fetch_target", side_effect=_fake_fetch
            ) as ft,
        ):
            args = _ns(
                target="rocky9",
                url=url,
                filter=None,
                arch="x86_64",
                kernel=None,
                variant="base",
                list=False,
                replace=True,
                force=True,
                image=False,
            )
            rc = cmd_fetch(args)

        assert rc == EXIT_OK
        assert ft.called
        assert not leftover.exists()

    def test_divergent_tag_refuses_without_replace(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        """Local tag differs from remote release: refuse by default so a
        fresh fetch doesn't silently mix two releases' files."""
        import ltvm_pkg.target_config as cfg

        out = tmp_targets / "artifacts"
        target_dir = out / "rocky9" / "x86_64"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / ".ltvm-release-tag").write_text(
            "rocky9-x86_64-5.14.0-500.el9_5\n"
        )
        url = (
            "https://x/releases/download/"
            "rocky9-x86_64-5.14.0-600.el9_7/manifest.json"
        )

        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cfg, "ARTIFACTS_DIR", out),
            patch.object(cli_mod, "fetch_target") as ft,
            patch.object(
                cli_mod,
                "_gh_api",
                return_value={"published_at": "2025-02-10T00:00:00Z"},
            ),
        ):
            args = _ns(
                target="rocky9",
                url=url,
                filter=None,
                arch="x86_64",
                kernel=None,
                variant="base",
                list=False,
                replace=False,
                force=False,
                image=False,
            )
            rc = cmd_fetch(args)

        assert rc == EXIT_ERROR
        assert not ft.called
        err = capsys.readouterr().err
        assert "5.14.0-500.el9_5" in err
        assert "5.14.0-600.el9_7" in err
        assert "--replace" in err
        # The refusal message must include the remote publish date so
        # the user can judge newness before deciding to upgrade.
        assert "2025-02-10" in err

    def test_divergent_tag_proceeds_with_replace(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        """--replace explicitly consents to overwriting a local copy."""
        import ltvm_pkg.target_config as cfg

        out = tmp_targets / "artifacts"
        target_dir = out / "rocky9" / "x86_64"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / ".ltvm-release-tag").write_text(
            "rocky9-x86_64-old\n"
        )
        (target_dir / "stale.txt").write_text("old")
        url = "https://x/releases/download/rocky9-x86_64-new/manifest.json"

        def _fake_fetch(target, url, base, **kw):  # type: ignore[no-untyped-def]
            (Path(base) / target / kw["arch"]).mkdir(
                parents=True, exist_ok=True
            )
            return Path(base) / target / kw["arch"]

        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cfg, "ARTIFACTS_DIR", out),
            patch.object(
                cli_mod, "fetch_target", side_effect=_fake_fetch
            ) as ft,
        ):
            args = _ns(
                target="rocky9",
                url=url,
                filter=None,
                arch="x86_64",
                kernel=None,
                variant="base",
                list=False,
                replace=True,
                force=False,
                image=False,
            )
            rc = cmd_fetch(args)

        assert rc == EXIT_OK
        assert ft.called
        assert not (target_dir / "stale.txt").exists()

    def test_fetch_target_failure_returns_error(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        import ltvm_pkg.target_config as cfg

        url = "https://x/releases/download/rocky9-x86_64-foo/manifest.json"
        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"),
            patch.object(
                cli_mod,
                "fetch_target",
                side_effect=RuntimeError("network down"),
            ),
        ):
            args = _ns(
                target="rocky9",
                url=url,
                filter=None,
                arch=None,
                kernel=None,
                variant="base",
                list=False,
                replace=False,
                force=False,
                image=False,
            )
            rc = cmd_fetch(args)

        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "Fetch failed" in err
        assert "network down" in err

    def test_schema_mismatch_triggers_update_check(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When fetch_target raises 'unrecognized manifest schema', the
        update-check escalation runs.  This is the user-self-heal path."""
        import ltvm_pkg.target_config as cfg

        url = "https://x/releases/download/rocky9-x86_64-foo/manifest.json"
        # maybe_check_for_updates lives in a sibling module; cmd_fetch
        # imports it lazily, so register a stub on the live module.
        from ltvm_pkg import update_check

        called: list[bool] = []

        def _stub(*, force: bool, use_json: bool) -> None:
            called.append(force)

        monkeypatch.setattr(
            update_check, "maybe_check_for_updates", _stub
        )

        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"),
            patch.object(
                cli_mod,
                "fetch_target",
                side_effect=RuntimeError(
                    "unrecognized manifest schema in foo.json"
                ),
            ),
        ):
            args = _ns(
                target="rocky9",
                url=url,
                filter=None,
                arch=None,
                kernel=None,
                variant="base",
                list=False,
                replace=False,
                force=False,
                image=False,
            )
            rc = cmd_fetch(args)

        assert rc == EXIT_ERROR
        assert called == [True], (
            "schema mismatch must escalate to a forced update-check"
        )

    def test_image_mode_calls_fetch_bootable(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"),
            patch(
                "ltvm_pkg.release_package.fetch_bootable",
                return_value=Path("/fake/disk.qcow2"),
            ) as fb,
            patch.object(
                cli_mod,
                "_find_release_url",
                return_value="https://x/y/disk.qcow2.zst",
            ) as fr,
        ):
            args = _ns(
                target="rocky9",
                url=None,
                filter=None,
                arch=None,
                kernel=None,
                variant="base",
                list=False,
                replace=False,
                force=False,
                image=True,
            )
            rc = cmd_fetch(args)

        assert rc == EXIT_OK
        # _find_release_url was invoked in bootable mode.
        _, kwargs = fr.call_args
        assert kwargs.get("mode") == "bootable"
        assert fb.called

    def test_image_mode_with_explicit_url_skips_lookup(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"),
            patch(
                "ltvm_pkg.release_package.fetch_bootable",
                return_value=Path("/fake/disk.qcow2"),
            ) as fb,
            patch.object(cli_mod, "_find_release_url") as fr,
        ):
            args = _ns(
                target="rocky9",
                url="https://x/already-known.qcow2.zst",
                filter=None,
                arch=None,
                kernel=None,
                variant="base",
                list=False,
                replace=False,
                force=False,
                image=True,
            )
            rc = cmd_fetch(args)

        assert rc == EXIT_OK
        assert not fr.called
        assert fb.called

    def test_image_mode_fetch_failure_surfaces(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"),
            patch(
                "ltvm_pkg.release_package.fetch_bootable",
                side_effect=RuntimeError("disk corrupt"),
            ),
            patch.object(
                cli_mod,
                "_find_release_url",
                return_value="https://x/d.qcow2.zst",
            ),
        ):
            args = _ns(
                target="rocky9",
                url=None,
                filter=None,
                arch=None,
                kernel=None,
                variant="base",
                list=False,
                replace=False,
                force=False,
                image=True,
            )
            rc = cmd_fetch(args)

        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "Fetch bootable failed" in err
        assert "disk corrupt" in err

    @pytest.mark.parametrize(
        "reported_machine,expected_arch",
        [("aarch64", "aarch64"), ("arm64", "aarch64"), ("x86_64", "x86_64")],
    )
    def test_default_arch_uses_host_arch(
        self,
        reported_machine: str,
        expected_arch: str,
        tmp_targets: Path,
    ) -> None:
        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "output"),
            patch(
                "ltvm_pkg.cli.util.platform.machine",
                return_value=reported_machine,
            ),
            patch.object(
                cli_mod,
                "_find_release_url",
                return_value=(
                    f"https://x/releases/download/rocky9-{expected_arch}-foo/"
                    "manifest.json"
                ),
            ) as fru,
            patch.object(
                cli_mod,
                "fetch_target",
                return_value=tmp_targets / "output" / "rocky9" / expected_arch,
            ),
        ):
            args = _ns(
                target="rocky9",
                url=None,
                filter=None,
                arch=None,
                kernel=None,
                variant="base",
                list=False,
                replace=False,
                force=False,
                image=False,
            )
            rc = cmd_fetch(args)

        assert rc == EXIT_OK
        assert fru.call_args.kwargs["arch"] == expected_arch

    def test_explicit_arch_override_wins(
        self,
        tmp_targets: Path,
    ) -> None:
        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cli_mod, "TargetConfig", _tc_factory(tmp_targets)),
            patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "output"),
            patch("ltvm_pkg.cli.util.platform.machine", return_value="aarch64"),
            patch.object(
                cli_mod,
                "_find_release_url",
                return_value=(
                    "https://x/releases/download/rocky9-x86_64-foo/"
                    "manifest.json"
                ),
            ) as fru,
            patch.object(
                cli_mod,
                "fetch_target",
                return_value=tmp_targets / "output" / "rocky9" / "x86_64",
            ),
        ):
            args = _ns(
                target="rocky9",
                url=None,
                filter=None,
                arch="x86_64",
                kernel=None,
                variant="base",
                list=False,
                replace=False,
                force=False,
                image=False,
            )
            rc = cmd_fetch(args)

        assert rc == EXIT_OK
        assert fru.call_args.kwargs["arch"] == "x86_64"


class TestHostArch:
    @pytest.mark.parametrize(
        "reported,expected",
        [
            ("x86_64", "x86_64"),
            ("aarch64", "aarch64"),
            ("arm64", "aarch64"),
            ("ppc64le", "ppc64le"),
        ],
    )
    def test_host_arch_normalisation(self, reported: str, expected: str) -> None:
        from ltvm_pkg.cli.util import host_arch

        with patch("ltvm_pkg.cli.util.platform.machine", return_value=reported):
            assert host_arch() == expected


def _tc_factory(tmp_targets: Path):
    """Factory so a single mock can build TargetConfigs for any name/arch
    used inside cmd_fetch (pre-flight kernel check + final hint)."""
    import ltvm_pkg.target_config as cfg

    def _make(name: str, **kw: Any) -> Any:
        with (
            patch.object(cfg, "TARGETS_DIR", tmp_targets / "targets"),
            patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"),
            patch.object(
                cfg,
                "TARGETS_YAML",
                tmp_targets / "targets" / "targets.yaml",
            ),
        ):
            return cfg.TargetConfig(name, **kw)

    return _make


# ---------------------------------------------------------------------------
# cmd_publish -- ecosystem and bootable modes
# ---------------------------------------------------------------------------


class TestCmdPublish:
    def test_no_lustre_ecosystem_publish_succeeds(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        import ltvm_pkg.target_config as cfg

        tc = _tc(tmp_targets)
        manifest = tmp_path / (
            "manifest-rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre.json"
        )
        manifest.write_text("{}")
        kernel_tar = tmp_path / "kernel.tar.zst"
        kernel_tar.write_bytes(b"k")
        assets = {"kernel": kernel_tar, "manifest": manifest}

        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"),
            patch.object(
                cli_mod, "package_target", return_value=assets
            ) as pt,
            patch.object(
                cli_mod,
                "_gh_release_upload",
                return_value=(None, None),
            ) as up,
        ):
            args = _ns(
                target="rocky9",
                no_lustre=True,
                output=None,
                tag=None,
                image=False,
            )
            rc = cmd_publish(args)

        assert rc == EXIT_OK
        assert pt.called
        assert up.called
        out = capsys.readouterr().out
        # Tag derived from manifest name.
        assert "Tag: rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre" in out
        # Recorded the tag locally for fetch idempotency.
        tag_file = (
            tmp_targets / "artifacts" / "rocky9" / "x86_64"
            / ".ltvm-release-tag"
        )
        assert tag_file.exists()
        assert (
            tag_file.read_text().strip()
            == "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre"
        )

    def test_package_failure_blocks_upload(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        tc = _tc(tmp_targets)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(
                cli_mod,
                "package_target",
                side_effect=ValueError("missing"),
            ),
            patch.object(cli_mod, "_gh_release_upload") as up,
        ):
            args = _ns(
                target="rocky9",
                no_lustre=True,
                output=None,
                tag=None,
                image=False,
            )
            rc = cmd_publish(args)

        assert rc == EXIT_ERROR
        assert not up.called
        assert "Package failed" in capsys.readouterr().err

    def test_upload_failure_returns_error(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        tc = _tc(tmp_targets)
        manifest = tmp_path / (
            "manifest-rocky9-x86_64-5.14.0-611.json"
        )
        manifest.write_text("{}")
        assets = {"manifest": manifest}

        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "package_target", return_value=assets),
            patch.object(
                cli_mod,
                "_gh_release_upload",
                return_value=(EXIT_ERROR, "gh upload boom"),
            ),
        ):
            args = _ns(
                target="rocky9",
                no_lustre=True,
                output=None,
                tag=None,
                image=False,
            )
            rc = cmd_publish(args)

        assert rc == EXIT_ERROR
        assert "gh upload boom" in capsys.readouterr().err

    def test_explicit_tag_overrides_derived(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        tc = _tc(tmp_targets)
        manifest = tmp_path / "manifest-rocky9-x86_64-5.14.0-611.json"
        manifest.write_text("{}")
        assets = {"manifest": manifest}
        captured_tag: list[str] = []

        def _record_upload(tag, paths, notes, use_json):  # type: ignore[no-untyped-def]
            captured_tag.append(tag)
            return None, None

        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch.object(cli_mod, "package_target", return_value=assets),
            patch.object(
                cli_mod, "_gh_release_upload", side_effect=_record_upload
            ),
        ):
            args = _ns(
                target="rocky9",
                no_lustre=True,
                output=None,
                tag="my-explicit-tag",
                image=False,
            )
            rc = cmd_publish(args)

        assert rc == EXIT_OK
        assert captured_tag == ["my-explicit-tag"]

    def test_image_mode_packages_bootable_and_uploads(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        tc = _tc(tmp_targets)
        asset = tmp_path / "bootable-rocky9-x86_64-5.14.0-611.qcow2.zst"
        asset.write_bytes(b"z")
        captured_tag: list[str] = []

        def _record_upload(tag, paths, notes, use_json):  # type: ignore[no-untyped-def]
            captured_tag.append(tag)
            return None, None

        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch(
                "ltvm_pkg.release_package.package_bootable",
                return_value=asset,
            ) as pb,
            patch.object(
                cli_mod, "_gh_release_upload", side_effect=_record_upload
            ),
            patch.object(cli_mod, "package_target") as pt,
            patch.object(cli_mod, "snapshot_lustre") as snap,
        ):
            args = _ns(
                target="rocky9",
                no_lustre=False,  # ignored in image mode
                output=None,
                tag=None,
                image=True,
            )
            rc = cmd_publish(args)

        assert rc == EXIT_OK
        assert pb.called
        # Image mode must NOT call ecosystem package_target / snapshot.
        assert not pt.called
        assert not snap.called
        # Tag derived by stripping the .qcow2.zst suffix.
        assert captured_tag == ["bootable-rocky9-x86_64-5.14.0-611"]

    def test_image_mode_package_failure_surfaces(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
    ) -> None:
        tc = _tc(tmp_targets)
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch(
                "ltvm_pkg.release_package.package_bootable",
                side_effect=FileNotFoundError("missing qcow2"),
            ),
            patch.object(cli_mod, "_gh_release_upload") as up,
        ):
            args = _ns(
                target="rocky9",
                no_lustre=True,
                output=None,
                tag=None,
                image=True,
            )
            rc = cmd_publish(args)

        assert rc == EXIT_ERROR
        assert not up.called
        assert "Package bootable failed" in capsys.readouterr().err

    def test_image_mode_upload_failure_surfaces(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_targets: Path,
        tmp_path: Path,
    ) -> None:
        tc = _tc(tmp_targets)
        asset = tmp_path / "bootable.qcow2.zst"
        asset.write_bytes(b"z")
        with (
            patch.object(cli_mod, "TargetConfig", return_value=tc),
            patch(
                "ltvm_pkg.release_package.package_bootable",
                return_value=asset,
            ),
            patch.object(
                cli_mod,
                "_gh_release_upload",
                return_value=(EXIT_ERROR, "gh said no"),
            ),
        ):
            args = _ns(
                target="rocky9",
                no_lustre=True,
                output=None,
                tag=None,
                image=True,
            )
            rc = cmd_publish(args)

        assert rc == EXIT_ERROR
        assert "gh said no" in capsys.readouterr().err

    def test_unknown_target_returns_not_found(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _raise(name: str, **kw: Any) -> None:
            raise ValueError(f"Unknown target: {name}")

        with patch.object(cli_mod, "TargetConfig", side_effect=_raise):
            args = _ns(
                target="not_a_real_target",
                no_lustre=True,
                output=None,
                tag=None,
                image=False,
            )
            rc = cmd_publish(args)

        assert rc == EXIT_NOT_FOUND
