"""Persistent NIU trip history with GPS coordinates."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from homeassistant.helpers.storage import Store

from .api import NiuApi, NiuApiError

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
MAX_SAVED_TRIPS = 500
MAX_RETRY_DELAY = 3600


class NiuTripHistory:
    """Manage downloaded NIU trip GPS histories."""

    def __init__(self, hass, entry_id: str):
        self.hass = hass
        self.store = Store(
            hass,
            STORAGE_VERSION,
            f"niu_trip_history_{entry_id}",
        )
        self.trips: dict[str, dict[str, Any]] = {}
        self.attempts: dict[str, int] = {}
        self.retry_after: dict[str, float] = {}
        self.initialized = False
        self.first_sync_completed = False

    async def async_load(self):
        """Load previously saved trips."""
        saved = await self.store.async_load()

        if isinstance(saved, dict):
            self.trips = saved.get("trips", {})
            self.attempts = saved.get("attempts", {})
            self.retry_after = saved.get("retry_after", {})
            self.first_sync_completed = saved.get("first_sync_completed", False)

        self.initialized = True
        _LOGGER.info(
            "Loaded %s NIU trips from local storage",
            len(self.trips),
        )

    async def async_save(self):
        """Persist trip history."""
        await self.store.async_save(
            {
                "trips": self.trips,
                "attempts": self.attempts,
                "retry_after": self.retry_after,
                "first_sync_completed": self.first_sync_completed,
            }
        )

    def get_pending_trip(self, track_list: dict) -> dict | None:
        """Find the newest trip not yet downloaded."""
        if not isinstance(track_list, dict):
            return None

        trips = track_list.get("data", [])

        if not isinstance(trips, list):
            return None

        for trip in reversed(trips):
            if not isinstance(trip, dict):
                continue

            track_id = str(trip.get("trackId", ""))

            if not track_id:
                continue

            if track_id in self.trips:
                continue

            if time.time() < self.retry_after.get(track_id, 0):
                continue

            return trip

        return None

    def download_trip(self, api: NiuApi, trip: dict) -> dict | None:
        """Download GPS details for one completed trip."""
        track_id = str(trip["trackId"])

        date = trip.get("date")

        if not date:
            timestamp = trip.get("startTime")

            if timestamp:
                date = datetime.fromtimestamp(
                    int(timestamp) / 1000,
                    tz=timezone.utc,
                ).strftime("%Y%m%d")

        if not date:
            _LOGGER.warning(
                "NIU trip %s has no usable date",
                track_id,
            )
            return None

        try:
            result = api.get_track_detail(track_id, str(date))
        except NiuApiError as err:
            _LOGGER.warning(
                "Could not download NIU trip %s: %s",
                track_id,
                err,
            )
            return None

        data = result.get("data")

        if not isinstance(data, dict):
            return None

        points = data.get("trackItems")

        if not isinstance(points, list) or not points:
            _LOGGER.debug(
                "NIU trip %s has no GPS points yet",
                track_id,
            )
            return None

        return {
            "track_id": track_id,
            "date": str(date),
            "summary": trip,
            "detail": data,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        }

    async def async_process(self, api: NiuApi):
        """Download at most one missing trip per coordinator update."""
        if not self.initialized:
            return

        if not self.first_sync_completed:
            track_list = api.dataTrackInfo or {}
            items = track_list.get("data", [])

            if not isinstance(items, list) or not items:
                return

            # NIU returns newest trips first.
            newest = items[0]

            for old_trip in items[1:]:
                if isinstance(old_trip, dict) and old_trip.get("trackId"):
                    old_id = str(old_trip["trackId"])
                    self.attempts.pop(old_id, None)
                    self.retry_after.pop(old_id, None)
                    self.trips[old_id] = {
                        "track_id": old_id,
                        "skipped_initial_sync": True,
                    }

            self.first_sync_completed = True
            await self.async_save()

        trip = self.get_pending_trip(api.dataTrackInfo)

        if trip is None:
            return

        track_id = str(trip["trackId"])

        result = await self.hass.async_add_executor_job(
            self.download_trip,
            api,
            trip,
        )

        if result is None:
            attempts = self.attempts.get(track_id, 0) + 1
            self.attempts[track_id] = attempts

            delay = min(
                300 * (2 ** min(attempts - 1, 10)),
                MAX_RETRY_DELAY,
            )
            self.retry_after[track_id] = time.time() + delay

            await self.async_save()
            return

        self.trips[track_id] = result
        self.attempts.pop(track_id, None)
        self.retry_after.pop(track_id, None)

        if len(self.trips) > MAX_SAVED_TRIPS:
            oldest = sorted(
                self.trips,
                key=lambda key: self.trips[key].get(
                    "downloaded_at", ""
                ),
            )
            for old_id in oldest[:-MAX_SAVED_TRIPS]:
                del self.trips[old_id]

        await self.async_save()

        _LOGGER.info(
            "Saved NIU trip %s with %s GPS points",
            track_id,
            len(result["detail"]["trackItems"]),
        )
