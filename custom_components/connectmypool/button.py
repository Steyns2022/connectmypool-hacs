from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .api import ConnectMyPoolApi, ConnectMyPoolError
from .const import DOMAIN, ACTION_LIGHT_SYNC
from .entity import ConnectMyPoolEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    api: ConnectMyPoolApi = data["api"]
    cfg: dict[str, Any] = data["config"]
    wait_for_execution: bool = data.get("wait_for_execution", True)

    entities: list[ButtonEntity] = []
    for lz in (cfg.get("lighting_zones") or []):
        entities.append(LightingZoneSyncButton(coordinator, api, wait_for_execution, lz))
    async_add_entities(entities)


class LightingZoneSyncButton(ConnectMyPoolEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, api: ConnectMyPoolApi, wait_for_execution: bool, lz: dict[str, Any]) -> None:
        self._api = api
        self._wait = bool(wait_for_execution)
        self._lz_number = int(lz["lighting_zone_number"])
        name = (lz.get("friendly_name") or lz.get("name") or f"Lighting Zone {self._lz_number}") + " Color Sync"
        super().__init__(coordinator, name, f"lightzone_{self._lz_number}_color_sync")

    async def async_press(self) -> None:
        try:
            await self._api.pool_action(
                pool_api_code=self.coordinator.pool_api_code,
                action_code=ACTION_LIGHT_SYNC,
                device_number=self._lz_number,
                value="",
                temperature_scale=self.coordinator.temperature_scale,
                wait_for_execution=self._wait,
            )
            await asyncio.sleep(1.0)
            await self.coordinator.async_request_refresh()
        except ConnectMyPoolError as err:
            raise HomeAssistantError(str(err)) from err
        finally:
            # No status field reflects a sync, so run the full schedule.
            self.coordinator.async_follow_up(key=self.unique_id)
