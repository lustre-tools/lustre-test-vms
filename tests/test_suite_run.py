"""ltvm suite: the run.sh it writes, where it runs, how it reads a run's
state and its results.  ssh is mocked throughout; the shell it generates
is run here only against a scratch directory standing in for the guest."""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ltvm_pkg import suite_run
from ltvm_pkg.vm_state import ClusterInfo, ClusterNotFound, VMNotFound
from tests.host_marks import needs_gnu_sed

FIXTURE = Path(__file__).parent / "fixtures" / "suite" / "results.yml"
RID = "sanity-20261008-120000"


def _auster_line(script: str) -> str:
    return next(x for x in script.splitlines() if "./auster" in x)


# -- run.sh ---------------------------------------------------------------


class TestRunScript:
    def test_default_recipe(self) -> None:
        s = suite_run.run_script(RID, "/usr/lib64/lustre", "sanity")
        lines = s.splitlines()
        d = f"/root/ltvm-suite/{RID}"
        assert lines[0] == "#!/bin/bash"
        assert lines[1] == f"echo $$ > {d}/pid"
        assert lines[2].startswith("cd /usr/lib64/lustre/tests ||")
        assert "export LUSTRE=/usr/lib64/lustre" in lines
        assert _auster_line(s) == (f"./auster -k -v -r -H -d {d}/logs sanity")
        assert lines[-1] == f"echo rc=$? > {d}/done"

    def test_unmounts_and_clears_cleanup_dm_dev_before_auster(self) -> None:
        lines = suite_run.run_script(RID, "/usr/lib64/lustre", "sanity")
        lines_l = lines.splitlines()
        sed = next(i for i, x in enumerate(lines_l) if "CLEANUP_DM_DEV" in x)
        cleanup = next(
            i for i, x in enumerate(lines_l) if "llmountcleanup.sh" in x
        )
        auster = next(i for i, x in enumerate(lines_l) if "./auster" in x)
        assert sed < cleanup < auster
        assert "CLEANUP_DM_DEV=false" in lines_l[sed]
        assert "cfg/local.sh" in lines_l[sed]

    def test_no_reformat_keeps_h(self) -> None:
        s = suite_run.run_script(
            RID, "/usr/lib64/lustre", "sanity", reformat=False
        )
        argv = shlex.split(_auster_line(s))
        assert "-r" not in argv
        assert "-H" in argv

    def test_slow_is_a_flag_and_the_suite_stays_positional(self) -> None:
        s = suite_run.run_script(RID, "/usr/lib64/lustre", "sanity", slow=True)
        argv = shlex.split(_auster_line(s))
        assert argv[:6] == ["./auster", "-k", "-v", "-s", "-r", "-H"]
        assert argv[argv.index("-s") + 1] != "sanity"
        assert argv[-1] == "sanity"

    def test_only_is_one_quoted_word(self) -> None:
        s = suite_run.run_script(
            RID, "/usr/lib64/lustre", "sanity", only="42a 39r"
        )
        line = _auster_line(s)
        assert line.endswith("sanity --only '42a 39r'")
        argv = shlex.split(line)
        assert argv[-2:] == ["--only", "42a 39r"]

    def test_env_prefixes_auster_quoted(self) -> None:
        s = suite_run.run_script(
            RID,
            "/usr/lib64/lustre",
            "sanity-sec",
            env=["SHARED_KEY=true", "ODD=a b;c"],
        )
        line = _auster_line(s)
        assert line.startswith("SHARED_KEY=true ODD='a b;c' ./auster ")

    def test_debian_libdir(self) -> None:
        s = suite_run.run_script(RID, "/usr/lib/lustre", "sanity")
        assert "cd /usr/lib/lustre/tests ||" in s

    def test_bad_env_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            suite_run.env_assignments(["NOEQUALS"])
        with pytest.raises(SystemExit):
            suite_run.env_assignments(["1X=y"])

    @pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
    @needs_gnu_sed
    def test_script_runs_the_recipe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Run the generated run.sh against a fake tests dir: the sed
        rewrites an old CLEANUP_DM_DEV=true, cleanup runs first, auster
        gets exactly the argv and env, and done records its status."""
        root = tmp_path / "suite"
        monkeypatch.setattr(suite_run, "SUITE_ROOT", str(root))
        libdir = tmp_path / "lustre"
        tests = libdir / "tests"
        (tests / "cfg").mkdir(parents=True)
        (tests / "cfg" / "local.sh").write_text(
            "OSTCOUNT=2\nCLEANUP_DM_DEV=true\n"
        )
        (tests / "llmountcleanup.sh").write_text(
            f"echo cleanup >> {tmp_path}/order\n"
        )
        auster = tests / "auster"
        auster.write_text(
            "#!/bin/bash\n"
            f"echo auster >> {tmp_path}/order\n"
            f'printf "%s\\n" "$@" > {tmp_path}/argv\n'
            f'echo "$SHARED_KEY" > {tmp_path}/env\n'
            "exit 3\n"
        )
        auster.chmod(0o755)
        d = root / RID
        d.mkdir(parents=True)
        script = suite_run.run_script(
            RID,
            str(libdir),
            "sanity",
            only="42a 39r",
            env=["SHARED_KEY=true"],
        )
        (d / "run.sh").write_text(script)
        subprocess.run(["bash", str(d / "run.sh")], check=True, timeout=30)

        assert (tests / "cfg" / "local.sh").read_text() == (
            "OSTCOUNT=2\nCLEANUP_DM_DEV=false\n"
        )
        assert (tmp_path / "order").read_text() == "cleanup\nauster\n"
        assert (tmp_path / "argv").read_text().splitlines() == [
            "-k",
            "-v",
            "-r",
            "-H",
            "-d",
            f"{d}/logs",
            "sanity",
            "--only",
            "42a 39r",
        ]
        assert (tmp_path / "env").read_text() == "true\n"
        assert (d / "done").read_text() == "rc=3\n"
        assert (d / "pid").read_text().strip().isdigit()


class TestLaunchCommand:
    def test_detached_under_a_session_keyring(self) -> None:
        cmd = suite_run.launch_command(RID)
        d = f"/root/ltvm-suite/{RID}"
        assert (
            f"setsid nohup keyctl session - bash {d}/run.sh "
            f"> {d}/out 2>&1 < /dev/null &"
        ) in cmd
        # a fresh directory: an existing run is never overwritten
        assert f"\nmkdir {d}\n" in cmd
        assert f"cat > {d}/run.sh" in cmd
        assert f"date +%s > {d}/started" in cmd


# -- where it runs ----------------------------------------------------------


def _cluster(*nodes: tuple[str, list[str]]) -> ClusterInfo:
    return ClusterInfo(
        name="co9", nodes=[{"name": n, "roles": r} for n, r in nodes]
    )


class TestTarget:
    def test_local_node_is_the_first_client(self) -> None:
        c = _cluster(
            ("co9-mds", ["mgs", "mds"]),
            ("co9-oss", ["oss"]),
            ("co9-c1", ["client"]),
            ("co9-c2", ["client"]),
        )
        assert c.local_node().name == "co9-c1"

    def test_local_node_without_clients_is_the_mgs(self) -> None:
        c = _cluster(("co9-oss", ["oss"]), ("co9-mds", ["mgs", "mds"]))
        assert c.local_node().name == "co9-mds"

    def test_cluster_runs_on_its_first_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = _cluster(
            ("co9-mds", ["mgs", "mds"]),
            ("co9-oss", ["oss"]),
            ("co9-c1", ["client"]),
        )
        monkeypatch.setattr(suite_run.ClusterInfo, "load", lambda name: c)
        t = suite_run.resolve_target("co9")
        assert (t.kind, t.exec_node) == ("cluster", "co9-c1")
        assert t.nodes == ["co9-mds", "co9-oss", "co9-c1"]

    def test_single_vm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_cluster(name: str) -> ClusterInfo:
            raise ClusterNotFound(name)

        monkeypatch.setattr(suite_run.ClusterInfo, "load", no_cluster)
        monkeypatch.setattr(
            suite_run.VMInfo, "load", lambda name: SimpleNamespace(ip="x")
        )
        t = suite_run.resolve_target("co1-single")
        assert (t.kind, t.exec_node, t.nodes) == (
            "vm",
            "co1-single",
            ["co1-single"],
        )

    def test_neither(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_cluster(name: str) -> ClusterInfo:
            raise ClusterNotFound(name)

        def no_vm(name: str) -> Any:
            raise VMNotFound(name)

        monkeypatch.setattr(suite_run.ClusterInfo, "load", no_cluster)
        monkeypatch.setattr(suite_run.VMInfo, "load", no_vm)
        with pytest.raises(suite_run.TargetNotFound):
            suite_run.resolve_target("nope")


# -- results.yml ------------------------------------------------------------


class TestResults:
    def test_parse(self) -> None:
        suites = suite_run.parse_results(FIXTURE.read_text())
        assert [s["name"] for s in suites] == ["sanity", "sanity-compr"]
        sanity = suites[0]
        assert sanity["status"] == "FAIL"
        assert sanity["duration"] == "62"
        by_name = {t["name"]: t for t in sanity["subtests"]}
        assert by_name["test_39r"] == {
            "name": "test_39r",
            "status": "FAIL",
            "duration": "12",
            "return_code": "1",
            "error": "atime not updated on disk",
        }
        assert by_name["test_78"]["error"] == "local OST"

    def test_nested_suite_status_goes_to_the_outer_test(self) -> None:
        compr = suite_run.parse_results(FIXTURE.read_text())[1]
        subs = compr["subtests"]
        assert [t["name"] for t in subs] == [
            "test_sanity",
            "test_setup",
            "test_cleanup",
            "test_1000",
            "test_2",
        ]
        assert subs[0]["status"] == "PASS"
        assert subs[0]["duration"] == "6"
        assert subs[2]["duration"] == "0"
        assert "status" not in subs[4]

    def test_summarize(self, tmp_path: Path) -> None:
        logs = tmp_path / "logs" / "2026-10-08" / "120000"
        logs.mkdir(parents=True)
        shutil.copy(FIXTURE, logs / "results.yml")
        s = suite_run.summarize(tmp_path)
        assert s["counts"] == {"PASS": 6, "FAIL": 1, "SKIP": 2}
        assert [(f["suite"], f["name"]) for f in s["failures"]] == [
            ("sanity", "test_39r")
        ]
        assert [(u["suite"], u["name"]) for u in s["unfinished"]] == [
            ("sanity-compr", "test_2")
        ]
        assert s["results_files"] == [str(logs / "results.yml")]

    def test_summarize_without_results(self, tmp_path: Path) -> None:
        s = suite_run.summarize(tmp_path)
        assert s["counts"] == {} and s["results_files"] == []

    def test_rsync_skips_debug_logs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            suite_run.VMInfo,
            "load",
            lambda name: SimpleNamespace(ip="10.0.0.9"),
        )
        t = suite_run.SuiteTarget("co1", "vm", "co1", ["co1"])
        argv = suite_run.rsync_argv(t, RID, tmp_path)
        assert argv[argv.index("--exclude") + 1] == "*.debug_log.*"
        assert argv[-2] == f"root@10.0.0.9:/root/ltvm-suite/{RID}/"
        assert argv[-1] == f"{tmp_path}/"
        argv = suite_run.rsync_argv(t, RID, tmp_path, debug_logs=True)
        assert "--exclude" not in argv

    def test_collect_counts_the_debug_logs_it_left(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(
            suite_run.VMInfo,
            "load",
            lambda name: SimpleNamespace(ip="10.0.0.9"),
        )
        monkeypatch.setattr(
            suite_run.subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
        )
        finds: list[str] = []

        def fake_ssh(node: str, command: str, **kw: Any) -> Any:
            finds.append(command)
            out = f"{3 * 2**20}\n{2**20}\n"
            return subprocess.CompletedProcess([], 0, out, "")

        monkeypatch.setattr(suite_run, "_ssh", fake_ssh)
        t = suite_run.SuiteTarget("co1", "vm", "co1", ["co1"])
        skipped = suite_run.collect(t, RID, tmp_path)
        assert skipped == (2, 4 * 2**20)
        assert "*.debug_log.*" in finds[0]
        suite_run.print_summary(
            suite_run.RunStatus(RID, "done", rc=0),
            suite_run.summarize(tmp_path),
            tmp_path,
            skipped,
        )
        out = capsys.readouterr().out
        assert "2 debug log(s) (4 MiB) not copied; --debug-logs" in out

        finds.clear()
        assert suite_run.collect(t, RID, tmp_path, debug_logs=True) is None
        assert finds == []


# -- status -----------------------------------------------------------------


def _run(rc: str = "", alive: bool = False, started: int = 1000) -> dict:
    return {
        "id": RID,
        "rc": rc,
        "alive": alive,
        "started": started,
        "current": "",
    }


class TestClassify:
    def test_done(self) -> None:
        s = suite_run.classify(_run(rc="0"), 2000, {"co1": 5000})
        assert (s.state, s.rc, s.age) == ("done", 0, 1000)
        assert s.label() == "done rc=0"

    def test_running(self) -> None:
        s = suite_run.classify(_run(alive=True), 2000, {"co1": 5000})
        assert s.state == "running"
        assert s.rebooted == [] and s.warnings() == []

    def test_died(self) -> None:
        s = suite_run.classify(_run(), 2000, {"co1": 5000})
        assert s.state == "died"
        assert s.rebooted == []

    def test_died_in_a_reboot(self) -> None:
        s = suite_run.classify(_run(), 2000, {"co1": 300})
        assert s.state == "died"
        assert s.rebooted == ["co1"]
        assert "/var/crash" in s.warnings()[0]

    def test_a_server_rebooted_under_a_running_suite(self) -> None:
        s = suite_run.classify(
            _run(alive=True),
            2000,
            {"co2-c1": 9000, "co2-oss": 120, "co2-mds": None},
        )
        assert s.state == "running"
        assert s.rebooted == ["co2-oss"]
        assert s.unreachable == ["co2-mds"]

    def test_a_power_cycle_from_the_suite_is_not_a_crash(self) -> None:
        run = _run(alive=True) | {
            "boots": {"co2-mds": "a", "co2-oss": "b"},
            "powered": {"co2-mds"},
        }
        s = suite_run.classify(
            run,
            2000,
            {"co2-mds": 120, "co2-oss": 120},
            {"co2-mds": "x", "co2-oss": "y"},
        )
        assert s.rebooted == ["co2-oss"]

    def test_power_lines_count_from_the_start_of_the_run(self) -> None:
        out = (
            "node\t2000\t9000\tid\n"
            "power\t900\tco2-oss\n"
            "power\t1500\tco2-mds\n"
            "run\tsanity-a\t\t1\t1000\t\n"
        )
        runs = suite_run.parse_probe(out)[2]
        assert runs[0]["powered"] == {"co2-mds"}

    def test_a_reboot_after_the_run_finished_is_not_a_crash(self) -> None:
        s = suite_run.classify(_run(rc="1"), 2000, {"co1": 300})
        assert (s.state, s.rc, s.rebooted) == ("done", 1, [])

    def test_parse_probe(self) -> None:
        out = (
            "node\t2000\t5000\n"
            f"run\t{RID}\t\t1\t1000\t== sanity test 42a: x ===\n"
            "run\tsanity-old\t0\t0\t10\t\n"
        )
        now, up, runs = suite_run.parse_probe(out)
        assert (now, up) == (2000, 5000)
        assert runs[0] == {
            "id": RID,
            "rc": "",
            "alive": True,
            "started": 1000,
            "current": "== sanity test 42a: x ===",
            "boots": {},
            "powered": set(),
        }
        assert runs[1]["rc"] == "0" and not runs[1]["alive"]

    def test_parse_probe_boot_ids(self) -> None:
        out = (
            "node\t2000\t5000\tb-now\n"
            f"run\t{RID}\t\t1\t1000\t\tco2-c1=b-c1 co2-oss=b-oss \n"
        )
        assert suite_run.probe_boot_id(out) == "b-now"
        runs = suite_run.parse_probe(out)[2]
        assert runs[0]["boots"] == {"co2-c1": "b-c1", "co2-oss": "b-oss"}

    def test_a_node_booted_just_before_the_run_did_not_crash(self) -> None:
        """Regression: after `cluster start`, a clock step in the guest
        made the run look older than the nodes, and status called two
        healthy nodes crashed."""
        run = dict(_run(alive=True), boots={"co2-oss": "b1", "co2-c2": "b2"})
        s = suite_run.classify(
            run,
            9000,
            {"co2-oss": 120, "co2-c2": 120},
            {"co2-oss": "b1", "co2-c2": "b2"},
        )
        assert s.rebooted == [] and s.warnings() == []

    def test_a_new_boot_id_is_a_crash(self) -> None:
        run = dict(_run(), boots={"co2-oss": "b1", "co2-c2": "b2"})
        s = suite_run.classify(
            run,
            2000,
            {"co2-oss": 9000, "co2-c2": None},
            {"co2-oss": "b1-new", "co2-c2": None},
        )
        assert s.rebooted == ["co2-oss"]
        assert s.unreachable == ["co2-c2"]

    def test_launch_records_boot_ids(self, tmp_path: Path) -> None:
        cmd = suite_run.launch_command(RID, {"co2-c1": "b-c1", "co2-oss": "b2"})
        d = f"/root/ltvm-suite/{RID}"
        line = next(x for x in cmd.splitlines() if x.endswith("/boots"))
        line = line.replace(d, str(tmp_path))
        subprocess.run(["bash", "-c", line], check=True)
        assert (tmp_path / "boots").read_text() == "co2-c1=b-c1\nco2-oss=b2\n"

    @pytest.mark.skipif(
        not Path("/proc/self/cmdline").exists(), reason="needs /proc"
    )
    def test_probe_script_against_real_processes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "suite"
        monkeypatch.setattr(suite_run, "SUITE_ROOT", str(root))
        for rid in ("a-done", "b-running", "c-died"):
            (root / rid).mkdir(parents=True)
            (root / rid / "started").write_text("100\n")
        (root / "a-done" / "done").write_text("rc=0\n")
        (root / "a-done" / "pid").write_text("1\n")
        run_sh = root / "b-running" / "run.sh"
        run_sh.write_text("sleep 60\n")
        (root / "b-running" / "out").write_text(
            "== sanity test 1: one ==\n== sanity test 2: two ==\nnoise\n"
        )
        proc = subprocess.Popen(["bash", str(run_sh)])
        dead = subprocess.Popen(["true"])
        dead.wait()
        try:
            (root / "b-running" / "pid").write_text(f"{proc.pid}\n")
            (root / "c-died" / "pid").write_text(f"{dead.pid}\n")
            r = subprocess.run(
                ["bash", "-c", suite_run.probe_script(None)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            single = subprocess.run(
                ["bash", "-c", suite_run.probe_script("c-died")],
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            proc.kill()
            proc.wait()
        assert r.returncode == 0, r.stderr
        now, up, runs = suite_run.parse_probe(r.stdout)
        assert now and up is not None
        states = {x["id"]: suite_run.classify(x, now, {}).label() for x in runs}
        assert states == {
            "a-done": "done rc=0",
            "b-running": "running",
            "c-died": "died",
        }
        cur = next(x for x in runs if x["id"] == "b-running")["current"]
        assert cur == "== sanity test 2: two =="
        assert [x["id"] for x in suite_run.parse_probe(single.stdout)[2]] == [
            "c-died"
        ]

    def test_probe_with_no_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(suite_run, "SUITE_ROOT", str(tmp_path / "none"))
        r = subprocess.run(
            ["bash", "-c", suite_run.probe_script(None)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0
        assert suite_run.parse_probe(r.stdout)[2] == []


# -- the commands, with ssh mocked -------------------------------------------


class FakeGuest:
    """Answers the commands suite_run sends, per node."""

    def __init__(self, probe: str = "node\t2000\t5000\n") -> None:
        self.probe = probe
        self.calls: list[tuple[str, str, str | None]] = []

    def __call__(
        self,
        node: str,
        command: str,
        *,
        stdin: str | None = None,
        timeout: int = 60,
    ) -> subprocess.CompletedProcess:
        self.calls.append((node, command, stdin))
        out = ""
        if command.startswith("printf 'node"):
            out = self.probe
        elif command.startswith("cut -d. -f1 /proc/uptime"):
            out = "5000\n"
        return subprocess.CompletedProcess([], 0, out, "")


@pytest.fixture
def vm_target(monkeypatch: pytest.MonkeyPatch) -> suite_run.SuiteTarget:
    t = suite_run.SuiteTarget("co1-single", "vm", "co1-single", ["co1-single"])
    monkeypatch.setattr(suite_run, "resolve_target", lambda name: t)
    monkeypatch.setattr(
        suite_run.VMInfo,
        "load",
        lambda name: SimpleNamespace(
            ip="10.0.0.5",
            os_id="rocky9",
            mdt_disks=2,
            ost_disks=4,
            disk_size=3 << 30,
        ),
    )
    monkeypatch.setattr(suite_run, "is_running", lambda vm: True)
    import ltvm_pkg.deploy as dp
    import ltvm_pkg.vm_commands as vc

    monkeypatch.setattr(vc, "_os_family_for_vm", lambda vm, ctx="": "rhel")
    disks: list[tuple] = []
    monkeypatch.setattr(
        dp,
        "configure_test_disks",
        lambda ip, mdt, ost, disk_size_bytes=0, os_family="rhel": disks.append(
            (ip, mdt, ost, disk_size_bytes, os_family)
        ),
    )
    t.disks = disks  # type: ignore[attr-defined]
    return t


def _run_args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = dict(
        target="co1-single",
        suite="sanity",
        only=None,
        env=None,
        slow=False,
        no_reformat=False,
        wait=False,
        timeout=None,
        results_dir=None,
        json=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


class TestCmdRun:
    def test_launches_with_the_script_on_stdin(
        self,
        vm_target: suite_run.SuiteTarget,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        guest = FakeGuest()
        monkeypatch.setattr(suite_run, "_ssh", guest)
        claimed: list[list[str]] = []
        monkeypatch.setattr(
            suite_run.vm_claim,
            "require_all",
            lambda vms, verb: claimed.append(list(vms)),
        )
        rc = suite_run.cmd_suite_run(
            _run_args(only=["42a", "39r"], env=["SHARED_KEY=true"])
        )
        assert rc == 0
        assert claimed == [["co1-single"]]
        node, cmd, stdin = guest.calls[-1]
        assert node == "co1-single"
        assert "keyctl session -" in cmd
        assert stdin is not None
        line = _auster_line(stdin)
        assert line.startswith("SHARED_KEY=true ./auster -k -v -r -H -d ")
        assert line.endswith(" sanity --only '42a 39r'")
        run_id = capsys.readouterr().out.strip()
        assert run_id.startswith("sanity-") and run_id in cmd

    def test_writes_the_vm_disk_block_first(
        self, vm_target: suite_run.SuiteTarget, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A VM from a fetched image has no disk block until deploy."""
        guest = FakeGuest()
        monkeypatch.setattr(suite_run, "_ssh", guest)
        monkeypatch.setattr(
            suite_run.vm_claim, "require_all", lambda vms, verb: None
        )
        assert suite_run.cmd_suite_run(_run_args()) == 0
        assert vm_target.disks == [  # type: ignore[attr-defined]
            ("10.0.0.5", 2, 4, 3 << 30, "rhel")
        ]

    def test_refuses_a_cluster_never_deployed(
        self, vm_target: suite_run.SuiteTarget, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        t = suite_run.SuiteTarget(
            "co2", "cluster", "co2-c1", ["co2-mds", "co2-c1"]
        )
        monkeypatch.setattr(suite_run, "resolve_target", lambda name: t)
        calls: list[str] = []

        def no_block(node, command, *, stdin=None, timeout=60):  # type: ignore[no-untyped-def]
            calls.append(command)
            rc = 1 if command.startswith("grep -qF") else 0
            return subprocess.CompletedProcess([], rc, "", "")

        monkeypatch.setattr(suite_run, "_ssh", no_block)
        monkeypatch.setattr(
            suite_run.vm_claim, "require_all", lambda vms, verb: None
        )
        with pytest.raises(SystemExit):
            suite_run.cmd_suite_run(_run_args(target="co2"))
        assert not any("keyctl" in c for c in calls)

    def test_refuses_while_another_run_is_going(
        self, vm_target: suite_run.SuiteTarget, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guest = FakeGuest("node\t2000\t5000\nrun\tsanity-old\t\t1\t1000\t\n")
        monkeypatch.setattr(suite_run, "_ssh", guest)
        monkeypatch.setattr(
            suite_run.vm_claim, "require_all", lambda vms, verb: None
        )
        with pytest.raises(SystemExit):
            suite_run.cmd_suite_run(_run_args())
        assert not any("keyctl" in c for _, c, _ in guest.calls)

    def test_refuses_a_stopped_vm(
        self, vm_target: suite_run.SuiteTarget, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guest = FakeGuest()
        monkeypatch.setattr(suite_run, "_ssh", guest)
        monkeypatch.setattr(
            suite_run.vm_claim, "require_all", lambda vms, verb: None
        )
        monkeypatch.setattr(suite_run, "is_running", lambda vm: False)
        with pytest.raises(SystemExit) as e:
            suite_run.cmd_suite_run(_run_args())
        assert e.value.code == suite_run.EXIT_UNREACHABLE
        assert guest.calls == []

    def test_refuses_a_claimed_vm(
        self, vm_target: suite_run.SuiteTarget, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guest = FakeGuest()
        monkeypatch.setattr(suite_run, "_ssh", guest)

        def held(vms: list[str], verb: str) -> None:
            sys.exit(1)

        monkeypatch.setattr(suite_run.vm_claim, "require_all", held)
        with pytest.raises(SystemExit):
            suite_run.cmd_suite_run(_run_args())
        assert guest.calls == []

    def test_cluster_nodes_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        t = suite_run.SuiteTarget(
            "co9", "cluster", "co9-c1", ["co9-mds", "co9-c1"]
        )
        monkeypatch.setattr(suite_run, "resolve_target", lambda name: t)
        monkeypatch.setattr(
            suite_run.VMInfo, "load", lambda name: SimpleNamespace(name=name)
        )
        monkeypatch.setattr(
            suite_run, "is_running", lambda vm: vm.name != "co9-mds"
        )
        monkeypatch.setattr(
            suite_run.vm_claim, "require_all", lambda vms, verb: None
        )
        with pytest.raises(SystemExit) as e:
            suite_run.cmd_suite_run(_run_args(target="co9"))
        assert e.value.code == suite_run.EXIT_UNREACHABLE

    def test_timeout_needs_wait(self) -> None:
        with pytest.raises(SystemExit):
            suite_run.cmd_suite_run(_run_args(timeout=60))

    def test_bad_suite_name(self) -> None:
        with pytest.raises(SystemExit):
            suite_run.cmd_suite_run(_run_args(suite="../x"))

    def test_wait_collects_and_fails_on_a_failed_subtest(
        self,
        vm_target: suite_run.SuiteTarget,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(suite_run, "_ssh", FakeGuest())
        monkeypatch.setattr(
            suite_run.vm_claim, "require_all", lambda vms, verb: None
        )
        cwd = tmp_path / "work"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        states = iter(
            [
                [],  # the busy check before launch
                [suite_run.RunStatus(RID, "running", current="== t 1 ==")],
                [suite_run.RunStatus(RID, "done", rc=0)],
            ]
        )
        monkeypatch.setattr(
            suite_run, "query", lambda t, rid=None: next(states)
        )
        monkeypatch.setattr(suite_run.time, "sleep", lambda s: None)

        dests: list[Path] = []

        def fake_collect(
            t: Any, run_id: str, dest: Path, debug_logs: bool = False
        ) -> None:
            dests.append(dest)
            logs = dest / "logs" / "x"
            logs.mkdir(parents=True)
            shutil.copy(FIXTURE, logs / "results.yml")

        monkeypatch.setattr(suite_run, "collect", fake_collect)
        rc = suite_run.cmd_suite_run(_run_args(wait=True))
        assert rc == suite_run.EXIT_ERROR
        out = capsys.readouterr().out
        assert "== t 1 ==" in out
        assert "6 pass, 1 fail, 2 skip" in out
        assert "FAIL sanity test_39r (12s): atime not updated on disk" in out
        # Under the working directory, named in full at both ends.
        (dest,) = dests
        assert dest.parent == cwd / "ltvm-suite" / "co1-single"
        assert dest.is_absolute()
        assert out.count(str(dest)) == 2
        assert f"results will be collected to {dest}" in out

    def test_wait_with_results_dir(
        self,
        vm_target: suite_run.SuiteTarget,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import json

        monkeypatch.setattr(suite_run, "_ssh", FakeGuest())
        monkeypatch.setattr(
            suite_run.vm_claim, "require_all", lambda vms, verb: None
        )
        states = iter([[], [suite_run.RunStatus(RID, "done", rc=0)]])
        monkeypatch.setattr(
            suite_run, "query", lambda t, rid=None: next(states)
        )
        dests: list[Path] = []
        monkeypatch.setattr(
            suite_run,
            "collect",
            lambda t, rid, dest, debug_logs=False: dests.append(dest),
        )
        mine = tmp_path / "mine"
        rc = suite_run.cmd_suite_run(
            _run_args(wait=True, results_dir=str(mine), json=True)
        )
        assert rc == 0
        assert dests == [mine]
        cap = capsys.readouterr()
        assert json.loads(cap.out)["dest"] == str(mine)
        assert f"results will be collected to {mine}" in cap.err

    def test_results_dir_needs_wait(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            suite_run.cmd_suite_run(_run_args(results_dir=str(tmp_path)))

    def test_an_unwritable_results_dir_stops_the_run_before_it_starts(
        self,
        vm_target: suite_run.SuiteTarget,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        guest = FakeGuest()
        monkeypatch.setattr(suite_run, "_ssh", guest)
        monkeypatch.setattr(
            suite_run.vm_claim, "require_all", lambda vms, verb: None
        )
        a_file = tmp_path / "file"
        a_file.write_text("")
        with pytest.raises(SystemExit):
            suite_run.cmd_suite_run(
                _run_args(wait=True, results_dir=str(a_file / "sub"))
            )
        assert not any("keyctl" in c for _, c, _ in guest.calls)

    def test_without_wait_names_where_collect_puts_results(
        self,
        vm_target: suite_run.SuiteTarget,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import json

        monkeypatch.setattr(suite_run, "_ssh", FakeGuest())
        monkeypatch.setattr(
            suite_run.vm_claim, "require_all", lambda vms, verb: None
        )
        assert suite_run.cmd_suite_run(_run_args(json=True)) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["results_dir"] == str(
            suite_run.default_dest("co1-single", data["run_id"])
        )


class TestResultsPlace:
    def test_default_is_under_the_working_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
        monkeypatch.chdir(tmp_path)
        assert suite_run.default_dest("co2", RID) == (
            tmp_path / "ltvm-suite" / "co2" / RID
        )

    def test_state_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
        assert suite_run.state_root() == tmp_path / "st" / "ltvm" / "suite"
        monkeypatch.delenv("XDG_STATE_HOME")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert suite_run.state_root() == (
            tmp_path / ".local" / "state" / "ltvm" / "suite"
        )

    def test_explicit_dir_is_made_absolute(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert suite_run.results_dest("out", "co2", RID) == tmp_path / "out"
        assert suite_run.results_dest(None, "co2", RID) == (
            suite_run.default_dest("co2", RID)
        )

    def test_collected_dir_finds_the_working_directory_then_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
        cwd = tmp_path / "work"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        assert suite_run.collected_dir("co2", RID) is None
        state = suite_run.state_root() / "co2" / RID
        state.mkdir(parents=True)
        assert suite_run.collected_dir("co2", RID) == state
        here = cwd / "ltvm-suite" / "co2" / RID
        here.mkdir(parents=True)
        assert suite_run.collected_dir("co2", RID) == here

    def test_with_the_working_directory_gone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
        gone = tmp_path / "gone"
        gone.mkdir()
        monkeypatch.chdir(gone)
        gone.rmdir()
        assert suite_run.collected_dir("co2", RID) is None
        state = suite_run.state_root() / "co2" / RID
        state.mkdir(parents=True)
        assert suite_run.collected_dir("co2", RID) == state
        with pytest.raises(SystemExit):
            suite_run.default_dest("co2", RID)
        assert suite_run.results_dest("/r", "co2", RID) == Path("/r")


class TestCmdStatus:
    def test_lists_runs(
        self,
        vm_target: suite_run.SuiteTarget,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        guest = FakeGuest(
            "node\t4000\t200\n"
            "run\tsanity-a\t0\t0\t1000\t\n"
            "run\tsanity-b\t\t0\t3400\t== sanity test 5 ==\n"
        )
        monkeypatch.setattr(suite_run, "_ssh", guest)
        rc = suite_run.cmd_suite_status(
            argparse.Namespace(target="co1-single", run_id=None, json=False)
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "sanity-a" in out and "done rc=0" in out
        b = next(x for x in out.splitlines() if x.startswith("sanity-b"))
        assert "died" in b
        assert "co1-single rebooted" in out and "/var/crash" in out
        assert "collected to" not in out

    def test_names_where_runs_were_collected(
        self,
        vm_target: suite_run.SuiteTarget,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The working directory, and the state directory a run was
        collected to while it was the default."""
        import json

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
        cwd = tmp_path / "work"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        new = cwd / "ltvm-suite" / "co1-single" / "sanity-a"
        new.mkdir(parents=True)
        old = suite_run.state_root() / "co1-single" / "sanity-b"
        old.mkdir(parents=True)
        probe = (
            "node\t4000\t5000\n"
            "run\tsanity-a\t0\t0\t1000\t\n"
            "run\tsanity-b\t0\t0\t2000\t\n"
            "run\tsanity-c\t0\t0\t3000\t\n"
        )
        monkeypatch.setattr(suite_run, "_ssh", FakeGuest(probe))
        args = argparse.Namespace(target="co1-single", run_id=None, json=False)
        assert suite_run.cmd_suite_status(args) == 0
        out = capsys.readouterr().out
        assert f"collected to {new}" in out
        assert f"collected to {old}" in out
        assert out.count("collected to") == 2
        args.json = True
        assert suite_run.cmd_suite_status(args) == 0
        data = json.loads(capsys.readouterr().out)
        dirs = {r["run_id"]: r["results_dir"] for r in data["runs"]}
        assert dirs == {
            "sanity-a": str(new),
            "sanity-b": str(old),
            "sanity-c": None,
        }
        assert data["results_root"] == str(new.parent)

    def test_unknown_run(
        self, vm_target: suite_run.SuiteTarget, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(suite_run, "_ssh", FakeGuest())
        with pytest.raises(SystemExit) as e:
            suite_run.cmd_suite_status(
                argparse.Namespace(target="co1-single", run_id=RID, json=False)
            )
        assert e.value.code == suite_run.EXIT_NOT_FOUND


class TestCmdCollect:
    def test_json_summary(
        self,
        vm_target: suite_run.SuiteTarget,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import json

        monkeypatch.setattr(
            suite_run,
            "_ssh",
            FakeGuest(f"node\t2000\t5000\nrun\t{RID}\t0\t0\t1000\t\n"),
        )
        seen: list[list[str]] = []

        def fake_rsync(argv: list[str], **kw: Any) -> Any:
            seen.append(argv)
            logs = Path(argv[-1]) / "logs" / "x"
            logs.mkdir(parents=True)
            shutil.copy(FIXTURE, logs / "results.yml")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(suite_run.subprocess, "run", fake_rsync)
        dest = tmp_path / "out"
        rc = suite_run.cmd_suite_collect(
            argparse.Namespace(
                target="co1-single",
                run_id=RID,
                results_dir=str(dest),
                json=True,
            )
        )
        assert rc == 0
        assert seen[0][0] == "rsync"
        cap = capsys.readouterr()
        data = json.loads(cap.out)
        assert data["status"]["state"] == "done"
        assert data["counts"] == {"PASS": 6, "FAIL": 1, "SKIP": 2}
        assert data["failures"][0]["name"] == "test_39r"
        assert data["dest"] == str(dest)
        assert f"collecting {RID} to {dest}" in cap.err

    def test_default_is_the_working_directory(
        self,
        vm_target: suite_run.SuiteTarget,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(
            suite_run,
            "_ssh",
            FakeGuest(f"node\t2000\t5000\nrun\t{RID}\t0\t0\t1000\t\n"),
        )
        dests: list[Path] = []
        monkeypatch.setattr(
            suite_run,
            "collect",
            lambda t, rid, dest, debug_logs=False: dests.append(dest),
        )
        cwd = tmp_path / "work"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        rc = suite_run.cmd_suite_collect(
            argparse.Namespace(
                target="co1-single", run_id=RID, results_dir=None, json=False
            )
        )
        assert rc == 0
        assert dests == [cwd / "ltvm-suite" / "co1-single" / RID]
        out = capsys.readouterr().out
        assert out.splitlines()[0] == f"collecting {RID} to {dests[0]}"
        assert f"results in {dests[0]}" in out


def test_cli_maps_a_missing_target_to_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ltvm_pkg.cli import cmd_suite_status

    def missing(name: str) -> Any:
        raise suite_run.TargetNotFound(f"no VM or cluster named '{name}'")

    monkeypatch.setattr(suite_run, "resolve_target", missing)
    rc = cmd_suite_status(
        argparse.Namespace(target="nope", run_id=None, json=False)
    )
    assert rc == suite_run.EXIT_NOT_FOUND
