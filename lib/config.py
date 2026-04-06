"""Target configuration for ltvm.

Single source of truth: targets/targets.yaml
Dockerfiles and package lists live in targets/<name>/.
"""

from __future__ import annotations

import hashlib
import json
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


def _load_registry() -> dict[str, Any]:
    """Load and return the full targets.yaml registry."""
    if not TARGETS_YAML.exists():
        raise FileNotFoundError(f"Target registry not found: {TARGETS_YAML}")
    with TARGETS_YAML.open() as f:
        data: dict[str, Any] = yaml.safe_load(f)
        return data


class TargetConfig:
    """Parsed configuration for a single build target."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.target_dir = TARGETS_DIR / name
        self.output_dir = OUTPUT_DIR / name

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
            h.update(self._hash_package_lists("dev").encode())

        elif artifact == "kernel":
            h.update(self.resolve_kernel(kernel).encode())
            for k, v in sorted(self.kernel_config_overrides.items()):
                h.update(f"{k}={v}".encode())
            common_frag = TARGETS_DIR / "common" / "kernel-config.fragment"
            if common_frag.exists():
                h.update(common_frag.read_bytes())

        elif artifact == "image":
            dockerfile = self.target_dir / "image.Dockerfile"
            if dockerfile.exists():
                h.update(dockerfile.read_bytes())
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


# ------------------------------------------------------------------
# Target scaffolding
# ------------------------------------------------------------------


def _infer_os(image: str) -> tuple[str, str, str]:
    """Infer (os_name, os_family, os_version) from a container image tag.

    Examples:
        rockylinux:9.7  -> (rocky, rhel, 9.7)
        almalinux:9.4   -> (alma, rhel, 9.4)
        ubuntu:24.04    -> (ubuntu, debian, 24.04)
        debian:12       -> (debian, debian, 12)
    """
    base = image.split("/")[-1]  # strip registry prefix
    name_part, _, ver = base.partition(":")
    name_part = name_part.lower()
    ver = ver or "unknown"

    rhel_names = {
        "rockylinux": "rocky",
        "almalinux": "alma",
        "centos": "centos",
        "oraclelinux": "oracle",
    }
    debian_names = {"ubuntu", "debian"}

    for key, short in rhel_names.items():
        if key in name_part:
            return short, "rhel", ver

    for dname in debian_names:
        if dname in name_part:
            return dname, "debian", ver

    return name_part, "unknown", ver


class ScaffoldResult(TypedDict):
    """Result of add_target scaffolding."""

    name: str
    target_dir: str
    files_created: list[str]


def add_target(
    name: str,
    image: str,
    *,
    kernel: str | None = None,
    srpm_url: str | None = None,
    server: bool | None = None,
    status: str = "planned",
) -> ScaffoldResult:
    """Scaffold a new target: directory, Dockerfiles, YAML entry.

    Creates targets/<name>/ with container.Dockerfile,
    image.Dockerfile, and packages-os.txt from templates.
    Adds the target entry to targets.yaml.

    Raises ValueError if the target already exists.
    """
    registry = _load_registry()
    targets = registry.get("targets", {})
    if name in targets:
        raise ValueError(f"Target {name!r} already exists in {TARGETS_YAML}")

    os_name, os_family, os_version = _infer_os(image)

    # Create target directory
    target_dir = TARGETS_DIR / name
    target_dir.mkdir(parents=True, exist_ok=True)

    files_created: list[str] = []

    # Find a reference target to copy from (prefer same os_family)
    ref_dir = _find_reference_target(os_family)

    # Generate container.Dockerfile
    container_df = target_dir / "container.Dockerfile"
    if not container_df.exists():
        _generate_container_dockerfile(container_df, image, os_family, ref_dir)
        files_created.append(str(container_df))

    # Generate image.Dockerfile
    image_df = target_dir / "image.Dockerfile"
    if not image_df.exists():
        _generate_image_dockerfile(image_df, image, name, os_family, ref_dir)
        files_created.append(str(image_df))

    # Generate packages-os.txt stub
    pkg_file = target_dir / "packages-os.txt"
    if not pkg_file.exists():
        _generate_packages_os(pkg_file, os_name, os_family)
        files_created.append(str(pkg_file))

    # Add to targets.yaml
    entry: dict[str, Any] = {
        "os_name": os_name,
        "os_version": os_version,
        "container_image": image,
        "status": status,
    }
    if os_family != "rhel":
        entry["os_family"] = os_family
    if server is not None and not server:
        entry["server"] = False
    if srpm_url:
        entry["srpm_url"] = srpm_url
    if kernel:
        entry["kernels"] = {
            "default": kernel,
            "available": [kernel],
            "config": {},
        }

    targets[name] = entry
    registry["targets"] = targets
    TARGETS_YAML.write_text(yaml.dump(registry, default_flow_style=False))
    files_created.append(str(TARGETS_YAML))

    return {
        "name": name,
        "target_dir": str(target_dir),
        "files_created": files_created,
    }


def _find_reference_target(os_family: str) -> Path | None:
    """Find an existing target directory to use as a template."""
    try:
        registry = _load_registry()
    except FileNotFoundError:
        return None

    targets: dict[str, Any] = registry.get("targets", {})
    for tname, tdata in targets.items():
        tfamily = tdata.get("os_family", "rhel")
        if tfamily == os_family:
            candidate = TARGETS_DIR / tname
            if (candidate / "container.Dockerfile").exists():
                return candidate
    return None


def _generate_container_dockerfile(
    path: Path, image: str, os_family: str, ref_dir: Path | None
) -> None:
    """Generate a container.Dockerfile for the new target."""
    if ref_dir and (ref_dir / "container.Dockerfile").exists():
        # Copy from reference and substitute the FROM line
        content = (ref_dir / "container.Dockerfile").read_text()
        lines = content.split("\n")
        new_lines = []
        for line in lines:
            if line.startswith("FROM "):
                new_lines.append(f"FROM {image}")
            else:
                new_lines.append(line)
        path.write_text("\n".join(new_lines))
        return

    # No reference -- generate a minimal stub
    if os_family == "debian":
        path.write_text(f"""\
