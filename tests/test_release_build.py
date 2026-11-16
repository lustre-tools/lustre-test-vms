"""ltvm build release: what it plans, how it schedules, what it refuses."""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from ltvm_pkg import release_build as rb
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
        builds, unmatched = rb.published_builds(
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
        builds, _ = rb.published_builds(
            RELEASES, "aarch64", DECLARED, _kernel_of, _is_base
        )
        assert [b.name for b in builds] == [
            "rocky10:6.12-rhel10.1",
            "rocky10-16k:6.12-rhel10.2",
        ]

    def test_variant_only_release_is_not_built(self) -> None:
        builds, _ = rb.published_builds(
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
        builds, _ = rb.published_builds(
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


class TestDisk:
    def test_one_filesystem_sums_every_need(self, tmp_path: Path) -> None:
        free = SimpleNamespace(free=100 * rb.GIB)
        problems = rb.disk_problems(
            tmp_path / "a",
            tmp_path / "s",
            tmp_path / "w",
            2,
            10,
            usage=lambda p: free,
            tmp=tmp_path / "t",
        )
        # 10 builds * 6 + 2 slots * (25 + 12 + 2) + 10 margin = 148
        assert len(problems) == 1
        assert "100 GiB free, about 148 GiB needed" in problems[0]
        for what in ("artifacts", "podman storage", "temporary files"):
            assert what in problems[0]

    def test_nothing_to_build_needs_nothing(self, tmp_path: Path) -> None:
        free = SimpleNamespace(free=0)
        assert (
            rb.disk_problems(
                tmp_path, tmp_path, tmp_path, 4, 0, usage=lambda p: free
            )
            == []
        )

    def test_slots_beyond_the_builds_reserve_nothing(
        self, tmp_path: Path
    ) -> None:
        free = SimpleNamespace(free=50 * rb.GIB)
        # 1 build: 6 + one slot of 39 + 10 margin = 55, not four slots
        (problem,) = rb.disk_problems(
            tmp_path,
            tmp_path,
            tmp_path,
            4,
            1,
            usage=lambda p: free,
            tmp=tmp_path,
        )
        assert "about 55 GiB needed" in problem

    def test_podman_machine_store_is_not_checked(self, tmp_path: Path) -> None:
        free = SimpleNamespace(free=1000 * rb.GIB)
        seen: list[Path] = []

        def usage(p: Path) -> Any:
            seen.append(p)
            return free

        assert (
            rb.disk_problems(
                tmp_path, None, tmp_path, 1, 1, usage=usage, tmp=tmp_path
            )
            == []
        )
        with patch.object(rb.sys, "platform", "darwin"):
            assert rb.podman_store() is None

    def test_enough(self, tmp_path: Path) -> None:
        free = SimpleNamespace(free=1000 * rb.GIB)
        assert (
            rb.disk_problems(
                tmp_path, tmp_path, tmp_path, 4, 12, usage=lambda p: free
            )
            == []
        )


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
        assert again.done("x86_64", b, "v1", "abc") == "5.14-rhel9.8-x"
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

    def outputs_complete(self, artifact: str, kernel: str) -> bool:
        return self.complete[artifact]

    def is_stale(self, artifact: str, kernel: str, extra_hash: bytes) -> bool:
        self.extra = extra_hash
        return self.stale


class TestVerify:
    def _setup(self, tmp_path: Path) -> tuple[_FakeTC, Path]:
        kdir = tmp_path / "kernels" / "5.14-rhel9.8-x"
        snap = kdir / "lustre-artifacts"
        (snap / "lib").mkdir(parents=True)
        (kdir / "meta.json").write_text("{}")
        (snap / "lib" / "lustre.ko").write_text("x")
        (snap / ".ltvm-snapshot.json").write_text(
            json.dumps({"lustre_commit": "abc123"})
        )
        (snap / ".ltvm-staging-meta.json").write_text(
            json.dumps(
                {"module_symvers_sha256": "s1", "lustre_modules_sha256": "l1"}
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
        assert tc.extra is not None and b"lustre:|l1" in tc.extra

    def test_other_commit(self, tmp_path: Path) -> None:
        tc, _ = self._setup(tmp_path)
        assert self._verify(tc, "def456") == [
            "Lustre snapshot is from abc123, not def456"
        ]

    def test_incomplete_kernel(self, tmp_path: Path) -> None:
        tc, _ = self._setup(tmp_path)
        tc.complete["kernel"] = False
        assert self._verify(tc)[0].endswith("is missing or incomplete")

    def test_snapshot_without_modules(self, tmp_path: Path) -> None:
        tc, snap = self._setup(tmp_path)
        (snap / "lib" / "lustre.ko").unlink()
        assert self._verify(tc)[0].endswith("has no modules")

    def test_image_without_this_lustre(self, tmp_path: Path) -> None:
        tc, snap = self._setup(tmp_path)
        tc.stale = True
        assert self._verify(tc) == [
            "image does not match its inputs and this Lustre"
        ]
        tc.stale = False
        (snap / ".ltvm-staging-meta.json").unlink()
        assert self._verify(tc) == [
            "image does not match its inputs and this Lustre"
        ]

    def test_no_image(self, tmp_path: Path) -> None:
        tc, _ = self._setup(tmp_path)
        tc.complete["image"] = False
        assert self._verify(tc) == ["no image"]


class TestWorkLock:
    def test_second_run_refused(self, tmp_path: Path) -> None:
        first = rb.lock_work_dir(tmp_path / "w")
        with pytest.raises(rb.ReleaseBuildError, match="another"):
            rb.lock_work_dir(tmp_path / "w")
        first.close()
        rb.lock_work_dir(tmp_path / "w").close()


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
            patch.object(rb, "podman_store", return_value=Path("/nonexistent"))
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
        from ltvm_pkg.cli.release import _ltvm_version, cmd_build_release

        head = _git(lustre_repo, "rev-parse", "HEAD")
        led = rb.Ledger(tmp_path / "work" / "done-x86_64.json")
        led.record(
            "x86_64",
            rb.Build("debian13", "6.12-debian13"),
            _ltvm_version(),
            head,
            "6.12-debian13-6.12.107",
        )

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
            patch.object(rb, "preflight", side_effect=fake_pre),
            patch.object(rb, "build_all", side_effect=fake_all) as ball,
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
        assert len(list((tmp_path / "work").glob("lustre-*"))) == 2


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


def test_no_ltvm_commit_means_no_ledger() -> None:
    from ltvm_pkg.cli import release

    with patch("ltvm_pkg.__version__", "0.5"):
        assert release._ltvm_version() is None
    with patch("ltvm_pkg.__version__", "0.5.367+da167a3"):
        assert release._ltvm_version() == "0.5.367+da167a3"
