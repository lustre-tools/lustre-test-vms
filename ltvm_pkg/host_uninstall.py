"""`ltvm uninstall`: take off this host what `ltvm install` put there.

Runs as the invoking user, like `ltvm install` on macOS, and elevates
only the operations that need root (``priv.sudo_run``).  It refuses to
run under sudo: the podman store, the skills links, the artifacts and
the crash dumps all belong to the user, and under sudo every one of
them would be looked for in root's home.  A real root login is fine.

Work happens in two halves:

- :func:`survey` only reads.  It finds what is there to remove, and the
  four things the user decides about -- the artifacts, the crash dumps,
  the podman machine and ltvm's podman images.  ``--dry-run`` stops
  here, so it never asks for a password.
- :func:`run` does the removal as an ordered list of named steps.  A
  step that fails is reported and the rest still run: a half-removed
  install is better than one that stopped at the first error, and the
  exit status says that something was left.

What install got from a package manager (QEMU, dnsmasq, podman, sshpass,
pdsh, the e2fsprogs and fakeroot formulae) stays: other things on the
host may use it, and nothing about ltvm is left in it.  So does
everything the user owns that ltvm did not make, and the source
checkout itself.  :func:`left_in_place` lists what stays.

Each removal mirrors its install step, and keeps that step's names --
the constants live with the installer, never copied here -- so the two
cannot drift apart.
"""

from __future__ import annotations

import contextlib
import grp
import logging
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import host_setup, rootless
from .host_setup import QEMU_PREFIX, VM_DIR
from .priv import atomic_write, sudo_run, which

log = logging.getLogger("ltvm")

LAUNCHER = Path("/usr/local/bin/ltvm")
DK_FILTER = Path("/usr/local/bin/dk-filter")
SUDOERS_FRAGMENT = Path("/etc/sudoers.d/ltvm")
QEMU_PROFILE = Path("/etc/profile.d/qemu-vms.sh")
SYSCTL_CONF = Path("/etc/sysctl.d/99-qemu-vms.conf")
BRIDGE_UNIT = "qemu-bridge.service"
BRIDGE_UNIT_PATH = Path("/etc/systemd/system") / BRIDGE_UNIT
ROOT_SSH_CONFIG = Path("/root/.ssh/config")
IMAGE_TOOLS_DIR = Path("/usr/local/bin")
IMAGE_TOOLS = ("mke2fs", "e2fsck", "tune2fs", "resize2fs")
HOSTS_FILE = Path("/etc/hosts")
MACOS_LOG_DIRS = (Path("/var/log/ltvm-dnsmasq"), Path("/var/log/socket_vmnet"))

# The default locations; a path the user moved with LTVM_VM_DIR or
# LTVM_QEMU_PREFIX is removed only when it passes _removable_tree().
_DEFAULT_VM_DIR = Path("/opt/qemu-vms")
_DEFAULT_QEMU_PREFIX = Path("/opt/qemu")
# With the users' homes, what _removable_tree() never lets those go.
_PROTECTED_DIRS = (
    "/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/local/bin",
    "/usr/local/sbin", "/opt/homebrew/bin", "/opt/local/bin", "/etc",
    "/var/lib", "/var/log", "/Applications", "/Library", "/System",
)  # fmt: skip

# What a launcher written by host_setup._render_ltvm_launcher looks like.
_LAUNCHER_RE = re.compile(r"^exec '[^']+' '[^']+/ltvm' \"\$@\"$", re.MULTILINE)

DONE = "done"
ABSENT = "absent"
KEPT = "kept"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class Choices:
    """Delete (True), keep (False), or ask (None) about each."""

    artifacts: bool | None = None
    crashes: bool | None = None
    podman_machine: bool | None = None
    podman_images: bool | None = None


@dataclass
class Survey:
    """What is on this host, read without root."""

    macos: bool
    vms: list[str] = field(default_factory=list)
    claimed: list[str] = field(default_factory=list)
    stray_names: list[str] = field(default_factory=list)
    artifacts: list[Path] = field(default_factory=list)
    crashes: Path | None = None
    podman_machine: str | None = None
    podman_machine_running: bool = False
    podman_tags: list[str] = field(default_factory=list)
    podman_volumes: list[str] = field(default_factory=list)
    # False when podman could not be asked (a stopped podman machine).
    podman_checked: bool = True
    subnet: str | None = None
    launchers: list[Path] = field(default_factory=list)


@dataclass
class Step:
    name: str
    status: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"step": self.name, "status": self.status, "detail": self.detail}


class Refused(RuntimeError):
    """Uninstall cannot go ahead; the message says why."""


# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------


def _user_home() -> Path:
    return Path.home()


def crash_dir() -> Path:
    """Where `ltvm vm crash-collect` puts dumps by default."""
    return _user_home() / "ltvm-crashes"


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or (_user_home() / default)) / "ltvm"


def cache_dir() -> Path:
    """The per-user cache: ZFS builds the shared artifacts cannot hold."""
    return _xdg("XDG_CACHE_HOME", ".cache")


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state")


def artifact_dirs() -> list[Path]:
    """The artifacts cache and the per-user cache, those that exist."""
    from .target_config import ARTIFACTS_DIR

    return [p for p in (ARTIFACTS_DIR, cache_dir()) if p.is_dir()]


def _protected_dirs() -> list[Path]:
    """Directories uninstall never removes, nor any directory above one."""
    homes = {_user_home()}
    homes.update(Path(pw.pw_dir) for pw in pwd.getpwall())
    paths = [Path(p) for p in _PROTECTED_DIRS] + sorted(homes)
    return [p.resolve() for p in paths if p.is_absolute()]


def _removable_tree(path: Path, default: Path) -> bool:
    """May uninstall `rm -rf` *path*, a directory install made?

    The default location always may.  One moved by an environment
    variable may when it is a real directory at least two levels deep
    that is not a home directory, a system directory or anything above
    one -- so a stray ``LTVM_VM_DIR=/usr/local`` cannot take /usr/local
    with it.
    """
    if path == default:
        return not path.is_symlink()
    if path.is_symlink() or not path.is_dir() or not path.is_absolute():
        return False
    resolved = path.resolve()
    if len(resolved.parts) < 3:
        return False
    # A home the password database does not list (LDAP).
    if resolved.parent in (Path("/home"), Path("/Users")):
        return False
    return not any(
        resolved == p or resolved in p.parents for p in _protected_dirs()
    )


# ------------------------------------------------------------------
# Survey
# ------------------------------------------------------------------


def _hosts_names() -> list[str]:
    """VM names that /etc/hosts or hosts.d still publish."""
    from .vm_state import MARKER

    names = set(rootless.hosts_entries())
    try:
        text = HOSTS_FILE.read_text()
    except OSError:
        text = ""
    tag = f"{MARKER}:"
    for line in text.splitlines():
        line = line.rstrip()
        if tag in line:
            names.add(line.rsplit(tag, 1)[1].strip())
    return sorted(n for n in names if n)


def _survey_vms(s: Survey) -> None:
    from . import vm_claim
    from .vm_state import VMInfo

    s.vms = VMInfo.all_names()
    for name in s.vms:
        try:
            vm_claim.check(name, "uninstall ltvm under")
        except vm_claim.ClaimError as e:
            s.claimed.append(str(e))
    s.stray_names = [n for n in _hosts_names() if n not in s.vms]


def _survey_podman(s: Survey) -> None:
    if not shutil.which("podman"):
        return
    from . import build_volumes, podman_gc

    if s.macos:
        machines = host_setup._podman_machine_list_macos()
        target = host_setup._default_or_first(
            [m for m in machines if m.get("VMType") != "libkrun"]
        ) or host_setup._default_or_first(machines)
        if target is None:
            return  # No machine, so no images to check either.
        s.podman_machine = target.get("Name") or "podman-machine-default"
        s.podman_machine_running = bool(target.get("Running"))
        if not s.podman_machine_running:
            s.podman_checked = False
            return
    s.podman_tags = sorted(t.tag for t in podman_gc.ltvm_tags())
    vols = build_volumes.list_volumes()
    if vols is None:
        s.podman_checked = False
    else:
        s.podman_volumes = sorted(v.name for v in vols)


def _read_subnet() -> str | None:
    try:
        return (VM_DIR / "subnet").read_text().strip() or None
    except OSError:
        return None


def survey(macos: bool | None = None) -> Survey:
    """Look at the host.  Reads only: no sudo, nothing written."""
    s = Survey(macos=host_setup.is_macos() if macos is None else macos)
    _survey_vms(s)
    s.artifacts = artifact_dirs()
    if crash_dir().is_dir():
        s.crashes = crash_dir()
    _survey_podman(s)
    # Read before anything is removed: the iptables rules to delete
    # are written in terms of it, and it lives in VM_DIR.
    if not s.macos:
        s.subnet = _read_subnet() or host_setup.DEFAULT_SUBNET
    if _is_ltvm_launcher(LAUNCHER):
        s.launchers.append(LAUNCHER)
    return s


