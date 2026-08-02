"""Setting up a config entry end to end.

This is the test that would have caught the two failures that reached a running
Home Assistant: a constant removed from const.py while another module still
imported it, and a coordinator method called that the base class did not have.
Neither is visible to py_compile, and neither shows up until something actually
imports every module and forwards the platforms.

The advertisement that would normally trigger a poll is skipped -- when to poll
is unit-tested separately -- and the poll is driven directly instead, so what
is under test here is that a reading turns into the right set of entities.
"""

from __future__ import annotations

import re

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import Platform
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from tensite_bms_ble import BatteryReading, ClusterReading

from custom_components.tensite_bms_ble.const import (
    CONF_ADDRESS,
    CONF_SERIAL,
    DOMAIN,
)

from .conftest import FakeServiceInfo

ADDRESS = "AA:BB:CC:DD:EE:FF"
MASTER = "1417725SLKOPGG08146"
SLAVE = "1417607SLKOPGG08051"

#: The captured fault: the app showed "Cell Faults -> Voltage Under: Fault".
FAULTY_BITS = bytes.fromhex("0000300000000000")


def full_reading() -> ClusterReading:
    """A bank reporting everything a real poll can return."""
    cells = tuple(range(3300, 3300 + 16))
    return ClusterReading(
        address=ADDRESS,
        master_serial=MASTER,
        batteries={
            MASTER: BatteryReading(
                serial=MASTER,
                position=0x01A0,
                cell_voltages_mv=cells,
                temperatures=(25, 26, 27, 28, -30, 29),
                alarm_bits=bytes(8),
                relay_routes=(1, 0, 0, 0),
                switch_routes=(3, 3, 3, 3),
                model="AB4850/100_2.0",
            ),
            SLAVE: BatteryReading(
                serial=SLAVE,
                position=0x0103,
                cell_voltages_mv=cells,
                temperatures=(25, 26, 27, 28),
                alarm_bits=FAULTY_BITS,
                relay_routes=(1, 0, 0, 0),
            ),
        },
    )


@pytest.fixture
async def entry(hass) -> MockConfigEntry:
    """A set-up config entry that has completed one poll."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_SERIAL: MASTER},
        options={},
    )
    config_entry.add_to_hass(hass)

    client = AsyncMock()
    client.async_read.return_value = full_reading()
    with (
        patch(
            "custom_components.tensite_bms_ble.coordinator.TensiteClusterClient",
            return_value=client,
        ),
        patch(
            "custom_components.tensite_bms_ble.coordinator"
            ".async_ble_device_from_address",
            return_value=object(),
        ),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = config_entry.runtime_data
        info = FakeServiceInfo(address=ADDRESS, device=object())
        coordinator.data = await coordinator._async_poll_cluster(info)
        coordinator.async_update_listeners()
        await hass.async_block_till_done()

    return config_entry


class TestSetup:
    async def test_entry_loads(self, hass, entry):
        assert entry.state is not None
        assert entry.runtime_data is not None

    async def test_every_platform_is_forwarded(self, hass, entry):
        """A platform that fails to import leaves its entities missing."""
        registry = er.async_get(hass)
        entities = er.async_entries_for_config_entry(registry, entry.entry_id)
        domains = {e.domain for e in entities}
        assert Platform.SENSOR in domains
        assert Platform.BINARY_SENSOR in domains

    async def test_unload_is_clean(self, hass, entry):
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


class TestDeviceTree:
    """Cluster -> battery, with cells flattened onto the battery."""

    async def test_a_device_per_battery_plus_the_cluster(self, hass, entry):
        registry = dr.async_get(hass)
        devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
        assert len(devices) == 3  # cluster + two batteries

    async def test_batteries_hang_off_the_cluster(self, hass, entry):
        registry = dr.async_get(hass)
        battery = registry.async_get_device(identifiers={(DOMAIN, MASTER)})
        cluster = registry.async_get_device(
            identifiers={(DOMAIN, f"cluster_{ADDRESS}")}
        )
        assert battery is not None and cluster is not None
        assert battery.via_device_id == cluster.id

    async def test_no_device_per_cell(self, hass, entry):
        """Cells belong beside the pack temperatures, not a click below."""
        registry = dr.async_get(hass)
        devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
        assert not any(
            any("_cell_" in ident for _, ident in d.identifiers) for d in devices
        )


class TestEntitiesCreated:
    """The entity set a reading should produce."""

    def entities(self, hass, entry, needle: str) -> list[str]:
        registry = er.async_get(hass)
        return [
            e.entity_id
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
            if needle in e.unique_id
        ]

    def matching(self, hass, entry, pattern: str) -> list[str]:
        registry = er.async_get(hass)
        return [
            e.entity_id
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
            if re.search(pattern, e.unique_id)
        ]

    async def test_sixteen_cells_per_battery(self, hass, entry):
        """Anchored, because "_cell_" also matches min/max/delta/sum voltage."""
        assert len(self.matching(hass, entry, r"_cell_\d+_voltage$")) == 32

    async def test_temperature_sensors_match_what_each_pack_reports(
        self, hass, entry
    ):
        """Six on the master, four on the slave -- not a fixed number."""
        master = self.entities(hass, entry, f"{MASTER}_pack_temperature")
        slave = self.entities(hass, entry, f"{SLAVE}_pack_temperature")
        assert len(master) == 6
        assert len(slave) == 4

    async def test_an_alarm_entity_per_slot_per_battery(self, hass, entry):
        assert len(self.entities(hass, entry, "_alarm_")) == 58  # 29 x 2

    async def test_a_relay_entity_per_reported_route(self, hass, entry):
        assert len(self.entities(hass, entry, "_relay_")) == 8  # 4 x 2

    async def test_system_status_on_the_cluster_and_each_battery(self, hass, entry):
        assert len(self.entities(hass, entry, "_system_status")) == 3

    async def test_battery_count_sensors_exist_and_the_generic_one_does_not(
        self, hass, entry
    ):
        assert len(self.entities(hass, entry, "_batteries_expected")) == 1
        assert len(self.entities(hass, entry, "_batteries_reported")) == 1
        assert self.entities(hass, entry, "_battery_count") == []


class TestStates:
    """A few states, to prove the entities are wired to the reading."""

    async def test_a_faulted_battery_reports_its_alarm(self, hass, entry):
        registry = er.async_get(hass)
        entity_id = next(
            e.entity_id
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
            if e.unique_id == f"{SLAVE}_active_alarms"
        )
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "1"
        assert state.attributes["alarms"] == ["Cell Voltage Under"]

    async def test_a_healthy_battery_reports_none(self, hass, entry):
        registry = er.async_get(hass)
        entity_id = next(
            e.entity_id
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
            if e.unique_id == f"{MASTER}_active_alarms"
        )
        assert hass.states.get(entity_id).state == "0"

    async def test_the_detected_battery_count_reaches_its_sensor(self, hass, entry):
        registry = er.async_get(hass)
        entity_id = next(
            e.entity_id
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
            if e.unique_id.endswith("_batteries_expected")
        )
        assert hass.states.get(entity_id).state == "2"
