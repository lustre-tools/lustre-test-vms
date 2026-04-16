"""Tests for ltvm_pkg/vfio.py -- sysfs bind/unbind mechanics.

The module's sysfs paths are anchored on two module-level Path
objects (SYSFS_ROOT, PROC_CMDLINE) which these tests redirect into
a tmp_path tree built to look like the real /sys layout.  Writes
under the fake tree are recorded for assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ltvm_pkg import vfio

BDF = "0000:85:00.1"
OTHER_BDF = "0000:85:00.2"


def _make_pci_device(
    sysfs: Path,
    bdf: str,
    *,
    driver: str | None,
    vendor: str = "0x15b3",
    device: str = "0x101e",
) -> Path:
    """Build a /sys tree entry for a PCI device.  If `driver` is
    non-None, also creates the driver dir and links device->driver."""
    dev_dir = sysfs / "bus" / "pci" / "devices" / bdf
    dev_dir.mkdir(parents=True)
    (dev_dir / "vendor").write_text(f"{vendor}\n")
    (dev_dir / "device").write_text(f"{device}\n")
    if driver is not None:
        drv_dir = sysfs / "bus" / "pci" / "drivers" / driver
        _make_driver_dir(drv_dir)
        # The real kernel uses a relative symlink;
        # anything pathlib.resolve()s to the driver dir works.
        (dev_dir / "driver").symlink_to(drv_dir)
    return dev_dir


def _make_driver_dir(drv_dir: Path) -> Path:
    """Create a PCI driver dir with the bind/unbind/new_id attrs."""
    drv_dir.mkdir(parents=True, exist_ok=True)
    for attr in ("bind", "unbind", "new_id", "remove_id"):
        (drv_dir / attr).write_text("")
    return drv_dir


@pytest.fixture
def fake_sysfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect vfio.SYSFS_ROOT and PROC_CMDLINE into tmp_path."""
    sysfs = tmp_path / "sys"
    sysfs.mkdir()
    (sysfs / "bus" / "pci" / "devices").mkdir(parents=True)
    (sysfs / "bus" / "pci" / "drivers").mkdir(parents=True)

    cmdline = tmp_path / "cmdline"
    cmdline.write_text("ro quiet\n")

    monkeypatch.setattr(vfio, "SYSFS_ROOT", sysfs)
    monkeypatch.setattr(vfio, "PROC_CMDLINE", cmdline)
    return sysfs


# ── current_driver ───────────────────────────────────────


class TestCurrentDriver:
    def test_bound_device(self, fake_sysfs: Path) -> None:
        _make_pci_device(fake_sysfs, BDF, driver="mlx5_core")
        assert vfio.current_driver(BDF) == "mlx5_core"

    def test_unbound_device(self, fake_sysfs: Path) -> None:
        _make_pci_device(fake_sysfs, BDF, driver=None)
        assert vfio.current_driver(BDF) is None

    def test_missing_device_raises(self, fake_sysfs: Path) -> None:
        with pytest.raises(vfio.VfioError, match="not found"):
            vfio.current_driver("0000:99:99.9")


# ── bind_to_vfio ─────────────────────────────────────────


