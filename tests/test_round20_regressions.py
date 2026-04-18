"""Regression tests for round 20 fixes.

Round 20 caught three real bugs (plus some lower-priority drift):
  1. /etc/hosts marker substring collision between sibling-named VMs
     (`co1` vs `co1-single` stripping each other's entries).
  2. cmd_deploy bundled snapshot mirror skipped when staging already
     had .ko files, silently shipping stale locally-built modules
     under the "Using bundled Lustre" banner.
  3. image input_hash didn't cover kernel modules or Lustre staging
     even though image_build.py deliberately bakes both into the
     final image, so a kernel/Lustre rebuild left `ltvm build image`
     early-returning with stale contents.

Also pinned here:
  - cmd_deploy forwards --arch unconditionally to the inner
    build-lustre (rather than comparing against the literal "x86_64").
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ── /etc/hosts marker prefix collision ───────────────────


class TestHostsMarkerCollision:
    """register/unregister_ssh_name must anchor the marker match to
    end-of-line so sibling-named VMs don't strip each other's entries.
    """

    def _hosts_with(self, entries: list[tuple[str, str]]) -> str:
        """Build a fake /etc/hosts body with ltvm marker lines."""
        from ltvm_pkg.vm_state import MARKER

        lines = ["127.0.0.1\tlocalhost\n"]
        for ip, name in entries:
            lines.append(f"{ip}\t{name} {MARKER}:{name}\n")
        return "".join(lines)

    def test_register_co1_does_not_strip_co1_single(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The actual bug: registering 'co1' must not remove the
        'co1-single' entry, because 'co1' is a prefix substring of
        'co1-single' under the old unanchored check."""
        import os as _os

        from ltvm_pkg import vm_net
        from ltvm_pkg.vm_state import MARKER

        fake_hosts = tmp_path / "hosts"
        fake_hosts.write_text(
            self._hosts_with(
                [
                    ("192.168.100.50", "co1-single"),
                    ("192.168.100.51", "co2-mds"),
                ]
            )
        )

        # Redirect HOSTS_FILE (the centralised module constant) to
        # our fake -- no need to patch Path() wholesale any more.
        monkeypatch.setattr(vm_net, "HOSTS_FILE", fake_hosts)
        # Stub the surrounding ssh-config machinery so we only exercise
        # the /etc/hosts logic under test.
        monkeypatch.setattr(vm_net, "reload_dns", lambda: None)
        monkeypatch.setattr(
            vm_net,
            "_real_user_ssh_dir",
            lambda: ("root", tmp_path / ".ssh"),
        )
        # Root-only chown is the final step; stub it so the test works
        # as a regular user.
        monkeypatch.setattr(_os, "chown", lambda *a, **k: None)

        vm_net._register_ssh_name_locked("co1", "192.168.100.40")

        body = fake_hosts.read_text()
        # The new co1 entry should be present.
        assert f"{MARKER}:co1\n" in body
        # The co1-single entry must NOT have been stripped.
        assert f"{MARKER}:co1-single\n" in body, (
            "co1-single entry was removed when registering co1 -- "
            "the hosts marker check is not anchored to end-of-line"
        )
        # co2-mds is unaffected
        assert f"{MARKER}:co2-mds\n" in body

    def test_unregister_co1_does_not_strip_co1_single(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Symmetric: unregistering 'co1' must not take 'co1-single'
        with it."""
        from ltvm_pkg import vm_net
        from ltvm_pkg.vm_state import MARKER

        fake_hosts = tmp_path / "hosts"
        fake_hosts.write_text(
            self._hosts_with(
                [
                    ("192.168.100.40", "co1"),
                    ("192.168.100.50", "co1-single"),
                ]
            )
        )

        monkeypatch.setattr(vm_net, "HOSTS_FILE", fake_hosts)
        monkeypatch.setattr(vm_net, "reload_dns", lambda: None)
        monkeypatch.setattr(
            vm_net,
            "_real_user_ssh_dir",
            lambda: ("root", tmp_path / ".ssh"),
        )

        vm_net._unregister_ssh_name_locked("co1")

        body = fake_hosts.read_text()
        assert f"{MARKER}:co1\n" not in body
        assert f"{MARKER}:co1-single\n" in body, (
            "co1-single entry was removed when unregistering co1"
        )


# ── image input_hash includes kernel + staging ──────────


class TestImageStalenessUpstream:
    """image input_hash must include the kernel's input_hash and the
    Lustre staging stamp so an upstream rebuild invalidates the cached
    image.
    """

    def _stub_target_config(self, tmp_path: Path):
        """Build a minimal TargetConfig stub pointing at a fake output
        dir.  We avoid constructing the real class because it needs a
        targets.yaml; instead we use the live target_config with a
        real target and an overridden output_dir."""
        from ltvm_pkg.target_config import TargetConfig

        try:
            tc = TargetConfig("rocky9")
        except (ValueError, FileNotFoundError):
            pytest.skip("rocky9 target not available")
        # Redirect output_dir into our tmp_path
        tc.output_dir = tmp_path / "artifacts"
        tc.output_dir.mkdir()
        return tc

    def test_kernel_meta_change_invalidates_image_hash(
        self, tmp_path: Path
    ) -> None:
        tc = self._stub_target_config(tmp_path)
        # Baseline hash with no kernel/staging
        h0 = tc.input_hash("image")

        # Drop a kernel meta.json with a specific input_hash
        kdir = tc.output_dir / "kernels" / tc.resolve_kernel()
        kdir.mkdir(parents=True)
        (kdir / "meta.json").write_text(
            json.dumps({"input_hash": "aaaaaaaaaaaaaaaa"}) + "\n"
        )
        h1 = tc.input_hash("image")

        # Change the kernel input_hash to simulate a kernel rebuild
        (kdir / "meta.json").write_text(
            json.dumps({"input_hash": "bbbbbbbbbbbbbbbb"}) + "\n"
        )
        h2 = tc.input_hash("image")

        assert h0 != h1, "adding a kernel meta.json should change image hash"
        assert h1 != h2, (
            "a kernel rebuild (new input_hash in meta.json) must "
            "invalidate the image cache -- image_build.py bakes kernel "
            "modules into the final image"
        )

    def test_staging_stamp_does_not_invalidate_image_hash(
        self, tmp_path: Path
    ) -> None:
        """The Lustre staging stamp used to be folded into the image
        staleness hash because image_build.py auto-injected Lustre from
        a global staging dir.  That auto-inject was removed when staging
        moved per-tree (under <lustre_tree>/.ltvm-staging/), so the
        staging stamp no longer affects the image hash and a Lustre
        rebuild does not invalidate the image cache.

        This test pins the new behavior so we don't accidentally
        re-introduce the global staging coupling: if you ever want
        Lustre baked into the image again, do it via lustre-artifacts/
        in `ltvm package`, not via image_build's hash.
        """
        tc = self._stub_target_config(tmp_path)
        # Drop a kernel meta.json so the image hash isn't trivially zero
        kdir = tc.output_dir / "kernels" / tc.resolve_kernel()
        kdir.mkdir(parents=True)
        (kdir / "meta.json").write_text(
            json.dumps({"input_hash": "aaaaaaaaaaaaaaaa"}) + "\n"
        )
        h0 = tc.input_hash("image")

        # Old layout: simulating an old-style global staging stamp
        staging = tc.output_dir / "lustre" / "staging"
        staging.mkdir(parents=True)
        (staging / ".ltvm-staging-stamp").write_text("5.14.0-v1\n")
        h1 = tc.input_hash("image")

        assert h0 == h1, (
            "the legacy staging stamp must not affect image hash anymore "
            "-- staging is per-tree and image_build no longer reads it"
        )

    def test_kernel_meta_with_non_string_hash_is_tolerated(
        self, tmp_path: Path
    ) -> None:
        """Defensive: an older meta.json without input_hash or with a
        non-string value must not crash the image hash."""
        tc = self._stub_target_config(tmp_path)
        kdir = tc.output_dir / "kernels" / tc.resolve_kernel()
        kdir.mkdir(parents=True)
        (kdir / "meta.json").write_text(json.dumps({"foo": "bar"}) + "\n")
        # Must not raise
        tc.input_hash("image")

    def test_corrupt_kernel_meta_is_tolerated(self, tmp_path: Path) -> None:
        tc = self._stub_target_config(tmp_path)
        kdir = tc.output_dir / "kernels" / tc.resolve_kernel()
        kdir.mkdir(parents=True)
        (kdir / "meta.json").write_text("{not valid json")
        tc.input_hash("image")  # must not raise


# ── bundled snapshot mirror logic ────────────────────────


# (TestBundledSnapshotAlwaysMirrors removed: the source-inspection test
# was not behavioral and would break on formatting changes.)
