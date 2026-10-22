# CLAUDE.md — FoxESS Battery Planner

HACS integration (domain `foxess_planner`) + bundled Lovelace card
(`custom:foxess-planner-card`). Replaces a hand-written YAML package that
did the same job (see "Origin"). Same shape as the maintainer's
`ha-feedandwater-cards` integration; copy its patterns when unsure.

## Layout

```
custom_components/foxess_planner/
  planner.py     PURE: unit conversion, forecast summing, weather estimate, target SoC
  schedule.py    PURE: v3 schedule groups, baseline carve-out, restore
  api.py         FoxESS client (injected aiohttp session; signature lives here)
  controller.py  HA: nightly plan, stop-at-target, restore, live preview, Store persistence
  config_flow.py credentials -> sources -> window (+ OptionsFlow)
  sensor/number/switch/button.py, entity.py   entities, entity_id = <domain>.<slug>_<suffix>
  frontend.py    serves + auto-registers the card (adapted from feedandwater)
  lovelace/foxess-planner-card.js
tests/           plain pytest, no HA (CI runs this)
tests_ha/        real-HA harness tests, local only (see README)
docs/            FORECAST_SOURCES.md (supported data integrations), FOXESS_API.md
```

`planner.py`, `schedule.py` and `api.py` must stay free of Homeassistant
imports: the tests load them by file path.

## Hard-won facts (do not undo without re-checking)

- **Signature separator is the literal text `\r\n`** (backslash r backslash
  n), not CRLF. A Jinja `'\r\n'` yields real CRLF and FoxESS rejects it.
  Pinned by `test_signature_uses_literal_backslashes_not_crlf`.
- Uses the **v3** scheduler (`extraParam` nested) plus the separate
  `set/flag` call, as in TonyM1958/FoxESS-Cloud. The origin YAML used v1 flat.
- macxq SoC entity is `sensor.foxess_bat_soc` (name option + "Bat SoC"), NOT
  `foxess_battery_soc` as the origin YAML assumed.
- Open-Meteo Solar Forecast energy sensors are **Wh** natively; Solcast and
  Forecast.Solar are kWh. Never assume kWh: read the unit attribute.
- Round half up (`floor(x+0.5)`), not Python's banker's `round()`.
- Minimum SoC (`min_target_soc`) is a HARD FLOOR: below it the planner
  charges regardless of forecast. 0 disables it.
- Multiple windows = one shared target applied to every window (not one plan
  per window). Only the LAST window end releases the schedule.
- Restart recovery uses `modified_at`, not the current time alone.
- Entity ids come from the setting KEY (`number.<slug>_charge_power_kw`), not
  the display name: tests that use the wrong id fail silently.
- Heredocs containing apostrophes break the Bash tool here: write patch
  scripts with the Write tool and run them.

## Testing notes (Windows)

- Plain tests: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests -q`
  (the globally installed HA pytest plugin blocks sockets and breaks them).
- HA tests: `python -m pytest tests_ha -q -p no:sugar`. `tests_ha/conftest.py`
  neutralises pytest-socket on win32; tests patch `async_get_clientsession`
  because aiodns needs a SelectorEventLoop on Windows.

## Status

v0.1.0 scaffolded 2026-10-22. 50 logic tests + 19 HA harness tests pass.
**Never run against a real inverter.** Dry run defaults ON. Unverified
points are listed at the bottom of docs/FOXESS_API.md. No GitHub repo yet.

## Next

1. Create the repo (`markosharknz1/ha-foxess-battery-planner`), push, CI.
2. Live test on the user's HA: dry run -> inspect `last_payload` -> one
   supervised real charge -> Cancel -> confirm schedule restored.
3. Brand icon (`custom_components/foxess_planner/brand/`), like feedandwater.
4. Release via GitHub Releases (HACS installs releases, not master).
5. Ideas: query device max power for `fdPwr`; window crossing midnight;
   dual-battery capacity from the API.

## Origin

The user supplied `foxess_cloud_battery_package.yaml` (HA package using
`rest_command` + 3 automations). Its logic is kept (same plan formula);
signature, endpoint version, SoC entity name, schedule clobbering and
error handling were fixed here.
