"""Tests for tab completion: the dynamic completers and the shell
registration that makes a shell call them.

Two halves, matching the two modules:

``ltvm_pkg.completion`` holds the completers argcomplete calls with the
current prefix and a best-effort parse of the command line.  What is
pinned here is that each one reads the right thing out of that parse
(``--kernel`` scoped to the target already typed, a snapshot tag scoped
to the VM), and that none of them can raise -- an exception in a
completer surfaces as a traceback in the user's prompt.

``ltvm_pkg.shell_completion`` holds the per-shell install.  What is
pinned here is the zsh ``#compdef`` wrapping (without which the first
TAB silently does nothing), path selection per shell, and that nothing
is written for a shell the host does not have.

Every test runs under the suite-wide LTVM_COMPLETION_ROOT tmpdir from
conftest, so none of this touches the real /etc.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import completion, shell_completion


def _args(**kw: object) -> argparse.Namespace:
    return argparse.Namespace(**kw)


# ── completers: the happy paths ───────────────────────────


class TestTargetAndArchCompleters:
    def test_targets_come_from_the_registry(self) -> None:
        names = completion.complete_targets()
        assert "rocky9" in names
        assert names == list(dict.fromkeys(names)), "no duplicates"

    def test_arches_are_canonical_not_aliases(self) -> None:
        arches = completion.complete_arches()
        assert arches == ["aarch64", "x86_64"]
        # arm64/amd64 are accepted by the CLI but deliberately not
        # offered: two spellings of one arch reads as four choices.
        assert "arm64" not in arches
        assert "amd64" not in arches


class TestKernelCompleter:
    def test_scoped_to_the_target_already_typed(self) -> None:
        rocky9 = completion.complete_kernels(parsed_args=_args(target="rocky9"))
        assert rocky9
        assert all(k.startswith("5.14-rhel9") for k in rocky9)

    def test_unions_across_targets_before_one_is_typed(self) -> None:
        """`--kernel` can precede the target, so completion must cope."""
        union = completion.complete_kernels(parsed_args=_args(target=None))
        rocky9 = completion.complete_kernels(parsed_args=_args(target="rocky9"))
        assert set(rocky9) <= set(union)
        assert len(union) > len(rocky9)

    def test_fetch_filter_offers_the_same_kernels(self) -> None:
        assert completion.complete_fetch_filter(
            parsed_args=_args(target="rocky9")
        ) == completion.complete_kernels(parsed_args=_args(target="rocky9"))


class TestVariantCompleter:
    def test_includes_base_and_the_declared_variants(self) -> None:
        variants = completion.complete_variants(
            parsed_args=_args(target="rocky9")
        )
        assert "base" in variants
        assert "mofed-24" in variants


class TestVersionCompleters:
    def test_zfs_offers_the_targets_pin_first(self) -> None:
        versions = completion.complete_zfs_versions(
            parsed_args=_args(target="rocky8")
        )
        # rocky8 pins 2.3.4 (2.4 dropped the 4.18 EL8 kernel).
        assert versions[0] == "2.3.4"

    def test_mofed_comes_from_the_variant_params(self) -> None:
        versions = completion.complete_mofed_versions(
            parsed_args=_args(target="rocky9")
        )
        assert versions == ["24.10-2.1.8.0"]


# ── completers: the cluster REMAINDER ─────────────────────


@pytest.fixture
def cluster(tmp_path: Path) -> Path:
    """A two-role cluster on disk for the remainder completer to read."""
    sockets = tmp_path / "sockets"
    sockets.mkdir()
    (sockets / "co2.cluster").write_text(
        '{"name": "co2", "nodes": ['
        '{"name": "co2-mds", "roles": ["mgs", "mds"]},'
        '{"name": "co2-oss1", "roles": ["oss"]}]}'
    )
    with patch("ltvm_pkg.vm_state.SOCKETS", sockets):
        yield sockets


class TestClusterMemberCompleter:
    """`cluster exec` / `ssh` take a role or a single node name.

    Before the subparser conversion one completer served every cluster
    action off a REMAINDER, counting how many tokens had been consumed to
    guess what was being typed.  argparse knows the shape now, so this is
    just "the cluster named so far, its roles then its nodes".
    """

    def test_roles_then_node_names(self, cluster: Path) -> None:
        got = completion.complete_cluster_members(parsed_args=_args(name="co2"))
        assert got == ["mds", "mgs", "oss", "co2-mds", "co2-oss1"]

    def test_nothing_before_a_cluster_is_named(self, cluster: Path) -> None:
        assert (
            completion.complete_cluster_members(parsed_args=_args(name=None))
            == []
        )

    def test_unknown_cluster_yields_nothing(self, cluster: Path) -> None:
        assert (
            completion.complete_cluster_members(parsed_args=_args(name="nope"))
            == []
        )


class TestClusterSpecsCompleter:
    """`cluster create`'s positionals: an optional target, then specs."""

    def test_first_word_offers_targets(self, cluster: Path) -> None:
        got = completion.complete_cluster_specs(
            prefix="roc", parsed_args=_args(specs=["roc"])
        )
        assert "rocky9" in got

    def test_first_word_also_offers_role_prefixes(self, cluster: Path) -> None:
        """`mgs+mds:` is easy to misremember -- the separator is '+', not
        ',' -- and a wrong role is only refused after the whole line."""
        got = completion.complete_cluster_specs(parsed_args=_args(specs=[]))
        for role in completion.CLUSTER_ROLES:
            assert role in got

    def test_past_the_first_word_a_target_is_not_offered(
        self, cluster: Path
    ) -> None:
        got = completion.complete_cluster_specs(
            parsed_args=_args(specs=["rocky9", ""])
        )
        assert "rocky9" not in got
        assert set(got) == set(completion.CLUSTER_ROLES)

    def test_a_word_with_a_colon_is_a_spec_not_a_target(
        self, cluster: Path
    ) -> None:
        got = completion.complete_cluster_specs(
            prefix="mgs:", parsed_args=_args(specs=["mgs:"])
        )
        assert "rocky9" not in got


