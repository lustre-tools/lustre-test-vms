"""Tests for ltvm_pkg/lustre_compat.py parsers."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ltvm_pkg.lustre_compat import (
    ChangeLogEntry,
    TargetIn,
    ValidationResult,
    dry_apply_patches,
    parse_changelog,
    parse_ldiskfs_series,
    parse_ldiskfs_series_file,
    parse_target_in,
    parse_which_patch,
    validate_target,
)
from ltvm_pkg.target_config import LustreMode

FIXTURES = Path(__file__).parent / "fixtures" / "lustre_compat"


def _mktree(root: Path) -> Path:
    (root / "lustre/kernel_patches/targets").mkdir(parents=True)
    return root


class TestFixtureTree:
    """Smoke tests against the checked-in fixture tree."""

    def test_which_patch(self) -> None:
        got = parse_which_patch(FIXTURES / "good_tree")
        assert got["5.14-rhel9.7.series"] == "5.14.0-611.13.1.el9"
        assert got["6.12-rhel10.0.series"] == "6.12.0-55.43.1.el10"

    def test_changelog(self) -> None:
        entry = parse_changelog(FIXTURES / "good_tree")
        assert "5.14.0-611.13.1.el9" in entry.server_primary
        assert "5.4.0" in entry.server_best_effort
        assert "6.12.0-55.43.1.el10" in entry.client_primary

    def test_target_in(self) -> None:
        ti = parse_target_in(FIXTURES / "good_tree", "5.14-rhel9.7")
        assert ti.KERNEL_SRPM == "kernel-5.14.0-611.13.1.el9_7.src.rpm"
        fc = parse_target_in(FIXTURES / "good_tree", "3.x-fc18")
        assert fc.SERIES == "3.x-fc18.series"


# ------------------------------------------------------------------
# parse_which_patch
# ------------------------------------------------------------------


WHICH_PATCH_SAMPLE = """\
Note that Lustre server kernels do not REQUIRE patches.

PATCH SERIES FOR SERVER KERNELS:
4.18-rhel8.10.series    4.18.0-553.89.1.el8  (RHEL 8.10)
5.14-rhel9.7.series     5.14.0-611.13.1.el9  (RHEL 9.7)
6.12-rhel10.0.series    6.12.0-55.43.1.el10  (RHEL 10.0)

See lustre/ChangeLog for supported client kernel versions.
"""


class TestParseWhichPatch:
    def test_well_formed(self, tmp_path: Path) -> None:
        tree = _mktree(tmp_path)
        (tree / "lustre/kernel_patches/which_patch").write_text(
            WHICH_PATCH_SAMPLE
        )
        got = parse_which_patch(tree)
        assert got == {
            "4.18-rhel8.10.series": "4.18.0-553.89.1.el8",
            "5.14-rhel9.7.series": "5.14.0-611.13.1.el9",
            "6.12-rhel10.0.series": "6.12.0-55.43.1.el10",
        }

    def test_missing_file(self, tmp_path: Path) -> None:
        tree = _mktree(tmp_path)
        with pytest.raises(FileNotFoundError, match="which_patch"):
            parse_which_patch(tree)

    def test_missing_header(self, tmp_path: Path) -> None:
        tree = _mktree(tmp_path)
        (tree / "lustre/kernel_patches/which_patch").write_text(
            "no table here\n"
        )
        with pytest.raises(ValueError, match="patch series table"):
            parse_which_patch(tree)


# ------------------------------------------------------------------
# parse_changelog
# ------------------------------------------------------------------


CHANGELOG_SAMPLE = """\
TBD Whamcloud
\t* version 2.18.0
\t* See https://wiki.whamcloud.com/ for support matrix.
\t* Server primary kernels built and tested during release cycle:
\t  5.14.0-611.13.1.el9  (RHEL9.7)
\t  4.18.0-553.89.1.el8  (RHEL8.10)
\t* Other server kernels known to build and work at some point (others may also work):
\t  4.18.0-425.10.1.el8  (RHEL8.7)
\t  5.14.21-150500.55.65 (SLES15 SP5)
\t  vanilla linux 5.4.0  (ZFS + ldiskfs)
\t* ldiskfs needs an ldiskfs patch series for that kernel
\t* Client primary kernels built and tested during release cycle:
\t  6.12.0-55.43.1.el10  (RHEL10.0)
\t  5.14.0-611.13.1.el9  (RHEL9.7)
\t* Other clients known to build on these kernels at some point (others may also work):
\t  4.18.0-348.23.1.el8  (RHEL8.5)
\t  5.3.18-24.96         (SLES15 SP2)

