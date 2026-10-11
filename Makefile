.PHONY: run-dev test lint reset-data migrate-home

run-dev:
	uv run streamlit run app.py

test:
	uv run pytest

# Run code quality checks (codespell, ruff, mypy)
lint:
	uv sync --dev --extra lint
	uv run codespell
	uv run ruff check . --diff
	uv run ruff format . --check --diff
	uv run mypy .

# Destructive reset of $DEMAT_HOME/data (see scripts/reset_data.py). Pass
# args with e.g. `make reset-data ARGS=--yes`.
reset-data:
	uv run python -m scripts.reset_data $(ARGS)

# One-time move of a developer checkout's repo-local data/ into $DEMAT_HOME
# (see scripts/migrate_home.py). Pass args with e.g.
# `make migrate-home ARGS=--yes`.
migrate-home:
	uv run python -m scripts.migrate_home $(ARGS)
