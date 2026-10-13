"""Pure helpers for ConnectMyPool channel state-machine cycling.

ConnectMyPool exposes no "set channel to mode X" operation for multi-state
channels: the only control is a single "cycle" action that advances the
channel one step through a fixed, firmware-defined sequence.  These helpers
contain the pure arithmetic for that sequence so it can be unit-tested without
Home Assistant or the network.

Sequences (per the ConnectMyPool integration guide and live Viron Connect 10):

    Filter pump:  Off -> Auto -> Medium -> High -> Off   (modes 0, 1, 4, 5)
    Other channels: Off -> Auto -> On -> Off             (modes 0, 1, 2)

The filter pump's sequence depends on the pump fitted, so it is chosen by the
``filter_pump_speeds`` option (see ``FILTER_PUMP_CYCLES``):

    single: Off -> Auto -> On                    (modes 0, 1, 2)
    two:    Off -> Auto -> Medium -> High        (modes 0, 1, 4, 5)
    three:  Off -> Auto -> Low -> Medium -> High (modes 0, 1, 3, 4, 5)
"""

from __future__ import annotations

from typing import Sequence

# Firmware cycle orders, keyed by the ConnectMyPool mode integers in const.CHANNEL_MODES.
FILTER_PUMP_CYCLE: tuple[int, ...] = (0, 1, 4, 5)   # Off -> Auto -> Medium -> High
SIMPLE_CHANNEL_CYCLE: tuple[int, ...] = (0, 1, 2)   # Off -> Auto -> On

FILTER_PUMP_CYCLES: dict[str, tuple[int, ...]] = {
    "single": (0, 1, 2),        # Off -> Auto -> On
    "two": FILTER_PUMP_CYCLE,   # Off -> Auto -> Medium -> High
    "three": (0, 1, 3, 4, 5),   # Off -> Auto -> Low -> Medium -> High
}


def filter_pump_cycle(speeds: str | None) -> tuple[int, ...]:
    """Return the filter pump cycle for a ``filter_pump_speeds`` option value.

    Unknown or missing values fall back to the two-speed cycle, which was the
    only behaviour before the option existed.
    """
    return FILTER_PUMP_CYCLES.get(speeds or "two", FILTER_PUMP_CYCLE)


class CycleError(ValueError):
    """A mode is not part of the channel's cycle sequence."""


def next_in_cycle(current: int, cycle: Sequence[int]) -> int:
    """Return the mode one cycle-step after ``current``.

    Raises CycleError if ``current`` is not part of ``cycle``.
    """
    try:
        index = list(cycle).index(current)
    except ValueError as err:
        raise CycleError(f"mode {current} not in cycle {tuple(cycle)}") from err
    return cycle[(index + 1) % len(cycle)]


def cycle_steps(current: int, desired: int, cycle: Sequence[int]) -> int:
    """Return how many forward cycle actions move ``current`` to ``desired``.

    The controller can only advance forward through the sequence, so the answer
    is the forward distance modulo the cycle length (0 when already there).
    Raises CycleError if either mode is not part of ``cycle``.
    """
    seq = list(cycle)
    try:
        ci = seq.index(current)
        di = seq.index(desired)
    except ValueError as err:
        raise CycleError(
            f"mode not in cycle {tuple(cycle)}: current={current} desired={desired}"
        ) from err
    return (di - ci) % len(seq)
