"""Tests for ltvm_pkg/release_package.py -- artifact resolution and packaging."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg.release_package import (
    _dir_size_mb,
    _find_artifacts,
    _resolve_kernel,
    export_build_container,
    package_target,
    snapshot_lustre,
)


class TestResolveKernel:
    def test_explicit_kernel(self, tmp_path: Path) -> None:
        name, path = _resolve_kernel(tmp_path, "my-kernel")
        assert name == "my-kernel"
        assert path == tmp_path / "kernels" / "my-kernel"

    def test_auto_detect_single(self, tmp_path: Path) -> None:
        kdir = tmp_path / "kernels" / "5.14-rhel9.7"
        kdir.mkdir(parents=True)
        (kdir / "vmlinux").touch()
        name, path = _resolve_kernel(tmp_path, None)
        assert name == "5.14-rhel9.7"
        assert path == kdir

    def test_auto_detect_picks_latest_sorted(self, tmp_path: Path) -> None:
        for k in ["beta-kernel", "alpha-kernel"]:
            kdir = tmp_path / "kernels" / k
            kdir.mkdir(parents=True)
            (kdir / "vmlinux").touch()
        name, _ = _resolve_kernel(tmp_path, None)
        assert name == "beta-kernel"

    def test_auto_detect_no_kernels_dir(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="No kernels/ directory"):
            _resolve_kernel(tmp_path, None)

    def test_auto_detect_no_vmlinux(self, tmp_path: Path) -> None:
        kdir = tmp_path / "kernels" / "empty-kernel"
        kdir.mkdir(parents=True)
        with pytest.raises(ValueError, match="No kernel with vmlinux"):
            _resolve_kernel(tmp_path, None)

    def test_auto_detect_ignores_dir_without_vmlinux(
        self, tmp_path: Path
    ) -> None:
        # Dir without vmlinux -- skipped
        (tmp_path / "kernels" / "bad").mkdir(parents=True)
        # Dir with vmlinux -- picked
        good = tmp_path / "kernels" / "good"
        good.mkdir(parents=True)
        (good / "vmlinux").touch()
        name, _ = _resolve_kernel(tmp_path, None)
        assert name == "good"


def _setup_artifacts(
    tmp_path: Path,
    kernel: str = "test-kernel",
    with_lustre: bool = False,
    missing: list[str] | None = None,
) -> Path:
    """Create a mock output directory with standard artifacts."""
    missing = missing or []

    kdir = tmp_path / "kernels" / kernel
    kdir.mkdir(parents=True)
    idir = tmp_path / "images" / kernel
    idir.mkdir(parents=True)
    cdir = tmp_path / "container"
    cdir.mkdir(parents=True)

    artifacts = {
        "vmlinux": kdir / "vmlinux",
        "vmlinuz": kdir / "vmlinuz",
        "build-tree": kdir / "build-tree",
        "modules": kdir / "modules",
        "image": idir / "base.ext4",
        "container": cdir / "image.tar",
    }

    for name, path in artifacts.items():
        if name in missing:
            continue
        if name in ("build-tree", "modules"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.touch()

    if with_lustre:
        (kdir / "lustre-artifacts").mkdir()

    return tmp_path


class TestFindArtifacts:
    def test_all_present(self, tmp_path: Path) -> None:
        out = _setup_artifacts(tmp_path)
        arts = _find_artifacts(out, kernel="test-kernel")
        assert "vmlinux" in arts
        assert "vmlinuz" in arts
        assert "build-tree" in arts
        assert "modules" in arts
        assert "image" in arts

    def test_with_lustre(self, tmp_path: Path) -> None:
        out = _setup_artifacts(tmp_path, with_lustre=True)
        arts = _find_artifacts(out, kernel="test-kernel")
        assert "lustre-artifacts" in arts

    def test_without_lustre(self, tmp_path: Path) -> None:
        out = _setup_artifacts(tmp_path)
        arts = _find_artifacts(out, kernel="test-kernel")
        assert "lustre-artifacts" not in arts

    def test_missing_vmlinux(self, tmp_path: Path) -> None:
        out = _setup_artifacts(tmp_path, missing=["vmlinux"])
        with pytest.raises(ValueError, match="Missing artifacts"):
            _find_artifacts(out, kernel="test-kernel")

    def test_missing_image(self, tmp_path: Path) -> None:
        out = _setup_artifacts(tmp_path, missing=["image"])
        with pytest.raises(ValueError, match="Missing artifacts"):
            _find_artifacts(out, kernel="test-kernel")

    def test_missing_multiple(self, tmp_path: Path) -> None:
        out = _setup_artifacts(tmp_path, missing=["vmlinux", "vmlinuz"])
        with pytest.raises(ValueError, match="vmlinux.*vmlinuz"):
            _find_artifacts(out, kernel="test-kernel")

    def test_auto_detect_kernel(self, tmp_path: Path) -> None:
        out = _setup_artifacts(tmp_path)
        arts = _find_artifacts(out)
        assert "vmlinux" in arts


# ---------------------------------------------------------------------------
# _dir_size_mb
# ---------------------------------------------------------------------------


class TestDirSizeMb:
    def test_success(self, tmp_path: Path) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "42\t/some/path\n"
        with patch("subprocess.run", return_value=mock_result):
            result = _dir_size_mb(tmp_path)
        assert result == 42.0

    def test_failure_returns_zero(self, tmp_path: Path) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            result = _dir_size_mb(tmp_path)
        assert result == 0.0


# ---------------------------------------------------------------------------
# export_build_container
# ---------------------------------------------------------------------------


class TestExportBuildContainer:
    def test_missing_image_raises(self, tmp_path: Path) -> None:
        """No build container in podman storage -> clean error pointing
        the user at `ltvm build container`."""
        check_result = MagicMock()
        check_result.returncode = 1  # `podman image exists` -> not found

        with patch("subprocess.run", return_value=check_result):
            with pytest.raises(RuntimeError, match="Run: ltvm build container"):
                export_build_container("my-target", tmp_path)

    def test_podman_missing_raises(self, tmp_path: Path) -> None:
        """podman binary not installed -> clean error."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="podman not found"):
                export_build_container("my-target", tmp_path)

    def test_save_failure_raises(self, tmp_path: Path) -> None:
        """podman save fails -> clean error with rc + stderr."""
        call_count = [0]

        def mock_run(cmd, *args, **kwargs):
            call_count[0] += 1
            r = MagicMock()
            if call_count[0] == 1:
                # `podman image exists` succeeds
                r.returncode = 0
            else:
                # `podman save` fails
                r.returncode = 7
                r.stderr = "no space left on device"
            return r

        with patch("subprocess.run", side_effect=mock_run):
            with pytest.raises(RuntimeError, match="podman save failed"):
                export_build_container("my-target", tmp_path)

    def test_success_writes_file_and_returns_path(self, tmp_path: Path) -> None:
        """Happy path: image.tar lands at output/<target>/container/."""

        def mock_run(cmd, *args, **kwargs):
            r = MagicMock()
            r.returncode = 0
            # If this is the save call, create the output file so
            # .stat().st_size works
            if "save" in cmd:
                out_idx = cmd.index("-o") + 1
                Path(cmd[out_idx]).write_bytes(b"x" * 1024)
            return r

        with patch("subprocess.run", side_effect=mock_run):
            path = export_build_container("my-target", tmp_path)

        assert path == tmp_path / "container" / "image.tar"
        assert path.exists()


