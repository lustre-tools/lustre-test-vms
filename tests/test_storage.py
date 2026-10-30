"""Hot and cold artifact storage (ltvm_pkg/storage.py).

[storage] in /etc/ltvm.conf (LTVM_SITE_CONFIG here) names the hot
directory, which replaces <repo>/artifacts unless LTVM_ARTIFACTS_DIR is
set, and an optional cold one that `ltvm target archive`/`restore` move
<target>/<arch> trees to and from, leaving a symlink at the hot path.
"""

from __future__ import annotations

import argparse
import errno
import importlib
import os
import stat
import sys
from collections import namedtuple
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import storage
from ltvm_pkg.vm_state import VMInfo


def _site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    conf = tmp_path / "ltvm.conf"
    conf.write_text(text)
    monkeypatch.setenv("LTVM_SITE_CONFIG", str(conf))
    storage._warned.clear()
    return conf


@pytest.fixture
def tiers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """A hot artifacts dir with one target, and a configured cold dir."""
    hot = tmp_path / "hot"
    cold = tmp_path / "cold"
    cold.mkdir()
    _site(tmp_path, monkeypatch, f"[storage]\nhot = {hot}\ncold = {cold}\n")
    arch = hot / "rocky8" / "x86_64"
    kdir = arch / "kernels" / "4.18-k"
    kdir.mkdir(parents=True)
    (kdir / "vmlinuz").write_bytes(b"kernel" * 100)
    idir = arch / "images" / "4.18-k"
    idir.mkdir(parents=True)
    img = idir / "base-20260101T000000Z-abcd.ext4"
    # Sparse: a hole, then a little data at the end.
    with open(img, "wb") as f:
        f.seek(8 * 1024 * 1024)
        f.write(b"superblock")
    (idir / "current.ext4").symlink_to(img.name)
    os.utime(img, ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))
    monkeypatch.setattr("ltvm_pkg.target_config.ARTIFACTS_DIR", hot)
    with patch.object(storage, "_vm_running", return_value=False):
        yield {
            "hot": hot,
            "cold": cold,
            "arch": arch,
            "image": img,
            "kernel": kdir / "vmlinuz",
        }


def _tree(root: Path) -> dict[str, tuple]:
    """Relative path -> (type, size, mtime_ns or link target)."""
    out: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = p.lstat()
            rel = str(p.relative_to(root))
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir",)
            else:
                out[rel] = ("file", st.st_size, st.st_mtime_ns)
    return out


def _leftovers(*dirs: Path) -> list[str]:
    found = []
    for d in dirs:
        if d.is_dir():
            found += [p.name for p in d.iterdir() if ".ltvm-" in p.name]
    return found


# ── configuration ────────────────────────────────────────


