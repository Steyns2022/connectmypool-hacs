from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from homeassistant.components import persistent_notification
from homeassistant.util import slugify
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.exceptions import ConfigEntryNotReady, ConfigEntryAuthFailed

from .api import ConnectMyPoolApi, ConnectMyPoolAuthError, ConnectMyPoolError
from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_POOL_API_CODE,
    CONF_BASE_URL,
    CONF_TEMPERATURE_SCALE,
    CONF_SCAN_INTERVAL,
    CONF_WAIT_FOR_EXECUTION,
    DEFAULT_BASE_URL,
    DEFAULT_TEMPERATURE_SCALE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WAIT_FOR_EXECUTION,
)
from .coordinator import ConnectMyPoolCoordinator

_LOGGER = logging.getLogger(__name__)

def _pretty_function(func: Any) -> str | None:
    if not func:
        return None
    s = str(func).strip()
    if not s:
        return None
    up = s.upper()
    # small heuristics to make function names human-friendly
    if "LIGHT" in up:
        return "Light"
    if "FILTER" in up or "PUMP" in up:
        return "Pump"
    if "BLOW" in up:
        return "Blower"
    if "JET" in up:
        return "Jets"
    if "HEAT" in up:
        return "Heater"
    if "SOLAR" in up:
        return "Solar"
    # Title-case with spaces
    return " ".join(w.capitalize() for w in re.split(r"[_\-\s]+", s) if w)


def _apply_unique_friendly_names(pool_config: dict[str, Any]) -> None:
    """Mutate pool_config to include a 'friendly_name' per device item.

    Some pools return duplicate names (e.g. multiple channels named 'Pool').
    HA can handle this, but the UI becomes a wall of identical labels.
    """

    def apply(list_key: str, name_key: str, id_key: str, kind: str) -> None:
        items = pool_config.get(list_key) or []
        if not isinstance(items, list) or not items:
            return

        base_names: list[str] = []
        for it in items:
            raw = (it.get(name_key) or "")
            name = str(raw).strip()
            if not name:
                num = it.get(id_key)
                name = f"{kind} {num}" if num is not None else kind
            base_names.append(name)

        counts: dict[str, int] = {}
        for n in base_names:
            counts[n] = counts.get(n, 0) + 1

        used: set[str] = set()
        for it, base in zip(items, base_names):
            if counts.get(base, 0) == 1 and base not in used:
                it["friendly_name"] = base
                used.add(base)
                continue

            num = it.get(id_key)
            func_label = _pretty_function(it.get("function"))
            if func_label and num is not None:
                candidate = f"{base} ({func_label} {num})"
            elif num is not None:
                candidate = f"{base} (Ch {num})"
            elif func_label:
                candidate = f"{base} ({func_label})"
            else:
                candidate = f"{base} (dup)"

            # Ensure uniqueness even if multiple identical candidates exist
            final = candidate
            i = 2
            while final in used:
                final = f"{candidate} {i}"
                i += 1

            it["friendly_name"] = final
            used.add(final)

    apply("channels", "name", "channel_number", "Channel")
    apply("valves", "name", "valve_number", "Valve")
    apply("heaters", "name", "heater_number", "Heater")
    apply("solar_systems", "name", "solar_number", "Solar")
    apply("lighting_zones", "name", "lighting_zone_number", "Lighting Zone")

