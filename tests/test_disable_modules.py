"""A --disable-modules build stages no .ko, and that is a good build."""

from __future__ import annotations

import ltvm_pkg.lustre_build as lb


class TestConfigureBuildsModules:
    def test_default_builds_modules(self) -> None:
        assert lb.configure_builds_modules(
            "./configure --with-linux=/kernel --disable-server"
        )

    def test_disable_modules_builds_none(self) -> None:
        """`ltvm build lustre --configure='--disable-modules'` built the
        userland, then failed its own check for want of a .ko."""
        assert not lb.configure_builds_modules(
            "./configure --with-linux=/kernel --disable-server"
            ' "--enable-mpitests=$(command -v mpicc || echo no)"'
            " --disable-libpthread --disable-modules"
        )

    def test_the_last_spelling_wins(self) -> None:
        assert lb.configure_builds_modules(
            "./configure --disable-modules --enable-modules"
        )
        assert not lb.configure_builds_modules(
            "./configure --enable-modules --enable-modules=no"
        )
