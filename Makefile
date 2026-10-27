.PHONY: check lint typecheck test

check: lint typecheck test

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck:
	uv run mypy

test:
	uv run pytest
