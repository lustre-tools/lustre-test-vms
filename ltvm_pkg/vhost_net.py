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


def ensure() -> None:
    """Load vhost_net now and at every boot, unless the host forbids it."""
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
    if (
        qemu_as_user
        and DEVICE.exists()
        and not os.access(DEVICE, os.R_OK | os.W_OK)
    ):
        import grp

        st = DEVICE.stat()
        try:
            group = grp.getgrgid(st.st_gid).gr_name
        except KeyError:
            group = str(st.st_gid)
        rep.issues += 1
        rep.lines.append(
            f"vhost-net: {DEVICE} ({oct(st.st_mode & 0o777)}, group {group}) "
            f"cannot be opened by this user, so NICs run without it; "
            f"add the user to group {group} (then log in again)"
        )
        if fix:
            rep.lines.append(
                "  not fixed: group membership is the admin's call"
            )
            rep.failures += 1
    if not rep.lines:
        rep.lines.append("vhost-net: ok")
    return rep
