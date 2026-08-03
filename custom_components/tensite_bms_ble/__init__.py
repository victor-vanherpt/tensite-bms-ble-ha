"""The Tensite BMS (BLE) integration."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.loader import async_get_integration

from .const import (
    CARD_FILENAME,
    CARD_REGISTERED,
    CARD_URL,
    CONF_ADDRESS,
    CONF_HIDE_SENTINEL_TEMPERATURES,
    CONF_MEMBER_SERIALS,
    CONF_SERIAL,
    DEFAULT_HIDE_SENTINEL_TEMPERATURES,
    DOMAIN,
    OBSOLETE_OPTIONS,
    RETIRED_SENSOR_KEYS,
)
from .coordinator import TensiteClusterCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]

#: Bumped when stored options change shape; see async_migrate_entry.
CONFIG_VERSION = 3

type TensiteConfigEntry = ConfigEntry[TensiteClusterCoordinator]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Strip options that no longer exist.

    Version 1 could store a battery count and a detect-automatically flag; the
    bank master states its own size, so an override could only ever disagree
    with an authoritative answer. Version 2 could store a poll delay; there is
    no polling left to space out. Leaving either behind would just be litter
    that a later reader has to work out the meaning of.
    """
    if entry.version >= CONFIG_VERSION:
        return True

    # Work out what is going before replacing the options, not after: reading
    # entry.options afterwards reports what survived, so the message would
    # always say nothing was dropped.
    dropped = sorted(set(entry.options) & OBSOLETE_OPTIONS)
    options = {k: v for k, v in entry.options.items() if k not in OBSOLETE_OPTIONS}
    hass.config_entries.async_update_entry(
        entry, options=options, version=CONFIG_VERSION
    )
    _LOGGER.debug(
        "%s: migrated to version %d, dropped %s",
        entry.data.get(CONF_ADDRESS),
        CONFIG_VERSION,
        ", ".join(dropped) or "nothing",
    )
    return True


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the cell grid card and load it into the dashboard.

    Shipped with the integration rather than copied into ``/config/www`` and
    added as a Lovelace resource by hand. The card reads entities this
    integration creates, so the two belong on the same version: installing them
    separately is how you end up with a card expecting a sensor that the
    installed integration does not have.

    ``add_extra_js_url`` is the supported route for a custom integration to get
    a module loaded -- it is what its own docstring describes it for. The card
    is then available on every dashboard without a resource entry.

    The frontend is an *after* dependency, not a hard one: a Home Assistant
    with no dashboard at all is a real configuration, and it should still get
    its sensors. So the file is always served and only the automatic loading
    is skipped when there is nothing to load it into.
    """
    if hass.data.get(CARD_REGISTERED):
        return
    hass.data[CARD_REGISTERED] = True

    integration = await async_get_integration(hass, DOMAIN)
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                CARD_URL,
                str(Path(__file__).parent / "www" / CARD_FILENAME),
                # Revalidate rather than cache hard: the ?v= below busts the
                # cache on a version bump, but a card edited without one would
                # otherwise keep serving the old copy with no way to tell.
                cache_headers=False,
            )
        ]
    )
    if "frontend" not in hass.config.components:
        _LOGGER.debug("Serving %s; no frontend to load it into", CARD_URL)
        return
    frontend.add_extra_js_url(hass, f"{CARD_URL}?v={integration.version}")
    _LOGGER.debug("Registered %s (integration %s)", CARD_URL, integration.version)


async def async_setup_entry(hass: HomeAssistant, entry: TensiteConfigEntry) -> bool:
    """Set up one battery cluster from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    serial: str | None = entry.data.get(CONF_SERIAL)

    await _async_register_card(hass)

    coordinator = TensiteClusterCoordinator(
        hass=hass,
        address=address,
        serial=serial,
        hide_sentinel_temperatures=entry.options.get(
            CONF_HIDE_SENTINEL_TEMPERATURES, DEFAULT_HIDE_SENTINEL_TEMPERATURES
        ),
    )
    entry.runtime_data = coordinator
    # What a later update is compared against; see _async_update_listener.
    coordinator.options_snapshot = dict(entry.options)

    # Start the coordinator *before* forwarding platforms, so it is already
    # listening for advertisements by the time entities are created.
    # async_start registers the Bluetooth callbacks and returns the unsubscribe.
    #
    # No connection attempt here on purpose. One can only be made while Home
    # Assistant holds a connectable path for the address, which exists around
    # advertisements, and this gateway can be minutes away from its next one --
    # blocking setup on that would stall startup, and failing setup would mean
    # no entities at all for a device that is merely quiet. The stream opens on
    # the first advertisement and entities fill in as the bank reports.
    entry.async_on_unload(coordinator.async_start())
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(
        coordinator.async_add_listener(
            lambda: _async_record_membership(hass, entry, coordinator)
        )
    )

    _async_migrate_cells_onto_batteries(hass, entry)
    _async_remove_retired_entities(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Deliberately no ConfigEntryNotReady on "no advertisement seen yet".
    # These batteries advertise intermittently, and availability is evaluated
    # once at construction -- so failing setup on it would leave the entry
    # bouncing through retries even while the cluster is perfectly reachable.
    # Entities report unavailable until the first poll lands, which is the
    # honest state anyway.
    _LOGGER.debug(
        "%s: set up (serial=%s, data already available=%s)",
        address,
        serial,
        coordinator.available,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TensiteConfigEntry) -> bool:
    """Unload a config entry, releasing the gateway's connection slot.

    Disconnecting explicitly rather than leaving it to garbage collection: the
    gateway takes one central at a time, so a reload that kept the old link
    open would lock out the new coordinator that replaces it.
    """
    await entry.runtime_data.async_release()
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
def _async_remove_retired_entities(
    hass: HomeAssistant, entry: TensiteConfigEntry
) -> None:
    """Delete entities that nothing writes to any more.

    Poll interval, duration and consecutive failures described a
    connect-read-disconnect cycle that is gone; "cell voltages updated" was a
    timestamp that changed every few seconds and filled the logbook. Left
    alone they would sit in the registry as permanently unavailable entities
    and in dashboards as empty cards.

    Matched on the unique_id *suffix* rather than built from the address,
    because these are not all cluster-level -- the cells one is per battery and
    so carries a serial.
    """
    ent_reg = er.async_get(hass)
    removed = [
        entity.entity_id
        for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if any(entity.unique_id.endswith(f"_{key}") for key in RETIRED_SENSOR_KEYS)
    ]
    for entity_id in removed:
        ent_reg.async_remove(entity_id)
    if removed:
        _LOGGER.info(
            "Removed %d retired entities: %s", len(removed), ", ".join(removed)
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
