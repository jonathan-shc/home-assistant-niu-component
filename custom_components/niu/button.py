"""Explicit NIU seat release command."""

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError

from .api import NiuApiError
from .const import DOMAIN
from .entity import NiuCoordinatorEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            NiuRefreshStatusButton(coordinator),
            NiuOpenSeatButton(coordinator),
            NiuCommandButton(coordinator, "wake", "Wake", "acc_on", "mdi:power"),
            NiuCommandButton(
                coordinator, "power_off", "Power Off", "acc_off", "mdi:power-off"
            ),
            NiuCommandButton(
                coordinator,
                "arm_alarm",
                "Arm Alarm",
                "fortification_on",
                "mdi:shield-lock",
            ),
            NiuCommandButton(
                coordinator,
                "disarm_alarm",
                "Disarm Alarm",
                "fortification_off",
                "mdi:shield-lock-open",
            ),
        ]
    )


class NiuRefreshStatusButton(NiuCoordinatorEntity, ButtonEntity):
    """Explicit read-only motor-status request, independent of history polling."""

    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"button.niu_scooter_{coordinator.api.sn}_refresh_status"
        self._attr_name = f"NIU Scooter {coordinator.api.sensor_prefix} Refresh Status"

    async def async_press(self):
        await self.coordinator.async_refresh_status()


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


class NiuCommandButton(NiuCoordinatorEntity, ButtonEntity):
    """One deliberate cloud command; never a claim of physical state."""

    def __init__(self, coordinator, key, name, command, icon):
        super().__init__(coordinator)
        self.command = command
        self._attr_unique_id = f"button.niu_scooter_{coordinator.api.sn}_{key}"
        self._attr_name = f"NIU Scooter {coordinator.api.sensor_prefix} {name}"
        self._attr_icon = icon

    async def async_press(self):
        try:
            await self.hass.async_add_executor_job(
                self.coordinator.api.send_command, self.command
            )
        except NiuApiError as err:
            raise HomeAssistantError(f"NIU command failed: {err}") from err
