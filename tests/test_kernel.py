"""Tests for lib/kernel.py -- target parsing and file resolution."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.kernel import (
    _build_config_fragment,
    _ensure_container_image,
    _find_srpm_url,
    _shell_var,
    download_srpm,
    kernel_status,
    parse_lustre_target,
    resolve_lustre_files,
)
from tests.conftest import _make_config

_ROCKY9_SRPM_URL = (
    "https://dl.rockylinux.org/pub/rocky/9/BaseOS/source/tree/Packages/k"
)


class TestShellVar:
    def test_simple_assignment(self) -> None:
        assert _shell_var("FOO=bar", "FOO") == "bar"

    def test_quoted_assignment(self) -> None:
        assert _shell_var('FOO="bar"', "FOO") == "bar"

    def test_single_quoted(self) -> None:
        assert _shell_var("FOO='bar'", "FOO") == "bar"

    def test_multiline(self) -> None:
        text = "AAA=111\nBBB=222\nCCC=333"
        assert _shell_var(text, "AAA") == "111"
        assert _shell_var(text, "BBB") == "222"
        assert _shell_var(text, "CCC") == "333"

    def test_missing_var(self) -> None:
        assert _shell_var("FOO=bar", "BAZ") is None

    def test_empty_text(self) -> None:
        assert _shell_var("", "FOO") is None

    def test_comment_lines_ignored(self) -> None:
        text = "# comment\nFOO=bar"
        assert _shell_var(text, "FOO") == "bar"

    def test_value_with_dots(self) -> None:
        assert _shell_var("lnxmaj=5.14.0", "lnxmaj") == "5.14.0"

    def test_value_with_dashes(self) -> None:
        text = "lnxrel=503.26.1.el9_7"
        assert _shell_var(text, "lnxrel") == "503.26.1.el9_7"

    def test_strips_whitespace(self) -> None:
        assert _shell_var("FOO=bar   ", "FOO") == "bar"

    def test_skips_shell_expansions(self) -> None:
        """Variables with ${} expansions should return None."""
        text = 'SRPM="kernel-${lnxmaj}-${lnxrel}.src.rpm"'
        assert _shell_var(text, "SRPM") is None


class TestParseLustreTarget:
    def test_parses_target_file(self, lustre_tree: Path) -> None:
        result = parse_lustre_target(lustre_tree, "5.14-rhel9.7")
        assert result["lnxmaj"] == "5.14.0"
        assert result["lnxrel"] == "503.26.1.el9_7"
        assert result["srpm"] == "kernel-5.14.0-503.26.1.el9_7.src.rpm"
        assert result["series"] == "5.14-rhel9.7.series"

    def test_missing_target_file(self, lustre_tree: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            parse_lustre_target(lustre_tree, "nonexistent")

    def test_missing_lnxmaj(self, lustre_tree: Path) -> None:
        tf = lustre_tree / "lustre/kernel_patches/targets/bad.target"
        tf.write_text("SERIES=bad.series\n")
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_lustre_target(lustre_tree, "bad")

    def test_default_series(self, lustre_tree: Path) -> None:
        """When SERIES is not set, defaults to <target>.series."""
        tf = lustre_tree / "lustre/kernel_patches/targets/noseries.target"
        tf.write_text("lnxmaj=5.14.0\nlnxrel=100.el9\n")
        result = parse_lustre_target(lustre_tree, "noseries")
        assert result["series"] == "noseries.series"


class TestResolveLustreFiles:
    def test_resolves_all_files(self, lustre_tree: Path) -> None:
        target_info = parse_lustre_target(lustre_tree, "5.14-rhel9.7")
        files = resolve_lustre_files(lustre_tree, "5.14-rhel9.7", target_info)

        assert files["config"].exists()
        assert "x86_64.config" in files["config"].name
        assert files["series_file"].exists()
        assert len(files["patches"]) == 2
        for p in files["patches"]:
            assert p.exists()
            assert p.suffix == ".patch"

    def test_missing_config(self, lustre_tree: Path) -> None:
        target_info = {
            "lnxmaj": "6.0.0",
            "lnxrel": "1.el9",
            "series": "fake.series",
        }
        with pytest.raises(FileNotFoundError, match="config not found"):
            resolve_lustre_files(lustre_tree, "fake-target", target_info)

    def test_missing_patch(self, lustre_tree: Path) -> None:
        """Series references a patch that doesn't exist."""
        series = (
            lustre_tree / "lustre/kernel_patches/series/5.14-rhel9.7.series"
        )
        series.write_text("patch1.patch\nmissing.patch\n")

        target_info = parse_lustre_target(lustre_tree, "5.14-rhel9.7")
        with pytest.raises(FileNotFoundError, match="Patch not found"):
            resolve_lustre_files(lustre_tree, "5.14-rhel9.7", target_info)

    def test_empty_series(self, lustre_tree: Path) -> None:
        """Empty series file means no patches."""
        series = (
            lustre_tree / "lustre/kernel_patches/series/5.14-rhel9.7.series"
        )
        series.write_text("")

        target_info = parse_lustre_target(lustre_tree, "5.14-rhel9.7")
        files = resolve_lustre_files(lustre_tree, "5.14-rhel9.7", target_info)
        assert files["patches"] == []

    def test_series_with_comments(self, lustre_tree: Path) -> None:
        """Comments and blank lines in series are skipped."""
        series = (
            lustre_tree / "lustre/kernel_patches/series/5.14-rhel9.7.series"
        )
        series.write_text(
            "# This is a comment\n\npatch1.patch\n\n# another\npatch2.patch\n"
        )

        target_info = parse_lustre_target(lustre_tree, "5.14-rhel9.7")
        files = resolve_lustre_files(lustre_tree, "5.14-rhel9.7", target_info)
        assert len(files["patches"]) == 2

    def test_nonexistent_series_file(self, lustre_tree: Path) -> None:
        """When series file doesn't exist, patches list is empty."""
        target_info = {
            "lnxmaj": "5.14.0",
            "lnxrel": "503.26.1.el9_7",
            "series": "nonexistent.series",
        }
        configs = lustre_tree / "lustre/kernel_patches/kernel_configs"
        (configs / "kernel-5.14.0-5.14-rhel9.7-x86_64.config").touch()

        files = resolve_lustre_files(lustre_tree, "5.14-rhel9.7", target_info)
        assert files["patches"] == []


