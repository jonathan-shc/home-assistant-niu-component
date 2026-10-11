"""Standalone tests for NIU trip history."""

import asyncio
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
NIU = ROOT / "custom_components" / "niu"


# Mock Home Assistant storage.
storage_module = types.ModuleType("homeassistant.helpers.storage")


class FakeStore:
    def __init__(self, hass, version, key):
        self.path = Path(hass.storage_dir) / f"{key}.json"

    async def async_load(self):
        if self.path.exists():
            return json.loads(self.path.read_text())
        return None

    async def async_save(self, data):
        self.path.write_text(json.dumps(data))


storage_module.Store = FakeStore

sys.modules["homeassistant"] = types.ModuleType("homeassistant")
sys.modules["homeassistant.helpers"] = types.ModuleType("homeassistant.helpers")
sys.modules["homeassistant.helpers.storage"] = storage_module

# Load the actual module without importing the full HA integration.
package = types.ModuleType("custom_components")
package.__path__ = [str(ROOT / "custom_components")]
sys.modules["custom_components"] = package

niu_package = types.ModuleType("custom_components.niu")
niu_package.__path__ = [str(NIU)]
sys.modules["custom_components.niu"] = niu_package

api_module = types.ModuleType("custom_components.niu.api")
api_module.NiuApi = type("NiuApi", (), {})
api_module.NiuApiError = type("NiuApiError", (Exception,), {})
sys.modules["custom_components.niu.api"] = api_module

spec = importlib.util.spec_from_file_location(
    "custom_components.niu.trip_history",
    NIU / "trip_history.py",
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

NiuTripHistory = module.NiuTripHistory


class FakeHass:
    def __init__(self, storage_dir):
        self.storage_dir = storage_dir

    async def async_add_executor_job(self, function, *args):
        return function(*args)


class FakeApi:
    def __init__(self):
        self.calls = []
        self.dataTrackInfo = {"data": []}

    def get_track_detail(self, track_id, date):
        self.calls.append(track_id)
        return {
            "status": 0,
            "data": {
                "trackItems": [
                    {
                        "lat": 52.0,
                        "lng": 5.0,
                        "speed": 20,
                        "date": 1791652590,
                    }
                ],
                "mileage": 0.678,
                "duration": 189,
            },
        }


def trip(track_id, start_time):
    return {
        "trackId": track_id,
        "date": "20261010",
        "startTime": start_time,
    }


async def run_tests():
    with tempfile.TemporaryDirectory() as directory:
        hass = FakeHass(directory)
        api = FakeApi()

        history = NiuTripHistory(hass, "test_vehicle")
        await history.async_load()

        # Test 1: Initial synchronization.
        api.dataTrackInfo = {
            "data": [
                trip("newest", 1791652590000),
                trip("older1", 1791652000000),
                trip("older2", 1791651000000),
            ]
        }

        await history.async_process(api)

        assert api.calls == ["newest"], api.calls
        assert "older1" not in history.trips
        print("✓ Test 1: Initial sync downloads only newest trip")

        # Test 2: No duplicate downloads.
        await history.async_process(api)

        assert api.calls == ["newest", "older1"], api.calls
        print("✓ Test 2: Historical trip is downloaded without redownloading newest")

        # Test 3: New trip detection.
        api.dataTrackInfo["data"].insert(
            0,
            trip("new_trip", 1791653000000),
        )

        await history.async_process(api)

        assert api.calls == ["newest", "older1", "new_trip"], api.calls
        assert "new_trip" in history.trips
        print("✓ Test 3: New trips are downloaded")

        # Test 4: Persistence across restarts.
        restarted = NiuTripHistory(hass, "test_vehicle")
        await restarted.async_load()

        assert "newest" in restarted.trips
        assert "new_trip" in restarted.trips
        assert "older1" in restarted.trips

        await restarted.async_process(api)
        assert api.calls == ["newest", "older1", "new_trip", "older2"], api.calls
        await restarted.async_process(api)
        assert api.calls == ["newest", "older1", "new_trip", "older2"], api.calls
        print("✓ Test 4: Trips survive restart without redownload")

        print("\nAll four tests passed!")


if __name__ == "__main__":
    asyncio.run(run_tests())
