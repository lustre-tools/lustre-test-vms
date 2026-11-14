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

import gzip
import hashlib
import json
import os
import platform
import stat
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
from tests.host_marks import x86_host  # noqa: F401

pytestmark = pytest.mark.usefixtures("x86_host")

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
    """A minimal .tar.gz with one top-level directory, byte-stable."""
    buf = BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tf,
    ):
        info = tarfile.TarInfo(f"{top}/zfs.release.in")
        payload = b"zfs\n"
        info.size = len(payload)
        tf.addfile(info, BytesIO(payload))
    return buf.getvalue()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_REAL_EXPECTED_SHA256 = zb.expected_sha256


@pytest.fixture(autouse=True)
def _release_sums(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake release tarballs here stand in for the real ones."""
    monkeypatch.setattr(
        zb, "expected_sha256", lambda v: _sha(_tarball_bytes(f"zfs-{v}"))
    )


class TestFetch:
    def test_cached_tarball_is_reused(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "zfs-2.4.0.tar.gz").write_bytes(_tarball_bytes("zfs-2.4.0"))
        with patch.object(zb.urllib.request, "urlopen") as uo:
            got = zb.fetch_tarball("2.4.0", cache)
        uo.assert_not_called()
        assert got.read_bytes() == _tarball_bytes("zfs-2.4.0")

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


# (name, type, link target) of each member
_Members = list[tuple[str, bytes, str]]

_UNSAFE: dict[str, _Members] = {
    "absolute-name": [("/abs/evil", tarfile.REGTYPE, "")],
    "dotdot-name": [("../evil", tarfile.REGTYPE, "")],
    "absolute-symlink": [("zfs/s", tarfile.SYMTYPE, "/etc/passwd")],
    "symlink-outside": [("zfs/s", tarfile.SYMTYPE, "../../evil")],
    "hardlink-outside": [("zfs/h", tarfile.LNKTYPE, "../evil")],
    "fifo": [("zfs/pipe", tarfile.FIFOTYPE, "")],
    "through-a-symlink": [
        ("zfs/s", tarfile.SYMTYPE, "."),
        ("zfs/s/../../evil", tarfile.REGTYPE, ""),
    ],
}


def _archive(members: _Members) -> BytesIO:
    """A .tar.gz of ``members``; each regular file holds four bytes."""
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, kind, link in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = link
            if info.isreg():
                info.size = 4
                tf.addfile(info, BytesIO(b"zfs\n"))
            else:
                tf.addfile(info)
    buf.seek(0)
    return buf


@pytest.mark.filterwarnings("ignore:Python 3.14 will:DeprecationWarning")
class TestExtract:
    """Debian 12 has Python 3.11.2, which has no tarfile data filter.
    The member check must keep an archive inside its directory there."""

    def _extract(self, members: _Members, dest: Path) -> None:
        with tarfile.open(fileobj=_archive(members), mode="r:gz") as tf:
            zb._extract(tf, dest)

    @pytest.mark.parametrize("data_filter", [True, False])
    def test_safe_archive_is_extracted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        data_filter: bool,
    ) -> None:
        if data_filter and not zb._HAS_DATA_FILTER:
            pytest.skip("this Python has no tarfile data filter")
        monkeypatch.setattr(zb, "_HAS_DATA_FILTER", data_filter)
        self._extract(
            [
                ("zfs/a", tarfile.REGTYPE, ""),
                ("zfs/sub/s", tarfile.SYMTYPE, "../a"),
                ("zfs/h", tarfile.LNKTYPE, "zfs/a"),
            ],
            tmp_path,
        )
        assert (tmp_path / "zfs" / "sub" / "s").read_bytes() == b"zfs\n"
        assert (tmp_path / "zfs" / "h").read_bytes() == b"zfs\n"

    @pytest.mark.parametrize("data_filter", [True, False])
    def test_owner_and_setuid_bits_are_dropped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        data_filter: bool,
    ) -> None:
        if data_filter and not zb._HAS_DATA_FILTER:
            pytest.skip("this Python has no tarfile data filter")
        monkeypatch.setattr(zb, "_HAS_DATA_FILTER", data_filter)
        buf = BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("zfs/tool")
            info.mode, info.uid, info.size = 0o6777, 12345, 4
            tf.addfile(info, BytesIO(b"zfs\n"))
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:gz") as tf:
            zb._extract(tf, tmp_path)
        st = (tmp_path / "zfs" / "tool").stat()
        assert stat.S_IMODE(st.st_mode) == 0o755
        assert st.st_uid == os.getuid()

    @pytest.mark.parametrize("case", sorted(_UNSAFE))
    def test_member_check_refuses_before_it_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
    ) -> None:
        monkeypatch.setattr(zb, "_HAS_DATA_FILTER", False)
        dest = tmp_path / "dest"
        dest.mkdir()
        with pytest.raises(zb.ZfsBuildError, match="archive member"):
            self._extract(_UNSAFE[case], dest)
        assert list(tmp_path.iterdir()) == [dest]
        assert list(dest.iterdir()) == []


# ── build preconditions ──────────────────────────────────


class TestBuildPreconditions:
    def test_debian_is_supported(self, tmp_targets: Path) -> None:
        """The inner script has an apt branch, so debian must not be
        turned away at the door."""
        assert "debian" in zb._SUPPORTED_OS_FAMILIES
        assert "rhel" in zb._SUPPORTED_OS_FAMILIES

    def test_refuses_an_unhandled_family(self, tmp_targets: Path) -> None:
        """Fail here, next to the reason, rather than inside the
        container on a package manager that isn't there."""
        yaml_path = tmp_targets / "targets" / "targets.yaml"
        data = yaml.safe_load(yaml_path.read_text())
        data["targets"]["rocky9"]["os_family"] = "suse"
        data["targets"]["rocky9"]["zfs"] = {"version": "2.4.0"}
        _write_targets_yaml(tmp_targets / "targets", data)
        tc = _make_config(tmp_targets)
        with pytest.raises(zb.ZfsBuildError, match="os_family"):
            zb.build_zfs(tc)

    def test_refuses_a_cross_arch_build(self, tmp_targets: Path) -> None:
        """`--arch` is advertised by every build command, and nothing
        in zfs_build or zfs-build-inner.sh cross-compiles.

        The inner script passes no ARCH / CROSS_COMPILE / --host and
        never sources cross-compile-env.sh (both kernel inner scripts
        do), while the container runs as the HOST -- so a cross-arch
        request configured ZFS with the host gcc against a foreign
        kernel build-tree and failed somewhere inside the container.
        Refuse at the door, next to the reason, as an unsupported
        os_family already is.
        """
        tc = _zfs_tc(tmp_targets)
        other = "aarch64" if platform.machine() != "aarch64" else "x86_64"
        with patch.object(type(tc), "arch", property(lambda self: other)):
            with pytest.raises(zb.ZfsBuildError, match="cross-compile"):
                zb.build_zfs(tc)

    def test_a_native_arch_build_is_not_refused(
        self, tmp_targets: Path
    ) -> None:
        """The guard must not fire on the ordinary case, including an
        explicit --arch naming the host's own arch under either of its
        accepted spellings."""
        from ltvm_pkg.cross_compile import normalize_arch

        tc = _zfs_tc(tmp_targets)
        native = normalize_arch(platform.machine())
        alias = {"x86_64": "amd64", "aarch64": "arm64"}.get(native, native)
        for spelling in (native, alias):
            with patch.object(
                type(tc), "arch", property(lambda self, s=spelling: s)
            ):
                # Gets past the arch guard to the next precondition.
                with pytest.raises(FileNotFoundError, match="build kernel"):
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


class TestInnerScript:
    """The one file that runs inside every build container."""

    def _script(self) -> str:
        return zb.INNER_SCRIPT.read_text()

    def test_has_both_package_managers(self) -> None:
        s = self._script()
        assert "command -v dnf" in s
        assert "command -v apt-get" in s

    def test_libdir_is_per_family(self) -> None:
        """ZFS has to install where the VM's ldconfig looks: lib64 on
        x86_64 EL, the multiarch triplet on Debian.  Getting this wrong
        builds fine and then fails to resolve libzfs at mount time."""
        s = self._script()
        assert "rpm --eval '%{_libdir}'" in s
        assert "dpkg-architecture -qDEB_HOST_MULTIARCH" in s

    def test_refuses_an_unknown_container(self) -> None:
        s = self._script()
        assert "no dnf or apt-get in this container" in s

    def test_verifies_both_consumers_outputs(self) -> None:
        """`make install` can return 0 with a partial DESTDIR, and
        LB_ZFS silently disables ZFS when its probes miss -- so the
        script checks for both halves before the host stamps it."""
        s = self._script()
        for probe in ("zfs.ko", "usr/sbin/zpool", "libzfs.so"):
            assert probe in s
        for probe in ("zfs_config.h", "module/Module.symvers"):
            assert probe in s


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


# ── shared artifacts owned by someone else ───────────────


def _fake_podman_into_mounts(cmd, **kw):
    """Stand in for the inner script, writing where the build mounted."""
    staging = Path(
        next(a for a in cmd if a.endswith(":/zfs-staging")).split(":")[0]
    )
    src = Path(next(a for a in cmd if a.endswith(":/zfs-src")).split(":")[0])
    mod = staging / "lib" / "modules" / _KVER / "extra"
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "zfs.ko").write_bytes(b"\x7fELF")
    (src / "module").mkdir(parents=True, exist_ok=True)
    (src / "zfs_config.h").write_text("")
    (src / "module" / "Module.symvers").write_text("")
    return SimpleNamespace(returncode=0)


