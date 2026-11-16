"""Tests for `ltvm uninstall` (ltvm_pkg/host_uninstall.py, cli.setup).

Nothing here may touch the host: a regression would uninstall the
developer's real ltvm.  Every path the module removes is moved under
tmp_path, ``sudo_run`` is a recorder that only ever deletes under
tmp_path, and a bare ``subprocess.run`` from the module fails the test.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ltvm_pkg import (
    host_firewall,
    host_setup,
    host_uninstall,
    rootless,
    shmrdma,
)
from ltvm_pkg.host_uninstall import Choices, Survey

# conftest stubs this for every test; the survey reads the real one.
_HOSTS_ENTRIES = rootless.hosts_entries


class FakeSudo:
    """Records each elevated command; really runs only rm/rmdir in tmp."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[list[str]] = []
        # argv prefix -> CompletedProcess, for commands that answer.
        self.answers: dict[
            tuple[str, ...], subprocess.CompletedProcess[str]
        ] = {}

    def answer(
        self, prefix: tuple[str, ...], rc: int = 0, out: str = ""
    ) -> None:
        self.answers[prefix] = subprocess.CompletedProcess(
            list(prefix), rc, out, ""
        )

    def __call__(
        self,
        cmd: list[str],
        *,
        check: bool = True,
        quiet: bool = False,
        **_: Any,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        if cmd[0] in ("rm", "rmdir"):
            for arg in cmd[1:]:
                if arg.startswith("-"):
                    continue
                p = Path(arg)
                assert self.root in p.parents, f"refusing to rm {p}"
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink(missing_ok=True)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        for prefix, result in self.answers.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                if check and result.returncode:
                    raise RuntimeError(f"Command failed: {cmd}")
                return result
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def ran(self, *prefix: str) -> bool:
        return any(tuple(c[: len(prefix)]) == prefix for c in self.calls)


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSudo:
    """A host laid out under tmp_path, and no way out of it."""
    root = tmp_path / "host"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SUDO_USER", raising=False)
    for var, sub in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_CACHE_HOME", ".cache"),
    ):
        monkeypatch.setenv(var, str(home / sub))

    vm_dir = root / "opt/qemu-vms"
    qemu = root / "opt/qemu"
    m = monkeypatch.setattr
    m(shmrdma, "_state_home", lambda uid: home / ".local/state")
    m(host_uninstall, "VM_DIR", vm_dir)
    m(host_uninstall, "_DEFAULT_VM_DIR", vm_dir)
    m(host_uninstall, "QEMU_PREFIX", qemu)
    m(host_uninstall, "_DEFAULT_QEMU_PREFIX", qemu)
    for name, rel in (
        ("LAUNCHER", "usr/local/bin/ltvm"),
        ("DK_FILTER", "usr/local/bin/dk-filter"),
        ("SUDOERS_FRAGMENT", "etc/sudoers.d/ltvm"),
        ("QEMU_PROFILE", "etc/profile.d/qemu-vms.sh"),
        ("SYSCTL_CONF", "etc/sysctl.d/99-qemu-vms.conf"),
        ("BRIDGE_UNIT_PATH", "etc/systemd/system/qemu-bridge.service"),
        ("ROOT_SSH_CONFIG", "root/.ssh/config"),
        ("IMAGE_TOOLS_DIR", "usr/local/bin"),
        ("HOSTS_FILE", "etc/hosts"),
    ):
        m(host_uninstall, name, root / rel)
    m(
        host_uninstall,
        "MACOS_LOG_DIRS",
        (root / "var/log/ltvm-dnsmasq", root / "var/log/socket_vmnet"),
    )
    for name, rel in (
        ("DNSMASQ_PLIST_PATH", "Library/LaunchDaemons/dnsmasq.plist"),
        ("SOCKET_VMNET_PLIST_PATH", "Library/LaunchDaemons/socket_vmnet.plist"),
        ("DNSMASQ_CONF_PATH", "usr/local/etc/ltvm-dnsmasq.conf"),
        ("DNSMASQ_PID_PATH", "var/run/ltvm-dnsmasq.pid"),
        ("LTVM_DNSMASQ_UNIT_PATH", "etc/systemd/system/ltvm-dnsmasq.service"),
        ("LTVM_DNSMASQ_CONF", "etc/ltvm/dnsmasq.conf"),
        ("LTVM_DNSMASQ_PID", "run/ltvm-dnsmasq.pid"),
        ("HOST_DNSMASQ_EXCEPT", "etc/dnsmasq.d/ltvm-fcbr0.conf"),
        ("LEGACY_DNSMASQ_CONF", "etc/dnsmasq.d/qemu-vms.conf"),
        ("_PODMAN_PROVIDER_CONF", "home/containers.conf.d/ltvm.conf"),
    ):
        m(host_setup, name, root / rel)
    m(
        rootless,
        "DNSMASQ_RELOAD_PLIST",
        root / "Library/LaunchDaemons/reload.plist",
    )
    m(
        rootless,
        "DNSMASQ_RELOAD_SCRIPT",
        root / "Library/Application Support/ltvm/ltvm-dnsmasq-reload",
    )
    m(rootless, "BRIDGE_ACL", root / "etc/qemu/bridge.conf")
    m(rootless, "HOSTS_DIR", vm_dir / "hosts.d")
    m(rootless, "hosts_entries", _HOSTS_ENTRIES)

    import ltvm_pkg.target_config as tc
    import ltvm_pkg.vm_state as vs

    m(tc, "ARTIFACTS_DIR", root / "repo/artifacts")
    m(vs, "SOCKETS", vm_dir / "sockets")

    sudo = FakeSudo(root)
    m(host_uninstall, "sudo_run", sudo)
    m("ltvm_pkg.priv.sudo_run", sudo)
    m("ltvm_pkg.priv.sudo_prime", lambda reason: None)

    def no_subprocess(*a: Any, **kw: Any) -> None:
        raise AssertionError(f"unstubbed subprocess.run: {a}")

    m(host_uninstall.subprocess, "run", no_subprocess)
    m(host_uninstall, "_launchd_loaded", lambda label: False)
    m(host_uninstall, "_group_exists", lambda name: True)
    m(host_uninstall.shutil, "which", lambda name, **kw: None)
    m(host_setup, "_systemctl_ok", lambda *a: False)
    m(
        "ltvm_pkg.skills.uninstall_skills",
        lambda repo, home=None: {"removed": {}},
    )
    m(host_firewall, "installed", lambda: [])
    m(host_setup, "_brew_socket_vmnet_prefix", lambda: None)
    monkeypatch.delenv("LTVM_VMNET_SOCKET", raising=False)
    return sudo


