"""VM claims: which session is using a VM (ltvm_pkg.vm_claim)."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ltvm_pkg import vm_claim, vm_commands
from ltvm_pkg.cli.claim import cmd_claim, cmd_release


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch):
    """Act as agent session *owner* whose process is *pid*."""

    def act_as(owner: str | None, pid: int | None = None) -> None:
        for var in ("LTVM_OWNER_ID", "LTVM_OWNER_PID"):
            monkeypatch.delenv(var, raising=False)
        if owner is not None:
            monkeypatch.setenv("LTVM_OWNER_ID", owner)
        if pid is not None:
            monkeypatch.setenv("LTVM_OWNER_PID", str(pid))

    return act_as


@pytest.fixture
def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class TestIdentity:
    def test_explicit_then_env_then_claude_then_user(self) -> None:
        env = {"LTVM_OWNER_ID": "ci:1", "CLAUDE_CODE_SESSION_ID": "abc"}
        assert vm_claim.current_owner("me", environ=env) == "me"
        assert vm_claim.current_owner(environ=env) == "ci:1"
        assert (
            vm_claim.current_owner(environ={"CLAUDE_CODE_SESSION_ID": "abc"})
            == "claude:abc"
        )
        assert vm_claim.current_owner(environ={}).startswith("user:")

    def test_pid_from_env_or_claude_session(self) -> None:
        assert vm_claim.current_pid(environ={"LTVM_OWNER_PID": "42"}) == 42
        claude = {"CLAUDE_CODE_SESSION_ID": "abc", "CLAUDE_PID": "7"}
        assert vm_claim.current_pid(environ=claude) == 7
        # CLAUDE_PID alone is not a session.
        assert vm_claim.current_pid(environ={"CLAUDE_PID": "7"}) is None
        assert vm_claim.current_pid(environ={"LTVM_OWNER_PID": "x"}) is None

    @pytest.mark.parametrize(
        ("text", "secs"),
        [("90", 90), ("30m", 1800), ("4h", 14400), ("2d", 172800)],
    )
    def test_parse_ttl(self, text: str, secs: int) -> None:
        assert vm_claim.parse_ttl(text) == secs

    @pytest.mark.parametrize("text", ["", "0", "4w", "-1h", "h"])
    def test_parse_ttl_rejects(self, text: str) -> None:
        with pytest.raises(ValueError):
            vm_claim.parse_ttl(text)


class TestClaims:
    def test_claim_creates_dir_and_file(self, session) -> None:
        vm_claim.CLAIMS_DIR.rmdir()
        session("a", os.getpid())
        c, replaced = vm_claim.claim("vm1", tree="/src/x")
        assert replaced is None
        assert vm_claim.claims_dir_ok()
        st = (vm_claim.CLAIMS_DIR / "vm1").stat()
        assert st.st_mode & 0o777 == 0o666
        loaded = vm_claim.load("vm1")
        assert loaded is not None
        assert (loaded.owner, loaded.pid, loaded.tree) == (
            "a",
            os.getpid(),
            "/src/x",
        )
        assert loaded.live()

    def test_live_claim_refuses_other_owner(self, session) -> None:
        session("a", os.getpid())
        vm_claim.claim("vm1")
        session("b", os.getpid())
        with pytest.raises(vm_claim.ClaimError, match="claimed by owner a"):
            vm_claim.claim("vm1")
        with pytest.raises(vm_claim.ClaimError):
            vm_claim.check("vm1", "deploy to")
        with pytest.raises(vm_claim.ClaimError):
            vm_claim.release("vm1")

    def test_same_owner_reclaims_and_keeps_since(self, session) -> None:
        session("a", os.getpid())
        first, _ = vm_claim.claim("vm1", tree="/t")
        again, replaced = vm_claim.claim("vm1")
        assert replaced is None
        assert again.since == first.since
        assert again.tree == "/t"
        vm_claim.check("vm1", "deploy to")

    def test_force_breaks_live_claim(self, session) -> None:
        session("a", os.getpid())
        vm_claim.claim("vm1")
        session("b")
        new, replaced = vm_claim.claim("vm1", force=True)
        assert replaced is not None and replaced.owner == "a"
        assert new.owner == "b"

    def test_dead_pid_claim_is_stale(self, session, dead_pid: int) -> None:
        session("a", dead_pid)
        vm_claim.claim("vm1")
        session("b")
        vm_claim.check("vm1", "deploy to")
        new, replaced = vm_claim.claim("vm1")
        assert replaced is not None and not replaced.live()
        assert new.owner == "b"

    def test_reused_pid_is_stale(self, session) -> None:
        session("a", os.getpid())
        with patch.object(vm_claim, "_pid_start", return_value="then"):
            vm_claim.claim("vm1")
        c = vm_claim.load("vm1")
        assert c is not None
        with patch.object(vm_claim, "_pid_start", return_value="now"):
            assert not c.live()

    def test_ttl_expires(self, session) -> None:
        session("a")
        c, _ = vm_claim.claim("vm1", ttl=60)
        assert c.expires is not None
        assert c.live(now=c.expires - 1)
        assert not c.live(now=c.expires)

    def test_release_and_forget_empty_the_file(self, session) -> None:
        session("a", os.getpid())
        vm_claim.claim("vm1")
        assert vm_claim.release("vm1") is not None
        assert vm_claim.load("vm1") is None
        assert vm_claim.release("vm1") is None
        vm_claim.claim("vm1")
        vm_claim.forget("vm1")
        assert vm_claim.load("vm1") is None
        # The file stays: another user may not recreate it in a 1777 dir.
        assert (vm_claim.CLAIMS_DIR / "vm1").exists()

    def test_symlinked_claim_file_refused(
        self, session, tmp_path: Path
    ) -> None:
        vm_claim.ensure_claims_dir()
        target = tmp_path / "victim"
        target.write_text("keep")
        (vm_claim.CLAIMS_DIR / "vm1").symlink_to(target)
        session("a")
        with pytest.raises(vm_claim.ClaimError, match="symlink"):
            vm_claim.claim("vm1")
        assert target.read_text() == "keep"

    def test_bad_vm_name(self, session) -> None:
        session("a")
        with pytest.raises(vm_claim.ClaimError, match="invalid VM name"):
            vm_claim.claim("../etc/passwd")

    def test_missing_dir_is_unclaimed(self) -> None:
        vm_claim.CLAIMS_DIR.rmdir()
        assert vm_claim.load("vm1") is None
        assert vm_claim.all_claims() == {}
        vm_claim.check("vm1", "deploy to")


class TestAutoClaim:
    def test_user_is_never_auto_claimed(self, session) -> None:
        session(None)
        assert vm_claim.auto_claim("vm1") is None
        assert vm_claim.load("vm1") is None

    def test_session_auto_claims(self, session) -> None:
        session("a", os.getpid())
        c = vm_claim.auto_claim("vm1", "/src/t")
        assert c is not None and c.tree == "/src/t"

    def test_claude_session_auto_claims(self, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
        c = vm_claim.auto_claim("vm1")
        assert c is not None
        assert (c.owner, c.pid) == ("claude:s1", os.getpid())

    def test_unwritable_store_only_warns(self, session, capsys) -> None:
        session("a")
        with patch.object(
            vm_claim,
            "ensure_claims_dir",
            side_effect=vm_claim.ClaimError("nope"),
        ):
            assert vm_claim.auto_claim("vm1") is None
        assert "not claiming vm1" in capsys.readouterr().err


class TestGates:
    def test_require_exits_for_other_session(self, session, capsys) -> None:
        session("a", os.getpid())
        vm_claim.claim("vm1")
        session("b")
        with pytest.raises(SystemExit) as e:
            vm_claim.require("vm1", "stop")
        assert e.value.code == 1
        assert "refusing to stop" in capsys.readouterr().err

    def test_require_all_checks_before_any(self, session) -> None:
        session("a", os.getpid())
        vm_claim.claim("vm2")
        session("b")
        with pytest.raises(SystemExit):
            vm_claim.require_all(["vm1", "vm2"], "deploy to")

    def test_manageable_gate_refuses_claimed_vm(self, session) -> None:
        session("a", os.getpid())
        vm_claim.claim("vm1")
        session("b")

        class _VM:
            name = "vm1"
            info_path = Path("/nonexistent")

        with pytest.raises(SystemExit):
            vm_commands._require_manageable(_VM(), "destroy")  # type: ignore[arg-type]

    def test_list_field(self) -> None:
        assert vm_commands._claim_field(None) == ""
        assert (
            vm_commands._claim_field({"owner": "a", "live": True})
            == " claimed=a"
        )
        assert vm_commands._claim_field({"owner": "a", "live": False}) == (
            " claimed=a(stale)"
        )


class TestCli:
    def _ns(self, **kw: object) -> argparse.Namespace:
        base: dict[str, object] = {
            "names": [],
            "json": False,
            "ttl": None,
            "tree": None,
            "owner": None,
            "pid": None,
            "force": False,
        }
        base.update(kw)
        return argparse.Namespace(**base)

    def test_claim_list_release(self, session, capsys) -> None:
        session("a", os.getpid())
        with patch("ltvm_pkg.vm_state.VMInfo.all_names", return_value=["vm1"]):
            assert cmd_claim(self._ns(names=["vm1"], ttl="1h")) == 0
            assert cmd_claim(self._ns(names=["nope"])) != 0
        assert cmd_claim(self._ns()) == 0
        out = capsys.readouterr().out
        assert "claimed vm1" in out and "vm1" in out.splitlines()[-1]
        assert cmd_release(self._ns(names=["vm1"])) == 0
        assert "released vm1" in capsys.readouterr().out

    def test_release_refuses_other_session(self, session) -> None:
        session("a", os.getpid())
        vm_claim.claim("vm1")
        session("b")
        assert cmd_release(self._ns(names=["vm1"])) != 0
        assert cmd_release(self._ns(names=["vm1"], force=True)) == 0
        assert vm_claim.load("vm1") is None


def test_doctor_check_creates_dir_and_clears_stale(session, dead_pid, capsys):
    vm_claim.CLAIMS_DIR.rmdir()
    found, failed = vm_commands._check_claims(fix=False)
    assert (found, failed) == (1, 0)
    found, failed = vm_commands._check_claims(fix=True)
    assert failed == 0 and vm_claim.claims_dir_ok()
    session("a", dead_pid)
    vm_claim.claim("vm1")
    assert vm_commands._check_claims(fix=True) == (0, 0)
    assert "stale claim: vm1" in capsys.readouterr().out
    assert vm_claim.load("vm1") is None


class TestReviewFixes:
    def test_existing_file_opened_without_o_creat(self, session) -> None:
        """fs.protected_regular refuses O_CREAT on another user's file."""
        session("a", os.getpid())
        vm_claim.claim("vm1")
        real_open = os.open
        flags_seen: list[int] = []

        def spy(path, flags, *a):  # type: ignore[no-untyped-def]
            flags_seen.append(flags)
            return real_open(path, flags, *a)

        session("b")
        with patch("ltvm_pkg.vm_claim.os.open", side_effect=spy):
            vm_claim.claim("vm1", force=True)
        assert flags_seen and not any(f & os.O_CREAT for f in flags_seen)

    def test_auto_claim_raises_when_another_session_won(self, session) -> None:
        session("a", os.getpid())
        vm_claim.claim("vm1")
        session("b", os.getpid())
        with pytest.raises(vm_claim.ClaimHeld):
            vm_claim.auto_claim("vm1")

    def test_pid_start_pins_locale_and_zone(self) -> None:
        with patch("ltvm_pkg.vm_claim.subprocess.run") as run:
            run.return_value.stdout = "Fri Sep 25 08:26:03 2026\n"
            assert vm_claim._pid_start(1) == "Fri Sep 25 08:26:03 2026"
        env = run.call_args.kwargs["env"]
        assert (env["LC_ALL"], env["TZ"]) == ("C", "UTC0")
