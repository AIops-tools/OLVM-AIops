"""MCP server wrapping OLVM AIops operations (stdio transport).

Thin adapter layer: each ``@mcp.tool()`` function (in ``olvm_aiops/mcp_server/tools/``)
delegates to the ``olvm_aiops`` ops package and is wrapped with the
olvm-aiops ``@governed_tool`` harness (audit / budget / risk-tier).

For Oracle Linux Virtualization Manager and upstream oVirt 4.5 engines.

Source: https://github.com/AIops-tools/OLVM-AIops
License: MIT
"""

import logging

from olvm_aiops.mcp_server._annotations import apply_risk_annotations
from olvm_aiops.mcp_server._shared import _safe_error, mcp, tool_errors

# Importing the tool modules registers every @mcp.tool() onto the shared
# `mcp` instance. Order does not matter; each module is self-contained.
from olvm_aiops.mcp_server.tools import (  # noqa: F401 — side effects
    reads,
    storage_vms,
    undo,
)

# Publish each tool's governance risk level as MCP annotations. Runs after
# every tool module has registered, so no tool is advertised unannotated.
apply_risk_annotations(mcp)

__all__ = ["mcp", "main", "_safe_error", "tool_errors"]

logger = logging.getLogger(__name__)


def main() -> None:
    """Run the MCP server over stdio."""
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="stdio")