def _build(tc, tmp: Path, *, podman=_fake_podman_into_mounts, **kw):
    tb = tmp / "zfs-2.4.0.tar.gz"
    tb.write_bytes(_tarball_bytes("zfs-2.4.0"))
    with (
        patch.object(zb.subprocess, "run") as run,
        patch.object(zb, "fetch_tarball", return_value=tb),
        patch.object(zb, "run_podman_with_cleanup", side_effect=podman) as pm,
    ):
        run.return_value = SimpleNamespace(returncode=0)
        out = zb.build_zfs(tc, version="2.4.0", **kw)
    return out, pm


@pytest.fixture
def not_owner():
    """Run as a user who does not own the kernel artifacts."""
    real = zb.os.geteuid()
    with patch.object(zb.os, "geteuid", return_value=real + 4242):
        yield


def _seed_complete(d: Path, version: str = "2.4.0") -> Path:
    mod = d / "staging" / "lib" / "modules" / _KVER
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "zfs.ko").write_bytes(b"\x7fELF")
    (d / "meta.json").write_text(json.dumps({"zfs_version": version}))
    return d / "staging"


class TestOnlyTheOwnerBuildsShared:
    def test_other_user_builds_into_own_cache(
        self, tmp_targets: Path, not_owner
    ) -> None:
        """Even when the directories would let them write: on a shared
        host `zfs/` can be group-writable through a default ACL, and a
        slot user building there would own what everyone else loads."""
        tc = _zfs_tc(tmp_targets)
        kdir = _seed_kernel(tc)
        (kdir / "zfs").mkdir()
        (kdir / "zfs").chmod(0o777)

        out, pm = _build(tc, tmp_targets)

        pm.assert_called_once()
        assert out == zb.user_zfs_dir(tc, None, "2.4.0")
        assert zb.user_cache_root() in out.parents
        assert list((kdir / "zfs").iterdir()) == []
        assert not zb.is_stale(tc, None, "2.4.0", out_dir=out)

    def test_other_users_build_is_reused(
        self, tmp_targets: Path, not_owner
    ) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        first, _ = _build(tc, tmp_targets)
        again, pm = _build(tc, tmp_targets)
        pm.assert_not_called()
        assert again == first
        with patch.object(zb, "build_zfs") as build:
            src, staging, _v = zb.ensure_zfs(tc, version="2.4.0")
        build.assert_not_called()
        assert (src, staging) == (first / "src", first / "staging")

    def test_force_rebuilds_the_users_own(
        self, tmp_targets: Path, not_owner
    ) -> None:
        tc = _zfs_tc(tmp_targets)
        kdir = _seed_kernel(tc)
        _build(tc, tmp_targets)
        out, pm = _build(tc, tmp_targets, force=True)
        pm.assert_called_once()
        assert out == zb.user_zfs_dir(tc, None, "2.4.0")
        assert not (kdir / "zfs").exists()

    def test_owner_builds_into_shared(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        out, _ = _build(tc, tmp_targets)
        assert out == zb.zfs_dir(tc, None, "2.4.0")
        assert not zb.user_zfs_dir(tc, None, "2.4.0").exists()

    def test_zfs_dir_made_by_another_user_is_not_ours(
        self, tmp_targets: Path
    ) -> None:
        tc = _zfs_tc(tmp_targets)
        kdir = _seed_kernel(tc)
        shared = zb.zfs_dir(tc, None, "2.4.0")
        # "/" stands in for a directory someone else owns.
        assert not zb._shared_is_ours(shared, Path("/"))
        assert zb._shared_is_ours(shared, kdir)

    def test_owner_permission_error_falls_back(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        shared = _seed_zfs(tc, "2.4.0", "stalehash")
        real_rmtree = zb.shutil.rmtree

        def rmtree(p, *a, **k):
            if shared in Path(p).parents or Path(p) == shared / "src":
                raise PermissionError("not ours")
            return real_rmtree(p, *a, **k)

        with patch.object(zb.shutil, "rmtree", side_effect=rmtree):
            out, _ = _build(tc, tmp_targets)
        assert out == zb.user_zfs_dir(tc, None, "2.4.0")
        assert (shared / "src" / "zfs_config.h").is_file()


class TestSharedTreeStaysReadOnly:
    def test_takes_the_kernel_dirs_write_bits(self, tmp_targets: Path) -> None:
        """A shared host's default ACL makes new dirs group-writable."""
        tc = _zfs_tc(tmp_targets)
        kdir = _seed_kernel(tc)
        kdir.chmod(0o755)
        (kdir / "build-tree").chmod(0o775)

        def group_writable(cmd, **kw):
            r = _fake_podman_into_mounts(cmd, **kw)
            for p in (kdir / "zfs").rglob("*"):
                p.chmod(0o775 if p.is_dir() else 0o664)
            (kdir / "zfs").chmod(0o775)
            return r

        out, _ = _build(tc, tmp_targets, podman=group_writable)

        assert out == zb.zfs_dir(tc, None, "2.4.0")
        for p in [kdir / "zfs", *(kdir / "zfs").rglob("*")]:
            assert not p.stat().st_mode & 0o022, p
        # Only the ZFS tree is touched, not the rest of the kernel dir.
        assert (kdir / "build-tree").stat().st_mode & 0o777 == 0o775

    def test_tightened_after_a_failed_build(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        kdir = _seed_kernel(tc)
        kdir.chmod(0o755)

        def fails_group_writable(cmd, **kw):
            (kdir / "zfs").chmod(0o775)
            for p in (kdir / "zfs").rglob("*"):
                p.chmod(0o775 if p.is_dir() else 0o664)
            return SimpleNamespace(returncode=2)

        with pytest.raises(zb.ZfsBuildError):
            _build(tc, tmp_targets, podman=fails_group_writable)
        for p in [kdir / "zfs", *(kdir / "zfs").rglob("*")]:
            assert not p.stat().st_mode & 0o022, p

    def test_a_group_writable_kernel_dir_is_left_alone(
        self, tmp_targets: Path
    ) -> None:
        tc = _zfs_tc(tmp_targets)
        kdir = _seed_kernel(tc)
        kdir.chmod(0o775)
        (kdir / "zfs" / "2.4.0").mkdir(parents=True)
        (kdir / "zfs" / "2.4.0").chmod(0o775)
        zb._no_more_writable_than(kdir / "zfs", kdir)
        assert (kdir / "zfs" / "2.4.0").stat().st_mode & 0o777 == 0o775


class TestSharedBuildIsOnlyUsedWhenTrusted:
    def test_group_writable_shared_is_ignored(
        self, tmp_targets: Path, capsys
    ) -> None:
        tc = _zfs_tc(tmp_targets)
        kdir = _seed_kernel(tc)
        kdir.chmod(0o755)
        shared = _seed_zfs(tc, "2.4.0", _fresh_hash(tc, "2.4.0"))
        (shared / "staging").chmod(0o777)
        assert zb.fresh_zfs_dir(tc, None, "2.4.0") is None
        assert "ignoring the shared ZFS" in capsys.readouterr().err

    def test_foreign_owned_shared_is_untrusted(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        shared = _seed_zfs(tc, "2.4.0", _fresh_hash(tc, "2.4.0"))
        assert "not owned" in (zb._untrusted(shared, Path("/")) or "")

    def test_trusted_shared_wins_over_user(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        _seed_zfs(tc, "2.4.0", _fresh_hash(tc, "2.4.0"))
        (zb.user_zfs_dir(tc, None, "2.4.0") / "staging").mkdir(parents=True)
        assert zb.fresh_zfs_dir(tc, None, "2.4.0") == zb.zfs_dir(
            tc, None, "2.4.0"
        )


class TestBuildLock:
    def test_a_waiting_build_uses_the_one_that_finished(
        self, tmp_targets: Path
    ) -> None:
        import threading

        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        shared = zb.zfs_dir(tc, None, "2.4.0")
        result: dict = {}
        with zb._build_lock(shared):
            t = threading.Thread(
                target=lambda: result.update(
                    zip(("out", "pm"), _build(tc, tmp_targets))
                )
            )
            t.start()
            t.join(0.5)
            assert t.is_alive(), "the second build did not wait"
            _seed_zfs(tc, "2.4.0", _fresh_hash(tc, "2.4.0"))
        t.join(10)
        assert result["out"] == shared
        result["pm"].assert_not_called()


class TestFindZfsStaging:
    def test_the_recorded_build_is_used(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        _seed_complete(zb.zfs_dir(tc, None, "2.4.0"))
        mine = _seed_complete(zb.user_zfs_dir(tc, None, "2.4.0"))
        got = zb.find_zfs_staging(tc, None, "2.4.0", recorded=str(mine.parent))
        assert got == mine

    def test_an_incomplete_recorded_build_is_an_error(
        self, tmp_targets: Path
    ) -> None:
        """No quiet swap to another ZFS: osd_zfs.ko links against this."""
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        _seed_complete(zb.zfs_dir(tc, None, "2.4.0"))
        mine = _seed_complete(zb.user_zfs_dir(tc, None, "2.4.0")).parent
        (mine / "meta.json").unlink()
        with pytest.raises(zb.ZfsBuildError, match=str(mine)):
            zb.find_zfs_staging(tc, None, "2.4.0", recorded=str(mine))

    def test_a_recorded_build_of_another_version_is_an_error(
        self, tmp_targets: Path
    ) -> None:
        tc = _zfs_tc(tmp_targets)
        mine = _seed_complete(zb.user_zfs_dir(tc, None, "2.4.0"), "2.3.4")
        with pytest.raises(zb.ZfsBuildError, match="2.3.4"):
            zb.find_zfs_staging(tc, None, "2.4.0", recorded=str(mine.parent))

    def test_half_installed_staging_is_never_shipped(
        self, tmp_targets: Path
    ) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        shared = _seed_complete(zb.zfs_dir(tc, None, "2.4.0")).parent
        (shared / "meta.json").unlink()
        with pytest.raises(zb.ZfsBuildError, match="no complete"):
            zb.find_zfs_staging(tc, None, "2.4.0")

    def test_unrecorded_takes_shared_then_user(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        mine = _seed_complete(zb.user_zfs_dir(tc, None, "2.4.0"))
        assert zb.find_zfs_staging(tc, None, "2.4.0") == mine
        shared = _seed_complete(zb.zfs_dir(tc, None, "2.4.0"))
        assert zb.find_zfs_staging(tc, None, "2.4.0") == shared

    def test_an_untrusted_recorded_shared_build_is_an_error(
        self, tmp_targets: Path
    ) -> None:
        tc = _zfs_tc(tmp_targets)
        kdir = _seed_kernel(tc)
        kdir.chmod(0o755)
        shared = _seed_complete(zb.zfs_dir(tc, None, "2.4.0")).parent
        (shared / "staging").chmod(0o777)
        with pytest.raises(zb.ZfsBuildError, match="writable"):
            zb.find_zfs_staging(tc, None, "2.4.0", recorded=str(shared))

    def test_a_non_string_record_is_ignored(self, tmp_targets: Path) -> None:
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)
        shared = _seed_complete(zb.zfs_dir(tc, None, "2.4.0"))
        assert zb.find_zfs_staging(tc, None, "2.4.0", recorded=7) == shared

    def test_lustre_staging_meta_records_the_zfs_dir(self) -> None:
        src = Path("ltvm_pkg/lustre_build.py").read_text()
        assert (
            '"zfs_dir": str(Path(zfs_src).parent) if zfs_src else None' in src
        )


class TestTarballChecksum:
    def test_pinned_versions(self) -> None:
        assert _REAL_EXPECTED_SHA256("2.4.0").startswith("7bdf13de")
        assert _REAL_EXPECTED_SHA256("2.3.4").startswith("9ec397cf")

    def test_unpinned_version_uses_the_releases_sums(self) -> None:
        body = (
            "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n"
            + "ab" * 32
            + "  zfs-9.9.9.tar.gz\n-----BEGIN PGP SIGNATURE-----\n"
        ).encode()
        with patch.object(zb.urllib.request, "urlopen") as uo:
            uo.return_value.__enter__.return_value = BytesIO(body)
            assert _REAL_EXPECTED_SHA256("9.9.9") == "ab" * 32
        # Kept in the user's own cache, so the second ask is offline.
        with patch.object(zb.urllib.request, "urlopen") as uo:
            assert _REAL_EXPECTED_SHA256("9.9.9") == "ab" * 32
        uo.assert_not_called()
        saved = zb.user_cache_root() / "zfs-sums" / "zfs-9.9.9.sha256"
        assert saved.read_text().strip() == "ab" * 32

    def test_a_tampered_cache_entry_is_not_used(
        self, tmp_path: Path, capsys
    ) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "zfs-2.4.0.tar.gz").write_bytes(b"not the release")
        payload = _tarball_bytes("zfs-2.4.0")
        with patch.object(zb.urllib.request, "urlopen") as uo:
            uo.return_value.__enter__.return_value = BytesIO(payload)
            got = zb.fetch_tarball("2.4.0", cache)
        assert got.read_bytes() == payload
        assert "sha256" in capsys.readouterr().err

    def test_a_bad_download_is_refused(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        with patch.object(zb.urllib.request, "urlopen") as uo:
            uo.return_value.__enter__.return_value = BytesIO(b"evil")
            with pytest.raises(zb.ZfsBuildError, match="sha256"):
                zb.fetch_tarball("2.4.0", cache)
        assert list(cache.iterdir()) == []

    def test_unpack_checks_what_it_reads(self, tmp_path: Path) -> None:
        tb = tmp_path / "zfs-2.4.0.tar.gz"
        tb.write_bytes(_tarball_bytes("zfs-2.4.0"))
        with pytest.raises(zb.ZfsBuildError, match="sha256"):
            zb._unpack(tb, "2.4.0", tmp_path / "src", "00" * 32)
        zb._unpack(tb, "2.4.0", tmp_path / "src", _sha(tb.read_bytes()))
        assert (tmp_path / "src" / "zfs.release.in").is_file()


@pytest.mark.skipif(
    __import__("os").geteuid() == 0, reason="root writes a read-only dir"
)
class TestTarballCacheFallback:
    def test_readonly_shared_cache_downloads_to_user_cache(
        self, tmp_path: Path
    ) -> None:
        shared = tmp_path / "shared-cache"
        shared.mkdir()
        shared.chmod(0o555)
        payload = _tarball_bytes("zfs-2.4.0")
        try:
            with (
                patch.object(zb, "tarball_cache_dir", return_value=shared),
                patch.object(zb.urllib.request, "urlopen") as uo,
            ):
                uo.return_value.__enter__.return_value = BytesIO(payload)
                got = zb.fetch_tarball("2.4.0")
        finally:
            shared.chmod(0o755)
        assert got == zb.user_tarball_cache_dir() / "zfs-2.4.0.tar.gz"
        assert list(shared.iterdir()) == []

    def test_readonly_shared_cache_is_still_read(self, tmp_path: Path) -> None:
        shared = tmp_path / "shared-cache"
        shared.mkdir()
        (shared / "zfs-2.4.0.tar.gz").write_bytes(_tarball_bytes("zfs-2.4.0"))
        shared.chmod(0o555)
        try:
            with (
                patch.object(zb, "tarball_cache_dir", return_value=shared),
                patch.object(zb.urllib.request, "urlopen") as uo,
            ):
                got = zb.fetch_tarball("2.4.0")
        finally:
            shared.chmod(0o755)
        uo.assert_not_called()
        assert got == shared / "zfs-2.4.0.tar.gz"


# ── --zfs-source: a build of the user's own OpenZFS tree ──


def _zfs_tree(root: Path, *, git: bool = True) -> Path:
    import subprocess as sp

    root.mkdir(parents=True)
    (root / "META").write_text("Meta:          1\nVersion:       2.4.99\n")
    (root / "module").mkdir()
    (root / "module" / "arc.c").write_text("void arc_buf_destroy(void);\n")
    (root / "autogen.sh").write_text("#!/bin/sh\n")
    (root / "autogen.sh").chmod(0o755)
    if git:
        (root / ".gitignore").write_text("*.o\n")
        sp.run(["git", "init", "-q", str(root)], check=True)
    return root


class TestZfsSource:
    """A build of an OpenZFS tree with a fix in it, for a ticket that
    needs one no release carries (LU-4009 wanted arc_buf_destroy
    exported, which 2.4.0 does not)."""

    def test_label_names_version_and_content(self, tmp_path: Path) -> None:
        tree = _zfs_tree(tmp_path / "zfs")
        label = zb.source_label(tree)
        assert label.startswith("2.4.99-src-")
        assert len(label) == len("2.4.99-src-") + 12
        assert zb.source_label(tree) == label
        (tree / "module" / "arc.c").write_text("/* fixed */\n")
        assert zb.source_label(tree) != label

    def test_build_output_in_the_tree_is_not_source(
        self, tmp_path: Path
    ) -> None:
        tree = _zfs_tree(tmp_path / "zfs")
        label = zb.source_label(tree)
        (tree / "module" / "arc.o").write_bytes(b"\x7fELF")
        assert zb.source_label(tree) == label
        assert "module/arc.o" not in zb.source_files(tree)

    def test_a_plain_directory_works_too(self, tmp_path: Path) -> None:
        tree = _zfs_tree(tmp_path / "zfs", git=False)
        assert zb.source_files(tree) == ["META", "autogen.sh", "module/arc.c"]

    def test_not_a_zfs_tree(self, tmp_path: Path) -> None:
        (tmp_path / "x").mkdir()
        with pytest.raises(zb.ZfsBuildError, match="not an OpenZFS"):
            zb.source_label(tmp_path / "x")

    def test_rebuild_hint(self) -> None:
        assert zb.rebuild_flag("2.4.0") == "--zfs-version 2.4.0"
        assert "--zfs-source" in zb.rebuild_flag("2.4.99-src-0123456789ab")

    def test_build_copies_the_tree_and_runs_autogen(
        self, tmp_targets: Path, tmp_path: Path
    ) -> None:
        import subprocess as sp

        real_run = sp.run
        tree = _zfs_tree(tmp_path / "zfs")
        label = zb.source_label(tree)
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)

        def run(cmd, *a, **k):
            if cmd[0] == "git":
                return real_run(cmd, *a, **k)
            return SimpleNamespace(returncode=0)

        with (
            patch.object(zb.subprocess, "run", side_effect=run),
            patch.object(zb, "fetch_tarball") as fetch,
            patch.object(
                zb,
                "run_podman_with_cleanup",
                side_effect=_fake_podman_into_mounts,
            ) as pm,
        ):
            out = zb.build_zfs(tc, source=tree)
        fetch.assert_not_called()
        assert out == zb.zfs_dir(tc, None, label)
        src = out / "src"
        assert (src / "module" / "arc.c").read_text().startswith("void")
        assert not (src / ".git").exists()
        cmd = pm.call_args.args[0]
        assert cmd[-2:] == [
            "-c",
            "cd /zfs-src && ./autogen.sh && exec bash /zfs-build-inner.sh",
        ]
        meta = json.loads((out / "meta.json").read_text())
        assert meta["zfs_version"] == label
        assert meta["zfs_source"] == str(tree.resolve())
        # What deploy checks before shipping it.
        assert zb._incomplete(out, label) is None
        assert zb.find_zfs_staging(tc, None, label, recorded=str(out)) == (
            out / "staging"
        )

    def test_unchanged_tree_is_not_rebuilt(
        self, tmp_targets: Path, tmp_path: Path
    ) -> None:
        import subprocess as sp

        real_run = sp.run
        tree = _zfs_tree(tmp_path / "zfs")
        tc = _zfs_tc(tmp_targets)
        _seed_kernel(tc)

        def run(cmd, *a, **k):
            if cmd[0] == "git":
                return real_run(cmd, *a, **k)
            return SimpleNamespace(returncode=0)

        with (
            patch.object(zb.subprocess, "run", side_effect=run),
            patch.object(
                zb,
                "run_podman_with_cleanup",
                side_effect=_fake_podman_into_mounts,
            ) as pm,
        ):
            zb.ensure_zfs(tc, source=tree)
            zb.ensure_zfs(tc, source=tree)
        assert pm.call_count == 1
