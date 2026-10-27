# Decisions

One line per design choice: date, choice, reason.

- 2026-10-27: Python 3.12 with uv, DuckDB and Parquet storage, polars for analysis. Fast, local, no services.
- 2026-10-27: Simulator never signs or submits live orders. Safety first.
- 2026-10-27: hatchling build backend with src layout. Standard, no config needed.
- 2026-10-27: pydantic models with `extra="forbid"` for config. Typos in YAML fail loudly.
- 2026-10-27: argparse for the CLI, not click or typer. No extra dependency for a small CLI.
- 2026-10-27: Ruff rules E,F,I,B,UP plus mypy strict with the pydantic plugin. Strict typing from day one.
