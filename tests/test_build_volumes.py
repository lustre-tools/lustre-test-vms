"""macOS build volumes: what is mounted, and when it falls back.

Nothing here runs podman: build_volumes._podman / _probe / _populate
are replaced, and conftest turns the volumes off for every other test.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import build_volumes
from ltvm_pkg.lustre_build import build_lustre


def _cp(rc: int = 0, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, out, "")


def _ls(*vols: tuple[str, dict[str, str]]) -> str:
    """`podman volume ls --format json` output for *vols*."""
    return json.dumps([{"Name": n, "Labels": labels} for n, labels in vols])


def _kvol(name: str, tree: Path | str | None) -> tuple[str, dict[str, str]]:
    labels = {build_volumes.LABEL_KIND: "kernel"}
    if tree is not None:
        labels[build_volumes.LABEL_TREE] = str(Path(tree).resolve())
    return name, labels


def _fake_podman(
    listing: str, rm_rc: int = 0, calls: list | None = None
) -> Any:
    def podman(*args: str, timeout: float | None = None) -> Any:
        if calls is not None:
            calls.append(args)
        if args[:2] == ("volume", "ls"):
            return _cp(out=listing)
        if args[:2] == ("volume", "rm"):
            return _cp(rc=rm_rc)
        return _cp()

    return podman


def _kernel_artifact(
    tmp_path: Path, kver: str = "5.14-rhel9.7-5.14.0-611"
) -> Path:
    kdir = tmp_path / "artifacts" / "rocky9" / "aarch64" / "kernels" / kver
    bt = kdir / "build-tree"
    (bt / "include" / "config").mkdir(parents=True)
    (bt / "include" / "config" / "kernel.release").write_text("5.14.0-611\n")
    (bt / "Module.symvers").write_text("")
    (kdir / "meta.json").write_text('{"input_hash": "aaaa"}')
    return bt


@pytest.fixture
def on_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_volumes, "is_macos", lambda: True)
    monkeypatch.delenv("LTVM_MACOS_BUILD_VOLUMES", raising=False)
    # The real lock is a file in the system temp dir, one per volume
    # name -- and every test's tmp_path makes a new name.
    monkeypatch.setattr(
        build_volumes, "_lock", lambda name: contextlib.nullcontext()
    )


class TestEnabled:
    def test_off_on_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(build_volumes, "is_macos", lambda: False)
        monkeypatch.delenv("LTVM_MACOS_BUILD_VOLUMES", raising=False)
        assert not build_volumes.enabled()

    def test_on_on_macos(self, on_mac: None) -> None:
        assert build_volumes.enabled()

    def test_env_turns_it_off(
        self, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_MACOS_BUILD_VOLUMES", "0")
        assert not build_volumes.enabled()

    def test_linux_mounts_are_the_bind_mounts_and_run_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_volumes, "is_macos", lambda: False)
        monkeypatch.delenv("LTVM_MACOS_BUILD_VOLUMES", raising=False)
        bt = _kernel_artifact(tmp_path)
        podman = MagicMock()
        monkeypatch.setattr(build_volumes, "_podman", podman)
        assert build_volumes.kernel_tree_mount(bt, "img") == str(bt)
        assert build_volumes.ccache_mount(
            "ltvm-build-x", tmp_path / "cc"
        ) == str(tmp_path / "cc")
        podman.assert_not_called()


class TestKernelVolumeName:
    def test_names_the_kernel_and_is_stable(self, tmp_path: Path) -> None:
        bt = _kernel_artifact(tmp_path)
        name = build_volumes.kernel_volume_name(bt)
        assert name.startswith(
            "ltvm-kernel-rocky9-aarch64-5.14-rhel9.7-5.14.0-611-"
        )
        assert name == build_volumes.kernel_volume_name(bt)

    def test_a_rebuilt_kernel_gets_another_volume(self, tmp_path: Path) -> None:
        bt = _kernel_artifact(tmp_path)
        before = build_volumes.kernel_volume_name(bt)
        (bt.parent / "meta.json").write_text('{"input_hash": "bbbb"}')
        assert build_volumes.kernel_volume_name(bt) != before

    def test_a_changed_symvers_gets_another_volume(
        self, tmp_path: Path
    ) -> None:
        bt = _kernel_artifact(tmp_path)
        before = build_volumes.kernel_volume_name(bt)
        (bt / "Module.symvers").write_text("0x1\tsym\tvmlinux\tEXPORT_SYMBOL\n")
        assert build_volumes.kernel_volume_name(bt) != before

    def test_a_changed_config_gets_another_volume(self, tmp_path: Path) -> None:
        bt = _kernel_artifact(tmp_path)
        before = build_volumes.kernel_volume_name(bt)
        (bt / ".config").write_text("CONFIG_FS_VERITY=y\n")
        assert build_volumes.kernel_volume_name(bt) != before

    def test_the_vmlinux_link_gets_another_volume(self, tmp_path: Path) -> None:
        bt = _kernel_artifact(tmp_path)
        before = build_volumes.kernel_volume_name(bt)
        (bt / "vmlinux").symlink_to("/vmlinux")
        assert build_volumes.kernel_volume_name(bt) != before

    def test_only_valid_volume_characters(self, tmp_path: Path) -> None:
        bt = _kernel_artifact(tmp_path, kver="5.14+odd name")
        name = build_volumes.kernel_volume_name(bt)
        assert all(c.isalnum() or c in "_.-" for c in name)


class TestKernelTreeMount:
    def test_ready_volume_is_used_without_copying(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bt = _kernel_artifact(tmp_path)
        monkeypatch.setattr(build_volumes, "_ready", lambda v, i: True)
        populate = MagicMock()
        monkeypatch.setattr(build_volumes, "_populate", populate)
        vol = build_volumes.kernel_tree_mount(bt, "img")
        assert vol == build_volumes.kernel_volume_name(bt)
        populate.assert_not_called()

    def test_missing_volume_is_copied_and_older_ones_removed(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only volumes copied from this same build-tree are superseded."""
        bt = _kernel_artifact(tmp_path)
        other_bt = _kernel_artifact(tmp_path, kver="5.14-rhel9.6-5.14.0-570")
        name = build_volumes.kernel_volume_name(bt)
        calls: list[tuple[str, ...]] = []
        listing = _ls(
            _kvol("ltvm-kernel-old-copy", bt),
            _kvol(name, bt),
            _kvol("ltvm-kernel-another-kernel", other_bt),
            _kvol("ltvm-kernel-another-checkout", tmp_path / "elsewhere"),
        )
        monkeypatch.setattr(
            build_volumes, "_podman", _fake_podman(listing, calls=calls)
        )
        monkeypatch.setattr(build_volumes, "_ready", lambda v, i: False)
        populate = MagicMock()
        monkeypatch.setattr(build_volumes, "_populate", populate)
        assert build_volumes.kernel_tree_mount(bt, "img") == name
        populate.assert_called_once_with(name, bt, "img")
        removed = [a[-1] for a in calls if a[:2] == ("volume", "rm")]
        assert removed == ["ltvm-kernel-old-copy"]

    def test_a_failed_copy_falls_back_to_the_bind_mount(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bt = _kernel_artifact(tmp_path)
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            build_volumes,
            "_podman",
            lambda *a, timeout=None: calls.append(a) or _cp(),
        )
        monkeypatch.setattr(build_volumes, "_ready", lambda v, i: False)

        def boom(*a: Any) -> None:
            raise RuntimeError("import failed")

        monkeypatch.setattr(build_volumes, "_populate", boom)
        assert build_volumes.kernel_tree_mount(bt, "img") == str(bt)
        # The partial volume is not left behind.
        assert (
            "volume",
            "rm",
            build_volumes.kernel_volume_name(bt),
        ) in calls


