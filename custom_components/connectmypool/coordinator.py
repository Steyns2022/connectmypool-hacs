from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any, Callable, Hashable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ConnectMyPoolApi,
    ConnectMyPoolError,
    ConnectMyPoolThrottleError,
    ConnectMyPoolNotConnectedError,
)
from .const import DEFAULT_SCAN_INTERVAL
from .followup import FollowUpRefresher

_LOGGER = logging.getLogger(__name__)


class ConnectMyPoolCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator fetching pool status."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: ConnectMyPoolApi,
        pool_api_code: str,
        temperature_scale: int,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="ConnectMyPool",
            update_interval=timedelta(seconds=max(DEFAULT_SCAN_INTERVAL, int(scan_interval))),
        )
        self.api = api
        self.pool_api_code = pool_api_code
        self.temperature_scale = int(temperature_scale)
        self._follow_up = FollowUpRefresher(
            self.async_refresh,
            lambda: self.api.fast_poll_active,
            create_task=lambda coro: hass.async_create_background_task(
                coro, "connectmypool_follow_up_refresh"
            ),
        )

    def async_follow_up(
        self,
        confirmed: Callable[[], bool] | None = None,
        *,
        key: Hashable | None = None,
        started_at: float | None = None,
    ) -> None:
        """Re-read status shortly after an action until ``confirmed()`` holds.

        Forced reads at ~15/30/60/90/150 s after the action, only inside the
        API's post-action un-throttled window.  A newer action replaces any
        pending schedule (see followup.py); ``key`` identifies the thing
        being changed so a newer target for it supersedes the older one.
        """
        self._follow_up.start(confirmed, key=key, started_at=started_at)

    def async_cancel_follow_up(self) -> None:
        self._follow_up.cancel()

    async def async_shutdown(self) -> None:
        self._follow_up.cancel()
        await super().async_shutdown()

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.api.pool_status(
                self.pool_api_code,
                temperature_scale=self.temperature_scale,
            )
        except ConnectMyPoolThrottleError as err:
            # If throttled, api.pool_status may already have returned cached data.
            raise UpdateFailed(f"Throttled: {err}") from err
        except ConnectMyPoolNotConnectedError as err:
            raise UpdateFailed(f"Pool not connected: {err}") from err
        except ConnectMyPoolError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Unexpected error: {err}") from err