# ── completers: snapshot tags ─────────────────────────────


class TestSnapshotTagCompleter:
    _QEMU_OUT = (
        "Snapshot list:\n"
        "ID        TAG           VM SIZE    DATE\n"
        "1         before-fix     0 B       2026-01-01\n"
        "2         clean          0 B       2026-01-02\n"
    )

    def test_tags_come_from_the_overlay(self, tmp_path: Path) -> None:
        overlay = tmp_path / "co1.qcow2"
        overlay.write_text("")
        vm = MagicMock(overlay_path=overlay)
        with (
            patch("ltvm_pkg.vm_state.VMInfo.load", return_value=vm),
            patch(
                "subprocess.run",
                return_value=MagicMock(stdout=self._QEMU_OUT, returncode=0),
            ) as run,
        ):
            got = completion.complete_snapshot_tags(
                parsed_args=_args(name="co1")
            )
        assert got == ["before-fix", "clean"]
        # -U is what makes this safe against a *running* VM's disk,
        # which a completer has to be.
        assert "-U" in run.call_args[0][0]

    def test_nothing_before_a_vm_is_named(self) -> None:
        assert (
            completion.complete_snapshot_tags(parsed_args=_args(name=None))
            == []
        )

    def test_nothing_when_the_overlay_is_absent(self, tmp_path: Path) -> None:
        vm = MagicMock(overlay_path=tmp_path / "missing.qcow2")
        with patch("ltvm_pkg.vm_state.VMInfo.load", return_value=vm):
            assert (
                completion.complete_snapshot_tags(parsed_args=_args(name="co1"))
                == []
            )


# ── completers: the blast shield ──────────────────────────


