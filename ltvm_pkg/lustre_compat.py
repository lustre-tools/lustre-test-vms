"""Pure parsers for Lustre compatibility metadata.

Reads declarative files in a Lustre source tree to determine
which kernels are supported/tested and what SRPM/series/config
a given kernel target expects.  No side effects, no I/O beyond
reading the requested file.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .kernel_build import _shell_var
from .lustre_tree import kp_targets, ldiskfs_patches, ldiskfs_series

if TYPE_CHECKING:
    from .target_config import LustreMode, TargetConfig


@dataclass(frozen=True)
class ChangeLogEntry:
    server_primary: list[str]
    server_best_effort: list[str]
    client_primary: list[str]
    client_best_effort: list[str]


@dataclass(frozen=True)
class TargetIn:
    lnxmaj: str
    lnxrel: str
    KERNEL_SRPM: str
    SERIES: str


# ------------------------------------------------------------------
# which_patch
# ------------------------------------------------------------------


_WHICH_PATCH_HEADER = "PATCH SERIES FOR SERVER KERNELS:"


def parse_which_patch(tree: Path) -> dict[str, str]:
    """Parse lustre/kernel_patches/which_patch.

    Returns a mapping of series filename -> kernel version string
    for every row in the "PATCH SERIES FOR SERVER KERNELS" table.
    Trailing parenthesized OS labels (e.g. "(RHEL 9.7)") are dropped.
    """
    path = Path(tree) / "lustre/kernel_patches/which_patch"
    if not path.exists():
        raise FileNotFoundError(
            f"which_patch not found at {path}; pass a valid Lustre tree"
        )

    result: dict[str, str] = {}
    in_table = False
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not in_table:
            if line.startswith(_WHICH_PATCH_HEADER):
                in_table = True
            continue
        if not line:
            # Blank line ends the table.
            break
        # Format: "<series>   <kernel-version>  (<label>)"
        m = re.match(r"(\S+)\s+(\S+)", line)
        if not m:
            continue
        series, version = m.group(1), m.group(2)
        result[series] = version

    if not result:
        raise ValueError(
            f"No patch series table found in {path} "
            f"(expected header {_WHICH_PATCH_HEADER!r})"
        )
    return result


# ------------------------------------------------------------------
# ChangeLog
# ------------------------------------------------------------------


# Matches e.g. "5.14.0-611.13.1.el9", "6.8.0-38", "5.14.21-150500.55.65",
# and "vanilla linux 5.4.0".  We accept the first whitespace-delimited
# token provided it looks like a kernel version (contains a digit and
# at least one dot).
_KVER_RE = re.compile(r"^([0-9][A-Za-z0-9_.+-]*)$")


def _is_kernel_version(tok: str) -> bool:
    if not _KVER_RE.match(tok):
        return False
    return "." in tok


def _extract_version(line: str) -> str | None:
    """Pull the kernel version token from a ChangeLog kernel-list line.

    Handles both normal lines (version is the first token) and the
    "vanilla linux <ver>" form.
    """
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith("vanilla linux"):
        parts = stripped.split()
        if len(parts) >= 3 and _is_kernel_version(parts[2]):
            return parts[2]
        return None
    first = stripped.split()[0]
    return first if _is_kernel_version(first) else None


# Headers that introduce each kernel list in the top entry.  Matching is
# done on the trimmed "* " bullet text, case-insensitive, substring-based
# so minor wording drift ("built and tested" vs "built/tested") still works.
_HEADERS = {
    "server_primary": "server primary kernels",
    "server_best_effort": "other server kernels",
    "client_primary": "client primary kernels",
    "client_best_effort": "other clients known",
}


def parse_changelog(tree: Path) -> ChangeLogEntry:
    """Parse the top entry of lustre/ChangeLog into kernel lists.

    Returns a ChangeLogEntry with four lists of kernel version
    strings.  Only the first (topmost) entry is consumed.
    """
    path = Path(tree) / "lustre/ChangeLog"
    if not path.exists():
        raise FileNotFoundError(
            f"ChangeLog not found at {path}; pass a valid Lustre tree"
        )

    lines = path.read_text().splitlines()
    # Identify where the top entry ends: the next release header.  The
    # top entry starts at line 0 (e.g. "TBD Whamcloud").  Subsequent
    # entries begin at column 0 with a date/tag followed by version,
    # so any non-indented non-empty line after the first is a terminator.
    end = len(lines)
    for i, line in enumerate(lines[1:], start=1):
        if line and not line[0].isspace():
            end = i
            break
    top = lines[:end]

    buckets: dict[str, list[str]] = {k: [] for k in _HEADERS}
    current: str | None = None
    saw_any_header = False
    for line in top:
        stripped = line.strip()
        if stripped.startswith("*"):
            bullet = stripped[1:].strip().lower()
            matched = None
            for key, needle in _HEADERS.items():
                if needle in bullet:
                    matched = key
                    break
            current = matched
            if matched:
                saw_any_header = True
            continue
        if current is None:
            continue
        ver = _extract_version(line)
        if ver is not None:
            buckets[current].append(ver)

    if not saw_any_header:
        raise ValueError(
            f"ChangeLog top entry in {path} contains no recognized "
            f"kernel list headers (expected e.g. 'Server primary kernels')"
        )

    return ChangeLogEntry(
        server_primary=buckets["server_primary"],
        server_best_effort=buckets["server_best_effort"],
        client_primary=buckets["client_primary"],
        client_best_effort=buckets["client_best_effort"],
    )


# ------------------------------------------------------------------
# <series>.target.in
# ------------------------------------------------------------------


def parse_target_in(tree: Path, series: str) -> TargetIn:
    """Parse lustre/kernel_patches/targets/<series>.target.in.

    Resolves simple ${var} expansions (e.g. KERNEL_SRPM usually
    references lnxmaj/lnxrel).  Falls back to the plain .target
    variant when no .target.in exists.
    """
    targets_dir = kp_targets(tree)
    path = targets_dir / f"{series}.target.in"
    if not path.exists():
        alt = targets_dir / f"{series}.target"
        if alt.exists():
            path = alt
        else:
            raise FileNotFoundError(
                f"Lustre target file not found: {targets_dir}/"
                f"{series}.target[.in]"
            )

    text = path.read_text()
    lnxmaj = _shell_var(text, "lnxmaj")
    lnxrel = _shell_var(text, "lnxrel")
    if not lnxmaj or not lnxrel:
        raise ValueError(f"Cannot parse lnxmaj/lnxrel from {path}")

    srpm = (
        _shell_var(text, "KERNEL_SRPM") or f"kernel-{lnxmaj}-{lnxrel}.src.rpm"
    )
    series_val = _shell_var(text, "SERIES")
    if series_val is None or series_val == "":
        series_val = f"{series}.series"

    return TargetIn(
        lnxmaj=lnxmaj,
        lnxrel=lnxrel,
        KERNEL_SRPM=srpm,
        SERIES=series_val,
    )


# ------------------------------------------------------------------
# ldiskfs series
# ------------------------------------------------------------------


def parse_ldiskfs_series(tree: Path) -> set[str]:
    """Return series file stems under ldiskfs/kernel_patches/series/.

    Each stem is the filename without ``.series`` (e.g.
    ``ldiskfs-6.8.0-90-ubuntu24``, ``ldiskfs-5.14.0-427.13.1.el9``).
    Returns an empty set if the directory is absent.
    """
    series_dir = ldiskfs_series(tree)
    if not series_dir.is_dir():
        return set()
    return {p.stem for p in series_dir.glob("*.series")}


# ------------------------------------------------------------------
# ldiskfs series-file parser and dry-apply runner
# ------------------------------------------------------------------


def parse_ldiskfs_series_file(tree: Path, series_stem: str) -> list[Path]:
    """Return absolute paths to patches listed in a series file.

    series_stem is the filename without .series (e.g.
    'ldiskfs-6.8.0-90-ubuntu24').  Skips comments (#-prefixed) and
    blank lines.  Patch paths are resolved under
    lustre/ldiskfs/kernel_patches/patches/.
    """
    series_file = ldiskfs_series(tree) / f"{series_stem}.series"
    patches_root = ldiskfs_patches(tree)
    result: list[Path] = []
    for line in series_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        result.append(patches_root / line)
    return result


def dry_apply_patches(
    patches: list[Path], kernel_src: Path
) -> tuple[bool, list[str]]:
    """Dry-apply a list of patches against a kernel source tree.

    Returns (all_clean, failures) where failures is a list of
    '<patch> <reason>' strings for patches that did not apply.
    Uses `patch -p1 --dry-run -F2 --no-backup-if-mismatch` so
    minor context drift is tolerated but genuine hunks that can't
    be found at any offset are caught.
    """
    failures: list[str] = []
    for patch_path in patches:
        try:
            proc = subprocess.run(
                [
                    "patch",
                    "-p1",
                    "--dry-run",
                    "-F2",
                    "--no-backup-if-mismatch",
                    "-d",
                    str(kernel_src),
                ],
                stdin=patch_path.open("rb"),
                capture_output=True,
            )
        except OSError as exc:
            failures.append(f"{patch_path.name}: cannot run patch: {exc}")
            continue
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace").strip()
            stdout = proc.stdout.decode(errors="replace").strip()
            detail = stderr or stdout
            first_fail = ""
            for line in (stdout + "\n" + stderr).splitlines():
                if "FAILED" in line:
                    first_fail = line.strip()
                    break
            reason = (
                first_fail or detail.splitlines()[0] if detail else "rejected"
            )
            failures.append(f"{patch_path.name}: {reason}")
    return (len(failures) == 0, failures)


# ------------------------------------------------------------------
# Compatibility gate
# ------------------------------------------------------------------


ValidationStatus = Literal["ok", "best_effort", "refuse", "error"]
MatchedIn = Literal[
    "which_patch_primary",
    "ldiskfs_series",
    "changelog_primary",
    "changelog_best_effort",
    "changelog_client_primary",
    "changelog_client_best_effort",
    # A vanilla kernel.org kernel newer than anything lustre/ChangeLog
    # lists.  Distinct from "not_listed": being ahead of the ChangeLog
    # is the point of an upstream target, not a reason to refuse it.
    "upstream_ahead",
    "not_listed",
]


@dataclass(frozen=True)
class ValidationResult:
    status: ValidationStatus
    mode: LustreMode | None
    kernel_version: str | None
    matched_in: MatchedIn | None
    message: str


# .target.in uses lnxrel like "611.13.1.el9_7" while the ChangeLog and
# which_patch tables list the same kernel as "5.14.0-611.13.1.el9"
# (no trailing "_7").  Normalize by stripping a single trailing "_N"
# from both sides before comparing so the minor-version suffix doesn't
# trigger a false mismatch.  This mirrors how the Lustre build itself
# maps target.in rows to the kernel lists in lustre/ChangeLog.
_KVER_SUFFIX_RE = re.compile(r"_\d+$")


def _normalize_kver(ver: str) -> str:
    return _KVER_SUFFIX_RE.sub("", ver.strip())


def _kver_from_target_in(ti: TargetIn) -> str:
    return f"{ti.lnxmaj}-{ti.lnxrel}"


def _kver_matches(declared: str, target_kver: str) -> bool:
    return _normalize_kver(declared) == _normalize_kver(target_kver)


# Extract the leading "<major>.<minor>" from a kernel-shaped token.
# Accepts "5.14-rhel9.7" -> "5.14", "6.8-ubuntu2404" -> "6.8",
# "5.14.0-611.13.1.el9_7" -> "5.14".
_KVER_MAJMIN_RE = re.compile(r"^(\d+)\.(\d+)")


def _kver_majmin(s: str) -> str | None:
    m = _KVER_MAJMIN_RE.match(s)
    return f"{m.group(1)}.{m.group(2)}" if m else None


# ------------------------------------------------------------------
# Vanilla (kernel.org) ldiskfs series selection
# ------------------------------------------------------------------

# Ported from the "probably mainline" ladder at the end of
# LDISKFS_LINUX_SERIES in config/lustre-build-ldiskfs.m4.  Lustre picks
# the ldiskfs series for a vanilla kernel by version range, not by
# filename, and the filename heuristic below cannot reproduce it: the
# mainline series are named ``ldiskfs-6.18-ml``, with a dash, while the
# heuristic looks for a ``ldiskfs-6.18.`` prefix with a dot and so
# matches none of them.
#
# Each entry is (inclusive floor, series stem); the last entry whose
# floor is <= the kernel version wins, and anything below the first
# floor has no series at all.  The 5.4.22 floor is not a typo -- the
# m4 hands (5.4.21, 5.10.0) to 5.4.136-ml while 5.4.21 itself gets
# 5.4.21-ml, and versions are discrete, so 5.4.22 reproduces that
# boundary exactly.
#
# Keep in sync with the m4 when Lustre adds a new -ml series; the
# top entry is the catch-all for every kernel newer than it, which is
# what lets an unreleased mainline kernel resolve at all.
_VANILLA_LDISKFS_LADDER: tuple[tuple[str, str], ...] = (
    ("5.4.0", "ldiskfs-5.4.0-ml"),
    ("5.4.21", "ldiskfs-5.4.21-ml"),
    ("5.4.22", "ldiskfs-5.4.136-ml"),
    ("5.10.0", "ldiskfs-5.10.0-ml"),
    ("6.1.0", "ldiskfs-6.1.38-ml"),
    ("6.6.0", "ldiskfs-6.6-ml"),
    ("6.12.0", "ldiskfs-6.12-ml"),
    ("6.18.0", "ldiskfs-6.18-ml"),
    ("6.19.0", "ldiskfs-7.0-ml"),
)


def _version_tuple(version: str) -> tuple[int, ...]:
    """Numeric tuple for a kernel version, ignoring any -rc suffix.

    An -rc is treated as its base version: 7.3-rc1 selects the same
    ldiskfs series 7.3 would, which is what Lustre's AS_VERSION_COMPARE
    ladder does with the release string it is handed.
    """
    base = version.partition("-rc")[0]
    parts = []
    for chunk in base.split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def vanilla_ldiskfs_series(version: str) -> str | None:
    """The ldiskfs series stem Lustre would pick for a vanilla kernel.

    ``version`` is an upstream kernel version ("7.2.3", "6.18.49",
    "7.3-rc1").  Returns the series stem without ``.series``, or None
    for kernels older than the first series Lustre ships.
    """
    v = _version_tuple(version)
    chosen: str | None = None
    for floor, stem in _VANILLA_LDISKFS_LADDER:
        if v >= _version_tuple(floor):
            chosen = stem
        else:
            break
    return chosen


def _distro_tokens(tc: Any) -> set[str]:
    """Distro markers that may appear in an ldiskfs series filename.

    Series are named ``ldiskfs-<kver>-<distro>``: ldiskfs-5.14-rhel9.7,
    ldiskfs-6.8.0-90-ubuntu24, ldiskfs-5.14.21-sles15sp4,
    ldiskfs-5.10.0-oe2203.  The distro half is what distinguishes two
    series that share a kernel major.minor.
    """
    try:
        family = str(tc.os_family)
        name = str(tc.os_name)
        version = str(tc.os_version)
    except Exception:
        return set()
    major = version.split(".", 1)[0]
    tokens = {f"{name}{major}"}
    if family == "rhel":
        tokens |= {f"rhel{major}", f"el{major}"}
    elif family == "debian":
        tokens.add(f"{name}{major}")
    return {t for t in tokens if t}


def _ldiskfs_series_matches(
    series_stems: set[str],
    kver_majmin: str | None,
    distro_tokens: set[str] | None = None,
) -> str | None:
    """Heuristic: does any ldiskfs series filename target this kernel?

    Series filenames look like ``ldiskfs-<kver>-<distro>`` (e.g.
    ``ldiskfs-6.8.0-90-ubuntu24``, ``ldiskfs-5.14.0-427.13.1.el9``).
    A stem must start with ``ldiskfs-<major>.<minor>`` *and* carry a
    marker for the target's distro.

    The distro half is not optional cosmetics.  RHEL 9 and SLES 15 SP4
    both ship 5.14 kernels, so on prefix alone
    ``ldiskfs-5.14.21-sles15sp4`` sorted ahead of every rhel9 series
    and vouched for a RHEL 9.0 kernel -- and when no kernel build-tree
    was available to dry-apply the patches, validate_target returned
    "ok" on the strength of that.  Among several same-distro
    candidates prefer an exact os_version match, then the highest,
    rather than whichever sorts first (which picked
    ldiskfs-6.8.0-100-ubuntu24 for a -45 kernel).
    """
    if kver_majmin is None:
        return None
    prefix = f"ldiskfs-{kver_majmin}."
    candidates = [s for s in sorted(series_stems) if s.startswith(prefix)]
    if not candidates:
        return None
    if not distro_tokens:
        # No distro context (older callers / upstream targets): keep
        # the historical prefix-only behavior.
        return candidates[0]
    same_distro = [s for s in candidates if any(t in s for t in distro_tokens)]
    if not same_distro:
        return None
    return same_distro[-1]


# A kernel name for an upstream target is either a bare spec
# ("latest", "6.18") or a built dir named ``<spec>-<version>``
# ("latest-7.3-rc1", "6.18-6.18.49").  Only the version half can be
# compared against ChangeLog, so pull it off the end.
_UPSTREAM_VER_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?(?:-rc\d+)?)$")


def _upstream_kver(tc: TargetConfig, kernel: str | None) -> str | None:
    """The concrete kernel.org version ``kernel`` names, if it names one.

    Falls back to the version recorded by the last build when the
    caller passed a moving spec ("latest") that no longer identifies a
    version on its own, so validating a built target still compares
    against what is actually on disk.
    """
    if kernel:
        m = _UPSTREAM_VER_RE.search(kernel)
        if m:
            return m.group(1)
    from .paths import load_meta_safe

    meta = load_meta_safe(tc.meta_path("kernel", kernel))
    if meta is not None:
        v = meta.get("upstream_version")
        if isinstance(v, str) and v:
            return v
    return None


def validate_target(
    tc: TargetConfig,
    lustre_tree: Path,
    kernel_build_tree: Path | None = None,
    kernel: str | None = None,
) -> ValidationResult:
    """Decide whether ``tc`` is supported by the given Lustre tree.

    Combines the kernel under build + tc.lustre_mode with the tree's
    declarative files (.target.in, which_patch, ChangeLog).  Returns
    a ValidationResult; callers use .status to gate further action.

    ``kernel`` is the kernel actually being built (short or full form);
    it defaults to the target's default kernel.  Passing it matters for
    every target that declares more than one kernel: validating
    ``--kernel 5.14-rhel9.8`` against the default 5.14-rhel9.7 checks
    the wrong .series/.target.in pair, so a genuinely incompatible
    non-default kernel sails through the gate (and a compatible one can
    be refused) purely because a sibling kernel is the default.
    """
    from .target_config import LustreMode

    mode = tc.lustre_mode
    # which_patch / .target.in lookups are keyed on the short name.
    series = tc._short_kernel_name(kernel) if kernel else tc.default_kernel

    # .target.in is a RHEL/SLES artifact; deb-source and kernel.org
    # targets have no such file.  For an upstream target the kernel
    # under build is a real kernel.org version, so prefer the resolved
    # one the caller passed over the spec ("latest", "6.18") that named
    # it -- a spec is not a version and cannot be compared against one.
    if tc.is_upstream:
        kver = _upstream_kver(tc, kernel) or series
    elif tc.kernel_deb_source:
        kver = series
    else:
        try:
            ti = parse_target_in(lustre_tree, series)
        except (FileNotFoundError, ValueError) as exc:
            return ValidationResult(
                status="error",
                mode=mode,
                kernel_version=None,
                matched_in=None,
                message=(
                    f"Cannot read .target.in for {series!r} under "
                    f"{lustre_tree}: {exc}"
                ),
            )
        kver = _kver_from_target_in(ti)

    if mode == LustreMode.SERVER_LDISKFS:
        try:
            wp = parse_which_patch(lustre_tree)
        except (FileNotFoundError, ValueError) as exc:
            return ValidationResult(
                status="error",
                mode=mode,
                kernel_version=kver,
                matched_in=None,
                message=f"Cannot read which_patch: {exc}",
            )
        series_file = f"{series}.series"
        if series_file in wp:
            declared = wp[series_file]
            if _kver_matches(declared, kver):
                return ValidationResult(
                    status="ok",
                    mode=mode,
                    kernel_version=kver,
                    matched_in="which_patch_primary",
                    message=(
                        f"{series} is listed in which_patch with matching "
                        f"kernel {declared} (target.in: {kver})"
                    ),
                )
            return ValidationResult(
                status="refuse",
                mode=mode,
                kernel_version=kver,
                matched_in="not_listed",
                message=(
                    f"{series} is listed in which_patch as {declared}, "
                    f"but target.in declares {kver} -- kernel version "
                    f"mismatch; this series does not match the kernel "
                    f"it claims to patch"
                ),
            )
        # Fallback: ldiskfs patches may ship under
        # ldiskfs/kernel_patches/series/ without being listed in
        # which_patch (e.g. some ubuntu/debian flows, and every
        # vanilla kernel).
        kver_mm = _kver_majmin(series) or _kver_majmin(kver)
        stems = parse_ldiskfs_series(lustre_tree)
        if tc.is_upstream:
            # A vanilla kernel is never in which_patch and its series is
            # chosen by version range, not filename -- ask the ladder
            # ported from Lustre's own configure.
            match_stem = vanilla_ldiskfs_series(kver)
            if match_stem is not None and match_stem not in stems:
                return ValidationResult(
                    status="refuse",
                    mode=mode,
                    kernel_version=kver,
                    matched_in="not_listed",
                    message=(
                        f"kernel {kver} maps to ldiskfs series "
                        f"{match_stem!r}, which this Lustre tree does "
                        f"not ship -- the tree predates server support "
                        f"for this kernel"
                    ),
                )
        else:
            match_stem = _ldiskfs_series_matches(
                stems, kver_mm, _distro_tokens(tc)
            )
        if match_stem is not None:
            bt = kernel_build_tree
            sysfs_c = bt / "fs" / "ext4" / "sysfs.c" if bt else None
            if (
                bt is not None
                and bt.is_dir()
                and sysfs_c is not None
                and sysfs_c.exists()
            ):
                patches = parse_ldiskfs_series_file(lustre_tree, match_stem)
                all_clean, failures = dry_apply_patches(patches, bt)
                if all_clean:
                    return ValidationResult(
                        status="ok",
                        mode=mode,
                        kernel_version=kver,
                        matched_in="ldiskfs_series",
                        message=(
                            f"ldiskfs series {match_stem!r}; "
                            f"all {len(patches)} patches dry-applied cleanly "
                            f"against {bt}"
                        ),
                    )
                return ValidationResult(
                    status="refuse",
                    mode=mode,
                    kernel_version=kver,
                    matched_in="not_listed",
                    message=(
                        f"ldiskfs series {match_stem!r} matched by filename "
                        f"but {len(failures)} patch(es) failed to dry-apply "
                        f"against {bt}: "
                        + "; ".join(failures[:3])
                        + (" ..." if len(failures) > 3 else "")
                    ),
                )
            no_bt_note = (
                "patch dry-apply not run (kernel not built yet); "
                "rerun validate after `ltvm build kernel`"
                if bt is None
                else "patch dry-apply not run (kernel build-tree incomplete)"
            )
            how = (
                "selected by Lustre's own mainline version ladder"
                if tc.is_upstream
                else f"matched by filename prefix ldiskfs-{kver_mm}."
            )
            return ValidationResult(
                status="ok",
                mode=mode,
                kernel_version=kver,
                matched_in="ldiskfs_series",
                message=(
                    f"ldiskfs series {match_stem!r} under "
                    f"ldiskfs/kernel_patches/series/ for kernel "
                    f"{kver} ({how}); {no_bt_note}"
                ),
            )
        return ValidationResult(
            status="refuse",
            mode=mode,
            kernel_version=kver,
            matched_in="not_listed",
            message=(
                f"{series} is not listed in lustre/kernel_patches/"
                f"which_patch and no matching ldiskfs series file "
                f"found under ldiskfs/kernel_patches/series/ "
                f"for kernel {kver}"
            ),
        )

    if mode == LustreMode.SERVER_ZFS:
        try:
            cl = parse_changelog(lustre_tree)
        except (FileNotFoundError, ValueError) as exc:
            return ValidationResult(
                status="error",
                mode=mode,
                kernel_version=kver,
                matched_in=None,
                message=f"Cannot read ChangeLog: {exc}",
            )
        for declared in cl.server_primary:
            if _kver_matches(declared, kver):
                return ValidationResult(
                    status="ok",
                    mode=mode,
                    kernel_version=kver,
                    matched_in="changelog_primary",
                    message=(
                        f"kernel {kver} is a server primary kernel "
                        f"in lustre/ChangeLog (matched {declared})"
                    ),
                )
        for declared in cl.server_best_effort:
            if _kver_matches(declared, kver):
                return ValidationResult(
                    status="best_effort",
                    mode=mode,
                    kernel_version=kver,
                    matched_in="changelog_best_effort",
                    message=(
                        f"kernel {kver} is listed in ChangeLog only as "
                        f"'other server kernels' (best-effort; matched "
                        f"{declared})"
                    ),
                )
        return ValidationResult(
            status="refuse",
            mode=mode,
            kernel_version=kver,
            matched_in="not_listed",
            message=(
                f"kernel {kver} is not listed in either the "
                f"'Server primary kernels' or 'Other server kernels' "
                f"section of lustre/ChangeLog"
            ),
        )

    if mode == LustreMode.CLIENT:
        try:
            cl = parse_changelog(lustre_tree)
        except (FileNotFoundError, ValueError) as exc:
            return ValidationResult(
                status="error",
                mode=mode,
                kernel_version=kver,
                matched_in=None,
                message=f"Cannot read ChangeLog: {exc}",
            )
        # Deb targets don't have a .target.in declaring an exact kver;
        # the best we have at validate-time is the kernel-name's
        # major.minor (e.g. "6.8" from "6.8-ubuntu2404").  Match any
        # ChangeLog entry whose major.minor equals ours; the actual
        # micro version is determined later at build time.
        kver_mm = _kver_majmin(kver) if tc.kernel_deb_source else None

        def _client_match(declared: str) -> bool:
            if _kver_matches(declared, kver):
                return True
            if kver_mm is not None and _kver_majmin(declared) == kver_mm:
                return True
            return False

        for declared in cl.client_primary:
            if _client_match(declared):
                return ValidationResult(
                    status="ok",
                    mode=mode,
                    kernel_version=kver,
                    matched_in="changelog_client_primary",
                    message=(
                        f"kernel {kver} is a client primary kernel "
                        f"in lustre/ChangeLog (matched {declared})"
                    ),
                )
        for declared in cl.client_best_effort:
            if _client_match(declared):
                return ValidationResult(
                    status="best_effort",
                    mode=mode,
                    kernel_version=kver,
                    matched_in="changelog_client_best_effort",
                    message=(
                        f"kernel {kver} is listed in ChangeLog only as "
                        f"'other clients' (best-effort; matched "
                        f"{declared})"
                    ),
                )
        if tc.is_upstream:
            # Building against a kernel Lustre has not tested is the
            # entire purpose of an upstream target -- refusing would
            # mean --force-compat on every single invocation, which
            # tells the user nothing they did not already know.
            return ValidationResult(
                status="best_effort",
                mode=mode,
                kernel_version=kver,
                matched_in="upstream_ahead",
                message=(
                    f"kernel {kver} is a vanilla kernel.org kernel that "
                    f"lustre/ChangeLog does not list; build breakage is "
                    f"expected and is what this target exists to find"
                ),
            )
        return ValidationResult(
            status="refuse",
            mode=mode,
            kernel_version=kver,
            matched_in="not_listed",
            message=(
                f"kernel {kver} is not listed in either the "
                f"'Client primary kernels' or 'Other clients' "
                f"section of lustre/ChangeLog"
            ),
        )

    # mypy proves this unreachable: LustreMode has exactly three
    # members and the three branches above each return.  Keep it
    # anyway -- it is the guard that catches a fourth mode added
    # without a branch here, and tests reach it by passing a mock.
    return ValidationResult(  # type: ignore[unreachable]
        status="error",
        mode=mode,
        kernel_version=kver,
        matched_in=None,
        message=f"Unhandled lustre mode: {mode!r}",
    )
