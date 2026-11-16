"""Releases keyed by their build inputs (ltvm_pkg/release_tags.py).

Publish tags a release rel-<target>-<arch>-<kver>[-<variant>]-<key>,
the key being the publishing checkout's inputs, the release format and
the build code's generation, and writes into its notes what it holds
and which ltvm published it.  A branch publishes a draft, which
`ltvm target promote` makes public after the merge; the default branch
publishes for everyone.  These tests cover the key, the tags and notes,
publish's checks, promote, pruning, and how fetch, `target fetch
--list`, `target list` and `build release` choose among published
releases of both tag schemes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import cli as cli_mod
from ltvm_pkg import release_build as rb
from ltvm_pkg import release_tags as rt
from ltvm_pkg.cli import EXIT_ERROR, EXIT_OK

# Saved before the suite's autouse fixture replaces them.
_DEFAULT_BRANCH_HEAD = rt.default_branch_head
_CHECKOUT_PROBLEM = rt.checkout_problem
_UNCOMMITTED_INPUTS = rt.uncommitted_inputs

KVER = "5.14.0-611.13.1.el9_7_lustre"
CORE = f"rocky9-x86_64-{KVER}"
OLD = CORE
MANIFEST = f"manifest-{CORE}.json"
MINE = "aaaaaaaaaa"
OTHER = "bbbbbbbbbb"
THIS_LTVM = "0.5.400+1111111"
HEAD = "1" * 40
ELSE = "2" * 40
K = "5.14-rhel9.7"
IDENT = ("rocky9", "x86_64", K, "base")
SCHEMA = rt.current_schema()


def _notes(
    *,
    version: str | None = None,
    commit: str | None = None,
    identity: tuple[str, str, str, str] | None = None,
    schema: str | None = None,
    built_at: str | None = None,
) -> str | None:
    lines = []
    if identity is not None:
        t, a, k, v = identity
        lines += [
            f"ltvm-target: {t}",
            f"ltvm-arch: {a}",
            f"ltvm-kernel: {k}",
            f"ltvm-variant: {v}",
        ]
    if schema is not None:
        lines.append(f"ltvm-schema: {schema}")
    if version is not None:
        lines.append(f"{rt.VERSION_NOTE} {version}")
    if commit is not None:
        lines.append(f"{rt.COMMIT_NOTE} {commit}")
    if built_at is not None:
        lines.append(f"{rt.BUILT_AT_NOTE} {built_at}")
    return "Pre-built artifacts\n\n" + "\n".join(lines) if lines else None


def _rel(
    tag: str,
    *names: str,
    published: str | None = None,
    draft: bool = False,
    complete: bool = True,
    **notes: Any,
) -> dict[str, Any]:
    """A release as the API lists it.  A keyed one carries the
    completion mark a finished publish writes, unless ``complete`` is
    False."""
    names = names or (MANIFEST,)
    rel: dict[str, Any] = {
        "tag_name": tag,
        "assets": [
            {
                "name": n,
                "browser_download_url": f"https://ex/releases/download/{tag}/{n}",
            }
            for n in names
        ],
    }
    body = _notes(**notes)
    if complete and rt.tag_key(tag) is not None:
        body = (body or "Pre-built artifacts\n") + "\n" + rt.COMPLETE_NOTE
    if body is not None:
        rel["body"] = body
    if published is not None:
        rel["published_at"] = published
    if draft:
        rel["draft"] = True
    return rel


def _checkout(
    key: Any = MINE,
    version: str = THIS_LTVM,
    commit: str | None = HEAD,
    on_default: Any = True,
) -> Any:
    """This checkout: its release key (a value, or a function of the
    kernel), its ltvm version and commit, and where its HEAD is against
    the release repository's default branch: True at its tip, False
    off it, None unknown, or a default_branch_position answer."""
    from contextlib import ExitStack

    stack = ExitStack()
    if callable(key):
        stack.enter_context(
            patch.object(
                rt,
                "release_key",
                side_effect=lambda t, a, k, v="base": key(k),
            )
        )
    else:
        stack.enter_context(patch.object(rt, "release_key", return_value=key))
    stack.enter_context(
        patch.object(rt, "this_ltvm_version", return_value=version)
    )
    stack.enter_context(
        patch.object(rt, "this_ltvm_commit", return_value=commit)
    )
    if isinstance(on_default, list):
        # One answer per time it is asked.
        stack.enter_context(
            patch.object(rt, "default_branch_position", side_effect=on_default)
        )
    else:
        stack.enter_context(
            patch.object(
                rt,
                "default_branch_position",
                return_value={True: "tip", False: "off", None: None}.get(
                    on_default, on_default
                ),
            )
        )
    return stack


# ------------------------------------------------------------------
# The key
# ------------------------------------------------------------------


def _no_built_kernel(tmp_path: Path) -> Any:
    from ltvm_pkg.target_config import TargetConfig

    return patch.object(
        TargetConfig, "meta_path", lambda self, *a, **k: tmp_path / "none"
    )


def test_key_digests_the_input_hashes_and_the_schema(tmp_path: Path) -> None:
    """The key is sha256 over the container, kernel and image hashes as
    the stability goldens pin them (no Lustre bytes, no built kernel),
    the release manifest's schema version, the build generation and the
    ZFS the target declares."""
    from ltvm_pkg.release_package import BUILD_GENERATION, SCHEMA_VERSION
    from ltvm_pkg.target_config import TargetConfig

    with _no_built_kernel(tmp_path):
        tc = TargetConfig("rocky9", arch="x86_64")
        lines = "".join(
            f"{a}={tc.input_hash(a, kernel=K)}\n"
            for a in ("container", "kernel", "image")
        )
    lines += f"schema={SCHEMA_VERSION}\nbuild={BUILD_GENERATION}\n"
    lines += f"zfs={rt.zfs_config(TargetConfig('rocky9', arch='x86_64'))}\n"
    want = hashlib.sha256(lines.encode()).hexdigest()[:10]
    assert rt.release_key("rocky9", "x86_64", K) == want
    assert len(want) == rt.KEY_LENGTH


@pytest.mark.parametrize("name", ["SCHEMA_VERSION", "BUILD_GENERATION"])
def test_a_schema_or_generation_bump_moves_the_key_not_input_hashes(
    name: str,
) -> None:
    from ltvm_pkg import release_package as rp
    from ltvm_pkg.target_config import TargetConfig

    tc = TargetConfig("rocky9", arch="x86_64")
    hashes = [tc.input_hash(a) for a in ("container", "kernel", "image")]
    before = rt.release_key("rocky9", "x86_64", K)
    bumped = (
        "2026-12-01-another-change"
        if name == "BUILD_GENERATION"
        else getattr(rp, name) + 1
    )
    with patch.object(rp, name, bumped):
        assert rt.release_key("rocky9", "x86_64", K) != before
        assert [
            tc.input_hash(a) for a in ("container", "kernel", "image")
        ] == hashes


def test_key_ignores_what_is_built_on_this_host(tmp_path: Path) -> None:
    """An image's input_hash folds in the built kernel's meta.json; the
    key must not, or a host's fetched kernel would change its key."""
    from ltvm_pkg.target_config import TargetConfig

    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"input_hash": "0123456789abcdef"}))
    clean = rt.release_key("rocky9", "x86_64", K)
    with _no_built_kernel(tmp_path):
        bare = TargetConfig("rocky9", arch="x86_64").input_hash("image")
    with patch.object(TargetConfig, "meta_path", lambda self, *a, **k: meta):
        tc = TargetConfig("rocky9", arch="x86_64")
        assert tc.input_hash("image") != bare
        assert tc.checkout_input_hash("image") == bare
        assert rt.release_key("rocky9", "x86_64", K) == clean


def test_key_differs_by_kernel_arch_and_variant() -> None:
    base = rt.release_key("rocky9", "x86_64", K)
    assert rt.release_key("rocky9", "x86_64", "5.14-rhel9.5") != base
    assert rt.release_key("rocky9", "aarch64", K) != base
    assert rt.release_key("rocky9", "x86_64", "5.14-rhel9.5", "mofed-24") != (
        rt.release_key("rocky9", "x86_64", "5.14-rhel9.5")
    )
    # Short and full kernel names are one kernel.
    assert (
        rt.release_key("rocky9", "x86_64", "5.14-rhel9.7-5.14.0-611.13.1.el9_7")
        == base
    )


# ------------------------------------------------------------------
# The build generation on built artifacts
# ------------------------------------------------------------------


def test_kernels_and_images_record_the_build_generation(
    tmp_path: Path,
) -> None:
    import ltvm_pkg.target_config as cfg
    from ltvm_pkg.release_package import BUILD_GENERATION

    with patch.object(cfg, "ARTIFACTS_DIR", tmp_path):
        tc = cfg.TargetConfig("rocky9", arch="x86_64")
        for artifact in ("container", "kernel", "image"):
            tc.write_meta(artifact, kernel=K)
            meta = json.loads(tc.meta_path(artifact, K).read_text())
            want = None if artifact == "container" else BUILD_GENERATION
            assert meta.get("build_generation") == want, artifact
            # It moves no input hash.
            assert meta["input_hash"] == tc.input_hash(artifact, K)


def test_another_generation_is_stale_an_unrecorded_one_is_not(
    tmp_path: Path,
) -> None:
    """A bump rebuilds kernels and images made by the previous build
    code; artifacts from before generations were recorded are kept (a
    rebuild of every artifact on every host is the thing the goldens
    guard against), though publish refuses them."""
    import ltvm_pkg.target_config as cfg
    from ltvm_pkg.cli.util import staleness_reasons
    from ltvm_pkg.kernel_build import kernel_status
    from ltvm_pkg.release_package import BUILD_GENERATION
    from tests.conftest import _make_kernel_outputs

    with patch.object(cfg, "ARTIFACTS_DIR", tmp_path):
        tc = cfg.TargetConfig("rocky9", arch="x86_64")
        _make_kernel_outputs(tc, K)
        tc.write_meta("kernel", kernel=K)
        path = tc.meta_path("kernel", K)
        meta = json.loads(path.read_text())
        assert not tc.is_stale("kernel", kernel=K)

        meta["build_generation"] = "2026-01-01-older"
        path.write_text(json.dumps(meta))
        assert tc.is_stale("kernel", kernel=K)
        assert kernel_status(tc, K)["stale"] is True
        assert staleness_reasons(tc, "kernel", meta, kernel=K) == [
            f"build generation 2026-01-01-older (now {BUILD_GENERATION})"
        ]

        del meta["build_generation"]
        path.write_text(json.dumps(meta))
        assert not tc.is_stale("kernel", kernel=K)
        assert kernel_status(tc, K)["stale"] is None


# ------------------------------------------------------------------
# Tags
# ------------------------------------------------------------------


def test_keyed_tag_round_trip() -> None:
    tag = rt.keyed_tag(CORE, MINE)
    assert tag == f"rel-{CORE}-{MINE}"
    assert rt.split_tag(tag) == (CORE, MINE)
    assert rt.split_tag(OLD) == (OLD, None)
    assert rt.split_tag("rel-foo-xyz") == ("rel-foo-xyz", None)


def test_tag_is_for_reads_both_schemes() -> None:
    assert rt.tag_is_for(OLD, "rocky9")
    assert rt.tag_is_for(rt.keyed_tag(CORE, MINE), "rocky9")
    assert not rt.tag_is_for(rt.keyed_tag(CORE, MINE), "rocky8")
    assert not rt.tag_is_for(f"bootable-{CORE}", "rocky9")
    assert not rt.tag_is_for("qemu-9.2.2", "rocky9")


def test_old_ltvm_filter_never_sees_keyed_tags() -> None:
    """Older ltvm keeps tags equal to the target or starting with
    "<target>-": no target may make a rel- tag look like its own."""
    from ltvm_pkg.target_config import list_targets

    names = list_targets()
    assert names
    for t in names:
        tag = rt.keyed_tag(f"{t}-x86_64-1.0", MINE)
        assert not any(tag == n or tag.startswith(n + "-") for n in names), t


def test_release_tag_files_name_one_kernel_in_both_schemes(
    tmp_path: Path,
) -> None:
    from ltvm_pkg.cli.util import (
        kver_from_release_tag,
        read_release_tag,
        write_release_tag,
    )

    keyed = rt.keyed_tag(CORE, MINE)
    assert kver_from_release_tag(keyed, "rocky9", "x86_64") == KVER
    assert kver_from_release_tag(OLD, "rocky9", "x86_64") == KVER
    mofed = rt.keyed_tag(f"{CORE}-mofed-24", MINE)
    assert kver_from_release_tag(mofed, "rocky9", "x86_64", "mofed-24") == KVER

    write_release_tag(tmp_path, "rocky9", "x86_64", OLD)
    write_release_tag(tmp_path, "rocky9", "x86_64", keyed)
    assert read_release_tag(tmp_path, "rocky9", "x86_64", KVER) == keyed


def test_kernel_name_maps_back_from_a_keyed_tag() -> None:
    tag = rt.keyed_tag(CORE, "9e1d5b5b57")
    assert cli_mod._fetched_kernel_name("rocky9", tag, "x86_64") == K


# ------------------------------------------------------------------
# Notes
# ------------------------------------------------------------------


def test_notes_name_the_release_and_its_publisher() -> None:
    rel = _rel(
        rt.keyed_tag(CORE, MINE),
        identity=IDENT,
        version="0.5.1+ab",
        commit=ELSE,
        schema=SCHEMA,
    )
    p = rt.Publisher.of(rel)
    assert p.identity() == IDENT
    assert (p.version, p.commit, p.schema) == ("0.5.1+ab", ELSE, SCHEMA)
    assert rt.notes_version(rel["body"]) == "0.5.1+ab"
    assert rt.notes_commit(rel["body"]) == ELSE
    assert rt.notes_commit(f"{rt.COMMIT_NOTE} not-hex") is None
    assert rt.Publisher.of({"tag_name": OLD}) == rt.Publisher()
    assert rt.Publisher.of({"body": "ltvm-kernel: x"}).identity() is None


def test_release_notes_carry_every_line() -> None:
    with _checkout(commit=HEAD):
        notes = rt.release_notes(
            "summary",
            target="mainline",
            arch="x86_64",
            kernel="stable",
            variant="base",
        )
    assert notes.startswith("summary\n\n")
    p = rt.Publisher.of({"body": notes})
    assert p.identity() == ("mainline", "x86_64", "stable", "base")
    assert (p.version, p.commit, p.schema) == (THIS_LTVM, HEAD, SCHEMA)
    assert "default-branch" not in notes
    # Complete, and the mark is last: what a publish writes when done.
    assert p.complete
    assert notes.splitlines()[-1] == rt.COMPLETE_NOTE
    unfinished = rt.incomplete_notes(notes)
    assert not rt.Publisher.of({"body": unfinished}).complete
    assert rt.Publisher.of({"body": unfinished}).identity() == p.identity()
    promoted = rt.promoted_notes(notes, "0.5.9+x")
    assert rt.promoted_notes(promoted, "0.5.10+y").count("promoted") == 1
    q = rt.Publisher.of({"body": promoted})
    assert (q.version, q.promoted_version, q.complete) == (
        THIS_LTVM,
        "0.5.9+x",
        True,
    )
    with _checkout(commit=None):
        notes = rt.release_notes(
            "s", target="t", arch="a", kernel="k", variant="v"
        )
    assert rt.notes_commit(notes) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("kernel", "x/../../escaped"),
        ("kernel", "../escaped"),
        ("kernel", "a..b"),
        ("target", "rocky9/../x"),
        ("variant", "-rf"),
        ("arch", "x86_64 y"),
        ("version", "$(id)"),
    ],
)
def test_notes_fields_that_are_not_plain_names_are_dropped(
    field: str, value: str
) -> None:
    good = {
        "target": "rocky9",
        "arch": "x86_64",
        "kernel": K,
        "variant": "base",
        "version": "0.5.1+a",
    }
    body = "\n".join(
        f"ltvm-{k}: {value if k == field else v}" for k, v in good.items()
    )
    p = rt.Publisher.of({"body": body})
    assert getattr(p, field) is None
    if field != "version":
        assert p.identity() is None


def test_version_order() -> None:
    assert rt.version_order("0.5.391+d8b1d5b") == (0, 5, 391)
    assert rt.version_order("0.6.2") == (0, 6, 2)
    assert rt.version_order("0.5+d8b1d5b") is None
    assert rt.version_order(None) is None
    assert rt.is_newer("0.5.401+a", "0.5.400+b")
    assert rt.is_newer("0.6.1", "0.5.900")
    assert not rt.is_newer("0.5.400+a", "0.5.400+b")
    assert not rt.is_newer(None, "0.5.1")
    assert not rt.is_newer("0.9.1", "0.5+abc")


