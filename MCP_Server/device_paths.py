"""Device paths: address a device at any depth of rack nesting.

A path alternates devices and chains, starting with a device on the track and
ending on a device (or, for chain listings, a chain). Callers number everything
from 1, like the rest of the tools. Two spellings are accepted:

- a string: ``"1/c2/1/c1/1"``, where a bare number is a device and ``cN`` is a
  chain: device 1 on the track, its chain 2, the first device on that chain,
  that device's chain 1, and the first device on that chain;
- a list: ``[1, ("chain", 2), 1, ("chain", 1), 1]``, where a bare number is a
  device and ``("chain", N)`` or ``["chain", N]`` (the JSON form) is a chain.

The Remote Script receives the path 0-based as ``[["device", 0], ["chain", 1], ...]``.
"""

import re
from collections.abc import Sequence
from typing import Any

DEVICE = "device"
CHAIN = "chain"
# A request that carries a device_path also carries this device_index. A Remote
# Script that predates paths ignores the path and rejects the index as out of
# range, so it fails instead of changing the first device on the track.
UNSUPPORTED_PATH_GUARD_INDEX = -1

_CHAIN_SEGMENT = re.compile(r"c(?:hain)?\s*(\d+)", re.IGNORECASE)
_DEVICE_SEGMENT = re.compile(r"d?(?:evice)?\s*(\d+)", re.IGNORECASE)

DevicePath = str | Sequence[Any]
WirePath = list[list[Any]]


def _bad(path: Any, why: str) -> ValueError:
    return ValueError(
        f"Invalid device_path {path!r}: {why}. "
        "Use numbers for devices and cN for chains, e.g. '1/c2/1' "
        "(device 1, its chain 2, the first device on that chain)."
    )


def _position(number: int, path: Any, kind: str) -> int:
    if number < 1:
        raise _bad(path, f"{kind} numbers start at 1, got {number}")
    return number - 1


def _string_segments(path: str) -> list[tuple[str, int]]:
    segments: list[tuple[str, int]] = []
    if not path.strip():
        return segments
    for raw in path.strip().split("/"):
        text = raw.strip()
        if not text:
            raise _bad(path, "empty segment")
        chain = _CHAIN_SEGMENT.fullmatch(text)
        device = _DEVICE_SEGMENT.fullmatch(text)
        if chain:
            segments.append((CHAIN, int(chain.group(1))))
        elif device:
            segments.append((DEVICE, int(device.group(1))))
        else:
            raise _bad(path, f"cannot read segment {text!r}")
    return segments


def _list_segments(path: Sequence[Any]) -> list[tuple[str, int]]:
    segments: list[tuple[str, int]] = []
    for item in path:
        if isinstance(item, bool):
            raise _bad(path, f"cannot read segment {item!r}")
        if isinstance(item, int):
            segments.append((DEVICE, item))
        elif (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and str(item[0]).lower() in (CHAIN, "c", DEVICE, "d")
            and isinstance(item[1], int)
            and not isinstance(item[1], bool)
        ):
            kind = CHAIN if str(item[0]).lower() in (CHAIN, "c") else DEVICE
            segments.append((kind, item[1]))
        else:
            raise _bad(path, f"cannot read segment {item!r}")
    return segments


def parse_device_path(path: DevicePath, *, allow_chain_end: bool = False) -> WirePath:
    """Turn a 1-based device path into the 0-based wire form.

    Raises ValueError when the path is empty, malformed, does not alternate
    device and chain from a device, or does not end where it must: on a device,
    or on a chain too when *allow_chain_end* is set.
    """
    if isinstance(path, str):
        segments = _string_segments(path)
    elif isinstance(path, Sequence):
        segments = _list_segments(path)
    else:
        raise _bad(path, "expected a string or a list")
    if not segments:
        raise _bad(path, "it is empty")

    wire: WirePath = []
    for position, (kind, number) in enumerate(segments):
        expected = DEVICE if position % 2 == 0 else CHAIN
        if kind != expected:
            raise _bad(
                path,
                f"segment {position + 1} must be a {expected}, not a {kind}; "
                "a path alternates device and chain, starting with a device",
            )
        wire.append([kind, _position(number, path, kind)])
    if wire[-1][0] == CHAIN and not allow_chain_end:
        raise _bad(path, "it must end on a device, not a chain")
    return wire


def path_request(track_index: int, wire: WirePath) -> dict[str, Any]:
    """Build the Remote Script params that address a device by *wire* path."""
    return {
        "track_index": track_index,
        "device_index": UNSUPPORTED_PATH_GUARD_INDEX,
        "chain_index": None,
        "device_path": wire,
    }