class TestCompletersNeverRaise:
    """An exception in a completer becomes a traceback in the user's
    prompt, so every one of them is wrapped."""

    @pytest.mark.parametrize(
        "name",
        [
            "complete_targets",
            "complete_vms",
            "complete_clusters",
            "complete_kernels",
            "complete_variants",
            "complete_arches",
            "complete_snapshot_tags",
            "complete_zfs_versions",
            "complete_mofed_versions",
            "complete_fetch_filter",
            "complete_cluster_members",
            "complete_cluster_specs",
        ],
    )
    def test_a_broken_registry_yields_no_completions(self, name: str) -> None:
        def boom(*a: object, **kw: object) -> object:
            raise RuntimeError("targets.yaml is shredded")

        with (
            patch("ltvm_pkg.target_config.list_targets", side_effect=boom),
            patch("ltvm_pkg.target_config.TargetConfig", side_effect=boom),
            patch("ltvm_pkg.cross_compile.supported_arches", side_effect=boom),
            patch("ltvm_pkg.vm_state.VMInfo.all_names", side_effect=boom),
            patch("ltvm_pkg.vm_state.VMInfo.load", side_effect=boom),
            patch("ltvm_pkg.vm_state.ClusterInfo.all_names", side_effect=boom),
            patch("ltvm_pkg.vm_state.ClusterInfo.load", side_effect=boom),
        ):
            assert getattr(completion, name)(parsed_args=_args()) == []

    def test_a_namespace_missing_every_attribute_is_fine(self) -> None:
        """argcomplete's parse is best-effort and may lack any dest."""
        for name in (
            "complete_kernels",
            "complete_variants",
            "complete_arches",
        ):
            assert isinstance(getattr(completion, name)(parsed_args=None), list)


# ── shell registration: the generated code ────────────────


class TestShellcode:
    def test_bash_registers_the_completion_function(self) -> None:
        code = shell_completion.shellcode("bash")
        assert "_python_argcomplete" in code
        assert "ltvm" in code

    def test_zsh_is_wrapped_to_complete_on_first_tab(self) -> None:
        """An autoloaded _ltvm's body *is* the completion function.

        Without the #compdef header and the trailing call, the bare
        argcomplete shellcode defines _python_argcomplete and returns --
        so the first TAB does nothing.  Measured in real zsh through a
        pty, completing `ltvm build kernel rocky9-`: wrapped gives
        `rocky9-64k`, bare leaves the line untouched.  Driving zsh needs
        an interactive pty, which is too slow and too fragile for this
        suite, so what is pinned here is the two pieces of structure
        that made the difference.
        """
        code = shell_completion.shellcode("zsh")
        assert code.startswith("#compdef ltvm\n")
        assert code.rstrip().endswith('_python_argcomplete "$@"')

    def test_fish_emits_a_complete_directive(self) -> None:
        code = shell_completion.shellcode("fish")
        assert "complete --command ltvm" in code

    def test_an_unknown_shell_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown shell"):
            shell_completion.shellcode("csh")


class TestDetectShell:
    @pytest.mark.parametrize(
        "shell_env,expected",
        [
            ("/bin/bash", "bash"),
            ("/usr/bin/zsh", "zsh"),
            ("/usr/local/bin/fish", "fish"),
            ("/bin/bash5", "bash"),
            ("/usr/bin/zsh-5.9", "zsh"),
            # Unknown and unset both fall back to bash rather than
            # erroring: being wrong only costs the user a --shell.
            ("/bin/csh", "bash"),
            ("", "bash"),
        ],
    )
    def test_from_shell_env(self, shell_env: str, expected: str) -> None:
        assert (
            shell_completion.detect_shell(
                {"SHELL": shell_env} if shell_env else {}
            )
            == expected
        )


# ── shell registration: where files land ──────────────────


