"""Target configuration for ltvm.

Single source of truth: targets/targets.yaml
Dockerfiles and package lists live in targets/<name>/.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class LustreMode(str, Enum):
    """Lustre build/deploy mode for a target.

    Client mode is intentionally absent -- it's a separate follow-on
    issue because client targets don't need a patched kernel build.
    """

    SERVER_LDISKFS = "server_ldiskfs"
    SERVER_ZFS = "server_zfs"

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

        # Schema validation: catch type errors in targets.yaml early
        # so we don't get confusing downstream behavior (e.g. server:
        # "yes" silently truthy, missing kernels block raising
        # KeyError mid-build).
        if not isinstance(self._data.get("server"), bool):
            raise ValueError(
                f"target {name!r}: 'server' must be a YAML boolean "
                f"(true/false), got {self._data.get('server')!r}"
            )
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
        available: list[str] = self._kernels.get("available", [])
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

    def input_hash(
        self,
        artifact: str,
        kernel: str | None = None,
        extra: bytes = b"",
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
        # args, etc. invalidate every artifact for this target.
        h.update(self.name.encode())
        h.update(self.arch.encode())
        h.update(json.dumps(self._data, sort_keys=True).encode())

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
            kernel_meta = (
                self.output_dir
                / "kernels"
                / self.resolve_kernel()
                / "meta.json"
            )
            km = load_meta_safe(kernel_meta)
            if km is not None:
                kh = km.get("input_hash")
                if isinstance(kh, str) and kh:
                    h.update(b"kernel:")
                    h.update(kh.encode())

        if extra:
            h.update(extra)

        return h.hexdigest()[:16]

    def _kernel_meta_file(self, kernel: str | None) -> Path:
        return (
            self.output_dir
            / "kernels"
            / self.resolve_kernel(kernel)
            / "meta.json"
        )

    def is_stale(
        self,
        artifact: str,
        kernel: str | None = None,
        extra_hash: bytes = b"",
    ) -> bool:
        """Check if an artifact needs rebuilding.

        ``extra_hash`` is forwarded to ``input_hash`` so callers can fold
        in inputs target_config doesn't know about (see ``input_hash``).
        """
        if artifact == "kernel":
            meta_file = self._kernel_meta_file(kernel)
        else:
            meta_file = self.output_dir / artifact / "meta.json"
        meta = load_meta_safe(meta_file)
        if meta is None:
            # Missing or corrupt meta -- treat as stale so the next
            # build overwrites it cleanly rather than crashing every
            # subsequent status/build command on the parse error.
            return True
        return bool(
            meta.get("input_hash")
            != self.input_hash(artifact, kernel=kernel, extra=extra_hash)
        )

    def write_meta(
        self,
        artifact: str,
        kernel: str | None = None,
        extra_hash: bytes = b"",
        **extra: object,
    ) -> None:
        """Write build metadata after a successful build.

        ``extra_hash`` is forwarded to ``input_hash`` so the persisted
        ``input_hash`` matches the one ``is_stale`` will compute on the
        next run.  ``extra`` keyword args are written into meta.json
        verbatim (kernel_version, build_date, etc.).
        """
        if artifact == "kernel":
            out_dir = self._kernel_meta_file(kernel).parent
        else:
            out_dir = self.output_dir / artifact
        out_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "target": self.name,
            "input_hash": self.input_hash(
                artifact, kernel=kernel, extra=extra_hash
            ),
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
