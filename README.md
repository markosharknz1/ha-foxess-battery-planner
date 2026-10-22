# FoxESS Battery Planner

A Home Assistant integration (HACS) that decides each night how much your
FoxESS battery needs from the grid, charges it only that far, and then puts
your normal schedule back. It comes with a dashboard card.

It uses your solar forecast, the battery level and your expected daytime use:

```
solar surplus = forecast x trust% - daytime usage
grid needed   = capacity - stored - surplus + pre-solar load
target SoC    = current SoC + grid needed + buffer   (clamped to your min / max)
```

Planning runs a few minutes before the first charge window opens (default
01:55 for a 02:00-06:00 window). If charging is needed, a Force Charge block
is added to your FoxESS **Mode Schedule** with the target as its max SoC.
When the battery reaches the target, the last window ends, or you press
Cancel, your own schedule is written back.

## Safety features

- **Multiple charge windows.** Up to three (for example a cheap overnight
  period plus a midday one). One target is worked out per night and applied
  to every window, so charging stops as soon as the target is reached even if
  a later window is still ahead. Windows must not overlap or cross midnight.
- **Minimum SoC, whatever the forecast.** If the battery is below the
  *Minimum SoC* slider at planning time, it charges up to at least that
  level, even when the forecast says the day is covered. Set it to 0 to turn
  this off.
- **Grid charge rate in kW.** A slider (default 3 kW) is sent to the
  inverter as the charge power for the block, to keep the combined load
  inside your supply limit when other appliances run. 0 means "not sent",
  which leaves the inverter at its own default, usually its maximum. This
  limits the charge command only; it does not know about your other loads.
  Confirm on the first live charge that grid import really stays at the
  chosen rate (see [docs/FOXESS_API.md](docs/FOXESS_API.md)).
- **Time-zone check.** Before writing, the planner asks FoxESS for the
  inverter site's time zone and compares it with Home Assistant's, including
  daylight-saving rules. A mismatch **blocks the write** and says why (the
  schedule is plain clock times, so a wrong zone means charging at the wrong
  hour). If FoxESS does not report a zone, it warns but does not block. An
  *Ignore time zone check* switch exists for the case where you know better.
- **Plan-time guard.** The automatic plan only runs if the clock is within 30
  minutes of the expected planning time; otherwise it skips and tells you.
- **Window end always restores**, and a restart is handled using the time the
  schedule was modified.

> **Status: v0.1.0, tested against a fake FoxESS API only.** The logic and
> the Home Assistant layer are tested (see below), but nothing has been
> run against a real inverter. **Dry run is on by default**: it builds the
> exact payload and shows it on the Status sensor without sending anything.
> Read [docs/FOXESS_API.md](docs/FOXESS_API.md) before turning it off.

## Requirements

- A FoxESS Cloud account with an **API key**
  (foxesscloud.com > User Profile > API Management) and the inverter serial.
- A battery state-of-charge sensor in Home Assistant, for example the
  FoxESS Cloud integration `macxq/foxess-ha` ("Bat SoC").
- A solar forecast sensor (Solcast, Forecast.Solar or Open-Meteo Solar
  Forecast), or a weather entity as a fallback. See
  [docs/FORECAST_SOURCES.md](docs/FORECAST_SOURCES.md) for exactly what is
  supported.
- An inverter that supports the Mode Schedule feature.

Home Assistant 2024.7 or newer.

## Install

1. **HACS > Integrations > ⋮ > Custom repositories**: add
   `https://github.com/markosharknz1/ha-foxess-battery-planner`, category
   **Integration**. Install **FoxESS Battery Planner** and restart HA.
