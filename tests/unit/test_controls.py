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
        self.settings.update(
            {
                "sup_tcs": "1",
                "tcs_value": "1",
                "sup_auto_parking": "1",
                "auto_parking_value": "1",
                "sup_foc_ebs_level": "1",
                "foc_ebs_level_value": "10",
            }
        )
        self.sound = {
            "cur_sound_volume": 5,
            "sup_sound_volume_max": 16,
            "sup_personality_sound": True,
            "cur_sound_type": "1",
            "sup_sound_type_infos": [
                {"sound_type": 1, "sound_name": "Standard"},
                {"sound_type": 2, "sound_name": "Techno"},
                {"sound_type": 3, "sound_name": "Gaming"},
            ],
        }
        self.auto_off = {
            "sup_auto_off_setting": True,
            "auto_off_time_value": 300,
            "list": [{"value": value} for value in (300, 600, 1800, 3600, 28800)],
        }
        self.api = types.SimpleNamespace(
            sn="TEST",
            sensor_prefix="MQi",
            get_settings=Mock(side_effect=lambda: dict(self.settings)),
            get_sound_settings=Mock(side_effect=lambda: dict(self.sound)),
            get_auto_off_settings=Mock(side_effect=lambda: dict(self.auto_off)),
            send_command=Mock(),
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

    async def test_refresh_button_only_reads_status(self):
        from unittest.mock import AsyncMock

        main = types.SimpleNamespace(
            api=self.api, last_update_success=True, async_refresh_status=AsyncMock()
        )
        button = self.button.NiuRefreshStatusButton(main)
        self.assertEqual(button._attr_unique_id, "button.niu_scooter_TEST_refresh_status")
        await button.async_press()
        main.async_refresh_status.assert_awaited_once()
        self.api.send_command.assert_not_called()
        self.api.open_seat.assert_not_called()

    async def test_stable_distinct_entity_ids_on_existing_device(self):
        await self.ready()
        entity = self.switch.NiuAutoSleepSwitch(self.coordinator)
        self.assertEqual(entity._attr_unique_id, "switch.niu_scooter_TEST_auto_sleep")
        self.assertEqual(
            entity._attr_device_info["identifiers"], {("niu", "Niu E-scooter")}
        )

    async def test_regenerative_braking_mapping_and_rejection(self):
        await self.ready()
        entity = self.select.NiuRegenerativeBrakingSelect(self.coordinator)
        self.assertEqual(entity.current_option, "Sterk")
        for option, value in self.module.REGEN_OPTIONS.items():
            await entity.async_select_option(option)
            self.api.set_setting.assert_called_with("foc_ebs_level_value", value)
        self.assertEqual(entity.current_option, "Sterk")
        with self.assertRaises(self.module.HomeAssistantError):
            await entity.async_select_option("onbekend")
        with self.assertRaises(self.module.HomeAssistantError):
            await self.coordinator.async_set_setting("foc_ebs_level_value", "4")

    async def test_tcs_and_auto_hold_use_exact_fields(self):
        await self.ready()
        for field, capability in (
            ("tcs_value", "sup_tcs"),
            ("auto_parking_value", "sup_auto_parking"),
        ):
            entity = self.switch.NiuBooleanSettingSwitch(
                self.coordinator, field, field, field, capability, "mdi:check"
            )
            self.assertTrue(entity.is_on)
            await entity.async_turn_off()
            self.api.set_setting.assert_called_with(field, "0")
            self.settings[field] = "0"
            self.coordinator.data = await self.coordinator._async_update_data()
            self.assertFalse(entity.is_on)
            await entity.async_turn_on()
            self.api.set_setting.assert_called_with(field, "1")
            with self.assertRaises(self.module.HomeAssistantError):
                await self.coordinator.async_set_setting(field, "2")

    async def test_auto_off_reads_time_field_and_posts_integer_seconds(self):
        await self.ready()
        entity = self.select.NiuAutoOffSelect(self.coordinator)
        self.assertEqual(
            entity.options, ["5 minuten", "10 minuten", "30 minuten", "1 uur", "8 uur"]
        )
        self.assertEqual(entity.current_option, "5 minuten")
        await entity.async_select_option("10 minuten")
        self.api.set_setting.assert_called_once_with("auto_off_value", 600)
        for value in ("600", 600.0, True, 0, 60):
            with self.assertRaises(self.module.HomeAssistantError):
                await self.coordinator.async_set_setting("auto_off_value", value)
        self.auto_off["list"] = [{"value": 600}]
        self.coordinator.data = await self.coordinator._async_update_data()
        self.assertEqual(entity.options, ["10 minuten"])
        with self.assertRaises(self.module.HomeAssistantError):
            await entity.async_select_option("8 uur")

    async def test_volume_uses_reported_max_and_readback(self):
        await self.ready()
        entity = self.number.NiuVolumeNumber(self.coordinator)
        self.assertEqual((entity.native_value, entity.native_max_value), (5, 16))
        self.assertTrue(entity.available)
        for value in (0, 7, 16):
            await entity.async_set_native_value(value)
            self.api.set_setting.assert_called_with("cur_sound_volume", str(value))
        self.assertEqual(entity.native_value, 5)
        for value in (-1, 17, 5.5, float("nan"), float("inf"), True):
            with self.assertRaises(self.module.HomeAssistantError):
                await entity.async_set_native_value(value)
        self.sound["sup_sound_volume_max"] = None
        self.coordinator.data = await self.coordinator._async_update_data()
        self.assertFalse(entity.available)

    async def test_sound_theme_uses_provider_names_and_codes(self):
        await self.ready()
        entity = self.select.NiuSoundThemeSelect(self.coordinator)
        self.assertEqual(entity.options, ["Standard", "Techno", "Gaming"])
        self.assertEqual(entity.current_option, "Standard")
        await entity.async_select_option("Gaming")
        self.api.set_setting.assert_called_once_with("cur_sound_type", "3")
        with self.assertRaises(self.module.HomeAssistantError):
            await entity.async_select_option("onbekend")

    async def test_optional_source_outages_do_not_disable_other_settings(self):
        await self.ready()
        self.api.get_sound_settings.side_effect = self.api_module.NiuConnectionError(
            "offline"
        )
        self.coordinator.data = await self.coordinator._async_update_data()
        self.assertFalse(self.number.NiuVolumeNumber(self.coordinator).available)
        self.assertTrue(self.switch.NiuAutoSleepSwitch(self.coordinator).available)
        self.assertTrue(self.select.NiuAutoOffSelect(self.coordinator).available)
        await self.coordinator.async_set_setting("tcs_value", "0")
        self.api.get_sound_settings.side_effect = lambda: dict(self.sound)
        self.api.get_settings.side_effect = self.api_module.NiuConnectionError(
            "offline"
        )
        self.coordinator.data = await self.coordinator._async_update_data()
        self.assertFalse(self.switch.NiuAutoSleepSwitch(self.coordinator).available)
        self.assertTrue(self.number.NiuVolumeNumber(self.coordinator).available)
        await self.coordinator.async_set_setting("cur_sound_volume", "7")

    async def test_optional_readback_failure_is_reported_for_its_source(self):
        await self.ready()
        self.api.get_auto_off_settings.side_effect = self.api_module.NiuConnectionError(
            "offline"
        )
        with self.assertRaisesRegex(self.module.HomeAssistantError, "accepted"):
            await self.coordinator.async_set_setting("auto_off_value", 600)
        self.assertTrue(self.switch.NiuAutoSleepSwitch(self.coordinator).available)
        self.assertFalse(self.select.NiuAutoOffSelect(self.coordinator).available)

    async def test_rate_limit_stops_remaining_reads_and_backs_off(self):
        self.api.get_settings.side_effect = self.api_module.NiuRateLimitError(900)
        await self.ready()
        self.assertEqual(self.coordinator.update_interval.total_seconds(), 900)
        self.api.get_sound_settings.assert_not_called()
        self.api.get_auto_off_settings.assert_not_called()
        self.assertFalse(self.number.NiuVolumeNumber(self.coordinator).available)

    async def test_command_buttons_send_only_the_selected_command(self):
        main = types.SimpleNamespace(api=self.api, last_update_success=True)
        for command in ("acc_on", "acc_off", "fortification_on", "fortification_off"):
            entity = self.button.NiuCommandButton(
                main, command, command, command, "mdi:power"
            )
            entity.hass = self.hass
            before = self.api.send_command.call_count
            await entity.async_press()
            self.assertEqual(self.api.send_command.call_count, before + 1)
            self.api.send_command.assert_called_with(command)

    async def test_malformed_provider_options_cannot_be_written(self):
        self.auto_off["list"] = [
            None,
            {},
            {"value": True},
            {"value": "bad"},
            {"value": 0},
        ]
        self.sound["sup_sound_type_infos"] = [
            None,
            {},
            {"sound_type": "1", "sound_name": ""},
        ]
        await self.ready()
        self.assertFalse(self.select.NiuAutoOffSelect(self.coordinator).available)
        self.assertFalse(self.select.NiuSoundThemeSelect(self.coordinator).available)
        with self.assertRaises(self.module.HomeAssistantError):
            await self.coordinator.async_set_setting("cur_sound_type", "1")
        self.api.set_setting.assert_not_called()

    async def test_settings_writes_and_readback_are_serialized(self):
        import asyncio

        events = []
        original_executor = self.hass.async_add_executor_job

        async def executor(target, *args):
            events.append((target, args))
            await asyncio.sleep(0)
            return await original_executor(target, *args)

        await self.ready()
        self.hass.async_add_executor_job = executor
        await asyncio.gather(
            self.coordinator.async_set_setting("tcs_value", "0"),
            self.coordinator.async_set_setting("auto_parking_value", "0"),
        )
        self.assertEqual(
            [event[0] for event in events],
            [
                self.api.set_setting,
                self.api.get_settings,
                self.api.get_sound_settings,
                self.api.get_auto_off_settings,
                self.api.set_setting,
                self.api.get_settings,
                self.api.get_sound_settings,
                self.api.get_auto_off_settings,
            ],
        )
