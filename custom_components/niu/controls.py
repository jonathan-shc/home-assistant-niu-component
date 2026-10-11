"""Isolated settings polling and validated NIU cloud controls."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import timedelta

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import NiuApiError, NiuRateLimitError
from .entity import NiuCoordinatorEntity

_LOGGER = logging.getLogger(__name__)
CHARGE_LIMIT_VALUES = ("80", "85", "90", "95", "100")


def finite_number(value):
    """Reject missing, boolean, malformed or non-finite provider values."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


class NiuControlCoordinator(DataUpdateCoordinator[dict]):
    """Settings failures must not stop status polling or GPS backfill."""

    def __init__(self, hass, entry, api):
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="niu_controls",
            update_interval=timedelta(seconds=150),
        )
        self.api = api
        self.settings_available = False
        self._control_lock = asyncio.Lock()

    async def _read_settings(self):
        try:
            settings = await self.hass.async_add_executor_job(self.api.get_settings)
        except NiuApiError as err:
            self.settings_available = False
            if isinstance(err, NiuRateLimitError):
                self.update_interval = timedelta(
                    seconds=max(150, err.retry_after or 300)
                )
            _LOGGER.warning("NIU settings unavailable: %s", err)
            return self.data or {}
        self.settings_available = True
        self.update_interval = timedelta(seconds=150)
        return settings

    async def _async_update_data(self):
        async with self._control_lock:
            return await self._read_settings()

    def supports(self, capability):
        return (self.data or {}).get(capability) in (1, "1", True)

    def power_bounds(self):
        """Only offer limits reported by the scooter; never invent a range."""
        power = (self.data or {}).get("charge_power_set_value")
        if not isinstance(power, dict):
            return None
        values = power.get("charge_power_range")
        if not isinstance(values, (list, tuple)) or len(values) != 2:
            return None
        low, high = map(finite_number, values)
        if low is None or high is None or low < 0 or high <= low:
            return None
        return low, high

    async def async_set_setting(self, field, value):
        async with self._control_lock:
            capabilities = {
                "low_power_mode_value": "sup_low_power_mode",
                "charging_limit_value": "sup_charging_limit",
                "charge_power_set_value": "sup_charge_power_set",
            }
            if not self.settings_available or not self.supports(
                capabilities.get(field)
            ):
                raise HomeAssistantError("NIU setting is currently unavailable")
            if field == "low_power_mode_value" and value not in ("0", "1"):
                raise HomeAssistantError("Invalid Auto Sleep value")
            if field == "charging_limit_value" and value not in CHARGE_LIMIT_VALUES:
                raise HomeAssistantError("Invalid NIU charging limit")
            if field == "charge_power_set_value":
                bounds = self.power_bounds()
                number = finite_number(value)
                if (
                    bounds is None
                    or number is None
                    or not number.is_integer()
                    or not bounds[0] <= number <= bounds[1]
                ):
                    raise HomeAssistantError(
                        "Charging power is outside the reported range"
                    )
            try:
                await self.hass.async_add_executor_job(
                    self.api.set_setting, field, value
                )
            except NiuApiError as err:
                raise HomeAssistantError(f"NIU rejected the setting: {err}") from err
            # Publish provider readback, not an optimistic requested value.
            settings = await self._read_settings()
            self.async_set_updated_data(settings)
            if not self.settings_available:
                raise HomeAssistantError(
                    "NIU accepted the request, but its setting could not be read back"
                )


class NiuControlEntity(NiuCoordinatorEntity):
    """Use existing device identity and scooter entity naming."""

    def __init__(self, coordinator, key, name, platform, capability):
        super().__init__(coordinator)
        self.capability = capability
        api = coordinator.api
        self._attr_unique_id = f"{platform}.niu_scooter_{api.sn}_{key}"
        self._attr_name = f"NIU Scooter {api.sensor_prefix} {name}"

    @property
    def available(self):
        return (
            super().available
            and self.coordinator.settings_available
            and self.coordinator.supports(self.capability)
        )
