"""Control isolation, safe bounds, provider readback and explicit commands."""

import importlib
import types
import unittest
from unittest.mock import Mock

from tests.unit.ha_stubs import clear_niu_modules, install_homeassistant_stubs


class FakeHass:
    async def async_add_executor_job(self, target, *args):
        return target(*args)


class ControlsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        install_homeassistant_stubs()
        clear_niu_modules()
        self.module = importlib.import_module("custom_components.niu.controls")
        self.switch = importlib.import_module("custom_components.niu.switch")
        self.select = importlib.import_module("custom_components.niu.select")
        self.number = importlib.import_module("custom_components.niu.number")
        self.button = importlib.import_module("custom_components.niu.button")
        self.api_module = importlib.import_module("custom_components.niu.api")
        self.settings = {
            "sup_low_power_mode": "1",
            "low_power_mode_value": "1",
            "sup_charging_limit": "1",
            "charging_limit_value": "100",
            "sup_charge_power_set": "1",
            "charge_power_set_value": {
                "set_value": "840",
                "charge_power_range": ["200", "1000"],
            },
        }
        self.api = types.SimpleNamespace(
            sn="TEST",
            sensor_prefix="MQi",
            get_settings=Mock(side_effect=lambda: dict(self.settings)),
            set_setting=Mock(),
            open_seat=Mock(),
        )
        self.hass = FakeHass()
        self.coordinator = self.module.NiuControlCoordinator(self.hass, None, self.api)

    async def ready(self):
        await self.coordinator.async_config_entry_first_refresh()

    async def test_setup_and_polling_never_actuate(self):
        await self.ready()
        self.api.set_setting.assert_not_called()
        self.api.open_seat.assert_not_called()

    async def test_auto_sleep_reads_app_setting_and_does_not_claim_requested_state(
        self,
    ):
        await self.ready()
        switch = self.switch.NiuAutoSleepSwitch(self.coordinator)
        self.assertTrue(switch.is_on)
        await switch.async_turn_off()
        self.api.set_setting.assert_called_once_with("low_power_mode_value", "0")
        self.assertTrue(switch.is_on)  # Provider still reports on, no optimistic off.
        self.settings["low_power_mode_value"] = "0"
        self.coordinator.data = await self.coordinator._async_update_data()
        self.assertFalse(switch.is_on)

    async def test_charge_limit_options_and_request(self):
        await self.ready()
        select = self.select.NiuChargingLimitSelect(self.coordinator)
        self.assertEqual(select._attr_options, ["80%", "85%", "90%", "95%", "100%"])
        await select.async_select_option("85%")
        self.api.set_setting.assert_called_once_with("charging_limit_value", "85")
        self.assertEqual(select.current_option, "100%")
        with self.assertRaises(self.module.HomeAssistantError):
            await select.async_select_option("60%")

    async def test_power_bounds_and_request(self):
        await self.ready()
        entity = self.number.NiuChargingPowerNumber(self.coordinator)
        self.assertTrue(entity.available)
        self.assertEqual(
            (entity.native_min_value, entity.native_max_value), (200, 1000)
        )
        self.assertEqual(entity.native_value, 840)
        await entity.async_set_native_value(600)
        self.api.set_setting.assert_called_once_with("charge_power_set_value", "600")
        for value in (0, 1001, float("nan"), float("inf"), 600.5, True):
            with self.assertRaises(self.module.HomeAssistantError):
                await entity.async_set_native_value(value)
        self.assertEqual(self.api.set_setting.call_count, 1)

    async def test_missing_or_malformed_power_range_blocks_writes(self):
        await self.ready()
        entity = self.number.NiuChargingPowerNumber(self.coordinator)
        for bounds in (
            None,
            [],
            [1000, 200],
            ["bad", 1000],
            [True, 1000],
            [0, float("inf")],
        ):
            self.settings["charge_power_set_value"]["charge_power_range"] = bounds
            self.coordinator.data = await self.coordinator._async_update_data()
            self.assertFalse(entity.available)
            with self.assertRaises(self.module.HomeAssistantError):
                await entity.async_set_native_value(600)
        self.api.set_setting.assert_not_called()

    async def test_unsupported_settings_unavailable_and_blocked(self):
        await self.ready()
        self.coordinator.data["sup_low_power_mode"] = "0"
        self.assertFalse(self.switch.NiuAutoSleepSwitch(self.coordinator).available)
        with self.assertRaises(self.module.HomeAssistantError):
            await self.coordinator.async_set_setting("low_power_mode_value", "0")
        self.api.set_setting.assert_not_called()

    async def test_settings_outage_does_not_fail_setup_and_recovers(self):
        self.api.get_settings.side_effect = self.api_module.NiuConnectionError(
            "offline"
        )
        await self.ready()
        self.assertFalse(self.coordinator.settings_available)
        self.api.get_settings.side_effect = lambda: dict(self.settings)
        self.coordinator.data = await self.coordinator._async_update_data()
        self.assertTrue(self.switch.NiuAutoSleepSwitch(self.coordinator).available)

    async def test_rejected_write_preserves_provider_state(self):
        await self.ready()
        self.api.set_setting.side_effect = self.api_module.NiuResponseError("rejected")
        with self.assertRaises(self.module.HomeAssistantError):
            await self.coordinator.async_set_setting("low_power_mode_value", "0")
        self.assertEqual(self.coordinator.data["low_power_mode_value"], "1")

    async def test_accepted_write_failed_readback_becomes_unavailable(self):
        await self.ready()
        self.api.get_settings.side_effect = self.api_module.NiuConnectionError(
            "offline"
        )
        with self.assertRaisesRegex(self.module.HomeAssistantError, "accepted"):
            await self.coordinator.async_set_setting("low_power_mode_value", "0")
        self.assertFalse(self.switch.NiuAutoSleepSwitch(self.coordinator).available)

    async def test_seat_button_issues_one_command_only_when_pressed(self):
        main = types.SimpleNamespace(api=self.api, last_update_success=True)
        button = self.button.NiuOpenSeatButton(main)
        button.hass = self.hass
        self.api.open_seat.assert_not_called()
        await button.async_press()
        self.api.open_seat.assert_called_once()
        self.api.open_seat.side_effect = self.api_module.NiuResponseError("rejected")
        with self.assertRaises(self.module.HomeAssistantError):
            await button.async_press()

    async def test_stable_distinct_entity_ids_on_existing_device(self):
        await self.ready()
        entity = self.switch.NiuAutoSleepSwitch(self.coordinator)
        self.assertEqual(entity._attr_unique_id, "switch.niu_scooter_TEST_auto_sleep")
        self.assertEqual(
            entity._attr_device_info["identifiers"], {("niu", "Niu E-scooter")}
        )
