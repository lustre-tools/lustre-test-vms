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
from typing import Any

from .deploy import configure_test_disks
from .paths import load_meta_safe
from .qemu_run import die, is_running, kill_qemu, launch_qemu, run
from .vm_net import (
    _real_user_ssh_dir,
    alloc_ip,
    deploy_ssh_key,
    mac_for_name,
    register_ssh_name,
    run_ssh,
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


def _seed_kdump_boot(vm: VMInfo) -> None:
    """Ensure /boot has vmlinuz + kdump initramfs for kexec-on-panic.

    QEMU passes the kernel externally via -kernel, so the VM image has
    no /boot/vmlinuz or initramfs unless image_build baked them in.
    As of the per-kernel image refactor, fresh images ship with both
    pre-built, and this function is a fast no-op that just (re)loads
    the kdump service.  For older images that predate the bake, or
    custom images without kdump artifacts, we fall back to scp'ing
    the vmlinuz and running dracut/update-initramfs at runtime.
    """
    if not vm.kernel:
        return

    kver = vm.kver
    if not kver:
        r = run_ssh(vm.ip, "uname -r", timeout=10)
        if r.returncode != 0:
            print(
                "warning: could not determine kernel version; kdump boot seeding skipped"
            )
            return
        kver = r.stdout.strip()

    # Determine OS family from target config.  Catch only the narrow
    # cases where the target config is genuinely missing/broken
    # (ValueError: unknown target / bad schema, FileNotFoundError:
    # targets.yaml gone).  A blanket `except Exception: pass` would
    # silently fall back to "rhel" for a Debian VM and run dracut
    # against an apt-based system.
    os_family = "rhel"
    if vm.os_id:
        try:
            from .target_config import TargetConfig

            os_family = TargetConfig(vm.os_id).os_family
        except (ValueError, FileNotFoundError) as e:
            print(
                f"warning: cannot resolve target {vm.os_id!r}, "
                f"defaulting to rhel kdump path: {e}",
                file=sys.stderr,
            )

    # Fast path: image was built with kdump baked in.  Probe for both
    # the kernel and the initramfs; if present, just make sure the
    # kdump service is (re)loaded for this kernel.
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
    if probe.returncode == 0:
        run_ssh(vm.ip, reload_cmd, timeout=30)
        return

    print(
        "warning: kdump boot artifacts missing from image; "
        "falling back to runtime seeding"
    )

    # Prefer the bzImage for kdump; fall back to vmlinux if that's all we have.
    kernel_host = Path(vm.kernel)
    if kernel_host.name == "vmlinux":
        vmlinuz = kernel_host.parent / "vmlinuz"
        if vmlinuz.exists():
            kernel_host = vmlinuz

    print(f"seeding /boot/vmlinuz-{kver} for kdump...")
    r = run(
        [
            "sshpass",
            "-p",
            ROOT_PASSWORD,
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            str(kernel_host),
            f"root@{vm.ip}:/boot/vmlinuz-{kver}",
        ],
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        die(f"failed to copy vmlinuz into VM /boot: {r.stderr.strip()}")

    if os_family == "debian":
        print("generating kdump initramfs (update-initramfs)...")
        # Remove broken initramfs hooks (e.g. dhcpcd) that fail when the
        # package isn't fully installed, then generate the initramfs.
        # Ubuntu kdump-tools expects the crash initrd at /var/lib/kdump/.
        # Copy the kernel config so kdump-tools can read it.
        config_src = Path(vm.kernel).parent / "build-tree" / ".config"
        if config_src.exists():
            r_scp = run(
                [
                    "sshpass",
                    "-p",
                    ROOT_PASSWORD,
                    "scp",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                    "-o",
                    "LogLevel=ERROR",
                    str(config_src),
                    f"root@{vm.ip}:/boot/config-{kver}",
                ],
                capture_output=True,
                timeout=10,
            )
            if r_scp.returncode != 0:
                die(
                    f"failed to copy kernel config to VM: "
                    f"{r_scp.stderr.strip()}"
                )
        r = run_ssh(
            vm.ip,
            # Use PIPESTATUS to check update-initramfs' exit code instead
            # of the grep that follows it (grep -v's rc would otherwise
            # mask initramfs failures).  `grep -v ... || true` keeps grep
            # quiet when every line is a warning, without losing the
            # PIPESTATUS check.
            f"rm -f /usr/share/initramfs-tools/hooks/dhcpcd && "
            f"{{ update-initramfs -c -k {kver} 2>&1 | "
            f"  {{ grep -v '^W:' || true; }}; "
            f'  test "${{PIPESTATUS[0]}}" -eq 0; }} && '
            f"mkdir -p /var/lib/kdump && "
            f"cp /boot/initrd.img-{kver} /var/lib/kdump/initrd.img-{kver} && "
            f"ln -sf /boot/vmlinuz-{kver} /var/lib/kdump/vmlinuz",
            timeout=120,
        )
        if r.returncode != 0:
            die(f"update-initramfs failed: {r.stdout.strip()}")
        r = run_ssh(
            vm.ip,
            "kdump-config load 2>&1; systemctl restart kdump-tools 2>&1",
            timeout=30,
        )
        if r.returncode != 0:
            die(f"kdump-tools service failed to start: {r.stdout.strip()}")
    else:
        print("generating kdump initramfs (dracut)...")
        r = run_ssh(
            vm.ip,
            f"dracut --kver {kver} --force /boot/initramfs-{kver}.img {kver} 2>&1",
            timeout=120,
        )
        if r.returncode != 0:
            die(f"dracut failed for kdump initramfs: {r.stdout.strip()}")
        r = run_ssh(vm.ip, "systemctl restart kdump 2>&1", timeout=30)
        if r.returncode != 0:
            die(f"kdump service failed to start: {r.stdout.strip()}")


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
    """Parse a disk size string (e.g. '500M', '10G') to bytes.

    Suffix must be M or G.  An already-parsed int is returned as-is.
    Raises SystemExit on invalid input.
    """
    if not value:
        return DISK_SIZE_BYTES
    if isinstance(value, int):
        return value if value >= _MIN_DISK_BYTES else DISK_SIZE_BYTES
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


def cmd_create(args: argparse.Namespace) -> None:
    name = args.name
    if not name:
        name = f"qemu-{int(time.time()) % 100000000}"
    _validate_vm_name(name)

    info_path = SOCKETS / f"{name}.info"
    if info_path.exists():
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
            return
        launch_qemu(vm)
        wait_for_ssh(vm.ip, SSH_TIMEOUT)
        register_ssh_name(vm.name, vm.ip)
        deploy_ssh_key(vm.ip)
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

    os_target = getattr(args, "os", "")
    explicit_image = getattr(args, "image", "")
    explicit_kernel = getattr(args, "kernel", "")
    arch = getattr(args, "arch", None) or "x86_64"
    defaulted_target = not os_target
    if defaulted_target:
        os_target = DEFAULT_TARGET
    # Pass explicit_kernel (a name, not a path) through to resolve_os_artifacts
    # which will find the right kernel dir and pair the correct image with it.
    os_arts = resolve_os_artifacts(
        os_target, arch=arch, kernel=explicit_kernel or None
    )
    if defaulted_target and not explicit_image and not explicit_kernel:
        print(
            f"using default target: {os_target} "
            f"(kernel: {os_arts.kernel.parent.name})"
        )
    image = explicit_image or str(os_arts.image)
    kernel = str(os_arts.kernel)
    # If the user didn't pass --mem, fall back to the target's default
    # (rocky10 needs 4096; others use 2048).  argparse default is None
    # so we can distinguish "user said 2048" from "user said nothing".
    if args.mem is None:
        args.mem = os_arts.default_mem

    base_name = Path(image).name
    os_id = os_target

    # Read kernel version from meta.json next to the kernel binary
    kver = ""
    kernel_meta = Path(kernel).parent / "meta.json"
    meta = load_meta_safe(kernel_meta)
    if meta is not None:
        kver = meta.get("kernel_version", "")

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
        )

        # Create overlay + backing disks.  If any step fails partway,
        # unlink everything we already created so we don't leave orphan
        # files behind for the next `ltvm create <same name>` to trip on.
        # The .info file isn't written yet so cmd_doctor can't see these
        # orphans either, which makes manual recovery awkward.
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
        launch_qemu(vm)
        wait_for_ssh(vm.ip, SSH_TIMEOUT)
        register_ssh_name(vm.name, vm.ip)
        deploy_ssh_key(vm.ip)
        _seed_kdump_boot(vm)
    except BaseException:
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
            except OSError:
                pass
        try:
            kill_qemu(vm)
        except Exception:
            pass
        try:
            _destroy_vm_artifacts(vm.name)
        except Exception:
            pass
        try:
            unregister_ssh_name(vm.name)
        except Exception:
            pass
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
            f"name={vm.name} ip={vm.ip} pid={vm.pid} "
            f"mdt_disks={vm.mdt_disks} ost_disks={vm.ost_disks}"
        )


