from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory

from .api import ConnectMyPoolApi, ConnectMyPoolError
from .const import (
    DOMAIN,
    CHANNEL_MODES,
    TRI_MODES,
    POOL_SPA,
    HEAT_COOL,
    ACTION_CYCLE_CHANNEL,
    ACTION_SET_VALVE_MODE,
    ACTION_SET_POOL_SPA,
    ACTION_SET_LIGHT_MODE,
    ACTION_SET_ACTIVE_FAVOURITE,
    ACTION_SET_SOLAR_MODE,
    ACTION_SET_HEAT_COOL,
    CONF_FILTER_PUMP_SPEEDS,
    DEFAULT_FILTER_PUMP_SPEEDS,
)
from .cycle import (
    SIMPLE_CHANNEL_CYCLE,
    filter_pump_cycle,
    CycleError,
    cycle_steps,
)
from .entity import ConnectMyPoolEntity

_LOGGER = logging.getLogger(__name__)

FILTER_PUMP_FUNCTION = 1
HEATER_PUMP_FUNCTION = 3

# Per-step verification schedule (seconds between fresh status reads).  A cycle
# action is only acknowledged by the cloud on acceptance, not physical
# execution, and the poolstatus snapshot then lags the controller by several
# seconds (observed ~10s), so the next cycle must not be sent until status
# actually shows the expected intermediate mode.  Each read forces a real
# coordinator fetch (see _refresh_and_read); we poll with backoff up to ~30s,
# returning as soon as the expected mode appears.
_VERIFY_DELAYS: tuple[float, ...] = (1.0, 2.0, 3.0, 4.0, 5.0, 5.0, 5.0, 5.0)

# Fixed settle between consecutive cycle presses.  The controller applies a
# cycle within a few seconds, but status lags, so pace by time (not by reading
# status) to guarantee each press is applied before the next is sent.
_INTER_STEP_SETTLE = 12.0


def _is_filter_pump_channel(ch: dict[str, Any]) -> bool:
    try:
        return int(ch.get("function")) == FILTER_PUMP_FUNCTION
    except (TypeError, ValueError):
        return False


def _is_heater_pump_channel(ch: dict[str, Any]) -> bool:
    """Heater-pump channel: energises heating circulation, so hide by default."""
    try:
        if int(ch.get("function")) == HEATER_PUMP_FUNCTION:
            return True
    except (TypeError, ValueError):
        pass
    name = f"{ch.get('friendly_name') or ''} {ch.get('name') or ''}".lower()
    return "heat" in name


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    api: ConnectMyPoolApi = data["api"]
    cfg: dict[str, Any] = data["config"]
    wait_for_execution: bool = data.get("wait_for_execution", True)
    pump_cycle = filter_pump_cycle(
        entry.options.get(
            CONF_FILTER_PUMP_SPEEDS,
            entry.data.get(CONF_FILTER_PUMP_SPEEDS, DEFAULT_FILTER_PUMP_SPEEDS),
        )
    )

    entities: list[SelectEntity] = []

    # Pool/Spa and Heat/Cool selectors if enabled
    if cfg.get("pool_spa_selection_enabled"):
        entities.append(PoolSpaSelect(coordinator, api, wait_for_execution))
    if cfg.get("heat_cool_selection_enabled"):
        entities.append(HeatCoolSelect(coordinator, api, wait_for_execution))

    # Active favourite
    favs = cfg.get("favourites") or []
    if favs:
        entities.append(ActiveFavouriteSelect(coordinator, api, wait_for_execution, favs))

    # Every channel is a multi-state device controlled by a cycle action, not a
    # true on/off switch.  The filter pump's cycle depends on the pump fitted
    # (single/two/three-speed, set by the filter_pump_speeds option); all
    # other channels (jets, blower, heater pump, ...) cycle Off/Auto/On.  The
    # heater-pump channel is disabled by default so it is not an accidental
    # one-tap control (it energises heating circulation).
    for ch in (cfg.get("channels") or []):
        if _is_filter_pump_channel(ch):
            entities.append(
                ChannelModeSelect(coordinator, api, wait_for_execution, ch, pump_cycle)
            )
        else:
            enabled = not _is_heater_pump_channel(ch)
            entities.append(
                ChannelModeSelect(
                    coordinator,
                    api,
                    wait_for_execution,
                    ch,
                    SIMPLE_CHANNEL_CYCLE,
                    enabled_default=enabled,
                )
            )

    # Valves
    for v in (cfg.get("valves") or []):
        entities.append(ValveModeSelect(coordinator, api, wait_for_execution, v))

    # Solar mode (solar setpoint handled by water_heater entity)
    for s in (cfg.get("solar_systems") or []):
        entities.append(SolarModeSelect(coordinator, api, wait_for_execution, s))

    # Lighting zone mode (Off/Auto/On). On/off + effects handled by light entity.
    for lz in (cfg.get("lighting_zones") or []):
        entities.append(LightingZoneModeSelect(coordinator, api, wait_for_execution, lz))

    async_add_entities(entities)


