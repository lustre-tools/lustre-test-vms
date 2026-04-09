"""Command implementations for ltvm CLI.

Each cmd_* function takes an argparse.Namespace and returns an int
exit code.  Private helpers shared across commands live here too.
The top-level ``ltvm`` script owns argparse setup and dispatch.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from ltvm_pkg import host_setup
from ltvm_pkg.target_config import TargetConfig, list_targets
from ltvm_pkg.image_build import build_image, image_status
from ltvm_pkg.kernel_build import build_kernel, kernel_status
from ltvm_pkg.lustre_build import build_lustre
from ltvm_pkg.release_package import (
    fetch_target,
    package_target,
    snapshot_lustre,
)
from ltvm_pkg.vm_state import ROOT_PASSWORD

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


def _build_container_tag(tc: TargetConfig) -> str:
    """Return the podman container image tag for a target + arch."""
    if tc.arch != "x86_64":
        return f"ltvm-build-{tc.name}-{tc.arch}"
    return f"ltvm-build-{tc.name}"


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


def _require_root(use_json: bool, hint: str = "") -> int | None:
    """Return an error code if not root, or None if root."""
    if os.getuid() != 0:
        msg = "This command requires root. Use: sudo ltvm ..."
        if hint:
            msg += f"\n  {hint}"
        return _error(msg, use_json)
    return None


def _qemu_ns(**kwargs: Any) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for qemu command functions."""
    return argparse.Namespace(**kwargs)


def _parse_size(s: str) -> int:
    """Parse a human-readable size like '1G', '512M', '2048' to bytes."""
    s = s.strip().upper()
    if s.endswith("G"):
        return int(s[:-1]) * 1024 * 1024 * 1024
    if s.endswith("M"):
        return int(s[:-1]) * 1024 * 1024
    return int(s)


def _parse_vm_kwargs(extra_args: list[str]) -> dict[str, Any]:
    """Parse --vcpus, --mem, --mdt-disks, --ost-disks, --disk-size,
    --target, --arch from a flat list of strings.

    Note: argparse REMAINDER captures all args after the positional,
    so flags like --arch that appear in vm_args must be parsed here
    rather than relying on the parent parser.
    """
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
        elif arg == "--disk-size" and i + 1 < len(extra_args):
            kwargs["disk_size"] = _parse_size(extra_args[i + 1])
            i += 2
        elif arg == "--target" and i + 1 < len(extra_args):
            kwargs["target"] = extra_args[i + 1]
            i += 2
        elif arg == "--os" and i + 1 < len(extra_args):
            kwargs["target"] = extra_args[i + 1]
            i += 2
        elif arg == "--arch" and i + 1 < len(extra_args):
            kwargs["arch"] = extra_args[i + 1]
            i += 2
        else:
            i += 1
    return kwargs


# ------------------------------------------------------------------
# Subcommand: build-all
# ------------------------------------------------------------------