def _touch(p: Path, text: str = "") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _statuses(report: list[host_uninstall.Step]) -> dict[str, str]:
    return {r.name: r.status for r in report}


# ------------------------------------------------------------------
# Pieces shared with install
# ------------------------------------------------------------------


def test_strip_ssh_block_keeps_other_stanzas() -> None:
    text = (
        "Host a\n    User x\n\n"
        f"{host_setup.SSH_BLOCK_MARKER}\nHost 192.168.100.*\n    User root\n"
        "Host b\n    User y\n"
    )
    assert host_setup.strip_ssh_block(text) == (
        "Host a\n    User x\n\nHost b\n    User y\n"
    )


def test_ufw_delete_puts_delete_after_route() -> None:
    assert host_firewall._ufw_delete(("allow", "in", "on", "fcbr0")) == [
        "ufw", "delete", "allow", "in", "on", "fcbr0",
    ]  # fmt: skip
    assert host_firewall._ufw_delete(("route", "allow", "out", "on", "fcbr0")) == [
        "ufw", "route", "delete", "allow", "out", "on", "fcbr0",
    ]  # fmt: skip


def test_untrust_bridge_ufw_tolerates_missing_rules(host: FakeSudo) -> None:
    for prefix in (("ufw", "delete"), ("ufw", "route", "delete")):
        host.answer(prefix, rc=1, out="Could not delete non-existent rule")
    assert host_firewall.untrust_bridge("ufw") == (False, [])
    assert sum(c[0] == "ufw" for c in host.calls) == len(
        host_firewall.UFW_RULES
    )


def test_untrust_bridge_firewalld_only_when_trusted(host: FakeSudo) -> None:
    for extra in ((), ("--permanent",)):
        host.answer(
            ("firewall-cmd", *extra, "--zone=trusted",
             "--query-interface=fcbr0"),
            rc=1,
        )  # fmt: skip
    with patch.object(host_firewall, "_firewalld_running", return_value=True):
        assert host_firewall.untrust_bridge("firewalld") == (False, [])
    assert not any("--remove-interface=fcbr0" in c for c in host.calls)

    # fcbr0 is gone, so only the permanent binding is left.
    host.answer(
        ("firewall-cmd", "--permanent", "--zone=trusted",
         "--query-interface=fcbr0"),
    )  # fmt: skip
    with patch.object(host_firewall, "_firewalld_running", return_value=True):
        assert host_firewall.untrust_bridge("firewalld") == (True, [])
    assert host.ran(
        "firewall-cmd",
        "--permanent",
        "--zone=trusted",
        "--remove-interface=fcbr0",
    )
    assert not host.ran(
        "firewall-cmd", "--zone=trusted", "--remove-interface=fcbr0"
    )


def test_untrust_bridge_stopped_firewalld_edits_saved_config(
    host: FakeSudo,
) -> None:
    with patch.object(host_firewall, "_firewalld_running", return_value=False):
        assert host_firewall.untrust_bridge("firewalld") == (True, [])
    assert host.ran(
        "firewall-offline-cmd", "--zone=trusted", "--remove-interface=fcbr0"
    )
    assert not host.ran("firewall-cmd")