class _BaseSelect(ConnectMyPoolEntity, SelectEntity):
    def __init__(
        self,
        coordinator,
        api: ConnectMyPoolApi,
        wait_for_execution: bool,
        name: str,
        unique_suffix: str,
    ) -> None:
        super().__init__(coordinator, name, unique_suffix)
        self._api = api
        self._wait = bool(wait_for_execution)

    async def _do_action(self, action_code: int, device_number: int = 0, value: str = "") -> None:
        try:
            await self._api.pool_action(
                pool_api_code=self.coordinator.pool_api_code,
                action_code=action_code,
                device_number=device_number,
                value=value,
                temperature_scale=self.coordinator.temperature_scale,
                wait_for_execution=self._wait,
            )
            await asyncio.sleep(1.0)
            await self.coordinator.async_request_refresh()
        except ConnectMyPoolError as err:
            raise HomeAssistantError(str(err)) from err


class PoolSpaSelect(_BaseSelect):
    _attr_options = list(POOL_SPA.values())

    def __init__(self, coordinator, api, wait_for_execution) -> None:
        super().__init__(coordinator, api, wait_for_execution, "Pool/Spa Selection", "pool_spa_selection")

    @property
    def current_option(self) -> str | None:
        val = self.data.get("pool_spa_selection")
        if val is None:
            return None
        try:
            return POOL_SPA.get(int(val), str(val))
        except Exception:
            return None

    async def async_select_option(self, option: str) -> None:
        desired = next((k for k, v in POOL_SPA.items() if v == option), None)
        if desired is None:
            raise HomeAssistantError(f"Unsupported option: {option}")
        await self._do_action(ACTION_SET_POOL_SPA, value=str(desired))


class HeatCoolSelect(_BaseSelect):
    _attr_options = list(HEAT_COOL.values())

    def __init__(self, coordinator, api, wait_for_execution) -> None:
        super().__init__(coordinator, api, wait_for_execution, "Heat/Cool Selection", "heat_cool_selection")

    @property
    def current_option(self) -> str | None:
        val = self.data.get("heat_cool_selection")
        if val is None:
            return None
        try:
            return HEAT_COOL.get(int(val), str(val))
        except Exception:
            return None

    async def async_select_option(self, option: str) -> None:
        desired = next((k for k, v in HEAT_COOL.items() if v == option), None)
        if desired is None:
            raise HomeAssistantError(f"Unsupported option: {option}")
        await self._do_action(ACTION_SET_HEAT_COOL, value=str(desired))


class ActiveFavouriteSelect(_BaseSelect):
    def __init__(self, coordinator, api, wait_for_execution, favs: list[dict[str, Any]]) -> None:
        self._by_number: dict[int, str] = {}
        for f in favs:
            try:
                num = int(f.get("favourite_number"))
                name = str(f.get("name") or f"Favourite {num}")
                self._by_number[num] = name
            except Exception:
                continue
        super().__init__(coordinator, api, wait_for_execution, "Active Favourite", "active_favourite")
        self._attr_options = list(self._by_number.values())

    @property
    def current_option(self) -> str | None:
        val = self.data.get("active_favourite")
        if val is None:
            return None
        try:
            num = int(val)
        except Exception:
            return None
        # 255 indicates no active favourite (per guide)
        if num == 255:
            return None
        return self._by_number.get(num)

    async def async_select_option(self, option: str) -> None:
        desired_num = next((k for k, v in self._by_number.items() if v == option), None)
        if desired_num is None:
            raise HomeAssistantError(f"Unknown favourite: {option}")
        await self._do_action(ACTION_SET_ACTIVE_FAVOURITE, value=str(desired_num))


