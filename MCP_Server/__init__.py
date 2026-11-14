"""Ableton Live integration through the Model Context Protocol."""

from MCP_Server.connection import AbletonConnection, get_ableton_connection

__version__ = "0.1.0"
__all__ = ["AbletonConnection", "get_ableton_connection"]
