"""Shared fixtures.

The coordinator is constructed directly rather than through a config entry.
Everything under test here is decision logic -- when to poll, when data is
stale, how the bank size is established -- and none of it needs a live
Bluetooth stack. Keeping the setup that small is deliberate: the bugs this
suite exists to catch were all arithmetic and state, and a heavyweight harness
would have made writing the tests unattractive enough not to bother.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from tensite_bms_ble import BatteryReading, ClusterReading

from custom_components.tensite_bms_ble.const import CONF_ADDRESS, CONF_SERIAL, DOMAIN

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load the integration under test."""
    return


@pytest.fixture(autouse=True)
def expected_lingering_timers() -> bool:
    """Allow the Bluetooth scanner's own expiry timer to outlive a test.

    ``enable_bluetooth`` starts a scanner that reschedules device expiry on a
    timer. It is Home Assistant's, not ours, and it is not a leak -- Home
    Assistant's own Bluetooth tests opt out of this assertion the same way.
    """
    return True


@pytest.fixture(autouse=True)
async def bluetooth(enable_bluetooth):
    """Set up Home Assistant's Bluetooth manager.

    The coordinator inherits from ActiveBluetoothDataUpdateCoordinator, whose
    constructor asks the manager whether the address is present -- so even
    building one needs the manager to exist.
    """
    return


@dataclass
class FakeServiceInfo:
    """Stand-in for BluetoothServiceInfoBleak.

    Only the attributes the coordinator actually reads are provided, so a test
    that starts depending on more of the real object fails loudly here rather
    than passing against a mock that answers everything.
    """

    address: str = "AA:BB:CC:DD:EE:FF"
    rssi: int = -50
    connectable: bool = True
    device: Any = None


@pytest.fixture
def service_info() -> FakeServiceInfo:
    return FakeServiceInfo()


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
    """A set-up config entry that has completed one poll.

    Shared because both the setup tests and the diagnostics tests need a
    loaded integration holding a realistic reading.
    """
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
