"""Host setup for Lustre QEMU test VMs.

Prepares a Linux host: builds QEMU, configures the network
bridge, installs vm.py/deploy-lustre.sh, and sets up SSH.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .platform import is_wsl2

log = logging.getLogger(__name__)

# Override the log format for setup output so it
# prints "==> message" instead of "lib.setup: message".
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("==> %(message)s"))
log.addHandler(_handler)
log.propagate = False

QEMU_VERSION = "9.2.2"
QEMU_PREFIX = Path(os.environ.get("LTVM_QEMU_PREFIX", "/opt/qemu"))
VM_DIR = Path(os.environ.get("LTVM_VM_DIR", "/opt/qemu-vms"))
DEFAULT_SUBNET = "192.168.100"

# Directory containing qemu/ host-config templates,
# relative to the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
QEMU_DIR = REPO_ROOT / "qemu"
HOST_CONFIG_DIR = QEMU_DIR / "host-config"


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
) -> subprocess.CompletedProcess[str]:
    """Run a command, return CompletedProcess."""
    log.debug("run: %s", " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, capture_output=quiet, text=True, cwd=cwd)
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


def _pkg_install(host: HostInfo, *pkgs: str) -> None:
    """Install packages using the host's package manager."""
    translated = _translate_pkgs(pkgs, host)
    if host.pkg_mgr == "dnf":
        _run(["dnf", "install", "-y", *translated], check=False)
    elif host.pkg_mgr == "apt":
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        subprocess.run(
            ["apt-get", "install", "-y", *translated], env=env, check=False
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
        "zstd": "zstd",
        "ip": "iproute2" if host.pkg_mgr == "apt" else "iproute",
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
    _BINARY_MAP = {"x86_64": "qemu-system-x86_64", "aarch64": "qemu-system-aarch64"}
    binary_name = _BINARY_MAP.get(arch, f"qemu-system-{arch}")
    qemu = QEMU_PREFIX / "bin" / binary_name
    if not qemu.exists():
        return None
    try:
        r = _run_quiet([str(qemu), "--version"], check=False)
        m = re.search(r"version (\d+\.\d+\.\d+)", r.stdout)
        return m.group(1) if m else "unknown"
    except Exception:
        return None


def _install_qemu_path_profile() -> None:
    """Ensure /opt/qemu/bin is in PATH for all users."""
    profile = Path("/etc/profile.d/qemu-vms.sh")
    profile.write_text(
        f'# Added by ltvm setup\nexport PATH="{QEMU_PREFIX}/bin:$PATH"\n'
    )


# GitHub release tag and asset names for pre-built QEMU binaries
_QEMU_RELEASE_TAG = f"qemu-{QEMU_VERSION}"
_QEMU_GITHUB_REPO = "lustre-tools/lustre-test-vms-v2"


def _fetch_prebuilt_qemu(host: HostInfo) -> bool:
    """Try to download pre-built QEMU binaries from GitHub.

    Returns True if installed successfully, False if not available.
    """
    # Determine which binary to fetch based on host OS
    osr = Path("/etc/os-release")
    os_id = "unknown"
    os_ver = "0"
    if osr.exists():
        for line in osr.read_text().splitlines():
            if line.startswith("ID="):
                os_id = line.split("=", 1)[1].strip('"').strip("'")
            elif line.startswith("VERSION_ID="):
                os_ver = line.split("=", 1)[1].strip('"').strip("'")

    if os_id in ("rocky", "rhel", "centos", "almalinux"):
        major = os_ver.split(".")[0]
        asset = f"qemu-{QEMU_VERSION}-el{major}.tar.gz"
    else:
        # No pre-built binary for this OS
        return False

    url = (
        f"https://github.com/{_QEMU_GITHUB_REPO}/releases/download/"
        f"{_QEMU_RELEASE_TAG}/{asset}"
    )

    log.info("Downloading pre-built QEMU %s for EL%s...", QEMU_VERSION, major)
    tmpdir = Path(tempfile.mkdtemp(prefix="qemu-fetch."))
    try:
        r = _run(
            ["curl", "-fsSL", url, "-o", str(tmpdir / asset)],
            check=False, quiet=True,
        )
        if r.returncode != 0:
            log.info("Pre-built QEMU not available for this platform")
            return False

        _run(["tar", "xf", str(tmpdir / asset), "-C", str(tmpdir)], check=True)

        QEMU_PREFIX.mkdir(parents=True, exist_ok=True)
        (QEMU_PREFIX / "bin").mkdir(exist_ok=True)
        for binary in ("qemu-system-x86_64", "qemu-system-aarch64", "qemu-img"):
            src = tmpdir / binary
            if src.exists():
                dst = QEMU_PREFIX / "bin" / binary
                shutil.copy2(str(src), str(dst))
                dst.chmod(0o755)

        # Verify microvm support (x86_64 binary)
        qemu = QEMU_PREFIX / "bin" / "qemu-system-x86_64"
        if qemu.exists():
            r = _run_quiet([str(qemu), "-machine", "help"], check=False)
            if "microvm" not in r.stdout:
                log.warning("Pre-built QEMU lacks microvm -- will build from source")
                return False

        log.info("Installed pre-built QEMU %s to %s", QEMU_VERSION, QEMU_PREFIX)
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _system_qemu_has_microvm() -> str | None:
    """Check if the system-packaged QEMU has microvm support.

    Returns the qemu binary path if microvm is available, else None.
    """
    for binary in ("qemu-system-x86_64", "qemu-kvm", "/usr/libexec/qemu-kvm"):
        path = shutil.which(binary) or binary
        try:
            r = _run_quiet([path, "-machine", "help"], check=False)
            if r.returncode == 0 and "microvm" in r.stdout:
                return path
        except (FileNotFoundError, OSError):
            continue
    return None


def _system_qemu_has_virt() -> str | None:
    """Check if the system-packaged QEMU aarch64 has virt machine support.

    Returns the qemu binary path if virt is available, else None.
    """
    for binary in ("qemu-system-aarch64",):
        path = shutil.which(binary) or binary
        try:
            r = _run_quiet([path, "-machine", "help"], check=False)
            if r.returncode == 0 and "virt" in r.stdout:
                return path
        except (FileNotFoundError, OSError):
            continue
    return None


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
            _run(["apt-get", "update", "-qq"], check=False)
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
        QEMU_VERSION, QEMU_PREFIX,
    )

    if existing:
        log.info("QEMU %s installed, rebuilding to %s", existing, QEMU_VERSION)

    # Build from source
    log.info("Installing QEMU build dependencies...")
    if host.pkg_mgr == "dnf":
        _run(["dnf", "install", "-y", "epel-release"], check=False)
        _run(["dnf", "config-manager", "--set-enabled", "crb"], check=False)
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
        _run(["apt-get", "update", "-qq"], check=False)
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

    _run(["pip3", "install", "tomli"], check=False)

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
            log.warning("QEMU aarch64 built but virt machine type not available")
    else:
        log.warning("qemu-system-aarch64 not found after build")

    _install_qemu_path_profile()

    log.info("QEMU %s installed at %s", QEMU_VERSION, QEMU_PREFIX)