def cmd_start(args: argparse.Namespace) -> None:
    for name in args.names:
        vm = VMInfo.load(name)
        launch_qemu(vm)
        wait_for_ssh(vm.ip, SSH_TIMEOUT)
        register_ssh_name(vm.name, vm.ip)
        # Both of these are idempotent and cheap on a re-start, but
        # they're necessary for fresh VMs whose create was interrupted
        # before they ran (so cmd_start recovers a half-set-up VM).
        deploy_ssh_key(vm.ip)
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
        try:
            vm = VMInfo.load(name)
            kill_qemu(vm)
        except VMNotFound:
            pass

        _destroy_vm_artifacts(name)
        unregister_ssh_name(name)
        # Match cmd_stop's "{name} not found" wording so a typo like
        # `ltvm destroy co1-signle` doesn't silently claim success.
        if existed:
            print(f"destroyed {name}")
        else:
            print(f"destroy: {name} not found")


# ── execution ────────────────────────────────────────────


def cmd_exec(args: argparse.Namespace) -> None:
    name = args.name
    try:
        vm = VMInfo.load(name)
    except VMNotFound:
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "exit_code": EXIT_NOT_FOUND,
                        "error": "VM not found",
                    }
                )
            )
        else:
            print(f"error: VM '{name}' not found", file=sys.stderr)
        sys.exit(EXIT_NOT_FOUND)

    if not is_running(vm):
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "exit_code": EXIT_UNREACHABLE,
                        "error": "VM not running",
                    }
                )
            )
        else:
            print(f"error: VM '{name}' not running", file=sys.stderr)
        sys.exit(EXIT_UNREACHABLE)

    command = " ".join(args.command)
    if not command:
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "exit_code": EXIT_ERROR,
                        "error": "no command given",
                    }
                )
            )
        else:
            print("error: no command given", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    try:
        r = run_ssh(vm.ip, command, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "exit_code": EXIT_TIMEOUT,
                        "error": f"timeout after {args.timeout}s",
                    }
                )
            )
        else:
            print(
                f"error: timeout after {args.timeout}s",
                file=sys.stderr,
            )
        sys.exit(EXIT_TIMEOUT)

    stdout = r.stdout or ""
    stderr = r.stderr or ""
    # JSON callers consumed a single "output" field historically; preserve
    # that by combining the streams there.  Human callers get them on the
    # right file descriptors so stderr stays visible separately.
    combined = stdout + stderr

    if r.returncode == 255:
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "exit_code": EXIT_UNREACHABLE,
                        "error": "unreachable",
                        "output": combined,
                    }
                )
            )
        else:
            print("error: unreachable", file=sys.stderr)
            if stdout:
                print(stdout, end="")
            if stderr:
                print(stderr, end="", file=sys.stderr)
        sys.exit(EXIT_UNREACHABLE)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": r.returncode == 0,
                    "exit_code": r.returncode,
                    "output": combined,
                }
            )
        )
    else:
        if stdout:
            print(stdout, end="")
        if stderr:
            print(stderr, end="", file=sys.stderr)
    sys.exit(r.returncode)


