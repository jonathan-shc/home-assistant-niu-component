"""Persistent NIU trip history with GPS coordinates."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any

from homeassistant.helpers.storage import Store

from .api import NiuApi, NiuApiError, NiuRateLimitError, NiuResponseError

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
MAX_RETRY_DELAY = 3600
MAX_GPS_ATTEMPTS = 3
HISTORY_SCAN_INTERVAL = 86400


class NiuTripHistory:
    """Manage downloaded NIU trip GPS histories."""

    def __init__(self, hass, entry_id: str):
        self.hass = hass
        self._save_lock = asyncio.Lock()
        self.store = Store(
            hass,
            STORAGE_VERSION,
            f"niu_trip_history_{entry_id}",
        )
        self.trips: dict[str, dict[str, Any]] = {}
        self.attempts: dict[str, int] = {}
        self.retry_after: dict[str, float] = {}
        self.summaries: dict[str, dict[str, Any]] = {}
        self.next_history_page = 2
        self.history_scan_complete = False
        self.last_page_ids: list[str] = []
        self.last_history_scan = 0.0
        self.request_retry_after = 0.0
        self.sync_error: str | None = None
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
            self.summaries = saved.get("summaries", {})
            self.next_history_page = saved.get("next_history_page", 2)
            self.history_scan_complete = saved.get("history_scan_complete", False)
            self.last_page_ids = saved.get("last_page_ids", [])
            self.last_history_scan = saved.get("last_history_scan", 0.0)
            self.request_retry_after = saved.get("request_retry_after", 0.0)
            self.selected_trip_id = saved.get("selected_trip_id")
            self.first_sync_completed = saved.get("first_sync_completed", False)

        self.initialized = True
        _LOGGER.info(
            "Loaded %s NIU trips from local storage",
            len(self.trips),
        )

    async def async_save(self):
        """Persist trip history."""
        async with self._save_lock:
            await self.store.async_save(
                {
                    "trips": self.trips.copy(),
                    "selected_trip_id": self.selected_trip_id,
                    "summaries": self.summaries.copy(),
                    "next_history_page": self.next_history_page,
                    "history_scan_complete": self.history_scan_complete,
                    "last_page_ids": self.last_page_ids,
                    "last_history_scan": self.last_history_scan,
                    "request_retry_after": self.request_retry_after,
                    "attempts": self.attempts.copy(),
                    "retry_after": self.retry_after.copy(),
                    "first_sync_completed": self.first_sync_completed,
                }
            )

    def get_pending_trip(self, track_list: dict) -> dict | None:
        """Find the newest missing route, including legacy initial-sync markers."""
        if not isinstance(track_list, dict):
            return None

        trips = track_list.get("data", [])

        if not isinstance(trips, list):
            return None

        for trip in trips:
            if not isinstance(trip, dict):
                continue

            track_id = str(trip.get("trackId", ""))

            if not track_id:
                continue

            saved = self.trips.get(track_id)
            if saved is not None and not saved.get("skipped_initial_sync"):
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
                try:
                    date = datetime.fromtimestamp(
                        int(timestamp) / 1000, tz=timezone.utc
                    ).strftime("%Y%m%d")
                except (OSError, TypeError, ValueError, OverflowError):
                    return None

        if not date:
            _LOGGER.warning(
                "NIU trip %s has no usable date",
                track_id,
            )
            return None

        try:
            result = api.get_track_detail(track_id, str(date))
        except NiuApiError:
            # Authentication, transport and rate-limit errors are not absent GPS.
            raise

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

    def merge_summaries(self, items: list[dict[str, Any]]) -> None:
        """Retain the full metadata index, deduplicated by trip ID."""
        for item in items:
            if not isinstance(item, dict):
                continue
            value = item.get("trackId")
            track_id = str(value) if value is not None else ""
            if track_id and len(track_id) <= 255:
                self.summaries[track_id] = item

    def pending_summaries(self) -> list[dict[str, Any]]:
        """Keep new rides ahead of historical downloads and retries."""
        pending = []
        for track_id, summary in self.summaries.items():
            saved = self.trips.get(track_id)
            if saved and not saved.get("skipped_initial_sync"):
                continue
            pending.append(summary)
        return sorted(
            pending,
            key=lambda item: _trip_sort_key(
                {
                    "track_id": str(item["trackId"]),
                    "summary": item,
                    "date": item.get("date", ""),
                }
            ),
            reverse=True,
        )

    @property
    def has_work(self) -> bool:
        """Use the faster history interval only while work remains."""
        return not self.history_scan_complete or bool(self.pending_summaries())

    async def async_process(self, api: NiuApi, *, discover: bool = True):
        """Read at most one older metadata page and one GPS route per step."""
        if not self.initialized or time.time() < self.request_retry_after:
            return
        if (
            self.history_scan_complete
            and time.time() - self.last_history_scan >= HISTORY_SCAN_INTERVAL
        ):
            self.history_scan_complete = False
            self.next_history_page = 2
            self.last_page_ids = []
        try:
            await self._async_process(api, discover=discover)
            self.sync_error = None
        except NiuApiError as err:
            # Pause all history requests together; retain the cursor and GPS queue.
            delay = err.retry_after if isinstance(err, NiuRateLimitError) else 300
            self.request_retry_after = time.time() + max(30, delay or 300)
            self.sync_error = str(err)[:255]
            await self.async_save()
            _LOGGER.warning("NIU history sync paused: %s", err)

    async def _async_process(self, api: NiuApi, *, discover: bool):
        items = (api.dataTrackInfo or {}).get("data")
        if not isinstance(items, list):
            return
        self.merge_summaries(items)
        self.first_sync_completed = True

        if discover and not self.history_scan_complete:
            if not items and not self.summaries:
                self.history_scan_complete = True
                self.last_history_scan = time.time()
            else:
                older = await self.hass.async_add_executor_job(
                    api.get_track_page, self.next_history_page
                )
                ids = [str(item.get("trackId", "")) for item in older]
                if older and (
                    set(ids) == set(self.last_page_ids)
                    or set(ids) == {str(item.get("trackId", "")) for item in items}
                ):
                    raise NiuResponseError("NIU history pagination did not advance")
                if not older:
                    self.history_scan_complete = True
                    self.last_history_scan = time.time()
                else:
                    self.merge_summaries(older)
                    self.last_page_ids = ids
                    self.next_history_page += 1
                # Persist discovery separately so an interrupted GPS fetch loses no page.
                await self.async_save()

        candidates = self.pending_summaries()
        pending = self.get_pending_trip({"data": candidates})
        if pending is None:
            await self.async_save()
            return
        track_id = str(pending["trackId"])
        result = await self.hass.async_add_executor_job(
            self.download_trip, api, pending
        )
        if result is None:
            attempts = self.attempts.get(track_id, 0) + 1
            self.attempts[track_id] = attempts
            if attempts >= MAX_GPS_ATTEMPTS:
                self.trips[track_id] = {
                    "track_id": track_id,
                    "summary": pending,
                    "date": pending.get("date"),
                    "gps_unavailable": True,
                }
                self.retry_after.pop(track_id, None)
            else:
                self.retry_after[track_id] = time.time() + min(
                    300 * 2 ** (attempts - 1), MAX_RETRY_DELAY
                )
        else:
            self.trips[track_id] = result
            self.attempts.pop(track_id, None)
            self.retry_after.pop(track_id, None)
        # The user's full archive is retained; live entity payloads remain bounded.
        await self.async_save()

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
    unavailable = sum(
        bool(trip.get("gps_unavailable")) for trip in history.trips.values()
    )
    attributes = {
        "trips": [],
        "total_trips": len(saved),
        "trips_truncated": False,
        "discovered_trips": len(history.summaries),
        "pending_trips": len(history.pending_summaries()),
        "unavailable_gps_trips": unavailable,
        "history_scan_complete": history.history_scan_complete,
        "next_history_page": history.next_history_page,
        "sync_status": (
            "waiting"
            if time.time() < history.request_retry_after
            else (
                "discovering"
                if not history.history_scan_complete
                else (
                    "downloading"
                    if history.pending_summaries()
                    else ("complete_with_missing_gps" if unavailable else "complete")
                )
            )
        ),
        "sync_error": history.sync_error,
    }
    for trip in saved[:MAX_HISTORY_TRIPS]:
        metadata = trip_metadata(trip)
        attributes["trips"].append(metadata)
        if _json_size(attributes) > MAX_ATTRIBUTE_BYTES:
            attributes["trips"].pop()
            break
    attributes["trips_truncated"] = len(attributes["trips"]) < len(saved)
    return attributes
