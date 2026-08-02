"""The Tensite BMS (BLE) integration."""

from __future__ import annotations

import logging
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    CONF_ADDRESS,
    CONF_AUTO_BATTERY_COUNT,
    CONF_EXPECTED_BATTERIES,
    CONF_HIDE_SENTINEL_TEMPERATURES,
    CONF_MEMBER_SERIALS,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    DEFAULT_AUTO_BATTERY_COUNT,
    DEFAULT_EXPECTED_BATTERIES,
    DEFAULT_HIDE_SENTINEL_TEMPERATURES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import TensiteClusterCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

type TensiteConfigEntry = ConfigEntry[TensiteClusterCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: TensiteConfigEntry) -> bool:
    """Set up one battery cluster from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    serial: str | None = entry.data.get(CONF_SERIAL)

    # No adoption of a count from previously seen members here. That predates
    # the roster: the master states the bank size outright, so writing a
    # remembered count into the options would only override the authoritative
    # answer with a stale guess.
    expected = entry.options.get(CONF_EXPECTED_BATTERIES, DEFAULT_EXPECTED_BATTERIES)

    coordinator = TensiteClusterCoordinator(
        hass=hass,
        address=address,
        serial=serial,
        poll_delay=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        expected_batteries=expected,
        hide_sentinel_temperatures=entry.options.get(
            CONF_HIDE_SENTINEL_TEMPERATURES, DEFAULT_HIDE_SENTINEL_TEMPERATURES
        ),
        auto_battery_count=entry.options.get(
            CONF_AUTO_BATTERY_COUNT, DEFAULT_AUTO_BATTERY_COUNT
        ),
    )
    entry.runtime_data = coordinator
    # What a later update is compared against; see _async_update_listener.
    coordinator.options_snapshot = dict(entry.options)

    # Start the coordinator *before* forwarding platforms, so it is already
    # listening for advertisements by the time entities are created.
    # async_start registers the Bluetooth callbacks and returns the unsubscribe.
    #
    # No first poll here on purpose. Polls are driven by advertisements, and
    # this gateway can be minutes away from its next one -- blocking setup on
    # that would stall startup, and failing setup would mean no entities at all
    # for a device that is merely quiet. Entities appear as the bank reports.
    entry.async_on_unload(coordinator.async_start())
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(
        coordinator.async_add_listener(
            lambda: _async_record_membership(hass, entry, coordinator)
        )
    )
    entry.async_on_unload(
        coordinator.async_add_listener(
            lambda: _async_sync_battery_count(hass, entry, coordinator)
        )
    )

    _async_migrate_cells_onto_batteries(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Deliberately no ConfigEntryNotReady on "no advertisement seen yet".
    # These batteries advertise intermittently, and availability is evaluated
    # once at construction -- so failing setup on it would leave the entry
    # bouncing through retries even while the cluster is perfectly reachable.
    # Entities report unavailable until the first poll lands, which is the
    # honest state anyway.
    _LOGGER.debug(
        "%s: set up (serial=%s, interval=%ss, advertisement seen=%s)",
        address,
        serial,
        coordinator.poll_delay,
        coordinator.available,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TensiteConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: TensiteConfigEntry
) -> None:
    """Reload when *options* change, so the new poll delay takes effect.

    The guard matters. Home Assistant fires this listener for any update to the
    entry, including the ones this integration makes itself: recording which
    batteries a gateway reported writes entry.data, which fired this, which
    reloaded the integration and threw away the reading that had just been
    polled. The bank was then re-enumerated, membership recorded again, and so
    on. Only an options change should cost a reload.
    """
    coordinator = entry.runtime_data
    if entry.options == coordinator.options_snapshot:
        return
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_record_membership(
    hass: HomeAssistant,
    entry: TensiteConfigEntry,
    coordinator: TensiteClusterCoordinator,
) -> None:
    """Record which batteries this gateway reports, for diagnostics.

    Purely informational. Sibling discoveries are deliberately *not* withdrawn:
    every battery in a bank advertises independently, and connecting to a
    specific one is a legitimate thing to want to do when debugging. Which
    entries to keep is the user's call, made with the Ignore button.
    """
    reading = coordinator.data
    if reading is None or not reading.batteries:
        return

    members = sorted(reading.batteries)
    data = {**entry.data}
    changed = False

    if data.get(CONF_MEMBER_SERIALS) != members:
        data[CONF_MEMBER_SERIALS] = members
        changed = True
    if reading.master_serial and data.get(CONF_SERIAL) != reading.master_serial:
        data[CONF_SERIAL] = reading.master_serial
        changed = True

    if changed:
        hass.config_entries.async_update_entry(entry, data=data)


@callback
def _async_sync_battery_count(
    hass: HomeAssistant,
    entry: TensiteConfigEntry,
    coordinator: TensiteClusterCoordinator,
) -> None:
    """Write the detected bank size into the option, while detection is on.

    Home Assistant option forms are static, so the checkbox cannot grey the
    number field out. Keeping the field in step with what the bank reports is
    the next best thing: it always shows what the integration is actually
    using, and switching detection off leaves a sensible starting value rather
    than a stale one.

    The snapshot is updated *before* the entry, because updating an entry fires
    the update listener, and that listener reloads whenever the options differ
    from the snapshot. Writing one without the other would reload the
    integration on the first poll and throw the reading away -- which is
    exactly the loop that _async_update_listener exists to prevent.
    """
    if not coordinator.auto_battery_count:
        return
    detected = coordinator.detected_batteries
    if not detected or entry.options.get(CONF_EXPECTED_BATTERIES) == detected:
        return

    options = {**entry.options, CONF_EXPECTED_BATTERIES: detected}
    coordinator.options_snapshot = dict(options)
    hass.config_entries.async_update_entry(entry, options=options)
    _LOGGER.debug(
        "%s: detected %d batteries, recorded in options",
        entry.data[CONF_ADDRESS],
        detected,
    )


_CELL_UNIQUE_ID = re.compile(r"^(?P<serial>.+)_cell_\d+_voltage$")


@callback
def _async_migrate_cells_onto_batteries(
    hass: HomeAssistant, entry: TensiteConfigEntry
) -> None:
    """Move cell voltage entities onto their battery device.

    Cells used to get a device each, which buried all sixteen voltages one
    click below the battery. They belong beside the pack temperature sensors
    instead. Re-registering them cannot wait for fresh cell data -- the BMS
    does not always broadcast cell frames -- so existing entities are moved in
    the registry and the emptied cell devices removed.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    moved = 0
    for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        match = _CELL_UNIQUE_ID.match(entity.unique_id)
        if not match:
            continue
        battery = dev_reg.async_get_device(
            identifiers={(DOMAIN, match.group("serial"))}
        )
        if battery is None or entity.device_id == battery.id:
            continue
        ent_reg.async_update_entity(entity.entity_id, device_id=battery.id)
        moved += 1

    removed = 0
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        if not any(
            domain == DOMAIN and "_cell_" in ident
            for domain, ident in device.identifiers
        ):
            continue
        if er.async_entries_for_device(
            ent_reg, device.id, include_disabled_entities=True
        ):
            continue
        dev_reg.async_remove_device(device.id)
        removed += 1

    if moved or removed:
        _LOGGER.info(
            "Moved %d cell entities onto their battery device and removed %d "
            "now-empty cell devices",
            moved,
            removed,
        )
