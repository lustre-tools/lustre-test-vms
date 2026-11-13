"""Stop the macOS podman machine once ltvm has left it idle for a while.

Run detached by ``host_setup.schedule_idle_stop()`` after a successful
build (``python -m ltvm_pkg.podman_idle``).  It waits until no ltvm
process has used podman for ``podman_idle_stop_delay()`` seconds -- each
one touches the last-use file when it takes the usage lock and when it
exits -- then stops the machine under the exclusive usage lock, the
same way the immediate auto-stop does: never while another ltvm holds
the lock, and never with a non-ltvm container running.

One waiter is enough for every build: it takes its own lock, and a
second waiter that cannot get it exits at once, since the first will
see the later use anyway.
"""

from __future__ import annotations

import fcntl
import logging
import os
import sys
import time

from ltvm_pkg import host_setup

log = logging.getLogger("ltvm.podman_idle")

#: How long to wait before asking again while another ltvm holds the
#: usage lock; its exit touches the last-use file and restarts the clock.
BUSY_RECHECK = 60.0


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def wait_and_stop() -> bool:
    """Wait out the idle delay, then stop the machine if still idle.

    Returns True when it stopped the machine.  Returns without stopping
    when the delay is 0 (the build stopped it already), when a non-ltvm
    container is running, or when podman cannot be asked -- a machine
    someone already stopped included.
    """
    while True:
        delay = host_setup.podman_idle_stop_delay()
        if delay <= 0:
            return False
        left = delay - host_setup.podman_idle_seconds()
        if left > 0:
            _sleep(left)
            continue
        with host_setup.podman_idle_lock() as idle:
            if idle and host_setup.podman_idle_seconds() >= delay:
                if not host_setup.should_stop_podman_machine_macos():
                    return False
                host_setup.stop_podman_machine_macos()
                return True
        _sleep(BUSY_RECHECK)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s podman_idle[{os.getpid()}] %(message)s",
    )
    if os.environ.get("LTVM_PODMAN_AUTOSTOP") == "0":
        return 0
    path = host_setup._podman_file("idle.lock")
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        log.info("cannot open %s: %s", path, e)
        return 1
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # another waiter has it
    try:
        if wait_and_stop():
            log.info("stopped the podman machine after it sat idle")
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
