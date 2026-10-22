# Solar and weather data sources

The planner needs three inputs. This page lists exactly what it reads, which
integrations are supported for each, and what happens when one is missing.

| Input | What it is | Required |
|---|---|---|
| Battery state of charge | A sensor reporting the battery level in % | Yes |
| Solar forecast for today | One or more sensors reporting expected production today, in Wh, kWh or MWh | One of these two |
| Weather entity (fallback) | A `weather.*` entity, plus your array size in kWp | One of these two |

## How forecast sensors are read

- You pick **one sensor per array** (several are allowed). Their values are
  **summed**.
- Each sensor's own `unit_of_measurement` is used to convert to kWh. `Wh`,
  `kWh` and `MWh` are understood. A sensor with no unit is assumed to be kWh.
  **Anything else (for example W) is rejected**, not guessed at. That stops a
  power sensor being mistaken for energy.
- Pick the **whole-day total for today**. Do not pick "remaining today",
  "this hour", "next hour" or "tomorrow".
- If some sensors are unavailable, the rest are summed and the card shows a
  warning. A partial sum under-estimates solar, so the error is in the
  direction of charging a little more from the grid.

## Supported forecast integrations

Sensor names below were read from each project's documentation or source on
2026-10-22. **The exact `entity_id` depends on your install** (device name,
multiple entries, `_2` suffixes), which is why you pick the entity from a list
instead of typing it. Check yours in Developer Tools > States.

### Solcast PV Forecast: `BJReplay/ha-solcast-solar` (HACS)

| Pick this sensor | Unit | Notes |
|---|---|---|
| **Forecast Today** | kWh | Daily total across all your rooftop sites |

- Typically `sensor.solcast_pv_forecast_forecast_today`.
- Do not also add the per-site total sensors: Forecast Today already includes
  every site, so that would double-count.
- Solcast's free "hobbyist" plan is limited to a small number of API calls per
  day (the README currently says 10 for new accounts, up to 50 for original
  ones; check, Solcast has changed this before). The integration manages its
  own schedule; this planner only reads the sensor.
- Also exposes 10th and 90th percentile estimates as attributes of the same
  sensor. The planner uses the main (50th percentile) state and applies its
  own **Solar forecast trust** factor on top.

### Forecast.Solar: built into Home Assistant

| Pick this sensor | Unit |
|---|---|
| **Estimated energy production - today** | kWh |

- Not "remaining today", "this hour" or "next hour".
- The docs give `sensor.energy_production_tomorrow` as an example ID, so the
  today sensor is likely `sensor.energy_production_today`. Confirm in your
  install.
- Several planes configured inside one entry are handled by the integration.
  If you have separate entries (for example two roofs), add each entry's
  "today" sensor here.
- The free tier updates hourly with one plane; an API key gives 30-minute
  updates and multiple planes.

### Open-Meteo Solar Forecast: `rany2/ha-open-meteo-solar-forecast` (HACS)

| Pick this sensor | Unit |
|---|---|
| **Estimated energy production - today** (key `energy_production_today`) | Wh native, shown as kWh |

- The sensor's native unit is **Wh**; Home Assistant may show kWh. Either is
  fine, the unit attribute is read and converted.
- One entry per array/string: add each one's "today" sensor.
- Its README does not mention an API key.

### Anything else

Any sensor that reports **expected production for today** in Wh, kWh or MWh
works, including template sensors you write yourself and other forecast
integrations. If you have one that is not listed, choose it in the same
field.

**Not suitable:** power sensors (W), hourly or half-hourly breakdowns, and
"remaining today" values. Those are rejected or would under-count.

## Weather fallback

If **no** forecast sensor can be read (none configured, or all unavailable),
and you configured a weather entity and an array size, the planner estimates:

```
forecast kWh = array kWp  x  clear-sky kWh per kWp per day  x  clearness
```

- **Clearness** comes from the first daily forecast item returned by
  `weather.get_forecasts` for your weather entity:
  - If it has numeric `cloud_coverage` (0-100 %), clearness is
    `1 - 0.75 x (cover/100)^3.4` (the Kasten-Czeplak relation): 0 % cloud gives
    1.0, 100 % cloud gives 0.25.
  - Otherwise the `condition` is mapped to a fixed factor (sunny 1.0,
    partly cloudy 0.6, cloudy 0.3, rainy 0.2, pouring 0.1, and so on).
  - If the weather entity supplies no forecast, its current state is used.
- **Clear-sky yield** is your setting (default 4.5 kWh per kWp per day, a
  cautious figure). Set it to what a fully sunny day gives you.
- Works with any weather integration that supports daily forecasts (for
  example Met.no, OpenWeatherMap, or a BOM integration). **I have not tested
  against any specific one.**
- This is a rough estimate. It ignores shading, panel angle, season and
  temperature. Use a proper PV forecast integration if you can; keep the
  weather entity as the safety net.

## Battery state of charge

Any sensor reporting battery % works.

| Source | Sensor | Notes |
|---|---|---|
| FoxESS Cloud, `macxq/foxess-ha` (HACS) | **Bat SoC** (and Bat SoC1/SoC2 for dual-battery) | Named `{name} - Bat SoC`, so with the default name `FoxESS` the ID is `sensor.foxess_bat_soc`. Polls every 5 minutes by default (10 in eco mode) |
| Any other integration | Any % sensor | Local Modbus integrations give fresher values than the cloud |

The freshness of this sensor matters: the planner stops charging when the
sensor reaches the target, and the FoxESS cloud poll can lag by minutes. The
charge block also carries the target as the inverter's own max SoC so the
inverter does not rely on Home Assistant to stop.

## When data is missing

| Situation | What the planner does |
|---|---|
| SoC sensor unavailable at plan time | Skips the night, status `skipped`, notification |
| Some forecast sensors unavailable | Sums the rest, warns on the card |
| All forecast sensors unavailable, weather configured | Uses the weather estimate, card says so |
| No forecast and no usable weather | Assumes **no solar** (forecast 0), so it charges as far as needed and the card shows a warning |
| FoxESS cloud unreachable | Retries up to 3 times at plan time (2-minute gaps) and notifies |

## Checking the first night

The "today" value must be the **new** day's forecast when planning runs
(default 01:55). Most integrations roll over at local midnight, but confirm
yours: after the first dry run, compare the **Solar forecast used** sensor
with what the source sensor showed.
