"""Local NIU historical trip selection."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity

from .const import DOMAIN
from .entity import NiuTripEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Add one selector per scooter without changing its existing config."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NiuTripSelect(coordinator)])


class NiuTripSelect(NiuTripEntity, SelectEntity):
    """Select a saved route by its stable trip ID."""

    _unrecorded_attributes = frozenset({"options"})

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "trip", "Trip", "select")

    @property
    def options(self) -> list[str]:
        """Include all downloaded trips, newest first, without sync markers."""
        return [trip["track_id"] for trip in self.history.saved_trips()]

    @property
    def current_option(self) -> str | None:
        """Default to the newest saved route until a historical ride is chosen."""
        trip = self.history.selected_trip
        return trip["track_id"] if trip else None

    async def async_select_option(self, option: str) -> None:
        """Save the choice and immediately update local entities, without polling."""
        await self.history.async_select_trip(option)
        self.coordinator.async_update_listeners()
