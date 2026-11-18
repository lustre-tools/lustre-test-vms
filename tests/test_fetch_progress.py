"""Tests for the fetch progress display (release_package.FetchProgress).

A fetch is several hundred-MB tarballs, so the display has to answer
"how far through the whole set" as well as "what is downloading now".
These cover the three ways it can go wrong: the total bar being derived
from the current file rather than the set, a line wrapping (which breaks
the redraw's line arithmetic and walks the display down the screen), and
a redrawn bar landing in a log file where nothing can erase it.
"""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import ltvm_pkg.release_package as rp
from ltvm_pkg.release_package import FetchProgress

MB = 1024 * 1024

# One realistic asset set: the four kinds a release publishes.
ASSETS = [
    ("container", "container-rocky9-x86_64-5.14.0-611.55.1.el9_7.tar.zst", 40),
    ("kernel", "kernel-rocky9-x86_64-5.14.0-611.55.1.el9_7.tar.zst", 90),
    ("image", "image-rocky9-x86_64-5.14.0-611.55.1.el9_7.tar.zst", 260),
    ("lustre", "lustre-rocky9-x86_64-5.14.0-611.55.1.el9_7.tar.zst", 70),
]
TOTAL = sum(size for _, _, size in ASSETS) * MB


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _frames(text: str) -> list[tuple[str, str]]:
    """The (item line, total line) pairs a terminal would have shown."""
    out = []
    for chunk in text.split("\x1b[1A"):
        parts = chunk.split("\n")
        if len(parts) < 2:
            continue
        line1 = parts[0].replace("\r", "").replace("\x1b[2K", "")
        line2 = parts[1].replace("\r", "").replace("\x1b[2K", "")
        out.append((line1, line2))
    return out


class TestTotalBar:
    """The bar spans the set, not the file being downloaded."""

    def test_total_reflects_all_assets_not_the_current_one(self) -> None:
        out = _Tty()
        with patch.object(
            rp.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((100, 24)),
        ):
            p = FetchProgress(TOTAL, len(ASSETS), stream=out)
            p.start_item(*ASSETS[0][:1], ASSETS[0][1], ASSETS[0][2] * MB)
            p.update(ASSETS[0][2] * MB)
            p.finish_item()
            p.start_item("kernel", ASSETS[1][1], ASSETS[1][2] * MB)
            p.update(45 * MB)  # half of the second asset
            p.close()
        item, total = _frames(out.getvalue())[-1]
        # Current asset: half of 90 MB.
        assert "45/90 MB" in item
        assert " 50%" in item
        # Whole set: 40 + 45 of 460 MB.
        assert "85/460 MB" in total
        assert " 18%" in total

    def test_finished_assets_stay_counted(self) -> None:
        out = _Tty()
        with patch.object(
            rp.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((100, 24)),
        ):
            p = FetchProgress(TOTAL, len(ASSETS), stream=out)
            for kind, name, size in ASSETS:
                p.start_item(kind, name, size * MB)
                p.update(size * MB)
                p.finish_item()
            p.start_item("extra", "x.tar.zst", 0)
            p.close()
        _, total = _frames(out.getvalue())[-1]
        assert "460/460 MB" in total
        assert "100%" in total

    def test_index_counts_through_the_set(self) -> None:
        out = _Tty()
        with patch.object(
            rp.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((100, 24)),
        ):
            p = FetchProgress(TOTAL, len(ASSETS), stream=out)
            for kind, name, size in ASSETS:
                p.start_item(kind, name, size * MB)
                p.finish_item()
            p.close()
        items = [item for item, _ in _frames(out.getvalue())]
        assert items[0].startswith("  [1/4] ")
        assert items[-1].startswith("  [4/4] ")


class TestCurrentItem:
    """The object being downloaded is named on its own line."""

    def test_names_the_asset_in_flight(self) -> None:
        out = _Tty()
        with patch.object(
            rp.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((100, 24)),
        ):
            p = FetchProgress(TOTAL, len(ASSETS), stream=out)
            p.start_item("image", ASSETS[2][1], ASSETS[2][2] * MB)
            p.update(130 * MB)
            p.close()
        item, _ = _frames(out.getvalue())[-1]
        assert ASSETS[2][1] in item
        assert "130/260 MB" in item

    @pytest.mark.parametrize("phase", ["verifying", "extracting"])
    def test_post_download_phase_is_named(self, phase: str) -> None:
        """A bar frozen at 100% through a sha256 pass reads as a hang."""
        out = _Tty()
        with patch.object(
            rp.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((100, 24)),
        ):
            p = FetchProgress(TOTAL, len(ASSETS), stream=out)
            p.start_item("image", ASSETS[2][1], ASSETS[2][2] * MB)
            p.update(ASSETS[2][2] * MB)
            p.set_phase(phase)
            p.close()
        item, _ = _frames(out.getvalue())[-1]
        assert phase in item


