"""Buttons: plan now, cancel and restore the schedule."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .controller import PlannerController
from .entity import PlannerEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    ctl: PlannerController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PlanNowButton(ctl), CancelButton(ctl)])


class PlanNowButton(PlannerEntity, ButtonEntity):
    """Run tonight's plan immediately (honours the dry-run switch)."""

    _attr_icon = "mdi:play-circle"

    def __init__(self, ctl: PlannerController) -> None:
        super().__init__(ctl, "button", "plan_now")

    async def async_press(self) -> None:
        await self.ctl.async_run_plan(manual=True)


class CancelButton(PlannerEntity, ButtonEntity):
    """Stop charging now and write the user's own schedule back."""

    _attr_icon = "mdi:stop-circle"

    def __init__(self, ctl: PlannerController) -> None:
        super().__init__(ctl, "button", "cancel")

    async def async_press(self) -> None:
        await self.ctl.async_restore("Cancelled by user")