class ChannelModeSelect(_BaseSelect):
    """Reliable multi-state selector for a ConnectMyPool channel.

    ConnectMyPool only exposes a single-step *cycle* action, so reaching a
    target mode means advancing the channel through its fixed sequence one step
    at a time.  The current mode is read fresh first (the cached value can be
    stale and mis-count steps), then exactly the required number of cycle
    actions are sent, paced by a fixed time settle so the controller applies
    each before the next arrives -- the cloud only acknowledges a cycle on
    acceptance and its status lags physical execution, so time, not a status
    read, is what reliably paces the presses.  Sending the exact count never
    overshoots; the final state is confirmed best-effort (a warning, never an
    error, if status has not caught up).  A per-entity lock serialises the whole
    operation and latest-request coalescing makes rapid selections converge on
    the most recent target.
    """

    def __init__(
        self,
        coordinator,
        api,
        wait_for_execution,
        ch: dict[str, Any],
        cycle: tuple[int, ...],
        *,
        enabled_default: bool = True,
    ) -> None:
        self._channel_number = int(ch["channel_number"])
        self._function = ch.get("function")
        self._cycle = tuple(cycle)
        self._mode_lock = asyncio.Lock()
        self._latest_requested: int | None = None
        friendly = ch.get("friendly_name") or ch.get("name") or f"Channel {self._channel_number}"
        super().__init__(coordinator, api, wait_for_execution, f"{friendly} Mode", f"channel_{self._channel_number}_mode")
        self._attr_options = [CHANNEL_MODES[mode] for mode in self._cycle]
        if not enabled_default:
            self._attr_entity_registry_enabled_default = False

    def _find_mode(self) -> Optional[int]:
        for c in (self.data.get("channels") or []):
            if int(c.get("channel_number")) == self._channel_number:
                try:
                    return int(c.get("mode"))
                except Exception:
                    return None
        return None

    def _label(self, mode: int | None) -> str:
        if mode is None:
            return "unknown"
        return CHANNEL_MODES.get(mode, str(mode))

    @property
    def current_option(self) -> str | None:
        mode = self._find_mode()
        if mode is None:
            return None
        return CHANNEL_MODES.get(mode, str(mode))

    async def _send_cycle(self) -> None:
        try:
            await self._api.pool_action(
                pool_api_code=self.coordinator.pool_api_code,
                action_code=ACTION_CYCLE_CHANNEL,
                device_number=self._channel_number,
                value="",
                temperature_scale=self.coordinator.temperature_scale,
                # Always wait for each individual step to execute: this is what
                # makes multi-step changes converge reliably.
                wait_for_execution=True,
            )
        except ConnectMyPoolError as err:
            raise HomeAssistantError(str(err)) from err

    async def _refresh_and_read(self, delay: float) -> int | None:
        if delay > 0:
            await asyncio.sleep(delay)
        # async_refresh() performs and awaits the fetch immediately;
        # async_request_refresh() only schedules a debounced refresh and would
        # return before fresh data is available, so the mode read back could be
        # stale.  The API's post-action fast-poll window keeps this un-throttled.
        await self.coordinator.async_refresh()
        return self._find_mode()

    async def _await_mode(self, expected: int) -> int | None:
        """Poll fresh status until the channel shows ``expected`` (or give up).

        The cloud status lags the controller, so read repeatedly with backoff
        and return as soon as it matches.  Returns the last observed mode if it
        never matched within the window.
        """
        observed: int | None = None
        for delay in _VERIFY_DELAYS:
            observed = await self._refresh_and_read(delay)
            if observed == expected:
                return observed
        return observed

    async def _set_mode_locked(self, desired: int) -> None:
        # Always compute from a freshly fetched status.  The coordinator's cached
        # value can be up to a poll interval old, which would mis-count the
        # number of cycle steps needed.
        current = await self._refresh_and_read(0)

        if current not in self._cycle:
            raise HomeAssistantError(
                f"{self._attr_name}: controller reported unexpected mode "
                f"'{self._label(current)}'. Refusing to cycle blindly."
            )

        try:
            total = cycle_steps(current, desired, self._cycle)
        except CycleError as err:
            raise HomeAssistantError(str(err)) from err

        if total == 0:
            return

        _LOGGER.debug(
            "%s: cycling %s -> %s (%d step(s)) via sequence %s",
            self._attr_name,
            self._label(current),
            self._label(desired),
            total,
            [CHANNEL_MODES[m] for m in self._cycle],
        )

        # Send exactly the required number of cycle actions, paced far enough
        # apart that the controller applies each one before the next arrives.
        # The cloud only acknowledges a cycle on acceptance and its status
        # snapshot lags physical execution unreliably, so a fixed time settle --
        # not a status read -- is what reliably paces the presses.  Sending the
        # exact count means the target is never overshot.
        for step in range(total):
            _LOGGER.debug("%s: sending cycle %d/%d", self._attr_name, step + 1, total)
            await self._send_cycle()
            if step < total - 1:
                await asyncio.sleep(_INTER_STEP_SETTLE)

        # Best-effort confirmation for the UI.  All required cycles have been
        # sent, so never send more; if status has not caught up within the
        # window, warn and let the coordinator's next poll reconcile.
        observed = await self._await_mode(desired)
        if observed == desired:
            _LOGGER.debug("%s: confirmed '%s'", self._attr_name, self._label(desired))
        else:
            _LOGGER.warning(
                "%s: '%s' not yet confirmed by status (last saw '%s'); the %d "
                "cycle action(s) were sent, state will reconcile on the next poll.",
                self._attr_name, self._label(desired), self._label(observed), total,
            )

    async def async_select_option(self, option: str) -> None:
        desired = next(
            (mode for mode in self._cycle if CHANNEL_MODES[mode] == option),
            None,
        )
        if desired is None:
            raise HomeAssistantError(f"Unsupported mode for {self._attr_name}: {option}")

        # Record the requested target before waiting for the lock so repeated UI
        # changes converge on the most recent selection rather than interleaving.
        self._latest_requested = desired

        async with self._mode_lock:
            while True:
                target = self._latest_requested
                if target is None:
                    return
                await self._set_mode_locked(target)
                if self._latest_requested == target:
                    return

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "channel_number": self._channel_number,
            "function": self._function,
            "cycle_sequence": [CHANNEL_MODES[mode] for mode in self._cycle],
        }


