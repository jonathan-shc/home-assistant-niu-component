"""Shared NIU entity definitions."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NiuDataUpdateCoordinator


class NiuCoordinatorEntity(CoordinatorEntity[NiuDataUpdateCoordinator]):
    """Base entity attached to a scooter and its shared coordinator."""

    def __init__(self, coordinator: NiuDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        device_name = "Niu E-scooter"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_name)},
            name=device_name,
            manufacturer="NIU",
            model="E-scooter",
        )


class NiuTripEntity(NiuCoordinatorEntity):
    """Base for local trip entities with the existing scooter naming scheme."""

    def __init__(self, coordinator, key: str, name: str, platform: str) -> None:
        super().__init__(coordinator)
        api = coordinator.data
        self.history = coordinator.trip_history
        self._attr_unique_id = f"{platform}.niu_scooter_{api.sn}_{key}"
        self._attr_name = f"NIU Scooter {api.sensor_prefix} {name}"
        self._attr_icon = "mdi:map-marker-path"

    @property
    def available(self) -> bool:
        """Stored routes remain readable when the NIU cloud is unavailable."""
        return self.history.initialized
