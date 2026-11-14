"""vhost-net on the host: is it there, and loading it.

ltvm turns on vhost=on for its virtio-net NICs when QEMU can open
/dev/vhost-net (qemu_run.vhost_net_usable).  This module is the host
side: whether the vhost_net module is loaded and loads at boot, whether
the ltvm user can open the device, and whether a modprobe rule blocks
it, for `ltvm doctor` and `ltvm install`.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEVICE = Path("/dev/vhost-net")
MODULES_LOAD = Path("/etc/modules-load.d/ltvm-vhost-net.conf")
UDEV_RULE = Path("/etc/udev/rules.d/70-ltvm-vhost-net.rules")
ACCESS_MODE = 0o660
UDEV_TEXT = (
    "# ltvm: unprivileged QEMUs open /dev/vhost-net for vhost=on NICs\n"
    'KERNEL=="vhost-net", GROUP="{group}", MODE="{mode:04o}"\n'
)
# vhost_net and what it needs; a host-wide module blocker may list any.
MODULES = ("vhost_net", "vhost", "vhost_iotlb", "tap", "tun")


class Blocked(RuntimeError):
    """A modprobe rule on this host refuses to load vhost_net."""


def blocked(showconfig: str | None = None) -> list[str]:
    """The vhost_net modules a modprobe ``install`` rule refuses to load.

    A rule like ``install vhost_net /bin/false`` is an admin's decision
    to keep the module out; ltvm reports it and leaves it alone.
    """
    if showconfig is None:
        modprobe = shutil.which("modprobe") or "/sbin/modprobe"
        try:
            r = subprocess.run(
                [modprobe, "--showconfig"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        showconfig = r.stdout
    found = []
    for line in showconfig.splitlines():
        m = re.match(r"install\s+(\S+)\s+(.*)$", line.strip())
        if not m or m.group(1).replace("-", "_") not in MODULES:
            continue
        cmd = m.group(2)
        if re.search(r"(^|[\s;&|])(/bin/|/usr/bin/)?(false|true)\b", cmd) and (
            "modprobe" not in cmd
        ):
            found.append(m.group(1))
    return found


def loaded() -> bool:
    return Path("/sys/module/vhost_net").is_dir()


def persistent() -> bool:
    """Whether some modules-load.d file loads vhost_net at boot."""
    for d in ("/etc/modules-load.d", "/usr/lib/modules-load.d"):
        for f in Path(d).glob("*.conf"):
            try:
                words = f.read_text().split()
            except OSError:
                continue
            if "vhost_net" in words or "vhost-net" in words:
                return True
    return False


def _group_gid() -> int | None:
    """The gid of the group install gives unprivileged VMs, if it exists."""
    import grp

    from .rootless import GROUP

    try:
        return grp.getgrnam(GROUP).gr_gid
    except KeyError:
        return None


def access_problem() -> str | None:
    """How /dev/vhost-net falls short of what install sets, or None.

    Install gives the ltvm group read-write access, as it gives that
    group the VM directories, so an unprivileged QEMU can open it.  A
    host without the group runs QEMU as root and needs nothing.
    """
    import grp

    from .rootless import GROUP

    gid = _group_gid()
    if gid is None:
        return None
    problems = []
    if not UDEV_RULE.is_file():
        problems.append(f"no {UDEV_RULE}")
    if DEVICE.exists():
        st = DEVICE.stat()
        mode = st.st_mode & 0o777
        if st.st_gid != gid or mode & 0o060 != 0o060:
            try:
                group = grp.getgrgid(st.st_gid).gr_name
            except KeyError:
                group = str(st.st_gid)
            problems.append(
                f"{DEVICE} is group {group} mode {mode:04o} "
                f"(want {GROUP} {ACCESS_MODE:04o})"
            )
    return "; ".join(problems) or None


def ensure_access() -> None:
    """Give the ltvm group read-write /dev/vhost-net, now and at boot."""
    from .priv import atomic_write, sudo_run
    from .rootless import GROUP

    if _group_gid() is None:
        return
    atomic_write(UDEV_RULE, UDEV_TEXT.format(group=GROUP, mode=ACCESS_MODE))
    if shutil.which("udevadm"):
        sudo_run(["udevadm", "control", "--reload"], check=False, quiet=True)
    if DEVICE.exists():
        sudo_run(["chgrp", GROUP, str(DEVICE)], quiet=True)
        sudo_run(["chmod", f"{ACCESS_MODE:04o}", str(DEVICE)], quiet=True)


def ensure() -> None:
    """Load vhost_net now and at every boot, and give the ltvm group the
    device, unless the host forbids the module."""
    from .priv import atomic_write, sudo_run

    rules = blocked()
    if rules:
        raise Blocked(
            f"a modprobe install rule blocks {', '.join(rules)}; "
            f"left as the host's admin set it"
        )
    if not loaded():
        sudo_run(["modprobe", "vhost_net"], quiet=True)
    if not persistent():
        atomic_write(
            MODULES_LOAD, "# ltvm: virtio-net NICs with vhost=on\nvhost_net\n"
        )
    ensure_access()


@dataclass
class Report:
    issues: int = 0
    failures: int = 0
    lines: list[str] = field(default_factory=list)


def doctor(fix: bool, qemu_as_user: bool) -> Report:
    """Check vhost-net for `ltvm doctor`; with *fix*, load and persist it.

    *qemu_as_user*: VMs here run QEMU as the user (a set-up host), so
    the user must be able to open the device; otherwise QEMU is root.
    """
    from .qemu_run import vhost_net_setting

    rep = Report()
    if vhost_net_setting() == "off":
        rep.lines.append(
            "vhost-net: off ([network] vhost_net or LTVM_VHOST_NET)"
        )
        return rep
    rules = blocked()
    if rules:
        rep.lines.append(
            f"vhost-net: blocked by a modprobe install rule for "
            f"{', '.join(rules)}; NICs run without it (not overridden)"
        )
        return rep
    problems = []
    if not loaded():
        problems.append("the vhost_net module is not loaded")
    if not persistent():
        problems.append("nothing loads vhost_net at boot")
    if problems:
        rep.issues += 1
        rep.lines.append(f"vhost-net: {'; '.join(problems)}")
        if fix:
            try:
                ensure()
                rep.lines.append(f"  fixed: modprobe vhost_net, {MODULES_LOAD}")
            except (OSError, RuntimeError, subprocess.CalledProcessError) as e:
                rep.lines.append(f"  could not fix: {e}")
                rep.failures += 1
    access = access_problem()
    if access and not problems:
        rep.issues += 1
        rep.lines.append(f"vhost-net: {access}")
        if fix:
            try:
                ensure_access()
                rep.lines.append(f"  fixed: {UDEV_RULE}, applied to {DEVICE}")
            except (OSError, RuntimeError, subprocess.CalledProcessError) as e:
                rep.lines.append(f"  could not fix: {e}")
                rep.failures += 1
    elif (
        qemu_as_user
        and not access
        and DEVICE.exists()
        and not os.access(DEVICE, os.R_OK | os.W_OK)
    ):
        # The device is the group's, but this login is not in it yet.
        rep.issues += 1
        rep.lines.append(
            f"vhost-net: this user cannot open {DEVICE}, so NICs run "
            f"without it; log in again if you were just added to the "
            f"ltvm group"
        )
    if not rep.lines:
        rep.lines.append("vhost-net: ok")
    return rep
