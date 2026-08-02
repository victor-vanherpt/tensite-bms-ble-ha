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
    CONF_EXPECTED_BATTERIES,
    CONF_HIDE_SENTINEL_TEMPERATURES,
    CONF_MEMBER_SERIALS,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
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

    # Entries created before the battery count was asked for at setup time
    # adopt the bank size already learned from polling, so they get the same
    # early-exit behaviour without the user having to go and configure it.
    expected = entry.options.get(CONF_EXPECTED_BATTERIES, DEFAULT_EXPECTED_BATTERIES)
    if not expected and (members := entry.data.get(CONF_MEMBER_SERIALS)):
        expected = len(members)
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_EXPECTED_BATTERIES: expected}
        )
        _LOGGER.info(
            "%s: adopted a battery count of %d from previously seen members",
            address,
            expected,
        )

    coordinator = TensiteClusterCoordinator(
        hass=hass,
        address=address,
        serial=serial,
        scan_interval=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        expected_batteries=expected,
        hide_sentinel_temperatures=entry.options.get(
            CONF_HIDE_SENTINEL_TEMPERATURES, DEFAULT_HIDE_SENTINEL_TEMPERATURES
        ),
    )
    entry.runtime_data = coordinator

    # Do the first poll before forwarding platforms, so entities are created
    # against real data and can size themselves (cell counts, temperature
    # sensor counts, relay routes) on the first pass.
    #
    # async_refresh rather than async_config_entry_first_refresh: the latter
    # raises ConfigEntryNotReady when a poll fails, which for a Bluetooth
    # device that is merely out of range for a moment means setup retry loops
    # and no entities at all. A failed first poll here just means the timer
    # picks it up.
    # Register with the Bluetooth manager first. This does not trigger
    # polls -- those are timed -- but without it Home Assistant stops
    # treating the address as one needing connectable access, and
    # connections fail with bogus "out of connection slots" errors.
    entry.async_on_unload(coordinator.async_start())
    await coordinator.async_refresh()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(
        coordinator.async_add_listener(
            lambda: _async_record_membership(hass, entry, coordinator)
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
        coordinator.scan_interval,
        coordinator.available,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TensiteConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: TensiteConfigEntry
) -> None:
    """Reload when options change, so the new poll interval takes effect."""
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
