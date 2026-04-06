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

from .models import (
    BASE_IMAGE,
    DISK_SIZE_BYTES,
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_TIMEOUT,
    EXIT_UNREACHABLE,
    KERNEL,
    MARKER,
    OVERLAYS,
    SOCKETS,
    VMInfo,
    VMNotFound,
)
from .net import (
    _real_user_ssh_dir,
    check_ip_collision,
    deploy_ssh_key,
    ip_for_name,
    mac_for_name,
    register_ssh_name,
    reload_dns,
    run_ssh,
    tap_for_name,
    unregister_ssh_name,
    wait_for_ssh,
)
from .process import die, is_running, kill_qemu, launch_qemu, run


def _fmt_epoch(epoch: int) -> str:
    """Format epoch seconds as a human-readable timestamp, or '-' if 0."""
    if not epoch:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


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


# ── lifecycle ────────────────────────────────────────────


def cmd_create(args: argparse.Namespace) -> None:
    name = args.name
    if not name:
        name = f"qemu-{int(time.time()) % 100000000}"

    if (SOCKETS / f"{name}.info").exists():
        die(f"VM '{name}' already exists")

    ip = args.ip or ip_for_name(name)
    check_ip_collision(name, ip)

    tap = tap_for_name(name)
    mac = mac_for_name(name)

    image = getattr(args, "image", "") or args.rootfs or str(BASE_IMAGE)
    kernel = getattr(args, "kernel", "") or ""

    base_name = Path(image).name if image else "rocky9-base.ext4"
    os_id = (
        "ubuntu24"
        if "ubuntu" in base_name
        else "rocky8"
        if "rocky8" in base_name
        else "rocky9"
    )

    vm = VMInfo(
        name=name,
        ip=ip,
        tap=tap,
        mac=mac,
        vcpus=args.vcpus,
        mem=args.mem,
        mdt_disks=args.mdt_disks,
        ost_disks=args.ost_disks,
        image=image if image != str(BASE_IMAGE) else "",
        kernel=kernel,
        created=int(time.time()),
        base_image=base_name,
        os_id=os_id,
    )

    # Create overlay
    run(
        [
            "qemu-img",
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

    # Create backing disks
    total = vm.mdt_disks + vm.ost_disks
    for n in range(1, total + 1):
        run(
            ["truncate", "-s", str(DISK_SIZE_BYTES), str(vm.disk_path(n))],
            check=True,
        )

    vm.save()
    launch_qemu(vm)
    wait_for_ssh(vm.ip, 30)
    register_ssh_name(vm.name, vm.ip)
    deploy_ssh_key(vm.ip)

    print(
        f"name={vm.name} ip={vm.ip} pid={vm.pid} "
        f"mdt_disks={vm.mdt_disks} ost_disks={vm.ost_disks}"
    )


def cmd_start(args: argparse.Namespace) -> None:
    for name in args.names:
        vm = VMInfo.load(name)
        launch_qemu(vm)
        wait_for_ssh(vm.ip, 30)
        register_ssh_name(vm.name, vm.ip)
        print(f"started {name}")


def cmd_start_all(args: argparse.Namespace) -> None:
    started = 0
    for name in VMInfo.all_names():
        vm = VMInfo.load(name)
        if not is_running(vm):
            launch_qemu(vm)
            wait_for_ssh(vm.ip, 30)
            register_ssh_name(vm.name, vm.ip)
            started += 1
            print(f"started {name}")
    print(f"started {started} VM(s)")


def cmd_stop(args: argparse.Namespace) -> None:
    for name in args.names:
        vm = VMInfo.load(name)
        kill_qemu(vm)
        print(f"stopped {name}")


def cmd_stop_all(args: argparse.Namespace) -> None:
    stopped = 0
    for name in VMInfo.all_names():
        vm = VMInfo.load(name)
        if is_running(vm):
            kill_qemu(vm)
            stopped += 1
            print(f"stopped {name}")
    print(f"stopped {stopped} VM(s)")


def cmd_restart(args: argparse.Namespace) -> None:
    for name in args.names:
        vm = VMInfo.load(name)
        kill_qemu(vm)
        launch_qemu(vm)
        wait_for_ssh(vm.ip, 30)
        register_ssh_name(vm.name, vm.ip)
        print(f"restarted {name}")


def cmd_destroy(args: argparse.Namespace) -> None:
    for name in args.names:
        try:
            vm = VMInfo.load(name)
            kill_qemu(vm)
        except VMNotFound:
            pass

        overlay = OVERLAYS / f"{name}.qcow2"
        for f in [overlay] + list(OVERLAYS.glob(f"{name}-disk*.img")):
            f.unlink(missing_ok=True)
        for ext in ("sock", "pid", "info", "log"):
            (SOCKETS / f"{name}.{ext}").unlink(missing_ok=True)

        unregister_ssh_name(name)
        print(f"destroyed {name}")


def cmd_ensure(args: argparse.Namespace) -> None:
    name = args.name
    info_path = SOCKETS / f"{name}.info"

    if info_path.exists():
        vm = VMInfo.load(name)
        if is_running(vm):
            wait_for_ssh(vm.ip, 30)
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
        wait_for_ssh(vm.ip, 30)
        register_ssh_name(vm.name, vm.ip)
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

    create_args = argparse.Namespace(
        name=name,
        vcpus=args.vcpus,
        mem=args.mem,
        ip=None,
        rootfs=None,
        image=getattr(args, "image", ""),
        kernel=getattr(args, "kernel", ""),
        mdt_disks=args.mdt_disks,
        ost_disks=args.ost_disks,
    )
    cmd_create(create_args)
    if args.json:
        print(
            json.dumps(
                {
                    "action": "created",
                    "name": name,
                    "status": "running",
                }
            )
        )


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

    output = r.stdout or ""
    if r.stderr:
        output += r.stderr

    if r.returncode == 255:
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "exit_code": EXIT_UNREACHABLE,
                        "error": "unreachable",
                        "output": output,
                    }
                )
            )
        else:
            print("error: unreachable", file=sys.stderr)
            if output:
                print(output)
        sys.exit(EXIT_UNREACHABLE)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": r.returncode == 0,
                    "exit_code": r.returncode,
                    "output": output,
                }
            )
        )
    else:
        if output:
            print(output, end="")
    sys.exit(r.returncode)


