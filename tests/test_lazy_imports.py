"""Every import inside a function body must resolve.

Lazy imports only run on the path that reaches them. When the line template was
adapted, cli/_common kept a lazy ``from olvm_aiops.connection import XoApiError`` in
its error handler: every CLI command that raised a handled error crashed with
ImportError, and no test noticed because nothing exercised the error path. This
walks the package AST and resolves each function-level import directly.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = ("olvm_aiops", "mcp_server")


def _lazy_imports() -> list[tuple[str, int, str, str | None]]:
    found = []
    for pkg in PACKAGES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            tree = ast.parse(path.read_text("utf-8"))
            for fn in (n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
                for node in ast.walk(fn):
                    rel = str(path.relative_to(ROOT))
                    if isinstance(node, ast.ImportFrom) and node.module \
                            and node.module.split(".")[0] in PACKAGES:
                        found += [(rel, node.lineno, node.module, a.name) for a in node.names]
                    elif isinstance(node, ast.Import):
                        found += [(rel, node.lineno, a.name, None) for a in node.names
                                  if a.name.split(".")[0] in PACKAGES]
    return found


@pytest.mark.unit
def test_there_are_lazy_imports_to_check():
    """Guard against the checker silently finding nothing (a broken walker passes)."""
    assert len(_lazy_imports()) >= 5


@pytest.mark.unit
def test_every_function_level_import_resolves():
    problems = []
    for rel, line, module, name in _lazy_imports():
        try:
            mod = importlib.import_module(module)
        except ImportError as exc:
            problems.append(f"{rel}:{line} {module}: {exc}")
            continue
        if name in (None, "*") or hasattr(mod, name):
            continue
        try:
            importlib.import_module(f"{module}.{name}")
        except ImportError:
            problems.append(f"{rel}:{line} {module}.{name} does not exist")
    assert not problems, "\n".join(problems)