class TestResolveTarget:
    def test_prefers_an_existing_directory(self, tmp_path: Path) -> None:
        (tmp_path / "usr/share/zsh/vendor-completions").mkdir(parents=True)
        target = shell_completion.resolve_target("zsh", root=tmp_path)
        assert target.directory_exists
        assert target.path.name == "_ltvm"
        assert target.path.parent.name == "vendor-completions"

    def test_falls_back_to_the_sysadmin_location(self, tmp_path: Path) -> None:
        """With nothing existing, pick the path a package upgrade will
        not overwrite -- the last candidate."""
        target = shell_completion.resolve_target("fish", root=tmp_path)
        assert not target.directory_exists
        assert target.path == tmp_path / "etc/fish/completions/ltvm.fish"

    def test_filenames_match_each_shells_convention(
        self, tmp_path: Path
    ) -> None:
        names = {
            s: shell_completion.resolve_target(s, root=tmp_path).path.name
            for s in shell_completion.SHELLS
        }
        # zsh's leading underscore is not cosmetic: compinit only scans
        # fpath entries matching _*.
        assert names == {"bash": "ltvm", "zsh": "_ltvm", "fish": "ltvm.fish"}

    def test_env_root_is_used_when_no_root_is_passed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_COMPLETION_ROOT", str(tmp_path))
        assert str(shell_completion.resolve_target("bash").path).startswith(
            str(tmp_path)
        )

    def test_an_explicit_root_beats_the_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_COMPLETION_ROOT", str(tmp_path / "env"))
        other = tmp_path / "explicit"
        assert str(
            shell_completion.resolve_target("bash", root=other).path
        ).startswith(str(other))


# ── shell registration: install / uninstall / status ──────


class TestInstall:
    def test_writes_the_code_for_a_present_shell(self, tmp_path: Path) -> None:
        with patch("shutil.which", return_value="/bin/bash"):
            results = shell_completion.install(("bash",), root=tmp_path)
        assert [r.status for r in results] == ["installed"]
        written = results[0].path.read_text()
        assert written == shell_completion.shellcode("bash")

    def test_skips_a_shell_the_host_does_not_have(self, tmp_path: Path) -> None:
        """Creating a fish completion dir on a host without fish is
        litter, not helpfulness."""
        with patch("shutil.which", return_value=None):
            results = shell_completion.install(("fish",), root=tmp_path)
        assert [r.status for r in results] == ["skipped"]
        assert not results[0].path.exists()

    def test_all_shells_overrides_the_presence_check(
        self, tmp_path: Path
    ) -> None:
        """For staging an image that will be used somewhere else."""
        with patch("shutil.which", return_value=None):
            results = shell_completion.install(root=tmp_path, all_shells=True)
        assert {r.status for r in results} == {"installed"}
        assert len(results) == len(shell_completion.SHELLS)

    def test_a_second_install_is_unchanged_not_rewritten(
        self, tmp_path: Path
    ) -> None:
        with patch("shutil.which", return_value="/bin/bash"):
            shell_completion.install(("bash",), root=tmp_path)
            again = shell_completion.install(("bash",), root=tmp_path)
        assert [r.status for r in again] == ["unchanged"]

    def test_a_stale_file_is_replaced(self, tmp_path: Path) -> None:
        with patch("shutil.which", return_value="/bin/bash"):
            first = shell_completion.install(("bash",), root=tmp_path)
            first[0].path.write_text("# from an older ltvm\n")
            again = shell_completion.install(("bash",), root=tmp_path)
        assert [r.status for r in again] == ["installed"]
        assert again[0].path.read_text() == shell_completion.shellcode("bash")

    def test_a_write_failure_is_reported_not_raised(
        self, tmp_path: Path
    ) -> None:
        """`ltvm install` must not fail over tab completion."""
        with (
            patch("shutil.which", return_value="/bin/bash"),
            patch(
                "ltvm_pkg.priv.atomic_write",
                side_effect=OSError("read-only filesystem"),
            ),
        ):
            results = shell_completion.install(("bash",), root=tmp_path)
        assert [r.status for r in results] == ["failed"]
        assert "read-only" in results[0].detail