def cmd_ssh(args: argparse.Namespace) -> None:
    vm = VMInfo.load(args.name)
    ssh_args = [
        "sshpass",
        "-p",
        "initial0",
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "LogLevel=ERROR",
        f"root@{vm.ip}",
    ] + args.command
    os.execvp("sshpass", ssh_args)


def cmd_cp_to(args: argparse.Namespace) -> None:
    vm = VMInfo.load(args.name)
    r = run(
        [
            "sshpass",
            "-p",
            "initial0",
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-r",
            args.src,
            f"root@{vm.ip}:{args.dest}",
        ],
        capture_output=False,
    )
    sys.exit(r.returncode)


def cmd_cp_from(args: argparse.Namespace) -> None:
    vm = VMInfo.load(args.name)
    r = run(
        [
            "sshpass",
            "-p",
            "initial0",
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-r",
            f"root@{vm.ip}:{args.src}",
            args.dest,
        ],
        capture_output=False,
    )
    sys.exit(r.returncode)


# ── info + observability ─────────────────────────────────


def cmd_list(args: argparse.Namespace) -> None:
    total_vcpus = 0
    total_mem = 0
    running_count = 0
    stopped_count = 0
    entries = []

    for name in VMInfo.all_names():
        vm = VMInfo.load(name)
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
                disks = f" mdt={e['mdt_disks']} ost={e['ost_disks']}"
            print(
                f"{e['name']:<20} {e['ip']:<18} {e['status']:<8} "
                f"pid={e['pid']:<8} vcpus={e['vcpus']} "
                f"mem={e['mem']:<6} disk={e['disk']:<8}{disks}"
            )
        print("---")
        print(
            f"{running_count} running, {stopped_count} stopped | "
            f"vcpus: {total_vcpus}/{host_cpus} | "
            f"mem: {total_mem}M/{host_mem_mb}M"
        )


