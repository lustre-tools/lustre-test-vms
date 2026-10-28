"""The podman images ltvm builds and then leaves behind.

Every container build tags ``ltvm-build-<target>[-<arch>][-<variant>]``
and every image build ``ltvm-image-<target>[-<arch>][-<variant>]``, plus
``<that>-final-<hex>`` when it injects a kernel's modules.  Three kinds of
them are dead weight, a few GB each:

- the image a rebuild moved a tag off, whose layers stay untagged;
- ``-final-<hex>``, which only the export that follows its build reads
  (plain ``-final`` before the tag was made per build);
- tags of a target, arch or variant no longer in targets.yaml.

Builds drop the first two themselves (:class:`Superseded`,
:func:`remove_tag`); ``ltvm clean`` offers all three (:func:`dead_tags`).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

_PREFIXES = ("ltvm-build-", "ltvm-image-")
_ARCHES = ("x86_64", "aarch64")
_FINAL_RE = re.compile(r"-final(-[0-9a-f]{8})?$")


def _podman(*args: str) -> str | None:
    """Run podman; its stdout, or None when it failed or is missing."""
    from ltvm_pkg.host_setup import hold_podman_usage

    hold_podman_usage()
    try:
        r = subprocess.run(["podman", *args], capture_output=True, text=True)
    except OSError:
        return None
    return r.stdout if r.returncode == 0 else None


def image_id(tag: str) -> str | None:
    """The image *tag* names, or None when there is none."""
    out = _podman("image", "inspect", "--format", "{{.Id}}", tag)
    return (out or "").strip() or None


def final_image_tag(tag: str) -> str:
    """A tag for one image build's kernel-module stage on top of *tag*."""
    return f"{tag}-final-{secrets.token_hex(4)}"


def remove_tag(tag: str) -> bool:
    """``podman rmi`` *tag*; False, quietly, when podman refuses."""
    return _podman("rmi", tag) is not None


class Superseded:
    """Images that the builds noted here move tags off.

    :meth:`note` each tag before the ``podman build`` that retags it,
    then :meth:`drop` once the whole build has succeeded.  Removal is
    newest first, so a variant image goes before the base it was built
    from, and without ``-f``: podman keeps an image a container or
    another tag still uses.
    """

    def __init__(self) -> None:
        self._noted: list[tuple[str, str]] = []

    def note(self, tag: str) -> None:
        old = image_id(tag)
        if old is not None:
            self._noted.append((tag, old))

    def drop(self) -> None:
        for tag, old in reversed(self._noted):
            if image_id(tag) == old:
                continue
            if remove_tag(old):
                log.info("Removed %s's previous image %s", tag, old[:12])
        self._noted.clear()


@dataclass
class TagInfo:
    tag: str  # without localhost/ and :latest
    size: int
    created: float  # epoch seconds


def ltvm_tags() -> list[TagInfo]:
    """Every ltvm-build-* and ltvm-image-* tag in this user's store."""
    try:
        rows = json.loads(_podman("images", "--format", "json") or "[]")
    except ValueError:
        return []
    found: list[TagInfo] = []
    for row in rows:
        for name in row.get("Names") or []:
            bare = name.removeprefix("localhost/")
            if ":" in bare:
                bare, version = bare.rsplit(":", 1)
                if version != "latest":
                    continue
            if bare.startswith(_PREFIXES):
                found.append(
                    TagInfo(
                        tag=bare,
                        size=int(row.get("Size") or 0),
                        created=float(row.get("Created") or 0),
                    )
                )
    return found


def expected_tags() -> set[str]:
    """Every tag a target, arch and variant in targets.yaml builds."""
    from .image_build import _container_image_tag
    from .target_config import (
        DEFAULT_VARIANT,
        TargetConfig,
        build_container_tag,
        list_targets,
    )

    tags: set[str] = set()
    for name in list_targets():
        for arch in _ARCHES:
            try:
                tc = TargetConfig(name, arch=arch)
            except Exception:
                continue
            image_tag = _container_image_tag(tc)
            tags.add(image_tag)
            for variant in [DEFAULT_VARIANT, *tc._variants]:
                tags.add(build_container_tag(name, arch, variant))
                if variant != DEFAULT_VARIANT:
                    tags.add(f"{image_tag}-{variant}")
    return tags


def dead_tags() -> list[tuple[TagInfo, str]]:
    """The ltvm tags nothing builds on or from, with the reason."""
    tags = ltvm_tags()
    if not tags:
        return []
    expected = expected_tags()
    dead: list[tuple[TagInfo, str]] = []
    for t in tags:
        if t.tag.startswith("ltvm-image-") and _FINAL_RE.search(t.tag):
            dead.append(
                (t, "image build's export stage; nothing reads it after")
            )
        elif t.tag not in expected:
            dead.append(
                (t, "no target, arch or variant in targets.yaml builds it")
            )
    return dead
