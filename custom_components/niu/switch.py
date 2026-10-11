"""NIU Auto Sleep configuration."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .const import DOMAIN
from .controls import NiuControlEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN]["_control_coordinators"][entry.entry_id]
    async_add_entities(
        [
            NiuAutoSleepSwitch(coordinator),
            NiuBooleanSettingSwitch(
                coordinator,
                "traction_control",
                "Traction Control",
                "tcs_value",
                "sup_tcs",
                "mdi:car-traction-control",
            ),
            NiuBooleanSettingSwitch(
                coordinator,
                "auto_hold",
                "Auto Hold",
                "auto_parking_value",
                "sup_auto_parking",
                "mdi:pause-circle-outline",
            ),
        ]
    )


class NiuBooleanSettingSwitch(NiuControlEntity, SwitchEntity):
    """Read and write an observed boolean scooter setting."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, key, name, field, capability, icon):
        super().__init__(coordinator, key, name, "switch", capability)
        self.field = field
        self._attr_icon = icon

    @property
    def is_on(self):
        value = (self.coordinator.data or {}).get(self.field)
        if value not in (0, "0", 1, "1"):
            return None
        return value in (1, "1")

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_set_setting(self.field, "1")

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_set_setting(self.field, "0")


class NiuAutoSleepSwitch(NiuBooleanSettingSwitch):
    """Preserve the existing Auto Sleep entity's identity."""

    def __init__(self, coordinator):
        super().__init__(
            coordinator,
            "auto_sleep",
            "Auto Sleep",
            "low_power_mode_value",
            "sup_low_power_mode",
            "mdi:sleep",
        )
