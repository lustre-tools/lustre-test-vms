# ltvm System Test Plan

End-to-end tests for the ltvm VM tooling. Run after a `build-all` or artifact
fetch to validate the full stack: artifact build → VM creation → Lustre
deployment → Lustre operation → crash/kdump.

**Targets** (in run order): rocky9 → rocky8 → rocky10 → ubuntu2404

**Server support**: rocky9 ✓  rocky8 ✗  rocky10 ✗  ubuntu2404 ✗

`ubuntu2404` is `experimental`.
Tests marked `(S)` require server support — N/A for client-only targets.
Tests marked `(C)` are client-only targets only.

---

## Test Matrix

| # | Phase | Test | S/C/A | rocky9 | rocky8 | rocky10 | ubuntu2404 |
|---|-------|------|-------|--------|--------|---------|------------|
| 0.1 | Prerequisites | `build-status` clean | A | PASS | PASS | PASS | |
| 0.2 | Prerequisites | `ltvm doctor` no orphans / stale TAP | A | PASS | PASS | PASS | |
| 0.3 | Prerequisites | Host bridge + DNS reachable from VM | A | PASS | PASS | PASS | |
| 1.1 | VM Creation | Default: 2 vCPU, 2 GiB, 1 MDT, 2 OST, 500 MiB | A | PASS | PASS | PASS | |
| 1.2 | VM Creation | `ensure` on running VM: exit 0, no second process | A | PASS | PASS | PASS | |
| 1.3 | VM Creation | `--disk-size` override reflected in `lsblk` | A | PASS | PASS | PASS | |
| 1.4 | VM Creation | `list --json` all expected fields non-null | A | PASS | PASS | PASS | |
| 1.5 | VM Creation | Overlay is qcow2 with backing file (not flat copy) | A | PASS | PASS | PASS | |
| 1.6 | VM Creation | Root fs resized to 8 GiB on first boot | A | PASS | PASS | PASS | |
| 2.1 | Disk Topology | 1 MDT, 1 OST — disks appear in VM | S | PASS | N/A | N/A | N/A |
| 2.2 | Disk Topology | 1 MDT, 2 OST (default) | S | PASS | N/A | N/A | N/A |
| 2.3 | Disk Topology | 1 MDT, 4 OST | S | PASS | N/A | N/A | N/A |
| 2.4 | Disk Topology | 2 MDT, 2 OST | S | PASS | N/A | N/A | N/A |
| 2.5 | Disk Topology | Client only (0/0) — no `/dev/vdb` | A | PASS | PASS | PASS | |
| 2.6 | Disk Topology | Disk ordering: vdb=MDT, vdc/vdd=OSTs | S | PASS | N/A | N/A | N/A |
| 3.1 | Lustre Boot | Single-node format + mount, real block devices | S | PASS | N/A | N/A | N/A |
| 3.2 | Lustre Boot | `lctl dl` shows MGS + MDT + OSTs all UP | S | PASS | N/A | N/A | N/A |
| 3.3 | Lustre Boot | `mntdev` shows `/dev/vd*`, not tmpfs/loop | S | PASS | N/A | N/A | N/A |
| 3.4 | Lustre Boot | `lfs df` reports capacity from all OSTs | S | PASS | N/A | N/A | N/A |
| 3.5 | Lustre Boot | `lsmod` lists `lustre`, `ldiskfs`, `lnet` | S | PASS | N/A | N/A | N/A |
| 3.6 | Lustre Boot | dmesg confirms dm-flakey active on data disks | S | PASS | N/A | N/A | N/A |
| 3.7 | Lustre Boot | `/proc/fs/lustre/` exists and non-empty | S | PASS | N/A | N/A | N/A |
| 4.1 | Deploy | Fresh deploy + mount on new VM | S | PASS | N/A | N/A | N/A |
| 4.2 | Deploy | Re-deploy to same VM (idempotency) | S | PASS | N/A | N/A | N/A |
| 4.3 | Deploy | 4-OST VM: all 4 OSTs visible in `lfs df` | S | PASS | N/A | N/A | N/A |
| 4.4 | Deploy | Deploy without `--mount`, then manual `llmount.sh` | S | PASS | N/A | N/A | N/A |
| 4.5 | Deploy | Re-deploy after unclean unload (wedged modules) | S | PASS | N/A | N/A | N/A |
| 4.6 | Deploy | `last_deploy` timestamp updated after deploy | S | PASS | N/A | N/A | N/A |
| 5.1 | Sanity | Tests 1, 2, 4 pass | S | PASS | N/A | N/A | N/A |
| 5.2 | Sanity | Test 17 | S | PASS | N/A | N/A | N/A |
| 5.3 | Sanity | No kernel oops in dmesg post-test | S | PASS | N/A | N/A | N/A |
| 5.4 | Sanity | Test 36 (cross-directory rename) | S | PASS | N/A | N/A | N/A |
| 5.5 | Sanity | No unexpected errors in `llite/*/stats` | S | PASS | N/A | N/A | N/A |
| 5.6 | Sanity | No Lustre assertion failures | S | PASS | N/A | N/A | N/A |
| 6.1 | Crash/kdump | NMI triggers panic + reboot | A | PASS | PASS | PASS | |
| 6.2 | Crash/kdump | vmcore present and > 1 MiB in `/var/crash/` | A | PASS | PASS | PASS | |
| 6.3 | Crash/kdump | `crash-tool` / `lustre_triage.py` parses vmcore | A | PASS | PASS | PASS | |
| 6.4 | Crash/kdump | `kdump.service` active after reboot | A | PASS | PASS | PASS | |
| 6.5 | Crash/kdump | vmlinux build-id matches running kernel | A | PASS | PASS | PASS | |
| 6.6 | Crash/kdump | `crash-collect --trigger` (sysrq path) works | A | PASS | PASS | PASS | |
| 7.1 | Multi-node | Cluster create + deploy + mount | S | | N/A | N/A | N/A |
| 7.2 | Multi-node | `lfs df` shows all OSTs across nodes | S | | N/A | N/A | N/A |
| 7.3 | Multi-node | `local.sh` correct MGSNID/MDSDEV/OSTDEV per node | S | | N/A | N/A | N/A |
| 7.4 | Multi-node | Client-only node mounts and sees all OSTs | S | | N/A | N/A | N/A |
| 7.5 | Multi-node | `cluster status` correct per-node state | S | | N/A | N/A | N/A |
| 7.6 | Multi-node | Sanity test 1 passes against multi-node cluster | S | | N/A | N/A | N/A |
| 8.1 | Client Build | Build with `--disable-server` (no ldiskfs/obdfilter) | C | N/A | PASS | PASS | |
| 8.2 | Client Build | `lsmod` shows client modules, not server modules | C | N/A | PASS | PASS | |
| 8.3 | Client Build | Client VM mounts a rocky9 server's Lustre over TCP | C | N/A | PASS | PASS | |
| 8.4 | Client Build | `lfs df` on client shows server OSTs | C | N/A | PASS | PASS | |
| 8.5 | Client Build | Write + read-back + checksum from client | C | N/A | PASS | PASS | |
| 9.1 | Snapshot | Snapshot, corrupt state, restore, Lustre remounts | S | | N/A | N/A | N/A |
| 9.2 | Snapshot | Restore to non-existent tag exits non-zero | A | | PASS | PASS | |
| 9.3 | Snapshot | Snapshot while running: VM stopped + restarted | A | | PASS | PASS | |
| 10.1 | Correctness | Write file, read back, md5sum matches | S | | N/A | N/A | N/A |
| 10.2 | Correctness | `lfs setstripe -c 2` uses 2 OSTs on 4-OST VM | S | | N/A | N/A | N/A |
| 10.3 | Correctness | Extended attributes round-trip | S | | N/A | N/A | N/A |
| 11.1 | CLI Errors | `exec` on stopped VM exits `EXIT_UNREACHABLE` | A | | PASS | PASS | |
| 11.2 | CLI Errors | `exec` timeout exits `EXIT_TIMEOUT` | A | | PASS | PASS | |
| 11.3 | CLI Errors | `destroy` on non-existent VM exits 0 | A | | PASS | PASS | |
| 11.4 | CLI Errors | `nmi` on stopped VM exits non-zero + clear error | A | | PASS | PASS | |
| 11.5 | CLI Errors | `doctor --fix` cleans up orphan overlays | A | | PASS | PASS | |