class TestStatusAndUninstall:
    def test_missing_then_current(self, tmp_path: Path) -> None:
        with patch("shutil.which", return_value="/bin/sh"):
            before = {
                r.shell: r.status
                for r in shell_completion.status(root=tmp_path)
            }
            assert set(before.values()) == {"missing"}
            shell_completion.install(root=tmp_path)
            after = {
                r.shell: r.status
                for r in shell_completion.status(root=tmp_path)
            }
        assert set(after.values()) == {"current"}

    def test_stale_is_distinguished_from_missing(self, tmp_path: Path) -> None:
        with patch("shutil.which", return_value="/bin/bash"):
            results = shell_completion.install(("bash",), root=tmp_path)
            results[0].path.write_text("# older ltvm\n")
            status = shell_completion.status(root=tmp_path)
        assert [r.status for r in status if r.shell == "bash"] == ["stale"]

    def test_absent_shells_are_not_reported(self, tmp_path: Path) -> None:
        """A missing fish completion on a host without fish is not a
        problem for doctor to nag about."""
        with patch("shutil.which", return_value=None):
            assert shell_completion.status(root=tmp_path) == []

    def test_uninstall_removes_what_install_wrote(self, tmp_path: Path) -> None:
        with patch("shutil.which", return_value="/bin/sh"):
            installed = shell_completion.install(root=tmp_path)
            removed = shell_completion.uninstall(root=tmp_path)
        assert {r.status for r in removed} == {"removed"}
        assert not any(r.path.exists() for r in installed)

    def test_uninstall_sweeps_every_candidate_directory(
        self, tmp_path: Path
    ) -> None:
        """An earlier install may have picked a different directory than
        the one that would be chosen now."""
        stray = tmp_path / "usr/share/bash-completion/completions"
        stray.mkdir(parents=True)
        (stray / "ltvm").write_text("# from an older layout\n")
        removed = shell_completion.uninstall(("bash",), root=tmp_path)
        assert [r.path for r in removed] == [stray / "ltvm"]
        assert not (stray / "ltvm").exists()

    def test_uninstall_on_a_clean_host_does_nothing(
        self, tmp_path: Path
    ) -> None:
        assert shell_completion.uninstall(root=tmp_path) == []


# ── parser wiring ─────────────────────────────────────────


def _walk(parser: argparse.ArgumentParser, path: str = "ltvm") -> dict:
    """Map "<command path>|<option or positional>" -> completer or None."""
    out: dict[str, object] = {}
    subs = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subs.append(action)
            continue
        key = "/".join(action.option_strings) or f"<{action.dest}>"
        out[f"{path}|{key}"] = getattr(action, "completer", None)
    for sub in subs:
        for name, sp in sub.choices.items():
            out.update(_walk(sp, f"{path} {name}"))
    return out


@pytest.fixture(scope="module")
def wiring() -> dict:
    from tests.test_parser_coverage import ltvm as ltvm_mod

    return _walk(ltvm_mod.build_parser())