# ------------------------------------------------------------------
# TestFindSrpmUrl
# ------------------------------------------------------------------


class TestFindSrpmUrl:
    def test_returns_url_with_base(self) -> None:
        url = _find_srpm_url(
            "kernel-5.14.0-503.26.1.el9_7.src.rpm", _ROCKY9_SRPM_URL
        )
        assert _ROCKY9_SRPM_URL in url

    def test_returns_url_with_srpm_name(self) -> None:
        srpm = "kernel-5.14.0-503.26.1.el9_7.src.rpm"
        url = _find_srpm_url(srpm, _ROCKY9_SRPM_URL)
        assert srpm in url

    def test_url_combines_base_and_name(self) -> None:
        srpm = "kernel-5.14.0-503.26.1.el9_7.src.rpm"
        url = _find_srpm_url(srpm, _ROCKY9_SRPM_URL)
        assert url == f"{_ROCKY9_SRPM_URL}/{srpm}"

    def test_different_base_url(self) -> None:
        srpm = "kernel-4.18.0-100.el8.src.rpm"
        base = "https://dl.rockylinux.org/pub/rocky/8/BaseOS/source/tree/Packages/k"
        url = _find_srpm_url(srpm, base)
        assert url == f"{base}/{srpm}"


# ------------------------------------------------------------------
# TestDownloadSrpm
# ------------------------------------------------------------------


class TestDownloadSrpm:
    def test_cached_file_returned_without_subprocess(
        self, tmp_path: Path
    ) -> None:
        srpm = "kernel-5.14.0-503.26.1.el9_7.src.rpm"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True)
        cached = cache_dir / srpm
        cached.touch()

        with patch("lib.kernel.subprocess.run") as mock_run:
            result = download_srpm(srpm, cache_dir, _ROCKY9_SRPM_URL)

        assert result == cached
        mock_run.assert_not_called()

    def test_missing_file_calls_curl(self, tmp_path: Path) -> None:
        srpm = "kernel-5.14.0-503.26.1.el9_7.src.rpm"
        cache_dir = tmp_path / "cache"

        with patch("lib.kernel.subprocess.run") as mock_run:
            result = download_srpm(srpm, cache_dir, _ROCKY9_SRPM_URL)

        assert result == cache_dir / srpm
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "curl"
        assert str(cache_dir / srpm) in cmd

    def test_cache_dir_created(self, tmp_path: Path) -> None:
        srpm = "kernel-5.14.0-503.26.1.el9_7.src.rpm"
        cache_dir = tmp_path / "cache" / "nested"

        with patch("lib.kernel.subprocess.run"):
            download_srpm(srpm, cache_dir, _ROCKY9_SRPM_URL)

        assert cache_dir.exists()

    def test_curl_includes_url(self, tmp_path: Path) -> None:
        srpm = "kernel-5.14.0-503.26.1.el9_7.src.rpm"
        cache_dir = tmp_path / "cache"

        with patch("lib.kernel.subprocess.run") as mock_run:
            download_srpm(srpm, cache_dir, _ROCKY9_SRPM_URL)

        cmd = mock_run.call_args[0][0]
        expected_url = f"{_ROCKY9_SRPM_URL}/{srpm}"
        assert expected_url in cmd


# ------------------------------------------------------------------
# TestEnsureContainerImage
# ------------------------------------------------------------------