# ---------------------------------------------------------------------------
# snapshot_lustre
# ---------------------------------------------------------------------------


def _setup_lustre_tree(tmp_path: Path) -> Path:
    """Create a minimal fake Lustre source tree (used only for metadata)."""
    tree = tmp_path / "lustre-tree"
    tree.mkdir()
    return tree


def _make_rsync_mock(dest_dir: Path) -> MagicMock:
    """Return a subprocess.run mock that creates dest_dir (simulating rsync).

    Also returns a fake commit hash for `git rev-parse HEAD` calls so that
    snapshot_lustre's git-commit capture step succeeds.
    """

    def _side_effect(cmd, *args, **kwargs):
        # rsync would create the destination; simulate that
        if cmd and cmd[0] == "rsync":
            dest_dir.mkdir(parents=True, exist_ok=True)
        result = MagicMock()
        result.returncode = 0
        if cmd and cmd[0] == "git" and "rev-parse" in cmd:
            result.stdout = "deadbeefcafef00dbaadf00dfeedfacedeadbeef\n"
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    return _side_effect


class TestSnapshotLustre:
    TARGET = "test-target"

    def _setup(self, tmp_path: Path, with_staging_ko: bool = True):
        """Create a fake target output_dir with kernel + per-tree staging.

        snapshot_lustre sources from <lustre_tree>/.ltvm-staging/<target>/,
        not from a global output dir.  The lustre_tree is both the source
        of staging content AND the metadata input for .ltvm-snapshot.json.
        """
        tree = _setup_lustre_tree(tmp_path)
        output_dir = tmp_path / "output"
        kdir = output_dir / "kernels" / "test-kernel"
        kdir.mkdir(parents=True)
        (kdir / "vmlinux").touch()
        staging = (
            tree / ".ltvm-staging" / self.TARGET / "x86_64" / "test-kernel"
        )
        if with_staging_ko:
            ko_dir = staging / "lib" / "modules" / "fake-kver" / "extra"
            ko_dir.mkdir(parents=True)
            (ko_dir / "lustre.ko").write_text("fake ko")
        else:
            staging.mkdir(parents=True)
        dest = kdir / "lustre-artifacts"
        return tree, output_dir, kdir, dest

    def test_missing_staging_raises(self, tmp_path: Path) -> None:
        tree = _setup_lustre_tree(tmp_path)
        output_dir = tmp_path / "output"
        kdir = output_dir / "kernels" / "test-kernel"
        kdir.mkdir(parents=True)
        (kdir / "vmlinux").touch()
        # No .ltvm-staging dir under tree -- snapshot_lustre should raise.
        with pytest.raises(ValueError, match="No staging directory"):
            snapshot_lustre(
                tree, output_dir, target=self.TARGET, kernel="test-kernel"
            )

    def test_empty_staging_raises(self, tmp_path: Path) -> None:
        tree, output_dir, kdir, dest = self._setup(
            tmp_path, with_staging_ko=False
        )
        with pytest.raises(ValueError, match="no .ko files"):
            snapshot_lustre(
                tree, output_dir, target=self.TARGET, kernel="test-kernel"
            )

    def test_with_staging_calls_rsync(self, tmp_path: Path) -> None:
        tree, output_dir, kdir, dest = self._setup(tmp_path)
        staging_src = (
            tree / ".ltvm-staging" / self.TARGET / "x86_64" / "test-kernel"
        )

        with (
            patch(
                "subprocess.run",
                side_effect=_make_rsync_mock(dest),
            ) as mock_run,
            patch("ltvm_pkg.release_package._dir_size_mb", return_value=10.0),
        ):
            result = snapshot_lustre(
                tree, output_dir, target=self.TARGET, kernel="test-kernel"
            )

        # rsync was called (plus possibly git rev-parse)
        rsync_calls = [
            c for c in mock_run.call_args_list if c[0][0][0] == "rsync"
        ]
        assert len(rsync_calls) == 1
        args = rsync_calls[0][0][0]
        assert str(staging_src) + "/" in args
        assert str(kdir / "lustre-artifacts") + "/" in args

        assert result == kdir / "lustre-artifacts"

    def test_snapshot_json_written(self, tmp_path: Path) -> None:
        tree, output_dir, kdir, dest = self._setup(tmp_path)

        with (
            patch(
                "subprocess.run",
                side_effect=_make_rsync_mock(dest),
            ),
            patch("ltvm_pkg.release_package._dir_size_mb", return_value=10.0),
        ):
            result = snapshot_lustre(
                tree, output_dir, target=self.TARGET, kernel="test-kernel"
            )

        meta_file = result / ".ltvm-snapshot.json"
        assert meta_file.exists()
        meta = json.loads(meta_file.read_text())
        assert meta["source"] == str(tree.resolve())
        assert meta["kernel"] == "test-kernel"
        assert meta["ko_count"] == 1


