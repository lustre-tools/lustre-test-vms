"""End-to-end test fixtures for the ltvm CLI.

These tests drive the real `ltvm` binary against the host's QEMU /
bridge / ssh stack.  They require:

  * running as root (sudo) -- `ltvm create` touches host networking
    and QEMU sockets
  * a pre-built rocky9 base image -- tests refuse to build one, that
    is far too slow for a functional test

The whole module skips cleanly (clear one-line hint) when either
condition is missing, so ordinary `pytest tests/` invocations do not
regress.  Tests use a distinctive `e2e-` VM name prefix and the
`vm_name` factory below guarantees teardown even on failure.

Failure-capture: any test that fails gets its per-VM artefacts
(console log, .info, and a best-effort ssh dump of network state)
copied into /tmp/test_e2e/<test-id>/<vm>/ so debugging does not need
a rerun.
"""

from __future__ import annotations

import os
import random
import shutil
import string
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Callable

import pytest

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
LTVM_BIN = REPO_ROOT / "ltvm"
SOCKETS_DIR = Path("/opt/qemu-vms/sockets")
FAILURE_CAPTURE_ROOT = Path("/tmp/test_e2e")


def _sudo_user_home() -> Path:
    """Return the invoking user's $HOME, even under sudo.

    The e2e suite is run as root (phase-1 establishes that), which
    makes ``Path.home()`` -> ``/root``.  Users keep their Lustre
    checkout in ``~/lustre-release`` under their real home, so we
    honour the usual sudo conventions: ``SUDO_USER`` is set by sudo
    and pwd lookups give us the right home.  Falls back to
    ``Path.home()`` for non-sudo invocations (tests would already
    be skipped in that case, but the fallback is cheap).
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            import pwd
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except (KeyError, ImportError):
            pass
    return Path.home()


# The user's Lustre source checkout.  Shared across test_04/05/07 so
# a single override point is available if a test host uses a
# non-standard location.
LUSTRE_TREE = _sudo_user_home() / "lustre-release"

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=5",
    "-o", "LogLevel=ERROR",
]

# Distinctive prefix so tests can't collide with hand-run dev VMs.
VM_PREFIX = "e2e-"
# Separate prefix for cluster names so a stray `ltvm cluster list` at
# cleanup can't mistake a per-VM `e2e-` name for a cluster.
CLUSTER_PREFIX = "e2e-clu-"

# Keep test runtime bounded.  Create (~15s) + cloud-init boot (~15s) +
# ssh ready is the long pole; the assertions themselves are sub-second.
SSH_READY_TIMEOUT = 90


# ---------------------------------------------------------------------------
# Module-level preflight (runs before any test collection under this dir)
# ---------------------------------------------------------------------------


def _rocky9_image_built() -> bool:
    """Return True iff rocky9 has at least one usable base image on disk.

    We look directly at the on-disk artefact tree rather than parsing
    the text `ltvm target list` (whose layout is fixed-width with
    optional remote column -- fragile to parse).  An image dir under
    artifacts/rocky9/<arch>/images/ containing a 'base' variant with a
    populated meta.json indicates a built base image.
    """
    images = REPO_ROOT / "artifacts" / "rocky9"
    if not images.exists():
        return False
    # Look for any .../images/*/base/meta.json OR
    # .../images/*/meta.json (single-variant layout).
    for arch_dir in images.iterdir():
        if not arch_dir.is_dir():
            continue
        img_dir = arch_dir / "images"
        if not img_dir.is_dir():
            continue
        for kernel_dir in img_dir.iterdir():
            if not kernel_dir.is_dir():
                continue
            # Check for a base variant in either the per-variant
            # layout or the flat single-variant layout.
            candidates = [
                kernel_dir / "base" / "meta.json",
                kernel_dir / "meta.json",
            ]
            for meta in candidates:
                if meta.exists():
                    return True
    return False


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Inject module-level skip markers before tests run.

    We do this here (not in a session-scope fixture) so skips fire
    cleanly at collection time rather than after pytest sets up
    coverage / logging.
    """
    if not items:
        return
    # Only apply to items under this directory.
    here = Path(__file__).parent.resolve()
    scoped = [it for it in items if here in Path(it.fspath).parents or
              Path(it.fspath).parent == here]
    if not scoped:
        return

    reason = None
    if os.geteuid() != 0:
        reason = (
            "tests/test_e2e requires root; "
            "run as `sudo pytest tests/test_e2e/`"
        )
    elif not LTVM_BIN.exists():
        reason = f"ltvm binary missing at {LTVM_BIN}"
    elif not _rocky9_image_built():
        reason = (
            "rocky9 base image not built; "
            "run `ltvm target fetch rocky9` or "
            "`ltvm build all rocky9 --lustre-tree <path>`"
        )

    if reason is not None:
        marker = pytest.mark.skip(reason=reason)
        for it in scoped:
            it.add_marker(marker)


