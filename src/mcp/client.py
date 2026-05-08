"""client.py — Async MCP SSE client for codebase cross-referencing.

Opens a fresh connection to the configured MCP server for each tool call
(stateless pattern — safe for short-lived review sessions).

Usage:
    client = MCPClient("http://localhost:8091/mcp/sse")
    tools  = await client.list_tools()          # [{"name": ..., "description": ...}]
    result = await client.call_tool("get_definition", {"symbol": "DataService"})
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class MCPClient:
    """Thin async client for an MCP SSE server."""

    def __init__(self, url: str) -> None:
        self._url = url.rstrip("/")
        # Normalise: the SSE endpoint must be the /sse path
        if not self._url.endswith("/sse"):
            self._url = self._url + "/sse" if "/mcp" in self._url else self._url + "/mcp/sse"
        self._tools_cache: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the list of tools advertised by the MCP server."""
        if self._tools_cache is not None:
            return self._tools_cache
        self._tools_cache = await self._fetch_tools()
        return self._tools_cache

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call *name* with *arguments* and return the plain-text result."""
        return await self._call(name, arguments)

    async def call_tools_batch(
        self, calls: list[tuple[str, dict[str, Any]]]
    ) -> list[str]:
        """Call multiple tools concurrently and return results in order."""
        import asyncio

        tasks = [self.call_tool(name, args) for name, args in calls]
        return list(await asyncio.gather(*tasks, return_exceptions=False))

    async def is_available(self) -> bool:
        """Return True if the MCP server responds to a health-check."""
        import httpx

        base = self._url.replace("/mcp/sse", "").replace("/sse", "")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{base}/healthz")
                return resp.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Internal: MCP SDK calls
    # ------------------------------------------------------------------

    async def _fetch_tools(self) -> list[dict[str, Any]]:
        try:
            from mcp.client.sse import sse_client
            from mcp import ClientSession
        except ImportError:
            logger.warning("mcp package not installed — MCP cross-referencing disabled")
            return []

        try:
            async with sse_client(self._url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return [
                        {"name": t.name, "description": t.description or ""}
                        for t in result.tools
                    ]
        except Exception as exc:
            logger.warning("MCP list_tools failed: %s", exc)
            return []

    async def _call(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            from mcp.client.sse import sse_client
            from mcp import ClientSession
        except ImportError:
            return f"mcp package not installed — cannot call tool '{name}'"

        try:
            async with sse_client(self._url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
                    if result.content:
                        return "\n".join(
                            c.text for c in result.content if hasattr(c, "text")
                        )
                    return ""
        except Exception as exc:
            logger.warning("MCP tool call failed (%s): %s", name, exc)
            return f"[MCP error calling {name}: {exc}]"
