"""Local NIU historical trip selection."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .controls import CHARGE_LIMIT_VALUES, REGEN_OPTIONS, NiuControlEntity

from .const import DOMAIN
from .entity import NiuTripEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Add one selector per scooter without changing its existing config."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    controls = hass.data[DOMAIN]["_control_coordinators"][entry.entry_id]
    async_add_entities(
        [
            NiuTripSelect(coordinator),
            NiuChargingLimitSelect(controls),
            NiuRegenerativeBrakingSelect(controls),
            NiuAutoOffSelect(controls),
            NiuSoundThemeSelect(controls),
        ]
    )


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


class NiuChargingLimitSelect(NiuControlEntity, SelectEntity):
    """Charge ceiling offered by the official NIU app."""

    _attr_icon = "mdi:battery-charging-high"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = [f"{value}%" for value in CHARGE_LIMIT_VALUES]

    def __init__(self, coordinator):
        super().__init__(
            coordinator,
            "charging_limit",
            "Charging Limit",
            "select",
            "sup_charging_limit",
        )

    @property
    def current_option(self):
        value = str((self.coordinator.data or {}).get("charging_limit_value"))
        return f"{value}%" if value in CHARGE_LIMIT_VALUES else None

    async def async_select_option(self, option):
        value = option.removesuffix("%")
        await self.coordinator.async_set_setting("charging_limit_value", value)


class NiuRegenerativeBrakingSelect(NiuControlEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:car-brake-retarder"
    _attr_options = list(REGEN_OPTIONS)

    def __init__(self, coordinator):
        super().__init__(
            coordinator,
            "regenerative_braking",
            "Regenerative Braking",
            "select",
            "sup_foc_ebs_level",
        )

    @property
    def current_option(self):
        value = str((self.coordinator.data or {}).get("foc_ebs_level_value"))
        return next(
            (name for name, code in REGEN_OPTIONS.items() if code == value), None
        )

    async def async_select_option(self, option):
        if option not in REGEN_OPTIONS:
            raise HomeAssistantError("Invalid regenerative braking option")
        await self.coordinator.async_set_setting(
            "foc_ebs_level_value", REGEN_OPTIONS[option]
        )


class NiuAutoOffSelect(NiuControlEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:timer-off-outline"

    def __init__(self, coordinator):
        super().__init__(
            coordinator, "auto_off", "Auto-Off", "select", "sup_auto_off_setting"
        )

    @property
    def available(self):
        return super().available and bool(self.coordinator.auto_off_options())

    @property
    def options(self):
        return list(self.coordinator.auto_off_options())

    @property
    def current_option(self):
        value = (
            (self.coordinator.data or {})
            .get("_auto_off", {})
            .get("auto_off_time_value")
        )
        return next(
            (
                name
                for name, seconds in self.coordinator.auto_off_options().items()
                if str(seconds) == str(value)
            ),
            None,
        )

    async def async_select_option(self, option):
        options = self.coordinator.auto_off_options()
        if option not in options:
            raise HomeAssistantError("Invalid Auto-Off option")
        await self.coordinator.async_set_setting("auto_off_value", options[option])


class NiuSoundThemeSelect(NiuControlEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:music-box-multiple"

    def __init__(self, coordinator):
        super().__init__(
            coordinator, "sound_theme", "Sound Theme", "select", "sup_personality_sound"
        )

    @property
    def available(self):
        return super().available and bool(self.coordinator.sound_theme_options())

    @property
    def options(self):
        return list(self.coordinator.sound_theme_options())

    @property
    def current_option(self):
        value = str(
            (self.coordinator.data or {}).get("_sound", {}).get("cur_sound_type")
        )
        return next(
            (
                name
                for name, code in self.coordinator.sound_theme_options().items()
                if code == value
            ),
            None,
        )

    async def async_select_option(self, option):
        options = self.coordinator.sound_theme_options()
        if option not in options:
            raise HomeAssistantError("Invalid NIU sound theme")
        await self.coordinator.async_set_setting("cur_sound_type", options[option])
