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
from typing import TYPE_CHECKING, Literal

from .kernel_build import _shell_var
from .lustre_tree import ldiskfs_patches, ldiskfs_series, kp_targets

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
    """Return series file stems under lustre/ldiskfs/kernel_patches/series/.

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
            reason = first_fail or detail.splitlines()[0] if detail else "rejected"
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


def _ldiskfs_series_matches(
    series_stems: set[str], kver_majmin: str | None
) -> str | None:
    """Heuristic: does any ldiskfs series filename appear to target the
    given kernel major.minor?

    Series filenames look like ``ldiskfs-<kver>-<distro>`` (e.g.
    ``ldiskfs-6.8.0-90-ubuntu24``, ``ldiskfs-5.14.0-427.13.1.el9``).
    We accept a target if any stem starts with ``ldiskfs-<major>.<minor>``.
    This errs on the side of accepting -- a future agent can tighten it
    by also checking the distro suffix (ubuntu<os_major>, el<os_major>,
    etc.) if false positives show up.
    """
    if kver_majmin is None:
        return None
    prefix = f"ldiskfs-{kver_majmin}."
    for stem in sorted(series_stems):
        if stem.startswith(prefix):
            return stem
    return None


def validate_target(
    tc: TargetConfig,
    lustre_tree: Path,
    kernel_build_tree: Path | None = None,
) -> ValidationResult:
    """Decide whether ``tc`` is supported by the given Lustre tree.

    Combines tc.default_kernel + tc.lustre_mode with the tree's
    declarative files (.target.in, which_patch, ChangeLog).  Returns
    a ValidationResult; callers use .status to gate further action.
    """
    from .target_config import LustreMode

    mode = tc.lustre_mode
    series = tc.default_kernel

    # .target.in is a RHEL/SLES artifact; deb-source targets have no
    # such file and get their kver from the distro source package.
    if tc.kernel_deb_source:
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
        # lustre/ldiskfs/kernel_patches/series/ without being listed in
        # which_patch (e.g. some ubuntu/debian flows).
        kver_mm = _kver_majmin(series) or _kver_majmin(kver)
        stems = parse_ldiskfs_series(lustre_tree)
        match_stem = _ldiskfs_series_matches(stems, kver_mm)
        if match_stem is not None:
            bt = kernel_build_tree
            sysfs_c = bt / "fs" / "ext4" / "sysfs.c" if bt else None
            if bt is not None and bt.is_dir() and sysfs_c is not None and sysfs_c.exists():
                patches = parse_ldiskfs_series_file(lustre_tree, match_stem)
                all_clean, failures = dry_apply_patches(patches, bt)
                if all_clean:
                    return ValidationResult(
                        status="ok",
                        mode=mode,
                        kernel_version=kver,
                        matched_in="ldiskfs_series",
                        message=(
                            f"matched ldiskfs series file {match_stem!r}; "
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
                "rerun validate after `ltvm build-kernel`"
                if bt is None
                else "patch dry-apply not run (kernel build-tree incomplete)"
            )
            return ValidationResult(
                status="ok",
                mode=mode,
                kernel_version=kver,
                matched_in="ldiskfs_series",
                message=(
                    f"matched ldiskfs series file {match_stem!r} under "
                    f"lustre/ldiskfs/kernel_patches/series/ for kernel "
                    f"{kver} (prefix ldiskfs-{kver_mm}.); {no_bt_note}"
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
                f"found under lustre/ldiskfs/kernel_patches/series/ "
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

    return ValidationResult(
        status="error",
        mode=mode,
        kernel_version=kver,
        matched_in=None,
        message=f"Unhandled lustre mode: {mode!r}",
    )
