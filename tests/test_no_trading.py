"""Hard safeguard: nothing in pmsim can sign or submit a real order.

These tests fail if any module imports a trading or signing package, names
an order-placing endpoint, or makes a non-GET HTTP call.
"""

import ast
import importlib
import pkgutil
import re
import sys
import tomllib
from pathlib import Path

import pmsim

SRC = Path(pmsim.__file__).parent
ROOT = SRC.parents[1]

# Packages that can sign transactions or talk to trading APIs.
FORBIDDEN_MODULES = (
    "py_clob_client",
    "py_order_utils",
    "eth_account",
    "eth_keys",
    "web3",
    "coincurve",
    "ecdsa",
    "kalshi_python",
    "solcx",
)
# CLOB order-placing and cancel paths (docs), as string literals in code.
ENDPOINT = re.compile(
    r"""["'][^"']*/(order|orders|cancel|cancel-all|cancel-orders|cancel-market-orders"""
    r"""|portfolio/orders)(\?|/|["'])"""
)
WRITE_CALLS = {"post", "put", "patch", "delete", "request", "stream"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
HTTP_HINTS = ("client", "http", "requests", "session")


def sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_no_forbidden_imports_in_source() -> None:
    for path in sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                assert root not in FORBIDDEN_MODULES, f"{path}: imports {name}"


def test_no_order_endpoints_in_source() -> None:
    for path in sources():
        text = path.read_text()
        assert not ENDPOINT.search(text), f"{path}: names an order endpoint"


def _receiver(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id.lower()
    if isinstance(node, ast.Attribute):
        return node.attr.lower()
    return ""


def test_no_write_http_calls() -> None:
    for path in sources():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and node.value in WRITE_METHODS:
                raise AssertionError(f"{path}: HTTP method literal {node.value!r}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                receiver = _receiver(node.func.value)
                if node.func.attr in WRITE_CALLS and any(h in receiver for h in HTTP_HINTS):
                    raise AssertionError(f"{path}: {receiver}.{node.func.attr}()")


def test_importing_everything_loads_no_trading_module() -> None:
    for mod in pkgutil.walk_packages(pmsim.__path__, "pmsim."):
        importlib.import_module(mod.name)
    loaded = {m.split(".")[0] for m in sys.modules}
    assert not loaded & set(FORBIDDEN_MODULES)


def test_dependencies_exclude_trading_packages() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    deps = " ".join(project["dependencies"]).lower().replace("-", "_")
    for name in FORBIDDEN_MODULES:
        assert name not in deps


def test_guard_patterns_catch_examples() -> None:
    assert ENDPOINT.search('url = f"{base}/order"')
    assert ENDPOINT.search("'/orders?id=1'")
    assert not ENDPOINT.search('"/markets/{ticker}/orderbook"')
    assert not ENDPOINT.search('"/book"')


def test_write_check_catches_examples() -> None:
    bad = ast.parse('client.post(url)\nhttpx.request("POST", url)')
    calls = [n for n in ast.walk(bad) if isinstance(n, ast.Call)]
    assert any(
        isinstance(c.func, ast.Attribute)
        and c.func.attr in WRITE_CALLS
        and any(h in _receiver(c.func.value) for h in HTTP_HINTS)
        for c in calls
    )
    assert any(isinstance(n, ast.Constant) and n.value in WRITE_METHODS for n in ast.walk(bad))
    ok = ast.parse("cache.put(url, {}, value)")
    call = next(n for n in ast.walk(ok) if isinstance(n, ast.Call))
    assert isinstance(call.func, ast.Attribute)
    assert not any(h in _receiver(call.func.value) for h in HTTP_HINTS)