2. **Settings > Devices & Services > Add Integration > FoxESS Battery
   Planner**:
   - **Credentials**: API key and inverter serial (checked against FoxESS
     immediately; stored in HA, not in YAML).
   - **Data**: battery SoC sensor, forecast sensor(s), optional weather
     fallback.
   - **Charge windows**: one to three start/end pairs, how long before to
     plan, minimum SoC on grid (the inverter's own discharge floor).
3. Add the card to a dashboard: `type: custom:foxess-planner-card`. The
   resource is registered automatically.
4. Set **Battery capacity**, **Daytime usage** and the other sliders on the
   card (Settings drawer).

## Your first night

1. Leave **Dry run** on. Press **Plan now** during the day.
2. Open the Status sensor's attributes. `last_payload` is exactly what would
   be sent. Compare it with your schedule in the FoxESS app.
3. Check **Solar forecast used** against your forecast sensor, and look at
   `time_zone_check` in the Status attributes: it should say `ok`.
4. Turn Dry run off and press **Plan now** in the evening, with a battery
   level that needs charging. Confirm in the FoxESS app that the block
   appears, then press **Cancel charge** and confirm your schedule returns.
5. Only then leave automatic planning to run overnight, and watch the first
   morning.

## Entities

For a prefix `fox` (the entity prefix you chose at setup):

| Entity | Purpose |
|---|---|
| `sensor.fox_status` | idle / charging / complete / not_needed / dry_run / skipped / error, with a message and details as attributes |
| `sensor.fox_target_soc` | Target SoC: the applied value while charging, otherwise a live preview |
| `sensor.fox_grid_needed` | kWh the plan expects to take from the grid |
| `sensor.fox_forecast` | Solar forecast actually used, and where it came from |
| `sensor.fox_last_run` | When the planner last ran |
| `number.fox_*` | Battery capacity, daytime usage, pre-solar load, forecast trust, buffer, minimum SoC, maximum target, grid charge rate (kW) |
| `switch.fox_enabled` | Automatic nightly planning on / off |
| `switch.fox_dry_run` | Plan without writing to FoxESS |
| `switch.fox_ignore_tz` | Skip the time-zone safety check |
| `button.fox_plan_now` | Run the plan now (respects Dry run) |
| `button.fox_cancel` | Stop and restore your schedule |

The preview updates live as you move a slider or the battery / forecast
changes, so you can see tonight's likely target before it runs.

## When things go wrong

- **FoxESS unreachable or rejects the write**: status `error`, a
  notification, up to 3 retries 2 minutes apart. The schedule is not marked
  as modified, so nothing half-applied is assumed.
- **Restore fails**: status `error`, retried up to 5 times; it stays marked
  as modified so it is retried again at the next trigger.
- **Home Assistant restarts mid-charge**: state is saved. If the window is
  over, your schedule is written back at startup; otherwise stop-at-target
  and the window end are re-armed.
- **Window end** always restores, so a missed stop cannot leave the battery
  force-charging past the window.

## Limits and known gaps

- Charge windows cannot cross midnight. A window such as 22:00-02:00 has to
  be split into two: 22:00-23:59 and 00:00-02:00.
- Cloud only: if FoxESS Cloud or your internet is down at planning time, no
  charge happens that night.
- The SoC reading can lag (cloud polling every few minutes), so the battery
  may overshoot the target by a few percent. The target is also sent as the
  inverter's max SoC where supported.
- One planner per inverter.

## Development

```bash
# pure logic (no Home Assistant needed), same as CI
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests -q

# integration tests on a real Home Assistant core (needs
# pytest-homeassistant-custom-component)
python -m pytest tests_ha -q
```

`tests/` covers unit conversion, the plan maths, schedule building and the
API client (including the request signature). `tests_ha/` sets up the
integration in a real Home Assistant with a fake FoxESS API and checks
entities, the dry run, a full charge-stop-restore cycle, failures, restart
recovery, the weather fallback and the config flow.

## Credits

The FoxESS API behaviour was worked out from
[TonyM1958/FoxESS-Cloud](https://github.com/TonyM1958/FoxESS-Cloud) and
FoxESS's published API. Battery sensors are typically provided by
[macxq/foxess-ha](https://github.com/macxq/foxess-ha).

MIT licensed.
