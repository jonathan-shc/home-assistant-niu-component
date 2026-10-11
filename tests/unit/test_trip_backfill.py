"""Regression tests for automatic NIU historical GPS downloads."""

import importlib
import types
import unittest
from unittest.mock import Mock, patch

from tests.unit.ha_stubs import clear_niu_modules, install_homeassistant_stubs


def summary(track_id, timestamp):
    return {"trackId": track_id, "date": "20261010", "startTime": timestamp}


class BackfillTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        install_homeassistant_stubs()
        clear_niu_modules()
        self.module = importlib.import_module("custom_components.niu.trip_history")
        self.api_module = importlib.import_module("custom_components.niu.api")

        async def execute(function, *args):
            return function(*args)

        self.hass = types.SimpleNamespace(async_add_executor_job=execute)
        self.history = self.module.NiuTripHistory(self.hass, "entry-1")
        self.history.initialized = True
        self.api = types.SimpleNamespace(
            dataTrackInfo={"data": [summary("new", 2000), summary("old", 1000)]},
            get_track_page=Mock(return_value=[]),
            get_track_detail=Mock(
                return_value={"data": {"trackItems": [{"lat": 52, "lng": 5}]}}
            ),
        )

    async def test_first_sync_and_repeated_polls_fill_history_without_duplicates(self):
        await self.history.async_process(self.api)
        self.assertEqual(list(self.history.trips), ["new"])
        await self.history.async_process(self.api)
        await self.history.async_process(self.api)
        self.assertEqual(
            [call.args[0] for call in self.api.get_track_detail.call_args_list],
            ["new", "old"],
        )
        self.assertEqual(len(self.history.saved_trips()), 2)

    async def test_existing_skip_markers_are_downloaded_after_upgrade(self):
        self.history.first_sync_completed = True
        self.history.trips["old"] = {"track_id": "old", "skipped_initial_sync": True}
        await self.history.async_process(self.api)
        original = self.history.trips["new"]
        await self.history.async_process(self.api)
        self.assertIs(self.history.trips["new"], original)
        self.assertNotIn("skipped_initial_sync", self.history.trips["old"])
        self.assertEqual(self.api.get_track_detail.call_count, 2)

    async def test_new_rides_take_priority_over_the_backfill_queue(self):
        await self.history.async_process(self.api)
        self.api.dataTrackInfo["data"].insert(0, summary("newer", 3000))
        await self.history.async_process(self.api)
        self.assertEqual(
            [call.args[0] for call in self.api.get_track_detail.call_args_list],
            ["new", "newer"],
        )
        self.assertNotIn("old", self.history.trips)

    async def test_missing_gps_uses_backoff_and_other_routes_still_download(self):
        self.api.get_track_detail.side_effect = [
            {"data": {"trackItems": []}},
            {"data": {"trackItems": [{"lat": 52, "lng": 5}]}},
        ]
        with patch.object(self.module.time, "time", return_value=1000):
            await self.history.async_process(self.api)
            self.assertEqual(self.history.retry_after["new"], 1300)
            await self.history.async_process(self.api)
            await self.history.async_process(self.api)
        self.assertEqual(
            [call.args[0] for call in self.api.get_track_detail.call_args_list],
            ["new", "old"],
        )
        self.assertEqual(self.history.saved_trips()[0]["track_id"], "old")

    async def test_restart_resumes_queue_and_preserves_explicit_selection(self):
        await self.history.async_process(self.api)
        await self.history.async_select_trip("new")
        restarted = self.module.NiuTripHistory(self.hass, "entry-1")
        await restarted.async_load()
        await restarted.async_process(self.api)
        self.assertEqual(restarted.selected_trip["track_id"], "new")
        self.assertEqual(len(restarted.saved_trips()), 2)
        self.assertEqual(self.api.get_track_detail.call_count, 2)

    async def test_late_download_of_older_ride_cannot_evict_latest_route(self):
        await self.history.async_process(self.api)
        await self.history.async_process(self.api)
        self.api.dataTrackInfo["data"].append(summary("oldest", 500))
        await self.history.async_process(self.api)
        self.assertEqual(
            [trip["track_id"] for trip in self.history.saved_trips()],
            ["new", "old", "oldest"],
        )

    async def test_empty_cloud_list_preserves_saved_history(self):
        self.api.dataTrackInfo["data"] = self.api.dataTrackInfo["data"][:1]
        await self.history.async_process(self.api)
        self.api.dataTrackInfo = {"data": []}
        await self.history.async_process(self.api)
        self.assertEqual(self.api.get_track_detail.call_count, 1)
        self.assertEqual(self.history.saved_trips()[0]["track_id"], "new")

    def test_api_requests_fifty_recent_trips(self):
        api = self.api_module.NiuApi("user", "password", 0)
        api.sn = "SN"
        with patch.object(api, "_authenticated_request", return_value={}) as request:
            api.post_info_track("/track/list")
        self.assertEqual(
            request.call_args.kwargs["json"], {"index": "0", "pagesize": 50, "sn": "SN"}
        )
