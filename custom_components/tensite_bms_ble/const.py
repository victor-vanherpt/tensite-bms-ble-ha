"""Constants for the Tensite BMS (BLE) integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "tensite_bms_ble"

MANUFACTURER: Final = "Tensite"
MODEL_CLUSTER: Final = "Battery cluster"
MODEL_BATTERY: Final = "TS-L5000"
MODEL_CELL: Final = "LiFePO4 cell"

#: The Lovelace cards shipped with the integration -- see _async_register_card.
#: One file defines both the per-battery grid and the cluster card, so they
#: cannot be installed at different versions.
CARD_FILENAME: Final = "tensite-cards.js"
#: Everything this integration serves. Resources are matched on the prefix, not
#: the full URL, so renaming the file updates the existing resource instead of
#: leaving a dead one behind next to the new one.
CARD_URL_PREFIX: Final = f"/{DOMAIN}/"
CARD_URL: Final = f"{CARD_URL_PREFIX}{CARD_FILENAME}"
#: hass.data flag: the card is registered once per Home Assistant run, not once
#: per config entry, since a second bank would re-register the same path.
CARD_REGISTERED: Final = f"{DOMAIN}_card_registered"

CONF_ADDRESS: Final = "address"
CONF_SERIAL: Final = "serial"

#: Serials of every battery found behind one gateway, learned on the first
#: successful poll. Every battery in a bank advertises independently, so this is
#: what lets sibling discovery flows be recognised as the same physical cluster
#: rather than offered as three more clusters to add.
CONF_MEMBER_SERIALS: Final = "member_serials"
#: When true, pack temperature sensors reporting a fault sentinel (-50 C or
#: -30 C) report unavailable instead of the sentinel value. Off by default so
#: the integration mirrors the vendor app, which displays the sentinel.
CONF_HIDE_SENTINEL_TEMPERATURES: Final = "hide_sentinel_temperatures"
DEFAULT_HIDE_SENTINEL_TEMPERATURES: Final = False

#: Floor on the gap between entity updates.
#:
#: Not a poll interval -- nothing is being asked for. The bank pushes frames on
#: its own cadence and a burst can carry several batteries at once, so updates
#: are coalesced to this and the most recent state wins.
#:
#: Set to the measured cell cadence rather than lower. Each battery emits cell
#: frames every ~5.1 s, so a faster push mostly re-reports numbers that have not
#: changed. Measured live, per ten minutes of recorder rows for this
#: integration's entities: 205 under five-minute polling, 4742 at a 2 s
#: throttle, 3838 at 5 s. The modest difference between the last two is the
#: point -- most of that volume is real change arriving at the hardware's own
#: rate, not throttle churn, so lowering this buys noise and raising it would
#: start dropping readings. Latency to a genuinely new value is at most one
#: interval: a pending update fires the moment the throttle expires.
UPDATE_THROTTLE: Final = 5.0

#: BlueZ must resolve services on a first connection; HA requires >= 10 s.
CONNECT_TIMEOUT: Final = 20.0

#: How long the config flow waits for a battery to report while checking that a
#: candidate actually relays data. Shorter than a normal poll: one battery is
#: enough to prove the point, and a config flow should not hang.
PROBE_TIMEOUT: Final = 35.0

#: Options that older versions stored and this one does not, stripped by
#: async_migrate_entry so they do not sit in the entry as litter.
#:
#: * ``expected_batteries`` / ``auto_battery_count`` (version 2): the bank
#:   master states its own size, so an override could only ever disagree with
#:   an authoritative answer.
#: * ``scan_interval`` (version 3): there is no polling left to space out. The
#:   connection is held open and the bank pushes; how often readings arrive is
#:   the hardware's business, not a setting.
OBSOLETE_OPTIONS: Final = frozenset(
    {"expected_batteries", "auto_battery_count", "scan_interval"}
)

#: Entity keys nothing writes to any more, removed from the registry on setup
#: so they do not linger as permanently unavailable entities. Matched on the
#: unique_id suffix, so both cluster- and battery-level keys work.
#:
#: The first three went with polling. ``cells_updated_at`` was a timestamp, and
#: Home Assistant treats a sensor without a unit, state class or numeric device
#: class as *not continuous* -- so each of its ~585 changes an hour became a
#: logbook line. It is replaced by ``cells_age``, which measures the same thing
#: in seconds and is filtered out of the logbook like every other measurement.
RETIRED_SENSOR_KEYS: Final = (
    "poll_interval",
    "poll_duration",
    "consecutive_failures",
    "cells_updated_at",
)

#: How long without frames before entities report unavailable.
#:
#: Generous next to the ~5 s cadence, and deliberately so: the recovery path is
#: what sets it. A dropped connection may have to wait for the gateway's next
#: advertisement to reopen, and this hardware advertises every 245-300 s, so a
#: shorter window would blank every entity on a single routine drop.
MIN_STALE_WINDOW: Final = 300.0  # seconds
