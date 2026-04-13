# Suggested Agent Patterns for ltvm-based Lustre Development

Copy/adapt these sections into your project's CLAUDE.md
or AGENTS.md. Replace `~/lustre-test-vms-v2` and
`~/lustre-release` with your actual locations.

---

## Agent Patterns

### 1. Parallel disjoint-file refactors

**When to use:** Multiple independent sub-tasks each
touching a clearly separated subset of files (different
modules, different targets, different test files).

**Model:** Opus  
**Isolation:** One isolated worktree per sub-agent  
**Constraint:** Explicitly list which files each agent
may touch; verify there is no overlap before starting.

**Prompt skeleton:**

```
Working in worktree at <path> (branch <branch>).

You may ONLY modify these files:
  ltvm_pkg/lustre_compat.py
  tests/test_lustre_compat.py

Do NOT touch targets/, cli.py, or any other file.

Task: <specific task description>

When done: run `ruff check ltvm_pkg/ && python -m pytest tests/`
and fix any failures before stopping.
```

**Example:** The afr wave (qb4/laj/r4h/tmh) ran four
Opus agents in parallel -- each owned one module and its
test file, with zero file overlap.

---

### 2. Single-PR refactor too big for one edit

**When to use:** A feature or refactor that spans many
files but is logically one unit of work -- too large for
a cowboy edit session but doesn't need parallelism.

**Model:** Opus  
**Isolation:** Isolated worktree, single agent  
**Pattern:** Give a detailed scope, list files that will
change, and include a test plan.

**Prompt skeleton:**

```
Working in worktree at <path> (branch <branch>).
No git push.

Scope: <feature name>

Files expected to change:
  ltvm_pkg/image_build.py    (main logic)
  ltvm_pkg/target_config.py  (path helpers)
  ltvm_pkg/cli.py            (new subcommand wiring)
  tests/test_image_build.py  (new tests)

Do NOT change: vm_commands.py, vm_cluster.py, deploy.py

Detailed requirements:
  <requirements>

Test plan:
  1. ltvm build-status shows correct image row per kernel
  2. ltvm build-image rocky9 --kernel 5.14-rhel9.5 succeeds
  3. ltvm ensure co1-single boots with the non-default image

Run `ruff check ltvm_pkg/ && python -m pytest tests/` before stopping.
```

**Example:** q7l (per-kernel images) and eh9
(per-kernel Lustre staging) used this pattern.

---

### 3. Doc refresh / command rewiring

**When to use:** Updating CLAUDE.md, SUGGESTED-AGENTS.md,
README, or fixing stale command examples after a
significant feature wave.

**Model:** Sonnet  
**Isolation:** Master directly (no worktree) -- docs
have no test suite, conflicts are trivial  
**Pattern:** Small, focused scope; verify every command
example against `ltvm <cmd> --help` before committing.

**Prompt skeleton:**

```
Working in ~/lustre-test-vms-v2 on master. No git push.

Refresh CLAUDE.md and SUGGESTED-AGENTS.md to match the
current CLI. Recent changes:
  - <change 1>
  - <change 2>

For every command example in the docs, verify against
`ltvm <cmd> --help` before writing.

Commit when done.
```

---

### 4. Test-gap filler

**When to use:** Coverage is low for a module that just
landed. No design decisions needed -- just write tests
that exercise the existing behaviour.

**Model:** Sonnet  
**Isolation:** Master directly  
**Pattern:** Point at the module; ask for tests that
cover the common path and the known edge cases.

**Prompt skeleton:**

```
Working in ~/lustre-test-vms-v2 on master. No git push.

Add tests for ltvm_pkg/<module>.py. Existing tests are
in tests/. The module was added/changed in commit <sha>.

Cover at minimum:
  - <case 1>
  - <case 2>
  - error path: <what should raise / return>

Run `python -m pytest tests/` to verify before stopping.
```

**Example:** commit b3228f7 filled coverage gaps after
the per-kernel image refactor.

---

### 5. Long-running build / fetch orchestration

**When to use:** `ltvm build-all`, `ltvm fetch`, or a
kernel rebuild. These are I/O-bound shell operations --
an agent adds no brainpower.

**Model:** N/A -- use Bash `run_in_background` directly  
**Pattern:** Fire the command in background, monitor
with the Monitor tool or wait for the notification.

```bash
# In a Claude Code session:
ltvm build-all rocky9 --lustre-tree ~/lustre-release
# or run_in_background for async
```