def cmd_status(args: argparse.Namespace) -> None:
    vm = VMInfo.load(args.name)
    qemu_status = "running" if is_running(vm) else "dead"

    ssh_status = "unreachable"
    if qemu_status == "running":
        try:
            r = run_ssh(vm.ip, "true", timeout=5)
            if r.returncode == 0:
                ssh_status = "ok"
        except subprocess.TimeoutExpired:
            pass

    lustre_status = "not loaded"
    mount_status = "not mounted"
    if ssh_status == "ok":
        try:
            r = run_ssh(
                vm.ip,
                "lsmod 2>/dev/null | grep -c lustre",
                timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip() not in ("", "0"):
                lustre_status = "loaded"
        except subprocess.TimeoutExpired:
            pass
        try:
            r = run_ssh(
                vm.ip,
                "mount 2>/dev/null | grep -c lustre",
                timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip() not in ("", "0"):
                mount_status = "mounted"
        except subprocess.TimeoutExpired:
            pass

    if args.json:
        print(
            json.dumps(
                {
                    "name": vm.name,
                    "ip": vm.ip,
                    "qemu": qemu_status,
                    "pid": vm.pid,
                    "ssh": ssh_status,
                    "lustre": lustre_status,
                    "mount": mount_status,
                    "vcpus": vm.vcpus,
                    "mem": vm.mem,
                    "mdt_disks": vm.mdt_disks,
                    "ost_disks": vm.ost_disks,
                    "created": vm.created,
                    "last_boot": vm.last_boot,
                    "last_deploy": vm.last_deploy,
                    "build_path": vm.build_path,
                    "kver": vm.kver,
                    "base_image": vm.base_image,
                    "os_id": vm.os_id,
                }
            )
        )
    else:
        print(f"{'name:':<14} {vm.name}")
        print(f"{'ip:':<14} {vm.ip}")
        print(f"{'qemu:':<14} {qemu_status} (pid {vm.pid})")
        print(f"{'ssh:':<14} {ssh_status}")
        print(f"{'lustre:':<14} {lustre_status}")
        print(f"{'mount:':<14} {mount_status}")
        print(
            f"{'resources:':<14} vcpus={vm.vcpus} mem={vm.mem} "
            f"mdt={vm.mdt_disks} ost={vm.ost_disks}"
        )
        print(f"{'os:':<14} {vm.os_id or '-'} ({vm.base_image or '-'})")
        print(f"{'created:':<14} {_fmt_epoch(vm.created)}")
        print(
            f"{'last boot:':<14} {_fmt_epoch(vm.last_boot)} ({_ago(vm.last_boot)})"
        )
        if vm.last_deploy:
            print(
                f"{'deployed:':<14} {_fmt_epoch(vm.last_deploy)} ({_ago(vm.last_deploy)})"
            )
            print(f"{'build:':<14} {vm.build_path}")
            print(f"{'kver:':<14} {vm.kver}")


def cmd_log(args: argparse.Namespace) -> None:
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


def cmd_lustre_log(args: argparse.Namespace) -> None:
    vm = VMInfo.load(args.name)
    if not is_running(vm):
        die(f"VM '{args.name}' not running", EXIT_UNREACHABLE)
    try:
        r = run_ssh(
            vm.ip,
            'lctl dk 2>/dev/null || echo "lctl not available"',
            timeout=10,
        )
        if r.stdout:
            print(r.stdout, end="")
        sys.exit(r.returncode)
    except subprocess.TimeoutExpired:
        die(
            f"timeout reading lustre log from '{args.name}'",
            EXIT_TIMEOUT,
        )


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

    if not is_running(vm):
        die(f"VM '{args.name}' not running")

    print("finding vmcore...")
    r = run_ssh(
        vm.ip,
        "ls -td /var/crash/*/vmcore 2>/dev/null | head -1",
        timeout=10,
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
            "initial0",
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

    print(f"vmcore: {local_vmcore}")

    if args.mod_dir:
        vmlinux = str(KERNEL)
        print("running lustre triage...")
        triage_script = Path(
            "/home/admin/llm_code_and_review_tools/"
            "lustre-drgn-tools/lustre_triage.py"
        )
        if not triage_script.exists():
            print(f"triage script not found: {triage_script}")
            print(f"vmcore dir: {local_dir}")
            return
        run(
            [
                "python3",
                str(triage_script),
                "--vmcore",
                str(local_vmcore),
                "--vmlinux",
                vmlinux,
                "--mod-dir",
                args.mod_dir,
                "--pretty",
            ],
            capture_output=False,
            timeout=120,
        )
        print(f"\nvmcore dir: {local_dir}")
    else:
        print(f"vmcore dir: {local_dir}")
        print("run triage:")
        print(
            f"  crash-tool recipes lustre "
            f"--vmcore {local_vmcore} "
            f"--vmlinux {KERNEL} "
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

    r = run(["qemu-img", "snapshot", "-c", tag, str(vm.overlay_path)])
    if r.returncode != 0:
        die(f"snapshot failed: {r.stderr}")

    print(f"snapshot '{tag}' created for {vm.name}")

    if was_running:
        print(f"restarting {vm.name}...")
        launch_qemu(vm)
        wait_for_ssh(vm.ip, 30)
        register_ssh_name(vm.name, vm.ip)
        print(f"started {vm.name}")


def cmd_restore(args: argparse.Namespace) -> None:
    vm = VMInfo.load(args.name)

    if not args.tag:
        print(f"snapshots for {vm.name}:")
        run(
            ["qemu-img", "snapshot", "-l", "-U", str(vm.overlay_path)],
            capture_output=False,
        )
        return

    if is_running(vm):
        print(f"stopping {vm.name} before restore...")
        kill_qemu(vm)

    r = run(
        ["qemu-img", "snapshot", "-a", args.tag, str(vm.overlay_path)],
    )
    if r.returncode != 0:
        die(f"restore failed: {r.stderr}")
    print(f"restored {vm.name} to '{args.tag}'")


# ── doctor ───────────────────────────────────────────────


def cmd_doctor(args: argparse.Namespace) -> None:
    issues = 0

    for name in VMInfo.all_names():
        vm = VMInfo.load(name)
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
                overlay.unlink()
                for disk in OVERLAYS.glob(f"{oname}-disk*.img"):
                    disk.unlink()
                print("  fixed: removed")

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
                        lines = [
                            ln
                            for ln in hosts.read_text().splitlines()
                            if f"{MARKER}:{hname}" not in ln
                        ]
                        hosts.write_text("\n".join(lines) + "\n")
                        reload_dns()
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
                vm = VMInfo.load(name)
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
    else:
        print("---")
        print(f"{issues} issue(s) found")
        if not args.fix:
            print("run with --fix to clean up")
