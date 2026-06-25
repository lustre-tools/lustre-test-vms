"""Host setup for Lustre QEMU test VMs.

Prepares a Linux host: installs QEMU, configures the network
bridge, installs the ltvm entry point,
and sets up SSH.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def is_macos() -> bool:
    return platform.system() == "Darwin"


def is_wsl2() -> bool:
    try:
        v = Path("/proc/version").read_text().lower()
        return "microsoft" in v or "wsl" in v
    except OSError:
        return False


class PodmanMachineError(RuntimeError):
    """Raised when podman is unusable on macOS (no binary, no running machine)."""


def check_podman_machine_macos() -> None:
    """Ensure podman is usable on macOS, auto-starting the machine if stopped.

    No-op on non-macOS hosts. On macOS, containers require a running
    ``podman machine``, so we pre-flight that before any container build.
    If the machine exists but is stopped, we start it; the user should
    not have to type the same command ltvm already knows it needs.
    Raises PodmanMachineError only when podman is absent or no machine
    has been initialized -- cases that genuinely need the user.
    """
    if not is_macos():
        return

    if not shutil.which("podman"):
        raise PodmanMachineError(
            "podman not found.\n"
            "Install it with:\n"
            "  brew install podman\n"
            "Then run:\n"
            "  podman machine init      # first time only\n"
            "  podman machine start"
        )

    try:
        r = subprocess.run(
            ["podman", "machine", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise PodmanMachineError(
            f"failed to query podman machine: {e}"
        ) from e

    machines: list[dict[str, Any]] = []
    if r.returncode == 0 and r.stdout.strip():
        try:
            parsed = json.loads(r.stdout)
            if isinstance(parsed, list):
                machines = parsed
        except json.JSONDecodeError:
            machines = []

    if not machines:
        raise PodmanMachineError(
            "no podman machine configured.\n"
            "Run:\n"
            "  podman machine init\n"
            "Then retry."
        )

    if any(m.get("Running") for m in machines):
        return

    log.info("Starting podman machine...")
    try:
        subprocess.run(
            ["podman", "machine", "start"],
            check=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise PodmanMachineError(
            f"failed to start podman machine: {e}"
        ) from e


log = logging.getLogger(__name__)

# Override the log format for setup output so it
# prints "==> message" instead of "lib.host_setup: message".
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("==> %(message)s"))
log.addHandler(_handler)
log.propagate = False

QEMU_VERSION = "9.2.2"
QEMU_PREFIX = Path(os.environ.get("LTVM_QEMU_PREFIX", "/opt/qemu"))
VM_DIR = Path(os.environ.get("LTVM_VM_DIR", "/opt/qemu-vms"))
DEFAULT_SUBNET = "192.168.100"

DEFAULT_VMNET_GATEWAY = os.environ.get("LTVM_VMNET_GATEWAY", "192.168.105.1")
SOCKET_VMNET_PLIST_LABEL = "io.github.lima-vm.socket_vmnet"
SOCKET_VMNET_PLIST_PATH = Path(
    f"/Library/LaunchDaemons/{SOCKET_VMNET_PLIST_LABEL}.plist"
)

DNSMASQ_PLIST_LABEL = "io.github.ltvm.dnsmasq"
DNSMASQ_PLIST_PATH = Path(
    f"/Library/LaunchDaemons/{DNSMASQ_PLIST_LABEL}.plist"
)
DNSMASQ_CONF_PATH = Path("/usr/local/etc/ltvm-dnsmasq.conf")
DNSMASQ_PID_PATH = Path("/var/run/ltvm-dnsmasq.pid")

REPO_ROOT = Path(__file__).resolve().parent.parent
# ltvm_pkg/ holds scripts, host-config templates, etc.
PKG_DIR = Path(__file__).resolve().parent
HOST_CONFIG_DIR = PKG_DIR / "host-config"


# ------------------------------------------------------------------
# Host OS detection
# ------------------------------------------------------------------


class HostInfo:
    """Detected host OS and package manager."""

    def __init__(self) -> None:
        self.id: str = "unknown"
        self.version: str = "0"
        self.pretty_name: str = "unknown"
        self.pkg_mgr: str | None = None  # "dnf" or "apt"

        osr = Path("/etc/os-release")
        if not osr.exists():
            raise RuntimeError(
                "Cannot detect host OS (/etc/os-release missing)"
            )

        env = {}
        for line in osr.read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                env[k] = v.strip('"').strip("'")

        self.id = env.get("ID", "unknown")
        self.version = env.get("VERSION_ID", "0")
        self.pretty_name = env.get("PRETTY_NAME", f"{self.id} {self.version}")

        if shutil.which("dnf"):
            self.pkg_mgr = "dnf"
        elif shutil.which("apt-get"):
            self.pkg_mgr = "apt"
        else:
            raise RuntimeError(
                "No supported package manager (need dnf or apt-get)"
            )

    def __str__(self) -> str:
        return f"{self.pretty_name} ({self.pkg_mgr})"


# RHEL -> Debian package name mapping for the small set
# of host packages we install.
_PKG_MAP = {
    "glib2-devel": "libglib2.0-dev",
    "pixman-devel": "libpixman-1-dev",
    "iptables-nft": "iptables",
    "pdsh-rcmd-ssh": "pdsh",
    "python3-pip": "python3-pip",
    "ninja-build": "ninja-build",
}


def _translate_pkgs(pkgs: tuple[str, ...], host: HostInfo) -> list[str]:
    """Translate RHEL package names for apt hosts."""
    if host.pkg_mgr != "apt":
        return list(pkgs)
    return [_PKG_MAP.get(p, p) for p in pkgs]


def _run(
    cmd: list[str],
    check: bool = True,
    quiet: bool = False,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, return CompletedProcess."""
    log.debug("run: %s", " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, capture_output=quiet, text=True, cwd=cwd, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(
            f"Command failed (rc={r.returncode}): "
            f"{' '.join(str(c) for c in cmd)}"
        )
    return r


def _run_quiet(
    cmd: list[str], check: bool = True
) -> subprocess.CompletedProcess[str]:
    return _run(cmd, check=check, quiet=True)


# Privilege helpers live in ltvm_pkg.priv so vm_commands, vm_net,
# qemu_run, image_export, and vm_cluster can import them without
# pulling in the rest of host_setup.  The module-private aliases
# below keep host_setup's existing call sites stable.
from ltvm_pkg.priv import sudo_prime as _sudo_prime  # noqa: E402
from ltvm_pkg.priv import sudo_run as _sudo_run  # noqa: E402


