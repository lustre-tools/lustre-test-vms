"""Tests for ltvm_pkg/release_package.py (split-asset, zstd, variants).

The API here is intentionally coarse: integration-style tests that
write fake artifacts to a tmp output dir, run package_target, and
verify the manifest + asset hashes.  Low-level primitives
(_sha256, _variant_suffix, asset naming) get small focused unit tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import ltvm_pkg.release_package as release_package
from ltvm_pkg.release_package import (
    DEFAULT_VARIANT,
    _bootable_asset_name,
    _container_asset_name,
    _image_asset_name,
    _kernel_asset_name,
    _lustre_asset_name,
    _manifest_name,
    _resolve_kernel,
    _sha256,
    _variant_suffix,
    package_bootable,
    package_target,
    snapshot_lustre,
)

# Tests that really build and unpack the assets need the tools that do
# it.  They are integration tests by design -- mocking tar and zstd
# would leave them asserting nothing about the tarballs they exist to
# check -- so on a host without them the honest outcome is a skip that
# names what is missing, not eleven failures deep inside subprocess.
# `sudo ltvm install` installs all three.
_HOST_TOOLS = ("tar", "zstd", "rsync")
_MISSING_TOOLS = [t for t in _HOST_TOOLS if shutil.which(t) is None]

needs_host_tools = pytest.mark.skipif(
    bool(_MISSING_TOOLS),
    reason=(
        f"needs host tool(s) {', '.join(_MISSING_TOOLS)}; "
        f"`sudo ltvm install` installs them"
    ),
)


@pytest.fixture
def no_zstd_preflight() -> Iterator[None]:
    """Neutralize the zstd presence check.

    For the tests that assert a clear error for a *missing input*: that
    check runs first, so without this they failed on a host without zstd
    having never reached the behaviour under test -- and they have no
    need of zstd to reach it.
    """
    with patch.object(release_package, "_check_zstd"):
        yield


# ---------------------------------------------------------------------------
# Low-level unit tests
# ---------------------------------------------------------------------------


class TestVariantSuffix:
    def test_base_is_empty(self) -> None:
        assert _variant_suffix(DEFAULT_VARIANT) == ""

    def test_non_base(self) -> None:
        assert _variant_suffix("mofed") == "-mofed"


class TestAssetNames:
    def test_container_base(self) -> None:
        assert (
            _container_asset_name("rocky9", "x86_64", "base")
            == "container-rocky9-x86_64.tar.zst"
        )

    def test_container_variant(self) -> None:
        assert (
            _container_asset_name("rocky9", "x86_64", "mofed")
            == "container-rocky9-x86_64-mofed.tar.zst"
        )

    def test_kernel_is_variant_independent(self) -> None:
        # Kernel assets deliberately drop the variant suffix so the
        # same bytes serve every variant (kernel is shared).
        kv = "5.14.0-611.13.1.el9_7_lustre"
        assert (
            _kernel_asset_name("rocky9", "x86_64", kv)
            == f"kernel-rocky9-x86_64-{kv}.tar.zst"
        )

    def test_image_variant(self) -> None:
        kv = "5.14.0-611"
        assert (
            _image_asset_name("rocky9", "x86_64", kv, "mofed")
            == f"image-rocky9-x86_64-{kv}-mofed.tar.zst"
        )

    def test_lustre_variant(self) -> None:
        kv = "5.14.0-611"
        assert (
            _lustre_asset_name("rocky9", "x86_64", kv, "mofed")
            == f"lustre-rocky9-x86_64-{kv}-mofed.tar.zst"
        )

    def test_bootable_default_ext(self) -> None:
        kv = "5.14.0-611"
        assert (
            _bootable_asset_name("rocky9", "x86_64", kv, "base")
            == f"bootable-rocky9-x86_64-{kv}.qcow2.zst"
        )

    def test_manifest(self) -> None:
        kv = "5.14.0-611"
        assert (
            _manifest_name("rocky9", "x86_64", kv, "mofed")
            == f"manifest-rocky9-x86_64-{kv}-mofed.json"
        )


class TestSha256:
    def test_matches_hashlib(self, tmp_path: Path) -> None:
        p = tmp_path / "f"
        data = b"hello\nworld\n"
        p.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert _sha256(p) == expected


class TestResolveKernel:
    def test_explicit(self, tmp_path: Path) -> None:
        name, path = _resolve_kernel(tmp_path, "my-kernel")
        assert name == "my-kernel"
        assert path == tmp_path / "kernels" / "my-kernel"

    def test_auto_detect_picks_latest_with_vmlinux(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "kernels" / "5.14-a").mkdir(parents=True)
        (tmp_path / "kernels" / "5.14-b").mkdir()
        (tmp_path / "kernels" / "5.14-b" / "vmlinux").write_bytes(b"")
        (tmp_path / "kernels" / "5.14-z").mkdir()  # no vmlinux -> skipped
        name, _ = _resolve_kernel(tmp_path, None)
        assert name == "5.14-b"

    def test_auto_detect_orders_numerically_not_lexically(
        self, tmp_path: Path
    ) -> None:
        """The packaged kernel decides the release tag, so picking the
        lexical max publishes a release named for the older kernel.
        """
        kernels = tmp_path / "kernels"
        older = "4.18-rhel8.10-4.18.0-553.89.1.el8_10"
        newer = "4.18-rhel8.10-4.18.0-553.155.1.el8_10"
        for d in (older, newer):
            (kernels / d).mkdir(parents=True)
            (kernels / d / "vmlinux").write_bytes(b"")
        assert sorted([older, newer])[-1] == older  # the trap
        assert _resolve_kernel(tmp_path, None)[0] == newer

    def test_prefix_match_orders_numerically_not_lexically(
        self, tmp_path: Path
    ) -> None:
        kernels = tmp_path / "kernels"
        older = "4.18-rhel8.10-4.18.0-553.89.1.el8_10"
        newer = "4.18-rhel8.10-4.18.0-553.155.1.el8_10"
        for d in (older, newer):
            (kernels / d).mkdir(parents=True)
        name, _ = _resolve_kernel(tmp_path, "4.18-rhel8.10")
        assert name == newer

    def test_default_kernel_wins_over_newest_built(
        self, tmp_path: Path
    ) -> None:
        """With no --kernel, package the target's declared default.

        Publish must answer the same question build does.  Scanning for
        the newest built kernel instead means `ltvm build all rocky10`
        acts on the 10.0 default while `ltvm target publish rocky10`
        packages 10.1, naming the release for a kernel nobody asked to
        publish.
        """
        kernels = tmp_path / "kernels"
        default = "6.12-rhel10.0-6.12.0-55.41.1.el10_0"
        newer = "6.12-rhel10.1-6.12.0-124.56.1.el10_1"
        for d in (default, newer):
            (kernels / d).mkdir(parents=True)
            (kernels / d / "vmlinux").write_bytes(b"")
        name, _ = _resolve_kernel(tmp_path, None, "6.12-rhel10.0")
        assert name == default

    def test_explicit_kernel_overrides_default(self, tmp_path: Path) -> None:
        """--kernel still selects a non-default kernel."""
        kernels = tmp_path / "kernels"
        for d in (
            "6.12-rhel10.0-6.12.0-55.41.1.el10_0",
            "6.12-rhel10.1-6.12.0-124.56.1.el10_1",
        ):
            (kernels / d).mkdir(parents=True)
        name, _ = _resolve_kernel(tmp_path, "6.12-rhel10.1", "6.12-rhel10.0")
        assert name == "6.12-rhel10.1-6.12.0-124.56.1.el10_1"

    def test_missing_kernels_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="No kernels/ directory"):
            _resolve_kernel(tmp_path, None)

    def test_no_vmlinux_raises(self, tmp_path: Path) -> None:
        (tmp_path / "kernels" / "5.14").mkdir(parents=True)
        with pytest.raises(ValueError, match="No kernel with vmlinux"):
            _resolve_kernel(tmp_path, None)


# ---------------------------------------------------------------------------
# package_target integration test
# ---------------------------------------------------------------------------


def _make_fake_output(tmp: Path, variant: str = DEFAULT_VARIANT) -> Path:
    """Build an output tree with just enough files for package_target.

    Layout follows TargetConfig: base variant uses the pre-variant
    paths, non-base nests under <variant>/.
    """
    out = tmp / "artifacts" / "rocky9" / "x86_64"
    variant_seg = "" if variant == DEFAULT_VARIANT else f"/{variant}"

    # Kernel (variant-independent).
    kdir = out / "kernels" / "5.14-rhel9.7"
    kdir.mkdir(parents=True)
    (kdir / "vmlinux").write_bytes(b"fake-vmlinux")
    (kdir / "vmlinuz").write_bytes(b"fake-vmlinuz")
    (kdir / "build-tree").mkdir()
    (kdir / "build-tree" / "Makefile").write_bytes(b"")
    (kdir / "modules").mkdir()
    (kdir / "modules" / "lib").mkdir()
    # Kernel metas carry lustre_target too; the packager validates
    # against meta_schema.KernelMeta rather than .get()-ing fields.
    (kdir / "meta.json").write_text(
        json.dumps(
            {
                "kernel_version": "5.14.0-611.test",
                "lustre_target": "5.14-rhel9.7",
            }
        )
    )

    # Container (variant-aware).
    cdir = Path(f"{out}/container{variant_seg}")
    cdir.mkdir(parents=True)
    (cdir / "image.tar").write_bytes(b"fake-container-tar")

    # Image (variant-aware).
    idir = Path(f"{out}/images/5.14-rhel9.7{variant_seg}")
    idir.mkdir(parents=True)
    (idir / "base.ext4").write_bytes(b"fake-ext4" * 1024)
    (idir / "meta.json").write_text(
        json.dumps({"kernel_version": "5.14.0-611.test"})
    )

    return out


class TestImageAssetRequiresBaseExt4:
    """`target publish` must not substitute an mke2fs temp file.

    The packager used to fall back to the first non-empty *.ext4
    whenever base.ext4 was missing or zero-length -- which is the exact
    hazard its own comment describes.  The tar member keeps its real
    name, and every consumer of a fetched image looks for "base.ext4"
    specifically, so the asset extracted cleanly and then read as "not
    built": `ltvm create` failed on a target that had just been fetched
    successfully.  Raising is the honest answer.
    """

    def _publish(self, out: Path, dest: Path) -> None:
        # _check_zstd is neutralized because this asserts a guard that
        # runs *before* any tarball is written: without it the test
        # fails on a bare checkout with "zstd not found", which says
        # nothing about the behaviour under test.  (tests/CLAUDE.md's
        # unit-vs-integration rule; the real-tarball tests below carry
        # @needs_host_tools instead.)
        with (
            patch("ltvm_pkg.release_package._check_zstd"),
            patch("ltvm_pkg.release_package.export_build_container") as m,
        ):
            m.return_value = out / "container" / "image.tar"
            package_target(
                "rocky9",
                out,
                kernel="5.14-rhel9.7",
                dest_dir=dest,
                arch="x86_64",
                variant=DEFAULT_VARIANT,
            )

    def test_a_leftover_temp_file_is_not_published(
        self, tmp_path: Path
    ) -> None:
        out = _make_fake_output(tmp_path)
        idir = out / "images" / "5.14-rhel9.7"
        (idir / "base.ext4").unlink()
        # What _export_to_ext4's NamedTemporaryFile leaves behind when
        # the build is killed (OOM during mke2fs -d, say).
        (idir / "ltvm-image-ab12cd.ext4").write_bytes(b"partial" * 1024)

        with pytest.raises(ValueError, match="no usable base.ext4"):
            self._publish(out, tmp_path / "release")

    def test_the_error_names_the_stray(self, tmp_path: Path) -> None:
        out = _make_fake_output(tmp_path)
        idir = out / "images" / "5.14-rhel9.7"
        (idir / "base.ext4").unlink()
        (idir / "ltvm-image-ab12cd.ext4").write_bytes(b"partial" * 1024)

        with pytest.raises(ValueError, match="ltvm-image-ab12cd.ext4"):
            self._publish(out, tmp_path / "release")

    def test_a_zero_length_base_is_refused(self, tmp_path: Path) -> None:
        out = _make_fake_output(tmp_path)
        (out / "images" / "5.14-rhel9.7" / "base.ext4").write_bytes(b"")

        with pytest.raises(ValueError, match="no usable base.ext4"):
            self._publish(out, tmp_path / "release")


class TestPackageTarget:
    @needs_host_tools
    def test_base_package(self, tmp_path: Path) -> None:
        out = _make_fake_output(tmp_path)
        dest = tmp_path / "release"

        # Stub out podman-facing export so we don't need a real builder.
        with patch("ltvm_pkg.release_package.export_build_container") as m:
            m.return_value = out / "container" / "image.tar"
            assets = package_target(
                "rocky9",
                out,
                kernel="5.14-rhel9.7",
                dest_dir=dest,
                arch="x86_64",
                variant=DEFAULT_VARIANT,
            )

        assert "container" in assets
        assert "kernel" in assets
        assert "image" in assets
        assert "manifest" in assets

        for kind, path in assets.items():
            assert path.exists(), f"{kind} asset missing at {path}"

        manifest = json.loads(assets["manifest"].read_text())
        from ltvm_pkg.release_package import SCHEMA_NAME, SCHEMA_VERSION

        assert manifest["schema"] == f"{SCHEMA_NAME}/{SCHEMA_VERSION}"
        assert "producer" in manifest
        assert manifest["target"] == "rocky9"
        assert manifest["variant"] == DEFAULT_VARIANT
        assert manifest["kernel_version"] == "5.14.0-611.test"
        kinds = {a["kind"] for a in manifest["assets"]}
        assert {"container", "kernel", "image"}.issubset(kinds)

    @needs_host_tools
    def test_variant_package(self, tmp_path: Path) -> None:
        out = _make_fake_output(tmp_path, variant="mofed")
        dest = tmp_path / "release"

        with patch("ltvm_pkg.release_package.export_build_container") as m:
            m.return_value = out / "container" / "mofed" / "image.tar"
            assets = package_target(
                "rocky9",
                out,
                kernel="5.14-rhel9.7",
                dest_dir=dest,
                arch="x86_64",
                variant="mofed",
            )

        # Asset names include the -mofed suffix for variant-aware kinds.
        assert "mofed" in assets["container"].name
        assert "mofed" in assets["image"].name
        # Kernel asset stays variant-independent.
        assert "mofed" not in assets["kernel"].name

        manifest = json.loads(assets["manifest"].read_text())
        assert manifest["variant"] == "mofed"

    @needs_host_tools
    def test_manifest_sha256_matches_assets(self, tmp_path: Path) -> None:
        out = _make_fake_output(tmp_path)
        dest = tmp_path / "release"

        with patch("ltvm_pkg.release_package.export_build_container") as m:
            m.return_value = out / "container" / "image.tar"
            assets = package_target(
                "rocky9",
                out,
                kernel="5.14-rhel9.7",
                dest_dir=dest,
                arch="x86_64",
            )

        manifest = json.loads(assets["manifest"].read_text())
        for entry in manifest["assets"]:
            asset_path = dest / entry["name"]
            assert _sha256(asset_path) == entry["sha256"]
            assert asset_path.stat().st_size == entry["size"]

    def test_missing_container_raises(
        self, tmp_path: Path, no_zstd_preflight: None
    ) -> None:
        out = _make_fake_output(tmp_path)
        (out / "container" / "image.tar").unlink()

        with patch("ltvm_pkg.release_package.export_build_container") as m:
            m.return_value = out / "container" / "image.tar"
            with pytest.raises(ValueError, match="missing artifacts"):
                package_target(
                    "rocky9",
                    out,
                    kernel="5.14-rhel9.7",
                    dest_dir=tmp_path / "release",
                )


class TestPackageBootable:
    @needs_host_tools
    def test_compresses_single_file(self, tmp_path: Path) -> None:
        out = _make_fake_output(tmp_path)
        qcow2 = out / "images" / "5.14-rhel9.7" / "bootable-5.14-rhel9.7.qcow2"
        qcow2.write_bytes(b"QCOW2\x00" * 4096)

        dest = tmp_path / "release"
        result = package_bootable(
            "rocky9",
            out,
            kernel="5.14-rhel9.7",
            dest_dir=dest,
            arch="x86_64",
            variant=DEFAULT_VARIANT,
            qcow2_path=qcow2,
        )
        assert result.exists()
        assert result.name.startswith("bootable-rocky9-x86_64-5.14.0-611")
        assert result.name.endswith(".qcow2.zst")

    def test_missing_qcow2_raises(
        self, tmp_path: Path, no_zstd_preflight: None
    ) -> None:
        out = _make_fake_output(tmp_path)
        with pytest.raises(FileNotFoundError, match="bootable qcow2 not found"):
            package_bootable(
                "rocky9",
                out,
                kernel="5.14-rhel9.7",
                dest_dir=tmp_path / "release",
            )


@needs_host_tools
class TestSnapshotLustreVariant:
    """snapshot_lustre still lives in release_package; cover the
    variant-aware destination path."""

    def test_variant_nests_under_subdir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = _make_fake_output(tmp_path)
        kdir = out / "kernels" / "5.14-rhel9.7"

        tree = tmp_path / "lustre-release"
        # Staging is variant-keyed: mofed sits in a sibling dir beside
        # the base one so a base build for the same kernel coexists
        # (nested, the base build's `rm -rf /staging/*` deleted it).
        staging = (
            tree / ".ltvm-staging" / "rocky9" / "x86_64" / "5.14-rhel9.7__mofed"
        )
        modules = staging / "lib" / "modules" / "5.14.0-611.test" / "extra"
        modules.mkdir(parents=True)
        ko = modules / "lustre.ko"
        # snapshot_lustre reads `vermagic` from this .ko (via the
        # in-Python ELF .modinfo parser in ltvm_pkg.paths).  That
        # parser walks real section headers, so the fixture has to be
        # a genuine ELF -- a blob with key=value bytes in it is what a
        # whole-file scan accepted, and a whole-file scan is exactly
        # what returned string-constant matches for real modules.
        from tests.conftest import make_fake_ko

        ko.write_bytes(
            make_fake_ko({"vermagic": "5.14.0-611.test SMP mod_unload"})
        )

        tree.mkdir(exist_ok=True)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q", str(tree)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tree),
                "commit",
                "--allow-empty",
                "-m",
                "init",
                "-q",
            ],
            check=True,
            env=env,
        )

        real_run = subprocess.run

        def fake_run(cmd, *a, **kw):  # type: ignore[no-untyped-def]
            class _R:
                def __init__(self, rc: int, out: str, err: str) -> None:
                    self.returncode = rc
                    self.stdout = out
                    self.stderr = err

            if isinstance(cmd, list) and cmd[:2] == ["modinfo", "-F"]:
                return _R(0, "5.14.0-611.test SMP mod_unload", "")
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr("ltvm_pkg.release_package.subprocess.run", fake_run)

        dest = snapshot_lustre(
            tree, out, "rocky9", kernel="5.14-rhel9.7", variant="mofed"
        )
        assert dest == kdir / "lustre-artifacts" / "mofed"
        assert (dest / ".ltvm-snapshot.json").exists()


# ---------------------------------------------------------------------------
# ZFS asset
# ---------------------------------------------------------------------------


def _add_lustre_snapshot(out: Path, zfs_version: str | None) -> Path:
    """Add a lustre-artifacts snapshot, optionally recording a ZFS."""
    lus = out / "kernels" / "5.14-rhel9.7" / "lustre-artifacts"
    (lus / "lib" / "modules").mkdir(parents=True)
    (lus / "lib" / "modules" / "lustre.ko").write_bytes(b"\x7fELF")
    (lus / ".ltvm-snapshot.json").write_text(
        json.dumps({"ko_count": 1, "zfs_version": zfs_version})
    )
    return lus


def _add_zfs_artifact(out: Path, version: str) -> Path:
    art = out / "kernels" / "5.14-rhel9.7" / "zfs" / version
    mods = art / "staging" / "lib" / "modules" / "5.14.0-611.test" / "extra"
    mods.mkdir(parents=True)
    (mods / "zfs.ko").write_bytes(b"\x7fELF")
    (art / "src").mkdir()
    (art / "src" / "zfs_config.h").write_text("")
    (art / "meta.json").write_text(json.dumps({"zfs_version": version}))
    return art


def _package(out: Path, dest: Path) -> dict:
    with patch("ltvm_pkg.release_package.export_build_container") as m:
        m.return_value = out / "container" / "image.tar"
        return package_target(
            "rocky9",
            out,
            kernel="5.14-rhel9.7",
            dest_dir=dest,
            arch="x86_64",
            variant=DEFAULT_VARIANT,
        )


@needs_host_tools
class TestZfsAsset:
    def test_published_when_lustre_was_built_with_zfs(
        self, tmp_path: Path
    ) -> None:
        out = _make_fake_output(tmp_path)
        _add_lustre_snapshot(out, "2.4.0")
        _add_zfs_artifact(out, "2.4.0")
        assets = _package(out, tmp_path / "release")

        assert "zfs" in assets
        assert assets["zfs"].name == (
            "zfs-rocky9-x86_64-5.14.0-611.test-2.4.0.tar.zst"
        )
        manifest = json.loads(assets["manifest"].read_text())
        assert manifest["zfs_version"] == "2.4.0"
        assert "zfs" in {a["kind"] for a in manifest["assets"]}

    def test_absent_when_lustre_had_no_zfs(self, tmp_path: Path) -> None:
        """The default: a release for a target nobody builds ZFS for
        must not carry 48 MB of it."""
        out = _make_fake_output(tmp_path)
        _add_lustre_snapshot(out, None)
        assets = _package(out, tmp_path / "release")
        assert "zfs" not in assets
        manifest = json.loads(assets["manifest"].read_text())
        assert "zfs_version" not in manifest

    def test_absent_without_a_lustre_snapshot(self, tmp_path: Path) -> None:
        """A kernel-only publish has nothing to tie a ZFS version to."""
        out = _make_fake_output(tmp_path)
        _add_zfs_artifact(out, "2.4.0")
        assets = _package(out, tmp_path / "release")
        assert "zfs" not in assets

    def test_skipped_when_the_artifact_is_missing(self, tmp_path: Path) -> None:
        """Publishing must not die because the ZFS build was cleaned;
        say so and publish the rest."""
        out = _make_fake_output(tmp_path)
        _add_lustre_snapshot(out, "2.4.0")
        assets = _package(out, tmp_path / "release")
        assert "zfs" not in assets
        manifest = json.loads(assets["manifest"].read_text())
        assert "zfs_version" not in manifest
        assert "kernel" in assets  # the rest still published

    def test_carries_staging_not_src(self, tmp_path: Path) -> None:
        """src/ is 180 MB compressed and only needed to *build* Lustre
        --with-zfs, which `ltvm build zfs` redoes in under two minutes."""
        out = _make_fake_output(tmp_path)
        _add_lustre_snapshot(out, "2.4.0")
        _add_zfs_artifact(out, "2.4.0")
        assets = _package(out, tmp_path / "release")

        listing = subprocess.run(
            [
                "tar",
                "--use-compress-program=zstd -d",
                "-tf",
                str(assets["zfs"]),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "zfs/2.4.0/staging/" in listing
        assert "zfs/2.4.0/meta.json" in listing
        assert "zfs/2.4.0/src" not in listing

    def test_kernel_asset_omits_zfs(self, tmp_path: Path) -> None:
        """Otherwise every base fetcher pays for a ZFS they never asked
        for -- the bug mofed-kmods hit before its own exclusion."""
        out = _make_fake_output(tmp_path)
        _add_lustre_snapshot(out, "2.4.0")
        _add_zfs_artifact(out, "2.4.0")
        assets = _package(out, tmp_path / "release")

        listing = subprocess.run(
            [
                "tar",
                "--use-compress-program=zstd -d",
                "-tf",
                str(assets["kernel"]),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "vmlinuz" in listing
        assert "/zfs/" not in listing


class TestPublishedMetadata:
    """What a release says about itself, and what it must not say."""

    @needs_host_tools
    def _manifest(self, tmp_path: Path) -> dict:
        """A real publish, read back.  Stubbing the tarball step would
        leave _asset_entry with no file to stat, so this does the real
        thing -- which is also what checks the manifest describes assets
        that exist."""
        out = _make_fake_output(tmp_path)
        dest = tmp_path / "release"
        with patch("ltvm_pkg.release_package.export_build_container") as m:
            m.return_value = out / "container" / "image.tar"
            package_target(
                "rocky9",
                out,
                kernel="5.14-rhel9.7",
                dest_dir=dest,
                arch="x86_64",
                variant=DEFAULT_VARIANT,
            )
        manifests = list(dest.glob("manifest-*.json"))
        assert manifests, f"no manifest written into {dest}"
        return json.loads(manifests[0].read_text())

    @needs_host_tools
    def test_manifest_names_the_short_kernel(self, tmp_path: Path) -> None:
        """`--kernel` takes the short name, and a consumer should be told
        it rather than reconstructing it from the directory name."""
        assert self._manifest(tmp_path)["kernel_short"] == "5.14-rhel9.7"

    @needs_host_tools
    def test_manifest_declares_its_extraction_layout(
        self, tmp_path: Path
    ) -> None:
        assert self._manifest(tmp_path)["layout"] == "artifacts-root"

    @needs_host_tools
    def test_manifest_carries_the_artifact_input_hashes(
        self, tmp_path: Path
    ) -> None:
        """So a client can tell a release is not current for its formula
        without downloading gigabytes to find out."""
        hashes = self._manifest(tmp_path)["input_hashes"]
        # The fixture writes an image meta and a kernel meta; neither
        # carries input_hash, so the map is honest about what it found
        # rather than inventing entries.
        assert isinstance(hashes, dict)

    @needs_host_tools
    def test_producer_records_a_findable_commit(self, tmp_path: Path) -> None:
        """ltvm_version used to be a bare 7-char describe that need not
        resolve in a fresh clone, because the field it read is never
        written."""
        prod = self._manifest(tmp_path)["producer"]
        assert prod["ltvm_version"]
        if "ltvm_commit" in prod:  # absent outside a git checkout
            assert len(prod["ltvm_commit"]) == 40


class TestTarballsCarryNoPublisherIdentity:
    @needs_host_tools
    def test_members_are_owned_by_numeric_root(self, tmp_path: Path) -> None:
        """Every member used to carry the publisher's username and uid."""
        import ltvm_pkg.release_package as rp

        src = tmp_path / "tree"
        (src / "sub").mkdir(parents=True)
        (src / "sub" / "f").write_text("x")
        out = tmp_path / "a.tar.zst"
        rp._tar_zstd(src, ["sub"], out)

        listing = subprocess.run(
            ["tar", "--numeric-owner", "-tvf", str(out)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "0/0" in listing
        assert os.environ.get("USER", "nobody-xyzzy") not in listing

    @needs_host_tools
    def test_two_runs_produce_identical_bytes(self, tmp_path: Path) -> None:
        """Reproducibility is the precondition for ever deduplicating
        assets by digest."""
        import ltvm_pkg.release_package as rp

        src = tmp_path / "tree"
        src.mkdir()
        (src / "a").write_text("one")
        (src / "b").write_text("two")
        first = tmp_path / "1.tar.zst"
        second = tmp_path / "2.tar.zst"
        rp._tar_zstd(src, ["a", "b"], first)
        rp._tar_zstd(src, ["a", "b"], second)
        assert first.read_bytes() == second.read_bytes()


class TestOversizedAssetWarning:
    def test_warns_past_the_github_cap(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Only the bootable asset was checked, so an oversized kernel or
        image asset was discovered by the upload being rejected --
        after minutes of zstd."""
        import ltvm_pkg.release_package as rp

        big = tmp_path / "kernel-rocky9.tar.zst"
        big.write_bytes(b"")
        with patch.object(
            Path, "stat", lambda self: SimpleNamespace(st_size=3 * 1024**3)
        ):
            rp._warn_if_oversized(big)
        err = capsys.readouterr().err
        assert "2 GiB asset cap" in err
        assert "kernel-rocky9.tar.zst" in err

    def test_silent_under_the_cap(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import ltvm_pkg.release_package as rp

        small = tmp_path / "small.tar.zst"
        small.write_bytes(b"x" * 10)
        rp._warn_if_oversized(small)
        assert capsys.readouterr().err == ""
