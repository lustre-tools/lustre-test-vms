"""MOFED kmods must rebuild when the kernel they link against changes.

kver is the EXTRAVERSION-derived release string
("5.14.0-611.47.1.el9_7_lustre").  It does not change when a Lustre
kernel patch or a kernels.config option does -- but Module.symvers
does.  Keying the cache on kver alone meant image_build reused kmods
built against the previous kernel and rpm -ivh --force'd them in;
mlx5_core/ib_core then fail to load in the VM with symbol-version
disagreement, taking ko2iblnd with them.
"""

from __future__ import annotations

import json
from pathlib import Path

from ltvm_pkg import mofed_kmod_build as mk
from tests.test_build_commands import _add_mofed_variant, _make_tc


def _mofed_tc(tmp_targets: Path):
    """A rocky9 TargetConfig bound to the mofed-24 variant."""
    _add_mofed_variant(tmp_targets)
    return _make_tc(tmp_targets, variant="mofed-24")


def _seed_kernel(tc, kernel: str | None, input_hash: str) -> Path:
    out = tc.kernel_output_dir(kernel)
    (out / "build-tree" / "include" / "config").mkdir(parents=True, exist_ok=True)
    (out / "build-tree" / "include" / "config" / "kernel.release").write_text(
        "5.14.0-611.47.1.el9_7_lustre\n"
    )
    (out / "meta.json").write_text(json.dumps({"input_hash": input_hash}))
    return out


def _seed_kmods(tc, kernel: str | None, input_hash: str) -> Path:
    out = mk.mofed_kmod_dir(tc, kernel)
    out.mkdir(parents=True, exist_ok=True)
    (out / "kmod-mlnx-ofa.rpm").write_bytes(b"rpm")
    (out / "meta.json").write_text(json.dumps({"input_hash": input_hash}))
    return out


class TestMofedKmodStaleness:
    def test_fresh_when_kernel_unchanged(self, tmp_targets: Path) -> None:
        tc = _mofed_tc(tmp_targets)
        _seed_kernel(tc, None, "kernelhash1")
        h = mk._input_hash(
            "5.14.0-611.47.1.el9_7_lustre", mk._mofed_version(tc), "kernelhash1"
        )
        _seed_kmods(tc, None, h)
        assert mk.is_stale(tc) is False

    def test_stale_when_kernel_input_hash_changes(
        self, tmp_targets: Path
    ) -> None:
        """Edit a kernel patch: same kver, different Module.symvers."""
        tc = _mofed_tc(tmp_targets)
        _seed_kernel(tc, None, "kernelhash1")
        h = mk._input_hash(
            "5.14.0-611.47.1.el9_7_lustre", mk._mofed_version(tc), "kernelhash1"
        )
        _seed_kmods(tc, None, h)
        assert mk.is_stale(tc) is False

        # Kernel rebuilt from an edited patch series: the release
        # string is identical, only the recorded input hash moved.
        _seed_kernel(tc, None, "kernelhash2")
        assert mk.is_stale(tc) is True

    def test_stale_when_rpms_missing_despite_matching_hash(
        self, tmp_targets: Path
    ) -> None:
        """A failed build wipes the RPMs but leaves meta.json."""
        tc = _mofed_tc(tmp_targets)
        _seed_kernel(tc, None, "kernelhash1")
        h = mk._input_hash(
            "5.14.0-611.47.1.el9_7_lustre", mk._mofed_version(tc), "kernelhash1"
        )
        out = _seed_kmods(tc, None, h)
        for rpm in out.glob("*.rpm"):
            rpm.unlink()
        assert mk.is_stale(tc) is True

    def test_input_hash_varies_with_kernel_hash(self) -> None:
        a = mk._input_hash("kver", "24.10", "hashA")
        b = mk._input_hash("kver", "24.10", "hashB")
        assert a != b