class TestParserWiring:
    """`_attach_completers` fills completers by option string across the
    whole subparser tree, so these assert the result rather than the
    table -- a new subcommand is covered without touching the table."""

    def test_arch_is_covered_on_every_subcommand(self, wiring: dict) -> None:
        arch = {k: v for k, v in wiring.items() if k.endswith("|--arch")}
        assert len(arch) > 30, "‑-arch comes from the shared `common` parent"
        assert all(v is completion.complete_arches for v in arch.values())

    def test_every_lustre_tree_gets_directory_completion(
        self, wiring: dict
    ) -> None:
        trees = {
            k: v
            for k, v in wiring.items()
            if k.endswith("|--lustre-tree") or k.endswith("|--build")
        }
        assert len(trees) >= 8, "--lustre-tree is declared at many call sites"
        assert all(v is completion.complete_directories for v in trees.values())

    @pytest.mark.parametrize(
        "key,expected",
        [
            # Wired per-command, not by table: `create --kernel` means
            # something else (see below).
            ("ltvm target validate|--kernel", "complete_kernels"),
            ("ltvm build kernel|--kernel", "complete_kernels"),
            ("ltvm target fetch|<filter>", "complete_fetch_filter"),
            ("ltvm vm restore|<tag>", "complete_snapshot_tags"),
            ("ltvm vm snapshot|--delete", "complete_snapshot_tags"),
            ("ltvm cluster exec|<target>", "complete_cluster_members"),
            ("ltvm cluster ssh|<target>", "complete_cluster_members"),
            ("ltvm cluster create|<specs>", "complete_cluster_specs"),
            ("ltvm cluster destroy|<name>", "complete_clusters"),
            ("ltvm cluster deploy|<name>", "complete_clusters"),
            ("ltvm cluster status|<name>", "complete_clusters"),
            ("ltvm cluster exec|<name>", "complete_clusters"),
            ("ltvm cluster ssh|<name>", "complete_clusters"),
            (
                "ltvm cluster deploy|--build/--lustre-tree",
                "complete_directories",
            ),
            ("ltvm make-install|--variant", "complete_variants"),
            ("ltvm make-uninstall|--variant", "complete_variants"),
            ("ltvm make-reinstall|--variant", "complete_variants"),
            ("ltvm build lustre|--zfs-version", "complete_zfs_versions"),
            ("ltvm build image|--mofed-version", "complete_mofed_versions"),
            ("ltvm vm crash-collect|--mod-dir", "complete_directories"),
            ("ltvm vm crash-collect|--outdir", "complete_directories"),
        ],
    )
    def test_specific_arguments(
        self, wiring: dict, key: str, expected: str
    ) -> None:
        assert wiring[key] is getattr(completion, expected)

    def test_create_kernel_and_image_stay_file_paths(
        self, wiring: dict
    ) -> None:
        """`create --kernel` is an explicit kernel *path*, and --image a
        base-image path -- so argcomplete's default FilesCompleter is
        right and a version-name completer would be actively wrong."""
        assert wiring["ltvm create|--kernel"] is None
        assert wiring["ltvm create|--image"] is None

    def test_snapshot_positional_tag_is_not_completed(
        self, wiring: dict
    ) -> None:
        """It names a snapshot being created; existing tags are not it."""
        assert wiring["ltvm vm snapshot|<tag>"] is None

    def test_export_output_and_ssh_key_stay_file_paths(
        self, wiring: dict
    ) -> None:
        assert wiring["ltvm target export|--output/-o"] is None
        assert wiring["ltvm target export|--ssh-key"] is None

    def test_an_explicit_completer_is_not_overridden_by_the_table(
        self,
    ) -> None:
        """The table is a default, not an override."""
        from tests.test_parser_coverage import ltvm as ltvm_mod

        parser = argparse.ArgumentParser()
        action = parser.add_argument("--arch")
        sentinel = object()
        action.completer = sentinel  # type: ignore[attr-defined]
        ltvm_mod._attach_completers(parser)
        assert action.completer is sentinel  # type: ignore[attr-defined]


# ── doctor integration ────────────────────────────────────


class TestDoctorCheck:
    def test_reports_each_shell_that_is_missing(self, tmp_path: Path) -> None:
        from ltvm_pkg import vm_commands

        with (
            patch("shutil.which", return_value="/bin/sh"),
            patch.dict("os.environ", {"LTVM_COMPLETION_ROOT": str(tmp_path)}),
        ):
            issues, notes, failures = vm_commands._check_completion(fix=False)
        assert len(issues) == len(shell_completion.SHELLS)
        assert all("tab completion missing" in i for i in issues)
        assert notes == [] and failures == 0

    def test_fix_installs_and_then_finds_nothing(self, tmp_path: Path) -> None:
        from ltvm_pkg import vm_commands

        with (
            patch("shutil.which", return_value="/bin/sh"),
            patch.dict("os.environ", {"LTVM_COMPLETION_ROOT": str(tmp_path)}),
        ):
            _, notes, failures = vm_commands._check_completion(fix=True)
            assert failures == 0
            assert any("fixed: wrote" in n for n in notes)
            again, _, _ = vm_commands._check_completion(fix=False)
        assert again == []

    def test_a_clean_host_reports_nothing(self, tmp_path: Path) -> None:
        from ltvm_pkg import vm_commands

        with (
            patch("shutil.which", return_value="/bin/sh"),
            patch.dict("os.environ", {"LTVM_COMPLETION_ROOT": str(tmp_path)}),
        ):
            shell_completion.install(root=tmp_path)
            assert vm_commands._check_completion(fix=False) == ([], [], 0)

    def test_an_unwritable_location_counts_as_unfixable(
        self, tmp_path: Path
    ) -> None:
        """doctor --fix must report a failure it could not repair, not
        claim success."""
        from ltvm_pkg import vm_commands

        with (
            patch("shutil.which", return_value="/bin/bash"),
            patch.dict("os.environ", {"LTVM_COMPLETION_ROOT": str(tmp_path)}),
            patch("ltvm_pkg.priv.atomic_write", side_effect=OSError("nope")),
        ):
            _, notes, failures = vm_commands._check_completion(fix=True)
        assert failures == len(shell_completion.SHELLS)
        assert all("FAILED to write" in n for n in notes)


