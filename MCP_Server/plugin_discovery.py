"""Find external (VST/AU) plug-ins in Live's browser and match them by name.

A full browser walk takes many round trips, so results are cached briefly.
The connection layer clears the cache whenever the connection to Live drops.
"""

from __future__ import annotations

import re
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from MCP_Server.connection import AbletonConnection

EXTERNAL_PLUGIN_CACHE_TTL_SECONDS = 120.0
# Browser roots that may hold plug-ins; aliases cover Live's naming differences.
PLUGIN_BROWSER_ROOTS = ("plugins", "vst3", "vst2", "au", "plug-ins")
MAX_BROWSER_DEPTH = 8
MAX_VISITED_PATHS = 2000

EXACT_MATCH_SCORE = 1000
PREFIX_MATCH_SCORE = 900
TOKEN_MATCH_SCORE = 700
SUBSTRING_MATCH_SCORE = 600

_external_plugin_cache_lock = threading.Lock()
_external_plugin_cache: dict[str, Any] = {
    "plugins": None,
    "built_at": 0.0,
}


def invalidate_external_plugin_cache() -> None:
    """Invalidate cached external plugin discovery results."""
    with _external_plugin_cache_lock:
        _external_plugin_cache["plugins"] = None
        _external_plugin_cache["built_at"] = 0.0


def normalize_plugin_search_text(value: str) -> str:
    """Normalize plugin names/queries for tolerant matching."""
    if not value:
        return ""
    cleaned = re.sub(r"[\s\-_]+", " ", value.strip().lower())
    return re.sub(r"\s+", " ", cleaned)


def plugin_match_score(plugin_name: str, query: str) -> int:
    """Compute a rough match score for plugin name search (0 = no match)."""
    normalized_name = normalize_plugin_search_text(plugin_name)
    normalized_query = normalize_plugin_search_text(query)

    if not normalized_query:
        return 1
    if normalized_name == normalized_query:
        return EXACT_MATCH_SCORE
    if normalized_name.startswith(normalized_query):
        return PREFIX_MATCH_SCORE

    query_tokens = [t for t in normalized_query.split(" ") if t]
    if query_tokens and all(token in normalized_name for token in query_tokens):
        # Token coverage, weighted by total query token length.
        return TOKEN_MATCH_SCORE + sum(len(t) for t in query_tokens)

    if normalized_query in normalized_name:
        return SUBSTRING_MATCH_SCORE + len(normalized_query)

    return 0


def rank_plugins(
    plugins: list[dict[str, Any]], query: str, exact_match: bool = False
) -> list[tuple[int, dict[str, Any]]]:
    """Return (score, plugin) pairs that match *query*, best match first."""
    scored = []
    for plugin in plugins:
        score = plugin_match_score(plugin.get("name", ""), query)
        if exact_match and score < EXACT_MATCH_SCORE:
            continue
        if score > 0:
            scored.append((score, plugin))
    scored.sort(key=lambda x: (-x[0], normalize_plugin_search_text(x[1].get("name", ""))))
    return scored


def collect_external_plugins_from_root(
    ableton: AbletonConnection,
    root_path: str,
    max_depth: int = MAX_BROWSER_DEPTH,
    max_visited_paths: int = MAX_VISITED_PATHS,
) -> list[dict[str, Any]]:
    """Walk a browser root depth-first and collect its loadable plugin items."""
    stack: list[tuple[str, int]] = [(root_path, 0)]
    visited: set[str] = set()
    plugins: list[dict[str, Any]] = []

    while stack:
        current_path, depth = stack.pop()
        if current_path in visited:
            continue
        visited.add(current_path)

        if len(visited) > max_visited_paths:
            raise RuntimeError(
                f"Plugin traversal exceeded safety limit ({max_visited_paths} paths)."
            )

        result = ableton.send_command("get_browser_items_at_path", {"path": current_path})
        if "error" in result:
            # Root errors matter; deeper path misses are expected from stale paths.
            if depth == 0:
                raise ValueError(result.get("error", "Unknown browser root error"))
            continue

        for item in result.get("items", []):
            name = (item.get("name") or "").strip()
            if not name:
                continue

            child_path = f"{current_path}/{name}"
            uri = item.get("uri")
            if item.get("is_loadable", False) and uri:
                plugins.append(
                    {
                        "name": name,
                        "uri": uri,
                        "path": child_path,
                        "is_device": bool(item.get("is_device", False)),
                        "root": root_path,
                    }
                )

            if item.get("is_folder", False) and depth < max_depth:
                stack.append((child_path, depth + 1))

    return plugins


def discover_external_plugins(ableton: AbletonConnection) -> list[dict[str, Any]]:
    """Discover loadable external plugins from the first non-empty browser root."""
    discovered_any_root = False
    errors: list[str] = []

    for root in PLUGIN_BROWSER_ROOTS:
        try:
            found = collect_external_plugins_from_root(ableton, root_path=root)
        except Exception as e:
            errors.append(f"{root}: {e}")
            continue
        discovered_any_root = True
        if found:
            # Aliases can point at the same tree and rescanning is expensive.
            found.sort(key=lambda p: normalize_plugin_search_text(p.get("name", "")))
            return found

    if discovered_any_root:
        return []

    raise ValueError(
        "Could not discover external plugins. Tried roots: {}. Last errors: {}".format(
            ", ".join(PLUGIN_BROWSER_ROOTS),
            " | ".join(errors) if errors else "none",
        )
    )


def get_cached_external_plugins(
    ableton: AbletonConnection,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """Return discovered plugins, rescanning when the cache is stale or forced."""
    now = time.monotonic()
    with _external_plugin_cache_lock:
        cached_plugins = _external_plugin_cache.get("plugins")
        built_at = float(_external_plugin_cache.get("built_at", 0.0) or 0.0)
        if (
            not force_refresh
            and cached_plugins is not None
            and (now - built_at) <= EXTERNAL_PLUGIN_CACHE_TTL_SECONDS
        ):
            return list(cached_plugins)

    discovered = discover_external_plugins(ableton)
    with _external_plugin_cache_lock:
        _external_plugin_cache["plugins"] = list(discovered)
        _external_plugin_cache["built_at"] = time.monotonic()
    return discovered
