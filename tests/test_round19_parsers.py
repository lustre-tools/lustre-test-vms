"""Tests for the parsing helpers added in round 19.

Round 19 found two parsing bugs in the existing code:
  - cmd_restore checked the snapshot tag with substring matching
    against the full `qemu-img snapshot -l` output, which falsely
    accepted any string that happened to appear in any column.
  - _gh_api fetched only the first 30 releases (no pagination), so
    older releases vanished from `ltvm fetch --list`.

The fixes added _parse_snapshot_tags() and _gh_next_link(), both
of which involve fragile text parsing.  Pin their behavior here so
the next refactor can't quietly break either one.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ltvm_pkg import cli
from ltvm_pkg.cli import _gh_next_link, _artifact_label, _gh_api
from ltvm_pkg.vm_commands import _parse_snapshot_tags


# ── _parse_snapshot_tags ─────────────────────────────────


class TestParseSnapshotTags:
    """Pull tag names out of `qemu-img snapshot -l` output."""

    def test_typical_output(self) -> None:
        out = (
            "Snapshot list:\n"
            "ID        TAG               VM SIZE                DATE       VM CLOCK     ICOUNT\n"
            "1         before-test       0 B 2024-01-01 12:00:00   00:00:00.000          0\n"
            "2         after-test        0 B 2024-01-01 12:30:00   00:00:01.234         42\n"
        )
        assert _parse_snapshot_tags(out) == {"before-test", "after-test"}

    def test_empty_output(self) -> None:
        assert _parse_snapshot_tags("") == set()

    def test_no_snapshots(self) -> None:
        """qemu-img output with the header row but no data rows."""
        out = (
            "Snapshot list:\n"
            "ID        TAG               VM SIZE                DATE       VM CLOCK     ICOUNT\n"
        )
        assert _parse_snapshot_tags(out) == set()

    def test_single_tag(self) -> None:
        out = (
            "Snapshot list:\n"
            "ID        TAG               VM SIZE                DATE       VM CLOCK     ICOUNT\n"
            "1         only-snapshot     0 B 2024-01-01 12:00:00   00:00:00.000          0\n"
        )
        assert _parse_snapshot_tags(out) == {"only-snapshot"}

    def test_id_column_not_mistaken_for_tag(self) -> None:
        """The bug round 19 caught: substring match against the full
        output meant a tag like '1' (matching the ID column) falsely
        existed.  Exact column extraction must distinguish ID from TAG.
        """
        out = (
            "Snapshot list:\n"
            "ID        TAG               VM SIZE                DATE       VM CLOCK     ICOUNT\n"
            "1         before-test       0 B 2024-01-01 12:00:00   00:00:00.000          0\n"
        )
        tags = _parse_snapshot_tags(out)
        assert "1" not in tags
        assert "before-test" in tags

    def test_date_substring_not_mistaken_for_tag(self) -> None:
        """A tag like '2024' (substring of the DATE column) must NOT
        be reported as existing under the old substring check."""
        out = (
            "Snapshot list:\n"
            "ID        TAG               VM SIZE                DATE       VM CLOCK     ICOUNT\n"
            "1         before-test       0 B 2024-01-01 12:00:00   00:00:00.000          0\n"
        )
        tags = _parse_snapshot_tags(out)
        assert "2024" not in tags
        assert "2024-01-01" not in tags

    def test_garbage_input_returns_empty(self) -> None:
        """Output without an ID/TAG header line returns empty -- the
        caller will then `die` with 'snapshot not found', which is the
        right answer when we can't read the table."""
        assert _parse_snapshot_tags("complete garbage\nno header\n") == set()


# ── _gh_next_link ────────────────────────────────────────


class TestGhNextLink:
    """Parse the rel="next" URL from a GitHub Link header."""

    def test_no_link_header(self) -> None:
        headers = "HTTP/2 200\nContent-Type: application/json\n"
        assert _gh_next_link(headers) is None

    def test_single_next_link(self) -> None:
        headers = (
            "HTTP/2 200\n"
            'Link: <https://api.github.com/x?page=2>; rel="next"\n'
            "Content-Type: application/json\n"
        )
        assert _gh_next_link(headers) == "https://api.github.com/x?page=2"

    def test_next_and_last(self) -> None:
        """Real GitHub responses have multiple rel values, comma-separated."""
        headers = (
            "HTTP/2 200\n"
            'Link: <https://api.github.com/x?page=2>; rel="next", '
            '<https://api.github.com/x?page=5>; rel="last"\n'
        )
        assert _gh_next_link(headers) == "https://api.github.com/x?page=2"

    def test_only_last_no_next(self) -> None:
        """Last page: rel="last" present but no rel="next"."""
        headers = (
            "HTTP/2 200\n"
            'Link: <https://api.github.com/x?page=1>; rel="first", '
            '<https://api.github.com/x?page=4>; rel="prev"\n'
        )
        assert _gh_next_link(headers) is None

    def test_case_insensitive_link_header(self) -> None:
        """HTTP headers are case-insensitive; some servers use lowercase."""
        headers = (
            "HTTP/2 200\n"
            'link: <https://api.github.com/x?page=2>; rel="next"\n'
        )
        assert _gh_next_link(headers) == "https://api.github.com/x?page=2"


