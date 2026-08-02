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
#: Storage key only. The setting is a *delay* between polls, not a period,
#: and is labelled that way in the UI -- but the key stays as-is so existing
#: configurations keep their value.
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

#: Floor on the poll delay.
#:
#: Equal to the default, because below it the setting does nothing measurable.
#: Polls are triggered by advertisements and this gateway advertises every
#: 245-300 seconds, so any delay shorter than that cadence means the same
#: thing: "poll on every advertisement". A 10 second option was offered
#: briefly; it was indistinguishable from 60 in practice and only implied a
#: control that does not exist.
MIN_SCAN_INTERVAL: Final = 60

#: An advertisement is the only chance to poll, and they arrive on a jittery
#: cadence rather than an exact grid. Requiring the full delay to have elapsed
#: means one landing a few seconds early is thrown away and the next chance is
#: a whole cadence later -- 289 s after the last poll, with a 300 s delay, costs
#: another five minutes for the sake of 11 s.
#:
#: So accept one this close to due. A fraction so it scales with the delay.
POLL_GRACE_FRACTION: Final = 0.1

#: Upper bound on how long one poll may hold the connection. The gateway
#: round-robins the bank at roughly 5-6 s per battery, so this needs headroom.
LISTEN_TIMEOUT: Final = 60.0

#: BlueZ must resolve services on a first connection; HA requires >= 10 s.
CONNECT_TIMEOUT: Final = 20.0

#: How long the config flow waits for a battery to report while checking that a
#: candidate actually relays data. Shorter than a normal poll: one battery is
#: enough to prove the point, and a config flow should not hang.
PROBE_TIMEOUT: Final = 35.0

#: 0 means "work it out", which is the normal case. A non-zero value forces
#: the count and is only needed when the automatic answer is wrong.
#: Largest bank the hardware supports, per the product documentation.
#: Bounds the override so a typo cannot make every poll wait out the full
#: listening window for batteries that cannot exist.
MAX_EXPECTED_BATTERIES: Final = 8

DEFAULT_EXPECTED_BATTERIES: Final = 0

#: How often to run a poll that ignores the expected count and waits out the
#: full listening window.
#:
#: This is what makes auto-detection safe. Learning the count from ordinary
#: polls alone is a trap: the gateway answers its batteries in rotation, so an
#: early poll easily catches three of four, and if that number then became the
#: early-exit condition every later poll would stop at three and the fourth
#: would never be found. A periodic unbounded poll is the escape hatch -- it
#: cannot exit early, so it sees the whole bank, and it is also the only way a
#: battery that has been *removed* stops being expected.
FULL_SCAN_INTERVAL: Final = 3600.0  # seconds

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
