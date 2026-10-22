"""End-to-end tests against a real Home Assistant core with a fake FoxESS API."""
from __future__ import annotations

import copy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import SupportsResponse
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.foxess_planner import controller as ctl_mod
from custom_components.foxess_planner.api import FoxessApiError
from custom_components.foxess_planner.const import DOMAIN
from custom_components.foxess_planner.schedule import ScheduleParams, group_span, make_group

P = ScheduleParams()
SOC = "sensor.foxess_bat_soc"


def _user_schedule():
    return [
        make_group("SelfUse", 0, 1440, P),
        make_group("ForceDischarge", 1020, 1200, P),  # the user's evening block
    ]


class FakeApi:
    def __init__(self, *args, **kwargs):
        self.schedule = {
            "enable": 1,
            "groups": _user_schedule(),
            "maxGroupCount": 8,
            "properties": {"maxsoc": {}},
        }
        self.writes: list = []
        self.flags: list = []
        self.fail_set: Exception | None = None
        self.site_tz: str | None = None

    async def get_site_timezone(self):
        return self.site_tz

    async def get_flag(self):
        return {"enable": 1, "support": True}

    async def get_schedule(self):
        return copy.deepcopy(self.schedule)

    async def set_schedule(self, groups, enable=True):
        if self.fail_set:
            raise self.fail_set
        self.writes.append((copy.deepcopy(groups), enable))
        self.schedule = {**self.schedule, "groups": copy.deepcopy(groups), "enable": int(enable)}

    async def set_flag(self, enable):
        self.flags.append(enable)


OPTIONS = {
    "soc_entity": SOC,
    "forecast_entities": ["sensor.om_today", "sensor.solcast_today"],
    "charge_start": "02:00:00",
    "charge_end": "06:00:00",
    "plan_lead_minutes": 5,
    "min_soc_on_grid": 10,
    "notify": False,
}


