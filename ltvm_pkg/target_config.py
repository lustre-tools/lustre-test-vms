"""Target configuration for ltvm.

Single source of truth: targets/targets.yaml
Dockerfiles and package lists live in targets/<name>/.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .paths import find_ltvm_root, load_meta_safe


class LustreMode(str, Enum):
    """Lustre build/deploy mode for a target.

    CLIENT targets build only client modules; no ldiskfs/OSD code
    and no kernel patches required.  validate_target consults
    ChangeLog's client_primary/client_best_effort lists for these.
    """

    SERVER_LDISKFS = "server_ldiskfs"
    SERVER_ZFS = "server_zfs"
    CLIENT = "client"


log = logging.getLogger("ltvm")

REPO_ROOT = find_ltvm_root()
TARGETS_DIR = REPO_ROOT / "targets"


def _resolve_artifacts_dir() -> Path:
    """Resolve the artifacts-cache directory.

    Honors ``LTVM_ARTIFACTS_DIR`` or defaults to ``<repo>/artifacts``.
    If the legacy ``<repo>/output`` directory exists and the new
    location does not, it is renamed in place so prior caches are
    preserved across the rename.
    """
    if "LTVM_ARTIFACTS_DIR" in os.environ:
        return Path(os.environ["LTVM_ARTIFACTS_DIR"])
    new = REPO_ROOT / "artifacts"
    legacy = REPO_ROOT / "output"
    if not new.exists() and legacy.is_dir():
        try:
            legacy.rename(new)
        except OSError:
            return legacy
    return new


ARTIFACTS_DIR = _resolve_artifacts_dir()
TARGETS_YAML = TARGETS_DIR / "targets.yaml"

_DEFAULTS = {
    "arch": "x86_64",
    "os_family": "rhel",
}

# Recognized targets.yaml keys.  Unknown keys fail loudly at load:
# a misspelled key (e.g. 'configure_arg') would otherwise be silently
# ignored by its accessor while still perturbing every artifact's
# input hash via base_data -- the worst of both worlds.
_KNOWN_TARGET_KEYS = frozenset(
    {
        "arch",
        "os_family",
        "os_name",
        "os_version",
        "container_image",
        "configure_args",
        "default_mem",
        "kernel_deb_source",
        "kernel_upstream",
        "kernels",
        "lustre",
        "srpm_url",
        "status",
        "variants",
        "zfs",
    }
)
_KNOWN_KERNELS_KEYS = frozenset({"available", "config", "default"})
_KNOWN_KERNEL_ENTRY_KEYS = frozenset({"name", "srpm_version"})
_KNOWN_LUSTRE_KEYS = frozenset({"mode"})
_KNOWN_ZFS_KEYS = frozenset({"version"})
_KNOWN_VARIANT_KEYS = frozenset(
    {
        "container_overlay",
        "image_overlay",
        "kernel",
        "packages",
        "params",
    }
)

_COPY_RE = re.compile(r"^\s*COPY\s+(\S+)", re.MULTILINE)

DEFAULT_VARIANT = "base"


class Variant:
    """Optional add-on layered on top of a target's base artifacts.

    A variant carries its own Dockerfile overlay(s), extra packages,
    and free-form params (e.g. ``mofed_version``) that fold into the
    artifact input hash.  The base variant is implicit and has no
    overlay; its paths match the pre-variant layout so existing
    on-disk caches keep working.
    """

    def __init__(
        self,
        name: str,
        data: dict[str, Any] | None,
        target_dir: Path,
    ) -> None:
        self.name = name
        self._data = data or {}
        # Typo'd variant keys are doubly silent otherwise: an unknown
        # key is dropped by the accessors below AND ignored by
        # hash_bytes, so e.g. a mistyped 'kernal:' pin would make the
        # variant silently apply to every kernel.
        unknown = set(self._data) - _KNOWN_VARIANT_KEYS
        if unknown:
            raise ValueError(
                f"variant {name!r}: unrecognized key(s) in targets.yaml: "
                f"{', '.join(sorted(unknown))}"
            )
        co = self._data.get("container_overlay")
        io = self._data.get("image_overlay")
        # Overlay paths in YAML are relative to the repo's targets/
        # directory so they can reference shared snippets.  We resolve
        # relative to TARGETS_DIR, not target_dir, for that reason.
        self.container_overlay: Path | None = (TARGETS_DIR / co) if co else None
        self.image_overlay: Path | None = (TARGETS_DIR / io) if io else None
        self.packages: list[str] = list(self._data.get("packages", []))
        self.params: dict[str, Any] = dict(self._data.get("params", {}))
        # Optional kernel pin: restrict this variant to one declared
        # kernel.  ``None`` means "applies to every kernel the target
        # declares" (the default).  See lustre_test_vms_v2-stp for
        # design rationale.
        k = self._data.get("kernel")
        self.pinned_kernel: str | None = str(k) if k is not None else None

    @property
    def is_base(self) -> bool:
        return self.name == DEFAULT_VARIANT

    def with_param_overrides(self, overrides: dict[str, Any]) -> Variant:
        """Return a copy of this variant with ``overrides`` merged into
        ``params``.  Used to thread CLI overrides (e.g. --mofed-version)
        into the variant's input hash without mutating shared state.
        """
        new_data = dict(self._data)
        new_params = {**self.params, **overrides}
        new_data["params"] = new_params
        v = Variant.__new__(Variant)
        v.name = self.name
        v._data = new_data
        v.container_overlay = self.container_overlay
        v.image_overlay = self.image_overlay
        v.packages = list(self.packages)
        v.params = new_params
        v.pinned_kernel = self.pinned_kernel
        return v

    def hash_bytes(self, artifact: str) -> bytes:
        """Extra bytes folded into the variant's input hash for
        ``artifact`` (``container`` or ``image``).  Only the overlay
        relevant to that artifact is mixed in; packages and params
        apply to both (a MOFED version bump should invalidate both
        the build container and the image)."""
        h = hashlib.sha256()
        h.update(b"variant:")
        h.update(self.name.encode())
        if artifact == "container" and self.container_overlay is not None:
            if self.container_overlay.exists():
                h.update(b"container_overlay:")
                h.update(self.container_overlay.read_bytes())
        if artifact == "image" and self.image_overlay is not None:
            if self.image_overlay.exists():
                h.update(b"image_overlay:")
                h.update(self.image_overlay.read_bytes())
        for p in sorted(self.packages):
            h.update(b"pkg:")
            h.update(p.encode())
        for k, v in sorted(self.params.items()):
            h.update(b"param:")
            h.update(f"{k}={v}".encode())
        return h.digest()


# Which formula produced an artifact's ``input_hash``.  Bumped only when
# the *composition* of the hash changes -- not when an input's contents
# do.  Recorded in every meta.json, so a stale artifact can be explained
# honestly: without it, narrowing the hash in scheme 2 made `--why`
# report "targets.yaml (changed)" for a targets.yaml that had not
# changed, at exactly the moment someone is asking why a rebuild started.
#
# History:
#   1  targets.yaml's whole per-target slice minus `variants` and `zfs`.
#   2  `kernels` excluded too; `kernels.config` and the built kernel's
#      own `available` entry folded back per kernel instead.
HASH_SCHEME = 2


# The ``-<lnxmaj>-<lnxrel>`` tail that turns a short kernel name into a
# built-dir name: a dotted three-part kernel version, dash-delimited on
# both sides (``5.14-rhel9.7`` -> ``5.14-rhel9.7-5.14.0-611.13.1.el9_7``).
# No declared short name is of that shape, so finding it is unambiguous.
_FULL_KERNEL_TAIL = re.compile(r"-\d+\.\d+\.\d+-")


def kernel_dir_version_key(name: str) -> tuple:
    """Natural-order sort key for kernel directory names.

    Kernel dirs are ``<lustre_target>-<lnxmaj>-<lnxrel>``, e.g.
    ``4.18-rhel8.10-4.18.0-553.155.1.el8_10``.  Comparing those as
    plain strings orders 553.155.1 *below* 553.89.1, so split each
    name into digit and non-digit runs and compare the digit runs
    numerically.  Each chunk is tagged with its kind so an int is
    never compared against a str when two names differ in shape.
    """
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.split(r"(\d+)", name)
    )


def matching_kernel_dirs(kernels_dir: Path, name: str) -> list[str]:
    """Built kernel dirs a short name could mean, newest version first.

    A short name from targets.yaml ("5.14-rhel9.7") does not identify a
    kernel: only the Lustre tree knows which lnxrel it stands for, and
    one artifacts dir is shared by every checkout on the machine.  Two
    trees declaring different point releases therefore leave two dirs
    under the same prefix, and any lookup by short name is picking one.
    """
    if not kernels_dir.is_dir():
        return []
    prefix = name + "-"
    return sorted(
        (
            d.name
            for d in kernels_dir.iterdir()
            if d.is_dir() and d.name.startswith(prefix)
        ),
        key=kernel_dir_version_key,
        reverse=True,
    )


# Ambiguous resolutions warn once per (dir, name) per process; a table
# command resolves the same target repeatedly and should not repeat
# itself.
_AMBIGUITY_WARNED: set[tuple[str, str]] = set()


def _warn_if_ambiguous(kernels_dir: Path, name: str, chosen: str) -> None:
    matches = matching_kernel_dirs(kernels_dir, name)
    if len(matches) < 2:
        return
    key = (str(kernels_dir), name)
    if key in _AMBIGUITY_WARNED:
        return
    _AMBIGUITY_WARNED.add(key)
    others = ", ".join(m for m in matches if m != chosen)
    log.warning(
        "%r matches %d built kernels; using %s (also built: %s). "
        "Pass --kernel <full-name> to pick a specific one.",
        name,
        len(matches),
        chosen,
        others,
    )


def resolve_kernel_dir(kernels_dir: Path, name: str) -> str:
    """Match a kernel name against the built dirs under ``kernels_dir``.

    ``name`` may be the short form from targets.yaml ("5.14-rhel9.7")
    or a full cached-dir name.  Exact match wins; otherwise the
    highest-versioned dir sharing the ``<name>-`` prefix; otherwise
    ``name`` unchanged, so callers naming a not-yet-built kernel get a
    path to create rather than an error.

    This is the single implementation of that lookup.  It used to be
    duplicated between TargetConfig.resolve_kernel and
    release_package._resolve_kernel, and the copies drifted -- one was
    fixed to order numerically while the other still ordered lexically,
    so a build and the publish that followed it packaged different
    kernels.
    """
    if not kernels_dir.is_dir():
        return name
    if (kernels_dir / name).is_dir():
        return name
    candidates = matching_kernel_dirs(kernels_dir, name)
    if candidates:
        chosen = candidates[0]
        # Highest version is a guess -- the tree that declares this
        # short name is the only thing that knows which release it
        # means, and this function has no tree.  Say so rather than
        # answer silently.
        _warn_if_ambiguous(kernels_dir, name, chosen)
        return chosen
    return name


def build_container_tag(
    name: str, arch: str = "x86_64", variant: str = DEFAULT_VARIANT
) -> str:
    """Compute the podman build-container tag for a target + arch + variant.

    Module-level so callers that don't have a full TargetConfig in hand
    (e.g. release_package.export_build_container, which may run against
    a synthetic target name) share the exact same logic.

    Base-variant tag is unchanged from the pre-variant scheme so
    existing cached podman images keep their tags.  Non-base variants
    get a ``-<variant>`` suffix.
    """
    if arch != "x86_64":
        tag = f"ltvm-build-{name}-{arch}"
    else:
        tag = f"ltvm-build-{name}"
    if variant != DEFAULT_VARIANT:
        tag = f"{tag}-{variant}"
    return tag


def _component_label(path: Path) -> str:
    """Name a hashed file the way `build status --why` should print it.

    Relative to ``targets/``, so a per-target file reads as
    ``rocky9/packages-os.txt`` rather than ``common/packages-os.txt``
    -- which was a path that does not exist, and would have collapsed
    two same-named files from different directories into one
    component.  Labels do not feed the digest (see _HashParts), so this
    cannot perturb staleness.
    """
    try:
        return str(path.relative_to(TARGETS_DIR))
    except ValueError:
        return path.name


class _HashParts:
    """Accumulates the bytes fed to a staleness hash, grouped by name.

    ``input_hash`` answers "must this be rebuilt"; `build status --why`
    answers "because of what".  Both read the same byte stream through
    this, rather than the second growing its own copy of the list of
    inputs -- a copy would drift, and an explanation that disagrees with
    the decision is worse than no explanation.

    ``update`` keeps hashlib's interface, so the hash body reads as it
    did before and the concatenation order -- the only thing the digest
    depends on -- is unchanged by construction.  Consecutive updates
    under one label are merged, so a group of related fields counts as
    one component.
    """

    def __init__(self) -> None:
        self._parts: list[tuple[str, bytearray]] = []
        self._label = "targets.yaml"

    def label(self, name: str) -> None:
        self._label = name

    def update(self, data: bytes) -> None:
        if self._parts and self._parts[-1][0] == self._label:
            self._parts[-1][1].extend(data)
        else:
            self._parts.append((self._label, bytearray(data)))

    def digest(self) -> str:
        h = hashlib.sha256()
        for _name, data in self._parts:
            h.update(data)
        return h.hexdigest()[:16]

    def components(self) -> dict[str, str]:
        """Per-component digests, in the order they were fed."""
        return {
            name: hashlib.sha256(bytes(data)).hexdigest()[:12]
            for name, data in self._parts
        }


def _dockerfile_referenced_files(dockerfile: Path) -> list[Path]:
    """Return the files under TARGETS_DIR referenced by COPY lines in a
    Dockerfile. Build context is TARGETS_DIR, so COPY sources like
    'common/setup-ssh.sh' resolve relative to it.

    Directories are walked recursively.  Files that don't exist are
    silently skipped (they'd fail the build but shouldn't crash staleness).
    """
    if not dockerfile.exists():
        return []
    text = dockerfile.read_text()
    result: list[Path] = []
    for match in _COPY_RE.finditer(text):
        src = match.group(1)
        # Ignore --from=... (multi-stage) and absolute paths outside context
        if src.startswith("--"):
            continue
        path = TARGETS_DIR / src
        if path.is_file():
            result.append(path)
        elif path.is_dir():
            for f in sorted(path.rglob("*")):
                if f.is_file():
                    result.append(f)
    return sorted(set(result))


def _load_registry() -> dict[str, Any]:
    """Load and return the full targets.yaml registry."""
    if not TARGETS_YAML.exists():
        raise FileNotFoundError(f"Target registry not found: {TARGETS_YAML}")
    with TARGETS_YAML.open() as f:
        data: dict[str, Any] = yaml.safe_load(f)
        return data


class TargetConfig:
    """Parsed configuration for a single build target.

    Args:
        name: Target name from targets.yaml (e.g. rocky9).
        arch: Optional architecture override.  When given, replaces the
              target's default arch.  Output is always routed to
              artifacts/<target>/<arch>/ regardless of arch -- the layout
              is uniform so cross-arch builds never collide and code
              paths don't need an x86_64 special case.
    """

    def __init__(
        self,
        name: str,
        arch: str | None = None,
        variant: str = DEFAULT_VARIANT,
    ) -> None:
        self.name = name
        self.variant_name = variant
        self.target_dir = TARGETS_DIR / name

        registry = _load_registry()
        targets = registry.get("targets", {})
        if name not in targets:
            raise ValueError(
                f"Unknown target: {name!r} (not in {TARGETS_YAML})"
            )

        defaults = {**_DEFAULTS, **registry.get("defaults", {})}
        raw = targets[name]
        # Merge defaults under target fields
        self._data: dict[str, Any] = {**defaults, **raw}
        # Whether targets.yaml states this target's arch explicitly.
        # An explicit arch is a *constraint* (rocky9-64k is aarch64 and
        # nothing else); the inherited default is merely the arch to
        # prefer when the host doesn't suggest one, since targets like
        # rocky9 are published for several.  Callers resolving "which
        # arch did the user mean" need to tell those apart.
        self.arch_is_declared: bool = "arch" in raw

        # Schema validation: catch type errors in targets.yaml early
        # so we don't get confusing downstream behavior (e.g. missing
        # kernels block raising KeyError mid-build).
        if "kernels" not in self._data or not isinstance(
            self._data["kernels"], dict
        ):
            raise ValueError(
                f"target {name!r}: missing or non-dict 'kernels' "
                f"block in targets.yaml"
            )
        self._kernels: dict[str, Any] = self._data["kernels"]
        if "default" not in self._kernels:
            raise ValueError(f"target {name!r}: 'kernels.default' is required")

        # Unknown-key validation: typos must fail loudly, not be
        # silently ignored (see _KNOWN_TARGET_KEYS comment).
        unknown = set(self._data) - _KNOWN_TARGET_KEYS
        if unknown:
            raise ValueError(
                f"target {name!r}: unrecognized key(s) in targets.yaml: "
                f"{', '.join(sorted(unknown))}"
            )
        unknown = set(self._kernels) - _KNOWN_KERNELS_KEYS
        if unknown:
            raise ValueError(
                f"target {name!r}: unrecognized key(s) under 'kernels': "
                f"{', '.join(sorted(unknown))}"
            )
        lustre_block = self._data.get("lustre")
        if isinstance(lustre_block, dict):
            unknown = set(lustre_block) - _KNOWN_LUSTRE_KEYS
            if unknown:
                raise ValueError(
                    f"target {name!r}: unrecognized key(s) under "
                    f"'lustre': {', '.join(sorted(unknown))}"
                )
        zfs_block = self._data.get("zfs")
        if zfs_block is not None:
            if not isinstance(zfs_block, dict):
                raise ValueError(
                    f"target {name!r}: 'zfs' must be a mapping, got "
                    f"{type(zfs_block).__name__}"
                )
            unknown = set(zfs_block) - _KNOWN_ZFS_KEYS
            if unknown:
                raise ValueError(
                    f"target {name!r}: unrecognized key(s) under "
                    f"'zfs': {', '.join(sorted(unknown))}"
                )
        for entry in self._kernels.get("available", []):
            if isinstance(entry, dict):
                if "name" not in entry:
                    raise ValueError(
                        f"target {name!r}: kernels.available mapping "
                        f"entry is missing its 'name' key: {entry!r}"
                    )
                unknown = set(entry) - _KNOWN_KERNEL_ENTRY_KEYS
                if unknown:
                    raise ValueError(
                        f"target {name!r}: unrecognized key(s) on "
                        f"kernels.available entry {entry['name']!r}: "
                        f"{', '.join(sorted(unknown))}"
                    )

        # kernels.default must be a declared kernel: a typo'd default
        # would otherwise become a phantom kernel that completion,
        # fetch --kernel validation, and clean all accept, failing only
        # deep inside a kernel build with a missing-.target error.
        # (An empty/absent 'available' list keeps the historical
        # behavior of the default implicitly declaring itself.)
        avail_names = [
            self._kernel_entry_name(e)
            for e in self._kernels.get("available", [])
        ]
        if avail_names and self._kernels["default"] not in avail_names:
            raise ValueError(
                f"target {name!r}: kernels.default "
                f"{self._kernels['default']!r} is not in "
                f"kernels.available ({', '.join(avail_names)})"
            )

        # Required OS metadata: accessed unconditionally by the
        # build/fetch header (describe_action), so a missing key would
        # otherwise crash with a raw KeyError mid-command.
        missing = [
            k
            for k in ("os_name", "os_version", "container_image")
            if k not in self._data
        ]
        if missing:
            raise ValueError(
                f"target {name!r}: missing required key(s) in "
                f"targets.yaml: {', '.join(missing)}"
            )

        # Resolve effective arch: CLI override > target > defaults
        if arch is not None:
            self._data["arch"] = arch

        self.output_dir = ARTIFACTS_DIR / name / str(self._data["arch"])

        # Gate on the same 'working' fallback the status property uses
        # (previously the gate said 'working' while the property said
        # 'unknown').  Deliberately does NOT setdefault into _data:
        # that would perturb input_hash for targets omitting status.
        status = self._data.get("status", "working")
        if status not in ("working", "experimental"):
            raise ValueError(
                f"Target {name!r} has status={status!r} and is not "
                f"available for use. Only 'working' and 'experimental' "
                f"targets can be built."
            )

        # REQUIRED: lustre.mode. No default, no back-compat -- targets
        # without an explicit mode fail loudly at load time so downstream
        # code never has to guess whether this is a server or client target.
        lustre = self._data.get("lustre")
        if not isinstance(lustre, dict) or "mode" not in lustre:
            raise ValueError(
                f"target {name!r}: missing required 'lustre.mode' in "
                f"{TARGETS_YAML}. Add a 'lustre: {{mode: server_ldiskfs}}' "
                f"block (valid modes: "
                f"{', '.join(m.value for m in LustreMode)})."
            )
        mode_raw = lustre["mode"]
        try:
            self.lustre_mode = LustreMode(mode_raw)
        except ValueError as exc:
            valid = ", ".join(m.value for m in LustreMode)
            raise ValueError(
                f"target {name!r}: unknown lustre.mode {mode_raw!r} in "
                f"{TARGETS_YAML} (valid modes: {valid})"
            ) from exc

        # Parse variants.  The base variant is always present implicitly,
        # with no overlay — its artifact paths match the pre-variant
        # layout so existing on-disk caches keep working.
        raw_variants = self._data.get("variants") or {}
        if not isinstance(raw_variants, dict):
            raise ValueError(
                f"target {name!r}: 'variants' must be a mapping, got "
                f"{type(raw_variants).__name__}"
            )
        if DEFAULT_VARIANT in raw_variants:
            raise ValueError(
                f"target {name!r}: 'base' is a reserved variant name "
                f"and cannot be declared in targets.yaml"
            )
        self._variants: dict[str, Variant] = {
            DEFAULT_VARIANT: Variant(DEFAULT_VARIANT, None, self.target_dir),
        }
        for vname, vdata in raw_variants.items():
            if not isinstance(vdata, dict):
                raise ValueError(
                    f"target {name!r}: variant {vname!r} must be a "
                    f"mapping, got {type(vdata).__name__}"
                )
            self._variants[vname] = Variant(vname, vdata, self.target_dir)

        # Validate kernel pins against the declared kernel list so a
        # typo in targets.yaml fails at TargetConfig load instead of
        # much later with a confusing "no kernel for variant" error.
        # Done lazily -- only if some variant actually pins -- so
        # malformed kernel entries don't break construction of
        # unrelated (unpinned-variant) targets.
        if any(v.pinned_kernel for v in self._variants.values()):
            declared_kernel_names = self.declared_kernels()
            for vname, var in self._variants.items():
                if var.pinned_kernel is None:
                    continue
                if var.pinned_kernel not in declared_kernel_names:
                    raise ValueError(
                        f"target {name!r} variant {vname!r}: pinned "
                        f"kernel {var.pinned_kernel!r} is not declared in "
                        f"kernels.available (declared: "
                        f"{', '.join(declared_kernel_names)})"
                    )

        if variant not in self._variants:
            declared = ", ".join(sorted(self._variants))
            raise ValueError(
                f"target {name!r}: unknown variant {variant!r} "
                f"(declared: {declared})"
            )

    # ------------------------------------------------------------------
    # OS metadata
    # ------------------------------------------------------------------

    @property
    def os_family(self) -> str:
        return str(self._data["os_family"])

    @property
    def os_name(self) -> str:
        return str(self._data["os_name"])

    @property
    def os_version(self) -> str:
        return str(self._data["os_version"])

    @property
    def arch(self) -> str:
        return str(self._data["arch"])

    @property
    def container_image(self) -> str:
        return str(self._data["container_image"])

    @property
    def container_tag(self) -> str:
        """Podman tag for this target's build container (bound variant)."""
        return build_container_tag(self.name, self.arch, self.variant_name)

    @property
    def status(self) -> str:
        # __init__ setdefaults this to 'working', so the key is always
        # present; keep .get for safety on hand-built instances.
        return str(self._data.get("status", "working"))

    @property
    def default_mem(self) -> int:
        """Default VM memory in MB (per-target; fallback 2048)."""
        return int(self._data.get("default_mem", 2048))

    @property
    def srpm_url(self) -> str | None:
        """Base URL for downloading kernel SRPMs, or None if not applicable."""
        v = self._data.get("srpm_url")
        return str(v) if v is not None else None

    @property
    def kernel_deb_source(self) -> str | None:
        """Deb package name for kernel source, or None if not applicable."""
        v = self._data.get("kernel_deb_source")
        return str(v) if v is not None else None

    @property
    def kernel_upstream(self) -> dict[str, Any] | None:
        """kernel.org source config, or None if this isn't an upstream target.

        Presence of the ``kernel_upstream`` block is what makes a target
        build vanilla kernel.org tarballs instead of a distro SRPM or
        linux-source deb.  See ltvm_pkg.upstream_kernel.
        """
        v = self._data.get("kernel_upstream")
        if v is None:
            return None
        if not isinstance(v, dict):
            raise ValueError(
                f"target {self.name!r}: kernel_upstream must be a mapping, "
                f"got {type(v).__name__}"
            )
        return dict(v)

    @property
    def is_upstream(self) -> bool:
        """True when kernels for this target come from kernel.org."""
        return self._data.get("kernel_upstream") is not None

    @property
    def configure_args(self) -> list[str]:
        """Extra configure args specific to this target (e.g. --with-o2ib=no)."""
        v = self._data.get("configure_args", [])
        return list(v)

    @property
    def zfs_version(self) -> str | None:
        """OpenZFS version to use when a build asks for ZFS.

        Declaring this does NOT turn ZFS on -- it only names the version
        `--zfs` builds.  None means "no target preference"; the caller
        falls back to zfs_build.DEFAULT_ZFS_VERSION.
        """
        block = self._data.get("zfs")
        if not isinstance(block, dict):
            return None
        v = block.get("version")
        return str(v) if v is not None else None

    # ROOT_PASSWORD and SSH_TIMEOUT are hardcoded constants in vm_state.py.
    # If we ever want to make them per-target, add a property here AND
    # have vm_state read it via TargetConfig -- right now neither happens.

    # ------------------------------------------------------------------
    # Kernel metadata
    # ------------------------------------------------------------------

    @property
    def default_kernel(self) -> str:
        """Default lustre target name (short form, e.g. 5.14-rhel9.7)."""
        return str(self._kernels["default"])

    @property
    def declared_kernel(self) -> str:
        """The kernel this (target, variant) acts on when none is given.

        A variant's pin wins over the target default -- rocky9's
        mofed-24 pins 5.14-rhel9.5 while the target defaults to
        5.14-rhel9.7.  Consulting default_kernel directly skips the pin
        and names a kernel the variant is forbidden to use.

        This is the *declared* name only; no directory lookup.  Pass it
        to resolve_kernel_dir() to get the built dir.
        """
        var = self._variants.get(self.variant_name)
        if var is not None and var.pinned_kernel is not None:
            return var.pinned_kernel
        return self.default_kernel

    def declared_kernels(self) -> list[str]:
        """Lustre target names declared as available in targets.yaml.

        Entries may be bare strings or mappings with a ``name`` key plus
        per-kernel overrides (see :meth:`kernel_overrides`).  Only names
        are returned here.
        """
        result = [
            self._kernel_entry_name(e) for e in self._raw_kernel_entries()
        ]
        if self.default_kernel not in result:
            result.insert(0, self.default_kernel)
        return result

    def _raw_kernel_entries(self) -> list[Any]:
        return list(self._kernels.get("available", []))

    @staticmethod
    def _kernel_entry_name(entry: Any) -> str:
        if isinstance(entry, str):
            return entry
        if isinstance(entry, dict) and "name" in entry:
            return str(entry["name"])
        raise ValueError(
            f"Invalid kernel entry in targets.yaml: {entry!r} "
            f"(expected string or mapping with 'name')"
        )

    def kernel_overrides(self, name: str) -> dict[str, Any]:
        """Return per-kernel override dict for ``name`` (possibly empty).

        Bare-string entries have no overrides.  Mapping entries carry
        everything except ``name`` as an override -- currently only
        ``srpm_version`` is honored (see kernel_build).
        """
        for entry in self._raw_kernel_entries():
            if isinstance(entry, str):
                if entry == name:
                    return {}
            elif isinstance(entry, dict) and entry.get("name") == name:
                return {k: v for k, v in entry.items() if k != "name"}
        return {}

    @property
    def kernel_config_overrides(self) -> dict[str, str]:
        """Kernel .config overrides from targets.yaml kernels.config.

        Values are normalized to kconfig syntax: YAML booleans (a bare
        ``yes``/``on``/``true`` loads as Python True) become y/n
        instead of leaking ``CONFIG_FOO=True`` into the fragment.
        """
        raw = self._kernels.get("config", {})
        return {
            k: ("y" if v is True else "n" if v is False else str(v))
            for k, v in raw.items()
        }

    def _short_kernel_name(self, name: str) -> str:
        """Return the short kernel name (e.g. "5.14-rhel9.7") from either
        a short or full ("5.14-rhel9.7-5.14.0-611.13.1.el9_7") form.

        Matches against the declared short names in targets.yaml, so any
        name already in short form passes through unchanged.

        A kernel *not* in ``kernels.available`` still has to normalize,
        which is why the fallback strips the ``-<lnxmaj>-<lnxrel>`` tail
        structurally instead of returning the name as-is.  Returning it
        unchanged broke every later command for that kernel, and two
        routine things produce one: building with an explicit
        ``--kernel`` the target does not declare (nothing rejects it),
        and dropping an old minor from ``kernels.available`` while its
        built dir is still on disk -- which ``ltvm clean`` explicitly
        anticipates.  The full name then reached ``parse_target_in`` as
        a .target basename (``[error] Cannot read .target.in``, which
        ``--force-compat`` cannot override) and produced a different
        ``input_hash`` than the short form, so one image was
        permanently stale and the other permanently rebuilt.
        """
        for entry in self._raw_kernel_entries():
            short = self._kernel_entry_name(entry)
            if name == short or name.startswith(short + "-"):
                return short
        m = _FULL_KERNEL_TAIL.search(name)
        if m:
            return name[: m.start()]
        # Neither declared nor in <short>-<lnxmaj>-<lnxrel> shape: a
        # short name for an undeclared kernel, or an upstream spec
        # ("latest", "6.18").  Both are already as short as they get.
        return name

    def resolve_kernel(self, kernel: str | None = None) -> str:
        """Resolve a kernel name (short or full) to the built dir name.

        Kernel directories are named <lustre_target>-<full_version>
        (e.g. 5.14-rhel9.7-5.14.0-611.13.1.el9_7 -- no _lustre suffix;
        that appears only in kernel.release / release tags, set by
        kernel-build-inner.sh's EXTRAVERSION).

        Resolution order:
          1. If this TargetConfig is bound to a variant with a kernel
             pin, the pin acts as the default.  Passing an explicit
             kernel that doesn't match the pin raises ValueError so
             mismatched (--variant, --kernel) combos fail loudly
             instead of silently routing to the wrong artifacts.
          2. Else if kernel is None, use default_kernel.
          3. Hand the resulting name to resolve_kernel_dir(), which
             does exact match -> highest-versioned prefix match ->
             name unchanged.  That function is shared with the release
             packager so both agree on which built kernel is newest.
        """
        # Honor the variant's kernel pin first: if bound to a variant
        # that pins a specific kernel, treat that pin as the default
        # and reject explicit --kernel that disagrees.  resolve_kernel
        # is on the hot path for every build/package/fetch call so
        # getting it right here catches the mismatch early.
        pin = None
        var = self._variants.get(self.variant_name)
        if var is not None and var.pinned_kernel is not None:
            pin = var.pinned_kernel
        if pin is not None:
            if kernel is None:
                kernel = pin
            elif self._short_kernel_name(kernel) != pin:
                raise ValueError(
                    f"target {self.name!r} variant "
                    f"{self.variant_name!r} is pinned to kernel "
                    f"{pin!r}; cannot use {kernel!r}"
                )
        name = kernel if kernel is not None else self.default_kernel
        return resolve_kernel_dir(self.output_dir / "kernels", name)

    def kernel_output_dir(self, kernel: str | None = None) -> Path:
        """Return the output directory for a kernel.

        Accepts short names (5.14-rhel9.7) or full names
        (5.14-rhel9.7-5.14.0-611.13.1.el9_7 -- no _lustre suffix in
        dir names).
        """
        return self.output_dir / "kernels" / self.resolve_kernel(kernel)

    def available_kernels(self) -> list[str]:
        """Return sorted list of built kernel directory names."""
        kernels_dir = self.output_dir / "kernels"
        if not kernels_dir.exists():
            return []
        return sorted(d.name for d in kernels_dir.iterdir() if d.is_dir())

    def image_output_dir(
        self, kernel: str | None = None, variant: str | None = None
    ) -> Path:
        """Return the output directory for an image, keyed by kernel.

        Images are per-kernel because `/lib/modules/<kver>/` is baked
        into the rootfs at build time and must match the kernel the VM
        will boot against.  Non-base variants nest under a subdir so
        base-variant paths keep their pre-variant layout and existing
        on-disk caches don't get orphaned.  ``variant=None`` means
        "use the variant this TargetConfig was bound to".
        """
        v = self.variant_name if variant is None else variant
        base = self.output_dir / "images" / self.resolve_kernel(kernel)
        return base if v == DEFAULT_VARIANT else base / v

    def container_output_dir(self, variant: str | None = None) -> Path:
        v = self.variant_name if variant is None else variant
        base = self.output_dir / "container"
        return base if v == DEFAULT_VARIANT else base / v

    def meta_path(
        self,
        artifact: str,
        kernel: str | None = None,
        variant: str | None = None,
    ) -> Path:
        """Path to meta.json for an artifact ('kernel'|'image'|'container').

        Single source of truth for meta.json location -- previously the
        path was joined three different ways (kernels/<resolved>/meta.json,
        image_output_dir(kernel)/meta.json, output_dir/<artifact>/meta.json),
        which silently diverged once image_output_dir grew per-kernel keying.
        """
        v = self.variant_name if variant is None else variant
        if artifact == "kernel":
            # Kernel is variant-independent; variant arg is accepted but
            # ignored so callers can thread it uniformly without branching.
            return self.kernel_output_dir(kernel) / "meta.json"
        if artifact == "image":
            return self.image_output_dir(kernel, variant=v) / "meta.json"
        if artifact == "container":
            return self.container_output_dir(variant=v) / "meta.json"
        raise ValueError(f"unknown artifact: {artifact!r}")

    # ------------------------------------------------------------------
    # Variants
    # ------------------------------------------------------------------

    def variants(self) -> dict[str, Variant]:
        """Return all variants for this target, including ``base``."""
        return dict(self._variants)

    def declared_variants(self) -> list[str]:
        """Variant names declared in targets.yaml (excludes the
        implicit ``base``)."""
        return [v for v in self._variants if v != DEFAULT_VARIANT]

    def variant(self, name: str) -> Variant:
        """Return the named variant, raising if it isn't declared."""
        if name not in self._variants:
            declared = ", ".join(sorted(self._variants)) or DEFAULT_VARIANT
            raise ValueError(
                f"target {self.name!r}: unknown variant {name!r} "
                f"(declared: {declared})"
            )
        return self._variants[name]

    def applicable_kernels(self, variant: str | None = None) -> list[str]:
        """Return the declared kernels the given variant applies to.

        * base variant (or any variant without a ``kernel:`` pin):
          every kernel the target declares.
        * variant with a ``kernel:`` pin: a single-element list with
          just that kernel.

        Consumed by cmd_targets / cmd_target_show so a pinned variant
        only surfaces under its one valid kernel, and by callers that
        iterate over (kernel, variant) pairs to emit asset rows.
        """
        v = self.variant_name if variant is None else variant
        all_kernels = self.declared_kernels()
        if v == DEFAULT_VARIANT:
            return all_kernels
        var = self.variant(v)
        if var.pinned_kernel is not None:
            return [var.pinned_kernel]
        return all_kernels

    # ------------------------------------------------------------------
    # Staleness and metadata
    # ------------------------------------------------------------------

    def input_hash(
        self,
        artifact: str,
        kernel: str | None = None,
        extra: bytes = b"",
        variant: str | None = None,
    ) -> str:
        """The staleness key for an artifact: see _hash_parts."""
        return self._hash_parts(artifact, kernel, extra, variant).digest()

    def input_components(
        self,
        artifact: str,
        kernel: str | None = None,
        extra: bytes = b"",
        variant: str | None = None,
    ) -> dict[str, str]:
        """Per-input digests behind :meth:`input_hash`.

        What `build status --why` diffs against the values recorded in
        meta.json to name the input that moved, instead of reporting only
        that something did.
        """
        return self._hash_parts(artifact, kernel, extra, variant).components()

    def _hash_parts(
        self,
        artifact: str,
        kernel: str | None = None,
        extra: bytes = b"",
        variant: str | None = None,
    ) -> _HashParts:
        """Hash inputs for an artifact to detect staleness.

        ``extra`` lets a caller fold additional input bytes into the hash
        without target_config needing to know about them.  In particular,
        kernel_build uses this to mix in the contents of Lustre kernel
        patches, the series file, the .target file, and the
        Lustre-provided kernel config -- target_config has no awareness
        of those files but they absolutely affect the built kernel.
        Without this, editing a patch in place doesn't invalidate the
        cached vmlinuz/vmlinux and `is_stale` returns False, silently
        skipping the rebuild that the user is iterating on -- the
        primary workflow this tool exists for.
        """
        h = _HashParts()

        # Always fold in this target's slice of targets.yaml so changes
        # to container_image, srpm_url, kernel_deb_source, configure
        # args, etc. invalidate every artifact for this target.  The
        # ``variants`` block is excluded here and mixed in separately
        # below (only for the relevant variant), so declaring a new
        # variant doesn't invalidate the base cache.
        #
        # ``zfs`` is excluded for a different reason: no byte of the
        # container, kernel or image depends on it.  ZFS is built
        # between the kernel and Lustre and installed into the VM at
        # deploy time, so the only things a version bump must
        # invalidate are the ZFS artifact itself (zfs_build hashes the
        # version directly) and the Lustre build (the version reaches
        # its configure-flags stamp via --with-zfs).  Folding it in
        # here would rebuild every container and kernel for a knob
        # they do not read.
        #
        # ``kernels`` is excluded for the same reason, and is the one
        # that cost the most: the whole block -- ``available`` list and
        # ``default`` included -- used to be hashed into every
        # artifact, so the documented routine operation "for a new
        # kernel minor on an existing OS, just add the short name to
        # kernels.available" invalidated that target's container, every
        # one of its kernels and every one of its images, on every
        # machine at once, with ``--why`` able to say only
        # "targets.yaml (changed)".  Nothing in a container, in kernel
        # N's build, or in an image reads the list of *other* available
        # kernels.  What a kernel build does read is folded back in
        # below, per kernel: ``kernels.config`` and that kernel's own
        # entry (a mapping entry's ``srpm_version``).  The image picks
        # the same up transitively, through the kernel meta's
        # input_hash.
        h.update(self.name.encode())
        h.update(self.arch.encode())
        base_data = {
            k: v
            for k, v in self._data.items()
            if k not in ("variants", "zfs", "kernels")
        }
        h.update(json.dumps(base_data, sort_keys=True).encode())

        if artifact == "container":
            dockerfile = self.target_dir / "container.Dockerfile"
            if dockerfile.exists():
                h.label("container.Dockerfile")
                h.update(dockerfile.read_bytes())
                # Only hash common/ files actually referenced by this
                # Dockerfile's COPY lines -- otherwise unrelated changes
                # (e.g. image-only setup scripts) invalidate the container.
                for f in _dockerfile_referenced_files(dockerfile):
                    if f.is_file():
                        h.label(_component_label(f))
                        h.update(f.read_bytes())
            h.label("packages-dev")
            h.update(self._hash_package_lists("dev").encode())

        elif artifact == "kernel":
            # Always hash the short kernel name (e.g. "5.14-rhel9.7"), not
            # the resolved full name ("5.14-rhel9.7-5.14.0-611.13.1.el9_7"),
            # so the hash is stable across builds and callers that pass
            # either form.
            raw = kernel if kernel is not None else self.default_kernel
            short_name = self._short_kernel_name(raw)
            h.label("kernel-name")
            h.update(short_name.encode())
            h.label("kernels.config")
            for k, v in sorted(self.kernel_config_overrides.items()):
                h.update(f"{k}={v}".encode())
            # This kernel's own entry in kernels.available, and only
            # this one: a mapping entry carries per-kernel build input
            # (rocky10 pins srpm_version that way), while a sibling's
            # entry changing is none of this kernel's business.
            h.label("kernel-entry")
            h.update(
                json.dumps(
                    self.kernel_overrides(short_name), sort_keys=True
                ).encode()
            )
            common_frag = TARGETS_DIR / "common" / "kernel-config.fragment"
            if common_frag.exists():
                h.label("common/kernel-config.fragment")
                h.update(common_frag.read_bytes())
            # The arch-specific fragment is also consumed by
            # kernel_build._build_config_fragment, so it must contribute
            # to the staleness hash too.
            arch_frag = (
                TARGETS_DIR / "common" / f"kernel-config-{self.arch}.fragment"
            )
            if arch_frag.exists():
                h.label(f"common/kernel-config-{self.arch}.fragment")
                h.update(arch_frag.read_bytes())
            # Hash only the inner build script that THIS target's
            # os_family actually invokes -- editing the deb script
            # shouldn't invalidate every RHEL kernel and vice versa.
            ltvm_pkg_dir = Path(__file__).parent
            if self.is_upstream:
                inner_name = "kernel-build-inner-upstream.sh"
            elif self.os_family == "debian":
                inner_name = "kernel-build-inner-deb.sh"
            else:
                inner_name = "kernel-build-inner.sh"
            inner_path = ltvm_pkg_dir / inner_name
            if inner_path.exists():
                h.label(inner_name)
                h.update(inner_path.read_bytes())
            # Also fold in the shared cross-compile helper -- both
            # inner scripts source it, so editing it MUST invalidate
            # the cached vmlinux or is_stale silently returns False.
            cross_helper = TARGETS_DIR / "common" / "cross-compile-env.sh"
            if cross_helper.exists():
                h.label("common/cross-compile-env.sh")
                h.update(cross_helper.read_bytes())

        elif artifact == "image":
            # Image output is keyed per-kernel because /lib/modules/<kver>/
            # is baked in at build time.  Fold the resolved kernel name
            # into the hash so two built kernels under the same target
            # don't collide on the same cached image.
            raw_k = kernel if kernel is not None else self.default_kernel
            short_k = self._short_kernel_name(raw_k)
            h.label("image-kernel")
            h.update(b"image-kernel:")
            h.update(short_k.encode())

            dockerfile = self.target_dir / "image.Dockerfile"
            if dockerfile.exists():
                h.label("image.Dockerfile")
                h.update(dockerfile.read_bytes())
                # Only hash common/ files actually referenced by this
                # Dockerfile's COPY lines.
                for f in _dockerfile_referenced_files(dockerfile):
                    if f.is_file():
                        h.label(_component_label(f))
                        h.update(f.read_bytes())
            h.label("packages-base+test+debug")
            h.update(self._hash_package_lists("base", "test", "debug").encode())
            # Note: packages-server.txt is already hashed via the
            # Dockerfile COPY scan above, so we deliberately do NOT
            # add it again here.  Server-ness comes from lustre.mode
            # (--enable-server for server_* modes) but every image
            # currently installs server packages unconditionally.
            #
            # image_build.py bakes kernel modules into the final image
            # (a second-stage podman build COPYs `kernels/<k>/modules/`).
            # Fold the kernel meta.json's input_hash into the image
            # staleness hash so a rebuilt kernel invalidates the image.
            #
            # The Lustre staging stamp used to be folded in here too,
            # back when image_build also auto-injected Lustre from a
            # global staging dir.  That auto-inject was removed when
            # staging moved per-tree under <lustre_tree>/.ltvm-staging,
            # and the maintainer is expected to bundle Lustre via
            # `ltvm package`'s lustre-artifacts/ instead.
            kernel_meta = self.meta_path("kernel", kernel)
            km = load_meta_safe(kernel_meta)
            if km is not None:
                kh = km.get("input_hash")
                if isinstance(kh, str) and kh:
                    h.label("kernel-artifact")
                    h.update(b"kernel:")
                    h.update(kh.encode())

        if extra:
            # For a kernel this is the Lustre tree's patch series, config
            # and .target file, mixed in by kernel_build (see the
            # docstring); nothing else passes it today.
            h.label(
                "lustre-tree-inputs" if artifact == "kernel" else "extra-inputs"
            )
            h.update(extra)

        # Fold variant inputs last so the base hash composition above
        # remains byte-identical for variant="base" -- i.e. adding the
        # variant feature does not invalidate any existing base caches.
        # Kernel artifacts ignore variant (kernel is shared across
        # variants; see image_build for module injection).
        v_name = self.variant_name if variant is None else variant
        if v_name != DEFAULT_VARIANT and artifact in ("container", "image"):
            # Not `v`: the kernel_config_overrides loop above binds that
            # name to a str, and mypy scopes a name to one type per
            # function.
            variant_obj = self.variant(v_name)
            h.label(f"variant:{v_name}")
            h.update(variant_obj.hash_bytes(artifact))

        return h

    def _kernel_meta_file(self, kernel: str | None) -> Path:
        return self.meta_path("kernel", kernel)

    def is_stale(
        self,
        artifact: str,
        kernel: str | None = None,
        extra_hash: bytes = b"",
        variant: str | None = None,
    ) -> bool:
        """Check if an artifact needs rebuilding.

        ``extra_hash`` is forwarded to ``input_hash`` so callers can fold
        in inputs target_config doesn't know about (see ``input_hash``).
        """
        v = self.variant_name if variant is None else variant
        meta_file = self.meta_path(artifact, kernel, variant=v)
        meta = load_meta_safe(meta_file)
        if meta is None:
            # Missing or corrupt meta -- treat as stale so the next
            # build overwrites it cleanly rather than crashing every
            # subsequent status/build command on the parse error.
            return True
        if not self.outputs_complete(artifact, kernel=kernel, variant=v):
            # meta.json says this hash was built, but the artifact it
            # describes isn't on disk.  A failed rebuild leaves exactly
            # this state: archive_outgoing_vmlinux() renames vmlinux
            # away and the inner script rm -rf's modules/ before the
            # step that dies, while meta.json (whose inputs did not
            # change) stays put.  Hash-only staleness then answered
            # "up to date" forever, and the image build silently
            # produced a rootfs with no kernel modules.  An interrupted
            # `target fetch` lands here too: tar can write meta.json
            # before the payload.
            log.info(
                "%s: %s meta is current but its outputs are missing -- "
                "rebuilding",
                self.name,
                artifact,
            )
            return True
        return bool(
            meta.get("input_hash")
            != self.input_hash(
                artifact, kernel=kernel, extra=extra_hash, variant=v
            )
        )

    def outputs_complete(
        self,
        artifact: str,
        kernel: str | None = None,
        variant: str | None = None,
    ) -> bool:
        """Are the files *artifact*'s meta.json claims to describe present?

        A meta.json is written at the end of a successful build, but
        nothing removes it when a *later* rebuild of the same inputs
        fails partway -- so the hash alone cannot tell "built" from
        "was built once, then destroyed".  Container images live in
        podman's store rather than the filesystem, so they are not
        checked here.
        """
        v = self.variant_name if variant is None else variant
        if artifact == "kernel":
            out = self.kernel_output_dir(kernel)
            if not (out / "vmlinux").exists():
                return False
            if not (out / "vmlinuz").exists():
                return False
            if not (out / "build-tree" / ".config").exists():
                return False
            mods = out / "modules"
            if not mods.is_dir():
                return False
            return any(mods.rglob("*.ko")) or any(mods.rglob("*.ko.xz"))
        if artifact == "image":
            return (
                self.image_output_dir(kernel, variant=v) / "base.ext4"
            ).exists()
        return True

    def write_meta(
        self,
        artifact: str,
        kernel: str | None = None,
        extra_hash: bytes = b"",
        variant: str | None = None,
        **extra: object,
    ) -> None:
        """Write build metadata after a successful build.

        ``extra_hash`` is forwarded to ``input_hash`` so the persisted
        ``input_hash`` matches the one ``is_stale`` will compute on the
        next run.  ``extra`` keyword args are written into meta.json
        verbatim (kernel_version, build_date, etc.).

        ``extra["hash_kernel"]`` names the kernel key to hash, when
        that differs from the one naming the output directory.  The
        kernel builder writes meta into the *full* directory name
        (5.14-rhel9.7-5.14.0-611.42.1.el9_7) but is_stale() is called
        with the declared short name.  input_hash() normalises the two
        via _short_kernel_name(), which can only do so for kernels
        declared in targets.yaml -- for anything else the two hashes
        differed and the kernel rebuilt from scratch on every single
        invocation, showing permanently stale in `build status`.  It is
        consumed here, not written into meta.json.
        """
        hash_kernel_raw = extra.pop("hash_kernel", None)
        hash_kernel = (
            hash_kernel_raw if isinstance(hash_kernel_raw, str) else None
        )
        v = self.variant_name if variant is None else variant
        if artifact == "kernel":
            out_dir = self._kernel_meta_file(kernel).parent
        elif artifact == "image":
            out_dir = self.image_output_dir(kernel, variant=v)
        elif artifact == "container":
            out_dir = self.container_output_dir(variant=v)
        else:
            out_dir = self.output_dir / artifact
        out_dir.mkdir(parents=True, exist_ok=True)
        hash_kernel_arg = hash_kernel if hash_kernel is not None else kernel
        meta = {
            "target": self.name,
            # Which formula the hash below came from.  An artifact whose
            # scheme predates this ltvm's is stale for a reason no
            # per-input diff can express, and staleness_reasons says so
            # rather than blaming an input that did not move.
            "hash_scheme": HASH_SCHEME,
            "input_hash": self.input_hash(
                artifact,
                kernel=hash_kernel_arg,
                extra=extra_hash,
                variant=v,
            ),
            # The per-input digests behind that hash, so `build status
            # --why` can name which one moved rather than reporting only
            # that the total did.  Computed from the same arguments,
            # necessarily: a breakdown over different inputs than the
            # hash would explain the wrong thing.
            "input_components": self.input_components(
                artifact,
                kernel=hash_kernel_arg,
                extra=extra_hash,
                variant=v,
            ),
            **extra,
        }
        if v != DEFAULT_VARIANT:
            meta["variant"] = v
        # Atomic write via tempfile + rename so a concurrent reader
        # (load_meta_safe) can't see a half-written JSON blob -- which
        # would fail to parse, return None, and trigger a spurious
        # rebuild.
        meta_path = out_dir / "meta.json"
        text = json.dumps(meta, indent=2) + "\n"
        fd, tmp_str = tempfile.mkstemp(
            dir=str(out_dir), prefix=f".{meta_path.name}."
        )
        tmp = Path(tmp_str)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.chmod(tmp, 0o644)
            tmp.rename(meta_path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _hash_package_lists(self, *roles: str) -> str:
        parts = []
        for role in roles:
            common = TARGETS_DIR / "common" / f"packages-{role}.txt"
            if common.exists():
                parts.append(common.read_text())
            per_os = self.target_dir / f"packages-{role}.txt"
            if per_os.exists():
                parts.append(per_os.read_text())
        return "\n".join(parts)


def list_targets() -> list[str]:
    """Return names of all targets declared in targets.yaml."""
    registry = _load_registry()
    return list(registry.get("targets", {}).keys())
