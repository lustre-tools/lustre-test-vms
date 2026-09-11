"""ZFS support: artifact build, staleness, and the Lustre/deploy wiring.

ZFS is an opt-in artifact that sits between the kernel and Lustre.  The
properties worth pinning down are:

  * it never perturbs the container/kernel/image input hashes, which is
    what lets it be an option rather than a target property;
  * it rebuilds when the kernel ABI moves under it, even though
    kernel.release does not change;
  * a Lustre staging tree records which ZFS it was built against, and
    deploy ships exactly that one.
"""

from __future__ import annotations

import json
import tarfile
import urllib.error
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from ltvm_pkg import zfs_build as zb
from tests.conftest import _make_config, _write_targets_yaml

# ── helpers ──────────────────────────────────────────────

_KVER = "5.14.0-611.47.1.el9_7_lustre"


def _zfs_tc(tmp_targets: Path, version: str | None = "2.4.0"):
    """A rocky9 TargetConfig, optionally declaring a zfs version."""
    if version is not None:
        yaml_path = tmp_targets / "targets" / "targets.yaml"
        data = yaml.safe_load(yaml_path.read_text())
        data["targets"]["rocky9"]["zfs"] = {"version": version}
        _write_targets_yaml(tmp_targets / "targets", data)
    return _make_config(tmp_targets)


def _seed_kernel(tc, input_hash: str = "kernelhash1", kernel=None) -> Path:
    out = tc.kernel_output_dir(kernel)
    cfgdir = out / "build-tree" / "include" / "config"
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "kernel.release").write_text(_KVER + "\n")
    (out / "meta.json").write_text(json.dumps({"input_hash": input_hash}))
    return out


def _seed_zfs(tc, version: str, input_hash: str, kernel=None) -> Path:
    """Lay down what a successful ZFS build leaves behind."""
    out = zb.zfs_dir(tc, kernel, version)
    src = zb.zfs_src_dir(tc, kernel, version)
    staging = zb.zfs_staging_dir(tc, kernel, version)
    (src / "module").mkdir(parents=True, exist_ok=True)
    (src / "zfs_config.h").write_text('#define ZFS_META_VERSION "x"\n')
    (src / "module" / "Module.symvers").write_text("")
    mod = staging / "lib" / "modules" / _KVER / "extra"
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "zfs.ko").write_bytes(b"\x7fELF")
    (out / "meta.json").write_text(json.dumps({"input_hash": input_hash}))
    return out


def _fresh_hash(tc, version: str, kernel_hash: str = "kernelhash1") -> str:
    return zb._input_hash(_KVER, version, kernel_hash)


# ── version resolution ───────────────────────────────────


class TestResolveVersion:
    def test_cli_override_wins(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets, "2.4.0")
        assert zb.resolve_zfs_version(tc, "2.2.7") == "2.2.7"

    def test_targets_yaml_next(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets, "2.3.4")
        assert zb.resolve_zfs_version(tc, None) == "2.3.4"

    def test_default_last(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets, version=None)
        assert tc.zfs_version is None
        assert zb.resolve_zfs_version(tc, None) == zb.DEFAULT_ZFS_VERSION

    def test_empty_override_falls_through(self, tmp_targets: Path) -> None:
        """argparse hands us None, but an empty string must not win."""
        tc = _zfs_tc(tmp_targets, "2.3.4")
        assert zb.resolve_zfs_version(tc, "") == "2.3.4"


