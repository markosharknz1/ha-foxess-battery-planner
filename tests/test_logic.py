"""Pure-logic tests for planner.py, schedule.py and api.py.

Plain pytest, no Home Assistant — modules are loaded by file path because
the package __init__ imports homeassistant (deliberately not in CI).
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "foxess_planner"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"fp_{name}", _PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


planner = _load("planner")
schedule = _load("schedule")
api = _load("api")


# ---------------------------------------------------------------- units

@pytest.mark.parametrize(
    "value,unit,expected",
    [
        ("12.5", "kWh", 12.5),
        ("12500", "Wh", 12.5),
        ("0.0125", "MWh", 12.5),
        ("12.5", None, 12.5),  # unitless template sensor
        ("12.5", "KWH", 12.5),
        ("unavailable", "kWh", None),
        ("unknown", "kWh", None),
        (None, "kWh", None),
        ("nan", "kWh", None),
        ("-3", "kWh", None),
        ("500", "W", None),  # power, not energy: rejected, not guessed
    ],
)
def test_to_kwh(value, unit, expected) -> None:
    assert planner.to_kwh(value, unit) == expected


def test_open_meteo_wh_and_solcast_kwh_sum() -> None:
    # Two arrays on different integrations/units add up correctly.
    result = planner.sum_forecasts([("8000", "Wh"), ("4.5", "kWh")])
    assert result.kwh == pytest.approx(12.5)
    assert (result.used, result.skipped) == (2, 0)


def test_partial_forecast_is_reported() -> None:
    result = planner.sum_forecasts([("8", "kWh"), ("unavailable", "kWh")])
    assert result.kwh == 8
    assert (result.used, result.skipped) == (1, 1)


def test_no_forecast_is_none() -> None:
    result = planner.sum_forecasts([("unknown", "kWh")])
    assert result.kwh is None and result.skipped == 1


# -------------------------------------------------------------- weather

def test_clearness_from_cloud_cover() -> None:
    assert planner.clearness_factor(cloud_coverage=0) == 1.0
    assert planner.clearness_factor(cloud_coverage=100) == pytest.approx(0.25)
    mid = planner.clearness_factor(cloud_coverage=50)
    assert 0.25 < mid < 1.0
    # clamps out-of-range input
    assert planner.clearness_factor(cloud_coverage=250) == pytest.approx(0.25)


def test_clearness_falls_back_to_condition() -> None:
    assert planner.clearness_factor(condition="sunny") == 1.0
    assert planner.clearness_factor(condition="rainy") < 0.3
    assert planner.clearness_factor(condition="nonsense") is None
    assert planner.clearness_factor() is None
    # cloud cover wins over condition when both are present
    assert planner.clearness_factor(cloud_coverage=100, condition="sunny") == pytest.approx(0.25)


def test_weather_estimate() -> None:
    assert planner.weather_estimate_kwh(6.6, 4.5, 0.5) == pytest.approx(14.85)
    assert planner.weather_estimate_kwh(0, 4.5, 0.5) is None
    assert planner.weather_estimate_kwh(6.6, 4.5, None) is None


# ----------------------------------------------------------------- plan

def _inputs(**kw):
    base = dict(
        soc=30, capacity_kwh=10, forecast_kwh=20, usage_kwh=10, pre_solar_kwh=1,
        safety=0.8, buffer_pct=3, min_target=20, max_target=100,
    )
    base.update(kw)
    return planner.PlanInputs(**base)


def test_plan_matches_original_yaml_formula() -> None:
    # surplus = max(20*0.8-10,0)=6 ; stored=3 ; needed=10-3-6+1=2
    # target = round(30 + 2/10*100 + 3) = 53
    plan = planner.compute_plan(_inputs())
    assert plan.solar_surplus_kwh == pytest.approx(6)
    assert plan.grid_needed_kwh == pytest.approx(2)
    assert plan.target_soc == 53 and plan.charge


def test_plan_no_charge_when_forecast_covers() -> None:
    plan = planner.compute_plan(_inputs(soc=60, forecast_kwh=40))
    assert not plan.charge


def test_plan_target_is_clamped() -> None:
    assert planner.compute_plan(_inputs(soc=0, forecast_kwh=0)).target_soc == 100
    capped = planner.compute_plan(_inputs(soc=0, forecast_kwh=0, max_target=80))
    assert capped.target_soc == 80
    # min target floors the figure but never forces a charge below current SoC
    # a minimum SoC is a hard floor: below it, charge regardless of forecast
    floored = planner.compute_plan(_inputs(soc=50, forecast_kwh=40, min_target=70))
    assert floored.target_soc == 70 and floored.charge
    assert "minimum" in floored.reason.lower()


def test_plan_small_need_is_ignored() -> None:
    # needed <= 0.2 kWh is noise
    plan = planner.compute_plan(_inputs(soc=79, forecast_kwh=0, usage_kwh=0, pre_solar_kwh=0))
    assert plan.grid_needed_kwh == pytest.approx(2.1)
    tiny = planner.compute_plan(_inputs(soc=98.5, forecast_kwh=0, usage_kwh=0, pre_solar_kwh=0))
    assert not tiny.charge


def test_plan_rounds_half_up_not_bankers() -> None:
    # raw target is exactly 52.5: round() would give 52, we want 53
    plan = planner.compute_plan(
        _inputs(soc=30, forecast_kwh=5.9375, usage_kwh=0, pre_solar_kwh=0, buffer_pct=0)
    )
    assert plan.target_soc == 53


# ------------------------------------------------------------- schedule

P = schedule.ScheduleParams(min_soc_on_grid=10, charge_power_w=0)
WIN = [(120, 360)]  # 02:00-06:00


def _spans(groups):
    return [(g["workMode"], *schedule.group_span(g)) for g in groups]


def test_charge_schedule_empty_baseline() -> None:
    groups = schedule.build_charge_schedule([], WIN, 80, P)
    assert _spans(groups) == [("SelfUse", 0, 1440), ("ForceCharge", 120, 360)]
    charge = groups[1]
    assert (charge["endHour"], charge["endMinute"]) == (5, 59)
    assert charge["extraParam"]["maxSoc"] == 80
    assert charge["extraParam"]["fdSoc"] == 80
    assert "fdPwr" not in charge["extraParam"]
    assert groups[0]["isRemainMode"] is True and charge["isRemainMode"] is False


def test_charge_power_included_when_set() -> None:
    p = schedule.ScheduleParams(charge_power_w=10500)
    groups = schedule.build_charge_schedule([], WIN, 80, p)
    assert groups[1]["extraParam"]["fdPwr"] == 10500


def test_max_soc_omitted_when_unsupported() -> None:
    p = schedule.ScheduleParams(supports_max_soc=False)
    groups = schedule.build_charge_schedule([], WIN, 80, p)
    assert "maxSoc" not in groups[1]["extraParam"]


def test_baseline_blocks_are_preserved_and_trimmed() -> None:
    base = schedule.normalize_baseline([
        schedule.make_group("SelfUse", 0, 1440, P),
        schedule.make_group("ForceCharge", 60, 180, P, 100),      # overlaps window start
        schedule.make_group("ForceDischarge", 1020, 1200, P),     # evening peak, untouched
        schedule.make_group("Backup", 0, 1, P),                   # tiny, before window
    ])
    groups = schedule.build_charge_schedule(base, WIN, 70, P)
    spans = _spans(groups)
    assert ("ForceCharge", 60, 120) in spans            # trimmed to end at window start
    assert ("ForceCharge", 120, 360) in spans           # ours
    assert ("ForceDischarge", 1020, 1200) in spans      # preserved
    assert spans[0] == ("SelfUse", 0, 1440)
    # no two non-remain groups overlap
    rest = sorted(s[1:] for s in spans[1:])
    assert all(a[1] <= b[0] for a, b in zip(rest, rest[1:]))


def test_block_spanning_window_is_split() -> None:
    base = [schedule.make_group("Backup", 60, 480, P)]
    groups = schedule.build_charge_schedule(base, WIN, 70, P)
    spans = _spans(groups)
    assert ("Backup", 60, 120) in spans and ("Backup", 360, 480) in spans


def test_too_many_groups_refuses() -> None:
    base = [schedule.make_group("Backup", 400 + i * 20, 410 + i * 20, P) for i in range(8)]
    with pytest.raises(schedule.ScheduleError):
        schedule.build_charge_schedule(base, WIN, 70, P)


def test_invalid_window_refuses() -> None:
    with pytest.raises(schedule.ScheduleError):
        schedule.build_charge_schedule([], [(360, 120)], 70, P)


def test_restore_returns_baseline_or_default() -> None:
    assert _spans(schedule.build_restore_schedule(None, P)) == [("SelfUse", 0, 1440)]
    base = [schedule.make_group("ForceDischarge", 1020, 1200, P)]
    restored = schedule.build_restore_schedule(base, P)
    assert _spans(restored) == [("SelfUse", 0, 1440), ("ForceDischarge", 1020, 1200)]


def test_normalize_drops_garbage() -> None:
    out = schedule.normalize_baseline([
        {"workMode": "SelfUse", "startHour": 0, "startMinute": 0, "endHour": 23, "endMinute": 59},
        {"workMode": "", "startHour": 1},
        {"startHour": 1},
        "junk",
        {"workMode": "Backup", "startHour": 5, "startMinute": 0, "endHour": 4, "endMinute": 0},
    ])
    assert len(out) == 1 and out[0]["isRemainMode"] is True
    assert out[0]["extraParam"] == {}


def test_has_and_strip_charge_window() -> None:
    groups = schedule.build_charge_schedule([], WIN, 70, P)
    assert schedule.has_charge_windows(groups, WIN)
    assert not schedule.has_charge_windows(groups, [(100, 360)])
    cleaned = schedule.strip_charge_windows(groups, WIN)
    assert _spans(cleaned) == [("SelfUse", 0, 1440)]


# ------------------------------------------------------------------ api

def test_signature_uses_literal_backslashes_not_crlf() -> None:
    path, token, ts = "/op/v3/device/scheduler/enable", "abc-123", "1700000000000"
    literal = f"{path}\\r\\n{token}\\r\\n{ts}"          # 4 literal chars per separator
    crlf = f"{path}\r\n{token}\r\n{ts}"                # what a templating engine produces
    assert "\\r\\n" in literal and "\r\n" not in literal
    assert api.sign(path, token, ts) == hashlib.md5(literal.encode()).hexdigest()
    assert api.sign(path, token, ts) != hashlib.md5(crlf.encode()).hexdigest()


class _Resp:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Resp(*item)


def _client(responses):
    session = _Session(responses)
    client = api.FoxessApi(session, "key", "SN1", min_interval=0, tries=3)
    return client, session


def _no_sleep(monkeypatch):
    async def fast(_):
        return None

    monkeypatch.setattr(api.asyncio, "sleep", fast)


def test_get_schedule_sends_signed_headers(monkeypatch) -> None:
    _no_sleep(monkeypatch)
    client, session = _client([(200, {"errno": 0, "result": {"enable": 1, "groups": []}})])
    result = asyncio.run(client.get_schedule())
    assert result["enable"] == 1
    url, body, headers = session.calls[0]
    assert url.endswith("/op/v3/device/scheduler/get") and body == {"deviceSN": "SN1"}
    assert headers["signature"] == api.sign(
        "/op/v3/device/scheduler/get", "key", headers["timestamp"]
    )


def test_retries_on_5xx_and_network_errors(monkeypatch) -> None:
    _no_sleep(monkeypatch)
    client, session = _client([
        (503, {}),
        ConnectionError("boom"),
        (200, {"errno": 0, "result": {"enable": 0}}),
    ])
    assert asyncio.run(client.get_flag()) == {"enable": 0}
    assert len(session.calls) == 3


def test_gives_up_after_tries(monkeypatch) -> None:
    _no_sleep(monkeypatch)
    client, _ = _client([ConnectionError("a"), ConnectionError("b"), ConnectionError("c")])
    with pytest.raises(api.FoxessConnectionError):
        asyncio.run(client.get_flag())


def test_api_errno_is_raised_not_retried(monkeypatch) -> None:
    _no_sleep(monkeypatch)
    client, session = _client([(200, {"errno": 40400, "msg": "x"})])
    with pytest.raises(api.FoxessApiError) as err:
        asyncio.run(client.get_flag())
    assert err.value.errno == 40400 and "limit" in str(err.value)
    assert len(session.calls) == 1


def test_set_schedule_writes_groups_then_flag(monkeypatch) -> None:
    _no_sleep(monkeypatch)
    ok = (200, {"errno": 0, "result": {}})
    client, session = _client([ok, ok])
    groups = schedule.build_charge_schedule([], WIN, 80, P)
    asyncio.run(client.set_schedule(groups))
    (u1, b1, _), (u2, b2, _) = session.calls
    assert u1.endswith("/op/v3/device/scheduler/enable")
    assert b1 == {"deviceSN": "SN1", "isDefault": False, "groups": groups}
    assert u2.endswith("/op/v1/device/scheduler/set/flag")
    assert b2 == {"deviceSN": "SN1", "enable": 1}


# --------------------------------------------------- minimum SoC override

def test_min_soc_forces_charge_even_with_huge_forecast() -> None:
    plan = planner.compute_plan(_inputs(soc=12, forecast_kwh=60, min_target=20))
    assert plan.charge and plan.target_soc >= 20
    assert plan.grid_needed_kwh <= 0.2  # forecast alone says no, minimum overrides


def test_at_or_above_min_soc_follows_forecast() -> None:
    plan = planner.compute_plan(_inputs(soc=25, forecast_kwh=60, min_target=20))
    assert not plan.charge


def test_min_soc_zero_disables_override() -> None:
    plan = planner.compute_plan(_inputs(soc=3, forecast_kwh=60, min_target=0))
    assert not plan.charge


def test_min_soc_respects_max_target() -> None:
    plan = planner.compute_plan(_inputs(soc=5, forecast_kwh=60, min_target=50, max_target=40))
    assert plan.target_soc == 40  # max wins when the two conflict


# ------------------------------------------------------- multiple windows

def test_two_windows_each_get_a_charge_block() -> None:
    groups = schedule.build_charge_schedule([], [(120, 360), (720, 840)], 80, P)
    spans = _spans(groups)
    assert ("ForceCharge", 120, 360) in spans and ("ForceCharge", 720, 840) in spans
    charges = [g for g in groups if g["workMode"] == "ForceCharge"]
    assert all(g["extraParam"]["maxSoc"] == 80 for g in charges)


def test_windows_may_be_given_in_any_order() -> None:
    groups = schedule.build_charge_schedule([], [(720, 840), (120, 360)], 80, P)
    assert [s[1] for s in _spans(groups) if s[0] == "ForceCharge"] == [120, 720]


def test_overlapping_windows_refused() -> None:
    with pytest.raises(schedule.ScheduleError):
        schedule.build_charge_schedule([], [(120, 400), (360, 500)], 80, P)


def test_baseline_block_cut_by_two_windows() -> None:
    base = [schedule.make_group("Backup", 60, 900, P)]
    groups = schedule.build_charge_schedule(base, [(120, 360), (720, 840)], 70, P)
    backups = sorted(s[1:] for s in _spans(groups) if s[0] == "Backup")
    assert backups == [(60, 120), (360, 720), (840, 900)]


def test_has_charge_windows_requires_every_window() -> None:
    one = schedule.build_charge_schedule([], [(120, 360)], 70, P)
    assert schedule.has_charge_windows(one, [(120, 360)])
    assert not schedule.has_charge_windows(one, [(120, 360), (720, 840)])


def test_strip_removes_all_our_windows_only() -> None:
    wins = [(120, 360), (720, 840)]
    groups = schedule.build_charge_schedule(
        [schedule.make_group("ForceCharge", 1000, 1100, P, 100)], wins, 70, P
    )
    cleaned = schedule.strip_charge_windows(groups, wins)
    assert ("ForceCharge", 1000, 1100) in _spans(cleaned)       # the user's own block stays
    assert ("ForceCharge", 120, 360) not in _spans(cleaned)


# ------------------------------------------------------ time-zone safety

def test_timezones_agree_same_and_different() -> None:
    from datetime import datetime, timezone
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    util = _load("util")
    assert util.timezones_agree("Australia/Sydney", "Australia/Melbourne", now) is True
    assert util.timezones_agree("Australia/Sydney", "Australia/Brisbane", now) is False  # no DST
    assert util.timezones_agree("Australia/Sydney", "Europe/London", now) is False
    assert util.timezones_agree("Australia/Sydney", "Not/AZone", now) is None


def test_minutes_apart_wraps_midnight() -> None:
    util = _load("util")
    assert util.minutes_apart(115, 115) == 0
    assert util.minutes_apart(115, 175) == 60
    assert util.minutes_apart(10, 1430) == 20
