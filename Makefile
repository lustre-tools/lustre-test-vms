.PHONY: lint fix format typecheck test test-fast test-e2e test-sh coverage check hooks install uninstall

# Lint only (report errors, don't fix)
lint:
	uv run ruff check .
	uv run ruff format --check .

# Auto-fix lint errors + format
fix:
	uv run ruff check --fix .
	uv run ruff format .

# Format only (no lint fixes)
format:
	uv run ruff format .

# Type check
typecheck:
	uv run mypy ltvm ltvm_pkg/

# Tests run in parallel (pytest-xdist): one worker per core, at most
# eight, past which this suite gets no faster.  A plain `uv run pytest`
# stays serial, which suits running one file.
PYTEST_PARALLEL = -n auto --maxprocesses=8

# Run tests (with coverage summary in terminal)
test:
	uv run pytest $(PYTEST_PARALLEL)

# Run tests without coverage (faster iteration during development)
test-fast:
	uv run pytest $(PYTEST_PARALLEL) --no-cov -q

# Run end-to-end tests (requires root, KVM, and a built rocky9 image).
# These drive the real `ltvm` binary; they skip cleanly when the host
# can't host them.  NB: tests/test_e2e/ is the live suite -- tests/e2e/
# holds the three root-filesystem scripts, which write to / and must
# only be run inside a throwaway machine (see CLAUDE.md).
test-e2e:
	uv run pytest tests/test_e2e/ -v --no-cov

# Shell-level tests for the LNet setup scripts baked into every image.
# Plain bash, no pytest -- 44 assertions over targets/common/setup-*.sh.
test-sh:
	@for t in tests/test_setup_*.sh; do \
		echo "== $$t"; bash "$$t" || exit 1; \
	done

# Run tests + open HTML coverage report
coverage:
	uv run pytest $(PYTEST_PARALLEL) --cov-report=html
	@echo "Coverage report: htmlcov/index.html"

# CI-friendly check (non-zero exit on any issue)
check: lint typecheck test test-sh

# Point git at the tracked hooks: ruff/mypy + patch-version bump on
# commit, build-hash bake afterwards.  Per clone, once.
hooks:
	git config core.hooksPath .githooks
	@echo "git hooks: .githooks (pre-commit, post-commit)"

# Install ltvm and host dependencies (QEMU, bridge, SSH, scripts)
install:
	sudo ./ltvm install

# Undo `make install`: everything ltvm install set up on this host.
# No sudo -- ltvm uninstall elevates only the steps that need it, and
# asks before deleting artifacts, crash dumps or podman state.  When
# ./ltvm does not run (python3 older than 3.10), the launcher on PATH,
# which pins its own Python, does it.  When neither runs, the launcher
# still comes off PATH.
uninstall:
	@if ./ltvm --version >/dev/null 2>&1; then \
		./ltvm uninstall; \
	elif ltvm --version >/dev/null 2>&1; then \
		ltvm uninstall; \
	else \
		echo "ltvm does not run: removing only its launcher" >&2; \
		sudo rm -f /usr/local/bin/ltvm /usr/local/bin/dk-filter; \
	fi
