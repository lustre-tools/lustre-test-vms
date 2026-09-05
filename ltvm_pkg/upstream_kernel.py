"""Resolve and fetch mainline kernel releases from kernel.org.

The third kernel source, alongside distro SRPMs (RHEL) and
``linux-source`` debs (Ubuntu): tarballs straight from kernel.org, so
Lustre can be built against kernels newer than any distro ships.

Everything here is pure apart from :func:`fetch_releases` (one HTTP
GET) and :func:`download_tarball` (one curl).  Resolution is separated
from fetching so the spec grammar is testable against a canned
``releases.json`` with no network.

Spec grammar (what ``--kernel`` accepts for an upstream target):

  ``latest``      alias for the ``mainline`` moniker -- the newest -rc,
                  which is a snapshot of Linus's tree at that tag and
                  the closest reproducible thing to his HEAD.
  ``mainline``    kernel.org's mainline moniker (same as ``latest``).
  ``stable``      newest stable release.
  ``longterm``    newest longterm release.
  ``6.18``        a release series -- the newest point release
                  kernel.org still publishes for it.
  ``7.2.3``       an exact release.
  ``7.3-rc1``     an exact release candidate.

Monikers and series pins are *moving* targets: what ``latest`` means
changes whenever Linus tags.  Resolution therefore happens at build
time and the concrete version is folded into the kernel's staleness
hash, so a moved alias rebuilds instead of silently reusing the old
vmlinux.  See ``kernel_build._build_kernel_upstream``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

RELEASES_URL = "https://www.kernel.org/releases.json"
CDN_URL = "https://cdn.kernel.org/pub/linux/kernel"
# -rc tags are not published on the CDN as release tarballs; they only
# exist as gitweb snapshots of Linus's tree.
SNAPSHOT_URL = "https://git.kernel.org/torvalds/t"

# Monikers that name "whatever kernel.org currently calls this", as
# opposed to a version.  ``latest`` is ours, not kernel.org's.
LATEST = "latest"
MONIKERS = ("mainline", "stable", "longterm")

_EXACT_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:-rc\d+)?$")
_SERIES_RE = re.compile(r"^\d+\.\d+$")


class UpstreamResolveError(RuntimeError):
    """A kernel spec could not be resolved to a concrete release."""


@dataclass(frozen=True)
class UpstreamRelease:
    """A concrete kernel.org release a spec resolved to."""

    version: str
    """Exact upstream version, e.g. "7.2.3" or "7.3-rc1"."""

    source: str
    """URL of the source tarball."""

    moniker: str | None = None
    """kernel.org moniker this came from, when a moniker was resolved."""

    @property
    def is_rc(self) -> bool:
        return "-rc" in self.version

    @property
    def tarball_name(self) -> str:
        return self.source.rsplit("/", 1)[-1]


def version_key(version: str) -> tuple:
    """Sort key ordering kernel versions the way humans read them.

    Splits on dots and orders numerically, so 6.18.49 sorts above
    6.18.9 rather than below it as a string compare would have it.
    An -rc sorts *below* the release it leads to (7.3-rc1 < 7.3),
    which is what "newest" has to mean or every series pin would
    prefer a release candidate over the finished release.
    """
    base, _, rc = version.partition("-rc")
    parts = [int(p) for p in base.split(".") if p.isdigit()]
    # Pad so 6.18 and 6.18.49 compare on equal footing.
    while len(parts) < 3:
        parts.append(0)
    # rc == "" for a final release; it must outrank any rc of the same
    # base, hence the (1, 0) vs (0, n) tag.
    rc_key = (1, 0) if not rc else (0, int(rc) if rc.isdigit() else 0)
    return (tuple(parts), rc_key)


def series_of(version: str) -> str:
    """The x.y series a version belongs to ("7.2.3" -> "7.2")."""
    base = version.partition("-rc")[0]
    bits = base.split(".")
    return ".".join(bits[:2]) if len(bits) >= 2 else base


def tarball_url(version: str) -> str:
    """Where kernel.org publishes the source for ``version``.

    Release candidates live only as snapshots of Linus's tree; final
    releases live on the CDN under their major's v<N>.x directory.
    """
    if "-rc" in version:
        return f"{SNAPSHOT_URL}/linux-{version}.tar.gz"
    major = version.split(".", 1)[0]
    return f"{CDN_URL}/v{major}.x/linux-{version}.tar.xz"


def fetch_releases(url: str = RELEASES_URL, timeout: float = 30.0) -> list[dict]:
    """Fetch kernel.org's releases.json and return its ``releases`` list.

    Uses curl rather than urllib to inherit the proxy configuration the
    rest of ltvm's downloads already rely on.
    """
    try:
        proc = subprocess.run(
            ["curl", "-fsSL", "--max-time", str(int(timeout)), url],
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:  # pragma: no cover - curl always present
        raise UpstreamResolveError(f"curl not found: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode(errors="replace").strip()
        raise UpstreamResolveError(
            f"could not fetch {url}: {err or exc}"
        ) from exc
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise UpstreamResolveError(f"{url} is not valid JSON: {exc}") from exc
    releases = data.get("releases")
    if not isinstance(releases, list):
        raise UpstreamResolveError(f"{url} has no 'releases' list")
    return releases


def _usable(releases: list[dict]) -> list[dict]:
    """Entries naming a real, downloadable version.

    linux-next has ``source: null`` and a ``next-YYYYMMDD`` version
    that is not a kernel version at all; it would otherwise crash
    version_key and can't be built from a tarball anyway.
    """
    out = []
    for r in releases:
        v = r.get("version")
        if not isinstance(v, str) or not _EXACT_RE.match(v):
            continue
        out.append(r)
    return out


def resolve(spec: str, releases: list[dict]) -> UpstreamRelease:
    """Resolve a kernel spec against a releases.json ``releases`` list.

    Raises UpstreamResolveError with the available options spelled out
    when the spec names nothing; the caller turns that into a CLI
    error.  See the module docstring for the grammar.
    """
    spec = spec.strip()
    if not spec:
        raise UpstreamResolveError("empty kernel spec")

    usable = _usable(releases)

    # 1. Monikers, including our "latest" alias for mainline.
    moniker = "mainline" if spec == LATEST else spec
    if moniker in MONIKERS:
        matches = [r for r in usable if r.get("moniker") == moniker]
        if not matches:
            raise UpstreamResolveError(
                f"kernel.org currently publishes no {moniker!r} release "
                f"(monikers seen: "
                f"{', '.join(sorted({str(r.get('moniker')) for r in usable}))})"
            )
        best = max(matches, key=lambda r: version_key(str(r["version"])))
        version = str(best["version"])
        return UpstreamRelease(
            version=version,
            source=str(best.get("source") or tarball_url(version)),
            moniker=moniker,
        )

    # 2. Exact version -- trust it even when releases.json has moved on,
    #    so an older point release stays buildable and reproducible.
    if _EXACT_RE.match(spec) and not _SERIES_RE.match(spec):
        for r in usable:
            if str(r["version"]) == spec:
                return UpstreamRelease(
                    version=spec,
                    source=str(r.get("source") or tarball_url(spec)),
                    moniker=str(r["moniker"]) if r.get("moniker") else None,
                )
        return UpstreamRelease(version=spec, source=tarball_url(spec))

    # 3. Series pin -- newest point release kernel.org still lists for
    #    it.  releases.json only carries the newest of each maintained
    #    line, so an unmaintained series falls back to its .0 release,
    #    which the CDN keeps forever.
    if _SERIES_RE.match(spec):
        in_series = [r for r in usable if series_of(str(r["version"])) == spec]
        # A series pin means the released series.  Handing back an -rc
        # because that is all kernel.org lists yet would quietly build a
        # release candidate for someone who asked for a release.
        matches = [r for r in in_series if "-rc" not in str(r["version"])]
        if not matches and in_series:
            newest = max(
                in_series, key=lambda r: version_key(str(r["version"]))
            )
            raise UpstreamResolveError(
                f"{spec} has no release yet -- kernel.org's newest {spec} "
                f"is {newest['version']}. Ask for {newest['version']} by "
                f"name, or use '{LATEST}' to track the newest -rc."
            )
        if matches:
            best = max(matches, key=lambda r: version_key(str(r["version"])))
            version = str(best["version"])
            return UpstreamRelease(
                version=version,
                source=str(best.get("source") or tarball_url(version)),
                moniker=str(best["moniker"]) if best.get("moniker") else None,
            )
        log.warning(
            "kernel.org no longer lists series %s; falling back to the "
            "%s release. Pass an exact version (e.g. %s.4) to pin a "
            "point release.",
            spec, spec, spec,
        )
        return UpstreamRelease(version=spec, source=tarball_url(spec))

    raise UpstreamResolveError(
        f"{spec!r} is not a kernel version, series or moniker. "
        f"Expected one of {LATEST}/{'/'.join(MONIKERS)}, a series "
        f"like 6.18, or an exact version like 7.2.3 or 7.3-rc1."
    )


def describe(releases: list[dict]) -> list[tuple[str, str, bool]]:
    """(moniker, version, iseol) rows for ``ltvm target show``."""
    rows = []
    for r in _usable(releases):
        rows.append(
            (
                str(r.get("moniker") or "?"),
                str(r["version"]),
                bool(r.get("iseol")),
            )
        )
    return rows


def download_tarball(rel: UpstreamRelease, cache_dir: str | Path) -> Path:
    """Download ``rel``'s tarball into ``cache_dir``, or reuse a cached copy.

    Mirrors download_srpm: fetch to a unique temp file in the same
    directory and rename on success, so an interrupted download can
    never leave a truncated tarball that the next run silently feeds to
    tar inside the container.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / rel.tarball_name
    if cached.exists() and cached.stat().st_size > 0:
        log.info("Using cached kernel tarball: %s", cached)
        return cached

    fd, tmp_str = tempfile.mkstemp(
        dir=str(cache_dir), prefix=f".{rel.tarball_name}.", suffix=".partial"
    )
    os.close(fd)
    tmp = Path(tmp_str)
    log.info("Downloading kernel source: %s", rel.source)
    try:
        subprocess.run(
            ["curl", "-fSL", "--progress-bar", "-o", str(tmp), rel.source],
            check=True,
        )
        if tmp.stat().st_size == 0:
            raise UpstreamResolveError(
                f"downloaded an empty tarball from {rel.source}"
            )
        tmp.rename(cached)
    except subprocess.CalledProcessError as exc:
        raise UpstreamResolveError(
            f"could not download {rel.source}: {exc}"
        ) from exc
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return cached