---

## Run Order

Work one target at a time, completing all applicable tests before moving to the next.

1. **rocky9** — server + client, all phases ✓ complete
2. **rocky8** — client-only, all applicable phases ✓ complete
3. **rocky10** — client-only, all applicable phases ✓ complete
4. **ubuntu2404** — client-only, same as rocky10 (status: `experimental`)

---

## Commands

### Phase 0: Prerequisites

```bash
ltvm build-status
sudo ltvm list
sudo vm.py doctor
```

### Phase 1: VM Creation — Default Behavior

```bash
sudo ltvm ensure co1-default
sudo ltvm list --json   # verify: vcpus=2, mem=2048, mdt_disks=1, ost_disks=2, disk=500MiB
# 1.2: ensure on running VM
sudo ltvm ensure co1-default   # should print "already running", exit 0
# 1.6: root fs size
sudo ltvm exec co1-default 'df -h /'
sudo ltvm destroy co1-default
```

### Phase 2: Disk Topology Variations

```bash
sudo ltvm ensure co1-t1 --mdt-disks 1 --ost-disks 1
sudo ltvm ensure co1-t3 --ost-disks 4
sudo ltvm ensure co1-t4 --mdt-disks 2 --ost-disks 2
sudo ltvm ensure co1-t5 --mdt-disks 0 --ost-disks 0

for vm in co1-t1 co1-t3 co1-t4 co1-t5; do
    echo "=== $vm ===" && sudo ltvm exec $vm 'lsblk'
done

sudo ltvm destroy co1-t1 co1-t3 co1-t4 co1-t5
```