class ValveModeSelect(_BaseSelect):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    _attr_options = list(TRI_MODES.values())

    def __init__(self, coordinator, api, wait_for_execution, valve: dict[str, Any]) -> None:
        self._valve_number = int(valve["valve_number"])
        self._function = valve.get("function")
        friendly = valve.get("friendly_name") or valve.get("name") or f"Valve {self._valve_number}"
        super().__init__(coordinator, api, wait_for_execution, f"{friendly} Mode", f"valve_{self._valve_number}_mode")

    def _find_mode(self) -> Optional[int]:
        for v in (self.data.get("valves") or []):
            if int(v.get("valve_number")) == self._valve_number:
                try:
                    return int(v.get("mode"))
                except Exception:
                    return None
        return None

    @property
    def current_option(self) -> str | None:
        mode = self._find_mode()
        if mode is None:
            return None
        return TRI_MODES.get(mode, str(mode))

    async def async_select_option(self, option: str) -> None:
        desired = next((k for k, v in TRI_MODES.items() if v == option), None)
        if desired is None:
            raise HomeAssistantError(f"Unsupported option: {option}")
        await self._do_action(ACTION_SET_VALVE_MODE, device_number=self._valve_number, value=str(desired))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "valve_number": self._valve_number,
            "function": self._function,
        }


class SolarModeSelect(_BaseSelect):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    _attr_options = list(TRI_MODES.values())

    def __init__(self, coordinator, api, wait_for_execution, solar: dict[str, Any]) -> None:
        self._solar_number = int(solar["solar_number"])
        friendly = solar.get("name") or f"Solar {self._solar_number}"
        super().__init__(coordinator, api, wait_for_execution, f"{friendly} Mode", f"solar_{self._solar_number}_mode")

    def _find_mode(self) -> Optional[int]:
        for s in (self.data.get("solar_systems") or []):
            if int(s.get("solar_number")) == self._solar_number:
                try:
                    return int(s.get("mode"))
                except Exception:
                    return None
        return None

    @property
    def current_option(self) -> str | None:
        mode = self._find_mode()
        if mode is None:
            return None
        return TRI_MODES.get(mode, str(mode))

    async def async_select_option(self, option: str) -> None:
        desired = next((k for k, v in TRI_MODES.items() if v == option), None)
        if desired is None:
            raise HomeAssistantError(f"Unsupported option: {option}")
        await self._do_action(ACTION_SET_SOLAR_MODE, device_number=self._solar_number, value=str(desired))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"solar_number": self._solar_number}


class LightingZoneModeSelect(_BaseSelect):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    _attr_options = list(TRI_MODES.values())

    def __init__(self, coordinator, api, wait_for_execution, lz: dict[str, Any]) -> None:
        self._lz_number = int(lz["lighting_zone_number"])
        friendly = lz.get("name") or f"Lighting Zone {self._lz_number}"
        super().__init__(coordinator, api, wait_for_execution, f"{friendly} Mode", f"lightzone_{self._lz_number}_mode")

    def _find_mode(self) -> Optional[int]:
        for lz in (self.data.get("lighting_zones") or []):
            if int(lz.get("lighting_zone_number")) == self._lz_number:
                try:
                    return int(lz.get("mode"))
                except Exception:
                    return None
        return None

    @property
    def current_option(self) -> str | None:
        mode = self._find_mode()
        if mode is None:
            return None
        return TRI_MODES.get(mode, str(mode))

    async def async_select_option(self, option: str) -> None:
        desired = next((k for k, v in TRI_MODES.items() if v == option), None)
        if desired is None:
            raise HomeAssistantError(f"Unsupported option: {option}")
        await self._do_action(ACTION_SET_LIGHT_MODE, device_number=self._lz_number, value=str(desired))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"lighting_zone_number": self._lz_number}
