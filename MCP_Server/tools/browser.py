"""Browser tools: browse Live's browser and load instruments, effects and plug-ins."""

import json
from typing import Any

from mcp.server.fastmcp import Context

from MCP_Server import connection, plugin_discovery
from MCP_Server.conversions import to_zero_based
from MCP_Server.tools.base import ErrorFormatter, ableton_tool

BROWSER_UNAVAILABLE_MESSAGE = (
    "Error: The Ableton browser is not available. "
    "Make sure Ableton Live is fully loaded and try again."
)
LIVE_UNREACHABLE_MESSAGE = (
    "Error: Could not access the Ableton Live application. "
    "Make sure Ableton Live is running and the Remote Script is loaded."
)
# How many of the equally good matches to name when a plug-in name is ambiguous.
MAX_AMBIGUOUS_MATCHES_SHOWN = 5


def _browser_error(action: str, path_hints: bool = False) -> ErrorFormatter:
    """Turn the Remote Script's browser errors into actionable replies."""

    def format_error(exc: Exception) -> str:
        message = str(exc)
        if "Browser is not available" in message:
            return BROWSER_UNAVAILABLE_MESSAGE
        if "Could not access Live application" in message:
            return LIVE_UNREACHABLE_MESSAGE
        if path_hints and "Unknown or unavailable category" in message:
            return (
                f"Error: {message}. Please check the available categories using get_browser_tree."
            )
        if path_hints and "Path part" in message and "not found" in message:
            return f"Error: {message}. Please check the path and try again."
        return f"Error {action}: {message}"

    return format_error


def _format_browser_tree(item: dict[str, Any], indent: int = 0) -> str:
    """Render one browser folder and its children as an indented bullet list."""
    if not item:
        return ""
    line = "  " * indent + f"• {item.get('name', 'Unknown')}"
    if item.get("path", ""):
        line += f" (path: {item['path']})"
    if item.get("has_more", False):
        line += " [...]"
    children = "".join(
        _format_browser_tree(child, indent + 1) for child in item.get("children", [])
    )
    return line + "\n" + children


def _load_item(ableton: connection.AbletonConnection, track_index: int, uri: str) -> Any:
    """Load the browser item *uri* onto a track (0-based index)."""
    return ableton.send_command("load_browser_item", {"track_index": track_index, "item_uri": uri})


@ableton_tool("getting browser tree", format_error=_browser_error("getting browser tree"))
def get_browser_tree(ctx: Context, category_type: str = "all") -> str:
    """
    Get a hierarchical tree of browser categories from Ableton.

    Parameters:
    - category_type: Type of categories to get ('all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects')
    """
    result = connection.get_ableton_connection().send_command(
        "get_browser_tree", {"category_type": category_type}
    )
    categories = result.get("categories", [])
    if "available_categories" in result and len(categories) == 0:
        return (
            f"No categories found for '{category_type}'. "
            f"Available browser categories: {', '.join(result.get('available_categories', []))}"
        )

    total_folders = result.get("total_folders", 0)
    header = f"Browser tree for '{category_type}' (showing {total_folders} folders):\n\n"
    return header + "".join(_format_browser_tree(category) + "\n" for category in categories)


@ableton_tool(
    "getting browser items at path",
    format_error=_browser_error("getting browser items at path", path_hints=True),
)
def get_browser_items_at_path(ctx: Context, path: str) -> str:
    """
    Get browser items at a specific path in Ableton's browser.

    Parameters:
    - path: Path in the format "category/folder/subfolder"
            where category is one of the available browser categories in Ableton
    """
    result = connection.get_ableton_connection().send_command(
        "get_browser_items_at_path", {"path": path}
    )
    if "error" in result and "available_categories" in result:
        return (
            f"Error: {result.get('error', '')}\n"
            f"Available browser categories: {', '.join(result.get('available_categories', []))}"
        )
    return json.dumps(result, indent=2)


@ableton_tool("loading instrument by URI")
def load_instrument_or_effect(ctx: Context, track_index: int, uri: str) -> str:
    """
    Load an instrument or effect onto a track using its URI.

    Parameters:
    - track_index: Track number (1-based).
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116').
    """
    ableton = connection.get_ableton_connection()
    result = _load_item(ableton, to_zero_based(track_index, "track_index"), uri)
    if not result.get("loaded", False):
        return f"Failed to load instrument with URI '{uri}'"
    new_devices = result.get("new_devices", [])
    if new_devices:
        return (
            f"Loaded instrument with URI '{uri}' on track {track_index}. "
            f"New devices: {', '.join(new_devices)}"
        )
    devices = result.get("devices_after", [])
    return (
        f"Loaded instrument with URI '{uri}' on track {track_index}. "
        f"Devices on track: {', '.join(devices)}"
    )


