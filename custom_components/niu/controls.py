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
REGEN_OPTIONS = {
    "Uit": "0",
    "Niveau 3": "3",
    "Standaard": "5",
    "Niveau 7": "7",
    "Sterk": "10",
}
AUTO_OFF_LABELS = {
    300: "5 minuten",
    600: "10 minuten",
    1800: "30 minuten",
    3600: "1 uur",
    28800: "8 uur",
}
CAPABILITIES = {
    "low_power_mode_value": "sup_low_power_mode",
    "charging_limit_value": "sup_charging_limit",
    "charge_power_set_value": "sup_charge_power_set",
    "foc_ebs_level_value": "sup_foc_ebs_level",
    "tcs_value": "sup_tcs",
    "auto_parking_value": "sup_auto_parking",
    "cur_sound_volume": "sup_sound_volume_max",
    "cur_sound_type": "sup_personality_sound",
    "auto_off_value": "sup_auto_off_setting",
}
SOUND_CAPABILITIES = {"sup_sound_volume_max", "sup_personality_sound"}


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
        self.sound_available = False
        self.auto_off_available = False
        self._control_lock = asyncio.Lock()

    async def _read_settings(self):
        settings = dict(self.data or {})
        rate_delay = 150
        sources = (
            (None, "settings_available", self.api.get_settings),
            ("_sound", "sound_available", self.api.get_sound_settings),
            ("_auto_off", "auto_off_available", self.api.get_auto_off_settings),
        )
        for key, available, getter in sources:
            try:
                data = await self.hass.async_add_executor_job(getter)
            except NiuApiError as err:
                setattr(self, available, False)
                if isinstance(err, NiuRateLimitError):
                    rate_delay = max(rate_delay, err.retry_after or 300)
                    self.update_interval = timedelta(seconds=rate_delay)
                    # A shared cloud rate limit applies to all remaining reads.
                    for remaining_key, remaining_available, _ in sources:
                        setattr(self, remaining_available, False)
                    _LOGGER.warning("NIU settings rate limited: %s", err)
                    return settings
                _LOGGER.warning("NIU %s unavailable: %s", key or "settings", err)
                continue
            setattr(self, available, True)
            if key is None:
                settings = {
                    **data,
                    "_sound": settings.get("_sound", {}),
                    "_auto_off": settings.get("_auto_off", {}),
                }
            else:
                settings[key] = data
        self.update_interval = timedelta(seconds=rate_delay)
        return settings

    async def _async_update_data(self):
        async with self._control_lock:
            return await self._read_settings()

    def capability_available(self, capability):
        if capability in SOUND_CAPABILITIES:
            return self.sound_available
        if capability == "sup_auto_off_setting":
            return self.auto_off_available
        return self.settings_available

    def source_data(self, capability):
        settings = self.data or {}
        if capability in SOUND_CAPABILITIES:
            return settings.get("_sound", {})
        if capability == "sup_auto_off_setting":
            return settings.get("_auto_off", {})
        return settings

    def supports(self, capability):
        if capability == "sup_sound_volume_max":
            return self.volume_max() is not None
        return self.source_data(capability).get(capability) in (1, "1", True)

    def volume_max(self):
        value = finite_number(
            (self.data or {}).get("_sound", {}).get("sup_sound_volume_max")
        )
        return (
            int(value)
            if value is not None and value.is_integer() and value >= 0
            else None
        )

    def auto_off_options(self):
        entries = (self.data or {}).get("_auto_off", {}).get("list")
        if not isinstance(entries, list):
            return {}
        options = {}
        for item in entries:
            if not isinstance(item, dict):
                continue
            value = finite_number(item.get("value"))
            if value is not None and value.is_integer() and value > 0:
                seconds = int(value)
                options[AUTO_OFF_LABELS.get(seconds, f"{seconds} seconden")] = seconds
        return options

    def sound_theme_options(self):
        entries = (self.data or {}).get("_sound", {}).get("sup_sound_type_infos")
        if not isinstance(entries, list):
            return {}
        options = {}
        for item in entries:
            if not isinstance(item, dict):
                continue
            value, name = item.get("sound_type"), item.get("sound_name")
            if value is not None and isinstance(name, str) and name:
                options[name] = str(value)
        return options

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
            capability = CAPABILITIES.get(field)
            if not self.capability_available(capability) or not self.supports(
                capability
            ):
                raise HomeAssistantError("NIU setting is currently unavailable")
            if field in {
                "low_power_mode_value",
                "tcs_value",
                "auto_parking_value",
            } and value not in ("0", "1"):
                raise HomeAssistantError("Invalid NIU switch value")
            if field == "foc_ebs_level_value" and value not in REGEN_OPTIONS.values():
                raise HomeAssistantError("Invalid regenerative braking level")
            if field == "auto_off_value" and (
                type(value) is not int or value not in self.auto_off_options().values()
            ):
                raise HomeAssistantError("Invalid Auto-Off duration")
            if (
                field == "cur_sound_type"
                and value not in self.sound_theme_options().values()
            ):
                raise HomeAssistantError("Invalid NIU sound theme")
            if field == "cur_sound_volume":
                number, maximum = finite_number(value), self.volume_max()
                if (
                    number is None
                    or not number.is_integer()
                    or maximum is None
                    or not 0 <= number <= maximum
                ):
                    raise HomeAssistantError("Volume is outside the reported range")
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
            if not self.capability_available(capability):
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
            and self.coordinator.capability_available(self.capability)
            and self.coordinator.supports(self.capability)
        )
