"""ltvm build release: what it plans, how it schedules, what it refuses."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from ltvm_pkg import release_build as rb
from ltvm_pkg.cli import release as release_mod
from ltvm_pkg.cli.fetch import _is_base_stem

DECLARED = {
    "rocky9": ["5.14-rhel9.8", "5.14-rhel9.7"],
    "rocky10": ["6.12-rhel10.2", "6.12-rhel10.1", "6.12-rhel10.0"],
    "rocky10-16k": ["6.12-rhel10.2"],
    "debian13": ["6.12-debian13"],
    "opensuse156": ["6.4-sles15sp7", "6.4-sles15sp6"],
}


def _rel(tag: str, manifest: str | None = None) -> dict[str, Any]:
    name = f"manifest-{manifest or tag}.json"
    return {"tag_name": tag, "assets": [{"name": name}]}


def _kernel_of(target: str, tag: str, arch: str) -> str | None:
    sigs = {
        "el9_8": "5.14-rhel9.8",
        "el9_7": "5.14-rhel9.7",
        "el10_2": "6.12-rhel10.2",
        "el10_1": "6.12-rhel10.1",
        "el10_0": "6.12-rhel10.0",
        "6.12.107": "6.12-debian13",
        "-150700.": "6.4-sles15sp7",
        "-150600.": "6.4-sles15sp6",
    }
    for sig, k in sigs.items():
        if sig in tag and k in DECLARED.get(target, []):
            return k
    return None


def _is_base(target: str, arch: str, stem: str) -> bool:
    return _is_base_stem(stem, ["mofed-24"] if target == "rocky9" else [])


RELEASES = [
    _rel("rocky9-x86_64-5.14.0-687.39.1.el9_8_lustre"),
    _rel("rocky9-x86_64-5.14.0-687.17.1.el9_8_lustre"),
    _rel("rocky9-x86_64-5.14.0-611.55.1.el9_7_lustre"),
    _rel(
        "rocky9-x86_64-5.14.0-611.55.1.el9_7_lustre-mofed-24",
        "rocky9-x86_64-5.14.0-611.55.1.el9_7_lustre-mofed-24",
    ),
    _rel("rocky10-x86_64-6.12.0-211.47.1.el10_2_lustre"),
    _rel("rocky10-x86_64-6.12.0-55.41.1.el10_0_lustre"),
    _rel("rocky10-aarch64-6.12.0-124.56.1.el10_1_lustre"),
    _rel("rocky10-16k-aarch64-6.12.0-211.47.1.el10_2_lustre"),
    _rel("debian13-x86_64-6.12.107-lustre"),
    _rel("opensuse156-x86_64-6.4.0-150700.53.66_lustre-default"),
    _rel("opensuse156-x86_64-6.4.0-150600.23.78_lustre-default"),
    _rel("rocky9-x86_64-5.14.0-503.40.1.el9_5_lustre"),
    {"tag_name": "qemu-macos-11.1.3-1", "assets": [{"name": "qemu.tar.gz"}]},
]


class TestPlan:
    def test_published_builds_for_one_arch(self) -> None:
        builds, unmatched, _ = rb.published_builds(
            RELEASES, "x86_64", DECLARED, _kernel_of, _is_base
        )
        assert [b.name for b in builds] == [
            "rocky9:5.14-rhel9.8",
            "rocky9:5.14-rhel9.7",
            "rocky10:6.12-rhel10.2",
            "rocky10:6.12-rhel10.0",
            "debian13:6.12-debian13",
            "opensuse156:6.4-sles15sp7",
            "opensuse156:6.4-sles15sp6",
        ]
        # rhel9.5 is not declared here, so it is reported, not built
        assert unmatched == ["rocky9-x86_64-5.14.0-503.40.1.el9_5_lustre"]

    def test_aarch64_takes_the_longest_target_prefix(self) -> None:
        builds, _, _ = rb.published_builds(
            RELEASES, "aarch64", DECLARED, _kernel_of, _is_base
        )
        assert [b.name for b in builds] == [
            "rocky10:6.12-rhel10.1",
            "rocky10-16k:6.12-rhel10.2",
        ]

    def test_variant_only_release_is_not_built(self) -> None:
        builds, _, _ = rb.published_builds(
            [RELEASES[3]], "x86_64", DECLARED, _kernel_of, _is_base
        )
        assert builds == []

    @pytest.mark.parametrize(
        "stem,base",
        [
            ("manifest-debian13-x86_64-6.12.107-lustre", True),
            (
                "manifest-opensuse156-x86_64-6.4.0-150700.53.66_lustre-default",
                True,
            ),
            ("manifest-ubuntu2604-server-x86_64-7.0.0-14-lustre", True),
            ("manifest-rocky9-x86_64-5.14.0-611.55.1.el9_7_lustre", True),
            (
                "manifest-rocky9-x86_64-5.14.0-611.55.1.el9_7_lustre-mofed",
                False,
            ),
        ],
    )
    def test_base_stem(self, stem: str, base: bool) -> None:
        assert _is_base_stem(stem, []) is base

    def test_breadth_first(self) -> None:
        builds, _, _ = rb.published_builds(
            RELEASES, "x86_64", DECLARED, _kernel_of, _is_base
        )
        assert [b.name for b in rb.breadth_first(builds)] == [
            "rocky9:5.14-rhel9.8",
            "rocky10:6.12-rhel10.2",
            "debian13:6.12-debian13",
            "opensuse156:6.4-sles15sp7",
            "rocky9:5.14-rhel9.7",
            "rocky10:6.12-rhel10.0",
            "opensuse156:6.4-sles15sp6",
        ]

    def test_only(self) -> None:
        builds = [
            rb.Build("rocky9", "5.14-rhel9.8"),
            rb.Build("rocky9", "5.14-rhel9.7"),
            rb.Build("rocky10", "6.12-rhel10.2"),
        ]
        assert rb.filter_builds(builds, None) == builds
        assert rb.filter_builds(builds, ["rocky9"]) == builds[:2]
        assert rb.filter_builds(builds, ["rocky9:5.14-rhel9.7", "rocky10"]) == [
            builds[1],
            builds[2],
        ]


class TestCpus:
    def test_cpulist_round_trip(self) -> None:
        cpus = rb.parse_cpulist("0-3,8,10-11\n")
        assert cpus == [0, 1, 2, 3, 8, 10, 11]
        assert rb.format_cpulist(cpus) == "0-3,8,10-11"

    def test_whole_numa_nodes_per_slot(self) -> None:
        # Sookie: 8 nodes of 8 cores, SMT siblings 64 apart
        nodes = [
            list(range(8 * i, 8 * i + 8)) + list(range(64 + 8 * i, 72 + 8 * i))
            for i in range(8)
        ]
        avail = sorted(c for n in nodes for c in n)
        sets = rb.cpu_sets(4, None, avail, nodes)
        assert [rb.format_cpulist(s or []) for s in sets] == [
            "0-15,64-79",
            "16-31,80-95",
            "32-47,96-111",
            "48-63,112-127",
        ]

    def test_fewer_nodes_than_slots_splits_cpus(self) -> None:
        sets = rb.cpu_sets(2, None, list(range(8)), [list(range(8))])
        assert sets == [[0, 1, 2, 3], [4, 5, 6, 7]]

    def test_affinity_limits_nodes(self) -> None:
        nodes = [[0, 1], [2, 3], [4, 5]]
        sets = rb.cpu_sets(2, None, [2, 3, 4, 5], nodes)
        assert sets == [[2, 3], [4, 5]]

    def test_unpinned(self) -> None:
        assert rb.cpu_sets(3, None, None, []) == [None, None, None]
        assert rb.cpu_sets(1, None, list(range(64)), []) == [None]

    def test_by_hand(self) -> None:
        assert rb.cpu_sets(2, "0-1:4,5", list(range(8)), []) == [[0, 1], [4, 5]]
        with pytest.raises(rb.ReleaseBuildError, match="2 CPU sets for 3"):
            rb.cpu_sets(3, "0-1:4,5", list(range(8)), [])

    @pytest.mark.parametrize(
        "n,jobs", [(None, 1), (8, 1), (64, 2), (128, 4), (512, 4)]
    )
    def test_default_jobs(self, n: int | None, jobs: int) -> None:
        assert rb.default_jobs(None if n is None else list(range(n))) == jobs


class TestStayAwake:
    def test_only_on_macos(self) -> None:
        with patch.object(rb.subprocess, "Popen") as popen:
            assert rb.stay_awake("linux") is None
        popen.assert_not_called()

    def test_caffeinate_follows_this_process(self) -> None:
        with (
            patch.object(
                rb.shutil, "which", return_value="/usr/bin/caffeinate"
            ),
            patch.object(rb.subprocess, "Popen") as popen,
        ):
            assert rb.stay_awake("darwin", pid=4242) is popen.return_value
        assert popen.call_args.args[0] == ["caffeinate", "-ims", "-w", "4242"]

    def test_no_caffeinate(self) -> None:
        with (
            patch.object(rb.shutil, "which", return_value=None),
            patch.object(rb.subprocess, "Popen") as popen,
        ):
            assert rb.stay_awake("darwin") is None
        popen.assert_not_called()


class TestDisk:
    def test_one_filesystem_sums_every_need(self, tmp_path: Path) -> None:
        free = SimpleNamespace(free=100 * rb.GIB)
        problems = rb.disk_problems(
            [tmp_path / "a"] * 10,
            tmp_path / "s",
            tmp_path / "w",
            2,
            10,
            usage=lambda p: free,
            tmp=tmp_path / "t",
        )
        # 10 builds * (6 + 1) + 2 slots * (25 + 12 + 2) + 10 margin = 158
        assert len(problems) == 1
        assert "100 GiB free, about 158 GiB needed" in problems[0]
        for what in ("artifacts", "podman storage", "temporary files"):
            assert what in problems[0]
        assert problems[0].count("artifacts (") == 1

    def test_enough(self, tmp_path: Path) -> None:
        free = SimpleNamespace(free=1000 * rb.GIB)
        assert (
            rb.disk_problems(
                [tmp_path] * 12, tmp_path, tmp_path, 4, 12, usage=lambda p: free
            )
            == []
        )

    def test_nothing_to_build_needs_nothing(self, tmp_path: Path) -> None:
        free = SimpleNamespace(free=0)
        assert (
            rb.disk_problems([], tmp_path, tmp_path, 4, 0, usage=lambda p: free)
            == []
        )

    def test_preflight_alone_still_needs_store_space(
        self, tmp_path: Path
    ) -> None:
        free = SimpleNamespace(free=0)
        (problem,) = rb.disk_problems(
            [],
            tmp_path,
            tmp_path,
            4,
            0,
            usage=lambda p: free,
            tmp=tmp_path,
            new_targets=10,
        )
        # 10 new targets * 6 + 10 margin
        assert "about 70 GiB needed" in problem

    def test_slots_beyond_the_builds_reserve_nothing(
        self, tmp_path: Path
    ) -> None:
        free = SimpleNamespace(free=50 * rb.GIB)
        # 1 build: 6 + 1 + one slot of 39 + 10 margin = 56, not four slots
        (problem,) = rb.disk_problems(
            [tmp_path],
            tmp_path,
            tmp_path,
            4,
            1,
            usage=lambda p: free,
            tmp=tmp_path,
        )
        assert "about 56 GiB needed" in problem

    def test_new_targets_need_store_space(self, tmp_path: Path) -> None:
        free = SimpleNamespace(free=60 * rb.GIB)
        args = ([tmp_path], tmp_path, tmp_path, 1, 1)
        kw: dict[str, Any] = {"usage": lambda p: free, "tmp": tmp_path}
        # 6 + 1 + 25 + 12 + 2 + 10 margin = 56
        assert rb.disk_problems(*args, **kw) == []
        # two targets with nothing in podman yet: 12 more
        (problem,) = rb.disk_problems(*args, **kw, new_targets=2)
        assert "about 68 GiB needed" in problem

    def test_archived_target_counts_on_its_own_filesystem(
        self, tmp_path: Path
    ) -> None:
        hot = tmp_path / "hot"
        cold = tmp_path / "cold" / "rocky9" / "x86_64"
        cold.mkdir(parents=True)
        (hot / "rocky9").mkdir(parents=True)
        (hot / "rocky9" / "x86_64").symlink_to(cold)
        seen: list[Path] = []

        def usage(p: Path) -> Any:
            seen.append(p)
            return SimpleNamespace(free=1000 * rb.GIB)

        real_stat = Path.stat

        def stat(self: Path, **kw: Any) -> Any:
            st = real_stat(self, **kw)
            dev = (
                2
                if str(self.resolve()).startswith(str(tmp_path / "cold"))
                else 1
            )
            return SimpleNamespace(st_dev=dev, st_mode=st.st_mode)

        with patch.object(Path, "stat", stat):
            rb.disk_problems(
                [hot / "rocky9" / "x86_64", hot / "rocky10" / "x86_64"],
                None,
                hot,
                1,
                2,
                usage=usage,
                tmp=hot,
            )
        assert any(p.resolve() == cold for p in seen)

    def test_podman_machine_store_is_not_checked(self, tmp_path: Path) -> None:
        free = SimpleNamespace(free=1000 * rb.GIB)
        assert (
            rb.disk_problems(
                [tmp_path],
                None,
                tmp_path,
                1,
                1,
                usage=lambda p: free,
                tmp=tmp_path,
            )
            == []
        )
        with patch.object(rb.sys, "platform", "darwin"):
            assert rb.podman_store() is None


class TestFailedStep:
    def test_last_step_error_wins(self) -> None:
        log = (
            "==> Building kernel ...\n"
            "error: Image build failed: Command '['podman', 'build']' "
            "returned non-zero exit status 123.\n"
        )
        assert rb.failed_step(log) == "image"
        assert rb.failed_step("error: Lustre snapshot failed: x\n") == "lustre"
        assert rb.failed_step("Killed\n") is None


def _runner(jobs: int = 2) -> rb.Runner:
    return rb.Runner(
        ltvm=["ltvm"], arch="x86_64", cpus=[None] * jobs, say=lambda m: None
    )


class TestScheduler:
    def test_never_two_builds_of_one_target(self) -> None:
        items = [
            rb.Build("rocky9", "a"),
            rb.Build("rocky9", "b"),
            rb.Build("rocky9", "c"),
            rb.Build("rocky10", "a"),
        ]
        running: dict[str, int] = {}
        lock = threading.Lock()
        worst: list[int] = [0]
        started: list[str] = []

        def work(slot: int, b: rb.Build) -> rb.Result:
            with lock:
                running[b.target] = running.get(b.target, 0) + 1
                worst[0] = max(worst[0], running[b.target])
                started.append(b.name)
            time.sleep(0.05)
            with lock:
                running[b.target] -= 1
            return rb.Result(b, "ok")

        res = rb.run_parallel(items, 3, work, _runner(3), one_per_target=True)
        assert worst[0] == 1
        assert set(res) == set(items)
        # the free slot takes rocky10 rather than waiting on rocky9
        assert started[:2] == ["rocky9:a", "rocky10:a"]

    def test_an_exception_fails_one_build(self) -> None:
        items = [rb.Build("rocky9", "a"), rb.Build("rocky10", "a")]

        def work(slot: int, b: rb.Build) -> rb.Result:
            if b.target == "rocky9":
                raise OSError("boom")
            return rb.Result(b, "ok")

        res = rb.run_parallel(items, 2, work, _runner(), one_per_target=True)
        assert res[items[0]].status == "failed"
        assert "boom" in res[items[0]].problems[0]
        assert res[items[1]].status == "ok"

    def test_stopping_starts_nothing_more(self) -> None:
        items = [rb.Build(f"t{i}", "k") for i in range(5)]
        runner = _runner(1)
        seen: list[rb.Build] = []

        def work(slot: int, b: rb.Build) -> rb.Result:
            seen.append(b)
            runner.stopping.set()
            return rb.Result(b, "ok")

        res = rb.run_parallel(items, 1, work, runner, one_per_target=True)
        assert len(seen) == 1 and len(res) == 1

    def test_runner_pins_and_logs(self, tmp_path: Path) -> None:
        runner = rb.Runner(
            ltvm=["ltvm"], arch="aarch64", cpus=[[0, 1, 2]], say=lambda m: None
        )
        calls: list[list[str]] = []

        class FakeProc:
            def __init__(self, cmd: list[str], **kw: Any) -> None:
                calls.append(cmd)
                kw["stdout"].write(b"output\n")

            def wait(self, timeout: float | None = None) -> int:
                return 3

            def poll(self) -> int:
                return 3

        log = tmp_path / "x" / "b.log"
        with patch.object(rb.subprocess, "Popen", FakeProc):
            assert runner.run(0, ["build", "all", "rocky9"], log) == 3
        assert calls == [
            [
                "taskset",
                "-c",
                "0-2",
                "ltvm",
                "build",
                "all",
                "rocky9",
                "--arch",
                "aarch64",
                "--no-update",
            ]
        ]
        assert log.read_text().startswith("$ taskset -c 0-2 ltvm build all")


class TestBuildAll:
    def _run(
        self, tmp_path: Path, rc: int, problems: list[str]
    ) -> tuple[dict[rb.Build, rb.Result], list[Any]]:
        runner = _runner(1)
        recorded: list[Any] = []

        def fake_run(slot: int, argv: list[str], log: Path) -> int:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(
                "error: Kernel build failed: make\n"
                if rc
                else "==> Building image for rocky9 "
                "(kernel=5.14-rhel9.8-5.14.0-687.39.1.el9_8)...\n"
            )
            return rc

        b = rb.Build("rocky9", "5.14-rhel9.8")
        with (
            patch.object(runner, "run", side_effect=fake_run) as run,
            patch.object(rb, "verify", return_value=problems) as ver,
        ):
            res = rb.build_all(
                [b],
                1,
                runner,
                [tmp_path / "t0"],
                tmp_path,
                "abc",
                lambda b, kdir: recorded.append((b, kdir)),
            )
        if not rc:
            assert ver.call_args.args[3] == "5.14-rhel9.8-5.14.0-687.39.1.el9_8"
        argv = run.call_args.args[1]
        assert argv == [
            "build",
            "all",
            "rocky9",
            "--kernel",
            "5.14-rhel9.8",
            "--lustre-tree",
            str(tmp_path / "t0"),
            "-y",
        ]
        return res, recorded

    def test_ok_is_recorded(self, tmp_path: Path) -> None:
        res, recorded = self._run(tmp_path, 0, [])
        assert [r.status for r in res.values()] == ["ok"]
        assert [k for _, k in recorded] == [
            "5.14-rhel9.8-5.14.0-687.39.1.el9_8"
        ]

    def test_failure_names_the_step(self, tmp_path: Path) -> None:
        res, recorded = self._run(tmp_path, 1, [])
        r = next(iter(res.values()))
        assert (r.status, r.step) == ("failed", "kernel")
        assert recorded == []

    def test_rc0_that_does_not_verify_fails(self, tmp_path: Path) -> None:
        res, recorded = self._run(
            tmp_path, 0, ["image is stale against its inputs"]
        )
        r = next(iter(res.values()))
        assert (r.status, r.step) == ("failed", "verify")
        assert recorded == []


class TestPreflight:
    def test_container_then_base_layers_per_target(
        self, tmp_path: Path
    ) -> None:
        runner = _runner(2)
        argvs: list[list[str]] = []

        def fake_run(slot: int, argv: list[str], log: Path) -> int:
            argvs.append(argv)
            return 1 if argv[:3] == ["build", "image", "rocky10"] else 0

        builds = [
            rb.Build("rocky9", "a"),
            rb.Build("rocky10", "a"),
            rb.Build("rocky9", "b"),
        ]
        with patch.object(runner, "run", side_effect=fake_run):
            res = rb.preflight(builds, 2, runner, tmp_path)
        assert res["rocky9"].status == "ok"
        assert (res["rocky10"].status, res["rocky10"].step) == (
            "failed",
            "image",
        )
        assert sorted(argvs) == sorted(
            [
                ["build", "container", "rocky9"],
                ["build", "image", "rocky9", "--base-only"],
                ["build", "container", "rocky10"],
                ["build", "image", "rocky10", "--base-only"],
            ]
        )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def lustre_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "lustre"
    (repo / "lustre" / "kernel_patches").mkdir(parents=True)
    (repo / "README").write_text("x\n")
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-qm",
        "one",
    )
    _git(repo, "tag", "v2_17_0")
    return repo


class TestTrees:
    def test_dirty_tree_refused(self, lustre_repo: Path) -> None:
        (lustre_repo / "README").write_text("changed\n")
        with pytest.raises(rb.ReleaseBuildError, match="uncommitted"):
            rb.lustre_head(lustre_repo)

    def test_untracked_files_are_fine(self, lustre_repo: Path) -> None:
        (lustre_repo / "build.log").write_text("x\n")
        assert rb.lustre_head(lustre_repo) == _git(
            lustre_repo, "rev-parse", "HEAD"
        )

    def test_clones_follow_head(
        self, lustre_repo: Path, tmp_path: Path
    ) -> None:
        head = rb.lustre_head(lustre_repo)
        trees = rb.prepare_trees(lustre_repo, head, tmp_path / "work", 2)
        assert [_git(t, "rev-parse", "HEAD") for t in trees] == [head, head]
        (lustre_repo / "README").write_text("two\n")
        _git(
            lustre_repo,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qam",
            "two",
        )
        _git(lustre_repo, "tag", "v2_17_1")
        head2 = rb.lustre_head(lustre_repo)
        trees = rb.prepare_trees(lustre_repo, head2, tmp_path / "work", 2)
        assert _git(trees[1], "rev-parse", "HEAD") == head2
        assert _git(trees[1], "describe", "--tags") == "v2_17_1"


class TestLedger:
    def test_round_trip(self, tmp_path: Path) -> None:
        b = rb.Build("rocky9", "5.14-rhel9.8")
        led = rb.Ledger(tmp_path / "done.json")
        assert led.done("x86_64", b, "v1", "abc") is None
        led.record("x86_64", b, "v1", "abc", "5.14-rhel9.8-x")
        again = rb.Ledger(tmp_path / "done.json")
        assert again.done("x86_64", b, "v1", "abc") == ("5.14-rhel9.8-x", None)
        assert again.done("x86_64", b, "v2", "abc") is None
        assert again.done("x86_64", b, "v1", "def") is None
        assert again.done("aarch64", b, "v1", "abc") is None

    def test_corrupt_ledger_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "done.json").write_text("{not json")
        led = rb.Ledger(tmp_path / "done.json")
        assert led.done("x86_64", rb.Build("a", "b"), "v", "c") is None


class TestKernelDirFromLog:
    def test_last_image_line(self) -> None:
        log = (
            "==> Building image for rocky9 (kernel=5.14-rhel9.7-old)...\n"
            "noise\n"
            "==> Building image for rocky9 (kernel=5.14-rhel9.7-5.14.0-611.13.1.el9_7)...\n"
            "    image took 3m\n"
        )
        assert rb.built_kernel_dir(log) == "5.14-rhel9.7-5.14.0-611.13.1.el9_7"
        assert rb.built_kernel_dir("no image step\n") is None

    def test_matches_what_build_all_prints(self) -> None:
        import inspect

        from ltvm_pkg.cli import build

        src = inspect.getsource(build._cmd_build_all_body)
        assert (
            'print(f"==> Building image for {args.target} '
            '(kernel={full_kernel})...")' in src
        )


class _FakeTC:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.complete = {"kernel": True, "image": True}
        self.stale = False
        self.extra: bytes | None = None

    def kernel_output_dir(self, kernel: str) -> Path:
        return self.root / "kernels" / kernel

    def image_output_dir(self, kernel: str) -> Path:
        return self.root / "images" / kernel

    def declared_variants(self) -> list[str]:
        return []

    def outputs_complete(self, artifact: str, kernel: str) -> bool:
        return self.complete[artifact]

    def is_stale(self, artifact: str, kernel: str, extra_hash: bytes) -> bool:
        self.extra = extra_hash
        return self.stale


class TestVerify:
    def _setup(self, tmp_path: Path) -> tuple[_FakeTC, Path]:
        from ltvm_pkg.lustre_build import _hash_file, _hash_staged_modules

        kdir = tmp_path / "kernels" / "5.14-rhel9.8-x"
        (kdir / "build-tree").mkdir(parents=True)
        (kdir / "build-tree" / "Module.symvers").write_text("0x1 sym vmlinux\n")
        snap = kdir / "lustre-artifacts"
        (snap / "lib").mkdir(parents=True)
        (kdir / "meta.json").write_text("{}")
        (snap / "lib" / "lustre.ko").write_text("x")
        (snap / "lib" / "libcfs.ko").write_text("y")
        (snap / ".ltvm-snapshot.json").write_text(
            json.dumps({"lustre_commit": "abc123"})
        )
        (snap / ".ltvm-staging-meta.json").write_text(
            json.dumps(
                {
                    "module_symvers_sha256": _hash_file(
                        kdir / "build-tree" / "Module.symvers"
                    ),
                    "lustre_modules_sha256": _hash_staged_modules(snap),
                }
            )
        )
        return _FakeTC(tmp_path), snap

    def _verify(self, tc: _FakeTC, head: str = "abc123") -> list[str]:
        with patch("ltvm_pkg.target_config.TargetConfig", return_value=tc):
            return rb.verify(
                rb.Build("rocky9", "5.14-rhel9.8"),
                "x86_64",
                head,
                "5.14-rhel9.8-x",
            )

    def test_good(self, tmp_path: Path) -> None:
        tc, _ = self._setup(tmp_path)
        assert self._verify(tc) == []
        # the image is checked against the snapshot's Lustre digests
        assert tc.extra is not None and b"lustre:|" in tc.extra

    def test_other_commit(self, tmp_path: Path) -> None:
        tc, _ = self._setup(tmp_path)
        assert self._verify(tc, "def456") == [
            "Lustre snapshot is from abc123, not def456"
        ]

    def test_incomplete_kernel(self, tmp_path: Path) -> None:
        tc, _ = self._setup(tmp_path)
        tc.complete["kernel"] = False
        assert self._verify(tc)[0].endswith("is missing or incomplete")

    def test_kernel_abi_changed_under_the_snapshot(
        self, tmp_path: Path
    ) -> None:
        tc, _ = self._setup(tmp_path)
        symvers = (
            tmp_path
            / "kernels"
            / "5.14-rhel9.8-x"
            / "build-tree"
            / "Module.symvers"
        )
        symvers.write_text("0x2 sym vmlinux\n")
        assert self._verify(tc)[0].endswith(
            "was built against another kernel ABI"
        )

    def test_payload_changed_since_verified(self, tmp_path: Path) -> None:
        tc, snap = self._setup(tmp_path)
        kdir = snap.parent
        (kdir / "modules" / "lib").mkdir(parents=True)
        (kdir / "modules" / "lib" / "ext4.ko").write_text("e")
        (snap / "sbin").mkdir()
        (snap / "sbin" / "mount.lustre").write_text("m")
        b = rb.Build("rocky9", "5.14-rhel9.8")
        with patch("ltvm_pkg.target_config.TargetConfig", return_value=tc):
            built = rb.build_payload(b, "x86_64", "5.14-rhel9.8-x")
            assert (
                rb.verify(b, "x86_64", "abc123", "5.14-rhel9.8-x", built) == []
            )
            for gone in (
                snap / "sbin" / "mount.lustre",
                kdir / "modules" / "lib" / "ext4.ko",
            ):
                content = gone.read_text()
                gone.unlink()
                (problem,) = rb.verify(
                    b, "x86_64", "abc123", "5.14-rhel9.8-x", built
                )
                assert problem.endswith("changed since the build was verified")
                gone.write_text(content)

    def test_payload_ignores_file_times(self, tmp_path: Path) -> None:
        import os

        _, snap = self._setup(tmp_path)
        before = rb.payload_digest(snap.parent)
        os.utime(snap / "lib" / "lustre.ko", (1, 1))
        assert rb.payload_digest(snap.parent) == before

    def test_snapshot_without_modules(self, tmp_path: Path) -> None:
        tc, snap = self._setup(tmp_path)
        (snap / "lib" / "lustre.ko").unlink()
        (snap / "lib" / "libcfs.ko").unlink()
        assert self._verify(tc)[0].endswith("has no modules")

    def test_image_without_this_lustre(self, tmp_path: Path) -> None:
        tc, snap = self._setup(tmp_path)
        tc.stale = True
        assert self._verify(tc) == [
            "image does not match its inputs and this Lustre"
        ]
        tc.stale = False
        (snap / ".ltvm-staging-meta.json").unlink()
        assert (
            "image does not match its inputs and this Lustre"
            in self._verify(tc)
        )

    def test_no_image(self, tmp_path: Path) -> None:
        tc, _ = self._setup(tmp_path)
        tc.complete["image"] = False
        assert self._verify(tc) == ["no image"]


class TestLocks:
    def test_second_run_refused(self, tmp_path: Path) -> None:
        first = rb.lock_dir(tmp_path / "w", "its trees")
        with pytest.raises(rb.ReleaseBuildError, match="another.*its trees"):
            rb.lock_dir(tmp_path / "w", "its trees")
        first.close()
        rb.lock_dir(tmp_path / "w", "its trees").close()


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().split()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return stat[2] != "Z"


class TestStop:
    def test_stop_reaches_the_whole_group(self, tmp_path: Path) -> None:
        pidfile = tmp_path / "child.pid"
        script = tmp_path / "parent.sh"
        # a grandchild that outlives a plain kill of its parent
        script.write_text(f"sleep 60 & echo $! > {pidfile}\nwait\n")
        runner = rb.Runner(
            ltvm=["bash", str(script)],
            arch="x86_64",
            cpus=[None],
            say=lambda m: None,
        )
        rcs: list[int] = []
        t = threading.Thread(
            target=lambda: rcs.append(runner.run(0, [], tmp_path / "log"))
        )
        t.start()
        deadline = time.monotonic() + 10
        while not pidfile.exists() or not pidfile.read_text().strip():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        child = int(pidfile.read_text())
        runner.stop(grace=5)
        t.join(10)
        assert rcs and rcs[0] != 0
        deadline = time.monotonic() + 5
        while _alive(child):
            assert time.monotonic() < deadline, "grandchild survived stop()"
            time.sleep(0.05)

    def test_nothing_starts_after_stop(self, tmp_path: Path) -> None:
        runner = _runner(1)
        runner.stop()
        with patch.object(rb.subprocess, "Popen") as popen:
            assert runner.run(0, ["build"], tmp_path / "log") == 130
        popen.assert_not_called()


def test_usable_cpus_follows_affinity() -> None:
    from ltvm_pkg.paths import usable_cpus

    with patch("os.sched_getaffinity", return_value={0, 1, 2}):
        assert usable_cpus() == 3


def rb_preflight_calls(summary: dict[str, Any]) -> list[rb.Build]:
    return list(_PREFLIGHT_SEEN)


_PREFLIGHT_SEEN: list[rb.Build] = []


class TestCommand:
    def _args(
        self, tree: Path, tmp_path: Path, **kw: Any
    ) -> argparse.Namespace:
        base = dict(
            json=True,
            arch="x86_64",
            lustre_tree=str(tree),
            only=None,
            jobs=2,
            cpus=None,
            no_preflight=False,
            fresh=False,
            stall_minutes=60,
            dry_run=True,
            work_dir=str(tmp_path / "work"),
            log_dir=str(tmp_path / "logs"),
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def _patches(self, verify_problems: list[str]) -> Any:
        import contextlib

        stack = contextlib.ExitStack()
        stack.enter_context(
            patch("ltvm_pkg.cli._gh_api", return_value=RELEASES)
        )
        stack.enter_context(
            patch("ltvm_pkg.cli.release._declared", return_value=DECLARED)
        )
        stack.enter_context(
            patch("ltvm_pkg.cli._fetched_kernel_name", _kernel_of)
        )
        stack.enter_context(
            patch(
                "ltvm_pkg.release_package._declared_variant_names",
                return_value=[],
            )
        )
        stack.enter_context(
            patch.object(rb, "verify", return_value=verify_problems)
        )
        stack.enter_context(
            patch.object(rb, "available_cpus", return_value=None)
        )
        stack.enter_context(
            patch.object(
                rb,
                "podman_store",
                return_value=Path(tempfile.mkdtemp())
                / "containers"
                / "storage",
            )
        )
        return stack

    def test_dry_run_plan(
        self,
        lustre_repo: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ltvm_pkg.cli.release import cmd_build_release

        with (
            self._patches([]),
            patch.object(rb, "disk_problems", return_value=[]),
        ):
            rc = cmd_build_release(self._args(lustre_repo, tmp_path))
        assert rc == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["builds"][:2] == [
            "rocky9:5.14-rhel9.8",
            "rocky10:6.12-rhel10.2",
        ]
        assert plan["jobs"] == 2 and plan["cpus"] == [None, None]
        assert plan["unmatched_releases"] == [
            "rocky9-x86_64-5.14.0-503.40.1.el9_5_lustre"
        ]
        assert not (tmp_path / "work").exists()

    def test_not_enough_disk_refuses(
        self,
        lustre_repo: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ltvm_pkg.cli.release import cmd_build_release

        with (
            self._patches([]),
            patch.object(rb, "disk_problems", return_value=["/: 1 GiB free"]),
            patch.object(rb, "prepare_trees") as prep,
        ):
            rc = cmd_build_release(
                self._args(lustre_repo, tmp_path, dry_run=False)
            )
        assert rc == 1
        prep.assert_not_called()

    def test_run_skips_done_and_reports(
        self,
        lustre_repo: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ltvm_pkg.cli.release import cmd_build_release

        head = _git(lustre_repo, "rev-parse", "HEAD")
        led = rb.Ledger(tmp_path / "work" / "done-x86_64.json")
        led.record(
            "x86_64",
            rb.Build("debian13", "6.12-debian13"),
            "0.5.1+abc",
            head,
            "6.12-debian13-6.12.107",
        )

        def fake_pre_record(todo: list[rb.Build], *a: Any) -> Any:
            _PREFLIGHT_SEEN[:] = todo
            return fake_pre(todo, *a)

        def fake_pre(todo: list[rb.Build], *a: Any) -> dict[str, rb.Result]:
            return {
                b.target: rb.Result(
                    b,
                    "failed" if b.target == "opensuse156" else "ok",
                    "image",
                    log="pre.log",
                )
                for b in todo
            }

        def fake_all(
            builds: list[rb.Build], *a: Any, **kw: Any
        ) -> dict[rb.Build, rb.Result]:
            return {
                b: rb.Result(
                    b, "failed" if b.kernel == "6.12-rhel10.0" else "ok"
                )
                for b in builds
            }

        with (
            self._patches([]),
            patch.object(rb, "disk_problems", return_value=[]),
            patch.object(rb, "preflight", side_effect=fake_pre_record),
            patch.object(rb, "build_all", side_effect=fake_all) as ball,
            patch(
                "ltvm_pkg.cli.release._ltvm_version", return_value="0.5.1+abc"
            ),
            patch("ltvm_pkg.cli.release._new_targets", return_value=0),
        ):
            rc = cmd_build_release(
                self._args(lustre_repo, tmp_path, dry_run=False)
            )
        assert rc == 1
        built = [b.name for b in ball.call_args.args[0]]
        assert "debian13:6.12-debian13" not in built
        assert not any(n.startswith("opensuse156") for n in built)
        summary = json.loads((tmp_path / "logs" / "summary.json").read_text())
        by = {f"{r['target']}:{r['kernel']}": r for r in summary["results"]}
        assert by["debian13:6.12-debian13"]["status"] == "done"
        assert by["opensuse156:6.4-sles15sp6"]["status"] == "skipped"
        assert by["rocky10:6.12-rhel10.0"]["status"] == "failed"
        assert by["rocky9:5.14-rhel9.8"]["status"] == "ok"
        assert (
            by["debian13:6.12-debian13"]["kernel_dir"]
            == "6.12-debian13-6.12.107"
        )
        assert len(list((tmp_path / "work").glob("lustre-*"))) == 2
        # the finished target still gets its preflight (its container)
        pre_targets = [b.target for b in rb_preflight_calls(summary)]
        assert "debian13" in pre_targets
        # the handlers and locks are put back
        import signal as _signal

        assert (
            _signal.getsignal(_signal.SIGTERM)
            is not release_mod._raise_interrupt
        )
        rb.lock_dir(tmp_path / "work", "x").close()

    def test_fresh_ignores_the_ledger(
        self,
        lustre_repo: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ltvm_pkg.cli.release import cmd_build_release

        led = rb.Ledger(tmp_path / "work" / "done-x86_64.json")
        led.record(
            "x86_64",
            rb.Build("debian13", "6.12-debian13"),
            "0.5.1+abc",
            _git(lustre_repo, "rev-parse", "HEAD"),
            "k",
        )
        with (
            self._patches([]),
            patch.object(rb, "disk_problems", return_value=[]),
            patch(
                "ltvm_pkg.cli.release._ltvm_version", return_value="0.5.1+abc"
            ),
            patch("ltvm_pkg.cli.release._new_targets", return_value=0),
        ):
            cmd_build_release(self._args(lustre_repo, tmp_path))
            plain = json.loads(capsys.readouterr().out)
            cmd_build_release(self._args(lustre_repo, tmp_path, fresh=True))
            fresh = json.loads(capsys.readouterr().out)
        assert plain["already_done"] == ["debian13:6.12-debian13"]
        assert fresh["already_done"] == []

    def test_bad_targets_yaml_stops_the_run(
        self,
        lustre_repo: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ltvm_pkg.cli.release import cmd_build_release

        with (
            self._patches([]),
            patch(
                "ltvm_pkg.cli.release._declared",
                side_effect=ValueError("rocky9: no kernel 'x'"),
            ),
        ):
            assert cmd_build_release(self._args(lustre_repo, tmp_path)) == 1
        assert "rocky9: no kernel" in capsys.readouterr().err


class TestBaseOnly:
    def test_build_image_base_only(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import ltvm_pkg.cli as cli_mod

        args = argparse.Namespace(
            json=True, target="rocky9", base_only=True, arch=None, variant=None
        )
        with (
            patch(
                "ltvm_pkg.cli.build._load_target_args",
                return_value=(object(), None),
            ),
            patch("ltvm_pkg.cli.build._preflight_podman", return_value=None),
            patch("ltvm_pkg.cli.build._preflight_container", return_value=None),
            patch(
                "ltvm_pkg.cli.build_image_base",
                return_value="ltvm-image-rocky9",
            ) as bib,
            patch("ltvm_pkg.cli.build_image") as bi,
        ):
            rc = cli_mod.cmd_build_image(args)
        assert rc == 0
        bib.assert_called_once()
        bi.assert_not_called()
        assert (
            json.loads(capsys.readouterr().out)["base_tag"]
            == "ltvm-image-rocky9"
        )


def test_target_list_sees_word_ending_releases() -> None:
    from ltvm_pkg.cli import _release_status

    rel = [_rel("debian13-x86_64-6.12.107-lustre")]
    with patch(
        "ltvm_pkg.release_package._declared_variant_names", return_value=[]
    ):
        _, remote = _release_status("debian13", "x86_64", rel)
    assert remote == "6.12.107-lustre"


def test_ltvm_identity_is_a_clean_head() -> None:
    from ltvm_pkg.cli import release

    clean = subprocess.CompletedProcess([], 0, "", "")
    dirty = subprocess.CompletedProcess([], 0, " M ltvm\n", "")
    with patch(
        "ltvm_pkg.version_info.git_version", return_value=("0.5.9+abc", "abc")
    ):
        with patch.object(subprocess, "run", return_value=clean):
            assert release._ltvm_version() == "0.5.9+abc abc"
        with patch.object(subprocess, "run", return_value=dirty):
            assert release._ltvm_version() is None
    with patch("ltvm_pkg.version_info.git_version", return_value=None):
        assert release._ltvm_version() is None


class TestCpuLimits:
    def test_more_jobs_than_cpus(self) -> None:
        with pytest.raises(rb.ReleaseBuildError, match="only 2 CPUs"):
            rb.cpu_sets(3, None, [0, 1], [])

    def test_empty_set_by_hand(self) -> None:
        with pytest.raises(rb.ReleaseBuildError, match="empty"):
            rb.cpu_sets(2, "0-1:", [0, 1, 2], [])


def test_fresh_forces_the_builds(tmp_path: Path) -> None:
    runner = _runner(1)
    with patch.object(runner, "run", return_value=1) as run:
        rb.build_all(
            [rb.Build("rocky9", "k")],
            1,
            runner,
            [tmp_path],
            tmp_path,
            "h",
            lambda b, k: None,
            force=True,
        )
    assert run.call_args.args[1][-1] == "--force"


def test_borrowing_clone_is_recloned(lustre_repo: Path, tmp_path: Path) -> None:
    head = rb.lustre_head(lustre_repo)
    work = tmp_path / "work"
    t = work / "lustre-0"
    _git(tmp_path, "clone", "-q", "--shared", str(lustre_repo), str(t))
    assert (t / ".git/objects/info/alternates").exists()
    (trees,) = [rb.prepare_trees(lustre_repo, head, work, 1)]
    assert not (trees[0] / ".git/objects/info/alternates").exists()
    assert _git(trees[0], "rev-parse", "HEAD") == head


def test_interrupt_waits_for_the_worker_not_the_join() -> None:
    runner = _runner(1)
    item = rb.Build("rocky9", "k")

    def work(slot: int, b: rb.Build) -> rb.Result:
        time.sleep(0.3)
        return rb.Result(b, "ok")

    real_join = threading.Thread.join
    calls = {"n": 0}

    def join(self: threading.Thread, timeout: float | None = None) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        real_join(self, timeout)

    with patch.object(threading.Thread, "join", join):
        res = rb.run_parallel([item], 1, work, runner, one_per_target=True)
    assert runner.stopping.is_set()
    assert res[item].status == "ok"


def test_one_build_left_is_not_pinned_to_a_slice(
    lustre_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from ltvm_pkg.cli.release import cmd_build_release

    t = TestCommand()
    with (
        t._patches([]),
        patch.object(rb, "available_cpus", return_value=list(range(128))),
        patch.object(rb, "numa_nodes", return_value=[]),
        patch.object(rb, "disk_problems", return_value=[]),
        patch("ltvm_pkg.cli.release._new_targets", return_value=0),
    ):
        cmd_build_release(
            t._args(lustre_repo, tmp_path, jobs=None, only=["debian13"])
        )
    plan = json.loads(capsys.readouterr().out)
    assert plan["jobs"] == 1 and plan["cpus"] == [None]


def test_ledger_marks_an_attempt_until_it_verifies(tmp_path: Path) -> None:
    b = rb.Build("rocky9", "k")
    led = rb.Ledger(tmp_path / "done.json")
    led.record("x86_64", b, "v", "c", "kd")
    runner = _runner(1)
    seen: list[Any] = []

    def fake_run(slot: int, argv: list[str], log: Path) -> int:
        again = rb.Ledger(tmp_path / "done.json")
        seen.append(
            (again.done("x86_64", b, "v", "c"), again.unfinished("x86_64", b))
        )
        return 1

    with patch.object(runner, "run", side_effect=fake_run):
        rb.build_all(
            [b],
            1,
            runner,
            [tmp_path],
            tmp_path,
            "c",
            lambda b, k: None,
            on_start=lambda b: led.start("x86_64", b),
        )
    # during the attempt, and after it failed, nothing counts as done and
    # the next run is told to force it
    assert seen == [(None, True)]
    after = rb.Ledger(tmp_path / "done.json")
    assert after.done("x86_64", b, "v", "c") is None
    assert after.unfinished("x86_64", b)
    after.record("x86_64", b, "v", "c", "kd")
    assert not rb.Ledger(tmp_path / "done.json").unfinished("x86_64", b)


def test_preflight_alone_needs_working_room(tmp_path: Path) -> None:
    free = SimpleNamespace(free=0)
    (problem,) = rb.disk_problems(
        [],
        tmp_path,
        tmp_path,
        1,
        0,
        usage=lambda p: free,
        tmp=tmp_path,
        preflight_slots=2,
    )
    # 2 slots * 25 + 10 margin
    assert "about 60 GiB needed" in problem


def test_lock_paths_are_each_taken_once(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths = rb.lock_paths(tmp_path / "a", tmp_path / "a", home=home)
    # artifacts and work coincide: one lock; podman's is per user
    assert len(paths) == 2
    assert paths[1][0] == home / ".cache" / "ltvm" / "release-build-podman.lock"
    # other directories are still this user's podman
    other = rb.lock_paths(tmp_path / "b", tmp_path / "c", home=home)
    assert other[2][0] == paths[1][0]
    locks = [rb.lock_dir(p, why) for p, why in paths]
    with pytest.raises(rb.ReleaseBuildError, match="this user's podman"):
        rb.lock_dir(*other[2])
    for f in locks:
        f.close()


def test_unwritable_lock_dir_is_an_error_not_a_crash(tmp_path: Path) -> None:
    with patch("builtins.open", side_effect=PermissionError("denied")):
        with pytest.raises(rb.ReleaseBuildError, match="cannot lock"):
            rb.lock_dir(tmp_path, "x")


def test_background_launch_gets_a_working_sigint(
    lustre_repo: Path, tmp_path: Path
) -> None:
    import signal as sig

    from ltvm_pkg.cli.release import cmd_build_release

    seen: list[Any] = []
    t = TestCommand()
    old = sig.signal(sig.SIGINT, sig.SIG_IGN)
    try:
        with (
            t._patches([]),
            patch.object(rb, "disk_problems", return_value=[]),
            patch("ltvm_pkg.cli.release._new_targets", return_value=0),
            patch(
                "ltvm_pkg.cli.release._plan_and_run",
                side_effect=lambda *a: (
                    seen.append(sig.getsignal(sig.SIGINT)) or 0
                ),
            ),
        ):
            cmd_build_release(t._args(lustre_repo, tmp_path, dry_run=False))
        assert seen == [sig.default_int_handler]
        assert sig.getsignal(sig.SIGINT) is sig.SIG_IGN
    finally:
        sig.signal(sig.SIGINT, old)


def test_new_ccache_space_is_counted(tmp_path: Path) -> None:
    free = SimpleNamespace(free=60 * rb.GIB)
    args = ([tmp_path], tmp_path, tmp_path, 1, 1)
    kw: dict[str, Any] = {"usage": lambda p: free, "tmp": tmp_path}
    assert rb.disk_problems(*args, **kw) == []
    (problem,) = rb.disk_problems(*args, **kw, ccache_bytes=10 * rb.GIB)
    # 56 + 2 * 5
    assert "about 66 GiB needed" in problem


def test_zero_need_filesystems_are_not_checked(tmp_path: Path) -> None:
    (problem,) = rb.disk_problems(
        [],
        tmp_path / "store",
        tmp_path / "work",
        1,
        0,
        usage=lambda p: SimpleNamespace(free=0),
        tmp=tmp_path / "tmp",
        preflight_slots=1,
    )
    # only the preflight's podman room, plus the margin
    assert "about 35 GiB needed for podman storage" in problem
    assert "Lustre trees" not in problem and "temporary" not in problem


def test_zfs_release_is_reported_not_built() -> None:
    rel = _rel("rocky9-x86_64-5.14.0-687.39.1.el9_8_lustre")
    rel["assets"].append(
        {"name": "zfs-rocky9-x86_64-5.14.0-687.39.1.el9_8_lustre-2.3.4.tar.zst"}
    )
    builds, unmatched, with_zfs = rb.published_builds(
        [rel], "x86_64", DECLARED, _kernel_of, _is_base
    )
    assert builds == [] and unmatched == []
    assert with_zfs == ["rocky9-x86_64-5.14.0-687.39.1.el9_8_lustre"]


def test_cross_preflight_needs_artifact_space(tmp_path: Path) -> None:
    (problem,) = rb.disk_problems(
        [],
        None,
        tmp_path,
        1,
        0,
        usage=lambda p: SimpleNamespace(free=0),
        tmp=tmp_path,
        preflight_slots=1,
        cross_preflight=[tmp_path / "a", tmp_path / "b"],
    )
    # 2 * 2 + 10 margin
    assert "about 14 GiB needed for artifacts" in problem


def test_a_silent_build_is_stopped_as_stalled(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "quiet.sh"
    script.write_text(f"echo started\nsleep 60 & echo $! > {pidfile}\nwait\n")
    runner = rb.Runner(
        ltvm=["bash", str(script)],
        arch="x86_64",
        cpus=[None],
        say=lambda m: None,
        stall_seconds=0.5,
        poll_seconds=0.1,
    )
    t0 = time.monotonic()
    assert runner.run(0, [], tmp_path / "log") == rb.STALLED
    assert time.monotonic() - t0 < 30
    assert "no output for" in (tmp_path / "log").read_text()
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _alive(child):
        assert time.monotonic() < deadline, "the stalled build's child survived"
        time.sleep(0.05)


def test_a_build_that_keeps_writing_is_not_stalled(tmp_path: Path) -> None:
    script = tmp_path / "chatty.sh"
    script.write_text("for i in 1 2 3 4 5 6 7 8; do echo $i; sleep 0.2; done\n")
    runner = rb.Runner(
        ltvm=["bash", str(script)],
        arch="x86_64",
        cpus=[None],
        say=lambda m: None,
        stall_seconds=0.5,
        poll_seconds=0.1,
    )
    assert runner.run(0, [], tmp_path / "log") == 0


def test_a_custom_tag_is_found_by_its_manifest() -> None:
    rel = _rel("rocky9", "rocky9-x86_64-5.14.0-687.39.1.el9_8_lustre")
    builds, unmatched, _ = rb.published_builds(
        [rel], "x86_64", DECLARED, _kernel_of, _is_base
    )
    assert [b.name for b in builds] == ["rocky9:5.14-rhel9.8"]
    assert unmatched == []


def test_a_new_commit_cleans_the_reused_clone(
    lustre_repo: Path, tmp_path: Path
) -> None:
    head = rb.lustre_head(lustre_repo)
    (tree,) = rb.prepare_trees(lustre_repo, head, tmp_path / "w", 1)
    (tree / "config.status").write_text("old configure\n")
    rb.prepare_trees(lustre_repo, head, tmp_path / "w", 1)
    assert (tree / "config.status").exists()  # same commit: kept
    (lustre_repo / "README").write_text("next\n")
    _git(
        lustre_repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-qam",
        "next",
    )
    rb.prepare_trees(
        lustre_repo, rb.lustre_head(lustre_repo), tmp_path / "w", 1
    )
    assert not (tree / "config.status").exists()


def test_one_target_gets_one_slot(
    lustre_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from ltvm_pkg.cli.release import cmd_build_release

    t = TestCommand()
    with (
        t._patches([]),
        patch.object(rb, "available_cpus", return_value=list(range(128))),
        patch.object(rb, "numa_nodes", return_value=[]),
        patch.object(rb, "disk_problems", return_value=[]),
        patch("ltvm_pkg.cli.release._new_targets", return_value=0),
    ):
        cmd_build_release(
            t._args(lustre_repo, tmp_path, jobs=4, only=["rocky10"])
        )
    plan = json.loads(capsys.readouterr().out)
    assert len(plan["builds"]) == 2 and plan["jobs"] == 1


def test_a_wedged_podman_fails_the_plan() -> None:
    from ltvm_pkg.cli import release

    with (
        patch.object(
            subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["podman"], 60),
        ),
        pytest.raises(rb.ReleaseBuildError, match="podman did not answer"),
    ):
        release._new_targets(["rocky9"], "x86_64")


def test_a_build_stopped_by_the_run_is_interrupted_not_failed(
    tmp_path: Path,
) -> None:
    runner = _runner(1)

    def fake_run(slot: int, argv: list[str], log: Path) -> int:
        runner.stopping.set()
        return 130

    with patch.object(runner, "run", side_effect=fake_run):
        res = rb.build_all(
            [rb.Build("rocky9", "k")],
            1,
            runner,
            [tmp_path],
            tmp_path,
            "h",
            lambda b, k: None,
        )
    assert [r.status for r in res.values()] == ["interrupted"]


def test_a_failed_wait_does_not_orphan_the_build(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "slow.sh"
    script.write_text(f"sleep 60 & echo $! > {pidfile}\nwait\n")
    runner = rb.Runner(
        ltvm=["bash", str(script)],
        arch="x86_64",
        cpus=[None],
        say=lambda m: None,
    )

    def boom(*a: Any) -> int:
        deadline = time.monotonic() + 10
        while not pidfile.exists() or not pidfile.read_text().strip():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        raise OSError("log disk full")

    with patch.object(runner, "_wait", side_effect=boom):
        with pytest.raises(OSError):
            runner.run(0, [], tmp_path / "log")
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _alive(child):
        assert time.monotonic() < deadline, "the build outlived its runner"
        time.sleep(0.05)
    assert not runner._procs


def test_payload_sees_same_size_changes(tmp_path: Path) -> None:
    (tmp_path / "lustre-artifacts").mkdir()
    (tmp_path / "lustre-artifacts" / "lustre.ko").write_bytes(b"old code")
    before = rb.payload_digest(tmp_path)
    (tmp_path / "lustre-artifacts" / "lustre.ko").write_bytes(b"new code")
    assert rb.payload_digest(tmp_path) != before


def test_zfs_is_judged_per_kernel() -> None:
    rel = {
        "tag_name": "rocky9",
        "assets": [
            {
                "name": "manifest-rocky9-x86_64-5.14.0-611.55.1.el9_7_lustre.json"
            },
            {
                "name": "manifest-rocky9-x86_64-5.14.0-687.39.1.el9_8_lustre.json"
            },
            {
                "name": "zfs-rocky9-x86_64-5.14.0-611.55.1.el9_7_lustre-2.3.4.tar.zst"
            },
        ],
    }
    builds, _, with_zfs = rb.published_builds(
        [rel], "x86_64", DECLARED, _kernel_of, _is_base
    )
    assert [b.name for b in builds] == ["rocky9:5.14-rhel9.8"]
    assert with_zfs == ["rocky9"]


def test_remote_podman_store_is_not_checked() -> None:
    remote = subprocess.CompletedProcess(
        [], 0, "true /var/lib/containers\n", ""
    )
    local = subprocess.CompletedProcess([], 0, "false /data/containers\n", "")
    with patch.object(rb.sys, "platform", "linux"):
        with patch.object(rb.subprocess, "run", return_value=remote):
            assert rb.podman_store() is None
        with patch.object(rb.subprocess, "run", return_value=local):
            assert rb.podman_store() == Path("/data/containers")


def test_a_release_candidate_is_unmatched() -> None:
    rel = _rel("rocky10-x86_64-6.12.0-rc5")
    builds, unmatched, _ = rb.published_builds(
        [rel],
        "x86_64",
        {"rocky10": ["6.12-rhel10.2"]},
        lambda t, s, a: "6.12-rhel10.2",
        _is_base,
    )
    assert builds == [] and unmatched == ["rocky10-x86_64-6.12.0-rc5"]


def test_a_truncated_image_is_not_whole(tmp_path: Path) -> None:
    from ltvm_pkg.paths import ext4_whole
    from tests.conftest import fake_ext4

    img = tmp_path / "base.ext4"
    img.write_bytes(fake_ext4(8))
    assert ext4_whole(img)
    img.write_bytes(fake_ext4(8)[:4096])
    assert not ext4_whole(img)
    img.write_bytes(b"not ext4" * 300)
    assert not ext4_whole(img)


def test_podman_volume_caches_are_not_charged_to_the_host(
    tmp_path: Path,
) -> None:
    from ltvm_pkg.cli import release

    builds = [rb.Build("rocky9", "k")]
    full = rb.CCACHE_BYTES_PER_TARGET
    exists = subprocess.CompletedProcess([], 0, b"", b"")
    missing = subprocess.CompletedProcess([], 1, b"", b"")
    with patch("ltvm_pkg.build_volumes.enabled", return_value=True):
        with patch.object(subprocess, "run", return_value=exists):
            assert release._ccache_room(builds, "x86_64", tmp_path) == 0
        # no volume yet: the build may fall back to the host directory
        with patch.object(subprocess, "run", return_value=missing):
            assert release._ccache_room(builds, "x86_64", tmp_path) == full
    with patch("ltvm_pkg.build_volumes.enabled", return_value=False):
        assert release._ccache_room(builds, "x86_64", tmp_path) == full


def test_kernel_ccache_room_is_charged_beside_the_artifacts() -> None:
    from ltvm_pkg.cli import release

    (entry,) = release._kernel_ccache_room([rb.Build("rocky9", "k")], "x86_64")
    path, room, what = entry
    assert path.name == "ccache" and room <= rb.CCACHE_BYTES_PER_TARGET
    assert "kernel ccache" in what
    (entry,) = release._kernel_ccache_room([rb.Build("rocky9", "k")], "aarch64")
    assert entry[0].name == "ccache-aarch64"


def test_a_kernel_is_planned_from_the_release_fetch_takes() -> None:
    """Two releases of one declared kernel: only the one fetch takes,
    the newest, says whether it carries ZFS.  Another release of it
    with ZFS neither brings the kernel back nor keeps it out."""
    plain = _rel("rocky9-x86_64-5.14.0-611.13.1.el9_7_lustre")
    zfs = _rel("rocky9-x86_64-5.14.0-611.55.1.el9_7_lustre")
    zfs["assets"].append(
        {"name": "zfs-rocky9-x86_64-5.14.0-611.55.1.el9_7_lustre-2.3.4.tar.zst"}
    )
    builds, _, with_zfs = rb.published_builds(
        [zfs, plain], "x86_64", DECLARED, _kernel_of, _is_base
    )
    assert builds == []
    assert with_zfs == ["rocky9-x86_64-5.14.0-611.55.1.el9_7_lustre"]
    builds, _, with_zfs = rb.published_builds(
        [plain, zfs], "x86_64", DECLARED, _kernel_of, _is_base
    )
    assert builds == [rb.Build("rocky9", "5.14-rhel9.7")]
    assert with_zfs == []


def test_payload_names_the_current_image_and_skips_variants(
    tmp_path: Path,
) -> None:
    kdir = tmp_path / "k"
    (kdir / "lustre-artifacts" / "selinux").mkdir(parents=True)
    (kdir / "lustre-artifacts" / "lustre.ko").write_text("x")
    images = tmp_path / "images"
    images.mkdir()
    (images / "base-1.ext4").write_bytes(b"a" * 10)
    (images / "base-2.ext4").write_bytes(b"b" * 10)
    cur = images / "current.ext4"
    cur.symlink_to("base-1.ext4")
    before = rb.payload_digest(kdir, cur, ["selinux"])
    # a variant's snapshot appearing is not a change to the base build
    (kdir / "lustre-artifacts" / "selinux" / "lustre.ko").write_text("v")
    assert rb.payload_digest(kdir, cur, ["selinux"]) == before
    # another image installed in its place is
    cur.unlink()
    cur.symlink_to("base-2.ext4")
    assert rb.payload_digest(kdir, cur, ["selinux"]) != before


def test_a_build_that_failed_its_check_is_forced(tmp_path: Path) -> None:
    runner = _runner(1)
    a, b = rb.Build("rocky9", "k"), rb.Build("rocky10", "k")
    argvs: dict[str, list[str]] = {}

    def fake_run(slot: int, argv: list[str], log: Path) -> int:
        argvs[argv[2]] = argv
        return 1

    with patch.object(runner, "run", side_effect=fake_run):
        rb.build_all(
            [a, b],
            1,
            runner,
            [tmp_path],
            tmp_path,
            "h",
            lambda x, k: None,
            force={b},
        )
    assert "--force" not in argvs["rocky9"]
    assert argvs["rocky10"][-1] == "--force"


def test_an_unfinished_attempt_is_forced_on_resume(
    lustre_repo: Path, tmp_path: Path
) -> None:
    from ltvm_pkg.cli.release import cmd_build_release

    t = TestCommand()
    rb.Ledger(tmp_path / "work" / "done-x86_64.json").start(
        "x86_64", rb.Build("rocky9", "5.14-rhel9.8")
    )
    with (
        t._patches([]),
        patch.object(rb, "disk_problems", return_value=[]),
        patch.object(rb, "preflight", return_value={}),
        patch.object(rb, "build_all", return_value={}) as ball,
        patch("ltvm_pkg.cli.release._ltvm_version", return_value="0.5.1+abc"),
        patch("ltvm_pkg.cli.release._new_targets", return_value=0),
    ):
        cmd_build_release(t._args(lustre_repo, tmp_path, dry_run=False))
    assert ball.call_args.kwargs["force"] == {
        rb.Build("rocky9", "5.14-rhel9.8")
    }


def test_payload_covers_the_kernel(tmp_path: Path) -> None:
    (tmp_path / "build-tree" / "include").mkdir(parents=True)
    (tmp_path / "build-tree" / "include" / "autoconf.h").write_text(
        "#define X 1\n"
    )
    (tmp_path / "vmlinuz").write_bytes(b"kernel-1")
    (tmp_path / "vmlinux").write_bytes(b"elf")
    before = rb.payload_digest(tmp_path)
    (tmp_path / "vmlinuz").write_bytes(b"kernel-2")  # same length
    assert rb.payload_digest(tmp_path) != before
    (tmp_path / "vmlinuz").write_bytes(b"kernel-1")
    assert rb.payload_digest(tmp_path) == before
    (tmp_path / "build-tree" / "include" / "autoconf.h").unlink()
    assert rb.payload_digest(tmp_path) != before


def test_impossible_ext4_geometry_is_not_whole(tmp_path: Path) -> None:
    import struct

    from ltvm_pkg.paths import ext4_whole
    from tests.conftest import fake_ext4

    data = bytearray(fake_ext4(8))
    struct.pack_into("<I", data, 1024 + 24, 0xFFFFFFFF)
    (tmp_path / "img").write_bytes(bytes(data))
    assert not ext4_whole(tmp_path / "img")


def test_an_interrupted_preparation_is_cleaned_on_retry(
    lustre_repo: Path, tmp_path: Path
) -> None:
    head = rb.lustre_head(lustre_repo)
    (tree,) = rb.prepare_trees(lustre_repo, head, tmp_path / "w", 1)
    (tree / "config.status").write_text("old configure\n")
    (lustre_repo / "README").write_text("next\n")
    _git(
        lustre_repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-qam",
        "next",
    )
    new = rb.lustre_head(lustre_repo)
    real = rb._git

    def interrupted(t: Path, *args: str) -> str:
        if args[:1] == ("clean",):
            raise KeyboardInterrupt
        return real(t, *args)

    with patch.object(rb, "_git", side_effect=interrupted):
        with pytest.raises(KeyboardInterrupt):
            rb.prepare_trees(lustre_repo, new, tmp_path / "w", 1)
    # HEAD did not move before the clean, so the retry cleans
    assert _git(tree, "rev-parse", "HEAD") == head
    rb.prepare_trees(lustre_repo, new, tmp_path / "w", 1)
    assert _git(tree, "rev-parse", "HEAD") == new
    assert not (tree / "config.status").exists()


def test_a_target_with_moving_kernels_is_unmatched() -> None:
    rel = _rel("mainline-x86_64-7.0.8")
    builds, unmatched, _ = rb.published_builds(
        [rel],
        "x86_64",
        {"mainline": ["latest", "stable", "longterm", "7.0", "6.18"]},
        lambda t, s, a: "7.0",
        _is_base,
    )
    assert builds == [] and unmatched == ["mainline-x86_64-7.0.8"]


def test_damage_since_an_older_build_is_forced(
    lustre_repo: Path, tmp_path: Path
) -> None:
    from ltvm_pkg.cli.release import cmd_build_release

    t = TestCommand()
    b = rb.Build("rocky9", "5.14-rhel9.8")
    led = rb.Ledger(tmp_path / "work" / "done-x86_64.json")
    led.record("x86_64", b, "0.5.0+old", "oldcommit", "kd", "payload-then")

    def common() -> tuple[Any, ...]:
        # Fresh each time: _patches() hands back an entered ExitStack,
        # and one reused after its first exit no longer patches the
        # GitHub API, so the second pass reached the live one.
        return (
            t._patches([]),
            patch.object(rb, "disk_problems", return_value=[]),
            patch.object(rb, "preflight", return_value={}),
            patch(
                "ltvm_pkg.cli.release._ltvm_version", return_value="0.5.1+abc"
            ),
            patch("ltvm_pkg.cli.release._new_targets", return_value=0),
        )

    for payload_now, forced in (("payload-then", set()), ("payload-now", {b})):
        import contextlib

        with contextlib.ExitStack() as st:
            for c in common():
                st.enter_context(c)
            st.enter_context(
                patch.object(rb, "build_payload", return_value=payload_now)
            )
            ball = st.enter_context(
                patch.object(rb, "build_all", return_value={})
            )
            cmd_build_release(t._args(lustre_repo, tmp_path, dry_run=False))
        assert ball.call_args.kwargs["force"] == forced
