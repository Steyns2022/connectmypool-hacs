from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from homeassistant.components.climate import ClimateEntity, HVACMode, ClimateEntityFeature
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .api import ConnectMyPoolApi, ConnectMyPoolError
from .const import (
    DOMAIN,
    ACTION_SET_HEATER_MODE,
    ACTION_SET_HEATER_SET_TEMP,
    ACTION_SET_HEAT_COOL,
    ACTION_SET_POOL_SPA,
    ACTION_SET_ACTIVE_FAVOURITE,
    ALL_AUTO_FAVOURITE_NAME,
    CHANNEL_MODES,
    CONF_HEAT_SPA_ONLY,
    CONF_SPA_HEAT_CONFIG,
    DEFAULT_HEAT_SPA_ONLY,
    DEFAULT_SPA_HEAT_CONFIG,
    HEATER_MODES,
    HEAT_COOL,
    POOL_SPA,
    SPA_CONFIG_CHECK_DELAYS,
    SPA_COOLDOWN_SECONDS,
    SPA_HEAT_SETTLE_SECONDS,
    SPA_JETS_FUNCTION,
)
from .entity import ConnectMyPoolEntity
from .spa_config import SpaConfigError, mismatches, parse_spa_config

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    api: ConnectMyPoolApi = data["api"]
    cfg: dict[str, Any] = data["config"]
    wait_for_execution: bool = data.get("wait_for_execution", True)
    heat_spa_only = bool(
        entry.options.get(CONF_HEAT_SPA_ONLY, entry.data.get(CONF_HEAT_SPA_ONLY, DEFAULT_HEAT_SPA_ONLY))
    )
    spa_heat_config = str(
        entry.options.get(CONF_SPA_HEAT_CONFIG, entry.data.get(CONF_SPA_HEAT_CONFIG, DEFAULT_SPA_HEAT_CONFIG))
    )

    entities: list[ClimateEntity] = []
    for h in (cfg.get("heaters") or []):
        entities.append(
            ConnectMyPoolHeaterClimate(
                coordinator, api, wait_for_execution, cfg, h, heat_spa_only, spa_heat_config
            )
        )
    async_add_entities(entities)


