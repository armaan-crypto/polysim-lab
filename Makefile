.PHONY: check lint typecheck test bench

check: lint typecheck test

lint:
	uv run ruff check src tests algorithms
	uv run ruff format --check src tests algorithms

typecheck:
	uv run mypy

test:
	uv run pytest --cov=pmsim.engine --cov=pmsim.book --cov-report=term:skip-covered --cov-fail-under=85

bench:
	uv run python scripts/bench_day.py 24
