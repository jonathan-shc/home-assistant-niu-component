"""Trip entity contracts, local selection and coordinator update ordering."""

from __future__ import annotations

import copy
import importlib
import json
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests.unit.ha_stubs import clear_niu_modules, install_homeassistant_stubs


def trip(track_id, start, points=None):
    """A stored response including fields which must never leak into metadata."""
    return {
        "track_id": track_id,
        "date": "20261010",
        "summary": {
            "startTime": start,
            "endTime": start + 1000,
            "distance": 678,
            "ridingtime": 189,
            "track_thumb": "https://private.example/token",
        },
        "detail": {
            "trackItems": (
                points
                if points is not None
                else [
                    {"lat": 52.0, "lng": 5.0, "speed": 20, "date": 100},
                    {"lat": 52.1, "lng": 5.1, "speed": 22, "date": 101},
                ]
            ),
            "private_field": "secret",
        },
        "downloaded_at": str(100000 - start),
    }


class TripEntitiesTest(unittest.IsolatedAsyncioTestCase):
    """Exercise the actual integration classes with isolated HA infrastructure."""

    def setUp(self):
        install_homeassistant_stubs()
        clear_niu_modules()
        self.history_module = importlib.import_module(
            "custom_components.niu.trip_history"
        )
        self.sensor_module = importlib.import_module("custom_components.niu.sensor")
        self.select_module = importlib.import_module("custom_components.niu.select")
        self.coordinator_module = importlib.import_module(
            "custom_components.niu.coordinator"
        )
        self.hass = types.SimpleNamespace(data={}, async_add_executor_job=AsyncMock())
        self.history = self.history_module.NiuTripHistory(self.hass, "entry-1")
        self.history.initialized = True
        self.api = types.SimpleNamespace(
            sn="SN-1", sensor_prefix="MQi", dataTrackInfo={"data": []}
        )
        self.coordinator = self.coordinator_module.NiuDataUpdateCoordinator(
            self.hass, None, self.api, {"track"}, trip_history=self.history
        )
        self.coordinator.data = self.api
        self.hass.data = {
            "niu": {
                "entry-1": self.coordinator,
                "_control_coordinators": {
                    "entry-1": types.SimpleNamespace(api=self.api)
                },
            }
        }

    def sensor(self, key):
        return self.sensor_module.NiuTripSensor(self.coordinator, key, key)

    def seed(self):
        self.history.trips = {
            "new": trip("new", 2000),
            "old": trip("old", 1000),
            "skipped": {"track_id": "skipped", "skipped_initial_sync": True},
        }

    async def test_setup_exposes_four_sensors_and_selector_without_config_migration(
        self,
    ):
        entry = types.SimpleNamespace(entry_id="entry-1", data={"niu_auth": {}})
        const = importlib.import_module("custom_components.niu.const")
        entry.data = {const.CONF_AUTH: {const.CONF_SENSORS: ["BatteryCharge"]}}
        add = Mock()
        before = copy.deepcopy(entry.data)
        await self.sensor_module.async_setup_entry(self.hass, entry, add)
        entities = add.call_args.args[0]
        self.assertEqual(len(entities), 6)
        self.assertEqual(
            [entity._attr_unique_id for entity in entities[2:]],
            [
                "sensor.niu_scooter_SN-1_last_trip",
                "sensor.niu_scooter_SN-1_trip_history",
                "sensor.niu_scooter_SN-1_trip_count",
                "sensor.niu_scooter_SN-1_selected_trip",
            ],
        )
        self.assertEqual(entities[2]._attr_name, "NIU Scooter MQi Last Trip")
        await self.select_module.async_setup_entry(self.hass, entry, add)
        selector = add.call_args.args[0][0]
        self.assertEqual(selector._attr_unique_id, "select.niu_scooter_SN-1_trip")
        self.assertEqual(entry.data, before)

    def test_empty_history_and_sync_markers_do_not_fabricate_trips(self):
        self.history.trips = {
            "skipped": {"track_id": "skipped", "skipped_initial_sync": True}
        }
        self.assertIsNone(self.sensor("last_trip").native_value)
        self.assertIsNone(self.sensor("selected_trip").native_value)
        self.assertEqual(self.sensor("trip_count").native_value, 0)
        self.assertEqual(self.sensor("last_trip").extra_state_attributes["route"], [])
        select = self.select_module.NiuTripSelect(self.coordinator)
        self.assertEqual(select.options, [])
        self.assertIsNone(select.current_option)

    def test_latest_is_ordered_by_ride_time_and_metadata_has_no_gps(self):
        self.seed()
        self.assertEqual(self.sensor("last_trip").native_value, "new")
        self.assertEqual(self.sensor("trip_count").native_value, 2)
        index = self.sensor("trip_history")
        self.assertEqual(index.native_value, 2)
        attributes = index.extra_state_attributes
        self.assertEqual(
            [item["track_id"] for item in attributes["trips"]], ["new", "old"]
        )
        self.assertEqual(attributes["trips"][0]["distance_km"], 0.678)
        self.assertEqual(attributes["trips"][0]["duration_seconds"], 189)
        serialized = json.dumps(attributes)
        for field in ("trackItems", "latitude", "longitude", "track_thumb", "secret"):
            self.assertNotIn(field, serialized)
        route = self.sensor("last_trip").extra_state_attributes
        self.assertEqual(
            route["route"],
            [
                {"latitude": 52.0, "longitude": 5.0},
                {"latitude": 52.1, "longitude": 5.1},
            ],
        )
        self.assertFalse(route["route_truncated"])
        self.assertEqual(route["gps_points_count"], 2)

    async def test_selection_updates_sensor_immediately_without_network_and_survives_restart(
        self,
    ):
        self.seed()
        selector = self.select_module.NiuTripSelect(self.coordinator)
        updates = []
        sensor = self.sensor("selected_trip")
        remove = self.coordinator.async_add_listener(
            lambda: updates.append(sensor.native_value)
        )
        self.assertEqual(selector.options, ["new", "old"])
        self.assertEqual(selector.current_option, "new")
        await selector.async_select_option("old")
        self.assertEqual(updates, ["old"])
        self.assertEqual(sensor.native_value, "old")
        self.assertEqual(sensor.extra_state_attributes["track_id"], "old")
        self.assertEqual(self.sensor("last_trip").native_value, "new")
        self.hass.async_add_executor_job.assert_not_called()
        restarted = self.history_module.NiuTripHistory(self.hass, "entry-1")
        await restarted.async_load()
        self.assertEqual(restarted.selected_trip["track_id"], "old")
        remove()
        await selector.async_select_option("new")
        self.assertEqual(updates, ["old"])

    async def test_invalid_selection_is_rejected_and_storage_failure_rolls_back(self):
        self.seed()
        selector = self.select_module.NiuTripSelect(self.coordinator)
        listener = Mock()
        self.coordinator.async_add_listener(listener)
        for invalid in ("missing", "skipped"):
            with self.assertRaises(ValueError):
                await selector.async_select_option(invalid)
        with (
            patch.object(self.history, "async_save", side_effect=OSError),
            self.assertRaises(OSError),
        ):
            await selector.async_select_option("old")
        self.assertEqual(selector.current_option, "new")
        listener.assert_not_called()

    async def test_selection_stays_fixed_on_new_trips_and_falls_back_after_eviction(
        self,
    ):
        self.seed()
        await self.history.async_select_trip("old")
        self.history.trips["newer"] = trip("newer", 3000)
        self.assertEqual(self.sensor("last_trip").native_value, "newer")
        self.assertEqual(self.sensor("selected_trip").native_value, "old")
        del self.history.trips["old"]
        self.assertEqual(self.sensor("selected_trip").native_value, "newer")
        self.assertEqual(
            self.select_module.NiuTripSelect(self.coordinator).current_option, "newer"
        )

    def test_large_route_is_bounded_preserves_endpoints_and_keeps_store_untouched(self):
        points = [
            {"lat": 52 + i / 1000000, "lng": 5 + i / 1000000} for i in range(2000)
        ]
        saved = trip("large", 1000, points)
        original = copy.deepcopy(saved)
        attributes = self.history_module.trip_route_attributes(saved)
        self.assertEqual(attributes["gps_points_count"], 2000)
        self.assertLessEqual(
            len(attributes["route"]), self.history_module.MAX_ROUTE_POINTS
        )
        self.assertTrue(attributes["route_truncated"])
        self.assertEqual(attributes["route"][0]["latitude"], points[0]["lat"])
        self.assertEqual(attributes["route"][-1]["latitude"], points[-1]["lat"])
        self.assertLessEqual(
            self.history_module._json_size(attributes),
            self.history_module.MAX_ATTRIBUTE_BYTES,
        )
        self.assertEqual(saved, original)

    def test_invalid_coordinates_and_nonfinite_metadata_are_filtered(self):
        saved = trip(
            "invalid",
            1000,
            [
                None,
                {"lat": "nan", "lng": 5},
                {"lat": 91, "lng": 5},
                {"lat": True, "lng": 5},
                {"lat": 52, "lng": "inf"},
                {"latitude": "0", "longitude": "0"},
            ],
        )
        saved["summary"]["distance"] = float("nan")
        attributes = self.history_module.trip_route_attributes(saved)
        self.assertEqual(attributes["route"], [{"latitude": 0.0, "longitude": 0.0}])
        self.assertEqual(attributes["valid_gps_points_count"], 1)
        self.assertNotIn("distance_km", attributes)
        self.assertFalse(attributes["route_truncated"])
        json.dumps(attributes, allow_nan=False)

    def test_metadata_history_byte_limit_with_long_unicode_ids(self):
        self.history.trips = {str(i): trip("界" * 250 + str(i), i) for i in range(100)}
        attributes = self.history_module.trip_history_attributes(self.history)
        self.assertTrue(attributes["trips_truncated"])
        self.assertLessEqual(
            len(attributes["trips"]), self.history_module.MAX_HISTORY_TRIPS
        )
        self.assertLessEqual(
            self.history_module._json_size(attributes),
            self.history_module.MAX_ATTRIBUTE_BYTES,
        )
        self.assertEqual(attributes["total_trips"], 100)
        self.assertEqual(self.sensor("trip_count").native_value, 100)

    def test_recorder_exclusions_and_local_availability_during_cloud_failure(self):
        self.seed()
        self.coordinator.last_update_success = False
        self.assertTrue(self.sensor("last_trip").available)
        self.assertTrue(self.select_module.NiuTripSelect(self.coordinator).available)
        self.assertIn("route", self.sensor("last_trip")._unrecorded_attributes)
        self.assertIn("trips", self.sensor("trip_history")._unrecorded_attributes)
        self.assertIn(
            "options",
            self.select_module.NiuTripSelect(self.coordinator)._unrecorded_attributes,
        )
        self.history.initialized = False
        self.assertFalse(self.sensor("last_trip").available)

    async def test_trip_processing_finishes_before_coordinator_notifies_entities(self):
        async def process(api):
            self.history.trips["downloaded"] = trip("downloaded", 1000)

        updates = []
        self.hass.async_add_executor_job.return_value = self.api
        self.coordinator.async_add_listener(
            lambda: updates.append(self.sensor("last_trip").native_value)
        )
        with patch.object(self.history, "async_process", side_effect=process):
            await self.coordinator.async_config_entry_first_refresh()
        self.assertEqual(updates, ["downloaded"])

    async def test_history_failure_does_not_break_existing_api_entities(self):
        self.hass.async_add_executor_job.return_value = self.api
        with patch.object(
            self.history, "async_process", side_effect=OSError
        ), self.assertLogs(self.coordinator_module._LOGGER, level="ERROR"):
            data = await self.coordinator._async_update_data()
        self.assertIs(data, self.api)

    async def test_added_track_polling_failure_preserves_battery_only_entries(self):
        api_module = importlib.import_module("custom_components.niu.api")
        const = importlib.import_module("custom_components.niu.const")
        self.api.initialize = Mock()
        self.api.updateBat = Mock()
        self.api.updateTrackInfo = Mock(side_effect=api_module.NiuRateLimitError(300))
        self.coordinator.sensor_groups = frozenset({const.SENSOR_TYPE_BAT})

        async def execute(function, *args):
            return function(*args)

        self.hass.async_add_executor_job.side_effect = execute
        with patch.object(self.history, "async_process") as process, self.assertLogs(
            self.coordinator_module._LOGGER, level="WARNING"
        ):
            data = await self.coordinator._async_update_data()
        self.assertIs(data, self.api)
        self.api.updateBat.assert_called_once()
        process.assert_not_called()

    async def test_stored_history_and_selection_are_isolated_per_entry(self):
        self.seed()
        await self.history.async_select_trip("old")
        other = self.history_module.NiuTripHistory(self.hass, "entry-2")
        await other.async_load()
        self.assertEqual(other.saved_trips(), [])
        self.assertIsNone(other.selected_trip)

    def test_missing_timestamp_uses_ride_date_and_malformed_records_are_ignored(self):
        older = trip("old", 0)
        older["summary"] = None
        older["date"] = "20261009"
        newer = trip("new", 0)
        newer["summary"] = {"startTime": "invalid"}
        self.history.trips = {
            "old": older,
            "new": newer,
            "bad": None,
            "empty": trip("empty", 0, []),
            "long_id": trip("x" * 256, 0),
        }
        self.assertEqual(
            [item["track_id"] for item in self.history.saved_trips()], ["new", "old"]
        )


if __name__ == "__main__":
    unittest.main()
