"""Vanilla kernel.org kernel targeting.

Covers the spec grammar (``latest``/``stable``/``6.18``/``7.2.3``), the
ldiskfs series ladder ported from Lustre's own configure, and the
compat gate's treatment of kernels newer than anything Lustre lists.

Resolution is tested against a canned releases.json so the suite never
depends on what kernel.org happens to be publishing today.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ltvm_pkg import upstream_kernel as uk
from ltvm_pkg.lustre_compat import (
    parse_changelog,
    vanilla_ldiskfs_series,
)

# A trimmed copy of https://www.kernel.org/releases.json, including the
# linux-next row whose version is not a kernel version and whose source
# is null -- the shape that would crash a naive parser.
RELEASES = [
    {
        "moniker": "mainline",
        "version": "7.3-rc1",
        "iseol": False,
        "source": "https://git.kernel.org/torvalds/t/linux-7.3-rc1.tar.gz",
    },
    {
        "moniker": "stable",
        "version": "7.2.3",
        "iseol": False,
        "source": "https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.2.3.tar.xz",
    },
    {
        "moniker": "stable",
        "version": "7.1.13",
        "iseol": True,
        "source": "https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.1.13.tar.xz",
    },
    {
        "moniker": "longterm",
        "version": "6.18.49",
        "iseol": False,
        "source": "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.18.49.tar.xz",
    },
    {
        "moniker": "longterm",
        "version": "6.12.108",
        "iseol": False,
        "source": "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.108.tar.xz",
    },
    {
        "moniker": "linux-next",
        "version": "next-20260904",
        "iseol": False,
        "source": None,
    },
]


class TestVersionKey:
    def test_orders_numerically_not_lexically(self) -> None:
        """6.18.49 is newer than 6.18.9; a string compare says otherwise."""
        assert uk.version_key("6.18.49") > uk.version_key("6.18.9")

    def test_rc_sorts_below_its_release(self) -> None:
        """Otherwise a series pin would prefer an rc over the release."""
        assert uk.version_key("7.3-rc1") < uk.version_key("7.3")
        assert uk.version_key("7.3-rc1") < uk.version_key("7.3-rc2")

    def test_short_and_long_versions_compare(self) -> None:
        assert uk.version_key("6.18") < uk.version_key("6.18.1")


class TestTarballUrl:
    def test_release_goes_to_cdn(self) -> None:
        assert uk.tarball_url("7.2.3") == (
            "https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.2.3.tar.xz"
        )

    def test_rc_goes_to_git_snapshot(self) -> None:
        """-rc tags are never published as CDN release tarballs."""
        assert uk.tarball_url("7.3-rc1") == (
            "https://git.kernel.org/torvalds/t/linux-7.3-rc1.tar.gz"
        )

    def test_major_picks_the_right_vN_directory(self) -> None:
        assert "/v6.x/" in uk.tarball_url("6.18.49")


class TestResolveMonikers:
    def test_latest_is_mainline(self) -> None:
        assert uk.resolve("latest", RELEASES).version == "7.3-rc1"
        assert uk.resolve("mainline", RELEASES).version == "7.3-rc1"

    def test_latest_records_the_moniker(self) -> None:
        assert uk.resolve("latest", RELEASES).moniker == "mainline"

    def test_stable_picks_newest_not_first(self) -> None:
        """Two stable rows are listed; the newest must win."""
        assert uk.resolve("stable", RELEASES).version == "7.2.3"

    def test_longterm_picks_newest(self) -> None:
        assert uk.resolve("longterm", RELEASES).version == "6.18.49"

    def test_uses_the_url_kernel_org_gave(self) -> None:
        assert uk.resolve("stable", RELEASES).source.endswith(
            "linux-7.2.3.tar.xz"
        )


class TestResolveVersions:
    def test_exact_version(self) -> None:
        r = uk.resolve("7.2.3", RELEASES)
        assert r.version == "7.2.3"

    def test_exact_rc(self) -> None:
        r = uk.resolve("7.3-rc1", RELEASES)
        assert r.version == "7.3-rc1"
        assert r.is_rc

    def test_exact_version_absent_from_json_still_resolves(self) -> None:
        """An older point release stays buildable after kernel.org moves on."""
        r = uk.resolve("6.18.10", RELEASES)
        assert r.version == "6.18.10"
        assert r.source.endswith("linux-6.18.10.tar.xz")

    def test_series_picks_newest_point_release(self) -> None:
        assert uk.resolve("6.18", RELEASES).version == "6.18.49"

    def test_series_unlisted_falls_back_to_base_release(self) -> None:
        """An EOL series is gone from releases.json but stays on the CDN."""
        r = uk.resolve("5.4", RELEASES)
        assert r.version == "5.4"
        assert r.source.endswith("linux-5.4.tar.xz")

    def test_series_does_not_pick_an_rc(self) -> None:
        """A series pin means the released series.

        7.3-rc1 is the only 7.3 row.  Returning it would quietly build a
        release candidate for someone who asked for a release, and
        falling back to linux-7.3.tar.xz would 404 much later, inside
        the container -- so say so now and name the way forward.
        """
        with pytest.raises(uk.UpstreamResolveError) as exc:
            uk.resolve("7.3", RELEASES)
        assert "7.3-rc1" in str(exc.value)
        assert "latest" in str(exc.value)


class TestResolveErrors:
    @pytest.mark.parametrize("spec", ["bogus", "", "6.x", "next-20260904"])
    def test_rejects_non_specs(self, spec: str) -> None:
        with pytest.raises(uk.UpstreamResolveError):
            uk.resolve(spec, RELEASES)

    def test_linux_next_is_not_selectable(self) -> None:
        """Its source is null and it cannot be built from a tarball."""
        with pytest.raises(uk.UpstreamResolveError):
            uk.resolve("linux-next", RELEASES)

    def test_message_lists_what_is_accepted(self) -> None:
        with pytest.raises(uk.UpstreamResolveError, match="latest"):
            uk.resolve("bogus", RELEASES)


class TestFetchReleases:
    def test_parses_the_releases_list(self) -> None:
        payload = json.dumps({"releases": RELEASES}).encode()
        with patch("ltvm_pkg.upstream_kernel.subprocess.run") as run:
            run.return_value.stdout = payload
            assert len(uk.fetch_releases()) == len(RELEASES)

    def test_bad_json_is_reported_not_raised_raw(self) -> None:
        with patch("ltvm_pkg.upstream_kernel.subprocess.run") as run:
            run.return_value.stdout = b"<html>404</html>"
            with pytest.raises(uk.UpstreamResolveError, match="valid JSON"):
                uk.fetch_releases()

    def test_missing_releases_key_is_reported(self) -> None:
        with patch("ltvm_pkg.upstream_kernel.subprocess.run") as run:
            run.return_value.stdout = b'{"latest_stable": {"version": "7.2.3"}}'
            with pytest.raises(uk.UpstreamResolveError, match="releases"):
                uk.fetch_releases()


class TestVanillaLdiskfsLadder:
    """Ported from the mainline ladder in config/lustre-build-ldiskfs.m4.

    Every expected stem below is a series file that exists in the Lustre
    tree; if Lustre adds a new -ml series the ladder needs the new entry
    and these cases need a new row.
    """

    @pytest.mark.parametrize(
        "version,expected",
        [
            ("5.4.0", "ldiskfs-5.4.0-ml"),
            ("5.4.10", "ldiskfs-5.4.0-ml"),
            ("5.4.21", "ldiskfs-5.4.21-ml"),
            ("5.4.50", "ldiskfs-5.4.136-ml"),
            ("5.10.0", "ldiskfs-5.10.0-ml"),
            ("6.1.36", "ldiskfs-6.1.38-ml"),
            ("6.6.13", "ldiskfs-6.6-ml"),
            ("6.12.95", "ldiskfs-6.12-ml"),
            ("6.18.22", "ldiskfs-6.18-ml"),
            ("6.18.49", "ldiskfs-6.18-ml"),
            ("6.19.12", "ldiskfs-7.0-ml"),
            ("7.0.8", "ldiskfs-7.0-ml"),
            ("7.2.3", "ldiskfs-7.0-ml"),
        ],
    )
    def test_ladder(self, version: str, expected: str) -> None:
        assert vanilla_ldiskfs_series(version) == expected

    def test_below_the_first_series_has_none(self) -> None:
        assert vanilla_ldiskfs_series("4.19.1") is None
        assert vanilla_ldiskfs_series("5.3.99") is None

    def test_rc_uses_its_base_version(self) -> None:
        """7.3-rc1 gets the same series 7.3 would."""
        assert vanilla_ldiskfs_series("7.3-rc1") == "ldiskfs-7.0-ml"

    def test_unreleased_kernel_falls_to_the_top_entry(self) -> None:
        """The catch-all is what lets a brand-new mainline resolve at all."""
        assert vanilla_ldiskfs_series("9.9.9") == "ldiskfs-7.0-ml"

    def test_dashed_ml_names_are_reached(self) -> None:
        """Regression: the filename heuristic looked for ``ldiskfs-6.18.``
        with a dot and so matched none of the ``-ml`` series, which are
        named with a dash.  Every one of these was previously None."""
        for v in ("6.6.1", "6.12.1", "6.18.1", "7.0.1"):
            assert vanilla_ldiskfs_series(v) is not None


# ------------------------------------------------------------------
# ChangeLog: both the flat and the distro-grouped layouts
# ------------------------------------------------------------------

# The layout Lustre used before kernels were grouped under distro
# headings.
CHANGELOG_FLAT = """\
2.17.0
       * Server primary kernels built and tested during release cycle:
         5.14.0-611.55.1.el9  (RHEL9.7)
         4.18.0-553.155.1.el8 (RHEL8.10)
       * ldiskfs needs an ldiskfs patch series for that kernel
       * Client primary kernels built and tested during release cycle:
         5.14.0-611.42.1.el9  (RHEL9.7)
         6.8.0-35             (Ubuntu 24.04)
       * Other clients known to build on these kernels at some point (others may also work):
         5.14.0-427.42.1.el9  (RHEL9.4)
         5.4.0-37             (Ubuntu 20.04)
