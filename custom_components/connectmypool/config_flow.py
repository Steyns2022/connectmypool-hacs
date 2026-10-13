from __future__ import annotations

import hashlib

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ConnectMyPoolApi, ConnectMyPoolError, ConnectMyPoolAuthError
from .const import (
    DOMAIN,
    CONF_POOL_API_CODE,
    CONF_BASE_URL,
    CONF_TEMPERATURE_SCALE,
    CONF_SCAN_INTERVAL,
    CONF_WAIT_FOR_EXECUTION,
    CONF_EXPOSE_CHANNEL_SWITCHES,
    CONF_EXPOSE_SETPOINT_NUMBERS,
    CONF_FILTER_PUMP_SPEEDS,
    DEFAULT_BASE_URL,
    DEFAULT_TEMPERATURE_SCALE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WAIT_FOR_EXECUTION,
    DEFAULT_EXPOSE_CHANNEL_SWITCHES,
    DEFAULT_EXPOSE_SETPOINT_NUMBERS,
    DEFAULT_FILTER_PUMP_SPEEDS,
    FILTER_PUMP_SPEED_OPTIONS,
)


def _stable_id(pool_api_code: str) -> str:
    return hashlib.sha1(pool_api_code.encode("utf-8")).hexdigest()[:12]


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_POOL_API_CODE): str,
        vol.Optional(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
        vol.Optional(CONF_TEMPERATURE_SCALE, default=DEFAULT_TEMPERATURE_SCALE): vol.In([0, 1]),
        vol.Optional(CONF_FILTER_PUMP_SPEEDS, default=DEFAULT_FILTER_PUMP_SPEEDS): vol.In(
            FILTER_PUMP_SPEED_OPTIONS
        ),
    }
)


class ConnectMyPoolConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            pool_api_code = user_input[CONF_POOL_API_CODE].strip()
            base_url = user_input.get(CONF_BASE_URL, DEFAULT_BASE_URL).strip()
            temperature_scale = int(user_input.get(CONF_TEMPERATURE_SCALE, DEFAULT_TEMPERATURE_SCALE))
            filter_pump_speeds = user_input.get(CONF_FILTER_PUMP_SPEEDS, DEFAULT_FILTER_PUMP_SPEEDS)

            session = async_get_clientsession(self.hass)
            api = ConnectMyPoolApi(session, base_url=base_url)

            try:
                # Validate by fetching config
                await api.pool_config(pool_api_code, force=True)
            except ConnectMyPoolAuthError:
                errors["base"] = "invalid_auth"
            except ConnectMyPoolError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(_stable_id(pool_api_code))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="ConnectMyPool",
                    data={
                        CONF_POOL_API_CODE: pool_api_code,
                        CONF_BASE_URL: base_url,
                        CONF_TEMPERATURE_SCALE: temperature_scale,
                        CONF_FILTER_PUMP_SPEEDS: filter_pump_speeds,
                    },
                )

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors)

    async def async_step_import(self, user_input) -> FlowResult:
        return await self.async_step_user(user_input)

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return ConnectMyPoolOptionsFlow()


class ConnectMyPoolOptionsFlow(config_entries.OptionsFlow):
    # Home Assistant provides self.config_entry (2024.11+); assigning it in
    # __init__ is no longer allowed and breaks the Configure dialog.

    async def async_step_init(self, user_input=None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(int, vol.Range(min=60, max=3600)),
                    vol.Optional(
                        CONF_WAIT_FOR_EXECUTION,
                        default=self.config_entry.options.get(CONF_WAIT_FOR_EXECUTION, DEFAULT_WAIT_FOR_EXECUTION),
                    ): bool,
                    vol.Optional(
                        CONF_EXPOSE_CHANNEL_SWITCHES,
                        default=self.config_entry.options.get(CONF_EXPOSE_CHANNEL_SWITCHES, DEFAULT_EXPOSE_CHANNEL_SWITCHES),
                    ): bool,
                    vol.Optional(
                        CONF_EXPOSE_SETPOINT_NUMBERS,
                        default=self.config_entry.options.get(CONF_EXPOSE_SETPOINT_NUMBERS, DEFAULT_EXPOSE_SETPOINT_NUMBERS),
                    ): bool,
                    vol.Optional(
                        CONF_FILTER_PUMP_SPEEDS,
                        default=self.config_entry.options.get(
                            CONF_FILTER_PUMP_SPEEDS,
                            self.config_entry.data.get(CONF_FILTER_PUMP_SPEEDS, DEFAULT_FILTER_PUMP_SPEEDS),
                        ),
                    ): vol.In(FILTER_PUMP_SPEED_OPTIONS),
                    vol.Optional(
                        CONF_TEMPERATURE_SCALE,
                        default=self.config_entry.options.get(
                            CONF_TEMPERATURE_SCALE,
                            self.config_entry.data.get(CONF_TEMPERATURE_SCALE, DEFAULT_TEMPERATURE_SCALE),
                        ),
                    ): vol.In([0, 1]),
                    vol.Optional(
                        CONF_BASE_URL,
                        default=self.config_entry.options.get(
                            CONF_BASE_URL,
                            self.config_entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
                        ),
                    ): str,
                }
            ),
        )