async def _setup(hass, options=None):
    hass.config.components.update({"http", "frontend"})
    hass.states.async_set(SOC, "30", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.om_today", "8000", {"unit_of_measurement": "Wh"})
    hass.states.async_set("sensor.solcast_today", "4.5", {"unit_of_measurement": "kWh"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Fox",
        unique_id="SN1",
        data={"name": "Fox", "slug": "fox", "api_key": "k", "device_sn": "SN1"},
        options=options or OPTIONS,
    )
    entry.add_to_hass(hass)
    with patch.object(ctl_mod, "FoxessApi", FakeApi), patch.object(
        ctl_mod, "async_get_clientsession", lambda _hass: None
    ), patch("custom_components.foxess_planner.async_register_card", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    ctl = hass.data[DOMAIN][entry.entry_id]
    ctl.api.site_tz = str(hass.config.time_zone)  # agrees with HA unless a test says otherwise
    return entry, ctl, ctl.api


async def _press(hass, entity_id):
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done()


async def _set_number(hass, entity_id, value):
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )
    await hass.async_block_till_done()


async def _dry_run_off(hass):
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.fox_dry_run"}, blocking=True
    )


async def test_entities_and_live_preview(hass):
    await _setup(hass)
    assert hass.states.get("sensor.fox_status").state == "idle"
    assert hass.states.get("switch.fox_dry_run").state == "on"  # safe by default
    assert hass.states.get("switch.fox_enabled").state == "on"
    assert hass.states.get("number.fox_battery_capacity").state == "10.0"
    # Wh (Open-Meteo style) and kWh (Solcast style) are summed: 8 + 4.5
    assert float(hass.states.get("sensor.fox_forecast").state) == pytest.approx(12.5)

    await _set_number(hass, "number.fox_daily_usage", 5)
    # surplus 5, stored 3, needed 3 -> 30 + 30 + 3 buffer = 63
    assert hass.states.get("sensor.fox_target_soc").state == "63"
    assert float(hass.states.get("sensor.fox_grid_needed").state) == pytest.approx(3.0)

    hass.states.async_set(SOC, "80", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.fox_status").attributes["preview_charge"] is False


async def test_dry_run_sends_nothing(hass):
    _, ctl, api = await _setup(hass)
    await _set_number(hass, "number.fox_daily_usage", 5)
    await _press(hass, "button.fox_plan_now")
    status = hass.states.get("sensor.fox_status")
    assert status.state == "dry_run"
    assert api.writes == []
    modes = [g["workMode"] for g in status.attributes["last_payload"]]
    assert "ForceCharge" in modes
    assert ctl.data["modified"] is False


async def test_charge_then_stop_at_target_restores_user_schedule(hass):
    _, ctl, api = await _setup(hass)
    await _set_number(hass, "number.fox_daily_usage", 5)
    await _dry_run_off(hass)
    await _press(hass, "button.fox_plan_now")

    assert hass.states.get("sensor.fox_status").state == "charging"
    groups, enable = api.writes[0]
    spans = {(g["workMode"], *group_span(g)) for g in groups}
    assert ("ForceCharge", 120, 360) in spans
    assert ("ForceDischarge", 1020, 1200) in spans  # user's block survived
    charge = next(g for g in groups if g["workMode"] == "ForceCharge")
    assert charge["extraParam"]["maxSoc"] == 63
    assert hass.states.get("sensor.fox_target_soc").state == "63"

    # battery climbs but not there yet -> still charging
    hass.states.async_set(SOC, "60", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.fox_status").state == "charging"

    # reaches target -> schedule written back exactly as the user had it
    hass.states.async_set(SOC, "63", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.fox_status").state == "complete"
    restored, _ = api.writes[-1]
    assert {(g["workMode"], *group_span(g)) for g in restored} == {
        ("SelfUse", 0, 1440),
        ("ForceDischarge", 1020, 1200),
    }
    assert ctl.data["modified"] is False


async def test_not_needed_leaves_schedule_alone(hass):
    _, ctl, api = await _setup(hass)
    await _set_number(hass, "number.fox_daily_usage", 5)  # forecast covers the day
    await _set_number(hass, "number.fox_pre_solar_load", 0)
    hass.states.async_set(SOC, "95", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    await _dry_run_off(hass)
    await _press(hass, "button.fox_plan_now")
    assert hass.states.get("sensor.fox_status").state == "not_needed"
    assert api.writes == []


async def test_api_failure_is_reported_and_leaves_state_clean(hass):
    _, ctl, api = await _setup(hass)
    await _set_number(hass, "number.fox_daily_usage", 5)
    await _dry_run_off(hass)
    api.fail_set = FoxessApiError("errno 40256: illegal signature", 40256)
    await _press(hass, "button.fox_plan_now")
    status = hass.states.get("sensor.fox_status")
    assert status.state == "error"
    assert "40256" in status.attributes["last_error"]
    assert ctl.data["modified"] is False


async def test_cancel_button_restores(hass):
    _, ctl, api = await _setup(hass)
    await _set_number(hass, "number.fox_daily_usage", 5)
    await _dry_run_off(hass)
    await _press(hass, "button.fox_plan_now")
    await _press(hass, "button.fox_cancel")
    assert hass.states.get("sensor.fox_status").state == "complete"
    assert ctl.data["modified"] is False


async def test_restart_after_window_restores(hass, freezer):
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-10-22 03:00:00+00:00")  # inside the 02:00-06:00 window
    _, ctl, api = await _setup(hass)
    await _set_number(hass, "number.fox_daily_usage", 5)
    await _dry_run_off(hass)
    await _press(hass, "button.fox_plan_now")
    assert ctl.data["modified"]

    # restart at 04:00, window still open: stay in charge
    freezer.move_to("2026-10-22 04:00:00+00:00")
    n = len(api.writes)
    await ctl._async_after_start()
    assert len(api.writes) == n and ctl.data["modified"]

    # restart at midday: the last window ended after we modified -> restore
    freezer.move_to("2026-10-22 12:00:00+00:00")
    await ctl._async_after_start()
    assert len(api.writes) == n + 1
    assert ctl.data["modified"] is False


async def test_disabled_skips_automatic_plan(hass):
    _, ctl, api = await _setup(hass)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.fox_enabled"}, blocking=True
    )
    await ctl.async_run_plan()  # as the timer would
    assert hass.states.get("sensor.fox_status").state == "disabled"


async def test_weather_fallback_when_no_forecast_sensor(hass):
    async def fake_forecasts(call):
        return {call.data["entity_id"]: {"forecast": [{"cloud_coverage": 0, "condition": "sunny"}]}}

    hass.services.async_register(
        "weather", "get_forecasts", fake_forecasts, supports_response=SupportsResponse.ONLY
    )
    opts = {
        **OPTIONS,
        "forecast_entities": [],
        "weather_entity": "weather.home",
        "array_kwp": 6.0,
        "clear_sky_yield": 4.5,
    }
    hass.states.async_set("weather.home", "sunny", {})
    await _setup(hass, opts)
    fc = hass.states.get("sensor.fox_forecast")
    assert float(fc.state) == pytest.approx(27.0)  # 6 kWp x 4.5 x clear sky
    assert fc.attributes["source"].startswith("weather:")


async def test_no_forecast_at_all_assumes_no_solar(hass):
    opts = {**OPTIONS, "forecast_entities": ["sensor.om_today"]}
    await _setup(hass, opts)
    hass.states.async_set("sensor.om_today", "unavailable", {"unit_of_measurement": "Wh"})
    await hass.async_block_till_done()
    fc = hass.states.get("sensor.fox_forecast")
    assert float(fc.state) == 0 and fc.attributes["missing"] is True


async def test_config_flow_happy_path_and_bad_key(hass):
    from custom_components.foxess_planner import config_flow as cf

    hass.config.components.update({"http", "frontend"})
    hass.states.async_set(SOC, "50", {})

    class Bad(FakeApi):
        async def get_flag(self):
            raise FoxessApiError("errno 40256", 40256)

    session = patch.object(cf, "async_get_clientsession", lambda _hass: None)
    session.start()
    with patch.object(cf, "FoxessApi", Bad):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {"name": "Fox", "api_key": "bad", "device_sn": "SN9"}
        )
    assert r["errors"] == {"base": "invalid_auth"}

    with patch.object(cf, "FoxessApi", FakeApi), patch(
        "custom_components.foxess_planner.async_register_card", AsyncMock()
    ), patch.object(ctl_mod, "FoxessApi", FakeApi), patch.object(
        ctl_mod, "async_get_clientsession", lambda _hass: None
    ):
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {"name": "Fox", "api_key": "good", "device_sn": "SN9"}
        )
        assert r["step_id"] == "sources"
        # no forecast at all -> rejected
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"soc_entity": SOC})
        assert r["errors"] == {"base": "need_forecast"}
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {"soc_entity": SOC, "forecast_entities": ["sensor.om_today"]}
        )
        assert r["step_id"] == "schedule"
        bad = {
            "charge_start": "06:00:00",
            "charge_end": "02:00:00",
            "plan_lead_minutes": 5,
            "min_soc_on_grid": 10,
            "notify": True,
        }
        r = await hass.config_entries.flow.async_configure(r["flow_id"], bad)
        assert r["errors"] == {"base": "window_order"}
        good = {**bad, "charge_start": "02:00:00", "charge_end": "06:00:00"}
        half = await hass.config_entries.flow.async_configure(
            r["flow_id"], {**good, "charge_start_2": "12:00:00"}
        )
        assert half["errors"] == {"base": "window_incomplete"}
        overlap = await hass.config_entries.flow.async_configure(
            r["flow_id"], {**good, "charge_start_2": "05:00:00", "charge_end_2": "07:00:00"}
        )
        assert overlap["errors"] == {"base": "window_overlap"}
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {**good, "charge_start_2": "12:00:00", "charge_end_2": "14:00:00"}
        )
        assert r["type"] == "create_entry"
        await hass.async_block_till_done()
    session.stop()
    assert r["data"]["slug"] == "fox"
    assert r["options"]["forecast_entities"] == ["sensor.om_today"]
    assert r["options"]["charge_end_2"] == "14:00:00"


