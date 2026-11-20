"""The hardware LNDs (--lnds kfi,efa): their pinned dependencies and
their seams with the Lustre build.  Nothing here fetches or builds."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ltvm_pkg import lnd_deps
from ltvm_pkg import lustre_build as lb
from tests.test_zfs_wiring import _capture_container_build, _configure_line


class TestParse:
    def test_none_is_empty(self) -> None:
        assert lnd_deps.parse_lnds(None) == ()
        assert lnd_deps.parse_lnds("") == ()

    def test_names_aliases_and_all(self) -> None:
        assert lnd_deps.parse_lnds("kfi,efa") == ("efa", "kfi")
        assert lnd_deps.parse_lnds(" kfilnd , EFA ") == ("efa", "kfi")
        assert lnd_deps.parse_lnds("all") == ("efa", "kfi")
        assert lnd_deps.parse_lnds("efa,efa") == ("efa",)

    def test_unknown_is_refused(self) -> None:
        with pytest.raises(ValueError, match="gni"):
            lnd_deps.parse_lnds("kfi,gni")


class TestConfigure:
    def _dirs(self, tmp_path: Path) -> dict[str, Path]:
        d = {"kfi": tmp_path / "kfab", "efa": tmp_path / "efa"}
        for p in d.values():
            p.mkdir()
        return d

    def test_absent_by_default(self, tmp_path: Path) -> None:
        cmd = _capture_container_build(tmp_path)
        script = cmd[-1]
        assert "--with-kfi" not in script
        assert "--enable-efa" not in script
        assert "kfabric.ko" not in script
        assert not any(a.endswith((":/kfi:ro", ":/efa:ro")) for a in cmd)

    def test_flags_mounts_and_kfabric_shipped(self, tmp_path: Path) -> None:
        dirs = self._dirs(tmp_path)
        cmd = _capture_container_build(tmp_path, lnd_dirs=dirs)
        line = _configure_line(cmd[-1])
        assert "--with-kfi=/kfi" in line
        assert "--enable-efa=/efa" in line
        assert f"{dirs['kfi']}:/kfi:ro" in cmd
        assert f"{dirs['efa']}:/efa:ro" in cmd
        # mounts go before the image tag, not into bash's argv
        assert cmd[-3] == "ltvm-build-rocky9"
        assert "/staging/lib/modules/1.2.3/extra/kfabric/kfabric.ko" in cmd[-1]

    def test_efa_alone(self, tmp_path: Path) -> None:
        dirs = self._dirs(tmp_path)
        del dirs["kfi"]
        cmd = _capture_container_build(tmp_path, lnd_dirs=dirs)
        line = _configure_line(cmd[-1])
        assert "--enable-efa=/efa" in line
        assert "--with-kfi" not in line
        assert "kfabric.ko" not in cmd[-1]

    def test_stamp_unchanged_without_lnds(self) -> None:
        line = "./configure --with-linux=/kernel --enable-server"
        bare = hashlib.sha256(line.encode()).hexdigest()
        assert lb.configure_stamp_hash(line, None, None) == bare
        assert lb.configure_stamp_hash(line, None, lnd_deps.identity(())) == (
            bare
        )

    def test_stamp_tracks_the_pins(self) -> None:
        """/kfi and /efa are mount points: a new pin must reconfigure
        even though the configure line does not change."""
        line = "./configure --with-kfi=/kfi"
        a = lb.configure_stamp_hash(line, None, lnd_deps.identity(("kfi",)))
        with patch.object(lnd_deps, "KFABRIC_COMMIT", "0" * 40):
            b = lb.configure_stamp_hash(line, None, lnd_deps.identity(("kfi",)))
        assert a != b != hashlib.sha256(line.encode()).hexdigest()
        # and alongside ZFS
        assert lb.configure_stamp_hash(line, "2.4.0", "x") != (
            lb.configure_stamp_hash(line, "2.4.0")
        )


class TestEfaHeader:
    def test_cached_header_is_used(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        data = b"efa\n"
        d = lnd_deps.efa_dir()
        d.mkdir(parents=True)
        (d / "efa_verbs.h").write_bytes(data)
        monkeypatch.setattr(
            lnd_deps, "EFA_HEADER_SHA256", hashlib.sha256(data).hexdigest()
        )
        with patch("urllib.request.urlopen") as get:
            assert lnd_deps.ensure_efa() == d
        get.assert_not_called()

    def test_wrong_digest_is_refused(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"not the header"

        with patch("urllib.request.urlopen", return_value=Resp()):
            with pytest.raises(lnd_deps.LndDepError, match="sha256"):
                lnd_deps.ensure_efa()
        assert not (lnd_deps.efa_dir() / "efa_verbs.h").exists()


class TestKfabricFresh:
    def _tree(self, d: Path, want: str) -> None:
        (d / "include").mkdir(parents=True)
        (d / "include" / "kfi_endpoint.h").write_text("")
        (d / "Module.symvers").write_text("")
        (d / "kfabric.ko").write_bytes(b"")
        (d / "meta.json").write_text(json.dumps({"input_hash": want}))

    def test_fresh(self, tmp_path: Path) -> None:
        self._tree(tmp_path, "h")
        assert lnd_deps._kfabric_fresh(tmp_path, "h", None)

    def test_other_inputs_are_stale(self, tmp_path: Path) -> None:
        self._tree(tmp_path, "h")
        assert not lnd_deps._kfabric_fresh(tmp_path, "other", None)

    def test_missing_module_is_stale(self, tmp_path: Path) -> None:
        self._tree(tmp_path, "h")
        (tmp_path / "kfabric.ko").unlink()
        assert not lnd_deps._kfabric_fresh(tmp_path, "h", None)

    def test_input_hash_covers_kernel_and_pin(self) -> None:
        a = lnd_deps._kfabric_input_hash("5.14", "k1")
        assert a != lnd_deps._kfabric_input_hash("5.14", "k2")
        assert a != lnd_deps._kfabric_input_hash("5.15", "k1")
        with patch.object(lnd_deps, "KFABRIC_COMMIT", "0" * 40):
            assert a != lnd_deps._kfabric_input_hash("5.14", "k1")


class TestCli:
    def test_not_requested(self) -> None:
        from ltvm_pkg.cli.build import _resolve_lnds

        args = argparse.Namespace(lnds=None)
        assert _resolve_lnds(None, args, None, True) == (None, None)  # type: ignore[arg-type]

    def test_bad_name_is_an_error(self) -> None:
        from ltvm_pkg.cli.build import _resolve_lnds

        args = argparse.Namespace(lnds="bogus")
        dirs, err = _resolve_lnds(None, args, None, True)  # type: ignore[arg-type]
        assert dirs is None and err and "bogus" in err

    def test_build_lustre_and_deploy_take_lnds(self) -> None:
        import importlib.machinery
        import importlib.util

        loader = importlib.machinery.SourceFileLoader("ltvm_main", "ltvm")
        spec = importlib.util.spec_from_loader("ltvm_main", loader)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        ltvm_parser = mod.build_parser()
        ns = ltvm_parser.parse_args(
            ["build", "lustre", "rocky9", "--lnds", "kfi,efa"]
        )
        assert ns.lnds == "kfi,efa"
        ns = ltvm_parser.parse_args(["deploy-lustre", "co1-x", "--lnds", "kfi"])
        assert ns.lnds == "kfi"
