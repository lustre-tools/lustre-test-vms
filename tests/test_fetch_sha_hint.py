"""Tests for the stale-manifest hint in fetch's sha256-mismatch error.

Regression: a published GitHub release had its container asset
re-uploaded out-of-band (215 MB -> 296 MB) without re-running
`ltvm target publish` to refresh the manifest.  Fetch correctly
refused, but the error said only 'sha256 mismatch' -- the user had
to dig through GitHub's asset metadata to figure out the cause.
The size delta is a cheap signal that the release was republished,
so surface it in the error message.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ltvm_pkg.release_package import _expect_sha256


def _write(path: Path, content: bytes) -> None:
    path.write_bytes(content)


class TestExpectSha256Hints:
    def test_match_silent(self, tmp_path: Path) -> None:
        f = tmp_path / "asset"
        _write(f, b"hello")
        # sha256("hello") = 2cf24dba...
        _expect_sha256(
            f,
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        )

    def test_size_mismatch_emits_stale_manifest_hint(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "asset"
        _write(f, b"x" * 296)  # 296 bytes on disk
        with pytest.raises(RuntimeError) as ei:
            _expect_sha256(
                f,
                "deadbeef" * 8,
                expected_size=215,  # manifest says 215
            )
        msg = str(ei.value)
        assert "size: manifest says 215 bytes, downloaded 296 bytes" in msg
        assert "re-uploaded after the manifest" in msg
        assert "ltvm target publish" in msg

    def test_size_match_emits_network_hint(self, tmp_path: Path) -> None:
        f = tmp_path / "asset"
        _write(f, b"x" * 100)
        with pytest.raises(RuntimeError) as ei:
            _expect_sha256(
                f,
                "deadbeef" * 8,
                expected_size=100,  # size agrees -> sha mismatch is suspicious
            )
        msg = str(ei.value)
        assert "network corruption" in msg
        assert "retry" in msg
        # Should not falsely accuse a stale manifest when size agrees.
        assert "manifest" not in msg or "manifest says" not in msg

    def test_no_size_arg_keeps_legacy_message(self, tmp_path: Path) -> None:
        """Older callers (fetch_bootable) don't pass expected_size; the
        function must still raise without crashing -- and the legacy
        message body is still present."""
        f = tmp_path / "asset"
        _write(f, b"x" * 50)
        with pytest.raises(RuntimeError) as ei:
            _expect_sha256(f, "deadbeef" * 8)
        msg = str(ei.value)
        assert "sha256 mismatch" in msg
        # No stale-manifest hint when we have nothing to compare.
        assert "re-uploaded" not in msg
