"""End-to-end tests for the QEMU microVM lifecycle.

These tests exercise the real vm.py / KVM stack and are excluded
from the normal `make test` run.  Run them with::

    make test-e2e

or directly::

    uv run pytest tests/e2e/ -v --no-cov -m e2e

Each test class creates VMs whose names are prefixed with
``ltvm-e2e-<pid>`` so parallel runs on the same host do not collide.
Teardown fixtures always attempt vm_destroy even when the test fails.
"""

from __future__ import annotations

import json
import os

import pytest

from lib.vmctl import (
    vm_create,
    vm_destroy,
    vm_ensure,
    vm_exec,
    vm_list,
    vm_status,
    vm_stop,
)

# ---------------------------------------------------------------------------
# Module-level name prefix -- unique per process to avoid collisions.
# ---------------------------------------------------------------------------

VM_BASE = f"ltvm-e2e-{os.getpid()}"


def _vm_name(test_name: str) -> str:
    """Build a per-test VM name from the raw pytest node name.

    Strips brackets (parametrize markers) and truncates to keep the
    total name short enough for vm.py.
    """
    safe = test_name.replace("[", "").replace("]", "").replace(" ", "_")
    return f"{VM_BASE}-{safe[:20]}"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vm_name(request: pytest.FixtureRequest) -> str:  # type: ignore[return]
    """Yield a unique VM name and always destroy the VM in teardown."""
    name = _vm_name(request.node.name)
    yield name
    vm_destroy(name)


@pytest.fixture
def running_vm(request: pytest.FixtureRequest) -> str:  # type: ignore[return]
    """Ensure a running VM, yield its name, destroy in teardown."""
    name = _vm_name(request.node.name)
    vm_ensure(name, vcpus=1, mem=1024)
    yield name
    vm_destroy(name)


# ---------------------------------------------------------------------------
# TestVmCreate
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestVmCreate:
    """Basic create / destroy round-trip and duplicate-create guard."""

    def test_create_and_destroy(self, vm_name: str) -> None:
        """Creating a new VM succeeds; destroying it also succeeds."""
        result = vm_create(vm_name, vcpus=1, mem=1024)
        assert result["ok"], (
            f"vm_create failed: rc={result['returncode']} "
            f"output={result['output']}"
        )

        destroy_result = vm_destroy(vm_name)
        assert destroy_result["ok"], (
            f"vm_destroy failed: rc={destroy_result['returncode']} "
            f"output={destroy_result['output']}"
        )

    def test_create_duplicate_fails(self, vm_name: str) -> None:
        """Creating a VM that already exists returns a non-ok result."""
        first = vm_create(vm_name, vcpus=1, mem=1024)
        assert first["ok"], (
            f"First vm_create failed unexpectedly: {first['output']}"
        )

        second = vm_create(vm_name, vcpus=1, mem=1024)
        assert not second["ok"], (
            "Expected second vm_create to fail for an existing VM, "
            f"but got ok=True (rc={second['returncode']})"
        )


# ---------------------------------------------------------------------------
# TestVmEnsure
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestVmEnsure:
    """Idempotent ensure semantics."""

    def test_ensure_creates_vm(self, vm_name: str) -> None:
        """vm_ensure on a non-existent VM creates and starts it."""
        result = vm_ensure(vm_name, vcpus=1, mem=1024)
        assert result["ok"], (
            f"vm_ensure failed: rc={result['returncode']} "
            f"output={result['output']}"
        )

    def test_ensure_idempotent(self, vm_name: str) -> None:
        """Calling vm_ensure twice on the same name both succeed."""
        first = vm_ensure(vm_name, vcpus=1, mem=1024)
        assert first["ok"], f"First vm_ensure failed: {first['output']}"

        second = vm_ensure(vm_name, vcpus=1, mem=1024)
        assert second["ok"], (
            f"Second vm_ensure (idempotent) failed: "
            f"rc={second['returncode']} output={second['output']}"
        )

    def test_ensure_starts_stopped(self, vm_name: str) -> None:
        """vm_ensure brings a stopped VM back up."""
        create_result = vm_ensure(vm_name, vcpus=1, mem=1024)
        assert create_result["ok"], (
            f"Initial vm_ensure failed: {create_result['output']}"
        )

        stop_result = vm_stop(vm_name)
        assert stop_result["ok"], f"vm_stop failed: {stop_result['output']}"

        restart_result = vm_ensure(vm_name, vcpus=1, mem=1024)
        assert restart_result["ok"], (
            f"vm_ensure after stop failed: "
            f"rc={restart_result['returncode']} "
            f"output={restart_result['output']}"
        )


