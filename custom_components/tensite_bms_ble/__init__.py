"""The Tensite BMS (BLE) integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_ADDRESS,
    CONF_EXPECTED_BATTERIES,
    CONF_MEMBER_SERIALS,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    DEFAULT_EXPECTED_BATTERIES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import TensiteClusterCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type TensiteConfigEntry = ConfigEntry[TensiteClusterCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: TensiteConfigEntry) -> bool:
    """Set up one battery cluster from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    serial: str | None = entry.data.get(CONF_SERIAL)

    coordinator = TensiteClusterCoordinator(
        hass=hass,
        address=address,
        serial=serial,
        scan_interval=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        expected_batteries=entry.options.get(
            CONF_EXPECTED_BATTERIES, DEFAULT_EXPECTED_BATTERIES
        ),
    )
    entry.runtime_data = coordinator

    # Start the coordinator *before* forwarding platforms, so it is already
    # listening for advertisements by the time entities are created.
    # async_start registers the Bluetooth callbacks and returns the unsubscribe.
    entry.async_on_unload(coordinator.async_start())
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(
        coordinator.async_add_listener(
            lambda: _async_record_membership(hass, entry, coordinator)
        )
    )

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