def _do_build_container(target_config: TargetConfig) -> str:
    """Run podman build for the build container and write meta."""
    tag = _build_container_tag(target_config)
    dockerfile = target_config.target_dir / "container.Dockerfile"
    if not dockerfile.exists():
        raise FileNotFoundError(
            f"No container.Dockerfile for target {target_config.name}"
        )

    from ltvm_pkg.target_config import TARGETS_DIR

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
    tc, err = _load_target(args.target, use_json, arch=getattr(args, "arch", None))
    if err is not None:
        return err
    assert tc is not None

    lustre_tree_arg = getattr(args, "lustre_tree_pos", None) or args.lustre_tree
    lustre_tree, err_msg = _resolve_lustre_tree(lustre_tree_arg)
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
            container_tag = _build_container_tag(tc)
            lmeta = build_lustre(
                lustre_tree,
                build_tree,
                container_tag=container_tag,
                enable_server=tc.server,
                force=args.force,
                arch=tc.arch,
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
    tc, err = _load_target(args.target, use_json, arch=getattr(args, "arch", None))
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
    tc, err = _load_target(args.target, use_json, arch=getattr(args, "arch", None))
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
    tc, err = _load_target(args.target, use_json, arch=getattr(args, "arch", None))
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
    tc, err = _load_target(args.target, use_json, arch=getattr(args, "arch", None))
    if err is not None:
        return err
    assert tc is not None

    lustre_tree_arg = getattr(args, "lustre_tree_pos", None) or getattr(
        args, "lustre_tree", None
    )
    lustre_tree, err_msg = _resolve_lustre_tree(lustre_tree_arg)
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
        extra += shlex.split(args.configure)

    jobs = getattr(args, "jobs", None)

    if not use_json:
        srv = "server+client" if enable_server else "client-only"
        print(f"Building Lustre ({srv}) against {args.target} kernel tree...")

    container_tag = _build_container_tag(tc)

    try:
        meta = build_lustre(
            lustre_tree,
            build_tree,
            container_tag=container_tag,
            enable_server=enable_server,
            extra_configure=extra,
            jobs=jobs,
            force=getattr(args, "force", False),
            arch=tc.arch,
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
    tc, err = _load_target(args.target, use_json, arch=getattr(args, "arch", None))
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
            arch=tc.arch,
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
    arch: str = "x86_64",
) -> str:
    """Find a tarball download URL from GitHub releases.

    Searches all releases for one whose tag starts with the target
    name and optionally contains filter_str.  Returns the first
    matching asset's download URL.

    For non-x86_64 arches, the asset filename contains '-<arch>'
    (e.g. ubuntu2404-6.8.12-aarch64.tar.gz).  x86_64 assets have
    no arch suffix, so we exclude files ending in known arch suffixes
    to avoid picking up an aarch64 asset for an x86_64 request.
    """
    releases = _gh_api("releases")
    if not isinstance(releases, list):
        releases = [releases]

    non_default_arches = ("aarch64",)  # arches that get a suffix in asset names

    for rel in releases:
        tag = rel.get("tag_name", "")
        if not tag.startswith(target):
            continue
        if filter_str and filter_str not in tag:
            continue
        for asset in rel.get("assets", []):
            name = asset.get("name", "")
            if not name.endswith((".tar.zst", ".tar.gz")):
                continue
            # For non-default arch, require the arch suffix in the asset name
            if arch != "x86_64":
                if f"-{arch}." not in name:
                    continue
            else:
                # For x86_64, skip assets that belong to other arches
                if any(f"-{a}." in name for a in non_default_arches):
                    continue
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
    arch = getattr(args, "arch", None) or "x86_64"

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

    from ltvm_pkg.target_config import OUTPUT_DIR

    # Resolve URL: explicit --url, or GitHub release lookup
    if not url:
        if not use_json:
            print(f"Looking up {target} from GitHub releases...")
        try:
            url = _find_release_url(target, filter_str=filt, arch=arch)
        except RuntimeError as e:
            return _error(str(e), use_json)

    # Extract release tag from URL to check if already fetched.
    # URL: .../releases/download/<tag>/<filename>
    # For non-default arch use an arch-qualified tag file so x86_64 and
    # aarch64 fetches don't stomp on each other.
    release_tag = url.split("/releases/download/")[1].split("/")[0] if "/releases/download/" in url else ""
    arch_suffix = f"-{arch}" if arch != "x86_64" else ""
    tag_file = OUTPUT_DIR / target / f".ltvm-release-tag{arch_suffix}"
    if release_tag and tag_file.exists():
        existing_tag = tag_file.read_text().strip()
        if existing_tag == release_tag:
            if not use_json:
                print(f"  Already up to date ({release_tag})")
            result = {"target": target, "path": str(OUTPUT_DIR / target)}
            _output(result, use_json)
            return EXIT_OK

    if not use_json:
        print(f"Fetching {target}...")

    try:
        target_dir = fetch_target(target, url, OUTPUT_DIR, arch=arch)
        # Record the release tag so repeat fetches are instant
        tag_file.parent.mkdir(parents=True, exist_ok=True)
        tag_file.write_text(release_tag + "\n")
    except Exception as e:
        return _error(f"Fetch failed: {e}", use_json)

    result = {"target": target, "path": str(target_dir)}
    _output(result, use_json)

    if not use_json:
        print()
        print("Next:")
        arch_flag = f" --arch {arch}" if arch != "x86_64" else ""
        print(f"  sudo ltvm vm create co1-test --os {target}{arch_flag} "
              f"--vcpus 2 --mem 2048 --mdt-disks 1 --ost-disks 2")
        print(f"  sudo ltvm deploy co1-test --mount")

    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: publish
# ------------------------------------------------------------------


def cmd_publish(args: argparse.Namespace) -> int:
    """Upload a packaged tarball to a GitHub release."""
    use_json = args.json
    tc, err = _load_target(args.target, use_json, arch=getattr(args, "arch", None))
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
        candidates = [
            c for c in sorted(tc.output_dir.parent.glob(pattern))
            if c.suffix in (".gz", ".zst") or c.name.endswith(".tar.gz") or c.name.endswith(".tar.zst")
        ]
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
# Subcommand: shell
# ------------------------------------------------------------------


def cmd_build_shell(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target(args.target, use_json, arch=getattr(args, "arch", None))
    if err is not None:
        return err
    assert tc is not None

    tag = _build_container_tag(tc)
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
        try:
            tc = TargetConfig(name)
        except ValueError:
            continue  # skip planned/disabled targets
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



# ------------------------------------------------------------------
# Runtime: VM management
# ------------------------------------------------------------------


def _configure_test_disks(
    ip: str, mdt_disks: int, ost_disks: int,
    disk_size_bytes: int = 0, os_family: str = "rhel",
) -> None:
    """Write OSTCOUNT/OSTDEV*/MDSCOUNT/MDSDEV*/OSTSIZE into cfg/local.sh.

    Virtio disks are attached in order (MDT first, then OST),
    starting at /dev/vdb (vda = rootfs).
    """
    from ltvm_pkg.vm_net import run_ssh

    testdir = (
        "/usr/lib/lustre/tests"
        if os_family == "debian"
        else "/usr/lib64/lustre/tests"
    )
    lines = []

    # Set device sizes in KB (test framework uses KB for OSTSIZE/MDSSIZE)
    if disk_size_bytes:
        size_kb = disk_size_bytes // 1024
        if mdt_disks:
            lines.append(f"MDSSIZE={size_kb}")
        if ost_disks:
            lines.append(f"OSTSIZE={size_kb}")

    if mdt_disks:
        lines.append(f"MDSCOUNT={mdt_disks}")
        for n in range(1, mdt_disks + 1):
            letter = chr(ord('a') + n)
            lines.append(f"MDSDEV{n}=/dev/vd{letter}")

    if ost_disks:
        lines.append(f"OSTCOUNT={ost_disks}")
        for n in range(1, ost_disks + 1):
            letter = chr(ord('a') + mdt_disks + n)
            lines.append(f"OSTDEV{n}=/dev/vd{letter}")

    snippet = "\\n".join(lines)
    script = (
        f"sed -i '/^# --- VM disk configuration/,/^# --- END VM disk/d' "
        f"{testdir}/cfg/local.sh 2>/dev/null || true; "
        f"printf '\\n# --- VM disk configuration (generated by ltvm deploy) ---\\n"
        f"{snippet}\\n"
        f"# --- END VM disk configuration ---\\n' >> {testdir}/cfg/local.sh"
    )
    run_ssh(ip, script, timeout=30)


def _lustre_mount_vm(name: str, os_family: str) -> int:
    """Run llmount.sh inside a VM. Returns exit code."""
    from ltvm_pkg.vm_state import VMInfo, VMNotFound
    from ltvm_pkg.vm_net import run_ssh
    try:
        vm = VMInfo.load(name)
    except VMNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_NOT_FOUND
    libdir = "/usr/lib/lustre" if os_family == "debian" else "/usr/lib64/lustre"
    try:
        r = run_ssh(
            vm.ip,
            f"cd {libdir}/tests && LUSTRE={libdir} bash llmount.sh",
            timeout=180,
        )
        if r.stdout:
            print(r.stdout, end="")
        return r.returncode
    except Exception as e:
        print(f"error: Lustre mount failed: {e}", file=sys.stderr)
        return EXIT_ERROR


def _os_family_for_vm(name: str) -> str:
    """Return os_family for a VM's os_id."""
    from ltvm_pkg.vm_state import VMInfo, VMNotFound
    vm = VMInfo.load(name)
    os_id = vm.os_id
    if not os_id:
        raise RuntimeError(f"VM '{name}' has no os_id recorded; was it created with ltvm?")
    return TargetConfig(os_id).os_family


def _vm_call(fn, ns, use_json: bool) -> int:
    """Call a vm_commands function, catching SystemExit and VMNotFound."""
    from ltvm_pkg.vm_state import VMNotFound
    try:
        fn(ns)
        return EXIT_OK
    except SystemExit as e:
        return int(e.code) if e.code is not None else EXIT_ERROR
    except VMNotFound as e:
        return _error(str(e), use_json)


def _resolve_disk_size(val: str | None) -> int | None:
    """Parse disk_size from argparse (string or None) to int bytes."""
    if val is None:
        return None
    if isinstance(val, int):
        return val
    return _parse_size(val)


def cmd_create(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_create as _create
    ns = _qemu_ns(
        name=args.name,
        vcpus=args.vcpus,
        mem=args.mem,
        ip=args.ip,
        rootfs=args.rootfs or "",
        image=args.image or "",
        kernel=args.kernel or "",
        mdt_disks=args.mdt_disks,
        ost_disks=args.ost_disks,
        disk_size=_resolve_disk_size(args.disk_size),
        arch=args.arch or "x86_64",
        os=args.os or "",
        _quiet=False,
        json=use_json,
    )
    return _vm_call(_create, ns, use_json)


def cmd_ensure(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_ensure as _ensure
    ns = _qemu_ns(
        name=args.name,
        vcpus=args.vcpus,
        mem=args.mem,
        ip=args.ip,
        rootfs=args.rootfs or "",
        image=args.image or "",
        kernel=args.kernel or "",
        mdt_disks=args.mdt_disks,
        ost_disks=args.ost_disks,
        disk_size=_resolve_disk_size(args.disk_size),
        arch=args.arch or "x86_64",
        os=args.os or "",
        _quiet=False,
        json=use_json,
    )
    return _vm_call(_ensure, ns, use_json)


def cmd_destroy(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_destroy as _destroy
    return _vm_call(_destroy, _qemu_ns(names=args.names), use_json)


def cmd_vm_start(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_start as _start
    return _vm_call(_start, _qemu_ns(names=args.names), use_json)


def cmd_vm_stop(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_stop as _stop
    return _vm_call(_stop, _qemu_ns(names=args.names), use_json)


def cmd_list(args: argparse.Namespace) -> int:
    use_json = args.json
    from ltvm_pkg.vm_commands import cmd_list as _list
    return _vm_call(_list, _qemu_ns(json=use_json), use_json)


def cmd_vm_ssh(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_ssh as _ssh
    return _vm_call(_ssh, _qemu_ns(name=args.name, command=args.command), use_json)


def cmd_console_log(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_console_log as _log
    return _vm_call(_log, _qemu_ns(name=args.name, lines=args.lines), use_json)


def cmd_dmesg(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_dmesg as _dmesg
    return _vm_call(_dmesg, _qemu_ns(name=args.name, tail=args.tail), use_json)


def cmd_crash_collect(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_crash_collect as _crash_collect
    return _vm_call(
        _crash_collect,
        _qemu_ns(
            name=args.name,
            outdir=args.outdir,
            trigger=args.trigger,
            wait=args.wait,
            mod_dir=args.mod_dir,
        ),
        use_json,
    )


def cmd_nmi(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_nmi as _nmi
    return _vm_call(_nmi, _qemu_ns(name=args.name), use_json)


def cmd_snapshot(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_snapshot as _snapshot
    return _vm_call(_snapshot, _qemu_ns(name=args.name, tag=args.tag), use_json)


def cmd_restore(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_restore as _restore
    return _vm_call(_restore, _qemu_ns(name=args.name, tag=args.tag), use_json)


def cmd_doctor(args: argparse.Namespace) -> int:
    use_json = args.json
    err = _require_root(use_json)
    if err is not None:
        return err
    from ltvm_pkg.vm_commands import cmd_doctor as _doctor
    return _vm_call(_doctor, _qemu_ns(fix=args.fix), use_json)


def cmd_deploy(args: argparse.Namespace) -> int:
    use_json = args.json
    target = getattr(args, "target", None)
    kernel = getattr(args, "kernel", None)
    ltvm_root = Path(__file__).parent.parent

    err = _require_root(use_json)
    if err is not None:
        return err

    from ltvm_pkg.vm_state import VMInfo, VMNotFound
    from ltvm_pkg.vm_net import run_ssh

    # Get VM info
    try:
        vm = VMInfo.load(args.vm)
    except VMNotFound as e:
        return _error(str(e), use_json)

    # Auto-detect target from VM metadata
    if not target:
        target = vm.os_id or None
        if target and not use_json:
            print(f"  Auto-detected target: {target}")
        if not target:
            return _error(
                f"Cannot detect target OS for VM '{args.vm}'. "
                f"Pass --target explicitly.",
                use_json,
            )

    # Resolve kernel name and target config
    try:
        tc = TargetConfig(target)
        resolved_kernel = tc.resolve_kernel(kernel)
    except ValueError:
        tc = None
        resolved_kernel = kernel or "unknown"

    os_family = tc.os_family if tc else "rhel"

    # Resolve build path: explicit --build, packaged Lustre, or cwd
    build_arg = getattr(args, "build", ".")
    if build_arg == ".":
        packaged = (
            ltvm_root / "output" / target / "kernels" / resolved_kernel / "lustre"
        )
        if packaged.is_dir():
            build_path = packaged
            if not use_json:
                print(f"  Using bundled Lustre (from ltvm fetch)")
        else:
            build_path = Path(".").resolve()
    else:
        build_path = Path(build_arg).resolve()

    if not build_path.is_dir():
        return _error(f"Build path not found: {build_path}", use_json)

    # Auto-build Lustre unless .staging/ is already fresh.
    # Fresh = .staging/ exists AND no source file is newer than the
    # staging directory itself.  We use an exclude list (build artifacts,
    # generated files, VCS dirs) rather than an include list so that new
    # source languages (*.rs, *.py, etc.) are caught automatically.
    staging = build_path / ".staging"

    def _staging_is_fresh(staging: Path, src: Path) -> bool:
        if not staging.is_dir():
            return False
        # staging must actually contain built modules
        if not any(staging.rglob("*.ko")):
            return False
        # find any source file newer than .staging/ by excluding artifacts
        r = subprocess.run(
            [
                "find", str(src),
                # prune entire directories first (fast)
                "-path", str(staging), "-prune", "-o",
                "-path", "*/.git", "-prune", "-o",
                "-path", "*/autom4te.cache", "-prune", "-o",
                "-path", "*/_lpb", "-prune", "-o",
                "-path", "*/kconftest.dir", "-prune", "-o",
                # exclude build artifacts and generated files by name
                r"\(",
                "-name", "*.o",
                "-o", "-name", "*.ko",
                "-o", "-name", "*.a",
                "-o", "-name", "*.so",
                "-o", "-name", "*.so.*",
                "-o", "-name", "*.cmd",
                "-o", "-name", "*.d",
                "-o", "-name", "*.tmp_*",
                "-o", "-name", "conftest*",
                "-o", "-name", "config.log",
                "-o", "-name", "config.status",
                "-o", "-name", ".ltvm-*",
                # Makefile is generated from Makefile.am; Makefile.am is source
                "-o", "-name", "Makefile",
                r"\)", "-prune", "-o",
                # anything not excluded and newer than staging is a source hit
                "-newer", str(staging), "-print", "-quit",
            ],
            capture_output=True, text=True,
        )
        return r.stdout.strip() == ""  # no newer source files found

    staging_fresh = _staging_is_fresh(staging, build_path)

    if staging_fresh:
        if not use_json:
            print(f"  .staging/ is up to date, skipping build")
    else:
        build_cmd = ["ltvm", "build-lustre", target, "--lustre-tree", str(build_path)]
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            build_cmd = ["sudo", "-u", sudo_user] + build_cmd
        r = subprocess.run(build_cmd, capture_output=False)
        if r.returncode != 0:
            return _error(f"Lustre build failed (rc={r.returncode})", use_json)

    if not staging.is_dir():
        return _error(
            f"No .staging/ in {build_path} -- run: ltvm build-lustre {target}",
            use_json,
        )

    # Stream staging tree into the VM, unpacking directly into /
    tar_cmd = (
        f"tar cf - -C {shlex.quote(str(staging))} . "
        f"| sshpass -p {shlex.quote(ROOT_PASSWORD)} ssh "
        f"-o StrictHostKeyChecking=no -o LogLevel=ERROR "
        f"root@{shlex.quote(vm.ip)} 'tar xf - -C / --keep-directory-symlink'"
    )
    r = subprocess.run(
        ["bash", "-c", tar_cmd], capture_output=True, text=True, timeout=120
    )
    if r.returncode != 0:
        output = (r.stdout or "") + (r.stderr or "")
        return _error(f"Deploy failed: {output.strip()}", use_json)

    # depmod + ldconfig to pick up new modules and libraries
    try:
        r = run_ssh(vm.ip, "depmod -a && ldconfig", timeout=60)
        if r.returncode != 0:
            return _error(
                f"depmod/ldconfig failed (rc={r.returncode}): {r.stderr}", use_json
            )
    except Exception as e:
        return _error(f"depmod/ldconfig failed: {e}", use_json)

    # Configure test framework's local.sh with VM disk topology
    if vm.mdt_disks or vm.ost_disks:
        _configure_test_disks(
            vm.ip, vm.mdt_disks, vm.ost_disks, vm.disk_size,
            os_family=os_family,
        )

    if not use_json:
        print(f"  Deployed Lustre to {args.vm}")

    # Optionally mount Lustre
    if args.mount:
        rc = _lustre_mount_vm(args.vm, os_family)
        if rc != EXIT_OK:
            return rc
        if not use_json:
            print(f"  Lustre mounted on {args.vm}")

    return EXIT_OK


def cmd_exec(args: argparse.Namespace) -> int:
    use_json = args.json
    if not args.cmd:
        return _error("exec requires a command", use_json)

    err = _require_root(use_json)
    if err is not None:
        return err

    from ltvm_pkg.vm_commands import cmd_exec as _qexec

    cmd_str = " ".join(args.cmd)
    timeout = getattr(args, "timeout", 120)
    try:
        _qexec(_qemu_ns(
            name=args.vm,
            command=[cmd_str],
            timeout=timeout,
            json=use_json,
        ))
        return EXIT_OK
    except SystemExit as e:
        return int(e.code) if e.code is not None else EXIT_ERROR


def cmd_cluster(args: argparse.Namespace) -> int:
    use_json = args.json
    action = args.action
    cargs = args.cluster_args

    err = _require_root(use_json)
    if err is not None:
        return err

    from ltvm_pkg.vm_cluster import (
        cmd_cluster_create as _qc_create,
        cmd_cluster_destroy as _qc_destroy,
        cmd_cluster_deploy as _qc_deploy,
        cmd_cluster_status as _qc_status,
        cmd_cluster_exec as _qc_exec,
        cmd_cluster_list as _qc_list,
        cmd_cluster_ssh as _qc_ssh,
    )

    def _call(fn, ns):
        try:
            fn(ns)
            return EXIT_OK
        except SystemExit as e:
            return int(e.code) if e.code is not None else EXIT_ERROR

    if action == "create":
        if len(cargs) < 2:
            return _error(
                "cluster create requires a name and at least one node spec",
                use_json,
                hint="ltvm cluster create <name> <role:vm[:disks]> ...",
            )
        return _call(
            _qc_create,
            _qemu_ns(name=cargs[0], nodes=cargs[1:], vcpus=2, mem=4096),
        )

    if action == "destroy":
        if not cargs:
            return _error("cluster destroy requires a name", use_json)
        return _call(_qc_destroy, _qemu_ns(name=cargs[0]))

    if action == "deploy":
        if not cargs:
            return _error("cluster deploy requires a name", use_json)
        name = cargs[0]
        build_path = "."
        mount = False
        server_only = False
        i = 1
        while i < len(cargs):
            if cargs[i] == "--build" and i + 1 < len(cargs):
                build_path = cargs[i + 1]
                i += 2
            elif cargs[i] == "--mount":
                mount = True
                i += 1
            elif cargs[i] == "--server-only":
                server_only = True
                i += 1
            else:
                i += 1
        return _call(
            _qc_deploy,
            _qemu_ns(
                name=name,
                lustre_source=build_path,
                mount=mount,
                server_only=server_only,
            ),
        )

    if action == "status":
        if not cargs:
            return _error("cluster status requires a name", use_json)
        return _call(_qc_status, _qemu_ns(name=cargs[0]))

    if action == "exec":
        if len(cargs) < 3:
            return _error(
                "cluster exec requires a name, role, and command",
                use_json,
                hint="ltvm cluster exec <name> <role> '<cmd>'",
            )
        return _call(
            _qc_exec,
            _qemu_ns(
                name=cargs[0],
                target=cargs[1],
                command=cargs[2:],
                timeout=120,
                json=use_json,
            ),
        )

    if action == "list":
        return _call(_qc_list, _qemu_ns())

    if action == "ssh":
        if len(cargs) < 2:
            return _error(
                "cluster ssh requires a name and a target (role or vm name)",
                use_json,
                hint="ltvm cluster ssh <name> <role> [cmd...]",
            )
        return _call(
            _qc_ssh,
            _qemu_ns(name=cargs[0], target=cargs[1], command=cargs[2:]),
        )

    return _error(f"Unknown cluster action: {action}", use_json)


# ------------------------------------------------------------------
# Subcommand: validate
# ------------------------------------------------------------------


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


