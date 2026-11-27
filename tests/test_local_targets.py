"""A host's own targets, kept outside the checkout ([targets] local)."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import ltvm_pkg.target_config as cfg


@pytest.fixture
def tree(tmp_targets: Path) -> Iterator[Path]:
    """tmp_targets with a derived target, rocky9-dbg, in the checkout."""
    targets = tmp_targets / "targets"
    data = yaml.safe_load((targets / "targets.yaml").read_text())
    dbg = dict(data["targets"]["rocky9"])
    dbg["kernels"] = {**dbg["kernels"], "config": {"CONFIG_KASAN": "y"}}
    data["targets"]["rocky9-dbg"] = dbg
    (targets / "targets.yaml").write_text(yaml.safe_dump(data))
    shutil.copytree(targets / "rocky9", targets / "rocky9-dbg")
    with (
        patch.object(cfg, "TARGETS_DIR", targets),
        patch.object(cfg, "TARGETS_YAML", targets / "targets.yaml"),
        patch.object(cfg, "ARTIFACTS_DIR", tmp_targets / "artifacts"),
        patch.object(cfg, "_warned", set()),
    ):
        yield tmp_targets


def move_to_local(tree: Path, name: str) -> Path:
    """Move a target's entry and directory out of the checkout."""
    targets = tree / "targets"
    local = tree / "local"
    local.mkdir()
    data = yaml.safe_load((targets / "targets.yaml").read_text())
    entry = data["targets"].pop(name)
    (targets / "targets.yaml").write_text(yaml.safe_dump(data))
    (local / "targets.yaml").write_text(
        yaml.safe_dump({"targets": {name: entry}})
    )
    shutil.move(str(targets / name), str(local / name))
    return local


def hashes(name: str) -> list[str]:
    tc = cfg.TargetConfig(name)
    return [tc.input_hash(a) for a in ("container", "kernel", "image")]


class TestLocalTargets:
    def test_moving_a_target_out_keeps_its_hashes(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Moving is not a change: nothing it built goes stale."""
        before = hashes("rocky9-dbg")
        local = move_to_local(tree, "rocky9-dbg")
        monkeypatch.setenv(cfg.LOCAL_TARGETS_ENV, str(local))
        assert hashes("rocky9-dbg") == before
        assert cfg.TargetConfig("rocky9-dbg").target_dir == local / "rocky9-dbg"
        assert (
            cfg.TargetConfig("rocky9").target_dir == tree / "targets" / "rocky9"
        )

    def test_listed_with_the_checkouts(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            cfg.LOCAL_TARGETS_ENV, str(move_to_local(tree, "rocky9-dbg"))
        )
        assert set(cfg.list_targets()) >= {"rocky9", "rocky9-dbg"}

    def test_site_config_names_the_directory(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        local = move_to_local(tree, "rocky9-dbg")
        conf = tree / "ltvm.conf"
        conf.write_text(f"[targets]\nlocal = {local}\n")
        monkeypatch.setenv("LTVM_SITE_CONFIG", str(conf))
        assert cfg.TargetConfig("rocky9-dbg").target_dir == local / "rocky9-dbg"
        # an empty variable turns off only the user's layer
        monkeypatch.setenv(cfg.LOCAL_TARGETS_ENV, "")
        assert cfg.TargetConfig("rocky9-dbg").target_dir == local / "rocky9-dbg"

    def test_user_layer_overrides_the_sites(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        site = move_to_local(tree, "rocky9-dbg")
        conf = tree / "ltvm.conf"
        conf.write_text(f"[targets]\nlocal = {site}\n")
        monkeypatch.setenv("LTVM_SITE_CONFIG", str(conf))
        user = tree / "config" / "ltvm" / "targets.d"
        shutil.copytree(site, user)
        data = yaml.safe_load((user / "targets.yaml").read_text())
        data["targets"]["rocky9-dbg"]["kernels"]["config"] = {
            "CONFIG_KCSAN": "y"
        }
        (user / "targets.yaml").write_text(yaml.safe_dump(data))
        site_hashes = hashes("rocky9-dbg")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tree / "config"))
        tc = cfg.TargetConfig("rocky9-dbg")
        assert tc.target_dir == user / "rocky9-dbg"
        assert tc.kernel_config_overrides.get("CONFIG_KCSAN") == "y"
        assert hashes("rocky9-dbg") != site_hashes
        monkeypatch.setenv(cfg.LOCAL_TARGETS_ENV, "")
        assert cfg.TargetConfig("rocky9-dbg").target_dir == site / "rocky9-dbg"

    def test_cannot_replace_a_checkout_target(
        self,
        tree: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        local = tree / "local"
        (local / "rocky9").mkdir(parents=True)
        (local / "targets.yaml").write_text(
            yaml.safe_dump({"targets": {"rocky9": {"os_name": "other"}}})
        )
        monkeypatch.setenv(cfg.LOCAL_TARGETS_ENV, str(local))
        tc = cfg.TargetConfig("rocky9")
        assert (
            tc.os_name == "rocky"
            and tc.target_dir == tree / "targets" / "rocky9"
        )
        assert "ignoring local target 'rocky9'" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("setting", "problem"),
        [
            ("relative/dir", "not an absolute path"),
            ("/no/such/dir", "no /no/such/dir/targets.yaml"),
        ],
    )
    def test_bad_setting_is_ignored(
        self,
        tree: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        setting: str,
        problem: str,
    ) -> None:
        monkeypatch.setenv(cfg.LOCAL_TARGETS_ENV, setting)
        assert "rocky9" in cfg.list_targets()
        assert problem in capsys.readouterr().err

    def test_file_without_targets_is_ignored(
        self,
        tree: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        local = tree / "local"
        local.mkdir()
        (local / "targets.yaml").write_text("defaults: {}\n")
        monkeypatch.setenv(cfg.LOCAL_TARGETS_ENV, str(local))
        assert "rocky9-dbg" in cfg.list_targets()
        assert "no 'targets:' mapping" in capsys.readouterr().err

    def test_unknown_target_names_both_files(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        local = move_to_local(tree, "rocky9-dbg")
        monkeypatch.setenv(cfg.LOCAL_TARGETS_ENV, str(local))
        with pytest.raises(
            ValueError, match=r"not in .*targets\.yaml or .*local/targets\.yaml"
        ):
            cfg.TargetConfig("nope")
