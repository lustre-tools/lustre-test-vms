"""Target configuration for ltvm.

Single source of truth: targets/targets.yaml
Dockerfiles and package lists live in targets/<name>/.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, TypedDict

import yaml

REPO_ROOT = Path(__file__).parent.parent
TARGETS_DIR = REPO_ROOT / "targets"
OUTPUT_DIR = REPO_ROOT / "output"
TARGETS_YAML = TARGETS_DIR / "targets.yaml"

_DEFAULTS = {
    "arch": "x86_64",
    "os_family": "rhel",
    "server": True,
}

_COPY_RE = re.compile(r"^\s*COPY\s+(\S+)", re.MULTILINE)


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
              target's default arch and routes output to an
              arch-qualified subdirectory (output/<target>/<arch>/).
              When None (default), the target's configured arch is used
              and output goes to output/<target>/ (backward-compatible).
    """

    def __init__(self, name: str, arch: str | None = None) -> None:
        self.name = name
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
        self._kernels: dict[str, Any] = self._data.get("kernels", {})

        # Resolve effective arch: CLI override > target > defaults
        default_arch = str(self._data["arch"])
        if arch is not None:
            self._data["arch"] = arch

        # Output directory: include arch subdirectory when an explicit
        # override was given (so x86_64 and aarch64 artifacts coexist).
        # When no override, keep the flat layout for backward compat.
        if arch is not None and arch != default_arch:
            self.output_dir = OUTPUT_DIR / name / arch
        else:
            self.output_dir = OUTPUT_DIR / name

        status = self._data.get("status", "working")
        if status not in ("working", "experimental"):
            raise ValueError(
                f"Target {name!r} has status={status!r} and is not "
                f"available for use. Only 'working' and 'experimental' "
                f"targets can be built."
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
    def server(self) -> bool:
        return bool(self._data["server"])

    @property
    def arch(self) -> str:
        return str(self._data["arch"])

    @property
    def container_image(self) -> str:
        return str(self._data["container_image"])

    @property
    def status(self) -> str:
        return str(self._data.get("status", "unknown"))

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

    @property
    def lustre_target(self) -> str:
        """Alias for default_kernel (backward compat)."""
        return self.default_kernel

    def declared_kernels(self) -> list[str]:
        """Lustre target names declared as available in targets.yaml."""
        available = self._kernels.get("available", [])
        result = list(available)
        if self.default_kernel not in result:
            result.insert(0, self.default_kernel)
        return result

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
        available = self._kernels.get("available", [])
        for short in available:
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

    def image_output_dir(self) -> Path:
        return self.output_dir / "image"

    def container_output_dir(self) -> Path:
        return self.output_dir / "container"

    # ------------------------------------------------------------------
    # Staleness and metadata
    # ------------------------------------------------------------------

    def input_hash(self, artifact: str, kernel: str | None = None) -> str:
        """Hash inputs for an artifact to detect staleness."""
        h = hashlib.sha256()

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

        elif artifact == "image":
            dockerfile = self.target_dir / "image.Dockerfile"
            if dockerfile.exists():
                h.update(dockerfile.read_bytes())
                # Only hash common/ files actually referenced by this
                # Dockerfile's COPY lines.
                for f in _dockerfile_referenced_files(dockerfile):
                    if f.is_file():
                        h.update(f.read_bytes())
            h.update(self._hash_package_lists("base", "test", "debug").encode())
            if self.server:
                h.update(self._hash_package_lists("server").encode())

        return h.hexdigest()[:16]

    def _kernel_meta_file(self, kernel: str | None) -> Path:
        return (
            self.output_dir
            / "kernels"
            / self.resolve_kernel(kernel)
            / "meta.json"
        )

    def is_stale(self, artifact: str, kernel: str | None = None) -> bool:
        """Check if an artifact needs rebuilding."""
        if artifact == "kernel":
            meta_file = self._kernel_meta_file(kernel)
        else:
            meta_file = self.output_dir / artifact / "meta.json"
        if not meta_file.exists():
            return True
        meta = json.loads(meta_file.read_text())
        return bool(
            meta.get("input_hash") != self.input_hash(artifact, kernel=kernel)
        )

    def write_meta(
        self, artifact: str, kernel: str | None = None, **extra: object
    ) -> None:
        """Write build metadata after a successful build."""
        if artifact == "kernel":
            out_dir = self._kernel_meta_file(kernel).parent
        else:
            out_dir = self.output_dir / artifact
        out_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "target": self.name,
            "input_hash": self.input_hash(artifact, kernel=kernel),
            **extra,
        }
        (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

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