def test_linux_firewall_undoes_every_installed_firewall(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ufw is disabled and firewalld stopped: their saved rules still go."""
    monkeypatch.setattr(
        host_firewall, "installed", lambda: ["firewalld", "ufw"]
    )
    monkeypatch.setattr(host_firewall, "_firewalld_running", lambda: False)
    assert host_uninstall._linux_firewall() == "firewalld ufw"
    assert host.ran("ufw", "delete", "allow", "in", "on", "fcbr0")


# ------------------------------------------------------------------
# Guards
# ------------------------------------------------------------------


def test_removable_tree_refuses_dangerous_overrides(
    host: FakeSudo, tmp_path: Path
) -> None:
    default = Path("/opt/qemu-vms")
    assert host_uninstall._removable_tree(default, default)
    assert not host_uninstall._removable_tree(Path("/usr"), default)
    assert not host_uninstall._removable_tree(Path("/"), default)
    home = Path.home()
    assert not host_uninstall._removable_tree(home, default)
    assert not host_uninstall._removable_tree(home.parent, default)
    custom = tmp_path / "data" / "vms"
    custom.mkdir(parents=True)
    assert host_uninstall._removable_tree(custom, default)
    link = tmp_path / "link"
    link.symlink_to(custom)
    assert not host_uninstall._removable_tree(link, default)
    assert not host_uninstall._removable_tree(Path("/usr/local"), default)
    assert not host_uninstall._removable_tree(Path("/etc"), default)


def test_removable_tree_refuses_other_homes(
    host: FakeSudo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "homes" / "bob"
    (other / "vms").mkdir(parents=True)
    pw = type("pw", (), {"pw_dir": str(other)})
    monkeypatch.setattr(host_uninstall.pwd, "getpwall", lambda: [pw])
    default = Path("/opt/qemu-vms")
    assert not host_uninstall._removable_tree(other, default)
    assert not host_uninstall._removable_tree(other.parent, default)
    assert host_uninstall._removable_tree(other / "vms", default)


def test_moved_vm_dir_that_is_unsafe_is_left(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_uninstall, "VM_DIR", Path.home())
    with pytest.raises(RuntimeError, match="will not rm -rf"):
        host_uninstall._vm_dir()
    assert Path.home().is_dir()


def test_launcher_removed_only_when_ltvm_wrote_it(host: FakeSudo) -> None:
    launcher = _touch(
        host_uninstall.LAUNCHER,
        host_setup._render_ltvm_launcher("/usr/bin/python3", "/src/ltvm"),
    )
    assert host_uninstall._is_ltvm_launcher(launcher)
    launcher.write_text("#!/bin/sh\necho someone else's ltvm\n")
    assert not host_uninstall._is_ltvm_launcher(launcher)


def test_launcher_symlink_from_older_install_is_ltvms(
    host: FakeSudo, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "ltvm_pkg").mkdir(parents=True)
    _touch(checkout / "ltvm")
    link = host_uninstall.LAUNCHER
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(checkout / "ltvm")
    assert host_uninstall._is_ltvm_launcher(link)
    # The checkout is gone: the dangling link is still ours.
    shutil.rmtree(checkout)
    assert host_uninstall._is_ltvm_launcher(link)
    link.unlink()
    link.symlink_to(_touch(tmp_path / "other" / "ltvm"))
    assert not host_uninstall._is_ltvm_launcher(link)


# ------------------------------------------------------------------
# Survey and questions
# ------------------------------------------------------------------


def test_survey_reads_without_sudo(host: FakeSudo) -> None:
    vm_dir = host_uninstall.VM_DIR
    _touch(vm_dir / "subnet", "192.168.200\n")
    _touch(vm_dir / "sockets" / "co1-a.info")
    _touch(
        host_uninstall.HOSTS_FILE,
        "127.0.0.1 localhost\n"
        "192.168.200.5\tco1-a # qemu-vm:co1-a\n"
        "192.168.200.6\tco1-gone # qemu-vm:co1-gone\n",
    )
    _touch(vm_dir / "hosts.d" / "co1-old", "x")
    (host_uninstall.crash_dir()).mkdir()
    s = host_uninstall.survey(macos=False)
    assert s.vms == ["co1-a"]
    assert s.stray_names == ["co1-gone", "co1-old"]
    assert s.subnet == "192.168.200"
    assert s.crashes == host_uninstall.crash_dir()
    assert host.calls == []


def test_survey_without_podman_machine_is_checked(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        host_uninstall.shutil, "which", lambda name, **kw: "/bin/" + name
    )
    monkeypatch.setattr(host_setup, "_podman_machine_list_macos", lambda: [])
    s = host_uninstall.survey(macos=True)
    assert s.podman_machine is None
    assert s.podman_checked
    assert not any(
        "not checked" in ln for ln in host_uninstall.describe(s, Choices())
    )


def test_which_finds_sbin_tools_off_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ltvm_pkg import priv

    tool = _touch(tmp_path / "sbin" / "iptables")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(priv, "_SBIN_DIRS", (str(tool.parent),))
    assert priv.which("iptables") == str(tool)


def test_ensure_dir_goes_to_sudo_when_parent_is_unsearchable(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ltvm_pkg import priv

    def denied(self: Path, *a: Any, **kw: Any) -> bool:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(priv.Path, "is_dir", denied)
    monkeypatch.setattr(priv.Path, "mkdir", denied)
    priv.ensure_dir(Path("/root/.ssh"))
    assert host.ran("mkdir", "-p", "/root/.ssh")


def test_questions_only_for_what_is_there() -> None:
    s = Survey(macos=True)
    assert host_uninstall.questions(s) == []
    s.artifacts = [Path("/a")]
    s.podman_machine = "pm"
    assert [a for a, _ in host_uninstall.questions(s)] == [
        "artifacts",
        "podman_machine",
    ]


def test_describe_marks_each_choice() -> None:
    s = Survey(macos=True, artifacts=[Path("/a")], crashes=Path("/c"))
    text = "\n".join(host_uninstall.describe(s, Choices(artifacts=True)))
    assert "[delete] artifacts /a" in text
    assert "[ask] crash dumps /c" in text


# ------------------------------------------------------------------
# Running
# ------------------------------------------------------------------


def _macos_host(host: FakeSudo) -> None:
    for p in (
        rootless.DNSMASQ_RELOAD_PLIST,
        rootless.DNSMASQ_RELOAD_SCRIPT,
        host_setup.DNSMASQ_PLIST_PATH,
        host_setup.SOCKET_VMNET_PLIST_PATH,
        host_setup.DNSMASQ_CONF_PATH,
        host_setup.DNSMASQ_PID_PATH,
        host_uninstall.MACOS_LOG_DIRS[0] / "stderr",
        host_uninstall.QEMU_PREFIX / "bin" / "qemu-img",
        host_uninstall.VM_DIR / "sockets" / ".keep",
    ):
        _touch(p)
    tool = host_uninstall.IMAGE_TOOLS_DIR / "mke2fs"
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.symlink_to("/opt/homebrew/opt/e2fsprogs/sbin/mke2fs")
    other = host_uninstall.IMAGE_TOOLS_DIR / "e2fsck"
    other.symlink_to("/somewhere/else/e2fsck")


def test_macos_removes_what_install_made(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _macos_host(host)
    loaded = {
        rootless.DNSMASQ_RELOAD_LABEL,
        host_setup.DNSMASQ_PLIST_LABEL,
        host_setup.SOCKET_VMNET_PLIST_LABEL,
    }

    def fake_loaded(label: str) -> bool:
        return label in loaded

    def bootout(cmd: list[str], **kw: Any) -> Any:
        if cmd[:2] == ["launchctl", "bootout"]:
            loaded.discard(cmd[2].removeprefix("system/"))
        return FakeSudo.__call__(host, cmd, **kw)

    monkeypatch.setattr(host_uninstall, "_launchd_loaded", fake_loaded)
    monkeypatch.setattr(host_uninstall, "sudo_run", bootout)

    report = host_uninstall.run(Survey(macos=True), Choices())
    st = _statuses(report)
    assert host_uninstall.FAILED not in st.values(), report
    assert not loaded
    assert not host_setup.DNSMASQ_PLIST_PATH.exists()
    assert not host_setup.SOCKET_VMNET_PLIST_PATH.exists()
    assert not rootless.DNSMASQ_RELOAD_PLIST.exists()
    # The job's program and the directory install made for it.
    assert not rootless.DNSMASQ_RELOAD_SCRIPT.parent.exists()
    assert not host_setup.DNSMASQ_CONF_PATH.exists()
    assert not host_uninstall.MACOS_LOG_DIRS[0].exists()
    assert not host_uninstall.QEMU_PREFIX.exists()
    assert not host_uninstall.VM_DIR.exists()
    assert not (host_uninstall.IMAGE_TOOLS_DIR / "mke2fs").is_symlink()
    assert (host_uninstall.IMAGE_TOOLS_DIR / "e2fsck").is_symlink()
    assert host.ran("dseditgroup", "-o", "delete", "ltvm")
    # The dnsmasq-reload job goes before the job it signals.
    outs = [c[2] for c in host.calls if c[:2] == ["launchctl", "bootout"]]
    assert outs.index(f"system/{rootless.DNSMASQ_RELOAD_LABEL}") < outs.index(
        f"system/{host_setup.DNSMASQ_PLIST_LABEL}"
    )


def test_linux_removes_what_install_made(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    for p in (
        host_setup.LTVM_DNSMASQ_UNIT_PATH,
        host_uninstall.BRIDGE_UNIT_PATH,
        host_setup.LTVM_DNSMASQ_CONF,
        host_uninstall.SYSCTL_CONF,
        host_uninstall.QEMU_PROFILE,
        host_uninstall.SUDOERS_FRAGMENT,
        host_uninstall.DK_FILTER,
        host_setup.HOST_DNSMASQ_EXCEPT,
        host_setup.LEGACY_DNSMASQ_CONF,
    ):
        _touch(p)
    _touch(rootless.BRIDGE_ACL, "allow virbr0\nallow fcbr0\n")
    ssh_text = (
        f"Host x\n    User y\n\n{host_setup.SSH_BLOCK_MARKER}\n"
        "Host 192.168.100.*\n    User root\n"
    )
    host.answer(("cat", str(host_uninstall.ROOT_SSH_CONFIG)), out=ssh_text)
    host.answer(("ip", "link", "show", "fcbr0"), rc=1)
    # Each rule is there once: -C succeeds until its -D has run.
    present = {tuple(r) for r in host_uninstall._iptables_rules("192.168.100")}

    def sudo(cmd: list[str], **kw: Any) -> Any:
        if cmd[0] == "iptables":
            rule = tuple("-D" if a == "-C" else a for a in cmd[1:])
            hit = rule in present
            if "-D" in cmd:
                present.discard(rule)
            host.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0 if hit else 1, "", "")
        return FakeSudo.__call__(host, cmd, **kw)

    written: dict[Path, tuple[str, int]] = {}
    monkeypatch.setattr(host_uninstall, "sudo_run", sudo)
    monkeypatch.setattr(
        host_uninstall,
        "atomic_write",
        lambda p, text, mode=0o644: written.__setitem__(p, (text, mode)),
    )
    monkeypatch.setattr(host_setup, "_systemctl_ok", lambda *a: True)
    monkeypatch.setattr(
        host_uninstall.shutil, "which", lambda n, **kw: f"/usr/sbin/{n}"
    )
    monkeypatch.setattr(host_uninstall.os, "geteuid", lambda: 1000)

    s = Survey(macos=False, subnet="192.168.100")
    report = host_uninstall.run(s, Choices())
    st = _statuses(report)
    assert host_uninstall.FAILED not in st.values(), report
    assert host.ran("systemctl", "disable", "--now", "qemu-bridge.service")
    assert host.ran("systemctl", "disable", "--now", "ltvm-dnsmasq.service")
    assert host.ran("systemctl", "try-restart", "dnsmasq.service")
    assert not present, "an iptables rule was left"
    assert not host_uninstall.SUDOERS_FRAGMENT.exists()
    assert not host_setup.LEGACY_DNSMASQ_CONF.exists()
    assert not host_setup.LTVM_DNSMASQ_CONF.parent.exists()
    assert written[rootless.BRIDGE_ACL] == ("allow virbr0\n", 0o644)
    assert written[host_uninstall.ROOT_SSH_CONFIG] == (
        "Host x\n    User y\n\n",
        0o600,
    )
    assert host.ran("groupdel", "ltvm")


def test_a_failed_step_does_not_stop_the_rest(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _touch(host_uninstall.VM_DIR / "x")

    def boom() -> None:
        raise RuntimeError("no")

    monkeypatch.setattr(host_uninstall, "_macos_daemons", boom)
    st = _statuses(host_uninstall.run(Survey(macos=True), Choices()))
    assert st["launchd jobs"] == host_uninstall.FAILED
    assert st["vm dir"] == host_uninstall.DONE
    assert not host_uninstall.VM_DIR.exists()


def test_shmrdma_daemons_stopped(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each user's daemon goes; another user's through sudo."""
    from ltvm_pkg import shmrdma

    me = os.geteuid()
    mine = host_uninstall.VM_DIR / f"shmrdma-{me}"
    theirs = host_uninstall.VM_DIR / f"shmrdma-{me + 1}"
    mine.mkdir(parents=True)
    theirs.mkdir()
    (host_uninstall.VM_DIR / "shmrdma-x").mkdir()
    direct: list[tuple[Path, int]] = []

    def stop_dir(d: Path, uid: int) -> str | None:
        direct.append((d, uid))
        return "pid 5" if d == mine else None

    monkeypatch.setattr(shmrdma, "stop_dir", stop_dir)
    host.answer((sys.executable, "-I", "-c"), out="pid 6\n")
    got = host_uninstall._shmrdma_daemons()
    assert got == f"{mine} (pid 5) {theirs} (pid 6)"
    assert (mine, me) in direct
    assert all(d != theirs for d, _ in direct)
    assert host.ran(sys.executable, "-I", "-c")
    assert [c[-2:] for c in host.calls] == [[str(theirs), str(me + 1)]]


def test_shmrdma_home_dir_removed(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The daemon's directory in the home goes even when the state
    directory is elsewhere."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    stopped: list[Path] = []
    monkeypatch.setattr(
        shmrdma, "stop_dir", lambda d, uid: stopped.append(d) or None
    )
    home_dir = shmrdma._candidate_dirs(os.geteuid())[1]
    _touch(home_dir / "shmrdmad.log")
    assert host_uninstall._shmrdma_daemons() == f"{home_dir} (removed)"
    assert stopped == [home_dir]
    assert not home_dir.exists()


def test_root_shmrdmad_stop_ignores_the_working_directory(
    host: FakeSudo, tmp_path: Path
) -> None:
    """The helper root runs imports nothing from the caller's cwd."""
    me = os.geteuid()
    (host_uninstall.VM_DIR / f"shmrdma-{me + 1}").mkdir(parents=True)
    host.answer((sys.executable, "-I", "-c"), out="")
    host_uninstall._shmrdma_daemons()
    [cmd] = host.calls
    plant = tmp_path / "plant"
    plant.mkdir()
    marker = tmp_path / "planted"
    (plant / "getpass.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    env = dict(os.environ, PYTHONPATH=str(plant))
    cmd = [*cmd[:-2], str(tmp_path / "none"), str(me + 1)]
    with subprocess.Popen(
        cmd, cwd=plant, env=env, stderr=subprocess.PIPE, text=True
    ) as p:
        _, err = p.communicate(timeout=60)
    assert p.returncode == 0, err
    assert not marker.exists()


def test_claimed_vms_refuse_before_anything(host: FakeSudo) -> None:
    s = Survey(macos=True, claimed=["co1-a is claimed by another session"])
    with pytest.raises(host_uninstall.Refused):
        host_uninstall.run(s, Choices())
    assert host.calls == []


def test_vms_destroyed_in_one_call(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def destroy(ns: argparse.Namespace) -> int:
        seen.append(list(ns.names))
        return 0

    monkeypatch.setattr("ltvm_pkg.cli.setup.cmd_destroy", destroy)
    s = Survey(macos=True, vms=["co1-a", "co1-b"])
    assert host_uninstall._destroy_vms(s, False) == "co1-a co1-b"
    assert seen == [["co1-a", "co1-b"]]


def test_vm_destroy_die_is_a_failed_step(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    def destroy(ns: argparse.Namespace) -> int:
        raise SystemExit(1)

    monkeypatch.setattr("ltvm_pkg.cli.setup.cmd_destroy", destroy)
    with pytest.raises(RuntimeError, match="exited 1"):
        host_uninstall._destroy_vms(Survey(macos=True, vms=["a"]), False)


# ------------------------------------------------------------------
# The user's data
# ------------------------------------------------------------------


def _data(host: FakeSudo) -> Survey:
    import ltvm_pkg.target_config as tc

    art = _touch(Path(tc.ARTIFACTS_DIR) / "rocky9" / "meta.json").parents[1]
    crashes = _touch(host_uninstall.crash_dir() / "vmcore").parent
    return Survey(macos=True, artifacts=[art], crashes=crashes)


def test_linux_has_no_machine_to_take_the_images(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> Any:
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(host_uninstall.subprocess, "run", run)
    s = Survey(macos=False, podman_tags=["ltvm-build-rocky9"])
    c = Choices(podman_machine=True, podman_images=True)
    host_uninstall._remove_podman_images(s, c)
    assert ["podman", "rmi", "ltvm-build-rocky9"] in ran
    kept = host_uninstall.kept(
        s, Choices(podman_machine=True, podman_images=False)
    )
    assert [k.name for k in kept] == ["podman images"]
    assert "podman images" in host_uninstall.confirmation(
        s, Choices(podman_machine=True, podman_images=False)
    )


def test_a_stuck_launchd_job_does_not_keep_the_others(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ltvm_pkg.rootless import DNSMASQ_RELOAD_LABEL

    def bootout(label: str) -> bool:
        if label == DNSMASQ_RELOAD_LABEL:
            raise RuntimeError(f"launchd still has {label} loaded")
        return True

    monkeypatch.setattr(host_uninstall, "_bootout", bootout)
    reload_plist = _touch(rootless.DNSMASQ_RELOAD_PLIST)
    dnsmasq_plist = _touch(host_setup.DNSMASQ_PLIST_PATH)
    with pytest.raises(RuntimeError, match=DNSMASQ_RELOAD_LABEL):
        host_uninstall._macos_daemons()
    assert reload_plist.exists()
    assert not dnsmasq_plist.exists()


def test_kept_by_default(host: FakeSudo) -> None:
    s = _data(host)
    report = host_uninstall.run(s, Choices(artifacts=False, crashes=False))
    assert (
        s.artifacts[0].is_dir() and s.crashes is not None and s.crashes.is_dir()
    )
    assert [r.name for r in report if r.status == host_uninstall.KEPT] == [
        "artifacts",
        "crashes",
    ]
    assert len({r.name for r in report}) == len(report)


def test_deleted_when_chosen(host: FakeSudo) -> None:
    s = _data(host)
    host_uninstall.run(s, Choices(artifacts=True, crashes=True))
    assert not s.artifacts[0].exists()
    assert s.crashes is not None and not s.crashes.exists()


def test_podman_machine_takes_images_and_provider_pin(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = _touch(host_setup._PODMAN_PROVIDER_CONF, "[machine]\n")
    ran: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> Any:
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(host_uninstall.subprocess, "run", run)
    s = Survey(
        macos=True, podman_machine="pm", podman_tags=["ltvm-build-rocky9"]
    )
    host_uninstall._remove_podman_images(
        s, Choices(podman_machine=True, podman_images=True)
    )
    host_uninstall._remove_podman_machine(s, Choices(podman_machine=True))
    assert ["podman", "rmi", "ltvm-build-rocky9"] not in ran
    assert ["podman", "machine", "rm", "--force", "pm"] in ran
    assert not pin.exists()


def test_kept_machine_keeps_provider_pin(host: FakeSudo) -> None:
    pin = _touch(host_setup._PODMAN_PROVIDER_CONF, "[machine]\n")
    s = Survey(macos=True, podman_machine="pm")
    assert (
        host_uninstall._remove_podman_machine(s, Choices(podman_machine=False))
        is None
    )
    assert pin.exists()


def test_images_removed_without_force(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> Any:
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(host_uninstall.subprocess, "run", run)
    s = Survey(
        macos=False,
        podman_tags=["ltvm-build-a"],
        podman_volumes=["ltvm-kernel-x"],
    )
    host_uninstall._remove_podman_images(s, Choices(podman_images=True))
    assert ran == [
        ["podman", "rmi", "ltvm-build-a"],
        ["podman", "volume", "rm", "ltvm-kernel-x"],
    ]


def test_user_state_keeps_suite_results(host: FakeSudo) -> None:
    _touch(host_uninstall.config_dir() / "config.json", "{}")
    _touch(host_uninstall.state_dir() / "telemetry.json", "{}")
    results = _touch(
        host_uninstall.state_dir() / "suite" / "r1" / "results.yml"
    )
    telemetry = host_uninstall.state_dir() / "telemetry.json"
    detail = host_uninstall._user_state()
    assert not host_uninstall.config_dir().exists()
    assert not telemetry.exists()
    assert results.exists()
    assert detail is not None
    assert str(telemetry) in detail.split()
    assert str(host_uninstall.state_dir()) not in detail.split()
    # Only the results are left: nothing more to report.
    assert host_uninstall._user_state() is None


# ------------------------------------------------------------------
# The command
# ------------------------------------------------------------------


def _args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = dict(
        json=False,
        artifacts=None,
        crashes=None,
        podman_machine=None,
        podman_images=None,
        yes=False,
        dry_run=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_parses_delete_and_keep_flags() -> None:
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader(
        "ltvm_script", str(Path(__file__).resolve().parents[1] / "ltvm")
    )
    spec = importlib.util.spec_from_loader("ltvm_script", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    args = mod.build_parser().parse_args(
        ["uninstall", "--delete-artifacts", "--keep-podman-machine", "--yes"]
    )
    assert args.artifacts is True
    assert args.podman_machine is False
    assert args.crashes is None
    with pytest.raises(SystemExit):
        mod.build_parser().parse_args(
            ["uninstall", "--delete-crashes", "--keep-crashes"]
        )


def test_cli_refuses_under_sudo(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ltvm_pkg.cli import setup

    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "alice")
    assert setup.cmd_uninstall(_args(yes=True)) != 0
    assert host.calls == []


def test_cli_dry_run_changes_nothing(
    host: FakeSudo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ltvm_pkg.cli import setup

    _touch(host_uninstall.VM_DIR / "x")

    def no_prime(reason: str) -> None:
        raise AssertionError("dry run asked for sudo")

    monkeypatch.setattr("ltvm_pkg.priv.sudo_prime", no_prime)
    assert setup.cmd_uninstall(_args(dry_run=True)) == 0
    assert host.calls == []
    assert host_uninstall.VM_DIR.exists()
    assert "ltvm uninstall will remove" in capsys.readouterr().out


def test_cli_dry_run_with_yes_shows_what_yes_keeps(
    host: FakeSudo, capsys: pytest.CaptureFixture[str]
) -> None:
    from ltvm_pkg.cli import setup

    _touch(host_uninstall.crash_dir() / "vmcore")
    assert setup.cmd_uninstall(_args(dry_run=True, yes=True)) == 0
    out = capsys.readouterr().out
    assert "[keep] crash dumps" in out
    assert "[ask]" not in out


def test_cli_without_a_terminal_needs_yes(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ltvm_pkg.cli import setup

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert setup.cmd_uninstall(_args()) != 0
    assert host.calls == []


def test_cli_asks_then_runs(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ltvm_pkg.cli import setup

    _data(host)
    _touch(host_uninstall.VM_DIR / "x")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(host_setup, "is_macos", lambda: True)
    answers = iter(["", "y", "y"])  # artifacts: keep; crashes: delete; go
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert setup.cmd_uninstall(_args()) == 0
    import ltvm_pkg.target_config as tc

    assert Path(tc.ARTIFACTS_DIR).is_dir()
    assert not host_uninstall.crash_dir().exists()
    assert not host_uninstall.VM_DIR.exists()


def test_cli_declined_removes_nothing(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ltvm_pkg.cli import setup

    _touch(host_uninstall.VM_DIR / "x")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert setup.cmd_uninstall(_args()) == 0
    assert host_uninstall.VM_DIR.exists()
    assert host.calls == []


def test_a_failed_vm_destroy_stops_the_teardown(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _macos_host(host)

    def destroy(ns: argparse.Namespace) -> int:
        raise SystemExit(1)

    monkeypatch.setattr("ltvm_pkg.cli.setup.cmd_destroy", destroy)
    monkeypatch.setattr(host_uninstall, "_launchd_loaded", lambda label: True)
    report = host_uninstall.run(Survey(macos=True, vms=["co1-a"]), Choices())
    st = _statuses(report)
    assert st["vms"] == host_uninstall.FAILED
    assert st["vm dir"] == host_uninstall.SKIPPED
    assert host_uninstall.VM_DIR.exists()
    assert host.calls == []


def test_cli_json_with_vms_is_one_document(
    host: FakeSudo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from ltvm_pkg.cli import setup

    _touch(host_uninstall.VM_DIR / "sockets" / "co1-a.info")

    def destroy(ns: argparse.Namespace) -> int:
        print("destroyed co1-a")
        return 0

    monkeypatch.setattr(setup, "cmd_destroy", destroy)
    monkeypatch.setattr(host_setup, "is_macos", lambda: True)
    assert setup.cmd_uninstall(_args(yes=True, json=True)) == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc["ok"] is True
    assert "destroyed co1-a" in captured.err


def test_socket_dir_in_the_keg_goes(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    keg = host.root / "opt/homebrew/opt/socket_vmnet"
    _touch(keg / "bin" / "socket_vmnet")
    sock = _touch(keg / "var" / "run" / "socket_vmnet")
    monkeypatch.setattr(host_setup, "_brew_socket_vmnet_prefix", lambda: keg)
    host_uninstall._macos_socket_dir()
    assert not (keg / "var").exists()
    assert not sock.exists()
    assert (keg / "bin" / "socket_vmnet").exists()


def test_reload_script_dir_with_other_files_stays(host: FakeSudo) -> None:
    script = _touch(rootless.DNSMASQ_RELOAD_SCRIPT)
    other = _touch(script.parent / "keep")
    host_uninstall._macos_files()
    assert not script.exists()
    assert other.exists()
    assert not host.ran("rmdir")


def test_host_installed_before_the_reload_script(host: FakeSudo) -> None:
    """An older install ran launchctl directly: no script, no directory."""
    _touch(host_setup.DNSMASQ_CONF_PATH)
    removed = host_uninstall._macos_files() or ""
    assert str(rootless.DNSMASQ_RELOAD_SCRIPT) not in removed
    assert not host.ran("rmdir")


def test_socket_dir_with_other_files_stays(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    keg = host.root / "opt/homebrew/opt/socket_vmnet"
    _touch(keg / "bin" / "socket_vmnet")
    _touch(keg / "var" / "run" / "socket_vmnet")
    other = _touch(keg / "var" / "log" / "keep")
    monkeypatch.setattr(host_setup, "_brew_socket_vmnet_prefix", lambda: keg)
    host_uninstall._macos_socket_dir()
    assert not (keg / "var" / "run").exists()
    assert other.exists()


def test_socket_dir_left_for_a_custom_socket(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LTVM_VMNET_SOCKET", "/somewhere/sock")
    assert host_uninstall._macos_socket_dir() is None
    assert host.calls == []


def test_confirmation_names_what_goes_and_stays() -> None:
    s = Survey(
        macos=True,
        artifacts=[Path("/a")],
        crashes=Path("/c"),
        podman_machine="pm",
        podman_tags=["ltvm-build-x"],
    )
    q = host_uninstall.confirmation(
        s, Choices(artifacts=True, crashes=False, podman_machine=True)
    )
    assert q == (
        "Remove the ltvm setup listed above, delete the artifacts and the "
        "podman machine 'pm', and keep the crash dumps?"
    )
    q = host_uninstall.confirmation(s, Choices())
    assert q.startswith(
        "Remove the ltvm setup listed above, keeping the artifacts,"
    )
    assert "ltvm's podman images" in q
    assert host_uninstall.confirmation(Survey(macos=True), Choices()) == (
        "Remove the ltvm setup listed above?"
    )


def _brew(monkeypatch: pytest.MonkeyPatch, installed: str) -> None:
    monkeypatch.setattr(
        host_uninstall.shutil, "which", lambda n: "/opt/homebrew/bin/brew"
    )

    def run(cmd: list[str], **kw: Any) -> Any:
        assert cmd[1:] == ["list", "--formula", "-1"]
        return subprocess.CompletedProcess(cmd, 0, installed, "")

    monkeypatch.setattr(host_uninstall.subprocess, "run", run)


def test_left_in_place_gives_a_brew_command(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _brew(monkeypatch, "qemu\nsocket_vmnet\npodman\nwget\nbash\n")
    s = Survey(macos=True, podman_machine="pm")
    lines = host_uninstall.left_in_place(s, Choices(podman_machine=True))
    assert "  brew uninstall qemu socket_vmnet podman bash" in lines


def test_left_in_place_keeps_podman_with_its_machine(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _brew(monkeypatch, "qemu\npodman\n")
    s = Survey(macos=True, podman_machine="pm")
    lines = host_uninstall.left_in_place(s, Choices(podman_machine=False))
    assert "  brew uninstall qemu" in lines
    assert "podman stays" in lines[0]


def test_left_in_place_without_brew_formulae(
    host: FakeSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _brew(monkeypatch, "wget\n")
    lines = host_uninstall.left_in_place(Survey(macos=True), Choices())
    assert not any("brew uninstall" in ln for ln in lines)
