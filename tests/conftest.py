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

import pytest

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
