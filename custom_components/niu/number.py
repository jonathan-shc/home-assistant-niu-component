"""NIU charging power with provider-reported limits."""

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .controls import NiuControlEntity, finite_number


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN]["_control_coordinators"][entry.entry_id]
    async_add_entities([NiuChargingPowerNumber(coordinator)])


class NiuChargingPowerNumber(NiuControlEntity, NumberEntity):
    _attr_icon = "mdi:ev-station"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator):
        super().__init__(
            coordinator,
            "charging_power",
            "Charging Power",
            "number",
            "sup_charge_power_set",
        )

    @property
    def available(self):
        return super().available and self.coordinator.power_bounds() is not None

    @property
    def native_value(self):
        value = (self.coordinator.data or {}).get("charge_power_set_value")
        return (
            finite_number(value.get("set_value")) if isinstance(value, dict) else None
        )

    @property
    def native_min_value(self):
        bounds = self.coordinator.power_bounds()
        return bounds[0] if bounds else 0

    @property
    def native_max_value(self):
        bounds = self.coordinator.power_bounds()
        return bounds[1] if bounds else 0

    @property
    def extra_state_attributes(self):
        return {
            "reported_setting_w": self.native_value,
            "range_available": self.coordinator.power_bounds() is not None,
        }

    async def async_set_native_value(self, value):
        number = finite_number(value)
        if number is None or not number.is_integer():
            raise HomeAssistantError("Charging power must be a whole number of watts")
        await self.coordinator.async_set_setting(
            "charge_power_set_value", str(int(number))
        )