@ableton_tool("loading drum kit")
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str) -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Parameters:
    - track_index: Track number (1-based).
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack').
    - kit_path: Path to the drum kit inside the browser (e.g., 'drums/acoustic/kit1').
    """
    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")

    if not _load_item(ableton, ti, rack_uri).get("loaded", False):
        return f"Failed to load drum rack with URI '{rack_uri}'"

    kit_result = ableton.send_command("get_browser_items_at_path", {"path": kit_path})
    if "error" in kit_result:
        return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"

    loadable_kits = [item for item in kit_result.get("items", []) if item.get("is_loadable", False)]
    if not loadable_kits:
        return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"

    kit = loadable_kits[0]
    _load_item(ableton, ti, kit.get("uri"))
    return f"Loaded drum rack and kit '{kit.get('name')}' on track {track_index}"


@ableton_tool("listing external plugins")
def list_external_plugins(
    ctx: Context,
    query: str = "",
    max_results: int = 50,
    refresh_cache: bool = False,
) -> str:
    """List discovered external plugins (VST/AU), optionally filtered by name query.

    Parameters:
    - query: Optional case-insensitive search string.
    - max_results: Maximum number of plugins to display.
    - refresh_cache: If True, force a rescan instead of using cached results.
    """
    ableton = connection.get_ableton_connection()
    plugins = plugin_discovery.get_cached_external_plugins(ableton, force_refresh=refresh_cache)
    matches = (
        [plugin for _, plugin in plugin_discovery.rank_plugins(plugins, query)]
        if query
        else plugins
    )

    if not matches:
        if query:
            return f"No external plugins matched query '{query}'."
        return "No external plugins were discovered."

    shown = matches[: max(1, int(max_results))]
    lines = [f"External plugins discovered: {len(matches)} total, showing {len(shown)}", ""]
    lines.extend(
        f"  {number}. {plugin.get('name', 'Unknown')} (path: {plugin.get('path', '?')})"
        for number, plugin in enumerate(shown, start=1)
    )
    if len(matches) > len(shown):
        lines.append("")
        lines.append(f"Use max_results={len(matches)} (or a tighter query) to see more.")
    return "\n".join(lines)


@ableton_tool("loading external plugin")
def load_external_plugin(
    ctx: Context,
    track_index: int,
    plugin_name: str,
    exact_match: bool = False,
    refresh_cache: bool = False,
) -> str:
    """Load an external plugin onto a track by plugin name (no URI required).

    Parameters:
    - track_index: Track number (1-based).
    - plugin_name: Plugin name to match (e.g., "FabFilter Pro-Q 3").
    - exact_match: If True, require exact normalized name match.
    - refresh_cache: If True, force a rescan before matching.
    """
    if not plugin_name or not plugin_name.strip():
        return "Error: plugin_name is required."

    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")
    plugins = plugin_discovery.get_cached_external_plugins(ableton, force_refresh=refresh_cache)

    scored = plugin_discovery.rank_plugins(plugins, plugin_name, exact_match=exact_match)
    if not scored:
        return (
            f"No external plugin matched '{plugin_name}'. "
            f"Try list_external_plugins(query='{plugin_name}') to inspect candidates."
        )

    top_score = scored[0][0]
    top_plugins = [plugin for score, plugin in scored if score == top_score]
    # Without an exact match, refuse to guess between equally strong candidates.
    if len(top_plugins) > 1 and top_score < plugin_discovery.EXACT_MATCH_SCORE:
        options = ", ".join(p.get("name", "?") for p in top_plugins[:MAX_AMBIGUOUS_MATCHES_SHOWN])
        return (
            f"Multiple plugins match '{plugin_name}': {options}. "
            "Please be more specific or set exact_match=True."
        )

    chosen = top_plugins[0]
    if _load_item(ableton, ti, chosen.get("uri")).get("loaded", False):
        return (
            f"Loaded external plugin '{chosen.get('name', '?')}' on track {track_index} "
            f"(matched '{plugin_name}')."
        )
    return f"Failed to load external plugin '{chosen.get('name', '?')}'."
