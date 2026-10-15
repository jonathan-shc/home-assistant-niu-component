"""A ride fetched while still under way is fetched again once it has grown."""

import importlib
import time
import types
import unittest
from unittest.mock import Mock

from tests.unit.ha_stubs import clear_niu_modules, install_homeassistant_stubs


def ride(end_ms, distance):
    return {
        "trackId": "ride",
        "date": "20261014",
        "startTime": 1791990668419,
        "endTime": end_ms,
        "distance": distance,
    }


def detail(points):
    return {"data": {"trackItems": [{"lat": 52 + i / 1000, "lng": 5} for i in range(points)]}}


class GrowingRideTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        install_homeassistant_stubs()
        clear_niu_modules()
        self.module = importlib.import_module("custom_components.niu.trip_history")

        async def execute(function, *args):
            return function(*args)

        self.hass = types.SimpleNamespace(async_add_executor_job=execute)
        self.history = self.module.NiuTripHistory(self.hass, "entry")
        self.history.initialized = True
        self.history.history_scan_complete = True
        self.history.last_history_scan = time.time()
        self.api = types.SimpleNamespace(
            dataTrackInfo={"data": []},
            get_track_page=Mock(return_value=[]),
            get_track_detail=Mock(return_value=detail(34)),
        )
        self.long_ago = (time.time() - 3600) * 1000

    async def test_ride_still_under_way_is_not_fetched_yet(self):
        self.api.dataTrackInfo = {"data": [ride(time.time() * 1000 - 5000, 937)]}
        await self.history.async_process(self.api)
        self.api.get_track_detail.assert_not_called()

    async def test_route_fetched_mid_ride_is_fetched_again_when_ride_grew(self):
        self.api.dataTrackInfo = {"data": [ride(self.long_ago - 1_350_000, 937)]}
        await self.history.async_process(self.api)
        self.assertEqual(self.api.get_track_detail.call_count, 1)

        self.api.dataTrackInfo = {"data": [ride(self.long_ago, 11799)]}
        self.api.get_track_detail.return_value = detail(480)
        await self.history.async_process(self.api)
        self.assertEqual(self.api.get_track_detail.call_count, 2)
        trip = self.history.saved_trips()[0]
        self.assertEqual(len(trip["detail"]["trackItems"]), 480)
        self.assertEqual(trip["summary"]["distance"], 11799)

        await self.history.async_process(self.api)
        self.assertEqual(self.api.get_track_detail.call_count, 2)  # done now

    async def test_failed_refetch_keeps_the_partial_route(self):
        self.api.dataTrackInfo = {"data": [ride(self.long_ago - 1_350_000, 937)]}
        await self.history.async_process(self.api)
        self.api.dataTrackInfo = {"data": [ride(self.long_ago, 11799)]}
        self.api.get_track_detail.return_value = {"data": {"trackItems": []}}
        for _ in range(self.module.MAX_GPS_ATTEMPTS):
            self.history.retry_after.clear()
            await self.history.async_process(self.api)
        trip = self.history.saved_trips()[0]
        self.assertEqual(len(trip["detail"]["trackItems"]), 34)
        self.assertFalse(self.history.has_work)
