.PHONY: lint fix format typecheck check

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
	uv run mypy ltvm lib/

# CI-friendly check (non-zero exit on any issue)
check: lint typecheck
