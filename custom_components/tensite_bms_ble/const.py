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

#: When true, pack temperature sensors reporting a fault sentinel (-50 C or
#: -30 C) report unavailable instead of the sentinel value. Off by default so
#: the integration mirrors the vendor app, which displays the sentinel.
CONF_HIDE_SENTINEL_TEMPERATURES: Final = "hide_sentinel_temperatures"
DEFAULT_HIDE_SENTINEL_TEMPERATURES: Final = False

#: A minute is about where the useful resolution runs out: a pack drifts a few
#: millivolts over that, so sampling faster mostly records noise. It also keeps
#: the gateway busy only ~10% of the time, which leaves its single connection
#: slot free for the vendor app.
#:
#: Lower is allowed down to MIN_SCAN_INTERVAL, which is what you want when
#: watching something happen rather than logging it.
DEFAULT_SCAN_INTERVAL: Final = 60  # seconds
MAX_SCAN_INTERVAL: Final = 3600

#: Floor on the polling interval.
#:
#: Polling is timed rather than advertisement-driven (see the coordinator), so
#: this is a real floor and not a wish: a 10 s setting really does connect every
#: 10 s. A poll takes 4-7 s in practice, so at the floor the gateway is busy
#: most of the time.
#:
#: That matters because the *gateway* accepts one BLE central at a time -- a
#: dedicated adapter on the Home Assistant side does not change this. Polling
#: near the floor will make the vendor app struggle to connect. It is allowed
#: because that is a legitimate thing to choose, not because it is free.
#:
#: A poll never starts while another is running, so a setting below the time a
#: poll actually takes degrades to "as fast as possible" rather than stacking
#: connections.
MIN_SCAN_INTERVAL: Final = 10

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

#: How many polling intervals may pass without data before entities are
#: reported unavailable. A single missed poll is routine on BLE; several in a
#: row is a real problem worth surfacing.
STALE_AFTER_INTERVALS: Final = 3

#: ...but never sooner than this, however short the interval. Scaling purely
#: with the interval means a fast poller declares itself dead absurdly quickly:
#: at the 10 s floor, three missed polls is half a minute, and a BLE device can
#: easily be unreachable that long while being perfectly healthy. The data is
#: no more stale at a short interval than a long one -- only the sampling rate
#: changed -- so the window has a floor of its own.
MIN_STALE_WINDOW: Final = 300.0  # seconds
