"""What shmrdma, shmnet NICs, --host-node and SMC need, and fixing it.

One list of checks serves three callers: ``ltvm doctor --shmrdma``
reports every one (and ``--fix`` repairs what it can), create and start
refuse a VM whose features miss one, and ``ltvm install`` installs the
host packages.  Each check says which features need it, so a VM that
only asks for --host-node is not refused for want of a C compiler.
"""

from __future__ import annotations

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


def _qemu_help(what: str, qemu: str | None = None) -> str:
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
        return ""
    return r.stdout


def _qemu_has_vhost_user_netdev(qemu: str | None = None) -> bool:
    """Whether QEMU was built with vhost-user.  ``-netdev help`` lists
    vhost-user even in a build without it (Homebrew's, on macOS), so
    ask for one: a build with it complains about the chardev instead."""
    qemu = qemu or _qemu()
    if "vhost-user" not in _qemu_help("netdev", qemu):
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
        return False
    return "not compiled" not in r.stderr


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
    script = Path(__file__).resolve().parent.parent / "ltvm"
    r = subprocess.run([sys.executable, str(script), *args], check=False)
    if r.returncode != 0:
        raise RuntimeError(
            f"`ltvm {' '.join(args)}` failed (rc={r.returncode})"
        )


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
    cc = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc")
    reqs.append(
        Req(
            "C compiler for shmrdmad",
            frozenset({OWN_DAEMON}),
            bool(cc),
            cc or "no cc or gcc on PATH",
            fix=lambda: install_packages("gcc"),
            how="install gcc",
        )
    )
    qemu = _qemu(arch)
    devices = _qemu_help("device", qemu)
    objects = _qemu_help("object", qemu)
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
    elif is_macos():
        # Homebrew's QEMU has no vhost-user; ltvm publishes one that does
        from .host_setup import install_qemu_macos_vhost

        reinstall = "install ltvm's QEMU for macOS (Apple Silicon only)"
        qemu_fix = install_qemu_macos_vhost
    reqs.append(
        Req(
            "QEMU vhost-user-fs-pci",
            frozenset({SHMRDMA}),
            '"vhost-user-fs-pci"' in devices,
            qemu,
            fix=qemu_fix,
            how=reinstall,
        )
    )
    reqs.append(
        Req(
            f"QEMU {backend}",
            frozenset({SHMRDMA, SHMNET}),
            backend in objects,
            qemu,
            fix=qemu_fix,
            how=reinstall,
        )
    )
    reqs.append(
        Req(
            "QEMU vhost-user netdev",
            frozenset({SHMNET}),
            _qemu_has_vhost_user_netdev(qemu),
            qemu,
            fix=qemu_fix,
            how=reinstall,
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
    from .qemu_run import qemu_binds_numa

    binds = qemu_binds_numa(qemu)
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
        uid = pwd.getpwnam(user).pw_uid
    d = shmrdma.run_dir(uid, create=False)
    ok = False
    detail = str(d)
    fix: Callable[[], None] | None = None
    try:
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
    except (OSError, shmrdma.UnsafeDir) as e:
        detail = str(e)

    return Req(
        "shmrdmad runtime directory",
        frozenset({OWN_DAEMON}),
        ok,
        detail,
        fix=fix,
        how="a private directory for the daemon's sockets",
    )


def _build_image(t: Target, force: bool = False) -> None:
    """Rebuild *t*'s image with the Lustre tree doctor was given."""
    if not t.lustre_tree:
        raise RuntimeError(
            "an image build bakes in Lustre from a source tree: pass "
            "--lustre-tree"
        )
    k = ["--kernel", t.kernel] if t.kernel else []
    _ltvm(
        "build",
        "image",
        t.name,
        *k,
        "--lustre-tree",
        t.lustre_tree,
        *(["--force"] if force else []),
    )


def target_checks(t: Target) -> list[Req]:
    """What the image and kernel of target *t* need."""
    reqs = []
    k = f" --kernel {t.kernel}" if t.kernel else ""
    if t.image is None or t.kernel_dir is None:
        tree = t.lustre_tree

        def build() -> None:
            if tree:
                _ltvm(
                    "build", "kernel", t.name, *k.split(), "--lustre-tree", tree
                )
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
    reqs.append(
        Req(
            f"{t.name} image has the shmrdma guest module",
            frozenset({SHMRDMA}),
            shmrdma.image_has_kmod(t.image),
            str(t.image),
            # Its inputs may be unchanged (the module failed to build
            # last time), so only a forced rebuild retries it.
            fix=lambda: _build_image(t, force=True),
            how=f"ltvm build image {t.name}{k} --lustre-tree <tree> "
            f"--force (a fetched image predates it)",
            recheck=lambda: _current_has_kmod(t),
        )
    )
    smc = _kernel_has_smc(t.kernel_dir)
    tree = t.lustre_tree

    def rebuild() -> None:
        if not tree:
            raise RuntimeError(
                "a kernel rebuild needs the Lustre tree whose patches it "
                "carries: pass --lustre-tree"
            )
        _ltvm("build", "kernel", t.name, *k.split(), "--lustre-tree", tree)
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
    if shmrdma_flag:
        feats |= {SHMRDMA, SMC}
    if any(n.type == "shmnet" for n in nics):
        feats.add(SHMNET)
    if shmrdma_flag == "auto" or any(
        n.type == "shmnet" and not n.socket for n in nics
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
    if not missing:
        return None
    flags = {
        SHMRDMA: "--shmrdma",
        SHMNET: "--nic shmnet",
        HOST_NODE: "--host-node",
    }
    asked = ", ".join(flags[f] for f in sorted(feats) if f in flags)
    target = ""
    if t is not None:
        target = f" {t.name}" + (f" --kernel {t.kernel}" if t.kernel else "")
    lines = [f"{asked} needs what this host does not have yet:"]
    lines += [f"  {r.name}: {r.detail}" for r in missing]
    lines.append(f"run `ltvm doctor --fix --shmrdma{target}` to set it up")
    return "\n".join(lines)


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
