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
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_OK,
    _error,
    _load_target,
    _load_target_args,
    _output,
    host_arch,
    resolve_arch,
)

log = logging.getLogger("ltvm")


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
_SLES_RE = re.compile(r"sles(\d+)sp(\d+)")
_KVER_PREFIX_RE = re.compile(r"^(\d+\.\d+)")
_KERNEL_RELEASE_TAILS = ("_lustre-default", "-lustre")


def _is_base_stem(stem: str, known_variants: list[str]) -> bool:
    """Whether a release asset's name, less its extensions, is a base
    release rather than a variant's.

    Two complementary rules, because neither alone is enough: an exact
    match against the variants the target declares (catches mofed-24,
    whose trailing segment "24" the heuristic below reads as a kver),
    plus a heuristic for variants we don't know about (a kver's last
    segment always carries a digit, a name like "-mofed" does not).
    Some kernel releases end in a word: the flavor of a SUSE kernel
    (6.4.0-150700.53.66_lustre-default), the CONFIG_LOCALVERSION of a
    Debian one (6.12.107-lustre).
    """
    if any(stem.endswith(f"-{v}") for v in known_variants):
        return False
    last_seg = stem.rsplit("-", 1)[-1]
    return not (
        last_seg
        and not any(ch.isdigit() for ch in last_seg)
        and not stem.endswith(_KERNEL_RELEASE_TAILS)
    )


def _kernel_release_signature(kname: str) -> str | None:
    """Derive a substring found in release tag/asset names for ``kname``.

    Release asset names use the kernel uname-r suffix:
    - RHEL kernels contain ``elX_Y`` where X.Y is the distro minor
      version, so ``5.14-rhel9.5`` -> ``el9_5``.
    - SLES releases start with the service pack, 6.4.0-150700.53.66
      for SLE 15 SP7, so ``6.4-sles15sp7`` -> ``-150700.``; SP6 and
      SP7 share the 6.4 kernel.
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
    m = _SLES_RE.search(kname)
    if m:
        return f"-{m.group(1)}{int(m.group(2)):02d}00."
    m = _KVER_PREFIX_RE.match(kname)
    if m:
        return m.group(1)
    return None


def _fetched_kernel_name(
    target: str, release_tag: str, arch: str
) -> str | None:
    """The declared kernel name whose signature matches ``release_tag``.

    Release tags carry the uname-r suffix, not the short name a user
    types, so this maps back: rocky9-x86_64-...el9_8_lustre ->
    5.14-rhel9.8.  None when nothing matches (an unparseable tag, or a
    target whose kernels share a signature).
    """
    if not release_tag:
        return None
    try:
        tc = _cli_attr("TargetConfig")(target, arch=arch)
        declared = tc.declared_kernels()
    except Exception:  # noqa: BLE001
        return None
    for name in declared:
        sig = _kernel_release_signature(name)
        if sig and sig in release_tag:
            return str(name)
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


def _checkout_key(
    target: str,
    arch: str,
    kernel: str | None,
    variant: str,
    memo: dict[tuple[str, str, str, str], str | None] | None = None,
) -> str | None:
    """This checkout's release key for one kernel, or None when it
    cannot be computed (no kernel, or a target this checkout does not
    declare).  ``memo`` spares rehashing across one listing."""
    if not kernel:
        return None
    k = (target, arch, kernel, variant)
    if memo is not None and k in memo:
        return memo[k]
    from ltvm_pkg.release_tags import release_key

    try:
        key: str | None = release_key(target, arch, kernel, variant)
    except Exception as e:  # noqa: BLE001
        log.debug("no release key for %s: %s", k, e)
        key = None
    if memo is not None:
        memo[k] = key
    return key


def _manifest_identity(
    rel: dict, target: str, arch: str
) -> tuple[str, str] | None:
    """(manifest stem, variant) of ``rel``'s manifest for target/arch."""
    from ltvm_pkg.release_package import _declared_variant_names

    known = _declared_variant_names(target, arch)
    prefix = f"manifest-{target}-{arch}-"
    for a in rel.get("assets", []):
        name = a.get("name", "")
        if not (name.startswith(prefix) and name.endswith(".json")):
            continue
        stem = name[: -len(".json")]
        if _is_base_stem(stem, known):
            return stem, "base"
        for v in known:
            if stem.endswith(f"-{v}"):
                return stem, v
    return None


def _declared_kernel(
    target: str,
    arch: str,
    kernel: str,
    memo: dict[tuple[str, str, str], str] | None = None,
) -> str:
    """``kernel`` as targets.yaml declares it: a full kernel dir name
    (``5.14-rhel9.7-5.14.0-611.13.1.el9_7``) becomes ``5.14-rhel9.7``."""
    k = (target, arch, kernel)
    if memo is not None and k in memo:
        return memo[k]
    try:
        tc = _cli_attr("TargetConfig")(target, arch=arch)
        name = str(tc._short_kernel_name(kernel))
    except Exception:  # noqa: BLE001
        name = kernel
    if memo is not None:
        memo[k] = name
    return name


def _named_fits(
    notes: Any,
    target: str,
    arch: str,
    variant: str,
    kernel: str | None,
    memo: dict[tuple[str, str, str], str] | None = None,
) -> bool | None:
    """Whether a release whose notes name what it holds is this target,
    arch, variant and (when given) declared kernel; None when its notes
    do not say, and only its asset names can."""
    named = notes.identity()
    if named is None:
        return None
    if named[0] != target or named[1] != arch or named[3] != variant:
        return False
    if kernel is None:
        return True
    return _declared_kernel(target, arch, named[2], memo) == _declared_kernel(
        target, arch, kernel, memo
    )


def _release_checkout_match(
    rel: dict,
    target: str,
    arch: str,
    memo: dict[tuple[str, str, str, str], str | None] | None = None,
) -> bool | None:
    """Whether ``rel`` was built from this checkout's inputs: None when
    it carries no key or its kernel cannot be named."""
    from ltvm_pkg.release_tags import Publisher, tag_key

    key = tag_key(rel.get("tag_name", ""))
    if key is None:
        return None
    notes = Publisher.of(rel)
    named = notes.identity()
    kernel: str | None
    if named is not None:
        if named[:2] != (target, arch):
            return None
        kernel, variant = named[2], named[3]
    else:
        ident = _manifest_identity(rel, target, arch)
        if ident is None:
            return None
        stem, variant = ident
        kernel = _fetched_kernel_name(target, stem, arch)
    want = _checkout_key(target, arch, kernel, variant, memo)
    if want is None:
        return None
    return key == want


def _find_release_url(
    target: str,
    filter_str: str | None = None,
    arch: str = "x86_64",
    kernel_signature: str | None = None,
    variant: str = "base",
    mode: str = "ecosystem",
    kernel: str | None = None,
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

    In ecosystem mode the releases of both tag schemes are candidates,
    and the one keyed with this checkout's inputs wins (see
    release_tags.pick_release).  ``kernel`` names the kernel the key is
    computed for; without it each release's own kernel is used.
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

    # Decide the variant from the asset's *stem* rather than matching a
    # tail against the whole name.  Two reasons:
    #
    #  - bootable assets are named <...>-<kver>[-<variant>].<ext>.zst,
    #    so the variant is not adjacent to the trailing .zst; matching
    #    `-<variant>.zst` meant a published variant bootable image
    #    could never be found.
    #  - the old base-variant rejection asked whether the last dashed
    #    segment contains a digit.  rocky9's only declared variant is
    #    `mofed-24`, whose last segment is `24` -- so a base lookup
    #    happily accepted a MOFED asset.
    #
    # Comparing against the variants the target actually declares is
    # exact, and needs no guessing about which tokens look like a kver.
    from ltvm_pkg.release_package import _declared_variant_names

    known_variants = _declared_variant_names(target, arch)

    def _asset_stem(name: str) -> str:
        """Asset name minus its format/compression extensions."""
        stem = name[: -len(suffix)] if name.endswith(suffix) else name
        if mode == "bootable":
            # ...<kver>[-<variant>].qcow2 / .raw
            dot = stem.rfind(".")
            if dot > 0:
                stem = stem[:dot]
        return stem

    from ltvm_pkg.release_tags import (
        UPDATE_HINT,
        Publisher,
        manifest_names,
        pick_release,
        selectable,
        tag_is_for,
        this_ltvm_version,
        visible,
    )

    def _tag_fits(tag: str) -> bool:
        if mode == "bootable":
            return tag == tag_prefix or tag.startswith(tag_prefix + "-")
        return tag_is_for(tag, target)

    # (tag, manifest stem, url, publishing ltvm) of each fitting
    # release, newest first.
    candidates: list[tuple[str, str, str, Publisher]] = []
    names: dict[tuple[str, str, str], str] = {}
    # Newest published first, whole releases only; the bootable
    # namespace keeps the API's order.
    usable = visible(releases) if mode == "bootable" else selectable(releases)
    for rel in usable:
        tag = rel.get("tag_name", "")
        if not _tag_fits(tag):
            continue
        if filter_str and filter_str not in tag:
            continue
        # A release whose notes name its kernel and variant is chosen by
        # them; the name's signature is for those published before.
        fits = (
            None
            if mode == "bootable"
            else _named_fits(
                Publisher.of(rel), target, arch, variant, kernel, names
            )
        )
        if fits is False:
            continue
        # One manifest per release: notes name one kernel and variant,
        # so a release holding two cannot say which is which.
        if fits and len(manifest_names(rel)) != 1:
            continue
        by_name = fits is None or (kernel is None and kernel_signature)
        if (
            by_name
            and kernel_signature
            and not _release_matches_kernel(rel, kernel_signature, arch)
        ):
            continue
        for asset in rel.get("assets", []):
            name = asset.get("name", "")
            if not name.startswith(prefix):
                continue
            if not name.endswith(suffix):
                continue
            stem = _asset_stem(name)
            if fits is None:
                if variant == "base":
                    if not _is_base_stem(stem, known_variants):
                        continue
                elif not stem.endswith(f"-{variant}"):
                    continue
            if by_name and kernel_signature and kernel_signature not in name:
                continue
            candidates.append(
                (
                    tag,
                    stem,
                    str(asset["browser_download_url"]),
                    Publisher.of(rel),
                )
            )
            break
        if mode == "bootable" and candidates:
            return candidates[0][2]

    memo: dict[tuple[str, str, str, str], str | None] = {}

    def _expected(i: int) -> str | None:
        # The declared kernel the notes name: a moving one (mainline's
        # stable) cannot be read back from the version in the name.
        notes = candidates[i][3]
        k = (
            notes.kernel
            or kernel
            or _fetched_kernel_name(target, candidates[i][1], arch)
        )
        return _checkout_key(target, arch, k, variant, memo)

    mine = this_ltvm_version() if candidates else None
    pick = pick_release(
        [c[0] for c in candidates],
        [c[3] for c in candidates],
        _expected,
        mine,
    )
    if pick.index is not None:
        chosen = candidates[pick.index]
        if not pick.matched:
            print(
                f"note: no published release of {target} ({variant}) is "
                f"keyed with this checkout's inputs; fetching {chosen[0]}, "
                f"built from other inputs, so `ltvm build status` may show "
                f"it stale.",
                file=sys.stderr,
            )
        if pick.newer:
            print(
                f"warning: {chosen[0]} was published by ltvm {pick.newer}, "
                f"newer than this one ({mine}); {UPDATE_HINT}.",
                file=sys.stderr,
            )
        return chosen[2]

    avail = [r.get("tag_name", "?") for r in releases]
    hint = f" matching '{filter_str}'" if filter_str else ""
    if kernel_signature:
        hint += f" kernel-signature={kernel_signature!r}"
    if variant != "base":
        hint += f" variant={variant!r}"
    kind = (
        "published bootable image"
        if mode == "bootable"
        else "published artifacts"
    )
    raise RuntimeError(
        f"No {kind} found for '{target}'{hint}\n"
        f"  Available releases: {', '.join(avail)}\n"
        f"  Try: ltvm target fetch --list"
    )


