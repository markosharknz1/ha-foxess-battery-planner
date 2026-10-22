"""FoxESS Cloud Open API client (scheduler endpoints only).

Signing: MD5 of ``path + "\\r\\n" + token + "\\r\\n" + timestamp`` where the
separator is the four literal characters backslash-r-backslash-n, NOT a
carriage return and line feed. This matches FoxESS's own sample code
(a raw f-string) and the TonyM1958/FoxESS-Cloud reference library. A
templating engine that unescapes ``\\r\\n`` produces a different, rejected
signature.

The session is injected (Home Assistant's shared aiohttp session), so this
module has no import-time dependencies.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.foxesscloud.com"
PATH_GET = "/op/v3/device/scheduler/get"
PATH_SET = "/op/v3/device/scheduler/enable"
PATH_DEVICE_LIST = "/op/v0/device/list"
PATH_PLANT_LIST = "/op/v0/plant/list"
PATH_GET_FLAG = "/op/v1/device/scheduler/get/flag"
PATH_SET_FLAG = "/op/v1/device/scheduler/set/flag"

_SEPARATOR = "\\r\\n"  # literal backslash-r-backslash-n — see module docstring
_ERRNO_HINTS = {
    40400: "FoxESS daily API call limit reached (1440 per inverter per day)",
}


class FoxessApiError(Exception):
    def __init__(self, message: str, errno: int | None = None) -> None:
        super().__init__(message)
        self.errno = errno


class FoxessConnectionError(FoxessApiError):
    """Network failure or HTTP error after all retries."""


def sign(path: str, token: str, timestamp: str) -> str:
    return hashlib.md5(
        f"{path}{_SEPARATOR}{token}{_SEPARATOR}{timestamp}".encode("utf-8")
    ).hexdigest()


class FoxessApi:
    def __init__(
        self,
        session,
        api_key: str,
        device_sn: str,
        *,
        time_zone: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = 20.0,
        tries: int = 3,
        min_interval: float = 1.0,
    ) -> None:
        self._session = session
        self._key = api_key
        self.device_sn = device_sn
        self._tz = time_zone
        self._base = base_url
        self._timeout = timeout
        self._tries = tries
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    def _headers(self, path: str) -> dict[str, str]:
        timestamp = str(round(time.time() * 1000))
        headers = {
            "token": self._key,
            "timestamp": timestamp,
            "signature": sign(path, self._key, timestamp),
            "lang": "en",
            "Content-Type": "application/json",
        }
        if self._tz:
            headers["timezone"] = self._tz
        return headers

    async def _post(self, path: str, body: dict) -> dict:
        async with self._lock:  # one call at a time, spaced out
            last_error: Exception | None = None
            for attempt in range(self._tries):
                wait = self._min_interval - (time.monotonic() - self._last_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    self._last_call = time.monotonic()
                    async with self._session.post(
                        self._base + path,
                        json=body,
                        headers=self._headers(path),
                        timeout=self._timeout,
                    ) as resp:
                        if resp.status >= 500:
                            raise ConnectionError(f"HTTP {resp.status}")  # retried
                        if resp.status != 200:
                            raise FoxessApiError(f"HTTP {resp.status}")
                        data = await resp.json(content_type=None)
                except FoxessApiError:
                    raise
                except Exception as err:  # noqa: BLE001 - aiohttp/timeout/json
                    last_error = err
                    _LOGGER.debug("FoxESS %s attempt %s failed: %s", path, attempt + 1, err)
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                errno = data.get("errno", 0)
                if errno != 0:
                    msg = _ERRNO_HINTS.get(errno) or data.get("msg") or "request failed"
                    raise FoxessApiError(f"errno {errno}: {msg}", errno)
                return data
            raise FoxessConnectionError(
                f"No response from FoxESS Cloud after {self._tries} tries: {last_error}"
            )

    async def get_schedule(self) -> dict:
        """Returns the v3 ``result``: enable, groups, maxGroupCount, properties."""
        data = await self._post(PATH_GET, {"deviceSN": self.device_sn})
        return data.get("result") or {}

    async def get_site_timezone(self) -> str | None:
        """IANA time zone of the plant this inverter belongs to (None if unknown).

        Device list -> stationID -> plant list -> ianaTimezone. If the device
        row carries no stationID but the account has a single plant, that
        plant is used.
        """
        page = {"currentPage": 1, "pageSize": 100}
        devices = (await self._post(PATH_DEVICE_LIST, page)).get("result") or {}
        station = None
        for row in devices.get("data") or []:
            if str(row.get("deviceSN", "")).upper() == self.device_sn.upper():
                station = row.get("stationID")
                break
        plants = ((await self._post(PATH_PLANT_LIST, page)).get("result") or {}).get("data") or []
        if station is not None:
            plants = [p for p in plants if p.get("stationID") == station] or plants
        if len(plants) == 1:
            return plants[0].get("ianaTimezone") or None
        return None

    async def get_flag(self) -> dict:
        data = await self._post(PATH_GET_FLAG, {"deviceSN": self.device_sn})
        return data.get("result") or {}

    async def set_flag(self, enable: bool) -> None:
        await self._post(
            PATH_SET_FLAG, {"deviceSN": self.device_sn, "enable": 1 if enable else 0}
        )

    async def set_schedule(self, groups: list[dict], enable: bool = True) -> None:
        await self._post(
            PATH_SET,
            {"deviceSN": self.device_sn, "isDefault": False, "groups": groups},
        )
        await self.set_flag(enable)
