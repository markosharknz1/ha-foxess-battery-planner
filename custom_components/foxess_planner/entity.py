"""Base entity for the FoxESS Battery Planner integration."""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_UPDATE
from .controller import PlannerController


class PlannerEntity(Entity):
    """One HA device per inverter (config entry) groups all entities.

    entity_id is pre-set to ``<domain>.<slug>_<suffix>`` so dashboard
    snippets and the bundled card can rely on the names — the same
    convention as the maintainer's feedandwater integration.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, ctl: PlannerController, domain: str, suffix: str) -> None:
        self.ctl = ctl
        self._attr_unique_id = f"{ctl.entry.entry_id}_{suffix}"
        self.entity_id = f"{domain}.{ctl.slug}_{suffix}"
        self._attr_translation_key = suffix
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ctl.entry.entry_id)},
            name=ctl.entry.title,
            manufacturer="FoxESS Battery Planner",
            model="Overnight charge planner",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_UPDATE.format(self.ctl.entry.entry_id), self._refresh
            )
        )

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()
