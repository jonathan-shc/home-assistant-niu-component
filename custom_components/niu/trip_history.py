"""Persistent NIU trip history with GPS coordinates."""

from __future__ import annotations

import json
import logging
import math
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
        self.selected_trip_id: str | None = None
        self.initialized = False
        self.first_sync_completed = False

    async def async_load(self):
        """Load previously saved trips."""
        saved = await self.store.async_load()

        if isinstance(saved, dict):
            self.trips = saved.get("trips", {})
            self.attempts = saved.get("attempts", {})
            self.retry_after = saved.get("retry_after", {})
            self.selected_trip_id = saved.get("selected_trip_id")
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
                "selected_trip_id": self.selected_trip_id,
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
                key=lambda key: self.trips[key].get("downloaded_at", ""),
            )
            for old_id in oldest[:-MAX_SAVED_TRIPS]:
                del self.trips[old_id]

        await self.async_save()

        _LOGGER.info(
            "Saved NIU trip %s with %s GPS points",
            track_id,
            len(result["detail"]["trackItems"]),
        )

    def saved_trips(self) -> list[dict[str, Any]]:
        """Return downloaded trips, newest ride first (exclude sync markers)."""
        trips = [
            trip
            for trip in self.trips.values()
            if isinstance(trip, dict)
            and isinstance(trip.get("track_id"), str)
            and 0 < len(trip["track_id"]) <= 255
            and isinstance(trip.get("detail"), dict)
            and isinstance(trip["detail"].get("trackItems"), list)
            and trip["detail"]["trackItems"]
            and not trip.get("skipped_initial_sync")
        ]
        return sorted(trips, key=_trip_sort_key, reverse=True)

    @property
    def selected_trip(self) -> dict[str, Any] | None:
        """Keep a selection until it is evicted; default to the latest ride."""
        trips = self.saved_trips()
        return next(
            (trip for trip in trips if trip["track_id"] == self.selected_trip_id),
            trips[0] if trips else None,
        )

    async def async_select_trip(self, track_id: str) -> None:
        """Persist a local selection without fetching NIU data."""
        if not any(trip["track_id"] == track_id for trip in self.saved_trips()):
            raise ValueError("Unknown NIU trip")
        previous = self.selected_trip_id
        self.selected_trip_id = track_id
        try:
            await self.async_save()
        except Exception:
            self.selected_trip_id = previous
            raise


def _trip_sort_key(trip: dict[str, Any]) -> tuple[float, str]:
    """Order by ride time, never by the time an older ride was downloaded."""
    summary = trip.get("summary") or {}
    start = summary.get("startTime") if isinstance(summary, dict) else None
    try:
        timestamp = float(start)
        if math.isfinite(timestamp):
            return timestamp, trip["track_id"]
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        timestamp = (
            datetime.strptime(trip.get("date", ""), "%Y%m%d")
            .replace(tzinfo=timezone.utc)
            .timestamp()
            * 1000
        )
    except (TypeError, ValueError, OverflowError):
        timestamp = 0
    return timestamp, trip["track_id"]


# Keep each route/history attribute payload below Recorder's 16 KiB limit,
# leaving room for HA's common attributes. Full GPS data remains in Store.
MAX_ROUTE_POINTS = 200
MAX_HISTORY_TRIPS = 50
MAX_ATTRIBUTE_BYTES = 12 * 1024


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8"))


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def trip_metadata(trip: dict[str, Any]) -> dict[str, Any]:
    """Expose only bounded metadata; never arbitrary API response fields."""
    result = {"track_id": trip["track_id"]}
    date = trip.get("date")
    if isinstance(date, str):
        result["date"] = date[:32]
    summary = trip.get("summary")
    detail = trip.get("detail")
    summary = summary if isinstance(summary, dict) else {}
    detail = detail if isinstance(detail, dict) else {}
    for output, fields in (
        ("start_time", ("startTime",)),
        ("end_time", ("endTime",)),
        ("distance_km", ("distance", "mileage")),
        ("duration_seconds", ("ridingtime", "duration")),
    ):
        for field in fields:
            number = _finite_number(summary.get(field, detail.get(field)))
            if number is not None:
                result[output] = number / 1000 if field == "distance" else number
                break
    points = detail.get("trackItems", [])
    result["gps_points_count"] = len(points) if isinstance(points, list) else 0
    return result


def trip_route_attributes(trip: dict[str, Any] | None) -> dict[str, Any]:
    """Expose coordinates in API order, with explicit display limits."""
    if trip is None:
        return {
            "route": [],
            "gps_points_count": 0,
            "route_points_count": 0,
            "route_truncated": False,
        }
    attributes = trip_metadata(trip)
    points = []
    for item in trip["detail"]["trackItems"]:
        if not isinstance(item, dict):
            continue
        lat = _finite_number(item.get("lat", item.get("latitude")))
        lng = _finite_number(item.get("lng", item.get("longitude")))
        if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            continue
        points.append({"latitude": lat, "longitude": lng})
    count = min(len(points), MAX_ROUTE_POINTS)
    while True:
        # Even sampling retains both endpoints and the entire ride's extent.
        route = (
            [points[round(i * (len(points) - 1) / (count - 1))] for i in range(count)]
            if count > 1
            else points[:count]
        )
        attributes.update(
            route=route,
            route_points_count=len(route),
            valid_gps_points_count=len(points),
            route_truncated=len(route) < len(points),
        )
        if _json_size(attributes) <= MAX_ATTRIBUTE_BYTES or count == 0:
            return attributes
        count -= 1


def trip_history_attributes(history: NiuTripHistory) -> dict[str, Any]:
    """Return a bounded recent-trip index with no GPS coordinates."""
    saved = history.saved_trips()
    attributes = {"trips": [], "total_trips": len(saved), "trips_truncated": False}
    for trip in saved[:MAX_HISTORY_TRIPS]:
        metadata = trip_metadata(trip)
        attributes["trips"].append(metadata)
        if _json_size(attributes) > MAX_ATTRIBUTE_BYTES:
            attributes["trips"].pop()
            break
    attributes["trips_truncated"] = len(attributes["trips"]) < len(saved)
    return attributes