SERVICE_SEND_ACTION = "send_action"
SERVICE_APPLY_ENTITY_ID_PREFIX = "apply_entity_id_prefix"


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration (service registration)."""
    hass.data.setdefault(DOMAIN, {})
    _async_setup_services(hass)
    return True


def _async_setup_services(hass: HomeAssistant) -> None:
    """Register service actions."""
    if hass.data[DOMAIN].get("_services_setup"):
        return

    def _resolve_entry_id(call: ServiceCall) -> str:
        entry_id = call.data.get("config_entry_id")
        entries = hass.data.get(DOMAIN, {})
        if entry_id is None:
            configured = [k for k in entries.keys() if not str(k).startswith("_")]
            if len(configured) == 1:
                entry_id = configured[0]
            else:
                raise ValueError("Multiple ConnectMyPool entries; provide config_entry_id")
        if entry_id not in hass.data[DOMAIN]:
            raise ValueError(f"Unknown config_entry_id: {entry_id}")
        return str(entry_id)

    send_action_schema = vol.Schema(
        {
            vol.Optional("config_entry_id"): str,
            vol.Required("action_code"): int,
            vol.Optional("device_number", default=0): int,
            vol.Optional("value", default=""): str,
            vol.Optional("wait_for_execution"): bool,
        }
    )

    async def _handle_send_action(call: ServiceCall) -> None:
        entry_id = _resolve_entry_id(call)
        data = hass.data[DOMAIN][entry_id]

        coordinator: ConnectMyPoolCoordinator = data["coordinator"]
        api: ConnectMyPoolApi = data["api"]
        wait = call.data.get("wait_for_execution", data.get("wait_for_execution", DEFAULT_WAIT_FOR_EXECUTION))

        await api.pool_action(
            pool_api_code=coordinator.pool_api_code,
            action_code=int(call.data["action_code"]),
            device_number=int(call.data.get("device_number", 0)),
            value=str(call.data.get("value", "")),
            temperature_scale=coordinator.temperature_scale,
            wait_for_execution=bool(wait),
        )

        await asyncio.sleep(1.5)
        await coordinator.async_request_refresh()
        coordinator.async_follow_up()

    apply_prefix_schema = vol.Schema(
        {
            vol.Optional("config_entry_id"): str,
            vol.Optional("prefix", default="connectmypool"): str,
            vol.Optional("dry_run", default=True): bool,
        }
    )

    async def _handle_apply_entity_id_prefix(call: ServiceCall) -> None:
        entry_id = _resolve_entry_id(call)
        prefix = slugify(str(call.data.get("prefix") or "connectmypool"))
        dry_run = bool(call.data.get("dry_run", True))

        ent_reg = er.async_get(hass)

        def _belongs(reg_entry) -> bool:
            # Prefer exact platform match; also require config entry association.
            if getattr(reg_entry, "platform", None) != DOMAIN:
                return False
            cfg_id = getattr(reg_entry, "config_entry_id", None)
            if cfg_id == entry_id:
                return True
            cfg_ids = getattr(reg_entry, "config_entry_ids", None)
            if cfg_ids and entry_id in cfg_ids:
                return True
            return False

        existing = set(ent_reg.entities)
        changes: list[tuple[str, str]] = []

        for reg_entry in ent_reg.entities.values():
            if not _belongs(reg_entry):
                continue
            old = reg_entry.entity_id
            if "." not in old:
                continue
            domain, obj = old.split(".", 1)
            if obj.startswith(prefix + "_"):
                continue

            base = f"{domain}.{prefix}_{obj}"
            new = base
            i = 2
            while new in existing and new != old:
                new = f"{base}_{i}"
                i += 1

            if new != old:
                changes.append((old, new))
                existing.add(new)
        if not changes:
            persistent_notification.async_create(hass,'No changes needed',title='ConnectMyPool entity_id prefix');return
        m='\n'.join([f'{o}->{n}' for o,n in changes])
        if dry_run:
            persistent_notification.async_create(hass,f'Dry run {len(changes)}\n{m}',title='ConnectMyPool entity_id prefix');return
        for o,n in changes: ent_reg.async_update_entity(o,new_entity_id=n)
        persistent_notification.async_create(hass,f'Renamed {len(changes)}\n{m}',title='ConnectMyPool entity_id prefix')

    hass.services.async_register(DOMAIN, SERVICE_SEND_ACTION, _handle_send_action, schema=send_action_schema)
    hass.services.async_register(
        DOMAIN,
        SERVICE_APPLY_ENTITY_ID_PREFIX,
        _handle_apply_entity_id_prefix,
        schema=apply_prefix_schema,
    )

    hass.data[DOMAIN]["_services_setup"] = True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    pool_api_code: str = entry.data[CONF_POOL_API_CODE]

    base_url: str = entry.options.get(CONF_BASE_URL, entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL))
    temperature_scale: int = entry.options.get(
        CONF_TEMPERATURE_SCALE, entry.data.get(CONF_TEMPERATURE_SCALE, DEFAULT_TEMPERATURE_SCALE)
    )
    scan_interval: int = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    wait_for_execution: bool = entry.options.get(CONF_WAIT_FOR_EXECUTION, DEFAULT_WAIT_FOR_EXECUTION)

    session = async_get_clientsession(hass)
    api = ConnectMyPoolApi(session, base_url=base_url, min_poll_seconds=60)

    # Load config once (used to build entities)
    try:
        pool_config = await api.pool_config(pool_api_code)
        _apply_unique_friendly_names(pool_config)
    except ConnectMyPoolAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except ConnectMyPoolError as err:
        raise ConfigEntryNotReady(str(err)) from err
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = ConnectMyPoolCoordinator(
        hass,
        api=api,
        pool_api_code=pool_api_code,
        temperature_scale=temperature_scale,
        scan_interval=scan_interval,
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "config": pool_config,
        "coordinator": coordinator,
        "wait_for_execution": wait_for_execution,
        "unsub_options_update_listener": entry.add_update_listener(_async_options_updated),
    }

    _async_setup_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if entry_data and (coordinator := entry_data.get("coordinator")):
            coordinator.async_cancel_follow_up()
        if entry_data and (unsub := entry_data.get("unsub_options_update_listener")):
            unsub()
    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
