"""Index, parameter-range and bar/beat conversions shared by the tools.

Convention: every MCP tool exposes **1-based** indices to callers, while the
Remote Script expects **0-based** indices. These helpers enforce the rule in
one place so individual tools stay simple.
"""

from typing import Any

DEFAULT_TIME_SIGNATURE = (4, 4)
# The Remote Script reads track_index -1 as "every track".
ALL_TRACKS = -1
QUARTER_NOTES_PER_WHOLE = 4


def to_zero_based(index: int, field_name: str = "index") -> int:
    """Convert a required 1-based MCP index to 0-based for the Remote Script.

    Raises ValueError when the caller passes 0 or a negative value.
    """
    if index < 1:
        raise ValueError(f"{field_name} must be >= 1 (1-based), got {index}")
    return index - 1


def optional_to_zero_based(index: int, field_name: str = "index") -> int | None:
    """Convert an optional 1-based index to 0-based.

    Returns None when *index* is 0 (meaning "not specified").
    Raises ValueError for negative values.
    """
    if index < 0:
        raise ValueError(f"{field_name} must be >= 0 (0 = unset, 1+ = 1-based), got {index}")
    if index == 0:
        return None
    return index - 1


def arrangement_clip_target(clip_index: int, clip_name: str) -> dict[str, Any]:
    """Build the clip-addressing params for arrangement clip commands.

    A non-empty clip_name wins. It is sent with clip_index -1: the Remote Script
    resolves by name first and ignores the index, while an outdated Remote Script
    that drops clip_name fails on the out-of-range index instead of silently
    changing the track's first clip.
    """
    if clip_name:
        return {"clip_name": clip_name, "clip_index": -1}
    return {"clip_index": to_zero_based(clip_index, "clip_index")}


def normalize_param(value: float, min_val: float, max_val: float) -> float:
    """Normalize a raw parameter value to the 0.0-1.0 range, clamped."""
    if max_val == min_val:
        return 0.0
    normalized = (value - min_val) / (max_val - min_val)
    return max(0.0, min(1.0, normalized))


def denormalize_param(normalized: float, min_val: float, max_val: float) -> float:
    """Convert a normalized 0.0-1.0 value (clamped first) to the raw range."""
    clamped = max(0.0, min(1.0, normalized))
    return min_val + clamped * (max_val - min_val)


def bar_to_beat(bar: int, numerator: int = 4, denominator: int = 4) -> float:
    """Convert a 1-based bar number to a beat position (bar 1 = beat 0)."""
    return (bar - 1) * numerator * (QUARTER_NOTES_PER_WHOLE / denominator)


def beat_to_bar(beat: float, numerator: int = 4, denominator: int = 4) -> int:
    """Convert a 0-based beat position to a 1-based bar number."""
    beats_per_bar = numerator * (QUARTER_NOTES_PER_WHOLE / denominator)
    return int(beat / beats_per_bar) + 1
