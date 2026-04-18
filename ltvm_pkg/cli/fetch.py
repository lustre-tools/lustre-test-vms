"""Fetch / package / publish subcommands.

Covers:
  * `ltvm target fetch`   -- pull pre-built artifacts from a GitHub release
  * `ltvm package`        -- package built artifacts for later publish
  * `ltvm publish`        -- upload a packaged set to a GitHub release

Also owns the GitHub-API helpers (``_gh_api``, ``_gh_next_link``,
``_find_release_url``, ``_list_releases``), the release-tag-matching
regexes (``_RHEL_RE``, ``_KVER_PREFIX_RE``), and the kernel-release
signature helpers (``_kernel_release_signature``,
``_release_matches_kernel``).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from ltvm_pkg.release_package import (
    fetch_target,
    package_target,
    snapshot_lustre,
)

from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_OK,
    _error,
    _load_target,
    _load_target_args,
    _output,
)


def _cli_attr(name: str) -> Any:
    """Look up ``name`` on ``ltvm_pkg.cli`` at call time.

    Lets tests monkey-patch cross-module names (fetch_target,
    package_target, snapshot_lustre, _gh_api, _gh_release_upload,
    _find_release_url, _resolve_lustre_tree, _gate_lustre_validation,
    TargetConfig) on ltvm_pkg.cli and have cmd_* in this submodule
    observe the replacement.
    """
    import ltvm_pkg.cli as _cli

    return getattr(_cli, name)


# ------------------------------------------------------------------
# Subcommand: package
# ------------------------------------------------------------------


def cmd_package(args: argparse.Namespace) -> int:
    use_json = args.json
    tc, err = _load_target_args(args, use_json)
    if err is not None:
        return err
    assert tc is not None

    kernel = getattr(args, "kernel", None)
    variant = getattr(args, "variant", None) or "base"

    if not use_json:
        from ltvm_pkg.cli.util import _print_target_header

        _print_target_header(
            tc, kernel=kernel, variant=variant, action="Packaging"
        )

    # Bundling Lustre is mandatory by default.  The publisher's whole
    # job is to ship something a fetcher can immediately deploy from --
    # a "kernel-only" package without Lustre forces every consumer to
    # build it themselves on first deploy, which defeats the whole
    # point of the fetch flow.  --no-lustre is the explicit opt-out.
    no_lustre = getattr(args, "no_lustre", False)
    if not no_lustre:
        lustre_tree_arg = getattr(args, "lustre_tree", None)
        lustre_path, err_msg = _cli_attr("_resolve_lustre_tree")(lustre_tree_arg)
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
        if not tc.kernel_deb_source:
            _cli_attr("_gate_lustre_validation")(
                tc, lustre_path, force=args.force_compat
            )
        if not use_json:
            print(f"Snapshotting Lustre tree from {lustre_path}...")
        try:
            _cli_attr("snapshot_lustre")(
                lustre_path,
                tc.output_dir,
                target=args.target,
                kernel=kernel,
                arch=tc.arch,
                variant=variant,
            )
        except Exception as e:
            return _error(f"Lustre snapshot failed: {e}", use_json)

    if not use_json:
        v_hint = "" if variant == "base" else f" variant={variant}"
        print(f"Packaging {args.target}{v_hint}...")

    try:
        assets = _cli_attr("package_target")(
            args.target,
            tc.output_dir,
            kernel=kernel,
            dest_dir=getattr(args, "output", None),
            arch=tc.arch,
            variant=variant,
        )
    except Exception as e:
        return _error(f"Package failed: {e}", use_json)

    result = {
        "target": args.target,
        "variant": variant,
        "assets": {kind: str(p) for kind, p in assets.items()},
    }
    _output(result, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# GitHub API helpers
# ------------------------------------------------------------------


def _gh_api(endpoint: str) -> dict | list:
    """Call GitHub API and return parsed JSON.

    For list endpoints (e.g. "releases"), follows the Link: rel="next"
    pagination header so callers see the full result set.  GitHub's
    default per_page is 30; we ask for 100 to minimize round trips.
    Without this, anything past the first 30 releases vanished from
    `ltvm target fetch --list` and produced "no release found" for older
    targets.
    """
    import ltvm_pkg.cli as _cli

    sep = "&" if "?" in endpoint else "?"
    url: str | None = (
        f"https://api.github.com/repos/{_cli.GITHUB_REPO}/{endpoint}{sep}per_page=100"
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


_RHEL_RE = re.compile(r"rhel(\d+)\.(\d+)")
_KVER_PREFIX_RE = re.compile(r"^(\d+\.\d+)")


def _kernel_release_signature(kname: str) -> str | None:
    """Derive a substring found in release tag/asset names for ``kname``.

    Release asset names use the kernel uname-r suffix:
    - RHEL kernels contain ``elX_Y`` where X.Y is the distro minor
      version, so ``5.14-rhel9.5`` -> ``el9_5``.
    - Non-RHEL (e.g. Ubuntu) fall back to the leading ``MAJOR.MINOR``
      kernel version, so ``6.8-ubuntu2404`` -> ``6.8``.  This only
      discriminates if the target's ``kernels.available`` entries
      differ in their kernel-major version; same-major variants would
      collide and need a richer signature.

    Returns None if no signature can be derived; callers then skip
    kernel-name filtering.
    """
    m = _RHEL_RE.search(kname)
    if m:
        return f"el{m.group(1)}_{m.group(2)}"
    m = _KVER_PREFIX_RE.match(kname)
    if m:
        return m.group(1)
    return None


def _release_matches_kernel(rel: dict, signature: str, arch: str) -> bool:
    """True iff ``rel`` has an arch-matching asset whose name contains
    the kernel signature."""
    arch_match = f"-{arch}-"
    for asset in rel.get("assets", []):
        name = asset.get("name", "")
        if arch_match not in name:
            continue
        if signature in name:
            return True
    # Tag itself sometimes carries the signature (publish derives tag
    # from tarball stem, which contains the kernel_version).
    tag = rel.get("tag_name", "")
    return signature in tag


def _find_release_url(
    target: str,
    filter_str: str | None = None,
    arch: str = "x86_64",
    kernel_signature: str | None = None,
    variant: str = "base",
    mode: str = "ecosystem",
) -> str:
    """Find an asset download URL from GitHub releases.

    ``mode`` selects which asset to look for:

      * "ecosystem" (default) -- the manifest JSON produced by
        ``package_target``; ``fetch_target`` consumes this URL.
      * "bootable" -- a standalone bootable qcow2.zst produced by
        ``package_bootable``.

    The asset name encodes (target, arch, kver, [variant]); for a
    variant fetch we require the exact ``-<variant>`` suffix so a
    ``--variant mofed`` request doesn't silently grab the base
    asset and vice versa.
    """
    releases = _cli_attr("_gh_api")("releases")
    if not isinstance(releases, list):
        releases = [releases]

    arch_match = f"-{arch}-"
    if mode == "bootable":
        prefix = f"bootable-{target}{arch_match}"
        suffix = ".zst"
        # Bootable publish tags them as ``bootable-<target>-<arch>-<kver>[-<variant>]``,
        # NOT ``<target>-<arch>-...`` like the ecosystem path -- the two
        # release namespaces are intentionally separate (see cmd_publish
        # docstring).  Filter tags accordingly or we'd never match.
        tag_prefix = f"bootable-{target}"
    else:
        prefix = f"manifest-{target}{arch_match}"
        suffix = ".json"
        tag_prefix = target

    variant_tail = (
        f"-{variant}{suffix}" if variant != "base" else suffix
    )
    # For base variant we need to reject names that have any variant
    # suffix (e.g. `-mofed.json`); otherwise a base lookup could grab
    # a mofed asset.  We check by stripping the suffix and looking for
    # a '-' in what remains after the kver.  Simpler: require the name
    # to end with a kver-looking token (digits/dots) before suffix.
    for rel in releases:
        tag = rel.get("tag_name", "")
        if tag != tag_prefix and not tag.startswith(tag_prefix + "-"):
            continue
        if filter_str and filter_str not in tag:
            continue
        if kernel_signature and not _release_matches_kernel(
            rel, kernel_signature, arch
        ):
            continue
        for asset in rel.get("assets", []):
            name = asset.get("name", "")
            if not name.startswith(prefix):
                continue
            if not name.endswith(variant_tail):
                continue
            if variant == "base":
                # Reject names whose tail before the suffix ends with
                # a non-numeric `-<variant>` segment.  The kver
                # always ends in digits.dots; variant segments are
                # letters.
                stem = name[:-len(suffix)]
                last_seg = stem.rsplit("-", 1)[-1]
                if last_seg and not any(
                    ch.isdigit() for ch in last_seg
                ):
                    continue
            if kernel_signature and kernel_signature not in name:
                continue
            return str(asset["browser_download_url"])

    avail = [r.get("tag_name", "?") for r in releases]
    hint = f" matching '{filter_str}'" if filter_str else ""
    if kernel_signature:
        hint += f" kernel-signature={kernel_signature!r}"
    if variant != "base":
        hint += f" variant={variant!r}"
    kind = "published bootable image" if mode == "bootable" else "published artifacts"
    raise RuntimeError(
        f"No {kind} found for '{target}'{hint}\n"
        f"  Available releases: {', '.join(avail)}\n"
        f"  Try: ltvm target fetch --list"
    )


def _list_releases(
    target: str | None = None,
    kernel_signature: str | None = None,
    arch: str = "x86_64",
) -> list[dict]:
    """List available releases, optionally filtered by target prefix."""
    releases = _cli_attr("_gh_api")("releases")
    if not isinstance(releases, list):
        releases = [releases]
    result = []
    for rel in releases:
        tag = rel.get("tag_name", "")
        if target and tag != target and not tag.startswith(target + "-"):
            continue
        if kernel_signature and not _release_matches_kernel(
            rel, kernel_signature, arch
        ):
            continue
        assets = [
            a["name"]
            for a in rel.get("assets", [])
            if a["name"].endswith(".tar.gz")
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


def _native_arch() -> str:
    """Return the host machine's arch in ltvm's canonical naming.

    Maps ``amd64`` -> ``x86_64`` and ``arm64`` -> ``aarch64`` so the
    result is comparable to the ``arch`` field in ``targets.yaml``.
    Unknown values fall through so a pass-through-and-let-lookup-fail
    surfaces a useful error rather than a silent wrong default.
    """
    import platform

    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    return m or "x86_64"


def _lookup_release_date(release_tag: str) -> str:
    """Return ``YYYY-MM-DD`` for ``release_tag``, or ``""`` on failure.

    Used only by the divergent-local-copy refusal path to annotate the
    error message.  A failure here (network hiccup, unknown tag) must
    not block the fetch logic -- we fall back to showing just the tag
    without a date.
    """
    try:
        rel = _cli_attr("_gh_api")(f"releases/tags/{release_tag}")
    except Exception:
        return ""
    if not isinstance(rel, dict):
        return ""
    return (rel.get("published_at") or "")[:10]


def _tag_file_date(tag_file: Any) -> str:
    """Return ``YYYY-MM-DD`` of ``tag_file``'s mtime, or ``""`` on error.

    The tag file is written at fetch/publish time, so its mtime is a
    reasonable stand-in for "when the local copy was produced".
    """
    from datetime import datetime

    try:
        return datetime.fromtimestamp(
            tag_file.stat().st_mtime
        ).strftime("%Y-%m-%d")
    except OSError:
        return ""


def _compare_dates(local: str, remote: str) -> str:
    """Return ``"newer"``, ``"older"``, or ``""`` comparing ISO dates.

    Empty-string inputs (missing data) yield ``""`` so the caller
    degrades gracefully to a direction-less refusal message.
    """
    if not local or not remote:
        return ""
    if local > remote:
        return "newer"
    if local < remote:
        return "older"
    return ""


def _dry_run_report(
    url: str,
    *,
    target: str,
    arch: str,
    variant: str,
    mode: str,
    use_json: bool,
    existing_tag: str = "",
    release_tag: str = "",
) -> None:
    """Print (or JSON-emit) what a fetch would do without doing it.

    Shared by the ecosystem and bootable paths.  ``existing_tag`` /
    ``release_tag`` are ecosystem-only; the bootable path leaves them
    as the empty-string defaults because it doesn't track them.
    """
    if use_json:
        payload: dict[str, Any] = {
            "dry_run": True,
            "target": target,
            "arch": arch,
            "variant": variant,
            "mode": mode,
            "url": url,
        }
        if release_tag:
            payload["release_tag"] = release_tag
        if existing_tag:
            payload["existing_tag"] = existing_tag
        print(json.dumps(payload, indent=2))
        return

    print("Dry run -- no files will be downloaded or modified.")
    print(f"  url:        {url}")
    if release_tag:
        print(f"  release:    {release_tag}")
    if existing_tag:
        if existing_tag == release_tag:
            print(f"  local tag:  {existing_tag} (already up to date)")
        else:
            print(f"  local tag:  {existing_tag} (would be replaced)")


def cmd_fetch(args: argparse.Namespace) -> int:
    use_json = args.json
    url = getattr(args, "url", None)
    target = getattr(args, "target", None)
    filt = getattr(args, "filter", None)
    # Default arch to the native host arch so `ltvm target fetch rocky9`
    # picks x86_64 on an Intel host and aarch64 on an ARM host without
    # the user having to remember --arch.  Explicit --arch still wins.
    arch = getattr(args, "arch", None) or _native_arch()
    kernel = getattr(args, "kernel", None)
    variant = getattr(args, "variant", None) or "base"
    image_mode = bool(getattr(args, "image", False))
    dry_run = bool(getattr(args, "dry_run", False))

    # Load TargetConfig whenever a target is named.  We need it for
    # kernel validation (pre-existing), for resolving the default
    # kernel (used in the description), and for printing os/mode
    # details.  Without a target (e.g. `--list` across all targets)
    # we just skip the lookup.
    tc = None
    kernel_signature: str | None = None
    if kernel and not target:
        return _error(
            "--kernel requires a target (e.g. ltvm target fetch rocky9 "
            "--kernel 5.14-rhel9.5)",
            use_json,
        )
    if target:
        tc, err = _load_target(target, use_json, arch=arch)
        if err is not None:
            return err
        assert tc is not None
        if kernel:
            declared = tc.declared_kernels()
            if kernel not in declared:
                return _error(
                    f"--kernel {kernel!r} not in targets.yaml "
                    f"kernels.available for {target}",
                    use_json,
                    hint=f"Available: {', '.join(declared)}",
                )
            kernel_signature = _kernel_release_signature(kernel)
            if kernel_signature is None and not use_json:
                print(
                    f"warning: could not derive release signature from "
                    f"--kernel {kernel!r}; falling back to first match.",
                    file=sys.stderr,
                )

    # --list: show available releases
    if getattr(args, "list", False):
        try:
            releases = _list_releases(
                target, kernel_signature=kernel_signature, arch=arch
            )
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
        return _error("target required (e.g. ltvm target fetch rocky9)", use_json)

    from ltvm_pkg.target_config import ARTIFACTS_DIR

    # Print a target-description header so the user can see exactly
    # what they're about to pull (OS, arch, kernel, mode, variant)
    # before the bytes start flying.  JSON mode stays quiet -- the
    # result envelope is the machine-readable contract.
    assert tc is not None
    if not use_json:
        from ltvm_pkg.cli.util import _print_target_header
        _print_target_header(
            tc, kernel=kernel, variant=variant, action="Fetching"
        )

    # Bootable mode: fetch a single qcow2.zst asset, no manifest, no
    # ecosystem.  Handled up-front so we skip all the tag/ecosystem
    # plumbing below.
    if image_mode:
        from ltvm_pkg.release_package import fetch_bootable

        if not url:
            if not use_json:
                print(
                    f"Looking up bootable {target} ({variant}) "
                    f"from GitHub releases..."
                )
            try:
                url = _cli_attr("_find_release_url")(
                    target,
                    filter_str=filt,
                    arch=arch,
                    kernel_signature=kernel_signature,
                    variant=variant,
                    mode="bootable",
                )
            except RuntimeError as e:
                return _error(str(e), use_json)

        if dry_run:
            _dry_run_report(
                url, target=target, arch=arch, variant=variant,
                mode="bootable", use_json=use_json,
            )
            return EXIT_OK

        try:
            path = fetch_bootable(
                target, url, ARTIFACTS_DIR, arch=arch, variant=variant
            )
        except Exception as e:
            return _error(f"Fetch bootable failed: {e}", use_json)

        if not use_json:
            print(f"  Bootable disk at: {path}")
        _output(
            {"target": target, "variant": variant, "path": str(path),
             "mode": "bootable"},
            use_json,
        )
        return EXIT_OK

    # Resolve URL: explicit --url, or GitHub release lookup.  In the
    # normal (ecosystem) path we look for the manifest; fetch_target
    # pulls the rest of the assets from the same release by parsing
    # their names out of the manifest.
    if not url:
        if not use_json:
            print(f"Looking up {target} ({variant}) from GitHub releases...")
        try:
            url = _cli_attr("_find_release_url")(
                target,
                filter_str=filt,
                arch=arch,
                kernel_signature=kernel_signature,
                variant=variant,
                mode="ecosystem",
            )
        except RuntimeError as e:
            return _error(str(e), use_json)

    # Extract release tag from URL to check if already fetched.
    # URL: .../releases/download/<tag>/<filename>
    # The tag file lives under the arch-qualified output dir so each
    # arch tracks its own release independently.
    if "/releases/download/" in url:
        release_tag = url.split("/releases/download/")[1].split("/")[0]
    else:
        release_tag = ""
    tag_file = ARTIFACTS_DIR / target / arch / ".ltvm-release-tag"
    replace = bool(getattr(args, "replace", False))
    force = bool(getattr(args, "force", False))
    existing_tag = (
        tag_file.read_text().strip() if tag_file.exists() else ""
    )

    if dry_run:
        # Early return so --dry-run ALWAYS reports the resolved URL,
        # even in the no-op "already up to date" and divergent-tag
        # cases below.  Users expect --dry-run to show what fetch
        # sees, not get short-circuited by idempotency checks.
        _dry_run_report(
            url, target=target, arch=arch, variant=variant,
            mode="ecosystem", use_json=use_json,
            existing_tag=existing_tag, release_tag=release_tag,
        )
        return EXIT_OK

    if release_tag and existing_tag == release_tag:
        # Same tag already on disk.  Without --replace: no-op success.
        # With --replace but no --force: refuse, because the "clean
        # re-fetch" would produce identical bytes -- probably not
        # what the user meant to pay for.
        if replace and not force:
            return _error(
                f"local copy already at {release_tag}; "
                f"--replace would re-download identical bytes",
                use_json,
                hint="pass --force to re-fetch anyway",
            )
        if not replace:
            if not use_json:
                print(f"  Already up to date ({release_tag})")
            result = {"target": target, "path": str(ARTIFACTS_DIR / target / arch)}
            _output(result, use_json)
            return EXIT_OK
    elif release_tag and existing_tag and existing_tag != release_tag:
        # Different tag already on disk.  Silently extracting the new
        # release on top of (or alongside) the existing one mixes two
        # releases' files and leaves the output dir in a state that's
        # hard to reason about -- so refuse by default.  --replace opts
        # into a clean overwrite; --force bypasses the guard for
        # scripts that have already decided.
        if not replace and not force:
            remote_date = _lookup_release_date(release_tag)
            local_date = _tag_file_date(tag_file)
            local_desc = existing_tag + (
                f" (fetched {local_date})" if local_date else ""
            )
            remote_desc = release_tag + (
                f" (published {remote_date})" if remote_date else ""
            )
            direction = _compare_dates(local_date, remote_date)
            if direction == "newer":
                header = "local copy is NEWER than remote release"
            elif direction == "older":
                header = "local copy is older than remote release"
            else:
                header = "local copy differs from remote release"
            return _error(
                f"{header}:\n"
                f"    local:  {local_desc}\n"
                f"    remote: {remote_desc}",
                use_json,
                hint=(
                    "pass --replace to overwrite with the remote "
                    "release, or leave the local copy as-is"
                ),
            )

    if dry_run:
        _dry_run_report(
            url, target=target, arch=arch, variant=variant,
            mode="ecosystem", use_json=use_json,
            existing_tag=existing_tag, release_tag=release_tag,
        )
        return EXIT_OK

    # --replace: wipe the target's output dir so a partial or
    # mismatched prior fetch doesn't leave stale files behind the
    # new extraction.  The reference directory is target/arch, not
    # target/, because per-arch fetches share artifacts/<target>/.
    if replace:
        target_out = ARTIFACTS_DIR / target / arch
        if target_out.exists():
            if not use_json:
                print(f"  Removing existing {target_out}...")
            import shutil as _shutil
            _shutil.rmtree(target_out)

    if not use_json:
        print(f"Fetching {target}...")

    try:
        target_dir = _cli_attr("fetch_target")(
            target, url, ARTIFACTS_DIR, arch=arch, variant=variant
        )
        # Record the release tag so repeat fetches are instant
        tag_file.parent.mkdir(parents=True, exist_ok=True)
        tag_file.write_text(release_tag + "\n")
    except Exception as e:
        # A schema-mismatch error means the published manifest was
        # produced by a newer (or older) ltvm.  Force an immediate
        # update-check prompt so the user can self-heal instead of
        # just reading a cryptic error.
        if "unrecognized manifest schema" in str(e) and not use_json:
            try:
                from ltvm_pkg.update_check import maybe_check_for_updates

                maybe_check_for_updates(force=True, use_json=False)
            except (ImportError, OSError):
                # The update check is advisory -- a missing module
                # or a network/IO hiccup must not mask the actual
                # fetch failure we're already reporting.  Genuine
                # programming bugs (TypeError, AttributeError) still
                # propagate so they surface with a traceback.
                pass
        return _error(f"Fetch failed: {e}", use_json)

    result = {"target": target, "path": str(target_dir)}
    _output(result, use_json)

    if not use_json:
        print()
        print("Next:")
        arch_flag = f" --arch {arch}" if arch != "x86_64" else ""
        print(
            f"  sudo ltvm create co1-test --target {target}{arch_flag} "
            f"--vcpus 2 --mdt-disks 1 --ost-disks 2"
        )
        print("  ltvm llmount co1-test")
        try:
            TargetConfig = _cli_attr("TargetConfig")
            tc_hint = TargetConfig(target)
            avail = tc_hint.declared_kernels()
            default_k = tc_hint.default_kernel
        except (ValueError, FileNotFoundError):
            # Best-effort hint -- if the target isn't parseable we
            # just drop the "try another kernel" line rather than
            # failing the fetch that already succeeded.
            avail = []
            default_k = ""
        if len(avail) > 1:
            alt = next((k for k in avail if k != default_k), None)
            if alt is not None:
                print(f"  # or pass --kernel {alt} to select a different kernel")

    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: publish
# ------------------------------------------------------------------


def _gh_release_upload(
    tag: str, assets: list[Path], notes: str, use_json: bool
) -> tuple[int | None, str | None]:
    """Create (if needed) a GitHub release at ``tag`` and upload every
    path in ``assets`` to it.  Returns (exit_code, err_msg): on success,
    (None, None).  ``--clobber`` is set so re-runs overwrite prior
    uploads with the same asset name, which matches the rest of the
    publish flow.
    """
    import ltvm_pkg.cli as _cli

    try:
        create = subprocess.run(
            [
                "gh", "release", "create", tag,
                "--repo", _cli.GITHUB_REPO,
                "--title", tag,
                "--notes", notes,
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return EXIT_ERROR, "gh CLI not found (https://cli.github.com/)"
    if create.returncode != 0:
        gh_msg = (create.stderr or "") + (create.stdout or "")
        if "already exists" not in gh_msg:
            return EXIT_ERROR, (
                f"gh release create failed (rc={create.returncode}): "
                f"{gh_msg.strip()}"
            )

    for a in assets:
        if not use_json:
            size_mb = a.stat().st_size / (1024 * 1024)
            print(f"  Uploading {a.name} ({size_mb:.0f} MB)...")
        r = subprocess.run(
            [
                "gh", "release", "upload", tag, str(a),
                "--repo", _cli.GITHUB_REPO, "--clobber",
            ],
        )
        if r.returncode != 0:
            return EXIT_ERROR, (
                f"gh release upload failed (rc={r.returncode}) for {a.name}"
            )
    return None, None


def cmd_publish(args: argparse.Namespace) -> int:
    """Upload a packaged asset set to a GitHub release.

    Two modes, selected by ``--image``:

    * default: publishes the ecosystem -- every asset listed by the
      manifest produced by ``package_target`` (container, kernel, image,
      optional lustre) to a single release tagged ``<target>-<arch>-<kver>[-<variant>]``.
      The manifest is uploaded alongside so ``fetch`` can verify.

    * ``--image``: publishes only a bootable qcow2 (produced by
      ``ltvm target export`` earlier) to a dedicated release tagged
      ``bootable-<target>-<arch>-<kver>[-<variant>]``.  Completely
      separate from the ecosystem release so fetching one doesn't
      accidentally grab the other.
    """
    import ltvm_pkg.cli as _cli

    use_json = args.json
    tc, err = _load_target_args(args, use_json)
    if err is not None:
        return err
    assert tc is not None

    kernel = getattr(args, "kernel", None)
    variant = getattr(args, "variant", None) or "base"
    image_mode = bool(getattr(args, "image", False))
    tag = getattr(args, "tag", None)

    if not use_json:
        from ltvm_pkg.cli.util import _print_target_header

        action = "Publishing bootable" if image_mode else "Publishing"
        _print_target_header(
            tc, kernel=kernel, variant=variant, action=action
        )

    if image_mode:
        from ltvm_pkg.release_package import package_bootable

        if not use_json:
            print(f"Preparing bootable asset for {args.target}...")
        try:
            asset = package_bootable(
                args.target,
                tc.output_dir,
                kernel=kernel,
                arch=tc.arch,
                variant=variant,
            )
        except Exception as e:
            return _error(f"Package bootable failed: {e}", use_json)

        # Tag: derive from the filename, stripping .qcow2.zst so the
        # tag reads naturally.  Keeps bootable releases separate from
        # ecosystem releases (they use different tag prefixes).
        if not tag:
            name = asset.name
            for suffix in (".qcow2.zst", ".raw.zst", ".zst"):
                if name.endswith(suffix):
                    tag = name[: -len(suffix)]
                    break
            else:
                tag = asset.stem

        if not use_json:
            print(f"  Tag: {tag}")
        exit_code, err_msg = _cli_attr("_gh_release_upload")(
            tag, [asset],
            notes=(
                f"Bootable disk image for {args.target} ({variant}) -- "
                f"self-contained, no ltvm runtime required"
            ),
            use_json=use_json,
        )
        if exit_code is not None:
            return _error(err_msg or "upload failed", use_json)

        url = f"https://github.com/{_cli.GITHUB_REPO}/releases/tag/{tag}"
        if not use_json:
            print(f"  Published: {url}")
        _output(
            {"target": args.target, "tag": tag, "asset": str(asset),
             "url": url, "mode": "bootable"},
            use_json,
        )
        return EXIT_OK

    # --- Ecosystem publish: package, then upload every asset. ---
    # cmd_package did the work if the caller ran it first, but we
    # can't tell cheaply; re-run it inline so `publish` is always
    # self-sufficient (package_target is idempotent given fresh
    # artifacts -- it just recompresses).
    no_lustre = getattr(args, "no_lustre", False)
    if not no_lustre:
        lustre_tree_arg = getattr(args, "lustre_tree", None)
        lustre_path, err_msg = _cli_attr("_resolve_lustre_tree")(lustre_tree_arg)
        if err_msg:
            return _error(
                err_msg, use_json,
                hint=(
                    "Run from a Lustre tree, pass --lustre-tree, or "
                    "use --no-lustre for a kernel-only publish"
                ),
            )
        assert lustre_path is not None
        if not tc.kernel_deb_source:
            _cli_attr("_gate_lustre_validation")(
                tc, lustre_path, force=args.force_compat
            )
        if not use_json:
            print(f"Snapshotting Lustre tree from {lustre_path}...")
        try:
            _cli_attr("snapshot_lustre")(
                lustre_path, tc.output_dir,
                target=args.target, kernel=kernel,
                arch=tc.arch, variant=variant,
            )
        except Exception as e:
            return _error(f"Lustre snapshot failed: {e}", use_json)

    if not use_json:
        v_hint = "" if variant == "base" else f" variant={variant}"
        print(f"Packaging {args.target}{v_hint}...")
    try:
        assets = _cli_attr("package_target")(
            args.target, tc.output_dir,
            kernel=kernel, arch=tc.arch, variant=variant,
            dest_dir=getattr(args, "output", None),
        )
    except Exception as e:
        return _error(f"Package failed: {e}", use_json)

    # Tag: derive from the manifest filename (strips .json, natural
    # read).  Variant is embedded in the manifest name for free.
    if not tag:
        manifest_name = assets["manifest"].name
        tag = manifest_name[len("manifest-"): -len(".json")]

    if not use_json:
        print(f"  Tag: {tag}")
        print(f"  Assets: {len(assets)}")

    # Upload EVERYTHING including the manifest so fetch can find it.
    to_upload = list(assets.values())
    exit_code, err_msg = _cli_attr("_gh_release_upload")(
        tag, to_upload,
        notes=f"Pre-built artifacts for {args.target} (variant={variant})",
        use_json=use_json,
    )
    if exit_code is not None:
        return _error(err_msg or "upload failed", use_json)

    url = f"https://github.com/{_cli.GITHUB_REPO}/releases/tag/{tag}"
    if not use_json:
        print(f"  Published: {url}")

    # Record the release tag locally so subsequent `ltvm fetch` knows
    # the artifacts already on disk match this release.  Use
    # ``tc.output_dir`` rather than the module-level ARTIFACTS_DIR so a
    # test that patches TargetConfig automatically gets the redirected
    # path -- otherwise the write lands on the real filesystem while
    # the rest of the publish flow is mocked.
    tag_file = tc.output_dir / ".ltvm-release-tag"
    tag_file.parent.mkdir(parents=True, exist_ok=True)
    tag_file.write_text(tag + "\n")

    result = {
        "target": args.target,
        "variant": variant,
        "tag": tag,
        "assets": {k: str(p) for k, p in assets.items()},
        "url": url,
    }
    _output(result, use_json)
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: delete
# ------------------------------------------------------------------


def cmd_delete(args: argparse.Namespace) -> int:
    """Delete a target's artifacts.

    Default mode wipes local ``artifacts/<target>/<arch>/`` (same as
    ``ltvm target clean``).  ``--remote`` instead deletes the published
    GitHub release -- tag is either explicit (``--tag``) or resolved
    via the same lookup ``fetch`` uses (target + optional
    ``--kernel`` / ``--variant`` / ``--image``).
    """
    import ltvm_pkg.cli as _cli

    use_json = args.json
    remote = bool(getattr(args, "remote", False))
    yes = bool(getattr(args, "yes", False))

    if not remote:
        from ltvm_pkg.cli.build import (
            _dir_size_bytes,
            _format_bytes,
            cmd_clean as _cmd_clean,
        )
        from ltvm_pkg.target_config import ARTIFACTS_DIR

        target = args.target
        all_arches = bool(getattr(args, "all_arches", False))
        arch_flag = getattr(args, "arch", None)
        tc, err = _load_target(target, use_json, arch=arch_flag)
        if err is not None:
            return err
        assert tc is not None
        if all_arches and arch_flag:
            return _error(
                "--arch and --all-arches are mutually exclusive", use_json
            )
        preview_paths = (
            [ARTIFACTS_DIR / target]
            if all_arches
            else [ARTIFACTS_DIR / target / tc.arch]
        )
        existing = [(p, _dir_size_bytes(p)) for p in preview_paths if p.exists()]
        if not existing:
            for p in preview_paths:
                print(f"nothing to delete at {p}")
            return EXIT_OK
        if not use_json:
            from ltvm_pkg.cli.util import _print_target_header

            _print_target_header(
                tc,
                kernel=getattr(args, "kernel", None),
                variant=getattr(args, "variant", None) or "base",
                action="Deleting",
            )
            for p, sz in existing:
                print(f"  {p} ({_format_bytes(sz)})")
            total = sum(sz for _, sz in existing)
            print(f"  total: {_format_bytes(total)}")
        if not yes:
            if use_json:
                return _error(
                    "--yes required to delete in --json mode",
                    use_json,
                    hint=f"Would delete: "
                    f"{', '.join(str(p) for p, _ in existing)}",
                )
            try:
                reply = input("Proceed? [y/N]: ").strip().lower()
            except EOFError:
                reply = ""
            if reply not in ("y", "yes"):
                print("aborted")
                return EXIT_ERROR
        return _cmd_clean(args)

    target = args.target
    if not target:
        return _error("target required for --remote delete", use_json)

    tag = getattr(args, "tag", None)
    kernel = getattr(args, "kernel", None)
    variant = getattr(args, "variant", None) or "base"
    image_mode = bool(getattr(args, "image", False))
    arch = getattr(args, "arch", None) or _native_arch()
    cleanup_tag = bool(getattr(args, "cleanup_tag", False))

    if not tag:
        kernel_signature: str | None = None
        if kernel:
            tc, err = _load_target(target, use_json, arch=arch)
            if err is not None:
                return err
            assert tc is not None
            declared = tc.declared_kernels()
            if kernel not in declared:
                return _error(
                    f"--kernel {kernel!r} not in targets.yaml "
                    f"kernels.available for {target}",
                    use_json,
                    hint=f"Available: {', '.join(declared)}",
                )
            kernel_signature = _kernel_release_signature(kernel)
        mode = "bootable" if image_mode else "ecosystem"
        try:
            url = _cli_attr("_find_release_url")(
                target,
                arch=arch,
                kernel_signature=kernel_signature,
                variant=variant,
                mode=mode,
            )
        except RuntimeError as e:
            return _error(str(e), use_json)
        # URL: https://github.com/<repo>/releases/download/<tag>/<asset>
        marker = "/releases/download/"
        if marker not in url:
            return _error(
                f"could not derive tag from release URL: {url}", use_json
            )
        tag = url.split(marker, 1)[1].split("/", 1)[0]

    if not use_json:
        from ltvm_pkg.cli.util import _print_target_header

        tc, err = _load_target(target, use_json, arch=arch)
        if err is None and tc is not None:
            _print_target_header(
                tc,
                kernel=kernel,
                variant=variant,
                action="Deleting remote",
            )
        extra = " (and git tag)" if cleanup_tag else ""
        print(f"  release tag: {tag}{extra}")

    if not yes:
        return _error(
            f"refusing to delete remote release {tag!r} without --yes",
            use_json,
            hint="re-run with --yes to confirm",
        )

    cmd = [
        "gh", "release", "delete", tag,
        "--repo", _cli.GITHUB_REPO, "--yes",
    ]
    if cleanup_tag:
        cmd.append("--cleanup-tag")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return _error(
            "gh CLI not found (https://cli.github.com/)", use_json
        )
    if r.returncode != 0:
        return _error(
            f"gh release delete failed (rc={r.returncode}): "
            f"{(r.stderr or r.stdout).strip()}",
            use_json,
        )
    _output(
        {"target": target, "tag": tag, "deleted": True,
         "cleanup_tag": cleanup_tag},
        use_json,
    )
    return EXIT_OK
