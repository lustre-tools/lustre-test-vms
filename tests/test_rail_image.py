"""Per-image capability survives rebuilds and never blesses a retained old image."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ltvm_pkg import image_build, image_store, rail_image, vm_commands
from ltvm_pkg.rail_topology import resolve_topology


def _export(
    directory: Path, payload: bytes, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Drive the real native install boundary with only external tools replaced."""

    def run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        """Make fake mkfs bytes while preserving the real install and marker writes."""
        if command[0] == "fakeroot":
            # The exporter has already allocated its sole output image.
            (temporary,) = directory.glob("ltvm-image-*.ext4")
            temporary.write_bytes(payload)
        output = "container\n" if kwargs.get("text") else b""
        return subprocess.CompletedProcess(command, 0, output, output)

    with monkeypatch.context() as local:
        local.setattr(image_build.subprocess, "run", run)
        local.setattr(
            image_build, "_is_macos_build_host", MagicMock(return_value=False)
        )
        return image_build._export_to_ext4("image-with-rail-helper", directory)


def test_real_build_keeps_capability_on_each_immutable_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rebuild supports both new and retained profile images, excluding an older legacy file."""
    legacy = tmp_path / "base.ext4"
    legacy.write_bytes(b"image built before rail support")
    (tmp_path / "meta.json").write_text(
        json.dumps({"rail_topology_version": 1})
    )
    first = _export(tmp_path, b"first rail-capable image", monkeypatch)
    second = _export(tmp_path, b"second rail-capable image", monkeypatch)
    assert first != second
    assert first.read_bytes() == b"first rail-capable image"
    assert image_store.current_image(tmp_path) == second
    assert rail_image.capability_path(
        tmp_path / "current.ext4"
    ) == rail_image.capability_path(second)
    for image in (first, second, tmp_path / "current.ext4"):
        rail_image.require_capability(image)
        assert rail_image.capability_path(image).read_bytes() == b"1\n"
    with pytest.raises(ValueError, match="unavailable"):
        rail_image.require_capability(legacy)
    topology = resolve_topology(["tcp", "tcp"], "shared")
    with pytest.raises(SystemExit):
        vm_commands._require_profile_image(str(legacy), topology)
    vm_commands._require_profile_image(str(first), topology)


@pytest.mark.parametrize(
    "raw", [b"", b"0\n", b"2\n", b"true", b"1\ntrailing", b"\xff", b"1" * 17]
)
def test_corrupt_or_unknown_sidecar_is_checked(
    tmp_path: Path, raw: bytes
) -> None:
    """Only the supported version token can admit a profiled VM."""
    image = tmp_path / "base.ext4"
    image.touch()
    rail_image.capability_path(image).write_bytes(raw)
    with pytest.raises(ValueError, match="invalid"):
        rail_image.require_capability(image)


def test_capability_reader_limits_its_file_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file consumer requests at most limit+1 bytes, even for malformed input."""
    image = tmp_path / "base.ext4"
    image.touch()
    sidecar = rail_image.capability_path(image)
    sidecar.write_bytes(b"1" * (rail_image.MAX_CAPABILITY_BYTES + 1))
    opened = MagicMock(wraps=rail_image.os.open)
    read = MagicMock(wraps=rail_image.os.read)
    monkeypatch.setattr(rail_image.os, "open", opened)
    monkeypatch.setattr(rail_image.os, "read", read)
    with pytest.raises(ValueError):
        rail_image.require_capability(image)
    opened.assert_called_once_with(sidecar, os.O_RDONLY | os.O_NONBLOCK)
    assert read.call_args.args[1] == rail_image.MAX_CAPABILITY_BYTES + 1
    assert read.call_count == 1


def test_failed_marker_replace_leaves_no_public_or_temporary_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A storage error cannot publish partial proof or leave a temporary marker."""
    image = tmp_path / "base.ext4"
    image.touch()
    monkeypatch.setattr(
        rail_image.os, "replace", MagicMock(side_effect=OSError("full"))
    )
    with pytest.raises(OSError, match="full"):
        rail_image.write_capability(image)
    assert list(tmp_path.iterdir()) == [image]


def test_missing_image_cannot_reuse_a_stale_sidecar(tmp_path: Path) -> None:
    """A marker left by a failed or removed image is never enough by itself."""
    missing = tmp_path / "base.ext4"
    rail_image.capability_path(missing).write_bytes(b"1\n")
    with pytest.raises(ValueError, match="unavailable"):
        rail_image.require_capability(missing)
    with pytest.raises(ValueError, match="regular file"):
        rail_image.write_capability(missing)


def test_fifo_sidecar_is_rejected_without_waiting_for_a_writer(
    tmp_path: Path,
) -> None:
    """A malformed artifact cannot block startup by replacing its marker with a FIFO."""
    image = tmp_path / "base.ext4"
    image.touch()
    os.mkfifo(rail_image.capability_path(image))
    with pytest.raises(ValueError, match="unavailable"):
        rail_image.require_capability(image)