class ConnectMyPoolHeaterClimate(ConnectMyPoolEntity, ClimateEntity):
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]

    def __init__(
        self,
        coordinator,
        api: ConnectMyPoolApi,
        wait_for_execution: bool,
        cfg: dict[str, Any],
        heater_cfg: dict[str, Any],
        heat_spa_only: bool = False,
        spa_heat_config: str = "",
    ) -> None:
        self._api = api
        self._wait = bool(wait_for_execution)
        self._cfg = cfg
        self._heater_number = int(heater_cfg.get("heater_number", 1))
        name = heater_cfg.get("friendly_name") or heater_cfg.get("name") or (f"Heater {self._heater_number}" if self._heater_number != 1 else "Heater")
        super().__init__(coordinator, name, f"heater_{self._heater_number}")

        self._heat_cool_enabled = bool(cfg.get("heat_cool_selection_enabled"))
        if self._heat_cool_enabled and HVACMode.COOL not in self._attr_hvac_modes:
            self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]

        # Combined pool + spa: expose Spa/Pool as presets so the heater card can
        # pick the body of water it heats.  With heat_spa_only, heating always
        # runs in Spa mode and the pool is never heated.
        self._pool_spa_enabled = bool(cfg.get("pool_spa_selection_enabled"))
        self._heat_spa_only = bool(heat_spa_only) and self._pool_spa_enabled
        if self._pool_spa_enabled:
            self._attr_supported_features |= ClimateEntityFeature.PRESET_MODE
            self._attr_preset_modes = [POOL_SPA[0], POOL_SPA[1]]  # Spa, Pool
        self._spa_jets_channel: int | None = None
        for ch in (cfg.get("channels") or []):
            try:
                if int(ch.get("function")) == SPA_JETS_FUNCTION:
                    self._spa_jets_channel = int(ch["channel_number"])
                    break
            except (TypeError, ValueError, KeyError):
                continue
        self._heat_lock = asyncio.Lock()

        # Channel modes required before the heater may start (heat_spa_only).
        channels = cfg.get("channels") or []
        self._channel_names = {}
        for ch in channels:
            try:
                self._channel_names[int(ch["channel_number"])] = str(ch.get("name") or f"Channel {ch['channel_number']}")
            except (KeyError, TypeError, ValueError):
                continue
        self._spa_config_error: str | None = None
        try:
            self._spa_target = parse_spa_config(spa_heat_config, channels)
        except SpaConfigError as err:
            self._spa_config_error = str(err)
            self._spa_target = {}
            _LOGGER.error("Spa heating configuration ignored: %s", err)
        if not self._spa_target and self._spa_jets_channel is not None:
            self._spa_target = {self._spa_jets_channel: 2}  # at least spa jets On (flow)

        # Progress of the running spa start/stop sequence, shown as an attribute
        # so dashboards can show a busy state and avoid repeated taps.
        self._sequence_stage: str | None = None
        self._last_sequence_error: str | None = None
        self._stop_task: asyncio.Task | None = None

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

    def _find_heater(self) -> dict[str, Any] | None:
        for h in (self.data.get("heaters") or []):
            if int(h.get("heater_number")) == self._heater_number:
                return h
        return None

    def _pool_or_spa(self) -> int:
        # Default to Pool if unknown.
        val = self.data.get("pool_spa_selection")
        try:
            return int(val)
        except Exception:
            return 1

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
        h = self._find_heater()
        if not h:
            return None

        try:
            is_spa = (self._pool_or_spa() == 0) and bool(self._cfg.get("pool_spa_selection_enabled"))
            key = "spa_set_temperature" if is_spa else "set_temperature"
            return float(h.get(key))
        except Exception:
            return None

    @property
    def hvac_mode(self) -> HVACMode | None:
        h = self._find_heater()
        if not h:
            return None
        try:
            heater_mode = int(h.get("mode"))
        except Exception:
            return None

        if heater_mode == 0:
            return HVACMode.OFF

        if not self._heat_cool_enabled:
            return HVACMode.HEAT

        try:
            hc = int(self.data.get("heat_cool_selection", 1))
        except Exception:
            hc = 1
        return HVACMode.HEAT if hc == 1 else HVACMode.COOL

    async def _do_action(self, action_code: int, *, value: str = "", device_number: int | None = None) -> None:
        try:
            await self._api.pool_action(
                pool_api_code=self.coordinator.pool_api_code,
                action_code=action_code,
                device_number=self._heater_number if device_number is None else device_number,
                value=value,
                temperature_scale=self.coordinator.temperature_scale,
                wait_for_execution=self._wait,
            )
            await asyncio.sleep(1.0)
            await self.coordinator.async_request_refresh()
        except ConnectMyPoolError as err:
            raise HomeAssistantError(str(err)) from err

    @property
    def preset_mode(self) -> str | None:
        if not self._pool_spa_enabled:
            return None
        val = self.data.get("pool_spa_selection")
        try:
            return POOL_SPA.get(int(val))
        except (TypeError, ValueError):
            return None

    def _heater_on(self) -> bool:
        h = self._find_heater() or {}
        try:
            return int(h.get("mode")) == 1
        except (TypeError, ValueError):
            return False

    def _set_stage(self, stage: str | None) -> None:
        self._sequence_stage = stage
        if self.hass is not None:
            self.async_write_ha_state()

    def _raise_if_busy(self) -> None:
        if self._heat_lock.locked():
            raise HomeAssistantError(
                f"Spa heating sequence in progress ({self._sequence_stage or 'starting'}); please wait."
            )

    def _cancel_stop_sequence(self) -> None:
        if self._stop_task is not None and not self._stop_task.done():
            self._stop_task.cancel()
        self._stop_task = None

    def _describe(self, wrong: dict[int, int | None]) -> str:
        return ", ".join(
            f"{self._channel_names.get(n, n)} is {CHANNEL_MODES.get(m, 'unknown') if m is not None else 'unknown'}"
            f" (needs {CHANNEL_MODES[self._spa_target[n]]})"
            for n, m in wrong.items()
        )

    async def _wait_for_spa_config(self) -> dict[int, int | None]:
        """Read fresh status until Spa mode and the target channel modes show.

        Returns the remaining mismatches (empty when all match).  Raises if the
        controller never reports Spa mode.
        """
        wrong: dict[int, int | None] = {}
        for delay in SPA_CONFIG_CHECK_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            await self.coordinator.async_refresh()
            wrong = mismatches(self._spa_target, self.data.get("channels") or [])
            if self._pool_or_spa() == 0 and not wrong:
                return {}
        if self._pool_or_spa() != 0:
            raise HomeAssistantError("The controller still reports Pool mode; heater not started.")
        return wrong

    async def _set_channel_mode(self, channel: int, mode: int) -> None:
        """Change a channel through its own select (reuses its paced cycling)."""
        entity_id = er.async_get(self.hass).async_get_entity_id(
            "select", DOMAIN, f"{self._pool_id}_channel_{channel}_mode"
        )
        if entity_id is None:
            raise HomeAssistantError(
                f"No control for {self._channel_names.get(channel, channel)}; heater not started."
            )
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": CHANNEL_MODES[mode]},
            blocking=True,
        )

    async def _start_spa_heating(self, hvac_mode: HVACMode) -> None:
        """Spa mode -> settle -> check (fix) channel configuration -> heater on.

        The heater only fires with water flowing through it, so it is never
        started unless the controller reports Spa mode and every configured
        channel (e.g. Filter Pump On, Spa Jets On, Blower On) in its mode.
        """
        self._cancel_stop_sequence()
        self._last_sequence_error = None
        try:
            self._set_stage("Switching to Spa")
            await self.coordinator.async_refresh()
            if self._pool_or_spa() != 0:
                try:
                    await self._do_action(ACTION_SET_POOL_SPA, value="0")
                except HomeAssistantError as err:
                    # A slow cloud reply doesn't mean the switch failed; the
                    # configuration check below confirms Spa mode either way.
                    _LOGGER.warning("Switch to Spa: %s; checking status", err)
            await asyncio.sleep(SPA_HEAT_SETTLE_SECONDS)

            self._set_stage("Checking configuration")
            wrong = await self._wait_for_spa_config()
            if wrong:
                for channel, _mode in wrong.items():
                    target = self._spa_target[channel]
                    self._set_stage(
                        f"Fixing: {self._channel_names.get(channel, channel)} \u2192 {CHANNEL_MODES[target]}"
                    )
                    await self._set_channel_mode(channel, target)
                self._set_stage("Checking configuration")
                wrong = await self._wait_for_spa_config()
                if wrong:
                    raise HomeAssistantError(
                        f"Spa configuration not reached ({self._describe(wrong)}); heater not started."
                    )

            self._set_stage("Starting heater")
            if self._heat_cool_enabled:
                await self._do_action(ACTION_SET_HEAT_COOL, value="1" if hvac_mode == HVACMode.HEAT else "0")
            await self._do_action(ACTION_SET_HEATER_MODE, value="1")
        except HomeAssistantError as err:
            self._last_sequence_error = str(err)
            raise
        finally:
            self._set_stage(None)

    def _favourite_applied(self, num: int) -> bool:
        try:
            if int(self.data.get("active_favourite")) == num:
                return True
        except (TypeError, ValueError):
            pass
        # Fallback: All Auto leaves every channel in Auto.
        modes = [c.get("mode") for c in (self.data.get("channels") or [])]
        return bool(modes) and all(str(m) == "1" for m in modes)

    async def _confirmed_action(
        self, label: str, action_code: int, *, value: str, applied, device_number: int | None = None
    ) -> bool:
        """Send an action and confirm it from fresh status; retry once.

        A timeout or cloud error doesn't abort: the pool often executes the
        instruction even when the reply is slow, so the status decides.
        """
        for attempt in (1, 2):
            try:
                await self._do_action(action_code, value=value, device_number=device_number)
            except HomeAssistantError as err:
                _LOGGER.warning("%s: %s (attempt %d); checking status", label, err, attempt)
            for delay in (0, 5, 10, 15):
                if delay:
                    await asyncio.sleep(delay)
                await self.coordinator.async_refresh()
                if applied():
                    return True
        return False

    async def _stop_spa_sequence(self) -> None:
        """After the heater is off: cool down with flow, then Pool, then All Auto."""
        self._last_sequence_error = None
        try:
            self._set_stage(f"Cooling down (heater off, {SPA_COOLDOWN_SECONDS // 60} min)")
            await asyncio.sleep(SPA_COOLDOWN_SECONDS)

            self._set_stage("Returning to Pool")
            if not await self._confirmed_action(
                "Return to Pool", ACTION_SET_POOL_SPA, value="1",
                applied=lambda: self._pool_or_spa() == 1,
            ):
                raise HomeAssistantError("the controller didn't confirm Pool mode")

            fav = next(
                (
                    f for f in (self._cfg.get("favourites") or [])
                    if str(f.get("name", "")).strip().lower() == ALL_AUTO_FAVOURITE_NAME.lower()
                ),
                None,
            )
            if fav is not None:
                self._set_stage("Setting All Auto")
                num = int(fav["favourite_number"])
                # The guide lists the favourite under device_number; some clients
                # send it as the value.  Send both.  Re-sending is harmless.
                if not await self._confirmed_action(
                    "All Auto", ACTION_SET_ACTIVE_FAVOURITE, value=str(num), device_number=num,
                    applied=lambda: self._favourite_applied(num),
                ):
                    self._last_sequence_error = "All Auto sent but not confirmed by status; check the controller."
            else:
                _LOGGER.warning("No '%s' favourite found; skipped", ALL_AUTO_FAVOURITE_NAME)
            self._set_stage(None)
        except asyncio.CancelledError:
            # Cancelled by a new Heat request, which now owns the stage.
            raise
        except Exception as err:  # noqa: BLE001
            self._last_sequence_error = f"Stop sequence failed: {err}"
            _LOGGER.error("Spa stop sequence failed: %s", err)
            self._set_stage(None)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        desired = next((k for k, v in POOL_SPA.items() if v == preset_mode), None)
        if desired is None or not self._pool_spa_enabled:
            raise HomeAssistantError(f"Unsupported preset: {preset_mode}")
        self._raise_if_busy()
        if desired == 1 and self._heat_spa_only and self._heater_on():
            # Never leave the heater running into Pool mode.
            await self._do_action(ACTION_SET_HEATER_MODE, value="0")
        await self._do_action(ACTION_SET_POOL_SPA, value=str(desired))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            self._raise_if_busy()
            if not await self._confirmed_action(
                "Heater off", ACTION_SET_HEATER_MODE, value="0",
                applied=lambda: not self._heater_on(),
            ):
                raise HomeAssistantError("The controller didn't confirm the heater is off; check it.")
            # Heat-spa-only: keep water flowing through the hot heater, then go
            # back to Pool and All Auto.  Only when the system is in Spa mode.
            if self._heat_spa_only and self._pool_or_spa() == 0 and self._stop_task is None:
                self._stop_task = self.hass.async_create_background_task(
                    self._stop_spa_sequence(), "connectmypool_spa_stop"
                )
                self._stop_task.add_done_callback(lambda _t: setattr(self, "_stop_task", None))
            return

        if self._heat_spa_only and hvac_mode in (HVACMode.HEAT, HVACMode.COOL):
            self._raise_if_busy()
            async with self._heat_lock:
                await self._start_spa_heating(hvac_mode)
            return

        if hvac_mode == HVACMode.HEAT:
            if self._heat_cool_enabled:
                await self._do_action(ACTION_SET_HEAT_COOL, value="1")
            await self._do_action(ACTION_SET_HEATER_MODE, value="1")
            return

        if hvac_mode == HVACMode.COOL:
            if not self._heat_cool_enabled:
                raise HomeAssistantError("Cooling mode is not enabled for this pool.")
            await self._do_action(ACTION_SET_HEAT_COOL, value="0")
            await self._do_action(ACTION_SET_HEATER_MODE, value="1")
            return

        raise HomeAssistantError(f"Unsupported hvac_mode: {hvac_mode}")

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get("temperature")
        if temp is None:
            return
        # ConnectMyPool expects an integer setpoint.
        try:
            val = str(int(round(float(temp))))
        except Exception as err:
            raise HomeAssistantError(f"Invalid temperature: {temp}") from err

        # Note: The API sets pool or spa setpoint based on the *current* pool/spa selection.
        await self._do_action(ACTION_SET_HEATER_SET_TEMP, value=val)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "heater_number": self._heater_number,
            "heat_spa_only": self._heat_spa_only,
            "spa_sequence": self._sequence_stage,
            "spa_sequence_busy": self._sequence_stage is not None,
            "spa_sequence_last_error": self._last_sequence_error,
            "spa_heat_config": {
                self._channel_names.get(n, str(n)): CHANNEL_MODES.get(m, str(m))
                for n, m in self._spa_target.items()
            },
            "spa_heat_config_error": self._spa_config_error,
            "heater_mode_raw": None if self._find_heater() is None else self._find_heater().get("mode"),
            "pool_spa_selection": POOL_SPA.get(self._pool_or_spa(), str(self._pool_or_spa())),
            "heat_cool_selection": HEAT_COOL.get(int(self.data.get("heat_cool_selection", 1)), str(self.data.get("heat_cool_selection"))),
        }