def _list_releases(
    target: str | None = None,
    kernel_signature: str | None = None,
    arch: str = "x86_64",
    kernel: str | None = None,
) -> list[dict]:
    """List available releases, optionally filtered by target prefix.

    With a target and ``kernel`` (a declared kernel), only that
    kernel's: a release whose notes name its kernel by them, as fetch
    chooses, any other by ``kernel_signature``.  Each entry says
    whether it was built from this checkout's inputs
    (``matches_checkout``: true, false, or null for a release without a
    key, whose inputs cannot be told).
    """
    from ltvm_pkg.release_tags import (
        Publisher,
        notes_commit,
        notes_version,
        selectable,
        tag_is_for,
    )

    releases = _cli_attr("_gh_api")("releases")
    if not isinstance(releases, list):
        releases = [releases]
    result = []
    memo: dict[tuple[str, str, str, str], str | None] = {}
    names: dict[tuple[str, str, str], str] = {}
    for rel in selectable(releases):
        tag = rel.get("tag_name", "")
        if target and not tag_is_for(tag, target):
            continue
        named = Publisher.of(rel).identity()
        if target and kernel and named is not None:
            if named[:2] != (target, arch) or _declared_kernel(
                target, arch, named[2], names
            ) != _declared_kernel(target, arch, kernel, names):
                continue
        elif kernel_signature and not _release_matches_kernel(
            rel, kernel_signature, arch
        ):
            continue
        # ltvm publishes zstd-compressed assets (.tar.zst for
        # container/kernel/image/lustre, .qcow2.zst for bootable); the
        # manifest .json is metadata, not a download.  The old ".tar.gz"
        # filter predated the zstd switch and matched nothing, so
        # `fetch --list --json` always reported an empty assets list.
        assets = [
            a["name"]
            for a in rel.get("assets", [])
            if a["name"].endswith(".zst")
        ]
        size_mb = sum(a.get("size", 0) for a in rel.get("assets", [])) / (
            1024 * 1024
        )
        entry: dict[str, Any] = {
            "tag": tag,
            "date": (rel.get("published_at") or "")[:10],
            "ltvm_version": notes_version(rel.get("body")),
            "ltvm_commit": notes_commit(rel.get("body")),
            "assets": assets,
            "size_mb": round(size_mb),
        }
        if target:
            entry["matches_checkout"] = _release_checkout_match(
                rel, target, arch, memo
            )
        result.append(entry)
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
        return datetime.fromtimestamp(tag_file.stat().st_mtime).strftime(
            "%Y-%m-%d"
        )
    except OSError:
        return ""


def _republished_at(release_tag: str, tag_file: Any) -> str:
    """When ``release_tag``'s assets were re-uploaded after the local
    copy was fetched, the newest upload time (ISO); otherwise ``""``.

    A republish keeps the tag when the kernel version is unchanged (a
    config or image fix), so the tag alone cannot tell the local copy
    from the release.  GitHub moves an asset's ``updated_at`` when it is
    uploaded again; the tag file's mtime is when it was fetched.  Any
    failure reads as "not republished", the tag-only answer.
    """
    from datetime import datetime, timezone

    try:
        fetched = datetime.fromtimestamp(tag_file.stat().st_mtime, timezone.utc)
        rel = _cli_attr("_gh_api")(f"releases/tags/{release_tag}")
        uploads = [
            datetime.fromisoformat(a["updated_at"].replace("Z", "+00:00"))
            for a in rel.get("assets", [])
            if a.get("updated_at")
        ]
    except Exception:
        return ""
    newest = max(uploads, default=None)
    if newest is None or newest <= fetched:
        return ""
    return newest.isoformat(timespec="minutes")


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
    republished: str = "",
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
        if republished:
            payload["republished"] = republished
        print(json.dumps(payload, indent=2))
        return

    print("Dry run -- no files will be downloaded or modified.")
    print(f"  url:        {url}")
    if release_tag:
        print(f"  release:    {release_tag}")
    if existing_tag:
        if republished:
            print(
                f"  local tag:  {existing_tag} (republished {republished}; "
                f"--replace takes it)"
            )
        elif existing_tag == release_tag:
            print(f"  local tag:  {existing_tag} (already up to date)")
        else:
            print(f"  local tag:  {existing_tag} (would be replaced)")


def _replace_scope(
    target: str, arch: str, kver: str, variant: str
) -> list[Path]:
    """Directories --replace should clear for one (kernel, variant).

    The kernel directory is identified by its meta.json kernel_version
    rather than by matching substrings of the directory name, so this
    cannot pick the wrong sibling.  The container is per-target and is
    re-extracted by the same fetch, so clearing it is safe and keeps
    the "clean re-fetch" guarantee for everything this fetch writes.
    """
    from ltvm_pkg.paths import load_meta_safe
    from ltvm_pkg.target_config import ARTIFACTS_DIR, DEFAULT_VARIANT

    root = ARTIFACTS_DIR / target / arch
    victims: list[Path] = []

    container = root / "container"
    victims.append(
        container if variant == DEFAULT_VARIANT else container / variant
    )

    kernels = root / "kernels"
    if kver and kernels.is_dir():
        for kdir in kernels.iterdir():
            if not kdir.is_dir():
                continue
            meta = load_meta_safe(kdir / "meta.json")
            if meta is None or meta.get("kernel_version") != kver:
                continue
            # The kernel itself is variant-independent; only drop it
            # for a base fetch, which is what re-extracts it.
            if variant == DEFAULT_VARIANT:
                victims.append(kdir)
            img = root / "images" / kdir.name
            victims.append(img if variant == DEFAULT_VARIANT else img / variant)
    return victims