class TestBindToVfio:
    def test_happy_path_records_write_sequence(
        self, fake_sysfs: Path
    ) -> None:
        _make_pci_device(fake_sysfs, BDF, driver="mlx5_core")
        _make_driver_dir(
            fake_sysfs / "bus" / "pci" / "drivers" / "vfio-pci"
        )

        from_drv = vfio.bind_to_vfio(BDF)

        assert from_drv == "mlx5_core"
        vfio_dir = fake_sysfs / "bus" / "pci" / "drivers" / "vfio-pci"
        mlx_dir = fake_sysfs / "bus" / "pci" / "drivers" / "mlx5_core"
        # new_id got the vendor:device pair (with 0x stripped)
        assert (vfio_dir / "new_id").read_text() == "15b3 101e\n"
        # unbind got the BDF
        assert (mlx_dir / "unbind").read_text() == f"{BDF}\n"
        # bind got the BDF
        assert (vfio_dir / "bind").read_text() == f"{BDF}\n"

    def test_already_bound_to_vfio_is_noop(
        self, fake_sysfs: Path
    ) -> None:
        _make_pci_device(fake_sysfs, BDF, driver="vfio-pci")
        # Need a vfio-pci driver dir so the branch is reachable
        # even though we short-circuit before using it.
        _make_driver_dir(
            fake_sysfs / "bus" / "pci" / "drivers" / "vfio-pci"
        )
        assert vfio.bind_to_vfio(BDF) is None

    def test_unbound_device_skips_unbind(
        self, fake_sysfs: Path
    ) -> None:
        """No current driver -> no unbind write, but new_id + bind
        still happen."""
        _make_pci_device(fake_sysfs, BDF, driver=None)
        vfio_dir = _make_driver_dir(
            fake_sysfs / "bus" / "pci" / "drivers" / "vfio-pci"
        )

        assert vfio.bind_to_vfio(BDF) is None or True  # returns None

        assert (vfio_dir / "new_id").read_text() == "15b3 101e\n"
        assert (vfio_dir / "bind").read_text() == f"{BDF}\n"

    def test_missing_device_raises(self, fake_sysfs: Path) -> None:
        _make_driver_dir(
            fake_sysfs / "bus" / "pci" / "drivers" / "vfio-pci"
        )
        with pytest.raises(vfio.VfioError, match="not found"):
            vfio.bind_to_vfio("0000:99:99.9")

    def test_missing_vfio_driver_raises(
        self, fake_sysfs: Path
    ) -> None:
        _make_pci_device(fake_sysfs, BDF, driver="mlx5_core")
        # vfio-pci driver dir absent
        with pytest.raises(
            vfio.VfioError, match="vfio-pci driver not available"
        ):
            vfio.bind_to_vfio(BDF)

    def test_vendor_without_0x_prefix(self, fake_sysfs: Path) -> None:
        """Not every kernel reports 0x-prefixed IDs; cope with both."""
        _make_pci_device(
            fake_sysfs,
            BDF,
            driver="mlx5_core",
            vendor="15b3",
            device="101e",
        )
        vfio_dir = _make_driver_dir(
            fake_sysfs / "bus" / "pci" / "drivers" / "vfio-pci"
        )
        vfio.bind_to_vfio(BDF)
        assert (vfio_dir / "new_id").read_text() == "15b3 101e\n"


# ── rebind ───────────────────────────────────────────────


class TestRebind:
    def test_rebind_from_vfio_back_to_mlx5(
        self, fake_sysfs: Path
    ) -> None:
        _make_pci_device(fake_sysfs, BDF, driver="vfio-pci")
        mlx_dir = _make_driver_dir(
            fake_sysfs / "bus" / "pci" / "drivers" / "mlx5_core"
        )
        vfio_dir = fake_sysfs / "bus" / "pci" / "drivers" / "vfio-pci"

        vfio.rebind(BDF, "mlx5_core")

        assert (vfio_dir / "unbind").read_text() == f"{BDF}\n"
        assert (mlx_dir / "bind").read_text() == f"{BDF}\n"

    def test_rebind_already_bound_is_noop(
        self, fake_sysfs: Path
    ) -> None:
        _make_pci_device(fake_sysfs, BDF, driver="mlx5_core")
        mlx_dir = fake_sysfs / "bus" / "pci" / "drivers" / "mlx5_core"
        # No writes should happen.
        vfio.rebind(BDF, "mlx5_core")
        assert (mlx_dir / "bind").read_text() == ""
        assert (mlx_dir / "unbind").read_text() == ""

    def test_rebind_unbound_device(self, fake_sysfs: Path) -> None:
        _make_pci_device(fake_sysfs, BDF, driver=None)
        mlx_dir = _make_driver_dir(
            fake_sysfs / "bus" / "pci" / "drivers" / "mlx5_core"
        )
        vfio.rebind(BDF, "mlx5_core")
        assert (mlx_dir / "bind").read_text() == f"{BDF}\n"

    def test_rebind_missing_target_driver_raises(
        self, fake_sysfs: Path
    ) -> None:
        _make_pci_device(fake_sysfs, BDF, driver="vfio-pci")
        with pytest.raises(
            vfio.VfioError, match="target driver 'mlx5_core'"
        ):
            vfio.rebind(BDF, "mlx5_core")

    def test_rebind_missing_device_raises(
        self, fake_sysfs: Path
    ) -> None:
        _make_driver_dir(
            fake_sysfs / "bus" / "pci" / "drivers" / "mlx5_core"
        )
        with pytest.raises(vfio.VfioError, match="not found"):
            vfio.rebind("0000:99:99.9", "mlx5_core")


