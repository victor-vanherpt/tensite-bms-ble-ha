"""Constants for the Tensite BMS (BLE) integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "tensite_bms_ble"

MANUFACTURER: Final = "Tensite"
MODEL_CLUSTER: Final = "Battery cluster"
MODEL_BATTERY: Final = "TS-L5000"
MODEL_CELL: Final = "LiFePO4 cell"

CONF_ADDRESS: Final = "address"
CONF_SERIAL: Final = "serial"

#: Serials of every battery found behind one gateway, learned on the first
#: successful poll. Every battery in a bank advertises independently, so this is
#: what lets sibling discovery flows be recognised as the same physical cluster
#: rather than offered as three more clusters to add.
CONF_MEMBER_SERIALS: Final = "member_serials"
CONF_EXPECTED_BATTERIES: Final = "expected_batteries"
CONF_SCAN_INTERVAL: Final = "scan_interval"

#: The gateway accepts one BLE central at a time and each poll holds the
#: connection for several seconds, so polling hard buys nothing and blocks
#: anything else that wants the slot. Cell voltages drift slowly.
DEFAULT_SCAN_INTERVAL: Final = 300  # seconds
MIN_SCAN_INTERVAL: Final = 60
MAX_SCAN_INTERVAL: Final = 3600

#: Upper bound on how long one poll may hold the connection. The gateway
#: round-robins the bank at roughly 5-6 s per battery, so this needs headroom.
LISTEN_TIMEOUT: Final = 60.0

#: BlueZ must resolve services on a first connection; HA requires >= 10 s.
CONNECT_TIMEOUT: Final = 20.0

#: How long the config flow waits for a battery to report while checking that a
#: candidate actually relays data. Shorter than a normal poll: one battery is
#: enough to prove the point, and a config flow should not hang.
PROBE_TIMEOUT: Final = 35.0

DEFAULT_EXPECTED_BATTERIES: Final = 0  # 0 = wait out the listen timeout