def cmd_llmount(args: argparse.Namespace) -> None:
    name = args.name
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
    os_family = "rhel"
    if vm.os_id:
        try:
            from .target_config import TargetConfig

            os_family = TargetConfig(vm.os_id).os_family
        except (ValueError, FileNotFoundError) as e:
            print(
                f"warning: cannot resolve target {vm.os_id!r}, "
                f"defaulting to rhel libdir: {e}",
                file=sys.stderr,
            )
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


def cmd_ssh(args: argparse.Namespace) -> None:
    vm = VMInfo.load(args.name)
    ssh_args = [
        "sshpass",
        "-p",
        ROOT_PASSWORD,
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        f"root@{vm.ip}",
    ] + args.command
    os.execvp("sshpass", ssh_args)


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


def cmd_dmesg(args: argparse.Namespace) -> None:
    vm = VMInfo.load(args.name)
    if not is_running(vm):
        die(f"VM '{args.name}' not running", EXIT_UNREACHABLE)
    try:
        r = run_ssh(vm.ip, f"dmesg | tail -n {args.tail}", timeout=10)
        if r.stdout:
            print(r.stdout, end="")
        if r.returncode != 0 and r.stderr:
            print(r.stderr, end="", file=sys.stderr)
        sys.exit(r.returncode)
    except subprocess.TimeoutExpired:
        die(f"timeout reading dmesg from '{args.name}'", EXIT_TIMEOUT)


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
        raise RuntimeError(result["error"].get("desc", str(result["error"])))


