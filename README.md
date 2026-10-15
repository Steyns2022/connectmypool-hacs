# ConnectMyPool (Home Assistant / HACS)

A polished Home Assistant custom integration for **AstralPool ConnectMyPool**.

It talks to the official ConnectMyPool cloud endpoints documented in the *Home Automation Integration Guide* (PoolConfig/PoolStatus/PoolAction).

## What you get

- **Pool water temperature** sensor
- **Channel controls**
  - **Filter pump** as a proper mode selector, matched to the pump fitted
    (*Filter pump type* setting):
    - single-speed: **Off / Auto / On**
    - two-speed (default, Viron): **Off / Auto / Medium Speed / High Speed**
    - three-speed: **Off / Auto / Low / Medium / High Speed**
  - Other simple channels (for example Spa Jets, Spa Blower and Heater Pump) as **On/Off switches**
  - ConnectMyPool only exposes a channel-cycle command, so the integration reads the current mode, sends only the required cycles, and verifies the reported state after each command
- **Valve mode** selects (Off/Auto/On)
- **Lighting**
  - `light` entities (On/Off + optional effects/patterns)
  - Separate lighting **Mode** select (Off/Auto/On) because “Auto” doesn’t fit the light model cleanly
  - “Color Sync” diagnostic button (matches the official action)
- **Heater** as a proper `climate` entity (Off/Heat/Cool where supported)
- **Solar** as a proper `water_heater` entity (Off/Auto/On + setpoint)
- Advanced service: `connectmypool.send_action` (raw action code access)

## Important note: API throttle

The ConnectMyPool cloud throttles **poolconfig** and **poolstatus** to about **one response per 60 seconds**, except for ~5 minutes after an instruction is sent.

This integration is designed around that: default scan interval is 60s, and it uses cached data if the cloud replies with a throttle error.

Because the cloud's status lags the controller, after every command (channel
mode, favourite, lighting, heater, Pool/Spa, setpoints) the integration re-reads
status at about **15, 30, 60, 90 and 150 seconds** after the command, stopping
as soon as the new state is reported. These reads happen only inside the
~5-minute un-throttled window, and a newer command replaces (does not add to) a
pending schedule.

## Install (recommended via HACS)

1. Create a GitHub repo and copy this code into it (or unzip the provided zip and push it).
2. In Home Assistant: **HACS → Integrations → ⋮ → Custom repositories**
3. Add your repo URL and select **Integration**
4. Install **ConnectMyPool**, then restart Home Assistant.
5. Add the integration via **Settings → Devices & Services → Add Integration → ConnectMyPool**
6. Enter your **Pool API Code** (from the ConnectMyPool app), choose °C/°F if needed.

## Options

In **Settings → Devices & Services → ConnectMyPool → Configure**:

- Filter pump type: single / two / three speed (also asked during setup).
  It must match your pump's real cycle, or mode changes are refused.
- Scan interval (60–3600 seconds)
- Wait for execution (recommended ON; avoids UI “flip-flop”)
- Expose channel switches (for non-filter ON/OFF channels)
- Expose legacy setpoint numbers (diagnostic; usually unnecessary with climate/water_heater entities)
- Base URL (only change if you’re explicitly told to)

## Troubleshooting

- If entities “snap back”: enable **Wait for execution** and keep scan interval ≥ 60s.
- If a filter-pump cycle does not produce the expected next mode, the integration stops rather than continuing to cycle blindly.
- If you see “Pool not connected”: the controller isn’t currently connected to ConnectMyPool’s cloud (Wi‑Fi / gateway / service issue).
- Diagnostics: **Settings → Devices & Services → ConnectMyPool → Download diagnostics** (API code is redacted).

---

**Disclaimer:** This is an unofficial community integration. You use it at your own risk.


## Entity ID prefix helper

This integration includes a service `connectmypool.apply_entity_id_prefix` that can rename the integration's entities in your entity registry to include a prefix (default: `connectmypool_`). Run it once with `dry_run: true` to preview, then again with `dry_run: false` to apply.


## Changelog

- **1.0.13** — Faster confirmation after a command: follow-up status reads at
  ~15/30/60/90/150 s after each action (only inside the post-action
  un-throttled window), stopping once the target state is reported; a newer
  command replaces the pending schedule. Channel mode changes still read
  fresh status before deciding how many cycle steps to send.
