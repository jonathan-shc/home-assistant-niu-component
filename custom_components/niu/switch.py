"""NIU Auto Sleep configuration."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .const import DOMAIN
from .controls import NiuControlEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN]["_control_coordinators"][entry.entry_id]
    async_add_entities([NiuAutoSleepSwitch(coordinator)])


class NiuAutoSleepSwitch(NiuControlEntity, SwitchEntity):
    """Auto Sleep is a setting, not current sleep or low-SOC E-Save status."""

    _attr_icon = "mdi:sleep"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator):
        super().__init__(
            coordinator, "auto_sleep", "Auto Sleep", "switch", "sup_low_power_mode"
        )

    @property
    def is_on(self):
        value = (self.coordinator.data or {}).get("low_power_mode_value")
        if value not in (0, "0", 1, "1"):
            return None
        return value in (1, "1")

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_set_setting("low_power_mode_value", "1")

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_set_setting("low_power_mode_value", "0")
