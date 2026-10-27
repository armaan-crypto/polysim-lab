"""Command line entry point."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from pmsim import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(prog="pmsim", description="Polymarket simulator")
    parser.add_argument("--version", action="version", version=f"pmsim {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return an exit code."""
    build_parser().parse_args(argv)
    return 0