# ── the `ltvm completion` subcommand ──────────────────────


class TestCompletionCommand:
    def _run(self, **kw: object) -> tuple[int, str]:
        from ltvm_pkg.cli.setup import cmd_completion

        defaults: dict[str, object] = {
            "shell": None,
            "install": False,
            "uninstall": False,
            "all_shells": False,
            "json": False,
        }
        defaults.update(kw)
        return cmd_completion(argparse.Namespace(**defaults))

    def test_prints_code_for_the_named_shell(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = self._run(shell="fish")
        out = capsys.readouterr().out
        assert rc == 0
        assert "complete --command ltvm" in out

    def test_stdout_is_only_the_code_so_eval_works(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`eval "$(ltvm completion)"` breaks on any stray commentary."""
        self._run(shell="bash")
        out = capsys.readouterr().out
        assert out.rstrip("\n") == shell_completion.shellcode("bash").rstrip(
            "\n"
        )

    def test_defaults_to_the_detected_shell(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SHELL", "/usr/bin/fish")
        self._run()
        assert "complete --command ltvm" in capsys.readouterr().out

    def test_install_writes_and_uninstall_removes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_COMPLETION_ROOT", str(tmp_path))
        with patch("shutil.which", return_value="/bin/bash"):
            assert self._run(install=True, shell="bash") == 0
            path = shell_completion.resolve_target("bash").path
            assert path.exists()
            assert self._run(uninstall=True, shell="bash") == 0
        assert not path.exists()

    def test_json_carries_the_shell_and_code(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        self._run(shell="zsh", json=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["shell"] == "zsh"
        assert payload["shellcode"].startswith("#compdef ltvm")

    def test_a_failed_install_exits_non_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_COMPLETION_ROOT", str(tmp_path))
        with (
            patch("shutil.which", return_value="/bin/bash"),
            patch("ltvm_pkg.priv.atomic_write", side_effect=OSError("nope")),
        ):
            assert self._run(install=True, shell="bash") != 0


# ── end to end, the way a shell invokes it ────────────────


def _shell_complete(
    line: str, env_extra: dict[str, str] | None = None
) -> list[str]:
    """Ask `ltvm` to complete *line* exactly as the shellcode does.

    argcomplete is driven entirely through the environment and writes
    its answer to fd 8, so this goes through a real subprocess: it is
    the only way to cover the `argcomplete.autocomplete(parser)` call in
    main() and the PYTHON_ARGCOMPLETE_OK marker together.
    """
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env.update(
        _ARGCOMPLETE="1",
        _ARGCOMPLETE_IFS="\n",
        _ARGCOMPLETE_SHELL="bash",
        _ARGCOMPLETE_COMP_WORDBREAKS=" \t\n\"'><=;|&(:",
        COMP_LINE=line,
        COMP_POINT=str(len(line)),
        COMP_TYPE="9",
    )
    env.update(env_extra or {})
    repo = Path(__file__).parent.parent
    r = subprocess.run(
        ["bash", "-c", f"{sys.executable} ./ltvm 8>&1 1>/dev/null 2>/dev/null"],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(repo),
        timeout=60,
    )
    # A unique match comes back with a trailing space -- argcomplete
    # telling the shell the word is finished.  Irrelevant here.
    return [w.rstrip() for w in r.stdout.split("\n") if w.strip()]


@pytest.mark.parametrize(
    "line,expected",
    [
        # Subcommands, including the one this change adds.
        ("ltvm comp", ["completion"]),
        # A target positional.
        ("ltvm build kernel rocky9-", ["rocky9-64k"]),
        # --arch, which had no completer at all before.
        ("ltvm build image rocky9 --arch aa", ["aarch64"]),
        # --kernel scoped by the target already on the line.
        ("ltvm build kernel rocky8 --kernel 4.", ["4.18-rhel8.10"]),
        # A shell-word value behind =, which argcomplete quotes itself.
        ("ltvm completion --shell z", ["zsh"]),
    ],
)
def test_end_to_end_completion(line: str, expected: list[str]) -> None:
    got = _shell_complete(line)
    for want in expected:
        assert want in got, f"{line!r} -> {got}"


def test_end_to_end_kernel_is_scoped_to_the_target() -> None:
    """The scoping is the point of complete_kernels; prove it survives
    the real argcomplete round trip and not just a direct call."""
    rocky8 = _shell_complete("ltvm build kernel rocky8 --kernel ")
    rocky9 = _shell_complete("ltvm build kernel rocky9 --kernel ")
    assert any(k.startswith("4.18") for k in rocky8)
    assert not any(k.startswith("5.14") for k in rocky8)
    assert any(k.startswith("5.14") for k in rocky9)


def test_end_to_end_completion_records_no_telemetry(tmp_path: Path) -> None:
    """Completion runs on every TAB.  If it recorded telemetry or ran the
    update check, a user holding TAB would both spam the counters and
    `git ls-remote` in a loop."""
    state = tmp_path / "state"
    got = _shell_complete(
        "ltvm build kernel rocky9 --kernel ",
        {
            "XDG_STATE_HOME": str(state),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "LTVM_TELEMETRY": "1",
        },
    )
    assert got, "sanity: completion produced something"
    counters = list(state.rglob("*.json")) if state.exists() else []
    assert counters == [], f"completion wrote telemetry state: {counters}"


# ── `ltvm install --verify` reporting ──────────────────────


class TestVerifyReporting:
    """`install --verify` answers "did my install take?", and install now
    sets up completion -- so it reports completion too.  Deliberately not
    part of `all_ok`: a file gone stale across a version bump is normal
    and self-heals on the next install, and failing the exit code over
    that would cry wolf.  `ltvm doctor` is what exits non-zero."""

    # verify() itself probes QEMU, /dev/kvm, the bridge and dnsmasq, and
    # has no unit test in this repo for that reason -- its completion
    # block is just shell_completion.status(), covered above.  What is
    # worth pinning is the rendering, which is where a wrong status would
    # actually mislead someone.

    def test_stale_is_surfaced_as_a_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from ltvm_pkg.host_setup import print_verify
        from tests.test_setup import _all_ok_result

        r = _all_ok_result()
        r["completion"] = {
            "bash": {"status": "stale", "path": "/etc/bash_completion.d/ltvm"}
        }
        print_verify(r)
        out = capsys.readouterr().out
        assert "tab completion (bash): stale" in out
        assert "ltvm doctor --fix" in out

    def test_current_shells_are_listed_on_one_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from ltvm_pkg.host_setup import print_verify
        from tests.test_setup import _all_ok_result

        r = _all_ok_result()
        r["completion"] = {
            s: {"status": "current", "path": f"/x/{s}"}
            for s in ("bash", "zsh", "fish")
        }
        print_verify(r)
        assert "tab completion: bash, fish, zsh" in capsys.readouterr().out

    def test_no_supported_shell_is_not_a_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from ltvm_pkg.host_setup import print_verify
        from tests.test_setup import _all_ok_result

        r = _all_ok_result()
        r["completion"] = {}
        print_verify(r)
        out = capsys.readouterr().out
        assert "no supported shell found" in out
        assert "WARNING: tab completion" not in out

    def test_a_result_without_the_key_still_prints(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An older cached result, or a caller that built its own."""
        from ltvm_pkg.host_setup import print_verify
        from tests.test_setup import _all_ok_result

        print_verify(_all_ok_result())
        assert "All checks passed." in capsys.readouterr().out
