"""claim / release subcommands (see ltvm_pkg.vm_claim)."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from ltvm_pkg.cli.util import EXIT_ERROR, EXIT_OK, _error, _output


def _claim_line(c: object) -> str:
    from ltvm_pkg.vm_claim import Claim

    assert isinstance(c, Claim)
    state = "live" if c.live() else "stale"
    return f"{c.vm:<20} {state:<6} {c.describe()}"


def cmd_claim(args: argparse.Namespace) -> int:
    from ltvm_pkg import vm_claim
    from ltvm_pkg.vm_state import VMInfo

    use_json = args.json
    if not args.names:
        claims = vm_claim.all_claims()
        if use_json:
            _output({"claims": [c.to_json() for c in claims.values()]}, True)
        elif not claims:
            print("no VMs claimed")
        else:
            for c in claims.values():
                print(_claim_line(c))
        return EXIT_OK

    try:
        ttl = vm_claim.parse_ttl(args.ttl) if args.ttl else None
    except ValueError as e:
        return _error(str(e), use_json)
    known = set(VMInfo.all_names())
    tree = str(Path(args.tree).expanduser().resolve()) if args.tree else None
    results = []
    rc = EXIT_OK
    for name in args.names:
        if name not in known:
            rc = _error(f"VM '{name}' not found", use_json)
            continue
        try:
            new, replaced = vm_claim.claim(
                name,
                owner=args.owner,
                pid=args.pid,
                ttl=ttl,
                tree=tree,
                force=args.force,
            )
        except (vm_claim.ClaimError, ValueError) as e:
            rc = _error(str(e), use_json)
            continue
        results.append(new.to_json())
        if not use_json:
            if replaced is not None:
                how = (
                    "broke"
                    if args.force and replaced.live()
                    else "took over stale"
                )
                print(f"{name}: {how} claim of {replaced.owner}")
            print(f"claimed {name} ({new.describe()})")
    if use_json:
        _output({"claims": results}, True)
    return rc


def cmd_release(args: argparse.Namespace) -> int:
    from ltvm_pkg import vm_claim

    use_json = args.json
    rc = EXIT_OK
    released = []
    for name in args.names:
        try:
            prev = vm_claim.release(name, owner=args.owner, force=args.force)
        except (vm_claim.ClaimError, ValueError) as e:
            rc = _error(str(e), use_json)
            continue
        if prev is None:
            if not use_json:
                print(f"{name}: not claimed")
            continue
        released.append(name)
        if not use_json:
            held = time.time() - prev.since
            print(
                f"released {name} (held by {prev.owner} for {held / 60:.0f}m)"
            )
    if use_json:
        _output({"released": released}, True)
    return rc if rc == EXIT_OK else EXIT_ERROR