2025-01-01 Whamcloud
\t* version 2.17.0
"""


class TestParseChangelog:
    def test_well_formed(self, tmp_path: Path) -> None:
        tree = tmp_path
        (tree / "lustre").mkdir()
        (tree / "lustre/ChangeLog").write_text(CHANGELOG_SAMPLE)
        entry = parse_changelog(tree)
        assert isinstance(entry, ChangeLogEntry)
        assert entry.server_primary == [
            "5.14.0-611.13.1.el9",
            "4.18.0-553.89.1.el8",
        ]
        assert entry.server_best_effort == [
            "4.18.0-425.10.1.el8",
            "5.14.21-150500.55.65",
            "5.4.0",
        ]
        assert entry.client_primary == [
            "6.12.0-55.43.1.el10",
            "5.14.0-611.13.1.el9",
        ]
        assert entry.client_best_effort == [
            "4.18.0-348.23.1.el8",
            "5.3.18-24.96",
        ]

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="ChangeLog"):
            parse_changelog(tmp_path)

    def test_malformed_no_headers(self, tmp_path: Path) -> None:
        (tmp_path / "lustre").mkdir()
        (tmp_path / "lustre/ChangeLog").write_text(
            "TBD Whamcloud\n\t* version 2.18.0\n\t* some unrelated note\n"
        )
        with pytest.raises(ValueError, match="no recognized"):
            parse_changelog(tmp_path)

    def test_only_top_entry_consumed(self, tmp_path: Path) -> None:
        """Kernels below the first release header must not leak in."""
        (tmp_path / "lustre").mkdir()
        (tmp_path / "lustre/ChangeLog").write_text(
            textwrap.dedent("""\
                TBD Whamcloud
                \t* version 2.18.0
                \t* Server primary kernels built and tested during release cycle:
                \t  5.14.0-611.13.1.el9  (RHEL9.7)

                2024-01-01 Whamcloud
                \t* version 2.17.0
                \t* Server primary kernels built and tested during release cycle:
                \t  9.9.9-old.el9  (should not appear)
                """)
        )
        entry = parse_changelog(tmp_path)
        assert entry.server_primary == ["5.14.0-611.13.1.el9"]

    def test_frozen(self, tmp_path: Path) -> None:
        (tmp_path / "lustre").mkdir()
        (tmp_path / "lustre/ChangeLog").write_text(CHANGELOG_SAMPLE)
        entry = parse_changelog(tmp_path)
        with pytest.raises(Exception):
            entry.server_primary = []  # type: ignore[misc]


# ------------------------------------------------------------------
# parse_target_in
# ------------------------------------------------------------------


TARGET_IN_RHEL97 = """\
lnxmaj="5.14.0"
lnxrel="611.13.1.el9_7"

KERNEL_SRPM=kernel-${lnxmaj}-${lnxrel}.src.rpm
SERIES=5.14-rhel9.7.series
EXTRA_VERSION=${lnxrel}_lustre.@VERSION@
LUSTRE_VERSION=@VERSION@
"""

TARGET_IN_NO_LUSTRE_VER = """\
lnxmaj=3.6.10
lnxrel=4.fc18

