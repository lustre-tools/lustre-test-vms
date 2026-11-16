"""``ltvm build release``: rebuild every published target on this host.

The work is in ltvm_pkg/release_build.py; this is the plan, the
report and the exit status.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ltvm_pkg import release_build as rb
from ltvm_pkg.cli.util import EXIT_ERROR, EXIT_OK, _error, host_arch

EXIT_INTERRUPTED = 130


def _cli_attr(name: str) -> Any:
    import ltvm_pkg.cli as cli

    return getattr(cli, name)


def _declared(arch: str) -> dict[str, list[str]]:
    from ltvm_pkg.target_config import TargetConfig, list_targets

    return {
        t: TargetConfig(t, arch=arch).declared_kernels() for t in list_targets()
    }


def _ltvm_version() -> str | None:
    """ltvm's version from its checkout's HEAD, or None when there is no
    clean commit to name it by: no git, or uncommitted changes.  Not
    the baked version, which a branch checkout does not refresh."""
    from ltvm_pkg.target_config import REPO_ROOT
    from ltvm_pkg.version_info import git_version

    v = git_version(REPO_ROOT)
    if v is None:
        return None
    r = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "status",
            "--porcelain",
            "--untracked-files=no",
        ],
        capture_output=True,
        text=True,
    )
    return (
        f"{v[0]} {v[1]}" if r.returncode == 0 and not r.stdout.strip() else None
    )


def _new_targets(targets: list[str], arch: str) -> int:
    """Targets podman lacks the build container or the image package
    layers for, which the preflight will build."""
    from ltvm_pkg.image_build import _container_image_tag
    from ltvm_pkg.target_config import TargetConfig

    n = 0
    for t in targets:
        try:
            tc = TargetConfig(t, arch=arch)
            tags = [tc.container_tag, _container_image_tag(tc)]
            missing = any(
                subprocess.run(
                    ["podman", "image", "exists", tag], capture_output=True
                ).returncode
                != 0
                for tag in tags
            )
            n += missing
        except (OSError, ValueError, KeyError):
            n += 1
    return n


def _new_caches(todo: list[rb.Build], arch: str, work: Path) -> int:
    """Targets with builds to run but no Lustre ccache in ``work`` yet
    (lustre_build keeps it beside the trees, named for the container)."""
    from ltvm_pkg.target_config import TargetConfig

    n = 0
    for t in dict.fromkeys(b.target for b in todo):
        try:
            tag = TargetConfig(t, arch=arch).container_tag
        except (ValueError, KeyError):
            n += 1
            continue
        n += not (
            work / ".ltvm-ccache" / tag.removeprefix("ltvm-build-")
        ).is_dir()
    return n


def _raise_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def cmd_build_release(args: argparse.Namespace) -> int:
    from ltvm_pkg.target_config import ARTIFACTS_DIR

    use_json = args.json
    out = sys.stderr if use_json else sys.stdout

    def say(msg: str) -> None:
        print(msg, file=out, flush=True)

    arch = args.arch or host_arch()
    tree, err = _cli_attr("_resolve_lustre_tree")(args.lustre_tree)
    if err:
        return _error(
            err, use_json, hint="pass --lustre-tree /path/to/lustre-release"
        )
    assert tree is not None
    try:
        head = rb.lustre_head(tree)
        releases = _cli_attr("_gh_api")("releases")
    except (rb.ReleaseBuildError, RuntimeError) as e:
        return _error(str(e), use_json)
    if not isinstance(releases, list):
        releases = [releases]

    from ltvm_pkg.release_package import _declared_variant_names

    def is_base(target: str, a: str, stem: str) -> bool:
        return bool(
            _cli_attr("_is_base_stem")(stem, _declared_variant_names(target, a))
        )

    try:
        declared = _declared(arch)
    except Exception as e:  # noqa: BLE001 -- any targets.yaml error
        return _error(f"targets.yaml: {e}", use_json)
    builds, unmatched = rb.published_builds(
        releases,
        arch,
        declared,
        _cli_attr("_fetched_kernel_name"),
        is_base,
    )
    builds = rb.filter_builds(builds, args.only)
    if not builds:
        return _error(
            f"nothing published for {arch}"
            + (f" matches --only {' '.join(args.only)}" if args.only else ""),
            use_json,
        )
    order = rb.breadth_first(builds)

    work = (
        Path(args.work_dir).resolve()
        if args.work_dir
        else ARTIFACTS_DIR / ".release-build"
    )
    locks: list[Any] = []
    old_term = None
    old_int = signal.getsignal(signal.SIGINT)
    if not args.dry_run:
        # The finished builds are decided under the locks: another run
        # could replace what they verify before these were taken.
        old_term = signal.signal(signal.SIGTERM, _raise_interrupt)
        # Launched in the background, SIGINT can arrive ignored, and the
        # builds would inherit that and skip their cleanup when stopped.
        if old_int is signal.SIG_IGN:
            signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            for path, why in rb.lock_paths(
                ARTIFACTS_DIR, work, rb.podman_store()
            ):
                locks.append(rb.lock_dir(path, why))
        except rb.ReleaseBuildError as e:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
            for f in locks:
                f.close()
            return _error(str(e), use_json)
    try:
        return _plan_and_run(
            args, arch, tree, head, work, order, unmatched, use_json, say
        )
    finally:
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
        for f in locks:
            f.close()


def _plan_and_run(
    args: argparse.Namespace,
    arch: str,
    tree: Path,
    head: str,
    work: Path,
    order: list[rb.Build],
    unmatched: list[str],
    use_json: bool,
    say: Any,
) -> int:
    from ltvm_pkg.target_config import ARTIFACTS_DIR

    ltvm_version = _ltvm_version()
    ledger = rb.Ledger(work / f"done-{arch}.json")
    done: dict[rb.Build, str] = {}
    for b in order:
        if args.fresh or not ltvm_version:
            break
        kdir = ledger.done(arch, b, ltvm_version, head)
        if kdir is not None and not rb.verify(b, arch, head, kdir):
            done[b] = kdir
    todo = [b for b in order if b not in done]
    targets = list(dict.fromkeys(b.target for b in order))

    avail = rb.available_cpus()
    if args.cpus and avail is None:
        return _error("--cpus needs Linux and taskset", use_json)
    n_sets = len(args.cpus.split(":")) if args.cpus else None
    if args.jobs and n_sets and args.jobs != n_sets:
        return _error(
            f"--cpus gives {n_sets} CPU sets for --jobs {args.jobs}", use_json
        )
    jobs = args.jobs or n_sets or rb.default_jobs(avail)
    # No more slots than builds left to run.  Sets given by hand keep
    # their meaning; automatic ones are shared out over the slots used.
    used = max(1, min(jobs, len(todo)))
    try:
        if args.cpus:
            cpus = rb.cpu_sets(jobs, args.cpus, avail, [])[:used]
        else:
            cpus = rb.cpu_sets(used, None, avail, rb.numa_nodes())
    except (rb.ReleaseBuildError, ValueError) as e:
        return _error(str(e), use_json)
    jobs = used
    store = rb.podman_store()
    disk = rb.disk_problems(
        [ARTIFACTS_DIR / b.target / arch for b in todo],
        store,
        work,
        jobs,
        len(todo),
        new_targets=_new_targets(targets, arch) if store else 0,
        preflight_slots=0 if args.no_preflight else min(jobs, len(targets)),
        new_caches=_new_caches(todo, arch, work),
    )

    plan: dict[str, Any] = {
        "arch": arch,
        "lustre_tree": str(tree),
        "lustre_commit": head,
        "ltvm": ltvm_version,
        "jobs": jobs,
        "cpus": [rb.format_cpulist(c) if c else None for c in cpus],
        "builds": [b.name for b in order],
        "already_done": [b.name for b in done],
        "unmatched_releases": unmatched,
        "artifacts": str(ARTIFACTS_DIR),
        "podman_store": str(store) if store else None,
        "work_dir": str(work),
        "disk_problems": disk,
    }
    say(
        f"Release build for {arch}: Lustre {head[:12]} from {tree}, "
        f"ltvm {ltvm_version or '(no clean commit, so nothing is skipped)'}"
    )
    say(
        f"  artifacts {ARTIFACTS_DIR}, podman storage "
        f"{store or '(in the podman machine; not checked)'}, work {work}"
    )
    for i, c in enumerate(cpus):
        say(
            f"  slot {i}: "
            + (f"cpus {rb.format_cpulist(c)}" if c else "unpinned")
        )
    say(f"  {len(order)} builds, {len(done)} already done:")
    for b in order:
        say(f"    {b.name}" + ("  (done)" if b in done else ""))
    for tag in unmatched:
        say(
            f"  WARNING: release {tag} matches no kernel in targets.yaml; not built"
        )
    for p in disk:
        say(f"  NOT ENOUGH DISK: {p}")

    if args.dry_run:
        if use_json:
            print(json.dumps(plan, indent=2))
        return EXIT_ERROR if disk else EXIT_OK
    if disk:
        return _error(
            "not enough disk for this build",
            use_json,
            hint="use fewer --jobs or --only, `ltvm clean`, or move podman's "
            "storage with CONTAINERS_STORAGE_CONF (docs/RELEASING.md)",
        )
    log_dir = (
        Path(args.log_dir).resolve()
        if args.log_dir
        else work / "logs" / time.strftime("%Y%m%d-%H%M%S")
    )
    try:
        trees = rb.prepare_trees(tree, head, work, jobs) if todo else []
    except rb.ReleaseBuildError as e:
        return _error(str(e), use_json)
    say(f"  logs in {log_dir}")
    return _run(
        args,
        arch,
        head,
        ltvm_version,
        ledger,
        order,
        done,
        todo,
        targets,
        jobs,
        cpus,
        trees,
        log_dir,
        plan,
        say,
    )


def _run(
    args: argparse.Namespace,
    arch: str,
    head: str,
    ltvm_version: str | None,
    ledger: rb.Ledger,
    order: list[rb.Build],
    done: dict[rb.Build, str],
    todo: list[rb.Build],
    targets: list[str],
    jobs: int,
    cpus: list[list[int] | None],
    trees: list[Path],
    log_dir: Path,
    plan: dict[str, Any],
    say: Any,
) -> int:
    from ltvm_pkg.target_config import REPO_ROOT

    runner = rb.Runner(
        ltvm=[sys.executable, str(REPO_ROOT / "ltvm")],
        arch=arch,
        cpus=cpus,
        say=say,
    )
    t0 = time.monotonic()
    pre: dict[str, rb.Result] = {}
    if not args.no_preflight:
        say(
            f"{time.strftime('%H:%M:%S')} preflight: containers and image packages"
        )
        firsts = [next(b for b in order if b.target == t) for t in targets]
        pre = rb.preflight(firsts, jobs, runner, log_dir)
    bad = {t: r for t, r in pre.items() if r.status != "ok"}
    runnable = [b for b in todo if b.target not in bad]
    res: dict[rb.Build, rb.Result] = {}
    if runnable and not runner.stopping.is_set():
        res = rb.build_all(
            runnable,
            jobs,
            runner,
            trees,
            log_dir,
            head,
            force=args.fresh,
            on_start=lambda b: ledger.forget(arch, b),
            on_ok=lambda b, kdir: (
                ledger.record(arch, b, ltvm_version, head, kdir)
                if ltvm_version
                else None
            ),
        )

    results: list[rb.Result] = []
    for b in order:
        if b.target in bad:
            r = bad[b.target]
            results.append(
                rb.Result(
                    b,
                    "skipped",
                    problems=[f"preflight failed at {r.step}: {r.log}"],
                )
            )
        elif b in done:
            results.append(rb.Result(b, "done", kernel_dir=done[b]))
        elif b in res:
            results.append(res[b])
        else:
            results.append(rb.Result(b, "skipped", problems=["not run"]))

    summary = {
        **plan,
        "log_dir": str(log_dir),
        "minutes": round((time.monotonic() - t0) / 60),
        "interrupted": runner.stopping.is_set(),
        "results": [r.as_dict() for r in results],
    }
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    say("")
    say(rb.summary_table(results))
    ok = sum(r.status in ("ok", "done") for r in results)
    say(
        f"\n{ok} of {len(results)} good in {summary['minutes']} minutes; "
        f"{log_dir / 'summary.json'}"
    )
    if args.json:
        print(json.dumps(summary, indent=2))
    if runner.stopping.is_set():
        return EXIT_INTERRUPTED
    return EXIT_OK if ok == len(results) else EXIT_ERROR