### Phase 3: Lustre — Single-node Boot and Format

```bash
sudo ltvm ensure co1-single
sudo ltvm deploy co1-single --build ~/lustre-release --mount
sudo ltvm exec co1-single 'lctl dl'
sudo ltvm exec co1-single 'lctl get_param osd-*.*.mntdev'
sudo ltvm exec co1-single 'lfs df /mnt/lustre'
sudo ltvm exec co1-single 'lsblk'
sudo ltvm exec co1-single 'lsmod | grep -E "lustre|ldiskfs|lnet"'
sudo ltvm exec co1-single 'ls /proc/fs/lustre/'
```

### Phase 4: Deployment Variations

```bash
# 4.1. Fresh deploy on new VM
sudo ltvm ensure co1-deploy
sudo ltvm deploy co1-deploy --build ~/lustre-release --mount
sudo ltvm exec co1-deploy 'lctl dl'
sudo ltvm exec co1-deploy 'lfs df /mnt/lustre'

# 4.2. Idempotency: re-deploy to same VM
sudo ltvm deploy co1-deploy --build ~/lustre-release --mount
sudo ltvm exec co1-deploy 'lctl dl'

# 4.3. 4-OST VM
sudo ltvm ensure co1-4ost --ost-disks 4
sudo ltvm deploy co1-4ost --build ~/lustre-release --mount
sudo ltvm exec co1-4ost 'lfs df /mnt/lustre'

# 4.4. Deploy without --mount, then mount manually
sudo ltvm ensure co1-nomount
sudo ltvm deploy co1-nomount --build ~/lustre-release
sudo ltvm exec co1-nomount 'lctl dl | grep -c UP'
sudo ltvm exec co1-nomount 'bash lustre/tests/llmount.sh'
sudo ltvm exec co1-nomount 'lctl dl'

sudo ltvm destroy co1-deploy co1-4ost co1-nomount
```

### Phase 5: Sanity Tests

```bash
sudo ltvm exec --timeout 300 co1-single \
    'sudo -E ONLY="1 2 4 17 36" bash /usr/lib64/lustre/tests/sanity.sh'
sudo ltvm dmesg co1-single | grep -iE 'BUG|Oops|assert'
sudo ltvm exec co1-single 'lctl get_param *.*.assertion_failed 2>/dev/null'
sudo ltvm exec co1-single 'cat /proc/fs/lustre/llite/*/stats | grep -v " 0 samples"'
```

### Phase 6: Crash / kdump

```bash
sudo ltvm nmi co1-single
# Wait ~30s for reboot
sudo ltvm crash-collect co1-single --mod-dir ~/lustre-release
# Verify kdump re-armed
sudo ltvm exec co1-single 'systemctl is-active kdump'
```

