"""Device tools: parameters, on/off, racks and drum pads, deletion, presets."""

from typing import Any

from mcp.server.fastmcp import Context

from MCP_Server import connection
from MCP_Server.conversions import optional_to_zero_based, to_zero_based
from MCP_Server.plugin_aliases import get_alias_for_param, get_categories, resolve_alias
from MCP_Server.tools.base import ableton_tool

UNCATEGORIZED = "Other"


def _categorize(param_name: str, categories: dict[str, list[str]] | None) -> str:
    """Return the alias category whose prefix matches *param_name*."""
    for cat_name, prefixes in (categories or {}).items():
        if any(param_name.startswith(prefix) for prefix in prefixes):
            return cat_name
    return UNCATEGORIZED


def _devices_named(
    ableton: connection.AbletonConnection, track: int, name: str
) -> list[dict[str, Any]]:
    """Return the devices on a track (0-based) whose name matches, any case."""
    info = ableton.send_command("get_track_info", {"track_index": track})
    return [d for d in info.get("devices", []) if d["name"].lower() == name.lower()]


def _describe_devices(devices: list[dict[str, Any]]) -> str:
    return ", ".join(f"{d['name']} (index {d['index'] + 1})" for d in devices)


@ableton_tool("getting device parameters")
def get_device_parameters(
    ctx: Context,
    track_index: int,
    device_index: int = 1,
    chain_index: int = 0,
    category: str = "",
    show_all: bool = False,
) -> str:
    """List parameters for a device on a track.

    Parameters:
    - track_index: Track number (1-based).
    - device_index: Device number on the track (1-based, default 1).
    - chain_index: Chain number inside a rack (1-based, 0 = no chain).
    - category: Filter by category name (returns detail for that category).
    - show_all: If True, return all parameters in detail mode.

    Default mode returns a summary grouped by category with counts.
    Specify category or show_all=True for full parameter details.
    """
    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")
    di = to_zero_based(device_index, "device_index")
    ci = optional_to_zero_based(chain_index, "chain_index")
    result = ableton.send_command(
        "get_device_parameters",
        # Always fetch the full list; grouping happens here.
        {"track_index": ti, "device_index": di, "chain_index": ci, "show_all": True},
    )

    device_name = result.get("device_name", "Unknown")
    params = result.get("parameters", [])
    param_count = result.get("parameter_count", len(params))

    for p in params:
        alias = get_alias_for_param(device_name, p["name"])
        if alias:
            p["alias"] = alias

    categories = get_categories(device_name)

    if show_all or category:
        filtered = params
        if category:
            filtered = [
                p for p in params if _categorize(p["name"], categories).lower() == category.lower()
            ]
            if not filtered:
                available = sorted({_categorize(p["name"], categories) for p in params})
                return (
                    f"No parameters found in category '{category}'. "
                    f"Available categories: {', '.join(available)}"
                )

        lines = [f"{device_name} — {len(filtered)} parameters", ""]
        for p in filtered:
            alias_str = f" ({p['alias']})" if p.get("alias") else ""
            enabled_str = "" if p["is_enabled"] else " [disabled]"
            lines.append(
                f"  {p['index'] + 1}. {p['name']}{alias_str}: {p['display_value']} "
                f"(normalized {round(p['value'], 2)}){enabled_str}"
            )
        return "\n".join(lines)

    groups: dict[str, list[dict[str, Any]]] = {}
    for p in params:
        groups.setdefault(_categorize(p["name"], categories), []).append(p)

    lines = [f"{device_name} — {param_count} parameters total", ""]
    lines.extend(
        f"  {cat_name}: {len(cat_params)} parameters" for cat_name, cat_params in groups.items()
    )
    lines.append("")
    lines.append("Use category='<name>' or show_all=True for full details.")
    return "\n".join(lines)