class TestTargetsYamlSchema:
    def test_zfs_version_parsed(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets, "2.4.0")
        assert tc.zfs_version == "2.4.0"

    def test_numeric_version_is_stringified(self, tmp_targets: Path) -> None:
        """YAML turns an unquoted 2.4 into a float; the URL needs a str."""
        yaml_path = tmp_targets / "targets" / "targets.yaml"
        data = yaml.safe_load(yaml_path.read_text())
        data["targets"]["rocky9"]["zfs"] = {"version": 2.4}
        _write_targets_yaml(tmp_targets / "targets", data)
        tc = _make_config(tmp_targets)
        assert tc.zfs_version == "2.4"

    def test_unknown_zfs_key_rejected(self, tmp_targets: Path) -> None:
        yaml_path = tmp_targets / "targets" / "targets.yaml"
        data = yaml.safe_load(yaml_path.read_text())
        data["targets"]["rocky9"]["zfs"] = {"versoin": "2.4.0"}
        _write_targets_yaml(tmp_targets / "targets", data)
        with pytest.raises(ValueError, match="under 'zfs'"):
            _make_config(tmp_targets)

    def test_non_mapping_zfs_rejected(self, tmp_targets: Path) -> None:
        yaml_path = tmp_targets / "targets" / "targets.yaml"
        data = yaml.safe_load(yaml_path.read_text())
        data["targets"]["rocky9"]["zfs"] = "2.4.0"
        _write_targets_yaml(tmp_targets / "targets", data)
        with pytest.raises(ValueError, match="'zfs' must be a mapping"):
            _make_config(tmp_targets)

    def test_zfs_block_does_not_perturb_artifact_hashes(
        self, tmp_targets: Path
    ) -> None:
        """The whole premise of ZFS-as-an-option.

        No byte of the container, kernel or image depends on the ZFS
        version, so declaring or bumping one must not invalidate any of
        them -- otherwise every existing artifact and published release
        rebuilds for a knob they do not read.
        """
        plain = _make_config(tmp_targets)
        before = {
            a: plain.input_hash(a) for a in ("container", "kernel", "image")
        }
        after = {}
        for version in ("2.4.0", "2.2.7"):
            tc = _zfs_tc(tmp_targets, version)
            after[version] = {
                a: tc.input_hash(a) for a in ("container", "kernel", "image")
            }
        assert after["2.4.0"] == before
        assert after["2.2.7"] == before

    def test_real_targets_yaml_declares_versions(self) -> None:
        """The shipped config must parse and name a version per server
        target -- a typo here only shows up on someone's first --zfs."""
        from ltvm_pkg.target_config import LustreMode, TargetConfig

        for name in ("rocky8", "rocky9", "rocky10"):
            tc = TargetConfig(name)
            assert tc.lustre_mode != LustreMode.CLIENT
            assert tc.zfs_version, f"{name} declares no zfs.version"


# ── artifact paths ───────────────────────────────────────


