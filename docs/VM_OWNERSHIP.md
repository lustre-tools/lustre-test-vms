# VM ownership metadata

LTVM records an advisory owner/session identifier on every newly created VM.
This lets an external lifecycle controller, such as Patch Watcher, discover
and clean up the VMs created by one agent session. Ownership is a label, not
an authorization mechanism: it does not restrict start, stop, or destroy.

## Creation contract

Both single-VM and cluster creation accept `--owner ID` and its equivalent
spelling, `--owner-id ID`:

```bash
ltvm create co1-test --owner-id patch-watcher:session-7f9c
sudo ltvm cluster create co1 --owner-id patch-watcher:session-7f9c \
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

`ltvm install` configures Linux sudo to preserve `LTVM_OWNER_ID`, allowing the
same environment to reach `sudo ltvm cluster create`. On hosts without that
sudo configuration, pass `--owner-id` explicitly or preserve the variable in
the host's sudo policy.

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