class TestWidth:
    """Neither line may reach the right margin.

    A wrapped line puts the cursor a row below where the redraw thinks
    it is, so `\\x1b[1A` lands on the wrong line and the display marches
    down the screen instead of being overwritten.
    """

    @pytest.mark.parametrize(
        "width", [200, 120, 100, 80, 72, 60, 50, 44, 36, 30, 24, 16, 10]
    )
    def test_no_line_reaches_the_margin(self, width: int) -> None:
        out = _Tty()
        with patch.object(
            rp.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((width, 24)),
        ):
            p = FetchProgress(TOTAL, len(ASSETS), stream=out)
            for kind, name, size in ASSETS:
                p.start_item(kind, name, size * MB)
                for frac in (0.0, 0.5, 1.0):
                    p.update(int(size * MB * frac))
                p.set_phase("extracting")
                p.finish_item()
            p.close()
        for item, total in _frames(out.getvalue()):
            assert len(item) <= width - 1, f"{len(item)} > {width - 1}: {item}"
            assert len(total) <= width - 1, f"{len(total)} > {width - 1}"

    def test_wide_terminal_keeps_every_field(self) -> None:
        out = _Tty()
        with patch.object(
            rp.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((100, 24)),
        ):
            p = FetchProgress(TOTAL, len(ASSETS), stream=out)
            p.start_item("image", ASSETS[2][1], ASSETS[2][2] * MB)
            p._samples.extend([(0.0, 0), (5.0, 100 * MB)])
            p.update(100 * MB)
            p.close()
        _, total = _frames(out.getvalue())[-1]
        assert "MB/s" in total
        assert "eta" in total
        assert "#" in total

    def test_narrow_terminal_drops_fields_rather_than_wrapping(self) -> None:
        out = _Tty()
        with patch.object(
            rp.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((30, 24)),
        ):
            p = FetchProgress(TOTAL, len(ASSETS), stream=out)
            p.start_item("image", ASSETS[2][1], ASSETS[2][2] * MB)
            p._samples.extend([(0.0, 0), (5.0, 100 * MB)])
            p.update(100 * MB)
            p.close()
        _, total = _frames(out.getvalue())[-1]
        assert "MB/s" not in total
        assert "%" in total

    def test_long_name_keeps_its_distinguishing_tail(self) -> None:
        """The kernel version and variant are at the end of the name."""
        out = _Tty()
        name = "image-rocky9-x86_64-5.14.0-611.55.1.el9_7-mofed.tar.zst"
        with patch.object(
            rp.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((50, 24)),
        ):
            p = FetchProgress(TOTAL, len(ASSETS), stream=out)
            p.start_item("image", name, 260 * MB)
            p.close()
        item, _ = _frames(out.getvalue())[-1]
        assert "mofed.tar.zst" in item
        assert "..." in item


class TestNonTty:
    """Off a TTY it is one line per asset -- a redrawn bar in a log is
    noise, and nothing can erase it."""

    def test_one_line_per_asset_and_no_escapes(self) -> None:
        out = io.StringIO()  # isatty() is False
        p = FetchProgress(TOTAL, len(ASSETS), stream=out)
        for kind, name, size in ASSETS:
            p.start_item(kind, name, size * MB)
            p.update(size * MB)
            p.set_phase("extracting")
            p.finish_item()
        p.close()
        text = out.getvalue()
        assert "\x1b" not in text
        assert "\r" not in text
        lines = text.splitlines()
        assert len(lines) == len(ASSETS)
        assert lines[0] == (
            "    [1/4] [container] "
            "container-rocky9-x86_64-5.14.0-611.55.1.el9_7.tar.zst (40 MB)"
        )

    def test_close_writes_nothing(self) -> None:
        out = io.StringIO()
        p = FetchProgress(TOTAL, len(ASSETS), stream=out)
        p.close()
        assert out.getvalue() == ""


class TestCursor:
    def test_close_lands_below_the_bar(self) -> None:
        """Whatever prints next -- a hint, or a traceback -- needs a
        line of its own."""
        out = _Tty()
        with patch.object(
            rp.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((100, 24)),
        ):
            p = FetchProgress(TOTAL, len(ASSETS), stream=out)
            p.start_item("image", ASSETS[2][1], ASSETS[2][2] * MB)
            p.close()
        assert out.getvalue().endswith("\n")

    def test_close_is_idempotent(self) -> None:
        out = _Tty()
        with patch.object(
            rp.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((100, 24)),
        ):
            p = FetchProgress(TOTAL, len(ASSETS), stream=out)
            p.start_item("image", ASSETS[2][1], ASSETS[2][2] * MB)
            p.close()
            before = out.getvalue()
            p.close()
        assert out.getvalue() == before


class TestRate:
    def test_rate_uses_the_recent_window(self) -> None:
        """A fetch that starts fast and then stalls must say so."""
        p = FetchProgress(TOTAL, len(ASSETS), stream=io.StringIO())
        with patch.object(rp.time, "monotonic", side_effect=[0.0, 2.0]):
            p._sample(0)
            p._sample(100 * MB)
        assert p._rate() == pytest.approx(50 * MB)

    def test_rate_unknown_before_two_samples(self) -> None:
        p = FetchProgress(TOTAL, len(ASSETS), stream=io.StringIO())
        assert p._rate() == 0.0

    def test_old_samples_fall_out_of_the_window(self) -> None:
        p = FetchProgress(TOTAL, len(ASSETS), stream=io.StringIO())
        times = [0.0, 1.0, 2.0, 30.0, 31.0]
        with patch.object(rp.time, "monotonic", side_effect=times):
            for i, _ in enumerate(times):
                p._sample(i * 10 * MB)
        assert all(t >= 30.0 - rp._RATE_WINDOW_SECONDS for t, _ in p._samples)


