"""FoxESS Battery Planner — solar-aware overnight grid charging.

One config entry per inverter. The controller is built once, stored in
hass.data[DOMAIN][entry_id], then platforms are forwarded — the same
lifecycle shape as the maintainer's feedandwater integration.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .controller import PlannerController
from .frontend import async_register_card

PLATFORMS = [
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ctl = PlannerController(hass, entry)
    await ctl.async_setup()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = ctl

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_register_card(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        ctl: PlannerController = hass.data[DOMAIN].pop(entry.entry_id)
        await ctl.async_unload()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
