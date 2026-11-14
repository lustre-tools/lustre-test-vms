"""Golden values for TargetConfig.input_hash.

``input_hash`` is the staleness key: it is written into every artifact's
meta.json and compared against a freshly computed one to decide whether
to rebuild.  So changing what it produces, for inputs that did not
change, silently invalidates every built artifact on every machine --
and the cost is a kernel rebuild per target, not a warning.

These goldens exist so a refactor of the hash *composition* (splitting
it into named components for `build status --why`, say) cannot do that
by accident.  They are not a specification of the algorithm: if you
deliberately change what feeds the hash, these values must be updated in
the same commit, and everyone rebuilds. That is the signal, and it
should be a conscious one.

They depend on targets.yaml and on the files the hash reads
(Dockerfiles, kernel config fragments, package lists, the inner build
scripts), so an intentional edit to any of those also moves them.
"""

from __future__ import annotations

import pytest

from ltvm_pkg.target_config import TargetConfig

# (target, artifact, kernel, variant, expected)
GOLDEN = [
    ("rocky8", "container", None, None, "05ec17e26c081652"),
    ("rocky8", "kernel", None, None, "94b2aa2076342737"),
    ("rocky8", "image", None, None, "c6e71aa5181f6bff"),
    ("rocky9", "container", None, None, "4a58226592cb2cf5"),
    ("rocky9", "kernel", None, None, "895d9670b0a945cb"),
    ("rocky9", "image", None, None, "43d34c61a0641127"),
    ("rocky9-64k", "container", None, None, "9ebe6bff482840f1"),
    ("rocky9-64k", "kernel", None, None, "12c9c532b86f8cf1"),
    ("rocky9-64k", "image", None, None, "33d689044bf10d48"),
    ("rocky10", "container", None, None, "9f9eabcec78fc131"),
    ("rocky10", "kernel", None, None, "7812dbd5a370c2b9"),
    ("rocky10", "image", None, None, "2208508630db3762"),
    ("rocky10-16k", "container", None, None, "ee00de82e663a154"),
    ("rocky10-16k", "kernel", None, None, "0b92dfd2a104d132"),
    ("rocky10-16k", "image", None, None, "81ad4e78bd099834"),
    ("mainline", "container", None, None, "c25d828a438d5e1b"),
    ("mainline", "kernel", None, None, "368c2341ea029471"),
    ("mainline", "image", None, None, "a6af28486a36fac2"),
    ("ubuntu2404", "container", None, None, "a6dbffaea81d2907"),
    ("ubuntu2404", "kernel", None, None, "2f2c2f9b5edffb9f"),
    ("ubuntu2404", "image", None, None, "b18263970e2aed9a"),
    ("ubuntu2604", "container", None, None, "a12a95e67b1528aa"),
    ("ubuntu2604", "kernel", None, None, "04b3d2e1fbbb0db6"),
    ("ubuntu2604", "image", None, None, "b18217637800a1f6"),
    ("ubuntu2604-server", "container", None, None, "9a6c381b934a8c9f"),
    ("ubuntu2604-server", "kernel", None, None, "faa918878df6999c"),
    ("ubuntu2604-server", "image", None, None, "04f47e7fdcb0e245"),
    ("debian13", "container", None, None, "8bc7587fe4a5bc3f"),
    ("debian13", "kernel", None, None, "b1897fdaad34a730"),
    ("debian13", "image", None, None, "61702badaa3ef5d2"),
    ("opensuse156", "container", None, None, "d9f755295c83a19c"),
    ("opensuse156", "kernel", None, None, "59696dfca0eefcf0"),
    ("opensuse156", "image", None, None, "ddad8c6c4a645b3a"),
    # A variant must not perturb the base hashes above, and must differ
    # from them.
    ("rocky9", "container", None, "mofed-24", "cc1a03c2d0fda310"),
    ("rocky9", "image", None, "mofed-24", "7746cd4b02c01136"),
    # An explicitly named kernel.
    ("rocky9", "kernel", "5.14-rhel9.5", None, "7a54d6d849f5ca6c"),
    ("rocky9", "image", "5.14-rhel9.5", None, "7323df68446f5f85"),
]


@pytest.fixture(autouse=True)
def _no_built_kernel(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """An image's hash folds in its built kernel's input_hash when one
    exists; pin the goldens without it, or they hold only on the host
    whose artifacts/ produced them."""
    monkeypatch.setattr(
        TargetConfig, "meta_path", lambda self, *a, **k: tmp_path / "none"
    )


@pytest.mark.parametrize(
    "target,artifact,kernel,variant,expected",
    GOLDEN,
    ids=[
        f"{t}-{a}{'-' + k if k else ''}{'-' + v if v else ''}"
        for t, a, k, v, _ in GOLDEN
    ],
)
def test_input_hash_is_unchanged(
    target: str,
    artifact: str,
    kernel: str | None,
    variant: str | None,
    expected: str,
) -> None:
    tc = TargetConfig(target, variant=variant or "base")
    got = tc.input_hash(artifact, kernel=kernel, variant=variant)
    assert got == expected, (
        f"input_hash({target}, {artifact}, kernel={kernel}, "
        f"variant={variant}) changed: {expected} -> {got}.\n"
        f"Every built {artifact} for {target} just became stale. If that "
        f"is intended, update the golden in the same commit."
    )


def test_extra_bytes_still_fold_in() -> None:
    """kernel_build passes the Lustre patch series through `extra`.

    Without it, editing a patch in place would not invalidate the cached
    vmlinuz -- the exact workflow ltvm exists for.
    """
    tc = TargetConfig("rocky9")
    assert tc.input_hash("kernel", extra=b"patchbytes") == "1df2e215c3ab50d4"
    assert tc.input_hash("kernel", extra=b"patchbytes") != tc.input_hash(
        "kernel"
    )
