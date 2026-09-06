.PHONY: lint fix format typecheck test test-fast test-e2e test-sh coverage check install uninstall

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

# Run tests (with coverage summary in terminal)
test:
	uv run pytest

# Run tests without coverage (faster iteration during development)
test-fast:
	uv run pytest --no-cov -q

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
	uv run pytest --cov-report=html
	@echo "Coverage report: htmlcov/index.html"

# CI-friendly check (non-zero exit on any issue)
check: lint typecheck test test-sh

# Install ltvm and host dependencies (QEMU, bridge, SSH, scripts)
install:
	sudo ./ltvm install

# Remove ltvm from PATH and clean up installed files
uninstall:
	sudo rm -f /usr/local/bin/ltvm /usr/local/bin/dk-filter
	sudo rm -f /etc/bash_completion.d/ltvm
