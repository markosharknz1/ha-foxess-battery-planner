"""Planner settings as number entities (sliders on the card)."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SETTINGS
from .controller import PlannerController
from .entity import PlannerEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    ctl: PlannerController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SettingNumber(ctl, key) for key in SETTINGS])


class SettingNumber(PlannerEntity, NumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, ctl: PlannerController, key: str) -> None:
        super().__init__(ctl, "number", key)
        self._key = key
        _, low, high, step, unit, icon = SETTINGS[key]
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon

    @property
    def native_value(self) -> float:
        return self.ctl.data["settings"][self._key]

    async def async_set_native_value(self, value: float) -> None:
        await self.ctl.async_set_setting(self._key, value)
