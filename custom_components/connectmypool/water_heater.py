from __future__ import annotations

import asyncio
from typing import Any, Optional

from homeassistant.components.water_heater import WaterHeaterEntity, WaterHeaterEntityFeature
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError

from .api import ConnectMyPoolApi, ConnectMyPoolError
from .const import DOMAIN, TRI_MODES, ACTION_SET_SOLAR_MODE, ACTION_SET_SOLAR_SET_TEMP
from .entity import ConnectMyPoolEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    api: ConnectMyPoolApi = data["api"]
    cfg: dict[str, Any] = data["config"]
    wait_for_execution: bool = data.get("wait_for_execution", True)

    entities: list[WaterHeaterEntity] = []
    for s in (cfg.get("solar_systems") or []):
        entities.append(ConnectMyPoolSolarWaterHeater(coordinator, api, wait_for_execution, s))
    async_add_entities(entities)


class ConnectMyPoolSolarWaterHeater(ConnectMyPoolEntity, WaterHeaterEntity):
    _attr_supported_features = WaterHeaterEntityFeature.TARGET_TEMPERATURE
    _attr_operation_list = list(TRI_MODES.values())

    def __init__(self, coordinator, api: ConnectMyPoolApi, wait_for_execution: bool, solar_cfg: dict[str, Any]) -> None:
        self._api = api
        self._wait = bool(wait_for_execution)
        self._solar_number = int(solar_cfg.get("solar_number", 1))
        name = solar_cfg.get("friendly_name") or solar_cfg.get("name") or (f"Solar {self._solar_number}" if self._solar_number != 1 else "Solar")
        super().__init__(coordinator, name, f"solar_{self._solar_number}")

    def _unit(self) -> str:
        return UnitOfTemperature.CELSIUS if int(self.coordinator.temperature_scale) == 0 else UnitOfTemperature.FAHRENHEIT

    @property
    def temperature_unit(self) -> str:
        return self._unit()

    @property
    def min_temp(self) -> float:
        return 10 if self.temperature_unit == UnitOfTemperature.CELSIUS else 50

    @property
    def max_temp(self) -> float:
        return 40 if self.temperature_unit == UnitOfTemperature.CELSIUS else 104

    def _find_solar(self) -> dict[str, Any] | None:
        for s in (self.data.get("solar_systems") or []):
            if int(s.get("solar_number")) == self._solar_number:
                return s
        return None

    @property
    def current_temperature(self) -> float | None:
        t = self.data.get("temperature")
        if t is None:
            return None
        try:
            return float(t)
        except Exception:
            return None

    @property
    def target_temperature(self) -> float | None:
        s = self._find_solar()
        if not s:
            return None
        try:
            return float(s.get("set_temperature"))
        except Exception:
            return None

    # WaterHeaterEntity operation strings are free-form; we use the same labels as TRI_MODES.
    @property
    def operation_mode(self) -> str | None:
        s = self._find_solar()
        if not s:
            return None
        try:
            mode = int(s.get("mode"))
        except Exception:
            return None
        return TRI_MODES.get(mode, str(mode))

    @property
    def current_operation(self) -> str | None:
        # Backwards compatibility with older HA property name
        return self.operation_mode

    async def _do_action(self, action_code: int, *, value: str = "") -> None:
        try:
            await self._api.pool_action(
                pool_api_code=self.coordinator.pool_api_code,
                action_code=action_code,
                device_number=self._solar_number,
                value=value,
                temperature_scale=self.coordinator.temperature_scale,
                wait_for_execution=self._wait,
            )
            await asyncio.sleep(1.0)
            await self.coordinator.async_request_refresh()
        except ConnectMyPoolError as err:
            raise HomeAssistantError(str(err)) from err
        finally:
            self.coordinator.async_follow_up(key=self.unique_id)

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        desired = next((k for k, v in TRI_MODES.items() if v == operation_mode), None)
        if desired is None:
            raise HomeAssistantError(f"Unsupported operation_mode: {operation_mode}")
        await self._do_action(ACTION_SET_SOLAR_MODE, value=str(desired))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get("temperature")
        if temp is None:
            return
        try:
            val = str(int(round(float(temp))))
        except Exception as err:
            raise HomeAssistantError(f"Invalid temperature: {temp}") from err
        await self._do_action(ACTION_SET_SOLAR_SET_TEMP, value=val)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self._find_solar() or {}
        mode = s.get("mode")
        try:
            mode_int = int(mode) if mode is not None else None
        except Exception:
            mode_int = None
        return {
            "solar_number": self._solar_number,
            "mode": mode_int,
            "set_temperature_raw": s.get("set_temperature"),
        }
