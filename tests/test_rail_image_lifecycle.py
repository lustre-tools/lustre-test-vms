"""Keep an image's rail capability through publish, fetch, and cleanup."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ltvm_pkg import image_store, release_package
from ltvm_pkg.rail_image import (
    capability_path,
    require_capability,
    write_capability,
)
from tests.test_clean_command import _make_kernel_dir, _run_prune
from tests.test_package import _make_fake_output, needs_host_tools


def _image(directory: Path, data: bytes, *, supported: bool = True) -> Path:
    """Install a scratch image using the same pair-move API as fetched images."""
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "scratch.ext4"
    source.write_bytes(data)
    if supported:
        write_capability(source)
    return image_store.install_image(source, directory)


def test_install_moves_capability_before_current_changes(
    tmp_path: Path,
) -> None:
    """A consumer must never see current pointing at a marker-less fetched image."""
    source = tmp_path / "download.ext4"
    source.write_bytes(b"supported guest")
    write_capability(source)
    original = image_store.set_current

    def inspect_current(directory: Path, name: str) -> None:
        """Check the complete pair at the exact current-pointer consumer boundary."""
        require_capability(directory / name)
        assert not capability_path(source).exists()
        original(directory, name)

    with patch.object(image_store, "set_current", side_effect=inspect_current):
        selected = image_store.install_image(source, tmp_path)
    assert image_store.current_image(tmp_path) == selected
    require_capability(tmp_path / image_store.CURRENT)


def test_install_rejects_invalid_marker_before_rename(tmp_path: Path) -> None:
    """A corrupt capability must leave the old current image and source intact."""
    old = _image(tmp_path, b"old")
    source = tmp_path / "download.ext4"
    source.write_bytes(b"new")
    capability_path(source).write_bytes(b"2\n")
    with pytest.raises(ValueError, match="capability is invalid"):
        image_store.install_image(source, tmp_path)
    assert image_store.current_image(tmp_path) == old
    assert source.read_bytes() == b"new"
    assert image_store.image_files(tmp_path) == [old]


def test_install_marker_failure_restores_source_pair(tmp_path: Path) -> None:
    """A failed marker move cannot publish or discard the extracted image."""
    old = _image(tmp_path, b"old")
    source = tmp_path / "download.ext4"
    source.write_bytes(b"new")
    write_capability(source)
    rename = os.rename

    def fail_marker(src: Path, dest: Path) -> None:
        """Inject a filesystem failure only when moving the supported marker."""
        if src == capability_path(source):
            raise PermissionError("marker move refused")
        rename(src, dest)

    with patch.object(image_store.os, "rename", side_effect=fail_marker):
        with pytest.raises(PermissionError, match="marker move refused"):
            image_store.install_image(source, tmp_path)
    assert image_store.current_image(tmp_path) == old
    assert source.read_bytes() == b"new"
    require_capability(source)
    assert image_store.image_files(tmp_path) == [old]


def test_install_bounds_collisions_with_orphan_marker(tmp_path: Path) -> None:
    """Never overwrite an existing marker or retry an image name without a bound."""
    source = tmp_path / "download.ext4"
    source.write_bytes(b"new")
    name = "base-20260101T000000-abcdef.ext4"
    marker = capability_path(tmp_path / name)
    marker.write_bytes(b"foreign marker")
    with patch.object(
        image_store, "_versioned_name", return_value=name
    ) as choose:
        with pytest.raises(RuntimeError, match="free versioned image name"):
            image_store.install_image(source, tmp_path)
    assert choose.call_count == 32
    assert marker.read_bytes() == b"foreign marker"
    assert source.read_bytes() == b"new"


@needs_host_tools
@pytest.mark.parametrize("variant", ["base", "mofed"])
@pytest.mark.parametrize("supported", [False, True])
def test_publish_fetch_keeps_capability_with_selected_image(
    tmp_path: Path, variant: str, supported: bool
) -> None:
    """Exercise real archive members, extraction, and versioned image installation."""
    output = _make_fake_output(tmp_path, variant)
    image_dir = output / "images" / "5.14-rhel9.7"
    if variant != "base":
        image_dir /= variant
    older = _image(image_dir, b"older image")
    selected = _image(image_dir, b"selected image", supported=supported)
    with patch.object(release_package, "export_build_container"):
        assets = release_package.package_target(
            "rocky9",
            output,
            kernel="5.14-rhel9.7",
            variant=variant,
            dest_dir=tmp_path / "release",
            include_lustre=False,
        )
    fetched = tmp_path / "fetched"
    staging = fetched / ".staging"
    staging.mkdir(parents=True)
    release_package._untar_zstd(assets["image"], staging)
    relative = image_dir.relative_to(output.parent.parent)
    archive_image = staging / relative / image_store.LEGACY
    assert archive_image.read_bytes() == b"selected image"
    assert capability_path(archive_image).exists() == supported
    legacy = fetched / relative / image_store.LEGACY
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"retained legacy")
    release_package._install_fetched_images(staging, fetched)
    current = image_store.current_image(legacy.parent)
    assert current is not None and current != legacy
    assert current.read_bytes() == selected.read_bytes()
    assert legacy.read_bytes() == b"retained legacy"
    assert not capability_path(legacy).exists()
    assert capability_path(older).exists()
    assert capability_path(current).exists() == supported
    if supported:
        require_capability(current)
    else:
        with pytest.raises(ValueError, match="capability is unavailable"):
            require_capability(current)


def test_publish_rejects_corrupt_selected_marker(tmp_path: Path) -> None:
    """Do not hide an invalid capability by publishing an apparently old image."""
    output = _make_fake_output(tmp_path)
    image = output / "images" / "5.14-rhel9.7" / image_store.LEGACY
    capability_path(image).write_bytes(b"unsupported\n")
    with (
        patch.object(release_package, "_check_zstd"),
        patch.object(release_package, "export_build_container"),
        patch.object(release_package, "_tar_zstd") as archive,
        pytest.raises(ValueError, match="capability is invalid"),
    ):
        release_package.package_target(
            "rocky9",
            output,
            kernel="5.14-rhel9.7",
            dest_dir=tmp_path / "release",
            include_lustre=False,
        )
    archive.assert_not_called()


def test_prune_removes_only_unused_image_and_its_marker(tmp_path: Path) -> None:
    """Retained current and referenced images must retain their own support token."""
    used = _image(tmp_path, b"used")
    unused = _image(tmp_path, b"unused")
    current = _image(tmp_path, b"current")
    with patch.object(
        image_store, "referenced_images", return_value=({str(used)}, [])
    ):
        assert image_store.prune_superseded(tmp_path) == len(b"unused")
    assert not unused.exists() and not capability_path(unused).exists()
    require_capability(used)
    require_capability(current)


def test_remove_image_does_not_follow_replaced_image_link(
    tmp_path: Path,
) -> None:
    """A shared-tree image link cannot select another image's support marker."""
    target = _image(tmp_path / "retained", b"referenced image")
    stale = tmp_path / "replaced.ext4"
    stale.symlink_to(target)
    image_store.remove_image(stale)
    assert not stale.is_symlink()
    assert target.read_bytes() == b"referenced image"
    require_capability(target)


def test_clean_removes_exact_image_marker_only_when_applied(
    tmp_targets: Path,
) -> None:
    """The normal clean consumer pairs individual-file deletion with marker cleanup."""
    arch = tmp_targets / "artifacts" / "rocky9" / "x86_64"
    kernel = "5.14-rhel9.7-5.14.0-611.49.1.el9_7"
    _make_kernel_dir(arch, kernel)
    image_dir = arch / "images" / kernel
    old = _image(image_dir, b"unused")
    current = _image(image_dir, b"current")
    assert _run_prune(tmp_targets, target="rocky9") == 0
    require_capability(old)
    assert _run_prune(tmp_targets, target="rocky9", apply=True) == 0
    assert not old.exists() and not capability_path(old).exists()
    require_capability(current)