# ------------------------------------------------------------------
# This checkout and the default branch
# ------------------------------------------------------------------


def _git_answers(*results: tuple[int, str]) -> Any:
    calls: list[list[str]] = []
    outs = iter(results)

    def run(args: list[str], timeout: int) -> Any:
        calls.append(args)
        rc, out = next(outs, (0, ""))
        return MagicMock(returncode=rc, stdout=out, stderr="")

    return calls, patch.object(rt, "_git_run", side_effect=run)


def test_default_branch_head_fetches_the_release_repository() -> None:
    """The repository releases go to, from its own URL: origin may be a
    fork, whose default branch holds work the release repository has
    not merged."""
    url = f"https://github.com/{cli_mod.GITHUB_REPO}.git"
    assert rt.release_repo_url() == url
    ls = "ref: refs/heads/master\tHEAD\nabc\tHEAD\n"
    calls, p = _git_answers((0, ls), (0, ""), (0, ELSE + "\n"))
    with p:
        assert _DEFAULT_BRANCH_HEAD() == ELSE
    assert calls[0] == ["ls-remote", "--symref", url, "HEAD"]
    # Into a ref of its own, read and deleted: never FETCH_HEAD.
    ref = calls[1][-1].split(":", 1)[1]
    assert ref.startswith("refs/ltvm-release-check/")
    assert calls[1] == ["fetch", "--quiet", url, f"+refs/heads/master:{ref}"]
    assert calls[2] == ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"]
    assert calls[3] == ["update-ref", "-d", ref]
    assert not any("origin" in c or "FETCH_HEAD" in c for c in calls)
    with patch.object(cli_mod, "GITHUB_REPO", "someone/fork"):
        assert rt.release_repo_url() == "https://github.com/someone/fork.git"
    # Anything that cannot be told is unknown.
    for answers in (
        [(128, "")],
        [(0, "no symref\n")],
        [(0, ls), (1, "")],
        [(0, ls), (0, ""), (1, "")],
        [(0, ls), (0, ""), (0, "not-a-commit\n")],
    ):
        calls, p = _git_answers(*answers)
        with p:
            assert _DEFAULT_BRANCH_HEAD() is None
        # The ref is deleted on failure too, once one was named.
        if len(answers) > 2:
            assert calls[-1][:2] == ["update-ref", "-d"]


def test_a_fetch_in_between_cannot_pass_for_the_default_branch(
    tmp_path: Path,
) -> None:
    """Against real git: an unrelated fetch right after the checked one
    rewrites FETCH_HEAD with a fork's unmerged branch.  The default
    branch's commit is still the one returned, and the private ref is
    gone after."""
    import subprocess

    import ltvm_pkg.target_config as cfg

    def git(where: Path, *args: str) -> str:
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.invalid",
                "-c",
                "init.defaultBranch=master",
                "-C",
                str(where),
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q")
    (work / "f").write_text("1")
    git(work, "add", "f")
    git(work, "commit", "-q", "-m", "on master")
    upstream_tip = git(work, "rev-parse", "HEAD")
    upstream = tmp_path / "upstream.git"
    git(tmp_path, "clone", "-q", "--bare", str(work), str(upstream))
    git(work, "checkout", "-q", "-b", "pr")
    (work / "f").write_text("2")
    git(work, "commit", "-q", "-am", "unmerged")
    fork = tmp_path / "fork.git"
    git(tmp_path, "clone", "-q", "--bare", str(work), str(fork))
    pr_tip = git(work, "rev-parse", "HEAD")
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "-q", str(fork), str(checkout))

    real = rt._git_run

    def interleaved(args: list[str], timeout: int) -> Any:
        r = real(args, timeout)
        if args[0] == "fetch":
            git(checkout, "fetch", "-q", str(fork), "pr")
            assert git(checkout, "rev-parse", "FETCH_HEAD") == pr_tip
        return r

    with (
        patch.object(cfg, "REPO_ROOT", checkout),
        patch.object(rt, "release_repo_url", return_value=str(upstream)),
        patch.object(rt, "_git_run", side_effect=interleaved),
    ):
        assert _DEFAULT_BRANCH_HEAD() == upstream_tip
    assert git(checkout, "for-each-ref", "refs/ltvm-release-check") == ""


@pytest.mark.parametrize(
    "head,contains,want",
    [
        (HEAD, None, "tip"),
        (ELSE, True, "ancestor"),
        (ELSE, False, "off"),
        (ELSE, None, None),
        (None, None, None),
    ],
)
def test_default_branch_position(
    head: str | None, contains: bool | None, want: str | None
) -> None:
    with (
        patch.object(rt, "this_ltvm_commit", return_value=HEAD),
        patch.object(rt, "default_branch_head", return_value=head),
        patch.object(rt, "default_branch_contains", return_value=contains),
    ):
        assert rt.default_branch_position() == want


def test_default_branch_contains_asks_history() -> None:
    calls, p = _git_answers((0, ""))
    with p:
        assert rt.default_branch_contains(HEAD, ELSE) is True
    assert calls == [["merge-base", "--is-ancestor", HEAD, ELSE]]
    _, p = _git_answers((1, ""))
    with p:
        assert rt.default_branch_contains(HEAD, ELSE) is False
    _, p = _git_answers((128, ""))
    with p:
        assert rt.default_branch_contains(HEAD, ELSE) is None
    assert rt.default_branch_contains(HEAD, None) is None
    assert rt.default_branch_contains(None, ELSE) is None
    assert rt.default_branch_contains("--all", ELSE) is None


def test_history_against_this_repository() -> None:
    import subprocess

    from ltvm_pkg.target_config import REPO_ROOT

    head = rt.this_ltvm_commit()
    if head is None:
        pytest.skip("ltvm is not running from a git checkout")
    parent = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD~1"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert rt.default_branch_contains(parent, head) is True
    assert rt.default_branch_contains(head, parent) is False


def test_a_dirty_or_unversioned_checkout_cannot_publish() -> None:
    with patch.object(rt, "this_ltvm_commit", return_value=HEAD):
        _, p = _git_answers((0, ""))
        with p:
            assert _CHECKOUT_PROBLEM() is None
        _, p = _git_answers((0, " M ltvm_pkg/kernel_build.py\n"))
        with p:
            assert "uncommitted changes" in (_CHECKOUT_PROBLEM() or "")
        _, p = _git_answers((128, ""))
        with p:
            assert _CHECKOUT_PROBLEM() is not None
    with patch.object(rt, "this_ltvm_commit", return_value=None):
        assert "not running from a git checkout" in (_CHECKOUT_PROBLEM() or "")


# ------------------------------------------------------------------
# Choosing a release
# ------------------------------------------------------------------


def _pick(rels: list[dict[str, Any]], key: str = MINE) -> rt.Pick:
    return rt.pick_release(
        [r["tag_name"] for r in rels],
        [rt.Publisher.of(r) for r in rels],
        lambda i: key,
        THIS_LTVM,
    )


def test_pick_prefers_the_matching_key_over_newer_releases() -> None:
    rels = [
        _rel(OLD),
        _rel(rt.keyed_tag(CORE, OTHER), version="0.5.390+2"),
        _rel(rt.keyed_tag(CORE, MINE), version="0.5.380+3"),
    ]
    assert _pick(rels) == rt.Pick(2, True)


def test_pick_otherwise_takes_the_newest_of_either_scheme() -> None:
    rels = [_rel(OLD), _rel(rt.keyed_tag(CORE, OTHER), version="0.5.1+a")]
    assert _pick(rels) == rt.Pick(0, False)
    assert _pick(rels[1:]) == rt.Pick(0, False)


def test_pick_asks_for_an_update_when_the_newest_is_newer() -> None:
    rels = [_rel(rt.keyed_tag(CORE, OTHER), version="0.5.410+pr")]
    assert _pick(rels) == rt.Pick(0, False, newer="0.5.410+pr")
    # Not for a release this checkout's key matches.
    assert _pick(rels, key=OTHER) == rt.Pick(0, True)


def test_pick_hashes_nothing_for_unkeyed_releases() -> None:
    def boom(i: int) -> str:
        raise AssertionError("key computed for an unkeyed release")

    none = rt.Publisher()
    assert rt.pick_release([OLD], [none], boom, THIS_LTVM) == rt.Pick(0)
    assert rt.pick_release([], [], boom, THIS_LTVM) == rt.Pick(None)


# ------------------------------------------------------------------
# fetch
# ------------------------------------------------------------------


def _find(releases: list[dict[str, Any]], **kw: Any) -> str:
    kw.setdefault("kernel", K)
    kw.setdefault("arch", "x86_64")
    target = kw.pop("target", "rocky9")
    with patch.object(cli_mod, "_gh_api", return_value=releases):
        return str(cli_mod._find_release_url(target, **kw))


def test_fetch_takes_the_release_keyed_with_this_checkout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mine = rt.keyed_tag(CORE, MINE)
    releases = [
        _rel(rt.keyed_tag(CORE, OTHER), version="0.5.1+b"),
        _rel(OLD),
        _rel(mine, version="0.5.380+a"),
    ]
    with _checkout():
        url = _find(releases, kernel_signature="el9_7")
    assert f"/{mine}/" in url
    assert capsys.readouterr().err == ""


def test_fetch_otherwise_takes_the_newest_and_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    releases = [_rel(OLD), _rel(rt.keyed_tag(CORE, OTHER), version="0.5.1+b")]
    with _checkout():
        assert f"/{OLD}/" in _find(releases)
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "built from other inputs" in err
    assert "sudo ltvm update" not in err


def test_fetch_asks_for_an_update_when_the_release_is_newer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    newest = rt.keyed_tag(CORE, OTHER)
    releases = [_rel(newest, version="0.5.410+pr"), _rel(OLD)]
    with _checkout():
        assert f"/{newest}/" in _find(releases)
    err = capsys.readouterr().err
    assert "published by ltvm 0.5.410+pr, newer than this one" in err
    assert THIS_LTVM in err
    assert "sudo ltvm update" in err


def test_fetch_never_takes_a_draft(capsys: pytest.CaptureFixture[str]) -> None:
    """A branch's publish is a draft until promoted: master keeps its
    own release however new the branch's is, and whatever its key."""
    draft = _rel(rt.keyed_tag(CORE, MINE), identity=IDENT, draft=True)
    with _checkout():
        assert f"/{OLD}/" in _find([draft, _rel(OLD)])
        with pytest.raises(RuntimeError, match="No published artifacts"):
            _find([draft])


def test_fetch_skips_a_named_release_holding_two_manifests() -> None:
    """Its notes name one kernel; which manifest is it cannot say."""
    two = _rel(
        rt.keyed_tag(CORE, MINE),
        "manifest-rocky9-x86_64-5.14.0-503.40.1.el9_7_lustre.json",
        MANIFEST,
        identity=IDENT,
    )
    with _checkout():
        assert f"/{OLD}/" in _find([two, _rel(OLD)])


def _mainline(kernel: str, kver: str, key: str = MINE) -> dict[str, Any]:
    core = f"mainline-x86_64-{kver}"
    return _rel(
        rt.keyed_tag(core, key),
        f"manifest-{core}.json",
        identity=("mainline", "x86_64", kernel, "base"),
        version="0.5.1+a",
    )


def test_fetch_takes_the_declared_kernel_asked_for() -> None:
    """Moving kernels have no signature: the notes say which declared
    kernel a release is, and fetch keeps to the one asked for even when
    another is newer and keyed with this checkout too."""
    releases = [
        _mainline("latest", "7.1.0-rc3-lustre"),
        _mainline("stable", "7.0.10-lustre"),
        _mainline("longterm", "6.18.4-lustre"),
    ]
    with _checkout():
        for kernel, kver in (
            ("stable", "7.0.10"),
            ("longterm", "6.18.4"),
            ("latest", "7.1.0-rc3"),
        ):
            url = _find(releases, target="mainline", kernel=kernel)
            assert f"mainline-x86_64-{kver}-lustre-{MINE}" in url, kernel


def test_fetch_keys_a_moving_kernel_by_the_name_in_its_notes() -> None:
    from ltvm_pkg.cli.fetch import _release_checkout_match

    rel = _mainline("stable", "7.0.10-lustre")
    by_kernel = {"stable": MINE}
    with _checkout(key=lambda k: by_kernel.get(k, OTHER)):
        assert _release_checkout_match(rel, "mainline", "x86_64") is True
        bare = dict(rel)
        del bare["body"]
        assert _release_checkout_match(bare, "mainline", "x86_64") is False


def test_a_full_kernel_dir_name_is_its_declared_kernel() -> None:
    from ltvm_pkg.cli.fetch import _named_fits

    full = "5.14-rhel9.7-5.14.0-611.13.1.el9_7"
    notes = rt.Publisher(
        target="rocky9", arch="x86_64", kernel=full, variant="base"
    )
    assert _named_fits(notes, "rocky9", "x86_64", "base", K) is True
    assert _named_fits(notes, "rocky9", "x86_64", "base", "5.14-rhel9.5") is (
        False
    )
    assert _named_fits(notes, "rocky9", "x86_64", "mofed-24", K) is False
    assert _named_fits(rt.Publisher(), "rocky9", "x86_64", "base", K) is None


def test_fetch_still_reads_variants_and_bootable_as_before() -> None:
    mofed = f"manifest-{CORE}-mofed-24.json"
    releases = [
        _rel(rt.keyed_tag(f"{CORE}-mofed-24", MINE), mofed, version="0.5.1+a"),
        _rel(f"bootable-{CORE}", f"bootable-{CORE}.qcow2.zst"),
        _rel(rt.keyed_tag(CORE, MINE), version="0.5.1+a"),
    ]
    with _checkout():
        assert _find(releases).endswith(MANIFEST)
        assert _find(releases, variant="mofed-24").endswith(mofed)
        boot = _find(releases, mode="bootable", kernel=None)
    assert boot.endswith(".qcow2.zst")


def test_schema_mismatch_asks_for_a_newer_ltvm(tmp_path: Path) -> None:
    from ltvm_pkg import release_package as rp

    def fake_download(url: str, dest: Path, **kw: Any) -> None:
        dest.write_text(
            json.dumps({"schema": "ltvm-release/99", "ltvm_version": "0.9.1+x"})
        )

    with patch.object(rp, "_download", side_effect=fake_download):
        with pytest.raises(RuntimeError) as exc:
            rp.fetch_target("rocky9", "https://ex/m.json", tmp_path)
    msg = str(exc.value)
    assert "needs a newer ltvm" in msg
    assert "0.9.1+x" in msg
    assert "sudo ltvm update" in msg


# ------------------------------------------------------------------
# fetch --list and target list
# ------------------------------------------------------------------


