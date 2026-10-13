from __future__ import annotations

DOMAIN = "connectmypool"

# Config keys
CONF_POOL_API_CODE = "pool_api_code"
CONF_BASE_URL = "base_url"
CONF_TEMPERATURE_SCALE = "temperature_scale"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_WAIT_FOR_EXECUTION = "wait_for_execution"
CONF_EXPOSE_CHANNEL_SWITCHES = "expose_channel_switches"
CONF_EXPOSE_SETPOINT_NUMBERS = "expose_setpoint_numbers"
CONF_FILTER_PUMP_SPEEDS = "filter_pump_speeds"

# Defaults
DEFAULT_BASE_URL = "https://www.connectmypool.com.au"
DEFAULT_TEMPERATURE_SCALE = 0  # 0=C, 1=F (per ConnectMyPool docs)
DEFAULT_SCAN_INTERVAL = 60     # seconds (ConnectMyPool cloud throttles config/status calls ~60s)
DEFAULT_WAIT_FOR_EXECUTION = True  # waiting avoids "optimistic" UI flips
DEFAULT_EXPOSE_CHANNEL_SWITCHES = True
DEFAULT_EXPOSE_SETPOINT_NUMBERS = False
FILTER_PUMP_SPEED_OPTIONS = ["single", "two", "three"]
DEFAULT_FILTER_PUMP_SPEEDS = "two"  # pre-option behaviour (Viron two-speed)

# Platforms
PLATFORMS: list[str] = [
    "sensor",
    "select",
    "switch",
    "light",
    "button",
    "climate",
    "water_heater",
    # Optional/legacy (disabled by default via options):
    "number",
]

# ---- Enumerations (per ConnectMyPool Home Automation Integration Guide) ----

CHANNEL_MODES = {
    0: "Off",
    1: "Auto",
    2: "On",
    3: "Low Speed",
    4: "Medium Speed",
    5: "High Speed",
}

TRI_MODES = {
    0: "Off",
    1: "Auto",
    2: "On",
}

HEATER_MODES = {
    0: "Off",
    1: "On",
}

POOL_SPA = {
    0: "Spa",
    1: "Pool",
}

HEAT_COOL = {
    0: "Cooling",
    1: "Heating",
}

# ---- Action codes ----
ACTION_CYCLE_CHANNEL = 1
ACTION_SET_VALVE_MODE = 2
ACTION_SET_POOL_SPA = 3
ACTION_SET_HEATER_MODE = 4
ACTION_SET_HEATER_SET_TEMP = 5
ACTION_SET_LIGHT_MODE = 6
ACTION_SET_LIGHT_COLOR = 7
ACTION_SET_ACTIVE_FAVOURITE = 8
ACTION_SET_SOLAR_MODE = 9
ACTION_SET_SOLAR_SET_TEMP = 10
ACTION_LIGHT_SYNC = 11
ACTION_SET_HEAT_COOL = 12

# Failure codes
FAILURE_CODE_THROTTLED = 6
FAILURE_CODE_POOL_NOT_CONNECTED = 7
