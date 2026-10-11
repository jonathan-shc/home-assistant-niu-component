"""Full pagination, durable progress, archive size and request pacing."""

import asyncio
import importlib
import types
import unittest
from unittest.mock import Mock, patch

from tests.unit.ha_stubs import clear_niu_modules, install_homeassistant_stubs
from tests.unit.test_trip_backfill import summary


class FullHistoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        install_homeassistant_stubs()
        clear_niu_modules()
        self.module = importlib.import_module("custom_components.niu.trip_history")
        self.api_module = importlib.import_module("custom_components.niu.api")
        self.coordinator_module = importlib.import_module(
            "custom_components.niu.coordinator"
        )

        async def execute(function, *args):
            return function(*args)

        self.hass = types.SimpleNamespace(async_add_executor_job=execute)
        self.history = self.module.NiuTripHistory(self.hass, "entry")
        self.history.initialized = True
        self.api = types.SimpleNamespace(
            dataTrackInfo={"data": [summary("new", 2000)]},
            get_track_page=Mock(return_value=[]),
            get_track_detail=Mock(
                return_value={"data": {"trackItems": [{"lat": 52, "lng": 5}]}}
            ),
        )

    def test_normalizes_both_live_response_shapes_and_rejects_invalid_data(self):
        api = self.api_module.NiuApi("user", "password", 0)
        for data in (
            [summary("ride", 1)],
            {"items": [summary("ride", 1)]},
            [],
            {"items": []},
        ):
            with patch.object(api, "post_info_track", return_value={"data": data}):
                result = api.get_track_page(2)
                self.assertIsInstance(result, list)
        for data in (None, {"items": None}, ["invalid"]):
            with patch.object(api, "post_info_track", return_value={"data": data}):
                with self.assertRaises(self.api_module.NiuResponseError):
                    api.get_track_page(2)
        with self.assertRaises(ValueError):
            api.get_track_page(0)

    async def test_full_600_ride_archive_is_discovered_and_downloaded_without_eviction(
        self,
    ):
        rides = [summary(str(i), i) for i in range(600, 0, -1)]
        self.api.dataTrackInfo = {"data": rides[:50]}
        self.api.get_track_page.side_effect = lambda page: rides[
            (page - 1) * 50 : page * 50
        ]
        for _ in range(600):
            await self.history.async_process(self.api)
        self.assertTrue(self.history.history_scan_complete)
        self.assertEqual(len(self.history.summaries), 600)
        self.assertEqual(len(self.history.saved_trips()), 600)
        self.assertEqual(self.api.get_track_detail.call_count, 600)
        self.assertEqual(
            [call.args[0] for call in self.api.get_track_page.call_args_list],
            list(range(2, 14)),
        )
        self.assertFalse(self.history.has_work)
        await self.history.async_process(self.api)
        self.assertEqual(self.api.get_track_detail.call_count, 600)
        attributes = self.module.trip_history_attributes(self.history)
        self.assertEqual(attributes["discovered_trips"], 600)
        self.assertEqual(attributes["pending_trips"], 0)
        self.assertEqual(attributes["sync_status"], "complete")
        self.assertLessEqual(
            self.module._json_size(attributes), self.module.MAX_ATTRIBUTE_BYTES
        )

    async def test_restart_resumes_from_saved_page_and_route_queue(self):
        self.api.get_track_page.side_effect = lambda page: (
            [summary("older", 1000)] if page == 2 else []
        )
        await self.history.async_process(self.api)
        await self.history.async_select_trip("new")
        restarted = self.module.NiuTripHistory(self.hass, "entry")
        await restarted.async_load()
        await restarted.async_process(self.api)
        self.assertEqual(
            [call.args[0] for call in self.api.get_track_page.call_args_list], [2, 3]
        )
        self.assertEqual(len(restarted.saved_trips()), 2)
        self.assertEqual(restarted.selected_trip["track_id"], "new")

    async def test_repeated_page_is_not_false_completion(self):
        self.api.get_track_page.return_value = self.api.dataTrackInfo["data"]
        with self.assertLogs(self.module._LOGGER, level="WARNING"):
            await self.history.async_process(self.api)
        self.assertFalse(self.history.history_scan_complete)
        self.assertEqual(self.history.next_history_page, 2)
        self.assertIn("did not advance", self.history.sync_error)
        self.api.get_track_detail.assert_not_called()

    async def test_rate_limit_pauses_queue_until_server_retry_after_without_losing_cursor(
        self,
    ):
        self.api.get_track_page.side_effect = self.api_module.NiuRateLimitError(7200)
        with patch.object(self.module.time, "time", return_value=1000), self.assertLogs(
            self.module._LOGGER, level="WARNING"
        ):
            await self.history.async_process(self.api)
            await self.history.async_process(self.api)
        self.assertEqual(self.history.request_retry_after, 8200)
        self.assertEqual(self.api.get_track_page.call_count, 1)
        self.assertEqual(self.history.next_history_page, 2)
        self.api.get_track_detail.assert_not_called()
        self.api.get_track_page.side_effect = None
        with patch.object(self.module.time, "time", return_value=8201):
            await self.history.async_process(self.api)
        self.assertEqual(len(self.history.saved_trips()), 1)
        self.assertIsNone(self.history.sync_error)

    async def test_detail_api_error_is_not_mislabeled_as_absent_gps(self):
        self.api.get_track_detail.side_effect = self.api_module.NiuConnectionError(
            "Offline"
        )
        with self.assertLogs(self.module._LOGGER, level="WARNING"):
            await self.history.async_process(self.api)
        self.assertNotIn("new", self.history.attempts)
        self.assertNotIn("new", self.history.trips)
        self.assertEqual(len(self.history.pending_summaries()), 1)

    async def test_empty_gps_is_checked_three_times_with_backoff_then_progress_finishes(
        self,
    ):
        self.api.get_track_detail.return_value = {"data": {"trackItems": []}}
        for now in (1000, 1301, 1902):
            with patch.object(self.module.time, "time", return_value=now):
                await self.history.async_process(self.api)
        self.assertTrue(self.history.trips["new"]["gps_unavailable"])
        self.assertEqual(self.api.get_track_detail.call_count, 3)
        self.assertFalse(self.history.has_work)
        attributes = self.module.trip_history_attributes(self.history)
        self.assertEqual(attributes["unavailable_gps_trips"], 1)
        self.assertEqual(attributes["sync_status"], "complete_with_missing_gps")
        self.assertEqual(len(self.history.saved_trips()), 0)

    async def test_completed_index_is_rescanned_daily_for_rides_missed_during_offline_period(
        self,
    ):
        with patch.object(self.module.time, "time", return_value=1000):
            await self.history.async_process(self.api)
        self.api.get_track_page.return_value = [summary("missed", 500)]
        with patch.object(
            self.module.time,
            "time",
            return_value=1000 + self.module.HISTORY_SCAN_INTERVAL,
        ):
            await self.history.async_process(self.api)
        self.assertFalse(self.history.history_scan_complete)
        self.assertIn("missed", self.history.trips)

    async def test_faster_history_steps_keep_battery_and_recent_trip_polling_at_150_seconds(
        self,
    ):
        self.api.initialize = Mock()
        self.api.updateBat = Mock()
        self.api.updateTrackInfo = Mock()
        const = importlib.import_module("custom_components.niu.const")
        coordinator = self.coordinator_module.NiuDataUpdateCoordinator(
            self.hass,
            None,
            self.api,
            {const.SENSOR_TYPE_BAT},
            trip_history=self.history,
        )
        self.api.get_track_page.side_effect = lambda page: (
            [summary("older1", 1000), summary("older2", 900), summary("older3", 800)]
            if page == 2
            else []
        )
        for now in (1000, 1030, 1060):
            with patch.object(self.coordinator_module, "monotonic", return_value=now):
                await coordinator._async_update_data()
        self.api.updateBat.assert_called_once()
        self.api.updateTrackInfo.assert_called_once()
        self.assertEqual(coordinator.update_interval.total_seconds(), 30)
        self.api.get_track_page.return_value = []
        self.history.request_retry_after = 0
        with patch.object(self.coordinator_module, "monotonic", return_value=1150):
            await coordinator._async_update_data()
        self.assertEqual(self.api.updateBat.call_count, 2)
        self.assertEqual(coordinator.update_interval.total_seconds(), 150)

    async def test_storage_writes_are_serialized_and_selection_save_uses_latest_snapshot(
        self,
    ):
        await self.history.async_process(self.api)
        entered = asyncio.Event()
        release = asyncio.Event()
        snapshots = []

        async def save(data):
            snapshots.append(data)
            if len(snapshots) == 1:
                entered.set()
                await release.wait()

        with patch.object(self.history.store, "async_save", side_effect=save):
            first = asyncio.create_task(self.history.async_save())
            await entered.wait()
            choice = asyncio.create_task(self.history.async_select_trip("new"))
            await asyncio.sleep(0)
            self.assertEqual(len(snapshots), 1)
            release.set()
            await asyncio.gather(first, choice)
        self.assertIsNone(snapshots[0]["selected_trip_id"])
        self.assertEqual(snapshots[-1]["selected_trip_id"], "new")
