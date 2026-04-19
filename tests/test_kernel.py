"""Tests for ltvm_pkg/kernel_build.py -- target parsing and file resolution."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg.kernel_build import (
    SrpmNotFoundError,
    _build_config_fragment,
    _ensure_container_image,
    _kernel_outputs_complete,
    _list_lustre_kernel_targets,
    _lustre_target_family,
    _run_kernel_podman,
    _shell_var,
    _srpm_fallback_urls,
    apply_srpm_override,
    diagnose_srpm_not_found,
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

    def test_resolves_shell_expansions(self) -> None:
        """Variables with ${} expansions are resolved from the same file."""
        text = 'lnxmaj="5.14.0"\nlnxrel="611.el9"\nSRPM="kernel-${lnxmaj}-${lnxrel}.src.rpm"'
        assert _shell_var(text, "SRPM") == "kernel-5.14.0-611.el9.src.rpm"

    def test_unresolvable_expansion_kept(self) -> None:
        """Expansions referencing undefined vars are kept as-is."""
        text = 'SRPM="kernel-${lnxmaj}-${lnxrel}.src.rpm"'
        assert _shell_var(text, "SRPM") == "kernel-${lnxmaj}-${lnxrel}.src.rpm"


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

    def test_missing_config_returns_none(self, lustre_tree: Path) -> None:
        # No Lustre-provided config for this target -- config is None
        # so the build path can extract it from the SRPM instead.
        target_info = {
            "lnxmaj": "6.0.0",
            "lnxrel": "1.el9",
            "series": "fake.series",
        }
        files = resolve_lustre_files(lustre_tree, "fake-target", target_info)
        assert files["config"] is None

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

        with patch("ltvm_pkg.kernel_build.subprocess.run") as mock_run:
            result = download_srpm(srpm, cache_dir, _ROCKY9_SRPM_URL)

        assert result == cached
        mock_run.assert_not_called()

    @staticmethod
    def _curl_mock(content: bytes = b"fake srpm") -> MagicMock:
        """A subprocess.run mock that creates the curl -o output file.

        download_srpm now writes to a .partial tempfile and renames
        on success, so plain mock_run.return_value won't suffice --
        we need the side_effect to actually create the partial file
        so the rename works.
        """

        def side_effect(cmd, *args, **kwargs):
            if cmd and cmd[0] == "curl" and "-o" in cmd:
                out_idx = cmd.index("-o") + 1
                Path(cmd[out_idx]).write_bytes(content)
            r = MagicMock()
            r.returncode = 0
            return r

        mock = MagicMock(side_effect=side_effect)
        return mock

    def test_missing_file_calls_curl(self, tmp_path: Path) -> None:
        srpm = "kernel-5.14.0-503.26.1.el9_7.src.rpm"
        cache_dir = tmp_path / "cache"

        with patch(
            "ltvm_pkg.kernel_build.subprocess.run",
            new=self._curl_mock(),
        ) as mock_run:
            result = download_srpm(srpm, cache_dir, _ROCKY9_SRPM_URL)

        assert result == cache_dir / srpm
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "curl"
        # curl writes to a .partial tmpfile that gets renamed to the
        # final cached path on success.
        out_idx = cmd.index("-o") + 1
        assert cmd[out_idx].endswith(".partial")

    def test_cache_dir_created(self, tmp_path: Path) -> None:
        srpm = "kernel-5.14.0-503.26.1.el9_7.src.rpm"
        cache_dir = tmp_path / "cache" / "nested"

        with patch(
            "ltvm_pkg.kernel_build.subprocess.run",
            new=self._curl_mock(),
        ):
            download_srpm(srpm, cache_dir, _ROCKY9_SRPM_URL)

        assert cache_dir.exists()

    def test_curl_includes_url(self, tmp_path: Path) -> None:
        srpm = "kernel-5.14.0-503.26.1.el9_7.src.rpm"
        cache_dir = tmp_path / "cache"

        with patch(
            "ltvm_pkg.kernel_build.subprocess.run",
            new=self._curl_mock(),
        ) as mock_run:
            download_srpm(srpm, cache_dir, _ROCKY9_SRPM_URL)

        cmd = mock_run.call_args[0][0]
        expected_url = f"{_ROCKY9_SRPM_URL}/{srpm}"
        assert expected_url in cmd

    def test_404_falls_back_to_vault(self, tmp_path: Path) -> None:
        import subprocess as _sp

        srpm = "kernel-5.14.0-503.40.1.el9_5.src.rpm"
        cache_dir = tmp_path / "cache"
        calls: list[str] = []

        def side_effect(cmd, *args, **kwargs):
            out_idx = cmd.index("-o") + 1
            url = cmd[-1]
            calls.append(url)
            if "/pub/rocky/9/" in url:
                raise _sp.CalledProcessError(22, cmd)
            Path(cmd[out_idx]).write_bytes(b"fake srpm")
            r = MagicMock()
            r.returncode = 0
            return r

        with patch(
            "ltvm_pkg.kernel_build.subprocess.run",
            new=MagicMock(side_effect=side_effect),
        ):
            download_srpm(srpm, cache_dir, _ROCKY9_SRPM_URL)

        assert len(calls) == 2
        assert "/pub/rocky/9/" in calls[0]
        assert "/vault/rocky/9.5/" in calls[1]
        assert calls[1].endswith(srpm)


class TestSrpmFallbackUrls:
    _PUB = "https://dl.rockylinux.org/pub/rocky/9/BaseOS/source/tree/Packages/k"

    def test_rocky_pub_to_vault(self) -> None:
        urls = _srpm_fallback_urls(
            self._PUB, "kernel-5.14.0-503.40.1.el9_5.src.rpm"
        )
        assert urls == [
            "https://dl.rockylinux.org/vault/rocky/9.5/BaseOS/source/tree/Packages/k/kernel-5.14.0-503.40.1.el9_5.src.rpm"
        ]

    def test_non_rocky_url_no_fallback(self) -> None:
        assert _srpm_fallback_urls("https://example.com/srpms", "kernel-x.src.rpm") == []

    def test_non_el_srpm_no_fallback(self) -> None:
        assert _srpm_fallback_urls(self._PUB, "kernel-something.src.rpm") == []

    def test_el_major_mismatch_no_fallback(self) -> None:
        assert _srpm_fallback_urls(
            self._PUB, "kernel-4.18.0-553.89.1.el8_10.src.rpm"
        ) == []


# ------------------------------------------------------------------
# SRPM-not-found diagnostics
# ------------------------------------------------------------------


class TestLustreTargetFamily:
    def test_rhel9(self) -> None:
        assert _lustre_target_family("5.14-rhel9.7") == "rhel9"

    def test_rhel8_double_digit_minor(self) -> None:
        assert _lustre_target_family("4.18-rhel8.10") == "rhel8"

    def test_non_matching(self) -> None:
        assert _lustre_target_family("random") is None


class TestListLustreKernelTargets:
    def test_picks_matching_family_only(self, tmp_path: Path) -> None:
        td = tmp_path / "lustre" / "kernel_patches" / "targets"
        td.mkdir(parents=True)
        for n in (
            "5.14-rhel9.0",
            "5.14-rhel9.5",
            "5.14-rhel9.7",
            "4.18-rhel8.10",
            "3.10-rhel7.9",
        ):
            (td / f"{n}.target.in").write_text("")
        out = _list_lustre_kernel_targets(tmp_path, "rhel9")
        assert out == ["5.14-rhel9.0", "5.14-rhel9.5", "5.14-rhel9.7"]

    def test_natural_sort_of_minor(self, tmp_path: Path) -> None:
        td = tmp_path / "lustre" / "kernel_patches" / "targets"
        td.mkdir(parents=True)
        for n in ("4.18-rhel8.2", "4.18-rhel8.10", "4.18-rhel8.1"):
            (td / f"{n}.target.in").write_text("")
        out = _list_lustre_kernel_targets(tmp_path, "rhel8")
        assert out == ["4.18-rhel8.1", "4.18-rhel8.2", "4.18-rhel8.10"]

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert _list_lustre_kernel_targets(tmp_path, "rhel9") == []


class TestDiagnoseSrpmNotFound:
    _PUB = "https://dl.rockylinux.org/pub/rocky/9/BaseOS/source/tree/Packages/k"
    _SRPM = "kernel-5.14.0-611.42.1.el9_7.src.rpm"

    def _lustre_tree_with_rhel9(self, tmp_path: Path) -> Path:
        td = tmp_path / "lustre" / "kernel_patches" / "targets"
        td.mkdir(parents=True)
        for n in (
            "5.14-rhel9.0",
            "5.14-rhel9.1",
            "5.14-rhel9.2",
            "5.14-rhel9.3",
            "5.14-rhel9.4",
            "5.14-rhel9.5",
            "5.14-rhel9.6",
            "5.14-rhel9.7",
        ):
            (td / f"{n}.target.in").write_text("")
        return tmp_path

    def test_all_404_plus_index_probe(self, tmp_path: Path) -> None:
        lt = self._lustre_tree_with_rhel9(tmp_path)

        def fake_404(url: str, timeout: float = 5.0) -> bool:
            return True

        def fake_probe(parent: str, srpm: str, timeout: float = 5.0) -> str:
            return "kernel-5.14.0-611.13.1.el9_7.src.rpm"

        with (
            patch(
                "ltvm_pkg.kernel_build._url_returns_404", side_effect=fake_404
            ),
            patch(
                "ltvm_pkg.kernel_build._probe_latest_rocky_srpm",
                side_effect=fake_probe,
            ),
        ):
            err = diagnose_srpm_not_found(
                self._SRPM, self._PUB, "rocky9", "5.14-rhel9.7", lt
            )

        assert isinstance(err, SrpmNotFoundError)
        msg = str(err)
        assert self._SRPM in msg
        assert "kernel-5.14.0-611.13.1.el9_7.src.rpm" in msg
        assert "--kernel 5.14-rhel9.6" in msg
        assert "5.14-rhel9.0" in msg
        assert "5.14-rhel9.7" in msg
        assert "Available Lustre rhel9 targets:" in msg

    def test_offline_skips_probe_but_still_lists_targets(
        self, tmp_path: Path
    ) -> None:
        lt = self._lustre_tree_with_rhel9(tmp_path)

        with (
            patch(
                "ltvm_pkg.kernel_build._url_returns_404",
                return_value=None,
            ),
            patch(
                "ltvm_pkg.kernel_build._probe_latest_rocky_srpm"
            ) as mock_probe,
        ):
            err = diagnose_srpm_not_found(
                self._SRPM, self._PUB, "rocky9", "5.14-rhel9.7", lt
            )

        mock_probe.assert_not_called()
        assert isinstance(err, SrpmNotFoundError)
        msg = str(err)
        assert "could not be probed" in msg or "offline" in msg
        assert "5.14-rhel9.5" in msg

    def test_non_404_returns_none(self, tmp_path: Path) -> None:
        lt = self._lustre_tree_with_rhel9(tmp_path)
        with patch(
            "ltvm_pkg.kernel_build._url_returns_404", return_value=False
        ):
            err = diagnose_srpm_not_found(
                self._SRPM, self._PUB, "rocky9", "5.14-rhel9.7", lt
            )
        assert err is None


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
        cfg.arch = "x86_64"
        cfg.target_dir = target_dir
        cfg.container_tag = "ltvm-build-rocky9"
        cfg.variant_name = "base"
        return cfg

    def test_returns_correct_tag(self, tmp_path: Path) -> None:
        cfg = self._make_target_config(tmp_path)
        with patch("ltvm_pkg.kernel_build.subprocess.run"):
            tag = _ensure_container_image(cfg)
        assert tag == "ltvm-build-rocky9"

    def test_calls_podman_build(self, tmp_path: Path) -> None:
        cfg = self._make_target_config(tmp_path)
        with patch("ltvm_pkg.kernel_build.subprocess.run") as mock_run:
            _ensure_container_image(cfg)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "podman"
        assert "build" in cmd

    def test_podman_build_uses_tag(self, tmp_path: Path) -> None:
        cfg = self._make_target_config(tmp_path)
        with patch("ltvm_pkg.kernel_build.subprocess.run") as mock_run:
            _ensure_container_image(cfg)
        cmd = mock_run.call_args[0][0]
        assert "ltvm-build-rocky9" in cmd

    def test_podman_build_uses_dockerfile(self, tmp_path: Path) -> None:
        cfg = self._make_target_config(tmp_path)
        dockerfile = str(cfg.target_dir / "container.Dockerfile")
        with patch("ltvm_pkg.kernel_build.subprocess.run") as mock_run:
            _ensure_container_image(cfg)
        cmd = mock_run.call_args[0][0]
        assert dockerfile in cmd

    def _platform_after(self, cmd: list[str]) -> str:
        assert "--platform" in cmd
        return cmd[cmd.index("--platform") + 1]

    def test_native_build_uses_host_platform(self, tmp_path: Path) -> None:
        """host == target: --platform resolves to the host arch (no
        emulation)."""
        cfg = self._make_target_config(tmp_path)
        cfg.arch = "x86_64"
        with patch(
            "ltvm_pkg.kernel_build.subprocess.run"
        ) as mock_run, patch(
            "ltvm_pkg.cross_compile.platform.machine",
            return_value="x86_64",
        ):
            _ensure_container_image(cfg)
        assert self._platform_after(mock_run.call_args[0][0]) == "linux/amd64"

    def test_cross_build_picks_host_not_target_platform(
        self, tmp_path: Path
    ) -> None:
        """The core bead-s3f invariant: on a cross host, --platform
        MUST be the host's platform so the container runs natively.
        Forcing target arch here is what caused emulation to kick in
        and silently bypassed the cross-compile code path."""
        cfg = self._make_target_config(tmp_path)
        cfg.arch = "x86_64"  # target
        with patch(
            "ltvm_pkg.kernel_build.subprocess.run"
        ) as mock_run, patch(
            "ltvm_pkg.cross_compile.platform.machine",
            return_value="aarch64",  # host
        ):
            _ensure_container_image(cfg)
        plat = self._platform_after(mock_run.call_args[0][0])
        assert plat == "linux/arm64", (
            "cross build on aarch64 host targeting x86_64 must run the "
            "container as linux/arm64 (host-native), NOT linux/amd64 "
            "(target/emulated) -- otherwise cross-compile-env.sh sees "
            "HOST_ARCH == TARGET_ARCH and never sets CROSSING=1"
        )

    def test_reverse_cross_build_picks_host(self, tmp_path: Path) -> None:
        """Symmetric case: x86_64 host targeting aarch64."""
        cfg = self._make_target_config(tmp_path)
        cfg.arch = "aarch64"  # target
        with patch(
            "ltvm_pkg.kernel_build.subprocess.run"
        ) as mock_run, patch(
            "ltvm_pkg.cross_compile.platform.machine",
            return_value="x86_64",  # host
        ):
            _ensure_container_image(cfg)
        plat = self._platform_after(mock_run.call_args[0][0])
        assert plat == "linux/amd64"


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

    def test_meta_present_returns_unknown_stale_without_extra_hash(
        self, tmp_targets: Path
    ) -> None:
        """Without an extra_hash from the caller, kernel_status returns
        ``stale=None`` (tristate "unknown") instead of guessing.

        ``cmd_status`` has no Lustre tree on hand to recompute the
        round-17 Lustre-inputs portion of the staleness hash.  Round 17
        accidentally reported every kernel as stale (the recompute
        without extra_hash never matched the persisted hash).  Round 18
        over-corrected to always-not-stale, which silently hid genuine
        staleness from the status table.  The right answer is "unknown"
        -- the CLI then renders ``built (?)`` so the user knows the
        check was inconclusive.
        """
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
        assert result["stale"] is None
        assert result["kernel_version"] == "5.14.0-503.26.1.el9_7"

    def test_stale_when_extra_hash_mismatches(self, tmp_targets: Path) -> None:
        """When the caller passes the live Lustre-inputs hash, staleness
        IS computed and a meta with a stale hash is reported stale.
        """
        cfg = _make_config(tmp_targets)
        kernel_dir = cfg.kernel_output_dir()
        kernel_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "input_hash": "deadbeefdeadbeef",
            "kernel_version": "5.14.0-503.26.1.el9_7",
            "srpm": "kernel-5.14.0-503.26.1.el9_7.src.rpm",
        }
        (kernel_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        result = kernel_status(cfg, extra_hash=b"any-non-empty")
        assert result["built"] is True
        assert result["stale"] is True

    def test_not_stale_when_extra_hash_matches(self, tmp_targets: Path) -> None:
        cfg = _make_config(tmp_targets)
        live_extra = b"live-lustre-inputs"
        input_hash = cfg.input_hash("kernel", extra=live_extra)
        kernel_dir = cfg.kernel_output_dir()
        kernel_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "input_hash": input_hash,
            "kernel_version": "5.14.0-503.26.1.el9_7",
            "srpm": "kernel-5.14.0-503.26.1.el9_7.src.rpm",
        }
        (kernel_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        result = kernel_status(cfg, extra_hash=live_extra)
        assert result["built"] is True
        assert result["stale"] is False

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


class TestApplySrpmOverride:
    _TI = {
        "lnxmaj": "6.12.0",
        "lnxrel": "55.43.1.el10_0",
        "srpm": "kernel-6.12.0-55.43.1.el10_0.src.rpm",
        "series": "6.12-rhel10.0.series",
    }

    def test_no_override_returns_unchanged(self) -> None:
        assert apply_srpm_override(self._TI, None, "6.12-rhel10.0") is self._TI
        assert apply_srpm_override(self._TI, "", "6.12-rhel10.0") is self._TI

    def test_override_rewrites_srpm(self) -> None:
        result = apply_srpm_override(
            self._TI, "6.12.0-55.41.1.el10_0", "6.12-rhel10.0"
        )
        assert result["lnxmaj"] == "6.12.0"
        assert result["lnxrel"] == "55.41.1.el10_0"
        assert result["srpm"] == "kernel-6.12.0-55.41.1.el10_0.src.rpm"
        assert result["series"] == "6.12-rhel10.0.series"

    def test_override_does_not_mutate_input(self) -> None:
        apply_srpm_override(
            self._TI, "6.12.0-55.41.1.el10_0", "6.12-rhel10.0"
        )
        assert self._TI["lnxrel"] == "55.43.1.el10_0"

    def test_invalid_override_raises(self) -> None:
        with pytest.raises(ValueError, match="must be '<lnxmaj>-<lnxrel>'"):
            apply_srpm_override(self._TI, "nohyphen", "6.12-rhel10.0")


class TestKernelOutputsComplete:
    """Artifact presence check used by the cleanup-EOF tolerance path."""

    def _make_complete(self, base: Path) -> None:
        (base / "vmlinux").write_bytes(b"\x7fELF")
        (base / "vmlinuz").write_bytes(b"MZ")
        bt = base / "build-tree"
        bt.mkdir()
        (bt / ".config").write_text("CONFIG_FOO=y\n")
        mod = base / "modules" / "lib" / "modules" / "6.1.0"
        mod.mkdir(parents=True)
        (mod / "kernel.ko").write_bytes(b"x")

    def test_complete(self, tmp_path: Path) -> None:
        self._make_complete(tmp_path)
        assert _kernel_outputs_complete(tmp_path)

    def test_missing_vmlinux(self, tmp_path: Path) -> None:
        self._make_complete(tmp_path)
        (tmp_path / "vmlinux").unlink()
        assert not _kernel_outputs_complete(tmp_path)

    def test_missing_build_tree_config(self, tmp_path: Path) -> None:
        self._make_complete(tmp_path)
        (tmp_path / "build-tree" / ".config").unlink()
        assert not _kernel_outputs_complete(tmp_path)

    def test_no_modules(self, tmp_path: Path) -> None:
        self._make_complete(tmp_path)
        import shutil as _sh
        _sh.rmtree(tmp_path / "modules")
        (tmp_path / "modules").mkdir()
        assert not _kernel_outputs_complete(tmp_path)


class TestRunKernelPodman:
    """Wrapper that tolerates cleanup EOF when outputs are on disk."""

    def _populate_outputs(self, base: Path) -> None:
        (base / "vmlinux").write_bytes(b"\x7fELF")
        (base / "vmlinuz").write_bytes(b"MZ")
        bt = base / "build-tree"
        bt.mkdir()
        (bt / ".config").write_text("CONFIG_FOO=y\n")
        mod = base / "modules" / "lib" / "modules" / "6.1.0"
        mod.mkdir(parents=True)
        (mod / "kernel.ko").write_bytes(b"x")

    def test_cleanup_eof_with_outputs_is_success(
        self, tmp_path: Path
    ) -> None:
        self._populate_outputs(tmp_path)
        fake = MagicMock()
        fake.returncode = 126
        fake.cleanup_eof = True
        with patch(
            "ltvm_pkg.kernel_build.run_podman_with_cleanup", return_value=fake
        ):
            _run_kernel_podman(["podman", "run", "foo"], tmp_path)

    def test_cleanup_eof_without_outputs_raises(
        self, tmp_path: Path
    ) -> None:
        import subprocess as _subprocess

        fake = MagicMock()
        fake.returncode = 126
        fake.cleanup_eof = True
        with patch(
            "ltvm_pkg.kernel_build.run_podman_with_cleanup", return_value=fake
        ):
            with pytest.raises(_subprocess.CalledProcessError):
                _run_kernel_podman(["podman", "run", "foo"], tmp_path)

    def test_nonzero_without_eof_raises(self, tmp_path: Path) -> None:
        import subprocess as _subprocess

        self._populate_outputs(tmp_path)
        fake = MagicMock()
        fake.returncode = 2
        fake.cleanup_eof = False
        with patch(
            "ltvm_pkg.kernel_build.run_podman_with_cleanup", return_value=fake
        ):
            with pytest.raises(_subprocess.CalledProcessError):
                _run_kernel_podman(["podman", "run", "foo"], tmp_path)

    def test_success_returns(self, tmp_path: Path) -> None:
        fake = MagicMock()
        fake.returncode = 0
        fake.cleanup_eof = False
        with patch(
            "ltvm_pkg.kernel_build.run_podman_with_cleanup", return_value=fake
        ):
            _run_kernel_podman(["podman", "run", "foo"], tmp_path)
