from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from .const import DEFAULT_BASE_URL, FAILURE_CODE_THROTTLED, FAILURE_CODE_POOL_NOT_CONNECTED

_LOGGER = logging.getLogger(__name__)


class ConnectMyPoolError(Exception):
    """Base error for ConnectMyPool."""


class ConnectMyPoolAuthError(ConnectMyPoolError):
    """Invalid API code / API not enabled / invalid key."""


class ConnectMyPoolThrottleError(ConnectMyPoolError):
    """Cloud rate limit / throttle exceeded."""


class ConnectMyPoolNotConnectedError(ConnectMyPoolError):
    """Pool controller is currently not connected to the cloud."""


class ConnectMyPoolActionError(ConnectMyPoolError):
    """Action failed or invalid."""


def _raise_for_failure(payload: dict[str, Any]) -> None:
    """Raise a typed exception if API returned a failure payload.

    The guide documents errors as:
      { failure_code: integer, failure_description: string }
    """
    if "failure_code" not in payload:
        return

    code = int(payload.get("failure_code", 1))
    desc = str(payload.get("failure_description", "Unknown error"))

    if code in (3, 4, 5):
        raise ConnectMyPoolAuthError(f"{code}: {desc}")
    if code == FAILURE_CODE_THROTTLED:
        raise ConnectMyPoolThrottleError(f"{code}: {desc}")
    if code == FAILURE_CODE_POOL_NOT_CONNECTED:
        raise ConnectMyPoolNotConnectedError(f"{code}: {desc}")
    # Everything else is a logical / validation error for a request
    raise ConnectMyPoolActionError(f"{code}: {desc}")


class ConnectMyPoolApi:
    """Minimal async client for the ConnectMyPool cloud API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = DEFAULT_BASE_URL,
        *,
        min_poll_seconds: int = 60,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._min_poll_seconds = max(1, int(min_poll_seconds))

        # Local caching to gracefully survive the cloud's 60s throttle
        self._last_status_at: float | None = None
        self._last_config_at: float | None = None
        self._cached_status: dict[str, Any] | None = None
        self._cached_config: dict[str, Any] | None = None

        # After sending any instruction, the cloud allows non-throttled calls for ~5 minutes (per guide)
        self._fast_poll_until: float | None = None

        self._action_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return self._base_url

    def _fast_poll_active(self) -> bool:
        return self._fast_poll_until is not None and time.monotonic() < self._fast_poll_until

    @property
    def fast_poll_active(self) -> bool:
        """True while the post-action un-throttled window is open."""
        return self._fast_poll_active()

    def _mark_fast_poll(self, seconds: int = 300) -> None:
        self._fast_poll_until = time.monotonic() + max(0, int(seconds))

    async def _post(self, path: str, json_data: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            async with self._session.post(
                url,
                json=json_data,
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers={"Accept": "application/json"},
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
        except asyncio.TimeoutError as err:
            raise ConnectMyPoolError("Timeout talking to ConnectMyPool") from err
        except aiohttp.ClientResponseError as err:
            raise ConnectMyPoolError(f"HTTP {err.status} from ConnectMyPool") from err
        except aiohttp.ClientError as err:
            raise ConnectMyPoolError("Network error talking to ConnectMyPool") from err

        # Some clients have seen list payloads; normalize.
        if isinstance(payload, list):
            payload = payload[0] if payload else {}

        if not isinstance(payload, dict):
            raise ConnectMyPoolError(f"Unexpected payload type: {type(payload)}")

        _raise_for_failure(payload)
        return payload

    async def pool_config(self, pool_api_code: str, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if (
            not force
            and not self._fast_poll_active()
            and self._cached_config is not None
            and self._last_config_at is not None
            and (now - self._last_config_at) < self._min_poll_seconds
        ):
            return self._cached_config

        try:
            payload = await self._post("/api/poolconfig", {"pool_api_code": pool_api_code})
        except ConnectMyPoolThrottleError:
            if self._cached_config is not None:
                _LOGGER.debug("poolconfig throttled; returning cached config")
                return self._cached_config
            raise

        self._cached_config = payload
        self._last_config_at = now
        return payload

    async def pool_status(self, pool_api_code: str, temperature_scale: int = 0, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if (
            not force
            and not self._fast_poll_active()
            and self._cached_status is not None
            and self._last_status_at is not None
            and (now - self._last_status_at) < self._min_poll_seconds
        ):
            return self._cached_status

        try:
            payload = await self._post(
                "/api/poolstatus",
                {"pool_api_code": pool_api_code, "temperature_scale": int(temperature_scale)},
            )
        except ConnectMyPoolThrottleError:
            if self._cached_status is not None:
                _LOGGER.debug("poolstatus throttled; returning cached status")
                return self._cached_status
            raise

        self._cached_status = payload
        self._last_status_at = now
        return payload

    async def pool_action(
        self,
        pool_api_code: str,
        action_code: int,
        *,
        device_number: int = 0,
        value: str = "",
        temperature_scale: int = 0,
        wait_for_execution: bool = True,
    ) -> dict[str, Any]:
        # Serialize actions; it reduces "UI flip-flop" and avoids racing refreshes.
        async with self._action_lock:
            try:
                payload = await self._post(
                    "/api/poolaction",
                    {
                        "pool_api_code": pool_api_code,
                        "action_code": int(action_code),
                        "device_number": int(device_number),
                        "value": str(value),
                        "temperature_scale": int(temperature_scale),
                        "wait_for_execution": bool(wait_for_execution),
                    },
                    # With wait_for_execution the cloud holds the reply until the
                    # pool has executed the instruction, which can exceed 30 s.
                    timeout=90 if wait_for_execution else 30,
                )
            finally:
                # After an instruction reaches the cloud, calls are not throttled
                # for ~5 minutes (per guide).  Mark it even if our request timed
                # out, so the follow-up status check isn't served from cache.
                self._mark_fast_poll(300)
            return payload

    async def pool_action_status(self, pool_api_code: str, action_number: int) -> dict[str, Any]:
        return await self._post(
            "/api/poolactionstatus",
            {"pool_api_code": pool_api_code, "action_number": int(action_number)},
        )
