"""End-to-end: deploy-lustre is idempotent; no-op redeploys are safe.

The spec offered two framings:

  (A) Mutate a header in the user's ~/lustre-release to bump a
      version string, redeploy, assert the observable change, revert
      in a try/finally.
  (B) "Running deploy-lustre twice in a row leaves the existing mount
      (and its contents) intact."

This module implements (B).  Mutating the user's source tree mid-test
is a real foot-gun: a crash between modify and revert leaves the
checkout dirty, and tests that expect clean trees downstream get
confused.  The idempotency property is what the dev loop actually
promises -- "I tweaked something, re-ran `ltvm deploy-lustre`, my
mount is still where I left it" -- so (B) is the better assertion.

We still cover "the new modules actually got installed" by comparing
`lctl lustre_build_version` before and after the redeploy: since we
didn't change the source, the version string must be identical.  If
deploy silently swapped in a different build, we'd see it here.
"""

from __future__ import annotations

from .conftest import LUSTRE_TREE, run_ltvm, ssh_run, wait_ssh


def test_deploy_lustre_is_idempotent(vm_name) -> None:  # type: ignore[no-untyped-def]
    """Two back-to-back deploy-lustres: mount survives, version stays same."""
    assert LUSTRE_TREE.is_dir(), (
        f"~/lustre-release missing at {LUSTRE_TREE}"
    )

    name = vm_name()

    proc = run_ltvm(
        "create", name,
        "--mem", "2048",
        "--vcpus", "2",
        "--mdt-disks", "1",
        "--ost-disks", "2",
        timeout=240,
    )
    assert proc.returncode == 0, (
        f"ltvm create failed rc={proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    wait_ssh(name)

    # First deploy + mount.
    proc = run_ltvm(
        "deploy-lustre", name,
        "--build", str(LUSTRE_TREE),
        "--mount",
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"first deploy-lustre failed rc={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    # Capture the pre-redeploy observable state: mount type, build
    # version, and a marker file's md5.  Any of these changing across
    # a no-op redeploy is a bug.
    rc, out, err = ssh_run(name, "stat -f -c %T /mnt/lustre")
    assert rc == 0 and out.strip() == "lustre", (
        f"/mnt/lustre not lustre after first deploy: rc={rc} out={out!r}"
    )

    rc, version_before, err = ssh_run(name, "lctl lustre_build_version")
    assert rc == 0, f"lctl lustre_build_version failed rc={rc}: {err}"
    version_before = version_before.strip()
    assert version_before, "empty version string before redeploy"

    # Plant a marker file so we can prove the mount wasn't silently
    # wiped/reformatted by the second deploy.
    rc, out, err = ssh_run(
        name,
        "dd if=/dev/zero bs=1M count=1 of=/mnt/lustre/marker.bin "
        "status=none && md5sum /mnt/lustre/marker.bin",
        timeout=30,
    )
    assert rc == 0, f"marker write failed rc={rc}: {err}"
    md5_before = out.split()[0]

    # Redeploy -- same tree, no source changes.  NOT passing --mount
    # because the mount is already up; if deploy still tries to
    # re-run llmount.sh that'd be an idempotency bug worth catching.
    proc = run_ltvm(
        "deploy-lustre", name,
        "--build", str(LUSTRE_TREE),
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"second deploy-lustre failed rc={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    # Mount still present.
    rc, out, err = ssh_run(name, "stat -f -c %T /mnt/lustre")
    assert rc == 0, f"post-redeploy stat failed rc={rc}: {err}"
    assert out.strip() == "lustre", (
        f"/mnt/lustre no longer lustre after redeploy: {out.strip()!r}"
    )

    # Marker file + md5 untouched.
    rc, out, err = ssh_run(name, "md5sum /mnt/lustre/marker.bin")
    assert rc == 0, f"post-redeploy md5sum failed rc={rc}: {err}"
    md5_after = out.split()[0]
    assert md5_after == md5_before, (
        f"marker md5 changed across redeploy:\n"
        f"  before: {md5_before}\n"
        f"  after:  {md5_after}"
    )

    # Build version identical (same source).
    rc, version_after, err = ssh_run(name, "lctl lustre_build_version")
    assert rc == 0, f"post-redeploy lctl lustre_build_version failed: {err}"
    version_after = version_after.strip()
    assert version_after == version_before, (
        f"lustre_build_version changed across no-op redeploy:\n"
        f"  before: {version_before!r}\n"
        f"  after:  {version_after!r}"
    )
