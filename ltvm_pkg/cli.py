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
from ltvm_pkg.deploy import deploy_to_vm, lustre_mount_vm
from ltvm_pkg.image_build import build_image, image_status
from ltvm_pkg.kernel_build import build_kernel, kernel_status
from ltvm_pkg.lustre_build import build_lustre
from ltvm_pkg.lustre_compat import ValidationResult, validate_target
from ltvm_pkg.paths import load_meta_safe
from ltvm_pkg.release_package import (
    fetch_target,
    package_target,
    snapshot_lustre,
)
from ltvm_pkg.target_config import TargetConfig, list_targets

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_FOUND = 2

# GitHub repo for release downloads.  Override with LTVM_GITHUB_REPO
# so a fork can use `ltvm fetch` / `ltvm publish` without editing
# source.
GITHUB_REPO = os.environ.get("LTVM_GITHUB_REPO", "lustre-tools/lustre-test-vms")


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
    meta = load_meta_safe(meta_file)
    if meta is None:
        return {"built": False, "stale": True}
    stale = target_config.is_stale("container")
    return {"built": True, "stale": stale, **meta}


def _artifact_label(status_dict: dict[str, Any]) -> str:
    """Produce a human label like 'current', 'stale (config changed)',
    or 'not built'.

    `stale` may be None for kernel artifacts when called from cmd_status,
    which has no Lustre tree on hand to recompute the round-17
    Lustre-inputs hash -- in that case we can't honestly say whether the
    cached vmlinuz is stale, so we render "built (?)" rather than lying
    in either direction.
    """
    if not status_dict.get("built", False):
        return "not built"
    stale = status_dict.get("stale", False)
    if stale is None:
        return "built (?)"
    if stale:
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


# ------------------------------------------------------------------
# Subcommand: build-all
# ------------------------------------------------------------------


def _do_build_container(target_config: TargetConfig) -> str:
    """Run podman build for the build container and write meta.

    Delegates to kernel_build._ensure_container_image so the podman
    invocation lives in exactly one place.
    """
    from ltvm_pkg.kernel_build import _ensure_container_image

    tag = _ensure_container_image(target_config)
    target_config.write_meta("container", image_tag=tag)
    return tag


