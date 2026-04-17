"""End-to-end: deploy-lustre + llmount + file I/O round-trip.

The slide-7 golden flow.  Creates a fresh VM with one MDT and two
OSTs, runs `ltvm deploy-lustre --build ~/lustre-release --mount`,
writes a file, cycles the mount, and asserts the file survives.

Implementation note on the round-trip assertion:
  `ltvm llmount` without --cleanup invokes the upstream llmount.sh,
  which defaults to running `formatall` before mounting -- it's a
  test-framework helper, not a user's mount command.  That formats
  the OST/MDT backing stores, so a naive mount-write-unmount-mount
  cycle loses the file every time.  For the "data survives" check
  we remount by hand over ssh with NOFORMAT=1, which is what
  llmount.sh itself honours (lustre/tests/llmount.sh line ~74).
  This keeps the test asserting the real user-visible property (a
  Lustre remount preserves data) rather than a quirk of llmount.sh.
"""

from __future__ import annotations

from .conftest import LUSTRE_TREE, run_ltvm, ssh_run, wait_ssh


def test_lustre_deploy_mount_write_remount(vm_name) -> None:  # type: ignore[no-untyped-def]
    """`ltvm deploy-lustre --mount` -> write -> remount -> md5 matches.

    Steps:
      1. Create fresh e2e VM (mdt=1 ost=2, small mem).
      2. `sudo ltvm deploy-lustre <vm> --build ~/lustre-release --mount`.
      3. ssh in: assert /mnt/lustre is a Lustre mount point, write a
         deterministic 4 MiB file, capture its md5.
      4. `sudo ltvm llmount <vm> --cleanup` -- full unmount.
      5. Remount via `NOFORMAT=1 bash llmount.sh` over ssh (see module
         docstring).
      6. Re-read the file and assert the md5 is unchanged.
    """
    # Fail fast with a clear reason if the user's Lustre tree is missing;
    # deploy-lustre's own error is less obvious from pytest output.
    assert LUSTRE_TREE.is_dir(), (
        f"~/lustre-release missing at {LUSTRE_TREE} -- "
        f"test_04 needs a Lustre source checkout to deploy from"
    )

    name = vm_name()

    # Small-enough VM to coexist with other dev VMs.  mdt=1 ost=2 is
    # the minimum that mounts cleanly; llmount.sh won't bring up
    # lustre without at least one OST.
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

    # Deploy + mount.  Allow a generous timeout: a cold lustre-build
    # against a stale .ltvm-staging takes ~60-90s on this host, and
    # the --mount step runs llmount.sh which does its own formatall.
    proc = run_ltvm(
        "deploy-lustre", name,
        "--build", str(LUSTRE_TREE),
        "--mount",
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"ltvm deploy-lustre failed rc={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    # Verify /mnt/lustre is actually a Lustre mount.  `stat -f -c %T`
    # returns the fs type string; for Lustre it's literally 'lustre'.
    rc, out, err = ssh_run(name, "stat -f -c %T /mnt/lustre")
    assert rc == 0, f"stat -f /mnt/lustre failed rc={rc}: {err}"
    assert out.strip() == "lustre", (
        f"/mnt/lustre is not a lustre mount; got {out.strip()!r}"
    )

    # Write a deterministic 4 MiB file and capture its md5.  Using
    # /dev/zero so the hash is fixed for repeatable debugging; the
    # test cares about round-trip integrity, not content entropy.
    rc, out, err = ssh_run(
        name,
        "dd if=/dev/zero bs=1M count=4 of=/mnt/lustre/test.bin "
        "status=none && md5sum /mnt/lustre/test.bin",
        timeout=60,
    )
    assert rc == 0, f"dd + md5sum failed rc={rc}: {err}"
    md5_before = out.split()[0]
    assert len(md5_before) == 32, (
        f"bad md5 output: {out!r}"
    )

    # Unmount via the user-facing `ltvm llmount --cleanup`.
    proc = run_ltvm("llmount", name, "--cleanup", timeout=120)
    assert proc.returncode == 0, (
        f"ltvm llmount --cleanup failed rc={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    # Confirm the mount really is gone so the next step proves the
    # remount, not a stale mount table.
    rc, out, _ = ssh_run(name, "mount | awk '$3==\"/mnt/lustre\"{print}'")
    assert rc == 0
    assert out.strip() == "", (
        f"/mnt/lustre still mounted after --cleanup:\n{out}"
    )

    # Remount WITHOUT reformat.  llmount.sh honours NOFORMAT=1 (it
    # skips the formatall at its top), so the backing OSTs/MDT keep
    # their data.  We talk to llmount.sh directly -- `ltvm llmount`
    # has no knob for this today; see module docstring.
    remount_cmd = (
        "cd /usr/lib64/lustre/tests && "
        "NOFORMAT=1 LUSTRE=/usr/lib64/lustre bash llmount.sh"
    )
    rc, out, err = ssh_run(name, remount_cmd, timeout=180)
    assert rc == 0, (
        f"NOFORMAT=1 llmount.sh failed rc={rc}\n"
        f"stdout:\n{out}\nstderr:\n{err}"
    )

    # Lustre should be remounted.
    rc, out, err = ssh_run(name, "stat -f -c %T /mnt/lustre")
    assert rc == 0, f"post-remount stat failed: {err}"
    assert out.strip() == "lustre", (
        f"/mnt/lustre not lustre after remount; got {out.strip()!r}"
    )

    # The file + its md5 must be intact.
    rc, out, err = ssh_run(name, "md5sum /mnt/lustre/test.bin", timeout=30)
    assert rc == 0, f"post-remount md5sum failed rc={rc}: {err}"
    md5_after = out.split()[0]
    assert md5_after == md5_before, (
        f"md5 changed across remount:\n"
        f"  before: {md5_before}\n"
        f"  after:  {md5_after}"
    )
