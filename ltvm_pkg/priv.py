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

Commands documented as never needing root (deploy-lustre, cluster
deploy) pass ``noninteractive=True``: sudo is then used only with
``-n`` -- a cached timestamp or NOPASSWD rule -- and a write that
would need a password raises ``PermissionError`` for the caller to
warn about instead of stalling an unattended run at a prompt.

These helpers are deliberately dependency-free (stdlib only) so
any module can import them without risking a circular import.
"""

from __future__ import annotations

import errno
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
        # Include the captured stderr: with quiet=True the child's
        # output goes nowhere else, so without this an atomic_write
        # sudo-fallback failure reports only an argv and an rc, and
        # the actual reason (ENOSPC, read-only fs, sudo policy) is
        # lost entirely.
        detail = ""
        if quiet:
            err = (r.stderr or r.stdout or "").strip()
            if err:
                detail = f": {err}"
        raise RuntimeError(
            f"Command failed (rc={r.returncode}): "
            f"{' '.join(str(c) for c in cmd)}{detail}"
        )
    return r


def sudo_run(
    cmd: list[str],
    *,
    check: bool = True,
    quiet: bool = False,
    noninteractive: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command under sudo (no-op prefix if already root).

    ``noninteractive`` adds ``-n``: use a credential that is already
    there (cached timestamp, NOPASSWD) and fail rather than prompt.
    """
    if os.geteuid() == 0:
        return _run(cmd, check=check, quiet=quiet)
    if noninteractive:
        return _run(["sudo", "-n", *cmd], check=check, quiet=quiet)
    return _run(["sudo", *cmd], check=check, quiet=quiet)


def sudo_ready() -> bool:
    """Can we sudo right now without being asked for a password?

    True when already root, or when ``sudo -n true`` succeeds -- an
    unexpired timestamp or a NOPASSWD rule.
    """
    if os.geteuid() == 0:
        return True
    return _run(["sudo", "-n", "true"], check=False, quiet=True).returncode == 0


def sudo_prime(reason: str) -> None:
    """Prompt for sudo credentials up front so later ``sudo_run()``
    calls don't interrupt with a surprise password prompt mid-flow.

    Skips the prompt entirely when ``sudo -n true`` succeeds, which
    covers both an unexpired sudo timestamp and ``NOPASSWD`` rules --
    in those cases ``sudo -v`` would still try to authenticate and
    fail in non-tty contexts (subshells, hooks, CI), aborting even
    though every later ``sudo`` would have worked.
    """
    if sudo_ready():
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


def _ltvm_owned(path: Path) -> bool:
    """Is *path* a file ltvm creates and must hand back to the human?

    ltvm writes two very different kinds of file through
    ``atomic_write()``: its own state (``/opt/qemu-vms/sockets/*.info``,
    lock files, overlays) which a later *unprivileged* ltvm has to
    rewrite, and pre-existing system files (``/etc/hosts``) which it
    only edits a line of.  Only the first kind may be chowned to the
    invoking user.

    Handing ``/etc/hosts`` to the invoking user -- which is what this
    function exists to prevent -- means any user who can run a single
    ``ltvm create`` owns it from then on and can rewrite it at will
    with no privilege at all.
    """
    vm_dir = Path(os.environ.get("LTVM_VM_DIR", "/opt/qemu-vms"))
    roots = [vm_dir]
    owner = invoking_user()
    if owner is not None:
        import pwd

        try:
            roots.append(Path(pwd.getpwnam(owner[0]).pw_dir))
        except KeyError:
            pass
    for root in roots:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


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


def atomic_write(
    path: Path,
    text: str,
    mode: int = 0o644,
    *,
    noninteractive: bool = False,
) -> None:
    """Write *text* to *path* atomically, falling back to sudo when
    the destination dir isn't user-writable.

    With ``noninteractive`` the fallback runs ``sudo -n`` only, and
    raises ``PermissionError`` up front when that would need a
    password -- for callers that must never block on a prompt.

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
            if noninteractive and not sudo_ready():
                raise
            sudo_run(
                ["mkdir", "-p", str(parent)],
                quiet=True,
                noninteractive=noninteractive,
            )

    owns = _ltvm_owned(path)
    prev_owner: tuple[int, int] | None = None
    if not owns:
        try:
            st = path.stat()
            prev_owner = (st.st_uid, st.st_gid)
        except OSError:
            prev_owner = None

    if os.access(str(parent), os.W_OK):
        fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.chmod(tmp, mode)
            # Preserve the destination's existing ownership for files
            # ltvm doesn't own (see _ltvm_owned): the tempfile we are
            # about to rename over it was created by us, so without
            # this a root-run ltvm would turn /etc/hosts root-owned
            # into whatever we happen to be.
            if not owns and prev_owner is not None:
                try:
                    os.chown(tmp, prev_owner[0], prev_owner[1])
                except OSError:
                    pass
            os.rename(tmp, str(path))
            if owns:
                chown_to_invoking_user(path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return

    if noninteractive and not sudo_ready():
        raise PermissionError(
            errno.EACCES,
            f"{parent} is not writable and sudo would need a password",
            str(path),
        )

    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir="/tmp")
    dest_tmp = parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        owner = invoking_user() if owns else None
        own_args = ["-o", owner[0], "-g", owner[1]] if owner is not None else []
        if not owns and prev_owner is not None:
            # Keep the system file's existing owner rather than letting
            # `install` default it to whoever sudo runs as.
            own_args = [
                "-o",
                str(prev_owner[0]),
                "-g",
                str(prev_owner[1]),
            ]
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
            noninteractive=noninteractive,
        )
        sudo_run(
            ["mv", "-f", str(dest_tmp), str(path)],
            quiet=True,
            noninteractive=noninteractive,
        )
    finally:
        sudo_run(
            ["rm", "-f", str(dest_tmp)],
            check=False,
            quiet=True,
            noninteractive=noninteractive,
        )
        try:
            os.unlink(tmp)
        except OSError:
            pass