def cmd_nmi(args: argparse.Namespace) -> None:
    vm = VMInfo.load(args.name)
    if not is_running(vm):
        die(f"VM '{args.name}' not running", EXIT_UNREACHABLE)
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
        die(f"failed to set NMI panic sysctls on '{args.name}': {e}")
    try:
        _qmp_nmi(vm.socket_path)
    except Exception as e:
        die(f"failed to inject NMI into '{args.name}': {e}")
    print(f"NMI injected into '{args.name}' (expect panic + kdump reboot)")


# ── crash-collect ────────────────────────────────────────


def cmd_crash_collect(args: argparse.Namespace) -> None:
    vm = VMInfo.load(args.name)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if args.trigger:
        if not is_running(vm):
            die(f"VM '{args.name}' not running, can't trigger crash")
        print(f"triggering crash on {args.name}...")
        try:
            run_ssh(vm.ip, "echo c > /proc/sysrq-trigger", timeout=5)
        except subprocess.TimeoutExpired:
            pass

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
            die(
                f"\nVM '{args.name}' did not come back after {args.wait + 5}s",
                EXIT_TIMEOUT,
            )
    elif not is_running(vm):
        die(f"VM '{args.name}' not running")

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
        die(
            f"timed out after {e.timeout}s probing /var/crash on '{args.name}'",
            EXIT_TIMEOUT,
        )
    if r.returncode != 0:
        # SSH failure (host unreachable, key mismatch, etc.) -- surface
        # the real error rather than the misleading "no vmcore found".
        die(
            f"failed to probe /var/crash on '{args.name}' "
            f"(rc={r.returncode}): {(r.stderr or r.stdout or '').strip()}"
        )
    vmcore_path = r.stdout.strip()
    if not vmcore_path:
        die("no vmcore found in /var/crash/")

    r = run_ssh(vm.ip, f"ls -lh {vmcore_path}", timeout=5)
    print(f"found: {r.stdout.strip()}")

    ts = time.strftime("%Y%m%d-%H%M%S")
    local_dir = outdir / f"crash-{args.name}-{ts}"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_vmcore = local_dir / "vmcore"

    print(f"copying vmcore to {local_vmcore}...")
    r = run(
        [
            "sshpass",
            "-p",
            ROOT_PASSWORD,
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            f"root@{vm.ip}:{vmcore_path}",
            str(local_vmcore),
        ],
        capture_output=False,
        timeout=300,
    )
    if r.returncode != 0:
        die("failed to copy vmcore")

    # Make vmcore readable by non-root so triage tools (run as SUDO_USER)
    # can open it.
    local_vmcore.chmod(0o644)

    print(f"vmcore: {local_vmcore}")

    # Resolve vmlinux: prefer the freshly-built vmlinux from output/, then
    # look next to vm.kernel (which usually points to vmlinuz).  We always
    # prefer vmlinux over vmlinuz because drgn needs full debug symbols.
    vmlinux: Path | None = None
    try:
        arts = resolve_os_artifacts(vm.os_id or DEFAULT_TARGET, arch=vm.arch)
        candidate = arts.kernel.parent / "vmlinux"
        if candidate.exists():
            vmlinux = candidate
    except Exception:
        pass
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
        sys.exit(EXIT_NOT_FOUND)

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
            return
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


