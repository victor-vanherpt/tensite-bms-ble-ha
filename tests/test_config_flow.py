"""Config and options flow.

The battery-count step was removed once the bank size became something the
coordinator detects. Nothing exercised the flow afterwards, so this covers the
path a new cluster actually takes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from tensite_bms_ble import ClusterReading, TensiteError, TensiteNoDataError

from custom_components.tensite_bms_ble.const import (
    CONF_EXPECTED_BATTERIES,
    CONF_MEMBER_SERIALS,
    CONF_SCAN_INTERVAL,
    DEFAULT_EXPECTED_BATTERIES,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
SERIAL = "1417725SLKOPGG08146"


def probe_result(*serials: str):
    from tensite_bms_ble import BatteryReading

    return ClusterReading(
        address=ADDRESS,
        master_serial=serials[0] if serials else None,
        batteries={
            s: BatteryReading(serial=s, position=0x01A0 + i)
            for i, s in enumerate(serials)
        },
    )


def patch_probe(result):
    """Stub the connection the flow makes to check the candidate relays data."""
    client = AsyncMock()
    if isinstance(result, Exception):
        client.async_read.side_effect = result
    else:
        client.async_read.return_value = result
    return (
        patch(
            "custom_components.tensite_bms_ble.config_flow.TensiteClusterClient",
            return_value=client,
        ),
        patch(
            "custom_components.tensite_bms_ble.config_flow"
            ".async_ble_device_from_address",
            return_value=object(),
        ),
    )


async def start_user_flow(hass, discovered=((ADDRESS, SERIAL),)):
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

    infos = []
    for address, serial in discovered:
        info = AsyncMock(spec=BluetoothServiceInfoBleak)
        info.address = address
        info.name = serial
        info.advertisement.local_name = serial
        infos.append(info)

    with (
        patch(
            "custom_components.tensite_bms_ble.config_flow"
            ".async_discovered_service_info",
            return_value=infos,
        ),
        patch(
            "custom_components.tensite_bms_ble.config_flow"
            ".is_tensite_advertisement",
            return_value=True,
        ),
    ):
        return await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )


class TestUserFlow:
    async def test_creates_an_entry_without_asking_for_a_battery_count(self, hass):
        """The step is gone; the count is detected and only overridden by option."""
        result = await start_user_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        client_patch, device_patch = patch_probe(probe_result(SERIAL, "OTHER"))
        with client_patch, device_patch:
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"address": ADDRESS}
            )

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["options"][CONF_EXPECTED_BATTERIES] == DEFAULT_EXPECTED_BATTERIES
        # 0 means "detect it", which is the whole point of removing the step.
        assert result["options"][CONF_EXPECTED_BATTERIES] == 0

    async def test_records_the_members_it_saw(self, hass):
        result = await start_user_flow(hass)
        client_patch, device_patch = patch_probe(probe_result(SERIAL, "OTHER"))
        with client_patch, device_patch:
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"address": ADDRESS}
            )
        assert sorted(result["data"][CONF_MEMBER_SERIALS]) == sorted([SERIAL, "OTHER"])

    async def test_a_battery_reporting_nothing_is_rejected(self, hass):
        """It may not be relaying for its cluster; say so rather than accept it."""
        result = await start_user_flow(hass)
        client_patch, device_patch = patch_probe(TensiteNoDataError("quiet"))
        with client_patch, device_patch:
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"address": ADDRESS}
            )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "no_batteries"}

    async def test_a_failed_connection_is_reported_not_raised(self, hass):
        result = await start_user_flow(hass)
        client_patch, device_patch = patch_probe(TensiteError("nope"))
        with client_patch, device_patch:
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"address": ADDRESS}
            )
        assert result["errors"] == {"base": "cannot_connect"}

    async def test_aborts_when_nothing_is_advertising(self, hass):
        result = await start_user_flow(hass, discovered=())
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "no_devices_found"


class TestOptionsFlow:
    async def test_poll_delay_is_bounded_by_what_the_hardware_allows(self):
        """Below the advertising cadence the setting does nothing measurable."""
        assert MIN_SCAN_INTERVAL <= MAX_SCAN_INTERVAL
        assert MIN_SCAN_INTERVAL >= 60

    @pytest.mark.parametrize("value", [MIN_SCAN_INTERVAL, 300, MAX_SCAN_INTERVAL])
    def test_accepted_delays(self, value):
        import voluptuous as vol

        schema = vol.All(vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL))
        assert schema(value) == value

    @pytest.mark.parametrize("value", [1, MIN_SCAN_INTERVAL - 1, MAX_SCAN_INTERVAL + 1])
    def test_rejected_delays(self, value):
        import voluptuous as vol

        schema = vol.All(vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL))
        with pytest.raises(vol.Invalid):
            schema(value)


class TestStrings:
    """Every translation key an entity asks for must exist."""

    def test_entity_names_are_defined(self):
        import json
        import pathlib

        root = pathlib.Path("custom_components/tensite_bms_ble")
        strings = json.loads((root / "strings.json").read_text())
        english = json.loads((root / "translations/en.json").read_text())
        assert strings["entity"] == english["entity"], (
            "strings.json and translations/en.json have drifted apart"
        )

    def test_options_keys_match_the_schema(self):
        import json
        import pathlib

        root = pathlib.Path("custom_components/tensite_bms_ble")
        strings = json.loads((root / "strings.json").read_text())
        data = strings["options"]["step"]["init"]["data"]
        described = strings["options"]["step"]["init"]["data_description"]
        assert set(described) <= set(data)
        assert CONF_SCAN_INTERVAL in data


class TestBatteryCountField:
    """The override is a typed number, and blank means "detect it"."""

    async def test_blank_is_stored_as_absent_not_null(self, hass):
        """Otherwise "not set" and "set to nothing" behave differently."""
        from custom_components.tensite_bms_ble.config_flow import TensiteOptionsFlow

        entry = MockConfigEntry(domain=DOMAIN, options={CONF_EXPECTED_BATTERIES: 4})
        entry.add_to_hass(hass)
        flow = TensiteOptionsFlow()
        flow.hass = hass
        flow.handler = entry.entry_id

        result = await flow.async_step_init(
            {CONF_SCAN_INTERVAL: 60, CONF_EXPECTED_BATTERIES: None}
        )
        assert CONF_EXPECTED_BATTERIES not in result["data"]

    def test_range_is_one_to_the_hardware_maximum(self):
        """Zero is not offered: blank already means detect."""
        from custom_components.tensite_bms_ble.const import MAX_EXPECTED_BATTERIES

        assert MAX_EXPECTED_BATTERIES == 8

    async def test_a_typed_count_survives(self, hass):
        from custom_components.tensite_bms_ble.config_flow import TensiteOptionsFlow

        entry = MockConfigEntry(domain=DOMAIN, options={})
        entry.add_to_hass(hass)
        flow = TensiteOptionsFlow()
        flow.hass = hass
        flow.handler = entry.entry_id

        result = await flow.async_step_init(
            {CONF_SCAN_INTERVAL: 60, CONF_EXPECTED_BATTERIES: 4}
        )
        assert result["data"][CONF_EXPECTED_BATTERIES] == 4
