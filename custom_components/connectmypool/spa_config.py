"""Pure helpers for the "heat the spa only" channel configuration.

The spa heating configuration is a short text option naming the channel modes
that must be running before the heater may start, for example:

    Filter Pump=On, Overflow=Off, Spa Jets=On, Blower=On, Tank Light=Off

Channel names are matched (case-insensitively) against the names reported by
poolconfig.  No Home Assistant or network dependencies, so it is unit tested.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

MODE_BY_NAME: dict[str, int] = {
    "off": 0,
    "auto": 1,
    "on": 2,
    "low": 3,
    "low speed": 3,
    "medium": 4,
    "medium speed": 4,
    "high": 5,
    "high speed": 5,
}


class SpaConfigError(ValueError):
    """The spa heating configuration text could not be understood."""


def parse_spa_config(text: str | None, channels: Iterable[dict[str, Any]]) -> dict[int, int]:
    """Return {channel_number: required mode} from the configuration text.

    Empty text returns an empty mapping.  Raises SpaConfigError for a malformed
    entry, an unknown channel name or an unknown mode.
    """
    by_name: dict[str, int] = {}
    for ch in channels:
        try:
            by_name[str(ch.get("name") or "").strip().lower()] = int(ch["channel_number"])
        except (KeyError, TypeError, ValueError):
            continue

    result: dict[int, int] = {}
    for raw in re.split(r"[,;\n]", text or ""):
        item = raw.strip()
        if not item:
            continue
        if "=" not in item:
            raise SpaConfigError(f"Expected 'Channel=Mode', got '{item}'")
        name, mode = (part.strip() for part in item.split("=", 1))
        number = by_name.get(name.lower())
        if number is None:
            raise SpaConfigError(f"Unknown channel '{name}'")
        mode_value = MODE_BY_NAME.get(mode.lower())
        if mode_value is None:
            raise SpaConfigError(f"Unknown mode '{mode}' for '{name}'")
        result[number] = mode_value
    return result


def mismatches(target: dict[int, int], status_channels: Iterable[dict[str, Any]]) -> dict[int, int | None]:
    """Return {channel_number: reported mode} for channels not in their target mode.

    A channel missing from the status (or with an unreadable mode) is reported
    with mode None and counts as a mismatch.
    """
    current: dict[int, int | None] = {}
    for c in status_channels:
        try:
            number = int(c.get("channel_number"))
        except (TypeError, ValueError):
            continue
        try:
            current[number] = int(c.get("mode"))
        except (TypeError, ValueError):
            current[number] = None
    return {
        number: current.get(number)
        for number, mode in target.items()
        if current.get(number) != mode
    }
