"""Single-VM CLI commands (lifecycle, execution, diagnostics)."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .deploy import configure_test_disks
from .paths import load_meta_safe
from .qemu_run import die, is_running, kill_qemu, launch_qemu, run
from .vm_net import (
    HOSTS_FILE,
    _real_user_ssh_dir,
    alloc_ip,
    deploy_ssh_key,
    mac_for_name,
    provision_vm_ssh,
    register_ssh_name,
    run_ssh,
    sshpass_scp_argv,
    tap_for_name,
    unregister_ssh_name,
    wait_for_ssh,
)
from .vm_state import (
    DEFAULT_TARGET,
    DISK_SIZE_BYTES,
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_TIMEOUT,
    EXIT_UNREACHABLE,
    MARKER,
    OVERLAYS,
    QEMU_IMG,
    ROOT_PASSWORD,
    SOCKETS,
    SSH_TIMEOUT,
    VMInfo,
    VMNotFound,
    lustre_libdir,
    resolve_os_artifacts,
)


def _handler_error(
    args: argparse.Namespace, msg: str, code: int = EXIT_ERROR
) -> int:
    """Emit a handler error honoring ``args.json``.

    JSON mode: print ``{"error": msg}`` to stdout (so wrapper scripts
    can parse stdout).  Text mode: print ``error: msg`` to stderr.
    Returns ``code`` so the caller can ``return _handler_error(...)``
    and let the outer wrapper propagate the exit code.
    """
    use_json = bool(getattr(args, "json", False))
    if use_json:
        print(json.dumps({"error": msg}))
    else:
        print(f"error: {msg}", file=sys.stderr)
    return code


@contextmanager
def _with_vm_stopped(
    vm: VMInfo,
    reason: str,
    *,
    register_before_wait: bool = False,
    get_error: Any = None,
) -> Iterator[None]:
    """Stop ``vm`` (if running) for the duration of the block, then
    restart it.

    Used by snapshot / restore paths that need to mutate the qcow2 overlay:
    qemu-img refuses to operate on an image open by another qemu process,
    and mutating overlay metadata under a running guest risks corruption.

    The restart always runs (via ``finally``) so a failure inside the
    block still brings the VM back up.  If the restart itself raises
    SystemExit (via die() in one of the restart steps), and ``get_error``
    is a callable returning a non-empty error string, that inner error
    is printed to stderr before the SystemExit propagates -- preserving
    the "both errors visible" semantics the original snapshot and
    snapshot-delete paths had.
    """
    was_running = is_running(vm)
    if was_running:
        print(f"stopping {vm.name} {reason}...")
        kill_qemu(vm)
    try:
        yield
    finally:
        if was_running:
            print(f"restarting {vm.name}...")
            try:
                launch_qemu(vm)
                if register_before_wait:
                    provision_vm_ssh(
                        vm, SSH_TIMEOUT, register_before_wait=True
                    )
                else:
                    provision_vm_ssh(vm, SSH_TIMEOUT)
                _seed_kdump_boot(vm)
                print(f"started {vm.name}")
            except SystemExit as e:
                if get_error is not None:
                    inner = get_error()
                    if inner:
                        print(f"error: {inner}", file=sys.stderr)
                raise e


def _os_family_for_vm(vm: VMInfo, context: str = "") -> str:
    """Resolve the OS family (e.g. 'rhel', 'debian') from ``vm.os_id``.

    Falls back to 'rhel' with a warning only when the target config is
    genuinely missing/broken (ValueError: unknown target / bad schema,
    FileNotFoundError: targets.yaml gone).  ``context`` is a short
    phrase appended to the warning message (e.g. 'kdump path').
    """
    if not vm.os_id:
        return "rhel"
    try:
        from .target_config import TargetConfig

        return TargetConfig(vm.os_id).os_family
    except (ValueError, FileNotFoundError) as e:
        suffix = f" {context}" if context else ""
        print(
            f"warning: cannot resolve target {vm.os_id!r}, "
            f"defaulting to rhel{suffix}: {e}",
            file=sys.stderr,
        )
        return "rhel"


def _seed_kdump_boot(vm: VMInfo) -> None:
    """Ensure /boot has vmlinuz + kdump initramfs for kexec-on-panic.

    QEMU passes the kernel externally via -kernel, so the VM image has
    no /boot/vmlinuz or initramfs unless image_build baked them in.
    Fresh images ship with both pre-built, so this is just a probe +
    kdump service reload.
    """
    if not vm.kernel:
        return

    if not vm.kver:
        raise RuntimeError(
            f"VM {vm.name!r} has no kver recorded; recreate the VM"
        )
    kver = vm.kver

    # Determine OS family from target config.  A blanket
    # `except Exception: pass` would silently fall back to "rhel" for
    # a Debian VM, so _os_family_for_vm() narrows to the missing/broken
    # target cases.
    os_family = _os_family_for_vm(vm, "kdump path")

    if os_family == "debian":
        initrd_path = f"/var/lib/kdump/initrd.img-{kver}"
        reload_cmd = (
            "kdump-config load 2>&1; systemctl restart kdump-tools 2>&1"
        )
    else:
        initrd_path = f"/boot/initramfs-{kver}.img"
        reload_cmd = "systemctl restart kdump 2>&1"

    probe = run_ssh(
        vm.ip,
        f"test -f /boot/vmlinuz-{kver} && test -f {initrd_path}",
        timeout=10,
    )
    if probe.returncode != 0:
        # Image predates baked-in kdump artifacts (or they got wiped).
        # Fall back to seeding from the host's kernel dir: scp vmlinuz
        # to /boot/, then regenerate initramfs inside the VM so it has
        # matching module dependencies for the kernel actually running.
        kernel_dir = Path(vm.kernel).parent if vm.kernel else None
        vmlinuz = kernel_dir / "vmlinuz" if kernel_dir else None
        if not vmlinuz or not vmlinuz.exists():
            die(
                f"no vmlinuz next to {vm.kernel!r} to seed to VM; "
                f"rebuild the image with `ltvm build image` "
                f"or restore the kernel output dir."
            )
        scp_r = run(
            sshpass_scp_argv(
                str(vmlinuz),
                f"root@{vm.ip}:/boot/vmlinuz-{kver}",
                extra_opts=["-o", "ConnectTimeout=5"],
            ),
        )
        if scp_r.returncode != 0:
            err = (scp_r.stderr or scp_r.stdout or "").strip()
            die(
                f"failed to seed vmlinuz to '{vm.name}' "
                f"(rc={scp_r.returncode}): {err}"
            )
        if os_family == "debian":
            regen_cmd = f"update-initramfs -c -k {kver}"
        else:
            regen_cmd = f"dracut --kver {kver} --force {initrd_path}"
        run_ssh(vm.ip, regen_cmd, timeout=120)
    run_ssh(vm.ip, reload_cmd, timeout=30)


def _ago(epoch: int) -> str:
    """Format epoch as relative time (e.g. '2h ago', '3d ago'), or '-'."""
    if not epoch:
        return "-"
    delta = int(time.time()) - epoch
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


_SIZE_SUFFIXES = {"M": 1 << 20, "G": 1 << 30}
_MIN_DISK_BYTES = 64 * (1 << 20)  # 64 MiB
_MAX_DISK_BYTES = 100 * (1 << 30)  # 100 GiB


def _parse_disk_size(value: str | int | None) -> int:
    """Parse a disk size to bytes.

    Accepts:
      * None or "" -> default
      * str like "500M", "10G" (suffix required, M or G)
      * int bytes -- validated against the same floor/ceiling as the
        string path.  A below-floor or above-ceiling int raises
        SystemExit, matching the string behavior; no silent substitution
        of the default (which previously surprised callers passing a
        small-but-deliberate byte count).

    Raises SystemExit on out-of-range or invalid input.
    """
    if value is None or value == "":
        return DISK_SIZE_BYTES
    if isinstance(value, int):
        if value < _MIN_DISK_BYTES:
            die(f"disk size {value} bytes is below the minimum of 64M")
        if value > _MAX_DISK_BYTES:
            die(f"disk size {value} bytes exceeds the maximum of 100G")
        return value
    s = value.strip().upper()
    if not s:
        return DISK_SIZE_BYTES
    suffix = s[-1]
    if suffix not in _SIZE_SUFFIXES:
        die(
            f"Invalid --disk-size '{value}': suffix must be M or G (e.g. 500M, 2G)"
        )
    try:
        n = int(s[:-1])
    except ValueError:
        die(f"Invalid --disk-size '{value}': not a number before the suffix")
    if n <= 0:
        die(f"Invalid --disk-size '{value}': size must be positive")
    result = n * _SIZE_SUFFIXES[suffix]
    if result < _MIN_DISK_BYTES:
        die(f"--disk-size '{value}' is below the minimum of 64M")
    if result > _MAX_DISK_BYTES:
        die(f"--disk-size '{value}' exceeds the maximum of 100G")
    return result


# ── lifecycle ────────────────────────────────────────────


_VM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# Accepted --nic types.  `tcp` is fully wired in this feature; the
# `softroce` and `passthrough` types are recognised syntactically so
# follow-up issues (lustre_test_vms_v2-r55, -5a0) don't have to re-plumb
# the CLI parser, but they are rejected at validation time with a
# pointer at the tracking issue.  Downstream code (launch_qemu,
# rc.local, etc.) must only see `tcp` values from this list until those
# issues land.
_NIC_TYPES_IMPLEMENTED = ("tcp", "softroce", "passthrough")
_NIC_TYPES_RESERVED: dict[str, str] = {}
_NIC_TYPES_ALL = _NIC_TYPES_IMPLEMENTED + tuple(_NIC_TYPES_RESERVED)


def parse_nic_spec(raw: str) -> tuple[str, str]:
    """Parse ``--nic <type>[:<arg>]`` into ``(type, arg)``.

    Raises ``SystemExit`` (via ``die()``) for unknown types.  Does NOT
    reject the reserved types (softroce / passthrough) -- callers that
    only support implemented types must reject them themselves so the
    error can cite where the work will land (see ``validate_nic_spec``).

    Returning ``(type, arg)`` keeps the reserved arg available for
    future backends -- passthrough needs the BDF string, softroce may
    take a device hint later.  ``arg`` is the empty string when no
    ``:<arg>`` was given.
    """
    if not raw:
        die("--nic value is empty")
    parts = raw.split(":", 1)
    nic_type = parts[0].strip().lower()
    nic_arg = parts[1] if len(parts) > 1 else ""
    if nic_type not in _NIC_TYPES_ALL:
        valid = ", ".join(_NIC_TYPES_ALL)
        die(
            f"unknown --nic type {nic_type!r}: valid types are: {valid}"
        )
    return nic_type, nic_arg


_BDF_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$")


def validate_nic_spec(raw: str) -> str:
    """Validate a ``--nic`` spec and return the normalised type string.

    Reserved types raise ``SystemExit`` with a pointer at the tracking
    issue.  Implemented types round-trip through the canonical form
    (lowercased, arg preserved for types that need it).  The returned
    string is what gets stored in ``VMInfo.nics`` and threaded onto the
    kernel cmdline as ``fc_nics=<csv>``.

    ``passthrough`` requires a PCIe BDF arg (``domain:bus:device.function``,
    e.g. ``0000:85:00.1``).  We syntax-check the BDF here; whether the
    device actually exists / is bindable is checked at create time in
    ``cmd_create`` so errors surface before VM state is written.
    """
    nic_type, nic_arg = parse_nic_spec(raw)
    if nic_type in _NIC_TYPES_RESERVED:
        issue = _NIC_TYPES_RESERVED[nic_type]
        die(
            f"--nic {nic_type!r} is not supported yet; that backend "
            f"lands in {issue}. For this issue use --nic tcp."
        )
    if nic_type == "passthrough":
        if not nic_arg:
            die(
                "--nic passthrough requires a PCIe BDF arg, "
                "e.g. --nic passthrough:0000:85:00.1"
            )
        if not _BDF_RE.match(nic_arg):
            die(
                f"--nic passthrough arg {nic_arg!r} is not a valid "
                f"PCIe BDF (expected domain:bus:device.function like "
                f"0000:85:00.1)"
            )
    # Implemented types: fold back to canonical storage form.
    if nic_arg:
        return f"{nic_type}:{nic_arg}"
    return nic_type


def _validate_vm_name(name: str) -> None:
    """Reject VM names with characters that break /etc/hosts, SSH
    config, hostnames, shell quoting, or QEMU's fc_name= cmdline arg.

    Allowed: ASCII alnum, dot, underscore, hyphen.  Must start with
    alnum (no leading hyphen so it doesn't look like a CLI flag).
    """
    if not name:
        die("VM name is empty")
    if len(name) > 63:
        # /etc/hosts hostname limit + DNS label limit
        die(f"VM name too long ({len(name)} > 63): {name!r}")
    if not _VM_NAME_RE.match(name):
        die(
            f"invalid VM name {name!r}: only ASCII letters, digits, "
            f"'.', '_', '-' are allowed (must start with alnum)"
        )


def _handle_existing_vm(name: str, args: argparse.Namespace) -> bool:
    """If a VMInfo already exists for *name*, handle the idempotent
    restart/no-op paths and return True.  Returns False when no
    existing .info file is found (caller should proceed to create).
    """
    info_path = SOCKETS / f"{name}.info"
    if not info_path.exists():
        return False
    vm = VMInfo.load(name)
    if is_running(vm):
        wait_for_ssh(vm.ip, SSH_TIMEOUT)
        register_ssh_name(vm.name, vm.ip)
        if args.json:
            print(
                json.dumps(
                    {
                        "action": "none",
                        "name": name,
                        "status": "already running",
                    }
                )
            )
        else:
            print(f"{name}: already running")
        return True
    launch_qemu(vm)
    provision_vm_ssh(vm, SSH_TIMEOUT)
    _seed_kdump_boot(vm)
    if args.json:
        print(
            json.dumps(
                {
                    "action": "started",
                    "name": name,
                    "status": "running",
                }
            )
        )
    else:
        print(f"{name}: started")
    return True


def _create_disks(vm: VMInfo, image: str) -> None:
    """Create overlay + backing disks for *vm*.  On any failure,
    unlink everything already created (best-effort) and re-raise so
    we don't leave orphan files for the next `ltvm create <same name>`
    to trip on.  The .info file isn't written yet so cmd_doctor can't
    see these orphans either, which makes manual recovery awkward.
    """
    try:
        run(
            [
                QEMU_IMG,
                "create",
                "-f",
                "qcow2",
                "-b",
                image,
                "-F",
                "raw",
                str(vm.overlay_path),
            ],
            capture_output=True,
            check=True,
        )

        # Grow the qcow2 virtual disk so the VM has room for Lustre
        # modules, logs, etc.  The ext4 filesystem is resized on first
        # boot (rc.local).
        run(
            [QEMU_IMG, "resize", str(vm.overlay_path), "8G"],
            capture_output=True,
            check=True,
        )

        # Create backing disks
        total = vm.mdt_disks + vm.ost_disks
        for n in range(1, total + 1):
            run(
                ["truncate", "-s", str(vm.disk_size), str(vm.disk_path(n))],
                check=True,
            )
    except BaseException:
        try:
            vm.overlay_path.unlink(missing_ok=True)
        except OSError:
            pass
        for n in range(1, vm.mdt_disks + vm.ost_disks + 1):
            try:
                vm.disk_path(n).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _chown_disks_to_sudo_user(vm: VMInfo) -> None:
    """When invoked via sudo, hand ownership of the disk images to the
    real user so snapshot/restore (which run qemu-img) work without root.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        import pwd as _pwd
        try:
            pw = _pwd.getpwnam(sudo_user)
            files_to_chown = [vm.overlay_path]
            for n in range(1, vm.mdt_disks + vm.ost_disks + 1):
                files_to_chown.append(vm.disk_path(n))
            for f in files_to_chown:
                try:
                    os.chown(f, pw.pw_uid, pw.pw_gid)
                except OSError:
                    pass
        except KeyError:
            pass


def _launch_and_wait(vm: VMInfo, passthrough_bdfs: list[str]) -> None:
    """Bind any passthrough devices to vfio-pci, launch QEMU, wait
    for SSH and seed the kdump boot.  The caller wraps this in an
    except/_rollback_launch_failure block so a launch failure restores
    host networking state.
    """
    # Bind any passthrough devices to vfio-pci and record the from-
    # driver for destroy-time rebind.  Inside the rollback umbrella
    # so a launch failure restores host networking.
    if passthrough_bdfs:
        from ltvm_pkg import vfio as _vfio
        for bdf in passthrough_bdfs:
            try:
                from_drv = _vfio.bind_to_vfio(bdf)
            except _vfio.VfioError as e:
                die(f"passthrough {bdf}: {e}")
            vm.passthrough_drivers[bdf] = from_drv or ""
        vm.save()
    launch_qemu(vm)
    provision_vm_ssh(vm, SSH_TIMEOUT)
    _seed_kdump_boot(vm)


def _rollback_launch_failure(vm: VMInfo) -> None:
    """Unwind the post-vm.save() phase on failure: preserve QEMU log,
    kill QEMU, destroy artifacts, unregister SSH name, rebind any
    vfio'd passthrough devices.  Each step is best-effort and logs
    (to stderr) rather than raising -- the original exception is what
    should surface to the caller.
    """
    # Best-effort cleanup of overlay/disks/.info/TAP.  If any
    # individual cleanup step fails, swallow it -- the original
    # exception is what we want to surface to the user.
    # Preserve the QEMU log out-of-band first; _destroy_vm_artifacts
    # erases it and the stderr message points users at a path that
    # no longer exists.
    if vm.log_path.exists():
        try:
            preserved = vm.log_path.with_suffix(".log.failed")
            vm.log_path.rename(preserved)
            print(
                f"  QEMU log preserved at {preserved}",
                file=sys.stderr,
            )
        except OSError as e:
            print(
                f"  rollback: preserving QEMU log failed: {e}",
                file=sys.stderr,
            )
    # Log (don't swallow silently) so a later investigator can see
    # which cleanup step failed; continue through the remaining
    # steps regardless.
    try:
        kill_qemu(vm)
    except Exception as e:
        print(f"  rollback: kill_qemu failed: {e}", file=sys.stderr)
    try:
        _destroy_vm_artifacts(vm.name)
    except Exception as e:
        print(
            f"  rollback: _destroy_vm_artifacts failed: {e}",
            file=sys.stderr,
        )
    try:
        unregister_ssh_name(vm.name)
    except Exception as e:
        print(
            f"  rollback: unregister_ssh_name failed: {e}",
            file=sys.stderr,
        )
    # Rebind any vfio'd devices back to their original drivers so
    # the host's network state is unchanged from a failed create.
    if vm.passthrough_drivers:
        from ltvm_pkg import vfio as _vfio
        for bdf, drv in vm.passthrough_drivers.items():
            if not drv:
                continue
            try:
                _vfio.rebind(bdf, drv)
            except Exception as e:
                print(
                    f"  rollback: rebind {bdf} -> {drv} failed: {e}",
                    file=sys.stderr,
                )


def cmd_create(args: argparse.Namespace) -> None:
    name = args.name
    _validate_vm_name(name)

    info_path = SOCKETS / f"{name}.info"
    if _handle_existing_vm(name, args):
        return

    # Validate vCPU and memory bounds.  argparse accepts any int (and
    # 0/negative values would crash QEMU AFTER vm.save() committed
    # the .info file -- leaving a half-created VM that requires
    # manual destroy).
    if args.vcpus is not None and args.vcpus <= 0:
        die(f"--vcpus must be > 0 (got {args.vcpus})")
    if args.mem is not None and args.mem <= 0:
        die(f"--mem must be > 0 (got {args.mem})")

    # Disk count bounds: vda is the rootfs, leaving vdb..vdz for
    # MDT+OST.  Past 25 letters we'd write /dev/vd{ etc. into the
    # test framework's cfg/local.sh.
    total_data_disks = (args.mdt_disks or 0) + (args.ost_disks or 0)
    if total_data_disks > 25:
        die(
            f"too many data disks ({total_data_disks}) -- "
            f"max 25 (vdb..vdz); got mdt={args.mdt_disks} "
            f"ost={args.ost_disks}"
        )

    tap = tap_for_name(name)
    mac = mac_for_name(name)

    # Parse + validate any extra NICs.  The mgmt NIC (eth0) is always
    # created; --nic adds NICs on top of it.  Invalid specs die() now
    # so we don't half-create the VM.  validate_nic_spec rejects
    # softroce/passthrough with a pointer at the follow-up issues.
    raw_nics = getattr(args, "nic", None) or []
    extra_nic_types = [validate_nic_spec(n) for n in raw_nics]

    # Passthrough NICs: pre-flight checks (no side effects) so a bad
    # spec or a missing IOMMU fails before we touch VM state on disk.
    # The actual vfio bind happens later in the launch block, inside
    # the rollback umbrella so a failed create rebinds the device.
    passthrough_bdfs = [
        spec.split(":", 1)[1] for spec in extra_nic_types
        if spec.startswith("passthrough:")
    ]
    if passthrough_bdfs:
        from ltvm_pkg import vfio as _vfio
        if not _vfio.iommu_enabled():
            die(
                "passthrough requires host IOMMU; boot with "
                "intel_iommu=on (or amd_iommu=on) on the host kernel cmdline "
                "and ensure /sys/kernel/iommu_groups/ is non-empty."
            )
        for bdf in passthrough_bdfs:
            if not (Path("/sys/bus/pci/devices") / bdf).is_dir():
                die(f"passthrough device {bdf!r} not found in /sys/bus/pci/devices")

    os_target = getattr(args, "os", "")
    explicit_image = getattr(args, "image", "")
    explicit_kernel = getattr(args, "kernel", "")
    arch = getattr(args, "arch", None) or "x86_64"
    variant = getattr(args, "variant", None) or "base"
    defaulted_target = not os_target
    if defaulted_target:
        os_target = DEFAULT_TARGET
    # Pass explicit_kernel (a name, not a path) through to resolve_os_artifacts
    # which will find the right kernel dir and pair the correct image with it.
    os_arts = resolve_os_artifacts(
        os_target, arch=arch, kernel=explicit_kernel or None, variant=variant
    )
    image = explicit_image or str(os_arts.image)
    kernel = str(os_arts.kernel)
    # If the user didn't pass --mem, fall back to the target's default
    # (rocky10 needs 4096; others use 2048).  argparse default is None
    # so we can distinguish "user said 2048" from "user said nothing".
    if args.mem is None:
        args.mem = os_arts.default_mem
    if defaulted_target and not explicit_image and not explicit_kernel:
        kver_short = os_arts.kernel.parent.name
        disk_desc = (
            f"mdt={args.mdt_disks} ost={args.ost_disks}"
            if (args.mdt_disks or args.ost_disks)
            else "no data disks"
        )
        image_meta = load_meta_safe(Path(os_arts.image).parent / "meta.json")
        lver = (image_meta or {}).get("lustre_version")
        lustre_desc = f" lustre={lver}" if lver else " (no lustre baked in)"
        print(
            f"using default target: {os_target} "
            f"(kernel: {kver_short};{lustre_desc}; "
            f"vcpus={args.vcpus} mem={args.mem}MB {disk_desc})"
        )

    base_name = Path(image).name
    os_id = os_target

    # Read kernel version from meta.json next to the kernel binary
    kernel_meta = Path(kernel).parent / "meta.json"
    meta = load_meta_safe(kernel_meta)
    if meta is None or not meta.get("kernel_version"):
        raise RuntimeError(
            f"kernel meta.json missing kernel_version: {kernel_meta}"
        )
    kver = meta["kernel_version"]

    disk_size = _parse_disk_size(getattr(args, "disk_size", None))

    # Allocate an IP under a file lock so that concurrent creates cannot
    # race and claim the same address.  The lock is held until vm.save()
    # commits the .info file, at which point the IP is visible to peers.
    with alloc_ip(name, explicit_ip=getattr(args, "ip", None) or None) as ip:
        # Re-check existence under the alloc_ip lock: the earlier check
        # at the top of the function is unsynchronized so two concurrent
        # `ltvm create <same name>` could both pass it.
        if info_path.exists():
            die(f"VM '{name}' already exists")
        vm = VMInfo(
            name=name,
            ip=ip,
            tap=tap,
            mac=mac,
            vcpus=args.vcpus,
            mem=args.mem,
            mdt_disks=args.mdt_disks,
            ost_disks=args.ost_disks,
            disk_size=disk_size,
            image=image,
            kernel=kernel,
            created=int(time.time()),
            base_image=base_name,
            os_id=os_id,
            kver=kver,
            arch=os_arts.arch,
            # Track the human who ran `sudo ltvm create`.  SUDO_USER
            # is set when invoked via sudo (the normal path); fall
            # back to "root" if invoked as root directly.  Surfaced
            # in `ltvm list` so a shared host can show whose VM is
            # whose without forcing per-user namespaces.
            creator=os.environ.get("SUDO_USER", "") or "root",
            variant=variant,
            nics=list(extra_nic_types),
            # passthrough_drivers is filled in below, inside the launch
            # umbrella, after we've actually bound each BDF to vfio-pci.
            passthrough_drivers={},
        )

        _create_disks(vm, image)
        _chown_disks_to_sudo_user(vm)

        vm.save()
    # Lock released; IP is now committed.
    #
    # If anything from launch_qemu through _seed_kdump_boot raises or
    # die()s, we want to leave the user with a clean slate (no
    # half-created VM, no leaked overlay/.info/IP/TAP).  Wrap the
    # remaining steps and unwind via _destroy_vm_artifacts on failure.
    #
    # NOTE: deploy_ssh_key and _seed_kdump_boot run on a live VM, so
    # cmd_start can recover them later -- but if they die() we'd leave
    # the VM running with no obvious indication.  Catch SystemExit too
    # so we can re-raise after cleanup.
    try:
        _launch_and_wait(vm, passthrough_bdfs)
    except BaseException:
        _rollback_launch_failure(vm)
        raise

    if args.json:
        print(
            json.dumps(
                {
                    "action": "created",
                    "name": vm.name,
                    "status": "running",
                }
            )
        )
    elif not getattr(args, "_quiet", False):
        print(
            f"VM created: {vm.name}\n"
            f"  ip:    {vm.ip}\n"
            f"  pid:   {vm.pid}\n"
            f"  disks: {vm.mdt_disks} MDT + {vm.ost_disks} OST"
        )


def cmd_start(args: argparse.Namespace) -> None:
    for name in args.names:
        vm = VMInfo.load(name)
        launch_qemu(vm)
        # register_before_wait: populate /etc/hosts BEFORE waiting for
        # SSH so a wait_for_ssh timeout doesn't leave a zombie VM
        # running with no DNS entry. deploy_ssh_key is idempotent and
        # cheap on re-start, but necessary for fresh VMs whose create
        # was interrupted (so cmd_start recovers a half-set-up VM).
        provision_vm_ssh(vm, SSH_TIMEOUT, register_before_wait=True)
        _seed_kdump_boot(vm)
        print(f"started {name}")


def cmd_stop(args: argparse.Namespace) -> None:
    for name in args.names:
        try:
            vm = VMInfo.load(name)
        except VMNotFound:
            # Match cmd_destroy: stopping a VM that doesn't exist is
            # a no-op, not a traceback.
            print(f"stop: {name} not found")
            continue
        kill_qemu(vm)
        print(f"stopped {name}")


def _destroy_vm_artifacts(name: str) -> None:
    """Remove on-disk artifacts (overlay, disks, sockets) for a VM.

    Caller is responsible for killing QEMU and unregistering DNS;
    this only handles the filesystem cleanup so it can be reused
    by both cmd_destroy and the cmd_create rollback path.
    """
    overlay = OVERLAYS / f"{name}.qcow2"
    for f in [overlay] + list(OVERLAYS.glob(f"{name}-disk*.img")):
        f.unlink(missing_ok=True)
    for ext in ("qmp", "pid", "info", "log"):
        (SOCKETS / f"{name}.{ext}").unlink(missing_ok=True)
    # Per-VM info lock file (created by VMInfo._info_lock).
    (SOCKETS / f".{name}.info.lock").unlink(missing_ok=True)


def cmd_destroy(args: argparse.Namespace) -> None:
    for name in args.names:
        existed = (SOCKETS / f"{name}.info").exists()
        passthrough_rebinds: dict[str, str] = {}
        try:
            vm = VMInfo.load(name)
            # Capture before kill so we still know what to rebind even
            # if the QMP socket is gone or kill_qemu raises partway.
            passthrough_rebinds = dict(vm.passthrough_drivers)
            kill_qemu(vm)
        except VMNotFound:
            pass

        # Rebind any passthrough devices to their original drivers
        # BEFORE clearing .info so a mid-destroy interruption can still
        # be diagnosed (.info holds the BDF -> from-driver map).  Run
        # in finally so a kill_qemu exception above doesn't strand a
        # vfio-bound device.
        if passthrough_rebinds:
            from ltvm_pkg import vfio as _vfio
            for bdf, drv in passthrough_rebinds.items():
                if not drv:
                    continue
                try:
                    _vfio.rebind(bdf, drv)
                except Exception as e:
                    print(
                        f"destroy {name}: rebind {bdf} -> {drv} failed: {e}",
                        file=sys.stderr,
                    )

        # Wrap artifact teardown in try/finally so a SIGINT (Ctrl-C)
        # between artifact cleanup and DNS cleanup still removes the
        # /etc/hosts entry -- otherwise the next `ltvm create <name>`
        # sees a stale host mapping.
        try:
            _destroy_vm_artifacts(name)
        finally:
            unregister_ssh_name(name)
        # Match cmd_stop's "{name} not found" wording so a typo like
        # `ltvm destroy co1-signle` doesn't silently claim success.
        if existed:
            print(f"destroyed {name}")
        else:
            print(f"destroy: {name} not found")


def cmd_llmount(args: argparse.Namespace) -> None:
    # cli.py parser positional is 'vm'; older call sites used 'name'
    name = getattr(args, "vm", None) or args.name
    timeout = getattr(args, "timeout", 300)
    cleanup = getattr(args, "cleanup", False)

    try:
        vm = VMInfo.load(name)
    except VMNotFound:
        print(f"error: VM '{name}' not found", file=sys.stderr)
        sys.exit(EXIT_NOT_FOUND)

    if not is_running(vm):
        print(f"error: VM '{name}' not running", file=sys.stderr)
        sys.exit(EXIT_UNREACHABLE)

    # Debian targets put Lustre under /usr/lib, RHEL under /usr/lib64.
    # Resolve from the VM's recorded os_id; fall back to rhel with a
    # warning if the target config is missing/broken.
    os_family = _os_family_for_vm(vm, "libdir")
    libdir = lustre_libdir(os_family)

    if cleanup:
        command = (
            f"cd {libdir}/tests && LUSTRE={libdir} bash llmountcleanup.sh"
            " && lustre_rmmod"
        )
    else:
        # Image-baked Lustre doesn't know the VM's virtio-disk topology,
        # so point MDSDEV*/OSTDEV* at the real block devices before the
        # test harness falls back to loopback files in /tmp.
        try:
            configure_test_disks(
                vm.ip,
                vm.mdt_disks,
                vm.ost_disks,
                disk_size_bytes=vm.disk_size,
                os_family=os_family,
            )
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        command = (
            "dmsetup remove_all;"
            f" cd {libdir}/tests && LUSTRE={libdir} bash llmount.sh"
        )

    try:
        r = run_ssh(vm.ip, command, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"error: timeout after {timeout}s", file=sys.stderr)
        sys.exit(EXIT_TIMEOUT)

    stdout = r.stdout or ""
    stderr = r.stderr or ""
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    sys.exit(r.returncode)


# ── info + observability ─────────────────────────────────


def cmd_list(args: argparse.Namespace) -> None:
    total_vcpus = 0
    total_mem = 0
    running_count = 0
    stopped_count = 0
    entries: list[dict[str, Any]] = []

    for name in VMInfo.all_names():
        try:
            vm = VMInfo.load(name)
        except VMNotFound:
            # Race: .info file disappeared between all_names() and load().
            # Skip this entry rather than crashing.
            continue
        status = "running" if is_running(vm) else "stopped"

        if status == "running":
            running_count += 1
            total_vcpus += vm.vcpus
            total_mem += vm.mem
        else:
            stopped_count += 1

        disk_mb = "-"
        if vm.overlay_path.exists():
            disk_mb = f"{vm.overlay_path.stat().st_size // 1048576}M"

        entries.append(
            {
                "name": vm.name,
                "ip": vm.ip,
                "status": status,
                "pid": vm.pid,
                "vcpus": vm.vcpus,
                "mem": vm.mem,
                "mdt_disks": vm.mdt_disks,
                "ost_disks": vm.ost_disks,
                "disk": disk_mb,
                "created": vm.created,
                "last_boot": vm.last_boot,
                "last_deploy": vm.last_deploy,
                "build_path": vm.build_path,
                "kver": vm.kver,
                "os_id": vm.os_id,
                "creator": vm.creator,
            }
        )

    host_cpus = os.cpu_count() or 1
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                host_mem_mb = int(line.split()[1]) // 1024
                break
        else:
            host_mem_mb = 0

    if args.json:
        print(
            json.dumps(
                {
                    "vms": entries,
                    "totals": {
                        "running": running_count,
                        "stopped": stopped_count,
                        "vcpus_used": total_vcpus,
                        "vcpus_available": host_cpus,
                        "mem_used_mb": total_mem,
                        "mem_available_mb": host_mem_mb,
                    },
                }
            )
        )
    else:
        if not entries:
            print("(no VMs)")
            return
        for e in entries:
            disks = ""
            if e["mdt_disks"] + e["ost_disks"] > 0:
                disks = f"mdt={e['mdt_disks']} ost={e['ost_disks']}"
            deploy = _ago(e["last_deploy"]) if e["last_deploy"] else "-"
            boot = _ago(e["last_boot"]) if e["last_boot"] else "-"
            os_id = e.get("os_id") or "-"
            # `by=` shows who created the VM (SUDO_USER at create time);
            # legacy VMs from before this field existed show `by=-`.
            creator = e.get("creator") or "-"
            print(
                f"{e['name']:<20} {e['ip']:<18} {e['status']:<8} "
                f"{os_id:<8} {disks:<14} "
                f"boot={boot:<10} deploy={deploy:<10} by={creator}"
            )
        print("---")
        print(
            f"{running_count} running, {stopped_count} stopped | "
            f"vcpus: {total_vcpus}/{host_cpus} | "
            f"mem: {total_mem}M/{host_mem_mb}M"
        )


def cmd_console_log(args: argparse.Namespace) -> None:
    vm = VMInfo.load(args.name)
    if not vm.log_path.exists():
        die(f"no log for VM '{args.name}'")
    lines = vm.log_path.read_text().splitlines()
    for line in lines[-args.lines :]:
        print(line)


# ── nmi ──────────────────────────────────────────────────


def _qmp_nmi(qmp_path: Path) -> None:
    """Send inject-nmi via QMP Unix socket."""
    import socket as _socket

    def _recv_json_line(sock: _socket.socket, buf: bytearray) -> dict[str, Any]:
        """Read until a newline-terminated JSON object is available.

        QMP frames responses as newline-delimited JSON.  A single recv()
        is unsafe: the response may be split across reads, multiple
        responses (or async events) may arrive in one read, or the read
        may include only a partial frame.  Loop until we have a full
        line, return the first JSON object, and leave any extra bytes
        in `buf` for the next call.
        """
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("QMP socket closed mid-response")
            buf.extend(chunk)
        line, _, rest = bytes(buf).partition(b"\n")
        buf.clear()
        buf.extend(rest)
        return json.loads(line)  # type: ignore[no-any-return]

    with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
        s.settimeout(5)  # apply to connect() too, not just recv()
        s.connect(str(qmp_path))
        # Drain the greeting (bounded to avoid unbounded growth from
        # a misbehaving QEMU that never sends "QMP").
        data = bytearray()
        while b"QMP" not in data or b"\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                raise RuntimeError("QMP socket closed before greeting")
            data.extend(chunk)
            if len(data) > 65536:
                raise RuntimeError("QMP greeting exceeded 64K, giving up")
        # Drop the greeting line; keep any trailing bytes for the next
        # frame so we don't lose data spilled past the newline.
        _, _, rest = bytes(data).partition(b"\n")
        buf = bytearray(rest)
        # Negotiate capabilities and read the response (one full line).
        s.sendall(json.dumps({"execute": "qmp_capabilities"}).encode() + b"\n")
        _recv_json_line(s, buf)
        # Inject NMI
        s.sendall(json.dumps({"execute": "inject-nmi"}).encode() + b"\n")
        # Skip async events (e.g. NMI, RESET) until we get the response
        # frame for our request.  QMP responses always carry "return" or
        # "error"; events carry "event" instead.
        while True:
            result = _recv_json_line(s, buf)
            if "return" in result or "error" in result:
                break
        if "error" in result:
            raise RuntimeError(
                result["error"].get("desc", str(result["error"]))
            )


def cmd_nmi(args: argparse.Namespace) -> int:
    vm = VMInfo.load(args.name)
    if not is_running(vm):
        return _handler_error(
            args, f"VM '{args.name}' not running", EXIT_UNREACHABLE
        )
    # Ensure the injected NMI causes a panic.  QEMU microVM delivers
    # inject-nmi as an ISA SERR NMI (PCI system error, reason 0xff), handled
    # by mem_parity_error() which only panics when panic_on_unrecovered_nmi=1.
    # Set all three NMI-panic knobs to cover every kernel version.
    try:
        run_ssh(
            vm.ip,
            "sysctl -w kernel.panic_on_unrecovered_nmi=1 "
            "kernel.panic_on_io_nmi=1 kernel.unknown_nmi_panic=1",
            timeout=10,
        )
    except Exception as e:
        return _handler_error(
            args, f"failed to set NMI panic sysctls on '{args.name}': {e}"
        )
    try:
        _qmp_nmi(vm.socket_path)
    except Exception as e:
        return _handler_error(
            args, f"failed to inject NMI into '{args.name}': {e}"
        )
    print(f"NMI injected into '{args.name}' (expect panic + kdump reboot)")
    return EXIT_OK


# ── crash-collect ────────────────────────────────────────


def cmd_crash_collect(args: argparse.Namespace) -> int:
    vm = VMInfo.load(args.name)
    raw_outdir = getattr(args, "outdir", None)
    outdir = Path(raw_outdir) if raw_outdir else Path.home() / "ltvm-crashes"
    outdir.mkdir(parents=True, exist_ok=True)

    if args.trigger:
        if not is_running(vm):
            return _handler_error(
                args, f"VM '{args.name}' not running, can't trigger crash"
            )
        print(f"triggering crash on {args.name}...")
        # The trigger's expected success case is ssh dying mid-command
        # as the kernel panics -- TimeoutExpired is the happy path.
        # A clean ssh return with rc != 0 means the SSH connection
        # worked but the sysrq write failed (auth, permissions,
        # command not found), and we'd hang forever waiting for a
        # kdump that never happens -- fail loud.  A clean rc == 0 is
        # unusual (kernel usually dies before ssh returns) but still
        # valid; fall through to the wait-for-reboot loop.
        try:
            r = run_ssh(vm.ip, "echo c > /proc/sysrq-trigger", timeout=5)
        except subprocess.TimeoutExpired:
            pass  # panic in flight; proceed to wait-for-reboot
        else:
            if r.returncode != 0:
                return _handler_error(
                    args,
                    f"failed to trigger crash on {args.name} "
                    f"(rc={r.returncode}): "
                    f"{(r.stderr or '').strip() or '(no stderr)'}",
                )

        print("waiting for kdump + reboot...", end="", flush=True)
        time.sleep(5)
        for i in range(args.wait):
            try:
                r = run_ssh(vm.ip, "true", timeout=3)
                if r.returncode == 0:
                    print(f" up after {i + 5}s")
                    break
            except subprocess.TimeoutExpired:
                pass
            print(".", end="", flush=True)
            time.sleep(1)
        else:
            return _handler_error(
                args,
                f"\nVM '{args.name}' did not come back after "
                f"{args.wait + 5}s",
                EXIT_TIMEOUT,
            )
    elif not is_running(vm):
        return _handler_error(args, f"VM '{args.name}' not running")

    print("finding vmcore...")
    # Support both RHEL format (/var/crash/*/vmcore) and
    # Ubuntu kdump-tools format (/var/crash/<ts>/dump.<ts>)
    try:
        r = run_ssh(
            vm.ip,
            "find /var/crash -maxdepth 3 -type f"
            r" \( -name 'vmcore' -o -name 'dump.*' \)"
            " 2>/dev/null | xargs ls -dt 2>/dev/null | head -1",
            timeout=10,
        )
    except subprocess.TimeoutExpired as e:
        return _handler_error(
            args,
            f"timed out after {e.timeout}s probing /var/crash on "
            f"'{args.name}'",
            EXIT_TIMEOUT,
        )
    if r.returncode != 0:
        # SSH failure (host unreachable, key mismatch, etc.) -- surface
        # the real error rather than the misleading "no vmcore found".
        return _handler_error(
            args,
            f"failed to probe /var/crash on '{args.name}' "
            f"(rc={r.returncode}): {(r.stderr or r.stdout or '').strip()}",
        )
    vmcore_path = r.stdout.strip()
    if not vmcore_path:
        return _handler_error(args, "no vmcore found in /var/crash/")

    r = run_ssh(vm.ip, f"ls -lh {vmcore_path}", timeout=5)
    print(f"found: {r.stdout.strip()}")

    ts = time.strftime("%Y%m%d-%H%M%S")
    local_dir = outdir / f"crash-{args.name}-{ts}"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_vmcore = local_dir / "vmcore"

    print(f"copying vmcore to {local_vmcore}...")
    try:
        r = run(
            sshpass_scp_argv(
                f"root@{vm.ip}:{vmcore_path}",
                str(local_vmcore),
            ),
            capture_output=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired as e:
        local_vmcore.unlink(missing_ok=True)
        return _handler_error(
            args,
            f"timed out after {e.timeout}s copying vmcore from "
            f"'{args.name}'",
            EXIT_TIMEOUT,
        )
    if r.returncode != 0:
        local_vmcore.unlink(missing_ok=True)
        return _handler_error(args, "failed to copy vmcore")

    # Make vmcore readable by non-root so triage tools (run as SUDO_USER)
    # can open it.
    local_vmcore.chmod(0o644)

    print(f"vmcore: {local_vmcore}")

    # Resolve vmlinux: prefer the freshly-built vmlinux from output/, then
    # look next to vm.kernel (which usually points to vmlinuz).  We always
    # prefer vmlinux over vmlinuz because drgn needs full debug symbols.
    if not vm.os_id:
        raise RuntimeError(
            f"VM '{vm.name}' has no os_id; recreate it"
        )
    vmlinux: Path | None = None
    try:
        arts = resolve_os_artifacts(vm.os_id, arch=vm.arch)
        candidate = arts.kernel.parent / "vmlinux"
        if candidate.exists():
            vmlinux = candidate
    except FileNotFoundError as e:
        print(
            f"warning: kernel artifacts not found for os_id={vm.os_id}: {e}",
            file=sys.stderr,
        )
    if vmlinux is None and vm.kernel:
        neighbor = Path(vm.kernel).parent / "vmlinux"
        if neighbor.exists():
            vmlinux = neighbor

    if vmlinux is None:
        # Without vmlinux the dump is unusable for symbol-aware triage,
        # so this should not be a silent success.  Print where the dump
        # lives so the user can run triage manually after fetching the
        # right vmlinux, then exit non-zero.
        print(
            "warning: no vmlinux found; triage skipped",
            file=sys.stderr,
        )
        print(f"vmcore dir: {local_dir}")
        return EXIT_NOT_FOUND

    if args.mod_dir:
        print("running lustre triage...")
        sudo_user = os.environ.get("SUDO_USER")
        # Search candidates in order: env var, $HOME, SUDO_USER's home.
        candidates: list[Path] = []
        env_script = os.environ.get("LTVM_TRIAGE_SCRIPT")
        if env_script:
            candidates.append(Path(env_script))
        homes = [Path.home()]
        if sudo_user:
            import pwd

            try:
                homes.append(Path(pwd.getpwnam(sudo_user).pw_dir))
            except KeyError:
                pass
        for home in homes:
            candidates.append(
                home
                / "llm_code_and_review_tools/lustre-drgn-tools/lustre_triage.py"
            )
        triage_script = next((c for c in candidates if c.exists()), None)
        if not triage_script:
            print(
                "triage script not found (set LTVM_TRIAGE_SCRIPT to lustre_triage.py path)"
            )
            print(f"vmcore dir: {local_dir}")
            return EXIT_OK
        # Run triage as SUDO_USER so user-installed packages (drgn) are
        # on the Python path.  Fall back to plain python3 if not sudo.
        python_cmd = ["python3"]
        if sudo_user:
            python_cmd = ["sudo", "-u", sudo_user, "python3"]
        triage_r = run(
            python_cmd
            + [
                str(triage_script),
                "--vmcore",
                str(local_vmcore),
                "--vmlinux",
                str(vmlinux),
                "--mod-dir",
                args.mod_dir,
                "--pretty",
            ],
            capture_output=False,
            timeout=120,
        )
        if triage_r.returncode != 0:
            print(
                f"warning: triage script failed (rc={triage_r.returncode})",
                file=sys.stderr,
            )
        print(f"\nvmcore dir: {local_dir}")
    else:
        print(f"vmcore dir: {local_dir}")
        print("run triage:")
        print(
            f"  crash-tool recipes lustre "
            f"--vmcore {local_vmcore} "
            f"--vmlinux {vmlinux} "
            f"--mod-dir <build-tree>"
        )
    return EXIT_OK


# ── snapshots ────────────────────────────────────────────


def cmd_snapshot(args: argparse.Namespace) -> None:
    vm = VMInfo.load(args.name)

    # --delete: remove a named snapshot from the overlay.  Kept small
    # on purpose: no VM restart dance since qemu-img snapshot -d works
    # on a stopped-or-running overlay the same way snapshot -c does.
    delete_tag = getattr(args, "delete", None)
    if delete_tag:
        delete_err: str | None = None
        with _with_vm_stopped(
            vm,
            "to delete snapshot",
            register_before_wait=True,
            get_error=lambda: (
                f"snapshot delete failed: {delete_err}"
                if delete_err
                else None
            ),
        ):
            r = run(
                [QEMU_IMG, "snapshot", "-d", delete_tag, str(vm.overlay_path)]
            )
            if r.returncode != 0:
                delete_err = (r.stderr or "qemu-img failed").strip()
            else:
                print(
                    f"snapshot '{delete_tag}' deleted from {vm.name}"
                )
        if delete_err:
            die(f"snapshot delete failed: {delete_err}")
        return

    tag = args.tag or f"snap-{time.strftime('%Y%m%d-%H%M%S')}"

    snapshot_err: str | None = None
    with _with_vm_stopped(
        vm,
        "for snapshot",
        get_error=lambda: (
            f"snapshot failed: {snapshot_err}" if snapshot_err else None
        ),
    ):
        r = run([QEMU_IMG, "snapshot", "-c", tag, str(vm.overlay_path)])
        if r.returncode != 0:
            snapshot_err = r.stderr or "snapshot failed"
        else:
            print(f"snapshot '{tag}' created for {vm.name}")
    if snapshot_err:
        die(f"snapshot failed: {snapshot_err}")


def _parse_snapshot_tags(qemu_img_output: str) -> set[str]:
    """Extract the TAG column from `qemu-img snapshot -l` output.

    Header looks like:
        Snapshot list:
        ID    TAG    VM SIZE   DATE    VM CLOCK   ICOUNT
        1     foo    0 B  ...

    We skip the first two lines and pull the second whitespace-separated
    field from each remaining row.  Returns an empty set if the format
    isn't recognized.
    """
    tags: set[str] = set()
    lines = qemu_img_output.splitlines()
    in_table = False
    for line in lines:
        if not in_table:
            # The header row contains "TAG" -- next lines are data rows
            if "TAG" in line and "ID" in line:
                in_table = True
            continue
        parts = line.split()
        if len(parts) >= 2:
            tags.add(parts[1])
    return tags


def cmd_restore(args: argparse.Namespace) -> None:
    vm = VMInfo.load(args.name)

    if not args.tag:
        print(f"snapshots for {vm.name}:")
        run(
            [QEMU_IMG, "snapshot", "-l", "-U", str(vm.overlay_path)],
            capture_output=False,
        )
        return

    # Verify the tag exists before stopping the VM.  Parse the
    # `qemu-img snapshot -l` table and check the TAG column exactly,
    # not via substring -- a substring check would falsely accept any
    # string that happens to appear in the table (an integer matching
    # an ID column, a date fragment, the literal "Snapshot list" header).
    check = run([QEMU_IMG, "snapshot", "-l", "-U", str(vm.overlay_path)])
    tags = _parse_snapshot_tags(check.stdout or "")
    if args.tag not in tags:
        die(f"restore failed: snapshot '{args.tag}' not found")

    restore_err: str | None = None
    with _with_vm_stopped(
        vm,
        "before restore",
        get_error=lambda: (
            f"restore failed: {restore_err}" if restore_err else None
        ),
    ):
        r = run(
            [QEMU_IMG, "snapshot", "-a", args.tag, str(vm.overlay_path)],
        )
        if r.returncode != 0:
            restore_err = r.stderr
            die(f"restore failed: {r.stderr}")
        print(f"restored {vm.name} to '{args.tag}'")


# ── doctor ───────────────────────────────────────────────


def _check_export_tools() -> list[str]:
    """Return warning lines for missing `ltvm target export` deps.

    Split out so tests can exercise the check directly without
    mocking the rest of cmd_doctor.  `ltvm install` is the fixer for
    anything listed here.
    """
    import shutil as _shutil

    warnings: list[str] = []
    for tool in ("parted", "qemu-img"):
        if _shutil.which(tool) is None:
            warnings.append(
                f"missing host tool: {tool} (needed by `ltvm target export`)"
            )
    if _shutil.which("grub2-install") is None and \
            _shutil.which("grub-install") is None:
        warnings.append(
            "missing host tool: grub2-install/grub-install "
            "(needed by `ltvm target export`)"
        )
    return warnings


def cmd_doctor(args: argparse.Namespace) -> int:
    issues = 0

    # Socket + overlay dir perms: non-root `ltvm list`/`deploy`/`llmount`
    # need read access, so these are owned root:root but should be 0755.
    # .info files should be 0644 so non-root callers can read VM state.
    for d in (SOCKETS, OVERLAYS):
        if d.exists():
            mode = d.stat().st_mode & 0o777
            if mode != 0o755:
                print(f"tight perms: {d} is {oct(mode)} (want 0o755)")
                issues += 1
                if args.fix:
                    try:
                        d.chmod(0o755)
                        print("  fixed: chmod 0755")
                    except OSError as e:
                        print(f"  could not chmod: {e}")
    for info in sorted(SOCKETS.glob("*.info")):
        try:
            mode = info.stat().st_mode & 0o777
        except OSError:
            continue
        if mode != 0o644:
            print(f"tight perms: {info.name} is {oct(mode)} (want 0o644)")
            issues += 1
            if args.fix:
                try:
                    info.chmod(0o644)
                    print("  fixed: chmod 0644")
                except OSError as e:
                    print(f"  could not chmod: {e}")

    for name in VMInfo.all_names():
        try:
            vm = VMInfo.load(name)
        except VMNotFound:
            continue
        if vm.pid > 0 and not is_running(vm):
            print(f"stale PID: {name} (pid {vm.pid} dead)")
            issues += 1
            if args.fix:
                vm.update_pid(0)
                print("  fixed: reset PID to 0")

    for overlay in sorted(OVERLAYS.glob("*.qcow2")):
        oname = overlay.stem
        if not (SOCKETS / f"{oname}.info").exists():
            size = overlay.stat().st_size // 1048576
            print(f"orphan overlay: {oname} ({size}M)")
            issues += 1
            if args.fix:
                overlay.unlink(missing_ok=True)
                for disk in OVERLAYS.glob(f"{oname}-disk*.img"):
                    disk.unlink(missing_ok=True)
                print("  fixed: removed")

    # Orphan data disks (overlay was removed but disks weren't, e.g.
    # crash between cmd_create's overlay create and disk truncate, or
    # the overlay was unlinked manually).
    for disk in sorted(OVERLAYS.glob("*-disk*.img")):
        # Strip "-diskN.img" off the end to get the VM name
        stem = re.sub(r"-disk\d+$", "", disk.stem)
        if not stem:
            continue
        if (OVERLAYS / f"{stem}.qcow2").exists():
            continue
        if (SOCKETS / f"{stem}.info").exists():
            continue
        size = disk.stat().st_size // 1048576
        print(f"orphan disk: {disk.name} ({size}M)")
        issues += 1
        if args.fix:
            disk.unlink(missing_ok=True)
            print("  fixed: removed")

    # Orphan socket-side files (.pid, .log, .qmp, .info.lock) whose
    # matching .info file is gone.  These accumulate when cmd_destroy
    # races with a crash, or when the user removes a .info by hand.
    for ext in ("pid", "log", "qmp"):
        for f in sorted(SOCKETS.glob(f"*.{ext}")):
            if not (SOCKETS / f"{f.stem}.info").exists():
                print(f"orphan {ext}: {f.name}")
                issues += 1
                if args.fix:
                    f.unlink(missing_ok=True)
                    print("  fixed: removed")
    for f in sorted(SOCKETS.glob(".*.info.lock")):
        # Strip leading "." and trailing ".info.lock"
        bare = f.name[1 : -len(".info.lock")]
        if not (SOCKETS / f"{bare}.info").exists():
            print(f"orphan info lock: {f.name}")
            issues += 1
            if args.fix:
                f.unlink(missing_ok=True)
                print("  fixed: removed")

    # Cluster files referencing dead nodes.  A user can `ltvm destroy
    # node` individually after a `cluster create`, leaving the .cluster
    # file pointing at VMs that no longer exist.
    from .vm_state import ClusterInfo, ClusterNotFound

    for cname in ClusterInfo.all_names():
        try:
            cluster = ClusterInfo.load(cname)
        except ClusterNotFound:
            continue
        missing_nodes = [
            n
            for n in cluster.get_nodes()
            if not (SOCKETS / f"{n.name}.info").exists()
        ]
        if missing_nodes and len(missing_nodes) == len(cluster.get_nodes()):
            # Every node gone -- the cluster file is meaningless.
            print(f"orphan cluster: {cname} (all nodes destroyed)")
            issues += 1
            if args.fix:
                cluster.path.unlink(missing_ok=True)
                print("  fixed: removed")
        elif missing_nodes:
            names = ", ".join(n.name for n in missing_nodes)
            print(f"degraded cluster: {cname} (missing nodes: {names})")
            issues += 1
            # No --fix for partial degradation: the user may want to
            # recreate the missing nodes manually.

    hosts = HOSTS_FILE
    if hosts.exists():
        for line in hosts.read_text().splitlines():
            m = re.search(
                rf"{re.escape(MARKER)}:(\S+)$",
                line,
            )
            if m:
                hname = m.group(1)
                if not (SOCKETS / f"{hname}.info").exists():
                    print(f"stale hosts entry: {hname}")
                    issues += 1
                    if args.fix:
                        # Delegate to the same atomic write path used
                        # by `ltvm destroy` so concurrent doctor + create
                        # races don't lose entries.
                        unregister_ssh_name(hname)
                        print("  fixed: removed from /etc/hosts")

    r = run(["ip", "-o", "link", "show", "type", "tun"])
    if r.returncode == 0:
        # Build the set of TAPs owned by running VMs -- mgmt TAP plus
        # every extra-NIC TAP.  Anything under tap-* that isn't in this
        # set (and is currently visible to `ip link`) is an orphan.
        live_taps: set[str] = set()
        for name in VMInfo.all_names():
            try:
                vm = VMInfo.load(name)
            except VMNotFound:
                continue
            if not is_running(vm):
                continue
            live_taps.add(vm.tap)
            for _idx, _nic_type, _tap, _mac in vm.extra_nics():
                live_taps.add(_tap)
        for line in r.stdout.splitlines():
            m = re.search(r":\s*(tap-\S+?)[@:]", line)
            if not m:
                continue
            tap = m.group(1)
            if tap in live_taps:
                continue
            print(f"orphan TAP: {tap}")
            issues += 1
            if args.fix:
                run(["ip", "link", "del", tap])
                print("  fixed: removed")

    for line in _check_export_tools():
        print(line)
        issues += 1

    _, ssh_dir = _real_user_ssh_dir()
    ssh_cfg = ssh_dir / "config"
    if ssh_cfg.exists():
        for line in ssh_cfg.read_text().splitlines():
            m = re.search(
                rf"{re.escape(MARKER)}:(\S+)$",
                line,
            )
            if m:
                sname = m.group(1)
                if not (SOCKETS / f"{sname}.info").exists():
                    print(f"stale ssh config: {sname}")
                    issues += 1
                    if args.fix:
                        unregister_ssh_name(sname)
                        print("  fixed: removed ssh config")

    if issues == 0:
        print("no issues found")
        return EXIT_OK
    print("---")
    if args.fix:
        print(f"{issues} issue(s) found and fixed")
        return EXIT_OK
    print(f"{issues} issue(s) found")
    # Offer to fix interactively when a human is at the terminal.
    # CI/non-tty callers still get the old "run with --fix" hint +
    # non-zero exit so they can gate on it.
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            resp = input("Fix them now? [y/N] ").strip().lower()
        except EOFError:
            resp = ""
        if resp in ("y", "yes"):
            args.fix = True
            print("---")
            return cmd_doctor(args)
    else:
        print("run with --fix to clean up")
    # Non-zero exit so CI scripts running `ltvm doctor` can detect
    # orphans without parsing stdout.  --fix path still returns 0
    # because the issues were resolved.
    return EXIT_ERROR
