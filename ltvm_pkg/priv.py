"""Privileged-operation helpers.

ltvm runs as the invoking user and elevates only the specific
operations that require root (bridge/tap setup, /etc/hosts edits,
qemu launch, losetup/mount, etc.).  This module exposes the
helpers that make that uniform across host_setup, vm_commands,
vm_net, qemu_run, image_export, and vm_cluster: ``sudo_run()``
prefixes a command with ``sudo`` when not already root, and
``sudo_prime()`` warms the sudo timestamp upfront so later
``sudo_run()`` calls don't surprise the user with a mid-flow
password prompt.  ``atomic_write()`` writes a file atomically,
falling back to a ``sudo install`` when the destination dir
isn't user-writable (e.g. ``/etc/hosts`` or ``/opt/qemu-vms/``).

These helpers are deliberately dependency-free (stdlib only) so
any module can import them without risking a circular import.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def _run(
    cmd: list[str],
    *,
    check: bool = True,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command, optionally capturing output, raising on non-zero."""
    log.debug("run: %s", " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, capture_output=quiet, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(
            f"Command failed (rc={r.returncode}): "
            f"{' '.join(str(c) for c in cmd)}"
        )
    return r


def sudo_run(
    cmd: list[str],
    *,
    check: bool = True,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command under sudo (no-op prefix if already root)."""
    if os.geteuid() == 0:
        return _run(cmd, check=check, quiet=quiet)
    return _run(["sudo", *cmd], check=check, quiet=quiet)


def sudo_prime(reason: str) -> None:
    """Prompt for sudo credentials up front so later ``sudo_run()``
    calls don't interrupt with a surprise password prompt mid-flow.

    Skips the prompt entirely when ``sudo -n true`` succeeds, which
    covers both an unexpired sudo timestamp and ``NOPASSWD`` rules --
    in those cases ``sudo -v`` would still try to authenticate and
    fail in non-tty contexts (subshells, hooks, CI), aborting even
    though every later ``sudo`` would have worked.
    """
    if os.geteuid() == 0:
        return
    if _run(
        ["sudo", "-n", "true"], check=False, quiet=True
    ).returncode == 0:
        return
    log.info("%s -- prompting for sudo credentials now.", reason)
    _run(["sudo", "-v"])


def invoking_user() -> tuple[str, str] | None:
    """(user, group) of the human behind this invocation, or None.

    Files ltvm writes into root-owned dirs like /opt/qemu-vms/sockets
    go through a ``sudo install``, so without an explicit owner they
    land root-owned and the next unprivileged ltvm command can't touch
    them.  Under sudo the human is $SUDO_USER; otherwise it is just us.
    None means we genuinely are root (a real root login) and there is
    no better owner to pick.
    """
    import grp
    import pwd

    name = os.environ.get("SUDO_USER")
    if not name and os.geteuid() != 0:
        try:
            name = pwd.getpwuid(os.geteuid()).pw_name
        except KeyError:
            return None
    if not name or name == "root":
        return None
    try:
        pw = pwd.getpwnam(name)
        return name, grp.getgrgid(pw.pw_gid).gr_name
    except KeyError:
        return None


def chown_to_invoking_user(path: Path) -> None:
    """Give *path* back to the human when we hold it as root.

    A no-op when not root or when there is no better owner.  Failures
    are ignored: ownership is a convenience, not correctness, and the
    caller has already written the file.
    """
    if os.geteuid() != 0:
        return
    owner = invoking_user()
    if owner is None:
        return
    import pwd

    try:
        pw = pwd.getpwnam(owner[0])
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except (KeyError, OSError):
        pass


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    """Write *text* to *path* atomically, falling back to sudo when
    the destination dir isn't user-writable.

    User-writable case: tempfile + rename in the same directory --
    a true atomic swap on the destination filesystem.

    Sudo fallback: write the tempfile under /tmp, install it to a
    temporary name in the destination directory, then rename it into
    place. This preserves the same-filesystem atomic replacement and
    avoids trying to mkstemp inside e.g. ``/etc/`` as a normal user.

    Creates parent directories as needed (sudo if required).
    """
    parent = path.parent
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            sudo_run(["mkdir", "-p", str(parent)], quiet=True)

    if os.access(str(parent), os.W_OK):
        fd, tmp = tempfile.mkstemp(
            dir=str(parent), prefix=f".{path.name}."
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.chmod(tmp, mode)
            os.rename(tmp, str(path))
            chown_to_invoking_user(path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return

    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir="/tmp")
    dest_tmp = parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        owner = invoking_user()
        own_args = (
            ["-o", owner[0], "-g", owner[1]] if owner is not None else []
        )
        sudo_run(
            [
                "install",
                "-m",
                f"{mode & 0o777:o}",
                *own_args,
                tmp,
                str(dest_tmp),
            ],
            quiet=True,
        )
        sudo_run(["mv", "-f", str(dest_tmp), str(path)], quiet=True)
    finally:
        sudo_run(
            ["rm", "-f", str(dest_tmp)], check=False, quiet=True
        )
        try:
            os.unlink(tmp)
        except OSError:
            pass
