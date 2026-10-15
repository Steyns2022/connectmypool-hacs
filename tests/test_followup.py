"""Unit tests for the post-action follow-up refresh schedule (followup.py).

Run from the repo root with:   python -m pytest tests/ -q

Time is virtual: ``FakeTime`` replaces the clock and sleep so the schedule's
timing can be asserted exactly without waiting.
"""

import asyncio
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "custom_components", "connectmypool"),
)

from followup import FOLLOW_UP_DELAYS, FollowUpRefresher  # noqa: E402


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self._waiters: list[tuple[float, asyncio.Future]] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append((self.now + delay, fut))
        await fut

    async def advance_to(self, t: float) -> None:
        """Move virtual time to ``t``, waking sleepers in order."""
        while True:
            await _settle()
            self._waiters = [w for w in self._waiters if not w[1].done()]
            due = sorted((w for w in self._waiters if w[0] <= t), key=lambda w: w[0])
            if not due:
                break
            when, fut = due[0]
            self.now = when
            fut.set_result(None)
        self.now = t
        await _settle()


class Harness:
    """A refresher wired to virtual time and a fake status source."""

    def __init__(self, window_until: float = 300.0) -> None:
        self.time = FakeTime()
        self.window_until = window_until
        self.refreshed_at: list[float] = []
        self.state: dict[str, int] = {}
        # state changes that the "cloud" reports from the Nth refresh onwards
        self.reports: dict[int, dict[str, int]] = {}
        self.fail_on: set[int] = set()
        self.refresher = FollowUpRefresher(
            self.refresh,
            lambda: self.time.now < self.window_until,
            sleep=self.time.sleep,
            clock=self.time.clock,
        )

    async def refresh(self) -> None:
        self.refreshed_at.append(self.time.now)
        n = len(self.refreshed_at)
        if n in self.fail_on:
            raise RuntimeError("cloud error")
        self.state.update(self.reports.get(n, {}))

    def target(self, name: str, value: int = 1):
        return lambda: self.state.get(name) == value


def run(coro):
    return asyncio.run(coro)


def test_default_schedule_inside_unthrottled_window():
    assert FOLLOW_UP_DELAYS == (15.0, 30.0, 60.0, 90.0, 150.0)
    assert max(FOLLOW_UP_DELAYS) < 300


def test_full_schedule_timing_when_never_confirmed():
    async def go():
        h = Harness()
        task = h.refresher.start(h.target("jets"))
        await h.time.advance_to(400)
        assert task.done()
        assert h.refreshed_at == [15, 30, 60, 90, 150]
        assert not h.refresher.active

    run(go())


def test_no_target_runs_full_schedule():
    async def go():
        h = Harness()
        h.refresher.start()
        await h.time.advance_to(400)
        assert h.refreshed_at == [15, 30, 60, 90, 150]

    run(go())


def test_stops_early_once_target_seen():
    async def go():
        h = Harness()
        h.reports = {2: {"jets": 1}}
        h.refresher.start(h.target("jets"))
        await h.time.advance_to(400)
        assert h.refreshed_at == [15, 30]

    run(go())


def test_no_read_when_already_confirmed_elsewhere():
    async def go():
        h = Harness()
        h.refresher.start(h.target("jets"))
        await h.time.advance_to(5)
        h.state["jets"] = 1  # e.g. the regular poll saw it first
        await h.time.advance_to(400)
        assert h.refreshed_at == []

    run(go())


def test_newer_action_replaces_pending_schedule():
    async def go():
        h = Harness()
        first = h.refresher.start(h.target("jets"))
        await h.time.advance_to(20)
        assert h.refreshed_at == [15]
        second = h.refresher.start(h.target("blower"))
        await h.time.advance_to(1000)
        assert first.cancelled()
        assert second.done()
        # Timing restarts from the newer action; no stacked reads from the first.
        assert h.refreshed_at == [15, 35, 50, 80, 110, 170]

    run(go())


def test_replacement_keeps_waiting_for_unseen_earlier_target():
    async def go():
        h = Harness()
        h.refresher.start(h.target("jets"), key="jets")
        await h.time.advance_to(1)
        h.reports = {1: {"blower": 1}, 3: {"jets": 1}}
        h.refresher.start(h.target("blower"), key="blower")
        await h.time.advance_to(1000)
        # blower seen at the 1st read, jets only at the 3rd: stop there.
        assert h.refreshed_at == [16, 31, 61]

    run(go())


def test_same_key_newer_target_supersedes_older():
    async def go():
        h = Harness()
        h.refresher.start(h.target("jets", 2), key="jets")  # never reported
        await h.time.advance_to(1)
        h.reports = {1: {"jets": 0}}
        h.refresher.start(h.target("jets", 0), key="jets")
        await h.time.advance_to(1000)
        assert h.refreshed_at == [16]

    run(go())


def test_no_polling_once_window_closed():
    async def go():
        h = Harness(window_until=100)
        h.refresher.start(h.target("jets"))
        await h.time.advance_to(1000)
        assert h.refreshed_at == [15, 30, 60, 90]

    run(go())


def test_no_polling_outside_window():
    async def go():
        h = Harness(window_until=0)
        task = h.refresher.start(h.target("jets"))
        await h.time.advance_to(1000)
        assert task.done()
        assert h.refreshed_at == []

    run(go())


def test_started_at_skips_slots_already_passed():
    async def go():
        h = Harness()
        h.time.now = 35.0  # e.g. after the channel select's in-call confirmation loop
        h.refresher.start(h.target("jets"), started_at=0.0)
        await h.time.advance_to(1000)
        assert h.refreshed_at == [60, 90, 150]

    run(go())


def test_refresh_error_does_not_stop_schedule():
    async def go():
        h = Harness()
        h.fail_on = {1}
        h.reports = {2: {"jets": 1}}
        h.refresher.start(h.target("jets"))
        await h.time.advance_to(1000)
        assert h.refreshed_at == [15, 30]

    run(go())


def test_cancel_stops_schedule():
    async def go():
        h = Harness()
        task = h.refresher.start(h.target("jets"))
        await h.time.advance_to(20)
        h.refresher.cancel()
        await h.time.advance_to(1000)
        assert task.cancelled()
        assert h.refreshed_at == [15]

    run(go())
