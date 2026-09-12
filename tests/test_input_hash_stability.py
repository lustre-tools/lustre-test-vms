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
    ("rocky8", "container", None, None, "4220fc23dca0f512"),
    ("rocky8", "kernel", None, None, "2fc26cb838a21c93"),
    ("rocky8", "image", None, None, "eb25bed2b8d736cb"),
    ("rocky9", "container", None, None, "53ff92f0c29a2c1a"),
    ("rocky9", "kernel", None, None, "54e1f16b54b180b3"),
    ("rocky9", "image", None, None, "446a4ffd0b5a7c9d"),
    ("rocky9-64k", "container", None, None, "2b44594e1b246688"),
    ("rocky9-64k", "kernel", None, None, "3650a25517ffed13"),
    ("rocky9-64k", "image", None, None, "3ff60ca267f08b89"),
    ("rocky10", "container", None, None, "a8b1c88bcad5e635"),
    ("rocky10", "kernel", None, None, "d12f8b2e1233413a"),
    ("rocky10", "image", None, None, "e6c2f40a0588fc5e"),
    ("mainline", "container", None, None, "3e5f339a82347536"),
    ("mainline", "kernel", None, None, "8a48d68aa12f941b"),
    ("mainline", "image", None, None, "1a9b6b740f679ab7"),
    ("ubuntu2404", "container", None, None, "eebd5c4d9d582ce5"),
    ("ubuntu2404", "kernel", None, None, "955f58eda24d55d4"),
    ("ubuntu2404", "image", None, None, "484dc30417ae8725"),
    # A variant must not perturb the base hashes above, and must differ
    # from them.
    ("rocky9", "container", None, "mofed-24", "388eb197f85522fe"),
    ("rocky9", "image", None, "mofed-24", "c3899bb4fe5bb76a"),
    # An explicitly named kernel.
    ("rocky9", "kernel", "5.14-rhel9.5", None, "da5d0ef496a45b25"),
    ("rocky9", "image", "5.14-rhel9.5", None, "c40338473697a3a4"),
]


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
    assert tc.input_hash("kernel", extra=b"patchbytes") == "5cddb9cc7956a553"
    assert tc.input_hash("kernel", extra=b"patchbytes") != tc.input_hash(
        "kernel"
    )