def cmd_build_all(args: argparse.Namespace) -> int:
    """Build container + kernel + image for a target.

    With --lustre-build, also builds the Lustre source tree
    against the freshly built kernel.
    """
    use_json = args.json
    tc, err = _load_target(
        args.target, use_json, arch=getattr(args, "arch", None)
    )
    if err is not None:
        return err
    assert tc is not None

    # build-all always requires a Lustre tree -- even for deb targets
    # where the kernel build itself doesn't need one, the surrounding
    # workflow (image inject, optional --lustre-build, packaging) does.
    lustre_tree, err_msg = _resolve_lustre_tree(args.lustre_tree)
    if err_msg:
        return _error(
            err_msg,
            use_json,
            hint="Run from a Lustre tree, or pass "
            "--lustre-tree /path/to/lustre-release",
        )
    assert lustre_tree is not None

    _gate_lustre_validation(
        tc, lustre_tree, force=getattr(args, "force_compat", False)
    )

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
        print(f"==> Building image for {args.target} (kernel={resolved_kernel})...")
    try:
        build_image(tc, force=args.force, kernel=kernel)
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
                target=args.target,
                enable_server=tc.server,
                extra_configure=list(tc.configure_args),
                jobs=getattr(args, "jobs", None),
                force=args.force,
                arch=tc.arch,
                kernel=resolved_kernel,
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
    tc, err = _load_target(
        args.target, use_json, arch=getattr(args, "arch", None)
    )
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
    tc, err = _load_target(
        args.target, use_json, arch=getattr(args, "arch", None)
    )
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
        _gate_lustre_validation(
            tc, lustre_tree, force=getattr(args, "force_compat", False)
        )

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
    tc, err = _load_target(
        args.target, use_json, arch=getattr(args, "arch", None)
    )
    if err is not None:
        return err
    assert tc is not None

    kernel = getattr(args, "kernel", None)
    resolved_kernel = tc.resolve_kernel(kernel)
    with_lustre = getattr(args, "with_lustre", None)

    if not use_json:
        extra = f" +lustre={with_lustre}" if with_lustre else ""
        print(
            f"Building image for {args.target} "
            f"(kernel={resolved_kernel}){extra}..."
        )

    try:
        path = build_image(
            tc,
            force=args.force,
            kernel=kernel,
            with_lustre=with_lustre,
        )
    except Exception as e:
        return _error(f"Image build failed: {e}", use_json)

    result = {
        "target": args.target,
        "kernel": resolved_kernel,
        "path": str(path),
        "with_lustre": with_lustre,
    }
    _output(result, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: build-lustre
# ------------------------------------------------------------------


def cmd_build_lustre(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target(
        args.target, use_json, arch=getattr(args, "arch", None)
    )
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

    _gate_lustre_validation(
        tc, lustre_tree, force=getattr(args, "force_compat", False)
    )

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

    # Pre-flight: check the container exists in podman storage and give a
    # clean, distinctive error if not.  build_lustre would otherwise raise
    # the same condition wrapped in "Lustre build failed: ...", which buries
    # the actionable hint.  This is the most common first-run error path,
    # so it gets first-class treatment.
    container_check = subprocess.run(
        ["podman", "image", "exists", container_tag],
        capture_output=True,
    )
    if container_check.returncode != 0:
        return _error(
            f"Build container '{container_tag}' not found in podman storage",
            use_json,
            hint=(
                f"Run: ltvm build-container {args.target}\n"
                f"  Or fetch a published target: ltvm fetch {args.target}"
            ),
        )

    try:
        meta = build_lustre(
            lustre_tree,
            build_tree,
            container_tag=container_tag,
            target=args.target,
            enable_server=enable_server,
            extra_configure=extra,
            jobs=jobs,
            force=getattr(args, "force", False),
            arch=tc.arch,
            kernel=resolved_kernel,
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
    tc, err = _load_target(
        args.target, use_json, arch=getattr(args, "arch", None)
    )
    if err is not None:
        return err
    assert tc is not None

    kernel = getattr(args, "kernel", None)

    # Bundling Lustre is mandatory by default.  The publisher's whole
    # job is to ship something a fetcher can immediately deploy from --
    # a "kernel-only" package without Lustre forces every consumer to
    # build it themselves on first deploy, which defeats the whole
    # point of the fetch flow.  --no-lustre is the explicit opt-out
    # for the "I just want a kernel package" case (e.g. publishing
    # an interim kernel for testing without rebuilding Lustre).
    no_lustre = getattr(args, "no_lustre", False)
    if not no_lustre:
        lustre_tree_arg = getattr(args, "lustre_tree", None)
        lustre_path, err_msg = _resolve_lustre_tree(lustre_tree_arg)
        if err_msg:
            return _error(
                err_msg,
                use_json,
                hint=(
                    "Run from a Lustre tree, pass --lustre-tree, or "
                    "use --no-lustre to publish a kernel-only package"
                ),
            )
        assert lustre_path is not None
        _gate_lustre_validation(
            tc, lustre_path, force=getattr(args, "force_compat", False)
        )
        if not use_json:
            print(f"Snapshotting Lustre tree from {lustre_path}...")
        try:
            snapshot_lustre(
                lustre_path,
                tc.output_dir,
                target=args.target,
                kernel=kernel,
                arch=tc.arch,
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


def _gh_api(endpoint: str) -> dict | list:
    """Call GitHub API and return parsed JSON.

    For list endpoints (e.g. "releases"), follows the Link: rel="next"
    pagination header so callers see the full result set.  GitHub's
    default per_page is 30; we ask for 100 to minimize round trips.
    Without this, anything past the first 30 releases vanished from
    `ltvm fetch --list` and produced "no release found" for older
    targets.
    """
    sep = "&" if "?" in endpoint else "?"
    url: str | None = (
        f"https://api.github.com/repos/{GITHUB_REPO}/{endpoint}{sep}per_page=100"
    )
    aggregated: list = []
    first_result: dict | list | None = None

    while url:
        # -D - dumps headers to stdout, then \r\n\r\n separates headers from body.
        try:
            r = subprocess.run(
                ["curl", "-fsSL", "--max-time", "30", "-D", "-", url],
                capture_output=True,
                text=True,
                timeout=35,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"GitHub API timed out after {e.timeout}s: {url}"
            )
        if r.returncode != 0:
            raise RuntimeError(
                f"GitHub API failed (rc={r.returncode}): {url}\n  {r.stderr.strip()}"
            )
        # Split on the last blank line: headers above, body below.
        # curl -D - prints all header blocks (including any redirects)
        # before the body.  Although HTTP uses \r\n\r\n as the separator
        # on the wire, subprocess text=True mode runs universal-newline
        # decoding, which translates \r\n to \n -- so in r.stdout the
        # separator is \n\n.  rpartition takes the LAST match so we keep
        # only the final response when curl followed redirects.
        headers, _, body = r.stdout.rpartition("\n\n")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            snippet = body[:200].replace("\n", " ")
            raise RuntimeError(
                f"GitHub API returned non-JSON: {url}\n  {e}\n  body: {snippet}"
            )

        if first_result is None:
            first_result = data
        if isinstance(data, list):
            aggregated.extend(data)
        else:
            # Non-list endpoint -- pagination doesn't apply, return as-is.
            assert isinstance(data, dict)
            return data

        url = _gh_next_link(headers)

    return (
        aggregated if isinstance(first_result, list) else (first_result or {})
    )


def _gh_next_link(headers: str) -> str | None:
    """Extract the rel="next" URL from a Link header, or None.

    Link header looks like:
        Link: <https://api.github.com/...?page=2>; rel="next", <...>; rel="last"
    """
    for line in headers.splitlines():
        if not line.lower().startswith("link:"):
            continue
        # Each comma-separated entry: <URL>; rel="..."
        for entry in line[5:].split(","):
            entry = entry.strip()
            if 'rel="next"' not in entry:
                continue
            lt = entry.find("<")
            gt = entry.find(">")
            if lt != -1 and gt != -1 and gt > lt:
                return entry[lt + 1 : gt]
    return None


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
        # Require exact match or a separator after the target name so
        # `rocky9` doesn't match a hypothetical `rocky90-...` tag.
        if tag != target and not tag.startswith(target + "-"):
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
        if target and tag != target and not tag.startswith(target + "-"):
            continue
        assets = [
            a["name"]
            for a in rel.get("assets", [])
            if a["name"].endswith((".tar.zst", ".tar.gz"))
        ]
        size_mb = sum(a.get("size", 0) for a in rel.get("assets", [])) / (
            1024 * 1024
        )
        result.append(
            {
                "tag": tag,
                "date": rel.get("published_at", "")[:10],
                "assets": assets,
                "size_mb": round(size_mb),
            }
        )
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
    if "/releases/download/" in url:
        release_tag = url.split("/releases/download/")[1].split("/")[0]
    else:
        release_tag = ""
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
        print(
            f"  sudo ltvm create co1-test --os {target}{arch_flag} "
            f"--vcpus 2 --mem 2048 --mdt-disks 1 --ost-disks 2"
        )
        print("  sudo ltvm deploy co1-test --mount")

    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: publish
# ------------------------------------------------------------------


def cmd_publish(args: argparse.Namespace) -> int:
    """Upload a packaged tarball to a GitHub release."""
    use_json = args.json
    tc, err = _load_target(
        args.target, use_json, arch=getattr(args, "arch", None)
    )
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
            c
            for c in sorted(tc.output_dir.parent.glob(pattern))
            if c.suffix in (".gz", ".zst")
            or c.name.endswith(".tar.gz")
            or c.name.endswith(".tar.zst")
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

    # Generate tag if not provided.  For "foo.tar.zst" / "foo.tar.gz",
    # tarball.stem is "foo.tar", so .replace(".tar", "") is enough --
    # the suffix is already gone by the time we look at the stem.
    if not tag:
        tag = tarball.stem.replace(".tar", "")

    # Create release + upload via gh CLI
    if not use_json:
        print(f"  Tag: {tag}")
        print(f"  Tarball: {tarball}")
        print(f"  Size: {tarball.stat().st_size / (1024 * 1024):.0f} MB")

    # Create release.  An "already exists" error is fine (we'll just
    # upload to the existing release below); any other failure is
    # fatal -- previously the return code was completely unchecked,
    # so an auth error would silently produce a confusing upload
    # failure two lines down.
    try:
        create = subprocess.run(
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
            text=True,
        )
    except FileNotFoundError:
        return _error(
            "gh CLI not found",
            use_json,
            hint="install GitHub CLI: https://cli.github.com/",
        )
    if create.returncode != 0:
        gh_msg = (create.stderr or "") + (create.stdout or "")
        if "already exists" not in gh_msg:
            return _error(
                f"gh release create failed (rc={create.returncode}): "
                f"{gh_msg.strip()}",
                use_json,
            )

    # Upload asset
    try:
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
    except FileNotFoundError:
        return _error(
            "gh CLI not found",
            use_json,
            hint="install GitHub CLI: https://cli.github.com/",
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

    # Record the release tag locally so subsequent `ltvm fetch` knows
    # the artifacts already on disk match this release.  cmd_fetch
    # reads from OUTPUT_DIR/<target>/.ltvm-release-tag* (always at the
    # target root, not the arch subdir), so write it there too --
    # otherwise aarch64 publish writes to output/<t>/aarch64/... and
    # fetch never finds it.
    from ltvm_pkg.target_config import OUTPUT_DIR

    arch = getattr(args, "arch", None) or "x86_64"
    arch_suffix = f"-{arch}" if arch != "x86_64" else ""
    tag_file = OUTPUT_DIR / args.target / f".ltvm-release-tag{arch_suffix}"
    tag_file.parent.mkdir(parents=True, exist_ok=True)
    tag_file.write_text(tag + "\n")

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
    tc, err = _load_target(
        args.target, use_json, arch=getattr(args, "arch", None)
    )
    if err is not None:
        return err
    assert tc is not None

    tag = _build_container_tag(tc)
    mount_path = Path(args.path).resolve()

    if not mount_path.is_dir():
        return _error(f"Mount path not found: {mount_path}", use_json)

    # Check container image exists
    try:
        result = subprocess.run(
            ["podman", "image", "exists", tag], capture_output=True
        )
    except FileNotFoundError:
        return _error(
            "podman not found",
            use_json,
            hint="install podman or run `ltvm install` to set up the host",
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
        # Images are per-kernel: build one row per built kernel dir, plus
        # the default kernel (even if nothing is built yet) so the user
        # sees "not built" instead of an empty section.
        built_kernels = tc.available_kernels()
        kernels_to_report = list(built_kernels)
        if tc.default_kernel not in kernels_to_report:
            # available_kernels() lists full-version dir names; the
            # default is the short name.  Resolve it to the matching
            # full name if one exists, otherwise keep the short name.
            kernels_to_report.append(tc.resolve_kernel(None))
        if not kernels_to_report:
            kernels_to_report = [tc.resolve_kernel(None)]
        images = {k: image_status(tc, kernel=k) for k in kernels_to_report}
        all_status[name] = {
            "container": cs,
            "kernel": ks,
            "images": images,
        }

    if use_json:
        print(json.dumps(all_status, indent=2))
    else:
        # Table output: one row per (target, kernel-image) pair.
        hdr = (
            f"{'Target':<12} {'Container':<14} {'Kernel':<26} "
            f"{'Image-Kernel':<44} {'Image':<14}"
        )
        print(hdr)
        print("-" * len(hdr))
        for name, st in all_status.items():
            c = _artifact_label(st["container"])
            k = _artifact_label(st["kernel"])
            for image_kernel, ims in st["images"].items():
                i = _artifact_label(ims)
                print(
                    f"{name:<12} {c:<14} {k:<26} "
                    f"{image_kernel:<44} {i:<14}"
                )

    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: validate (Lustre compatibility gate)
# ------------------------------------------------------------------


# Exit codes used by cmd_validate.  "refuse" is a first-class
# failure (1); "error" is reserved for parse / IO problems (2) so
# scripts can distinguish "Lustre says no" from "we couldn't even
# tell".
_VALIDATE_EXIT = {
    "ok": EXIT_OK,
    "best_effort": EXIT_OK,
    "refuse": EXIT_ERROR,
    "error": EXIT_NOT_FOUND,
}


def _validation_result_to_dict(r: ValidationResult) -> dict[str, Any]:
    return {
        "status": r.status,
        "mode": r.mode.value if r.mode is not None else None,
        "kernel_version": r.kernel_version,
        "matched_in": r.matched_in,
        "message": r.message,
    }


def cmd_validate(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target(args.target, use_json)
    if err is not None:
        return err
    assert tc is not None

    lustre_arg = getattr(args, "lustre_tree", None)
    if lustre_arg is None:
        default = Path.home() / "lustre-release"
        lustre_arg = str(default)
    lustre_tree, err_msg = _resolve_lustre_tree(lustre_arg)
    if err_msg:
        return _error(
            err_msg,
            use_json,
            hint="Pass --lustre-tree /path/to/lustre-release",
        )
    assert lustre_tree is not None

    result = validate_target(tc, lustre_tree)
    exit_code = _VALIDATE_EXIT[result.status]
    force = bool(getattr(args, "force", False))

    if use_json:
        print(json.dumps(_validation_result_to_dict(result), indent=2))
    else:
        tag = f"[{result.status}]"
        if result.status == "refuse" and force:
            print(f"--force: {tag} {result.message}")
        else:
            print(f"{tag} {result.message}")

    if result.status == "refuse" and force:
        return EXIT_OK
    return exit_code


def _gate_lustre_validation(
    tc: TargetConfig,
    lustre_tree: Path,
    *,
    force: bool,
) -> None:
    """Run validate_target as a gate before producing Lustre artifacts.

    Behavior by status:
      ok           silent pass
      best_effort  one-line stderr warning, pass
      refuse       print message; raise SystemExit(EXIT_ERROR) unless
                   force is True (then print override line, pass)
      error        print message; raise SystemExit(EXIT_ERROR) regardless
                   of force -- parse/IO failures are not overridable
    """
    result = validate_target(tc, lustre_tree)
    if result.status == "ok":
        return
    if result.status == "best_effort":
        print(f"warning: [best_effort] {result.message}", file=sys.stderr)
        return
    if result.status == "refuse":
        if force:
            print(
                f"--force-compat: overriding refusal: {result.message}",
                file=sys.stderr,
            )
            return
        print(f"[refuse] {result.message}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    # "error": parse / IO problems.  Not force-able.
    print(f"[error] {result.message}", file=sys.stderr)
    raise SystemExit(EXIT_ERROR)


# ------------------------------------------------------------------
# Runtime: VM management
# ------------------------------------------------------------------


def _vm_call(fn: Any, ns: argparse.Namespace, use_json: bool) -> int:
    """Call a vm_commands function, catching SystemExit and VMNotFound.

    Honors the return code of the wrapped function so handlers like
    cmd_doctor can signal "issues found" via a non-zero exit.
    """
    from ltvm_pkg.vm_state import VMNotFound

    try:
        rc = fn(ns)
        return rc if isinstance(rc, int) else EXIT_OK
    except SystemExit as e:
        return int(e.code) if e.code is not None else EXIT_ERROR
    except VMNotFound as e:
        return _error(str(e), use_json)


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
        disk_size=args.disk_size,
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
        disk_size=args.disk_size,
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

    return _vm_call(
        _ssh, _qemu_ns(name=args.name, command=args.command), use_json
    )


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

    err = _require_root(use_json)
    if err is not None:
        return err

    from ltvm_pkg.vm_state import VMInfo, VMNotFound

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

    # Resolve kernel name and target config.  Pass vm.arch through so
    # the target's output_dir is arch-qualified -- otherwise an aarch64
    # VM looks for its kernel/staging under the x86_64 output paths and
    # fails to find anything.  We require a valid target here so a
    # missing entry in targets.yaml fails loudly instead of silently
    # falling back to RHEL paths.
    vm_arch = vm.arch or "x86_64"
    try:
        tc = TargetConfig(target, arch=vm_arch)
    except ValueError as e:
        return _error(
            f"Unknown target '{target}' for VM '{args.vm}': {e}",
            use_json,
            hint="Check `ltvm status` for valid targets.",
        )
    resolved_kernel = tc.resolve_kernel(kernel)
    os_family = tc.os_family

    # Resolve build path:
    #   1. Explicit --build PATH wins (including --build .)
    #   2. Otherwise, if a bundled snapshot from `ltvm fetch` exists,
    #      copy it into staging and use it directly (no source rebuild)
    #   3. Otherwise, fall back to cwd
    build_arg = getattr(args, "build", None)
    bundled_snapshot: Path | None = None
    if build_arg is not None:
        build_path = Path(build_arg).resolve()
    else:
        # Use tc.output_dir (arch-qualified) instead of a hand-built
        # ltvm_root/output/<target>/ path so the bundled-snapshot lookup
        # honors LTVM_ROOT and the /usr/local/bin/ltvm symlink resolution
        # AND finds the correct arch-qualified subdirectory.
        packaged = (
            tc.output_dir / "kernels" / resolved_kernel / "lustre-artifacts"
        )
        # A bundled snapshot is identified by the .ltvm-snapshot.json marker
        # written by snapshot_lustre.  It already has DESTDIR layout
        # (usr/, lib/modules/), so we can deploy it directly without
        # going through build-lustre.
        if packaged.is_dir() and (packaged / ".ltvm-snapshot.json").exists():
            bundled_snapshot = packaged
            build_path = packaged
            if not use_json:
                print("  Using bundled Lustre (from ltvm fetch)")
        else:
            build_path = Path(".").resolve()

    if not build_path.is_dir():
        return _error(f"Build path not found: {build_path}", use_json)

    # Validate that --build points at an actual Lustre source tree
    # before we try to feed it to `ltvm build-lustre`.  Skip this when
    # we picked up a bundled snapshot, which is a DESTDIR layout (usr/,
    # lib/modules/), not a source tree.  Without this validation a typo
    # like `--build /wrong/dir` produces a confusing error several
    # subprocess hops away inside the build container.
    if bundled_snapshot is None:
        missing = [
            n
            for n in ("configure.ac", "lustre", "lnet")
            if not (build_path / n).exists()
        ]
        if missing:
            return _error(
                f"--build:'{build_path}' does not look like a Lustre "
                f"source tree (missing: {', '.join(missing)})",
                use_json,
            )

    userspace_only = getattr(args, "userspace_only", False)

    # Staging now lives inside the lustre tree at
    # <build_path>/.ltvm-staging/<target>/<arch>/<kernel>/, per-kernel
    # so two kernels' userland (usr/sbin, etc.) coexist without
    # clobbering each other.  The kernel key comes from the VM's
    # actual kernel (falling back to the target's default) so a VM
    # created with a non-default kernel deploys the Lustre that was
    # built against that kernel.
    from ltvm_pkg.lustre_build import staging_path as _staging_path

    deploy_kernel = resolved_kernel
    if vm.kernel:
        vm_kernel_name = Path(vm.kernel).parent.name
        if vm_kernel_name:
            deploy_kernel = tc.resolve_kernel(vm_kernel_name)
    staging = _staging_path(
        build_path, target, arch=vm_arch, kernel=deploy_kernel
    )
    # If the bundled-snapshot path is involved we DON'T require a
    # pre-existing per-kernel staging -- the snapshot rsync below
    # populates it.  Otherwise, if the user is deploying against a
    # source tree without having built Lustre for this kernel, refuse
    # with a clear hint rather than falling through to an automatic
    # `ltvm build-lustre` that might target the wrong kernel.
    if bundled_snapshot is None and not userspace_only:
        legacy = _staging_path(
            build_path, target, arch=vm_arch, kernel=None
        )
        if not staging.is_dir() and legacy.is_dir() and any(
            legacy.rglob("*.ko")
        ):
            return _error(
                f"Lustre staging for kernel {deploy_kernel} is missing "
                f"at {staging}, but a legacy per-target staging exists "
                f"at {legacy}. Per-kernel staging is now required.",
                use_json,
                hint=(
                    f"Run: ltvm build-lustre {target} "
                    f"--kernel {deploy_kernel} --lustre-tree {build_path}"
                ),
            )

    # If we picked up a bundled snapshot, mirror it into staging
    # unconditionally.  Previously we skipped the mirror whenever
    # staging already contained .ko files, but that silently shipped
    # stale modules from an earlier `ltvm build-lustre` run under the
    # "Using bundled Lustre" banner -- the user thought they were
    # deploying what they fetched but actually got what was last built
    # locally.  rsync --delete is the right tool here: the bundled
    # snapshot is the declared source of truth when bundled_snapshot
    # is not None.
    if bundled_snapshot is not None:
        if not use_json:
            print(f"  Mirroring bundled snapshot into staging: {staging}")
        staging.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [
                "rsync",
                "-a",
                "--delete",
                str(bundled_snapshot) + "/",
                str(staging) + "/",
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            return _error(
                f"Failed to mirror bundled snapshot: {r.stderr.strip()}",
                use_json,
            )

    def _staging_is_fresh(staging: Path, src: Path) -> bool:
        """Check if the staging dir is newer than all source files.

        Uses an explicit `.ltvm-staging-stamp` file written at the end
        of a successful build_lustre run as the reference mtime, NOT
        the staging dir's own mtime: directory mtime only changes when
        entries are added/removed in that exact directory, so an
        in-place rewrite of an existing .ko file under
        lib/modules/.../extra/ leaves the top-level staging mtime
        unchanged and the freshness check would lie.
        """
        if not staging.is_dir():
            return False
        if not any(staging.rglob("*.ko")):
            return False
        stamp = staging / ".ltvm-staging-stamp"
        if not stamp.is_file():
            # Pre-stamp builds (or a build that crashed before writing
            # the stamp): treat as stale so we rebuild rather than
            # silently skip.
            return False
        # Staging is outside the source tree so the find exclusions are
        # simpler -- just skip build artifacts and VCS dirs.
        r = subprocess.run(
            [
                "find",
                str(src),
                "-path",
                "*/.git",
                "-prune",
                "-o",
                "-path",
                "*/autom4te.cache",
                "-prune",
                "-o",
                "-path",
                "*/_lpb",
                "-prune",
                "-o",
                "-path",
                "*/kconftest.dir",
                "-prune",
                "-o",
                "(",
                "-name",
                "*.o",
                "-o",
                "-name",
                "*.ko",
                "-o",
                "-name",
                "*.a",
                "-o",
                "-name",
                "*.so",
                "-o",
                "-name",
                "*.so.*",
                "-o",
                "-name",
                "*.cmd",
                "-o",
                "-name",
                "*.d",
                "-o",
                "-name",
                "*.tmp_*",
                "-o",
                "-name",
                "conftest*",
                "-o",
                "-name",
                "config.log",
                "-o",
                "-name",
                "config.status",
                "-o",
                "-name",
                ".ltvm-*",
                ")",
                "-prune",
                "-o",
                "-newer",
                str(stamp),
                "-print",
                "-quit",
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            return False  # treat find errors conservatively as stale
        return r.stdout.strip() == ""

    if userspace_only:
        if not staging.is_dir():
            return _error(
                f"No staging for {target} -- run: ltvm build-lustre {target}",
                use_json,
            )
        if not use_json:
            print("  Userspace-only deploy (skipping kernel modules)")
    elif bundled_snapshot is not None:
        # Bundled snapshot: staging was either just mirrored or already
        # populated.  Don't run _staging_is_fresh -- build_path here is
        # the snapshot's DESTDIR layout, NOT a Lustre source tree, so
        # falling through to `ltvm build-lustre --lustre-tree <snapshot>`
        # would error out with "not a Lustre source tree".
        if not use_json:
            print("  Using bundled staging, skipping source build")
    else:
        staging_fresh = _staging_is_fresh(staging, build_path)

        if staging_fresh:
            if not use_json:
                print("  Staging up to date, skipping build")
        else:
            _gate_lustre_validation(
                tc,
                build_path,
                force=getattr(args, "force_compat", False),
            )
            build_cmd = [
                "ltvm",
                "build-lustre",
                target,
                "--lustre-tree",
                str(build_path),
            ]
            # Forward the VM's actual kernel to build-lustre.  Without
            # this, a VM created with a non-default kernel rebuilds
            # Lustre against the target's *default* kernel tree, producing
            # modules that the running kernel can't load.  Cluster deploy
            # already does this; single-node deploy was missing it.
            if vm.kernel:
                kernel_name = Path(vm.kernel).parent.name
                if kernel_name:
                    build_cmd += ["--kernel", kernel_name]
            # Forward the VM's arch unconditionally so cross-arch builds
            # end up in the right staging dir and link against the right
            # toolchain.  Comparing against the literal "x86_64" was
            # wrong for a target whose default arch is something else:
            # an x86_64 VM built against an aarch64-default target would
            # then NOT forward --arch, and the inner build-lustre would
            # default to aarch64 and deploy the wrong modules.  Idempotent
            # for x86_64-default targets too, so just always forward.
            build_cmd += ["--arch", vm_arch]
            sudo_user = os.environ.get("SUDO_USER")
            if sudo_user:
                build_cmd = ["sudo", "-u", sudo_user] + build_cmd
            build_proc = subprocess.run(build_cmd, capture_output=False)
            if build_proc.returncode != 0:
                return _error(
                    f"Lustre build failed (rc={build_proc.returncode})",
                    use_json,
                )

            if not staging.is_dir() or not any(staging.rglob("*.ko")):
                return _error(
                    f"Lustre build succeeded but no staging with modules for {target}",
                    use_json,
                )

    try:
        deploy_to_vm(
            vm,
            staging,
            os_family=os_family,
            userspace_only=userspace_only,
        )
    except RuntimeError as e:
        return _error(str(e), use_json)

    # Record successful deploy.  Swallow VMNotFound: the deploy itself
    # already succeeded, so a concurrent `ltvm destroy` racing with the
    # final .info write shouldn't turn the whole command into a
    # traceback.  Round 17 made _update_fields raise instead of silently
    # no-op'ing, so we now explicitly handle the race here -- cmd_deploy
    # is dispatched directly (not through _vm_call), so without this
    # catch the exception leaks as a Python traceback to the user.
    import time as _time

    kver = vm.kver  # already set on boot; keep existing value
    try:
        vm.update_deploy(int(_time.time()), str(build_path), kver)
    except VMNotFound:
        if not use_json:
            print(
                f"  Warning: VM '{args.vm}' was destroyed mid-deploy; "
                f"metadata not recorded",
                file=sys.stderr,
            )

    if not use_json:
        print(f"  Deployed Lustre to {args.vm}")

    # Optionally mount Lustre
    if args.mount:
        rc = lustre_mount_vm(args.vm, os_family)
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
        _qexec(
            _qemu_ns(
                name=args.vm,
                command=[cmd_str],
                timeout=timeout,
                json=use_json,
            )
        )
        return EXIT_OK
    except SystemExit as e:
        return int(e.code) if e.code is not None else EXIT_ERROR


def cmd_llmount(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    err = _require_root(use_json)
    if err is not None:
        return err

    from ltvm_pkg.vm_commands import cmd_llmount as _qllmount

    timeout = getattr(args, "timeout", 300)
    cleanup = getattr(args, "cleanup", False)
    try:
        _qllmount(
            _qemu_ns(
                name=args.vm,
                timeout=timeout,
                cleanup=cleanup,
            )
        )
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
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_deploy as _qc_deploy,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_destroy as _qc_destroy,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_exec as _qc_exec,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_list as _qc_list,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_ssh as _qc_ssh,
    )
    from ltvm_pkg.vm_cluster import (
        cmd_cluster_status as _qc_status,
    )

    def _call(fn: Any, ns: argparse.Namespace) -> int:
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
                hint="ltvm cluster create <name> [--os TARGET] [--arch ARCH] "
                "[--vcpus N] [--mem MB] <role:vm[:disks]> ...",
            )
        # Parse optional flags out of cargs; remaining positionals are
        # name + node specs.
        vcpus = 2
        # mem=None means "let cmd_create resolve from os_arts.default_mem"
        # so cluster nodes inherit the per-target default (e.g. rocky10
        # needs 4096) instead of being silently overridden.
        mem: int | None = None
        os_target: str | None = None
        arch: str | None = None
        disk_size: str | None = None
        positional: list[str] = []
        i = 0
        while i < len(cargs):
            if cargs[i] == "--vcpus" and i + 1 < len(cargs):
                vcpus = int(cargs[i + 1])
                i += 2
            elif cargs[i] == "--mem" and i + 1 < len(cargs):
                mem = int(cargs[i + 1])
                i += 2
            elif cargs[i] == "--os" and i + 1 < len(cargs):
                os_target = cargs[i + 1]
                i += 2
            elif cargs[i] == "--arch" and i + 1 < len(cargs):
                arch = cargs[i + 1]
                i += 2
            elif cargs[i] == "--disk-size" and i + 1 < len(cargs):
                disk_size = cargs[i + 1]
                i += 2
            elif cargs[i].startswith("--"):
                return _error(
                    f"cluster create: unknown argument '{cargs[i]}'",
                    use_json,
                    hint="valid: --vcpus, --mem, --os, --arch, --disk-size",
                )
            else:
                positional.append(cargs[i])
                i += 1
        if len(positional) < 2:
            return _error(
                "cluster create requires a name and at least one node spec",
                use_json,
                hint="ltvm cluster create <name> [--os TARGET] [--arch ARCH] "
                "[--vcpus N] [--mem MB] <role:vm[:disks]> ...",
            )
        return _call(
            _qc_create,
            _qemu_ns(
                name=positional[0],
                nodes=positional[1:],
                vcpus=vcpus,
                mem=mem,
                os=os_target,
                arch=arch,
                disk_size=disk_size,
            ),
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
                return _error(
                    f"cluster deploy: unknown argument '{cargs[i]}'",
                    use_json,
                    hint="valid: --build PATH, --mount, --server-only",
                )
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
        )
    except RuntimeError as e:
        return _error(str(e), use_json)
    except Exception as e:
        return _error(f"Setup failed: {e}", use_json)

    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: update
# ------------------------------------------------------------------


def _ltvm_repo_root() -> Path:
    """Return the on-disk repo root for this ltvm checkout.

    `ltvm install` symlinks the entry-point script into ``/usr/local/bin``
    and then resolves that symlink at startup, so when the user runs
    ``ltvm update`` from an installed copy we still load ``ltvm_pkg``
    from the real checkout.  ``Path(__file__).resolve()`` follows any
    intermediate symlink and lands us in the real ``<repo>/ltvm_pkg/``,
    so the parent of the parent is the real repo root regardless of how
    ltvm was invoked.
    """
    # cli.py lives at <repo>/ltvm_pkg/cli.py
    return Path(__file__).resolve().parent.parent


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess:
    """Run a git command against ``repo`` and return the CompletedProcess."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=60,
    )


def _current_version() -> str:
    """Return the version string, recomputing fresh from disk.

    ``ltvm_pkg.__version__`` is captured at import time, so after a
    successful update we recompute via ``_compute_version`` to pick up
    the new git hash without forcing a reload.
    """
    from ltvm_pkg import _compute_version

    return _compute_version()


def cmd_update(args: argparse.Namespace) -> int:
    """Pull the latest ltvm from the upstream git remote.

    Refuses to act on a dirty working tree unless --force is given.
    Uses --ff-only so we never silently create a merge commit on the
    user's checkout.  Reports the old and new version on success.
    """
    use_json = args.json
    repo = _ltvm_repo_root()

    if not (repo / ".git").exists():
        return _error(
            f"{repo} is not a git checkout -- cannot update",
            use_json,
            hint="Reinstall ltvm by cloning "
            "https://github.com/lustre-tools/lustre-test-vms",
        )

    old_version = _current_version()

    # --check: just report whether an update is available
    if getattr(args, "check", False):
        try:
            _git(repo, "fetch", "--quiet")
        except subprocess.CalledProcessError as e:
            return _error(
                f"git fetch failed: {e.stderr.strip() or e}", use_json
            )
        try:
            behind = _git(
                repo, "rev-list", "--count", "HEAD..@{u}"
            ).stdout.strip()
        except subprocess.CalledProcessError as e:
            return _error(
                f"git rev-list failed: {e.stderr.strip() or e}",
                use_json,
                hint="Is the current branch tracking an upstream?",
            )
        n = int(behind or "0")
        result = {
            "version": old_version,
            "behind": n,
            "update_available": n > 0,
        }
        _output(result, use_json)
        return EXIT_OK

    # Refuse on dirty working tree unless forced
    if not getattr(args, "force", False):
        status = _git(repo, "status", "--porcelain").stdout
        if status.strip():
            return _error(
                "working tree has local changes -- refusing to update",
                use_json,
                hint="Commit or stash your changes, or pass --force",
            )

    try:
        _git(repo, "fetch", "--quiet")
    except subprocess.CalledProcessError as e:
        return _error(f"git fetch failed: {e.stderr.strip() or e}", use_json)

    try:
        pull = _git(repo, "pull", "--ff-only")
    except subprocess.CalledProcessError as e:
        return _error(
            f"git pull --ff-only failed: {e.stderr.strip() or e}",
            use_json,
            hint="The local branch has diverged from upstream. "
            "Resolve manually with git.",
        )

    # Refresh _build_info.py so the new short hash takes effect
    # immediately, even if the post-commit hook isn't installed.
    try:
        new_hash = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
        if new_hash:
            (repo / "ltvm_pkg" / "_build_info.py").write_text(
                '"""Auto-generated by ltvm update. Do not edit or commit."""\n\n'
                f'BUILD_HASH = "{new_hash}"\n'
            )
    except (subprocess.CalledProcessError, OSError):
        # Non-fatal: version reporting will fall back to the runtime
        # git rev-parse path.
        pass

    new_version = _current_version()

    result = {
        "old_version": old_version,
        "new_version": new_version,
        "changed": old_version != new_version,
        "git": pull.stdout.strip(),
    }
    if not use_json:
        if old_version == new_version:
            print(f"Already up to date at {new_version}")
        else:
            print(f"Updated ltvm: {old_version} -> {new_version}")
        if pull.stdout.strip():
            print(pull.stdout.strip())
    else:
        print(json.dumps(result, indent=2))
    return EXIT_OK
