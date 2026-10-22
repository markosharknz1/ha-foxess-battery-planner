"""Constants for the FoxESS Battery Planner integration."""
from __future__ import annotations

DOMAIN = "foxess_planner"

# --- config entry data (credentials, identity) ---
CONF_NAME = "name"
CONF_SLUG = "slug"
CONF_API_KEY = "api_key"
CONF_DEVICE_SN = "device_sn"

# --- options (rewirable later) ---
CONF_SOC_ENTITY = "soc_entity"
CONF_FORECAST_ENTITIES = "forecast_entities"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_ARRAY_KWP = "array_kwp"
CONF_CLEAR_SKY_YIELD = "clear_sky_yield"
CONF_CHARGE_START = "charge_start"
CONF_CHARGE_END = "charge_end"
CONF_CHARGE_START_2 = "charge_start_2"
CONF_CHARGE_END_2 = "charge_end_2"
CONF_CHARGE_START_3 = "charge_start_3"
CONF_CHARGE_END_3 = "charge_end_3"
# (start, end) option keys for each window; window 1 is required, 2 and 3 optional
WINDOW_KEYS = (
    (CONF_CHARGE_START, CONF_CHARGE_END),
    (CONF_CHARGE_START_2, CONF_CHARGE_END_2),
    (CONF_CHARGE_START_3, CONF_CHARGE_END_3),
)
CONF_PLAN_LEAD = "plan_lead_minutes"
CONF_MIN_SOC_ON_GRID = "min_soc_on_grid"
CONF_NOTIFY = "notify"

DEFAULTS = {
    CONF_ARRAY_KWP: 0.0,
    CONF_CLEAR_SKY_YIELD: 4.5,
    CONF_CHARGE_START: "02:00:00",
    CONF_CHARGE_END: "06:00:00",
    CONF_PLAN_LEAD: 5,
    CONF_MIN_SOC_ON_GRID: 10,
    CONF_NOTIFY: True,
}

# --- runtime settings (number entities), persisted in the integration store ---
# key: (default, min, max, step, unit, icon)
SETTINGS = {
    "battery_capacity": (10.0, 1.0, 100.0, 0.5, "kWh", "mdi:battery-high"),
    "daily_usage": (10.0, 0.0, 100.0, 0.5, "kWh", "mdi:home-lightning-bolt"),
    "pre_solar_load": (1.0, 0.0, 10.0, 0.5, "kWh", "mdi:weather-sunset-up"),
    "solar_trust": (80.0, 10.0, 120.0, 5.0, "%", "mdi:solar-power"),
    "buffer": (3.0, 0.0, 20.0, 1.0, "%", "mdi:battery-plus"),
    "min_target_soc": (20.0, 0.0, 100.0, 5.0, "%", "mdi:battery-arrow-down"),
    "max_target_soc": (100.0, 10.0, 100.0, 5.0, "%", "mdi:battery-arrow-up"),
    # Grid charge rate sent to the inverter. 0 = not sent (inverter default,
    # usually its maximum), so the default is a deliberate, modest figure.
    "charge_power_kw": (3.0, 0.0, 30.0, 0.1, "kW", "mdi:flash"),
}

# --- status values ---
STATUS_IDLE = "idle"
STATUS_DISABLED = "disabled"
STATUS_NOT_NEEDED = "not_needed"
STATUS_CHARGING = "charging"
STATUS_COMPLETE = "complete"
STATUS_DRY_RUN = "dry_run"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"
STATUSES = [
    STATUS_IDLE,
    STATUS_DISABLED,
    STATUS_NOT_NEEDED,
    STATUS_CHARGING,
    STATUS_COMPLETE,
    STATUS_DRY_RUN,
    STATUS_SKIPPED,
    STATUS_ERROR,
]

SIGNAL_UPDATE = f"{DOMAIN}_update_{{}}"  # .format(entry_id)

STORE_VERSION = 1
MAX_PLAN_RETRIES = 3
MAX_RESTORE_RETRIES = 5
PLAN_TIME_TOLERANCE_MIN = 30  # automatic plans run only this close to schedule
TZ_RECHECK_HOURS = 6
RETRY_DELAY_S = 120
