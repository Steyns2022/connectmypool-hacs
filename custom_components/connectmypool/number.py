from __future__ import annotations

import asyncio
from typing import Any, Optional

from homeassistant.components.number import NumberEntity
from homeassistant.const import UnitOfTemperature, EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .api import ConnectMyPoolApi, ConnectMyPoolError
from .const import (
    DOMAIN,
    CONF_EXPOSE_SETPOINT_NUMBERS,
    DEFAULT_EXPOSE_SETPOINT_NUMBERS,
    ACTION_SET_HEATER_SET_TEMP,
    ACTION_SET_SOLAR_SET_TEMP,
)
from .entity import ConnectMyPoolEntity


async def async_setup_entry(hass, entry, async_add_entities):
    if not entry.options.get(CONF_EXPOSE_SETPOINT_NUMBERS, DEFAULT_EXPOSE_SETPOINT_NUMBERS):
        return

    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    api: ConnectMyPoolApi = data["api"]
    cfg: dict[str, Any] = data["config"]
    wait_for_execution: bool = data.get("wait_for_execution", True)

    entities: list[NumberEntity] = []

    for h in (cfg.get("heaters") or []):
        entities.append(HeaterSetpointNumber(coordinator, api, wait_for_execution, h))
    for s in (cfg.get("solar_systems") or []):
        entities.append(SolarSetpointNumber(coordinator, api, wait_for_execution, s))

    async_add_entities(entities)


class _BaseSetpointNumber(ConnectMyPoolEntity, NumberEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, api: ConnectMyPoolApi, wait_for_execution: bool, name: str, unique_suffix: str, device_number: int, action_code: int) -> None:
        super().__init__(coordinator, name, unique_suffix)
        self._api = api
        self._wait = bool(wait_for_execution)
        self._device_number = int(device_number)
        self._action_code = int(action_code)

    @property
    def native_unit_of_measurement(self) -> str:
        return UnitOfTemperature.CELSIUS if int(self.coordinator.temperature_scale) == 0 else UnitOfTemperature.FAHRENHEIT

    @property
    def native_min_value(self) -> float:
        return 10 if self.native_unit_of_measurement == UnitOfTemperature.CELSIUS else 50

    @property
    def native_max_value(self) -> float:
        return 40 if self.native_unit_of_measurement == UnitOfTemperature.CELSIUS else 104

    @property
    def native_step(self) -> float:
        return 1

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self._api.pool_action(
                pool_api_code=self.coordinator.pool_api_code,
                action_code=self._action_code,
                device_number=self._device_number,
                value=str(int(round(value))),
                temperature_scale=self.coordinator.temperature_scale,
                wait_for_execution=self._wait,
            )
            await asyncio.sleep(1.0)
            await self.coordinator.async_request_refresh()
        except ConnectMyPoolError as err:
            raise HomeAssistantError(str(err)) from err
        finally:
            self.coordinator.async_follow_up(key=self.unique_id)


class HeaterSetpointNumber(_BaseSetpointNumber):
    def __init__(self, coordinator, api, wait_for_execution, heater_cfg: dict[str, Any]) -> None:
        hn = int(heater_cfg.get("heater_number", 1))
        name = heater_cfg.get("friendly_name") or heater_cfg.get("name") or (f"Heater {hn} Setpoint")
        super().__init__(coordinator, api, wait_for_execution, name, f"heater_{hn}_setpoint", hn, ACTION_SET_HEATER_SET_TEMP)

    @property
    def native_value(self) -> float | None:
        # Uses *pool* setpoint; spa setpoint depends on current selection and isn't safe to set blindly.
        for h in (self.data.get("heaters") or []):
            if int(h.get("heater_number")) == self._device_number:
                try:
                    return float(h.get("set_temperature"))
                except Exception:
                    return None
        return None


class SolarSetpointNumber(_BaseSetpointNumber):
    def __init__(self, coordinator, api, wait_for_execution, solar_cfg: dict[str, Any]) -> None:
        sn = int(solar_cfg.get("solar_number", 1))
        name = solar_cfg.get("friendly_name") or solar_cfg.get("name") or (f"Solar {sn} Setpoint")
        super().__init__(coordinator, api, wait_for_execution, name, f"solar_{sn}_setpoint", sn, ACTION_SET_SOLAR_SET_TEMP)

    @property
    def native_value(self) -> float | None:
        for s in (self.data.get("solar_systems") or []):
            if int(s.get("solar_number")) == self._device_number:
                try:
                    return float(s.get("set_temperature"))
                except Exception:
                    return None
        return None