class TestConfig:
    def _reload(self) -> Path:
        import ltvm_pkg.target_config as cfg

        importlib.reload(cfg)
        return cfg.ARTIFACTS_DIR

    @pytest.fixture(autouse=True)
    def _restore_module(self) -> Iterator[None]:
        yield
        import ltvm_pkg.target_config as cfg

        importlib.reload(cfg)

    def test_hot_replaces_the_repo_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LTVM_ARTIFACTS_DIR", raising=False)
        _site(tmp_path, monkeypatch, "[storage]\nhot = /srv/ltvm-hot\n")
        assert self._reload() == Path("/srv/ltvm-hot")
        assert storage.hot_source().startswith("[storage] hot in ")

    def test_env_wins_over_hot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _site(tmp_path, monkeypatch, "[storage]\nhot = /srv/ltvm-hot\n")
        monkeypatch.setenv("LTVM_ARTIFACTS_DIR", str(tmp_path / "mine"))
        assert self._reload() == tmp_path / "mine"
        assert storage.hot_source() == "LTVM_ARTIFACTS_DIR"

    def test_default_without_either(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ltvm_pkg.target_config as cfg

        monkeypatch.delenv("LTVM_ARTIFACTS_DIR", raising=False)
        _site(tmp_path, monkeypatch, "[memory]\novercommit = 2\n")
        assert self._reload() == cfg.REPO_ROOT / "artifacts"
        assert storage.hot_source() == "default"

    def test_relative_hot_warns_and_is_ignored(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import ltvm_pkg.target_config as cfg

        monkeypatch.delenv("LTVM_ARTIFACTS_DIR", raising=False)
        _site(tmp_path, monkeypatch, "[storage]\nhot = cache/ltvm\n")
        assert self._reload() == cfg.REPO_ROOT / "artifacts"
        err = capsys.readouterr().err
        assert "ignoring [storage] hot" in err
        assert "not an absolute path" in err

    def test_malformed_file_warns_not_traceback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _site(tmp_path, monkeypatch, "hot = /x\n")  # no section header
        assert storage.configured("hot") is None
        assert "ignoring [storage]" in capsys.readouterr().err

    def test_cold_unset_missing_and_relative(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _site(tmp_path, monkeypatch, "[storage]\nhot = /x\n")
        with pytest.raises(storage.StorageError, match=r"\[storage\] cold"):
            storage.cold_root()
        _site(tmp_path, monkeypatch, "[storage]\ncold = rel/dir\n")
        with pytest.raises(storage.StorageError, match="absolute"):
            storage.cold_root()
        _site(tmp_path, monkeypatch, f"[storage]\ncold = {tmp_path / 'no'}\n")
        with pytest.raises(storage.StorageError, match="mounted"):
            storage.cold_root()
        (tmp_path / "no").mkdir()
        assert storage.cold_root() == tmp_path / "no"


# ── group sharing ────────────────────────────────────────


class TestSharing:
    def test_private_dir_is_not_shared(self, tmp_path: Path) -> None:
        d = tmp_path / "d"
        d.mkdir(mode=0o755)
        assert storage.sharing(d) is None

    def test_setgid_dir_is_shared(self, tmp_path: Path) -> None:
        d = tmp_path / "d"
        d.mkdir()
        d.chmod(0o2775)
        sh = storage.sharing(d)
        assert sh is not None and sh.setgid and sh.group_writable
        assert "setgid" in sh.describe()

    def test_umask_opened_to_the_group_only_when_shared(
        self, tiers: dict
    ) -> None:
        old = os.umask(0o022)
        try:
            assert storage.share_new_files() is False
            assert os.umask(0o022) == 0o022
            tiers["hot"].chmod(0o2775)
            assert storage.share_new_files() is True
            assert os.umask(0o022) == 0o002
        finally:
            os.umask(old)

    def test_podman_args_for_rw_mounts_under_a_shared_root(
        self, tiers: dict, tmp_path: Path
    ) -> None:
        ccache = tiers["arch"] / "ccache"
        ccache.mkdir()
        rw = ["podman", "run", "-v", f"{ccache}:/ccache:Z", "img"]
        ro = ["podman", "run", "-v", f"{ccache}:/kernel:ro", "img"]
        other = ["podman", "run", "-v", f"{tmp_path}:/lustre", "img"]
        assert storage.podman_share_args(rw) == []  # hot not shared yet
        tiers["hot"].chmod(0o2775)
        assert storage.podman_share_args(rw) == [
            "--umask",
            "0002",
            "-e",
            "CCACHE_UMASK=002",
        ]
        assert storage.podman_share_args(ro) == []
        assert storage.podman_share_args(other) == []

    def test_podman_wrapper_injects_them(self, tiers: dict) -> None:
        from ltvm_pkg import podman_run

        tiers["hot"].chmod(0o2775)
        cmd = ["podman", "run", "-v", f"{tiers['arch']}:/out", "img"]
        with patch.object(podman_run.subprocess, "Popen") as popen:
            proc = popen.return_value
            proc.wait.return_value = 0
            proc.returncode = 0
            proc.stderr = None
            proc.poll.return_value = 0
            proc.communicate.return_value = (None, None)
            try:
                podman_run.run_podman_with_cleanup(cmd, stderr=None)
            except Exception:  # noqa: BLE001 -- only the argv matters
                pass
        argv = popen.call_args[0][0]
        assert "--umask" in argv and "CCACHE_UMASK=002" in argv

    @pytest.mark.skipif(
        not sys.platform.startswith("linux"),
        reason="BSD mkdir does not inherit a parent's setgid bit",
    )
    def test_copy_into_shared_cold_gives_group_the_owner_bits(
        self, tiers: dict
    ) -> None:
        tiers["cold"].chmod(0o2775)
        (tiers["arch"] / "kernels").chmod(0o755)
        tiers["kernel"].chmod(0o644)
        with patch.object(storage, "_same_fs", return_value=False):
            storage.move_target("rocky8", "x86_64", to_cold=True)
        moved = tiers["cold"] / "rocky8" / "x86_64"
        assert stat.S_IMODE((moved / "kernels").stat().st_mode) & 0o070 == 0o070
        assert (moved / "kernels").stat().st_mode & stat.S_ISGID
        kmode = stat.S_IMODE((moved / "kernels/4.18-k/vmlinuz").stat().st_mode)
        assert kmode & 0o060 == 0o060


# ── archive / restore ────────────────────────────────────


class TestCopyFile:
    @pytest.mark.parametrize("seek_extents", [True, False])
    def test_partial_writes_copy_every_byte(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        seek_extents: bool,
    ) -> None:
        src, dst = tmp_path / "src", tmp_path / "dst"
        # Multiple read buffers, with holes before and after the data.
        data = bytes(range(256)) * ((4 << 20) // 256 + 1)
        with src.open("wb") as f:
            f.seek(8 << 20)
            f.write(data)
            f.truncate(16 << 20)
        if not seek_extents:
            monkeypatch.delattr(storage.os, "SEEK_DATA", raising=False)
        real_pwrite = os.pwrite

        def partial(fd: int, buf: bytes, off: int) -> int:
            return real_pwrite(fd, buf[: max(1, len(buf) // 2)], off)

        with patch.object(storage.os, "pwrite", side_effect=partial):
            storage._copy_file(str(src), str(dst), src.stat().st_size)
        assert dst.read_bytes() == src.read_bytes()


class TestArchiveRestore:
    def test_archive_leaves_a_symlink_and_restore_removes_it(
        self, tiers: dict
    ) -> None:
        arch, cold = tiers["arch"], tiers["cold"] / "rocky8" / "x86_64"
        before = _tree(arch)
        moves = storage.move_target("rocky8", None, to_cold=True, say=print)
        assert [m.arch for m in moves] == ["x86_64"]
        assert arch.is_symlink() and Path(os.readlink(arch)) == cold
        assert _tree(cold) == before
        # Every path computed through the hot dir still works.
        assert tiers["kernel"].read_bytes() == b"kernel" * 100
        assert storage.archived(arch) == cold

        storage.move_target("rocky8", None, to_cold=False)
        assert arch.is_dir() and not arch.is_symlink()
        assert _tree(arch) == before
        assert not (tiers["cold"] / "rocky8").exists()

    def test_cross_filesystem_copy_is_checked_and_sparse(
        self, tiers: dict
    ) -> None:
        arch, cold = tiers["arch"], tiers["cold"] / "rocky8" / "x86_64"
        before = _tree(arch)
        src_blocks = tiers["image"].stat().st_blocks
        with patch.object(storage, "_same_fs", return_value=False):
            storage.move_target("rocky8", "x86_64", to_cold=True)
            assert arch.is_symlink()
            assert _tree(cold) == before  # sizes and mtime_ns kept
            copy = cold / "images/4.18-k" / tiers["image"].name
            assert copy.stat().st_blocks <= max(src_blocks, 64)
            assert _leftovers(tiers["cold"] / "rocky8", arch.parent) == []

            storage.move_target("rocky8", "x86_64", to_cold=False)
        assert not arch.is_symlink() and _tree(arch) == before
        assert not cold.exists()
        assert _leftovers(arch.parent) == []

    def test_enospc_mid_copy_leaves_the_source_untouched(
        self, tiers: dict
    ) -> None:
        arch = tiers["arch"]
        before = _tree(arch)
        real_copy = storage._copy_file
        calls = {"n": 0}

        def flaky(src: str, dst: str, size: int) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(errno.ENOSPC, "No space left on device", dst)
            real_copy(src, dst, size)

        with (
            patch.object(storage, "_same_fs", return_value=False),
            patch.object(storage, "_copy_file", side_effect=flaky),
            pytest.raises(storage.StorageError, match="No space left"),
        ):
            storage.move_target("rocky8", "x86_64", to_cold=True)
        assert not arch.is_symlink() and _tree(arch) == before
        assert not (tiers["cold"] / "rocky8" / "x86_64").exists()
        assert _leftovers(tiers["cold"] / "rocky8", arch.parent) == []

    @pytest.mark.parametrize("failure", ["zero", "enospc", "eio", "eof"])
    def test_incomplete_io_leaves_the_source_untouched(
        self, tiers: dict, failure: str
    ) -> None:
        arch = tiers["arch"]
        cold = tiers["cold"] / "rocky8" / "x86_64"
        before = _tree(arch)
        inode = arch.stat().st_ino
        contents = {
            p.relative_to(arch): p.read_bytes()
            for p in arch.rglob("*")
            if p.is_file() and not p.is_symlink()
        }
        real_pwrite, real_pread = os.pwrite, os.pread
        writes = 0
        reads = 0

        def fail_write(fd: int, buf: bytes, off: int) -> int:
            nonlocal writes
            writes += 1
            if writes == 1:
                return real_pwrite(fd, buf[: max(1, len(buf) // 2)], off)
            if failure == "zero":
                return 0
            code = errno.ENOSPC if failure == "enospc" else errno.EIO
            raise OSError(code, "injected write failure")

        def fail_read(fd: int, size: int, off: int) -> bytes:
            nonlocal reads
            reads += 1
            return real_pread(fd, max(1, size // 2), off) if reads == 1 else b""

        operation = "pread" if failure == "eof" else "pwrite"
        effect = fail_read if failure == "eof" else fail_write
        with (
            patch.object(storage, "_same_fs", return_value=False),
            patch.object(storage.os, operation, side_effect=effect),
            patch.object(storage, "_remove_tree") as remove,
            pytest.raises(storage.StorageError, match="copying .* failed"),
        ):
            storage.move_target("rocky8", "x86_64", to_cold=True)
        assert not arch.is_symlink() and arch.stat().st_ino == inode
        assert _tree(arch) == before
        for rel, data in contents.items():
            assert (arch / rel).read_bytes() == data
        remove.assert_not_called()
        assert not cold.exists()
        assert _leftovers(cold.parent, arch.parent) == []

    def test_failed_verify_cleans_up(self, tiers: dict) -> None:
        arch = tiers["arch"]
        before = _tree(arch)
        with (
            patch.object(storage, "_same_fs", return_value=False),
            patch.object(
                storage,
                "_verify_copy",
                side_effect=storage.StorageError("copy differs"),
            ),
            pytest.raises(storage.StorageError, match="copy differs"),
        ):
            storage.move_target("rocky8", "x86_64", to_cold=True)
        assert _tree(arch) == before
        assert _leftovers(tiers["cold"] / "rocky8") == []

    def test_verify_catches_a_changed_mtime(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        (a / "f").write_text("x")
        storage._copy_tree(a, b, share=False)
        storage._verify_copy(a, b)
        os.utime(b / "f", ns=(0, 12345))
        with pytest.raises(storage.StorageError, match="size:mtime"):
            storage._verify_copy(a, b)

    def test_failed_switch_restores_the_source(self, tiers: dict) -> None:
        arch = tiers["arch"]
        before = _tree(arch)
        real_symlink = os.symlink

        def no_link(src: object, dst: object, *a: object, **k: object) -> None:
            if str(dst).endswith("-link"):
                raise OSError(errno.EPERM, "denied", str(dst))
            real_symlink(src, dst, *a, **k)  # type: ignore[arg-type]

        with (
            patch.object(storage, "_same_fs", return_value=False),
            patch.object(storage.os, "symlink", side_effect=no_link),
            pytest.raises(storage.StorageError, match="cannot link"),
        ):
            storage.move_target("rocky8", "x86_64", to_cold=True)
        assert not arch.is_symlink() and _tree(arch) == before
        assert not (tiers["cold"] / "rocky8" / "x86_64").exists()

    def test_unremovable_old_copy_is_a_warning_not_a_failure(
        self, tmp_path: Path
    ) -> None:
        src, dest = tmp_path / "src", tmp_path / "other" / "dest"
        (src / "d").mkdir(parents=True)
        (src / "d" / "f").write_text("x")

        def refuse(p: Path) -> None:
            raise OSError(errno.EACCES, "Permission denied", str(p))

        with patch.object(storage, "_same_fs", return_value=False):
            warning = storage.relocate(src, dest, link_back=True, remove=refuse)
        assert warning and "remove it by hand" in warning
        assert src.is_symlink() and (dest / "d" / "f").read_text() == "x"

    def test_not_enough_space_moves_nothing(self, tiers: dict) -> None:
        tiny = namedtuple("Usage", "total used free")(10, 9, 1)
        with (
            patch.object(storage, "_same_fs", return_value=False),
            patch.object(storage.shutil, "disk_usage", return_value=tiny),
            pytest.raises(storage.StorageError, match="not enough space"),
        ):
            storage.move_target("rocky8", "x86_64", to_cold=True)
        assert not tiers["arch"].is_symlink()

    def test_unwritable_source_dir_is_refused_up_front(
        self, tiers: dict
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("root can write anything")
        kdir = tiers["arch"] / "kernels" / "4.18-k"
        kdir.chmod(0o555)
        try:
            with pytest.raises(storage.StorageError, match="not writable"):
                storage.move_target("rocky8", "x86_64", to_cold=True)
        finally:
            kdir.chmod(0o755)
        assert not tiers["arch"].is_symlink()

    def test_archive_twice_and_restore_nothing_say_so(
        self, tiers: dict
    ) -> None:
        with pytest.raises(storage.StorageError, match="nothing to restore"):
            storage.move_target("rocky8", None, to_cold=False)
        storage.move_target("rocky8", None, to_cold=True)
        with pytest.raises(storage.StorageError, match="already archived"):
            storage.move_target("rocky8", "x86_64", to_cold=True)
        with pytest.raises(storage.StorageError, match="not a target"):
            storage.move_target("../etc", None, to_cold=True)

    def test_no_cold_configured(
        self, tiers: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _site(tmp_path, monkeypatch, f"[storage]\nhot = {tiers['hot']}\n")
        with pytest.raises(storage.StorageError, match="no cold storage"):
            storage.move_target("rocky8", None, to_cold=True)


# ── VMs using the artifacts ──────────────────────────────


def _qcow2_naming(path: Path, backing: str) -> None:
    """A qcow2 header that names *backing*, as qemu-img create -b does."""
    name = backing.encode()
    hdr = bytearray(512)
    hdr[0:4] = b"QFI\xfb"
    hdr[4:8] = (3).to_bytes(4, "big")
    hdr[8:16] = (512 - len(name)).to_bytes(8, "big")
    hdr[16:20] = len(name).to_bytes(4, "big")
    hdr[512 - len(name) :] = name
    path.write_bytes(bytes(hdr))


class TestVMReferences:
    def _vm(self, name: str, image: Path, kernel: Path) -> VMInfo:
        from ltvm_pkg import vm_state

        vm = VMInfo(name=name, ip="10.0.0.9", image=str(image))
        vm.kernel = str(kernel)
        vm.save()
        vm_state.OVERLAYS.mkdir(parents=True, exist_ok=True)
        _qcow2_naming(vm.overlay_path, str(image))
        return vm

    def test_archive_refused_while_a_vm_uses_the_target(
        self, tiers: dict
    ) -> None:
        self._vm("co1-old", tiers["image"], tiers["kernel"])
        with pytest.raises(storage.StorageError) as e:
            storage.move_target("rocky8", None, to_cold=True)
        assert "co1-old" in str(e.value) and "--force" in str(e.value)
        assert not tiers["arch"].is_symlink()

    def test_force_archives_and_the_vm_paths_still_resolve(
        self, tiers: dict
    ) -> None:
        self._vm("co1-old", tiers["image"], tiers["kernel"])
        storage.move_target("rocky8", None, to_cold=True, force=True)
        vm = VMInfo.load("co1-old")
        assert vm.image == str(tiers["image"])  # untouched
        assert Path(vm.image).is_file() and Path(vm.kernel).is_file()
        from ltvm_pkg.image_store import check_backing

        assert check_backing(vm) is None

    def test_a_running_vm_blocks_even_with_force(self, tiers: dict) -> None:
        self._vm("co1-live", tiers["image"], tiers["kernel"])
        with (
            patch.object(storage, "_vm_running", return_value=True),
            pytest.raises(storage.StorageError, match="running"),
        ):
            storage.move_target("rocky8", None, to_cold=True, force=True)
        assert not tiers["arch"].is_symlink()

    def test_orphan_overlay_counts_too(self, tiers: dict) -> None:
        from ltvm_pkg import vm_state

        vm_state.OVERLAYS.mkdir(parents=True, exist_ok=True)
        _qcow2_naming(vm_state.OVERLAYS / "gone.qcow2", str(tiers["image"]))
        with pytest.raises(storage.StorageError, match="gone"):
            storage.move_target("rocky8", None, to_cold=True)

    def test_restore_force_repoints_a_vm_that_names_the_cold_path(
        self, tiers: dict
    ) -> None:
        storage.move_target("rocky8", None, to_cold=True)
        cold_arch = tiers["cold"] / "rocky8" / "x86_64"
        cold_img = cold_arch / tiers["image"].relative_to(tiers["arch"])
        vm = self._vm("co1-cold", cold_img, tiers["kernel"])
        with pytest.raises(storage.StorageError, match="co1-cold"):
            storage.move_target("rocky8", None, to_cold=False)

        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(storage.subprocess, "run", return_value=ok) as run:
            moves = storage.move_target(
                "rocky8", None, to_cold=False, force=True
            )
        assert moves[0].warning is None
        assert VMInfo.load("co1-cold").image == str(tiers["image"])
        argv = run.call_args[0][0]
        assert argv[1:6] == ["rebase", "-u", "-b", str(tiers["image"]), "-F"]
        assert argv[-1] == str(vm.overlay_path)

    def test_cli_refusal_exits_nonzero_naming_the_vm(
        self, tiers: dict, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from ltvm_pkg.cli import EXIT_ERROR, cmd_target_archive

        self._vm("co1-old", tiers["image"], tiers["kernel"])
        args = argparse.Namespace(
            target="rocky8", arch=None, force=False, json=False
        )
        assert cmd_target_archive(args) == EXIT_ERROR
        assert "co1-old" in capsys.readouterr().err

    def test_cli_json(
        self, tiers: dict, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        from ltvm_pkg.cli import EXIT_OK, cmd_target_archive

        args = argparse.Namespace(
            target="rocky8", arch="x86_64", force=False, json=True
        )
        assert cmd_target_archive(args) == EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["action"] == "archive"
        assert out["moved"][0]["to"] == str(tiers["cold"] / "rocky8" / "x86_64")


# ── the rest of ltvm, on an archived target ──────────────


class TestArchivedTargetElsewhere:
    def test_fetch_refuses_an_archived_target(
        self, tiers: dict, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from ltvm_pkg.cli.fetch import cmd_fetch

        storage.move_target("rocky8", None, to_cold=True)
        args = argparse.Namespace(
            json=False,
            target="rocky8",
            arch="x86_64",
            kernel=None,
            variant=None,
            image=False,
            dry_run=False,
            list=False,
        )
        with patch("ltvm_pkg.cli.fetch._load_target") as load:
            load.return_value = (MagicMock(default_kernel="4.18-k"), None)
            assert cmd_fetch(args) != 0
        err = capsys.readouterr().err
        assert "archived in cold storage" in err
        assert "ltvm target restore rocky8" in err

    def test_target_clean_removes_the_cold_copy_and_the_link(
        self, tiers: dict, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from ltvm_pkg.cli.build import cmd_clean

        storage.move_target("rocky8", None, to_cold=True)
        tc = MagicMock(arch="x86_64")
        with (
            patch("ltvm_pkg.cli.build._load_target", return_value=(tc, None)),
            patch("ltvm_pkg.cli.build.resolve_arch", return_value="x86_64"),
        ):
            rc = cmd_clean(
                argparse.Namespace(
                    target="rocky8", arch=None, all_arches=True, json=False
                )
            )
        assert rc == 0
        assert not (tiers["cold"] / "rocky8" / "x86_64").exists()
        assert not (tiers["hot"] / "rocky8").exists()

    def test_build_status_marks_the_target_cold(
        self, tiers: dict, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        from ltvm_pkg import cli as cli_mod

        storage.move_target("rocky8", None, to_cold=True, say=lambda _: None)
        tc = MagicMock(output_dir=tiers["arch"])
        tc.available_kernels.return_value = []
        tc.resolve_kernel.return_value = "4.18-k"
        tc.declared_variants.return_value = []
        tc.image_output_dir.return_value = tiers["arch"] / "nope"
        with (
            patch("ltvm_pkg.cli.list_targets", return_value=["rocky8"]),
            patch("ltvm_pkg.cli.TargetConfig", return_value=tc),
            patch(
                "ltvm_pkg.cli.build._container_status",
                return_value={"built": True, "stale": False},
            ),
            patch(
                "ltvm_pkg.cli.build.kernel_status",
                return_value={"built": True, "stale": False},
            ),
            patch(
                "ltvm_pkg.cli.build.image_status",
                return_value={"built": True, "stale": False, "kernel": "k"},
            ),
        ):
            cli_mod.cmd_status(argparse.Namespace(json=True, arch="x86_64"))
            data = json.loads(capsys.readouterr().out)
            cli_mod.cmd_status(argparse.Namespace(json=False, arch="x86_64"))
            text = capsys.readouterr().out
        assert data["rocky8"]["storage"] == "cold"
        assert data["rocky8"]["cold_path"] == str(
            tiers["cold"] / "rocky8" / "x86_64"
        )
        assert "Cold (archived" in text and "rocky8" in text


# ── doctor ───────────────────────────────────────────────


class TestDoctorReport:
    def test_reports_both_dirs(self, tiers: dict) -> None:
        tiers["cold"].chmod(0o2775)
        storage.move_target("rocky8", None, to_cold=True)
        issues, notes = storage.doctor_report()
        assert issues == []
        hot = next(n for n in notes if n.startswith("storage hot:"))
        assert str(tiers["hot"]) in hot and "[storage] hot in" in hot
        assert "writable by you" in hot and "free" in hot
        assert "not group-shared" in hot
        cold = next(n for n in notes if n.startswith("storage cold:"))
        assert "group-shared" in cold and "setgid" in cold
        assert "1 arch dir(s) archived" in cold

    def test_unreadable_dir_in_hot_is_skipped(self, tiers: dict) -> None:
        """hot on a filesystem of its own has a lost+found only root
        can read."""
        if os.geteuid() == 0:
            pytest.skip("root can read anything")
        tiers["cold"].chmod(0o2775)
        storage.move_target("rocky8", None, to_cold=True)
        lost = tiers["hot"] / "lost+found"
        lost.mkdir(mode=0o000)
        try:
            issues, notes = storage.doctor_report()
        finally:
            lost.chmod(0o755)
        assert issues == []
        cold = next(n for n in notes if n.startswith("storage cold:"))
        assert "1 arch dir(s) archived" in cold

    def test_missing_cold_is_an_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hot = tmp_path / "hot"
        hot.mkdir()
        monkeypatch.setattr("ltvm_pkg.target_config.ARTIFACTS_DIR", hot)
        _site(
            tmp_path,
            monkeypatch,
            f"[storage]\nhot = {hot}\ncold = {tmp_path / 'hdd'}\n",
        )
        issues, _notes = storage.doctor_report()
        assert any("mounted" in i for i in issues)

    def test_missing_configured_hot_and_bad_cold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hot = tmp_path / "ssd"
        monkeypatch.setattr("ltvm_pkg.target_config.ARTIFACTS_DIR", hot)
        monkeypatch.delenv("LTVM_ARTIFACTS_DIR", raising=False)
        _site(tmp_path, monkeypatch, f"[storage]\nhot = {hot}\ncold = x\n")
        issues, _notes = storage.doctor_report()
        assert any("does not exist" in i and str(hot) in i for i in issues)
        assert any("not an absolute path" in i for i in issues)

    def test_unconfigured_default_is_quiet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "ltvm_pkg.target_config.ARTIFACTS_DIR", tmp_path / "artifacts"
        )
        monkeypatch.delenv("LTVM_ARTIFACTS_DIR", raising=False)
        issues, notes = storage.doctor_report()
        assert issues == []
        assert any("not created yet" in n for n in notes)
        assert any("storage cold: not configured" in n for n in notes)


class TestWhichCommandsShare:
    """Only the commands that write artifacts open the umask: a VM
    overlay created with a group-writable mode would let any group
    member write another user's VM disk."""

    def test_writers(self) -> None:
        from tests.test_build_commands import ltvm

        def ns(command: str, **kw: str) -> argparse.Namespace:
            return argparse.Namespace(command=command, **kw)

        assert ltvm._writes_artifacts(ns("build", build_action="kernel"))
        assert ltvm._writes_artifacts(ns("target", target_action="fetch"))
        assert ltvm._writes_artifacts(ns("target", target_action="archive"))
        assert ltvm._writes_artifacts(ns("clean"))
        assert not ltvm._writes_artifacts(ns("build", build_action="lustre"))
        assert not ltvm._writes_artifacts(ns("target", target_action="list"))
        assert not ltvm._writes_artifacts(ns("create"))
