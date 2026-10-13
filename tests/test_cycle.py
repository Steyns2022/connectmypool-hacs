"""Unit tests for the pure channel cycle state-machine helpers.

These test the arithmetic that decides how many single-step "cycle" actions are
needed to reach a target mode, independent of Home Assistant or the network.

Run from the repo root with:   python -m pytest tests/ -q
"""

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "custom_components", "connectmypool"),
)

from cycle import (  # noqa: E402
    FILTER_PUMP_CYCLE,
    FILTER_PUMP_CYCLES,
    SIMPLE_CHANNEL_CYCLE,
    CycleError,
    cycle_steps,
    filter_pump_cycle,
    next_in_cycle,
)

# Filter pump: Off(0) -> Auto(1) -> Medium(4) -> High(5) -> Off
# Simple:      Off(0) -> Auto(1) -> On(2) -> Off


@pytest.mark.parametrize(
    "current,desired,expected",
    [
        (0, 0, 0),  # no-op
        (1, 1, 0),  # no-op
        (0, 1, 1),  # Off -> Auto
        (1, 4, 1),  # Auto -> Medium
        (1, 5, 2),  # Auto -> High (the common multi-step case)
        (1, 0, 3),  # Auto -> Off (full wrap)
        (5, 0, 1),  # High -> Off
        (5, 1, 2),  # High -> Auto
        (4, 5, 1),  # Medium -> High
    ],
)
def test_filter_cycle_steps(current, desired, expected):
    assert cycle_steps(current, desired, FILTER_PUMP_CYCLE) == expected


@pytest.mark.parametrize(
    "current,desired,expected",
    [
        (0, 0, 0),  # no-op
        (0, 1, 1),  # Off -> Auto
        (0, 2, 2),  # Off -> On (the common turn-on case)
        (1, 2, 1),  # Auto -> On
        (2, 0, 1),  # On -> Off
        (2, 1, 2),  # On -> Auto
        (1, 0, 2),  # Auto -> Off
    ],
)
def test_simple_cycle_steps(current, desired, expected):
    assert cycle_steps(current, desired, SIMPLE_CHANNEL_CYCLE) == expected


def test_steps_never_exceed_cycle_length():
    for cyc in (FILTER_PUMP_CYCLE, SIMPLE_CHANNEL_CYCLE):
        for c in cyc:
            for d in cyc:
                assert 0 <= cycle_steps(c, d, cyc) < len(cyc)


@pytest.mark.parametrize(
    "current,expected",
    [(0, 1), (1, 4), (4, 5), (5, 0)],  # one step advances through the filter sequence
)
def test_filter_next_in_cycle(current, expected):
    assert next_in_cycle(current, FILTER_PUMP_CYCLE) == expected


@pytest.mark.parametrize("current,expected", [(0, 1), (1, 2), (2, 0)])
def test_simple_next_in_cycle(current, expected):
    assert next_in_cycle(current, SIMPLE_CHANNEL_CYCLE) == expected


def test_walking_the_full_sequence_one_step_at_a_time():
    # Simulate the integration's per-step loop: from each start, take the
    # computed number of steps, advancing one at a time, and confirm arrival.
    for cyc in (FILTER_PUMP_CYCLE, SIMPLE_CHANNEL_CYCLE):
        for start in cyc:
            for target in cyc:
                steps = cycle_steps(start, target, cyc)
                mode = start
                for _ in range(steps):
                    mode = next_in_cycle(mode, cyc)
                assert mode == target


def test_unknown_mode_raises():
    with pytest.raises(CycleError):
        cycle_steps(99, 0, FILTER_PUMP_CYCLE)
    with pytest.raises(CycleError):
        cycle_steps(0, 99, SIMPLE_CHANNEL_CYCLE)
    with pytest.raises(CycleError):
        next_in_cycle(3, SIMPLE_CHANNEL_CYCLE)  # 3 is not a simple-channel mode


# Filter pump type option: single / two / three speed.


@pytest.mark.parametrize(
    "speeds,expected",
    [
        ("single", (0, 1, 2)),
        ("two", (0, 1, 4, 5)),
        ("three", (0, 1, 3, 4, 5)),
        (None, (0, 1, 4, 5)),  # missing option keeps the old two-speed default
        ("bogus", (0, 1, 4, 5)),
    ],
)
def test_filter_pump_cycle_option(speeds, expected):
    assert filter_pump_cycle(speeds) == expected


@pytest.mark.parametrize(
    "current,desired,expected",
    [
        (1, 2, 1),  # Auto -> On
        (2, 0, 1),  # On -> Off
        (0, 1, 1),  # Off -> Auto
        (1, 0, 2),  # Auto -> Off (passes through On)
        (2, 1, 2),  # On -> Auto (passes through Off)
    ],
)
def test_single_speed_filter_steps(current, desired, expected):
    assert cycle_steps(current, desired, filter_pump_cycle("single")) == expected


def test_single_speed_pump_on_is_not_rejected():
    # A single-speed pump reports On (2), which the two-speed cycle rejects.
    with pytest.raises(CycleError):
        cycle_steps(2, 0, FILTER_PUMP_CYCLE)
    assert cycle_steps(2, 0, filter_pump_cycle("single")) == 1


@pytest.mark.parametrize(
    "current,desired,expected",
    [(1, 3, 1), (3, 5, 2), (5, 0, 1), (1, 0, 4)],
)
def test_three_speed_filter_steps(current, desired, expected):
    assert cycle_steps(current, desired, filter_pump_cycle("three")) == expected


def test_every_pump_type_walks_to_every_target():
    for cyc in FILTER_PUMP_CYCLES.values():
        for start in cyc:
            for target in cyc:
                mode = start
                for _ in range(cycle_steps(start, target, cyc)):
                    mode = next_in_cycle(mode, cyc)
                assert mode == target
