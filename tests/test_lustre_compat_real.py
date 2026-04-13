"""Sanity tests that run lustre_compat parsers against a real tree.

Skipped when ~/lustre-release is not present.  These guard against
drift in the real file format that synthetic fixtures would miss.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ltvm_pkg.lustre_compat import (
    ChangeLogEntry,
    TargetIn,
    parse_changelog,
    parse_target_in,
    parse_which_patch,
)

LUSTRE_TREE = Path.home() / "lustre-release"

pytestmark = pytest.mark.skipif(
    not (LUSTRE_TREE / "lustre" / "ChangeLog").exists(),
    reason="~/lustre-release not available",
)


_KERNEL_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+")


def test_which_patch_parses_known_rhel9_series():
    wp = parse_which_patch(LUSTRE_TREE)
    assert isinstance(wp, dict)
    assert len(wp) >= 5, f"expected several series, got {len(wp)}"
    assert "5.14-rhel9.7.series" in wp
    kver = wp["5.14-rhel9.7.series"]
    assert _KERNEL_VERSION_RE.match(kver), kver
    assert ".el9" in kver


def test_changelog_top_entry_has_primary_kernels():
    cl = parse_changelog(LUSTRE_TREE)
    assert isinstance(cl, ChangeLogEntry)
    assert cl.server_primary, "expected a non-empty server_primary list"
    assert cl.client_primary, "expected a non-empty client_primary list"
    for kver in cl.server_primary + cl.client_primary:
        assert _KERNEL_VERSION_RE.match(kver), kver


def test_target_in_for_rhel9_7():
    ti = parse_target_in(LUSTRE_TREE, "5.14-rhel9.7")
    assert isinstance(ti, TargetIn)
    assert ti.lnxmaj.startswith("5.14.")
    assert ".el9" in ti.lnxrel
    assert ti.KERNEL_SRPM.startswith("kernel-")
    assert ti.KERNEL_SRPM.endswith(".src.rpm")
    assert ti.SERIES.endswith(".series")


def test_kernel_build_srpm_shape_matches_target_in():
    from ltvm_pkg.kernel_build import parse_lustre_target

    legacy = parse_lustre_target(LUSTRE_TREE, "5.14-rhel9.7")
    new = parse_target_in(LUSTRE_TREE, "5.14-rhel9.7")
    assert legacy["lnxmaj"] == new.lnxmaj
    assert legacy["lnxrel"] == new.lnxrel
    assert legacy["series"] == new.SERIES
