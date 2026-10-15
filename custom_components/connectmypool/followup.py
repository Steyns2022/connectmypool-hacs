"""Follow-up status refreshes after a pool action (pure asyncio, no HA imports).

The ConnectMyPool cloud acknowledges an action before the controller reports
it, and ``poolstatus`` then lags by anything from seconds to minutes.  The
regular poll (>= 60 s) can leave Home Assistant showing the old state for
several minutes, so after each action a short schedule of forced status reads
runs at ``FOLLOW_UP_DELAYS`` seconds after the action, stopping early once
every pending target state is seen.

Rules:
* Reads only happen while the API's post-action un-throttled window is open
  (``window_open()``); once it closes the schedule ends, so nothing ever polls
  faster than the API allows.
* A newer action replaces (never stacks on) a pending schedule: its timing
  restarts from the new action, and it keeps waiting for any earlier target
  that has not been seen yet -- unless the newer action has the same ``key``
  (e.g. the same entity), whose newer target then supersedes the old one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Coroutine, Hashable, Optional

_LOGGER = logging.getLogger(__name__)

# Seconds after the action at which status is re-read.  All well inside the
# ~300 s un-throttled window the cloud opens after any action.
FOLLOW_UP_DELAYS: tuple[float, ...] = (15.0, 30.0, 60.0, 90.0, 150.0)

Check = Optional[Callable[[], bool]]


class FollowUpRefresher:
    """Runs at most one follow-up refresh schedule at a time."""

    def __init__(
        self,
        refresh: Callable[[], Awaitable[None]],
        window_open: Callable[[], bool],
        *,
        create_task: Callable[[Coroutine], asyncio.Task] | None = None,
        delays: tuple[float, ...] = FOLLOW_UP_DELAYS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._refresh = refresh
        self._window_open = window_open
        self._create_task = create_task or asyncio.ensure_future
        self._delays = tuple(sorted(delays))
        self._sleep = sleep
        self._clock = clock
        self._task: asyncio.Task | None = None
        # Targets still awaited, by key.  A ``None`` check = an action without a
        # checkable target, which keeps the schedule running to the end.
        self._checks: dict[Hashable, Check] = {}

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def _satisfied(self, check: Check) -> bool:
        if check is None:
            return False
        try:
            return bool(check())
        except Exception:  # noqa: BLE001 - a broken check must not kill polling
            return False

    def _all_confirmed(self) -> bool:
        return bool(self._checks) and all(self._satisfied(c) for c in self._checks.values())

    def start(
        self,
        confirmed: Check = None,
        *,
        key: Hashable | None = None,
        started_at: float | None = None,
    ) -> asyncio.Task:
        """(Re)start the schedule for an action sent at ``started_at`` (default: now).

        Delays that already lie in the past (e.g. after an in-call
        confirmation loop) are skipped rather than fired back to back.
        """
        if self.active:
            self._task.cancel()
            # Keep earlier targets that have not been seen yet.
            self._checks = {k: c for k, c in self._checks.items() if not self._satisfied(c)}
        else:
            self._checks = {}
        self._checks[object() if key is None else key] = confirmed
        start = self._clock() if started_at is None else started_at
        self._task = self._create_task(self._run(start))
        return self._task

    def cancel(self) -> None:
        if self.active:
            self._task.cancel()
        self._task = None
        self._checks = {}

    async def _run(self, start: float) -> None:
        for delay in self._delays:
            wait = start + delay - self._clock()
            if wait < 0:
                continue  # this slot already passed
            if wait > 0:
                await self._sleep(wait)
            if self._all_confirmed():
                break
            if not self._window_open():
                _LOGGER.debug("Follow-up refresh stopped: un-throttled window closed")
                break
            try:
                # Shielded: a newer action cancelling this schedule must not
                # abort a status fetch that is already in flight.
                await asyncio.shield(self._refresh())
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Follow-up refresh failed: %s", err)
            if self._all_confirmed():
                _LOGGER.debug("Follow-up refresh: target state confirmed")
                break
        self._checks = {}
