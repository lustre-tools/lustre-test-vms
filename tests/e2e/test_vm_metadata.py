"""End-to-end tests for VM metadata tracking.

Verifies that created/last_boot/last_deploy timestamps and
build_path/kver/os_id are persisted in the .info file and
surfaced in status/list output.

Run with::

    LTVM_LUSTRE_TREE=~/code_shared/master_checkouts/1 \\
        uv run pytest tests/e2e/test_vm_metadata.py -v --no-cov -m e2e
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from lib.vmctl import (
    deploy,
    vm_destroy,
    vm_ensure,
    vm_status,
    vm_stop,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VM_BASE = f"ltvm-e2e-meta-{os.getpid()}"


def _vm_name(suffix: str) -> str:
    safe = suffix.replace("[", "").replace("]", "").replace(" ", "_")
    return f"{VM_BASE}-{safe[:16]}"


def _find_lustre_tree() -> Path | None:
    override = os.environ.get("LTVM_LUSTRE_TREE")
    if not override:
        return None
    p = Path(override).expanduser().resolve()
    if not p.is_dir():
        return None
    if not any(f for f in p.rglob("*.ko") if "kconftest" not in str(f)):
        return None
    return p


LUSTRE_TREE = _find_lustre_tree()

skip_no_tree = pytest.mark.skipif(
    LUSTRE_TREE is None,
    reason="LTVM_LUSTRE_TREE not set or tree has no .ko files",
)


# ---------------------------------------------------------------------------
# TestCreateMetadata
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestCreateMetadata:
    """Metadata fields are set when a VM is created."""

    def test_created_timestamp(self) -> None:
        """created is set to a recent epoch in the status JSON."""
        name = _vm_name("created-ts")
        before = int(time.time())
        try:
            result = vm_ensure(name, vcpus=1, mem=1024)
            assert result["ok"], f"ensure failed: {result['output']}"

            result = vm_status(name, json_output=True)
            assert result["ok"], f"status failed: {result['output']}"
            data = json.loads(result["output"])

            assert data["created"] >= before, (
                f"created={data['created']} is before test start={before}"
            )
            assert data["created"] <= int(time.time()), (
                f"created={data['created']} is in the future"
            )
        finally:
            vm_destroy(name)

    def test_last_boot_set_on_create(self) -> None:
        """last_boot is set when a VM is created (first boot)."""
        name = _vm_name("boot-create")
        before = int(time.time())
        try:
            result = vm_ensure(name, vcpus=1, mem=1024)
            assert result["ok"], f"ensure failed: {result['output']}"

            result = vm_status(name, json_output=True)
            assert result["ok"], f"status failed: {result['output']}"
            data = json.loads(result["output"])

            assert data["last_boot"] >= before, (
                f"last_boot={data['last_boot']} not set on create"
            )
        finally:
            vm_destroy(name)

    def test_last_boot_updates_on_restart(self) -> None:
        """last_boot updates when a stopped VM is restarted."""
        name = _vm_name("boot-restart")
        try:
            result = vm_ensure(name, vcpus=1, mem=1024)
            assert result["ok"], f"ensure failed: {result['output']}"

            result = vm_status(name, json_output=True)
            assert result["ok"]
            first_boot = json.loads(result["output"])["last_boot"]

            # Stop and restart
            time.sleep(1)
            vm_stop(name)
            time.sleep(1)
            before_restart = int(time.time())
            vm_ensure(name, vcpus=1, mem=1024)

            result = vm_status(name, json_output=True)
            assert result["ok"]
            second_boot = json.loads(result["output"])["last_boot"]

            assert second_boot >= before_restart, (
                f"last_boot not updated on restart: "
                f"first={first_boot} second={second_boot}"
            )
        finally:
            vm_destroy(name)

    def test_os_id_set(self) -> None:
        """os_id is set based on the base image."""
        name = _vm_name("os-id")
        try:
            result = vm_ensure(name, vcpus=1, mem=1024)
            assert result["ok"], f"ensure failed: {result['output']}"

            result = vm_status(name, json_output=True)
            assert result["ok"]
            data = json.loads(result["output"])
            assert data["os_id"] in ("rocky9", "rocky8", "ubuntu24"), (
                f"Unexpected os_id: {data['os_id']}"
            )
            assert data["base_image"], "base_image should be set"
        finally:
            vm_destroy(name)

    def test_no_deploy_metadata_before_deploy(self) -> None:
        """last_deploy/build_path/kver are empty before any deploy."""
        name = _vm_name("no-deploy")
        try:
            result = vm_ensure(name, vcpus=1, mem=1024)
            assert result["ok"]

            result = vm_status(name, json_output=True)
            assert result["ok"]
            data = json.loads(result["output"])
            assert data["last_deploy"] == 0
            assert data["build_path"] == ""
            assert data["kver"] == ""
        finally:
            vm_destroy(name)


# ---------------------------------------------------------------------------
# TestDeployMetadata
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@skip_no_tree
class TestDeployMetadata:
    """Deploy metadata is recorded after a successful deploy."""

    def test_deploy_sets_metadata(self) -> None:
        """After deploy, last_deploy/build_path/kver are populated."""
        assert LUSTRE_TREE is not None
        name = _vm_name("deploy-meta")
        before = int(time.time())
        try:
            result = vm_ensure(
                name, vcpus=2, mem=4096, mdt_disks=1, ost_disks=1
            )
            assert result["ok"], f"ensure failed: {result['output']}"

            result = deploy(name, build_path=LUSTRE_TREE, mount=False)
            assert result["ok"], (
                f"deploy failed: rc={result['returncode']}\n{result['output']}"
            )

            result = vm_status(name, json_output=True)
            assert result["ok"]
            data = json.loads(result["output"])

            assert data["last_deploy"] >= before, (
                f"last_deploy={data['last_deploy']} not set after deploy"
            )
            assert str(LUSTRE_TREE) in data["build_path"], (
                f"build_path={data['build_path']} doesn't match "
                f"LUSTRE_TREE={LUSTRE_TREE}"
            )
            assert data["kver"], "kver should be set after deploy"
        finally:
            vm_destroy(name)