# ---------------------------------------------------------------------------
# Per-test VM-name factory with guaranteed teardown
# ---------------------------------------------------------------------------


# Map test_nodeid -> list of VM names the test claimed.  Consumed by
# the failure-capture hook below.
_TEST_VMS: dict[str, list[str]] = {}


def _rand_suffix(n: int = 6) -> str:
    return "".join(
        random.choice(string.ascii_lowercase + string.digits) for _ in range(n)
    )


def _destroy(name: str) -> None:
    """Best-effort `ltvm destroy` for teardown.  Never raises."""
    try:
        subprocess.run(
            ["sudo", "-n", str(LTVM_BIN), "destroy", name],
            check=False, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


@pytest.fixture
def vm_name(request: pytest.FixtureRequest) -> Iterator[Callable[[str], str]]:
    """Yield a factory that mints unique VM names and registers them.

    Usage::

        def test_foo(vm_name):
            name = vm_name()          # 'e2e-test-foo-abc123'
            or:
            name = vm_name("extra")   # 'e2e-test-foo-extra-abc123'

    Every name minted is destroyed in teardown, failure or not.
    Names are prefixed with `e2e-` so they cannot collide with
    hand-run dev VMs.
    """
    nodeid = request.node.nodeid
    # Derive a short, fs-safe base from the test name (not full nodeid).
    base = request.node.name.replace("[", "-").replace("]", "")
    base = "".join(c if c.isalnum() or c == "-" else "-" for c in base)
    # Cap to keep final hostname well under 63 chars.
    base = base[:28].strip("-")
    minted: list[str] = []

    def _mint(tag: str = "") -> str:
        suffix = _rand_suffix()
        parts = [VM_PREFIX.rstrip("-"), base]
        if tag:
            parts.append(tag)
        parts.append(suffix)
        name = "-".join(p for p in parts if p)
        minted.append(name)
        _TEST_VMS.setdefault(nodeid, []).append(name)
        return name

    try:
        yield _mint
    finally:
        for n in minted:
            _destroy(n)


# Map test_nodeid -> list of cluster names the test claimed.
_TEST_CLUSTERS: dict[str, list[str]] = {}


def _cluster_destroy(name: str) -> None:
    """Best-effort `ltvm cluster destroy`.  Never raises.

    Also wipes the .cluster state file directly afterwards so a
    half-broken cluster (e.g. create succeeded but the state file is
    corrupt, or destroy ran but failed to unlink it) doesn't leak
    into the next test's `cluster list`.
    """
    try:
        subprocess.run(
            ["sudo", "-n", str(LTVM_BIN), "cluster", "destroy", name],
            check=False, capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Belt-and-braces: state file is at /opt/qemu-vms/sockets/<n>.cluster.
    try:
        (SOCKETS_DIR / f"{name}.cluster").unlink(missing_ok=True)
    except OSError:
        pass


@pytest.fixture
def cluster_name(
    request: pytest.FixtureRequest,
) -> Iterator[Callable[[], tuple[str, str, str]]]:
    """Yield a factory returning ``(cluster, mds_vm, oss_vm)`` name triples.

    The cluster name is prefixed with ``e2e-clu-`` and the two
    node names are derived from the cluster name so teardown is
    easy to reason about: we tear down the cluster (which destroys
    the constituent VMs), and as an extra safety net the fixture
    explicitly destroys each VM name too in case the cluster state
    file is gone but the VMs still exist.
    """
    nodeid = request.node.nodeid
    base = request.node.name.replace("[", "-").replace("]", "")
    base = "".join(c if c.isalnum() or c == "-" else "-" for c in base)
    base = base[:20].strip("-")
    minted: list[tuple[str, str, str]] = []

    def _mint() -> tuple[str, str, str]:
        suffix = _rand_suffix()
        cname = f"{CLUSTER_PREFIX}{base}-{suffix}"
        mds = f"{cname}-mds"
        oss = f"{cname}-oss"
        minted.append((cname, mds, oss))
        _TEST_CLUSTERS.setdefault(nodeid, []).append(cname)
        # Also register the constituent VMs under the normal VM map
        # so the failure-capture hook grabs their console logs.
        _TEST_VMS.setdefault(nodeid, []).extend([mds, oss])
        return cname, mds, oss

    try:
        yield _mint
    finally:
        for cname, mds, oss in minted:
            _cluster_destroy(cname)
            # Safety: if the cluster destroy missed a node (half-state),
            # _destroy is a no-op on missing VMs.
            _destroy(mds)
            _destroy(oss)


# ---------------------------------------------------------------------------
# ltvm / ssh helpers (module-scope convenience)
# ---------------------------------------------------------------------------


def run_ltvm(
    *args: str,
    check: bool = False,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    """Run the `ltvm` CLI under sudo -n (must be root already).

    We pass -n so any missing sudoers entry fails fast with a clear
    stderr instead of hanging on a password prompt.
    """
    return subprocess.run(
        ["sudo", "-n", str(LTVM_BIN), *args],
        check=check, capture_output=True, text=True, timeout=timeout,
    )


def ssh_run(
    name: str, command: str, timeout: int = 30
) -> tuple[int, str, str]:
    """Run `command` over ssh to the VM.  Returns (rc, stdout, stderr).

    We deliberately do NOT use -q so failure output is visible; we
    suppress known_hosts / host key noise with UserKnownHostsFile and
    LogLevel=ERROR.
    """
    proc = subprocess.run(
        ["ssh", *SSH_OPTS, name, command],
        check=False, capture_output=True, text=True, timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def wait_ssh(name: str, timeout: int = SSH_READY_TIMEOUT) -> None:
    """Poll ssh until it returns rc==0 or `timeout` seconds elapse.

    Fails the test (not the fixture) with pytest.fail() so the error
    travels up as a normal test failure -- which triggers the
    failure-capture hook below.
    """
    deadline = time.monotonic() + timeout
    last_err = ""
    # Brief initial wait -- cmd_create already waits for SSH, so usually
    # we're ready on the first probe; this is defensive.
    while time.monotonic() < deadline:
        rc, _, stderr = ssh_run(name, "true", timeout=5)
        if rc == 0:
            return
        last_err = stderr.strip() or f"rc={rc}"
        time.sleep(2)
    pytest.fail(f"ssh to {name!r} not ready in {timeout}s: {last_err}")


# ---------------------------------------------------------------------------
# Failure-capture hook
# ---------------------------------------------------------------------------


def _safe_copy(src: Path, dst: Path) -> None:
    try:
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    except OSError:
        pass


def _capture_ssh_dump(name: str, dst: Path) -> None:
    """Best-effort: capture networking state from the VM.  Never raises."""
    cmd = (
        "set +e; "
        "echo '== ip -br addr =='; ip -br addr; "
        "echo '== ip link =='; ip link; "
        "echo '== rdma link show =='; rdma link show 2>&1; "
        "echo '== /etc/modprobe.d/lnet.conf =='; "
        "cat /etc/modprobe.d/lnet.conf 2>&1; "
        "echo '== ip -d link show eth1 =='; ip -d link show eth1 2>&1; "
    )
    try:
        rc, out, err = ssh_run(name, cmd, timeout=10)
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(
            f"# rc={rc}\n# stderr:\n{err}\n# stdout:\n{out}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Iterator[None]:
    """On test failure, preserve per-VM artefacts under /tmp/test_e2e/."""
    outcome = yield
    report = outcome.get_result()
    if report.when not in ("setup", "call"):
        return
    if report.passed:
        return

    vms = _TEST_VMS.get(item.nodeid, [])
    if not vms:
        return

    # Sanitise nodeid for path component.
    safe_id = item.nodeid.replace("/", "_").replace("::", "__")
    safe_id = "".join(
        c if c.isalnum() or c in "-._" else "_" for c in safe_id
    )
    base_dir = FAILURE_CAPTURE_ROOT / safe_id

    for vm in vms:
        vm_dir = base_dir / vm
        _safe_copy(SOCKETS_DIR / f"{vm}.log", vm_dir / f"{vm}.log")
        _safe_copy(
            SOCKETS_DIR / f"{vm}.log.failed", vm_dir / f"{vm}.log.failed"
        )
        _safe_copy(SOCKETS_DIR / f"{vm}.info", vm_dir / f"{vm}.info")
        _capture_ssh_dump(vm, vm_dir / "ssh-dump.txt")
        try:
            # Also capture pytest stderr/longrepr.
            longrepr = str(report.longrepr) if report.longrepr else ""
            (vm_dir / "pytest-longrepr.txt").write_text(
                longrepr, encoding="utf-8",
            )
        except OSError:
            pass

    try:
        print(
            f"\n[test_e2e] failure artefacts preserved at {base_dir}",
            flush=True,
        )
    except OSError:
        pass