def _pkg_install(host: HostInfo, *pkgs: str) -> None:
    """Install packages using the host's package manager.

    Failures are logged with the package list so the user can see what
    actually went wrong, instead of being silently swallowed and causing
    confusing downstream errors.
    """
    translated = _translate_pkgs(pkgs, host)
    if host.pkg_mgr == "dnf":
        r = _run(["dnf", "install", "-y", *translated], check=False)
    elif host.pkg_mgr == "apt":
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        r = subprocess.run(
            ["apt-get", "install", "-y", *translated],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
    else:
        return
    if r.returncode != 0:
        log.warning(
            "package install failed (rc=%s) for: %s\n%s",
            r.returncode,
            " ".join(translated),
            (r.stderr or "").strip(),
        )


# ------------------------------------------------------------------
# Prerequisite checks
# ------------------------------------------------------------------


def check_prerequisites(host: HostInfo) -> None:
    """Verify and install basic prerequisites."""
    needed = {
        "curl": "curl",
        "tar": "tar",
        "make": "make",
        "gcc": "gcc",
        "ip": "iproute2" if host.pkg_mgr == "apt" else "iproute",
        "fakeroot": "fakeroot",
        "mke2fs": "e2fsprogs",
        # zstd: publish/fetch compress artifacts with it; gzip is too
        # weak on ext4 rootfs content (compresses ~2x) to fit within
        # GitHub's 2 GiB per-asset release cap.
        "zstd": "zstd",
        # Needed by `ltvm target export` (bootable-disk packaging).
        "parted": "parted",
        ("grub-install" if host.pkg_mgr == "apt" else "grub2-install"): (
            "grub-pc-bin" if host.pkg_mgr == "apt" else "grub2-pc"
        ),
    }
    missing = []
    for cmd, pkg in needed.items():
        if not shutil.which(cmd):
            missing.append(pkg)
    if missing:
        log.info("Installing missing prerequisites: %s", " ".join(missing))
        _pkg_install(host, *missing)

    if not shutil.which("podman"):
        log.info("Installing podman (needed for container/image builds)...")
        _pkg_install(host, "podman")

    # iptables-nft is needed by the qemu-bridge.service NAT/FORWARD
    # rules.  EL9 ships it preinstalled; EL8 does not, so `ltvm install`
    # fails at the bridge step with "iptables: command not found" unless
    # we install it here (bug lustre_test_vms_v2-yh9).  The install in
    # setup_network() also covers this, but doing it here at the prereq
    # stage keeps failure modes consistent across EL8/EL9 and makes
    # `ltvm doctor` / partial-step runs work.  Only applies to dnf --
    # Debian/Ubuntu's `iptables` package uses nft transparently.
    if host.pkg_mgr == "dnf" and not shutil.which("iptables"):
        log.info("Installing iptables-nft (needed for host bridge NAT)...")
        _pkg_install(host, "iptables-nft")

    # python3-pyyaml needed by ltvm fetch/resolve
    try:
        import yaml  # noqa: F401
    except ImportError:
        log.info("Installing python3-pyyaml...")
        if host.pkg_mgr == "dnf":
            _pkg_install(host, "python3-pyyaml")
        elif host.pkg_mgr == "apt":
            _pkg_install(host, "python3-yaml")


def _detect_platform_hint() -> str:
    """Return a short hint about how to enable KVM on this platform."""
    if is_wsl2():
        return (
            "WSL2 detected. From elevated PowerShell on the Windows host:\n"
            "  Set-VMProcessor -VMName WSL "
            "-ExposeVirtualizationExtensions $true\n"
            "  wsl --shutdown"
        )
    # Check if we're inside a VM
    product = Path("/sys/class/dmi/id/product_name")
    if product.exists():
        name = product.read_text().strip().lower()
        if "virtualbox" in name:
            return "VirtualBox detected. Enable nested VT-x in VM settings."
        if "vmware" in name:
            return (
                "VMware detected. Enable 'Virtualize Intel VT-x' "
                "in VM processor settings."
            )
        if "kvm" in name or "qemu" in name:
            return (
                "KVM/QEMU guest detected. Ensure the host has:\n"
                "  options kvm_intel nested=1  (or kvm_amd)\n"
                "  <cpu mode='host-passthrough'/> in the VM XML"
            )
    hypervisor = Path("/sys/hypervisor/type")
    if hypervisor.exists():
        hv = hypervisor.read_text().strip()
        if hv == "xen":
            return "Xen detected. Use HVM mode with nested virt."
    # Azure/Hyper-V
    vendor = Path("/sys/class/dmi/id/sys_vendor")
    if vendor.exists() and "microsoft" in vendor.read_text().lower():
        return (
            "Hyper-V detected. From elevated PowerShell on the host:\n"
            "  Set-VMProcessor -VMName <name> "
            "-ExposeVirtualizationExtensions $true"
        )
    # Check for Apple Virtualization Framework / Parallels
    if vendor.exists():
        v = vendor.read_text().lower()
        if "apple" in v:
            return (
                "Apple Virtualization Framework detected. Ensure "
                "'Use Apple Virtualization' is enabled in UTM/Parallels."
            )
        if "parallels" in v:
            return "Parallels detected. Enable nested virt in VM config."
    return "Check BIOS for VT-x/AMD-V, or enable nested virt if this is a VM."


def check_kvm(require: bool = True) -> bool:
    """Check for /dev/kvm.  Returns True if present."""
    if Path("/dev/kvm").exists():
        return True

    hint = _detect_platform_hint()
    doc_path = REPO_ROOT / "docs" / "NESTED_VIRTUALIZATION.md"
    msg = (
        "/dev/kvm not found -- KVM is required for ltvm VMs.\n"
        "\n"
        f"  {hint}\n"
        "\n"
        f"  Full guide: {doc_path}\n"
        "  (or see docs/NESTED_VIRTUALIZATION.md in the repo)"
    )
    if require:
        raise RuntimeError(msg)
    log.warning(msg)
    return False


# ------------------------------------------------------------------
# QEMU
# ------------------------------------------------------------------


def _qemu_installed_version(arch: str = "x86_64") -> str | None:
    """Return installed QEMU version string, or None."""
    _BINARY_MAP = {
        "x86_64": "qemu-system-x86_64",
        "aarch64": "qemu-system-aarch64",
    }
    binary_name = _BINARY_MAP.get(arch, f"qemu-system-{arch}")
    qemu = QEMU_PREFIX / "bin" / binary_name
    if not qemu.exists():
        return None
    try:
        r = _run_quiet([str(qemu), "--version"], check=False)
        m = re.search(r"version (\d+\.\d+\.\d+)", r.stdout)
        return m.group(1) if m else "unknown"
    except OSError:
        return None


def _install_qemu_path_profile() -> None:
    """Ensure /opt/qemu/bin is in PATH for all users."""
    profile = Path("/etc/profile.d/qemu-vms.sh")
    profile.write_text(
        f'# Added by ltvm setup\nexport PATH="{QEMU_PREFIX}/bin:$PATH"\n'
    )


# GitHub release tag and asset names for pre-built QEMU binaries
_QEMU_RELEASE_TAG = f"qemu-{QEMU_VERSION}"
_QEMU_GITHUB_REPO = "lustre-tools/lustre-test-vms"
# EL major versions for which we publish prebuilt QEMU tarballs on the
# GitHub release `qemu-<QEMU_VERSION>`.  Keep in sync with the assets
# actually uploaded -- running `gh release view qemu-<ver>` should list
# one `qemu-<ver>-el<N>.tar.gz` for each entry here.  EL8 is intentionally
# absent: building QEMU 9.2.2 needs Rocky 8 podman tooling that the CI
# publisher doesn't currently have set up (see CLAUDE.md "Rebuilding
# Pre-built QEMU Binaries" for the manual recipe).
_QEMU_PUBLISHED_EL_MAJORS = frozenset({"9", "10"})


def _is_el_host(host: HostInfo) -> bool:
    return host.id in ("rocky", "rhel", "centos", "almalinux")


def _fetch_prebuilt_qemu(host: HostInfo) -> bool:
    """Try to download pre-built QEMU binaries from GitHub.

    Returns True if installed successfully, False if not available.

    On EL hosts where no prebuilt asset is published (see
    _QEMU_PUBLISHED_EL_MAJORS) this raises RuntimeError with a clear
    message, rather than returning False and letting the caller fall
    through to a source build.  QEMU 9.2.2 fails to build on EL8 (glib,
    pixman, etc. too old -- see beads lustre_test_vms_v2-47m), so
    silently falling through produces a confusing compile failure far
    from the real problem.
    """
    if not _is_el_host(host):
        # No pre-built binary for non-EL hosts; apt path handles its own
        # QEMU via the system package, and source build is viable there.
        return False

    major = host.version.split(".")[0]
    asset = f"qemu-{QEMU_VERSION}-el{major}.tar.gz"

    if major not in _QEMU_PUBLISHED_EL_MAJORS:
        published = ", ".join(
            f"el{m}" for m in sorted(_QEMU_PUBLISHED_EL_MAJORS)
        )
        raise RuntimeError(
            f"No pre-built QEMU {QEMU_VERSION} asset published for "
            f"EL{major} (have: {published}).\n"
            f"\n"
            f"QEMU {QEMU_VERSION} cannot be built from source on EL{major} "
            f"(system glib2/pixman/etc. are too old).\n"
            f"\n"
            f"Ask a maintainer to build and publish {asset}:\n"
            f"  - Follow the 'Rebuilding Pre-built QEMU Binaries' recipe "
            f"in lustre-test-vms-v2/CLAUDE.md\n"
            f"  - Publish with: gh release upload {_QEMU_RELEASE_TAG} "
            f"{asset} --clobber\n"
            f"\n"
            f"Tracking: beads lustre_test_vms_v2-47m"
        )

    url = (
        f"https://github.com/{_QEMU_GITHUB_REPO}/releases/download/"
        f"{_QEMU_RELEASE_TAG}/{asset}"
    )

    log.info("Downloading pre-built QEMU %s for EL%s...", QEMU_VERSION, major)
    tmpdir = Path(tempfile.mkdtemp(prefix="qemu-fetch."))
    try:
        r = _run(
            ["curl", "-fsSL", url, "-o", str(tmpdir / asset)],
            check=False,
            quiet=True,
        )
        if r.returncode != 0:
            # EL major is in the published set but the download failed
            # (network, transient GitHub error, asset removed, ...).  Do
            # NOT silently fall through to a source build on EL: hard-fail
            # so the user knows to retry or investigate, instead of
            # watching an unrelated compile error.
            raise RuntimeError(
                f"Failed to download pre-built QEMU asset {asset} from "
                f"{url} (curl rc={r.returncode}).\n"
                f"Check network access to github.com, or retry.  "
                f"Do not fall back to a source build on EL hosts -- "
                f"required build deps may be too old."
            )

        # Tarball is structured as a /opt/qemu overlay: bin/qemu-system-*,
        # bin/qemu-img, share/qemu/<firmware>.  Extract directly into
        # QEMU_PREFIX so firmware lands at the path QEMU was configured to
        # look for it (--prefix=/opt/qemu => looks at /opt/qemu/share/qemu/).
        QEMU_PREFIX.mkdir(parents=True, exist_ok=True)
        _run(
            ["tar", "xf", str(tmpdir / asset), "-C", str(QEMU_PREFIX)],
            check=True,
        )

        # Verify the x86_64 binary is present and supports microvm.
        qemu = QEMU_PREFIX / "bin" / "qemu-system-x86_64"
        if not qemu.exists():
            raise RuntimeError(
                f"Pre-built QEMU tarball is missing bin/qemu-system-x86_64 -- "
                f"GitHub release asset {asset} may be malformed"
            )
        r = _run_quiet([str(qemu), "-machine", "help"], check=False)
        if "microvm" not in r.stdout:
            raise RuntimeError(
                "Downloaded pre-built QEMU lacks microvm machine type -- "
                "this should not happen; the GitHub release asset may be corrupt"
            )

        # Verify firmware files are present.  QEMU was configured with
        # --prefix=/opt/qemu so it looks for firmware at share/qemu/.  Without
        # these files, microvm boots fail with cryptic "could not load PC
        # BIOS 'bios-microvm.bin'" errors at runtime, far from the install.
        firmware_dir = QEMU_PREFIX / "share" / "qemu"
        required_firmware = ("bios-microvm.bin", "linuxboot_dma.bin")
        missing = [
            f for f in required_firmware if not (firmware_dir / f).exists()
        ]
        if missing:
            raise RuntimeError(
                f"Pre-built QEMU tarball is missing firmware files: "
                f"{', '.join(missing)} (expected under {firmware_dir}). "
                f"The GitHub release asset {asset} was built without running "
                f"`make install` -- rebuild and re-upload using the updated "
                f"build instructions in CLAUDE.md."
            )

        log.info("Installed pre-built QEMU %s to %s", QEMU_VERSION, QEMU_PREFIX)
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _system_qemu_has_machine(
    binaries: tuple[str, ...],
    machine: str,
) -> str | None:
    """Return the first qemu binary in `binaries` that supports `machine`.

    Used to detect whether the system QEMU has microvm (x86_64) or
    virt (aarch64) machine support before falling back to a source build.
    """
    for binary in binaries:
        path = shutil.which(binary) or binary
        try:
            r = _run_quiet([path, "-machine", "help"], check=False)
            if r.returncode == 0 and machine in r.stdout:
                return path
        except (FileNotFoundError, OSError):
            continue
    return None


def _system_qemu_has_microvm() -> str | None:
    """Check if the system-packaged QEMU x86_64 has microvm support."""
    return _system_qemu_has_machine(
        ("qemu-system-x86_64", "qemu-kvm", "/usr/libexec/qemu-kvm"),
        "microvm",
    )


def _system_qemu_has_virt() -> str | None:
    """Check if the system-packaged QEMU aarch64 has virt machine support."""
    return _system_qemu_has_machine(("qemu-system-aarch64",), "virt")


def _brew_qemu_prefix() -> Path | None:
    """Return the Homebrew prefix for the qemu package, or None."""
    brew = shutil.which("brew")
    if not brew:
        return None
    r = _run_quiet([brew, "--prefix", "qemu"], check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    p = Path(r.stdout.strip())
    if not (p / "bin" / "qemu-system-x86_64").exists():
        return None
    return p


def _brew_socket_vmnet_prefix() -> Path | None:
    """Return the Homebrew prefix for the socket_vmnet package, or None."""
    brew = shutil.which("brew")
    if not brew:
        return None
    r = _run_quiet([brew, "--prefix", "socket_vmnet"], check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    p = Path(r.stdout.strip())
    if not (p / "bin" / "socket_vmnet").exists():
        return None
    return p


def socket_vmnet_path() -> Path | None:
    """Return the path to the socket_vmnet daemon binary, or None."""
    prefix = _brew_socket_vmnet_prefix()
    if prefix is None:
        return None
    return prefix / "bin" / "socket_vmnet"


def socket_vmnet_socket_path() -> Path:
    """Return the Unix socket path that the socket_vmnet daemon listens on.

    Defaults to ``<brew-prefix>/var/run/socket_vmnet`` when socket_vmnet is
    installed via Homebrew (per the Lima convention), falling back to
    ``/var/run/socket_vmnet`` otherwise.  Overridable via
    ``LTVM_VMNET_SOCKET`` for custom daemon layouts.
    """
    env = os.environ.get("LTVM_VMNET_SOCKET")
    if env:
        return Path(env)
    prefix = _brew_socket_vmnet_prefix()
    if prefix is not None:
        return prefix / "var" / "run" / "socket_vmnet"
    return Path("/var/run/socket_vmnet")


def install_sshpass_macos(force: bool = False) -> None:
    """Install sshpass on macOS via Homebrew.

    sshpass is invoked by deploy/wait_for_ssh/run_ssh to talk to
    freshly-booted VMs that only have password auth (root/initial0)
    until ssh-key provisioning lands.  Without it, ``ltvm create``
    fails right after boot with ``[Errno 2] No such file or directory:
    'sshpass'`` and rolls back the whole VM.
    """
    if shutil.which("sshpass") and not force:
        return
    brew = shutil.which("brew")
    if not brew:
        raise RuntimeError(
            "Homebrew not found. Install it from https://brew.sh, "
            "then run: brew install sshpass"
        )
    log.info("Installing sshpass via Homebrew...")
    _run([brew, "install", "sshpass"])
    if not shutil.which("sshpass"):
        raise RuntimeError(
            "brew install sshpass succeeded but sshpass not on PATH"
        )


def install_socket_vmnet_macos(force: bool = False) -> None:
    """Install socket_vmnet on macOS via Homebrew.

    socket_vmnet (from the Lima project) runs as a privileged daemon and
    provides vmnet-shared networking to unprivileged QEMU processes.
    """
    brew_prefix = _brew_socket_vmnet_prefix()
    if brew_prefix and not force:
        log.info(
            "socket_vmnet already installed at %s/bin/socket_vmnet",
            brew_prefix,
        )
        return

    brew = shutil.which("brew")
    if not brew:
        raise RuntimeError(
            "Homebrew not found. Install it from https://brew.sh, "
            "then run: brew install socket_vmnet"
        )
    log.info("Installing socket_vmnet via Homebrew...")
    _run([brew, "install", "socket_vmnet"])
    brew_prefix = _brew_socket_vmnet_prefix()
    if not brew_prefix:
        raise RuntimeError(
            "brew install socket_vmnet succeeded but socket_vmnet binary not "
            "found in the expected Homebrew prefix. "
            "Check: brew --prefix socket_vmnet"
        )
    log.info("socket_vmnet installed at %s/bin/socket_vmnet", brew_prefix)


def _render_socket_vmnet_plist() -> str:
    """Render the launchd plist from the template, substituting paths."""
    bin_path = socket_vmnet_path()
    if bin_path is None:
        raise RuntimeError(
            "socket_vmnet binary not found -- run `ltvm install` or "
            "`brew install socket_vmnet` first"
        )
    sock_path = socket_vmnet_socket_path()
    template = (
        HOST_CONFIG_DIR / f"{SOCKET_VMNET_PLIST_LABEL}.plist"
    ).read_text()
    return (
        template.replace("@SOCKET_VMNET_BIN@", str(bin_path))
        .replace("@SOCKET_VMNET_SOCKET@", str(sock_path))
        .replace("@VMNET_GATEWAY@", DEFAULT_VMNET_GATEWAY)
    )


def _socket_vmnet_daemon_loaded() -> bool:
    """Return True if launchd has the socket_vmnet job loaded."""
    r = _run_quiet(
        ["launchctl", "print", f"system/{SOCKET_VMNET_PLIST_LABEL}"],
        check=False,
    )
    return r.returncode == 0


def socket_vmnet_reachable() -> bool:
    """Return True if the socket_vmnet socket exists and is a UNIX socket."""
    import stat as _stat

    sock = socket_vmnet_socket_path()
    try:
        mode = sock.stat().st_mode
        return _stat.S_ISSOCK(mode)
    except (OSError, TypeError):
        return False


def install_socket_vmnet_launchd_macos(force: bool = False) -> None:
    """Install and load the socket_vmnet launchd plist.

    Mirrors Lima's approach: a root-owned LaunchDaemon that starts at load
    and stays up (KeepAlive=true).  Only installed when the user runs
    `ltvm install`, so laptops that never do pay nothing.
    """
    desired = _render_socket_vmnet_plist()
    needs_write = True
    if SOCKET_VMNET_PLIST_PATH.exists() and not force:
        try:
            if SOCKET_VMNET_PLIST_PATH.read_text() == desired:
                needs_write = False
        except OSError:
            pass

    if needs_write:
        _sudo_prime(f"Installing {SOCKET_VMNET_PLIST_PATH} requires root")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".plist", delete=False
        ) as tf:
            tf.write(desired)
            tmp_path = tf.name
        try:
            _sudo_run(["mkdir", "-p", "/var/log/socket_vmnet"])
            # The socket_vmnet brew package doesn't create its var/run dir,
            # but the daemon binds its Unix socket there -- without this
            # mkdir the plist boots, fails ENOENT on bind(), and respawns
            # forever ("ERROR| socket_bindlisten: No such file or directory").
            _sudo_run(
                ["mkdir", "-p", str(socket_vmnet_socket_path().parent)]
            )
            _sudo_run(
                [
                    "install",
                    "-m",
                    "0644",
                    "-o",
                    "root",
                    "-g",
                    "wheel",
                    tmp_path,
                    str(SOCKET_VMNET_PLIST_PATH),
                ]
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        if _socket_vmnet_daemon_loaded():
            _sudo_run(
                ["launchctl", "bootout", f"system/{SOCKET_VMNET_PLIST_LABEL}"],
                check=False,
            )
        log.info("Installed %s", SOCKET_VMNET_PLIST_PATH)

    if not _socket_vmnet_daemon_loaded():
        _sudo_prime("Loading the socket_vmnet launchd job requires root")
        _sudo_run(
            ["launchctl", "bootstrap", "system", str(SOCKET_VMNET_PLIST_PATH)]
        )
        log.info("Loaded %s", SOCKET_VMNET_PLIST_LABEL)
    else:
        log.info("%s already loaded", SOCKET_VMNET_PLIST_LABEL)


def _dnsmasq_daemon_loaded() -> bool:
    r = _run_quiet(
        ["launchctl", "print", f"system/{DNSMASQ_PLIST_LABEL}"],
        check=False,
    )
    return r.returncode == 0


def _brew_dnsmasq_bin() -> Path | None:
    brew = shutil.which("brew")
    if not brew:
        return None
    r = _run_quiet([brew, "--prefix", "dnsmasq"], check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    bin_path = Path(r.stdout.strip()) / "sbin" / "dnsmasq"
    return bin_path if bin_path.exists() else None


def install_dnsmasq_macos(force: bool = False) -> None:
    """Install dnsmasq + a launchd job that binds it to the
    socket_vmnet gateway IP, so guests can resolve each other by name.

    Without this, VM ``resolv.conf`` points at fc_gw (192.168.105.1)
    where nothing answers DNS on macOS, and ``ssh co1-mds`` from one
    VM to another silently fails to resolve.  Linux uses the
    qemu-bridge dnsmasq for the same job.
    """
    bin_path = _brew_dnsmasq_bin()
    if bin_path is None or force:
        brew = shutil.which("brew")
        if not brew:
            raise RuntimeError(
                "Homebrew not found. Install it from https://brew.sh, "
                "then run: brew install dnsmasq"
            )
        log.info("Installing dnsmasq via Homebrew...")
        _run([brew, "install", "dnsmasq"])
        bin_path = _brew_dnsmasq_bin()
        if bin_path is None:
            raise RuntimeError(
                "brew install dnsmasq succeeded but dnsmasq binary "
                "not found in the expected Homebrew prefix."
            )

    desired_conf = (
        (HOST_CONFIG_DIR / "ltvm-dnsmasq-macos.conf")
        .read_text()
        .replace("@VMNET_GATEWAY@", DEFAULT_VMNET_GATEWAY)
    )
    desired_plist = (
        (HOST_CONFIG_DIR / f"{DNSMASQ_PLIST_LABEL}.plist")
        .read_text()
        .replace("@DNSMASQ_BIN@", str(bin_path))
        .replace("@DNSMASQ_CONF@", str(DNSMASQ_CONF_PATH))
        .replace("@DNSMASQ_PID@", str(DNSMASQ_PID_PATH))
    )

    needs_reload = False

    # Conf file: write under sudo with root:wheel 0644.
    cur_conf = (
        DNSMASQ_CONF_PATH.read_text()
        if DNSMASQ_CONF_PATH.exists()
        else ""
    )
    if cur_conf != desired_conf or force:
        _sudo_prime(
            f"Installing {DNSMASQ_CONF_PATH} requires root"
        )
        _sudo_run(
            ["mkdir", "-p", str(DNSMASQ_CONF_PATH.parent)]
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".conf", delete=False
        ) as tf:
            tf.write(desired_conf)
            tmp_conf = tf.name
        try:
            _sudo_run(
                ["install", "-m", "0644", "-o", "root", "-g",
                 "wheel", tmp_conf, str(DNSMASQ_CONF_PATH)]
            )
        finally:
            Path(tmp_conf).unlink(missing_ok=True)
        log.info("Installed %s", DNSMASQ_CONF_PATH)
        needs_reload = True

    # Plist: same pattern as socket_vmnet.  bootout-then-bootstrap
    # when the plist contents change so launchd picks up the new
    # ProgramArguments.
    cur_plist = (
        DNSMASQ_PLIST_PATH.read_text()
        if DNSMASQ_PLIST_PATH.exists()
        else ""
    )
    if cur_plist != desired_plist or force:
        _sudo_prime(
            f"Installing {DNSMASQ_PLIST_PATH} requires root"
        )
        _sudo_run(["mkdir", "-p", "/var/log/ltvm-dnsmasq"])
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".plist", delete=False
        ) as tf:
            tf.write(desired_plist)
            tmp_plist = tf.name
        try:
            _sudo_run(
                ["install", "-m", "0644", "-o", "root", "-g",
                 "wheel", tmp_plist, str(DNSMASQ_PLIST_PATH)]
            )
        finally:
            Path(tmp_plist).unlink(missing_ok=True)
        if _dnsmasq_daemon_loaded():
            _sudo_run(
                ["launchctl", "bootout",
                 f"system/{DNSMASQ_PLIST_LABEL}"],
                check=False,
            )
        log.info("Installed %s", DNSMASQ_PLIST_PATH)
        needs_reload = True

    if not _dnsmasq_daemon_loaded():
        _sudo_prime("Loading the ltvm-dnsmasq launchd job requires root")
        _sudo_run(
            ["launchctl", "bootstrap", "system",
             str(DNSMASQ_PLIST_PATH)]
        )
        log.info("Loaded %s", DNSMASQ_PLIST_LABEL)
    elif needs_reload:
        _sudo_run(
            ["launchctl", "kickstart", "-k",
             f"system/{DNSMASQ_PLIST_LABEL}"]
        )


def ensure_socket_vmnet_running() -> None:
    """Ensure the socket_vmnet daemon is reachable before launching a VM.

    Safe to call repeatedly: no-op when the socket is already reachable.
    If the plist is installed but the daemon isn't running, loads or
    kickstarts it and waits briefly for the socket to appear.
    """
    if socket_vmnet_reachable():
        return

    if SOCKET_VMNET_PLIST_PATH.exists():
        if not _socket_vmnet_daemon_loaded():
            log.info("socket_vmnet not loaded; loading launchd job...")
            _sudo_prime("Loading the socket_vmnet launchd job requires root")
            _sudo_run(
                [
                    "launchctl",
                    "bootstrap",
                    "system",
                    str(SOCKET_VMNET_PLIST_PATH),
                ],
                check=False,
            )
        else:
            _sudo_run(
                [
                    "launchctl",
                    "kickstart",
                    f"system/{SOCKET_VMNET_PLIST_LABEL}",
                ],
                check=False,
            )
        import time as _time

        for _ in range(50):
            if socket_vmnet_reachable():
                return
            _time.sleep(0.1)

    raise RuntimeError(
        f"socket_vmnet is not reachable at {socket_vmnet_socket_path()}.\n"
        f"VMs on macOS need the socket_vmnet daemon running.\n"
        f"Fix with:\n"
        f"  ltvm install          # installs + loads the launchd plist\n"
        f"or manually:\n"
        f"  sudo launchctl bootstrap system {SOCKET_VMNET_PLIST_PATH}"
    )


def _podman_machine_list_macos() -> list[dict[str, Any]]:
    """Return `podman machine list --format json` parsed, or [] on failure."""
    try:
        r = subprocess.run(
            ["podman", "machine", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        parsed = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return parsed


PODMAN_MACHINE_MEMORY_MIB = 4096
"""Floor for podman machine memory (MiB).

Cross-compiling a RHEL kernel with module BTF runs pahole on every .ko
in parallel; peak RSS easily clears 3 GB on the largest modules
(amdgpu.ko in particular).  At podman's 2 GB default the OOM killer
takes out the BTF step and the build fails with cryptic Error 137.
4 GB has headroom for parallel pahole + ld + cc1 without thrashing.
"""


def _podman_machine_memory(name: str) -> int | None:
    try:
        r = subprocess.run(
            [
                "podman", "machine", "inspect", name,
                "--format", "{{.Resources.Memory}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


LTVM_PODMAN_PROVIDER = "applehv"
"""podman machine backend ltvm pins on macOS.

applehv is Apple's native Hypervisor.framework: built into macOS, needs no
helper binary, and works on Intel and Apple Silicon.  The alternative,
libkrun, drives the VM through a separate ``krunkit`` executable and exists
only to add GPU passthrough -- which ltvm's build VM never uses.  Recent
podman / Podman Desktop default to libkrun on Apple Silicon, and a krunkit
that is missing or (typically when installed via Homebrew) only half-working
kills ``podman machine start`` with the opaque::

    Error: krunkit exited unexpectedly with exit code 2

Pinning applehv sidesteps that whole failure mode.
"""

_PODMAN_PROVIDER_CONF = (
    Path.home() / ".config" / "containers" / "containers.conf.d"
    / "ltvm-machine-provider.conf"
)


def _pin_podman_provider_applehv() -> None:
    """Make applehv podman's durable default machine provider.

    Writes a ``containers.conf.d`` drop-in -- the persistent, shell-
    independent equivalent of ``export CONTAINERS_MACHINE_PROVIDER=applehv``
    -- so every podman invocation (ltvm's and the user's own) agrees on the
    backend.  Best-effort: a failure here is logged, not fatal, since the
    init/start paths also pass the provider via the environment.
    """
    body = (
        "# Managed by ltvm.  Pin podman machine to Apple's native\n"
        "# Hypervisor.framework backend; libkrun/krunkit is GPU-only and a\n"
        "# frequent source of 'krunkit exited unexpectedly' start failures.\n"
        "# Delete this file to let podman choose its own default again.\n"
        "[machine]\n"
        f'provider = "{LTVM_PODMAN_PROVIDER}"\n'
    )
    try:
        if (
            _PODMAN_PROVIDER_CONF.is_file()
            and _PODMAN_PROVIDER_CONF.read_text() == body
        ):
            return
        _PODMAN_PROVIDER_CONF.parent.mkdir(parents=True, exist_ok=True)
        _PODMAN_PROVIDER_CONF.write_text(body)
        log.info(
            "Pinned podman machine provider to %s (%s)",
            LTVM_PODMAN_PROVIDER, _PODMAN_PROVIDER_CONF,
        )
    except OSError as e:
        log.warning(
            "could not pin podman provider to %s: %s",
            LTVM_PODMAN_PROVIDER, e,
        )


def _podman_provider_env(
    provider: str = LTVM_PODMAN_PROVIDER,
) -> dict[str, str]:
    """os.environ with CONTAINERS_MACHINE_PROVIDER forced to *provider*."""
    return dict(os.environ, CONTAINERS_MACHINE_PROVIDER=provider)


def _podman_machine_init_applehv() -> None:
    log.info(
        "Initializing podman machine on the %s backend "
        "(podman machine init --memory=%s)...",
        LTVM_PODMAN_PROVIDER, PODMAN_MACHINE_MEMORY_MIB,
    )
    _run(
        ["podman", "machine", "init",
         "--memory", str(PODMAN_MACHINE_MEMORY_MIB)],
        env=_podman_provider_env(),
    )


def _default_or_first(
    machines: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for m in machines:
        if m.get("Default"):
            return m
    return machines[0] if machines else None


def install_podman_macos(force: bool = False) -> bool:
    """Install podman on macOS and ensure a podman machine is running.

    Returns True if this call started (or initialized+started) the machine,
    False if it was already running.  Idempotent and safe to re-run.

    ltvm pins the applehv backend (see :data:`LTVM_PODMAN_PROVIDER`): on
    Apple Silicon podman otherwise defaults to libkrun, whose krunkit helper
    routinely fails to start, and ltvm never needs its GPU support.
    """
    if not shutil.which("podman"):
        brew = shutil.which("brew")
        if not brew:
            raise RuntimeError(
                "Homebrew not found. Install it from https://brew.sh, "
                "then run: brew install podman"
            )
        log.info("Installing podman via Homebrew...")
        _run([brew, "install", "podman"])
    elif not force:
        log.info("podman already installed")

    # Make applehv the durable default before touching any machine, so this
    # call -- and every later podman command -- targets the same backend.
    _pin_podman_provider_applehv()

    machines = _podman_machine_list_macos()
    # Machines ltvm can drive as-is: applehv, or anything not *explicitly*
    # libkrun (older podman may omit VMType -- never destroy what we cannot
    # positively identify as the broken backend).
    libkrun = [m for m in machines if m.get("VMType") == "libkrun"]
    usable = [m for m in machines if m.get("VMType") != "libkrun"]

    if not usable:
        # The only machine(s) present are on libkrun -- the backend that
        # fails to start without a working krunkit -- so retire them and
        # build an applehv replacement.  When a usable machine also exists
        # we skip this entirely and leave any libkrun machine untouched.
        for m in libkrun:
            nm = m.get("Name") or "podman-machine-default"
            log.warning(
                "Retiring podman machine '%s' on the libkrun provider; ltvm "
                "pins applehv.  Re-create it yourself if you need libkrun.",
                nm,
            )
            prov_env = _podman_provider_env("libkrun")
            _run(["podman", "machine", "stop", nm],
                 check=False, env=prov_env)
            _run(["podman", "machine", "rm", "--force", nm],
                 check=False, env=prov_env)
        _podman_machine_init_applehv()
        machines = _podman_machine_list_macos()
        usable = [m for m in machines if m.get("VMType") != "libkrun"]
    else:
        # Bump under-provisioned machines so cross kernel builds don't OOM.
        for m in usable:
            name = m.get("Name") or "podman-machine-default"
            mem = _podman_machine_memory(name)
            if mem is None or mem >= PODMAN_MACHINE_MEMORY_MIB:
                continue
            log.info(
                f"Bumping {name} memory: {mem} -> "
                f"{PODMAN_MACHINE_MEMORY_MIB} MiB (kernel BTF needs headroom)"
            )
            running = bool(m.get("Running"))
            if running:
                _run(["podman", "machine", "stop", name],
                     env=_podman_provider_env())
            _run([
                "podman", "machine", "set", name,
                "--memory", str(PODMAN_MACHINE_MEMORY_MIB),
            ], env=_podman_provider_env())
            if running:
                _run(["podman", "machine", "start", name],
                     env=_podman_provider_env())
                m["Running"] = True

    target = _default_or_first(usable)
    if target is not None and target.get("Running"):
        log.info("podman machine already running")
        return False

    name = (target.get("Name") if target else None) or "podman-machine-default"
    log.info("Starting podman machine (podman machine start)...")
    _run(["podman", "machine", "start", name], env=_podman_provider_env())
    return True


def should_stop_podman_machine_macos() -> bool:
    """Return True if it's safe to auto-stop the podman machine.

    Safe means: no containers running, or every running container uses an
    ltvm build image (tag starts with 'ltvm-build-' or 'ltvm-').  If any
    non-ltvm container is running the user is doing other podman work, so
    we leave the machine up.
    """
    try:
        r = subprocess.run(
            ["podman", "ps", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False
    try:
        parsed = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, list):
        return False
    if not parsed:
        return True
    for c in parsed:
        images: list[str] = []
        img = c.get("Image")
        if isinstance(img, str):
            images.append(img)
        names = c.get("ImageName") or c.get("Names")
        if isinstance(names, list):
            images.extend(n for n in names if isinstance(n, str))
        elif isinstance(names, str):
            images.append(names)
        if not images:
            return False
        if not all(_is_ltvm_image_tag(t) for t in images):
            return False
    return True


def _is_ltvm_image_tag(tag: str) -> bool:
    """Return True if the image tag is an ltvm-produced build image."""
    base = tag.rsplit("/", 1)[-1]
    return base.startswith("ltvm-build-") or base.startswith("ltvm-")


def stop_podman_machine_macos() -> None:
    """Stop the podman machine; log and swallow errors."""
    log.info("Stopping podman machine (podman machine stop)...")
    try:
        _run(["podman", "machine", "stop"], check=False, quiet=True)
    except OSError as e:
        log.warning("podman machine stop failed: %s", e)


def install_qemu_macos(force: bool = False) -> None:
    """Install QEMU on macOS via Homebrew."""
    existing = _qemu_installed_version()
    if existing and not force:
        log.info("QEMU %s already installed", existing)
        return

    brew_prefix = _brew_qemu_prefix()

    if not brew_prefix:
        brew = shutil.which("brew")
        if not brew:
            raise RuntimeError(
                "Homebrew not found. Install it from https://brew.sh, "
                "then run: brew install qemu"
            )
        log.info("Installing QEMU via Homebrew...")
        _run([brew, "install", "qemu"])
        brew_prefix = _brew_qemu_prefix()
        if not brew_prefix:
            raise RuntimeError(
                "brew install qemu succeeded but qemu-system-x86_64 not found "
                "in the expected Homebrew prefix. Check: brew --prefix qemu"
            )

    _sudo_run(["mkdir", "-p", str(QEMU_PREFIX / "bin")], quiet=True)

    for tool in ("qemu-system-x86_64", "qemu-system-aarch64", "qemu-img"):
        src = brew_prefix / "bin" / tool
        if src.exists():
            link = QEMU_PREFIX / "bin" / tool
            _sudo_run(["rm", "-f", str(link)], quiet=True)
            _sudo_run(["ln", "-s", str(src), str(link)], quiet=True)

    # Homebrew puts firmware under <prefix>/share/qemu/; we need
    # QEMU_PREFIX/share/qemu/ to point there so QEMU finds it.
    brew_share_qemu = brew_prefix / "share" / "qemu"
    if brew_share_qemu.is_dir():
        share_dir = QEMU_PREFIX / "share"
        _sudo_run(["mkdir", "-p", str(share_dir)], quiet=True)
        share_link = share_dir / "qemu"
        _sudo_run(["rm", "-f", str(share_link)], quiet=True)
        _sudo_run(["ln", "-s", str(brew_share_qemu), str(share_link)], quiet=True)

    ver_r = _run_quiet(
        [str(QEMU_PREFIX / "bin" / "qemu-system-x86_64"), "--version"],
        check=False,
    )
    ver_m = re.search(r"version (\d+\.\d+\.\d+)", ver_r.stdout)
    ver = ver_m.group(1) if ver_m else "unknown"
    log.info("Using Homebrew QEMU %s (%s)", ver, brew_prefix)


def install_image_tools_macos(force: bool = False) -> None:
    """Install host tools needed by `ltvm build image` on macOS.

    image_build assembles the ext4 rootfs on the host with `mke2fs -d`
    plus the rest of e2fsprogs (`e2fsck`, `tune2fs`, `resize2fs`) and
    `fakeroot`.  None ship in macOS.  Brew has all of them.
    e2fsprogs is keg-only (its sbin/ collides with macOS's BSD
    counterparts in /sbin), so we symlink each binary we use into
    /usr/local/bin/ where shutil.which() will find it without
    polluting all of e2fsprogs onto PATH.  fakeroot installs at
    /opt/homebrew/bin which is already on PATH for typical Mac shells.
    """
    brew = shutil.which("brew")
    if not brew:
        raise RuntimeError(
            "Homebrew not found.  Install it from https://brew.sh, "
            "then run: brew install e2fsprogs fakeroot"
        )

    have_fakeroot = bool(shutil.which("fakeroot"))
    if not have_fakeroot:
        log.info("Installing fakeroot via Homebrew...")
        _run([brew, "install", "fakeroot"])

    # `fakeroot /bin/bash -c <script>` runs without DYLD_INSERT_LIBRARIES
    # because SIP strips it from system bash, defeating the fakeroot
    # uid spoof and producing ext4 images whose inodes carry the host
    # uid (resulting in "must be owned by root" failures from sshd
    # and friends at first boot).  A brew bash isn't SIP-protected,
    # so we install it and image_build uses it explicitly.
    if not Path("/opt/homebrew/bin/bash").exists() or force:
        log.info("Installing bash via Homebrew (needed for fakeroot)...")
        _run([brew, "install", "bash"])

    e2fs_prefix: Path | None = None
    r = _run_quiet([brew, "--prefix", "e2fsprogs"], check=False)
    if r.returncode == 0 and r.stdout.strip():
        candidate = Path(r.stdout.strip())
        if (candidate / "sbin" / "mke2fs").exists():
            e2fs_prefix = candidate
    if not e2fs_prefix:
        log.info("Installing e2fsprogs via Homebrew...")
        _run([brew, "install", "e2fsprogs"])
        r = _run_quiet([brew, "--prefix", "e2fsprogs"], check=True)
        e2fs_prefix = Path(r.stdout.strip())

    bin_dir = Path("/usr/local/bin")
    primed_dir = False
    # Tools image_build invokes by bare name -- needs to be findable
    # via shutil.which / PATH.  e2fsck, tune2fs, resize2fs are used in
    # the post-mke2fs check + reshrink + feature re-enable steps.
    for tool in ("mke2fs", "e2fsck", "tune2fs", "resize2fs"):
        src = e2fs_prefix / "sbin" / tool
        if not src.exists():
            continue
        link = bin_dir / tool
        need_link = (
            force
            or not link.is_symlink()
            or link.resolve() != src.resolve()
        )
        if not need_link:
            continue
        if not primed_dir:
            _sudo_run(["mkdir", "-p", str(bin_dir)], quiet=True)
            primed_dir = True
        _sudo_run(["rm", "-f", str(link)], quiet=True)
        _sudo_run(["ln", "-s", str(src), str(link)], quiet=True)
        log.info("%s symlinked at %s -> %s", tool, link, src)


def install_qemu(host: HostInfo, force: bool = False) -> None:
    """Install QEMU with microvm support.

    Tries the system package first (fast).  Falls back to building
    from source only if the packaged QEMU lacks microvm support.
    """
    existing = _qemu_installed_version()
    if existing and not force:
        log.info("QEMU %s already installed", existing)
        return

    # Step 1: check if a system QEMU already has microvm
    sys_qemu = _system_qemu_has_microvm()

    # Step 2: if not, try installing the system package
    if not sys_qemu and not force:
        log.info("Installing system QEMU package...")
        if host.pkg_mgr == "dnf":
            _pkg_install(host, "qemu-kvm")
        elif host.pkg_mgr == "apt":
            _run(["apt-get", "update", "-qq"])
            _pkg_install(host, "qemu-system-x86")
        sys_qemu = _system_qemu_has_microvm()

    # Step 3: if system QEMU has microvm, symlink it
    if sys_qemu and not force:
        QEMU_PREFIX.mkdir(parents=True, exist_ok=True)
        (QEMU_PREFIX / "bin").mkdir(exist_ok=True)
        for tool in ("qemu-system-x86_64", "qemu-system-aarch64", "qemu-img"):
            sys_tool = shutil.which(tool)
            if sys_tool:
                link = QEMU_PREFIX / "bin" / tool
                link.unlink(missing_ok=True)
                link.symlink_to(sys_tool)
        # Also try to install aarch64 system package if not already present
        if not _system_qemu_has_virt():
            log.info("Installing qemu-system-aarch64 for cross-arch support...")
            if host.pkg_mgr == "dnf":
                _pkg_install(host, "qemu-system-aarch64")
            elif host.pkg_mgr == "apt":
                _pkg_install(host, "qemu-system-arm")
            sys_aarch64 = _system_qemu_has_virt()
            if sys_aarch64:
                link = QEMU_PREFIX / "bin" / "qemu-system-aarch64"
                link.unlink(missing_ok=True)
                link.symlink_to(sys_aarch64)
        ver_r = _run_quiet([sys_qemu, "--version"], check=False)
        ver_m = re.search(r"version (\d+\.\d+\.\d+)", ver_r.stdout)
        ver = ver_m.group(1) if ver_m else "unknown"
        log.info("Using system QEMU %s (%s) -- has microvm", ver, sys_qemu)
        _install_qemu_path_profile()
        return

    # Step 4: try pre-built QEMU binary from GitHub
    if _fetch_prebuilt_qemu(host):
        _install_qemu_path_profile()
        return

    # Step 5: build from source -- installs to QEMU_PREFIX (/opt/qemu),
    # does not replace the system QEMU.
    log.info(
        "System QEMU lacks microvm support and no pre-built binary "
        "available. Building QEMU %s from source into %s.",
        QEMU_VERSION,
        QEMU_PREFIX,
    )

    if existing:
        log.info("QEMU %s installed, rebuilding to %s", existing, QEMU_VERSION)

    # Build from source
    log.info("Installing QEMU build dependencies...")
    if host.pkg_mgr == "dnf":
        _run(["dnf", "install", "-y", "epel-release"])
        # The repo providing extra build deps (glib2-devel, pixman-devel,
        # etc.) has different names per EL major version:
        #   EL8  -> "powertools" (or "PowerTools" on early 8.x releases)
        #   EL9+ -> "crb"
        # See beads issue lustre_test_vms_v2-xr3. We code defensively:
        # try the expected name for the detected EL major first, then
        # fall back to the other known spellings so this works on any
        # Rocky/RHEL/Alma 8/9/10 host without needing an EL8 test rig.
        el_major = host.version.split(".", 1)[0] if host.version else ""
        if el_major == "8":
            crb_candidates = ["powertools", "PowerTools", "crb"]
        else:
            crb_candidates = ["crb", "powertools", "PowerTools"]
        for repo_name in crb_candidates:
            r = _run(
                ["dnf", "config-manager", "--set-enabled", repo_name],
                check=False,
                quiet=True,
            )
            if r.returncode == 0:
                log.info("Enabled repo %s", repo_name)
                break
        else:
            log.warning(
                "Could not enable CRB/PowerTools repo; tried %s. "
                "Build deps may fail to install.",
                ", ".join(crb_candidates),
            )
        _pkg_install(
            host,
            "gcc",
            "make",
            "glib2-devel",
            "pixman-devel",
            "python3",
            "python3-pip",
            "flex",
            "bison",
            "ninja-build",
        )
    elif host.pkg_mgr == "apt":
        _run(["apt-get", "update", "-qq"])
        _pkg_install(
            host,
            "gcc",
            "make",
            "libglib2.0-dev",
            "libpixman-1-dev",
            "python3",
            "python3-pip",
            "flex",
            "bison",
            "ninja-build",
            "pkg-config",
        )

    _run(["pip3", "install", "tomli"])

    tmpdir = Path(tempfile.mkdtemp(prefix="qemu-build."))
    try:
        log.info("Downloading QEMU %s...", QEMU_VERSION)
        url = f"https://download.qemu.org/qemu-{QEMU_VERSION}.tar.xz"
        _run(["curl", "-fsSL", url, "-o", str(tmpdir / "qemu.tar.xz")])
        _run(["tar", "xJf", str(tmpdir / "qemu.tar.xz"), "-C", str(tmpdir)])

        srcdir = tmpdir / f"qemu-{QEMU_VERSION}"

        log.info("Configuring...")
        _run(
            [
                str(srcdir / "configure"),
                "--target-list=x86_64-softmmu,aarch64-softmmu",
                "--disable-docs",
                "--disable-user",
                "--disable-gtk",
                "--disable-sdl",
                "--disable-vnc",
                "--disable-spice",
                "--disable-opengl",
                "--disable-xen",
                "--disable-curl",
                "--disable-rbd",
                "--disable-libssh",
                "--disable-capstone",
                "--disable-dbus-display",
                f"--prefix={QEMU_PREFIX}",
            ],
            check=True,
            cwd=srcdir,
        )

        ncpu = os.cpu_count() or 4
        log.info("Building (this takes a few minutes)...")
        _run(["make", f"-j{ncpu}"], check=True, cwd=srcdir)
        _run(["make", "install"], check=True, cwd=srcdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # Verify microvm support (x86_64)
    qemu_x86 = QEMU_PREFIX / "bin" / "qemu-system-x86_64"
    r = _run_quiet([str(qemu_x86), "-machine", "help"])
    if "microvm" not in r.stdout:
        raise RuntimeError("QEMU built but microvm machine type not available")

    # Verify virt support (aarch64)
    qemu_arm = QEMU_PREFIX / "bin" / "qemu-system-aarch64"
    if qemu_arm.exists():
        r = _run_quiet([str(qemu_arm), "-machine", "help"])
        if "virt" not in r.stdout:
            raise RuntimeError(
                "QEMU aarch64 built but virt machine type not available"
            )
    else:
        raise RuntimeError(
            f"qemu-system-aarch64 not found after build at {qemu_arm}"
        )

    _install_qemu_path_profile()

    log.info("QEMU %s installed at %s", QEMU_VERSION, QEMU_PREFIX)


# ------------------------------------------------------------------
# Network bridge
# ------------------------------------------------------------------


def _network_already_configured(subnet: str) -> bool:
    """Detect a working pre-existing network setup we should not touch.

    Returns True if BOTH:
      - the fcbr0 bridge interface exists and has the expected subnet
        address (so the user has already brought it up), AND
      - dnsmasq.service is active (so something is already serving DHCP
        on the bridge)

    When True, setup_network() short-circuits.  This protects users who
    have a hand-rolled or coexisting dnsmasq setup (e.g. a separate
    firecracker drop-in shipping `bind-interfaces`, which would conflict
    with our `bind-dynamic` and prevent dnsmasq from restarting).  We
    only touch the network when nothing is there.

    The check is intentionally narrow: we don't try to inspect the
    dnsmasq config or verify it serves the *right* subnet -- if the
    bridge has the right address and dnsmasq is running, that's
    "working from the host's perspective" and we leave it alone.
    """
    expected_addr = f"{subnet}.1/24"
    r = _run_quiet(["ip", "-4", "addr", "show", "dev", "fcbr0"], check=False)
    if r.returncode != 0 or expected_addr not in r.stdout:
        return False
    r = _run_quiet(["systemctl", "is-active", "dnsmasq"], check=False)
    return r.returncode == 0


def setup_network(host: HostInfo, subnet: str = DEFAULT_SUBNET) -> None:
    """Configure fcbr0 bridge, dnsmasq, and NAT."""
    if _network_already_configured(subnet):
        log.info(
            "fcbr0 bridge on %s.0/24 and dnsmasq already configured -- "
            "leaving network setup untouched",
            subnet,
        )
        # Still persist the subnet file so vm_state.SUBNET reads the
        # right value at import time even when we skip everything else.
        VM_DIR.mkdir(parents=True, exist_ok=True)
        (VM_DIR / "subnet").write_text(subnet + "\n")
        return

    log.info("Configuring network bridge (fcbr0) on %s.0/24", subnet)

    # Persist the chosen subnet so vm_state.SUBNET (read at import time
    # from this file in vm_net's process) matches the host bridge.
    # Without this, --subnet would only configure the host side and
    # vm_net.alloc_ip would still hand out 192.168.100.x addresses.
    VM_DIR.mkdir(parents=True, exist_ok=True)
    (VM_DIR / "subnet").write_text(subnet + "\n")

    # WSL2: ensure iptables-legacy is used.
    # iptables-nft can misbehave in WSL2 kernels lacking full nftables support.
    if is_wsl2():
        legacy = shutil.which("iptables-legacy")
        if legacy:
            _run(
                ["update-alternatives", "--set", "iptables", legacy],
            )
            alt6 = shutil.which("ip6tables-legacy")
            if alt6:
                _run(
                    ["update-alternatives", "--set", "ip6tables", alt6],
                )
            log.info("WSL2: using iptables-legacy")

    if host.pkg_mgr == "dnf":
        _pkg_install(host, "dnsmasq", "iptables-nft")
    elif host.pkg_mgr == "apt":
        _pkg_install(host, "dnsmasq", "iptables")

    # Verify critical deps installed
    for cmd in ("dnsmasq", "iptables"):
        if not shutil.which(cmd):
            raise RuntimeError(
                f"{cmd} not found after install -- "
                "check package manager errors above"
            )

    # sysctl: enable IP forwarding (+ disable bridge netfilter if the
    # br_netfilter module is loadable).  Fresh kernels in nested VMs
    # (notably UTM Ubuntu) don't auto-load br_netfilter, so the
    # bridge-nf-call-* sysctls in our conf reference /proc paths that
    # don't exist yet and `sysctl -p` fails.  Try to load the module +
    # persist; if it's genuinely unavailable (WSL2, minimal containers,
    # chroots), drop the bridge lines from the conf -- there's no
    # bridge<->iptables interaction to disable in that case anyway.
    try:
        br_netfilter_ok = (
            subprocess.run(
                ["modprobe", "br_netfilter"],
                capture_output=True,
                text=True,
            ).returncode == 0
        )
    except FileNotFoundError:
        br_netfilter_ok = False

    if br_netfilter_ok:
        Path("/etc/modules-load.d").mkdir(parents=True, exist_ok=True)
        Path("/etc/modules-load.d/br_netfilter.conf").write_text(
            "br_netfilter\n"
        )

    sysctl_src = HOST_CONFIG_DIR / "99-qemu-vms.conf"
    sysctl_text = sysctl_src.read_text()
    if not br_netfilter_ok and not Path("/proc/sys/net/bridge").exists():
        log.warning(
            "br_netfilter not loadable; skipping bridge-nf-call sysctls"
        )
        sysctl_text = "".join(
            line + "\n"
            for line in sysctl_text.splitlines()
            if "bridge-nf-call" not in line
        )
    sysctl_dst = Path("/etc/sysctl.d/99-qemu-vms.conf")
    sysctl_dst.write_text(sysctl_text)
    _run(["sysctl", "-p", str(sysctl_dst)])

    # Generate bridge service with correct subnet
    svc_tmpl = (HOST_CONFIG_DIR / "qemu-bridge.service").read_text()
    svc_text = svc_tmpl.replace("192.168.100", subnet)
    Path("/etc/systemd/system/qemu-bridge.service").write_text(svc_text)

    # Generate dnsmasq config
    dns_tmpl = (HOST_CONFIG_DIR / "qemu-dnsmasq.conf").read_text()
    dns_text = dns_tmpl.replace("192.168.100", subnet)
    Path("/etc/dnsmasq.d").mkdir(exist_ok=True)
    Path("/etc/dnsmasq.d/qemu-vms.conf").write_text(dns_text)

    # Some distros (e.g. Rocky 9) ship /etc/dnsmasq.conf with bind-interfaces
    # set, which conflicts with bind-dynamic in our drop-in config.  Comment it
    # out so dnsmasq can start.
    system_dnsmasq = Path("/etc/dnsmasq.conf")
    if system_dnsmasq.exists():
        content = system_dnsmasq.read_text()
        if "\nbind-interfaces\n" in content:
            system_dnsmasq.write_text(
                content.replace(
                    "\nbind-interfaces\n",
                    "\n# bind-interfaces  # disabled by ltvm\n",
                )
            )
            log.info(
                "Commented out bind-interfaces in /etc/dnsmasq.conf"
                " (conflicts with bind-dynamic)"
            )

    _run(["systemctl", "daemon-reload"])
    _run(["systemctl", "enable", "--now", "qemu-bridge"])
    _run(["systemctl", "restart", "dnsmasq"])

    # Verify
    r = _run_quiet(["ip", "link", "show", "fcbr0"], check=False)
    if r.returncode != 0:
        raise RuntimeError(
            "fcbr0 bridge not created -- check: systemctl status qemu-bridge"
        )

    log.info("Bridge fcbr0 active at %s.1/24", subnet)


# ------------------------------------------------------------------
# Install scripts
# ------------------------------------------------------------------


def install_scripts(host: HostInfo) -> None:
    """Install dk-filter and VM dirs."""
    log.info("Installing scripts and VM directories")

    for d in ("overlays", "sockets"):
        p = VM_DIR / d
        p.mkdir(parents=True, exist_ok=True)
        p.chmod(0o755)

    # Heal existing .info files from installs predating the 0644 default
    # (non-root `ltvm list` would PermissionError otherwise).
    sockets = VM_DIR / "sockets"
    for info in sockets.glob("*.info"):
        try:
            info.chmod(0o644)
        except OSError:
            pass

    # dk-filter
    dk = PKG_DIR / "dk-filter"
    if dk.exists():
        dst = Path("/usr/local/bin/dk-filter")
        shutil.copy2(str(dk), str(dst))
        dst.chmod(0o755)

    # pdsh + sshpass for multi-node clusters
    if host.pkg_mgr == "dnf":
        _pkg_install(host, "pdsh", "pdsh-rcmd-ssh", "sshpass")
    elif host.pkg_mgr == "apt":
        _pkg_install(host, "pdsh", "sshpass")

    log.info("Installed to %s", VM_DIR)


# ------------------------------------------------------------------
# SSH config
# ------------------------------------------------------------------

SSH_BLOCK_MARKER = "# lustre-test-vms"


def setup_ssh(subnet: str = DEFAULT_SUBNET) -> None:
    """Configure host SSH for fast VM access."""
    log.info("Configuring SSH for fast VM access")

    ssh_dir = Path("/root/.ssh")
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    config = ssh_dir / "config"

    if config.exists():
        text = config.read_text()
        if SSH_BLOCK_MARKER in text:
            if f"Host {subnet}." in text:
                log.info("SSH config already current")
                return
            # Subnet changed -- strip old block
            log.info("Updating SSH config for new subnet")
            lines = text.splitlines(keepends=True)
            out = []
            skip = False
            for line in lines:
                if SSH_BLOCK_MARKER in line:
                    skip = True
                    continue
                if skip and line.strip() == "":
                    skip = False
                    continue
                if skip:
                    continue
                out.append(line)
            text = "".join(out)
            config.write_text(text)

    block = f"""
{SSH_BLOCK_MARKER}
Host {subnet}.*
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
    ServerAliveInterval 5
    ServerAliveCountMax 3
    ConnectTimeout 5
    User root
"""
    with config.open("a") as f:
        f.write(block)
    config.chmod(0o600)

    log.info("SSH config updated for %s.*", subnet)


# ------------------------------------------------------------------
# Verify
# ------------------------------------------------------------------


def verify(subnet: str = DEFAULT_SUBNET) -> dict[str, Any]:
    """Check existing setup.  Returns dict of results."""
    results: dict[str, Any] = {}
    macos = is_macos()

    # QEMU (x86_64)
    ver = _qemu_installed_version("x86_64")
    results["qemu"] = {
        "installed": ver is not None,
        "version": ver,
        "path": str(QEMU_PREFIX),
    }
    # QEMU (aarch64)
    ver_arm = _qemu_installed_version("aarch64")
    results["qemu_aarch64"] = {
        "installed": ver_arm is not None,
        "version": ver_arm,
    }

    if macos:
        # macOS uses Hypervisor.framework; no /dev/kvm
        results["kvm"] = {
            "available": True,
            "note": "Hypervisor.framework (macOS)",
        }
        results["bridge"] = {
            "up": True,
            "address": None,
            "note": "not required on macOS",
        }
        results["dnsmasq"] = {"running": True, "note": "not required on macOS"}
        results["ssh"] = {"configured": True, "note": "not required on macOS"}
        bin_path = socket_vmnet_path()
        results["socket_vmnet"] = {
            "installed": bin_path is not None,
            "path": str(bin_path) if bin_path else None,
            "plist": (
                str(SOCKET_VMNET_PLIST_PATH)
                if SOCKET_VMNET_PLIST_PATH.exists()
                else None
            ),
            "loaded": _socket_vmnet_daemon_loaded(),
            "reachable": socket_vmnet_reachable(),
            "socket": str(socket_vmnet_socket_path()),
        }
    else:
        # KVM
        results["kvm"] = {"available": Path("/dev/kvm").exists()}

        # Bridge
        r = _run_quiet(["ip", "-4", "addr", "show", "fcbr0"], check=False)
        bridge_up = r.returncode == 0
        addr = None
        if bridge_up:
            m = re.search(r"inet (\S+)", r.stdout)
            addr = m.group(1) if m else None
        results["bridge"] = {
            "up": bridge_up,
            "address": addr,
        }

        # dnsmasq
        r = _run_quiet(["systemctl", "is-active", "dnsmasq"], check=False)
        results["dnsmasq"] = {
            "running": r.returncode == 0,
        }

        # SSH config
        ssh_config = Path("/root/.ssh/config")
        results["ssh"] = {
            "configured": (
                ssh_config.exists()
                and SSH_BLOCK_MARKER in ssh_config.read_text()
            ),
        }

    # Scripts
    results["ltvm"] = {
        "installed": shutil.which("ltvm") is not None,
        "path": shutil.which("ltvm"),
    }

    # podman
    pv = None
    if shutil.which("podman"):
        r = _run_quiet(["podman", "--version"], check=False)
        m = re.search(r"(\d+\.\d+\.\d+)", r.stdout or "")
        pv = m.group(1) if m else "unknown"
    results["podman"] = {
        "installed": pv is not None,
        "version": pv,
    }

    # zstd (used by release_package publish/fetch)
    zv = None
    zstd_path = shutil.which("zstd")
    if zstd_path:
        r = _run_quiet(["zstd", "--version"], check=False)
        m = re.search(r"v?(\d+\.\d+\.\d+)", r.stdout or "")
        zv = m.group(1) if m else "unknown"
    results["zstd"] = {
        "installed": zstd_path is not None,
        "version": zv,
    }

    # Overall
    checks = [
        results["qemu"]["installed"],
        results["kvm"]["available"],
        results["bridge"]["up"],
        results["dnsmasq"]["running"],
        results["ltvm"]["installed"],
        results["podman"]["installed"],
        results["zstd"]["installed"],
        results["ssh"]["configured"],
    ]
    if macos:
        checks.append(results["socket_vmnet"]["installed"])
    results["all_ok"] = all(checks)

    return results


def print_verify(results: dict[str, Any]) -> None:
    """Print verify results in human-readable form."""

    def ok(msg: str) -> None:
        print(f"  {msg}")

    def fail(msg: str) -> None:
        print(f"  WARNING: {msg}")

    q = results["qemu"]
    if q["installed"]:
        ok(f"QEMU x86_64: {q['version']} at {q['path']}")
    else:
        fail("QEMU x86_64: not installed")

    qa = results.get("qemu_aarch64", {})
    if qa.get("installed"):
        ok(f"QEMU aarch64: {qa['version']}")
    else:
        ok("QEMU aarch64: not installed (optional, for cross-arch targets)")

    kvm = results["kvm"]
    if kvm.get("note"):
        ok(f"KVM: {kvm['note']}")
    elif kvm["available"]:
        ok("KVM: available")
    else:
        fail("KVM: /dev/kvm not found")

    b = results["bridge"]
    if b.get("note"):
        ok(f"Bridge: {b['note']}")
    elif b["up"]:
        ok(f"Bridge: fcbr0 at {b['address']}")
    else:
        fail("Bridge: fcbr0 not found")

    dns = results["dnsmasq"]
    if dns.get("note"):
        ok(f"dnsmasq: {dns['note']}")
    elif dns["running"]:
        ok("dnsmasq: running")
    else:
        fail("dnsmasq: not running")

    s = results["ltvm"]
    if s["installed"]:
        ok(f"ltvm: {s['path']}")
    else:
        fail("ltvm: not in PATH")

    p = results["podman"]
    if p["installed"]:
        ok(f"podman: {p['version']}")
    else:
        fail("podman: not installed (needed by ltvm)")

    z = results.get("zstd", {})
    if z.get("installed"):
        ok(f"zstd: {z['version']}")
    else:
        fail("zstd: not installed (needed by ltvm target publish/fetch)")

    ssh = results["ssh"]
    if ssh.get("note"):
        ok(f"SSH config: {ssh['note']}")
    elif ssh["configured"]:
        ok("SSH config: configured")
    else:
        fail("SSH config: not configured")

    sv = results.get("socket_vmnet")
    if sv is not None:
        if not sv["installed"]:
            fail(
                "socket_vmnet: not installed "
                "(run `ltvm install` or `brew install socket_vmnet`)"
            )
        elif sv["reachable"]:
            ok(f"socket_vmnet: reachable at {sv['socket']}")
        elif sv["loaded"]:
            fail(
                f"socket_vmnet: launchd job loaded but socket "
                f"{sv['socket']} not yet open"
            )
        elif sv["plist"]:
            fail(
                f"socket_vmnet: plist at {sv['plist']} not loaded "
                f"(sudo launchctl bootstrap system {sv['plist']})"
            )
        else:
            fail(
                "socket_vmnet: installed but launchd plist missing "
                "(run `ltvm install`)"
            )

    print()
    if results["all_ok"]:
        print("All checks passed.")
    else:
        print("Some checks failed -- re-run setup for missing components.")


# ------------------------------------------------------------------
# ltvm launcher wrapper
# ------------------------------------------------------------------


def _render_ltvm_launcher(python: str, script: str) -> str:
    """Render the /usr/local/bin/ltvm wrapper script text.

    Pins the Python interpreter so PATH drift on the host (e.g. an older
    /usr/bin/python3 shadowing a brew python) can't send ltvm through a
    Python that fails its own 3.10 floor check.
    """
    return (
        "#!/bin/sh\n"
        f"exec '{python}' '{script}' \"$@\"\n"
    )


def _desired_ltvm_launcher(repo_ltvm: Path) -> str:
    return _render_ltvm_launcher(sys.executable, str(repo_ltvm.resolve()))


def _ltvm_launcher_needs_write(link: Path, repo_ltvm: Path) -> bool:
    """True if ``link`` isn't already our exact wrapper for ``repo_ltvm``."""
    desired = _desired_ltvm_launcher(repo_ltvm)
    try:
        if link.is_symlink() or not link.exists():
            return True
        return link.read_text() != desired
    except OSError:
        return True


def _install_ltvm_launcher(link: Path, repo_ltvm: Path) -> bool:
    """Install a shell wrapper at ``link`` that exec()s the repo's ltvm
    under the Python currently executing this code.

    Returns True if a write was performed, False if the existing wrapper
    was already identical (no sudo rewrite needed).
    """
    if not _ltvm_launcher_needs_write(link, repo_ltvm):
        return False

    desired = _desired_ltvm_launcher(repo_ltvm)
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="ltvm-launcher.", delete=False
    ) as tf:
        tf.write(desired)
        tmp_path = tf.name
    try:
        _sudo_run(["install", "-m", "0755", tmp_path, str(link)])
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    log.info("ltvm launcher installed to %s -> %s", link, sys.executable)
    return True


def _check_stale_ltvm_launcher(link: Path) -> None:
    """Warn if ``link`` is a wrapper pinning a Python that no longer exists.

    Happens after a Homebrew Python version change or a pyenv cleanup:
    the old wrapper still points at a binary that's gone, so any `ltvm`
    invocation fails with a cryptic /bin/sh exec error before Python runs.
    """
    try:
        if not link.exists() or link.is_symlink():
            return
        text = link.read_text()
    except OSError:
        return
    m = re.search(r"^exec '([^']+)'", text, flags=re.MULTILINE)
    if not m:
        return
    pinned = m.group(1)
    if Path(pinned).exists():
        return
    log.warning(
        "stale ltvm launcher at %s points to missing Python %s; "
        "re-run `./ltvm install` to repin it.",
        link,
        pinned,
    )


# ------------------------------------------------------------------
# Top-level orchestration
# ------------------------------------------------------------------


def _run_setup_macos(
    steps: list[str] | None = None,
    force: bool = False,
) -> None:
    if os.geteuid() == 0:
        raise RuntimeError(
            "Do not run `ltvm install` as root on macOS: Homebrew refuses "
            "to run as root.  Run it as your normal user -- it will invoke "
            "sudo only for the specific operations that need it."
        )

    all_steps = steps is None
    active: set[str] = set(steps or ["qemu", "network", "podman"])

    log.info("Host: macOS %s (%s)", platform.mac_ver()[0], platform.machine())

    ltvm_script = REPO_ROOT / "ltvm"
    link = Path("/usr/local/bin/ltvm")
    _check_stale_ltvm_launcher(link)
    need_launcher = ltvm_script.exists() and _ltvm_launcher_needs_write(
        link, ltvm_script
    )
    if "qemu" in active or need_launcher:
        _sudo_prime(
            "Installing ltvm on macOS needs sudo for /opt/qemu and "
            f"{link}"
        )

    if "qemu" in active:
        install_qemu_macos(force=force)
        install_image_tools_macos(force=force)

    if "network" in active:
        install_socket_vmnet_macos(force=force)
        install_socket_vmnet_launchd_macos(force=force)
        install_sshpass_macos(force=force)
        install_dnsmasq_macos(force=force)

    if "podman" in active:
        install_podman_macos(force=force)

    if ltvm_script.exists():
        if need_launcher:
            _install_ltvm_launcher(link, ltvm_script)
        else:
            log.info("ltvm launcher already current at %s", link)

    if all_steps:
        log.info("")
        log.info("Install complete.")
        log.info("")
        log.info("Note: VMs use socket_vmnet (vmnet-shared) for networking;")
        log.info("a small dnsmasq is bound to %s for VM<->VM name resolution.",
                 DEFAULT_VMNET_GATEWAY)
        log.info("")
        log.info("Next:")
        log.info("  ltvm target fetch rocky9")


def run_setup(
    steps: list[str] | None = None,
    subnet: str = DEFAULT_SUBNET,
    force: bool = False,
) -> None:
    """Run host setup.

    steps: list of step names, or None for all.
           Valid: "qemu", "network", "install", "ssh".
    """
    if is_macos():
        _run_setup_macos(steps=steps, force=force)
        return

    if os.geteuid() != 0:
        raise RuntimeError("Must run as root")

    host = HostInfo()
    log.info("Host: %s", host)

    check_prerequisites(host)

    all_steps = steps is None
    active: set[str] = set(steps or ["qemu", "network", "install", "ssh"])

    # KVM: hard-fail on full setup, warn on individual
    # steps
    if all_steps:
        check_kvm(require=True)
    else:
        check_kvm(require=False)

    if "qemu" in active:
        install_qemu(host, force=force)
    if "network" in active:
        setup_network(host, subnet=subnet)
    if "install" in active:
        install_scripts(host)
    if "ssh" in active:
        setup_ssh(subnet=subnet)

    # Always install the ltvm launcher in PATH
    ltvm_script = REPO_ROOT / "ltvm"
    if ltvm_script.exists():
        link = Path("/usr/local/bin/ltvm")
        _check_stale_ltvm_launcher(link)
        _install_ltvm_launcher(link, ltvm_script)

        # Some distros (Rocky 9, RHEL) ship sudoers with secure_path that
        # excludes /usr/local/bin, so `sudo ltvm` would fail with "command
        # not found".  Drop in a sudoers fragment to extend secure_path.
        sudoers_d = Path("/etc/sudoers.d")
        if sudoers_d.is_dir():
            sudoers_drop = sudoers_d / "ltvm"
            sudoers_drop.write_text(
                'Defaults secure_path="/sbin:/bin:/usr/sbin:/usr/bin:'
                '/usr/local/sbin:/usr/local/bin"\n'
            )
            sudoers_drop.chmod(0o440)
            log.info("sudo secure_path extended via %s", sudoers_drop)

    # Install bash tab completion via argcomplete.
    # We bake the output of `register-python-argcomplete ltvm` straight
    # into /etc/bash_completion.d/ltvm so the completion file is
    # self-contained -- no PATH lookup for register-python-argcomplete at
    # every shell startup, and it keeps working even if the venv moves.
    comp_dir = Path("/etc/bash_completion.d")
    register_bin = REPO_ROOT / ".venv" / "bin" / "register-python-argcomplete"
    if comp_dir.is_dir():
        if register_bin.exists():
            try:
                result = subprocess.run(
                    [str(register_bin), "ltvm"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                comp_dest = comp_dir / "ltvm"
                comp_dest.write_text(result.stdout)
                comp_dest.chmod(0o644)
                log.info("Tab completion installed to %s", comp_dest)
            except subprocess.CalledProcessError as e:
                log.warning(
                    "Failed to generate tab completion (%s): %s",
                    e.returncode,
                    (e.stderr or "").strip(),
                )
        else:
            log.warning(
                "argcomplete not found at %s; run `uv sync` and re-run "
                "`ltvm install` to get tab completion",
                register_bin,
            )

    if all_steps:
        log.info("")
        log.info("Install complete.")
        log.info("")
        log.info("Next:")
        log.info("  ltvm target fetch rocky9")
        log.info(
            "  sudo ltvm create co1-test --target rocky9 --vcpus 2 --mdt-disks 1 --ost-disks 2"
        )
        log.info("  ltvm llmount co1-test")
