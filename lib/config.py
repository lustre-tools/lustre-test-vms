"""Target configuration parsing for ltvm."""

from __future__ import annotations

import configparser
import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TARGETS_DIR = REPO_ROOT / "targets"
OUTPUT_DIR = REPO_ROOT / "output"


class TargetConfig:
    """Parsed target configuration."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.target_dir = TARGETS_DIR / name
        self.output_dir = OUTPUT_DIR / name

        if not self.target_dir.exists():
            raise ValueError(f"Unknown target: {name} (no {self.target_dir})")

        self._target = configparser.ConfigParser()
        self._target.read(self.target_dir / "target.conf")

        self._kernel = configparser.RawConfigParser()
        self._kernel.optionxform = str  # type: ignore[assignment]  # preserve case
        self._kernel.read(self.target_dir / "kernel.conf")

    @property
    def os_family(self) -> str:
        return self._target.get("target", "os_family")

    @property
    def os_name(self) -> str:
        return self._target.get("target", "os_name")

    @property
    def os_version(self) -> str:
        return self._target.get("target", "os_version")

    @property
    def server(self) -> bool:
        return self._target.getboolean("target", "server")

    @property
    def arch(self) -> str:
        return self._target.get("target", "arch")

    @property
    def container_image(self) -> str:
        return self._target.get("target", "container_image")

    @property
    def lustre_target(self) -> str:
        return self._kernel.get("kernel", "lustre_target")

    @property
    def default_kernel(self) -> str:
        """The default kernel name (lustre_target from kernel.conf)."""
        return self.lustre_target

    @property
    def kernel_config_overrides(self) -> dict[str, str]:
        """Microvm-specific kernel config overrides."""
        overrides: dict[str, str] = {}
        if self._kernel.has_section("config"):
            overrides.update(dict(self._kernel.items("config")))
        return overrides

    def resolve_kernel(self, kernel: str | None = None) -> str:
        """Return the kernel name to use, falling back to the default."""
        return kernel if kernel is not None else self.default_kernel

    def kernel_output_dir(self, kernel: str | None = None) -> Path:
        """Return the output directory for a kernel build.

        When kernel is None, the default kernel (lustre_target) is used.
        Kernels are stored under output/<target>/kernels/<kernel_name>/.
        """
        return self.output_dir / "kernels" / self.resolve_kernel(kernel)

    def available_kernels(self) -> list[str]:
        """Return a sorted list of kernel names that have been built."""
        kernels_dir = self.output_dir / "kernels"
        if not kernels_dir.exists():
            return []
        return sorted(d.name for d in kernels_dir.iterdir() if d.is_dir())

    def image_output_dir(self) -> Path:
        return self.output_dir / "image"

    def container_output_dir(self) -> Path:
        return self.output_dir / "container"

    def input_hash(self, artifact: str, kernel: str | None = None) -> str:
        """Hash the inputs for an artifact to detect staleness.

        artifact: 'container', 'kernel', or 'image'
        kernel: kernel name override for artifact=='kernel'; defaults to
                lustre_target when None.
        """
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
        """Return the meta.json path for a kernel artifact."""
        return (
            self.output_dir
            / "kernels"
            / self.resolve_kernel(kernel)
            / "meta.json"
        )

    def is_stale(self, artifact: str, kernel: str | None = None) -> bool:
        """Check if an artifact needs rebuilding.

        For artifact=='kernel', kernel selects which kernel to check;
        defaults to the configured lustre_target.
        """
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
        """Write build metadata after successful build.

        For artifact=='kernel', kernel selects which kernel's metadata to
        write; defaults to the configured lustre_target.
        """
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
    """Return names of all configured targets."""
    targets = []
    for d in sorted(TARGETS_DIR.iterdir()):
        if d.is_dir() and (d / "target.conf").exists():
            targets.append(d.name)
    return targets
