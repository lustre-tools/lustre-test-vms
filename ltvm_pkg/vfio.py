"""VFIO-PCI bind/unbind helpers for PCIe device passthrough.

Standalone mechanics for handing a host PCIe device (typically an
SR-IOV VF) to a QEMU guest via vfio-pci.  These are pure functions
operating on /sys -- no VM-specific state, no QEMU wiring.  The
caller (future --nic passthrough:<BDF> code) is responsible for:

  * picking which BDF to bind
  * stashing the "from-driver" on VMInfo so destroy can restore it
  * refusing two VMs claiming the same BDF

VF preparation (sriov_numvfs, GUID / MAC assignment, policy=Up) is
site-specific and explicitly NOT this module's responsibility.  By
the time bind_to_vfio() sees a BDF, the VF is expected to already
be bound to its normal driver (mlx5_core, ixgbevf, ...).

All sysfs writes require root.  Callers get a clear error message
and can decide whether to re-exec under sudo.
"""

from __future__ import annotations

import errno
from pathlib import Path

# Exposed at module scope so tests can redirect to tmp_path without
# monkey-patching every call site.
SYSFS_ROOT = Path("/sys")
PROC_CMDLINE = Path("/proc/cmdline")


class VfioError(Exception):
    """Raised for any vfio bind/unbind / validation failure."""


def _pci_device_dir(bdf: str) -> Path:
    return SYSFS_ROOT / "bus" / "pci" / "devices" / bdf


def _pci_driver_dir(driver: str) -> Path:
    return SYSFS_ROOT / "bus" / "pci" / "drivers" / driver


def _sysfs_write(path: Path, value: str) -> None:
    """Write `value` to a sysfs attribute.

    Translates EACCES/EPERM into a friendly "needs root" message and
    any other OSError into VfioError with the path for context.
    """
    try:
        with open(path, "w") as f:
            f.write(value)
    except PermissionError as exc:
        raise VfioError(
            f"permission denied writing {path} "
            "(vfio bind needs root -- use sudo ltvm): "
            f"{exc}"
        ) from exc
    except OSError as exc:
        # EBUSY on unbind typically means the device is in use
        # (another VM, or the host still has an open handle).
        if exc.errno == errno.EBUSY:
            raise VfioError(
                f"device busy writing {path} "
                "(another process may hold the device): "
                f"{exc}"
            ) from exc
        raise VfioError(f"failed writing {path}: {exc}") from exc


def current_driver(bdf: str) -> str | None:
    """Return the name of the driver currently bound to `bdf`, or
    None if the device is unbound.

    Reads the /sys/bus/pci/devices/<bdf>/driver symlink and returns
    its basename (e.g. 'mlx5_core').  Returns None if the symlink is
    absent (unbound device).  Raises VfioError if the BDF itself is
    not present under /sys/bus/pci/devices.
    """
    dev_dir = _pci_device_dir(bdf)
    if not dev_dir.exists():
        raise VfioError(
            f"PCI device {bdf} not found under {dev_dir}"
        )
    driver_link = dev_dir / "driver"
    if not driver_link.exists() and not driver_link.is_symlink():
        return None
    try:
        target = driver_link.resolve()
    except OSError:
        return None
    return target.name


def bind_to_vfio(bdf: str) -> str | None:
    """Bind `bdf` to vfio-pci.

    Sequence:
      1. Read vendor:device from sysfs.
      2. Write "<vendor> <device>" to vfio-pci/new_id so vfio-pci
         recognises the device (space-separated, without the 0x
         prefix; the kernel parses hex either way but the canonical
         form is two whitespace-separated tokens).
      3. Unbind from the current driver.
      4. Write the BDF to vfio-pci/bind.

    Returns the name of the previous driver so the caller can stash
    it for rebind-on-destroy.  Returns None if the device was
    already bound to vfio-pci (idempotent no-op).

    Raises VfioError if the device is missing, vfio-pci is not
    available, or any sysfs write fails.
    """
    dev_dir = _pci_device_dir(bdf)
    if not dev_dir.exists():
        raise VfioError(
            f"PCI device {bdf} not found under {dev_dir}"
        )

    from_driver = current_driver(bdf)
    if from_driver == "vfio-pci":
        # Already bound -- nothing to do, no from-driver to report.
        return None

    vfio_dir = _pci_driver_dir("vfio-pci")
    if not vfio_dir.exists():
        raise VfioError(
            "vfio-pci driver not available -- is the vfio_pci "
            "module loaded?  (modprobe vfio-pci)"
        )

    # Read vendor + device IDs from sysfs.  They come back as
    # "0xXXXX\n"; strip the prefix and newline.
    try:
        vendor = (dev_dir / "vendor").read_text().strip()
        device = (dev_dir / "device").read_text().strip()
    except OSError as exc:
        raise VfioError(
            f"failed reading vendor/device IDs for {bdf}: {exc}"
        ) from exc
    if vendor.startswith("0x"):
        vendor = vendor[2:]
    if device.startswith("0x"):
        device = device[2:]

    # 1. Register the vendor:device with vfio-pci so it will accept
    #    the bind.  new_id may already know about this pair (e.g.
    #    from a previous bind); that write returns EEXIST, which
    #    we swallow.
    try:
        _sysfs_write(vfio_dir / "new_id", f"{vendor} {device}\n")
    except VfioError as exc:
        # EEXIST is fine: vfio-pci already has this ID registered.
        if "File exists" not in str(exc) and "EEXIST" not in str(exc):
            raise

    # 2. Unbind from the current driver (if any).
    if from_driver is not None:
        old_unbind = _pci_driver_dir(from_driver) / "unbind"
        _sysfs_write(old_unbind, f"{bdf}\n")

    # 3. Bind to vfio-pci.  new_id normally auto-binds matching
    #    devices, so the explicit bind may EEXIST; treat that as a
    #    successful bind.
    try:
        _sysfs_write(vfio_dir / "bind", f"{bdf}\n")
    except VfioError as exc:
        if "File exists" not in str(exc) and "EEXIST" not in str(exc):
            raise

    return from_driver


