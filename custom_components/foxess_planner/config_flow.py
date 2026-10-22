"""Config flow: credentials -> data sources -> charge schedule.

Credentials and identity live in entry.data; everything else in
entry.options so the OptionsFlow can rewire sources and timing later
without recreating the entry (settings and history are kept).
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FoxessApi, FoxessApiError, FoxessConnectionError
from .const import (
    CONF_API_KEY,
    CONF_ARRAY_KWP,
    CONF_CHARGE_END,
    CONF_CHARGE_START,
    CONF_CLEAR_SKY_YIELD,
    CONF_DEVICE_SN,
    CONF_FORECAST_ENTITIES,
    CONF_MIN_SOC_ON_GRID,
    CONF_NAME,
    CONF_NOTIFY,
    CONF_PLAN_LEAD,
    CONF_SLUG,
    CONF_SOC_ENTITY,
    CONF_WEATHER_ENTITY,
    DEFAULTS,
    DOMAIN,
    WINDOW_KEYS,
)
from .util import slugify_name, time_to_minutes, valid_slug


def _sources_schema(current: dict[str, Any]) -> vol.Schema:
    def sug(key):
        value = current.get(key, DEFAULTS.get(key))
        return {"suggested_value": value} if value not in (None, "") else {}

    return vol.Schema(
        {
            vol.Required(CONF_SOC_ENTITY, description=sug(CONF_SOC_ENTITY)): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(
                CONF_FORECAST_ENTITIES, description=sug(CONF_FORECAST_ENTITIES)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", multiple=True)
            ),
            vol.Optional(
                CONF_WEATHER_ENTITY, description=sug(CONF_WEATHER_ENTITY)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
            vol.Optional(CONF_ARRAY_KWP, description=sug(CONF_ARRAY_KWP)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=100, step=0.1, mode="box", unit_of_measurement="kWp")
            ),
            vol.Optional(
                CONF_CLEAR_SKY_YIELD, description=sug(CONF_CLEAR_SKY_YIELD)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=8, step=0.1, mode="box", unit_of_measurement="kWh/kWp/day")
            ),
        }
    )


def _schedule_schema(current: dict[str, Any]) -> vol.Schema:
    def sug(key):
        value = current.get(key, DEFAULTS.get(key))
        return {"suggested_value": value} if value not in (None, "") else {}

    fields: dict[Any, Any] = {}
    for index, (start_key, end_key) in enumerate(WINDOW_KEYS):
        marker = vol.Required if index == 0 else vol.Optional
        fields[marker(start_key, description=sug(start_key))] = selector.TimeSelector()
        fields[marker(end_key, description=sug(end_key))] = selector.TimeSelector()
    fields[vol.Required(CONF_PLAN_LEAD, description=sug(CONF_PLAN_LEAD))] = selector.NumberSelector(
        selector.NumberSelectorConfig(min=1, max=60, step=1, mode="box", unit_of_measurement="min")
    )
    fields[vol.Required(CONF_MIN_SOC_ON_GRID, description=sug(CONF_MIN_SOC_ON_GRID))] = selector.NumberSelector(
        selector.NumberSelectorConfig(min=10, max=100, step=1, mode="box", unit_of_measurement="%")
    )
    fields[vol.Required(CONF_NOTIFY, description=sug(CONF_NOTIFY))] = selector.BooleanSelector()
    return vol.Schema(fields)


def _check_sources(user_input: dict[str, Any]) -> dict[str, str]:
    has_forecast = bool(user_input.get(CONF_FORECAST_ENTITIES))
    has_weather = bool(user_input.get(CONF_WEATHER_ENTITY)) and float(
        user_input.get(CONF_ARRAY_KWP) or 0
    ) > 0
    if not (has_forecast or has_weather):
        return {"base": "need_forecast"}
    return {}


def _check_schedule(user_input: dict[str, Any]) -> dict[str, str]:
    spans = []
    for start_key, end_key in WINDOW_KEYS:
        start, end = user_input.get(start_key), user_input.get(end_key)
        if bool(start) != bool(end):
            return {"base": "window_incomplete"}
        if start:
            first, last = time_to_minutes(start), time_to_minutes(end)
            if last <= first:
                return {"base": "window_order"}
            spans.append((first, last))
    spans.sort()
    if any(spans[i][1] > spans[i + 1][0] for i in range(len(spans) - 1)):
        return {"base": "window_overlap"}
    return {}


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    out = dict(user_input)
    for key in (CONF_PLAN_LEAD, CONF_MIN_SOC_ON_GRID):
        if key in out:
            out[key] = int(out[key])
    return out


class FoxessPlannerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            slug = (user_input.get(CONF_SLUG) or slugify_name(name)).strip()
            sn = user_input[CONF_DEVICE_SN].strip()
            if not valid_slug(slug):
                errors[CONF_SLUG] = "invalid_slug"
            else:
                await self.async_set_unique_id(sn)
                self._abort_if_unique_id_configured()
                api = FoxessApi(
                    async_get_clientsession(self.hass), user_input[CONF_API_KEY].strip(), sn
                )
                try:
                    await api.get_flag()
                except FoxessConnectionError:
                    errors["base"] = "cannot_connect"
                except FoxessApiError:
                    errors["base"] = "invalid_auth"
                if not errors:
                    self._data = {
                        CONF_NAME: name,
                        CONF_SLUG: slug,
                        CONF_API_KEY: user_input[CONF_API_KEY].strip(),
                        CONF_DEVICE_SN: sn,
                    }
                    return await self.async_step_sources()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default="FoxESS Battery"): str,
                    vol.Optional(CONF_SLUG): str,
                    vol.Required(CONF_API_KEY): selector.TextSelector(
                        selector.TextSelectorConfig(type="password")
                    ),
                    vol.Required(CONF_DEVICE_SN): str,
                }
            ),
            errors=errors,
        )

    async def async_step_sources(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _check_sources(user_input)
            if not errors:
                self._options.update(_clean(user_input))
                return await self.async_step_schedule()
        return self.async_show_form(
            step_id="sources", data_schema=_sources_schema(user_input or {}), errors=errors
        )

    async def async_step_schedule(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _check_schedule(user_input)
            if not errors:
                self._options.update(_clean(user_input))
                return self.async_create_entry(
                    title=self._data[CONF_NAME], data=self._data, options=self._options
                )
        return self.async_show_form(
            step_id="schedule", data_schema=_schedule_schema(user_input or {}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return FoxessPlannerOptionsFlow()


class FoxessPlannerOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options, **self._options}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _check_sources(user_input)
            if not errors:
                self._options.update(_clean(user_input))
                # Cleared optional fields must really clear.
                for key in (CONF_WEATHER_ENTITY, CONF_FORECAST_ENTITIES):
                    self._options.setdefault(key, None if key == CONF_WEATHER_ENTITY else [])
                return await self.async_step_schedule()
        return self.async_show_form(
            step_id="init", data_schema=_sources_schema(user_input or self._current()), errors=errors
        )

    async def async_step_schedule(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _check_schedule(user_input)
            if not errors:
                self._options.update(_clean(user_input))
                for start_key, end_key in WINDOW_KEYS[1:]:
                    self._options.setdefault(start_key, None)
                    self._options.setdefault(end_key, None)
                return self.async_create_entry(title="", data={**self.config_entry.options, **self._options})
        return self.async_show_form(
            step_id="schedule", data_schema=_schedule_schema(user_input or self._current()), errors=errors
        )
