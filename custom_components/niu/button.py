"""Explicit NIU seat release command."""

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError

from .api import NiuApiError
from .const import DOMAIN
from .entity import NiuCoordinatorEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([NiuOpenSeatButton(hass.data[DOMAIN][entry.entry_id])])


class NiuOpenSeatButton(NiuCoordinatorEntity, ButtonEntity):
    """Release the seat only when the user presses the button."""

    _attr_icon = "mdi:car-seat"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        api = coordinator.api
        self._attr_unique_id = f"button.niu_scooter_{api.sn}_open_seat"
        self._attr_name = f"NIU Scooter {api.sensor_prefix} Open Seat"

    async def async_press(self):
        try:
            await self.hass.async_add_executor_job(self.coordinator.api.open_seat)
        except NiuApiError as err:
            raise HomeAssistantError(f"NIU seat release failed: {err}") from err