def rebind(bdf: str, driver: str) -> None:
    """Rebind `bdf` to `driver`.  Used on VM destroy to restore the
    host's original binding.

    If the device is currently bound (e.g. still to vfio-pci), it
    is unbound first.  A missing driver directory is fatal -- the
    caller almost certainly wants to know.
    """
    dev_dir = _pci_device_dir(bdf)
    if not dev_dir.exists():
        raise VfioError(
            f"PCI device {bdf} not found under {dev_dir}"
        )
    target_dir = _pci_driver_dir(driver)
    if not target_dir.exists():
        raise VfioError(
            f"target driver {driver!r} not present under "
            f"{target_dir} (module not loaded?)"
        )

    cur = current_driver(bdf)
    if cur == driver:
        return
    if cur is not None:
        _sysfs_write(_pci_driver_dir(cur) / "unbind", f"{bdf}\n")
    _sysfs_write(target_dir / "bind", f"{bdf}\n")


def iommu_enabled() -> bool:
    """True iff the host has a working IOMMU.

    Two conditions must both hold:
      * /proc/cmdline contains intel_iommu=on or amd_iommu=on (or
        iommu=pt, which some distros use as the enable flag).
      * /sys/kernel/iommu_groups/ exists and is non-empty.

    The second check catches the case where the cmdline flag is
    present but the IOMMU hardware didn't actually initialise
    (e.g. VT-d disabled in BIOS).
    """
    try:
        cmdline = PROC_CMDLINE.read_text()
    except OSError:
        return False

    tokens = cmdline.split()
    cmdline_ok = any(
        t in ("intel_iommu=on", "amd_iommu=on", "iommu=pt")
        for t in tokens
    )
    if not cmdline_ok:
        return False

    groups_dir = SYSFS_ROOT / "kernel" / "iommu_groups"
    if not groups_dir.is_dir():
        return False
    try:
        # Non-empty: at least one group directory exists.
        return any(groups_dir.iterdir())
    except OSError:
        return False


def resolve_ifname_to_bdf(ifname: str) -> str:
    """Map a Linux netdev name (e.g. 'ens17f0v0') to its PCIe BDF.

    Reads /sys/class/net/<ifname>/device, which is a symlink into
    /sys/bus/pci/devices/<bdf> for PCIe-backed devices.  The BDF is
    returned in standard "0000:XX:YY.Z" form.

    Raises VfioError if the netdev does not exist or is not backed
    by a PCI device (e.g. it's a virtual interface like a bridge or
    tap).
    """
    net_dir = SYSFS_ROOT / "class" / "net" / ifname
    if not net_dir.exists():
        raise VfioError(f"netdev {ifname!r} not found at {net_dir}")
    device_link = net_dir / "device"
    if not device_link.exists():
        raise VfioError(
            f"netdev {ifname!r} has no PCI parent "
            f"(no {device_link}); is it a virtual interface?"
        )
    try:
        target = device_link.resolve()
    except OSError as exc:
        raise VfioError(
            f"failed resolving {device_link}: {exc}"
        ) from exc

    # The resolved path for a PCI-backed netdev ends in the BDF.
    # Sanity-check format before returning.
    bdf = target.name
    if not _looks_like_bdf(bdf):
        raise VfioError(
            f"netdev {ifname!r} resolves to {target} which does "
            "not look like a PCI BDF"
        )
    return bdf


def _looks_like_bdf(s: str) -> bool:
    """Cheap check: XXXX:XX:XX.X with hex digits."""
    # e.g. 0000:85:00.1
    if len(s) != 12:
        return False
    if s[4] != ":" or s[7] != ":" or s[10] != ".":
        return False
    hex_chars = set("0123456789abcdefABCDEF")
    for i, ch in enumerate(s):
        if i in (4, 7, 10):
            continue
        if ch not in hex_chars:
            return False
    return True
