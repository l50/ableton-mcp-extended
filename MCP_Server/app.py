"""The FastMCP application that every tool module registers on."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from MCP_Server import connection

logger = logging.getLogger(__name__)


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Manage server startup and shutdown lifecycle."""
    try:
        # Do NOT touch Ableton here. This hook runs before the MCP initialize
        # handshake is answered; any blocking socket I/O (or Live not running)
        # delays/kills the handshake and the client reports "Server disconnected".
        # The connection is established lazily by the first tool call instead.
        logger.info("AbletonMCP server starting up (Ableton connection is lazy)")
        yield {}
    finally:
        connection.close_connection()
        logger.info("AbletonMCP server shut down")


mcp = FastMCP(
    "AbletonMCP",
    instructions="Ableton Live integration through the Model Context Protocol",
    lifespan=server_lifespan,
)