def questions(s: Survey) -> list[tuple[str, str]]:
    """(attribute of Choices, question) for each thing that is there."""
    out: list[tuple[str, str]] = []
    if s.artifacts:
        where = ", ".join(str(p) for p in s.artifacts)
        out.append(
            ("artifacts", f"Delete the built and fetched artifacts ({where})?")
        )
    if s.crashes is not None:
        out.append(("crashes", f"Delete the crash dumps in {s.crashes}?"))
    if s.podman_machine is not None:
        out.append(
            (
                "podman_machine",
                f"Delete the podman machine '{s.podman_machine}'?  Everything "
                f"in it goes, not only ltvm's images",
            )
        )
    if s.podman_tags or s.podman_volumes:
        n = len(s.podman_tags) + len(s.podman_volumes)
        out.append(
            ("podman_images", f"Delete ltvm's {n} podman images and volumes?")
        )
    return out


def describe(s: Survey, c: Choices) -> list[str]:
    """The plan, in lines, for the user to confirm."""

    def fate(choice: bool | None) -> str:
        return {True: "delete", False: "keep", None: "ask"}[choice]

    lines = ["ltvm uninstall will remove:"]
    if s.vms:
        lines.append(f"  VMs (destroyed): {' '.join(s.vms)}")
    if s.stray_names:
        lines.append(f"  name entries of gone VMs: {' '.join(s.stray_names)}")
    if s.macos:
        lines.append(
            "  the socket_vmnet and dnsmasq LaunchDaemons, their "
            "config, pid, log and socket files, and the dnsmasq reload "
            "script"
        )
        lines.append(f"  the QEMU links in {QEMU_PREFIX}")
        lines.append(f"  the e2fsprogs links in {IMAGE_TOOLS_DIR}")
    else:
        lines.append(
            f"  fcbr0 ({s.subnet}.0/24), {BRIDGE_UNIT}, "
            f"{host_setup.LTVM_DNSMASQ_UNIT} and their iptables, ufw or "
            f"firewalld rules"
        )
        lines.append(
            "  ltvm's files in /etc (dnsmasq, sysctl, sudoers, profile, "
            "bridge.conf line, root's ssh config block)"
        )
        lines.append(f"  {QEMU_PREFIX}, {DK_FILTER}")
    lines.append(f"  {VM_DIR} and the '{rootless.GROUP}' group")
    lines.append(
        "  tab completion, the agent skill links, ltvm's config and state"
    )
    for p in s.launchers:
        lines.append(f"  the launcher {p}")
    lines.append("Asked about (default keep):")
    asked = False
    if s.artifacts:
        asked = True
        for p in s.artifacts:
            lines.append(f"  [{fate(c.artifacts)}] artifacts {p}")
    if s.crashes is not None:
        asked = True
        lines.append(f"  [{fate(c.crashes)}] crash dumps {s.crashes}")
    if s.podman_machine is not None:
        asked = True
        lines.append(
            f"  [{fate(c.podman_machine)}] podman machine {s.podman_machine}"
        )
    if s.podman_tags or s.podman_volumes:
        asked = True
        n = len(s.podman_tags) + len(s.podman_volumes)
        lines.append(
            f"  [{fate(c.podman_images)}] {n} ltvm podman images and volumes"
        )
    elif not s.podman_checked:
        asked = True
        lines.append(
            "  podman images: not checked (the podman machine is stopped)"
        )
    if not asked:
        lines.append("  nothing")
    return lines


def _join(items: list[str]) -> str:
    """ "a", "a and b", "a, b and c"."""
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def machine_goes(s: Survey, c: Choices) -> bool:
    """Is a podman machine there, and to be deleted?  Not on Linux."""
    return s.podman_machine is not None and bool(c.podman_machine)


def goes_with_machine(attr: str, s: Survey, c: Choices) -> bool:
    """Is *attr* a question a yes to deleting the machine answers?"""
    return attr == "podman_images" and machine_goes(s, c)


def confirmation(s: Survey, c: Choices) -> str:
    """The last question, naming what a yes deletes and what it keeps."""
    labels = {
        "artifacts": "the artifacts",
        "crashes": "the crash dumps",
        "podman_machine": f"the podman machine '{s.podman_machine}'",
        "podman_images": "ltvm's podman images",
    }
    delete: list[str] = []
    keep: list[str] = []
    for attr, _ in questions(s):
        if goes_with_machine(attr, s, c):
            continue
        (delete if getattr(c, attr) else keep).append(labels[attr])
    q = "Remove the ltvm setup listed above"
    if delete:
        q += f", delete {_join(delete)}"
    if keep:
        q += (
            f", and keep {_join(keep)}"
            if delete
            else f", keeping {_join(keep)}"
        )
    return q + "?"


