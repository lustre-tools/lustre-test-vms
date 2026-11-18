"""What shmrdma, shmnet NICs, --host-node and SMC need, and fixing it.

One list of checks serves three callers: ``ltvm doctor --shmrdma``
reports every one (and ``--fix`` repairs what it can), create and start
refuse a VM whose features miss one, and ``ltvm install`` installs the
host packages.  Each check says which features need it, so a VM that
only asks for --host-node is not refused for want of a C compiler.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import shmrdma, vm_state

SHMRDMA = "shmrdma"  # --shmrdma
SHMNET = "shmnet"  # --nic shmnet
HOST_NODE = "host-node"  # --host-node
SMC = "smc"  # SMC sockets over the device (not a flag; never refuses)
# ltvm's own shmrdmad, which it builds and runs: --shmrdma or a shmnet
# NIC without a socket given by hand.  Not a flag of its own.
OWN_DAEMON = "shmrdmad"
SHMRDMA_NIC = "shmrdma-nic"  # --nic shmrdma

# Host packages the features need, by the command that proves them.
# `ltvm install` installs these with its other prerequisites.
HOST_PACKAGES = {"gcc": "gcc", "taskset": "util-linux", "numactl": "numactl"}


@dataclass
class Req:
    """One requirement and whether this host and target meet it."""

    name: str
    features: frozenset[str]
    ok: bool
    detail: str
    # How to fix it: run by `doctor --fix`, or None when only the text
    # in ``how`` can (a person has to act).
    fix: Callable[[], None] | None = None
    how: str = ""
    hard: bool = True  # False: worth reporting, never a refusal
    # Asks again after ``fix`` ran, when the fix can succeed and still
    # leave the requirement unmet.
    recheck: Callable[[], bool] | None = None

    def needed_by(self, features: set[str]) -> bool:
        return bool(self.features & features)


@dataclass
class Target:
    """The artifacts a check of images and kernels looks at."""

    name: str
    kernel: str | None = None
    lustre_tree: str | None = None
    image: Path | None = None
    kernel_dir: Path | None = None
    arch: str | None = None
    problem: str = ""
    extra: dict[str, str] = field(default_factory=dict)
    # A VM's own record of whether its image has the guest module;
    # None asks the image's meta.json.
    has_kmod: bool | None = None


def resolve_target(
    name: str,
    kernel: str | None,
    lustre_tree: str | None = None,
    arch: str | None = None,
) -> Target:
    """The image and kernel ltvm would create a VM of *name* from: for
    *arch*, or the host's, as ``create`` picks it."""
    from .cli.util import host_arch

    arch = arch or host_arch()
    t = Target(name, kernel, lustre_tree, arch=arch)
    try:
        arts = vm_state.resolve_os_artifacts(name, arch=arch, kernel=kernel)
    except (RuntimeError, FileNotFoundError, ValueError, SystemExit) as e:
        t.problem = str(e) or f"no artifacts for {name}"
        return t
    t.image = Path(arts.image)
    t.kernel_dir = Path(arts.kernel).parent
    return t


def _native_arch() -> str:
    import platform

    arch = platform.machine()
    return {"arm64": "aarch64", "amd64": "x86_64"}.get(arch, arch)


def _qemu(arch: str | None = None) -> str:
    """The QEMU a guest of *arch* runs on (the host's arch if None)."""
    return vm_state.qemu_binary_for_arch(arch or _native_arch())


