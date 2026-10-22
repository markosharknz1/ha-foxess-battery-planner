"""Sensors: status, target SoC, grid need, forecast used, last run."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STATUS_CHARGING, STATUSES
from .controller import PlannerController
from .entity import PlannerEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    ctl: PlannerController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            StatusSensor(ctl),
            TargetSocSensor(ctl),
            GridNeededSensor(ctl),
            ForecastSensor(ctl),
            LastRunSensor(ctl),
        ]
    )


def _plan(ctl: PlannerController):
    return ctl.preview.get("plan")


class StatusSensor(PlannerEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUSES
    _attr_icon = "mdi:battery-clock"

    def __init__(self, ctl: PlannerController) -> None:
        super().__init__(ctl, "sensor", "status")

    @property
    def native_value(self) -> str:
        return self.ctl.data["status"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d, ctl = self.ctl.data, self.ctl
        plan = _plan(ctl)
        attrs: dict[str, Any] = {
            "message": d["message"],
            "enabled": d["enabled"],
            "dry_run": d["dry_run"],
            "charging": d["modified"] and d["status"] == STATUS_CHARGING,
            "charge_window": ctl.window_label,
            "charge_power_kw": d["settings"].get("charge_power_kw"),
            "time_zone_check": d.get("tz_check"),
            "battery_soc": ctl.preview.get("soc"),
            "target_soc": d["target_soc"] if d["modified"] else (plan.target_soc if plan else None),
            "last_error": d["last_error"],
            "last_run": d["last_run"],
            "plan_summary": (d.get("applied") or {}).get("summary"),
        }
        if plan:
            attrs.update(
                stored_kwh=round(plan.stored_kwh, 2),
                solar_surplus_kwh=round(plan.solar_surplus_kwh, 2),
                preview_reason=plan.reason,
                preview_charge=plan.charge,
            )
        if d["dry_run"] and d.get("last_payload"):
            attrs["last_payload"] = d["last_payload"]
        return attrs


class TargetSocSensor(PlannerEntity, SensorEntity):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-charging-high"

    def __init__(self, ctl: PlannerController) -> None:
        super().__init__(ctl, "sensor", "target_soc")

    @property
    def native_value(self) -> int | None:
        d = self.ctl.data
        if d["modified"] and d["target_soc"] is not None:
            return d["target_soc"]
        plan = _plan(self.ctl)
        return plan.target_soc if plan else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"source": "applied" if self.ctl.data["modified"] else "preview"}


class GridNeededSensor(PlannerEntity, SensorEntity):
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:transmission-tower-import"

    def __init__(self, ctl: PlannerController) -> None:
        super().__init__(ctl, "sensor", "grid_needed")

    @property
    def native_value(self) -> float | None:
        plan = _plan(self.ctl)
        return round(plan.grid_needed_kwh, 2) if plan else None


class ForecastSensor(PlannerEntity, SensorEntity):
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, ctl: PlannerController) -> None:
        super().__init__(ctl, "sensor", "forecast")

    @property
    def native_value(self) -> float | None:
        fc = self.ctl.preview.get("forecast")
        return round(fc["kwh"], 2) if fc else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        fc = self.ctl.preview.get("forecast") or {}
        return {
            "source": fc.get("source"),
            "sources_used": fc.get("used"),
            "sources_unavailable": fc.get("skipped"),
            "fallback": fc.get("fallback"),
            "missing": fc.get("missing", False),
        }


class LastRunSensor(PlannerEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:history"

    def __init__(self, ctl: PlannerController) -> None:
        super().__init__(ctl, "sensor", "last_run")

    @property
    def native_value(self) -> datetime | None:
        raw = self.ctl.data.get("last_run")
        return dt_util.parse_datetime(raw) if raw else None
