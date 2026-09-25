# VM ownership metadata

LTVM records an advisory owner/session identifier on every newly created VM.
This lets an external lifecycle controller, such as Patch Watcher, discover
and clean up the VMs created by one agent session. Ownership is a label, not
an authorization mechanism: it does not restrict start, stop, or destroy.
Which session is *using* a VM right now is a separate, enforced record: see
[Claims](#claims).

## Creation contract

Both single-VM and cluster creation accept `--owner ID` and its equivalent
spelling, `--owner-id ID`:

```bash
ltvm create co1-test --owner-id patch-watcher:session-7f9c
ltvm cluster create co1 --owner-id patch-watcher:session-7f9c \
    mgs+mds:co1-mds:1 oss:co1-oss:1
```

The owner is optional caller input. LTVM always resolves a value for a new VM
using this precedence:

1. explicit `--owner` or `--owner-id`;
2. non-empty `LTVM_OWNER_ID` from the environment;
3. `pid:<n>`, where `<n>` is the invoking LTVM process ID.

The `pid:` fallback preserves ordinary, unchanged `ltvm create` workflows and
provides useful per-invocation grouping. It is not a durable agent-session ID.
Controllers should launch their agent with a stable opaque value instead:

```bash
export LTVM_OWNER_ID=patch-watcher:session-7f9c
ltvm create co1-test
```

On a host where VMs still need root, `ltvm install` configures Linux sudo to
preserve `LTVM_OWNER_ID`, allowing the same environment to reach
`sudo ltvm cluster create`. On hosts without that sudo configuration, pass
`--owner-id` explicitly or preserve the variable in the host's sudo policy.

A cluster resolves its owner once in the parent command and passes it
explicitly to every member create. The cluster and all member VMs therefore
have exactly the same owner, including when the PID fallback is used.

Owner IDs are opaque strings up to 255 characters. They may contain spaces,
slashes, colons, and equals signs, but not NUL, carriage return, or newline.

## Discovery and persistence contract

`ltvm list --json` includes `owner_id` on every VM entry:

```json
{
  "vms": [
    {
      "name": "co1-test",
      "owner_id": "patch-watcher:session-7f9c"
    },
    {
      "name": "old-vm",
      "owner_id": null
    }
  ]
}
```

The other existing VM fields and the `totals` object are unchanged. Human
`ltvm list` output shows `owner=<id>`, and the JSON success response from a new
`ltvm create --json` includes `owner_id`.

On disk, a VM's `/opt/qemu-vms/sockets/<name>.info` state contains
`OWNER_ID=<id>`. Cluster state contains top-level JSON `"owner_id"`. Restart,
stop, deploy, and snapshot operations retain the value. Destroy removes it
only when it removes the VM's state. State written by older LTVM versions has
no owner field and loads normally with `owner_id: null`.

An idempotent `ltvm create` of an existing VM does not replace its persisted
owner. A controller can reconcile by filtering `ltvm list --json` by
`owner_id`, then destroying the matching VM names when its session terminates.

## Claims

A claim says which session is using a VM now, as opposed to which one
created it.  `ltvm claim <vm>` takes one, `ltvm release <vm>` drops it, and
`ltvm claim` with no VM lists them (`--json` for a document).  `ltvm list`
adds `claimed=<owner>` to a claimed VM and a `claim` object (or `null`) to
each JSON entry.

These commands refuse a VM that another owner holds a live claim on, before
asking for sudo or building anything: `deploy-lustre`, `llmount`/`llumount`,
`start`, `stop`, `destroy`, `vm snapshot/restore/nmi/crash-collect/set`, and
`cluster start/stop/deploy/llmount/exec/destroy` (all nodes are checked
before any is touched).  `ltvm release --force <vm>` breaks the claim.
`ssh` cannot be gated.

The claimant is, in order: `--owner`; `LTVM_OWNER_ID`; `claude:<id>` from
Claude Code's `CLAUDE_CODE_SESSION_ID`; otherwise `user:<name>`.  A claim
ends when its process exits (`--pid`, `LTVM_OWNER_PID`, or Claude Code's
`CLAUDE_PID`; checked by pid and start time), when a `--ttl` runs out, or
on release; a claim with neither lasts until released.  The next claim
takes over a stale one, and `ltvm doctor --fix` clears them.

`deploy-lustre` and `cluster deploy` claim unclaimed VMs for a session
owner (anything but `user:`), recording the Lustre tree, so an agent is
covered without doing anything.  A person deploying by hand claims nothing.

Claims are files in `VM_DIR/claims/` (mode 1777), one per VM, rewritten in
place under `flock` and never renamed or unlinked: the sticky bit would stop
another user taking over a stale claim otherwise.  `ltvm install` and
`ltvm doctor --fix` create the directory; without it nothing is refused and
`deploy-lustre` warns that it could not claim.  The Linux sudoers fragment
keeps the claim variables across `sudo ltvm`.
