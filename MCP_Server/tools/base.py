"""Shared plumbing for the MCP tools: registration and error replies."""

import functools
import logging
from collections.abc import Callable
from typing import Any

from MCP_Server.app import mcp

logger = logging.getLogger(__name__)

ErrorFormatter = Callable[[Exception], str]


def ableton_tool(
    action: str, format_error: ErrorFormatter | None = None
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Register a function as an MCP tool that never raises to the client.

    Any exception the tool raises (a bad argument, a lost connection, an
    error reported by Live) is logged and returned as the text
    ``"Error <action>: <message>"``, or as ``format_error(exc)`` when given.
    The tool's name, parameters and description come from the function.
    """

    def decorator(fn: Callable[..., str]) -> Callable[..., str]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                logger.error("Error %s: %s", action, e)
                if format_error is not None:
                    return format_error(e)
                return f"Error {action}: {e}"

        return mcp.tool()(wrapper)

    return decorator
