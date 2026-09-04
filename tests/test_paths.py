"""Tests for the shared helpers in ltvm_pkg.paths.

read_modinfo_field backs the vermagic check that decides whether a
Lustre staging tree may be snapshotted for release, so a wrong answer
here ships modules that cannot load.  It had no tests before.
"""

from __future__ import annotations

from pathlib import Path

from ltvm_pkg.paths import read_modinfo_field
from tests.conftest import make_fake_ko


class TestReadModinfoField:
    def _ko(self, tmp_path: Path, modinfo: dict[str, str]) -> Path:
        p = tmp_path / "m.ko"
        p.write_bytes(make_fake_ko(modinfo))
        return p

    def test_reads_declared_fields(self, tmp_path: Path) -> None:
        ko = self._ko(
            tmp_path,
            {"vermagic": "5.14.0-611.55.1.el9_7_lustre SMP mod_unload",
             "version": "2.17.58"},
        )
        assert read_modinfo_field(ko, "version") == "2.17.58"
        assert read_modinfo_field(ko, "vermagic").startswith("5.14.0-611")

    def test_absent_field_is_none_not_a_longer_keys_value(
        self, tmp_path: Path
    ) -> None:
        """'version' must not match inside 'rhelversion' / 'srcversion'.

        Real rhel modules declare rhelversion and srcversion but no
        version.  A substring match returned rhelversion's value, so
        read_modinfo_field(ko, "version") answered "9.7" for every one
        of them.
        """
        ko = self._ko(
            tmp_path,
            {"rhelversion": "9.7", "srcversion": "DEADBEEF0123456"},
        )
        assert read_modinfo_field(ko, "version") is None
        assert read_modinfo_field(ko, "rhelversion") == "9.7"
        assert read_modinfo_field(ko, "srcversion") == "DEADBEEF0123456"

    def test_string_constants_outside_modinfo_are_ignored(
        self, tmp_path: Path
    ) -> None:
        """Only .modinfo bytes are entries.

        sunrpc.ko carries the format string
        'server=%s program=%u version=%u protocol=%d', which a
        whole-file scan reported as its version.
        """
        ko = tmp_path / "m.ko"
        blob = make_fake_ko({"license": "GPL"})
        blob += b"server=%s program=%u version=%u protocol=%d\x00"
        ko.write_bytes(blob)
        assert read_modinfo_field(ko, "version") is None
        assert read_modinfo_field(ko, "license") == "GPL"

    def test_non_elf_and_missing_file_return_none(
        self, tmp_path: Path
    ) -> None:
        junk = tmp_path / "junk.ko"
        junk.write_bytes(b"not an elf\x00vermagic=1.2.3\x00")
        assert read_modinfo_field(junk, "vermagic") is None
        assert read_modinfo_field(tmp_path / "nope.ko", "vermagic") is None
