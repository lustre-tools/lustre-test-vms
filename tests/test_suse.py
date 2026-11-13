"""opensuse156: SLES 15 kernels from SUSE's release tags.

Public SLES kernel SRPMs are scarce, but every released kernel is an
rpm-<version>-<release> tag: SUSE/kernel's is the source with series.conf
applied, and SUSE/kernel-source's of the same name has the config.
These cover the host side of that build and the places the suse OS
family differs from rhel and debian.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import kernel_build, vm_cluster, vm_commands
from ltvm_pkg.lustre_compat import (
    _built_kver,
    _kver_from_target_in,
    parse_target_in,
)
from ltvm_pkg.target_config import LustreMode, TargetConfig
from ltvm_pkg.vm_state import VMInfo, lustre_libdir

# conftest neutralizes it for every test; this file tests it.
_ENSURE_PDSH = vm_cluster._ensure_pdsh

# The shape of Lustre's 6.4-sles15sp7.target.in.
_SLES_TARGET = """\
lnxmaj="6.4"
lnxmin=".0"
lnxrel="150700.53.66"
rpmfix=".1"
KERNEL_SRPM=kernel-source-${lnxmaj}${lnxmin}-${lnxrel}${rpmfix}.src.rpm
SERIES=""
VERSION=$lnxmaj
"""


def _tree(tmp_path: Path, name: str, text: str) -> Path:
    targets = tmp_path / "lustre" / "kernel_patches" / "targets"
    targets.mkdir(parents=True)
    (targets / f"{name}.target.in").write_text(text)
    return tmp_path


class TestTarget:
    def test_loads_as_a_suse_client(self) -> None:
        tc = TargetConfig("opensuse156")
        assert tc.os_family == "suse"
        assert tc.lustre_mode == LustreMode.CLIENT
        assert tc.kernel_suse_source == "https://github.com/SUSE"
        assert tc.srpm_url is None

    def test_kernel_hash_reads_the_suse_inner_script(self) -> None:
        """Only the script the target runs feeds its kernel hash, so
        editing the SUSE one rebuilds no rocky kernel and vice versa."""
        suse = TargetConfig("opensuse156").input_components("kernel")
        assert "kernel-build-inner-suse.sh" in suse
        assert "kernel-build-inner.sh" not in suse
        rocky = TargetConfig("rocky9").input_components("kernel")
        assert "kernel-build-inner-suse.sh" not in rocky

    def test_installs_lustre_where_rhel_does(self) -> None:
        assert lustre_libdir("suse") == lustre_libdir("rhel")


class TestParseSuseTarget:
    def test_version_joins_lnxmaj_and_lnxmin(self, tmp_path: Path) -> None:
        tree = _tree(tmp_path, "6.4-sles15sp7", _SLES_TARGET)
        info = kernel_build.parse_suse_target(tree, "6.4-sles15sp7")
        assert info == {
            "version": "6.4.0",
            "release": "150700.53.66",
            "series": "",
        }
        assert (
            kernel_build.suse_tag(info["version"], info["release"])
            == "rpm-6.4.0-150700.53.66"
        )

    def test_missing_target_file(self, tmp_path: Path) -> None:
        tree = _tree(tmp_path, "6.4-sles15sp7", _SLES_TARGET)
        with pytest.raises(FileNotFoundError):
            kernel_build.parse_suse_target(tree, "6.4-sles15sp6")

    def test_compat_gate_reads_the_changelog_version(
        self, tmp_path: Path
    ) -> None:
        """ChangeLog lists SLES kernels as 6.4.0-<release>; the gate
        once compared 6.4-<release> and refused every one of them."""
        tree = _tree(tmp_path, "6.4-sles15sp7", _SLES_TARGET)
        ti = parse_target_in(tree, "6.4-sles15sp7")
        assert _kver_from_target_in(ti) == "6.4.0-150700.53.66"


class TestBuiltKver:
    """Once a kernel is built, the gate checks its release instead."""

    @pytest.mark.parametrize(
        "release,want",
        [
            ("6.4.0-150700.53.66_lustre-default", "6.4.0-150700.53.66"),
            ("5.14.0-611.55.1.el9_7_lustre", "5.14.0-611.55.1.el9_7"),
            ("7.0.0-14-lustre", "7.0.0-14-lustre"),
        ],
    )
    def test_drops_the_lustre_marker(
        self, tmp_path: Path, release: str, want: str
    ) -> None:
        (tmp_path / "build-tree").mkdir()
        (tmp_path / "meta.json").write_text(
            json.dumps({"kernel_version": release})
        )
        assert _built_kver(tmp_path / "build-tree") == want


class TestSourceUrls:
    def test_tree_and_config_come_from_the_same_tag(self) -> None:
        tree, config = kernel_build.suse_source_urls(
            "https://github.com/SUSE/", "rpm-6.4.0-150700.53.66", "x86_64"
        )
        assert tree == (
            "https://github.com/SUSE/kernel/archive/refs/tags/"
            "rpm-6.4.0-150700.53.66.tar.gz"
        )
        assert config == (
            "https://github.com/SUSE/kernel-source/raw/"
            "rpm-6.4.0-150700.53.66/config/x86_64/default"
        )

    def test_aarch64_is_suses_arm64(self) -> None:
        _, config = kernel_build.suse_source_urls(
            "https://github.com/SUSE", "rpm-6.4.0-150700.53.66", "aarch64"
        )
        assert config.endswith("/config/arm64/default")

    def test_unknown_arch(self) -> None:
        with pytest.raises(ValueError, match="riscv64"):
            kernel_build.suse_source_urls(
                "https://github.com/SUSE", "rpm-6.4.0-1", "riscv64"
            )


class TestDispatch:
    def test_build_kernel_takes_the_suse_path(self, tmp_path: Path) -> None:
        tc = TargetConfig("opensuse156")
        with (
            patch.object(
                kernel_build, "_build_kernel_suse", return_value={"ok": 1}
            ) as suse,
            patch.object(kernel_build, "_build_kernel_srpm") as srpm,
        ):
            assert kernel_build.build_kernel(tc, tmp_path) == {"ok": 1}
        suse.assert_called_once()
        srpm.assert_not_called()

    def test_needs_a_lustre_tree(self) -> None:
        with pytest.raises(ValueError, match="lustre_tree"):
            kernel_build.build_kernel(TargetConfig("opensuse156"), None)


class TestCluster:
    def test_pdsh_comes_from_zypper(self) -> None:
        with patch.object(
            vm_cluster,
            "run_ssh",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as ssh:
            _ENSURE_PDSH("co3-c1", "10.0.0.3", "suse")
        assert (
            "zypper -n -q install --no-recommends pdsh"
            in (ssh.call_args.args[1])
        )


class TestKdump:
    def _vm(self, tmp_path: Path) -> VMInfo:
        kdir = tmp_path / "kernels" / "6.4"
        kdir.mkdir(parents=True)
        (kdir / "vmlinux").write_bytes(b"ELF")
        (kdir / "vmlinuz").write_bytes(b"bz")
        return VMInfo(
            name="co1-sles",
            ip="10.0.0.98",
            kernel=str(kdir / "vmlinux"),
            kver="6.4.0-150700.53.66_lustre-default",
            os_id="opensuse156",
        )

    def _seed(self, tmp_path: Path, present: bool) -> list[str]:
        def _ssh(ip, cmd, **kw):
            r = MagicMock(stdout="", stderr="")
            r.returncode = 1 if cmd.startswith("test -f") and not present else 0
            return r

        with (
            patch("ltvm_pkg.vm_commands.run_ssh", side_effect=_ssh) as ssh,
            patch(
                "ltvm_pkg.vm_commands.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ),
        ):
            vm_commands._seed_kdump_boot(self._vm(tmp_path))
        return [c.args[1] for c in ssh.call_args_list]

    def test_probes_suses_initrd(self, tmp_path: Path) -> None:
        cmds = self._seed(tmp_path, present=True)
        assert any("test -f /var/lib/kdump/initrd" in c for c in cmds)
        assert any("systemctl --no-block start kdump " in c for c in cmds)
        assert not any("restart" in c for c in cmds)
        assert not any("mkdumprd" in c for c in cmds)

    def test_missing_initrd_is_built_by_the_service(
        self, tmp_path: Path
    ) -> None:
        """Not mkdumprd directly: kdump.service runs it on first boot,
        and a second run would race that one."""
        cmds = self._seed(tmp_path, present=False)
        assert any(
            "ln -sf vmlinuz-6.4.0-150700.53.66_lustre-default /boot/vmlinuz"
            in c
            and "systemctl restart kdump" in c
            for c in cmds
        )
        assert not any("mkdumprd" in c or "dracut" in c for c in cmds)


class TestFetch:
    """fetch found neither published opensuse156 release: the kernel
    flavor ending the release (-default) read as a variant name, and
    the 6.4 signature could not tell SP6 from SP7."""

    _TAGS = (
        "opensuse156-x86_64-6.4.0-150700.53.66_lustre-default",
        "opensuse156-x86_64-6.4.0-150600.23.78_lustre-default",
    )

    def _releases(self) -> list[dict]:
        return [
            {
                "tag_name": tag,
                "assets": [
                    {
                        "name": f"manifest-{tag}.json",
                        "browser_download_url": f"https://ex/{tag}.json",
                    }
                ],
            }
            for tag in self._TAGS
        ]

    @pytest.mark.parametrize(
        "kernel,tag",
        [("6.4-sles15sp7", _TAGS[0]), ("6.4-sles15sp6", _TAGS[1])],
    )
    def test_each_kernel_finds_its_release(self, kernel: str, tag: str) -> None:
        from ltvm_pkg.cli.fetch import (
            _find_release_url,
            _kernel_release_signature,
        )

        with patch("ltvm_pkg.cli._gh_api", return_value=self._releases()):
            url = _find_release_url(
                "opensuse156",
                arch="x86_64",
                kernel_signature=_kernel_release_signature(kernel),
            )
        assert url == f"https://ex/{tag}.json"

    def test_release_maps_back_to_its_kernel(self) -> None:
        from ltvm_pkg.cli.fetch import _fetched_kernel_name

        assert (
            _fetched_kernel_name("opensuse156", self._TAGS[1], "x86_64")
            == "6.4-sles15sp6"
        )