KERNEL_SRPM=kernel-${lnxmaj}-${lnxrel}.src.rpm
SERIES=""
"""


class TestParseTargetIn:
    def test_resolves_expansions(self, tmp_path: Path) -> None:
        tree = _mktree(tmp_path)
        (
            tree / "lustre/kernel_patches/targets/5.14-rhel9.7.target.in"
        ).write_text(TARGET_IN_RHEL97)
        ti = parse_target_in(tree, "5.14-rhel9.7")
        assert isinstance(ti, TargetIn)
        assert ti.lnxmaj == "5.14.0"
        assert ti.lnxrel == "611.13.1.el9_7"
        assert ti.KERNEL_SRPM == "kernel-5.14.0-611.13.1.el9_7.src.rpm"
        assert ti.SERIES == "5.14-rhel9.7.series"

    def test_empty_series_defaults(self, tmp_path: Path) -> None:
        tree = _mktree(tmp_path)
        (tree / "lustre/kernel_patches/targets/3.x-fc18.target.in").write_text(
            TARGET_IN_NO_LUSTRE_VER
        )
        ti = parse_target_in(tree, "3.x-fc18")
        assert ti.SERIES == "3.x-fc18.series"
        assert ti.KERNEL_SRPM == "kernel-3.6.10-4.fc18.src.rpm"

    def test_fallback_to_plain_target(self, tmp_path: Path) -> None:
        tree = _mktree(tmp_path)
        (tree / "lustre/kernel_patches/targets/plain.target").write_text(
            "lnxmaj=5.14.0\nlnxrel=100.el9\nSERIES=plain.series\n"
            "KERNEL_SRPM=kernel-5.14.0-100.el9.src.rpm\n"
        )
        ti = parse_target_in(tree, "plain")
        assert ti.lnxmaj == "5.14.0"
        assert ti.SERIES == "plain.series"

    def test_missing_file(self, tmp_path: Path) -> None:
        tree = _mktree(tmp_path)
        with pytest.raises(FileNotFoundError, match="target file not found"):
            parse_target_in(tree, "nope")

    def test_missing_lnxmaj(self, tmp_path: Path) -> None:
        tree = _mktree(tmp_path)
        (tree / "lustre/kernel_patches/targets/bad.target.in").write_text(
            "SERIES=bad.series\n"
        )
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_target_in(tree, "bad")

    def test_frozen(self, tmp_path: Path) -> None:
        tree = _mktree(tmp_path)
        (
            tree / "lustre/kernel_patches/targets/5.14-rhel9.7.target.in"
        ).write_text(TARGET_IN_RHEL97)
        ti = parse_target_in(tree, "5.14-rhel9.7")
        with pytest.raises(Exception):
            ti.lnxmaj = "x"  # type: ignore[misc]


# ------------------------------------------------------------------
# parse_ldiskfs_series
# ------------------------------------------------------------------


class TestParseLdiskfsSeries:
    def test_empty_when_dir_absent(self, tmp_path: Path) -> None:
        assert parse_ldiskfs_series(tmp_path) == set()

    def test_reads_series_stems(self, tmp_path: Path) -> None:
        d = tmp_path / "ldiskfs/kernel_patches/series"
        d.mkdir(parents=True)
        (d / "ldiskfs-6.8.0-90-ubuntu24.series").write_text("")
        (d / "ldiskfs-5.14.0-427.13.1.el9.series").write_text("")
        (d / "README").write_text("ignored")
        got = parse_ldiskfs_series(tmp_path)
        assert got == {
            "ldiskfs-6.8.0-90-ubuntu24",
            "ldiskfs-5.14.0-427.13.1.el9",
        }


# ------------------------------------------------------------------
# validate_target
# ------------------------------------------------------------------


class _FakeTC:
    """Minimal stand-in for TargetConfig -- validate_target only reads
    .default_kernel and .lustre_mode."""

    def __init__(
        self,
        lustre_target: str,
        lustre_mode: LustreMode,
        kernel_deb_source: str = "",
    ) -> None:
        self.default_kernel = lustre_target
        self.lustre_mode = lustre_mode
        self.kernel_deb_source = kernel_deb_source


def _make_tree(
    tmp_path: Path,
    *,
    which_patch: str | None,
    changelog: str | None,
    target_ins: dict[str, str],
    ldiskfs_series: list[str] | None = None,
) -> Path:
    tree = _mktree(tmp_path)
    (tree / "lustre").mkdir(exist_ok=True)
    if which_patch is not None:
        (tree / "lustre/kernel_patches/which_patch").write_text(which_patch)
    if changelog is not None:
        (tree / "lustre/ChangeLog").write_text(changelog)
    for name, body in target_ins.items():
        (tree / f"lustre/kernel_patches/targets/{name}.target.in").write_text(
            body
        )
    if ldiskfs_series:
        ld = tree / "ldiskfs/kernel_patches/series"
        ld.mkdir(parents=True, exist_ok=True)
        for stem in ldiskfs_series:
            (ld / f"{stem}.series").write_text("")
    return tree


_TI_RHEL97 = (
    'lnxmaj="5.14.0"\n'
    'lnxrel="611.13.1.el9_7"\n'
    "KERNEL_SRPM=kernel-${lnxmaj}-${lnxrel}.src.rpm\n"
    "SERIES=5.14-rhel9.7.series\n"
)

_TI_UBUNTU2404 = (
    'lnxmaj="6.8.0"\n'
    'lnxrel="90"\n'
    "KERNEL_SRPM=kernel-${lnxmaj}-${lnxrel}.src.rpm\n"
    "SERIES=6.8-ubuntu2404.series\n"
)

_TI_RHEL85 = (
    'lnxmaj="4.18.0"\n'
    'lnxrel="348.23.1.el8"\n'
    "KERNEL_SRPM=kernel-${lnxmaj}-${lnxrel}.src.rpm\n"
    "SERIES=4.18-rhel8.5.series\n"
)

_TI_RHEL97_MISMATCH = (
    'lnxmaj="5.14.0"\n'
    'lnxrel="999.99.9.el9_7"\n'
    "KERNEL_SRPM=kernel-${lnxmaj}-${lnxrel}.src.rpm\n"
    "SERIES=5.14-rhel9.7.series\n"
)

_WP_BASIC = (
    "PATCH SERIES FOR SERVER KERNELS:\n"
    "5.14-rhel9.7.series    5.14.0-611.13.1.el9  (RHEL 9.7)\n"
    "\n"
)

_WP_ABSENT = (
    "PATCH SERIES FOR SERVER KERNELS:\n"
    "4.18-rhel8.10.series    4.18.0-553.89.1.el8  (RHEL 8.10)\n"
    "\n"
)

_CL_PRIMARY = (
    "TBD Whamcloud\n"
    "\t* version 2.18.0\n"
    "\t* Server primary kernels built and tested during release cycle:\n"
    "\t  5.14.0-611.13.1.el9  (RHEL9.7)\n"
    "\t* Other server kernels known to build and work at some point:\n"
    "\t  vanilla linux 5.4.0  (ZFS + ldiskfs)\n"
    "\t* Client primary kernels built and tested during release cycle:\n"
    "\t  5.14.0-611.13.1.el9  (RHEL9.7)\n"
    "\t* Other clients known to build on these kernels at some point:\n"
    "\t  4.18.0-348.23.1.el8  (RHEL8.5)\n"
)

_CL_BEST_EFFORT_ONLY = (
    "TBD Whamcloud\n"
    "\t* version 2.18.0\n"
    "\t* Server primary kernels built and tested during release cycle:\n"
    "\t  4.18.0-553.89.1.el8  (RHEL8.10)\n"
    "\t* Other server kernels known to build and work at some point:\n"
    "\t  5.14.0-611.13.1.el9  (RHEL9.7)\n"
    "\t* Client primary kernels built and tested during release cycle:\n"
    "\t  4.18.0-553.89.1.el8  (RHEL8.10)\n"
    "\t* Other clients known to build on these kernels at some point:\n"
    "\t  4.18.0-348.23.1.el8  (RHEL8.5)\n"
)

_CL_ABSENT = (
    "TBD Whamcloud\n"
    "\t* version 2.18.0\n"
    "\t* Server primary kernels built and tested during release cycle:\n"
    "\t  4.18.0-553.89.1.el8  (RHEL8.10)\n"
    "\t* Other server kernels known to build and work at some point:\n"
    "\t  4.18.0-425.10.1.el8  (RHEL8.7)\n"
    "\t* Client primary kernels built and tested during release cycle:\n"
    "\t  4.18.0-553.89.1.el8  (RHEL8.10)\n"
    "\t* Other clients known to build on these kernels at some point:\n"
    "\t  4.18.0-348.23.1.el8  (RHEL8.5)\n"
)


class TestValidateTarget:
    def test_ldiskfs_primary_match(self, tmp_path: Path) -> None:
        tree = _make_tree(
            tmp_path,
            which_patch=_WP_BASIC,
            changelog=_CL_PRIMARY,
            target_ins={"5.14-rhel9.7": _TI_RHEL97},
        )
        tc = _FakeTC("5.14-rhel9.7", LustreMode.SERVER_LDISKFS)
        r = validate_target(tc, tree)
        assert r.status == "ok"
        assert r.matched_in == "which_patch_primary"
        assert r.mode == LustreMode.SERVER_LDISKFS
        assert r.kernel_version == "5.14.0-611.13.1.el9_7"
        assert r.message

    def test_ldiskfs_series_listed_kver_mismatch(self, tmp_path: Path) -> None:
        tree = _make_tree(
            tmp_path,
            which_patch=_WP_BASIC,
            changelog=_CL_PRIMARY,
            target_ins={"5.14-rhel9.7": _TI_RHEL97_MISMATCH},
        )
        tc = _FakeTC("5.14-rhel9.7", LustreMode.SERVER_LDISKFS)
        r = validate_target(tc, tree)
        assert r.status == "refuse"
        assert r.matched_in == "not_listed"
        assert "mismatch" in r.message

    def test_ldiskfs_series_absent(self, tmp_path: Path) -> None:
        tree = _make_tree(
            tmp_path,
            which_patch=_WP_ABSENT,
            changelog=_CL_PRIMARY,
            target_ins={"5.14-rhel9.7": _TI_RHEL97},
        )
        tc = _FakeTC("5.14-rhel9.7", LustreMode.SERVER_LDISKFS)
        r = validate_target(tc, tree)
        assert r.status == "refuse"
        assert r.matched_in == "not_listed"
        assert "not listed" in r.message

    def test_zfs_primary_match(self, tmp_path: Path) -> None:
        tree = _make_tree(
            tmp_path,
            which_patch=_WP_BASIC,
            changelog=_CL_PRIMARY,
            target_ins={"5.14-rhel9.7": _TI_RHEL97},
        )
        tc = _FakeTC("5.14-rhel9.7", LustreMode.SERVER_ZFS)
        r = validate_target(tc, tree)
        assert r.status == "ok"
        assert r.matched_in == "changelog_primary"

    def test_zfs_best_effort(self, tmp_path: Path) -> None:
        tree = _make_tree(
            tmp_path,
            which_patch=_WP_BASIC,
            changelog=_CL_BEST_EFFORT_ONLY,
            target_ins={"5.14-rhel9.7": _TI_RHEL97},
        )
        tc = _FakeTC("5.14-rhel9.7", LustreMode.SERVER_ZFS)
        r = validate_target(tc, tree)
        assert r.status == "best_effort"
        assert r.matched_in == "changelog_best_effort"

    def test_zfs_absent(self, tmp_path: Path) -> None:
        tree = _make_tree(
            tmp_path,
            which_patch=_WP_BASIC,
            changelog=_CL_ABSENT,
            target_ins={"5.14-rhel9.7": _TI_RHEL97},
        )
        tc = _FakeTC("5.14-rhel9.7", LustreMode.SERVER_ZFS)
        r = validate_target(tc, tree)
        assert r.status == "refuse"
        assert r.matched_in == "not_listed"

    def test_error_missing_target_in(self, tmp_path: Path) -> None:
        tree = _make_tree(
            tmp_path,
            which_patch=_WP_BASIC,
            changelog=_CL_PRIMARY,
            target_ins={},
        )
        tc = _FakeTC("5.14-rhel9.7", LustreMode.SERVER_LDISKFS)
        r = validate_target(tc, tree)
        assert r.status == "error"
        assert r.kernel_version is None
        assert "target.in" in r.message

    def test_error_malformed_changelog(self, tmp_path: Path) -> None:
        tree = _make_tree(
            tmp_path,
            which_patch=_WP_BASIC,
            changelog="TBD Whamcloud\n\t* version 2.18.0\n",
            target_ins={"5.14-rhel9.7": _TI_RHEL97},
        )
        tc = _FakeTC("5.14-rhel9.7", LustreMode.SERVER_ZFS)
        r = validate_target(tc, tree)
        assert r.status == "error"
        assert "ChangeLog" in r.message
        assert r.matched_in is None

    def test_error_missing_which_patch(self, tmp_path: Path) -> None:
        tree = _make_tree(
            tmp_path,
            which_patch=None,
            changelog=_CL_PRIMARY,
            target_ins={"5.14-rhel9.7": _TI_RHEL97},
        )
        tc = _FakeTC("5.14-rhel9.7", LustreMode.SERVER_LDISKFS)
        r = validate_target(tc, tree)
        assert r.status == "error"
        assert "which_patch" in r.message

    def test_ldiskfs_series_fallback_ubuntu(self, tmp_path: Path) -> None:
        """ubuntu server_ldiskfs: not in which_patch but a matching
        ldiskfs-<major>.<minor>.* series file exists -> ok."""
        tree = _make_tree(
            tmp_path,
            which_patch=_WP_ABSENT,
            changelog=_CL_PRIMARY,
            target_ins={"6.8-ubuntu2404": _TI_UBUNTU2404},
            ldiskfs_series=["ldiskfs-6.8.0-90-ubuntu24"],
        )
        tc = _FakeTC("6.8-ubuntu2404", LustreMode.SERVER_LDISKFS)
        r = validate_target(tc, tree)
        assert r.status == "ok"
        assert r.matched_in == "ldiskfs_series"
        assert "ldiskfs-6.8.0-90-ubuntu24" in r.message

    def test_ldiskfs_series_no_match(self, tmp_path: Path) -> None:
        """ubuntu server_ldiskfs: no ldiskfs series file matches the
        kernel -> refuse."""
        tree = _make_tree(
            tmp_path,
            which_patch=_WP_ABSENT,
            changelog=_CL_PRIMARY,
            target_ins={"6.8-ubuntu2404": _TI_UBUNTU2404},
            ldiskfs_series=["ldiskfs-5.14.0-427.13.1.el9"],
        )
        tc = _FakeTC("6.8-ubuntu2404", LustreMode.SERVER_LDISKFS)
        r = validate_target(tc, tree)
        assert r.status == "refuse"
        assert r.matched_in == "not_listed"

    def test_client_primary_match(self, tmp_path: Path) -> None:
        tree = _make_tree(
            tmp_path,
            which_patch=_WP_BASIC,
            changelog=_CL_PRIMARY,
            target_ins={"5.14-rhel9.7": _TI_RHEL97},
        )
        tc = _FakeTC("5.14-rhel9.7", LustreMode.CLIENT)
        r = validate_target(tc, tree)
        assert r.status == "ok"
        assert r.matched_in == "changelog_client_primary"

    def test_client_best_effort_match(self, tmp_path: Path) -> None:
        tree = _make_tree(
            tmp_path,
            which_patch=_WP_BASIC,
            changelog=_CL_PRIMARY,
            target_ins={"4.18-rhel8.5": _TI_RHEL85},
        )
        tc = _FakeTC("4.18-rhel8.5", LustreMode.CLIENT)
        r = validate_target(tc, tree)
        assert r.status == "best_effort"
        assert r.matched_in == "changelog_client_best_effort"

    def test_client_absent(self, tmp_path: Path) -> None:
        tree = _make_tree(
            tmp_path,
            which_patch=_WP_BASIC,
            changelog=_CL_ABSENT,
            target_ins={"5.14-rhel9.7": _TI_RHEL97},
        )
        tc = _FakeTC("5.14-rhel9.7", LustreMode.CLIENT)
        r = validate_target(tc, tree)
        assert r.status == "refuse"
        assert r.matched_in == "not_listed"

    def test_result_is_frozen_and_fully_populated(self, tmp_path: Path) -> None:
        tree = _make_tree(
            tmp_path,
            which_patch=_WP_BASIC,
            changelog=_CL_PRIMARY,
            target_ins={"5.14-rhel9.7": _TI_RHEL97},
        )
        tc = _FakeTC("5.14-rhel9.7", LustreMode.SERVER_LDISKFS)
        r = validate_target(tc, tree)
        assert isinstance(r, ValidationResult)
        assert r.status == "ok"
        assert r.matched_in == "which_patch_primary"
        assert r.mode == LustreMode.SERVER_LDISKFS
        assert r.kernel_version == "5.14.0-611.13.1.el9_7"
        assert r.message
        with pytest.raises(Exception):
            r.status = "refuse"  # type: ignore[misc]


# ------------------------------------------------------------------
# parse_ldiskfs_series_file
# ------------------------------------------------------------------

_SERIES_WITH_COMMENTS = """\
# This is a header comment

