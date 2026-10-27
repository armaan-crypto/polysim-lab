"""The no-real-orders rule, shared by the safeguard tests and the custom algorithm loader."""

from __future__ import annotations

import ast

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


def forbidden_imports(source: str) -> list[str]:
    """Names of forbidden packages the source imports."""
    found = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        found += [n for n in names if n.split(".")[0] in FORBIDDEN_MODULES]
    return found
