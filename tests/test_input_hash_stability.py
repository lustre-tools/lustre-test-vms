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
    ("rocky8", "container", None, None, "9e11ed638f52633a"),
    ("rocky8", "kernel", None, None, "e843357a885eb539"),
    ("rocky8", "image", None, None, "967600d4671b3ec2"),
    ("rocky9", "container", None, None, "3e3483d4f53cdd18"),
    ("rocky9", "kernel", None, None, "1485949f7a6473db"),
    ("rocky9", "image", None, None, "d7a07fada94d9c89"),
    ("rocky9-64k", "container", None, None, "07ce85d20cd087d2"),
    ("rocky9-64k", "kernel", None, None, "4245925c36d098eb"),
    ("rocky9-64k", "image", None, None, "c2601dec8babe1f8"),
    ("rocky10", "container", None, None, "b290e5e6638f8755"),
    ("rocky10", "kernel", None, None, "1037d062e354c6b6"),
    ("rocky10", "image", None, None, "8852957af5cb682e"),
    ("mainline", "container", None, None, "969d2693b0a5b00a"),
    ("mainline", "kernel", None, None, "5a30ae513ab3098a"),
    ("mainline", "image", None, None, "79f7e0927adc1585"),
    ("ubuntu2404", "container", None, None, "45d60623743a35ca"),
    ("ubuntu2404", "kernel", None, None, "dc8fdebadd7c2925"),
    ("ubuntu2404", "image", None, None, "227881da2e0c18c7"),
    # A variant must not perturb the base hashes above, and must differ
    # from them.
    ("rocky9", "container", None, "mofed-24", "23a82735caa4c736"),
    ("rocky9", "image", None, "mofed-24", "0dc548ea2642e0bf"),
    # An explicitly named kernel.
    ("rocky9", "kernel", "5.14-rhel9.5", None, "d8a7a2972f902189"),
    ("rocky9", "image", "5.14-rhel9.5", None, "f47482b297d701ac"),
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
    assert tc.input_hash("kernel", extra=b"patchbytes") == "f55467f5dd11eafd"
    assert tc.input_hash("kernel", extra=b"patchbytes") != tc.input_hash(
        "kernel"
    )


class TestKernelsAvailableIsNotFoldedIn:
    """Adding a kernel minor must invalidate nothing.

    ``kernels.available`` and ``kernels.default`` used to be hashed
    into every artifact via the whole-``kernels`` blob, so the
    documented routine operation -- "for a new kernel minor on an
    existing OS, just add the short name to kernels.available" --
    rebuilt that target's container, every kernel and every image on
    every machine, for a list none of them read.
    """

    def _with_extra_minor(self, target: str) -> TargetConfig:
        import copy

        tc = TargetConfig(target)
        tc._data = copy.deepcopy(tc._data)
        tc._kernels = tc._data["kernels"]
        tc._kernels["available"].append("5.14-rhel9.99")
        return tc

    @pytest.mark.parametrize("artifact", ["container", "kernel", "image"])
    def test_adding_a_minor_changes_nothing(self, artifact: str) -> None:
        before = TargetConfig("rocky9").input_hash(
            artifact, kernel="5.14-rhel9.5"
        )
        after = self._with_extra_minor("rocky9").input_hash(
            artifact, kernel="5.14-rhel9.5"
        )
        assert before == after

    def test_a_kernels_own_entry_still_invalidates_it(self) -> None:
        """rocky10 pins srpm_version per kernel in a mapping entry.

        That is real build input, so it has to keep invalidating its
        own kernel -- and only its own, which the whole-blob hash could
        not express.
        """
        import copy

        tc = TargetConfig("rocky10")
        names = [
            e if isinstance(e, str) else e["name"]
            for e in tc._kernels["available"]
        ]
        pinned = [
            e["name"]
            for e in tc._kernels["available"]
            if isinstance(e, dict) and "srpm_version" in e
        ]
        assert pinned, "rocky10 no longer pins an srpm_version"
        before = {n: tc.input_hash("kernel", kernel=n) for n in names}

        tc2 = TargetConfig("rocky10")
        tc2._data = copy.deepcopy(tc2._data)
        tc2._kernels = tc2._data["kernels"]
        for e in tc2._kernels["available"]:
            if isinstance(e, dict) and "srpm_version" in e:
                e["srpm_version"] = "9.9.9-bogus.el10_0"
        for n in names:
            after = tc2.input_hash("kernel", kernel=n)
            if n in pinned:
                assert after != before[n], n
            else:
                assert after == before[n], n

    def test_kernel_config_overrides_still_fold_in(self) -> None:
        """kernels.config is read by the kernel build, so it stays."""
        import copy

        tc = TargetConfig("rocky9")
        before = tc.input_hash("kernel", kernel="5.14-rhel9.5")
        tc2 = TargetConfig("rocky9")
        tc2._data = copy.deepcopy(tc2._data)
        tc2._kernels = tc2._data["kernels"]
        tc2._kernels.setdefault("config", {})["CONFIG_LTVM_TEST"] = "y"
        assert tc2.input_hash("kernel", kernel="5.14-rhel9.5") != before
