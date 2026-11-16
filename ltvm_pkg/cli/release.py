"""``ltvm build release``: rebuild every published target on this host.

The work is in ltvm_pkg/release_build.py; this is the plan, the
report and the exit status.
"""

from __future__ import annotations

import argparse
import json
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

    out: dict[str, list[str]] = {}
    for t in list_targets():
        try:
            out[t] = TargetConfig(t, arch=arch).declared_kernels()
        except Exception:  # noqa: BLE001 -- a target this arch lacks
            continue
    return out


def _ltvm_version() -> str | None:
    """ltvm's version with its commit, or None when it has no commit to
    give (a copy without git or a baked version), which would let the
    ledger mistake one ltvm for another."""
    import ltvm_pkg

    v = ltvm_pkg.__version__
    return v if "+" in v else None


def cmd_build_release(args: argparse.Namespace) -> int:
    from ltvm_pkg.target_config import ARTIFACTS_DIR, REPO_ROOT

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

    builds, unmatched = rb.published_builds(
        releases,
        arch,
        _declared(arch),
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
    ltvm_version = _ltvm_version()
    ledger = rb.Ledger(work / f"done-{arch}.json")
    done = []
    for b in order:
        kdir = (
            ledger.done(arch, b, ltvm_version, head) if ltvm_version else None
        )
        if kdir is not None and not rb.verify(b, arch, head, kdir):
            done.append(b)
    todo = [b for b in order if b not in done]

    avail = rb.available_cpus()
    if args.cpus and avail is None:
        return _error("--cpus needs Linux and taskset", use_json)
    n_sets = len(args.cpus.split(":")) if args.cpus else None
    if args.jobs and n_sets and args.jobs != n_sets:
        return _error(
            f"--cpus gives {n_sets} CPU sets for --jobs {args.jobs}", use_json
        )
    jobs = args.jobs or n_sets or rb.default_jobs(avail)
    try:
        cpus = rb.cpu_sets(jobs, args.cpus, avail, rb.numa_nodes())
    except (rb.ReleaseBuildError, ValueError) as e:
        return _error(str(e), use_json)
    # no more slots than builds left to run
    jobs = max(1, min(jobs, len(todo)))
    cpus = cpus[:jobs]
    store = rb.podman_store()
    disk = rb.disk_problems(ARTIFACTS_DIR, store, work, jobs, len(todo))

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
        f"ltvm {ltvm_version or '(commit unknown, so nothing is skipped)'}"
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
    if not todo:
        say("Nothing to build.")
        if use_json:
            print(json.dumps({**plan, "results": []}, indent=2))
        return EXIT_OK

    log_dir = (
        Path(args.log_dir).resolve()
        if args.log_dir
        else work / "logs" / time.strftime("%Y%m%d-%H%M%S")
    )
    try:
        work_lock = rb.lock_work_dir(work)
        trees = rb.prepare_trees(tree, head, work, jobs)
    except rb.ReleaseBuildError as e:
        return _error(str(e), use_json)
    say(f"  logs in {log_dir}")

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
        pre = rb.preflight(todo, jobs, runner, log_dir)
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
            on_ok=lambda b, kdir: (
                ledger.record(arch, b, ltvm_version, head, kdir)
                if ltvm_version
                else None
            ),
        )

    results: list[rb.Result] = []
    for b in order:
        if b in done:
            results.append(rb.Result(b, "done"))
        elif b.target in bad:
            r = bad[b.target]
            results.append(
                rb.Result(
                    b,
                    "skipped",
                    problems=[f"preflight failed at {r.step}: {r.log}"],
                )
            )
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
    if use_json:
        print(json.dumps(summary, indent=2))
    work_lock.close()
    if runner.stopping.is_set():
        return EXIT_INTERRUPTED
    return EXIT_OK if ok == len(results) else EXIT_ERROR