# ---------------------------------------------------------------------------
# package_target
# ---------------------------------------------------------------------------


def _setup_package_artifacts(
    tmp_path: Path,
    kernel: str = "test-kernel",
    with_lustre: bool = False,
    kernel_version: str | None = None,
) -> Path:
    """Create a minimal output dir ready for package_target."""
    output_dir = tmp_path / "output" / "my-target" / "x86_64"
    kdir = output_dir / "kernels" / kernel
    kdir.mkdir(parents=True)
    (kdir / "vmlinux").touch()
    (kdir / "vmlinuz").touch()
    (kdir / "build-tree").mkdir()
    (kdir / "modules").mkdir()
    idir = output_dir / "images" / kernel
    idir.mkdir(parents=True)
    (idir / "base.ext4").touch()
    cdir = output_dir / "container"
    cdir.mkdir()
    (cdir / "image.tar").touch()

    if kernel_version is not None:
        meta = {"kernel_version": kernel_version}
        (kdir / "meta.json").write_text(json.dumps(meta))

    if with_lustre:
        (kdir / "lustre-artifacts").mkdir()

    return output_dir


class TestPackageTarget:
    @pytest.fixture(autouse=True)
    def _stub_container_export(self):
        """Stub out the podman save call.

        package_target now exports the build container before tarballing,
        which would otherwise call out to real podman in unit tests.  The
        test fixtures already pre-create the container/image.tar file via
        _setup_package_artifacts so the subsequent _find_artifacts check
        passes; this stub just bypasses the podman save itself.
        """
        with patch("ltvm_pkg.release_package.export_build_container") as m:
            m.return_value = Path("/fake/container/image.tar")
            yield m

    def _touch_tarball(self, dest_dir: Path, pattern: str) -> None:
        """Create a fake tarball so stat().st_size works."""
        for p in dest_dir.iterdir():
            if pattern in p.name:
                p.write_bytes(b"fake tarball data" * 100)
                return
        # Create one matching the pattern
        (dest_dir / pattern).write_bytes(b"fake tarball data" * 100)

    def test_creates_tarball_gz(self, tmp_path: Path) -> None:
        output_dir = _setup_package_artifacts(tmp_path, kernel_version="5.14.0")
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        def mock_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            for arg in cmd:
                if arg.endswith(".tar.gz"):
                    Path(arg).write_bytes(b"x" * 1024)
            return result

        with patch("subprocess.run", side_effect=mock_run):
            tarball = package_target(
                "my-target",
                output_dir,
                kernel="test-kernel",
                dest_dir=dest_dir,
            )

        assert tarball.name.endswith(".tar.gz")
        assert "my-target" in tarball.name
        assert "5.14.0" in tarball.name

    def test_version_from_meta_json(self, tmp_path: Path) -> None:
        output_dir = _setup_package_artifacts(
            tmp_path, kernel_version="5.99.1-custom"
        )
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        def mock_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            for arg in cmd:
                if ".tar." in arg:
                    Path(arg).write_bytes(b"x" * 512)
            return result

        with patch("subprocess.run", side_effect=mock_run):
            tarball = package_target(
                "my-target",
                output_dir,
                kernel="test-kernel",
                dest_dir=dest_dir,
            )

        assert "5.99.1-custom" in tarball.name

    def test_raises_without_meta(self, tmp_path: Path) -> None:
        output_dir = _setup_package_artifacts(tmp_path)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        def mock_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            for arg in cmd:
                if ".tar." in arg:
                    Path(arg).write_bytes(b"x" * 512)
            return result

        with patch("subprocess.run", side_effect=mock_run):
            with pytest.raises(
                RuntimeError, match="meta.json missing or unreadable"
            ):
                package_target(
                    "my-target",
                    output_dir,
                    kernel="test-kernel",
                    dest_dir=dest_dir,
                )

    def test_manifest_written(self, tmp_path: Path) -> None:
        output_dir = _setup_package_artifacts(tmp_path, kernel_version="1.0")
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        def mock_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            for arg in cmd:
                if ".tar." in arg:
                    Path(arg).write_bytes(b"x" * 512)
            return result

        with patch("subprocess.run", side_effect=mock_run):
            tarball = package_target(
                "my-target",
                output_dir,
                kernel="test-kernel",
                dest_dir=dest_dir,
            )

        # Manifest sits alongside the tarball
        manifest_path = tarball.with_suffix(tarball.suffix + ".json")
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["target"] == "my-target"
        assert manifest["kernel"] == "test-kernel"
        assert manifest["kernel_version"] == "1.0"
        assert "vmlinux" in manifest["contents"]
        assert isinstance(manifest["has_lustre_artifacts"], bool)
        assert isinstance(manifest["size_bytes"], int)

    def test_dest_dir_none_lands_in_output_root(
        self, tmp_path: Path
    ) -> None:
        """dest_dir=None -> tarball placed at the OUTPUT_DIR root, two
        levels above the arch-qualified output_dir."""
        output_dir = _setup_package_artifacts(tmp_path, kernel_version="1.0")

        def mock_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            for arg in cmd:
                if ".tar." in arg:
                    Path(arg).write_bytes(b"x" * 512)
            return result

        with patch("subprocess.run", side_effect=mock_run):
            tarball = package_target(
                "my-target",
                output_dir,
                kernel="test-kernel",
                dest_dir=None,
            )

        assert tarball.parent == output_dir.parent.parent

    def test_manifest_has_lustre_artifacts_true(self, tmp_path: Path) -> None:
        output_dir = _setup_package_artifacts(
            tmp_path, kernel_version="1.0", with_lustre=True
        )
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        def mock_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            for arg in cmd:
                if ".tar." in arg:
                    Path(arg).write_bytes(b"x" * 512)
            return result

        with patch("subprocess.run", side_effect=mock_run):
            tarball = package_target(
                "my-target",
                output_dir,
                kernel="test-kernel",
                dest_dir=dest_dir,
            )

        manifest_path = tarball.with_suffix(tarball.suffix + ".json")
        manifest = json.loads(manifest_path.read_text())
        assert manifest["has_lustre_artifacts"] is True
        assert "lustre-artifacts" in manifest["contents"]