Do NOT spawn an agent for this. If the build fails,
read the error output and fix it yourself (one agent,
master, targeted fix).

---

## Building

### Quick Start (pre-built artifacts)

```bash
cd ~/lustre-test-vms-v2
ltvm fetch rocky9
ltvm build-status
```

### Building from Scratch

```bash
ltvm build-all rocky9 --lustre-tree ~/lustre-release
ltvm build-status
```

### Kernel Build

```bash
# Default kernel
ltvm build-kernel rocky9 --lustre-tree ~/lustre-release

# Non-default kernel minor
ltvm build-kernel rocky9 --kernel 5.14-rhel9.5 \
    --lustre-tree ~/lustre-release
```

### Lustre Build

```bash
ltvm build-lustre rocky9 ~/lustre-release
ltvm build-lustre rocky9 ~/lustre-release --kernel 5.14-rhel9.5
```

### Compat Check

```bash
ltvm validate rocky9 --lustre-tree ~/lustre-release
# exit 0 = compatible, 1 = warning, 2 = refused
```

Override a refusal (not a hard error):
```bash
ltvm build-lustre rocky9 ~/lustre-release --force-compat
ltvm deploy co1-single --build ~/lustre-release --force-compat
```

---

## Deploy Workflow

### Single-node VMs

```bash
ltvm ensure co1-single \
    --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3

ltvm deploy co1-single \
    --build ~/lustre-release --mount
```

### Multi-node Clusters

Roles: `mgs`, `mds`, `oss`, `client` (join with `+`).
Node spec: `roles:vmname[:disks]`.

```bash
ltvm cluster create co2 \
    mgs+mds:co2-mds:1 oss:co2-oss:3
ltvm cluster deploy co2 \
    --build ~/lustre-release --mount
ltvm cluster exec co2 oss 'lctl dl'
ltvm cluster destroy co2
```

### VM Naming Convention

Names MUST include the checkout number.
Format: `co<N>-<role>`. Examples: `co1-single`,
`co2-mds`, `co2-oss`. Never use bare names like `testvm`.

---

## Loading and Unloading Lustre

Use `ltvm llmount` -- it handles `dmsetup remove_all`
automatically before calling `llmount.sh`.

```bash
ltvm llmount co1-single             # mount
ltvm llmount co1-single --cleanup   # unmount + lustre_rmmod
```

Destroy/recreate is often faster than cleanup (~15-20s):
```bash
ltvm destroy co1-single
ltvm ensure co1-single \
    --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3
ltvm deploy co1-single --build ~/lustre-release --mount
```

---

## Testing

### Run Tests (inside VM)

```bash
ltvm exec co1-single \
    'sudo -E ONLY=42a bash /usr/lib64/lustre/tests/sanity.sh'
# or with auster:
ltvm exec co1-single \
    'sudo -E auster -s sanity --only 42a'
```

Auster logs: `/tmp/test_logs/YYYY-MM-DD/HHMMSS/`

---

## Debugging

### Debug Log Quick Reference

```bash
ltvm exec co1-single 'lctl set_param debug=-1'
ltvm exec co1-single 'lctl set_param debug_mb=10000'
ltvm exec co1-single 'lctl clear'
ltvm exec co1-single 'lctl mark "before repro"'
# ... reproduce ...
ltvm exec co1-single 'lctl dk /tmp/dk.log'
sudo vm.py cp-from co1-single /tmp/dk.log .
```

### kdump / Crash Analysis

VMs boot with `crashkernel=512M`. kdump artifacts are
baked into the image at build time.

```bash
ltvm nmi co1-single              # trigger panic + kdump
ltvm crash-collect co1-single --mod-dir ~/lustre-release
```

vmlinux is at `output/<target>/kernel/vmlinux`.

---

## VM Internals

- QEMU binary: `/opt/qemu/bin/qemu-system-x86_64`
- Base image: `<repo>/output/<target>/images/<kernel>/base.ext4`
- Kernel: `<repo>/output/<target>/kernel/vmlinux`
- Kernel build tree: `<repo>/output/<target>/kernel/build-tree/`
- qcow2 overlays: `/opt/qemu-vms/overlays/`
- TAP devices on `fcbr0` bridge, `192.168.100.0/24`
- IPs derived from VM name (md5 hash -> last octet)
- After host reboot: `sudo vm.py start-all`
