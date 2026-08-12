"""Compatibility imports for historical callers.

Current Runtime code must import :mod:`supportguard.mcp.runtime`, which is the
single owner of long-lived stdio MCP process and session lifecycles.  This
module remains only until the Phase 6 historical-test disposition is complete.
"""

from supportguard.mcp.runtime import (
    EXPECTED_TOOLS,
    FROZEN_SCHEMA_HASHES,
    ManagedServer,
    MCPCallResult,
    MCPManager,
    MCPTransportFailure,
    ServerHealth,
    ServerName,
    SupervisorState,
    ToolTransport,
    _safe_protocol_failure,
    classify_mcp_failure,
    is_retryable_mcp_failure,
)

__all__ = [
    "EXPECTED_TOOLS",
    "FROZEN_SCHEMA_HASHES",
    "MCPCallResult",
    "MCPManager",
    "MCPTransportFailure",
    "ManagedServer",
    "ServerHealth",
    "ServerName",
    "SupervisorState",
    "ToolTransport",
    "_safe_protocol_failure",
    "classify_mcp_failure",
    "is_retryable_mcp_failure",
]