@ableton_tool("setting device parameter")
def set_device_parameter(
    ctx: Context,
    track_index: int,
    device_index: int = 1,
    chain_index: int = 0,
    parameter_name: str = "",
    parameter_index: int = 0,
    value: float = 0.0,
) -> str:
    """Set a device parameter value.

    Parameters:
    - track_index: Track number (1-based).
    - device_index: Device number (1-based, default 1).
    - chain_index: Chain number inside a rack (1-based, 0 = no chain).
    - parameter_name: Parameter name, friendly alias, or partial match.
    - parameter_index: Parameter number (1-based, alternative to name).
    - value: Normalized value 0.0-1.0.
    """
    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")
    di = to_zero_based(device_index, "device_index")
    ci = optional_to_zero_based(chain_index, "chain_index")
    pi = optional_to_zero_based(parameter_index, "parameter_index")
    device = {"track_index": ti, "device_index": di, "chain_index": ci}

    resolved_name = parameter_name
    alias_used = None
    if parameter_name:
        # The alias table is keyed by device name, so look the device up first.
        info = ableton.send_command("get_device_parameters", {**device, "show_all": False})
        real_name = resolve_alias(info.get("device_name", ""), parameter_name)
        if real_name:
            alias_used = parameter_name
            resolved_name = real_name

    result = ableton.send_command(
        "set_device_parameter",
        {
            **device,
            "parameter_name": resolved_name if resolved_name else None,
            "parameter_index": pi,
            "value": value,
        },
    )

    param_name = result.get("parameter_name", "?")
    display = result.get("display_value", "?")
    new_val = result.get("new_value", value)
    msg = f"Set {param_name} to {display} (normalized {round(new_val, 2)})"
    if alias_used:
        msg += f" [alias: {alias_used}]"
    if result.get("clamped", False):
        msg += " (value was clamped to 0.0-1.0 range)"
    return msg


def _set_device_enabled(
    track_index: int, device_index: int, device_name: str, chain_index: int, enabled: bool
) -> str:
    """Shared body of enable_device and disable_device."""
    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")
    di = optional_to_zero_based(device_index, "device_index")
    ci = optional_to_zero_based(chain_index, "chain_index")

    if device_name and di is None:
        matches = _devices_named(ableton, ti, device_name)
        if not matches:
            return f"Error: Device '{device_name}' not found on track {track_index}"
        if len(matches) > 1:
            return (
                f"Error: Multiple devices named '{device_name}' on track {track_index}: "
                f"{_describe_devices(matches)}"
            )
        di = matches[0]["index"]
    elif di is None:
        di = 0

    result = ableton.send_command(
        "set_device_enabled",
        {"track_index": ti, "device_index": di, "chain_index": ci, "enabled": enabled},
    )
    state = "enabled" if result.get("is_active", enabled) else "disabled"
    return f"{result.get('device_name', '?')} {state}"


@ableton_tool("toggling device")
def enable_device(
    ctx: Context,
    track_index: int,
    device_index: int = 0,
    device_name: str = "",
    chain_index: int = 0,
) -> str:
    """Enable (activate) a device on a track.

    Parameters:
    - track_index: Track number (1-based).
    - device_index: Device number (1-based). Use 0 if using device_name.
    - device_name: Device name (alternative to device_index).
    - chain_index: Chain number inside a rack (1-based, 0 = no chain).
    """
    return _set_device_enabled(track_index, device_index, device_name, chain_index, True)


@ableton_tool("toggling device")
def disable_device(
    ctx: Context,
    track_index: int,
    device_index: int = 0,
    device_name: str = "",
    chain_index: int = 0,
) -> str:
    """Disable (bypass) a device on a track.

    Parameters:
    - track_index: Track number (1-based).
    - device_index: Device number (1-based). Use 0 if using device_name.
    - device_name: Device name (alternative to device_index).
    - chain_index: Chain number inside a rack (1-based, 0 = no chain).
    """
    return _set_device_enabled(track_index, device_index, device_name, chain_index, False)