def cmd_fetch(args: argparse.Namespace) -> int:
    use_json = args.json
    url = getattr(args, "url", None)
    target = getattr(args, "target", None)
    filt = getattr(args, "filter", None)
    # Default arch to the native host arch so `ltvm target fetch rocky9`
    # picks x86_64 on an Intel host and aarch64 on an ARM host without
    # the user having to remember --arch.  Explicit --arch still wins.
    # Explicit --arch, else the target's declared arch when it states
    # one (rocky9-64k is aarch64-only), else the host's.  Plain
    # _native_arch() looked for an x86_64 release of an aarch64-only
    # target, and disagreed with what `target clean` would remove.
    arch = resolve_arch(args, target) or _native_arch()
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
    default_signature: str | None = None
    default_unavailable = False
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
            if kernel_signature is None:
                # Never suppress this under --json.  mainline's moving
                # aliases (latest / stable / longterm) match neither
                # the rhelX.Y nor the MAJOR.MINOR pattern, so
                # `target fetch mainline --kernel latest --json`
                # fetched whichever release GitHub happened to list
                # first -- plausibly the `stable` one -- and said
                # nothing at all.  JSON output goes to stdout, so a
                # stderr warning cannot corrupt it.
                print(
                    f"warning: could not derive a release signature from "
                    f"--kernel {kernel!r}; falling back to the first "
                    f"published release for {target}, which may not be "
                    f"the kernel you asked for.",
                    file=sys.stderr,
                )
        else:
            # No --kernel: fetch the release for the target's *default*
            # kernel, which is the one `ltvm create` will ask for.
            # Taking the newest published release instead left a fresh
            # host holding artifacts its own create refuses to use --
            # "Default kernel ... is not built" immediately after a
            # fetch that said it succeeded.
            default_signature = _kernel_release_signature(tc.default_kernel)

    # --list: show available releases
    if getattr(args, "list", False):
        try:
            releases = _list_releases(
                target,
                kernel_signature=kernel_signature,
                arch=arch,
                kernel=kernel,
            )
        except RuntimeError as e:
            return _error(str(e), use_json)
        if use_json:
            _output(releases, use_json)
        else:
            if not releases:
                print("  (no releases found)")
            for r in releases:
                match = r.get("matches_checkout")
                note = (
                    ""
                    if match is None
                    else "  built from this checkout's inputs"
                    if match
                    else "  built from other inputs"
                )
                print(
                    f"  {r['tag']:<60s}  {r['size_mb']:>5d} MB  "
                    f"{r['date']}{note}"
                )
        return EXIT_OK

    if not target:
        return _error(
            "target required (e.g. ltvm target fetch rocky9)", use_json
        )

    from ltvm_pkg.storage import archived
    from ltvm_pkg.target_config import ARTIFACTS_DIR

    # tar replaces a symlink to a directory with a real directory when it
    # extracts one over it, so a fetch into an archived target would land
    # a partial tree in hot storage and orphan the cold one.
    cold = archived(ARTIFACTS_DIR / target / arch)
    if cold is not None and not dry_run:
        return _error(
            f"{target}/{arch} is archived in cold storage ({cold})",
            use_json,
            hint=f"ltvm target restore {target} --arch {arch}, then fetch",
        )

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
                url,
                target=target,
                arch=arch,
                variant=variant,
                mode="bootable",
                use_json=use_json,
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
            {
                "target": target,
                "variant": variant,
                "path": str(path),
                "mode": "bootable",
            },
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
        find = _cli_attr("_find_release_url")
        try:
            url = find(
                target,
                filter_str=filt,
                arch=arch,
                kernel_signature=kernel_signature or default_signature,
                variant=variant,
                mode="ecosystem",
                kernel=kernel or tc.default_kernel,
            )
        except RuntimeError as e:
            if kernel_signature or not default_signature:
                return _error(str(e), use_json)
            # The default kernel has no published release.  Take what
            # is published rather than refusing, but say so: the create
            # that follows needs --kernel for these artifacts.
            try:
                url = find(
                    target,
                    filter_str=filt,
                    arch=arch,
                    kernel_signature=None,
                    variant=variant,
                    mode="ecosystem",
                )
            except RuntimeError as e2:
                return _error(str(e2), use_json)
            default_unavailable = True
            print(
                f"warning: no published release for {target}'s default "
                f"kernel ({tc.default_kernel if tc else '?'}); fetching "
                f"another one -- `ltvm create` will need --kernel.",
                file=sys.stderr,
            )

    # Extract release tag from URL to check if already fetched.
    # URL: .../releases/download/<tag>/<filename>
    # The tag file lives under the arch-qualified output dir so each
    # arch tracks its own release independently.
    if "/releases/download/" in url:
        release_tag = url.split("/releases/download/")[1].split("/")[0]
    else:
        release_tag = ""
    # Tracked per (kernel, variant): fetching a second kernel is
    # additive, not a divergence.  Only a *different release of the
    # same kernel* is a real conflict.
    from ltvm_pkg.cli.util import (
        kver_from_release_tag,
        read_release_tag,
        release_tag_file,
        write_release_tag,
    )

    fetch_kver = (
        kver_from_release_tag(release_tag, target, arch, variant)
        if release_tag
        else ""
    )
    tag_root = ARTIFACTS_DIR / target / arch
    tag_file = release_tag_file(tag_root, fetch_kver, variant)
    replace = bool(getattr(args, "replace", False))
    force = bool(getattr(args, "force", False))
    existing_tag = (
        read_release_tag(tag_root, target, arch, fetch_kver, variant)
        if fetch_kver
        else ""
    )

    republished = (
        _republished_at(release_tag, tag_file)
        if release_tag and existing_tag == release_tag
        else ""
    )
    if dry_run:
        # Early return so --dry-run ALWAYS reports the resolved URL,
        # even in the no-op "already up to date" and divergent-tag
        # cases below.  Users expect --dry-run to show what fetch
        # sees, not get short-circuited by idempotency checks.
        _dry_run_report(
            url,
            target=target,
            arch=arch,
            variant=variant,
            mode="ecosystem",
            use_json=use_json,
            existing_tag=existing_tag,
            release_tag=release_tag,
            republished=republished,
        )
        return EXIT_OK

    if republished and not replace:
        return _error(
            f"{release_tag} was republished ({republished}) after the "
            f"local copy was fetched ({_tag_file_date(tag_file)})",
            use_json,
            hint="pass --replace to take the republished release",
        )
    if release_tag and existing_tag == release_tag and not republished:
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
            result = {
                "target": target,
                "path": str(ARTIFACTS_DIR / target / arch),
            }
            _output(result, use_json)
            return EXIT_OK
    elif release_tag and existing_tag and existing_tag != release_tag:
        # Different tag already on disk.  Silently extracting the new
        # release on top of (or alongside) the existing one mixes two
        # releases' files and leaves the output dir in a state that's
        # hard to reason about -- so refuse by default.  --replace opts
        # into a clean overwrite.
        #
        # --force alone must NOT bypass this: it skips the refusal but
        # not the cleanup, because _replace_scope only runs under
        # `if replace`.  The result was the exact mixed state the
        # refusal exists to prevent -- stale kernels/<old>/,
        # images/<old>/ and a half-overwritten container/ -- and
        # --force's own help only claims to mean "with --replace,
        # re-fetch even when the tag already matches".  Treat --force
        # on a divergent tag as implying --replace.
        if force and not replace:
            log.warning(
                "--force on a divergent release (%s -> %s): "
                "replacing the local copy rather than extracting over it",
                existing_tag,
                release_tag,
            )
            replace = True
        if not replace:
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

    # (No second --dry-run check here: the one above returns before any
    # of the idempotency branches, precisely so --dry-run always
    # reports the resolved URL.  A duplicate block used to sit here and
    # was dead code -- mypy flagged it as unreachable.)

    # --replace: clear this kernel's artifacts so a partial or
    # mismatched prior fetch doesn't leave stale files behind the new
    # extraction.  Scoped to the (kernel, variant) being fetched, plus
    # the container, which is per-target and re-extracted by this same
    # fetch.  Wiping all of artifacts/<target>/<arch> instead destroyed
    # every *other* kernel the user had fetched -- and that wipe was
    # what ltvm suggested when fetching a second kernel tripped the
    # divergence check.
    if replace:
        import shutil as _shutil

        for victim in _replace_scope(target, arch, fetch_kver, variant):
            if victim.exists():
                if not use_json:
                    print(f"  Removing existing {victim}...")
                _shutil.rmtree(victim)

    if not use_json:
        print(f"Fetching {target}...")

    try:
        target_dir = _cli_attr("fetch_target")(
            target, url, ARTIFACTS_DIR, arch=arch, variant=variant
        )
        # Record the release tag so repeat fetches are instant
        if release_tag:
            write_release_tag(tag_root, target, arch, release_tag, variant)
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
        arch_flag = f" --arch {arch}" if arch != host_arch() else ""
        # When what we fetched is not the target's default kernel,
        # create needs to be told -- otherwise it asks for the default,
        # finds it unbuilt, and refuses right after a successful fetch.
        fetched = _fetched_kernel_name(target, release_tag, arch)
        kernel_flag = (
            f" --kernel {fetched}"
            if (kernel_signature or default_unavailable) and fetched
            else ""
        )
        print(
            f"  ltvm create co1-test --target {target}{arch_flag}"
            f"{kernel_flag} --vcpus 2 --mdt-disks 1 --ost-disks 2"
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
                print(
                    f"  # or pass --kernel {alt} to select a different kernel"
                )

    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: publish
# ------------------------------------------------------------------


def _gh_release_info(tag: str) -> dict[str, Any] | None:
    """The release ``tag`` as the logged-in gh user sees it, drafts
    included: {"state": "absent" | "draft" | "published", "body": its
    notes, "assets": asset names}.  None when gh cannot say."""
    import ltvm_pkg.cli as _cli

    try:
        r = subprocess.run(
            [
                "gh",
                "release",
                "view",
                tag,
                "--repo",
                _cli.GITHUB_REPO,
                "--json",
                "isDraft,body,assets",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if r.returncode != 0:
        msg = ((r.stderr or "") + (r.stdout or "")).lower()
        if "not found" in msg:
            return {"state": "absent", "body": "", "assets": []}
        return None
    try:
        data = json.loads(r.stdout)
        return {
            "state": "draft" if data.get("isDraft") else "published",
            "body": str(data.get("body") or ""),
            "assets": [
                str(a.get("name", "")) for a in data.get("assets") or []
            ],
        }
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


def _gh_release_upload(
    tag: str,
    assets: list[Path],
    notes: str,
    use_json: bool,
    public: bool = True,
) -> tuple[int | None, str | None]:
    """Create or replace the GitHub release ``tag`` with ``assets`` and
    ``notes``.  Returns (exit_code, err_msg): on success, (None, None).

    The release is a draft, and its notes lack the completion mark
    (release_tags.COMPLETE_NOTE), while its assets change: a new one is
    created so, an existing one, draft or public, made so before any
    asset changes.  Nothing selects or promotes it then, so nobody takes
    a mix of old and new assets, or new assets under old notes.  The
    last step writes ``notes``, mark included, and with ``public``
    publishes it; without, it stays a draft.  On a failure it is left
    an incomplete draft.  ``--clobber`` replaces assets of the same
    name.
    One maintainer publishes a given target and arch at a time: two
    publishes of one tag at once can still mix their assets.
    """
    import ltvm_pkg.cli as _cli

    def gh(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["gh", "release", *args, "--repo", _cli.GITHUB_REPO],
            capture_output=True,
            text=True,
        )

    def said(r: subprocess.CompletedProcess[str]) -> str:
        return ((r.stderr or "") + (r.stdout or "")).strip()

    from ltvm_pkg.release_tags import incomplete_notes

    left = (
        f"; release {tag} is left a draft marked incomplete, so neither "
        f"fetch nor promote takes it"
    )
    # The first change to any release: notes without the completion
    # mark (and, for a public one, back to a draft), before any asset.
    unfinished = incomplete_notes(notes)
    try:
        info = _cli_attr("_gh_release_info")(tag)
        if info is None:
            return EXIT_ERROR, (
                f"cannot tell whether release {tag} exists (gh release view "
                f"failed)"
            )
        if info["state"] == "absent":
            r = gh(
                "create", tag, "--title", tag, "--notes", unfinished, "--draft"
            )
            if r.returncode != 0:
                return EXIT_ERROR, (
                    f"gh release create failed (rc={r.returncode}): {said(r)}"
                )
        else:
            r = gh("edit", tag, "--notes", unfinished, "--draft=true")
            if r.returncode != 0:
                return EXIT_ERROR, (
                    f"could not mark release {tag} incomplete before "
                    f"replacing its assets (rc={r.returncode}): {said(r)}; "
                    f"nothing was changed"
                )
    except FileNotFoundError:
        return EXIT_ERROR, "gh CLI not found (https://cli.github.com/)"

    for a in assets:
        if not use_json:
            size_mb = a.stat().st_size / (1024 * 1024)
            print(f"  Uploading {a.name} ({size_mb:.0f} MB)...")
        up = subprocess.run(
            [
                "gh",
                "release",
                "upload",
                tag,
                str(a),
                "--repo",
                _cli.GITHUB_REPO,
                "--clobber",
            ],
        )
        if up.returncode != 0:
            return EXIT_ERROR, (
                f"gh release upload failed (rc={up.returncode}) for "
                f"{a.name}{left}"
            )
    # The notes say what the release holds and who published it, which
    # fetch decides by: replaced with the assets, published with them.
    from datetime import datetime, timezone

    from ltvm_pkg.release_tags import finished_notes

    r = gh(
        "edit",
        tag,
        "--notes",
        finished_notes(notes, datetime.now(timezone.utc)),
        "--draft=false" if public else "--draft=true",
    )
    if r.returncode != 0:
        return EXIT_ERROR, (
            f"gh release edit failed (rc={r.returncode}) finishing {tag} "
            f"with its notes: {said(r)}{left}"
        )
    return None, None


def _gh_release_delete(tag: str) -> str | None:
    """Delete the release ``tag`` and its git tag; the error, or None."""
    import ltvm_pkg.cli as _cli

    try:
        r = subprocess.run(
            [
                "gh",
                "release",
                "delete",
                tag,
                "--repo",
                _cli.GITHUB_REPO,
                "--yes",
                "--cleanup-tag",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return "gh CLI not found (https://cli.github.com/)"
    if r.returncode != 0:
        return f"rc={r.returncode}: {(r.stderr or r.stdout).strip()}"
    return None


def _gh_release_publish(tag: str, notes: str) -> str | None:
    """Make the draft ``tag`` public with ``notes``; the error, or None."""
    import ltvm_pkg.cli as _cli

    try:
        r = subprocess.run(
            [
                "gh",
                "release",
                "edit",
                tag,
                "--repo",
                _cli.GITHUB_REPO,
                "--notes",
                notes,
                "--draft=false",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return "gh CLI not found (https://cli.github.com/)"
    if r.returncode != 0:
        return f"rc={r.returncode}: {(r.stderr or r.stdout).strip()}"
    return None


def _gh_draft_releases() -> list[dict[str, Any]]:
    """Every draft release, as the logged-in gh user sees them, in the
    shape the releases API gives: tag_name, draft, created_at, body and
    assets.  One whose details could not be read carries
    ``lookup_failed``.  Raises RuntimeError when gh cannot list them."""
    import ltvm_pkg.cli as _cli

    try:
        r = subprocess.run(
            [
                "gh",
                "release",
                "list",
                "--repo",
                _cli.GITHUB_REPO,
                "--limit",
                "1000",
                "--json",
                "tagName,isDraft,createdAt",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError("gh CLI not found (https://cli.github.com/)")
    if r.returncode != 0:
        raise RuntimeError(f"gh release list failed: {r.stderr.strip()}")
    try:
        rows = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gh release list returned non-JSON: {e}")
    out: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("isDraft"):
            continue
        tag = str(row.get("tagName", ""))
        created = row.get("createdAt")
        info = _cli_attr("_gh_release_info")(tag)
        if info is None:
            # Kept, so promote reports it rather than silently missing
            # a draft it was asked to promote.
            out.append(
                {
                    "tag_name": tag,
                    "draft": True,
                    "created_at": created,
                    "lookup_failed": True,
                }
            )
            continue
        if info["state"] != "draft":
            continue
        out.append(
            {
                "tag_name": tag,
                "draft": True,
                "created_at": created,
                "body": info["body"],
                "assets": [{"name": n} for n in info["assets"]],
            }
        )
    return out


def _publish_input_mismatches(
    tc: Any,
    kernel: str,
    variant: str,
    lustre_src: Path | None = None,
    include_lustre: bool = True,
) -> list[str]:
    """Why the artifacts about to be published must not be, one line
    each; empty when they may.

    Whether each component is current is what the builds themselves
    decide, by the same functions: the container's and the image's
    is_stale, the image's against the Lustre being packaged and its
    MOFED kmods exactly as image_build hashes them, the kernel's
    status, release_build's check that the Lustre was built against
    this kernel, and the MOFED kmods' and ZFS's own is_stale.  On top
    of those, what only a release needs: a kernel and image that record
    this build generation (not merely none), built in the container
    they belong to, and a ZFS that is the one the key covers.

    ``lustre_src`` is the Lustre that will be packaged when it is not
    yet the snapshot (publish --lustre-tree): its staging.  Without
    ``include_lustre`` (--no-lustre) no Lustre ships, so an image with
    Lustre baked in is refused.
    """
    from ltvm_pkg.cli.util import has_recorded_components, staleness_reasons
    from ltvm_pkg.image_build import (
        _lustre_staging_hash_input,
        mofed_kmods_hash,
    )
    from ltvm_pkg.kernel_build import kernel_status
    from ltvm_pkg.paths import load_meta_safe
    from ltvm_pkg.release_build import lustre_fits_kernel
    from ltvm_pkg.release_package import BUILD_GENERATION, _snapshot_zfs_version
    from ltvm_pkg.target_config import DEFAULT_VARIANT

    v_flag = "" if variant == DEFAULT_VARIANT else f" --variant {variant}"
    rebuild = (
        f"rebuild with `ltvm build all {tc.name} --kernel {kernel}{v_flag}`"
    )
    problems: list[str] = []
    metas: dict[str, dict[str, Any]] = {}
    for artifact in ("container", "kernel", "image"):
        path = tc.meta_path(artifact, kernel, variant=variant)
        meta = load_meta_safe(path)
        if meta is None:
            problems.append(f"{artifact}: no meta.json at {path}")
        else:
            metas[artifact] = meta

    def stale(artifact: str, meta: dict[str, Any]) -> None:
        why = staleness_reasons(
            tc, artifact, meta, kernel=kernel, variant=variant
        )
        problems.extend(f"{artifact}: {r}" for r in why)
        if not why:
            problems.append(
                f"{artifact}: not current against its inputs; {rebuild}"
            )

    def generation(artifact: str, meta: dict[str, Any]) -> bool:
        got = meta.get("build_generation")
        if got == BUILD_GENERATION:
            return True
        problems.append(
            f"{artifact}: built by build generation "
            f"{got if got is not None else '(unrecorded)'}, this "
            f"checkout's is {BUILD_GENERATION}"
        )
        return False

    def built_in(artifact: str, meta: dict[str, Any]) -> bool:
        # A kernel is built in the base variant's container whatever
        # variant asked for it (kernel_build._ensure_kernel_builder), an
        # image in its own.
        if artifact == "kernel":
            which = "the base variant's build container"
            want_hash = tc.input_hash("container", variant=DEFAULT_VARIANT)
            base = (
                load_meta_safe(
                    tc.meta_path("container", variant=DEFAULT_VARIANT)
                )
                or {}
            )
            want_id = (
                base.get("image_id")
                if base.get("input_hash") == want_hash
                else None
            )
        else:
            which = "the build container being published"
            cmeta = metas.get("container") or {}
            want_hash = cmeta.get("input_hash")
            want_id = cmeta.get("image_id")
        got = meta.get("container_input_hash")
        if got is None:
            problems.append(
                f"{artifact}: does not record the build container it was "
                f"built in (built before ltvm recorded it)"
            )
            return False
        if want_hash is not None and got != want_hash:
            problems.append(
                f"{artifact}: built in another build container than {which} "
                f"(its inputs differ)"
            )
            return False
        got_id = meta.get("container_image_id")
        if got_id and want_id and got_id != want_id:
            problems.append(
                f"{artifact}: built in another build container than {which} "
                f"(podman image {str(got_id)[:12]}, not {str(want_id)[:12]})"
            )
            return False
        return True

    cmeta = metas.get("container")
    if cmeta is not None and tc.is_stale("container", variant=variant):
        stale("container", cmeta)

    kmeta = metas.get("kernel")
    if kmeta is not None:
        if not tc.outputs_complete("kernel", kernel=kernel):
            problems.append(f"kernel: its files are incomplete; {rebuild}")
        elif generation("kernel", kmeta) and built_in("kernel", kmeta):
            # Without the Lustre tree its patches come from, the kernel's
            # status answers by generation alone; the per-input digests
            # answer for the rest, as `build status --why` reads them.
            if not has_recorded_components(kmeta):
                problems.append(
                    "kernel: built before per-input digests were recorded, "
                    "so it cannot be checked"
                )
            elif kernel_status(tc, kernel).get("stale") or staleness_reasons(
                tc, "kernel", kmeta, kernel=kernel, variant=variant
            ):
                stale("kernel", kmeta)

    kdir = tc.kernel_output_dir(kernel)
    snap = kdir / "lustre-artifacts"
    if variant != DEFAULT_VARIANT:
        snap = snap / variant
    lustre = lustre_src or snap
    imeta = metas.get("image")

    lustre_input = b""
    zfs_version: str | None = None
    # Whether Lustre is baked into the image: image_build folds the
    # Lustre's staging hash into the image's only then.  Without it the
    # release is split, a kernel-only image and the Lustre beside it,
    # and the Lustre is checked against the kernel alone.
    baked = bool(imeta and imeta.get("with_lustre"))
    # What the image is checked against could not be read, or is itself
    # refused: its own staleness would only repeat that.
    image_inputs_known = True
    if not include_lustre:
        if imeta is not None and (
            imeta.get("with_lustre") or imeta.get("lustre_version")
        ):
            image_inputs_known = False
            problems.append(
                "image: has Lustre baked in, but --no-lustre ships none; "
                f"rebuild it without Lustre (`ltvm build image {tc.name} "
                f"--kernel {kernel}{v_flag} --no-lustre`), or publish with "
                f"Lustre"
            )
    elif (lustre / ".ltvm-staging-meta.json").exists() or (
        lustre / ".ltvm-snapshot.json"
    ).exists():
        try:
            lustre_input = _lustre_staging_hash_input(lustre)
        except FileNotFoundError as e:
            if baked:
                image_inputs_known = False
            problems.append(f"Lustre: {e}")
        if kmeta is not None and not lustre_fits_kernel(kdir, lustre):
            problems.append(
                f"Lustre: built against another build of this kernel (its "
                f"Module.symvers differs); rebuild with `ltvm build all "
                f"{tc.name} --kernel {kernel}{v_flag} --lustre-tree <tree>`"
            )
        zfs_version = _snapshot_zfs_version(lustre)

    mofed_input = b""
    if imeta is not None and imeta.get("mofed_kmods"):
        from ltvm_pkg.mofed_kmod_build import is_stale as mofed_is_stale
        from ltvm_pkg.mofed_kmod_build import mofed_kmod_dir

        try:
            mdir: Path | None = mofed_kmod_dir(tc, kernel)
        except Exception:  # noqa: BLE001
            mdir = None
        if mdir is None or mofed_is_stale(tc, kernel):
            image_inputs_known = False
            problems.append(
                f"MOFED kmods: not current against this kernel; rebuild with "
                f"`ltvm build mofed-kmods {tc.name} --kernel {kernel}{v_flag}`"
            )
        else:
            mofed_input = mofed_kmods_hash(mdir)

    if imeta is not None:
        fits = generation("image", imeta)
        fits = built_in("image", imeta) and fits
        if (
            fits
            and image_inputs_known
            and tc.is_stale(
                "image",
                kernel=kernel,
                extra_hash=(lustre_input if baked else b"") + mofed_input,
                variant=variant,
            )
        ):
            stale("image", imeta)

    if zfs_version:
        from ltvm_pkg.zfs_build import is_stale as zfs_is_stale
        from ltvm_pkg.zfs_build import resolve_zfs_version

        declared = resolve_zfs_version(tc)
        what = f"ZFS {zfs_version}"
        # The key covers the ZFS targets.yaml declares, not one a build
        # was told to use instead (--zfs-version).
        if zfs_version != declared:
            problems.append(
                f"{what}: the Lustre was built against ZFS {zfs_version}, "
                f"but this checkout declares {declared}, which the release "
                f"key covers"
            )
        elif zfs_is_stale(tc, kernel, zfs_version):
            problems.append(
                f"{what}: not current against this kernel, or incomplete; "
                f"rebuild with `ltvm build zfs {tc.name} --kernel {kernel}`"
            )
    return problems


def _prune_after_publish(
    identity: tuple[str, str, str, str],
    tag: str,
    *,
    dry_run: bool,
    use_json: bool,
) -> list[dict[str, Any]]:
    """Delete the published releases of ``identity`` (target, arch,
    declared kernel, variant) that ``tag`` supersedes (see
    release_tags.prune_plan).  A failure here never fails the publish
    or promote: the release is already up."""
    from datetime import datetime, timezone

    from ltvm_pkg.release_tags import prune_plan

    def say(msg: str) -> None:
        if not use_json:
            print(msg)

    try:
        releases = _cli_attr("_gh_api")("releases")
    except RuntimeError as e:
        print(
            f"warning: prune skipped, cannot list releases: {e}",
            file=sys.stderr,
        )
        # Said in the result, so promote can count it a failure: once
        # the release is public, a rerun finds no draft to retry from.
        return [
            {
                "tag": None,
                "delete": False,
                "action": "list failed",
                "reason": "prune skipped",
                "error": str(e),
            }
        ]
    if not isinstance(releases, list):
        releases = [releases]
    plan = prune_plan(releases, tag, datetime.now(timezone.utc), identity)
    out: list[dict[str, Any]] = []
    if plan:
        say("  Older releases of this kernel and variant:")
    for d in plan:
        entry = d.as_dict()
        if not d.delete:
            entry["action"] = "kept"
            say(f"    kept     {d.tag} ({d.reason})")
        elif dry_run:
            entry["action"] = "would delete"
            say(f"    would delete {d.tag} ({d.reason})")
        else:
            err = _cli_attr("_gh_release_delete")(d.tag)
            if err is None:
                entry["action"] = "deleted"
                say(f"    deleted  {d.tag} ({d.reason})")
            else:
                entry["action"] = "delete failed"
                entry["error"] = err
                print(
                    f"warning: could not delete release {d.tag}: {err}",
                    file=sys.stderr,
                )
        out.append(entry)
    return out


def _publish_refusal(
    tag: str,
    public: bool,
    manifest_name: str,
    identity: tuple[str, str, str, str],
    replace_unmerged: bool,
) -> tuple[str, str] | None:
    """Why publishing to ``tag`` must not go ahead, as (error, hint);
    None when it may.

    - A release holds one manifest: another one already in it refuses.
    - A published release is replaced only from the default branch: a
      branch publishes drafts, and must not hide or change what
      everyone fetches.
    - A draft is resumed when this commit published it.  One another
      commit published is someone else's publish in progress (one
      maintainer publishes a target and arch at a time), or an older
      one of this branch: taken over only with --replace-unmerged.
    """
    from ltvm_pkg.release_tags import Publisher, this_ltvm_commit

    info = _cli_attr("_gh_release_info")(tag)
    if info is None:
        return (
            f"cannot tell whether release {tag} already exists "
            f"(gh release view failed)",
            "check `gh auth status`",
        )
    if info["state"] == "absent":
        return None
    others = [
        n
        for n in info["assets"]
        if n.startswith("manifest-")
        and n.endswith(".json")
        and n != manifest_name
    ]
    if others:
        return (
            f"release {tag} already holds {', '.join(others)}: publishing "
            f"{manifest_name} there would leave two manifests in one release",
            "delete the stray manifest from that release, then publish again",
        )
    notes = Publisher.of({"body": info["body"]})
    if info["state"] == "published":
        if public:
            return None
        return (
            f"release {tag} is published, and this checkout's commit is not "
            f"known to be on the release repository's default branch: a "
            f"branch publishes drafts, and must not replace a release "
            f"everyone fetches",
            "merge first and publish from the default branch",
        )
    if notes.identity() not in (None, identity):
        return (
            f"draft {tag} names {'/'.join(notes.identity() or ())}, not "
            f"{'/'.join(identity)}",
            "check that draft's notes; delete it if it is stale",
        )
    commit = this_ltvm_commit()
    if notes.commit == commit or replace_unmerged:
        return None
    return (
        f"draft {tag} was published from commit "
        f"{(notes.commit or 'unknown')[:12]}, not this checkout's "
        f"{(commit or 'unknown')[:12]}: another maintainer may be "
        f"publishing it, and one publishes a target and arch at a time",
        "pass --replace-unmerged to take the draft over",
    )


def _bootable_tag_refusal(tag: str) -> str | None:
    """Why ``--image --tag tag`` must not publish there: the ecosystem
    tag namespaces are the ecosystem's, and a bootable publish would
    rewrite such a release's notes without its completion mark."""
    if not tag.startswith("bootable-"):
        return (
            f"--tag {tag}: a bootable image's release tag must start with "
            f"bootable-; other tags are ecosystem releases"
        )
    info = _cli_attr("_gh_release_info")(tag)
    if info is None:
        return (
            f"cannot tell whether release {tag} exists (gh release view failed)"
        )
    held = [
        n
        for n in info["assets"]
        if n.startswith("manifest-") and n.endswith(".json")
    ]
    if held:
        return (
            f"release {tag} holds an ecosystem manifest ({', '.join(held)}): "
            f"a bootable image does not go there"
        )
    return None


def _uncommitted_problem(
    identities: list[tuple[str, str, str, str]],
) -> str | None:
    """Why this checkout's release inputs for ``identities`` are not all
    its commit's: untracked or ignored files the hashes read, which the
    tracked-files check cannot see (release_tags.uncommitted_inputs)."""
    from ltvm_pkg import release_tags as rt

    found = rt.uncommitted_inputs(identities)
    if found is None:
        return (
            "cannot compare this checkout's release inputs with its commit "
            "(exporting HEAD failed)"
        )
    lines = sorted({d for diffs in found.values() for d in diffs})
    if not lines:
        return None
    return (
        "this checkout's release inputs are not all in its commit, so the "
        "release key would describe files the commit does not hold:\n"
        + "\n".join(f"    {d}" for d in lines)
    )


def _snapshot_missing(snap_dir: Path) -> str:
    return (
        "Lustre snapshot missing under "
        f"{snap_dir}.\n"
        "  publish needs Lustre staged into the artifacts "
        "dir.  Either:\n"
        "    --lustre-tree <path>   "
        "(snapshot from a Lustre source tree on the fly)\n"
        "    ltvm build all <target> --lustre-tree <path>   "
        "(then re-run publish)\n"
        "    --no-lustre            "
        "(ship a kernel-only release on purpose)"
    )


def _may_prune(position: str | None, no_prune: bool, use_json: bool) -> bool:
    """Whether to prune after a public publish or a promote: only from
    the tip of the release repository's default branch.  An older
    checkout may publish, but its releases are not what that branch
    builds now, and pruning by them could delete the one it fetches."""
    if no_prune or position not in ("tip", "ancestor"):
        return False
    if position != "tip":
        if not use_json:
            print(
                "  Prune skipped: this checkout is not at the tip of the "
                "default branch."
            )
        return False
    return True


def _publish_bootable_dry_run(
    tc: Any, kernel: str | None, variant: str, tag: str | None, use_json: bool
) -> int:
    """What `publish --image` would upload, without packaging it."""
    from ltvm_pkg.paths import load_meta_safe
    from ltvm_pkg.release_package import (
        _bootable_asset_name,
        _declared_default_kernel,
        _resolve_kernel,
    )

    if not tag:
        try:
            _, kdir = _resolve_kernel(
                tc.output_dir,
                kernel,
                _declared_default_kernel(tc.name, tc.arch, variant),
            )
        except ValueError as e:
            return _error(f"Package bootable failed: {e}", use_json)
        kver = (load_meta_safe(kdir / "meta.json") or {}).get("kernel_version")
        if not kver:
            return _error(
                f"kernel meta.json missing at {kdir / 'meta.json'}", use_json
            )
        tag = _bootable_asset_name(tc.name, tc.arch, kver, variant)[
            : -len(".qcow2.zst")
        ]
    if use_json:
        _output(
            {
                "dry_run": True,
                "target": tc.name,
                "variant": variant,
                "tag": tag,
                "mode": "bootable",
            },
            use_json,
        )
    else:
        print("Dry run: nothing will be packaged or uploaded.")
        print(f"  Tag: {tag}")
    return EXIT_OK


def cmd_publish(args: argparse.Namespace) -> int:
    """Upload a packaged asset set to a GitHub release.

    Two modes, selected by ``--image``:

    * default: publishes the ecosystem -- every asset listed by the
      manifest produced by ``package_target`` (container, kernel, image,
      optional lustre) to a single release tagged
      ``rel-<target>-<arch>-<kver>[-<variant>]-<key>``, the key being
      this checkout's inputs (release_tags.release_key).  From a
      checkout whose HEAD is not on the release repository's default
      branch (release_tags.default_branch_position) the release
      stays a draft until ``ltvm target promote``; from the default
      branch it is public, and this kernel and variant's superseded
      releases are pruned.  _publish_refusal and
      _publish_input_mismatches say what is refused.  The manifest is
      uploaded alongside so ``fetch`` can verify.

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
    no_upload = bool(getattr(args, "no_upload", False))
    tag = getattr(args, "tag", None)
    # Read before either mode can package or upload anything.
    dry_run = bool(getattr(args, "dry_run", False))
    no_prune = bool(getattr(args, "no_prune", False))

    if not use_json:
        from ltvm_pkg.cli.util import _print_target_header

        if image_mode:
            action = "Publishing bootable"
        elif no_upload:
            action = "Packaging"
        else:
            action = "Publishing"
        _print_target_header(tc, kernel=kernel, variant=variant, action=action)

    if image_mode and tag:
        bad_tag = _bootable_tag_refusal(tag)
        if bad_tag is not None:
            return _error(bad_tag, use_json)
    if image_mode and dry_run:
        return _publish_bootable_dry_run(tc, kernel, variant, tag, use_json)

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
            tag,
            [asset],
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
            {
                "target": args.target,
                "tag": tag,
                "asset": str(asset),
                "url": url,
                "mode": "bootable",
            },
            use_json,
        )
        return EXIT_OK

    # --- Ecosystem publish: package, then upload every asset (unless
    # --no-upload, which short-circuits after packaging). ---
    from ltvm_pkg import release_tags as rt
    from ltvm_pkg.paths import load_meta_safe
    from ltvm_pkg.release_package import (
        _declared_default_kernel,
        _manifest_name,
        _resolve_kernel,
    )

    replace_unmerged = bool(getattr(args, "replace_unmerged", False))
    if dry_run and no_upload:
        return _error(
            "--dry-run and --no-upload together: --dry-run previews a "
            "publish, --no-upload packages without one",
            use_json,
            hint="drop one of them",
        )
    if tag:
        # Always derived: a name given by hand could carry a key the
        # artifacts were not built with, or be one of the unprefixed
        # tags older ltvm fetches, whose loader cannot use a container
        # archive saved by id.
        return _error(
            f"--tag {tag}: an ecosystem release is always tagged from its "
            f"kernel and key; --tag names --image releases only",
            use_json,
        )

    # The kernel being published, as package_target will resolve it.
    try:
        pub_kernel, pub_kernel_dir = _resolve_kernel(
            tc.output_dir,
            kernel,
            _declared_default_kernel(tc.name, tc.arch, variant),
        )
    except ValueError as e:
        if not no_upload:
            return _error(f"Package failed: {e}", use_json)
        pub_kernel, pub_kernel_dir = kernel or "", Path()
    try:
        decl_kernel = str(tc._short_kernel_name(pub_kernel))
    except Exception:  # noqa: BLE001
        decl_kernel = pub_kernel

    key: str | None = None
    public = False
    position: str | None = None
    manifest_name = ""
    if not no_upload:
        # The release records this commit and keys this checkout's
        # inputs: neither describes uncommitted changes.
        problem = rt.checkout_problem() or _uncommitted_problem(
            [(tc.name, tc.arch, decl_kernel, variant)]
        )
        if problem is not None:
            return _error(
                f"cannot publish: {problem}",
                use_json,
                hint="commit the changes, or move them aside, then publish",
            )
        # With --lustre-tree, the Lustre packaged is the one snapshotted
        # from that tree below: check its staging, not the snapshot it
        # will replace.
        lustre_src: Path | None = None
        if getattr(args, "lustre_tree", None) and not getattr(
            args, "no_lustre", False
        ):
            from ltvm_pkg.lustre_build import staging_path

            lustre_src = staging_path(
                Path(args.lustre_tree).resolve(),
                tc.name,
                arch=tc.arch,
                kernel=pub_kernel,
                variant=variant,
            )
        problems = _cli_attr("_publish_input_mismatches")(
            tc,
            pub_kernel,
            variant,
            lustre_src=lustre_src,
            include_lustre=not getattr(args, "no_lustre", False),
        )
        if problems:
            return _error(
                "the artifacts were not built from this checkout's "
                "inputs and build code, so the release would not describe "
                "them:\n" + "\n".join(f"    {p}" for p in problems),
                use_json,
                hint=f"rebuild them (ltvm build all {tc.name})",
            )
        kver = (load_meta_safe(pub_kernel_dir / "meta.json") or {}).get(
            "kernel_version"
        )
        if not kver:
            return _error(
                f"kernel meta.json missing at {pub_kernel_dir / 'meta.json'}",
                use_json,
            )
        manifest_name = _manifest_name(tc.name, tc.arch, kver, variant)
        try:
            key = rt.release_key(tc.name, tc.arch, pub_kernel, variant)
        except Exception as e:  # noqa: BLE001
            return _error(f"cannot compute the release key: {e}", use_json)
        tag = rt.keyed_tag(manifest_name[len("manifest-") : -len(".json")], key)
        # A branch publishes a draft, which nothing fetches until
        # `ltvm target promote` after its merge; the default branch of
        # the repository releases go to publishes for everyone.
        position = rt.default_branch_position()
        public = position in ("tip", "ancestor")
        refused = _publish_refusal(
            tag,
            public,
            manifest_name,
            (tc.name, tc.arch, decl_kernel, variant),
            replace_unmerged,
        )
        if refused is not None:
            return _error(refused[0], use_json, hint=refused[1])

    if dry_run:
        assert tag is not None
        # What packaging would refuse, read-only: the Lustre it would
        # snapshot or take, and the build container it would export.
        if not getattr(args, "no_lustre", False):
            if lustre_src is not None:
                if not lustre_src.is_dir():
                    return _error(
                        f"Lustre snapshot failed: no staging directory at "
                        f"{lustre_src}; run `ltvm build lustre {tc.name} "
                        f"--kernel {pub_kernel}` in that tree first",
                        use_json,
                    )
            else:
                snap_dir = pub_kernel_dir / "lustre-artifacts"
                if variant != "base":
                    snap_dir = snap_dir / variant
                if not (snap_dir / ".ltvm-snapshot.json").exists():
                    return _error(_snapshot_missing(snap_dir), use_json)
        from ltvm_pkg.release_package import check_build_container

        try:
            check_build_container(tc.name, tc.output_dir, tc.arch, variant)
        except RuntimeError as e:
            return _error(f"Package failed: {e}", use_json)
        if not use_json:
            print("Dry run: nothing will be packaged, uploaded or deleted.")
            print(f"  Tag: {tag}")
            if key:
                print(f"  Key: {key} (artifacts match this checkout)")
            print(f"  As: {'a public release' if public else 'a draft'}")
        pruned = (
            _prune_after_publish(
                (tc.name, tc.arch, decl_kernel, variant),
                tag,
                dry_run=True,
                use_json=use_json,
            )
            if _may_prune(position, no_prune, use_json)
            else []
        )
        if use_json:
            _output(
                {
                    "dry_run": True,
                    "target": args.target,
                    "variant": variant,
                    "tag": tag,
                    "key": key,
                    "draft": not public,
                    "pruned": pruned,
                },
                use_json,
            )
        return EXIT_OK

    # Lustre staging lands in two places: the per-tree
    # ``<lustre_tree>/.ltvm-staging/`` written by ``ltvm build lustre``,
    # and a snapshot under ``kernels/<kver>/lustre-artifacts/`` that
    # ``ltvm build all`` puts in place for the packager.  publish reads
    # the snapshot, not the per-tree staging -- so a publish run after
    # ``build kernel`` + ``build lustre`` + ``build image`` (without
    # ``build all``) silently produced a Lustre-less release.
    #
    # Resolve that here: if the snapshot is missing, take ``--lustre-tree``
    # and run ``snapshot_lustre`` on the fly.  Without ``--lustre-tree``
    # and without an existing snapshot, fail loud (or honor ``--no-lustre``)
    # -- "silently shipped without Lustre" is too costly to be the default.
    no_lustre = getattr(args, "no_lustre", False)
    if not no_lustre:
        # Resolve full kernel name the same way package_target will --
        # including the declared default, or this gate inspects a
        # different kernel than the one that gets packaged and a
        # Lustre-less release ships without a word.
        from ltvm_pkg.release_package import (
            _declared_default_kernel,
            _resolve_kernel,
        )

        kernel_name, kernel_dir = _resolve_kernel(
            tc.output_dir,
            kernel,
            _declared_default_kernel(tc.name, tc.arch, variant),
        )
        snap_root = kernel_dir / "lustre-artifacts"
        snap_dir = snap_root if variant == "base" else snap_root / variant
        lustre_tree = getattr(args, "lustre_tree", None)
        # A named tree always replaces the snapshot: an older one left by
        # an earlier publish must not ship in its place.
        if lustre_tree or not (snap_dir / ".ltvm-snapshot.json").exists():
            if lustre_tree:
                if not use_json:
                    print(
                        f"Snapshotting Lustre staging from {lustre_tree} "
                        f"into artifacts..."
                    )
                try:
                    _cli_attr("snapshot_lustre")(
                        lustre_tree,
                        tc.output_dir,
                        target=args.target,
                        kernel=kernel_name,
                        arch=tc.arch,
                        variant=variant,
                    )
                except Exception as e:
                    return _error(f"Lustre snapshot failed: {e}", use_json)
            else:
                return _error(_snapshot_missing(snap_dir), use_json)
            if not no_upload:
                # Checked once more on the snapshot just taken: it is
                # what is packaged.
                problems = _cli_attr("_publish_input_mismatches")(
                    tc, pub_kernel, variant
                )
                if problems:
                    return _error(
                        "the Lustre snapshot just taken does not fit the "
                        "artifacts:\n"
                        + "\n".join(f"    {p}" for p in problems),
                        use_json,
                        hint=f"rebuild them (ltvm build all {tc.name})",
                    )

    if not use_json:
        v_hint = "" if variant == "base" else f" variant={variant}"
        print(f"Packaging {args.target}{v_hint}...")
    try:
        assets = _cli_attr("package_target")(
            args.target,
            tc.output_dir,
            kernel=kernel,
            arch=tc.arch,
            variant=variant,
            dest_dir=getattr(args, "output", None),
            include_lustre=not no_lustre,
        )
    except Exception as e:
        return _error(f"Package failed: {e}", use_json)

    if no_upload:
        _output(
            {
                "target": args.target,
                "variant": variant,
                "assets": {k: str(p) for k, p in assets.items()},
            },
            use_json,
        )
        return EXIT_OK

    assert tag is not None
    if assets["manifest"].name != manifest_name:
        return _error(
            f"packaged {assets['manifest'].name}, expected {manifest_name}",
            use_json,
        )
    if not use_json:
        print(f"  Tag: {tag}{'' if public else ' (draft)'}")
        print(f"  Assets: {len(assets)}")

    # Upload EVERYTHING including the manifest so fetch can find it.
    to_upload = list(assets.values())
    exit_code, err_msg = _cli_attr("_gh_release_upload")(
        tag,
        to_upload,
        notes=rt.release_notes(
            f"Pre-built artifacts for {args.target} (variant={variant})",
            target=tc.name,
            arch=tc.arch,
            kernel=decl_kernel,
            variant=variant,
        ),
        use_json=use_json,
        public=public,
    )
    if exit_code is not None:
        return _error(err_msg or "upload failed", use_json)

    url = f"https://github.com/{_cli.GITHUB_REPO}/releases/tag/{tag}"
    if not use_json:
        if public:
            print(f"  Published: {url}")
        else:
            why = (
                f"this commit is not on {_cli.GITHUB_REPO}'s default branch"
                if position == "off"
                else f"whether this commit is on {_cli.GITHUB_REPO}'s "
                f"default branch could not be checked"
            )
            print(
                f"  Published as a draft ({why}): nothing fetches it until "
                f"`ltvm target promote {tc.name}` runs on the default "
                f"branch after the merge."
            )

    # Record the release tag locally so subsequent `ltvm fetch` knows
    # the artifacts already on disk match this release.
    from ltvm_pkg.cli.util import write_release_tag

    # tc.output_dir, not the global ARTIFACTS_DIR: they differ under
    # tests that patch only the TargetConfig, and reaching for the
    # global wrote release tags into the real artifacts tree.
    write_release_tag(tc.output_dir, tc.name, tc.arch, tag, variant)

    result: dict[str, Any] = {
        "target": args.target,
        "variant": variant,
        "tag": tag,
        "key": key,
        "draft": not public,
        "assets": {k: str(p) for k, p in assets.items()},
        "url": url,
    }
    # Asked again after the upload: the default branch may have moved on
    # while it ran, and pruning is for its tip only.
    if (
        public
        and not no_prune
        and _may_prune(rt.default_branch_position(), no_prune, use_json)
    ):
        result["pruned"] = _prune_after_publish(
            (tc.name, tc.arch, decl_kernel, variant),
            tag,
            dry_run=False,
            use_json=use_json,
        )
    # The prune printed its own lines; text mode lists the rest.
    _output(
        result
        if use_json
        else {k: v for k, v in result.items() if k != "pruned"},
        use_json,
    )
    return EXIT_OK


# ------------------------------------------------------------------
# Subcommand: promote
# ------------------------------------------------------------------


def cmd_promote(args: argparse.Namespace) -> int:
    """Make public the draft releases a branch published, once merged.

    Runs from a clean checkout whose HEAD is in the history of the
    release repository's default branch.  A draft ``rel-`` release is promoted when its notes
    name a target this run covers and its key is the one this checkout
    computes for that target, arch, kernel and variant: the release is
    built from what the default branch now builds, and its upload
    finished (its notes carry the completion mark).  The promoting
    ltvm's version is added to its notes.  Each promoted release then
    prunes the releases it supersedes.  Every other draft of those
    targets is left, with the reason.  Fails, after doing the rest, if
    GitHub refused any promotion or deletion.
    """
    from ltvm_pkg import release_tags as rt

    use_json = args.json
    dry_run = bool(getattr(args, "dry_run", False))
    names: list[str] = list(getattr(args, "targets", None) or [])
    if getattr(args, "all", False):
        if names:
            return _error("pass target names or --all, not both", use_json)
        names = list(_cli_attr("list_targets")())
    if not names:
        return _error(
            "name the targets to promote, or pass --all",
            use_json,
            hint="ltvm target promote rocky9  |  ltvm target promote --all",
        )
    arch = getattr(args, "arch", None)

    problem = rt.checkout_problem()
    if problem is not None:
        return _error(f"cannot promote: {problem}", use_json)
    import ltvm_pkg.cli as _cli

    position = rt.default_branch_position()
    if position not in ("tip", "ancestor"):
        return _error(
            "cannot promote: this checkout's HEAD is "
            + (
                f"not in the history of {_cli.GITHUB_REPO}'s default branch"
                if position == "off"
                else f"not known to be on {_cli.GITHUB_REPO}'s default "
                f"branch (git could not fetch it)"
            ),
            use_json,
            hint="check out the default branch after the merge, pull, and "
            "promote from there",
        )
    try:
        drafts = _cli_attr("_gh_draft_releases")()
    except RuntimeError as e:
        return _error(str(e), use_json)

    promoted: list[dict[str, Any]] = []
    left: list[dict[str, Any]] = []
    memo: dict[tuple[str, str, str, str], str | None] = {}
    mine = rt.this_ltvm_version()
    # A promotion or a prune that GitHub refused: the run fails, after
    # the rest are done.  A draft that does not match is not a failure.
    failed = False

    def leave(tag: str, why: str) -> None:
        left.append({"tag": tag, "reason": why})
        if not use_json:
            print(f"  left     {tag} ({why})")

    # Drafts this checkout would promote, grouped by what they are and
    # what built them: (target, arch, kernel, variant) and key.
    matching: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for rel in drafts:
        tag = str(rel.get("tag_name", ""))
        if rt.tag_key(tag) is None or not any(
            rt.tag_is_for(tag, t) for t in names
        ):
            continue
        if rel.get("lookup_failed"):
            failed = True
            leave(tag, "reading it failed (gh release view): run promote again")
            continue
        notes = rt.Publisher.of(rel)
        ident = notes.identity()
        if ident is None:
            leave(tag, "its notes do not name its target, kernel and variant")
            continue
        t, a, k, v = ident
        if t not in names or (arch and a != arch):
            continue
        if not notes.complete:
            leave(
                tag,
                "its upload did not finish (no completion mark): publish "
                "it again",
            )
            continue
        if len(rt.manifest_names(rel)) != 1:
            leave(tag, "it does not hold exactly one manifest")
            continue
        want = _checkout_key(t, a, k, v, memo)
        if want is None:
            leave(tag, f"this checkout does not declare {t}/{a}/{k}/{v}")
            continue
        if rt.tag_key(tag) != want:
            leave(
                tag,
                f"built from other inputs than this checkout's (key {want} "
                f"here): not what the default branch builds",
            )
            continue
        matching.setdefault((*ident, want), []).append(rel)

    if matching:
        problem = _uncommitted_problem(
            sorted({(g[0], g[1], g[2], g[3]) for g in matching})
        )
        if problem is not None:
            return _error(
                f"cannot promote: {problem}",
                use_json,
                hint="commit the changes, or move them aside, then promote",
            )

    # Only the newest build of each group goes public, newest by when
    # its publish finished (rt.build_order), among the group's drafts
    # and its published releases: an older one published later would
    # be newer to fetch, and on a retry after a partial failure it
    # could be.  Not created_at, the tag commit's date, which builds
    # made between two commits share.
    chosen: list[tuple[dict[str, Any], tuple[str, str, str, str]]] = []
    if matching:
        try:
            published = rt.selectable(_cli_attr("_gh_api")("releases"))
        except RuntimeError as e:
            published = None
            for rels in matching.values():
                for rel in rels:
                    failed = True
                    leave(
                        str(rel["tag_name"]),
                        f"cannot list published releases to order it: {e}",
                    )
        for group, rels in matching.items() if published is not None else ():
            ident4 = (group[0], group[1], group[2], group[3])
            builds = list(rels) + [
                r
                for r in published or []
                if rt.tag_key(str(r.get("tag_name", ""))) == group[4]
                and rt.Publisher.of(r).identity() == ident4
            ]
            newest = max(builds, key=rt.build_order)
            for rel in rels:
                if rel is not newest:
                    leave(
                        str(rel["tag_name"]),
                        f"superseded by the newer build {newest['tag_name']}",
                    )
            if newest in rels:
                chosen.append((newest, ident4))

    # Oldest first, so of builds of one kernel under different keys the
    # newer is published last, and is the newest published.
    for rel, ident in sorted(chosen, key=lambda c: rt.build_order(c[0])):
        tag = str(rel["tag_name"])
        if dry_run:
            entry: dict[str, Any] = {"tag": tag, "action": "would promote"}
            if not use_json:
                print(f"  would promote {tag}")
            entry["pruned"] = (
                _prune_after_publish(
                    ident, tag, dry_run=True, use_json=use_json
                )
                if _may_prune(position, False, use_json)
                else []
            )
            # As the real run would: a prune it could not plan fails it.
            if any(p.get("action") == "list failed" for p in entry["pruned"]):
                failed = True
            promoted.append(entry)
            continue
        # The default-branch ltvm promoting it: what fetch's update
        # prompt compares, as a branch's version count does not order
        # against the default branch's.
        err = _cli_attr("_gh_release_publish")(
            tag, rt.promoted_notes(str(rel.get("body") or ""), mine)
        )
        if err is not None:
            failed = True
            leave(tag, f"promoting it failed: {err}")
            continue
        if not use_json:
            print(f"  promoted {tag}")
        # Asked again now, not at the start: the default branch may
        # have moved on while this ran.
        pruned = (
            _prune_after_publish(ident, tag, dry_run=False, use_json=use_json)
            if _may_prune(rt.default_branch_position(), False, use_json)
            else []
        )
        if any(
            p.get("action") in ("delete failed", "list failed") for p in pruned
        ):
            failed = True
        promoted.append({"tag": tag, "action": "promoted", "pruned": pruned})

    if not use_json and not promoted and not left:
        print("  no draft releases of those targets")
    if use_json:
        _output(
            {
                "dry_run": dry_run,
                "promoted": promoted,
                "left": left,
                "failed": failed,
            },
            use_json,
        )
    if failed:
        if not use_json:
            print(
                "error: some promotions or deletions failed (see above)",
                file=sys.stderr,
            )
        return EXIT_ERROR
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
        )
        from ltvm_pkg.cli.build import (
            cmd_clean as _cmd_clean,
        )
        from ltvm_pkg.target_config import ARTIFACTS_DIR

        target = args.target
        all_arches = bool(getattr(args, "all_arches", False))
        arch_flag = resolve_arch(args, target)
        tc, err = _load_target(target, use_json, arch=arch_flag)
        if err is not None:
            return err
        assert tc is not None
        if all_arches and getattr(args, "arch", None):
            return _error(
                "--arch and --all-arches are mutually exclusive", use_json
            )
        preview_paths = (
            [ARTIFACTS_DIR / target]
            if all_arches
            else [ARTIFACTS_DIR / target / tc.arch]
        )
        existing = [
            (p, _dir_size_bytes(p)) for p in preview_paths if p.exists()
        ]
        if not existing:
            # Nothing to confirm, so hand straight to _cmd_clean: it
            # reports an absent path in BOTH modes (a `wiped` entry with
            # removed=false under --json, "nothing to clean at <p>"
            # otherwise).  This branch used to print plain text
            # unconditionally -- the one path in cmd_delete that did --
            # so `target delete --json` emitted a bare sentence on
            # stdout and exit 0 where every other path emits an
            # envelope.
            return _cmd_clean(args)
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
    arch = getattr(args, "arch", None) or host_arch()
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
                kernel=kernel,
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
        "gh",
        "release",
        "delete",
        tag,
        "--repo",
        _cli.GITHUB_REPO,
        "--yes",
    ]
    if cleanup_tag:
        cmd.append("--cleanup-tag")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return _error("gh CLI not found (https://cli.github.com/)", use_json)
    if r.returncode != 0:
        return _error(
            f"gh release delete failed (rc={r.returncode}): "
            f"{(r.stderr or r.stdout).strip()}",
            use_json,
        )
    _output(
        {
            "target": target,
            "tag": tag,
            "deleted": True,
            "cleanup_tag": cleanup_tag,
        },
        use_json,
    )
    return EXIT_OK
