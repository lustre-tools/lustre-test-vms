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
    ("rocky8", "container", None, None, "dfa0fc93d2774ebf"),
    ("rocky8", "kernel", None, None, "f9282b2d2a61b2ff"),
    ("rocky8", "image", None, None, "3184a3510a2bea97"),
    ("rocky9", "container", None, None, "4a58226592cb2cf5"),
    ("rocky9", "kernel", None, None, "9917b5f43ad2a431"),
    ("rocky9", "image", None, None, "d908b8e05ce9e1a7"),
    ("rocky9-64k", "container", None, None, "9ebe6bff482840f1"),
    ("rocky9-64k", "kernel", None, None, "5cfa9a6bfe6a0141"),
    ("rocky9-64k", "image", None, None, "480cc6a568d2f5f4"),
    ("rocky10", "container", None, None, "9f9eabcec78fc131"),
    ("rocky10", "kernel", None, None, "9ffe169f16cb7b21"),
    ("rocky10", "image", None, None, "cdcb09573ebfa9bf"),
    ("rocky10-16k", "container", None, None, "ee00de82e663a154"),
    ("rocky10-16k", "kernel", None, None, "1d40dec71e52ba4e"),
    ("rocky10-16k", "image", None, None, "b2421f46c0a1aaf7"),
    ("mainline", "container", None, None, "c25d828a438d5e1b"),
    ("mainline", "kernel", None, None, "553a4dfdd38396f1"),
    ("mainline", "image", None, None, "a897b57c69782cb5"),
    ("ubuntu2404", "container", None, None, "a6dbffaea81d2907"),
    ("ubuntu2404", "kernel", None, None, "e4cb01f684b1ba28"),
    ("ubuntu2404", "image", None, None, "01fd938d83dc2fc8"),
    ("ubuntu2604", "container", None, None, "a12a95e67b1528aa"),
    ("ubuntu2604", "kernel", None, None, "9e7932acc03c6d3d"),
    ("ubuntu2604", "image", None, None, "2a84c14bc8bdb89c"),
    ("ubuntu2604-server", "container", None, None, "9a6c381b934a8c9f"),
    ("ubuntu2604-server", "kernel", None, None, "1d45a0ce8ced1133"),
    ("ubuntu2604-server", "image", None, None, "b1118e6362632c76"),
    ("debian13", "container", None, None, "8bc7587fe4a5bc3f"),
    ("debian13", "kernel", None, None, "1b3bf62e8f4399b2"),
    ("debian13", "image", None, None, "9957672a69ca3156"),
    ("opensuse156", "container", None, None, "d9f755295c83a19c"),
    ("opensuse156", "kernel", None, None, "7264d6e86c697b5c"),
    ("opensuse156", "image", None, None, "38796d469bcbaea9"),
    # A variant must not perturb the base hashes above, and must differ
    # from them.
    ("rocky9", "container", None, "mofed-24", "cc1a03c2d0fda310"),
    ("rocky9", "image", None, "mofed-24", "68425d760796f4dd"),
    # An explicitly named kernel.
    ("rocky9", "kernel", "5.14-rhel9.5", None, "a0f7b639375312bb"),
    ("rocky9", "image", "5.14-rhel9.5", None, "801544844df67165"),
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
    assert tc.input_hash("kernel", extra=b"patchbytes") == "de148f25f7c9c125"
    assert tc.input_hash("kernel", extra=b"patchbytes") != tc.input_hash(
        "kernel"
    )


def test_shmrdma_sources_skip_mofed_images(monkeypatch) -> None:
    """A MOFED image never carries shmrdma.ko, so its sources are not an
    input of that image; they are of the base image."""
    from ltvm_pkg import shmrdma

    base = TargetConfig("rocky9")
    mofed = TargetConfig("rocky9", variant="mofed-24")
    before = (
        base.input_hash("image"),
        mofed.input_hash("image", variant="mofed-24"),
    )
    monkeypatch.setattr(shmrdma, "kmod_source_hash", lambda: b"edited")
    after = (
        base.input_hash("image"),
        mofed.input_hash("image", variant="mofed-24"),
    )
    assert before[0] != after[0]
    assert before[1] == after[1]