FROM {image}

# TODO: Customize for this target
# Build container for kernel and Lustre builds.

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \\
        build-essential bc bison flex \\
        libelf-dev libssl-dev \\
        rsync tar gzip xz-utils \\
        kmod git python3 python3-dev \\
    && rm -rf /var/lib/apt/lists/*

# Lustre build dependencies
RUN apt-get update && apt-get install -y \\
        autoconf automake libtool \\
        libyaml-dev libselinux1-dev zlib1g-dev \\
        module-assistant \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
ENTRYPOINT ["/bin/bash"]
""")
    else:
        path.write_text(f"""\
FROM {image}

# TODO: Customize for this target
# Build container for kernel and Lustre builds.

RUN dnf -y install dnf-plugins-core epel-release \\
    && dnf -y install \\
        rpm-build gcc gcc-c++ make bc bison flex \\
        elfutils-libelf-devel openssl-devel \\
        perl-interpreter ncurses-devel dwarves \\
        rsync tar gzip xz kmod \\
        python3 python3-devel \\
    && dnf clean all

# Lustre build dependencies
RUN dnf -y install \\
        autoconf automake libtool git patch \\
        libyaml-devel libmount-devel \\
        libselinux-devel zlib-devel \\
    && dnf clean all

WORKDIR /build
ENTRYPOINT ["/bin/bash"]
""")


def _generate_image_dockerfile(
    path: Path,
    image: str,
    name: str,
    os_family: str,
    ref_dir: Path | None,
) -> None:
    """Generate an image.Dockerfile for the new target."""
    if ref_dir and (ref_dir / "image.Dockerfile").exists():
        content = (ref_dir / "image.Dockerfile").read_text()
        lines = content.split("\n")
        ref_name = ref_dir.name
        new_lines = []
        for line in lines:
            if line.startswith("FROM "):
                new_lines.append(f"FROM {image}")
            else:
                # Replace references to the old target name in COPY paths
                new_lines.append(line.replace(f"{ref_name}/", f"{name}/"))
        path.write_text("\n".join(new_lines))
        return

    # No reference -- minimal stub
    if os_family == "debian":
        path.write_text(f"""\
FROM {image}

# TODO: Customize for this target
# VM base image -- exported to raw ext4 for QEMU microvm use.

ENV DEBIAN_FRONTEND=noninteractive

COPY common/packages-base.txt /tmp/packages-base.txt
COPY {name}/packages-os.txt /tmp/packages-os.txt

RUN apt-get update \\
    && cat /tmp/packages-base.txt /tmp/packages-os.txt \\
        | grep -v '^\\s*#' | grep -v '^\\s*$' \\
        | sort -u \\
        | xargs apt-get install -y \\
    && rm -rf /var/lib/apt/lists/*

# TODO: Add SSH setup, serial console, networking, kdump
# See an existing rhel target's image.Dockerfile for reference.

ENTRYPOINT ["/bin/bash"]
""")
    else:
        path.write_text(f"""\
FROM {image}

# TODO: Customize for this target
# VM base image -- exported to raw ext4 for QEMU microvm use.

COPY common/packages-base.txt /tmp/packages-base.txt
COPY {name}/packages-os.txt /tmp/packages-os.txt

RUN cat /tmp/packages-base.txt /tmp/packages-os.txt \\
    | grep -v '^\\s*#' | grep -v '^\\s*$' \\
    | sort -u \\
    | xargs dnf -y --allowerasing install \\
    && dnf clean all

# TODO: Add SSH setup, serial console, networking, kdump
# See an existing rhel target's image.Dockerfile for reference.

ENTRYPOINT ["/bin/bash"]
""")


def _generate_packages_os(path: Path, os_name: str, os_family: str) -> None:
    """Generate a packages-os.txt stub."""
    if os_family == "debian":
        path.write_text(f"""\
# packages-os.txt -- {os_name}-specific packages
# Add distro-specific packages here

# TODO: Add {os_name}-specific packages
""")
    else:
        path.write_text(f"""\
# packages-os.txt -- {os_name}-specific packages
# Add distro-specific packages here

{os_name}-release
epel-release
""")
