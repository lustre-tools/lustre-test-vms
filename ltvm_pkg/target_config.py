"""Target configuration for ltvm.

Single source of truth: targets/targets.yaml
Dockerfiles and package lists live in targets/<name>/.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class LustreMode(str, Enum):
    """Lustre build/deploy mode for a target.

    CLIENT targets build only client modules; no ldiskfs/OSD code
    and no kernel patches required.  validate_target consults
    ChangeLog's client_primary/client_best_effort lists for these.
    """

    SERVER_LDISKFS = "server_ldiskfs"
    SERVER_ZFS = "server_zfs"
    CLIENT = "client"

from .paths import find_ltvm_root, load_meta_safe

REPO_ROOT = find_ltvm_root()
TARGETS_DIR = REPO_ROOT / "targets"
OUTPUT_DIR = (
    Path(os.environ["LTVM_OUTPUT_DIR"])
    if "LTVM_OUTPUT_DIR" in os.environ
    else REPO_ROOT / "output"
)
TARGETS_YAML = TARGETS_DIR / "targets.yaml"

_DEFAULTS = {
    "arch": "x86_64",
    "os_family": "rhel",
}

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
        co = self._data.get("container_overlay")
        io = self._data.get("image_overlay")
        # Overlay paths in YAML are relative to the repo's targets/
        # directory so they can reference shared snippets.  We resolve
        # relative to TARGETS_DIR, not target_dir, for that reason.
        self.container_overlay: Path | None = (
            (TARGETS_DIR / co) if co else None
        )
        self.image_overlay: Path | None = (TARGETS_DIR / io) if io else None
        self.packages: list[str] = list(self._data.get("packages", []))
        self.params: dict[str, Any] = dict(self._data.get("params", {}))

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
              output/<target>/<arch>/ regardless of arch -- the layout
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

        # Resolve effective arch: CLI override > target > defaults
        if arch is not None:
            self._data["arch"] = arch

        self.output_dir = OUTPUT_DIR / name / str(self._data["arch"])

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

    def container_tag_for(self, variant: str) -> str:
        """Container tag for an explicit variant (bypasses the bound one)."""
        return build_container_tag(self.name, self.arch, variant)

    @property
    def status(self) -> str:
        return str(self._data.get("status", "unknown"))

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
    def configure_args(self) -> list[str]:
        """Extra configure args specific to this target (e.g. --with-o2ib=no)."""
        v = self._data.get("configure_args", [])
        return list(v)

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

    def declared_kernels(self) -> list[str]:
        """Lustre target names declared as available in targets.yaml.

        Entries may be bare strings or mappings with a ``name`` key plus
        per-kernel overrides (see :meth:`kernel_overrides`).  Only names
        are returned here.
        """
        result = [self._kernel_entry_name(e) for e in self._raw_kernel_entries()]
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
        """Kernel .config overrides from targets.yaml kernels.config."""
        return dict(self._kernels.get("config", {}))

    def _short_kernel_name(self, name: str) -> str:
        """Return the short kernel name (e.g. "5.14-rhel9.7") from either
        a short or full ("5.14-rhel9.7-5.14.0-611.13.1.el9_7") form.

        Matches against the declared short names in targets.yaml, so any
        name already in short form passes through unchanged.
        """
        for entry in self._raw_kernel_entries():
            short = self._kernel_entry_name(entry)
            if name == short or name.startswith(short + "-"):
                return short
        # Fallback: if no match, return as-is (new kernel or unknown form)
        return name

    def resolve_kernel(self, kernel: str | None = None) -> str:
        """Resolve a kernel name (short or full) to the built dir name.

        Kernel directories are named <lustre_target>-<full_version>
        (e.g. 5.14-rhel9.7-5.14.0-611.13.1.el9_7_lustre).

        Resolution order:
          1. If kernel is None, use default_kernel.
          2. Exact directory match.
          3. Prefix match: scan for dirs starting with <kernel>-,
             pick the lexicographically latest.
          4. Return name as-is (for new builds not yet on disk).
        """
        name = kernel if kernel is not None else self.default_kernel
        kernels_dir = self.output_dir / "kernels"

        if not kernels_dir.exists():
            return name

        # Exact match
        if (kernels_dir / name).is_dir():
            return name

        # Prefix match (short name -> full-version dir)
        prefix = name + "-"
        candidates = sorted(
            d.name
            for d in kernels_dir.iterdir()
            if d.is_dir() and d.name.startswith(prefix)
        )
        if candidates:
            return candidates[-1]

        return name

    def kernel_output_dir(self, kernel: str | None = None) -> Path:
        """Return the output directory for a kernel.

        Accepts short names (5.14-rhel9.7) or full names
        (5.14-rhel9.7-5.14.0-611.13.1.el9_7_lustre).
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
        h = hashlib.sha256()

        # Always fold in this target's slice of targets.yaml so changes
        # to container_image, srpm_url, kernel_deb_source, configure
        # args, etc. invalidate every artifact for this target.  The
        # ``variants`` block is excluded here and mixed in separately
        # below (only for the relevant variant), so declaring a new
        # variant doesn't invalidate the base cache.
        h.update(self.name.encode())
        h.update(self.arch.encode())
        base_data = {k: v for k, v in self._data.items() if k != "variants"}
        h.update(json.dumps(base_data, sort_keys=True).encode())

        if artifact == "container":
            dockerfile = self.target_dir / "container.Dockerfile"
            if dockerfile.exists():
                h.update(dockerfile.read_bytes())
                # Only hash common/ files actually referenced by this
                # Dockerfile's COPY lines -- otherwise unrelated changes
                # (e.g. image-only setup scripts) invalidate the container.
                for f in _dockerfile_referenced_files(dockerfile):
                    if f.is_file():
                        h.update(f.read_bytes())
            h.update(self._hash_package_lists("dev").encode())

        elif artifact == "kernel":
            # Always hash the short kernel name (e.g. "5.14-rhel9.7"), not
            # the resolved full name ("5.14-rhel9.7-5.14.0-611.13.1.el9_7"),
            # so the hash is stable across builds and callers that pass
            # either form.
            raw = kernel if kernel is not None else self.default_kernel
            short_name = self._short_kernel_name(raw)
            h.update(short_name.encode())
            for k, v in sorted(self.kernel_config_overrides.items()):
                h.update(f"{k}={v}".encode())
            common_frag = TARGETS_DIR / "common" / "kernel-config.fragment"
            if common_frag.exists():
                h.update(common_frag.read_bytes())
            # The arch-specific fragment is also consumed by
            # kernel_build._build_config_fragment, so it must contribute
            # to the staleness hash too.
            arch_frag = (
                TARGETS_DIR / "common" / f"kernel-config-{self.arch}.fragment"
            )
            if arch_frag.exists():
                h.update(arch_frag.read_bytes())
            # Hash only the inner build script that THIS target's
            # os_family actually invokes -- editing the deb script
            # shouldn't invalidate every RHEL kernel and vice versa.
            ltvm_pkg_dir = Path(__file__).parent
            inner_name = (
                "kernel-build-inner-deb.sh"
                if self.os_family == "debian"
                else "kernel-build-inner.sh"
            )
            inner_path = ltvm_pkg_dir / inner_name
            if inner_path.exists():
                h.update(inner_path.read_bytes())

        elif artifact == "image":
            # Image output is keyed per-kernel because /lib/modules/<kver>/
            # is baked in at build time.  Fold the resolved kernel name
            # into the hash so two built kernels under the same target
            # don't collide on the same cached image.
            raw_k = kernel if kernel is not None else self.default_kernel
            short_k = self._short_kernel_name(raw_k)
            h.update(b"image-kernel:")
            h.update(short_k.encode())

            dockerfile = self.target_dir / "image.Dockerfile"
            if dockerfile.exists():
                h.update(dockerfile.read_bytes())
                # Only hash common/ files actually referenced by this
                # Dockerfile's COPY lines.
                for f in _dockerfile_referenced_files(dockerfile):
                    if f.is_file():
                        h.update(f.read_bytes())
            h.update(self._hash_package_lists("base", "test", "debug").encode())
            # Note: packages-server.txt is already hashed via the
            # Dockerfile COPY scan above, so we deliberately do NOT
            # add it again here.  The `server` field in targets.yaml
            # affects Lustre build (--enable-server) but every image
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
                    h.update(b"kernel:")
                    h.update(kh.encode())

        if extra:
            h.update(extra)

        # Fold variant inputs last so the base hash composition above
        # remains byte-identical for variant="base" -- i.e. adding the
        # variant feature does not invalidate any existing base caches.
        # Kernel artifacts ignore variant (kernel is shared across
        # variants; see image_build for module injection).
        v_name = self.variant_name if variant is None else variant
        if v_name != DEFAULT_VARIANT and artifact in ("container", "image"):
            v = self.variant(v_name)
            h.update(v.hash_bytes(artifact))

        return h.hexdigest()[:16]

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
        return bool(
            meta.get("input_hash")
            != self.input_hash(
                artifact, kernel=kernel, extra=extra_hash, variant=v
            )
        )

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
        """
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
        meta = {
            "target": self.name,
            "input_hash": self.input_hash(
                artifact, kernel=kernel, extra=extra_hash, variant=v
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