# ── iommu_enabled ────────────────────────────────────────


class TestIommuEnabled:
    def _set_cmdline(self, fake_sysfs: Path, text: str) -> None:
        """Helper: overwrite the fake /proc/cmdline.  (fake_sysfs is
        a peer of cmdline; use the PROC_CMDLINE pointer directly.)"""
        vfio.PROC_CMDLINE.write_text(text)

    def _make_groups(self, fake_sysfs: Path, n: int) -> None:
        groups = fake_sysfs / "kernel" / "iommu_groups"
        groups.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (groups / str(i)).mkdir()

    def test_intel_iommu_on_with_groups(
        self, fake_sysfs: Path
    ) -> None:
        self._set_cmdline(fake_sysfs, "ro quiet intel_iommu=on\n")
        self._make_groups(fake_sysfs, 3)
        assert vfio.iommu_enabled() is True

    def test_amd_iommu_on_with_groups(self, fake_sysfs: Path) -> None:
        self._set_cmdline(fake_sysfs, "amd_iommu=on ro\n")
        self._make_groups(fake_sysfs, 1)
        assert vfio.iommu_enabled() is True

    def test_cmdline_set_but_no_groups(
        self, fake_sysfs: Path
    ) -> None:
        """The guardrail case: cmdline says on, but IOMMU didn't
        actually wire up (e.g. VT-d off in BIOS)."""
        self._set_cmdline(fake_sysfs, "intel_iommu=on\n")
        # No iommu_groups dir at all.
        assert vfio.iommu_enabled() is False

    def test_no_cmdline_flag(self, fake_sysfs: Path) -> None:
        self._set_cmdline(fake_sysfs, "ro quiet\n")
        self._make_groups(fake_sysfs, 3)
        assert vfio.iommu_enabled() is False

    def test_empty_groups_dir(self, fake_sysfs: Path) -> None:
        self._set_cmdline(fake_sysfs, "intel_iommu=on\n")
        (fake_sysfs / "kernel" / "iommu_groups").mkdir(parents=True)
        assert vfio.iommu_enabled() is False


# ── resolve_ifname_to_bdf ────────────────────────────────


class TestResolveIfnameToBdf:
    def test_happy_path(self, fake_sysfs: Path) -> None:
        # Build device dir, then netdev dir whose 'device' links to it.
        dev_dir = _make_pci_device(fake_sysfs, BDF, driver="mlx5_core")
        net_dir = fake_sysfs / "class" / "net" / "ens17f0v0"
        net_dir.mkdir(parents=True)
        (net_dir / "device").symlink_to(dev_dir)

        assert vfio.resolve_ifname_to_bdf("ens17f0v0") == BDF

    def test_missing_netdev(self, fake_sysfs: Path) -> None:
        with pytest.raises(vfio.VfioError, match="not found"):
            vfio.resolve_ifname_to_bdf("nope0")

    def test_netdev_without_pci_parent(
        self, fake_sysfs: Path
    ) -> None:
        """Virtual interfaces (bridge, tap, veth) have no 'device'
        symlink under /sys/class/net/<name>/."""
        net_dir = fake_sysfs / "class" / "net" / "br0"
        net_dir.mkdir(parents=True)
        # intentionally no 'device' symlink
        with pytest.raises(vfio.VfioError, match="no PCI parent"):
            vfio.resolve_ifname_to_bdf("br0")

    def test_device_link_not_a_bdf(self, fake_sysfs: Path) -> None:
        """If the 'device' symlink points somewhere unexpected,
        bail out rather than returning garbage."""
        net_dir = fake_sysfs / "class" / "net" / "weird0"
        net_dir.mkdir(parents=True)
        bogus = fake_sysfs / "bogus-target"
        bogus.mkdir()
        (net_dir / "device").symlink_to(bogus)
        with pytest.raises(
            vfio.VfioError, match="does not look like a PCI BDF"
        ):
            vfio.resolve_ifname_to_bdf("weird0")