# ---------------------------------------------------------------------------
# Tarball layout: image must appear under <target>/images/<kernel>/
# ---------------------------------------------------------------------------


def _setup_per_kernel_image_artifacts(
    tmp_path: Path,
    kernel: str = "test-kernel",
    kernel_version: str = "5.14.0",
) -> Path:
    """Minimal output dir with the new per-kernel image layout."""
    output_dir = tmp_path / "output" / "my-target" / "x86_64"
    kdir = output_dir / "kernels" / kernel
    kdir.mkdir(parents=True)
    (kdir / "vmlinux").touch()
    (kdir / "vmlinuz").touch()
    (kdir / "build-tree").mkdir()
    (kdir / "modules").mkdir()
    (kdir / "meta.json").write_text(json.dumps({"kernel_version": kernel_version}))
    idir = output_dir / "images" / kernel
    idir.mkdir(parents=True)
    (idir / "base.ext4").touch()
    cdir = output_dir / "container"
    cdir.mkdir()
    (cdir / "image.tar").touch()
    return output_dir


class TestTarballLayout:
    """Verify the tar paths passed to the tar command reflect the new layout.

    We intercept subprocess.run and inspect the path arguments so the test
    stays hermetic (no real tar required) while still catching
    regressions in path construction.
    """

    @pytest.fixture(autouse=True)
    def _stub_container_export(self):
        with patch("ltvm_pkg.release_package.export_build_container") as m:
            m.return_value = Path("/fake/container/image.tar")
            yield m

    def test_image_under_images_kernel_subdir(self, tmp_path: Path) -> None:
        output_dir = _setup_per_kernel_image_artifacts(
            tmp_path, kernel="5.14-rhel9.5", kernel_version="5.14.0"
        )
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        captured_tar_args: list[list[str]] = []

        def mock_run(cmd, *args, **kwargs):
            result = MagicMock()
            if "tar" in cmd[0]:
                captured_tar_args.append(cmd)
                result.returncode = 0
                for arg in cmd:
                    if ".tar." in arg:
                        Path(arg).write_bytes(b"x" * 16)
            else:
                result.returncode = 0
            return result

        with patch("subprocess.run", side_effect=mock_run):
            package_target(
                "my-target",
                output_dir,
                kernel="5.14-rhel9.5",
                dest_dir=dest_dir,
            )

        assert captured_tar_args, "tar was never called"
        tar_cmd = captured_tar_args[0]
        # The path list follows -C <tar_base> in the command.
        c_idx = tar_cmd.index("-C")
        tar_paths = tar_cmd[c_idx + 2 :]
        image_path = next(
            (p for p in tar_paths if "image" in p), None
        )
        assert image_path is not None, f"no image path in tar args: {tar_paths}"
        assert "images" in image_path, (
            f"image should be under images/<kernel>/, got: {image_path}"
        )
        assert "5.14-rhel9.5" in image_path, (
            f"kernel name missing from image path: {image_path}"
        )