def _list_ns(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "json": False,
        "url": None,
        "target": "rocky9",
        "filter": None,
        "arch": "x86_64",
        "kernel": None,
        "variant": "base",
        "image": False,
        "dry_run": False,
        "list": True,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _three() -> list[dict[str, Any]]:
    return [
        _rel(rt.keyed_tag(CORE, OTHER), identity=IDENT, version="0.5.410+pr"),
        _rel(
            rt.keyed_tag(CORE, MINE),
            identity=IDENT,
            version="0.5.400+a",
            commit=ELSE,
        ),
        _rel(OLD),
        _rel(rt.keyed_tag(CORE, "c" * 10), identity=IDENT, draft=True),
    ]


def test_fetch_list_marks_the_release_of_this_checkout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _checkout(), patch.object(cli_mod, "_gh_api", return_value=_three()):
        assert cli_mod.cmd_fetch(_list_ns(json=True)) == EXIT_OK
        rows = json.loads(capsys.readouterr().out)
        # The draft is not listed.
        assert [r["matches_checkout"] for r in rows] == [False, True, None]
        assert rows[0]["ltvm_version"] == "0.5.410+pr"
        assert rows[1]["ltvm_commit"] == ELSE
        assert rows[2]["ltvm_version"] is None

        assert cli_mod.cmd_fetch(_list_ns()) == EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    assert lines[-3].endswith("built from other inputs")
    assert lines[-2].endswith("built from this checkout's inputs")
    assert "inputs" not in lines[-1]


def test_target_list_status_names_the_matching_release(
    tmp_path: Path,
) -> None:
    import ltvm_pkg.target_config as cfg

    with _checkout(), patch.object(cfg, "ARTIFACTS_DIR", tmp_path):
        local, remote, match = cli_mod._release_status_full(
            "rocky9",
            "x86_64",
            _three(),
            kernel_signature="el9_7",
            kernel=K,
        )
        assert (local, remote, match) == ("-", f"{KVER}-{MINE}", True)

        other = [_rel(rt.keyed_tag(CORE, OTHER), version="0.5.399+o")]
        _, remote, match = cli_mod._release_status_full(
            "rocky9", "x86_64", other, kernel=K
        )
        assert (remote, match) == (f"{KVER}-{OTHER}", False)
        _, remote, match = cli_mod._release_status_full(
            "rocky9", "x86_64", [_rel(OLD)], kernel=K
        )
        assert (remote, match) == (KVER, None)
        # Drafts are not there at all.
        draft = [_rel(rt.keyed_tag(CORE, MINE), identity=IDENT, draft=True)]
        _, remote, _ = cli_mod._release_status_full(
            "rocky9", "x86_64", draft, kernel=K
        )
        assert remote == "-"

        # The two-value form older callers use is unchanged.
        assert cli_mod._release_status("rocky9", "x86_64", [_rel(OLD)]) == (
            "-",
            KVER,
        )


def test_target_list_rows_keep_to_their_declared_kernel(
    tmp_path: Path,
) -> None:
    import ltvm_pkg.target_config as cfg

    releases = [
        _mainline("latest", "7.1.0-rc3-lustre"),
        _mainline("stable", "7.0.10-lustre"),
    ]
    with _checkout(), patch.object(cfg, "ARTIFACTS_DIR", tmp_path):
        _, remote, match = cli_mod._release_status_full(
            "mainline", "x86_64", releases, kernel="stable"
        )
        assert (remote, match) == (f"7.0.10-lustre-{MINE}", True)
        _, remote, _ = cli_mod._release_status_full(
            "mainline", "x86_64", releases, kernel="longterm"
        )
        assert remote == "-"


def test_target_list_marks_the_remote_cell(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import ltvm_pkg.target_config as cfg

    ns = argparse.Namespace(
        json=False,
        list_filter=None,
        arch="x86_64",
        all_arches=False,
        all_kernels=False,
    )
    with (
        _checkout(),
        patch.object(cfg, "ARTIFACTS_DIR", tmp_path),
        patch.object(cli_mod, "list_targets", return_value=["rocky9"]),
        patch.object(cli_mod, "_gh_api", return_value=_three()),
    ):
        assert cli_mod.cmd_targets(ns) == EXIT_OK
        out = capsys.readouterr().out
        assert "✓=" in out
        assert "release built from this checkout's inputs" in out

        ns.json = True
        assert cli_mod.cmd_targets(ns) == EXIT_OK
    rows = json.loads(capsys.readouterr().out)
    base = [
        r for r in rows if r.get("kernel") == K and r.get("variant") == "base"
    ]
    assert base[0]["remote_matches_checkout"] is True


# ------------------------------------------------------------------
# build release
# ------------------------------------------------------------------

ZFS = f"zfs-{CORE}-2.2.7.tar.zst"
DECLARED = {"rocky9": [K]}


def _plan(
    releases: list[dict[str, Any]], key: str | None
) -> tuple[list[rb.Build], list[str], list[str]]:
    return rb.published_builds(
        releases,
        "x86_64",
        DECLARED,
        lambda t, stem, a: K if "el9_7" in stem else None,
        lambda t, a, stem: not stem.endswith("-mofed-24"),
        lambda t, k, a: key,
    )


def test_build_release_plans_a_kernel_once_across_schemes() -> None:
    releases = [_rel(rt.keyed_tag(CORE, MINE), version="0.5.1+a"), _rel(OLD)]
    builds, unmatched, with_zfs = _plan(releases, MINE)
    assert builds == [rb.Build("rocky9", K)]
    assert unmatched == []
    assert with_zfs == []


def test_build_release_reads_the_release_fetch_would_take() -> None:
    """The old release carries ZFS, the keyed one does not: which one
    counts decides whether the kernel is built."""
    releases = [
        _rel(OLD, MANIFEST, ZFS),
        _rel(rt.keyed_tag(CORE, MINE), version="0.5.1+a"),
    ]
    builds, _, with_zfs = _plan(releases, MINE)
    assert builds == [rb.Build("rocky9", K)]
    assert with_zfs == []
    # No key match: the newest, which carries ZFS, counts.
    builds, _, with_zfs = _plan(releases, OTHER)
    assert builds == []
    assert with_zfs == [OLD]


def test_build_release_groups_a_kernel_across_its_versions() -> None:
    moved = "rocky9-x86_64-5.14.0-611.55.1.el9_7_lustre"
    releases = [
        _rel(
            rt.keyed_tag(moved, MINE),
            f"manifest-{moved}.json",
            identity=IDENT,
            version="0.5.1+a",
        ),
        _rel(OLD, MANIFEST, ZFS),
    ]
    builds, unmatched, with_zfs = _plan(releases, MINE)
    assert builds == [rb.Build("rocky9", K)]
    assert (unmatched, with_zfs) == ([], [])


def test_build_release_takes_the_kernel_from_the_notes() -> None:
    rel = _rel(
        rt.keyed_tag(CORE, MINE),
        identity=("rocky9", "x86_64", K, "mofed-24"),
        version="0.5.1+a",
    )
    assert _plan([rel], MINE) == ([], [], [])
    rel = _rel(
        rt.keyed_tag("rocky9-x86_64-9.9.9-odd", MINE),
        "manifest-rocky9-x86_64-9.9.9-odd.json",
        identity=IDENT,
        version="0.5.1+a",
    )
    assert _plan([rel], MINE)[0] == [rb.Build("rocky9", K)]


def test_build_release_takes_only_declared_kernels_from_notes() -> None:
    """A kernel the notes name goes on to name build logs: one that is
    not declared, or not a plain name, is unmatched, never built."""
    escaped = _rel(
        rt.keyed_tag(CORE, MINE),
        identity=("rocky9", "x86_64", "x/../../escaped", "base"),
        version="0.5.1+a",
    )
    # The bad field drops the identity; the name maps to the kernel.
    assert _plan([escaped], MINE)[0] == [rb.Build("rocky9", K)]
    removed = _rel(
        rt.keyed_tag(CORE, MINE),
        identity=("rocky9", "x86_64", "5.14-rhel9.0", "base"),
        version="0.5.1+a",
    )
    builds, unmatched, _ = _plan([removed], MINE)
    assert builds == []
    assert unmatched == [removed["tag_name"]]


def test_build_release_ignores_drafts() -> None:
    draft = _rel(rt.keyed_tag(CORE, MINE), identity=IDENT, draft=True)
    assert _plan([draft], MINE) == ([], [], [])


# ------------------------------------------------------------------
# publish
# ------------------------------------------------------------------


@pytest.fixture
def artifacts(tmp_path: Path) -> Any:
    """rocky9's real targets with artifacts in a tmp dir, written as the
    builds write them: a container, and a complete kernel and image
    (without Lustre) current against this checkout."""
    import ltvm_pkg.target_config as cfg
    from tests.conftest import _make_image_outputs

    with patch.object(cfg, "ARTIFACTS_DIR", tmp_path):
        tc = cfg.TargetConfig("rocky9", arch="x86_64")
        tc.write_meta(
            "container", image_tag="ltvm-build-rocky9", image_id="sha1"
        )
        _kernel_files(tc, K)
        tc.write_meta(
            "kernel",
            kernel=K,
            extra_hash=b"lustre patches",
            kernel_version=KVER,
        )
        _make_image_outputs(tc, K)
        tc.write_meta("image", kernel=K)
        yield tc


def _kernel_files(tc: Any, kernel: str) -> None:
    """A complete built kernel's files, with the build tree modules are
    built against."""
    from tests.conftest import _make_kernel_outputs

    out = _make_kernel_outputs(tc, kernel)
    tree = out / "build-tree"
    (tree / "Module.symvers").write_text("symvers\n")
    (tree / "include" / "config").mkdir(parents=True, exist_ok=True)
    (tree / "include" / "config" / "kernel.release").write_text(KVER + "\n")


def _staged(
    where: Path, tc: Any, kernel: str, zfs: str | None, lustre: str = "l1"
) -> Path:
    """A Lustre staging (or snapshot) built against ``kernel``."""
    import hashlib as _h

    symvers = (
        tc.kernel_output_dir(kernel) / "build-tree" / "Module.symvers"
    ).read_bytes()
    where.mkdir(parents=True, exist_ok=True)
    (where / "lustre.ko").write_bytes(b"ko")
    meta: dict[str, Any] = {
        "module_symvers_sha256": _h.sha256(symvers).hexdigest(),
        "lustre_modules_sha256": lustre,
    }
    if zfs:
        meta["zfs_version"] = zfs
    (where / ".ltvm-staging-meta.json").write_text(json.dumps(meta))
    return where


def _bake(
    tc: Any,
    zfs: str | None = None,
    lustre: str = "l1",
    mofed: Path | None = None,
) -> Path:
    """Snapshot a Lustre beside the kernel and build the image with it
    (and with MOFED kmods from ``mofed``), as `build all` leaves them."""
    from ltvm_pkg.image_build import (
        _lustre_staging_hash_input,
        mofed_kmods_hash,
    )

    snap = _staged(
        tc.kernel_output_dir(K) / "lustre-artifacts", tc, K, zfs, lustre
    )
    (snap / ".ltvm-snapshot.json").write_text(
        json.dumps({"zfs_version": zfs} if zfs else {})
    )
    extra = _lustre_staging_hash_input(snap) + (
        mofed_kmods_hash(mofed) if mofed else b""
    )
    fields: dict[str, Any] = {
        "with_lustre": "/lustre",
        "lustre_version": "2.17.0",
    }
    if mofed:
        fields["mofed_kmods"] = [p.name for p in mofed.glob("*.rpm")]
    tc.write_meta("image", kernel=K, extra_hash=extra, **fields)
    return snap


def _edit_meta(path: Path, **fields: Any) -> None:
    meta = json.loads(path.read_text())
    for k, v in fields.items():
        if v is None:
            meta.pop(k, None)
        elif k in (
            "build_generation",
            "container_input_hash",
            "container_image_id",
        ):
            meta[k] = v
        else:
            # An artifact built from other inputs: its digest moves, and
            # its whole hash with it.
            meta["input_components"][k] = v
            meta["input_hash"] = "0" * 16
    path.write_text(json.dumps(meta))


def test_publish_check_passes_matching_artifacts(artifacts: Any) -> None:
    assert cli_mod._publish_input_mismatches(artifacts, K, "base") == []


def test_publish_check_names_what_differs(artifacts: Any) -> None:
    tc = artifacts
    _edit_meta(tc.meta_path("kernel", K), **{"kernel-name": "0" * 12})
    _edit_meta(tc.meta_path("image", K), **{"image.Dockerfile": "0" * 12})
    tc.meta_path("container").unlink()
    problems = cli_mod._publish_input_mismatches(tc, K, "base")
    assert problems[0].startswith("container: no meta.json")
    assert problems[1] == "kernel: kernel-name (changed)"
    assert problems[2] == "image: image.Dockerfile (changed)"
    # A stale image whose digests cannot say why: still refused.
    meta = json.loads(tc.meta_path("image", K).read_text())
    del meta["input_components"]
    tc.meta_path("image", K).write_text(json.dumps(meta))
    problems = cli_mod._publish_input_mismatches(tc, K, "base")
    assert problems[2].startswith("image: not current against its inputs")


@pytest.mark.parametrize("recorded", [None, 0])
def test_publish_check_refuses_another_build_generation(
    artifacts: Any, recorded: int | None
) -> None:
    """Old artifacts must not take a new generation's key: one built by
    another generation, or before generations were recorded, is not
    what this checkout's build code makes.  An explicit --tag (no key)
    is refused too."""
    from ltvm_pkg.release_package import BUILD_GENERATION

    _edit_meta(artifacts.meta_path("kernel", K), build_generation=recorded)
    want = (
        f"kernel: built by build generation "
        f"{recorded if recorded is not None else '(unrecorded)'}, this "
        f"checkout's is {BUILD_GENERATION}"
    )
    assert cli_mod._publish_input_mismatches(artifacts, K, "base") == [want]


@pytest.mark.parametrize(
    "fields,why",
    [
        (
            {"container_input_hash": "0" * 16},
            "kernel: built in another build container than the base "
            "variant's build container (its inputs differ)",
        ),
        (
            {"container_input_hash": None},
            "kernel: does not record the build container it was built in",
        ),
        (
            {"container_image_id": "sha2"},
            "kernel: built in another build container than the base "
            "variant's build container (podman image sha2, not sha1)",
        ),
    ],
)
def test_publish_check_refuses_artifacts_from_another_container(
    artifacts: Any, fields: dict[str, Any], why: str
) -> None:
    """A changed container rebuilds as its own artifact while the kernel
    and image stay cached: their own inputs match, but they were built
    in the container before.  The release key names the new one."""
    _edit_meta(artifacts.meta_path("kernel", K), **fields)
    problems = cli_mod._publish_input_mismatches(artifacts, K, "base")
    assert len(problems) == 1
    assert problems[0].startswith(why)


def test_kernels_and_images_record_their_build_container(
    tmp_path: Path,
) -> None:
    """At build time, without moving an input hash or making anything
    stale."""
    import ltvm_pkg.target_config as cfg
    from tests.conftest import _make_kernel_outputs

    with patch.object(cfg, "ARTIFACTS_DIR", tmp_path):
        tc = cfg.TargetConfig("rocky9", arch="x86_64")
        _make_kernel_outputs(tc, K)
        # No container meta yet: its inputs only.
        tc.write_meta("kernel", kernel=K)
        meta = json.loads(tc.meta_path("kernel", K).read_text())
        assert meta["container_input_hash"] == tc.input_hash("container")
        assert "container_image_id" not in meta
        tc.write_meta("container", image_tag="t", image_id="sha-a")
        tc.write_meta("kernel", kernel=K)
        meta = json.loads(tc.meta_path("kernel", K).read_text())
        assert meta["container_image_id"] == "sha-a"
        assert meta["input_hash"] == tc.input_hash("kernel", K)
        assert not tc.is_stale("kernel", kernel=K)
        cmeta = json.loads(tc.meta_path("container").read_text())
        assert "container_input_hash" not in cmeta
        # A container meta for other inputs lends no id.
        cmeta["input_hash"] = "0" * 16
        tc.meta_path("container").write_text(json.dumps(cmeta))
        tc.write_meta("kernel", kernel=K)
        meta = json.loads(tc.meta_path("kernel", K).read_text())
        assert "container_image_id" not in meta
        # A kernel recording another container is not stale for builds.
        meta["container_input_hash"] = "1" * 16
        tc.meta_path("kernel", K).write_text(json.dumps(meta))
        assert not tc.is_stale("kernel", kernel=K)


def test_fetch_list_keeps_to_the_declared_kernel_asked_for(
    capsys: pytest.CaptureFixture[str],
) -> None:
    releases = [
        _mainline("latest", "7.1.0-rc3-lustre"),
        _mainline("stable", "7.0.10-lustre"),
        _rel(
            "mainline-x86_64-6.18.4-lustre",
            "manifest-mainline-x86_64-6.18.4-lustre.json",
        ),
    ]
    with _checkout(), patch.object(cli_mod, "_gh_api", return_value=releases):
        assert (
            cli_mod.cmd_fetch(
                _list_ns(target="mainline", kernel="stable", json=True)
            )
            == EXIT_OK
        )
        rows = json.loads(capsys.readouterr().out)
        # latest's release is not listed for stable.  The one without
        # notes cannot say, and a moving kernel has no signature to
        # tell it by, so it is listed as fetch would consider it.
        assert [r["tag"] for r in rows] == [
            rt.keyed_tag("mainline-x86_64-7.0.10-lustre", MINE),
            "mainline-x86_64-6.18.4-lustre",
        ]
        # Without --kernel, all of them.
        rows = cli_mod._list_releases("mainline", arch="x86_64")
        assert len(rows) == 3
        # A release without notes is still matched by its signature.
        rows = cli_mod._list_releases(
            "mainline", kernel_signature="6.18", arch="x86_64", kernel="6.18"
        )
        assert [r["tag"] for r in rows] == ["mainline-x86_64-6.18.4-lustre"]


def _pub_ns(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "json": True,
        "target": "rocky9",
        "kernel": K,
        "variant": "base",
        "arch": "x86_64",
        "image": False,
        "no_lustre": True,
        "no_upload": False,
        "output": None,
        "tag": None,
        "dry_run": False,
        "no_prune": False,
        "replace_unmerged": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


STALE = rt.keyed_tag(CORE, "5555555555")


def _releases_around(now: datetime, published: str) -> list[dict[str, Any]]:
    def ago(days: float) -> str:
        return (now - timedelta(days=days)).isoformat().replace("+00:00", "Z")

    el95 = "rocky9-x86_64-5.14.0-503.40.1.el9_5_lustre"
    return [
        _rel(published, published=ago(0), identity=IDENT),
        _rel(rt.keyed_tag(CORE, OTHER), published=ago(20), identity=IDENT),
        _rel(STALE, published=ago(60), identity=IDENT),
        _rel(OLD, published=ago(90)),
        _rel(
            rt.keyed_tag(el95, OTHER),
            f"manifest-{el95}.json",
            published=ago(90),
            identity=("rocky9", "x86_64", "5.14-rhel9.5", "base"),
        ),
    ]


def _absent() -> dict[str, Any]:
    return {"state": "absent", "body": "", "assets": []}


def _publish(
    artifacts: Any,
    tmp_path: Path,
    releases: list[dict[str, Any]],
    delete_result: str | None = None,
    on_default: Any = True,
    upload: Any = (None, None),
    info: Any = "absent",
    problem: str | None = None,
    **kw: Any,
) -> tuple[int, Any, Any, Any]:
    manifest = tmp_path / MANIFEST
    manifest.write_text("{}")
    with (
        _checkout(on_default=on_default),
        patch.object(rt, "checkout_problem", return_value=problem),
        patch.object(
            cli_mod,
            "_gh_release_info",
            return_value=_absent() if info == "absent" else info,
        ),
        patch.object(cli_mod, "_gh_api", return_value=releases) as api,
        patch.object(
            cli_mod, "package_target", return_value={"manifest": manifest}
        ),
        patch.object(cli_mod, "_gh_release_upload", return_value=upload) as up,
        patch.object(
            cli_mod, "_gh_release_delete", return_value=delete_result
        ) as rm,
        patch(
            "ltvm_pkg.release_package.check_build_container",
            return_value="sha1",
        ),
    ):
        rc = cli_mod.cmd_publish(_pub_ns(**kw))
    return rc, api, up, rm


def test_publish_refuses_artifacts_from_other_inputs(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_meta(
        artifacts.meta_path("image", K), **{"image.Dockerfile": "0" * 12}
    )
    rc, _, up, _ = _publish(artifacts, tmp_path, [], json=False)
    assert rc == EXIT_ERROR
    err = capsys.readouterr().err
    assert "not built from this checkout's inputs" in err
    assert "image: image.Dockerfile (changed)" in err
    assert not up.called


def test_publish_refuses_a_dirty_checkout(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _, up, _ = _publish(
        artifacts,
        tmp_path,
        [],
        problem="the ltvm checkout has uncommitted changes to tracked files",
        json=False,
    )
    assert rc == EXIT_ERROR
    assert "uncommitted changes" in capsys.readouterr().err
    assert not up.called


@pytest.mark.parametrize("tag", [OLD, "my-tag", rt.keyed_tag(CORE, MINE)])
def test_publish_takes_no_tag_for_an_ecosystem_release(
    artifacts: Any, tmp_path: Path, tag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The tag is always derived: by hand it could carry a key the
    artifacts were not built with, or be an unprefixed tag older ltvm
    fetches and cannot load these archives from."""
    for dry_run in (False, True):
        rc, _, up, _ = _publish(
            artifacts, tmp_path, [], tag=tag, dry_run=dry_run, json=False
        )
        assert rc == EXIT_ERROR
        assert "always tagged from its kernel and key" in (
            capsys.readouterr().err
        )
        assert not up.called


def test_publish_from_a_branch_leaves_a_draft(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Off the default branch nothing is published for everyone, and
    nothing is pruned: the draft waits for promote after the merge."""
    tag = rt.keyed_tag(CORE, MINE)
    now = datetime.now(timezone.utc)
    for on_default in (False, None):
        rc, api, up, rm = _publish(
            artifacts,
            tmp_path,
            _releases_around(now, tag),
            on_default=on_default,
        )
        assert rc == EXIT_OK
        assert up.call_args.kwargs["public"] is False
        assert not api.called and not rm.called
        out = json.loads(capsys.readouterr().out)
        assert out["draft"] is True
        assert "pruned" not in out


def test_publish_from_the_default_branch_is_public_and_prunes(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tag = rt.keyed_tag(CORE, MINE)
    now = datetime.now(timezone.utc)
    rc, _, up, rm = _publish(artifacts, tmp_path, _releases_around(now, tag))
    assert rc == EXIT_OK
    assert up.call_args.args[0] == tag
    assert up.call_args.kwargs["public"] is True
    notes = rt.Publisher.of({"body": up.call_args.kwargs["notes"]})
    assert notes.identity() == IDENT
    assert (notes.version, notes.commit, notes.schema) == (
        THIS_LTVM,
        HEAD,
        SCHEMA,
    )
    # Old-scheme releases and other kernels are never pruned.
    assert [c.args[0] for c in rm.call_args_list] == [STALE]
    out = json.loads(capsys.readouterr().out)
    assert out["draft"] is False
    actions = {p["tag"]: p["action"] for p in out["pruned"]}
    assert actions == {
        tag: "kept",
        rt.keyed_tag(CORE, OTHER): "kept",
        STALE: "deleted",
    }


def _info(state: str, commit: str | None = HEAD, **kw: Any) -> dict[str, Any]:
    return {
        "state": state,
        "body": _notes(identity=kw.get("identity", IDENT), commit=commit),
        "assets": kw.get("assets", [MANIFEST]),
    }


@pytest.mark.parametrize(
    "info,on_default,flag,refused,why",
    [
        # A published release is replaced from the default branch only.
        (_info("published", ELSE), True, False, False, ""),
        (_info("published", ELSE), False, False, True, "is published"),
        (_info("published", ELSE), None, True, True, "is published"),
        # This commit's draft is resumed; another commit's is someone
        # else's publish, taken over only on request.
        (_info("draft", HEAD), False, False, False, ""),
        (_info("draft", ELSE), False, False, True, "--replace-unmerged"),
        (_info("draft", ELSE), True, False, True, "--replace-unmerged"),
        (_info("draft", ELSE), False, True, False, ""),
        (_info("draft", None), False, False, True, "--replace-unmerged"),
        # A draft naming another kernel is never taken over.
        (
            _info("draft", HEAD, identity=("rocky9", "x86_64", "x", "base")),
            False,
            True,
            True,
            "names rocky9/x86_64/x/base",
        ),
        # One manifest per release.
        (
            _info("draft", HEAD, assets=["manifest-rocky9-x86_64-other.json"]),
            False,
            True,
            True,
            "two manifests",
        ),
        (None, True, False, True, "cannot tell"),
    ],
)
def test_publish_refuses_what_it_must_not_replace(
    artifacts: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    info: dict[str, Any] | None,
    on_default: bool | None,
    flag: bool,
    refused: bool,
    why: str,
) -> None:
    tag = rt.keyed_tag(CORE, MINE)
    for dry_run in (False, True):
        rc, _, up, _ = _publish(
            artifacts,
            tmp_path,
            [],
            on_default=on_default,
            info=info,
            replace_unmerged=flag,
            dry_run=dry_run,
            json=False,
        )
        err = capsys.readouterr().err
        assert (rc == EXIT_ERROR) is refused, (dry_run, err)
        assert up.called is (not refused and not dry_run)
        if refused:
            assert why in err
            if info is not None:
                assert tag in err


def test_publish_dry_run_reports_without_doing(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tag = rt.keyed_tag(CORE, MINE)
    now = datetime.now(timezone.utc)
    with patch.object(cli_mod, "package_target") as pt:
        rc, _, up, rm = _publish(
            artifacts, tmp_path, _releases_around(now, "x"), dry_run=True
        )
    assert rc == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["tag"] == tag
    assert out["key"] == MINE
    assert out["draft"] is False
    actions = {p["tag"]: p["action"] for p in out["pruned"]}
    assert actions == {rt.keyed_tag(CORE, OTHER): "kept", STALE: "would delete"}
    assert not pt.called and not up.called and not rm.called
    # From a branch: a draft, so nothing would be pruned.
    rc, api, _, _ = _publish(
        artifacts, tmp_path, [], on_default=False, dry_run=True
    )
    out = json.loads(capsys.readouterr().out)
    assert (out["draft"], out["pruned"]) == (True, [])
    assert not api.called


def test_publish_bootable_dry_run_uploads_nothing(
    artifacts: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch("ltvm_pkg.release_package.package_bootable") as pb,
        patch.object(cli_mod, "_gh_release_upload") as up,
    ):
        rc = cli_mod.cmd_publish(_pub_ns(image=True, dry_run=True))
    assert rc == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["tag"] == f"bootable-{CORE}"
    assert out["dry_run"] is True
    assert not pb.called
    assert not up.called


def test_publish_names_the_declared_kernel_for_a_full_kernel_dir(
    artifacts: Any, tmp_path: Path
) -> None:
    """RELEASING.md has publish take the build's full kernel dir."""
    full = "5.14-rhel9.7-5.14.0-611.13.1.el9_7"
    out = artifacts.output_dir
    for d in ("kernels", "images"):
        (out / d / K).rename(out / d / full)
    rc, _, up, _ = _publish(artifacts, tmp_path, [], kernel=full)
    assert rc == EXIT_OK
    notes = rt.Publisher.of({"body": up.call_args.kwargs["notes"]})
    assert notes.kernel == K


def test_publish_text_names_each_release_and_why(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tag = rt.keyed_tag(CORE, MINE)
    now = datetime.now(timezone.utc)
    _publish(artifacts, tmp_path, _releases_around(now, tag), json=False)
    out = capsys.readouterr().out
    assert f"kept     {tag} (just published)" in out
    assert f"kept     {rt.keyed_tag(CORE, OTHER)} (newest other release" in out
    assert f"deleted  {STALE} (superseded" in out
    assert f" {OLD} (" not in out


def test_publish_failed_delete_is_a_warning(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tag = rt.keyed_tag(CORE, MINE)
    now = datetime.now(timezone.utc)
    rc, _, _, rm = _publish(
        artifacts, tmp_path, _releases_around(now, tag), delete_result="boom"
    )
    assert rc == EXIT_OK
    assert rm.called
    captured = capsys.readouterr()
    assert f"could not delete release {STALE}: boom" in captured.err
    pruned = json.loads(captured.out)["pruned"]
    assert {p["tag"]: p["action"] for p in pruned}[STALE] == "delete failed"


def test_publish_no_prune(artifacts: Any, tmp_path: Path) -> None:
    rc, api, _, rm = _publish(artifacts, tmp_path, [], no_prune=True)
    assert rc == EXIT_OK
    assert not api.called
    assert not rm.called


def test_publish_failed_upload_prunes_nothing(
    artifacts: Any, tmp_path: Path
) -> None:
    """A failure at any step leaves the release a draft (see
    test_fetch_publish's _gh_release_upload tests) and fails the
    publish, which then prunes nothing."""
    rc, api, _, rm = _publish(
        artifacts, tmp_path, [], upload=(EXIT_ERROR, "left as a draft")
    )
    assert rc == EXIT_ERROR
    assert not api.called
    assert not rm.called


def _proc(rc: int, out: str = "", err: str = "") -> Any:
    return MagicMock(returncode=rc, stdout=out, stderr=err)


def test_a_draft_upload_ends_a_draft(tmp_path: Path) -> None:
    a = tmp_path / "x.tar.zst"
    a.write_bytes(b"x")
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> Any:
        calls.append(cmd)
        return _proc(0)

    with (
        patch.object(cli_mod, "_gh_release_info", return_value=_info("draft")),
        patch("ltvm_pkg.cli.subprocess.run", side_effect=run),
    ):
        rc, _ = cli_mod._gh_release_upload("t", [a], "n", True, public=False)
    assert rc is None
    # Even a draft loses its completion mark before any asset changes.
    assert calls[0][:4] == ["gh", "release", "edit", "t"]
    assert calls[1][:3] == ["gh", "release", "upload"]
    assert calls[-1][:4] == ["gh", "release", "edit", "t"]
    assert "--draft=true" in calls[-1]


def _notes_of(cmd: list[str]) -> str:
    return cmd[cmd.index("--notes") + 1]


@pytest.mark.parametrize("state", ["published", "draft", "absent"])
def test_the_completion_mark_goes_first_and_comes_back_last(
    tmp_path: Path, state: str
) -> None:
    a = tmp_path / "x.tar.zst"
    a.write_bytes(b"x")
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> Any:
        calls.append(cmd)
        return _proc(0)

    with _checkout():
        notes = rt.release_notes(
            "s", target="rocky9", arch="x86_64", kernel=K, variant="base"
        )
    with (
        patch.object(
            cli_mod,
            "_gh_release_info",
            return_value=_info(state) if state != "absent" else _absent(),
        ),
        patch("ltvm_pkg.cli.subprocess.run", side_effect=run),
    ):
        cli_mod._gh_release_upload("t", [a], notes, True, public=True)
    first, last = calls[0], calls[-1]
    assert first[2] == ("create" if state == "absent" else "edit")
    assert "--draft" in first or "--draft=true" in first
    assert not rt.Publisher.of({"body": _notes_of(first)}).complete
    assert calls[1][:3] == ["gh", "release", "upload"]
    assert rt.Publisher.of({"body": _notes_of(last)}).complete
    assert "--draft=false" in last


def test_an_interrupted_replacement_is_never_promoted_or_fetched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A republish fails after replacing some assets: the release is a
    draft whose notes lack the mark.  Promote leaves it; were it public
    somehow, fetch would pass it by too."""
    a, b = tmp_path / "a.tar.zst", tmp_path / "b.tar.zst"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> Any:
        calls.append(cmd)
        uploads = [c for c in calls if c[2] == "upload"]
        return _proc(1 if len(uploads) == 2 else 0, err="network")

    tag = rt.keyed_tag(CORE, MINE)
    with _checkout():
        notes = rt.release_notes(
            "s", target="rocky9", arch="x86_64", kernel=K, variant="base"
        )
    with (
        patch.object(
            cli_mod,
            "_gh_release_info",
            return_value={"state": "published", "body": notes, "assets": []},
        ),
        patch("ltvm_pkg.cli.subprocess.run", side_effect=run),
    ):
        rc, err = cli_mod._gh_release_upload(tag, [a, b], notes, True)
    assert rc == EXIT_ERROR and err is not None
    assert "incomplete" in err
    left_body = _notes_of(calls[0])
    broken = {
        "tag_name": tag,
        "draft": True,
        "body": left_body,
        "assets": [{"name": MANIFEST}],
    }
    with (
        _checkout(),
        patch.object(rt, "checkout_problem", return_value=None),
        patch.object(cli_mod, "_gh_draft_releases", return_value=[broken]),
        patch.object(cli_mod, "_gh_release_publish") as pub,
    ):
        assert cli_mod.cmd_promote(_promote_ns()) == EXIT_OK
    assert not pub.called
    out = json.loads(capsys.readouterr().out)
    assert "did not finish" in out["left"][0]["reason"]
    public = dict(broken, draft=False)
    with _checkout():
        with pytest.raises(RuntimeError, match="No published artifacts"):
            _find([public])


def test_gh_release_info_reads_drafts() -> None:
    from ltvm_pkg.cli.fetch import _gh_release_info

    for result, want in (
        (
            _proc(
                0,
                json.dumps(
                    {"isDraft": True, "body": "b", "assets": [{"name": "m"}]}
                ),
            ),
            {"state": "draft", "body": "b", "assets": ["m"]},
        ),
        (
            _proc(
                0, json.dumps({"isDraft": False, "body": None, "assets": []})
            ),
            {"state": "published", "body": "", "assets": []},
        ),
        (_proc(1, err="release not found"), _absent()),
        (_proc(1, err="HTTP 401"), None),
        (_proc(0, "garbage"), None),
    ):
        with patch("ltvm_pkg.cli.subprocess.run", return_value=result) as run:
            assert _gh_release_info("t") == want
        assert run.call_args.args[0][:4] == ["gh", "release", "view", "t"]


# ------------------------------------------------------------------
# promote
# ------------------------------------------------------------------


def _promote_ns(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "json": True,
        "targets": ["rocky9"],
        "all": False,
        "arch": None,
        "dry_run": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _drafts() -> list[dict[str, Any]]:
    el95 = "rocky9-x86_64-5.14.0-503.40.1.el9_5_lustre"
    return [
        _rel(
            rt.keyed_tag(CORE, MINE),
            identity=IDENT,
            draft=True,
            built_at="2026-11-01T00:00:00Z",
        ),
        _rel(rt.keyed_tag(CORE, OTHER), identity=IDENT, draft=True),
        _rel(rt.keyed_tag(CORE, "c" * 10), draft=True),
        _rel(
            rt.keyed_tag(el95, MINE),
            f"manifest-{el95}.json",
            MANIFEST,
            identity=("rocky9", "x86_64", "5.14-rhel9.5", "base"),
            draft=True,
        ),
        _rel(
            rt.keyed_tag(f"rocky9-aarch64-{KVER}", MINE),
            f"manifest-rocky9-aarch64-{KVER}.json",
            identity=("rocky9", "aarch64", K, "base"),
            draft=True,
            built_at="2026-11-02T00:00:00Z",
        ),
        _rel(
            rt.keyed_tag(f"rocky8-x86_64-{KVER}", MINE),
            identity=("rocky8", "x86_64", "4.18-rhel8.10", "base"),
            draft=True,
        ),
    ]


def _published_around(now: datetime) -> list[dict[str, Any]]:
    """What is public while the drafts wait: the older releases of
    their kernel, none of them the drafts themselves."""
    mine = rt.keyed_tag(CORE, MINE)
    return [r for r in _releases_around(now, mine) if r["tag_name"] != mine]


def _promote(
    on_default: Any = True,
    problem: str | None = None,
    published: str | None = None,
    **kw: Any,
) -> tuple[int, Any, Any]:
    now = datetime.now(timezone.utc)
    with (
        _checkout(on_default=on_default),
        patch.object(rt, "checkout_problem", return_value=problem),
        patch.object(cli_mod, "_gh_draft_releases", return_value=_drafts()),
        patch.object(
            cli_mod,
            "_gh_release_publish",
            side_effect=lambda tag, notes: published,
        ) as pub,
        patch.object(cli_mod, "_gh_api", return_value=_published_around(now)),
        patch.object(cli_mod, "_gh_release_delete", return_value=None) as rm,
    ):
        rc = cli_mod.cmd_promote(_promote_ns(**kw))
    return rc, pub, rm


def test_promote_makes_public_the_drafts_this_checkout_builds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, pub, rm = _promote()
    assert rc == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    promoted = [p["tag"] for p in out["promoted"]]
    aarch64 = rt.keyed_tag(f"rocky9-aarch64-{KVER}", MINE)
    assert promoted == [rt.keyed_tag(CORE, MINE), aarch64]
    assert [c.args[0] for c in pub.call_args_list] == promoted
    left = {p["tag"]: p["reason"] for p in out["left"]}
    assert "built from other inputs" in left[rt.keyed_tag(CORE, OTHER)]
    assert "notes do not name" in left[rt.keyed_tag(CORE, "c" * 10)]
    el95 = "rocky9-x86_64-5.14.0-503.40.1.el9_5_lustre"
    assert "exactly one manifest" in left[rt.keyed_tag(el95, MINE)]
    # Another target's draft is neither promoted nor reported.
    assert not any("rocky8" in t for t in promoted + list(left))
    # Each promotion prunes what it supersedes.
    assert STALE in [c.args[0] for c in rm.call_args_list]
    pruned = out["promoted"][0]["pruned"]
    assert {p["tag"]: p["action"] for p in pruned}[STALE] == "deleted"


def test_promote_keeps_to_an_arch_and_to_all_targets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _promote(arch="x86_64")
    out = json.loads(capsys.readouterr().out)
    assert [p["tag"] for p in out["promoted"]] == [rt.keyed_tag(CORE, MINE)]
    with patch.object(
        cli_mod, "list_targets", return_value=["rocky8", "rocky9"]
    ):
        _promote(targets=[], all=True, arch="x86_64")
    out = json.loads(capsys.readouterr().out)
    assert rt.keyed_tag(f"rocky8-x86_64-{KVER}", MINE) in [
        p["tag"] for p in out["promoted"]
    ]


def test_promote_dry_run_changes_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, pub, rm = _promote(dry_run=True)
    assert rc == EXIT_OK
    assert not pub.called and not rm.called
    out = json.loads(capsys.readouterr().out)
    assert out["promoted"][0]["action"] == "would promote"
    assert {p["tag"]: p["action"] for p in out["promoted"][0]["pruned"]}[
        STALE
    ] == "would delete"


def test_a_failed_deletion_fails_promote_after_the_rest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(timezone.utc)
    with (
        _checkout(),
        patch.object(rt, "checkout_problem", return_value=None),
        patch.object(cli_mod, "_gh_draft_releases", return_value=_drafts()),
        patch.object(cli_mod, "_gh_release_publish", return_value=None) as pub,
        patch.object(
            cli_mod,
            "_gh_api",
            return_value=_published_around(now),
        ),
        patch.object(cli_mod, "_gh_release_delete", return_value="denied"),
    ):
        rc = cli_mod.cmd_promote(_promote_ns())
    assert rc == EXIT_ERROR
    # The other draft was still promoted.
    assert pub.call_count == 2
    assert json.loads(capsys.readouterr().out)["failed"] is True


def test_drafts_that_do_not_match_do_not_fail_promote() -> None:
    with (
        _checkout(key="eeeeeeeeee"),
        patch.object(rt, "checkout_problem", return_value=None),
        patch.object(cli_mod, "_gh_draft_releases", return_value=_drafts()),
        patch.object(cli_mod, "_gh_release_publish") as pub,
    ):
        assert cli_mod.cmd_promote(_promote_ns()) == EXIT_OK
    assert not pub.called


def test_promote_records_its_version_in_the_notes() -> None:
    """A branch's version count does not order against the default
    branch's: the promoting ltvm's version is what the update prompt
    compares, the publisher's stays as provenance."""
    rc, pub, _ = _promote()
    assert rc == EXIT_OK
    notes = rt.Publisher.of({"body": pub.call_args_list[0].args[1]})
    assert notes.promoted_version == THIS_LTVM
    assert notes.complete
    assert notes.identity() == IDENT


def test_the_update_prompt_compares_the_promoting_version() -> None:
    def rel(version: str, promoted: str | None) -> dict[str, Any]:
        r = _rel(rt.keyed_tag(CORE, OTHER), version=version)
        if promoted:
            r["body"] = rt.promoted_notes(r["body"], promoted)
        return r

    # A squash merge: the branch counted more commits than the default
    # branch has, which promoted it at a version no newer than this.
    assert _pick([rel("0.5.900+pr", "0.5.390+m")]) == rt.Pick(0, False)
    assert _pick([rel("0.5.100+pr", "0.5.410+m")]) == rt.Pick(
        0, False, newer="0.5.410+m"
    )
    assert _pick([rel("0.5.410+pr", None)]).newer == "0.5.410+pr"


def test_a_failed_promotion_is_left_and_prunes_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, pub, rm = _promote(published="denied")
    assert rc == EXIT_ERROR
    assert not rm.called
    out = json.loads(capsys.readouterr().out)
    assert out["promoted"] == []
    left = {p["tag"]: p["reason"] for p in out["left"]}
    assert "denied" in left[rt.keyed_tag(CORE, MINE)]


@pytest.mark.parametrize(
    "kw,why",
    [
        ({"on_default": False}, "not in the history"),
        ({"on_default": None}, "not known to be on"),
        (
            {"problem": "the ltvm checkout has uncommitted changes"},
            "uncommitted",
        ),
        ({"targets": []}, "--all"),
        ({"all": True}, "not both"),
    ],
)
def test_promote_refuses(
    kw: dict[str, Any], why: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, pub, _ = _promote(**kw, json=False)
    assert rc == EXIT_ERROR
    assert why in capsys.readouterr().err
    assert not pub.called


def test_gh_draft_releases_lists_drafts_only() -> None:
    from ltvm_pkg.cli.fetch import _gh_draft_releases

    rows = [
        {
            "tagName": "rel-a-1111111111",
            "isDraft": True,
            "createdAt": "2026-11-01T00:00:00Z",
        },
        {"tagName": "rel-b-2222222222", "isDraft": False},
    ]
    with (
        patch(
            "ltvm_pkg.cli.subprocess.run",
            return_value=_proc(0, json.dumps(rows)),
        ),
        patch.object(
            cli_mod,
            "_gh_release_info",
            return_value={"state": "draft", "body": "b", "assets": ["m.json"]},
        ) as info,
    ):
        got = _gh_draft_releases()
    assert got == [
        {
            "tag_name": "rel-a-1111111111",
            "draft": True,
            "created_at": "2026-11-01T00:00:00Z",
            "body": "b",
            "assets": [{"name": "m.json"}],
        }
    ]
    info.assert_called_once_with("rel-a-1111111111")
    # One that cannot be read is kept, so promote can report it.
    with (
        patch(
            "ltvm_pkg.cli.subprocess.run",
            return_value=_proc(0, json.dumps(rows)),
        ),
        patch.object(cli_mod, "_gh_release_info", return_value=None),
    ):
        got = _gh_draft_releases()
    assert [(r["tag_name"], r.get("lookup_failed")) for r in got] == [
        ("rel-a-1111111111", True)
    ]
    with patch("ltvm_pkg.cli.subprocess.run", return_value=_proc(1, err="x")):
        with pytest.raises(RuntimeError):
            _gh_draft_releases()


# ------------------------------------------------------------------
# The exported container is the one checked
# ------------------------------------------------------------------


def _podman(image_id: str | None) -> tuple[list[list[str]], Any]:
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> Any:
        calls.append(cmd)
        if cmd[:3] == ["podman", "image", "inspect"]:
            return MagicMock(
                returncode=0 if image_id else 1,
                stdout=f"{image_id}\n" if image_id else "",
            )
        if cmd[:2] == ["podman", "save"]:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"tar")
        return MagicMock(returncode=0, stdout="", stderr="")

    return calls, patch(
        "ltvm_pkg.release_package.subprocess.run", side_effect=run
    )


def _container_meta(out: Path, **fields: Any) -> None:
    d = out / "rocky9" / "x86_64" / "container"
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(fields))


@pytest.mark.parametrize(
    "recorded,tagged,ok",
    [
        ("sha-a", "sha-a", True),
        ("sha-a", "sha-b", False),
        (None, "sha-a", False),
    ],
)
def test_package_exports_only_the_container_the_meta_describes(
    tmp_path: Path, recorded: str | None, tagged: str, ok: bool
) -> None:
    from ltvm_pkg import release_package as rp

    out = tmp_path / "rocky9" / "x86_64"
    _container_meta(
        tmp_path,
        image_tag="ltvm-build-rocky9",
        **({"image_id": recorded} if recorded else {}),
    )
    calls, p = _podman(tagged)
    with p, patch.object(rp, "_ensure_podman"):
        if ok:
            rp.export_build_container("rocky9", out, arch="x86_64")
        else:
            with pytest.raises(RuntimeError) as exc:
                rp.export_build_container("rocky9", out, arch="x86_64")
            assert "ltvm build container rocky9" in str(exc.value)
    saved = [c for c in calls if c[:2] == ["podman", "save"]]
    assert bool(saved) is ok
    if ok:
        # The id checked is the one exported, with podman save, which a
        # remote podman streams to the client: no window in which the
        # tag could move.
        tar = out / "container" / "image.tar"
        assert saved == [["podman", "save", "-o", str(tar), "sha-a"]]
        assert tar.read_bytes() == b"tar"


@pytest.mark.parametrize(
    "said,ref",
    [
        ("Loaded image: sha256:" + "a" * 64 + "\n", "a" * 64),
        (
            "Loaded image: localhost/ltvm-build-rocky9:latest\n",
            "localhost/ltvm-build-rocky9:latest",
        ),
        (
            "Getting image\nLoaded image(s): localhost/x:latest\n",
            "localhost/x:latest",
        ),
        ("nothing\n", None),
    ],
)
def test_the_loaded_image_is_read_from_podman_load(
    said: str, ref: str | None
) -> None:
    from ltvm_pkg.release_package import loaded_image_ref

    assert loaded_image_ref(said) == ref


def test_every_loader_tags_what_it_loads(tmp_path: Path) -> None:
    """An archive saved by id carries no tag, an older one does: the
    loader names what it loaded either way."""
    from ltvm_pkg import release_package as rp

    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> Any:
        calls.append(cmd)
        out = "Loaded image: sha256:abc\n" if cmd[1] == "load" else ""
        return MagicMock(returncode=0, stdout=out, stderr="")

    tar = tmp_path / "image.tar"
    with patch("ltvm_pkg.release_package.subprocess.run", side_effect=run):
        assert rp.load_container_archive(tar, "ltvm-build-rocky9") == "abc"
    assert calls == [
        ["podman", "load", "-i", str(tar)],
        ["podman", "tag", "abc", "ltvm-build-rocky9"],
    ]


def test_fetch_tags_the_container_it_loads(tmp_path: Path) -> None:
    from ltvm_pkg import release_package as rp

    with patch.object(rp, "load_container_archive", return_value="abc") as load:
        target_dir = tmp_path / "rocky9" / "x86_64"
        archive = target_dir / "container" / "image.tar"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"x")
        manifest = {
            "schema": rp._schema_id(),
            "target": "rocky9",
            "kernel": K,
            "variant": "base",
            "assets": [],
        }

        def fake_download(url: str, dest: Path, **kw: Any) -> None:
            dest.write_text(json.dumps(manifest))

        with (
            patch.object(rp, "_download", side_effect=fake_download),
            patch.object(rp, "_ensure_podman"),
            patch.object(rp, "share_base_images"),
        ):
            rp.fetch_target("rocky9", "https://ex/m.json", tmp_path)
    load.assert_called_once_with(archive, "ltvm-build-rocky9")


@pytest.mark.skipif(
    __import__("shutil").which("podman") is None, reason="no podman"
)
def test_archives_saved_by_id_and_by_tag_load_under_the_tag(
    tmp_path: Path,
) -> None:
    """Against real podman, with a throwaway image: an archive saved by
    id, as export now writes, and one saved by tag, as releases before
    it carry, both load as the same id under the name loaders use."""
    import subprocess

    from ltvm_pkg import release_package as rp

    def podman(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["podman", *args], capture_output=True, text=True, timeout=120
        )

    src = "localhost/ltvm-pytest-keyed-src:latest"
    name = "ltvm-pytest-keyed"
    (tmp_path / "f").write_text("x")
    subprocess.run(
        ["tar", "cf", str(tmp_path / "in.tar"), "-C", str(tmp_path), "f"],
        check=True,
    )
    if podman("import", "-q", str(tmp_path / "in.tar"), src).returncode != 0:
        pytest.skip("podman cannot import here")
    try:
        image_id = rp.container_image_id(src)
        assert image_id
        by_id = tmp_path / "by-id.tar"
        by_tag = tmp_path / "by-tag.tar"
        assert podman("save", "-o", str(by_id), image_id).returncode == 0
        assert podman("tag", src, name).returncode == 0
        assert podman("save", "-o", str(by_tag), name).returncode == 0
        for archive in (by_id, by_tag):
            podman("rmi", "-f", src, f"localhost/{name}:latest")
            assert rp.container_image_id(name) is None
            rp.load_container_archive(archive, name)
            assert rp.container_image_id(name) == image_id
    finally:
        podman("rmi", "-f", src)
        podman("rmi", "-f", f"localhost/{name}:latest")


def test_container_build_records_the_id_its_build_wrote(
    tmp_path: Path,
) -> None:
    """The id comes from the build (podman build --iidfile), not from
    the shared tag afterwards, which another checkout may have moved."""
    import ltvm_pkg.target_config as cfg
    from ltvm_pkg.cli.build import _do_build_container

    def build(tc: Any, iidfile: Path | None = None) -> str:
        assert iidfile is not None
        iidfile.write_text("sha256:" + "a" * 64)
        return "ltvm-build-rocky9"

    with patch.object(cfg, "ARTIFACTS_DIR", tmp_path):
        tc = cfg.TargetConfig("rocky9", arch="x86_64")
        with (
            patch(
                "ltvm_pkg.kernel_build._ensure_container_image",
                side_effect=build,
            ),
            patch("ltvm_pkg.release_package.container_image_id") as inspect,
        ):
            _do_build_container(tc)
        assert not inspect.called
        meta = json.loads(tc.meta_path("container").read_text())
        assert meta["image_id"] == "a" * 64
        # Recording it moves no input hash.
        assert meta["input_hash"] == tc.input_hash("container")
        assert not tc.is_stale("container")


def test_every_container_build_writes_the_iidfile(tmp_path: Path) -> None:
    from ltvm_pkg.kernel_build import _ensure_container_image

    target_dir = tmp_path / "rocky9"
    target_dir.mkdir()
    (target_dir / "container.Dockerfile").write_text("FROM x\n")
    tc = MagicMock()
    tc.name, tc.arch, tc.variant_name = "rocky9", "x86_64", "base"
    tc.target_dir = target_dir
    iid = tmp_path / "iid"
    with patch("ltvm_pkg.kernel_build.subprocess.run") as run:
        _ensure_container_image(tc, iidfile=iid)
    cmd = run.call_args.args[0]
    assert cmd[cmd.index("--iidfile") + 1] == str(iid)
    with patch("ltvm_pkg.kernel_build.subprocess.run") as run:
        _ensure_container_image(tc)
    assert "--iidfile" not in run.call_args.args[0]


# ------------------------------------------------------------------
# prune_plan
# ------------------------------------------------------------------

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
PUB = rt.keyed_tag(CORE, MINE)


def _at(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _k(core: str = CORE, key: str = "6666666666") -> str:
    return rt.keyed_tag(core, key)


def _cand(
    tag: str,
    days: float | None,
    identity: tuple[str, str, str, str] = IDENT,
    *names: str,
    draft: bool = False,
) -> dict[str, Any]:
    return _rel(
        tag,
        *names,
        published=None if days is None else _at(days),
        identity=identity,
        draft=draft,
    )


def _plan_prune(releases: list[dict[str, Any]]) -> dict[str, tuple[bool, str]]:
    plan = rt.prune_plan(releases, PUB, NOW, IDENT)
    return {d.tag: (d.delete, d.reason) for d in plan}


def test_prune_the_age_boundary() -> None:
    exactly = _k(key="1111111111")
    young = _k(key="2222222222")
    stale = _k(key="3333333333")
    newest = _k(key=OTHER)
    plan = _plan_prune(
        [
            _cand(PUB, 0),
            _cand(newest, 1),
            _cand(exactly, 14),
            _cand(young, 14 - 1 / 86400),
            _cand(stale, 40),
        ]
    )
    assert plan[PUB] == (False, "just published")
    assert plan[newest] == (False, "newest other release")
    assert plan[exactly][0] is True
    assert plan[young][0] is False
    assert "under 14 days old" in plan[young][1]
    assert plan[stale][0] is True


def test_prune_keeps_the_newest_other_however_old() -> None:
    newest = _k(key=OTHER)
    plan = _plan_prune([_cand(newest, 100), _cand(_k(), 200)])
    assert plan[newest][0] is False
    assert plan[_k()][0] is True


def test_prune_never_touches_drafts_or_old_scheme_releases() -> None:
    """Drafts are branches' publishes waiting for promote; unkeyed
    releases are all an ltvm older than keys can fetch."""
    plan = _plan_prune(
        [
            _cand(_k(key=OTHER), 1),
            _rel(OLD, published=_at(400)),
            _cand(_k(), 400, draft=True),
        ]
    )
    assert list(plan) == [_k(key=OTHER)]


def test_prune_reads_identity_from_the_notes_only() -> None:
    """Nothing is inferred from asset names: not another kernel, arch,
    target or variant, nor a variant this checkout no longer declares
    whose numeric suffix reads like a kernel version, nor a release
    whose notes do not say."""
    el95 = "rocky9-x86_64-5.14.0-503.40.1.el9_5_lustre"
    gone = f"{CORE}-mofed-24"
    releases = [
        _cand(
            _k(el95),
            400,
            ("rocky9", "x86_64", "5.14-rhel9.5", "base"),
            f"manifest-{el95}.json",
        ),
        _cand(
            _k(gone),
            400,
            ("rocky9", "x86_64", K, "mofed-24"),
            f"manifest-{gone}.json",
        ),
        _cand(_k(key="7777777777"), 400, ("rocky9", "aarch64", K, "base")),
        _cand(_k(key="8888888888"), 400, ("rocky9-64k", "x86_64", K, "base")),
        _rel(_k(key="9999999999"), published=_at(400)),
        _rel(
            f"bootable-{CORE}", f"bootable-{CORE}.qcow2.zst", published=_at(400)
        ),
        _rel("qemu-9.2.2", "qemu.tar.gz", published=_at(400)),
        # Two manifests in one release: never a candidate.
        _cand(
            _k(key="abababab12"),
            400,
            IDENT,
            MANIFEST,
            f"manifest-{gone}.json",
        ),
    ]
    assert _plan_prune(releases) == {}


def test_prune_follows_a_kernel_whose_version_moved() -> None:
    moved = "rocky9-x86_64-5.14.0-570.12.1.el9_7_lustre"
    plan = _plan_prune(
        [
            _cand(_k(key=OTHER), 1),
            _cand(_k(moved), 60, IDENT, f"manifest-{moved}.json"),
        ]
    )
    assert plan[_k(moved)][0] is True


def test_prune_keeps_a_release_with_no_date() -> None:
    plan = _plan_prune([_cand(_k(key=OTHER), 1), _cand(_k(), None)])
    assert plan[_k()] == (False, "no publish date")


# ------------------------------------------------------------------
# Newest published first, whatever order the API lists
# ------------------------------------------------------------------


def _late_promotion() -> list[dict[str, Any]]:
    """Draft A was created first and promoted last; B was created and
    published in between.  The API lists by creation, B first."""
    return [
        _rel(
            rt.keyed_tag(CORE, "b" * 10),
            identity=IDENT,
            version="0.5.1+b",
            published="2026-11-10T00:00:00Z",
        ),
        _rel(
            rt.keyed_tag(CORE, "a" * 10),
            identity=IDENT,
            version="0.5.1+a",
            published="2026-11-12T00:00:00Z",
        ),
        _rel(OLD, published="2026-01-01T00:00:00Z"),
    ]


def test_selectable_sorts_by_publication_and_drops_the_unfinished() -> None:
    unfinished = _rel(
        rt.keyed_tag(CORE, "d" * 10),
        published="2026-11-15T00:00:00Z",
        complete=False,
    )
    undated = _rel("rocky9-x86_64-undated")
    got = rt.selectable([*_late_promotion(), unfinished, undated])
    assert [r["tag_name"] for r in got] == [
        rt.keyed_tag(CORE, "a" * 10),
        rt.keyed_tag(CORE, "b" * 10),
        OLD,
        "rocky9-x86_64-undated",
    ]


def test_every_selection_takes_the_newest_published() -> None:
    import ltvm_pkg.target_config as cfg

    late = rt.keyed_tag(CORE, "a" * 10)
    with _checkout(key="eeeeeeeeee"):
        assert f"/{late}/" in _find(_late_promotion())
        with patch.object(cfg, "ARTIFACTS_DIR", Path("/nonexistent")):
            _, remote, _ = cli_mod._release_status_full(
                "rocky9", "x86_64", _late_promotion(), kernel=K
            )
        assert remote == f"{KVER}-{'a' * 10}"
        with patch.object(cli_mod, "_gh_api", return_value=_late_promotion()):
            rows = cli_mod._list_releases("rocky9", arch="x86_64")
        assert rows[0]["tag"] == late
    zfs_on_b = _late_promotion()
    zfs_on_b[0]["assets"].append({"name": ZFS})
    builds, _, with_zfs = _plan(zfs_on_b, "eeeeeeeeee")
    # The later one, without ZFS, is what fetch takes and build plans.
    assert (builds, with_zfs) == ([rb.Build("rocky9", K)], [])


def test_unfinished_keyed_releases_are_absent_everywhere(
    tmp_path: Path,
) -> None:
    import ltvm_pkg.target_config as cfg

    unfinished = _rel(rt.keyed_tag(CORE, MINE), identity=IDENT, complete=False)
    releases = [unfinished, _rel(OLD)]
    with _checkout():
        assert f"/{OLD}/" in _find(releases)
        with patch.object(cli_mod, "_gh_api", return_value=releases):
            rows = cli_mod._list_releases("rocky9", arch="x86_64")
        assert [r["tag"] for r in rows] == [OLD]
        with patch.object(cfg, "ARTIFACTS_DIR", tmp_path):
            _, remote, _ = cli_mod._release_status_full(
                "rocky9", "x86_64", releases, kernel=K
            )
        assert remote == KVER
    assert _plan([unfinished], MINE) == ([], [], [])
    assert _plan_prune([unfinished]) == {}


# ------------------------------------------------------------------
# Pruning only from the tip; promote order and lookups; dry runs
# ------------------------------------------------------------------


def test_an_older_default_branch_checkout_publishes_but_does_not_prune(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Its releases are public, but not what the branch builds now:
    pruning by them could delete the release the tip fetches."""
    tag = rt.keyed_tag(CORE, MINE)
    now = datetime.now(timezone.utc)
    rc, api, up, rm = _publish(
        artifacts,
        tmp_path,
        _releases_around(now, tag),
        on_default="ancestor",
        json=False,
    )
    assert rc == EXIT_OK
    assert up.call_args.kwargs["public"] is True
    assert not api.called and not rm.called
    assert "Prune skipped: this checkout is not at the tip" in (
        capsys.readouterr().out
    )


def test_promote_from_an_older_checkout_does_not_prune(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, pub, rm = _promote(on_default="ancestor")
    assert rc == EXIT_OK
    assert pub.called
    assert not rm.called
    out = json.loads(capsys.readouterr().out)
    assert all(p["pruned"] == [] for p in out["promoted"])


def _point_releases(
    older_draft: bool = True, newer_draft: bool = True
) -> tuple[str, str, list[dict[str, Any]]]:
    """A moving kernel built twice, both keyed alike, the newer second;
    gh lists the newer first."""
    older = "rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre"
    newer = "rocky9-x86_64-5.14.0-611.55.1.el9_7_lustre"
    rels = [
        _rel(
            rt.keyed_tag(newer, MINE),
            f"manifest-{newer}.json",
            identity=IDENT,
            draft=newer_draft,
            built_at="2026-11-12T10:00:00Z",
        ),
        _rel(
            rt.keyed_tag(older, MINE),
            f"manifest-{older}.json",
            identity=IDENT,
            draft=older_draft,
            built_at="2026-11-02T10:00:00Z",
        ),
    ]
    rels[0]["created_at"] = "2026-11-12T00:00:00Z"
    rels[1]["created_at"] = "2026-11-02T00:00:00Z"
    return rt.keyed_tag(older, MINE), rt.keyed_tag(newer, MINE), rels


def _promote_with(
    drafts: list[dict[str, Any]], published: list[dict[str, Any]]
) -> tuple[int, Any, dict[str, Any]]:
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with (
        _checkout(),
        patch.object(rt, "checkout_problem", return_value=None),
        patch.object(cli_mod, "_gh_draft_releases", return_value=drafts),
        patch.object(cli_mod, "_gh_release_publish", return_value=None) as pub,
        patch.object(cli_mod, "_gh_api", return_value=published),
        redirect_stdout(buf),
    ):
        rc = cli_mod.cmd_promote(_promote_ns())
    return rc, pub, json.loads(buf.getvalue())


def test_promote_publishes_only_the_newest_build() -> None:
    """Of two drafts of one kernel and key, the newer goes public and
    the older is left superseded: published later, it would be what
    fetch takes."""
    older, newer, drafts = _point_releases()
    rc, pub, out = _promote_with(drafts, [])
    assert rc == EXIT_OK
    assert [c.args[0] for c in pub.call_args_list] == [newer]
    left = {p["tag"]: p["reason"] for p in out["left"]}
    assert left[older] == f"superseded by the newer build {newer}"


def test_a_retry_never_publishes_an_older_build_after_a_newer() -> None:
    """The first run published the newer build and failed on the older;
    on the retry the older is superseded by the published newer one."""
    older, newer, rels = _point_releases(newer_draft=False)
    rels[0]["published_at"] = "2026-11-13T00:00:00Z"
    rc, pub, out = _promote_with([rels[1]], [rels[0]])
    assert rc == EXIT_OK
    assert not pub.called
    left = {p["tag"]: p["reason"] for p in out["left"]}
    assert left[older] == f"superseded by the newer build {newer}"


def test_a_draft_newer_than_its_published_build_is_promoted() -> None:
    older, newer, rels = _point_releases(older_draft=False)
    rels[1]["published_at"] = "2026-11-03T00:00:00Z"
    rc, pub, _ = _promote_with([rels[0]], [rels[1]])
    assert rc == EXIT_OK
    assert [c.args[0] for c in pub.call_args_list] == [newer]


def test_promote_without_the_published_list_promotes_nothing() -> None:
    """Without it, which build is newest cannot be told."""
    _, _, drafts = _point_releases()
    with (
        _checkout(),
        patch.object(rt, "checkout_problem", return_value=None),
        patch.object(cli_mod, "_gh_draft_releases", return_value=drafts),
        patch.object(cli_mod, "_gh_release_publish") as pub,
        patch.object(cli_mod, "_gh_api", side_effect=RuntimeError("offline")),
    ):
        assert cli_mod.cmd_promote(_promote_ns(json=False)) == EXIT_ERROR
    assert not pub.called


def test_the_default_branch_is_asked_again_before_each_prune() -> None:
    """It may move on during a long promote: no prune once this
    checkout is no longer its tip."""
    now = datetime.now(timezone.utc)
    with (
        _checkout(on_default=["tip", "ancestor", "ancestor"]),
        patch.object(rt, "checkout_problem", return_value=None),
        patch.object(cli_mod, "_gh_draft_releases", return_value=_drafts()),
        patch.object(cli_mod, "_gh_release_publish", return_value=None) as pub,
        patch.object(cli_mod, "_gh_api", return_value=_published_around(now)),
        patch.object(cli_mod, "_gh_release_delete") as rm,
    ):
        assert cli_mod.cmd_promote(_promote_ns()) == EXIT_OK
    assert pub.call_count == 2
    assert not rm.called


def test_publish_asks_again_after_the_upload_before_pruning(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The tip when the publish started, an ancestor by the time its
    upload finished: public, but nothing pruned."""
    tag = rt.keyed_tag(CORE, MINE)
    now = datetime.now(timezone.utc)
    rc, api, up, rm = _publish(
        artifacts,
        tmp_path,
        _releases_around(now, tag),
        on_default=["tip", "ancestor"],
        json=False,
    )
    assert rc == EXIT_OK
    assert up.call_args.kwargs["public"] is True
    assert not rm.called


def test_build_generation_is_a_named_value() -> None:
    """A string, replaced on each bump: two branches bumping it
    conflict in git rather than merge to the same value."""
    from ltvm_pkg import release_package as rp

    assert isinstance(rp.BUILD_GENERATION, str)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-[a-z0-9-]+", rp.BUILD_GENERATION)


def test_a_draft_that_cannot_be_read_fails_promote(
    capsys: pytest.CaptureFixture[str],
) -> None:
    unread = {
        "tag_name": rt.keyed_tag(CORE, "f" * 10),
        "draft": True,
        "lookup_failed": True,
    }
    drafts = [unread, *_drafts()]
    with (
        _checkout(),
        patch.object(rt, "checkout_problem", return_value=None),
        patch.object(cli_mod, "_gh_draft_releases", return_value=drafts),
        patch.object(cli_mod, "_gh_release_publish", return_value=None) as pub,
        patch.object(cli_mod, "_gh_api", return_value=[]),
    ):
        assert cli_mod.cmd_promote(_promote_ns()) == EXIT_ERROR
    # The others were still promoted.
    assert pub.call_count == 2
    out = json.loads(capsys.readouterr().out)
    assert out["failed"] is True
    left = {p["tag"]: p["reason"] for p in out["left"]}
    assert "reading it failed" in left[unread["tag_name"]]


def test_dry_run_and_no_upload_together_are_refused(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch.object(cli_mod, "package_target") as pt:
        rc, _, up, _ = _publish(
            artifacts, tmp_path, [], dry_run=True, no_upload=True, json=False
        )
    assert rc == EXIT_ERROR
    assert "--dry-run and --no-upload together" in capsys.readouterr().err
    assert not pt.called and not up.called


def test_a_dry_run_checks_the_container_packaging_would_export(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / MANIFEST
    manifest.write_text("{}")
    with (
        _checkout(),
        patch.object(rt, "checkout_problem", return_value=None),
        patch.object(cli_mod, "_gh_release_info", return_value=_absent()),
        patch.object(cli_mod, "_gh_api", return_value=[]),
        patch(
            "ltvm_pkg.release_package.check_build_container",
            side_effect=RuntimeError("the podman image tagged x is not it"),
        ) as check,
    ):
        rc = cli_mod.cmd_publish(_pub_ns(dry_run=True, json=False))
    assert rc == EXIT_ERROR
    assert "the podman image tagged x is not it" in capsys.readouterr().err
    check.assert_called_once_with(
        "rocky9", artifacts.output_dir, "x86_64", "base"
    )


def test_the_container_check_is_read_only(tmp_path: Path) -> None:
    """check_build_container inspects; only export saves."""
    from ltvm_pkg import release_package as rp

    out = tmp_path / "rocky9" / "x86_64"
    _container_meta(tmp_path, image_tag="ltvm-build-rocky9", image_id="sha-a")
    calls, p = _podman("sha-a")
    with p, patch.object(rp, "_ensure_podman"):
        assert rp.check_build_container("rocky9", out, "x86_64") == "sha-a"
    assert all(c[:2] != ["podman", "save"] for c in calls)
    assert not (out / "container" / "image.tar").exists()


# ------------------------------------------------------------------
# Which build is newest: when its publish finished
# ------------------------------------------------------------------


def test_the_retry_with_equal_commit_dates_keeps_the_newer_build() -> None:
    """created_at is the tag commit's date: builds uploaded between two
    commits share it.  The newer build, already public, wins by when
    its publish finished, and the older draft stays superseded."""
    older, newer, rels = _point_releases(newer_draft=False)
    for r in rels:
        r["created_at"] = "2026-11-02T00:00:00Z"
    rels[0]["published_at"] = "2026-11-13T00:00:00Z"
    rc, pub, out = _promote_with([rels[1]], [rels[0]])
    assert rc == EXIT_OK
    assert not pub.called
    left = {p["tag"]: p["reason"] for p in out["left"]}
    assert left[older] == f"superseded by the newer build {newer}"


@pytest.mark.parametrize(
    "draft_built,published_built,promoted",
    [
        # A tie, or no times at all: the published build wins.
        ("2026-11-05T00:00:00Z", "2026-11-05T00:00:00Z", False),
        (None, None, False),
        # A draft without a time sorts oldest.
        (None, "2026-11-05T00:00:00Z", False),
        ("2026-11-06T00:00:00Z", "2026-11-05T00:00:00Z", True),
        ("2026-11-06T00:00:00Z", None, True),
    ],
)
def test_a_draft_never_wins_a_tie_against_a_published_build(
    draft_built: str | None, published_built: str | None, promoted: bool
) -> None:
    older, newer, rels = _point_releases(older_draft=False)
    for r, built in ((rels[0], draft_built), (rels[1], published_built)):
        r["body"] = _notes(identity=IDENT, built_at=built) + (
            "\n" + rt.COMPLETE_NOTE
        )
    _, pub, _ = _promote_with([rels[0]], [rels[1]])
    assert pub.called is promoted


def test_two_drafts_tied_go_by_tag() -> None:
    a = {"tag_name": "rel-a", "draft": True, "body": ""}
    b = {"tag_name": "rel-b", "draft": True, "body": ""}
    assert max([a, b], key=rt.build_order) is b
    assert max([b, a], key=rt.build_order) is b


def test_the_finish_time_is_written_last_with_the_mark() -> None:
    with _checkout():
        notes = rt.release_notes(
            "s", target="rocky9", arch="x86_64", kernel=K, variant="base"
        )
    now = datetime(2026, 11, 16, 12, 30, 5, tzinfo=timezone.utc)
    done = rt.finished_notes(notes, now)
    assert done.splitlines()[-2:] == [
        f"{rt.BUILT_AT_NOTE} 2026-11-16T12:30:05Z",
        rt.COMPLETE_NOTE,
    ]
    assert rt.Publisher.of({"body": done}).built_at == now
    # Written again, it replaces the earlier time.
    later = rt.finished_notes(done, now + timedelta(hours=1))
    assert later.count(rt.BUILT_AT_NOTE) == 1
    # While the assets change, neither line is there.
    unfinished = rt.incomplete_notes(done)
    assert rt.BUILT_AT_NOTE not in unfinished
    assert rt.COMPLETE_NOTE not in unfinished
    # Notes that never carried the mark (bootable) are left alone.
    assert rt.finished_notes("bootable image", now) == "bootable image"


@pytest.mark.parametrize(
    "value",
    [
        "2026-11-16",
        "2026-11-16T12:30:05",
        "yesterday",
        "2026-11-16T12:30:05+00:00",
    ],
)
def test_a_finish_time_not_in_the_written_form_is_ignored(value: str) -> None:
    body = f"{rt.BUILT_AT_NOTE} {value}\n{rt.COMPLETE_NOTE}"
    assert rt.Publisher.of({"body": body}).built_at is None


def test_the_upload_writes_the_finish_time_in_its_last_edit(
    tmp_path: Path,
) -> None:
    a = tmp_path / "x.tar.zst"
    a.write_bytes(b"x")
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> Any:
        calls.append(cmd)
        return _proc(0)

    with _checkout():
        notes = rt.release_notes(
            "s", target="rocky9", arch="x86_64", kernel=K, variant="base"
        )
    with (
        patch.object(cli_mod, "_gh_release_info", return_value=_absent()),
        patch("ltvm_pkg.cli.subprocess.run", side_effect=run),
    ):
        cli_mod._gh_release_upload("t", [a], notes, True)
    assert rt.Publisher.of({"body": _notes_of(calls[0])}).built_at is None
    last = rt.Publisher.of({"body": _notes_of(calls[-1])})
    assert last.complete and last.built_at is not None


# ------------------------------------------------------------------
# Ninth review
# ------------------------------------------------------------------


def test_build_release_skips_a_named_release_with_two_manifests() -> None:
    """As fetch does: notes name one kernel, and cannot say which of two
    manifests is it, so neither plans a build nor decides ZFS."""
    two = _rel(
        rt.keyed_tag(CORE, MINE),
        MANIFEST,
        "manifest-rocky9-x86_64-5.14.0-503.40.1.el9_7_lustre.json",
        ZFS,
        identity=IDENT,
        published="2026-11-10T00:00:00Z",
    )
    assert _plan([two], MINE) == ([], [two["tag_name"]], [])
    good = _rel(OLD, published="2026-11-01T00:00:00Z")
    builds, unmatched, with_zfs = _plan([two, good], MINE)
    assert builds == [rb.Build("rocky9", K)]
    assert unmatched == [two["tag_name"]]
    assert with_zfs == []
    with _checkout():
        assert f"/{OLD}/" in _find([two, good])


def test_a_failed_prune_listing_fails_promote_after_the_rest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Once public, a release leaves no draft for a rerun to retry the
    prune from: the run must say it failed."""
    with (
        _checkout(),
        patch.object(rt, "checkout_problem", return_value=None),
        patch.object(cli_mod, "_gh_draft_releases", return_value=_drafts()),
        patch.object(cli_mod, "_gh_release_publish", return_value=None) as pub,
        patch.object(
            cli_mod, "_gh_api", side_effect=[[], RuntimeError("down"), []]
        ),
    ):
        rc = cli_mod.cmd_promote(_promote_ns())
    assert rc == EXIT_ERROR
    assert pub.call_count == 2
    out = json.loads(capsys.readouterr().out)
    assert out["failed"] is True
    assert out["promoted"][0]["pruned"][0]["action"] == "list failed"
    assert out["promoted"][1]["pruned"] == []


def test_a_failed_prune_listing_is_a_warning_for_publish(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / MANIFEST
    manifest.write_text("{}")
    with (
        _checkout(),
        patch.object(rt, "checkout_problem", return_value=None),
        patch.object(cli_mod, "_gh_release_info", return_value=_absent()),
        patch.object(cli_mod, "_gh_api", side_effect=RuntimeError("down")),
        patch.object(
            cli_mod, "package_target", return_value={"manifest": manifest}
        ),
        patch.object(cli_mod, "_gh_release_upload", return_value=(None, None)),
        patch.object(cli_mod, "_gh_release_delete") as rm,
    ):
        rc = cli_mod.cmd_publish(_pub_ns())
    assert rc == EXIT_OK
    assert not rm.called
    captured = capsys.readouterr()
    assert "prune skipped, cannot list releases: down" in captured.err


# ------------------------------------------------------------------
# A shared kernel is built in the base variant's container
# ------------------------------------------------------------------

K95 = "5.14-rhel9.5"


def test_base_then_variant_publish_both_pass_on_the_shared_kernel(
    tmp_path: Path,
) -> None:
    """mofed-24 shares rocky9's 5.14-rhel9.5 kernel.  Built under the
    variant (a forced `build all --variant mofed-24`), the kernel still
    records the base builder, so the base and the variant publish both
    accept it, each image against its own container; a kernel built in
    another container is refused by both."""
    import ltvm_pkg.target_config as cfg
    from tests.conftest import _make_image_outputs, _make_kernel_outputs

    with patch.object(cfg, "ARTIFACTS_DIR", tmp_path):
        base = cfg.TargetConfig("rocky9", arch="x86_64")
        mofed = cfg.TargetConfig("rocky9", arch="x86_64", variant="mofed-24")
        base.write_meta("container", image_tag="b", image_id="base-id")
        mofed.write_meta("container", image_tag="m", image_id="mofed-id")
        _make_kernel_outputs(base, K95)
        mofed.write_meta("kernel", kernel=K95, extra_hash=b"lustre")
        kmeta = json.loads(base.meta_path("kernel", K95).read_text())
        assert kmeta["container_input_hash"] == base.input_hash("container")
        assert kmeta["container_image_id"] == "base-id"
        _make_image_outputs(base, K95, variant="base")
        _make_image_outputs(mofed, K95, variant="mofed-24")
        base.write_meta("image", kernel=K95)
        mofed.write_meta("image", kernel=K95)
        imeta = json.loads(
            mofed.meta_path("image", K95, variant="mofed-24").read_text()
        )
        assert imeta["container_input_hash"] == mofed.input_hash("container")
        assert imeta["container_image_id"] == "mofed-id"

        assert cli_mod._publish_input_mismatches(base, K95, "base") == []
        assert cli_mod._publish_input_mismatches(mofed, K95, "mofed-24") == []

        _edit_meta(base.meta_path("kernel", K95), container_input_hash="0" * 16)
        for tc, variant in ((base, "base"), (mofed, "mofed-24")):
            problems = cli_mod._publish_input_mismatches(tc, K95, variant)
            assert problems == [
                "kernel: built in another build container than the base "
                "variant's build container (its inputs differ)"
            ], variant


def test_a_variant_kernel_build_uses_the_base_builder() -> None:
    import ltvm_pkg.target_config as cfg
    from ltvm_pkg import kernel_build as kb

    mofed = cfg.TargetConfig("rocky9", arch="x86_64", variant="mofed-24")
    base = cfg.TargetConfig("rocky9", arch="x86_64")
    for tc in (mofed, base):
        with patch.object(
            kb, "_ensure_container_image", return_value="t"
        ) as ensure:
            assert kb._ensure_kernel_builder(tc) == "t"
        built = ensure.call_args.args[0]
        assert built.variant_name == "base"
        assert (built.name, built.arch) == ("rocky9", "x86_64")


# ------------------------------------------------------------------
# MOFED kmods and ZFS follow the kernel's build generation
# ------------------------------------------------------------------


def _kernel_with_tree(tc: Any, kernel: str) -> dict[str, Any]:
    tree = tc.kernel_output_dir(kernel) / "build-tree" / "include" / "config"
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "kernel.release").write_text("5.14.0-503.40.1.el9_5_lustre\n")
    tc.write_meta("kernel", kernel=kernel)
    return dict(json.loads(tc.meta_path("kernel", kernel).read_text()))


def _set_kernel_generation(tc: Any, kernel: str, value: Any) -> None:
    path = tc.meta_path("kernel", kernel)
    meta = json.loads(path.read_text())
    if value is None:
        meta.pop("build_generation", None)
    else:
        meta["build_generation"] = value
    path.write_text(json.dumps(meta))


def test_mofed_kmods_follow_the_kernels_build_generation(
    tmp_path: Path,
) -> None:
    import ltvm_pkg.target_config as cfg
    from ltvm_pkg import mofed_kmod_build as mk
    from ltvm_pkg.release_package import BUILD_GENERATION

    with patch.object(cfg, "ARTIFACTS_DIR", tmp_path):
        tc = cfg.TargetConfig("rocky9", arch="x86_64", variant="mofed-24")
        _kernel_with_tree(tc, K95)
        marks = mk._kernel_marks(tc, K95)
        assert marks == {"kernel_build_generation": BUILD_GENERATION}
        out = mk.mofed_kmod_dir(tc, K95)
        out.mkdir(parents=True)
        (out / "x.rpm").write_bytes(b"r")
        expected = mk._input_hash(
            "5.14.0-503.40.1.el9_5_lustre",
            mk._mofed_version(tc),
            mk._kernel_input_hash(tc, K95),
        )
        meta = {"input_hash": expected, **marks}
        (out / "meta.json").write_text(json.dumps(meta))
        assert not mk.is_stale(tc, K95)
        # The kernel rebuilt by a new generation: same input_hash.
        _set_kernel_generation(tc, K95, "2099-01-01-next")
        assert mk.is_stale(tc, K95)
        # A kernel without the mark is fitted by any kmods: a kernel and
        # kmods both from before it was recorded stay current.
        _set_kernel_generation(tc, K95, None)
        assert not mk.is_stale(tc, K95)
        (out / "meta.json").write_text(json.dumps({"input_hash": expected}))
        assert not mk.is_stale(tc, K95)
        # Unmarked kmods against a marked kernel are not: the kernel may
        # have been rebuilt for a generation, its input_hash unchanged.
        _set_kernel_generation(tc, K95, "2099-01-01-next")
        assert mk.is_stale(tc, K95)


def test_zfs_follows_the_kernels_build_generation(tmp_path: Path) -> None:
    import ltvm_pkg.target_config as cfg
    from ltvm_pkg import zfs_build as zb

    with patch.object(cfg, "ARTIFACTS_DIR", tmp_path):
        tc = cfg.TargetConfig("rocky9", arch="x86_64")
        _kernel_with_tree(tc, K95)
        ver = "2.3.4"
        out = zb.zfs_dir(tc, K95, ver)
        (out / "src" / "module").mkdir(parents=True)
        (out / "src" / "zfs_config.h").write_text("x")
        (out / "src" / "module" / "Module.symvers").write_text("x")
        mods = out / "staging" / "lib" / "modules" / "k"
        mods.mkdir(parents=True)
        (mods / "zfs.ko").write_bytes(b"k")
        expected = zb._input_hash(
            "5.14.0-503.40.1.el9_5_lustre", ver, zb._kernel_input_hash(tc, K95)
        )
        meta = {"input_hash": expected, **zb._kernel_marks(tc, K95)}
        (out / "meta.json").write_text(json.dumps(meta))
        assert not zb.is_stale(tc, K95, ver)
        _set_kernel_generation(tc, K95, "2099-01-01-next")
        assert zb.is_stale(tc, K95, ver)
        _set_kernel_generation(tc, K95, None)
        assert not zb.is_stale(tc, K95, ver)
        (out / "meta.json").write_text(json.dumps({"input_hash": expected}))
        assert not zb.is_stale(tc, K95, ver)
        _set_kernel_generation(tc, K95, "2099-01-01-next")
        assert zb.is_stale(tc, K95, ver)


def test_a_symvers_digest_is_followed_when_recorded() -> None:
    from ltvm_pkg.target_config import kernel_abi_marks, kernel_abi_mismatch

    kernel = {"build_generation": "g", "module_symvers_sha256": "a"}
    marks = kernel_abi_marks(kernel)
    assert marks == {
        "kernel_build_generation": "g",
        "kernel_module_symvers_sha256": "a",
    }
    assert kernel_abi_mismatch(marks, kernel) is None
    assert kernel_abi_mismatch(marks, {**kernel, "module_symvers_sha256": "b"})
    assert kernel_abi_mismatch(marks, {"build_generation": "g"}) is None


def _zfs_version() -> str:
    """The ZFS rocky9 declares, which the release key covers."""
    from ltvm_pkg.target_config import TargetConfig
    from ltvm_pkg.zfs_build import resolve_zfs_version

    return resolve_zfs_version(TargetConfig("rocky9", arch="x86_64"))


def _zfs_built(tc: Any, version: str, ko: bool = True) -> Path:
    """A ZFS built against the kernel as it is now, as zfs_build leaves
    it: its own is_stale calls it current."""
    from ltvm_pkg import zfs_build as zb

    zdir = zb.zfs_dir(tc, K, version)
    (zdir / "src" / "module").mkdir(parents=True, exist_ok=True)
    (zdir / "src" / "zfs_config.h").write_text("x")
    (zdir / "src" / "module" / "Module.symvers").write_text("x")
    mods = zdir / "staging" / "lib" / "modules" / "k"
    mods.mkdir(parents=True, exist_ok=True)
    if ko:
        (mods / "zfs.ko").write_bytes(b"k")
    meta = {
        "input_hash": zb._input_hash(
            KVER, version, zb._kernel_input_hash(tc, K)
        ),
        **zb._kernel_marks(tc, K),
    }
    (zdir / "meta.json").write_text(json.dumps(meta))
    return zdir


def _mofed_built(tmp_path: Path) -> Path:
    mdir = tmp_path / "mofed"
    mdir.mkdir(exist_ok=True)
    (mdir / "mlnx-ofa_kernel.rpm").write_bytes(b"rpm")
    return mdir


def test_a_release_with_current_lustre_zfs_and_mofed_passes(
    artifacts: Any, tmp_path: Path
) -> None:
    tc = artifacts
    zv = _zfs_version()
    _zfs_built(tc, zv)
    mdir = _mofed_built(tmp_path)
    _bake(tc, zfs=zv, mofed=mdir)
    with (
        patch("ltvm_pkg.mofed_kmod_build.mofed_kmod_dir", return_value=mdir),
        patch("ltvm_pkg.mofed_kmod_build.is_stale", return_value=False),
    ):
        assert cli_mod._publish_input_mismatches(tc, K, "base") == []


def test_publish_refuses_a_zfs_built_against_other_kernel_inputs(
    artifacts: Any,
) -> None:
    """The fourteenth review's first scenario: a kernel config change
    rebuilds the kernel and image, but the ZFS and the Lustre snapshot
    are left from before.  ZFS's own is_stale sees its recorded kernel
    input hash is the old one; so does publish now."""
    tc = artifacts
    zv = _zfs_version()
    _zfs_built(tc, zv)
    snap = _bake(tc, zfs=zv)
    assert cli_mod._publish_input_mismatches(tc, K, "base") == []
    # The kernel rebuilt from other inputs, same generation and ABI
    # mark; the image rebuilt against it and the same snapshot.
    tc.write_meta(
        "kernel", kernel=K, extra_hash=b"other config", kernel_version=KVER
    )
    from ltvm_pkg.image_build import _lustre_staging_hash_input

    tc.write_meta(
        "image",
        kernel=K,
        extra_hash=_lustre_staging_hash_input(snap),
        with_lustre="/lustre",
    )
    problems = cli_mod._publish_input_mismatches(tc, K, "base")
    assert problems == [
        f"ZFS {zv}: not current against this kernel, or incomplete; rebuild "
        f"with `ltvm build zfs rocky9 --kernel {K}`"
    ]


def test_publish_refuses_an_image_baked_with_other_lustre(
    artifacts: Any,
) -> None:
    """The second scenario: the image was built with one Lustre (and
    the ZFS it linked against); the snapshot is now another.  The
    image's own is_stale, given the snapshot to be packaged, sees it."""
    tc = artifacts
    _bake(tc, lustre="old lustre")
    snap = tc.kernel_output_dir(K) / "lustre-artifacts"
    _staged(snap, tc, K, None, lustre="new lustre")
    problems = cli_mod._publish_input_mismatches(tc, K, "base")
    assert problems == [
        "image: not current against its inputs; rebuild with "
        f"`ltvm build all rocky9 --kernel {K}`"
    ]


def test_publish_refuses_lustre_built_against_another_kernel_abi(
    artifacts: Any,
) -> None:
    tc = artifacts
    _bake(tc)
    (tc.kernel_output_dir(K) / "build-tree" / "Module.symvers").write_text(
        "new\n"
    )
    problems = cli_mod._publish_input_mismatches(tc, K, "base")
    assert any(
        p.startswith("Lustre: built against another build") for p in problems
    )


@pytest.mark.parametrize("ko,meta", [(False, True), (True, False)])
def test_publish_refuses_an_incomplete_or_unrecorded_zfs(
    artifacts: Any, ko: bool, meta: bool
) -> None:
    zv = _zfs_version()
    zdir = _zfs_built(artifacts, zv, ko=ko)
    if not meta:
        (zdir / "meta.json").unlink()
    _bake(artifacts, zfs=zv)
    problems = cli_mod._publish_input_mismatches(artifacts, K, "base")
    assert problems == [
        f"ZFS {zv}: not current against this kernel, or incomplete; rebuild "
        f"with `ltvm build zfs rocky9 --kernel {K}`"
    ]


def test_publish_refuses_stale_mofed_kmods(
    artifacts: Any, tmp_path: Path
) -> None:
    mdir = _mofed_built(tmp_path)
    _bake(artifacts, mofed=mdir)
    with (
        patch("ltvm_pkg.mofed_kmod_build.mofed_kmod_dir", return_value=mdir),
        patch("ltvm_pkg.mofed_kmod_build.is_stale", return_value=True),
    ):
        problems = cli_mod._publish_input_mismatches(artifacts, K, "base")
    assert problems[0].startswith(
        "MOFED kmods: not current against this kernel"
    )


def test_publish_refuses_a_zfs_other_than_the_declared_one(
    artifacts: Any,
) -> None:
    """The key covers the ZFS targets.yaml declares; a Lustre built with
    --zfs-version against another is not what that key describes."""
    _zfs_built(artifacts, "9.9.9")
    _bake(artifacts, zfs="9.9.9")
    problems = cli_mod._publish_input_mismatches(artifacts, K, "base")
    assert problems == [
        f"ZFS 9.9.9: the Lustre was built against ZFS 9.9.9, but this "
        f"checkout declares {_zfs_version()}, which the release key covers"
    ]


def test_no_lustre_refuses_an_image_with_lustre_baked_in(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--no-lustre ships no Lustre: an image with Lustre in it would,
    with whatever ZFS that Lustre links against."""
    _bake(artifacts, zfs=_zfs_version())
    problems = cli_mod._publish_input_mismatches(
        artifacts, K, "base", include_lustre=False
    )
    assert len(problems) == 1
    assert problems[0].startswith("image: has Lustre baked in")
    rc, _, up, _ = _publish(artifacts, tmp_path, [], no_lustre=True, json=False)
    assert rc == EXIT_ERROR
    assert "has Lustre baked in" in capsys.readouterr().err
    assert not up.called


def test_no_lustre_takes_an_image_without_lustre_and_checks_no_zfs(
    artifacts: Any, tmp_path: Path
) -> None:
    """A stale ZFS beside a snapshot nothing ships is not looked at; the
    image's MOFED kmods still are."""
    zv = _zfs_version()
    zdir = _zfs_built(artifacts, zv)
    (zdir / "meta.json").unlink()
    _staged(
        artifacts.kernel_output_dir(K) / "lustre-artifacts", artifacts, K, zv
    )
    assert (
        cli_mod._publish_input_mismatches(
            artifacts, K, "base", include_lustre=False
        )
        == []
    )
    mdir = _mofed_built(tmp_path)
    from ltvm_pkg.image_build import mofed_kmods_hash

    artifacts.write_meta(
        "image",
        kernel=K,
        extra_hash=mofed_kmods_hash(mdir),
        mofed_kmods=["x.rpm"],
    )
    with (
        patch("ltvm_pkg.mofed_kmod_build.mofed_kmod_dir", return_value=mdir),
        patch("ltvm_pkg.mofed_kmod_build.is_stale", return_value=True),
    ):
        problems = cli_mod._publish_input_mismatches(
            artifacts, K, "base", include_lustre=False
        )
    assert [p.split(":")[0] for p in problems] == ["MOFED kmods"]


def test_the_declared_zfs_moves_the_key_and_no_input_hash() -> None:
    import copy

    import ltvm_pkg.target_config as cfg

    real = cfg._load_registry()
    tc = cfg.TargetConfig("rocky9", arch="x86_64")
    hashes = [tc.input_hash(a) for a in ("container", "kernel", "image")]
    before = rt.release_key("rocky9", "x86_64", K)
    bumped = copy.deepcopy(real)
    bumped["targets"]["rocky9"]["zfs"] = {"version": "9.9.9"}
    with patch.object(cfg, "_load_registry", return_value=bumped):
        assert rt.release_key("rocky9", "x86_64", K) != before
        tc2 = cfg.TargetConfig("rocky9", arch="x86_64")
        assert [
            tc2.input_hash(a) for a in ("container", "kernel", "image")
        ] == (hashes)


def test_an_unmarked_kernel_fits_unmarked_modules() -> None:
    from ltvm_pkg.target_config import kernel_abi_mismatch

    assert kernel_abi_mismatch({}, {"input_hash": "x"}) is None
    assert kernel_abi_mismatch({"kernel_build_generation": "g"}, {}) is None
    assert kernel_abi_mismatch({}, {"build_generation": "g"})


def _zfs_tree_staging(tree: Path, tc: Any, version: str | None) -> Path:
    from ltvm_pkg.lustre_build import staging_path

    return _staged(
        staging_path(tree, "rocky9", arch="x86_64", kernel=K), tc, K, version
    )


def test_publish_checks_the_lustre_it_will_snapshot(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With --lustre-tree, what is packaged is the snapshot taken from
    that tree, not the one already there: it is checked before anything
    is snapshotted, packaged or uploaded, dry run included."""
    zv = _zfs_version()
    tree = tmp_path / "lustre"
    _zfs_tree_staging(tree, artifacts, zv)
    zdir = _zfs_built(artifacts, zv)
    (zdir / "meta.json").unlink()
    # The snapshot there now, and the image built with it, carry no ZFS.
    _bake(artifacts)
    for dry_run in (True, False):
        with patch.object(cli_mod, "snapshot_lustre") as snapshot:
            rc, _, up, _ = _publish(
                artifacts,
                tmp_path,
                [],
                lustre_tree=str(tree),
                no_lustre=False,
                dry_run=dry_run,
                json=False,
            )
        assert rc == EXIT_ERROR, dry_run
        assert f"ZFS {zv}: not current" in capsys.readouterr().err
        assert not snapshot.called and not up.called


def test_publish_checks_the_snapshot_it_took_before_packaging(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    zv = _zfs_version()
    tree = tmp_path / "lustre"
    _zfs_tree_staging(tree, artifacts, None)
    _bake(artifacts)
    zdir = _zfs_built(artifacts, zv)
    (zdir / "meta.json").unlink()

    def snapshot(*a: Any, **kw: Any) -> None:
        # What lands differs from the staging checked: caught too.
        snap = artifacts.kernel_output_dir(K) / "lustre-artifacts"
        _staged(snap, artifacts, K, zv)
        (snap / ".ltvm-snapshot.json").write_text(
            json.dumps({"zfs_version": zv})
        )

    with patch.object(cli_mod, "snapshot_lustre", side_effect=snapshot):
        rc, _, up, _ = _publish(
            artifacts,
            tmp_path,
            [],
            lustre_tree=str(tree),
            no_lustre=False,
            json=False,
        )
    assert rc == EXIT_ERROR
    assert "the Lustre snapshot just taken" in capsys.readouterr().err
    assert not up.called


# ------------------------------------------------------------------
# Release inputs the commit does not hold
# ------------------------------------------------------------------


def test_an_untracked_input_is_found_against_the_commit(tmp_path: Path) -> None:
    """An optional kernel config fragment, untracked: the tracked-files
    check cannot see it, but the key reads it.  Computing the inputs
    again from HEAD exported shows it."""
    import shutil
    import subprocess

    import ltvm_pkg.target_config as cfg

    repo = tmp_path / "repo"
    ignore = shutil.ignore_patterns("__pycache__", "_build_info.py")
    for d in ("ltvm_pkg", "targets", "shmrdma"):
        shutil.copytree(cfg.REPO_ROOT / d, repo / d, ignore=ignore)

    def git(*args: str) -> None:
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.invalid",
                "-C",
                str(repo),
                *args,
            ],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "x")
    targets = repo / "targets"
    ident = ("rocky9", "x86_64", K, "base")
    with (
        patch.object(cfg, "REPO_ROOT", repo),
        patch.object(cfg, "TARGETS_DIR", targets),
        patch.object(cfg, "TARGETS_YAML", targets / "targets.yaml"),
    ):
        assert _UNCOMMITTED_INPUTS([ident]) == {ident: []}
        frag = targets / "common" / "kernel-config-x86_64.fragment"
        assert not frag.exists()
        frag.write_text("CONFIG_X=y\n")
        assert _UNCOMMITTED_INPUTS([ident]) == {
            ident: [
                "kernel: common/kernel-config-x86_64.fragment (not in the commit)"
            ]
        }
        # A file the hashes do not read is not looked at.
        frag.unlink()
        (repo / "scratch.txt").write_text("x")
        assert _UNCOMMITTED_INPUTS([ident]) == {ident: []}


_FRAGMENT = ["kernel: common/kernel-config-x86_64.fragment (not in the commit)"]


def test_publish_refuses_inputs_its_commit_does_not_hold(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for dry_run in (False, True):
        with patch.object(
            rt,
            "uncommitted_inputs",
            side_effect=lambda ids: {tuple(i): _FRAGMENT for i in ids},
        ):
            rc, _, up, _ = _publish(
                artifacts, tmp_path, [], dry_run=dry_run, json=False
            )
        assert rc == EXIT_ERROR
        err = capsys.readouterr().err
        assert "not all in its commit" in err
        assert "common/kernel-config-x86_64.fragment" in err
        assert not up.called
    with patch.object(rt, "uncommitted_inputs", return_value=None):
        rc, _, up, _ = _publish(artifacts, tmp_path, [], json=False)
    assert rc == EXIT_ERROR
    assert "exporting HEAD failed" in capsys.readouterr().err


def test_promote_refuses_inputs_its_commit_does_not_hold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The finding's case: a maintainer at the default branch tip has,
    untracked, the fragment a pull request adds, and so computes that
    pull request's key."""
    with patch.object(
        rt,
        "uncommitted_inputs",
        side_effect=lambda ids: {tuple(i): _FRAGMENT for i in ids},
    ):
        rc, pub, rm = _promote(json=False)
    assert rc == EXIT_ERROR
    assert "common/kernel-config-x86_64.fragment" in capsys.readouterr().err
    assert not pub.called and not rm.called


def test_a_dry_run_checks_the_lustre_it_would_package(
    artifacts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """As the publish itself would: a snapshot there, or a staging to
    snapshot from the tree given."""
    rc, _, _, _ = _publish(
        artifacts, tmp_path, [], dry_run=True, no_lustre=False, json=False
    )
    assert rc == EXIT_ERROR
    assert "Lustre snapshot missing under" in capsys.readouterr().err
    rc, _, _, _ = _publish(
        artifacts,
        tmp_path,
        [],
        dry_run=True,
        no_lustre=False,
        lustre_tree=str(tmp_path / "empty-tree"),
        json=False,
    )
    assert rc == EXIT_ERROR
    assert "no staging directory at" in capsys.readouterr().err
    _bake(artifacts)
    rc, _, _, _ = _publish(
        artifacts, tmp_path, [], dry_run=True, no_lustre=False, json=False
    )
    assert rc == EXIT_OK


def test_a_split_release_passes_with_its_lustre_checked_on_its_own(
    artifacts: Any,
) -> None:
    """An image built without Lustre, beside a Lustre snapshot: the
    Lustre ships as its own asset, so it is not part of the image's
    inputs (image_build folds it in only when it bakes it in); it is
    still checked against the kernel."""
    tc = artifacts
    snap = _staged(tc.kernel_output_dir(K) / "lustre-artifacts", tc, K, None)
    (snap / ".ltvm-snapshot.json").write_text("{}")
    assert not json.loads(tc.meta_path("image", K).read_text()).get(
        "with_lustre"
    )
    assert cli_mod._publish_input_mismatches(tc, K, "base") == []
    (tc.kernel_output_dir(K) / "build-tree" / "Module.symvers").write_text(
        "new\n"
    )
    problems = cli_mod._publish_input_mismatches(tc, K, "base")
    assert [p.split(":")[0] for p in problems] == ["Lustre"]
    assert "built against another build of this kernel" in problems[0]


def test_a_baked_image_still_needs_the_lustre_it_was_built_with(
    artifacts: Any,
) -> None:
    tc = artifacts
    _bake(tc, lustre="baked")
    assert cli_mod._publish_input_mismatches(tc, K, "base") == []
    _staged(
        tc.kernel_output_dir(K) / "lustre-artifacts",
        tc,
        K,
        None,
        lustre="other",
    )
    problems = cli_mod._publish_input_mismatches(tc, K, "base")
    assert problems == [
        "image: not current against its inputs; rebuild with "
        f"`ltvm build all rocky9 --kernel {K}`"
    ]


def test_a_promote_dry_run_that_cannot_plan_its_prune_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """As the real run does: a script relying on the dry run's status
    must not read an unchecked prune as a checked one."""
    with (
        _checkout(),
        patch.object(rt, "checkout_problem", return_value=None),
        patch.object(cli_mod, "_gh_draft_releases", return_value=_drafts()),
        patch.object(cli_mod, "_gh_release_publish") as pub,
        patch.object(
            cli_mod, "_gh_api", side_effect=[[], RuntimeError("down"), []]
        ),
        patch.object(cli_mod, "_gh_release_delete") as rm,
    ):
        rc = cli_mod.cmd_promote(_promote_ns(dry_run=True))
    assert rc == EXIT_ERROR
    assert not pub.called and not rm.called
    out = json.loads(capsys.readouterr().out)
    assert out["failed"] is True
    assert out["promoted"][0]["action"] == "would promote"
    assert out["promoted"][0]["pruned"][0]["action"] == "list failed"
