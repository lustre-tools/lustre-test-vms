"""The `ltvm telemetry` subcommand.

status / show / on / off / send.  `show` exists so that "look at
exactly what leaves your machine" is something a user can check
rather than something the documentation asserts.
"""

from __future__ import annotations

import argparse
import json
import logging

from ltvm_pkg import telemetry
from ltvm_pkg.cli.util import EXIT_OK, _error, _output

log = logging.getLogger("ltvm.telemetry")


def cmd_telemetry(args: argparse.Namespace) -> int:
    use_json = bool(getattr(args, "json", False))
    action = getattr(args, "telemetry_action", None)

    if action == "show":
        payload = telemetry.preview()
        if use_json:
            _output(payload, True)
        else:
            print(json.dumps(payload, indent=2))
            print(f"\nPOSTed to {telemetry.ENDPOINT}", flush=True)
        return EXIT_OK

    if action in ("on", "off"):
        telemetry.set_enabled(action == "on")
        state = telemetry.status()
        # An env var or /etc/ltvm.conf outranks the per-user setting we
        # just wrote, so say so rather than reporting a change that
        # will not take effect.
        if action == "on" and not state["enabled"]:
            msg = (
                "telemetry enabled for this user, but still off: "
                f"{state['disabled_by']}"
            )
        else:
            msg = f"telemetry {action}"
        if use_json:
            _output({"enabled": state["enabled"], "message": msg}, True)
        else:
            print(msg)
        return EXIT_OK

    if action == "send":
        if not telemetry.is_enabled():
            return _error("telemetry is disabled", use_json)
        ok = telemetry.send_now()
        if not getattr(args, "quiet", False):
            _output({"sent": ok} if use_json else f"sent: {ok}", use_json)
        # A failed send is not a failed command: the caller asked us to
        # try, and an unreachable server is not their problem.
        return EXIT_OK

    if action == "status" or action is None:
        state = telemetry.status()
        if use_json:
            _output(state, True)
            return EXIT_OK
        print(f"enabled:    {state['enabled']}")
        if state["disabled_by"]:
            print(f"disabled by: {state['disabled_by']}")
        print(f"install ID: {state['install_id'] or '(not yet assigned)'}")
        print(f"last send:  {state['last_send'] or 'never'}")
        print(f"every:      {state['interval_days']} days")
        print(f"endpoint:   {state['endpoint']}")
        return EXIT_OK

    return _error(f"unknown telemetry action: {action}", use_json)
