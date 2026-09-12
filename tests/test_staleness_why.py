"""Tests for `ltvm build status --why`.

"stale" on its own leaves the user guessing whether a 40-minute kernel
rebuild is warranted.  `--why` names the input that moved, by diffing
the per-input digests meta.json records against freshly computed ones.

The invariant that matters most is elsewhere, in
tests/test_input_hash_stability.py: splitting the hash into named
components must not change the digest, or every artifact on every
machine goes stale at once.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ltvm_pkg.cli.util import has_recorded_components, staleness_reasons
from ltvm_pkg.target_config import TargetConfig


@pytest.fixture
def arts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An artifacts dir of our own, so nothing touches the repo's."""
    root = tmp_path / "artifacts"
    root.mkdir()
    monkeypatch.setattr("ltvm_pkg.target_config.ARTIFACTS_DIR", root)
    return root


def _status(tc: TargetConfig, artifact: str = "container") -> dict[str, Any]:
    """The status dict shape cmd_status builds: meta plus built/stale."""
    meta = json.loads((tc.container_output_dir() / "meta.json").read_text())
    return {"built": True, "stale": tc.is_stale(artifact), **meta}


class TestInputComponents:
    def test_components_cover_the_hash_inputs(self) -> None:
        comps = TargetConfig("rocky9").input_components("container")
        assert "targets.yaml" in comps
        assert "container.Dockerfile" in comps
        assert all(len(v) == 12 for v in comps.values())

    def test_a_changed_file_changes_only_its_component(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tc = TargetConfig("rocky9")
        before = tc.input_components("container")
        # Rewrite one hashed file through the real path it is read from.
        real = Path("targets/common/build-e2fsprogs.sh")
        original = real.read_bytes()
        try:
            real.write_bytes(original + b"\n# tweak\n")
            after = TargetConfig("rocky9").input_components("container")
        finally:
            real.write_bytes(original)
        changed = [k for k in after if before.get(k) != after[k]]
        assert changed == ["common/build-e2fsprogs.sh"]

    def test_the_kernel_lustre_inputs_are_a_named_component(self) -> None:
        """kernel_build folds the patch series in through `extra`."""
        comps = TargetConfig("rocky9").input_components(
            "kernel", extra=b"patches"
        )
        assert "lustre-tree-inputs" in comps

    def test_a_component_is_named_by_its_real_path(self) -> None:
        """Every hashed file was labelled "common/<basename>", so a
        per-target file was reported at a path that does not exist --
        and two same-named files from different directories would have
        collapsed into one component.  Labels do not feed the digest,
        so this only ever affected what `--why` printed.
        """
        comps = TargetConfig("rocky9").input_components("image")
        assert "rocky9/packages-os.txt" in comps
        assert "common/packages-os.txt" not in comps
        # Genuinely shared files keep reading as common/.
        assert "common/packages-base.txt" in comps

    def test_relabelling_did_not_move_the_digest(self) -> None:
        """The guard for the above: _HashParts.label records a name and
        never feeds the hash, and the goldens pin that."""
        tc = TargetConfig("rocky9")
        comps = tc.input_components("image")
        assert any("/" in k for k in comps)
        assert tc.input_hash("image") == tc._hash_parts("image").digest()

    def test_a_variant_adds_its_own_component(self) -> None:
        comps = TargetConfig("rocky9", variant="mofed-24").input_components(
            "container", variant="mofed-24"
        )
        assert "variant:mofed-24" in comps


class TestWriteMetaRecordsComponents:
    def test_meta_carries_the_components(self, arts: Path) -> None:
        tc = TargetConfig("rocky9")
        tc.write_meta("container", image_tag="t")
        meta = json.loads((tc.container_output_dir() / "meta.json").read_text())
        assert meta["input_components"]
        assert meta["input_hash"]

    def test_components_match_a_fresh_computation(self, arts: Path) -> None:
        """Recorded under the same arguments as the hash, or --why would
        explain inputs the decision was not made on."""
        tc = TargetConfig("rocky9")
        tc.write_meta("container", image_tag="t")
        meta = json.loads((tc.container_output_dir() / "meta.json").read_text())
        assert meta["input_components"] == tc.input_components("container")


class TestStalenessReasons:
    def test_names_the_changed_input(self, arts: Path) -> None:
        tc = TargetConfig("rocky9")
        tc.write_meta("container", image_tag="t")
        status = _status(tc)
        assert status["stale"] is False

        # Pretend one recorded input was different when it was built.
        status["input_components"] = {
            **status["input_components"],
            "container.Dockerfile": "0" * 12,
        }
        reasons = staleness_reasons(tc, "container", status)
        assert reasons == ["container.Dockerfile (changed)"]

    def test_reports_a_new_input(self, arts: Path) -> None:
        tc = TargetConfig("rocky9")
        tc.write_meta("container", image_tag="t")
        status = _status(tc)
        status["input_components"] = {
            k: v
            for k, v in status["input_components"].items()
            if k != "container.Dockerfile"
        }
        assert "container.Dockerfile (new input)" in staleness_reasons(
            tc, "container", status
        )

    def test_reports_an_input_that_went_away(self, arts: Path) -> None:
        tc = TargetConfig("rocky9")
        tc.write_meta("container", image_tag="t")
        status = _status(tc)
        status["input_components"] = {
            **status["input_components"],
            "common/gone.sh": "a" * 12,
        }
        assert "common/gone.sh (no longer an input)" in staleness_reasons(
            tc, "container", status
        )

    def test_nothing_to_say_for_a_current_artifact(self, arts: Path) -> None:
        tc = TargetConfig("rocky9")
        tc.write_meta("container", image_tag="t")
        assert staleness_reasons(tc, "container", _status(tc)) == []

    def test_a_legacy_meta_has_no_recorded_components(self, arts: Path) -> None:
        """Artifacts built before the digests were written must read as
        "cannot tell", not as "nothing changed"."""
        tc = TargetConfig("rocky9")
        tc.write_meta("container", image_tag="t")
        status = _status(tc)
        del status["input_components"]
        assert has_recorded_components(status) is False
        assert staleness_reasons(tc, "container", status) == []

    def test_the_kernel_lustre_component_is_not_blamed(
        self, arts: Path
    ) -> None:
        """`build status` has no Lustre tree, so it cannot recompute that
        component -- and must not report it as having disappeared."""
        tc = TargetConfig("rocky9")
        tc.write_meta("kernel", extra_hash=b"patches", kernel_version="x")
        meta = json.loads((tc.kernel_output_dir() / "meta.json").read_text())
        status = {"built": True, "stale": True, **meta}
        assert "lustre-tree-inputs" in status["input_components"]
        reasons = staleness_reasons(tc, "kernel", status)
        assert not any("lustre-tree-inputs" in r for r in reasons)

    def test_never_raises_when_recomputation_fails(
        self, arts: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explaining staleness must not be what breaks build status."""
        tc = TargetConfig("rocky9")
        tc.write_meta("container", image_tag="t")
        status = _status(tc)
        monkeypatch.setattr(
            TargetConfig,
            "input_components",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")),
        )
        assert staleness_reasons(tc, "container", status) == []