@ableton_tool("getting chain info")
def get_chain_info(
    ctx: Context,
    track_index: int,
    device_index: int = 1,
    chain_index: int = 0,
) -> str:
    """List chains in a rack device, or devices within a specific chain.

    Parameters:
    - track_index: Track number (1-based).
    - device_index: Device number (1-based, default 1).
    - chain_index: Chain number (1-based) to drill into. 0 = list all chains.
    """
    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")
    di = to_zero_based(device_index, "device_index")
    ci = optional_to_zero_based(chain_index, "chain_index")
    result = ableton.send_command(
        "get_chain_info", {"track_index": ti, "device_index": di, "chain_index": ci}
    )

    if chain_index > 0:
        devices = result.get("devices", [])
        lines = [f"Chain '{result.get('chain_name', '?')}' — {len(devices)} devices", ""]
        for d in devices:
            active = "" if d.get("is_active", True) else " [disabled]"
            lines.append(
                f"  {d['index'] + 1}. {d['name']} ({d['type']}, "
                f"{d.get('parameter_count', '?')} params){active}"
            )
        return "\n".join(lines)

    chains = result.get("chains", [])
    lines = [f"{result.get('device_name', '?')} — {len(chains)} chains", ""]
    for c in chains:
        mute_str = " [muted]" if c.get("mute") else ""
        solo_str = " [solo]" if c.get("solo") else ""
        dev_names = ", ".join(d["name"] for d in c.get("devices", []))
        lines.append(
            f"  {c['index'] + 1}. {c['name']}{mute_str}{solo_str}: "
            f"{c['device_count']} devices ({dev_names or 'empty'})"
        )
    return "\n".join(lines)


@ableton_tool("getting drum pad info")
def get_drum_pad_info(ctx: Context, track_index: int, device_index: int = 1) -> str:
    """List filled drum pads in a Drum Rack.

    Parameters:
    - track_index: Track number (1-based).
    - device_index: Device number (1-based, default 1).
    """
    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")
    di = to_zero_based(device_index, "device_index")
    result = ableton.send_command("get_drum_pad_info", {"track_index": ti, "device_index": di})

    pads = result.get("filled_pads", [])
    lines = [f"{result.get('device_name', '?')} — {len(pads)} filled pads", ""]
    for pad in pads:
        mute_str = " [muted]" if pad.get("mute") else ""
        solo_str = " [solo]" if pad.get("solo") else ""
        dev_names = [d["name"] for chain in pad.get("chains", []) for d in chain.get("devices", [])]
        lines.append(
            f"  Note {pad['note']}: {pad['name']}{mute_str}{solo_str} → "
            f"{', '.join(dev_names) or 'empty'}"
        )
    return "\n".join(lines)


@ableton_tool("deleting device")
def delete_device(
    ctx: Context,
    track_index: int,
    device_index: int = 0,
    device_name: str = "",
) -> str:
    """Delete a device from a track.

    Parameters:
    - track_index: Track number (1-based).
    - device_index: Device number (1-based).
    - device_name: Device name (alternative to device_index).
    """
    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")
    di = optional_to_zero_based(device_index, "device_index")

    if device_name and di is None:
        matches = _devices_named(ableton, ti, device_name)
        if not matches:
            return f"Error: Device '{device_name}' not found on track {track_index}"
        if len(matches) > 1:
            return f"Error: Multiple devices named '{device_name}': {_describe_devices(matches)}"
        di = matches[0]["index"]
    elif di is None:
        di = 0

    result = ableton.send_command("delete_device", {"track_index": ti, "device_index": di})
    return (
        f"Deleted {result.get('deleted_device', 'device')}. "
        f"{result.get('remaining_devices', '?')} devices remaining."
    )


@ableton_tool("navigating preset")
def navigate_device_preset(
    ctx: Context,
    track_index: int,
    device_index: int = 1,
    chain_index: int = 0,
    direction: str = "next",
) -> str:
    """Navigate device presets (next/previous/current).

    Parameters:
    - track_index: Track number (1-based).
    - device_index: Device number (1-based, default 1).
    - chain_index: Chain number inside a rack (1-based, 0 = no chain).
    - direction: "next", "previous", or "current".
    """
    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")
    di = to_zero_based(device_index, "device_index")
    ci = optional_to_zero_based(chain_index, "chain_index")
    result = ableton.send_command(
        "navigate_preset",
        {"track_index": ti, "device_index": di, "chain_index": ci, "direction": direction},
    )

    verb = "current preset is" if direction == "current" else "loaded preset"
    return "{}: {} '{}' ({}/{})".format(
        result.get("device_name", "?"),
        verb,
        result.get("preset_name", "?"),
        result.get("preset_index", 0) + 1,
        result.get("preset_count", 0),
    )
