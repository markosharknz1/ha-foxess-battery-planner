# FoxESS Cloud API notes

What this integration sends to FoxESS, where each fact comes from, and what
is still unverified against a real inverter.

## Endpoints used

| Purpose | Endpoint |
|---|---|
| Read schedule (also credential check) | `POST /op/v3/device/scheduler/get` |
| Write schedule | `POST /op/v3/device/scheduler/enable` |
| Read / set the schedule on-off flag | `POST /op/v1/device/scheduler/get/flag`, `.../set/flag` |
| Inverter site time zone | `POST /op/v0/device/list` (find the serial, read `stationID`), then `POST /op/v0/plant/list` (read `ianaTimezone`) |

Base URL `https://www.foxesscloud.com`. All bodies are JSON with
`deviceSN`.

## Request signature

Headers: `token` (your API key), `timestamp` (ms), `signature`, `lang`,
`Content-Type: application/json`.

```
signature = md5( path + "\r\n" + token + "\r\n" + timestamp )
```

**The separator is the four literal characters backslash, r, backslash, n.
It is not a carriage return plus line feed.** FoxESS's sample code and the
reference library both build it with a raw string. A template engine such as
Jinja turns `'\r\n'` into real control characters and produces a different
signature, which FoxESS rejects.

This is the most likely reason a YAML `rest_command` package that writes
`'\r\n'` in a Jinja template would fail. `tests/test_logic.py` pins the
correct form.

## Schedule group shape (v3)

```json
{
  "startHour": 2, "startMinute": 0, "endHour": 5, "endMinute": 59,
  "workMode": "ForceCharge",
  "isRemainMode": false,
  "extraParam": { "minSocOnGrid": 10, "maxSoc": 80, "fdSoc": 80 }
}
```

- The end time is **inclusive**, so a window ending at 06:00 is sent as 05:59.
- The all-day group (00:00 to 23:59) is the background ("remain") mode.
- `fdPwr` (charge power in W) is only sent if you set a charge power limit.
- `maxSoc` is sent only if the device's `properties` advertise it.
- Up to `maxGroupCount` groups (8 on the devices seen). The planner refuses
  to write rather than drop one of your blocks.

The v1 `scheduler/enable` call with flat fields (used by some YAML
packages) is a different, older form. This integration follows the v3 shape
used by the reference library.

## How the schedule is preserved

FoxESS replaces the whole schedule on each write. So:

1. At plan time, if the integration has not already modified the schedule, it
   reads the live schedule and stores it as the **baseline**.
2. The charge block is carved into the baseline. Your other blocks are kept;
   any that overlap the window are trimmed or split.
3. At the end (target reached, window ended, or Cancel), the baseline is
   written back, and the on/off flag is returned to what it was.

The baseline is saved to disk, so a Home Assistant restart mid-charge still
restores it. Edits you make in the FoxESS app **while a charge is in
progress** are overwritten at the end.

## Rate limits

FoxESS allows 1440 calls per inverter per day. The `macxq/foxess-ha`
integration uses roughly 528 per day by default (264 in eco mode). This
planner uses a handful on a normal night.

## Sources

- TonyM1958/FoxESS-Cloud, `src/foxesscloud/openapi.py`: signature, v3
  scheduler get/enable, set/flag, group and `extraParam` shapes.
- macxq/foxess-ha README: entity names, polling intervals, rate limits.

## Not verified against a live inverter

These come from reading the reference library, not from running this code
against FoxESS:

- That the device list row carries a `stationID` that matches a plant in the
  plant list. If it does not and the account has more than one plant, the
  time zone comes back unknown and the check only warns.
- That `fdPwr` in a `ForceCharge` group limits the **grid charge rate** on
  your model. The reference library sets it for forced modes, but I have
  not seen FoxESS state the exact meaning. Watch grid import during the
  first live charge; if it ignores the setting, lower the circuit load
  another way (for example a smaller inverter-side limit in the FoxESS app).
- That `scheduler/enable` accepts the all-day group together with an
  overlapping `ForceCharge` group in one request.
- Whether `fdPwr` is required, or rejected, for `ForceCharge` on your model.
  It is sent by default (3 kW). If FoxESS rejects the write with it, set the
  Grid charge rate slider to 0 to leave it out.
- Whether time outside any group behaves as Self Use on your firmware.
- The exact `errno` values returned for each failure (only `40400`, the
  daily limit, has a specific message).

That is why **Dry run is on by default**: it builds and records the exact
payload without sending it. The first live night should be watched.
