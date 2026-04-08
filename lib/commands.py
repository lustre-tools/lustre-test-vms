"""Command implementations for ltvm CLI.

Each cmd_* function takes an argparse.Namespace and returns an int
exit code.  Private helpers shared across commands live here too.
The top-level ``ltvm`` script owns argparse setup and dispatch.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from lib import setup as host_setup
from lib import vmctl
from lib.config import TargetConfig, add_target, list_targets
from lib.image import build_image, image_status
from lib.kernel import build_kernel, kernel_status
from lib.lustre import build_lustre
from lib.package import (
    fetch_target,
    install_target,
    package_target,
    snapshot_lustre,
)
from lib.validate import print_results, validate_target
from lib.vmctl import RunResult

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_FOUND = 2

# GitHub repo for release downloads
GITHUB_REPO = "lustre-tools/lustre-test-vms-v2"


# ------------------------------------------------------------------
# Output helpers
# ------------------------------------------------------------------


def _resolve_lustre_tree(
    arg_value: str | None,
) -> tuple[Path | None, str | None]:
    """Resolve --lustre-tree, defaulting to cwd.

    Returns (Path, error_string).  error_string is None on success.
    """
    p = Path(arg_value).resolve() if arg_value else Path.cwd()
    if not p.is_dir():
        return None, f"Not a directory: {p}"
    kp = p / "lustre" / "kernel_patches"
    if not kp.is_dir():
        return None, (
            f"{p} does not look like a Lustre tree (no lustre/kernel_patches/)"
        )
    return p, None


def _output(data: Any, use_json: bool) -> None:
    """Print data as JSON or as a human-readable string."""
    if use_json:
        print(json.dumps(data, indent=2))
    else:
        if isinstance(data, str):
            print(data)
        elif isinstance(data, dict):
            for k, v in data.items():
                print(f"  {k}: {v}")
        elif isinstance(data, list):
            for item in data:
                print(item)


def _error(msg: str, use_json: bool, hint: str | None = None) -> int:
    """Print an error message and return EXIT_ERROR."""
    if use_json:
        err = {"error": msg}
        if hint:
            err["hint"] = hint
        print(json.dumps(err, indent=2), file=sys.stderr)
    else:
        print(f"error: {msg}", file=sys.stderr)
        if hint:
            print(f"hint: {hint}", file=sys.stderr)
    return EXIT_ERROR


def _not_found(msg: str, use_json: bool, hint: str | None = None) -> int:
    """Print a not-found message and return EXIT_NOT_FOUND."""
    if use_json:
        err = {"error": msg}
        if hint:
            err["hint"] = hint
        print(json.dumps(err, indent=2), file=sys.stderr)
    else:
        print(f"error: {msg}", file=sys.stderr)
        if hint:
            print(f"hint: {hint}", file=sys.stderr)
    return EXIT_NOT_FOUND


def _load_target(
    name: str, use_json: bool, arch: str | None = None
) -> tuple[TargetConfig | None, int | None]:
    """Load a TargetConfig, returning (config, None) or
    (None, exit_code) on failure."""
    try:
        return TargetConfig(name, arch=arch), None
    except ValueError as e:
        targets = list_targets()
        hint = (
            f"Available targets: {', '.join(targets)}"
            if targets
            else "No targets configured"
        )
        code = _not_found(str(e), use_json, hint=hint)
        return None, code


# ------------------------------------------------------------------
# Container status helper
# ------------------------------------------------------------------


def _container_status(target_config: TargetConfig) -> dict[str, Any]:
    """Return status dict for the build container artifact."""
    meta_file = target_config.container_output_dir() / "meta.json"
    if not meta_file.exists():
        return {"built": False, "stale": True}

    meta = json.loads(meta_file.read_text())
    stale = target_config.is_stale("container")
    return {"built": True, "stale": stale, **meta}


def _artifact_label(status_dict: dict[str, Any]) -> str:
    """Produce a human label like 'current', 'stale (config changed)',
    or 'not built'."""
    if not status_dict.get("built", False):
        return "not built"
    if status_dict.get("stale", False):
        return "stale"
    return "current"


def _runtime_result(res: RunResult, use_json: bool) -> int:
    """Print a runtime result dict and return the exit code."""
    if use_json:
        print(json.dumps(res, indent=2))
    else:
        if res["output"]:
            print(res["output"])
        if not res["ok"] and not res["output"]:
            print("Command failed with no output", file=sys.stderr)
    return 0 if res["ok"] else res.get("returncode", EXIT_ERROR)


def _parse_vm_kwargs(extra_args: list[str]) -> dict[str, Any]:
    """Parse --vcpus, --mem, --mdt-disks, --ost-disks, --target
    from a flat list of strings."""
    kwargs: dict[str, Any] = {}
    i = 0
    while i < len(extra_args):
        arg = extra_args[i]
        if arg == "--vcpus" and i + 1 < len(extra_args):
            kwargs["vcpus"] = int(extra_args[i + 1])
            i += 2
        elif arg == "--mem" and i + 1 < len(extra_args):
            kwargs["mem"] = int(extra_args[i + 1])
            i += 2
        elif arg == "--mdt-disks" and i + 1 < len(extra_args):
            kwargs["mdt_disks"] = int(extra_args[i + 1])
            i += 2
        elif arg == "--ost-disks" and i + 1 < len(extra_args):
            kwargs["ost_disks"] = int(extra_args[i + 1])
            i += 2
        elif arg == "--target" and i + 1 < len(extra_args):
            kwargs["target"] = extra_args[i + 1]
            i += 2
        else:
            i += 1
    return kwargs


# ------------------------------------------------------------------
# Subcommand: build-all
# ------------------------------------------------------------------


def _do_build_container(target_config: TargetConfig) -> str:
    """Run podman build for the build container and write meta."""
    tag = f"ltvm-build-{target_config.name}"
    dockerfile = target_config.target_dir / "container.Dockerfile"
    if not dockerfile.exists():
        raise FileNotFoundError(
            f"No container.Dockerfile for target {target_config.name}"
        )

    from lib.config import TARGETS_DIR

    subprocess.run(
        ["podman", "build", "-t", tag, "-f", str(dockerfile), str(TARGETS_DIR)],
        check=True,
    )

    target_config.write_meta("container", image_tag=tag)
    return tag


def cmd_build_all(args: argparse.Namespace) -> int:
    """Build container + kernel + image for a target.

    With --lustre-build, also builds the Lustre source tree
    against the freshly built kernel.
    """
    use_json = args.json
    tc, err = _load_target(args.target, use_json)
    if err is not None:
        return err
    assert tc is not None

    lustre_tree, err_msg = _resolve_lustre_tree(args.lustre_tree)
    if err_msg:
        return _error(
            err_msg,
            use_json,
            hint="Run from a Lustre tree, or pass "
            "--lustre-tree /path/to/lustre-release",
        )
    assert lustre_tree is not None

    kernel = getattr(args, "kernel", None)
    resolved_kernel = tc.resolve_kernel(kernel)

    results: dict[str, Any] = {}

    # 1. Container
    if not use_json:
        print(f"==> Building container for {args.target}...")
    try:
        _do_build_container(tc)
        results["container"] = "ok"
    except Exception as e:
        return _error(f"Container build failed: {e}", use_json)

    # 2. Kernel
    if not use_json:
        print(f"==> Building kernel {resolved_kernel} for {args.target}...")
    try:
        kmeta = build_kernel(
            tc,
            lustre_tree,
            force=args.force,
            kernel=kernel,
        )
        results["kernel"] = kmeta
    except Exception as e:
        return _error(f"Kernel build failed: {e}", use_json)

    # 3. Image
    if not use_json:
        print(f"==> Building image for {args.target}...")
    try:
        build_image(tc, force=args.force)
        results["image"] = "ok"
    except Exception as e:
        return _error(f"Image build failed: {e}", use_json)

    # 4. Lustre (optional -- only when --lustre-build is passed)
    if getattr(args, "lustre_build", False):
        if not use_json:
            print(
                f"==> Building Lustre against {resolved_kernel} kernel tree..."
            )
        build_tree = tc.kernel_output_dir(kernel=resolved_kernel) / "build-tree"
        try:
            container_tag = f"ltvm-build-{args.target}"
            lmeta = build_lustre(
                lustre_tree,
                build_tree,
                container_tag=container_tag,
                enable_server=tc.server,
                force=args.force,
            )
            results["lustre"] = lmeta
        except Exception as e:
            return _error(f"Lustre build failed: {e}", use_json)

    _output(results, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: build-container
# ------------------------------------------------------------------


def cmd_build_container(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target(args.target, use_json)
    if err is not None:
        return err
    assert tc is not None

    if not use_json:
        print(f"Building container for {args.target}...")

    try:
        tag = _do_build_container(tc)
    except Exception as e:
        return _error(f"Container build failed: {e}", use_json)

    result = {"target": args.target, "image_tag": tag}
    _output(result, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: build-kernel
# ------------------------------------------------------------------


def cmd_build_kernel(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target(args.target, use_json)
    if err is not None:
        return err
    assert tc is not None

    # Deb-based targets don't need a Lustre tree for kernel builds
    lustre_tree = None
    if not tc.kernel_deb_source:
        lustre_tree, err_msg = _resolve_lustre_tree(args.lustre_tree)
        if err_msg:
            return _error(
                err_msg,
                use_json,
                hint="Run from a Lustre tree, or pass "
                "--lustre-tree /path/to/lustre-release",
            )
        assert lustre_tree is not None

    kernel = getattr(args, "kernel", None)

    if not use_json:
        k = tc.resolve_kernel(kernel)
        print(f"Building kernel {k} for {args.target}...")

    try:
        meta = build_kernel(
            tc,
            lustre_tree,
            force=args.force,
            kernel=kernel,
        )
    except Exception as e:
        return _error(f"Kernel build failed: {e}", use_json)

    _output(meta, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: build-image
# ------------------------------------------------------------------


def cmd_build_image(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target(args.target, use_json)
    if err is not None:
        return err
    assert tc is not None

    if not use_json:
        print(f"Building image for {args.target}...")

    try:
        path = build_image(tc, force=args.force)
    except Exception as e:
        return _error(f"Image build failed: {e}", use_json)

    result = {"target": args.target, "path": str(path)}
    _output(result, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: build-lustre
# ------------------------------------------------------------------


def cmd_build_lustre(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target(args.target, use_json)
    if err is not None:
        return err
    assert tc is not None

    lustre_tree, err_msg = _resolve_lustre_tree(
        getattr(args, "lustre_tree", None)
    )
    if err_msg:
        return _error(
            err_msg,
            use_json,
            hint="Pass --lustre-tree or run from a Lustre source tree",
        )
    assert lustre_tree is not None

    kernel = getattr(args, "kernel", None)
    resolved_kernel = tc.resolve_kernel(kernel)
    build_tree = tc.kernel_output_dir(kernel=resolved_kernel) / "build-tree"
    if not build_tree.is_dir():
        return _error(
            f"Kernel build-tree not found: {build_tree}",
            use_json,
            hint=f"Run: ltvm build-kernel {args.target} "
            f"--kernel {resolved_kernel}",
        )

    # Server build follows target.conf unless overridden
    enable_server = tc.server
    if getattr(args, "disable_server", False):
        enable_server = False
    elif getattr(args, "enable_server", False):
        enable_server = True

    extra = list(tc.configure_args)
    if getattr(args, "configure", None):
        import shlex

        extra += shlex.split(args.configure)

    jobs = getattr(args, "jobs", None)

    if not use_json:
        srv = "server+client" if enable_server else "client-only"
        print(f"Building Lustre ({srv}) against {args.target} kernel tree...")

    container_tag = f"ltvm-build-{args.target}"

    try:
        meta = build_lustre(
            lustre_tree,
            build_tree,
            container_tag=container_tag,
            enable_server=enable_server,
            extra_configure=extra,
            jobs=jobs,
            force=getattr(args, "force", False),
        )
    except Exception as e:
        return _error(f"Lustre build failed: {e}", use_json)

    _output(meta, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: package
# ------------------------------------------------------------------


def cmd_package(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target(args.target, use_json)
    if err is not None:
        return err
    assert tc is not None

    kernel = getattr(args, "kernel", None)

    # Snapshot Lustre tree if --lustre-tree provided
    lustre_tree_arg = getattr(args, "lustre_tree", None)
    if lustre_tree_arg:
        lustre_path, err_msg = _resolve_lustre_tree(lustre_tree_arg)
        if err_msg:
            return _error(err_msg, use_json)
        assert lustre_path is not None
        if not use_json:
            print("Snapshotting Lustre tree...")
        try:
            snapshot_lustre(
                lustre_path,
                tc.output_dir,
                kernel=kernel,
            )
        except Exception as e:
            return _error(f"Lustre snapshot failed: {e}", use_json)

    if not use_json:
        print(f"Packaging {args.target}...")

    try:
        tarball = package_target(
            args.target,
            tc.output_dir,
            kernel=kernel,
            dest_dir=getattr(args, "output", None),
        )
    except Exception as e:
        return _error(f"Package failed: {e}", use_json)

    result = {"target": args.target, "tarball": str(tarball)}
    _output(result, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: fetch
# ------------------------------------------------------------------


def _gh_api(endpoint: str) -> dict:
    """Call GitHub API and return parsed JSON."""
    api = f"https://api.github.com/repos/{GITHUB_REPO}/{endpoint}"
    r = subprocess.run(
        ["curl", "-fsSL", api], capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"GitHub API failed (rc={r.returncode}): {api}\n  {r.stderr.strip()}"
        )
    return json.loads(r.stdout)


def _find_release_url(
    target: str,
    filter_str: str | None = None,
) -> str:
    """Find a tarball download URL from GitHub releases.

    Searches all releases for one whose tag starts with the target
    name and optionally contains filter_str.  Returns the first
    matching asset's download URL.
    """
    releases = _gh_api("releases")
    if not isinstance(releases, list):
        releases = [releases]

    for rel in releases:
        tag = rel.get("tag_name", "")
        if not tag.startswith(target):
            continue
        if filter_str and filter_str not in tag:
            continue
        for asset in rel.get("assets", []):
            name = asset.get("name", "")
            if name.endswith((".tar.zst", ".tar.gz")):
                return str(asset["browser_download_url"])

    avail = [r.get("tag_name", "?") for r in releases]
    hint = f" matching '{filter_str}'" if filter_str else ""
    raise RuntimeError(
        f"No release found for '{target}'{hint}\n"
        f"  Available releases: {', '.join(avail)}\n"
        f"  Try: ltvm fetch --list"
    )


def _list_releases(target: str | None = None) -> list[dict]:
    """List available releases, optionally filtered by target prefix."""
    releases = _gh_api("releases")
    if not isinstance(releases, list):
        releases = [releases]
    result = []
    for rel in releases:
        tag = rel.get("tag_name", "")
        if target and not tag.startswith(target):
            continue
        assets = [a["name"] for a in rel.get("assets", [])
                  if a["name"].endswith((".tar.zst", ".tar.gz"))]
        size_mb = sum(a.get("size", 0) for a in rel.get("assets", [])) / (1024 * 1024)
        result.append({
            "tag": tag,
            "date": rel.get("published_at", "")[:10],
            "assets": assets,
            "size_mb": round(size_mb),
        })
    return result


def cmd_fetch(args: argparse.Namespace) -> int:
    use_json = args.json
    url = getattr(args, "url", None)
    target = getattr(args, "target", None)
    filt = getattr(args, "filter", None)

    # --list: show available releases
    if getattr(args, "list", False):
        try:
            releases = _list_releases(target)
        except RuntimeError as e:
            return _error(str(e), use_json)
        if use_json:
            _output(releases, use_json)
        else:
            if not releases:
                print("  (no releases found)")
            for r in releases:
                print(f"  {r['tag']:<60s}  {r['size_mb']:>5d} MB  {r['date']}")
        return EXIT_OK

    if not target:
        return _error("target required (e.g. ltvm fetch rocky9)", use_json)

    from lib.config import OUTPUT_DIR

    # Resolve URL: explicit --url, or GitHub release lookup
    if not url:
        if not use_json:
            print(f"Looking up {target} from GitHub releases...")
        try:
            url = _find_release_url(target, filter_str=filt)
        except RuntimeError as e:
            return _error(str(e), use_json)
        if not use_json:
            print(f"  Found: {url}")

    if not use_json:
        print(f"Fetching {target}...")

    try:
        target_dir = fetch_target(target, url, OUTPUT_DIR)
    except Exception as e:
        return _error(f"Fetch failed: {e}", use_json)

    result = {"target": target, "path": str(target_dir)}
    _output(result, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: publish
# ------------------------------------------------------------------


def cmd_publish(args: argparse.Namespace) -> int:
    """Upload a packaged tarball to a GitHub release."""
    use_json = args.json
    tc, err = _load_target(args.target, use_json)
    if err is not None:
        return err
    assert tc is not None

    kernel = getattr(args, "kernel", None)
    tag = getattr(args, "tag", None)
    tarball_path = getattr(args, "tarball", None)

    # Find tarball: explicit path or auto-detect from output/
    if tarball_path:
        tarball = Path(tarball_path)
        if not tarball.exists():
            return _error(
                f"Tarball not found: {tarball}",
                use_json,
            )
    else:
        # Look for existing tarball in output/
        pattern = f"{args.target}-*.tar.*"
        candidates = sorted(tc.output_dir.parent.glob(pattern))
        if kernel:
            candidates = [c for c in candidates if kernel in c.name]
        if not candidates:
            return _error(
                f"No tarball found matching {pattern}",
                use_json,
                hint=f"Run 'ltvm package {args.target}' first",
            )
        tarball = candidates[-1]  # newest

    if not use_json:
        print(f"Publishing {tarball.name}...")

    # Generate tag if not provided
    if not tag:
        tag = tarball.stem.replace(".tar", "")
        # Clean up double extensions
        for ext in (".zst", ".gz"):
            tag = tag.removesuffix(ext)

    # Create release + upload via gh CLI
    if not use_json:
        print(f"  Tag: {tag}")
        print(f"  Tarball: {tarball}")
        print(f"  Size: {tarball.stat().st_size / (1024 * 1024):.0f} MB")

    # Create release (ok if it already exists)
    subprocess.run(
        [
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            GITHUB_REPO,
            "--title",
            tag,
            "--notes",
            f"Pre-built artifacts for {args.target}",
        ],
        capture_output=True,
    )

    # Upload asset
    r = subprocess.run(
        [
            "gh",
            "release",
            "upload",
            tag,
            str(tarball),
            "--repo",
            GITHUB_REPO,
            "--clobber",
        ],
    )
    if r.returncode != 0:
        return _error(
            f"Upload failed (rc={r.returncode})",
            use_json,
            hint="Check 'gh auth status' for credentials",
        )

    url = f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}"
    if not use_json:
        print(f"  Published: {url}")

    result = {
        "target": args.target,
        "tag": tag,
        "tarball": str(tarball),
        "url": url,
    }
    _output(result, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: install
# ------------------------------------------------------------------


def cmd_install(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target(args.target, use_json)
    if err is not None:
        return err
    assert tc is not None

    if not tc.output_dir.is_dir():
        return _error(
            f"No artifacts for {args.target}",
            use_json,
            hint=f"Run 'ltvm fetch {args.target}' or "
            f"'ltvm build-all {args.target}' first",
        )

    kernel = getattr(args, "kernel", None)

    if not use_json:
        print(f"Installing {args.target} to system paths...")

    try:
        installed = install_target(
            args.target,
            tc.output_dir,
            kernel=kernel,
        )
    except Exception as e:
        return _error(f"Install failed: {e}", use_json)

    _output(installed, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: shell
# ------------------------------------------------------------------


def cmd_shell(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target(args.target, use_json)
    if err is not None:
        return err
    assert tc is not None

    tag = f"ltvm-build-{args.target}"
    mount_path = Path(args.path).resolve()

    if not mount_path.is_dir():
        return _error(f"Mount path not found: {mount_path}", use_json)

    # Check container image exists
    result = subprocess.run(
        ["podman", "image", "exists", tag], capture_output=True
    )
    if result.returncode != 0:
        return _error(
            f"Container image {tag} not found",
            use_json,
            hint=f"Run: ltvm build-container {args.target}",
        )

    if not use_json:
        print(
            f"Entering build container for {args.target} "
            f"with {mount_path} mounted at /src..."
        )

    rc = subprocess.run(
        [
            "podman",
            "run",
            "--rm",
            "-it",
            "-v",
            f"{mount_path}:/src:Z",
            "-w",
            "/src",
            tag,
            "bash",
        ]
    ).returncode

    return rc


# ------------------------------------------------------------------
# Subcommand: status
# ------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    use_json = args.json
    targets = list_targets()

    if not targets:
        if use_json:
            print(json.dumps({"targets": []}))
        else:
            print("No targets configured.")
        return EXIT_OK

    all_status = {}
    for name in targets:
        tc = TargetConfig(name)
        cs = _container_status(tc)
        ks = kernel_status(tc)
        ims = image_status(tc)
        all_status[name] = {
            "container": cs,
            "kernel": ks,
            "image": ims,
        }

    if use_json:
        print(json.dumps(all_status, indent=2))
    else:
        # Table output
        hdr = f"{'Target':<12} {'Container':<14} {'Kernel':<26} {'Image':<14}"
        print(hdr)
        print("-" * len(hdr))
        for name, st in all_status.items():
            c = _artifact_label(st["container"])
            k = _artifact_label(st["kernel"])
            i = _artifact_label(st["image"])
            print(f"{name:<12} {c:<14} {k:<26} {i:<14}")

    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: update
# ------------------------------------------------------------------


def cmd_update(args: argparse.Namespace) -> int:
    use_json = args.json

    if args.all:
        targets = list_targets()
    else:
        targets = [args.target]

    if not targets:
        return _error("No targets to update", use_json)

    results: dict[str, str | list[str]] = {}
    for name in targets:
        tc, err = _load_target(name, use_json)
        if err is not None:
            return err
        assert tc is not None

        updated = []

        if tc.is_stale("container"):
            if not use_json:
                print(f"Rebuilding container for {name}...")
            try:
                _do_build_container(tc)
                updated.append("container")
            except Exception as e:
                return _error(
                    f"Container rebuild failed for {name}: {e}", use_json
                )

        if tc.is_stale("image"):
            if not use_json:
                print(f"Rebuilding image for {name}...")
            try:
                build_image(tc, force=True)
                updated.append("image")
            except Exception as e:
                return _error(f"Image rebuild failed for {name}: {e}", use_json)

        # Kernel requires --lustre-tree, skip if not stale
        if tc.is_stale("kernel"):
            if args.lustre_tree:
                if not use_json:
                    print(f"Rebuilding kernel for {name}...")
                try:
                    build_kernel(
                        tc, Path(args.lustre_tree).resolve(), force=True
                    )
                    updated.append("kernel")
                except Exception as e:
                    return _error(
                        f"Kernel rebuild failed for {name}: {e}", use_json
                    )
            else:
                if not use_json:
                    print(
                        f"Kernel for {name} is stale but "
                        "--lustre-tree not provided, skipping"
                    )

        if not updated:
            results[name] = "up to date"
        else:
            results[name] = updated

    _output(results, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# Runtime: VM management
# ------------------------------------------------------------------


def cmd_vm(args: argparse.Namespace) -> int:
    use_json = args.json
    action = args.action
    vm_args = args.vm_args

    if action == "list":
        res = vmctl.vm_list(json_output=use_json)
        return _runtime_result(res, use_json)

    if action == "status":
        if not vm_args:
            return _error("vm status requires a VM name", use_json)
        res = vmctl.vm_status(vm_args[0], json_output=use_json)
        return _runtime_result(res, use_json)

    if action in ("create", "ensure"):
        if not vm_args:
            return _error(f"vm {action} requires a VM name", use_json)
        name = vm_args[0]
        # Parse optional flags from vm_args[1:]
        kwargs = _parse_vm_kwargs(vm_args[1:])
        fn = vmctl.vm_create if action == "create" else vmctl.vm_ensure
        res = fn(name, **kwargs)
        return _runtime_result(res, use_json)

    if action == "destroy":
        if not vm_args:
            return _error("vm destroy requires a VM name", use_json)
        res = vmctl.vm_destroy(vm_args[0])
        return _runtime_result(res, use_json)

    if action in ("start", "stop", "restart"):
        if not vm_args:
            return _error(f"vm {action} requires a VM name", use_json)
        fn = getattr(vmctl, f"vm_{action}")
        res = fn(vm_args[0])
        return _runtime_result(res, use_json)

    return _error(f"Unknown vm action: {action}", use_json)


def cmd_deploy(args: argparse.Namespace) -> int:
    use_json = args.json
    target = getattr(args, "target", None)
    kernel = getattr(args, "kernel", None)
    ltvm_root = Path(__file__).parent.parent

    # Auto-detect target from VM metadata if not specified
    if not target:
        try:
            res = vmctl.vm_status(args.vm, json_output=True)
            if res["ok"]:
                import json as _json
                status = _json.loads(res["output"])
                target = status.get("os_id") or None
                if target and not use_json:
                    print(f"  Auto-detected target: {target}")
        except Exception:
            pass
        if not target:
            return _error(
                f"Cannot detect target OS for VM '{args.vm}'. "
                f"Pass --target explicitly.",
                use_json,
            )

    # Resolve kernel name for path lookups
    try:
        tc = TargetConfig(target)
        resolved_kernel = tc.resolve_kernel(kernel)
    except ValueError:
        resolved_kernel = kernel or "unknown"

    # Resolve build path: explicit --build, or packaged Lustre,
    # or cwd
    build_arg = getattr(args, "build", ".")
    if build_arg == ".":
        # Check for packaged Lustre tree first
        packaged = (
            ltvm_root
            / "output"
            / target
            / "kernels"
            / resolved_kernel
            / "lustre"
        )
        if packaged.is_dir():
            build_path = packaged
            if not use_json:
                print(f"  Using packaged Lustre from {packaged}")
        else:
            build_path = Path(".").resolve()
    else:
        build_path = Path(build_arg).resolve()

    if not build_path.is_dir():
        return _error(f"Build path not found: {build_path}", use_json)

    # Auto-detect kernel modules from ltvm output
    kernel_modules = None
    mods_path = (
        ltvm_root / "output" / target / "kernels" / resolved_kernel / "modules"
    )
    if mods_path.is_dir():
        kernel_modules = str(mods_path)
        if not use_json:
            print(f"  Deploying kernel modules from {mods_path}")

    res = vmctl.deploy(
        args.vm,
        str(build_path),
        mount=args.mount,
        kernel_modules=kernel_modules,
        os_family=tc.os_family,
    )
    return _runtime_result(res, use_json)


def cmd_exec(args: argparse.Namespace) -> int:
    use_json = args.json
    if not args.cmd:
        return _error("exec requires a command", use_json)
    cmd_str = " ".join(args.cmd)
    timeout = getattr(args, "timeout", 120)
    res = vmctl.vm_exec(args.vm, cmd_str, timeout=timeout)
    return _runtime_result(res, use_json)


def cmd_cluster(args: argparse.Namespace) -> int:
    use_json = args.json
    action = args.action
    cargs = args.cluster_args

    if action == "create":
        if len(cargs) < 2:
            return _error(
                "cluster create requires a name and at least one node spec",
                use_json,
                hint="ltvm cluster create <name> <role:vm[:disks]> ...",
            )
        name = cargs[0]
        specs = cargs[1:]
        res = vmctl.cluster_create(name, *specs)
        return _runtime_result(res, use_json)

    if action == "destroy":
        if not cargs:
            return _error("cluster destroy requires a name", use_json)
        res = vmctl.cluster_destroy(cargs[0])
        return _runtime_result(res, use_json)

    if action == "deploy":
        if not cargs:
            return _error("cluster deploy requires a name", use_json)
        name = cargs[0]
        # Parse --build and --mount from remaining args
        build_path = "."
        mount = False
        i = 1
        while i < len(cargs):
            if cargs[i] == "--build" and i + 1 < len(cargs):
                build_path = cargs[i + 1]
                i += 2
            elif cargs[i] == "--mount":
                mount = True
                i += 1
            else:
                i += 1
        res = vmctl.cluster_deploy(name, build_path, mount=mount)
        return _runtime_result(res, use_json)

    if action == "status":
        if not cargs:
            return _error("cluster status requires a name", use_json)
        res = vmctl.cluster_status(cargs[0])
        return _runtime_result(res, use_json)

    if action == "exec":
        if len(cargs) < 3:
            return _error(
                "cluster exec requires a name, role, and command",
                use_json,
                hint="ltvm cluster exec <name> <role> '<cmd>'",
            )
        name = cargs[0]
        role = cargs[1]
        cmd_str = " ".join(cargs[2:])
        res = vmctl.cluster_exec(name, role, cmd_str)
        return _runtime_result(res, use_json)

    return _error(f"Unknown cluster action: {action}", use_json)


# ------------------------------------------------------------------
# Subcommand: validate
# ------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    """Run validation checks on a built target."""
    use_json = args.json
    tc, err = _load_target(args.target, use_json)
    if err is not None:
        return err
    assert tc is not None

    lustre_tree = None
    if args.lustre_tree:
        lustre_tree, err_msg = _resolve_lustre_tree(args.lustre_tree)
        if err_msg:
            return _error(err_msg, use_json)

    try:
        summary = validate_target(
            tc, lustre_tree=lustre_tree, verbose=args.verbose
        )
    except Exception as e:
        return _error(f"Validation failed: {e}", use_json)

    if use_json:
        print(json.dumps(summary, indent=2))
    else:
        print_results(summary, verbose=args.verbose)

    return EXIT_OK if summary["all_passed"] else EXIT_ERROR


# ------------------------------------------------------------------
# Subcommand: setup
# ------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    """Run host setup (QEMU, network, scripts, SSH)."""
    use_json = args.json

    # Collect requested steps
    explicit = []
    if args.qemu:
        explicit.append("qemu")
    if args.network:
        explicit.append("network")
    if args.install:
        explicit.append("install")
    if args.ssh:
        explicit.append("ssh")
    steps = explicit or None  # None = all

    if args.verify:
        try:
            results = host_setup.verify(subnet=args.subnet)
        except Exception as e:
            return _error(str(e), use_json)
        if use_json:
            print(json.dumps(results, indent=2))
        else:
            host_setup.print_verify(results)
        return EXIT_OK if results["all_ok"] else EXIT_ERROR

    try:
        host_setup.run_setup(
            steps=steps,
            subnet=args.subnet,
            force=getattr(args, "force", False),
            json_output=use_json,
        )
    except RuntimeError as e:
        return _error(str(e), use_json)
    except Exception as e:
        return _error(f"Setup failed: {e}", use_json)

    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: add-target
# ------------------------------------------------------------------


def cmd_add_target(args: argparse.Namespace) -> int:
    """Scaffold a new target: directory, Dockerfiles, YAML entry."""
    use_json = args.json
    name = args.name
    image = args.image

    kernel = getattr(args, "kernel", None)
    srpm_url = getattr(args, "srpm_url", None)
    server = None
    if getattr(args, "no_server", False):
        server = False

    try:
        result = add_target(
            name,
            image,
            kernel=kernel,
            srpm_url=srpm_url,
            server=server,
        )
    except ValueError as e:
        return _error(str(e), use_json)

    if use_json:
        _output(result, use_json)
    else:
        print(f"Created target {name!r}:")
        print(f"  Directory: {result['target_dir']}")
        for f in result["files_created"]:
            print(f"  + {f}")
        print()
        print("Next steps:")
        print(f"  1. Review and customize the Dockerfiles in targets/{name}/")
        print("  2. Edit targets/targets.yaml to adjust settings")
        if kernel:
            print(f"  3. Run: ltvm build-all {name} --lustre-tree <path>")
        else:
            print(f"  3. Add a kernel entry, then: ltvm build-all {name}")

    return EXIT_OK