"""

# The layout on current master: same sections, but each is subdivided by
# distro with an underlined heading, and a "Vanilla" group carries
# kernel.org releases.
CHANGELOG_GROUPED = """\
2.17.0
       * Server primary kernels built and tested during release cycle:
         RHEL 9
         ------
         5.14.0-611.55.1.el9   (RHEL9.7)
         Vanilla
         -------
         vanilla linux 7.0.8   (ldiskfs)
         vanilla linux 6.18.22 (ZFS + ldiskfs)
       * ldiskfs needs an ldiskfs patch series for that kernel
       * Client primary kernels built and tested during release cycle:
         RHEL 10
         -------
         6.12.0-211.47.1.el10  (RHEL10.2)
         Ubuntu
         ------
         6.8.0-35              (Ubuntu 24.04)
       * Other clients known to build on these kernels at some point (others may also work):
         Ubuntu
         ------
         7.0.0-15              (Ubuntu 24.04)
         Vanilla
         -------
         7.0.8                 (vanilla kernel.org)
         6.19.12               (vanilla kernel.org)
"""


def _tree_with_changelog(tmp_path: Path, text: str) -> Path:
    (tmp_path / "lustre").mkdir(parents=True, exist_ok=True)
    (tmp_path / "lustre" / "ChangeLog").write_text(text)
    return tmp_path


class TestChangeLogFormats:
    """Both layouts must parse: trees in flight span the reformat."""

    def test_flat_layout(self, tmp_path: Path) -> None:
        cl = parse_changelog(_tree_with_changelog(tmp_path, CHANGELOG_FLAT))
        assert "5.14.0-611.55.1.el9" in cl.server_primary
        assert "6.8.0-35" in cl.client_primary
        assert "5.4.0-37" in cl.client_best_effort

    def test_grouped_layout(self, tmp_path: Path) -> None:
        cl = parse_changelog(_tree_with_changelog(tmp_path, CHANGELOG_GROUPED))
        assert "5.14.0-611.55.1.el9" in cl.server_primary
        assert "6.12.0-211.47.1.el10" in cl.client_primary
        assert "7.0.0-15" in cl.client_best_effort

    def test_grouped_layout_finds_vanilla_kernels(self, tmp_path: Path) -> None:
        """The Vanilla group is why an upstream target can validate at all."""
        cl = parse_changelog(_tree_with_changelog(tmp_path, CHANGELOG_GROUPED))
        assert "7.0.8" in cl.server_primary
        assert "6.18.22" in cl.server_primary
        assert "7.0.8" in cl.client_best_effort
        assert "6.19.12" in cl.client_best_effort

    def test_distro_headings_are_not_read_as_kernels(
        self, tmp_path: Path
    ) -> None:
        """"RHEL 10", "Vanilla" and the ----- rules are headings, not versions."""
        cl = parse_changelog(_tree_with_changelog(tmp_path, CHANGELOG_GROUPED))
        every = (
            cl.server_primary
            + cl.server_best_effort
            + cl.client_primary
            + cl.client_best_effort
        )
        for entry in every:
            assert entry[0].isdigit(), f"heading leaked in as a kernel: {entry}"


# ------------------------------------------------------------------
# The compat gate on an upstream target
# ------------------------------------------------------------------

# The -ml series Lustre ships, as bare files: validate_target only needs
# the names to exist unless a kernel build tree is present to dry-apply
# against.
_ML_SERIES = (
    "ldiskfs-5.4.0-ml",
    "ldiskfs-5.4.21-ml",
    "ldiskfs-5.4.136-ml",
    "ldiskfs-5.10.0-ml",
    "ldiskfs-6.1.38-ml",
    "ldiskfs-6.6-ml",
    "ldiskfs-6.12-ml",
    "ldiskfs-6.18-ml",
    "ldiskfs-7.0-ml",
)


@pytest.fixture()
def upstream_tree(tmp_path: Path) -> Path:
    """A Lustre tree shaped like master: which_patch plus -ml series."""
    tree = _tree_with_changelog(tmp_path, CHANGELOG_GROUPED)
    kp = tree / "lustre" / "kernel_patches"
    kp.mkdir(parents=True, exist_ok=True)
    (kp / "which_patch").write_text(
        "PATCH SERIES FOR SERVER KERNELS:\n"
        "5.14-rhel9.7.series     5.14.0-611.55.1.el9  (RHEL 9.7)\n"
    )
    series = tree / "ldiskfs" / "kernel_patches" / "series"
    series.mkdir(parents=True, exist_ok=True)
    for stem in _ML_SERIES:
        (series / f"{stem}.series").write_text("")
    return tree


@pytest.fixture()
def mainline_tc():
    from ltvm_pkg.target_config import TargetConfig

    return TargetConfig("mainline")


class TestUpstreamGate:
    def _validate(self, tc, tree: Path, kernel: str):
        from ltvm_pkg.lustre_compat import validate_target

        return validate_target(tc, tree, kernel=kernel)

    @pytest.mark.parametrize(
        "kernel,stem",
        [
            ("stable-7.2.3", "ldiskfs-7.0-ml"),
            ("latest-7.3-rc1", "ldiskfs-7.0-ml"),
            ("6.18-6.18.49", "ldiskfs-6.18-ml"),
            ("longterm-6.12.108", "ldiskfs-6.12-ml"),
        ],
    )
    def test_vanilla_kernels_pass_with_the_right_series(
        self, mainline_tc, upstream_tree: Path, kernel: str, stem: str
    ) -> None:
        r = self._validate(mainline_tc, upstream_tree, kernel)
        assert r.status == "ok", r.message
        assert stem in r.message

    def test_version_is_read_off_the_built_dir_name(
        self, mainline_tc, upstream_tree: Path
    ) -> None:
        """A spec is not a version; only the <spec>-<version> half is."""
        r = self._validate(mainline_tc, upstream_tree, "latest-7.3-rc1")
        assert r.kernel_version == "7.3-rc1"

    def test_kernel_below_the_ladder_is_refused(
        self, mainline_tc, upstream_tree: Path
    ) -> None:
        r = self._validate(mainline_tc, upstream_tree, "4.19-4.19.1")
        assert r.status == "refuse"

    def test_tree_without_the_series_is_refused_with_the_reason(
        self, mainline_tc, tmp_path: Path
    ) -> None:
        """An older Lustre tree predating vanilla server support."""
        tree = _tree_with_changelog(tmp_path, CHANGELOG_GROUPED)
        kp = tree / "lustre" / "kernel_patches"
        kp.mkdir(parents=True, exist_ok=True)
        (kp / "which_patch").write_text(
            "PATCH SERIES FOR SERVER KERNELS:\n"
            "5.14-rhel9.7.series     5.14.0-611.55.1.el9  (RHEL 9.7)\n"
        )
        (tree / "ldiskfs" / "kernel_patches" / "series").mkdir(parents=True)
        r = self._validate(mainline_tc, tree, "stable-7.2.3")
        assert r.status == "refuse"
        assert "ldiskfs-7.0-ml" in r.message
        assert "does not ship" in r.message


class TestUpstreamClientGate:
    """Client mode warns rather than refusing: being ahead is the point."""

    @pytest.fixture()
    def client_tc(self, mainline_tc):
        from ltvm_pkg.target_config import LustreMode

        mainline_tc.lustre_mode = LustreMode.CLIENT
        return mainline_tc

    def test_listed_vanilla_kernel_matches_changelog(
        self, client_tc, upstream_tree: Path
    ) -> None:
        from ltvm_pkg.lustre_compat import validate_target

        r = validate_target(client_tc, upstream_tree, kernel="7.0-7.0.8")
        assert r.status in ("ok", "best_effort")
        assert r.matched_in != "upstream_ahead"

    def test_newer_than_changelog_warns_and_proceeds(
        self, client_tc, upstream_tree: Path
    ) -> None:
        from ltvm_pkg.lustre_compat import validate_target

        r = validate_target(client_tc, upstream_tree, kernel="latest-7.3-rc1")
        assert r.status == "best_effort"
        assert r.matched_in == "upstream_ahead"
        assert "expected" in r.message


class TestMainlineTarget:
    def test_declares_the_specs_it_accepts(self, mainline_tc) -> None:
        assert set(mainline_tc.declared_kernels()) >= {
            "latest",
            "stable",
            "longterm",
        }

    def test_is_upstream_and_server_mode(self, mainline_tc) -> None:
        from ltvm_pkg.target_config import LustreMode

        assert mainline_tc.is_upstream
        assert mainline_tc.lustre_mode == LustreMode.SERVER_LDISKFS

    def test_defaults_to_a_release_not_an_rc(self, mainline_tc) -> None:
        assert mainline_tc.default_kernel == "stable"

    def test_built_dir_names_round_trip_to_their_spec(
        self, mainline_tc
    ) -> None:
        assert mainline_tc._short_kernel_name("latest-7.3-rc1") == "latest"
        assert mainline_tc._short_kernel_name("6.18-6.18.49") == "6.18"

    def test_resolved_version_reaches_the_staleness_hash(
        self, mainline_tc
    ) -> None:
        """Two builds of "latest" differ if Linus tagged in between."""
        a = mainline_tc.input_hash(
            "kernel", kernel="latest", extra=b"upstream:7.3-rc1"
        )
        b = mainline_tc.input_hash(
            "kernel", kernel="latest", extra=b"upstream:7.3-rc2"
        )
        assert a != b

    def test_uses_the_upstream_inner_build_script(self, mainline_tc) -> None:
        """A non-upstream target must not hash the upstream script."""
        from ltvm_pkg.target_config import TargetConfig

        rocky = TargetConfig("rocky10")
        assert mainline_tc.input_hash("kernel") != rocky.input_hash("kernel")