linux-5.16/ext4-inode-version.patch
# another comment
linux-6.10/ext4-prealloc.patch

base/ext4-htree-lock.patch
"""


def _make_ldiskfs_tree(
    tmp_path: Path, series_stem: str, series_content: str, patches: dict[str, str]
) -> Path:
    """Build a minimal ldiskfs directory with a series file and patch files."""
    series_dir = tmp_path / "ldiskfs" / "kernel_patches" / "series"
    series_dir.mkdir(parents=True)
    (series_dir / f"{series_stem}.series").write_text(series_content)
    patches_root = tmp_path / "ldiskfs" / "kernel_patches" / "patches"
    for rel_path, content in patches.items():
        p = patches_root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


_CLEAN_PATCH = """\
--- a/fs/ext4/sysfs.c
+++ b/fs/ext4/sysfs.c
@@ -1,4 +1,4 @@
 /* sysfs.c -- ext4 sysfs interface */
-static int ext4_attr_match = 1;
+static int ext4_attr_match = 42;
 static int ext4_attr_other = 2;
 static int ext4_attr_third = 3;
"""

_FAILING_PATCH = """\
--- a/fs/ext4/sysfs.c
+++ b/fs/ext4/sysfs.c
@@ -100,4 +100,4 @@
 /* line that does not exist */