# ------------------------------------------------------------------
# Network bridge
# ------------------------------------------------------------------


def setup_network(host: HostInfo, subnet: str = DEFAULT_SUBNET) -> None:
    """Configure fcbr0 bridge, dnsmasq, and NAT."""
    log.info("Configuring network bridge (fcbr0) on %s.0/24", subnet)

    # WSL2: ensure iptables-legacy is used.
    # iptables-nft can misbehave in WSL2 kernels lacking full nftables support.
    if is_wsl2():
        legacy = shutil.which("iptables-legacy")
        if legacy:
            _run(
                ["update-alternatives", "--set", "iptables", legacy],
                check=False,
            )
            alt6 = shutil.which("ip6tables-legacy")
            if alt6:
                _run(
                    ["update-alternatives", "--set", "ip6tables", alt6],
                    check=False,
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

    # sysctl: enable IP forwarding
    sysctl_src = HOST_CONFIG_DIR / "99-qemu-vms.conf"
    sysctl_dst = Path("/etc/sysctl.d/99-qemu-vms.conf")
    sysctl_dst.write_text(sysctl_src.read_text())
    _run(["sysctl", "-p", str(sysctl_dst)], check=False)

    # Generate bridge service with correct subnet
    svc_tmpl = (HOST_CONFIG_DIR / "qemu-bridge.service").read_text()
    svc_text = svc_tmpl.replace("192.168.100", subnet)
    Path("/etc/systemd/system/qemu-bridge.service").write_text(svc_text)

    # Generate dnsmasq config
    dns_tmpl = (HOST_CONFIG_DIR / "qemu-dnsmasq.conf").read_text()
    dns_text = dns_tmpl.replace("192.168.100", subnet)
    Path("/etc/dnsmasq.d").mkdir(exist_ok=True)
    Path("/etc/dnsmasq.d/qemu-vms.conf").write_text(dns_text)

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
    """Install vm.py, deploy-lustre.sh, dk-filter, and qemu package."""
    log.info("Installing vm.py and deploy-lustre.sh")

    for d in ("overlays", "sockets", "kernel", "images"):
        (VM_DIR / d).mkdir(parents=True, exist_ok=True)

    for script in ("vm.py", "deploy-lustre.sh"):
        src = QEMU_DIR / script
        if not src.exists():
            log.warning("%s not found at %s, skipping", script, src)
            continue
        dst = VM_DIR / script
        shutil.copy2(str(src), str(dst))
        dst.chmod(0o755)
        link = Path("/usr/local/bin") / script
        link.unlink(missing_ok=True)
        link.symlink_to(dst)

    # vm.py imports from qemu/ package -- install it alongside
    qemu_pkg_dst = VM_DIR / "qemu"
    if QEMU_DIR.is_dir():
        if qemu_pkg_dst.exists():
            shutil.rmtree(str(qemu_pkg_dst))
        shutil.copytree(
            str(QEMU_DIR),
            str(qemu_pkg_dst),
            ignore=shutil.ignore_patterns("host-config"),
        )

    # targets.yaml -- needed by vm.py for --os resolution
    targets_yaml = REPO_ROOT / "targets" / "targets.yaml"
    if targets_yaml.exists():
        targets_dst = VM_DIR / "targets"
        targets_dst.mkdir(exist_ok=True)
        shutil.copy2(str(targets_yaml), str(targets_dst / "targets.yaml"))

    # dk-filter
    dk = QEMU_DIR / "dk-filter"
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

MARKER = "# lustre-test-vms"


def setup_ssh(subnet: str = DEFAULT_SUBNET) -> None:
    """Configure host SSH for fast VM access."""
    log.info("Configuring SSH for fast VM access")

    ssh_dir = Path("/root/.ssh")
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    config = ssh_dir / "config"

    if config.exists():
        text = config.read_text()
        if MARKER in text:
            if f"Host {subnet}." in text:
                log.info("SSH config already current")
                return
            # Subnet changed -- strip old block
            log.info("Updating SSH config for new subnet")
            lines = text.splitlines(keepends=True)
            out = []
            skip = False
            for line in lines:
                if MARKER in line:
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
{MARKER}
Host {subnet}.*
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
    ServerAliveInterval 1
    ServerAliveCountMax 2
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

    # Scripts
    for script in ("vm.py", "deploy-lustre.sh"):
        results[script] = {
            "installed": shutil.which(script) is not None,
            "path": shutil.which(script),
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

    # SSH config
    ssh_config = Path("/root/.ssh/config")
    results["ssh"] = {
        "configured": (
            ssh_config.exists() and MARKER in ssh_config.read_text()
        ),
    }

    # Overall
    results["all_ok"] = all(
        [
            results["qemu"]["installed"],
            results["kvm"]["available"],
            results["bridge"]["up"],
            results["dnsmasq"]["running"],
            results["vm.py"]["installed"],
            results["deploy-lustre.sh"]["installed"],
            results["podman"]["installed"],
            results["ssh"]["configured"],
        ]
    )

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

    if results["kvm"]["available"]:
        ok("KVM: available")
    else:
        fail("KVM: /dev/kvm not found")

    b = results["bridge"]
    if b["up"]:
        ok(f"Bridge: fcbr0 at {b['address']}")
    else:
        fail("Bridge: fcbr0 not found")

    if results["dnsmasq"]["running"]:
        ok("dnsmasq: running")
    else:
        fail("dnsmasq: not running")

    for script in ("vm.py", "deploy-lustre.sh"):
        s = results[script]
        if s["installed"]:
            ok(f"{script}: {s['path']}")
        else:
            fail(f"{script}: not in PATH")

    p = results["podman"]
    if p["installed"]:
        ok(f"podman: {p['version']}")
    else:
        fail("podman: not installed (needed by ltvm)")

    if results["ssh"]["configured"]:
        ok("SSH config: configured")
    else:
        fail("SSH config: not configured")

    print()
    if results["all_ok"]:
        print("All checks passed.")
    else:
        print("Some checks failed -- re-run setup for missing components.")


# ------------------------------------------------------------------
# Top-level orchestration
# ------------------------------------------------------------------


def run_setup(
    steps: list[str] | None = None,
    subnet: str = DEFAULT_SUBNET,
    force: bool = False,
    json_output: bool = False,
) -> None:
    """Run host setup.

    steps: list of step names, or None for all.
           Valid: "qemu", "network", "install", "ssh".
    """
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

    if all_steps:
        log.info("")
        log.info("Host setup complete.")
        log.info("")
        log.info("Next: build VM artifacts with ltvm:")
        log.info("  ./ltvm init rocky9 --lustre-tree /path/to/lustre")
        log.info("")
        log.info("Then create a VM:")
        log.info("  sudo ltvm vm ensure co1-single \\")
        log.info("      --vcpus 2 --mem 4096 --mdt-disks 1 --ost-disks 3")
