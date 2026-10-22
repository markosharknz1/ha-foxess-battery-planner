"""Runtime controller: nightly plan, stop-at-target, restore, live preview.

One controller per config entry. All mutable state lives in ``self.data``
and is persisted through a Store, so a restart mid-charge recovers: the
saved baseline schedule is written back if the window has already ended,
otherwise the stop-at-target and window-end listeners are re-armed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import FoxessApi, FoxessApiError
from .const import (
    CONF_API_KEY,
    CONF_ARRAY_KWP,
    CONF_CHARGE_END,
    CONF_CHARGE_START,
    CONF_CLEAR_SKY_YIELD,
    CONF_DEVICE_SN,
    CONF_FORECAST_ENTITIES,
    CONF_MIN_SOC_ON_GRID,
    CONF_NOTIFY,
    CONF_PLAN_LEAD,
    CONF_SLUG,
    CONF_SOC_ENTITY,
    CONF_WEATHER_ENTITY,
    DEFAULTS,
    DOMAIN,
    MAX_PLAN_RETRIES,
    MAX_RESTORE_RETRIES,
    RETRY_DELAY_S,
    SETTINGS,
    SIGNAL_UPDATE,
    STATUS_CHARGING,
    STATUS_COMPLETE,
    STATUS_DISABLED,
    STATUS_DRY_RUN,
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_NOT_NEEDED,
    STATUS_SKIPPED,
    PLAN_TIME_TOLERANCE_MIN,
    STORE_VERSION,
    TZ_RECHECK_HOURS,
    WINDOW_KEYS,
)
from .planner import (
    PlanInputs,
    clearness_factor,
    compute_plan,
    sum_forecasts,
    weather_estimate_kwh,
)
from .schedule import (
    ScheduleError,
    ScheduleParams,
    build_charge_schedule,
    build_restore_schedule,
    check_windows,
    has_charge_windows,
    normalize_baseline,
    strip_charge_windows,
)
from .util import minutes_apart, minutes_to_hhmm, time_to_minutes, timezones_agree

_LOGGER = logging.getLogger(__name__)

_BAD = ("unknown", "unavailable", "none", "")


def _default_data() -> dict[str, Any]:
    return {
        "settings": {k: v[0] for k, v in SETTINGS.items()},
        "enabled": True,
        "dry_run": True,  # first runs only report what they would send
        "baseline": None,
        "baseline_enabled": True,
        "modified": False,
        "target_soc": None,
        "status": STATUS_IDLE,
        "message": "Waiting for the first plan",
        "last_run": None,
        "last_error": None,
        "last_payload": None,
        "applied": None,
        "modified_at": None,
        "ignore_tz": False,  # skip the inverter time-zone safety check
        "tz_check": None,
        "tz_checked_at": None,
    }


class PlannerController:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.slug: str = entry.data[CONF_SLUG]
        self.api = FoxessApi(
            async_get_clientsession(hass),
            entry.data[CONF_API_KEY],
            entry.data[CONF_DEVICE_SN],
            time_zone=hass.config.time_zone,
        )
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.data: dict[str, Any] = _default_data()
        self.preview: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._unsubs: list = []
        self._retry_unsub = None
        self._params = ScheduleParams()

    # ------------------------------------------------------------ options

    def opt(self, key: str):
        if key in self.entry.options:
            return self.entry.options[key]
        return self.entry.data.get(key, DEFAULTS.get(key))

    @property
    def windows(self) -> list[tuple[int, int]]:
        """Configured charge windows as sorted (start, end) minutes."""
        found = []
        for start_key, end_key in WINDOW_KEYS:
            start, end = self.opt(start_key), self.opt(end_key)
            if start and end:
                found.append((time_to_minutes(start), time_to_minutes(end)))
        return check_windows(found)

    @property
    def window_label(self) -> str:
        return ", ".join(
            f"{minutes_to_hhmm(s)}-{minutes_to_hhmm(e)}" for s, e in self.windows
        )

    def _schedule_params(self, live: dict | None = None) -> ScheduleParams:
        supports_max = True
        max_groups = 8
        if live:
            props = live.get("properties")
            if isinstance(props, dict) and props:
                supports_max = "maxsoc" in props
            try:
                max_groups = int(live.get("maxGroupCount") or 8)
            except (TypeError, ValueError):
                pass
        return ScheduleParams(
            min_soc_on_grid=int(self.opt(CONF_MIN_SOC_ON_GRID)),
            charge_power_w=int(round(self.data["settings"].get("charge_power_kw", 0) * 1000)),
            supports_max_soc=supports_max,
            max_groups=max_groups,
        )

    # ----------------------------------------------------------- lifecycle

    async def async_setup(self) -> None:
        saved = await self._store.async_load()
        if isinstance(saved, dict):
            defaults = _default_data()
            settings = {**defaults["settings"], **(saved.get("settings") or {})}
            self.data = {**defaults, **saved, "settings": settings}

        windows = self.windows
        lead = int(self.opt(CONF_PLAN_LEAD))
        self._plan_at = max(windows[0][0] - lead, 0)
        self._last_end = windows[-1][1]
        self._unsubs.append(
            async_track_time_change(
                self.hass,
                self._on_plan_time,
                hour=self._plan_at // 60,
                minute=self._plan_at % 60,
                second=0,
            )
        )
        for _, end in windows:
            self._unsubs.append(
                async_track_time_change(
                    self.hass,
                    self._make_end_callback(end),
                    hour=end // 60 % 24,
                    minute=end % 60,
                    second=0,
                )
            )
        watched = [self.opt(CONF_SOC_ENTITY), *(self.opt(CONF_FORECAST_ENTITIES) or [])]
        if self.opt(CONF_WEATHER_ENTITY):
            watched.append(self.opt(CONF_WEATHER_ENTITY))
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [e for e in watched if e], self._on_source_change
            )
        )

        from homeassistant.helpers.start import async_at_started

        async_at_started(self.hass, self._async_after_start)

    async def _async_after_start(self, _hass=None) -> None:
        await self.async_refresh_preview()
        if not self.data["modified"]:
            return
        # Restore if the last window ended after the schedule was modified.
        since = dt_util.parse_datetime(self.data.get("modified_at") or "")
        now = dt_util.now()
        if since is None or self._next_last_end_after(since) <= now:
            await self.async_restore("Restored after restart (charge window over)")
        else:
            _LOGGER.info("Restart mid-charge: stop-at-target and window-end re-armed")

    def _next_last_end_after(self, since: datetime) -> datetime:
        """First occurrence of the last window's end time after ``since``."""
        local = dt_util.as_local(since)
        end = self._last_end
        moment = local.replace(hour=end // 60 % 24, minute=end % 60, second=0, microsecond=0)
        if end >= 1440:
            moment += timedelta(days=1)
        if moment <= local:
            moment += timedelta(days=1)
        return moment

    async def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._retry_unsub:
            self._retry_unsub()
            self._retry_unsub = None

    async def _async_save(self) -> None:
        await self._store.async_save(self.data)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    def _publish(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    # ------------------------------------------------------ runtime values

    async def async_set_setting(self, key: str, value: float) -> None:
        self.data["settings"][key] = float(value)
        await self._async_save()
        await self.async_refresh_preview()

    async def async_set_flag(self, key: str, value: bool) -> None:
        self.data[key] = bool(value)
        if key == "enabled" and not value and not self.data["modified"]:
            self.data["status"] = STATUS_DISABLED
            self.data["message"] = "Automatic planning is off"
        elif key == "enabled" and value and self.data["status"] == STATUS_DISABLED:
            self.data["status"] = STATUS_IDLE
            self.data["message"] = "Waiting for the next plan"
        await self._async_save()

    # --------------------------------------------------------- source data

    def _read_soc(self) -> float | None:
        state = self.hass.states.get(self.opt(CONF_SOC_ENTITY))
        if state is None or str(state.state).lower() in _BAD:
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    async def _weather_estimate(self) -> tuple[float | None, str]:
        entity = self.opt(CONF_WEATHER_ENTITY)
        kwp = float(self.opt(CONF_ARRAY_KWP) or 0)
        if not entity or kwp <= 0:
            return None, ""
        cover = condition = None
        try:
            resp = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity, "type": "daily"},
                blocking=True,
                return_response=True,
            )
            items = (resp.get(entity) or {}).get("forecast") or []
            if items:
                cover = items[0].get("cloud_coverage")
                condition = items[0].get("condition")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("weather.get_forecasts failed for %s: %s", entity, err)
        if cover is None and condition is None:
            state = self.hass.states.get(entity)
            if state is not None:
                cover = state.attributes.get("cloud_coverage")
                condition = state.state
        factor = clearness_factor(cover, condition)
        kwh = weather_estimate_kwh(kwp, self.opt(CONF_CLEAR_SKY_YIELD), factor)
        return kwh, f"weather:{entity}"

    async def _read_forecast(self) -> dict[str, Any]:
        readings = []
        for entity in self.opt(CONF_FORECAST_ENTITIES) or []:
            state = self.hass.states.get(entity)
            if state is None:
                readings.append((None, None))
            else:
                readings.append((state.state, state.attributes.get("unit_of_measurement")))
        result = sum_forecasts(readings)
        if result.kwh is not None:
            note = "forecast sensors"
            if result.skipped:
                note += f" ({result.skipped} of {result.used + result.skipped} unavailable)"
            return {"kwh": result.kwh, "source": note, "used": result.used,
                    "skipped": result.skipped, "fallback": False}
        kwh, label = await self._weather_estimate()
        if kwh is not None:
            return {"kwh": kwh, "source": label, "used": 0,
                    "skipped": result.skipped, "fallback": True}
        return {"kwh": 0.0, "source": "none (assuming no solar)", "used": 0,
                "skipped": result.skipped, "fallback": True, "missing": True}

    async def _compute(self) -> dict[str, Any]:
        soc = self._read_soc()
        forecast = await self._read_forecast()
        s = self.data["settings"]
        plan = None
        if soc is not None:
            plan = compute_plan(
                PlanInputs(
                    soc=soc,
                    capacity_kwh=s["battery_capacity"],
                    forecast_kwh=forecast["kwh"],
                    usage_kwh=s["daily_usage"],
                    pre_solar_kwh=s["pre_solar_load"],
                    safety=s["solar_trust"] / 100.0,
                    buffer_pct=s["buffer"],
                    min_target=s["min_target_soc"],
                    max_target=s["max_target_soc"],
                )
            )
        return {"soc": soc, "forecast": forecast, "plan": plan, "at": dt_util.now().isoformat()}

    async def async_refresh_preview(self) -> None:
        self.preview = await self._compute()
        self._publish()

    # ----------------------------------------------------------- listeners

    @callback
    def _on_source_change(self, event: Event) -> None:
        self.hass.async_create_task(self._handle_source_change(event.data.get("entity_id")))

    async def _handle_source_change(self, entity_id: str | None) -> None:
        await self.async_refresh_preview()
        if (
            entity_id == self.opt(CONF_SOC_ENTITY)
            and self.data["modified"]
            and self.data["status"] == STATUS_CHARGING
        ):
            soc = self.preview.get("soc")
            target = self.data.get("target_soc")
            if soc is not None and target is not None and soc >= target:
                await self.async_restore(f"Target {target}% reached")

    @callback
    def _on_plan_time(self, _now: datetime) -> None:
        self.hass.async_create_task(self.async_run_plan())

    def _make_end_callback(self, end: int):
        @callback
        def _on_window_end(_now: datetime) -> None:
            # Only the last window's end releases the schedule: earlier windows
            # ending must leave later charge blocks in place.
            if self.data["modified"] and end == self._last_end:
                self.hass.async_create_task(self.async_restore("Charge window ended"))

        return _on_window_end

    # ---------------------------------------------------------------- plan

    def _notify(self, title: str, message: str) -> None:
        if self.opt(CONF_NOTIFY):
            persistent_notification.async_create(
                self.hass, message, title=title, notification_id=f"{DOMAIN}_{self.slug}"
            )

    def _set_status(self, status: str, message: str, *, error: str | None = None) -> None:
        self.data["status"] = status
        self.data["message"] = message
        self.data["last_error"] = error
        self.data["last_run"] = dt_util.now().isoformat()

    async def async_run_plan(self, manual: bool = False, attempt: int = 0) -> None:
        async with self._lock:
            await self._run_plan_locked(manual, attempt)
        await self._async_save()

    async def _run_plan_locked(self, manual: bool, attempt: int) -> None:
        if not manual and not self.data["enabled"]:
            self._set_status(STATUS_DISABLED, "Automatic planning is off")
            return

        if not manual:
            now = dt_util.now()
            drift = minutes_apart(now.hour * 60 + now.minute, self._plan_at)
            if drift > PLAN_TIME_TOLERANCE_MIN:
                msg = (
                    f"Plan fired at {now:%H:%M}, expected near {minutes_to_hhmm(self._plan_at)}. "
                    "Skipped as a safety measure; check the Home Assistant clock and time zone."
                )
                self._set_status(STATUS_SKIPPED, msg)
                self._notify("FoxESS planner skipped: unexpected time", msg)
                return

        info = await self._compute()
        self.preview = info
        plan, soc, forecast = info["plan"], info["soc"], info["forecast"]

        if plan is None:
            msg = f"Battery SoC sensor {self.opt(CONF_SOC_ENTITY)} is unavailable"
            self._set_status(STATUS_SKIPPED, msg)
            self._notify("FoxESS planner skipped tonight", msg)
            return

        summary = (
            f"SoC {soc:.0f}% | forecast {forecast['kwh']:.1f} kWh ({forecast['source']}) | "
            f"grid needed {plan.grid_needed_kwh:.1f} kWh"
        )

        if not plan.charge:
            if self.data["modified"]:
                await self._restore_locked(f"{plan.reason}; releasing earlier charge")
            self.data["target_soc"] = plan.target_soc
            self._set_status(STATUS_NOT_NEEDED, plan.reason)
            self.data["applied"] = {"summary": summary, "charge": False}
            self._notify("FoxESS overnight charge not needed", summary)
            return

        windows = self.windows
        tz = await self._check_timezone()
        if tz["status"] == "mismatch" and not self.data["ignore_tz"]:
            msg = (
                f"Time zone mismatch: FoxESS site is {tz['site_tz']}, Home Assistant is "
                f"{tz['ha_tz']}. The charge window would run at the wrong time, so nothing "
                "was written. Fix the zone in the FoxESS app or Home Assistant."
            )
            self._set_status(STATUS_ERROR, msg, error=msg)
            self._notify("FoxESS planner blocked: time zone mismatch", msg)
            return

        try:
            live = await self.api.get_schedule()
            self._params = self._schedule_params(live)
            if not self.data["modified"]:
                # The live schedule is the user's own: remember it so the
                # restore step puts their blocks back, not a blank schedule.
                self.data["baseline"] = strip_charge_windows(live.get("groups"), windows)
                self.data["baseline_enabled"] = bool(live.get("enable"))
            groups = build_charge_schedule(
                self.data["baseline"] or [], windows, plan.target_soc, self._params
            )

            if self.data["dry_run"]:
                self.data["last_payload"] = groups
                self._set_status(
                    STATUS_DRY_RUN,
                    f"Dry run: would charge to {plan.target_soc}%. Nothing was sent to FoxESS.",
                )
                self._notify(
                    "FoxESS planner dry run",
                    f"{summary}\nTarget {plan.target_soc}%. "
                    "Payload is on the status sensor's last_payload attribute.",
                )
                return

            await self.api.set_schedule(groups)
            check = await self.api.get_schedule()
            if not has_charge_windows(normalize_baseline(check.get("groups")), windows):
                raise FoxessApiError("FoxESS accepted the schedule but it does not show the charge block")
        except (FoxessApiError, ScheduleError) as err:
            await self._handle_plan_error(err, manual, attempt)
            return

        self.data["modified"] = True
        self.data["modified_at"] = dt_util.now().isoformat()
        self.data["target_soc"] = plan.target_soc
        self.data["last_payload"] = groups
        self.data["applied"] = {"summary": summary, "charge": True}
        self._set_status(STATUS_CHARGING, f"Charging from grid to {plan.target_soc}%")
        self._notify(
            "FoxESS overnight charge started", f"{summary}\nTarget {plan.target_soc}%"
        )

    async def _check_timezone(self) -> dict[str, Any]:
        """Compare the FoxESS site's time zone with Home Assistant's.

        'ok' results are cached for a few hours; 'unknown' (the lookup failed
        or FoxESS did not say) never blocks, but is shown on the status sensor.
        """
        cached = self.data.get("tz_check")
        checked = dt_util.parse_datetime(self.data.get("tz_checked_at") or "")
        if (
            cached
            and cached.get("status") == "ok"
            and checked
            and dt_util.now() - checked < timedelta(hours=TZ_RECHECK_HOURS)
        ):
            return cached
        ha_tz = str(self.hass.config.time_zone)
        result: dict[str, Any] = {"status": "unknown", "ha_tz": ha_tz, "site_tz": None}
        try:
            site_tz = await self.api.get_site_timezone()
        except FoxessApiError as err:
            result["detail"] = f"lookup failed: {err}"
        else:
            if not site_tz:
                result["detail"] = "FoxESS did not report a time zone for this inverter"
            else:
                result["site_tz"] = site_tz
                agree = timezones_agree(site_tz, ha_tz, dt_util.utcnow())
                if agree is True:
                    result["status"] = "ok"
                elif agree is False:
                    result["status"] = "mismatch"
                else:
                    result["detail"] = "could not compare the zone names"
        self.data["tz_check"] = result
        self.data["tz_checked_at"] = dt_util.now().isoformat()
        return result

    async def _handle_plan_error(self, err: Exception, manual: bool, attempt: int) -> None:
        text = str(err)
        self._set_status(STATUS_ERROR, f"Plan failed: {text}", error=text)
        retry = not manual and attempt < MAX_PLAN_RETRIES and isinstance(err, FoxessApiError)
        if retry:
            self._notify("FoxESS planner error", f"{text}\nRetrying in {RETRY_DELAY_S // 60} minutes.")
            self._retry_unsub = async_call_later(
                self.hass,
                RETRY_DELAY_S,
                lambda _now: self.hass.async_create_task(
                    self.async_run_plan(attempt=attempt + 1)
                ),
            )
        else:
            self._notify("FoxESS planner FAILED", text)

    # ------------------------------------------------------------- restore

    async def async_restore(self, reason: str, attempt: int = 0) -> None:
        async with self._lock:
            await self._restore_locked(reason, attempt)
        await self._async_save()

    async def _restore_locked(self, reason: str, attempt: int = 0) -> None:
        if not self.data["modified"]:
            self._set_status(STATUS_IDLE, "Nothing to restore")
            return
        try:
            groups = build_restore_schedule(self.data["baseline"], self._params)
            enable = bool(self.data.get("baseline_enabled", True))
            if not self.data["baseline"] and not enable:
                await self.api.set_flag(False)
            else:
                await self.api.set_schedule(groups, enable=enable)
        except (FoxessApiError, ScheduleError) as err:
            text = str(err)
            self._set_status(STATUS_ERROR, f"Restore failed: {text}", error=text)
            self._notify("FoxESS planner could NOT restore the schedule", text)
            if attempt < MAX_RESTORE_RETRIES:  # stays "modified", so keep trying
                self._retry_unsub = async_call_later(
                    self.hass,
                    RETRY_DELAY_S,
                    lambda _now: self.hass.async_create_task(
                        self.async_restore(reason, attempt + 1)
                    ),
                )
            return
        self.data["modified"] = False
        self._set_status(STATUS_COMPLETE, f"{reason}. Schedule restored.")
        self._notify("FoxESS charge finished", f"{reason}. Your schedule is back to normal.")