class TestDownloadDrivesTheBar:
    """_download feeds the bar from the file as curl writes it."""

    def _fake_curl(self, tmp_path: Path, chunks: int = 4) -> Path:
        """A curl stand-in that writes its -o file in visible steps."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        curl = bindir / "curl"
        curl.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "dest = sys.argv[sys.argv.index('-o') + 1]\n"
            "with open(dest, 'wb', buffering=0) as f:\n"
            f"    for _ in range({chunks}):\n"
            "        f.write(b'x' * 1024)\n"
            "        time.sleep(0.1)\n"
        )
        curl.chmod(0o755)
        return bindir

    def test_updates_climb_to_the_final_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "PATH", f"{self._fake_curl(tmp_path)}:{os.environ['PATH']}"
        )
        monkeypatch.setattr(rp, "_PROGRESS_POLL_SECONDS", 0.02)
        seen: list[int] = []

        class Recorder:
            def update(self, n: int) -> None:
                seen.append(n)

        dest = tmp_path / "asset.tar.zst"
        rp._download("http://example/asset", dest, progress=Recorder())

        assert dest.stat().st_size == 4 * 1024
        assert seen, "the bar was never fed"
        assert seen == sorted(seen), f"progress went backwards: {seen}"
        assert seen[-1] == 4 * 1024
        assert min(seen) < 4 * 1024, f"only ever saw the final size: {seen}"
        assert len(set(seen)) > 2, f"the bar barely moved: {seen}"

    def test_curl_draws_no_bar_of_its_own(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two bars on one terminal is one too many."""
        captured: dict[str, list[str]] = {}

        class FakeProc:
            def wait(self, timeout: float | None = None) -> int:
                return 0

        def fake_popen(cmd: list[str], *a: Any, **kw: Any) -> FakeProc:
            captured["cmd"] = cmd
            return FakeProc()

        dest = tmp_path / "asset.tar.zst"
        dest.write_bytes(b"x")
        with patch.object(rp.subprocess, "Popen", fake_popen):
            rp._download("http://example/asset", dest, progress=None)
            assert "--progress-bar" in captured["cmd"]
            rp._download(
                "http://example/asset", dest, progress=FetchProgress(1, 1)
            )
            assert "--progress-bar" not in captured["cmd"]


class TestDownloadFailurePaths:
    """Behaviour the poll loop must keep from the subprocess.run version."""

    class _Hang:
        """A curl that never finishes on its own, but does die when
        signalled."""

        def __init__(self, on_first_wait: Exception | None = None) -> None:
            self.on_first_wait = on_first_wait
            self.waits = 0
            self.terminated = False
            self.killed = False

        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            if self.terminated or self.killed:
                return -9
            if self.waits == 1 and self.on_first_wait is not None:
                raise self.on_first_wait
            raise subprocess.TimeoutExpired("curl", timeout or 0)

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    def test_interrupt_removes_the_partial_file(self, tmp_path: Path) -> None:
        dest = tmp_path / "asset.tar.zst"
        dest.write_bytes(b"half a download")
        proc = self._Hang(on_first_wait=KeyboardInterrupt())
        with (
            patch.object(rp.subprocess, "Popen", return_value=proc),
            pytest.raises(KeyboardInterrupt),
        ):
            rp._download("http://example/asset", dest)
        assert not dest.exists()
        assert proc.terminated

    def test_timeout_kills_curl_and_removes_the_file(
        self, tmp_path: Path
    ) -> None:
        dest = tmp_path / "asset.tar.zst"
        dest.write_bytes(b"half a download")
        proc = self._Hang()
        with (
            patch.object(rp.subprocess, "Popen", return_value=proc),
            patch.object(rp, "_DOWNLOAD_TIMEOUT", 0),
            pytest.raises(subprocess.TimeoutExpired),
        ):
            rp._download("http://example/asset", dest)
        assert not dest.exists()
        assert proc.killed

    def test_non_zero_exit_removes_the_file(self, tmp_path: Path) -> None:
        dest = tmp_path / "asset.tar.zst"
        dest.write_bytes(b"a 404 body")

        class Failed:
            def wait(self, timeout: float | None = None) -> int:
                return 22

        with (
            patch.object(rp.subprocess, "Popen", return_value=Failed()),
            pytest.raises(RuntimeError, match="Download failed"),
        ):
            rp._download("http://example/asset", dest)
        assert not dest.exists()

    def test_missing_curl_still_says_so(self, tmp_path: Path) -> None:
        with (
            patch.object(
                rp.subprocess, "Popen", side_effect=FileNotFoundError()
            ),
            pytest.raises(RuntimeError, match="curl not found"),
        ):
            rp._download("http://example/asset", tmp_path / "x")
