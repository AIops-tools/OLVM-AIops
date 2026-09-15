"""CLI package for olvm-aiops.

Re-exports ``app`` so the pyproject entry point
``olvm-aiops = "olvm_aiops.cli:app"`` works unchanged.
"""

from olvm_aiops.cli._root import app

__all__ = ["app"]