def _qemu_help(what: str, qemu: str | None = None) -> str | None:
    """``-<what> help``'s output, or None when QEMU did not answer."""
    qemu = qemu or _qemu()
    try:
        r = subprocess.run(
            # aarch64 QEMU has no default machine and answers
            # -netdev help only once one is named
            [qemu, "-M", "none", f"-{what}", "help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout


def _qemu_has_vhost_user_netdev(qemu: str | None = None) -> bool | None:
    """Whether QEMU was built with vhost-user, None if it did not say.
    ``-netdev help`` lists vhost-user even in a build without it
    (Homebrew's, on macOS), so ask for one: a build with it complains
    about the chardev instead."""
    qemu = qemu or _qemu()
    netdevs = _qemu_help("netdev", qemu)
    if netdevs is None:
        return None
    if "vhost-user" not in netdevs:
        return False
    try:
        r = subprocess.run(
            [
                qemu,
                "-M",
                "none",
                "-display",
                "none",
                "-netdev",
                "vhost-user,id=probe,chardev=ltvm-probe",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return "not compiled" not in r.stderr


def _caps_cache() -> Path:
    return shmrdma._cache_dir().parent / "qemu-caps.json"


def _qemu_caps(qemu: str) -> dict[str, bool]:
    """What *qemu* supports of what the features need, probed once per
    binary (path, size and mtime) and cached, since every start asks."""
    from .host_setup import is_macos
    from .qemu_run import qemu_binds_numa

    try:
        real = os.path.realpath(qemu)
        st = os.stat(real)
        ident = f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        real = ident = ""
    cache: dict = {}
    try:
        cache_path: Path | None = _caps_cache()
    except RuntimeError:
        cache_path = None
    if ident and cache_path:
        try:
            raw = json.loads(cache_path.read_text())
            cache = raw if isinstance(raw, dict) else {}
            hit = cache[real]
            if hit["id"] == ident:
                return {k: bool(v) for k, v in hit["caps"].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    devices = _qemu_help("device", qemu)
    objects = _qemu_help("object", qemu)
    netdev = _qemu_has_vhost_user_netdev(qemu)
    caps = {
        "vhost-user-fs-pci": '"vhost-user-fs-pci"' in (devices or ""),
        "shared-ram": shmrdma.SHARED_RAM_BACKEND in (objects or ""),
        "vhost-user-netdev": bool(netdev),
        "binds-numa": not is_macos() and qemu_binds_numa(qemu),
    }
    # an empty device list is a probe that failed, not a QEMU without
    # devices; a probe that failed is asked again next time
    if (
        ident
        and cache_path
        and devices
        and objects is not None
        and (netdev is not None)
    ):
        cache[real] = {"id": ident, "caps": caps}
        path = cache_path
        tmp = path.with_name(f".{path.name}.{os.getpid()}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(cache, indent=2) + "\n")
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)
    return caps


def install_packages(*pkgs: str) -> None:
    """Install host packages as `ltvm install` does, elevating with sudo."""
    from .host_setup import HostInfo, _translate_pkgs
    from .priv import sudo_run

    host = HostInfo()
    names = _translate_pkgs(pkgs, host)
    if host.pkg_mgr == "dnf":
        cmd = ["dnf", "install", "-y", *names]
    elif host.pkg_mgr == "apt":
        cmd = ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install"]
        cmd += ["-y", *names]
    else:
        raise RuntimeError(f"no package manager to install {' '.join(pkgs)}")
    sudo_run(cmd)


def _ltvm(*args: str) -> None:
    """Run an ltvm command as a subprocess, its output to the user."""
    user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and user:
        raise RuntimeError(
            f"`ltvm {' '.join(args[:2])}` would run as root: run "
            f"`ltvm doctor --shmrdma --fix` as {user}, without sudo"
        )
    script = Path(__file__).resolve().parent.parent / "ltvm"
    r = subprocess.run([sys.executable, str(script), *args], check=False)
    if r.returncode != 0:
        raise RuntimeError(
            f"`ltvm {' '.join(args)}` failed (rc={r.returncode})"
        )


def _smc_excluded(target: str) -> bool:
    """Whether *target*'s kernels leave SMC to the distro config."""
    from .target_config import TargetConfig

    try:
        return "CONFIG_SMC" in TargetConfig(target).kernel_config_whiteouts
    except (ValueError, KeyError, OSError):
        return False


def _kernel_has_smc(kernel_dir: Path) -> bool | None:
    """Whether the kernel was built with SMC; None if it cannot be told."""
    config = kernel_dir / "build-tree" / ".config"
    try:
        text = config.read_text(errors="replace")
    except OSError:
        return None
    return "\nCONFIG_SMC=m" in text or "\nCONFIG_SMC=y" in text


def host_checks(arch: str | None = None) -> list[Req]:
    """What the host itself needs, for guests of *arch* (the host's if
    None): each arch has its own QEMU."""
    reqs = []
    cmd = shmrdma._compiler()
    cc = " ".join(cmd) if cmd and shutil.which(cmd[0]) else None
    built = not cc and not shmrdma.daemon_needs_compiler()
    if cc or built:
        detail = cc or "none, and none needed: shmrdmad is built"
    elif cmd and os.environ.get("CC"):
        detail = f"CC={os.environ['CC']}: {cmd[0]} is not on PATH"
    else:
        detail = "no cc or gcc on PATH"
    reqs.append(
        Req(
            "C compiler for shmrdmad",
            frozenset({OWN_DAEMON}),
            bool(cc) or built,
            detail,
            fix=lambda: install_packages("gcc"),
            how="install gcc",
        )
    )
    qemu = _qemu(arch)
    caps = _qemu_caps(qemu)
    backend = shmrdma.SHARED_RAM_BACKEND
    reinstall = "reinstall ltvm's QEMU: `sudo ltvm setup --qemu --force`"
    qemu_fix: Callable[[], None] | None = None
    from .host_setup import is_macos

    if is_macos() and (arch or _native_arch()) != _native_arch():
        reinstall = (
            f"ltvm's QEMU for macOS runs native guests only; Homebrew's "
            f"qemu-system-{arch} has no vhost-user: use a "
            f"{_native_arch()} target"
        )
    elif is_macos() and _native_arch() != "aarch64":
        reinstall = (
            "no QEMU with vhost-user is published for Intel Macs: use "
            "Apple Silicon or a Linux host"
        )
    elif is_macos():
        # Homebrew's QEMU has no vhost-user; ltvm publishes one that does
        from .host_setup import install_qemu_macos_vhost

        reinstall = "install ltvm's QEMU for macOS"
        qemu_fix = install_qemu_macos_vhost

    def _recheck(cap: str) -> Callable[[], bool] | None:
        if qemu_fix is None:
            return None
        return lambda: _qemu_caps(_qemu(arch))[cap]

    reqs.append(
        Req(
            "QEMU vhost-user-fs-pci",
            frozenset({SHMRDMA}),
            caps["vhost-user-fs-pci"],
            qemu,
            fix=qemu_fix,
            how=reinstall,
            recheck=_recheck("vhost-user-fs-pci"),
        )
    )
    reqs.append(
        Req(
            f"QEMU {backend}",
            frozenset({SHMRDMA, SHMNET}),
            caps["shared-ram"],
            qemu,
            fix=qemu_fix,
            how=reinstall,
            recheck=_recheck("shared-ram"),
        )
    )
    reqs.append(
        Req(
            "QEMU vhost-user netdev",
            frozenset({SHMNET}),
            caps["vhost-user-netdev"],
            qemu,
            fix=qemu_fix,
            how=reinstall,
            recheck=_recheck("vhost-user-netdev"),
        )
    )
    reqs.append(_run_dir_check())
    if is_macos():
        # No NUMA nodes to pin to: create refuses --host-node here
        # (qemu_run.host_node_problem); taskset and numactl do not exist.
        reqs.append(
            Req(
                "host NUMA nodes",
                frozenset({HOST_NODE}),
                False,
                "macOS has none",
                how="--host-node is for Linux hosts with NUMA nodes",
                hard=False,
            )
        )
        return reqs
    reqs.append(
        Req(
            "taskset",
            frozenset({HOST_NODE}),
            bool(shutil.which("taskset")),
            shutil.which("taskset") or "not on PATH",
            fix=lambda: install_packages("util-linux"),
            how="install util-linux",
        )
    )
    binds = caps["binds-numa"]
    numactl = shutil.which("numactl")
    reqs.append(
        Req(
            "NUMA memory binding",
            frozenset({HOST_NODE}),
            binds or bool(numactl),
            "QEMU binds memory itself (libnuma)"
            if binds
            else (numactl or "QEMU lacks libnuma and numactl is missing"),
            fix=lambda: install_packages("numactl"),
            how="install numactl",
            # Without it the pinned vCPUs still place most memory.
            hard=False,
        )
    )
    nodes = shmrdma.daemon_nodes()
    if nodes:
        from .qemu_run import host_node_cpus

        bad = [n for n in nodes if not host_node_cpus(n)]
        reqs.append(
            Req(
                "shmrdmad CPU nodes",
                frozenset({OWN_DAEMON}),
                not bad,
                f"nodes {nodes}"
                + (f"; {bad} have no CPUs here" if bad else ""),
                how="fix [shmrdma] nodes in /etc/ltvm.conf or "
                "LTVM_SHMRDMAD_NODES",
                hard=False,
            )
        )
    return reqs


def _run_dir_check() -> Req:
    uid = os.geteuid()
    user = None
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        import pwd

        user = os.environ["SUDO_USER"]
        try:
            uid = pwd.getpwnam(user).pw_uid
        except KeyError:
            user = None
    ok = False
    detail = ""
    fix: Callable[[], None] | None = None
    try:
        d = shmrdma.run_dir(uid, create=False)
        detail = str(d)
        if os.path.lexists(d):
            shmrdma._check_private(d, uid)
            ok = True
        elif user is not None:
            # root never makes it: the user's own ltvm run does
            detail = f"none yet; {user}'s first ltvm run makes one"
        else:
            free = [
                c
                for c in shmrdma._candidate_dirs(uid)
                if not os.path.lexists(c) and shmrdma.creatable(c)
            ]
            ok = bool(free)
            detail = (
                f"{free[0]} (to be created)"
                if ok
                else "no candidate can be created: "
                + ", ".join(map(str, shmrdma._candidate_dirs(uid)))
            )

            def make() -> None:
                shmrdma.run_dir(uid, create=True)

            fix = make
    except (OSError, RuntimeError, shmrdma.UnsafeDir) as e:
        detail = str(e)

    return Req(
        "shmrdmad runtime directory",
        frozenset({OWN_DAEMON}),
        ok,
        detail,
        fix=fix,
        how="a private directory for the daemon's sockets",
    )


def _selection(t: Target) -> list[str]:
    """The options that pick *t*'s artifacts on a build command line."""
    return (["--kernel", t.kernel] if t.kernel else []) + (
        ["--arch", t.arch] if t.arch else []
    )


def _cross(t: Target) -> bool:
    """An image of another arch than the host's: built without the module."""
    return bool(t.arch) and t.arch != _native_arch()


def _build_image(t: Target, force: bool = False) -> None:
    """Rebuild *t*'s image with the Lustre tree doctor was given."""
    if not t.lustre_tree:
        raise RuntimeError(
            "an image build bakes in Lustre from a source tree: pass "
            "--lustre-tree"
        )
    _ltvm(
        "build",
        "image",
        t.name,
        *_selection(t),
        "--lustre-tree",
        t.lustre_tree,
        *(["--force"] if force else []),
    )


def target_checks(t: Target) -> list[Req]:
    """What the image and kernel of target *t* need."""
    reqs = []
    sel = _selection(t)
    k = "".join(f" {a}" for a in sel)
    if t.image is None or t.kernel_dir is None:
        tree = t.lustre_tree

        def build() -> None:
            if tree:
                _ltvm("build", "kernel", t.name, *sel, "--lustre-tree", tree)
            _build_image(t)

        reqs.append(
            Req(
                f"{t.name} artifacts",
                frozenset({SHMRDMA, SMC}),
                False,
                t.problem.splitlines()[0] if t.problem else "not built",
                fix=build,
                how=f"ltvm build kernel {t.name}{k} --lustre-tree <tree> "
                f"if it has no kernel yet (doctor takes --lustre-tree), "
                f"then ltvm build image {t.name}{k}",
            )
        )
        return reqs
    has_kmod = (
        shmrdma.image_has_kmod(t.image) if t.has_kmod is None else t.has_kmod
    )
    if _cross(t) and not has_kmod:
        reqs.append(
            Req(
                f"{t.name} image has the shmrdma guest module",
                frozenset({SHMRDMA}),
                False,
                f"{t.image}: a {t.arch} image built on a "
                f"{_native_arch()} host has no guest module",
                how=f"build the {t.arch} image on a {t.arch} host; a "
                f"rebuild here leaves it out again",
            )
        )
    else:
        reqs.append(
            Req(
                f"{t.name} image has the shmrdma guest module",
                frozenset({SHMRDMA}),
                has_kmod,
                str(t.image),
                # Its inputs may be unchanged (the module failed to build
                # last time), so only a forced rebuild retries it.
                fix=lambda: _build_image(t, force=True),
                how=f"ltvm build image {t.name}{k} --lustre-tree <tree> "
                f"--force (a fetched image predates it)",
                recheck=lambda: _current_has_kmod(t),
            )
        )
        # a VM's own record means it was created already: one that
        # booted on such an image is warned about after it starts
        if has_kmod and t.has_kmod is None:
            reqs.append(
                Req(
                    f"{t.name} image binds --nic shmrdma devices",
                    frozenset({SHMRDMA_NIC}),
                    shmrdma.image_has_nic_tags(t.image) is not False,
                    f"{t.image}: its guest module predates them",
                    fix=lambda: _build_image(t, force=True),
                    how=f"ltvm build image {t.name}{k} --lustre-tree "
                    f"<tree> --force",
                    recheck=lambda: _current_has_nic_tags(t),
                )
            )
    smc = _kernel_has_smc(t.kernel_dir)
    if _smc_excluded(t.name):
        reqs.append(
            Req(
                f"{t.name} kernel has SMC",
                frozenset({SMC}),
                bool(smc),
                f"{t.name}'s kernels are built without SMC "
                f"(targets.yaml); SMC connections fall back to TCP",
                hard=False,
            )
        )
        return reqs
    tree = t.lustre_tree

    def rebuild() -> None:
        if not tree:
            raise RuntimeError(
                "a kernel rebuild needs the Lustre tree whose patches it "
                "carries: pass --lustre-tree"
            )
        _ltvm("build", "kernel", t.name, *sel, "--lustre-tree", tree)
        _build_image(t)

    reqs.append(
        Req(
            f"{t.name} kernel has SMC",
            frozenset({SMC}),
            bool(smc),
            str(t.kernel_dir)
            + ("" if smc is not None else " (no build tree to check)"),
            fix=rebuild,
            how=f"ltvm build kernel {t.name}{k} --lustre-tree <tree>, then "
            f"ltvm build image {t.name}{k}",
            # shmrdma works without it; SMC connections fall back to TCP.
            hard=False,
        )
    )
    return reqs


def features_of(shmrdma_flag: str, nics: list, host_node: int) -> set[str]:
    """The features a VM with these settings uses."""
    feats = set()
    if shmrdma_flag or any(n.type == "shmrdma" for n in nics):
        feats |= {SHMRDMA, SMC}
    if any(n.type == "shmrdma" for n in nics):
        feats.add(SHMRDMA_NIC)
    if any(n.on_switch for n in nics):
        feats.add(SHMNET)
    if shmrdma_flag == "auto" or any(
        (n.type == "shmnet" and not n.socket) or n.type == "shmrdma"
        for n in nics
    ):
        feats.add(OWN_DAEMON)
    if host_node >= 0:
        feats.add(HOST_NODE)
    return feats


def refusal(
    feats: set[str], t: Target | None = None, arch: str | None = None
) -> str | None:
    """One message naming what *feats* lack here, or None if nothing.

    Only hard requirements of a requested feature refuse; *t* adds the
    image and kernel checks.  *arch* (else *t*'s, else the host's) picks
    the QEMU checked.  Never builds anything.
    """
    if not feats:
        return None
    arch = arch or (t.arch if t is not None else None)
    reqs = host_checks(arch) + (target_checks(t) if t is not None else [])
    missing = [r for r in reqs if r.hard and not r.ok and r.needed_by(feats)]
    running = _root_daemon_check() if OWN_DAEMON in feats else None
    if running is not None and not running.ok:
        missing.append(running)
    if not missing:
        return None
    flags = {
        SHMRDMA: "--shmrdma or --nic shmrdma",
        SHMNET: "--nic shmnet",
        HOST_NODE: "--host-node",
    }
    asked = ", ".join(flags[f] for f in sorted(feats) if f in flags)
    target = ""
    if t is not None:
        target = f" {t.name}" + "".join(f" {a}" for a in _selection(t))
    lines = [f"{asked} needs what this host does not have yet:"]
    lines += [f"  {r.name}: {r.detail}" for r in missing]
    # what doctor cannot fix says what will, and doctor is not offered
    lines += [f"  ({r.how})" for r in missing if r.fix is None and r.how]
    if any(r is not running and r.fix is not None for r in missing):
        lines.append(f"run `ltvm doctor --fix --shmrdma{target}` to set it up")
    return "\n".join(lines)


def _root_daemon_check() -> Req | None:
    """Under sudo, still root (a classic host): ltvm will not start the
    user's daemon as root, so it must be running already.  None when
    this process is the daemon's owner."""
    try:
        d = shmrdma.daemon(create=False)
    except (KeyError, OSError, RuntimeError) as e:
        return Req("shmrdmad running", frozenset({OWN_DAEMON}), False, str(e))
    if os.geteuid() == d.uid:
        return None
    how = f"run `ltvm vm shmrdmad start` as {d.user} first"
    return Req(
        "shmrdmad running",
        frozenset({OWN_DAEMON}),
        bool(d.pid()),
        f"not running for {d.user}, and ltvm will not start it as root: " + how,
        how=how,
    )


def _current_has_nic_tags(t: Target) -> bool:
    from .image_store import current_image

    if t.image is None:
        return False
    cur = current_image(Path(t.image).parent)
    return cur is not None and bool(shmrdma.image_has_nic_tags(cur))


def _current_has_kmod(t: Target) -> bool:
    from .image_store import current_image

    if t.image is None:
        return False
    cur = current_image(Path(t.image).parent)
    return cur is not None and shmrdma.image_has_kmod(cur)


def doctor(t: Target, fix: bool) -> tuple[int, int]:
    """Report every requirement; with *fix*, repair what can be.

    Returns (issues, fixes that failed).
    """
    issues = failures = 0
    print(f"shmrdma, shmnet, --host-node and SMC ({t.name}):")
    for r in host_checks(t.arch) + target_checks(t):
        feats = ", ".join(sorted(r.features))
        if r.ok:
            print(f"  ok       {r.name} ({feats}): {r.detail}")
            continue
        # An optional item is reported and fixed, but not an issue.
        issues += r.hard
        label = "missing " if r.hard else "optional"
        print(f"  {label} {r.name} ({feats}): {r.detail}")
        if not fix:
            print(f"           fix: {r.how}")
            continue
        if r.fix is None:
            print(f"           cannot fix automatically: {r.how}")
            failures += r.hard
            continue
        try:
            r.fix()
            if r.recheck is not None and not r.recheck():
                raise RuntimeError(f"still missing after: {r.how}")
            print(f"           fixed: {r.how}")
        except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
            print(f"           could not fix: {e}")
            failures += r.hard
    return issues, failures
