"""Command implementations for ltvm CLI.

Each cmd_* function takes an argparse.Namespace and returns an int
exit code.  Implementation now lives in per-concern submodules
(util, build, targets, fetch, deploy, cluster, vm, setup); this
package's __init__ re-exports every public name those submodules
expose so ``from ltvm_pkg.cli import cmd_build_all`` keeps working
and attribute-patching tests (``patch.object(cli_mod, "X")``) still
find every name they expect.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from ltvm_pkg import host_setup
from ltvm_pkg.deploy import deploy_to_vm, lustre_mount_vm
from ltvm_pkg.image_build import build_image, image_status
from ltvm_pkg.kernel_build import build_kernel, kernel_status
from ltvm_pkg.lustre_build import (
    build_lustre,
    read_staging_meta,
    staging_path,
)
from ltvm_pkg.lustre_compat import ValidationResult, validate_target
from ltvm_pkg.paths import load_meta_safe
from ltvm_pkg.release_package import (
    fetch_target,
    package_target,
    snapshot_lustre,
)
from ltvm_pkg.target_config import LustreMode, TargetConfig, list_targets

from ltvm_pkg.cli.util import (
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    _artifact_label,
    _container_status,
    _emit_error,
    _error,
    _load_target,
    _load_target_args,
    _maybe_print_traceback,
    _output,
    _print_target_header,
    _qemu_ns,
    _require_root,
)
from ltvm_pkg.cli.build import (
    _do_build_container,
    _gate_lustre_validation,
    _resolve_lustre_tree,
    cmd_build_all,
    cmd_build_container,
    cmd_build_image,
    cmd_build_kernel,
    cmd_build_lustre,
    cmd_build_mofed_kmods,
    cmd_build_shell,
    cmd_clean,
    cmd_status,
)

# GitHub repo for release downloads.  Override with LTVM_GITHUB_REPO
# so a fork can use `ltvm fetch` / `ltvm publish` without editing
# source.
GITHUB_REPO = os.environ.get("LTVM_GITHUB_REPO", "lustre-tools/lustre-test-vms")

# Imported AFTER GITHUB_REPO so fetch.py's module-level _gh_api reads
# the live value (tests flip it via monkeypatch).  fetch uses the
# _cli_attr indirection at call time, so order here is mostly a
# style issue -- but keep it consistent with the "constants first,
# submodule re-exports second" pattern.
from ltvm_pkg.cli.fetch import (  # noqa: E402
    _KVER_PREFIX_RE,
    _RHEL_RE,
    _find_release_url,
    _gh_api,
    _gh_next_link,
    _gh_release_upload,
    _kernel_release_signature,
    _list_releases,
    _release_matches_kernel,
    cmd_delete,
    cmd_fetch,
    cmd_package,
    cmd_publish,
)
from ltvm_pkg.cli.targets import (  # noqa: E402
    _VALIDATE_EXIT,
    _release_status,
    _validation_result_to_dict,
    _variant_suffix_in_tag,
    cmd_target_export,
    cmd_target_show,
    cmd_targets,
    cmd_validate,
)
from ltvm_pkg.cli.vm import (  # noqa: E402
    _vm_call,
    cmd_console_log,
    cmd_crash_collect,
    cmd_list,
    cmd_nmi,
    cmd_restore,
    cmd_snapshot,
    cmd_vm_start,
    cmd_vm_stop,
)
from ltvm_pkg.cli.deploy import (  # noqa: E402
    cmd_deploy,
    cmd_llmount,
)
from ltvm_pkg.cli.cluster import cmd_cluster  # noqa: E402
from ltvm_pkg.cli.setup import (  # noqa: E402
    _current_version,
    _git,
    _ltvm_repo_root,
    cmd_create,
    cmd_destroy,
    cmd_doctor,
    cmd_setup,
    cmd_update,
)
