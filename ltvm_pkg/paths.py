"""Single source of truth for the ltvm repo root.

target_config.py and vm_state.py both need to resolve where outputs
live.  Round 19 found the helper duplicated in both modules with a
comment literally saying "must agree with the other one" -- a
maintenance bomb waiting to go off.  This module is the canonical
implementation; both consumers re-export the same function so any
future changes happen in exactly one place.

We deliberately avoid imports beyond pathlib/os so this can be pulled
in by every other module without circular-import risk.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("ltvm")


def load_meta_safe(meta_file: Path) -> dict[str, Any] | None:
    """Read and parse a meta.json file, tolerating corruption.

    Returns the parsed dict on success, or None if the file is missing,
    unreadable, or contains invalid JSON.  A corrupt meta.json (e.g. from
    a build that crashed mid-write, or a partially-truncated artifact)
    must NOT brick subsequent commands -- callers should treat None as
    'no meta' (typically: stale / needs rebuild).

    A warning is logged on parse failure so the user can investigate
    rather than silently re-running the build.
    """
    try:
        return json.loads(meta_file.read_text())  # type: ignore[no-any-return]
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        log.warning("ignoring corrupt meta file %s: %s", meta_file, e)
        return None


def read_modinfo_field(ko_path: Path, field: str) -> str | None:
    """Read a single .modinfo field from a Linux .ko ELF in pure Python.

    Equivalent to ``modinfo -F <field> <ko>`` but doesn't depend on
    the kmod userspace, which is Linux-only and not available on
    macOS hosts that build images via podman machine.

    .ko files are ELF relocatable objects with a ``.modinfo`` section
    containing null-separated ``key=value`` entries.  The bytes pattern
    is regular enough that we can scan the whole file rather than
    walk ELF headers; the cost is reading the (~1MB) module twice in
    edge cases, which is fine for a metadata lookup that runs at most
    a handful of times per build.

    Returns ``None`` if the field isn't present or the file can't be
    read.  Raises nothing; callers can fall back to a default.
    """
    try:
        data = ko_path.read_bytes()
    except OSError:
        return None
    section = _elf_section(data, b".modinfo")
    if section is None:
        return None
    needle = f"{field}=".encode()
    for entry in section.split(b"\x00"):
        if entry.startswith(needle):
            return entry[len(needle):].decode("utf-8", errors="replace")
    return None


def _elf_section(data: bytes, want: bytes) -> bytes | None:
    """Return the contents of ELF section *want*, or None.

    Scanning the whole file for ``<field>=`` instead is not safe: the
    needle also occurs inside longer keys (``version=`` within
    ``rhelversion=9.7``) and inside ordinary string constants -- real
    modules here carry the format string ``program=%u version=%u
    protocol=%d``, so a whole-file scan reported that as sunrpc.ko's
    "version".  Only the bytes of .modinfo are entries.

    Minimal ELF32/ELF64 section-header walk, both endiannesses.
    Returns None for anything malformed; callers treat that as "field
    not present" and fall back.
    """
    import struct

    if len(data) < 64 or data[:4] != b"\x7fELF":
        return None
    is64 = data[4] == 2
    end = "<" if data[5] == 1 else ">"
    try:
        if is64:
            e_shoff, = struct.unpack_from(end + "Q", data, 0x28)
            e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(
                end + "HHH", data, 0x3A
            )
        else:
            e_shoff, = struct.unpack_from(end + "I", data, 0x20)
            e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(
                end + "HHH", data, 0x2E
            )
        if not e_shoff or e_shstrndx >= e_shnum:
            return None

        def sh(i: int) -> tuple[int, int, int]:
            off = e_shoff + i * e_shentsize
            name, = struct.unpack_from(end + "I", data, off)
            if is64:
                s_off, s_size = struct.unpack_from(end + "QQ", data, off + 0x18)
            else:
                s_off, s_size = struct.unpack_from(end + "II", data, off + 0x10)
            return name, s_off, s_size

        _, str_off, str_size = sh(e_shstrndx)
        strtab = data[str_off:str_off + str_size]
        for i in range(e_shnum):
            name_off, s_off, s_size = sh(i)
            nul = strtab.find(b"\x00", name_off)
            if nul < 0:
                continue
            if strtab[name_off:nul] == want:
                return data[s_off:s_off + s_size]
    except (struct.error, IndexError):
        return None
    return None


def find_ltvm_root() -> Path:
    """Resolve the ltvm repo root.

    Resolution order:
      1. LTVM_ROOT environment variable, if set.
      2. /usr/local/bin/ltvm symlink target's parent (the install path).
      3. This file's grandparent (the source-tree fallback).

    Build outputs land under <root>/artifacts/, and the runtime later reads
    them from the same path -- so target_config.py (build side) and
    vm_state.py (runtime side) MUST agree on this resolution.
    """
    env = os.environ.get("LTVM_ROOT")
    if env:
        return Path(env)
    ltvm_link = Path("/usr/local/bin/ltvm")
    if ltvm_link.is_symlink():
        return ltvm_link.resolve().parent
    return Path(__file__).resolve().parent.parent