# What ltvm install has Homebrew install on macOS.
BREW_FORMULAE = (
    "qemu",
    "socket_vmnet",
    "dnsmasq",
    "sshpass",
    "podman",
    "e2fsprogs",
    "fakeroot",
    "bash",
)


def _brew_formulae(keep_podman: bool) -> list[str]:
    """The formulae of BREW_FORMULAE still installed, for a brew command."""
    brew = shutil.which("brew")
    if not brew:
        return []
    wanted = [f for f in BREW_FORMULAE if not (keep_podman and f == "podman")]
    try:
        r = subprocess.run(
            [brew, "list", "--formula", "-1"], capture_output=True, text=True
        )
    except OSError:
        return wanted
    if r.returncode:
        return wanted
    have = set(r.stdout.split())
    return [f for f in wanted if f in have]


def left_in_place(s: Survey, c: Choices | None = None) -> list[str]:
    """What uninstall leaves, by design, and how to remove it."""
    if s.macos:
        # Removing podman would strand a podman machine the user kept.
        keep_podman = s.podman_machine is not None and not (
            c is not None and machine_goes(s, c)
        )
        out: list[str] = []
        formulae = _brew_formulae(keep_podman)
        if formulae:
            note = (
                " (podman stays with the podman machine)" if keep_podman else ""
            )
            out += [
                f"Homebrew formulae ltvm installed{note}.  To remove them "
                f"too, if nothing else of yours uses them:",
                "  brew uninstall " + " ".join(formulae),
            ]
        return out + ["this ltvm checkout"]
    out = [
        "packages ltvm installed (qemu, dnsmasq, iptables, pdsh, sshpass, "
        "...): other software may use them",
        "/etc/modules-load.d/br_netfilter.conf, and net.ipv4.ip_forward "
        "until the next reboot",
        "membership of the kvm group",
        "this ltvm checkout",
    ]
    helper = rootless.installed_helper()
    if helper is not None and rootless.is_setuid_root(helper):
        out.append(
            f"{helper} setuid root (chmod u-s {helper} if nothing else "
            f"needs it; on apt hosts, dpkg-statoverride --remove {helper})"
        )
    return out


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _is_ltvm_launcher(path: Path) -> bool:
    """Did install put *path* there?  A wrapper, or the symlink to a
    checkout's ltvm that installs before the wrapper made."""
    try:
        if path.is_symlink():
            target = Path(os.readlink(path))
            if not target.is_absolute():
                target = path.parent / target
            return target.name == "ltvm" and (
                not target.exists() or (target.parent / "ltvm_pkg").is_dir()
            )
        return bool(_LAUNCHER_RE.search(path.read_text()))
    except (OSError, UnicodeDecodeError):
        return False


def _exists(path: Path) -> bool:
    """Path.exists(), with a path under an unreadable dir counted as there."""
    try:
        return path.exists() or path.is_symlink()
    except PermissionError:
        return True


def _rm(*paths: Path) -> list[str]:
    """``sudo rm -f`` the paths that exist; the ones removed."""
    present = [str(p) for p in paths if _exists(p)]
    if present:
        sudo_run(["rm", "-f", *present], quiet=True)
    return present


def _rmdir_if_empty(d: Path) -> list[str]:
    """``sudo rmdir`` *d*, a directory install made, when nothing else
    put files in it; ``[str(d)]`` when it went."""
    if d.is_dir() and not d.is_symlink() and not any(d.iterdir()):
        sudo_run(["rmdir", str(d)], quiet=True)
        return [str(d)]
    return []


def _rm_tree(path: Path) -> bool:
    if not _exists(path):
        return False
    sudo_run(["rm", "-rf", str(path)], quiet=True)
    return True


def _user_rm_tree(path: Path) -> None:
    """Remove a tree the user owns, elevating only if refused."""
    from .podman_run import rmtree

    try:
        rmtree(path)
    except PermissionError:
        sudo_run(["rm", "-rf", str(path)], quiet=True)


