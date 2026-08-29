"""Owner/session identifiers for agent-created VMs.

Ownership is advisory lifecycle metadata: it lets an external controller
discover the VMs created by one session without changing who may operate on
those VMs.  Values are deliberately opaque to ltvm.  The automatic fallback
is namespaced as ``pid:<n>`` so it cannot be mistaken for a durable session ID.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

OWNER_ID_ENV = "LTVM_OWNER_ID"
MAX_OWNER_ID_LENGTH = 255


def validate_owner_id(owner_id: str) -> str:
    """Validate and return an opaque owner ID safe for ``VMInfo`` state.

    VM state is line-oriented, so control characters that could inject a new
    field are rejected.  Other characters, including ``:``, ``/``, spaces,
    and ``=``, remain valid because callers own the identifier namespace.
    """
    if not owner_id:
        raise ValueError("owner ID must not be empty")
    if len(owner_id) > MAX_OWNER_ID_LENGTH:
        raise ValueError(
            f"owner ID is too long ({len(owner_id)} > {MAX_OWNER_ID_LENGTH})"
        )
    if any(ch in owner_id for ch in ("\n", "\r", "\x00")):
        raise ValueError("owner ID must not contain NUL or newline characters")
    return owner_id


def resolve_owner_id(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    pid: int | None = None,
) -> str:
    """Resolve ownership using CLI, environment, then process precedence.

    An explicitly supplied value always wins.  Otherwise a non-empty
    ``LTVM_OWNER_ID`` is used.  With neither, new VMs still receive a useful,
    typed per-invocation value of ``pid:<ltvm-pid>``.  Existing legacy VM state
    remains unowned; this helper is only called by create paths.
    """
    if explicit is not None:
        return validate_owner_id(explicit)

    env = os.environ if environ is None else environ
    environment_owner = env.get(OWNER_ID_ENV)
    if environment_owner:
        return validate_owner_id(environment_owner)

    effective_pid = os.getpid() if pid is None else pid
    return f"pid:{effective_pid}"