# ── _gh_api header/body split ────────────────────────────


def _fake_curl(stdout: str) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


class TestGhApiSplit:
    """Regression: subprocess text=True runs universal-newline
    decoding, translating \\r\\n to \\n.  The header/body separator
    in curl -D - output is therefore \\n\\n, not \\r\\n\\r\\n.  The
    original code split on \\r\\n\\r\\n, which silently failed and
    left the entire response (headers included) in `body`, producing
    a JSONDecodeError on the leading `HTTP/2 200` line.
    """

    def test_parses_body_after_single_header_block(self) -> None:
        body = json.dumps([{"tag_name": "v1"}])
        stdout = (
            "HTTP/2 200\n"
            "content-type: application/json\n"
            "\n"
            f"{body}"
        )
        with patch.object(cli.subprocess, "run", return_value=_fake_curl(stdout)):
            result = _gh_api("releases")
        assert result == [{"tag_name": "v1"}]

    def test_parses_body_after_redirect_header_blocks(self) -> None:
        """curl -L emits each response's headers before the body; we
        must split on the LAST blank line so redirected-to responses
        (e.g. the rename redirect from /repos/old-name to /repos/new-name)
        parse correctly."""
        body = json.dumps([{"tag_name": "v2"}])
        stdout = (
            "HTTP/2 301\n"
            "location: https://api.github.com/repos/new-name/releases\n"
            "\n"
            "HTTP/2 200\n"
            "content-type: application/json\n"
            "\n"
            f"{body}"
        )
        with patch.object(cli.subprocess, "run", return_value=_fake_curl(stdout)):
            result = _gh_api("releases")
        assert result == [{"tag_name": "v2"}]

    def test_dict_endpoint_returns_as_is(self) -> None:
        """Non-list responses (e.g. a single release) don't paginate."""
        body = json.dumps({"tag_name": "v1", "id": 42})
        stdout = f"HTTP/2 200\ncontent-type: application/json\n\n{body}"
        with patch.object(cli.subprocess, "run", return_value=_fake_curl(stdout)):
            result = _gh_api("releases/42")
        assert result == {"tag_name": "v1", "id": 42}

    def test_malformed_body_raises_with_snippet(self) -> None:
        """When the body genuinely isn't JSON, the error surfaces the
        first 200 chars so the caller can debug."""
        stdout = "HTTP/2 500\ncontent-type: text/html\n\n<html>boom</html>"
        with patch.object(cli.subprocess, "run", return_value=_fake_curl(stdout)):
            with pytest.raises(RuntimeError, match="non-JSON"):
                _gh_api("releases")

    def test_timeout_raises_clean_error(self) -> None:
        """TimeoutExpired should surface as a clean RuntimeError with
        the URL, not an uncaught traceback out of the CLI."""
        import subprocess as sp

        def _raise_timeout(*args: object, **kwargs: object) -> None:
            raise sp.TimeoutExpired(cmd=["curl"], timeout=35)

        with patch.object(cli.subprocess, "run", side_effect=_raise_timeout):
            with pytest.raises(RuntimeError, match="timed out after 35s"):
                _gh_api("releases")


# ── _artifact_label tristate ─────────────────────────────


class TestArtifactLabelTristate:
    """The kernel staleness flag is now a tristate to avoid both the
    round-17 always-stale and the round-18 always-not-stale bugs."""

    def test_not_built(self) -> None:
        assert _artifact_label({"built": False}) == "not built"

    def test_built_current(self) -> None:
        assert _artifact_label({"built": True, "stale": False}) == "current"

    def test_built_stale(self) -> None:
        assert _artifact_label({"built": True, "stale": True}) == "stale"

    def test_built_unknown(self) -> None:
        """stale=None means 'we couldn't honestly compute it' (no
        Lustre tree available); render as `built (?)`."""
        assert _artifact_label({"built": True, "stale": None}) == "built (?)"

    def test_missing_stale_field_treated_as_current(self) -> None:
        """Defensive: if 'stale' isn't set at all, default to current
        (matches the pre-tristate behavior for container/image which
        always set stale to a bool)."""
        assert _artifact_label({"built": True}) == "current"