def _quiet(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return sudo_run(cmd, check=False, quiet=True)


def _launchd_loaded(label: str) -> bool:
    return host_setup._launchd_loaded(label)


def _bootout(label: str, wait: float = 15.0) -> bool:
    """Unload system job *label* and wait until launchd lets it go."""
    if not _launchd_loaded(label):
        return False
    _quiet(["launchctl", "bootout", f"system/{label}"])
    deadline = time.monotonic() + wait
    while _launchd_loaded(label) and time.monotonic() < deadline:
        time.sleep(0.2)
    if _launchd_loaded(label):
        raise RuntimeError(f"launchd still has {label} loaded")
    return True


def _group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
    except KeyError:
        return False
    return True


# ------------------------------------------------------------------
# Steps.  Each returns what it removed (None: nothing there) or raises.
# ------------------------------------------------------------------

StepFn = Callable[[], "str | None"]


def _destroy_vms(s: Survey, use_json: bool) -> str | None:
    if not s.vms:
        return None
    import argparse

    from .cli.setup import cmd_destroy

    # One call for every VM, so a failover pair goes together, through
    # the CLI wrapper, so the claim check and privileges are destroy's.
    ns = argparse.Namespace(names=list(s.vms), json=use_json)
    # destroy prints a line per VM, which under --json would land in
    # the middle of uninstall's own document.
    out = sys.stderr if use_json else sys.stdout
    try:
        with contextlib.redirect_stdout(out):
            rc = cmd_destroy(ns)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    if rc:
        raise RuntimeError(f"ltvm destroy exited {rc}")
    return " ".join(s.vms)


def _forget_stray_names(s: Survey) -> str | None:
    if not s.stray_names:
        return None
    from .vm_net import unregister_ssh_name

    for name in s.stray_names:
        unregister_ssh_name(name)
    return " ".join(s.stray_names)


def _remove_podman_images(s: Survey, c: Choices) -> str | None:
    if not (s.podman_tags or s.podman_volumes) or not c.podman_images:
        return None
    if machine_goes(s, c):
        return "went with the podman machine"
    kept: list[str] = []
    for tag in s.podman_tags:
        if subprocess.run(
            ["podman", "rmi", tag], capture_output=True, text=True
        ).returncode:
            kept.append(tag)
    for vol in s.podman_volumes:
        # No -f: that would remove the containers using it too.
        if subprocess.run(
            ["podman", "volume", "rm", vol], capture_output=True, text=True
        ).returncode:
            kept.append(vol)
    if kept:
        raise RuntimeError(f"podman refused (in use?): {' '.join(kept)}")
    return f"{len(s.podman_tags)} images, {len(s.podman_volumes)} volumes"


def _remove_podman_machine(s: Survey, c: Choices) -> str | None:
    if s.podman_machine is None or not c.podman_machine:
        return None
    env = host_setup._podman_provider_env()
    for cmd in (
        ["podman", "machine", "stop", s.podman_machine],
        ["podman", "machine", "rm", "--force", s.podman_machine],
    ):
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if r.returncode and cmd[2] == "rm":
            raise RuntimeError(r.stderr.strip() or f"{' '.join(cmd)} failed")
    # The provider pin goes with the machine.  Kept alone it would be
    # harmless; removed while the machine stays, a podman that defaults
    # to libkrun would stop listing the applehv machine at all.
    host_setup._PODMAN_PROVIDER_CONF.unlink(missing_ok=True)
    return s.podman_machine


def _macos_daemons() -> str | None:
    from .rootless import DNSMASQ_RELOAD_LABEL, DNSMASQ_RELOAD_PLIST

    # The reload job first: it signals the dnsmasq job.
    jobs = [
        (DNSMASQ_RELOAD_LABEL, DNSMASQ_RELOAD_PLIST),
        (host_setup.DNSMASQ_PLIST_LABEL, host_setup.DNSMASQ_PLIST_PATH),
        (
            host_setup.SOCKET_VMNET_PLIST_LABEL,
            host_setup.SOCKET_VMNET_PLIST_PATH,
        ),
    ]
    done: list[str] = []
    stuck: list[str] = []
    for label, plist in jobs:
        try:
            unloaded = _bootout(label)
        except RuntimeError as e:
            stuck.append(str(e))  # its plist stays, for launchd to use
            continue
        if _rm(plist) or unloaded:
            done.append(label)
    if stuck:
        raise RuntimeError("; ".join(stuck))
    return " ".join(done) or None


def _macos_files() -> str | None:
    from .rootless import DNSMASQ_RELOAD_SCRIPT

    removed = _rm(
        host_setup.DNSMASQ_CONF_PATH,
        host_setup.DNSMASQ_PID_PATH,
        DNSMASQ_RELOAD_SCRIPT,
    )
    removed += _rmdir_if_empty(DNSMASQ_RELOAD_SCRIPT.parent)
    for d in MACOS_LOG_DIRS:
        if _rm_tree(d):
            removed.append(str(d))
    return " ".join(removed) or None


def _macos_socket_dir() -> str | None:
    """The socket's directory install made inside socket_vmnet's keg.

    install_socket_vmnet_launchd_macos makes ``<keg>/var/run`` with
    sudo, since the formula ships no var/, and launchd puts root's socket
    in it.  ``brew uninstall socket_vmnet`` runs as the user and stops
    with "Could not remove socket_vmnet keg" on them, so they go here,
    once the daemon is gone.  A directory something else put files in
    stays.
    """
    if os.environ.get("LTVM_VMNET_SOCKET"):
        return None  # the user's own layout, not install's
    if host_setup._brew_socket_vmnet_prefix() is None:
        return None
    sock = host_setup.socket_vmnet_socket_path()
    removed = _rm(sock)
    for d in (sock.parent, sock.parent.parent):  # var/run, then var
        removed += _rmdir_if_empty(d)
    return " ".join(removed) or None


def _macos_image_tools() -> str | None:
    links: list[Path] = []
    for tool in IMAGE_TOOLS:
        link = IMAGE_TOOLS_DIR / tool
        if link.is_symlink() and "e2fsprogs" in os.readlink(link):
            links.append(link)
    return " ".join(_rm(*links)) or None


def _qemu_prefix() -> str | None:
    if not _exists(QEMU_PREFIX):
        return None
    if not _removable_tree(QEMU_PREFIX, _DEFAULT_QEMU_PREFIX):
        raise RuntimeError(
            f"left {QEMU_PREFIX}: LTVM_QEMU_PREFIX points somewhere "
            f"uninstall will not rm -rf"
        )
    _rm_tree(QEMU_PREFIX)
    return str(QEMU_PREFIX)


def _iptables_rules(subnet: str) -> list[list[str]]:
    """The rules qemu-bridge.service adds, as ``-D`` argument lists."""
    net = f"{subnet}.0/24"
    return [
        ["-t", "nat", "-D", "POSTROUTING", "-s", net, "!", "-d", net,
         "-j", "MASQUERADE"],
        ["-D", "FORWARD", "-i", rootless.BRIDGE, "-j", "ACCEPT"],
        ["-D", "FORWARD", "-o", rootless.BRIDGE, "-j", "ACCEPT"],
    ]  # fmt: skip


def _linux_services(s: Survey) -> str | None:
    """Remove installed network services after their guest TAPs are gone."""
    done: list[str] = []
    for unit in (
        host_setup.LTVM_DNSMASQ_UNIT,
        host_setup.RAIL_UNIT,
        BRIDGE_UNIT,
    ):
        if host_setup._systemctl_ok("cat", unit):
            # Stopping qemu-bridge runs its ExecStop, which deletes fcbr0.
            _quiet(["systemctl", "disable", "--now", unit])
            done.append(unit)
    if host_setup.RAIL_UNIT_PATH.exists():
        # A failed service stop must not leave owned data bridges behind silently.
        from .rail_host import remove_bridges

        if os.geteuid() == 0:
            remove_bridges()
        else:
            # Elevate this API adapter because uninstall itself runs as the user.
            sudo_run(
                [
                    "env",
                    f"PYTHONPATH={host_setup.REPO_ROOT}",
                    sys.executable,
                    "-m",
                    "ltvm_pkg.rail_host",
                    "stop",
                ],
                quiet=True,
            )
    if _rm(
        host_setup.LTVM_DNSMASQ_UNIT_PATH,
        host_setup.RAIL_UNIT_PATH,
        BRIDGE_UNIT_PATH,
    ):
        _quiet(["systemctl", "daemon-reload"])
    if which("ip") and not _quiet(["ip", "link", "show", "fcbr0"]).returncode:
        _quiet(["ip", "link", "del", "fcbr0"])
        done.append("fcbr0")
    # ExecStop leaves the iptables rules; -C before each -D, as the
    # unit's own -C before -A, and again in case one was added twice.
    if which("iptables") and s.subnet:
        for rule in _iptables_rules(s.subnet):
            check = [a if a != "-D" else "-C" for a in rule]
            while not _quiet(["iptables", *check]).returncode:
                if _quiet(["iptables", *rule]).returncode:
                    break
                if "iptables rules" not in done:
                    done.append("iptables rules")
    return " ".join(done) or None


def _linux_firewall() -> str | None:
    from . import host_firewall

    done: list[str] = []
    failed: list[str] = []
    for kind in host_firewall.installed():
        changed, bad = host_firewall.untrust_bridge(kind)
        if changed:
            done.append(kind)
        failed += bad
    if failed:
        raise RuntimeError("; ".join(failed))
    return " ".join(done) or None


def _linux_host_dnsmasq() -> str | None:
    """Give the host's own dnsmasq fcbr0 back, as it was.

    An older ltvm wrote its own config into the host's instance, which
    `install --network` takes out again; a host never reinstalled has it.
    """
    removed = _rm(
        host_setup.HOST_DNSMASQ_EXCEPT, host_setup.LEGACY_DNSMASQ_CONF
    )
    if not removed:
        return None
    # try-restart: only a running dnsmasq.service rereads its drop-ins.
    _quiet(["systemctl", "try-restart", "dnsmasq.service"])
    return " ".join(removed)


def _linux_files() -> str | None:
    removed = _rm(
        host_setup.LTVM_DNSMASQ_CONF,
        host_setup.LTVM_DNSMASQ_PID,
        SYSCTL_CONF,
        QEMU_PROFILE,
        SUDOERS_FRAGMENT,
        DK_FILTER,
    )
    removed += _rmdir_if_empty(host_setup.LTVM_DNSMASQ_CONF.parent)
    return " ".join(removed) or None


def _linux_bridge_acl() -> str | None:
    acl = rootless.BRIDGE_ACL
    try:
        text = acl.read_text()
    except OSError:
        return None
    from .rail_host import bridge_names

    ours = {f"allow {name}" for name in bridge_names()}
    lines = text.splitlines()
    kept: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() == f"allow {rootless.BRIDGE}":
            continue
        if (
            line == "# ltvm data bridge"
            and index + 1 < len(lines)
            and lines[index + 1].strip() in ours
        ):
            continue
        if (
            index
            and lines[index - 1] == "# ltvm data bridge"
            and line.strip() in ours
        ):
            # Preexisting administrator entries have no ownership marker.
            continue
        kept.append(line)
    if len(kept) == len(text.splitlines()):
        return None
    if any(ln.strip() for ln in kept):
        atomic_write(acl, "\n".join(kept) + "\n", 0o644)
    else:
        _rm(acl)
    return f"ltvm bridge rules in {acl}"


def _linux_selinux() -> str | None:
    if not which("semanage"):
        return None
    spec = host_setup.hosts_dir_fcontext_spec(VM_DIR / "hosts.d")
    if _quiet(["semanage", "fcontext", "-d", spec]).returncode:
        return None
    return f"fcontext {spec}"


def _linux_root_ssh() -> str | None:
    if os.geteuid() == 0:
        try:
            text = ROOT_SSH_CONFIG.read_text()
        except OSError:
            return None
    else:
        # /root is not ours to stat, let alone read.
        r = _quiet(["cat", str(ROOT_SSH_CONFIG)])
        if r.returncode:
            return None
        text = r.stdout
    if host_setup.SSH_BLOCK_MARKER not in text:
        return None
    atomic_write(ROOT_SSH_CONFIG, host_setup.strip_ssh_block(text), 0o600)
    return str(ROOT_SSH_CONFIG)


def _vm_dir() -> str | None:
    if not _exists(VM_DIR):
        return None
    if not _removable_tree(VM_DIR, _DEFAULT_VM_DIR):
        raise RuntimeError(
            f"left {VM_DIR}: LTVM_VM_DIR points somewhere uninstall "
            f"will not rm -rf"
        )
    _rm_tree(VM_DIR)
    return str(VM_DIR)


def _group(macos: bool) -> str | None:
    if not _group_exists(rootless.GROUP):
        return None
    if macos:
        sudo_run(["dseditgroup", "-o", "delete", rootless.GROUP], quiet=True)
    else:
        sudo_run(["groupdel", rootless.GROUP], quiet=True)
    return rootless.GROUP


def _completion() -> str | None:
    from . import shell_completion

    results = shell_completion.uninstall()
    failed = [f"{r.path}: {r.detail}" for r in results if r.status == "failed"]
    if failed:
        raise RuntimeError("; ".join(failed))
    return " ".join(str(r.path) for r in results) or None


def _skills() -> str | None:
    from . import skills
    from .cli.setup import _ltvm_repo_root

    result = skills.uninstall_skills(_ltvm_repo_root())
    return "; ".join(skills.describe(result)) or None


def _artifacts(s: Survey, c: Choices) -> str | None:
    if not s.artifacts or not c.artifacts:
        return None
    for p in s.artifacts:
        _user_rm_tree(p)
    return " ".join(str(p) for p in s.artifacts)


def _crashes(s: Survey, c: Choices) -> str | None:
    if s.crashes is None or not c.crashes:
        return None
    _user_rm_tree(s.crashes)
    return str(s.crashes)


def _user_state() -> str | None:
    """ltvm's config and state, but not suite results kept there."""
    removed: list[str] = []
    cfg = config_dir()
    if cfg.is_dir():
        _user_rm_tree(cfg)
        removed.append(str(cfg))
    state = state_dir()
    if state.is_dir():
        gone = [e for e in state.iterdir() if e.name != "suite"]
        for entry in gone:
            if entry.is_dir() and not entry.is_symlink():
                _user_rm_tree(entry)
            else:
                entry.unlink()
        if not any(state.iterdir()):
            state.rmdir()
            removed.append(str(state))
        else:
            removed.extend(str(e) for e in gone)
    return " ".join(removed) or None


def _launchers(s: Survey) -> str | None:
    return " ".join(_rm(*s.launchers)) or None


def steps(
    s: Survey, c: Choices, use_json: bool = False
) -> list[tuple[str, StepFn]]:
    """The removal, in order.

    VMs first, while everything they are torn down through is still
    there.  The launcher last, so a failure part way leaves a working
    `ltvm uninstall` to run again.
    """
    out: list[tuple[str, StepFn]] = [
        ("vms", lambda: _destroy_vms(s, use_json)),
        ("vm names", lambda: _forget_stray_names(s)),
        ("podman images", lambda: _remove_podman_images(s, c)),
        ("podman machine", lambda: _remove_podman_machine(s, c)),
    ]
    if s.macos:
        out += [
            ("launchd jobs", _macos_daemons),
            ("socket_vmnet socket", _macos_socket_dir),
            ("network files", _macos_files),
            ("image tools", _macos_image_tools),
        ]
    else:
        out += [
            ("network services", lambda: _linux_services(s)),
            ("firewall", _linux_firewall),
            ("host dnsmasq", _linux_host_dnsmasq),
            ("bridge acl", _linux_bridge_acl),
            ("selinux label", _linux_selinux),
            ("root ssh config", _linux_root_ssh),
            ("files", _linux_files),
        ]
    out += [
        ("qemu", _qemu_prefix),
        ("vm dir", _vm_dir),
        # Anything that belongs to the ltvm group, or runs as a user
        # whose group it is, has to be gone before this step.
        ("group", lambda: _group(s.macos)),
        ("completion", _completion),
        ("skills", _skills),
        ("artifacts", lambda: _artifacts(s, c)),
        ("crashes", lambda: _crashes(s, c)),
        ("config and state", _user_state),
        ("launcher", lambda: _launchers(s)),
    ]
    return out


def kept(s: Survey, c: Choices) -> list[Step]:
    """What the user chose to keep, as steps for the report."""
    out: list[Step] = []
    if s.artifacts and not c.artifacts:
        out.append(Step("artifacts", KEPT, " ".join(map(str, s.artifacts))))
    if s.crashes is not None and not c.crashes:
        out.append(Step("crashes", KEPT, str(s.crashes)))
    if s.podman_machine is not None and not c.podman_machine:
        out.append(Step("podman machine", KEPT, s.podman_machine))
    if (s.podman_tags or s.podman_volumes) and not (
        c.podman_images or machine_goes(s, c)
    ):
        n = len(s.podman_tags) + len(s.podman_volumes)
        out.append(Step("podman images", KEPT, f"{n} images and volumes"))
    return out


def check(s: Survey) -> None:
    """Raise Refused when uninstall cannot go ahead on this host."""
    if s.claimed:
        raise Refused(
            "VMs another session is using:\n  " + "\n  ".join(s.claimed)
        )


def run(s: Survey, c: Choices, use_json: bool = False) -> list[Step]:
    """Remove everything, step by step; one failure does not stop the rest."""
    check(s)
    report: list[Step] = []
    stopped = False
    for name, fn in steps(s, c, use_json):
        if stopped:
            report.append(Step(name, SKIPPED, "the VMs were not all destroyed"))
            continue
        try:
            detail = fn()
        except Exception as e:  # noqa: BLE001
            log.warning("uninstall: %s: %s", name, e)
            report.append(Step(name, FAILED, str(e)))
            # A VM left running would keep its QEMU while everything
            # that finds it -- its .info, its socket, the network --
            # goes.  Stop here and let a second run retry.
            stopped = name == "vms"
            continue
        if detail is None:
            report.append(Step(name, ABSENT))
        else:
            log.info("Removed %s: %s", name, detail)
            report.append(Step(name, DONE, detail))
    # A step the user chose to keep reports that, not "absent".
    keeps = kept(s, c)
    names = {k.name for k in keeps}
    return [r for r in report if r.name not in names] + keeps