async def test_charge_rate_is_sent_in_watts_from_kw_slider(hass):
    _, ctl, api = await _setup(hass)
    await _set_number(hass, "number.fox_daily_usage", 5)
    await _set_number(hass, "number.fox_charge_power_kw", 2.5)
    await _dry_run_off(hass)
    await _press(hass, "button.fox_plan_now")
    charge = next(g for g in api.writes[0][0] if g["workMode"] == "ForceCharge")
    assert charge["extraParam"]["fdPwr"] == 2500
    assert hass.states.get("sensor.fox_status").attributes["charge_power_kw"] == 2.5


async def test_charge_rate_zero_is_not_sent(hass):
    _, ctl, api = await _setup(hass)
    await _set_number(hass, "number.fox_daily_usage", 5)
    await _set_number(hass, "number.fox_charge_power_kw", 0)
    await _dry_run_off(hass)
    await _press(hass, "button.fox_plan_now")
    charge = next(g for g in api.writes[0][0] if g["workMode"] == "ForceCharge")
    assert "fdPwr" not in charge["extraParam"]


async def test_minimum_soc_overrides_a_great_forecast(hass):
    _, ctl, api = await _setup(hass)
    await _set_number(hass, "number.fox_daily_usage", 0)  # forecast covers everything
    await _set_number(hass, "number.fox_pre_solar_load", 0)
    hass.states.async_set(SOC, "10", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    await _dry_run_off(hass)
    await _press(hass, "button.fox_plan_now")
    assert hass.states.get("sensor.fox_status").state == "charging"
    charge = next(g for g in api.writes[0][0] if g["workMode"] == "ForceCharge")
    assert charge["extraParam"]["maxSoc"] == 20  # the default minimum SoC


async def test_two_windows_charge_and_release_after_the_last(hass):
    opts = {**OPTIONS, "charge_start_2": "12:00:00", "charge_end_2": "14:00:00"}
    _, ctl, api = await _setup(hass, opts)
    await _set_number(hass, "number.fox_daily_usage", 5)
    await _dry_run_off(hass)
    await _press(hass, "button.fox_plan_now")
    spans = {(g["workMode"], *group_span(g)) for g in api.writes[0][0]}
    assert ("ForceCharge", 120, 360) in spans and ("ForceCharge", 720, 840) in spans
    label = hass.states.get("sensor.fox_status").attributes["charge_window"]
    assert label == "02:00-06:00, 12:00-14:00"

    # first window ends: the later block must stay in place
    ctl._make_end_callback(360)(None)
    await hass.async_block_till_done()
    assert ctl.data["modified"] and len(api.writes) == 1

    # last window ends: the saved schedule goes back
    ctl._make_end_callback(840)(None)
    await hass.async_block_till_done()
    assert ctl.data["modified"] is False and len(api.writes) == 2


async def test_timezone_mismatch_blocks_the_write(hass):
    _, ctl, api = await _setup(hass)
    await _set_number(hass, "number.fox_daily_usage", 5)
    await _dry_run_off(hass)
    api.site_tz = "Europe/London"  # HA harness zone is US/Pacific
    await _press(hass, "button.fox_plan_now")
    status = hass.states.get("sensor.fox_status")
    assert status.state == "error" and "Time zone mismatch" in status.attributes["last_error"]
    assert api.writes == [] and ctl.data["modified"] is False
    assert status.attributes["time_zone_check"]["status"] == "mismatch"

    # the explicit override lets it through
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.fox_ignore_tz"}, blocking=True
    )
    await _press(hass, "button.fox_plan_now")
    assert hass.states.get("sensor.fox_status").state == "charging"