class TestReady:
    def test_needs_the_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            build_volumes, "_podman", lambda *a, timeout=None: _cp()
        )
        monkeypatch.setattr(build_volumes, "_probe", lambda i, m, s: "\n")
        assert not build_volumes._ready("v", "img")
        monkeypatch.setattr(build_volumes, "_probe", lambda i, m, s: "ready\n")
        assert build_volumes._ready("v", "img")

    def test_a_missing_volume_is_not_ready(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            build_volumes, "_podman", lambda *a, timeout=None: _cp(rc=1)
        )
        assert not build_volumes._ready("v", "img")


class TestCcacheMount:
    def test_writable_volume_is_used(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            build_volumes, "_podman", lambda *a, timeout=None: _cp()
        )
        monkeypatch.setattr(build_volumes, "_probe", lambda i, m, s: "ok\n")
        assert (
            build_volumes.ccache_mount("ltvm-build-rocky9-aarch64", tmp_path)
            == "ltvm-build-ccache-rocky9-aarch64"
        )

    def test_unwritable_volume_falls_back(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What podman machine 5.x did, and why 0e4c1c9 left volumes."""
        monkeypatch.setattr(
            build_volumes, "_podman", lambda *a, timeout=None: _cp()
        )
        monkeypatch.setattr(build_volumes, "_probe", lambda i, m, s: None)
        assert build_volumes.ccache_mount("ltvm-build-x", tmp_path) == str(
            tmp_path
        )

    def test_does_not_reuse_the_old_ccache_volume_name(self) -> None:
        assert not build_volumes.ccache_volume_name("ltvm-build-x").startswith(
            "ltvm-ccache-"
        )


class TestBuildLustreMounts:
    """The mounts build_lustre hands podman, with and without volumes."""

    def _tree(self, tmp_path: Path) -> Path:
        lustre = tmp_path / "lustre"
        (lustre / "lustre" / "kernel_patches").mkdir(parents=True)
        (lustre / "configure").write_text("#!/bin/sh\n")
        return lustre

    def _mounts(self, tmp_path: Path) -> tuple[list[str], Path]:
        lustre = self._tree(tmp_path)
        bt = _kernel_artifact(tmp_path)
        captured: list[list[str]] = []

        def run(cmd: list[str], *a: Any, **kw: Any) -> Any:
            if cmd[0] == "podman":
                captured.append(cmd)
            r = MagicMock()
            r.returncode = 0
            return r

        with (
            patch("ltvm_pkg.lustre_build.subprocess.run", side_effect=run),
            patch(
                "ltvm_pkg.lustre_build.run_podman_with_cleanup", side_effect=run
            ),
            patch("ltvm_pkg.lustre_build._container_exists", return_value=True),
        ):
            try:
                build_lustre(
                    lustre,
                    bt,
                    container_tag="ltvm-build-rocky9",
                    target="rocky9",
                )
            except Exception:
                pass
        assert captured, "podman run was not called"
        self.cmd = captured[0]
        return [
            self.cmd[i + 1] for i, a in enumerate(self.cmd) if a == "-v"
        ], bt

    def test_bind_mounts_when_volumes_are_off(self, tmp_path: Path) -> None:
        mounts, bt = self._mounts(tmp_path)
        assert f"{bt}:/kernel:ro" in mounts
        ccache = [m for m in mounts if m.endswith(":/ccache")]
        assert ccache and ccache[0].startswith(str(tmp_path))
        assert not any(a.startswith("CCACHE_MAXSIZE") for a in self.cmd)

    def test_volumes_on_macos(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_volumes, "_ready", lambda v, i: True)
        monkeypatch.setattr(
            build_volumes, "_podman", lambda *a, timeout=None: _cp()
        )
        monkeypatch.setattr(build_volumes, "_probe", lambda i, m, s: "ok\n")
        mounts, bt = self._mounts(tmp_path)
        assert f"{build_volumes.kernel_volume_name(bt)}:/kernel:ro" in mounts
        assert "ltvm-build-ccache-rocky9:/ccache" in mounts
        # The volume's ccache is ltvm's to cap; the host dir's is not.
        assert "CCACHE_MAXSIZE=5G" in self.cmd
        # The tree itself stays a bind mount: it is edited on macOS.
        assert f"{tmp_path / 'lustre'}:/lustre" in mounts


def test_no_volume_is_ever_force_removed(
    tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`podman volume rm -f` also removes the containers using it, which
    would kill a build still running against an older kernel."""
    bt = _kernel_artifact(tmp_path)
    name = build_volumes.kernel_volume_name(bt)
    calls: list[tuple[str, ...]] = []
    listing = _ls(_kvol("ltvm-kernel-old-copy", bt), _kvol(name, bt))
    monkeypatch.setattr(
        build_volumes, "_podman", _fake_podman(listing, rm_rc=2, calls=calls)
    )
    build_volumes._remove_superseded(name, bt)  # in use: kept, no error
    monkeypatch.setattr(build_volumes, "_ready", lambda v, i: False)

    def boom(*a: Any) -> None:
        raise RuntimeError("import failed")

    monkeypatch.setattr(build_volumes, "_populate", boom)
    build_volumes.kernel_tree_mount(bt, "img")
    assert build_volumes.remove_volume("x") is False  # in use
    rms = [a for a in calls if a[:2] == ("volume", "rm")]
    assert len(rms) >= 3 and not any("-f" in a or "--force" in a for a in rms)


class TestListing:
    def test_only_ltvm_volumes_with_their_labels(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        listing = _ls(
            _kvol("ltvm-kernel-a", tmp_path),
            (
                "ltvm-build-ccache-rocky9-aarch64",
                {build_volumes.LABEL_KIND: "ccache"},
            ),
            ("someone-elses", {}),
        )
        monkeypatch.setattr(build_volumes, "_podman", _fake_podman(listing))
        vols = build_volumes.list_volumes()
        assert vols is not None
        assert [(v.name, v.kind) for v in vols] == [
            ("ltvm-kernel-a", "kernel"),
            ("ltvm-build-ccache-rocky9-aarch64", "ccache"),
        ]
        assert vols[0].build_tree == str(tmp_path.resolve())

    def test_none_when_podman_cannot_be_asked(
        self, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stopped machine is "not checked", never "there are none"."""
        monkeypatch.setattr(
            build_volumes, "_podman", lambda *a, timeout=None: _cp(rc=125)
        )
        assert build_volumes.list_volumes() is None
        assert build_volumes.dead_kernel_volumes() is None
        assert build_volumes.kernel_volumes_under([Path("/x")]) is None

    def test_listed_on_a_mac_even_with_the_volumes_turned_off(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_MACOS_BUILD_VOLUMES", "0")
        listing = _ls(_kvol("ltvm-kernel-a", tmp_path / "gone"))
        monkeypatch.setattr(build_volumes, "_podman", _fake_podman(listing))
        dead = build_volumes.dead_kernel_volumes()
        assert dead is not None and [v.name for v, _ in dead] == [
            "ltvm-kernel-a"
        ]

    def test_nothing_and_no_podman_off_a_mac(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_volumes, "is_macos", lambda: False)
        podman = MagicMock()
        monkeypatch.setattr(build_volumes, "_podman", podman)
        assert build_volumes.list_volumes() == []
        assert build_volumes.dead_kernel_volumes() == []
        assert build_volumes.doctor_report() == ([], [])
        podman.assert_not_called()


class TestDead:
    def test_why_each_volume_is_dead_or_not(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bt = _kernel_artifact(tmp_path)
        live = build_volumes.kernel_volume_name(bt)
        listing = _ls(
            _kvol(live, bt),
            _kvol("ltvm-kernel-superseded", bt),
            _kvol(
                "ltvm-kernel-gone",
                tmp_path / "no" / "kernels" / "k" / "build-tree",
            ),
            ("ltvm-kernel-unlabelled", {}),
            ("ltvm-build-ccache-x", {build_volumes.LABEL_KIND: "ccache"}),
        )
        monkeypatch.setattr(build_volumes, "_podman", _fake_podman(listing))
        dead = build_volumes.dead_kernel_volumes()
        assert dead is not None
        why = {v.name: reason for v, reason in dead}
        assert set(why) == {
            "ltvm-kernel-superseded",
            "ltvm-kernel-gone",
            "ltvm-kernel-unlabelled",
        }
        assert why["ltvm-kernel-superseded"].startswith("superseded")
        assert "gone" in why["ltvm-kernel-gone"]

    def test_scoped_to_one_targets_artifacts(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`ltvm clean <target>` looks only at that target's volumes; one
        whose origin is unknown is not assumed to be its."""
        arts = tmp_path / "artifacts"
        listing = _ls(
            _kvol(
                "ltvm-kernel-r9",
                arts / "rocky9" / "aarch64" / "kernels" / "a" / "build-tree",
            ),
            _kvol(
                "ltvm-kernel-r10",
                arts / "rocky10" / "aarch64" / "kernels" / "b" / "build-tree",
            ),
            ("ltvm-kernel-unlabelled", {}),
        )
        monkeypatch.setattr(build_volumes, "_podman", _fake_podman(listing))
        dead = build_volumes.dead_kernel_volumes([arts / "rocky9"])
        assert dead is not None and [v.name for v, _ in dead] == [
            "ltvm-kernel-r9"
        ]

    def test_volumes_of_kernels_about_to_be_deleted(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Live volumes too: what `clean --apply` deletes, they copy."""
        bt = _kernel_artifact(tmp_path)
        other = _kernel_artifact(tmp_path, kver="5.14-rhel9.6-5.14.0-570")
        listing = _ls(
            _kvol(build_volumes.kernel_volume_name(bt), bt),
            _kvol(build_volumes.kernel_volume_name(other), other),
        )
        monkeypatch.setattr(build_volumes, "_podman", _fake_podman(listing))
        going = build_volumes.kernel_volumes_under([bt.parent])
        assert going is not None
        assert [v.name for v in going] == [build_volumes.kernel_volume_name(bt)]


class TestSizes:
    def test_parses_podman_system_df(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = (
            "Images space usage:\n\nREPOSITORY TAG IMAGE ID CREATED SIZE SHARED SIZE\n"
            "\nLocal Volumes space usage:\n\nVOLUME NAME  LINKS  SIZE\n"
            "ltvm-kernel-a  0  1.8GB\nltvm-build-ccache-x  1  140.2MB\nsmall  0  0B\n"
        )
        monkeypatch.setattr(
            build_volumes, "_podman", lambda *a, timeout=None: _cp(out=out)
        )
        assert build_volumes.volume_sizes() == {
            "ltvm-kernel-a": 1_800_000_000,
            "ltvm-build-ccache-x": 140_200_000,
            "small": 0,
        }

    def test_unknown_when_podman_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            build_volumes, "_podman", lambda *a, timeout=None: _cp(rc=125)
        )
        assert build_volumes.volume_sizes() == {}


class TestDoctor:
    def test_does_not_look_while_the_machine_is_stopped(
        self, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, ...]] = []

        def podman(*args: str, timeout: float | None = None) -> Any:
            calls.append(args)
            if args[:2] == ("machine", "list"):
                return _cp(out=json.dumps([{"Name": "m", "Running": False}]))
            raise AssertionError(
                f"doctor ran podman {args} on a stopped machine"
            )

        monkeypatch.setattr(build_volumes, "_podman", podman)
        warnings, info = build_volumes.doctor_report()
        assert warnings == [] and "not checked" in info[0]
        assert all(a[0] == "machine" and "start" not in a for a in calls)

    def test_sizes_and_dead_volumes(
        self, tmp_path: Path, on_mac: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bt = _kernel_artifact(tmp_path)
        live = build_volumes.kernel_volume_name(bt)
        listing = _ls(
            _kvol(live, bt),
            _kvol("ltvm-kernel-gone", tmp_path / "gone"),
            ("ltvm-build-ccache-x", {build_volumes.LABEL_KIND: "ccache"}),
        )
        df = (
            "Local Volumes space usage:\n\nVOLUME NAME LINKS SIZE\n"
            f"{live} 0 1.8GB\nltvm-kernel-gone 0 1.7GB\nltvm-build-ccache-x 0 140MB\n"
        )

        def podman(*args: str, timeout: float | None = None) -> Any:
            if args[:2] == ("machine", "list"):
                return _cp(out=json.dumps([{"Name": "m", "Running": True}]))
            if args[:2] == ("volume", "ls"):
                return _cp(out=listing)
            if args[:2] == ("system", "df"):
                return _cp(out=df)
            return _cp()

        monkeypatch.setattr(build_volumes, "_podman", podman)
        warnings, info = build_volumes.doctor_report()
        assert "2 kernel build-tree(s), 3.5 GB" in info[0]
        assert "1 ccache, 140.0 MB" in info[0]
        assert (
            len(warnings) == 1
            and "1 kernel build-tree volume(s)" in warnings[0]
        )
        assert "1.7 GB" in warnings[0] and "ltvm clean --apply" in warnings[0]


def test_the_copy_uses_macos_tar_not_whatever_is_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its flags are bsdtar's; a GNU tar first on PATH refuses them."""
    bt = _kernel_artifact(tmp_path)
    monkeypatch.setattr(
        build_volumes, "_podman", lambda *a, timeout=None: _cp()
    )
    monkeypatch.setattr(build_volumes, "_probe", lambda i, m, s: "")
    popen = MagicMock()
    popen.return_value.wait.return_value = 0
    monkeypatch.setattr(build_volumes.subprocess, "Popen", popen)
    monkeypatch.setattr(build_volumes.subprocess, "run", lambda *a, **k: _cp())
    build_volumes._populate("v", bt, "img")
    assert popen.call_args.args[0][0] == "/usr/bin/tar"