# ── snapshots ────────────────────────────────────────────


def cmd_snapshot(args: argparse.Namespace) -> None:
    vm = VMInfo.load(args.name)
    tag = args.tag or f"snap-{time.strftime('%Y%m%d-%H%M%S')}"

    was_running = is_running(vm)
    if was_running:
        print(f"stopping {vm.name} for snapshot...")
        kill_qemu(vm)

    snapshot_err: str | None = None
    try:
        r = run([QEMU_IMG, "snapshot", "-c", tag, str(vm.overlay_path)])
        if r.returncode != 0:
            snapshot_err = r.stderr or "snapshot failed"
        else:
            print(f"snapshot '{tag}' created for {vm.name}")
    finally:
        if was_running:
            # Restart even if snapshot failed -- the user expects the
            # VM to come back up either way.  We don't use die() here
            # so the original snapshot error message survives.
            print(f"restarting {vm.name}...")
            try:
                launch_qemu(vm)
                wait_for_ssh(vm.ip, SSH_TIMEOUT)
                register_ssh_name(vm.name, vm.ip)
                # Match cmd_start: idempotent post-boot init.
                deploy_ssh_key(vm.ip)
                _seed_kdump_boot(vm)
                print(f"started {vm.name}")
            except SystemExit as e:
                # die() inside one of the restart steps -- print
                # both errors and exit with the snapshot error if
                # there was one, otherwise the restart error.
                if snapshot_err:
                    print(
                        f"error: snapshot failed: {snapshot_err}",
                        file=sys.stderr,
                    )
                raise e
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

    was_running = is_running(vm)
    if was_running:
        print(f"stopping {vm.name} before restore...")
        kill_qemu(vm)

    r = run(
        [QEMU_IMG, "snapshot", "-a", args.tag, str(vm.overlay_path)],
    )
    if r.returncode != 0:
        die(f"restore failed: {r.stderr}")
    print(f"restored {vm.name} to '{args.tag}'")

    if was_running:
        print(f"restarting {vm.name}...")
        launch_qemu(vm)
        wait_for_ssh(vm.ip, SSH_TIMEOUT)
        register_ssh_name(vm.name, vm.ip)
        # Match cmd_start: re-deploy the user's SSH key and re-seed
        # /boot for kdump.  Both are idempotent and necessary if the
        # restored snapshot predates the most recent setup.
        deploy_ssh_key(vm.ip)
        _seed_kdump_boot(vm)


# ── doctor ───────────────────────────────────────────────


def cmd_doctor(args: argparse.Namespace) -> int:
    issues = 0

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

    hosts = Path("/etc/hosts")
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
        for line in r.stdout.splitlines():
            m = re.search(r":\s*(tap-\S+?)[@:]", line)
            if not m:
                continue
            tap = m.group(1)
            found = False
            for name in VMInfo.all_names():
                try:
                    vm = VMInfo.load(name)
                except VMNotFound:
                    continue
                if vm.tap == tap and is_running(vm):
                    found = True
                    break
            if not found:
                print(f"orphan TAP: {tap}")
                issues += 1
                if args.fix:
                    run(["ip", "link", "del", tap])
                    print("  fixed: removed")

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
    print("run with --fix to clean up")
    # Non-zero exit so CI scripts running `ltvm doctor` can detect
    # orphans without parsing stdout.  --fix path still returns 0
    # because the issues were resolved.
    return EXIT_ERROR
