"""Coordinate NIU API polling for all entities in a config entry."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from time import monotonic, time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import NiuApi, NiuApiError, NiuRateLimitError
from .const import (
    DOMAIN,
    SENSOR_TYPE_BAT,
    SENSOR_TYPE_BAT2,
    SENSOR_TYPE_DIST,
    SENSOR_TYPE_MOTO,
    SENSOR_TYPE_OVERALL,
    SENSOR_TYPE_POS,
    SENSOR_TYPE_TRACK,
    SENSOR_TYPES,
)
from .trip_history import NiuTripHistory

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=150)
HISTORY_INTERVAL = timedelta(seconds=30)
DEFAULT_RATE_LIMIT_BACKOFF = 300
MIN_RATE_LIMIT_BACKOFF = 30
MAX_RATE_LIMIT_BACKOFF = 3600


def required_sensor_groups(sensors_selected: list[str]) -> set[str]:
    """Return the API data groups needed by the selected entities."""
    return {
        SENSOR_TYPES[sensor][3] for sensor in sensors_selected if sensor in SENSOR_TYPES
    }


class NiuDataUpdateCoordinator(DataUpdateCoordinator[NiuApi]):
    """Fetch each required NIU endpoint once per polling cycle."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        api: NiuApi,
        sensor_groups: set[str],
        trip_history: NiuTripHistory | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=POLL_INTERVAL,
        )
        self.trip_history = trip_history
        self.api = api
        self.sensor_groups = frozenset(sensor_groups)
        self._initialized = False
        self._skip_trip_history = False
        self._next_api_poll = 0.0
        self._api_update_lock = asyncio.Lock()
        self._api_retry_at = 0.0
        self.status_checked_at = 0.0

    def _rate_limit_delay(self, err: NiuRateLimitError) -> float:
        """Share provider backoff between scheduled and explicit requests."""
        delay = max(
            MIN_RATE_LIMIT_BACKOFF,
            min(err.retry_after or DEFAULT_RATE_LIMIT_BACKOFF, MAX_RATE_LIMIT_BACKOFF),
        )
        self._api_retry_at = monotonic() + delay
        return delay

    async def async_refresh_status(self) -> None:
        """Read motor status now, bypassing only the normal polling interval."""
        async with self._api_update_lock:
            if monotonic() < self._api_retry_at:
                raise HomeAssistantError("NIU status refresh blocked by API backoff")
            try:
                await self.hass.async_add_executor_job(self.api.updateMoto)
            except NiuRateLimitError as err:
                self._rate_limit_delay(err)
                raise HomeAssistantError("NIU status refresh rate limited") from err
            except NiuApiError as err:
                raise HomeAssistantError(f"NIU status refresh failed: {err}") from err
            # A successful request is distinct from the provider measurement time.
            self.status_checked_at = time()
            self.async_set_updated_data(self.api)

    def _sync_update_data(self) -> NiuApi:
        """Initialize once, then update each required endpoint exactly once."""
        if not self._initialized:
            _LOGGER.debug("Initializing shared NIU API client")
            self.api.initialize()
            self._initialized = True

        now = monotonic()
        if self.trip_history is not None and now < self._next_api_poll:
            return self.api

        _LOGGER.debug(
            "Updating NIU data groups: %s", ", ".join(sorted(self.sensor_groups))
        )
        if self.sensor_groups & {SENSOR_TYPE_BAT, SENSOR_TYPE_BAT2}:
            self.api.updateBat()
        if self.sensor_groups & {
            SENSOR_TYPE_MOTO,
            SENSOR_TYPE_POS,
            SENSOR_TYPE_DIST,
        }:
            self.api.updateMoto()
        if SENSOR_TYPE_OVERALL in self.sensor_groups:
            self.api.updateMotoInfo()
        self._skip_trip_history = False
        if SENSOR_TYPE_TRACK in self.sensor_groups:
            self.api.updateTrackInfo()
        elif self.trip_history is not None:
            # An added history endpoint must not break battery-only entries.
            try:
                self.api.updateTrackInfo()
            except NiuApiError as err:
                self._skip_trip_history = True
                _LOGGER.warning("NIU trip list update failed: %s", err)
        self._next_api_poll = now + POLL_INTERVAL.total_seconds()
        return self.api

    async def _async_update_data(self) -> NiuApi:
        """Fetch NIU data without blocking Home Assistant's event loop."""
        try:
            async with self._api_update_lock:
                remaining = self._api_retry_at - monotonic()
                if remaining > 0:
                    raise UpdateFailed("NIU API backoff active", retry_after=remaining)
                data = await self.hass.async_add_executor_job(self._sync_update_data)
        except NiuRateLimitError as err:
            retry_after = self._rate_limit_delay(err)
            raise UpdateFailed(
                "NIU API rate limit exceeded", retry_after=retry_after
            ) from err
        except NiuApiError as err:
            raise UpdateFailed(f"NIU API update failed: {err}") from err
        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed("Unexpected error while updating NIU data") from err

        if self.trip_history is not None and not self._skip_trip_history:
            try:
                await self.trip_history.async_process(data)
            except Exception:
                # GPS/history failures must not take existing battery entities offline.
                _LOGGER.exception("NIU trip history update failed")
        if self.trip_history is not None:
            self.update_interval = (
                HISTORY_INTERVAL if self.trip_history.has_work else POLL_INTERVAL
            )
        return data
