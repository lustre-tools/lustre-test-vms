"""Bind guest rail support to one immutable image filename with a small sidecar."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from .rail_topology import TOPOLOGY_VERSION

MAX_CAPABILITY_BYTES = 16
CAPABILITY_SUFFIX = ".rail-topology"


def capability_path(image: Path) -> Path:
    """Resolve current-image links so each version keeps its own capability."""
    resolved = image.resolve()
    return Path(str(resolved) + CAPABILITY_SUFFIX)


def write_capability(image: Path) -> None:
    """Atomically stamp an image whose installed guest scripts support this version."""
    if not image.is_file():
        # Builders cannot stamp a missing image or a special device as supported.
        raise ValueError(f"image is not a regular file: {image}")
    destination = capability_path(image)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=destination.parent, prefix=".rail-cap-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o644)
            stream.write(f"{TOPOLOGY_VERSION}\n".encode("ascii"))
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            # Failed writes leave no partial public marker or temporary file.
            temporary.unlink(missing_ok=True)


def require_capability(image: Path) -> None:
    """Read at most 17 bytes; reject old, missing, corrupt, or unknown versions."""
    try:
        if not image.is_file():
            # A stale sidecar cannot grant support to a missing image.
            raise ValueError(f"image is not a regular file: {image}")
        sidecar = capability_path(image)
        descriptor = os.open(sidecar, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                # Nonblocking open plus fstat rejects FIFOs without a race or wait.
                raise ValueError(
                    f"image rail capability is not a regular file: {sidecar}"
                )
            raw = os.read(descriptor, MAX_CAPABILITY_BYTES + 1)
        finally:
            os.close(descriptor)
    except (OSError, ValueError, RuntimeError) as error:
        raise ValueError(
            f"image rail capability is unavailable: {image}"
        ) from error
    expected = str(TOPOLOGY_VERSION).encode("ascii")
    if len(raw) > MAX_CAPABILITY_BYTES or raw not in (
        expected,
        expected + b"\n",
    ):
        # Shared directory metadata cannot grant support to an older retained file.
        raise ValueError(f"image rail capability is invalid: {image}")
