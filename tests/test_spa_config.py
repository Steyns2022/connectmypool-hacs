"""Unit tests for the spa heating configuration helpers (spa_config.py).

Run from the repo root with:   python -m pytest tests/ -q
"""

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "custom_components", "connectmypool"),
)

from spa_config import SpaConfigError, mismatches, parse_spa_config  # noqa: E402

# Channel layout of a real combined pool + spa (Viron Connect 10).
CHANNELS = [
    {"channel_number": 0, "function": 1, "name": "Filter Pump"},
    {"channel_number": 1, "function": 13, "name": "Overflow"},
    {"channel_number": 2, "function": 12, "name": "Spa Jets"},
    {"channel_number": 3, "function": 9, "name": "Blower"},
    {"channel_number": 6, "function": 18, "name": "Tank Light"},
]
SPA = "Filter Pump=On, Overflow=Off, Spa Jets=On, Blower=On, Tank Light=Off"


def test_parse_full_spa_config():
    assert parse_spa_config(SPA, CHANNELS) == {0: 2, 1: 0, 2: 2, 3: 2, 6: 0}


def test_parse_is_case_and_separator_tolerant():
    text = " spa jets = ON ;\nblower=on,  FILTER PUMP=High Speed "
    assert parse_spa_config(text, CHANNELS) == {2: 2, 3: 2, 0: 5}


@pytest.mark.parametrize("text", ["", None, "  ", ",,"])
def test_empty_config(text):
    assert parse_spa_config(text, CHANNELS) == {}


@pytest.mark.parametrize(
    "text",
    ["Spa Jets", "Hot Tub=On", "Spa Jets=Maybe"],
)
def test_bad_config_raises(text):
    with pytest.raises(SpaConfigError):
        parse_spa_config(text, CHANNELS)


def _status(**modes):
    numbers = {"pump": 0, "overflow": 1, "jets": 2, "blower": 3, "tank": 6}
    return [{"channel_number": numbers[k], "mode": v} for k, v in modes.items()]


def test_spa_config_reached():
    target = parse_spa_config(SPA, CHANNELS)
    status = _status(pump=2, overflow=0, jets=2, blower=2, tank=0)
    assert mismatches(target, status) == {}


def test_blower_not_on_is_a_mismatch():
    target = parse_spa_config(SPA, CHANNELS)
    status = _status(pump=2, overflow=0, jets=2, blower=0, tank=0)
    assert mismatches(target, status) == {3: 0}


def test_pool_mode_layout_mismatches():
    # What the pool reported in Pool mode at discovery.
    target = parse_spa_config(SPA, CHANNELS)
    status = _status(pump=1, overflow=1, jets=0, blower=0, tank=1)
    assert mismatches(target, status) == {0: 1, 1: 1, 2: 0, 3: 0, 6: 1}


def test_missing_or_unreadable_channel_counts_as_mismatch():
    target = {2: 2, 3: 2}
    status = [{"channel_number": 2, "mode": "x"}]
    assert mismatches(target, status) == {2: None, 3: None}
