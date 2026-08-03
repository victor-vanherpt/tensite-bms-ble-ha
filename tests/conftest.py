"""Shared fixtures.

The coordinator is constructed directly rather than through a config entry.
Everything under test here is decision logic -- when to connect, when data is
stale, how the bank size is established -- and none of it needs a live
Bluetooth stack. Keeping the setup that small is deliberate: the bugs this
suite exists to catch were all arithmetic and state, and a heavyweight harness
would have made writing the tests unattractive enough not to bother.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from tensite_bms_ble import BatteryReading, ClusterReading

from custom_components.tensite_bms_ble import CONFIG_VERSION
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




class FakeStream:
    """Stand-in for TensiteClusterStream.

    The real one owns a BLE connection and a reconnect loop; what the
    coordinator cares about is narrower -- that it starts, that it can be
    stopped, and that readings and connection changes arrive by callback. So
    this implements that surface and lets a test push either at will.
    """

    #: Every instance built during a test, newest last.
    instances: list["FakeStream"] = []
    #: Set by a test to make the next stream refuse to connect.
    next_start_error: Exception | None = None

    def __init__(self, device, *, serial=None, on_update, on_connection_change=None, **_):
        self.device = device
        self.serial = serial
        self.on_update = on_update
        self.on_connection_change = on_connection_change
        self.started = 0
        self.stopped = 0
        self.running = False
        self.connected = False
        self.reconnects = 0
        self.last_error: str | None = None
        self.start_error = FakeStream.next_start_error
        FakeStream.instances.append(self)

    @property
    def is_running(self) -> bool:
        return self.running

    @property
    def is_connected(self) -> bool:
        return self.connected

    def update_device(self, device) -> None:
        self.device = device

    async def async_start(self, timeout=None) -> None:
        self.started += 1
        if self.start_error is not None:
            raise self.start_error
        self.running = self.connected = True
        if self.on_connection_change:
            self.on_connection_change(True)

    async def async_stop(self) -> None:
        self.stopped += 1
        self.running = self.connected = False
        if self.on_connection_change:
            self.on_connection_change(False)

    def drop(self) -> None:
        """Lose the connection without stopping the stream."""
        self.connected = False
        if self.on_connection_change:
            self.on_connection_change(False)

    def push(self, reading: ClusterReading) -> None:
        self.on_update(reading)


@pytest.fixture
def fake_stream():
    """Patch the coordinator's stream class, exposing what it built."""
    FakeStream.instances.clear()
    FakeStream.next_start_error = None
    with patch(
        "custom_components.tensite_bms_ble.coordinator.TensiteClusterStream",
        FakeStream,
    ):
        yield FakeStream


@pytest.fixture
async def entry(hass, fake_stream) -> MockConfigEntry:
    """A set-up config entry holding a realistic reading.

    Shared because both the setup tests and the diagnostics tests need a
    loaded integration with data in it.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_SERIAL: MASTER},
        options={},
        version=CONFIG_VERSION,
    )
    config_entry.add_to_hass(hass)

    with patch(
        "custom_components.tensite_bms_ble.coordinator"
        ".async_ble_device_from_address",
        return_value=object(),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = config_entry.runtime_data
        info = FakeServiceInfo(address=ADDRESS, device=object())
        await coordinator._async_ensure_streaming(info)
        fake_stream.instances[-1].push(full_reading())
        await hass.async_block_till_done()

    return config_entry
