"""`ltvm cluster failover/failback`."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from ltvm_pkg import cluster_failover
from ltvm_pkg.vm_state import ClusterInfo


def _cluster(*nodes: tuple[Any, ...]) -> ClusterInfo:
    """(name, roles, mdt, ost, ip[, failover_for]) tuples."""
    return ClusterInfo(
        name="g",
        nodes=[
            {
                "name": n[0],
                "roles": list(n[1]),
                "mdt_disks": n[2],
                "ost_disks": n[3],
                "ip": n[4],
                **({"failover_for": n[5]} if len(n) > 5 else {}),
            }
            for n in nodes
        ],
    )


class TestNodeFacets:
    def test_combined_mgs_moves_with_mds1(self) -> None:
        c = _cluster(
            ("g-mds1", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("g-oss1", ["oss"], 0, 2, "10.0.0.11"),
            ("g-c", ["client"], 0, 0, "10.0.0.12"),
        )
        assert cluster_failover.node_facets(c) == {
            "g-mds1": ["mds1"],
            "g-oss1": ["ost1", "ost2"],
        }

    def test_separate_mgs_is_its_own_facet(self) -> None:
        c = _cluster(
            ("g-mgs", ["mgs"], 0, 0, "10.0.0.9"),
            ("g-mds1", ["mds"], 1, 0, "10.0.0.10"),
            ("g-oss1", ["oss"], 0, 1, "10.0.0.11"),
        )
        assert cluster_failover.node_facets(c)["g-mgs"] == ["mgs"]

    def test_indexes_follow_node_order(self) -> None:
        c = _cluster(
            ("g-mds1", ["mgs", "mds"], 2, 0, "10.0.0.10"),
            ("g-oss1", ["oss"], 0, 2, "10.0.0.11"),
            ("g-oss2", ["oss"], 0, 1, "10.0.0.12"),
        )
        f = cluster_failover.node_facets(c)
        assert f["g-mds1"] == ["mds1", "mds2"]
        assert f["g-oss2"] == ["ost3"]


class TestPair:
    def _c(self) -> ClusterInfo:
        return _cluster(
            ("g-mds1", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("g-oss1", ["oss"], 0, 2, "10.0.0.11"),
            ("g-c", ["client"], 0, 0, "10.0.0.12"),
            ("g-oss2", ["failover"], 0, 0, "10.0.0.13", "g-oss1"),
        )

    def test_a_node_without_partner_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            cluster_failover._Pair(self._c(), "g-mds1")

    def test_the_partner_name_points_at_the_primary(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            cluster_failover._Pair(self._c(), "g-oss2")
        assert "failover g g-oss1" in capsys.readouterr().err


def test_active_reads_host_and_mount_state() -> None:
    pair = object.__new__(cluster_failover._Pair)
    pair.facets = ["ost1", "ost2"]
    out = "ost1 g-oss2 mounted\nost2 g-oss1 unreachable\nnoise\n"
    with patch.object(
        cluster_failover._Pair,
        "facets_cmd",
        return_value=cluster_failover.subprocess.CompletedProcess(
            "", 0, out, ""
        ),
    ):
        assert pair.active() == {
            "ost1": ("g-oss2", "mounted"),
            "ost2": ("g-oss1", "unreachable"),
        }


def test_freeze_is_refused_on_macos(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import argparse

    args = argparse.Namespace(
        name="g", node="g-oss1", how="freeze", hold=False, timeout=60
    )
    with (
        patch.object(cluster_failover.sys, "platform", "darwin"),
        pytest.raises(SystemExit),
    ):
        cluster_failover.cmd_cluster_failover(args)
    assert "needs a Linux host" in capsys.readouterr().err


def _pair_with_primary() -> Any:
    pair = object.__new__(cluster_failover._Pair)
    pair.primary = cluster_failover.VMInfo(name="g-oss1", ip="10.0.0.11")
    pair.facets = ["ost1"]
    return pair


def test_a_kill_that_cannot_signal_stops_before_the_move() -> None:
    with (
        patch.object(cluster_failover, "_signal_qemu", return_value=False),
        pytest.raises(SystemExit),
    ):
        cluster_failover._fault(_pair_with_primary(), "kill", 60)


def test_a_failed_sigstop_gives_the_link_back() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    with (
        patch.object(
            cluster_failover,
            "_qmp",
            side_effect=lambda vm, c, **a: calls.append((c, a)),
        ),
        patch.object(cluster_failover, "_signal_qemu", return_value=False),
        pytest.raises(SystemExit),
    ):
        cluster_failover._fault(_pair_with_primary(), "freeze", 60)
    assert [a["up"] for _, a in calls] == [False, True]


@pytest.mark.parametrize(
    ("split", "text"),
    [([], "refused by MMP"), (["ost1"], "SPLIT BRAIN: ost1")],
)
def test_split_brain_report(
    capsys: pytest.CaptureFixture[str], split: list[str], text: str
) -> None:
    cluster_failover._report(
        {
            "action": "failover",
            "from": "g-oss1",
            "to": "g-oss2",
            "facets": ["ost1"],
            "rc": 1 if split else 0,
            "mount_secs": 12.0,
            "split_brain": split,
            "output": "",
        },
        use_json=False,
    )
    assert text in capsys.readouterr().out


class TestMovedFacets:
    def _c(self) -> ClusterInfo:
        return _cluster(
            ("g-mds1", ["mgs", "mds"], 1, 0, "10.0.0.10"),
            ("g-oss1", ["oss"], 0, 2, "10.0.0.11"),
            ("g-c", ["client"], 0, 0, "10.0.0.12"),
            ("g-oss2", ["failover"], 0, 0, "10.0.0.13", "g-oss1"),
        )

    def _moved(self, active: dict[str, tuple[str, str]]) -> Any:
        with (
            patch.object(cluster_failover.VMInfo, "load"),
            patch.object(cluster_failover, "lustre_libdir", return_value="/l"),
            patch(
                "ltvm_pkg.vm_commands._os_family_for_vm", return_value="rhel"
            ),
            patch.object(cluster_failover._Pair, "active", return_value=active),
        ):
            return cluster_failover.moved_facets(self._c())

    def test_a_mounted_or_silent_partner_counts(self) -> None:
        assert self._moved(
            {
                "ost1": ("g-oss2", "mounted"),
                "ost2": ("g-oss2", "unreachable"),
            }
        ) == {"g-oss1": ["ost1", "ost2"]}

    def test_a_stale_record_does_not_count(self) -> None:
        assert (
            self._moved(
                {"ost1": ("g-oss2", "unmounted"), "ost2": ("g-oss1", "mounted")}
            )
            == {}
        )

    def test_no_partner_needs_no_probe(self) -> None:
        c = _cluster(("g-mds1", ["mgs", "mds"], 1, 0, "10.0.0.10"))
        with patch.object(cluster_failover._Pair, "active") as active:
            assert cluster_failover.moved_facets(c) == {}
        active.assert_not_called()

    def test_the_refusal_names_the_failback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch.object(
                cluster_failover,
                "moved_facets",
                return_value={"g-oss1": ["ost1"]},
            ),
            pytest.raises(SystemExit),
        ):
            cluster_failover.require_no_moved_facets(self._c(), "start a suite")
        assert "ltvm cluster failback g g-oss1" in capsys.readouterr().err


class TestFailbackFailure:
    """A failback whose mount fails names the way on (fb-46a5ae8c6b1c)."""

    def _run(self, after: dict[str, tuple[str, str]], capsys: Any) -> str:
        import argparse

        pair = object.__new__(cluster_failover._Pair)
        pair.primary = cluster_failover.VMInfo(name="g-mds1", ip="10.0.0.10")
        pair.partner = cluster_failover.VMInfo(name="g-mds2", ip="10.0.0.13")
        pair.client = cluster_failover.VMInfo(name="g-c", ip="10.0.0.12")
        pair.facets = ["mds1", "mds2"]
        before = {f: ("g-mds2", "mounted") for f in pair.facets}
        ok = cluster_failover.subprocess.CompletedProcess("", 0, "", "")
        failed = cluster_failover.subprocess.CompletedProcess(
            "", 1, "mount of mds1 failed; it is mounted nowhere\n", ""
        )

        def facets_cmd(action: str, facets: list[str], timeout: int) -> Any:
            return failed if action == "move" else ok

        args = argparse.Namespace(name="g", node="g-mds1", timeout=60)
        with (
            patch.object(cluster_failover.ClusterInfo, "load"),
            patch.object(cluster_failover, "_require"),
            patch.object(cluster_failover, "_Pair", return_value=pair),
            patch.object(cluster_failover, "is_running", return_value=True),
            patch.object(cluster_failover, "_unfault"),
            patch.object(pair, "active", side_effect=[before, after]),
            patch.object(pair, "facets_cmd", side_effect=facets_cmd),
            pytest.raises(SystemExit),
        ):
            cluster_failover.cmd_cluster_failback(args)
        err: str = capsys.readouterr().err
        return err

    def test_mounted_nowhere_names_the_retry_and_llmount(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        after = {f: ("g-mds1", "unmounted") for f in ("mds1", "mds2")}
        err = self._run(after, capsys)
        assert "mds1, mds2 mounted nowhere" in err
        assert "ltvm cluster failback" in err
        assert "ltvm cluster llmount" in err

    def test_targets_left_on_the_partner_get_no_hint(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        after = {f: ("g-mds2", "mounted") for f in ("mds1", "mds2")}
        assert "mounted nowhere" not in self._run(after, capsys)