class TestEnsureContainerImage:
    def _make_target_config(self, tmp_path: Path) -> MagicMock:
        target_dir = tmp_path / "targets" / "rocky9"
        target_dir.mkdir(parents=True)
        (target_dir / "container.Dockerfile").write_text("FROM rockylinux:9\n")
        cfg = MagicMock()
        cfg.name = "rocky9"
        cfg.target_dir = target_dir
        return cfg

    def test_returns_correct_tag(self, tmp_path: Path) -> None:
        cfg = self._make_target_config(tmp_path)
        with patch("lib.kernel.subprocess.run"):
            tag = _ensure_container_image(cfg)
        assert tag == "ltvm-build-rocky9"

    def test_calls_podman_build(self, tmp_path: Path) -> None:
        cfg = self._make_target_config(tmp_path)
        with patch("lib.kernel.subprocess.run") as mock_run:
            _ensure_container_image(cfg)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "podman"
        assert "build" in cmd

    def test_podman_build_uses_tag(self, tmp_path: Path) -> None:
        cfg = self._make_target_config(tmp_path)
        with patch("lib.kernel.subprocess.run") as mock_run:
            _ensure_container_image(cfg)
        cmd = mock_run.call_args[0][0]
        assert "ltvm-build-rocky9" in cmd

    def test_podman_build_uses_dockerfile(self, tmp_path: Path) -> None:
        cfg = self._make_target_config(tmp_path)
        dockerfile = str(cfg.target_dir / "container.Dockerfile")
        with patch("lib.kernel.subprocess.run") as mock_run:
            _ensure_container_image(cfg)
        cmd = mock_run.call_args[0][0]
        assert dockerfile in cmd


# ------------------------------------------------------------------
# TestBuildConfigFragment
# ------------------------------------------------------------------


class TestBuildConfigFragment:
    def test_contains_common_fragment(self, tmp_targets: Path) -> None:
        cfg = _make_config(tmp_targets)
        frag = _build_config_fragment(cfg)
        assert "CONFIG_VIRTIO=y" in frag
        assert "CONFIG_9P_FS=y" in frag

    def test_contains_target_overrides(self, tmp_targets: Path) -> None:
        cfg = _make_config(tmp_targets)
        frag = _build_config_fragment(cfg)
        # targets.yaml has CONFIG_XEN_PVH=y in kernels.config
        assert "CONFIG_XEN_PVH=y" in frag

    def test_ends_with_newline(self, tmp_targets: Path) -> None:
        cfg = _make_config(tmp_targets)
        frag = _build_config_fragment(cfg)
        assert frag.endswith("\n")

    def test_no_common_fragment_still_returns_overrides(
        self, tmp_targets: Path
    ) -> None:
        common_frag = (
            tmp_targets / "targets" / "common" / "kernel-config.fragment"
        )
        common_frag.unlink()
        cfg = _make_config(tmp_targets)
        frag = _build_config_fragment(cfg)
        assert "CONFIG_XEN_PVH=y" in frag
        assert "CONFIG_VIRTIO=y" not in frag


# ------------------------------------------------------------------
# TestKernelStatus
# ------------------------------------------------------------------


class TestKernelStatus:
    def test_no_meta_returns_not_built(self, tmp_targets: Path) -> None:
        cfg = _make_config(tmp_targets)
        result = kernel_status(cfg)
        assert result["built"] is False
        assert result["stale"] is True

    def test_matching_hash_returns_not_stale(self, tmp_targets: Path) -> None:
        cfg = _make_config(tmp_targets)
        input_hash = cfg.input_hash("kernel")
        kernel_dir = cfg.kernel_output_dir()
        kernel_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "input_hash": input_hash,
            "kernel_version": "5.14.0-503.26.1.el9_7",
            "srpm": "kernel-5.14.0-503.26.1.el9_7.src.rpm",
        }
        (kernel_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        result = kernel_status(cfg)
        assert result["built"] is True
        assert result["stale"] is False
        assert result["kernel_version"] == "5.14.0-503.26.1.el9_7"

    def test_stale_hash_returns_stale(self, tmp_targets: Path) -> None:
        cfg = _make_config(tmp_targets)
        kernel_dir = cfg.kernel_output_dir()
        kernel_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "input_hash": "deadbeefdeadbeef",
            "kernel_version": "5.14.0-503.26.1.el9_7",
            "srpm": "kernel-5.14.0-503.26.1.el9_7.src.rpm",
        }
        (kernel_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        result = kernel_status(cfg)
        assert result["built"] is True
        assert result["stale"] is True

    def test_meta_fields_propagated(self, tmp_targets: Path) -> None:
        cfg = _make_config(tmp_targets)
        input_hash = cfg.input_hash("kernel")
        kernel_dir = cfg.kernel_output_dir()
        kernel_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "input_hash": input_hash,
            "kernel_version": "5.14.0-503.26.1.el9_7",
            "srpm": "kernel-5.14.0-503.26.1.el9_7.src.rpm",
            "lnxmaj": "5.14.0",
            "lnxrel": "503.26.1.el9_7",
        }
        (kernel_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        result = kernel_status(cfg)
        assert result["lnxmaj"] == "5.14.0"
        assert result["lnxrel"] == "503.26.1.el9_7"
        assert result["srpm"] == "kernel-5.14.0-503.26.1.el9_7.src.rpm"