class TestPaths:
    def test_layout(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        d = zb.zfs_dir(tc, None, "2.4.0")
        assert d.parent.name == "zfs"
        assert d.name == "2.4.0"
        assert d.parent.parent == tc.kernel_output_dir(None)
        assert zb.zfs_src_dir(tc, None, "2.4.0") == d / "src"
        assert zb.zfs_staging_dir(tc, None, "2.4.0") == d / "staging"

    def test_versions_do_not_collide(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        assert zb.zfs_dir(tc, None, "2.4.0") != zb.zfs_dir(tc, None, "2.2.7")

    def test_tarball_cache_is_global(self, tmp_targets: Path) -> None:
        """Shared across targets: the tarball is arch- and distro-
        independent source, and `ltvm target clean` must not cost every
        other target a re-download."""
        tc = _zfs_tc(tmp_targets)
        import ltvm_pkg.target_config as cfg

        with patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"):
            cache = zb.tarball_cache_dir()
        assert tc.output_dir not in cache.parents
        assert cache.name == "zfs"


# ── staleness ────────────────────────────────────────────


class TestStaleness:
    def test_fresh_when_nothing_changed(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        _seed_zfs(tc, "2.4.0", _fresh_hash(tc, "2.4.0"))
        assert zb.is_stale(tc, None, "2.4.0") is False

    def test_stale_without_meta(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        assert zb.is_stale(tc, None, "2.4.0") is True

    def test_stale_when_kernel_abi_moves(self, tmp_targets: Path) -> None:
        """Edit a kernel patch: same kernel.release, new Module.symvers.

        kver alone cannot see this, which is why the kernel artifact's
        own input_hash is in the hash.  Missing it would leave zfs.ko
        linked against the previous symbol versions and unloadable.
        """
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc, "kernelhash1")
        _seed_zfs(tc, "2.4.0", _fresh_hash(tc, "2.4.0", "kernelhash1"))
        assert zb.is_stale(tc, None, "2.4.0") is False
        _seed_kernel(tc, "kernelhash2")
        assert zb.is_stale(tc, None, "2.4.0") is True

    def test_stale_when_inner_script_changes(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        _seed_zfs(tc, "2.4.0", _fresh_hash(tc, "2.4.0"))
        with patch.object(zb, "INNER_SCRIPT", MagicMock()) as script:
            script.read_bytes.return_value = b"different"
            assert zb.is_stale(tc, None, "2.4.0") is True

    def test_stale_when_kernel_unbuilt(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_zfs(tc, "2.4.0", "whatever")
        assert zb.is_stale(tc, None, "2.4.0") is True

    def test_stale_when_modules_missing(self, tmp_targets: Path) -> None:
        """meta.json can outlive the tree it vouches for."""
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        _seed_zfs(tc, "2.4.0", _fresh_hash(tc, "2.4.0"))
        ko = next(zb.zfs_staging_dir(tc, None, "2.4.0").rglob("zfs.ko"))
        ko.unlink()
        assert zb.is_stale(tc, None, "2.4.0") is True

    def test_stale_when_symvers_missing(self, tmp_targets: Path) -> None:
        """Lustre's --with-zfs reads module/Module.symvers; without it
        LB_ZFS silently sets enable_zfs=no and Lustre builds no OSD."""
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        _seed_zfs(tc, "2.4.0", _fresh_hash(tc, "2.4.0"))
        (
            zb.zfs_src_dir(tc, None, "2.4.0") / "module" / "Module.symvers"
        ).unlink()
        assert zb.is_stale(tc, None, "2.4.0") is True

    def test_other_version_is_stale(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        _seed_zfs(tc, "2.4.0", _fresh_hash(tc, "2.4.0"))
        assert zb.is_stale(tc, None, "2.2.7") is True


# ── tarball fetch and unpack ─────────────────────────────


def _tarball_bytes(top: str) -> bytes:
    """A minimal .tar.gz with one top-level directory."""
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(f"{top}/zfs.release.in")
        payload = b"zfs\n"
        info.size = len(payload)
        tf.addfile(info, BytesIO(payload))
    return buf.getvalue()


class TestFetch:
    def test_cached_tarball_is_reused(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "zfs-2.4.0.tar.gz").write_bytes(b"cached")
        with patch.object(zb.urllib.request, "urlopen") as uo:
            got = zb.fetch_tarball("2.4.0", cache)
        uo.assert_not_called()
        assert got.read_bytes() == b"cached"

    def test_empty_cached_tarball_is_not_reused(self, tmp_path: Path) -> None:
        """A zero-byte file is the shape a killed download leaves."""
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "zfs-2.4.0.tar.gz").write_bytes(b"")
        payload = _tarball_bytes("zfs-2.4.0")
        with patch.object(zb.urllib.request, "urlopen") as uo:
            uo.return_value.__enter__.return_value = BytesIO(payload)
            got = zb.fetch_tarball("2.4.0", cache)
        assert got.read_bytes() == payload

    def test_download_writes_cache(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        payload = _tarball_bytes("zfs-2.4.0")
        with patch.object(zb.urllib.request, "urlopen") as uo:
            uo.return_value.__enter__.return_value = BytesIO(payload)
            got = zb.fetch_tarball("2.4.0", cache)
        assert got == cache / "zfs-2.4.0.tar.gz"
        assert got.read_bytes() == payload

    def test_404_names_the_version(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        with patch.object(zb.urllib.request, "urlopen") as uo:
            uo.side_effect = urllib.error.HTTPError(
                "u", 404, "Not Found", {}, None
            )
            with pytest.raises(zb.ZfsBuildError, match="9.9.9"):
                zb.fetch_tarball("9.9.9", cache)

    def test_failed_download_leaves_no_cache_entry(
        self, tmp_path: Path
    ) -> None:
        """Otherwise the next run reuses a truncated tarball forever."""
        cache = tmp_path / "cache"
        with patch.object(zb.urllib.request, "urlopen") as uo:
            uo.side_effect = urllib.error.URLError("boom")
            with pytest.raises(zb.ZfsBuildError):
                zb.fetch_tarball("2.4.0", cache)
        assert list(cache.iterdir()) == []


class TestUnpack:
    def test_contents_land_directly_in_src(self, tmp_path: Path) -> None:
        tb = tmp_path / "zfs-2.4.0.tar.gz"
        tb.write_bytes(_tarball_bytes("zfs-2.4.0"))
        src = tmp_path / "art" / "src"
        zb._unpack(tb, "2.4.0", src)
        assert (src / "zfs.release.in").is_file()

    def test_unexpected_top_dir_tolerated(self, tmp_path: Path) -> None:
        """An rc tag's tarball may not be named after the version."""
        tb = tmp_path / "zfs-2.4.0.tar.gz"
        tb.write_bytes(_tarball_bytes("zfs-2.4.0-rc1"))
        src = tmp_path / "art" / "src"
        zb._unpack(tb, "2.4.0", src)
        assert (src / "zfs.release.in").is_file()

    def test_existing_tree_is_replaced(self, tmp_path: Path) -> None:
        src = tmp_path / "art" / "src"
        src.mkdir(parents=True)
        (src / "stale").write_text("old")
        tb = tmp_path / "zfs-2.4.0.tar.gz"
        tb.write_bytes(_tarball_bytes("zfs-2.4.0"))
        zb._unpack(tb, "2.4.0", src)
        assert not (src / "stale").exists()
        assert (src / "zfs.release.in").is_file()


# ── build preconditions ──────────────────────────────────


class TestBuildPreconditions:
    def test_refuses_non_rhel_family(self, tmp_targets: Path) -> None:
        yaml_path = tmp_targets / "targets" / "targets.yaml"
        data = yaml.safe_load(yaml_path.read_text())
        data["targets"]["rocky9"]["os_family"] = "debian"
        data["targets"]["rocky9"]["zfs"] = {"version": "2.4.0"}
        _write_targets_yaml(tmp_targets / "targets", data)
        tc = _make_config(tmp_targets)
        with pytest.raises(zb.ZfsBuildError, match="os_family"):
            zb.build_zfs(tc)

    def test_refuses_without_kernel_build_tree(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        with pytest.raises(FileNotFoundError, match="build kernel"):
            zb.build_zfs(tc)

    def test_refuses_without_container(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        with patch.object(zb.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=1)
            with pytest.raises(zb.ZfsBuildError, match="build container"):
                zb.build_zfs(tc)

    def test_stale_meta_dropped_before_the_build_runs(
        self, tmp_targets: Path
    ) -> None:
        """A build that dies partway must not leave a meta.json
        vouching for the half-built tree it was replacing."""
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        _seed_zfs(tc, "2.4.0", "stalehash")
        meta = zb.zfs_dir(tc, None, "2.4.0") / "meta.json"
        assert meta.is_file()
        tb = tmp_targets / "zfs-2.4.0.tar.gz"
        tb.write_bytes(_tarball_bytes("zfs-2.4.0"))
        with (
            patch.object(zb.subprocess, "run") as run,
            patch.object(zb, "fetch_tarball", return_value=tb),
            patch.object(zb, "run_podman_with_cleanup") as podman,
        ):
            run.return_value = SimpleNamespace(returncode=0)
            podman.return_value = SimpleNamespace(returncode=1)
            with pytest.raises(zb.ZfsBuildError):
                zb.build_zfs(tc)
        assert not meta.exists()

    def test_cached_build_is_skipped(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        _seed_zfs(tc, "2.4.0", _fresh_hash(tc, "2.4.0"))
        with (
            patch.object(zb.subprocess, "run") as run,
            patch.object(zb, "run_podman_with_cleanup") as podman,
        ):
            run.return_value = SimpleNamespace(returncode=0)
            out = zb.build_zfs(tc, version="2.4.0")
        podman.assert_not_called()
        assert out == zb.zfs_dir(tc, None, "2.4.0")

    def test_ensure_zfs_skips_when_fresh(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        _seed_zfs(tc, "2.4.0", _fresh_hash(tc, "2.4.0"))
        with patch.object(zb, "build_zfs") as build:
            src, staging, ver = zb.ensure_zfs(tc)
        build.assert_not_called()
        assert ver == "2.4.0"
        assert src == zb.zfs_src_dir(tc, None, "2.4.0")
        assert staging == zb.zfs_staging_dir(tc, None, "2.4.0")

    def test_ensure_zfs_builds_when_stale(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        with patch.object(zb, "build_zfs") as build:
            zb.ensure_zfs(tc)
        build.assert_called_once()


class TestPodmanInvocation:
    """The container run has to hand the inner script both consumers'
    output dirs and the kernel it links against."""

    def _run_build(self, tmp_targets: Path):
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        tb = tmp_targets / "zfs-2.4.0.tar.gz"
        tb.write_bytes(_tarball_bytes("zfs-2.4.0"))

        def _fake_podman(cmd, **kw):
            # Stand in for the inner script's outputs so the meta write
            # downstream has something to record.
            staging = zb.zfs_staging_dir(tc, None, "2.4.0")
            mod = staging / "lib" / "modules" / _KVER / "extra"
            mod.mkdir(parents=True, exist_ok=True)
            (mod / "zfs.ko").write_bytes(b"\x7fELF")
            return SimpleNamespace(returncode=0)

        with (
            patch.object(zb.subprocess, "run") as run,
            patch.object(zb, "fetch_tarball", return_value=tb),
            patch.object(
                zb, "run_podman_with_cleanup", side_effect=_fake_podman
            ) as podman,
        ):
            run.return_value = SimpleNamespace(returncode=0)
            zb.build_zfs(tc, version="2.4.0")
        return tc, podman.call_args[0][0]

    def test_mounts_and_env(self, tmp_targets: Path) -> None:
        tc, cmd = self._run_build(tmp_targets)
        joined = " ".join(cmd)
        assert f"{tc.kernel_output_dir(None) / 'build-tree'}:/kernel:ro" in cmd
        assert f"{zb.zfs_src_dir(tc, None, '2.4.0')}:/zfs-src" in cmd
        assert f"{zb.zfs_staging_dir(tc, None, '2.4.0')}:/zfs-staging" in cmd
        assert f"KVER={_KVER}" in cmd
        assert "/zfs-build-inner.sh" in joined

    def test_meta_records_version_and_modules(self, tmp_targets: Path) -> None:
        tc, _cmd = self._run_build(tmp_targets)
        meta = json.loads(
            (zb.zfs_dir(tc, None, "2.4.0") / "meta.json").read_text()
        )
        assert meta["zfs_version"] == "2.4.0"
        assert meta["kernel"] == _KVER
        assert "zfs.ko" in meta["modules"]
        assert meta["input_hash"] == zb._input_hash(
            _KVER, "2.4.0", "kernelhash1"
        )
