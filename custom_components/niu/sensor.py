"""Sensor entities for NIU scooters."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity

from .const import (
    CONF_AUTH,
    CONF_SENSORS,
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
from .coordinator import NiuDataUpdateCoordinator
from .entity import NiuCoordinatorEntity, NiuTripEntity
from .trip_history import trip_history_attributes, trip_route_attributes


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up NIU sensors from a config entry."""
    coordinator: NiuDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    # Add the raw riding-mode code to existing entries without changing their config.
    sensors_selected = list(entry.data[CONF_AUTH][CONF_SENSORS])
    if "RidingMode" not in sensors_selected:
        sensors_selected.append("RidingMode")

    entities = []
    for sensor in sensors_selected:
        if sensor == "LastTrackThumb" or sensor not in SENSOR_TYPES:
            continue
        sensor_config = SENSOR_TYPES[sensor]
        entities.append(
            NiuSensor(
                coordinator,
                sensor,
                sensor_config[0],
                sensor_config[1],
                sensor_config[2],
                sensor_config[3],
                sensor_config[4],
                sensor_config[5],
            )
        )

    entities.extend(
        NiuTripSensor(coordinator, key, name)
        for key, name in (
            ("last_trip", "Last Trip"),
            ("trip_history", "Trip History"),
            ("trip_count", "Trip Count"),
            ("selected_trip", "Selected Trip"),
        )
    )
    async_add_entities(entities)


class NiuSensor(NiuCoordinatorEntity, SensorEntity):
    """A NIU sensor backed by the config entry's shared coordinator."""

    def __init__(
        self,
        coordinator: NiuDataUpdateCoordinator,
        name: str,
        sensor_id: str,
        unit: str,
        data_field: str,
        sensor_group: str,
        device_class: str,
        icon: str,
    ) -> None:
        NiuCoordinatorEntity.__init__(self, coordinator)
        api = coordinator.data
        self._api = api
        self._data_field = data_field
        self._sensor_group = sensor_group
        self._attr_unique_id = f"sensor.niu_scooter_{api.sn}_{sensor_id}"
        self._attr_name = f"NIU Scooter {api.sensor_prefix} {name}"
        self._attr_native_unit_of_measurement = unit or None
        self._attr_device_class = device_class if device_class != "none" else None
        self._attr_icon = icon

    @property
    def native_value(self):
        """Return a value from the coordinator's cached API response."""
        if self._sensor_group == SENSOR_TYPE_BAT:
            return self._api.getDataBatA(self._data_field)
        if self._sensor_group == SENSOR_TYPE_BAT2:
            return self._api.getDataBatB(self._data_field)
        if self._sensor_group == SENSOR_TYPE_MOTO:
            return self._api.getDataMoto(self._data_field)
        if self._sensor_group == SENSOR_TYPE_POS:
            return self._api.getDataPos(self._data_field)
        if self._sensor_group == SENSOR_TYPE_DIST:
            return self._api.getDataDist(self._data_field)
        if self._sensor_group == SENSOR_TYPE_OVERALL:
            return self._api.getDataOverall(self._data_field)
        if self._sensor_group == SENSOR_TYPE_TRACK:
            return self._api.getDataTrack(self._data_field)
        return None

    @property
    def extra_state_attributes(self) -> dict | None:
        """Return diagnostic attributes for the connection sensor."""
        if self._sensor_group != SENSOR_TYPE_MOTO or self._data_field != "isConnected":
            return None

        attributes = {
            "bmsId_a": self._api.getDataBatA("bmsId"),
            "latitude": self._api.getDataPos("lat"),
            "longitude": self._api.getDataPos("lng"),
            # Provider measurement times (Unix milliseconds), not HA poll times.
            "gps_timestamp_ms": self._api.getDataMoto("gpsTimestamp"),
            "info_timestamp_ms": self._api.getDataMoto("infoTimestamp"),
            "is_acc_on": self._api.getDataMoto("isAccOn"),
            "current_speed": self._api.getDataMoto("nowSpeed"),
            "gsm": self._api.getDataMoto("gsm"),
            "gps": self._api.getDataMoto("gps"),
            "time": self._api.getDataDist("time"),
            "range": self._api.getDataMoto("estimatedMileage"),
            "battery_a": self._api.getDataBatA("batteryCharging"),
            "battery_grade_a": self._api.getDataBatA("gradeBattery"),
            "centre_ctrl_batt": self._api.getDataMoto("centreCtrlBattery"),
        }
        if self._api.hasSecondBattery():
            attributes.update(
                {
                    "bmsId_b": self._api.getDataBatB("bmsId"),
                    "battery_b": self._api.getDataBatB("batteryCharging"),
                    "battery_grade_b": self._api.getDataBatB("gradeBattery"),
                }
            )
        return attributes


class NiuTripSensor(NiuTripEntity, SensorEntity):
    """Expose a route, trip index or count from persistent local history."""

    _unrecorded_attributes = frozenset({"route", "trips"})

    def __init__(self, coordinator, key: str, name: str) -> None:
        super().__init__(coordinator, key, name, "sensor")
        self.key = key

    @property
    def native_value(self):
        """Use short trip IDs for routes and counts for the history index."""
        trips = self.history.saved_trips()
        if self.key in ("trip_count", "trip_history"):
            return len(trips)
        trip = (
            self.history.selected_trip
            if self.key == "selected_trip"
            else (trips[0] if trips else None)
        )
        return trip["track_id"] if trip else None

    @property
    def extra_state_attributes(self) -> dict | None:
        """Keep full raw API details and GPS histories outside entity state."""
        if self.key == "trip_count":
            return None
        if self.key == "trip_history":
            return trip_history_attributes(self.history)
        trips = self.history.saved_trips()
        trip = (
            self.history.selected_trip
            if self.key == "selected_trip"
            else (trips[0] if trips else None)
        )
        return trip_route_attributes(trip)
