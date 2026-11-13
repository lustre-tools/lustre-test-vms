"""The configure cache: replaying a cached config.status on a fresh tree.

The shell runs for real here, against a stand-in configure whose
config.status writes config.h the way autoconf's does: from its S[] and
D[] tables.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ltvm_pkg import lustre_build

# A stand-in for configure: counts its runs and writes a config.status
# carrying the version and the four numbers lustre-version.m4 derives.
FAKE_CONFIGURE = r"""
echo run >> "$RUNS"
v=$(sed -n "s/^PACKAGE_VERSION='\(.*\)'$/\1/p" configure)
set -- $(echo "$v" | sed -re 's/([0-9]+)\.([0-9]+)\.([0-9]+).*/\1 \2 \3/')
fix=$(echo "$v" | sed -re 's/([0-9]+)\.([0-9]+)\.([0-9]+)([-\._][a-z]*([0-9]+))?.*/\5/')
cat > config.status <<EOS
#!/bin/sh
sed -n -e 's/^D\\["\(.*\)"\\]=" \(.*\)"\$/#define \1 \2/p' \\
  -e 's/^S\\["PACKAGE_VERSION"\\]="\(.*\)"\$/#define VERSION "\1"/p' "\$0" > config.h
exit 0
S["PACKAGE_VERSION"]="$v"
S["PACKAGE_STRING"]="Lustre $v"
D["LUSTRE_MAJOR"]=" $1"
D["LUSTRE_MINOR"]=" $2"
D["LUSTRE_PATCH"]=" $3"
D["LUSTRE_FIX"]=" ${fix:-0}"
EOS
chmod +x config.status
./config.status
"""


def _tree(root: Path, name: str, version: str, body: str = "probes") -> Path:
    tree = root / name
    tree.mkdir()
    (tree / "configure").write_text(
        f"PACKAGE_VERSION='{version}'\n# {body} for Lustre {version}\n"
    )
    return tree


def _configure(
    tree: Path, cache: Path, runs: Path, key: str | None = "k1"
) -> str:
    with patch.object(lustre_build, "CONFIGURE_CACHE_DIR", str(cache)):
        block = lustre_build.configure_cache_block(FAKE_CONFIGURE, key)
    r = subprocess.run(
        ["bash", "-c", f"set -e\nLTVER=lt-2.4.7\n{block}"],
        cwd=tree,
        env={**os.environ, "RUNS": str(runs)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def _defines(tree: Path) -> dict[str, str]:
    out = {}
    for line in (tree / "config.h").read_text().splitlines():
        _, name, value = line.split(" ", 2)
        out[name] = value
    return out


@pytest.fixture
def env(tmp_path: Path) -> tuple[Path, Path, Path]:
    return tmp_path, tmp_path / "cache", tmp_path / "runs"


def test_a_fresh_tree_at_another_version_replays(env) -> None:
    root, cache, runs = env
    _configure(_tree(root, "a", "2.17.52_2_g2832d4f"), cache, runs)
    assert runs.read_text().count("run") == 1
    b = _tree(root, "b", "2.17.61_79_g289d62e")
    out = _configure(b, cache, runs)
    assert "replayed cached results" in out
    assert runs.read_text().count("run") == 1  # configure did not run
    assert _defines(b) == {
        "LUSTRE_MAJOR": "2",
        "LUSTRE_MINOR": "17",
        "LUSTRE_PATCH": "61",
        "LUSTRE_FIX": "79",
        "VERSION": '"2.17.61_79_g289d62e"',
    }
    assert "2832d4f" not in (b / "config.status").read_text()


def test_a_tagged_version_gets_fix_zero(env) -> None:
    root, cache, runs = env
    _configure(_tree(root, "a", "2.17.52_2_g2832d4f"), cache, runs)
    b = _tree(root, "b", "2.18.0")
    _configure(b, cache, runs)
    assert runs.read_text().count("run") == 1
    assert _defines(b)["LUSTRE_FIX"] == "0"
    assert _defines(b)["LUSTRE_MINOR"] == "18"


def test_a_changed_configure_runs_for_real(env) -> None:
    root, cache, runs = env
    _configure(_tree(root, "a", "2.17.52_2_g2832d4f"), cache, runs)
    b = _tree(root, "b", "2.17.61_79_g289d62e", body="a new probe")
    out = _configure(b, cache, runs)
    assert "replayed" not in out
    assert runs.read_text().count("run") == 2
    assert _defines(b)["VERSION"] == '"2.17.61_79_g289d62e"'


def test_another_key_base_runs_for_real(env) -> None:
    """Another kernel, image or set of flags: no replay."""
    root, cache, runs = env
    _configure(_tree(root, "a", "2.17.52_2_g2832d4f"), cache, runs, key="k1")
    _configure(_tree(root, "b", "2.17.52_2_g2832d4f"), cache, runs, key="k2")
    assert runs.read_text().count("run") == 2


def test_no_key_base_is_plain_configure(env) -> None:
    root, cache, runs = env
    _configure(_tree(root, "a", "2.17.52_2_g2832d4f"), cache, runs, key=None)
    assert not cache.exists()
    _configure(_tree(root, "b", "2.17.52_2_g2832d4f"), cache, runs, key=None)
    assert runs.read_text().count("run") == 2


def test_a_corrupt_entry_falls_back(env) -> None:
    """A cached config.status whose numbers do not match its own version
    is not Lustre's derivation; configure runs instead."""
    root, cache, runs = env
    _configure(_tree(root, "a", "2.17.52_2_g2832d4f"), cache, runs)
    (entry,) = cache.glob("*.status")
    entry.write_text(
        entry.read_text().replace('LUSTRE_PATCH"]=" 52"', 'LUSTRE_PATCH"]=" 9"')
    )
    b = _tree(root, "b", "2.17.61_79_g289d62e")
    out = _configure(b, cache, runs)
    assert "replayed" not in out
    assert runs.read_text().count("run") == 2
    assert _defines(b)["LUSTRE_PATCH"] == "61"


def test_an_unwritable_cache_still_configures(env) -> None:
    root, cache, runs = env
    cache.parent.mkdir(exist_ok=True)
    cache.write_text("not a directory")
    a = _tree(root, "a", "2.17.52_2_g2832d4f")
    _configure(a, cache, runs)
    assert _defines(a)["VERSION"] == '"2.17.52_2_g2832d4f"'


def test_off_switch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LTVM_CONFIGURE_CACHE", "0")
    assert (
        lustre_build.configure_cache_key_base(
            cfg_hash="x",
            build_tree=tmp_path,
            container_tag="t",
            target="rocky9",
            arch="x86_64",
        )
        is None
    )


def test_key_base_covers_the_kernel(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LTVM_CONFIGURE_CACHE", raising=False)
    (tmp_path / ".config").write_text("CONFIG_X=y\n")
    (tmp_path / "Module.symvers").write_text("0x1 sym vmlinux EXPORT_SYMBOL\n")
    with patch("ltvm_pkg.podman_gc.image_id", return_value="sha256:img"):

        def key() -> str | None:
            return lustre_build.configure_cache_key_base(
                cfg_hash="x",
                build_tree=tmp_path,
                container_tag="t",
                target="rocky9",
                arch="x86_64",
            )

        first = key()
        (tmp_path / "Module.symvers").write_text(
            "0x2 other vmlinux EXPORT_SYMBOL\n"
        )
        assert first and key() != first
    with patch("ltvm_pkg.podman_gc.image_id", return_value=None):
        assert key() is None