-this line is not present;
+replaced line;
 more context;
 final context;
"""

_SYSFS_C_CONTENT = """\
/* sysfs.c -- ext4 sysfs interface */
static int ext4_attr_match = 1;
static int ext4_attr_other = 2;
static int ext4_attr_third = 3;
"""


def _make_kernel_src(tmp_path: Path) -> Path:
    """Return a minimal kernel source tree with fs/ext4/sysfs.c."""
    src = tmp_path / "kernel_src"
    ext4 = src / "fs" / "ext4"
    ext4.mkdir(parents=True)
    (ext4 / "sysfs.c").write_text(_SYSFS_C_CONTENT)
    return src


class TestParseLdiskfsSeriesFile:
    def test_parses_comments_and_blanks(self, tmp_path: Path) -> None:
        stem = "ldiskfs-6.8.0-90-ubuntu24"
        _make_ldiskfs_tree(
            tmp_path,
            stem,
            _SERIES_WITH_COMMENTS,
            {
                "linux-5.16/ext4-inode-version.patch": "",
                "linux-6.10/ext4-prealloc.patch": "",
                "base/ext4-htree-lock.patch": "",
            },
        )
        result = parse_ldiskfs_series_file(tmp_path, stem)
        assert len(result) == 3
        patches_root = tmp_path / "ldiskfs" / "kernel_patches" / "patches"
        assert result[0] == patches_root / "linux-5.16" / "ext4-inode-version.patch"
        assert result[1] == patches_root / "linux-6.10" / "ext4-prealloc.patch"
        assert result[2] == patches_root / "base" / "ext4-htree-lock.patch"

    def test_empty_series(self, tmp_path: Path) -> None:
        stem = "ldiskfs-5.14-rhel9.7"
        _make_ldiskfs_tree(tmp_path, stem, "# only a comment\n\n", {})
        result = parse_ldiskfs_series_file(tmp_path, stem)
        assert result == []


# ------------------------------------------------------------------
# dry_apply_patches
# ------------------------------------------------------------------


class TestDryApplyPatches:
    def test_clean_patch_returns_true(self, tmp_path: Path) -> None:
        kernel_src = _make_kernel_src(tmp_path)
        patch_file = tmp_path / "clean.patch"
        patch_file.write_text(_CLEAN_PATCH)
        ok, failures = dry_apply_patches([patch_file], kernel_src)
        assert ok is True
        assert failures == []

    def test_failing_patch_returns_false(self, tmp_path: Path) -> None:
        kernel_src = _make_kernel_src(tmp_path)
        patch_file = tmp_path / "bad.patch"
        patch_file.write_text(_FAILING_PATCH)
        ok, failures = dry_apply_patches([patch_file], kernel_src)
        assert ok is False
        assert len(failures) == 1
        assert "bad.patch" in failures[0]

    def test_mixed_patches(self, tmp_path: Path) -> None:
        kernel_src = _make_kernel_src(tmp_path)
        clean = tmp_path / "clean.patch"
        clean.write_text(_CLEAN_PATCH)
        bad = tmp_path / "bad.patch"
        bad.write_text(_FAILING_PATCH)
        ok, failures = dry_apply_patches([clean, bad], kernel_src)
        assert ok is False
        assert len(failures) == 1
        assert "bad.patch" in failures[0]

    def test_empty_list(self, tmp_path: Path) -> None:
        kernel_src = _make_kernel_src(tmp_path)
        ok, failures = dry_apply_patches([], kernel_src)
        assert ok is True
        assert failures == []


# ------------------------------------------------------------------
# validate_target -- ldiskfs_series with kernel_build_tree
# ------------------------------------------------------------------


def _make_ldiskfs_validate_tree(
    tmp_path: Path,
    series_stem: str,
    patch_content: str,
) -> Path:
    """Build a minimal Lustre+ldiskfs tree for ldiskfs_series validation."""
    tree = _mktree(tmp_path)
    (tree / "lustre").mkdir(exist_ok=True)
    (tree / "lustre/kernel_patches/which_patch").write_text(_WP_ABSENT)
    target_body = (
        'lnxmaj="6.8.0"\n'
        'lnxrel="90"\n'
        "KERNEL_SRPM=kernel-${lnxmaj}-${lnxrel}.src.rpm\n"
        "SERIES=6.8-ubuntu2404.series\n"
    )
    (tree / "lustre/kernel_patches/targets/6.8-ubuntu2404.target.in").write_text(
        target_body
    )
    series_dir = tree / "ldiskfs" / "kernel_patches" / "series"
    series_dir.mkdir(parents=True)
    (series_dir / f"{series_stem}.series").write_text(
        "subdir/test.patch\n"
    )
    patches_dir = tree / "ldiskfs" / "kernel_patches" / "patches" / "subdir"
    patches_dir.mkdir(parents=True)
    (patches_dir / "test.patch").write_text(patch_content)
    return tree


class TestValidateTargetLdiskfsBuildTree:
    def test_no_build_tree_ok_with_note(self, tmp_path: Path) -> None:
        tree = _make_ldiskfs_validate_tree(
            tmp_path, "ldiskfs-6.8.0-90-ubuntu24", _CLEAN_PATCH
        )
        tc = _FakeTC("6.8-ubuntu2404", LustreMode.SERVER_LDISKFS)
        r = validate_target(tc, tree, kernel_build_tree=None)
        assert r.status == "ok"
        assert r.matched_in == "ldiskfs_series"
        assert "build-kernel" in r.message

    def test_build_tree_clean_patches_ok(self, tmp_path: Path) -> None:
        tree = _make_ldiskfs_validate_tree(
            tmp_path, "ldiskfs-6.8.0-90-ubuntu24", _CLEAN_PATCH
        )
        kernel_src = _make_kernel_src(tmp_path)
        tc = _FakeTC("6.8-ubuntu2404", LustreMode.SERVER_LDISKFS)
        r = validate_target(tc, tree, kernel_build_tree=kernel_src)
        assert r.status == "ok"
        assert r.matched_in == "ldiskfs_series"
        assert "all 1 patches dry-applied cleanly" in r.message

    def test_build_tree_failing_patches_refuse(self, tmp_path: Path) -> None:
        tree = _make_ldiskfs_validate_tree(
            tmp_path, "ldiskfs-6.8.0-90-ubuntu24", _FAILING_PATCH
        )
        kernel_src = _make_kernel_src(tmp_path)
        tc = _FakeTC("6.8-ubuntu2404", LustreMode.SERVER_LDISKFS)
        r = validate_target(tc, tree, kernel_build_tree=kernel_src)
        assert r.status == "refuse"
        assert r.matched_in == "not_listed"
        assert "test.patch" in r.message

    def test_build_tree_dir_absent_ok_with_note(self, tmp_path: Path) -> None:
        tree = _make_ldiskfs_validate_tree(
            tmp_path, "ldiskfs-6.8.0-90-ubuntu24", _CLEAN_PATCH
        )
        nonexistent = tmp_path / "no-such-build-tree"
        tc = _FakeTC("6.8-ubuntu2404", LustreMode.SERVER_LDISKFS)
        r = validate_target(tc, tree, kernel_build_tree=nonexistent)
        assert r.status == "ok"
        assert r.matched_in == "ldiskfs_series"
        assert "build-tree incomplete" in r.message
