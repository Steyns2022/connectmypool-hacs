from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.exceptions import HomeAssistantError

from .api import ConnectMyPoolApi, ConnectMyPoolError
from .const import DOMAIN, ACTION_SET_LIGHT_MODE, ACTION_SET_LIGHT_COLOR, TRI_MODES
from .entity import ConnectMyPoolEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    api: ConnectMyPoolApi = data["api"]
    cfg: dict[str, Any] = data["config"]
    wait_for_execution: bool = data.get("wait_for_execution", True)

    entities: list[LightEntity] = []
    for lz in (cfg.get("lighting_zones") or []):
        entities.append(LightingZoneLight(coordinator, api, wait_for_execution, lz))
    async_add_entities(entities)


class LightingZoneLight(ConnectMyPoolEntity, LightEntity):
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, coordinator, api: ConnectMyPoolApi, wait_for_execution: bool, lz: dict[str, Any]) -> None:
        self._api = api
        self._wait = bool(wait_for_execution)
        self._lz_number = int(lz["lighting_zone_number"])
        self._color_enabled = bool(lz.get("color_enabled", False))

        name = lz.get("friendly_name") or lz.get("name") or f"Lighting Zone {self._lz_number}"
        super().__init__(coordinator, name, f"lightzone_{self._lz_number}")

        # Effect list from config
        self._effects_by_name: dict[str, int] = {}
        if self._color_enabled:
            for c in (lz.get("colors_available") or []):
                try:
                    num = int(c.get("color_number"))
                    nm = str(c.get("color_name"))
                    self._effects_by_name[nm] = num
                except Exception:
                    continue
            self._attr_effect_list = list(self._effects_by_name.keys())

    def _find_lz(self) -> dict[str, Any] | None:
        for lz in (self.data.get("lighting_zones") or []):
            if int(lz.get("lighting_zone_number")) == self._lz_number:
                return lz
        return None

    @property
    def is_on(self) -> bool | None:
        lz = self._find_lz()
        if not lz:
            return None
        try:
            mode = int(lz.get("mode"))
        except Exception:
            return None
        # Treat Auto as "on" for UI purposes; the separate Mode select exposes Auto explicitly.
        return mode != 0

    @property
    def effect(self) -> str | None:
        if not self._color_enabled:
            return None
        lz = self._find_lz()
        if not lz:
            return None
        col = lz.get("color")
        if col is None:
            return None
        try:
            col_num = int(col)
        except Exception:
            return None
        for name, num in self._effects_by_name.items():
            if num == col_num:
                return name
        return str(col_num)

    def _mode(self) -> int | None:
        try:
            return int((self._find_lz() or {}).get("mode"))
        except (TypeError, ValueError):
            return None

    def _color(self) -> int | None:
        try:
            return int((self._find_lz() or {}).get("color"))
        except (TypeError, ValueError):
            return None

    async def _do_action(self, action_code: int, *, value: str = "") -> None:
        if action_code == ACTION_SET_LIGHT_MODE:
            confirmed = lambda: self._mode() == int(value)  # noqa: E731
        else:
            confirmed = lambda: self._color() == int(value)  # noqa: E731
        try:
            await self._api.pool_action(
                pool_api_code=self.coordinator.pool_api_code,
                action_code=action_code,
                device_number=self._lz_number,
                value=value,
                temperature_scale=self.coordinator.temperature_scale,
                wait_for_execution=self._wait,
            )
            await asyncio.sleep(1.0)
            await self.coordinator.async_request_refresh()
        except ConnectMyPoolError as err:
            raise HomeAssistantError(str(err)) from err
        finally:
            # Also after an error: a slow reply often still executes.
            self.coordinator.async_follow_up(confirmed, key=(self.unique_id, action_code))

    async def async_turn_on(self, **kwargs: Any) -> None:
        # Turn on sets mode to "On" (2). If you want Auto, use the corresponding Mode select entity.
        await self._do_action(ACTION_SET_LIGHT_MODE, value="2")

        effect = kwargs.get("effect")
        if effect and self._color_enabled:
            num = self._effects_by_name.get(effect)
            if num is None:
                raise HomeAssistantError(f"Unknown effect: {effect}")
            await self._do_action(ACTION_SET_LIGHT_COLOR, value=str(num))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._do_action(ACTION_SET_LIGHT_MODE, value="0")

    async def async_set_effect(self, effect: str) -> None:
        if not self._color_enabled:
            raise HomeAssistantError("This lighting zone does not support colors/effects.")
        num = self._effects_by_name.get(effect)
        if num is None:
            raise HomeAssistantError(f"Unknown effect: {effect}")
        await self._do_action(ACTION_SET_LIGHT_COLOR, value=str(num))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lz = self._find_lz() or {}
        mode = lz.get("mode")
        try:
            mode_int = int(mode) if mode is not None else None
        except Exception:
            mode_int = None
        return {
            "lighting_zone_number": self._lz_number,
            "mode": mode_int,
            "mode_label": None if mode_int is None else TRI_MODES.get(mode_int, str(mode_int)),
            "color_number": lz.get("color"),
        }