### Phase 7: Multi-node Cluster (optional)

```bash
sudo ltvm cluster create co1 mgs+mds:co1-mds:1 oss:co1-oss:3
sudo ltvm cluster deploy co1 --build ~/lustre-release --mount
sudo ltvm cluster exec co1 oss 'lctl dl'
sudo ltvm cluster exec co1 mds 'lfs df /mnt/lustre'
sudo ltvm cluster destroy co1
```

### Phase 8: Client Build (non-server targets)

Client VM mounts a Lustre filesystem served by a rocky9 server VM.

```bash
# Server: rocky9 (already tested in phase 3)
sudo ltvm ensure co1-server
sudo ltvm deploy co1-server --build ~/lustre-release --mount
SERVER_IP=$(sudo ltvm list --json | python3 -c \
    "import sys,json; [print(v['ip']) for v in json.load(sys.stdin)['vms'] \
    if v['name']=='co1-server']")

# Client VM (target OS, no disks)
sudo ltvm ensure co1-client --os <target> --mdt-disks 0 --ost-disks 0
# Deploy client-only modules and mount
sudo ltvm exec co1-client "mount -t lustre ${SERVER_IP}@tcp:/lustre /mnt/lustre"
sudo ltvm exec co1-client 'lsmod | grep -E "lustre|lnet"'
sudo ltvm exec co1-client 'lsmod | grep -E "ldiskfs|obdfilter"'  # should be absent
sudo ltvm exec co1-client 'lfs df /mnt/lustre'
sudo ltvm exec co1-client \
    'dd if=/dev/urandom of=/mnt/lustre/test.dat bs=1M count=10 && \
     md5sum /mnt/lustre/test.dat | tee /tmp/orig.md5 && \
     md5sum /mnt/lustre/test.dat && diff /tmp/orig.md5 -'

sudo ltvm destroy co1-server co1-client
```

### Phase 9: Snapshot / Restore

```bash
sudo ltvm snapshot co1-single before-test
sudo ltvm exec co1-single 'sudo bash lustre/tests/llmountcleanup.sh && sudo lustre_rmmod'
sudo ltvm restore co1-single before-test
sudo ltvm exec co1-single 'lctl dl'
# Error path
sudo ltvm restore co1-single no-such-tag   # expect non-zero exit
```

### Phase 10: Correctness

```bash
sudo ltvm exec co1-single \
    'dd if=/dev/urandom of=/mnt/lustre/test.dat bs=1M count=50 && \
     md5sum /mnt/lustre/test.dat | tee /tmp/orig.md5 && \
     md5sum /mnt/lustre/test.dat && diff /tmp/orig.md5 -'
sudo ltvm exec co1-single \
    'touch /mnt/lustre/xattr-test && \
     setfattr -n user.foo -v bar /mnt/lustre/xattr-test && \
     getfattr -n user.foo /mnt/lustre/xattr-test | grep bar'
# Striping (4-OST VM)
sudo ltvm exec co1-4ost \
    'lfs setstripe -c 2 /mnt/lustre/striped.dat && \
     dd if=/dev/zero of=/mnt/lustre/striped.dat bs=1M count=10 && \
     lfs getstripe /mnt/lustre/striped.dat'
```

### Phase 11: CLI Error Paths

```bash
sudo ltvm stop co1-single
sudo ltvm exec co1-single 'echo hi'         # expect EXIT_UNREACHABLE
sudo ltvm nmi co1-single                    # expect non-zero + clear error
sudo ltvm destroy co1-nonexistent           # expect exit 0
sudo ltvm start co1-single
sudo ltvm exec --timeout 2 co1-single 'sleep 30'   # expect EXIT_TIMEOUT
```

---

## Notes

- VMs are disposable — prefer destroy + recreate over cleanup scripts.
- All disk operations must use real block devices (`/dev/vd*`), never loopback or tmpfs.
- `ltvm deploy` is idempotent: unmounts/unloads before re-deploying.
- Stuck VM: destroy and recreate.
- Client-only targets (rocky10, ubuntu2404, debian12) skip all `(S)` tests.
- Update the matrix above as each test is run.
