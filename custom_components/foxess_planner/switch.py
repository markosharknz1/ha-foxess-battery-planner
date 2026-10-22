"""Switches: automatic planning on/off, dry-run on/off."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .controller import PlannerController
from .entity import PlannerEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    ctl: PlannerController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            FlagSwitch(ctl, "enabled", "mdi:calendar-check"),
            FlagSwitch(ctl, "dry_run", "mdi:test-tube"),
            FlagSwitch(ctl, "ignore_tz", "mdi:clock-alert-outline"),
        ]
    )


class FlagSwitch(PlannerEntity, SwitchEntity):
    """``enabled``: nightly plan runs. ``dry_run``: plan but never write to FoxESS."""

    def __init__(self, ctl: PlannerController, key: str, icon: str) -> None:
        super().__init__(ctl, "switch", key)
        self._key = key
        self._attr_icon = icon
        if key in ("dry_run", "ignore_tz"):
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool:
        return bool(self.ctl.data[self._key])

    async def async_turn_on(self, **kwargs) -> None:
        await self.ctl.async_set_flag(self._key, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.ctl.async_set_flag(self._key, False)