async def test_timezone_unknown_does_not_block(hass):
    _, ctl, api = await _setup(hass)
    await _set_number(hass, "number.fox_daily_usage", 5)
    await _dry_run_off(hass)
    api.site_tz = None  # FoxESS gave no zone
    await _press(hass, "button.fox_plan_now")
    status = hass.states.get("sensor.fox_status")
    assert status.state == "charging"
    assert status.attributes["time_zone_check"]["status"] == "unknown"


async def test_same_offset_zone_names_agree(hass):
    _, ctl, api = await _setup(hass)
    await hass.config.async_set_time_zone("Australia/Sydney")
    api.site_tz = "Australia/Melbourne"
    check = await ctl._check_timezone()
    assert check["status"] == "ok"


async def test_automatic_plan_at_the_wrong_time_is_skipped(hass, freezer):
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-10-22 12:00:00+00:00")  # plan is due ~01:55
    _, ctl, api = await _setup(hass)
    await _set_number(hass, "number.fox_daily_usage", 5)
    await _dry_run_off(hass)
    await ctl.async_run_plan()  # as the timer would
    status = hass.states.get("sensor.fox_status")
    assert status.state == "skipped" and "expected near 01:55" in status.attributes["message"]
    assert api.writes == []

    freezer.move_to("2026-10-22 01:57:00+00:00")  # close enough: runs
    await ctl.async_run_plan()
    assert hass.states.get("sensor.fox_status").state == "charging"
