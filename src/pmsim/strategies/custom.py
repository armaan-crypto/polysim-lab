"""Load your own algorithms from Python files in the `algorithms/` folder.

A file becomes an algorithm when it defines a `Strategy` subclass. An optional
nested pydantic `Config` class supplies its settings. Files that import a
trading or signing package are refused, so custom code can only reach the
simulator.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from pmsim.safety import forbidden_imports
from pmsim.strategies.base import Strategy
from pmsim.strategies.legs import LegStrategy

ALGORITHMS_DIR = Path("algorithms")


@dataclass(frozen=True)
class CustomAlgorithm:
    name: str
    path: Path
    title: str
    blurb: str
    cls: type[Strategy] | None
    config: type[BaseModel] | None
    error: str = ""


def _strategy_class(module: object, module_name: str) -> type[Strategy] | None:
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if (
            issubclass(obj, Strategy)
            and obj not in (Strategy, LegStrategy)
            and obj.__module__ == module_name
        ):
            return obj
    return None


def _load(path: Path) -> CustomAlgorithm:
    name = path.stem
    fallback = name.replace("_", " ").capitalize()

    def failed(msg: str) -> CustomAlgorithm:
        return CustomAlgorithm(name, path, fallback, "", None, None, msg)

    try:
        source = path.read_text()
        banned = forbidden_imports(source)
    except (OSError, SyntaxError) as exc:
        return failed(f"{type(exc).__name__}: {exc}")
    if banned:
        return failed(f"imports trading packages ({', '.join(banned)}); not allowed")
    module_name = f"pmsim_user_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return failed("cannot import this file")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # user code: report, never crash the app
        return failed(f"{type(exc).__name__}: {exc}")
    cls = _strategy_class(module, module_name)
    if cls is None:
        return failed("no Strategy subclass found")
    config = getattr(cls, "Config", None)
    if config is not None and not (isinstance(config, type) and issubclass(config, BaseModel)):
        return failed("Config must be a pydantic BaseModel")
    doc = inspect.getdoc(cls) or ""
    title = str(getattr(cls, "title", "")) or fallback
    return CustomAlgorithm(name, path, title, doc.split("\n\n")[0], cls, config)


def discover(directory: Path = ALGORITHMS_DIR) -> list[CustomAlgorithm]:
    """Every `*.py` file in `directory` except names starting with `_`."""
    if not directory.is_dir():
        return []
    return [_load(p) for p in sorted(directory.glob("*.py")) if not p.name.startswith("_")]


def build(algo: CustomAlgorithm, params: dict[str, object]) -> Strategy:
    if algo.cls is None:
        raise ValueError(f"algorithm {algo.name!r} failed to load: {algo.error}")
    if algo.config is None:
        strategy = algo.cls()
    else:
        strategy = algo.cls(algo.config.model_validate(params))  # type: ignore[call-arg]
    strategy.name = algo.name
    return strategy
