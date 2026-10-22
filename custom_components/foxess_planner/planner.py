"""Pure planning logic: unit handling, forecast resolution, target SoC.

No Home Assistant imports, so tests load this file straight from disk.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

_ENERGY_TO_KWH = {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0}

# Weather-entity condition -> fraction of clear-sky output. Only used when
# no numeric cloud cover is available. Deliberately conservative.
CONDITION_CLEARNESS = {
    "sunny": 1.0,
    "clear-night": 1.0,
    "windy": 0.95,
    "windy-variant": 0.8,
    "partlycloudy": 0.6,
    "exceptional": 0.5,
    "cloudy": 0.3,
    "fog": 0.25,
    "rainy": 0.2,
    "snowy": 0.15,
    "snowy-rainy": 0.15,
    "lightning": 0.15,
    "lightning-rainy": 0.1,
    "hail": 0.1,
    "pouring": 0.1,
}


def to_kwh(value, unit) -> float | None:
    """Convert a sensor reading to kWh. None for anything unusable.

    A missing unit is taken as kWh (some template sensors omit it); an
    unrecognised unit (e.g. W) is rejected rather than guessed at.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number) or number < 0:
        return None
    factor = _ENERGY_TO_KWH.get(str(unit if unit else "kWh").strip().lower())
    if factor is None:
        return None
    return number * factor


@dataclass(frozen=True)
class ForecastResult:
    kwh: float | None  # None when no source could be read
    used: int  # sources that contributed
    skipped: int  # sources that were unavailable / unusable


def sum_forecasts(readings) -> ForecastResult:
    """Sum several (state, unit) readings — one per array/string/plane.

    A partial sum under-estimates solar, which errs towards charging from
    the grid, so it is used but reported via ``skipped``.
    """
    total = 0.0
    used = skipped = 0
    for state, unit in readings:
        kwh = to_kwh(state, unit)
        if kwh is None:
            skipped += 1
        else:
            total += kwh
            used += 1
    return ForecastResult(total if used else None, used, skipped)


def clearness_factor(cloud_coverage=None, condition=None) -> float | None:
    """Fraction of clear-sky output expected under the given sky.

    Numeric cloud cover (0-100 %) uses the Kasten-Czeplak relation; the
    condition string is the fallback.
    """
    if cloud_coverage is not None:
        try:
            cover = min(max(float(cloud_coverage), 0.0), 100.0) / 100.0
        except (TypeError, ValueError):
            cover = None
        if cover is not None:
            return 1.0 - 0.75 * cover**3.4
    if condition is not None:
        return CONDITION_CLEARNESS.get(str(condition).lower())
    return None


def weather_estimate_kwh(array_kwp, yield_per_kwp, clearness) -> float | None:
    """Rough daily yield: array size x clear-sky kWh/kWp x clearness."""
    try:
        kwp = float(array_kwp)
        per_kwp = float(yield_per_kwp)
    except (TypeError, ValueError):
        return None
    if clearness is None or kwp <= 0 or per_kwp <= 0:
        return None
    return kwp * per_kwp * clearness


@dataclass(frozen=True)
class PlanInputs:
    soc: float  # %
    capacity_kwh: float
    forecast_kwh: float
    usage_kwh: float  # expected household use while the sun is up
    pre_solar_kwh: float  # battery draw between window end and solar start
    safety: float  # fraction of the forecast to trust (0.8 = 80 %)
    buffer_pct: float
    min_target: float
    max_target: float


@dataclass(frozen=True)
class Plan:
    charge: bool
    target_soc: int
    grid_needed_kwh: float
    solar_surplus_kwh: float
    stored_kwh: float
    reason: str


def compute_plan(p: PlanInputs) -> Plan:
    capacity = max(float(p.capacity_kwh), 0.1)
    soc = min(max(float(p.soc), 0.0), 100.0)
    stored = soc / 100.0 * capacity
    surplus = max(p.forecast_kwh * p.safety - p.usage_kwh, 0.0)
    needed = max(capacity - stored - surplus + p.pre_solar_kwh, 0.0)

    high = min(max(float(p.max_target), 1.0), 100.0)
    low = min(float(p.min_target), high)
    raw = soc + needed / capacity * 100.0 + p.buffer_pct
    target = int(min(max(math.floor(raw + 0.5), low), high))

    below_min = soc < low - 0.01
    if below_min:
        target = max(target, int(math.ceil(low)))
        charge = True
        reason = f"Below the {int(low)}% minimum SoC: charge to {target}%"
    elif needed <= 0.2:
        charge, reason = False, "Battery plus forecast cover the day"
    elif target <= soc:
        charge, reason = False, f"Already at or above the {target}% target"
    else:
        charge, reason = True, f"Charge from grid to {target}%"
    return Plan(
        charge=charge,
        target_soc=target,
        grid_needed_kwh=needed,
        solar_surplus_kwh=surplus,
        stored_kwh=stored,
        reason=reason,
    )