# ---------------------------------------------------------------------------
# TestVmExec
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestVmExec:
    """Remote command execution inside a running VM."""

    def test_echo(self, running_vm: str) -> None:
        """A simple echo command returns 'hello' in the output."""
        result = vm_exec(running_vm, "echo hello")
        assert result["ok"], (
            f"vm_exec echo failed: rc={result['returncode']} "
            f"output={result['output']}"
        )
        assert "hello" in result["output"], (
            f"Expected 'hello' in output, got: {result['output']!r}"
        )

    def test_hostname(self, running_vm: str) -> None:
        """hostname command returns a non-empty string."""
        result = vm_exec(running_vm, "hostname")
        assert result["ok"], (
            f"vm_exec hostname failed: rc={result['returncode']} "
            f"output={result['output']}"
        )
        assert result["output"].strip(), "Expected non-empty hostname output"

    def test_nonexistent_vm(self) -> None:
        """Executing on a VM that does not exist returns not-found (2)
        or unreachable (4) -- never 0."""
        nonexistent = f"{VM_BASE}-nxvm-{os.getpid()}"
        result = vm_exec(nonexistent, "true")
        assert result["returncode"] in (2, 4), (
            f"Expected returncode 2 (not-found) or 4 (unreachable) "
            f"for nonexistent VM, got {result['returncode']}: "
            f"{result['output']}"
        )

    def test_timeout(self, running_vm: str) -> None:
        """A command that exceeds the timeout returns returncode 3."""
        result = vm_exec(running_vm, "sleep 30", timeout=2)
        assert result["returncode"] == 3, (
            f"Expected returncode 3 (timeout), "
            f"got {result['returncode']}: {result['output']}"
        )


# ---------------------------------------------------------------------------
# TestVmStatus
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestVmStatus:
    """vm_status output for running and non-existent VMs."""

    def test_status_running(self, running_vm: str) -> None:
        """Status of a running VM returns ok with non-empty output."""
        result = vm_status(running_vm)
        assert result["ok"], (
            f"vm_status failed: rc={result['returncode']} "
            f"output={result['output']}"
        )
        assert result["output"].strip(), (
            "Expected non-empty status output for a running VM"
        )

    def test_status_json(self, running_vm: str) -> None:
        """JSON status of a running VM returns ok and valid JSON."""
        result = vm_status(running_vm, json_output=True)
        assert result["ok"], (
            f"vm_status --json failed: rc={result['returncode']} "
            f"output={result['output']}"
        )
        try:
            parsed = json.loads(result["output"])
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"vm_status --json returned invalid JSON: {exc}\n"
                f"output={result['output']!r}"
            )
        # The parsed value should be a dict or list (not bare None/int)
        assert isinstance(parsed, (dict, list)), (
            f"Expected JSON object or array, got {type(parsed)}: "
            f"{result['output']!r}"
        )

    def test_status_nonexistent(self) -> None:
        """Status of an unknown VM name returns not-ok."""
        nonexistent = f"{VM_BASE}-nxst-{os.getpid()}"
        result = vm_status(nonexistent)
        assert not result["ok"], (
            f"Expected vm_status to fail for nonexistent VM "
            f"'{nonexistent}', but got ok=True: {result['output']}"
        )


# ---------------------------------------------------------------------------
# TestVmList
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestVmList:
    """vm_list output includes known running VMs."""

    def test_list_contains_vm(self, running_vm: str) -> None:
        """A VM that was ensured appears in the plain-text list output."""
        result = vm_list()
        assert result["ok"], (
            f"vm_list failed: rc={result['returncode']} "
            f"output={result['output']}"
        )
        assert running_vm in result["output"], (
            f"Expected VM name '{running_vm}' in vm_list output:\n"
            f"{result['output']}"
        )

    def test_list_json(self, running_vm: str) -> None:
        """vm_list --json returns ok and valid JSON (list or dict)."""
        result = vm_list(json_output=True)
        assert result["ok"], (
            f"vm_list --json failed: rc={result['returncode']} "
            f"output={result['output']}"
        )
        try:
            parsed = json.loads(result["output"])
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"vm_list --json returned invalid JSON: {exc}\n"
                f"output={result['output']!r}"
            )
        assert isinstance(parsed, (dict, list)), (
            f"Expected JSON object or array from vm_list --json, "
            f"got {type(parsed)}"
        )
