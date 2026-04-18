"""Typed schemas for artifacts/**/meta.json files.

Each build artifact type (container, kernel, image) writes a meta.json
alongside its output.  The schema varies by artifact, which made it easy
for new fields to creep in inconsistently -- these TypedDicts document
exactly what keys each writer must produce, and exactly what keys each
reader is allowed to assume are present.

Writers should construct the dict by the matching TypedDict so a missing
required field fails type-check.  Readers call ``require_*_meta()`` to
validate required keys at runtime and raise ``RuntimeError`` on missing
keys -- no silent ``.get(..., "")`` fallbacks on critical fields.

Deliberately no schema_version: this is a single-user tool, artifacts
are disposable (regenerated from source), and migration logic is pure
cost.  Breaking reads force a rebuild -- which is the right outcome.

Common fields present in every meta.json (written by
``TargetConfig.write_meta``):
    - ``target``:     target name (e.g. "rocky9")
    - ``input_hash``: 16-hex staleness key
"""

from __future__ import annotations

from typing import Any, Mapping, TypedDict


class _BaseMeta(TypedDict):
    target: str
    input_hash: str


class ContainerMeta(_BaseMeta, total=False):
    """meta.json schema for build containers."""
    image_tag: str  # required in practice; set by cmd_build_all


class KernelMeta(_BaseMeta, total=False):
    """meta.json schema for built kernels.

    Required: kernel_version, lustre_target, patches_applied,
    vmlinux_bytes, vmlinuz_bytes, built_at.
    """
    kernel_version: str
    lustre_target: str
    patches_applied: int
    vmlinux_bytes: int
    vmlinuz_bytes: int
    built_at: str


class ImageMeta(_BaseMeta, total=False):
    """meta.json schema for VM base images.

    Required: build_date, kernel_name, image_size_mb, build_seconds,
    packages. Optional: with_lustre.
    """
    build_date: str
    kernel_name: str
    image_size_mb: float
    build_seconds: float
    packages: Any
    with_lustre: str | None
    lustre_version: str | None


_KERNEL_REQUIRED = ("kernel_version", "lustre_target")
_CONTAINER_REQUIRED = ("image_tag",)
_IMAGE_REQUIRED = ("kernel_name", "build_date")


def _require(meta: Mapping[str, Any], fields: tuple[str, ...], path: object) -> None:
    missing = [k for k in fields if not meta.get(k)]
    if missing:
        raise RuntimeError(
            f"meta.json missing required fields {missing!r}: {path}"
        )


def require_kernel_meta(meta: Mapping[str, Any], path: object) -> None:
    """Raise RuntimeError if a kernel meta.json lacks required fields."""
    _require(meta, _KERNEL_REQUIRED, path)


def require_container_meta(meta: Mapping[str, Any], path: object) -> None:
    _require(meta, _CONTAINER_REQUIRED, path)


def require_image_meta(meta: Mapping[str, Any], path: object) -> None:
    _require(meta, _IMAGE_REQUIRED, path)
