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
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

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
        name, _ = _resolve_kernel(
            tmp_path, "6.12-rhel10.1", "6.12-rhel10.0"
        )
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
    (kdir / "meta.json").write_text(
        json.dumps({"kernel_version": "5.14.0-611.test"})
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


class TestPackageTarget:
    def test_base_package(self, tmp_path: Path) -> None:
        out = _make_fake_output(tmp_path)
        dest = tmp_path / "release"

        # Stub out podman-facing export so we don't need a real builder.
        with patch(
            "ltvm_pkg.release_package.export_build_container"
        ) as m:
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
        from ltvm_pkg.release_package import SCHEMA_VERSION, SCHEMA_NAME

        assert manifest["schema"] == f"{SCHEMA_NAME}/{SCHEMA_VERSION}"
        assert "producer" in manifest
        assert manifest["target"] == "rocky9"
        assert manifest["variant"] == DEFAULT_VARIANT
        assert manifest["kernel_version"] == "5.14.0-611.test"
        kinds = {a["kind"] for a in manifest["assets"]}
        assert {"container", "kernel", "image"}.issubset(kinds)

    def test_variant_package(self, tmp_path: Path) -> None:
        out = _make_fake_output(tmp_path, variant="mofed")
        dest = tmp_path / "release"

        with patch(
            "ltvm_pkg.release_package.export_build_container"
        ) as m:
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

    def test_manifest_sha256_matches_assets(self, tmp_path: Path) -> None:
        out = _make_fake_output(tmp_path)
        dest = tmp_path / "release"

        with patch(
            "ltvm_pkg.release_package.export_build_container"
        ) as m:
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

    def test_missing_container_raises(self, tmp_path: Path) -> None:
        out = _make_fake_output(tmp_path)
        (out / "container" / "image.tar").unlink()

        with patch(
            "ltvm_pkg.release_package.export_build_container"
        ) as m:
            m.return_value = out / "container" / "image.tar"
            with pytest.raises(ValueError, match="missing artifacts"):
                package_target(
                    "rocky9",
                    out,
                    kernel="5.14-rhel9.7",
                    dest_dir=tmp_path / "release",
                )


class TestPackageBootable:
    def test_compresses_single_file(self, tmp_path: Path) -> None:
        out = _make_fake_output(tmp_path)
        qcow2 = (
            out / "images" / "5.14-rhel9.7" / "bootable-5.14-rhel9.7.qcow2"
        )
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

    def test_missing_qcow2_raises(self, tmp_path: Path) -> None:
        out = _make_fake_output(tmp_path)
        with pytest.raises(FileNotFoundError, match="bootable qcow2 not found"):
            package_bootable(
                "rocky9",
                out,
                kernel="5.14-rhel9.7",
                dest_dir=tmp_path / "release",
            )


class TestSnapshotLustreVariant:
    """snapshot_lustre still lives in release_package; cover the
    variant-aware destination path."""

    def test_variant_nests_under_subdir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = _make_fake_output(tmp_path)
        kdir = out / "kernels" / "5.14-rhel9.7"

        tree = tmp_path / "lustre-release"
        # Staging is variant-keyed: mofed nests under a /mofed subdir
        # so a base build for the same kernel coexists.
        staging = (
            tree / ".ltvm-staging" / "rocky9" / "x86_64"
            / "5.14-rhel9.7" / "mofed"
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
        env = {**os.environ, "GIT_AUTHOR_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(
            ["git", "init", "-q", str(tree)], check=True
        )
        subprocess.run(
            ["git", "-C", str(tree), "commit", "--allow-empty",
             "-m", "init", "-q"],
            check=True, env=env,
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

        monkeypatch.setattr(
            "ltvm_pkg.release_package.subprocess.run", fake_run
        )

        dest = snapshot_lustre(
            tree, out, "rocky9", kernel="5.14-rhel9.7", variant="mofed"
        )
        assert dest == kdir / "lustre-artifacts" / "mofed"
        assert (dest / ".ltvm-snapshot.json").exists()
