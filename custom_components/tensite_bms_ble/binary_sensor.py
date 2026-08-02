"""Fault reporting.

The BMS sends an eight-byte fault bitfield (frame type 0x01) that is entirely
zero while nothing is wrong -- 356 samples across four healthy batteries, and
non-zero on the one faulty unit captured.

That field packs 29 named alarms as 2-bit severities. The layout was read out
of the vendor app's own parser rather than guessed from captures; see
``tensite_bms_ble.alarms`` for the derivation. Each battery therefore gets:

* one "Alarm ..." sensor per named alarm, matching the app's alarm page row
  for row.

* one "System Status" sensor per battery and one for the bank, reading Ok or
  Fault.

System Status carries ``BinarySensorDeviceClass.PROBLEM``, which is what makes
it join Home Assistant's problem groupings and alerting. That device class also
defines the polarity -- ``on`` means a problem is detected, ``off`` means
normal -- which is the conventional mapping and the reason inverting it would
be wrong: anything consuming binary sensors generically reads ``on`` as the
state worth acting on.

A device class supplies its own state wording, so these render OK/Problem and
custom state translations would be ignored.

All of these are enabled by default so they take part in Home Assistant's
problem groupings and can be triggered on directly. That is 29 alarms per
battery, nearly all of which read OK indefinitely -- ``sensor.*_active_alarms``
exists as the compact view over the same data, carrying a count as its state
and the firing alarms as attributes.
"""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from tensite_bms_ble import ALARM_SLOTS, AlarmSlot, BatteryReading
from tensite_bms_ble.const import ROUTE_ACTIVE

from . import TensiteConfigEntry
from .coordinator import TensiteClusterCoordinator
from .entity import TensiteEntity, battery_device_info, cluster_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TensiteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the cluster fault sensor now, per-battery ones as they appear."""
    coordinator = entry.runtime_data
    async_add_entities([TensiteClusterStatus(coordinator)])

    known: set[str] = set()
    #: Relay routes already given entities, per battery. Counted rather than
    #: flagged because how many routes a battery reports is only known once its
    #: relay frame arrives, which can be a later poll than the one that first
    #: revealed the battery.
    known_routes: dict[str, int] = {}

    @callback
    def _add_new_batteries() -> None:
        reading = coordinator.data
        if reading is None:
            return
        entities: list[BinarySensorEntity] = []

        for serial in reading.batteries:
            if serial not in known:
                known.add(serial)
                entities.append(TensiteBatteryStatus(coordinator, serial))
                entities.extend(
                    TensiteBatteryAlarm(coordinator, serial, slot)
                    for slot in ALARM_SLOTS
                )

        for serial, battery in reading.batteries.items():
            seen = known_routes.get(serial, 0)
            total = len(battery.relay_routes)
            if total > seen:
                known_routes[serial] = total
                entities.extend(
                    TensiteRelayRoute(coordinator, serial, index)
                    for index in range(seen, total)
                )

        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_batteries))
    _add_new_batteries()


class TensiteRelayRoute(TensiteEntity, BinarySensorEntity):
    """One relay route, matching a toggle on the vendor app's Relay tab.

    Reported as a plain on/off rather than a switch: the app's page builds no
    command message at all, so this is a status readout and there is nothing to
    write back. Disabled by default -- every battery reports the same four
    routes and they have not moved in any capture.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: TensiteClusterCoordinator, serial: str, index: int
    ) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._index = index
        self._attr_name = f"Relay {index + 1}"
        self._attr_unique_id = f"{serial}_relay_{index + 1}"
        reading = coordinator.data
        battery = reading.batteries.get(serial) if reading else None
        self._attr_device_info = battery_device_info(
            coordinator,
            serial,
            battery.position_label if battery else "",
            battery.model if battery else None,
        )

    @property
    def _routes(self) -> tuple[int, ...]:
        reading = self.coordinator.data
        battery = reading.batteries.get(self._serial) if reading else None
        return battery.relay_routes if battery else ()

    @property
    def available(self) -> bool:
        return self.coordinator.has_fresh_data and self._index < len(self._routes)

    @property
    def is_on(self) -> bool | None:
        routes = self._routes
        if self._index >= len(routes):
            return None
        return routes[self._index] == ROUTE_ACTIVE

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        routes = self._routes
        # The raw 0-3 value: only 1 is established as "active", so keep the
        # underlying number visible rather than flattening 0 and 3 together.
        return {
            "value": routes[self._index] if self._index < len(routes) else None
        }


class TensiteBatteryAlarm(TensiteEntity, BinarySensorEntity):
    """One named alarm from the app's alarm page, for one battery."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    # Not EntityCategory.DIAGNOSTIC: that category is for technical detail that
    # does not affect the primary function (signal strength, frame counters). A
    # fault alarm is a primary signal, and diagnostic entities are filed away
    # from the problem groupings these are meant to feed.
    #
    # Enabled by default so every alarm joins those groupings and can be
    # triggered on without being switched on by hand. That is 29 per battery,
    # nearly all reading OK forever; "Active Alarms" stays the compact view.

    def __init__(
        self,
        coordinator: TensiteClusterCoordinator,
        serial: str,
        slot: AlarmSlot,
    ) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._slot = slot
        # Prefixed so all 29 sort and search together, and so an alarm is
        # recognisable as one out of context -- "Temperature Over" on its own
        # reads like a measurement.
        self._attr_name = f"Alarm {slot.name}"
        self._attr_unique_id = f"{serial}_alarm_{slot.key}"
        reading = coordinator.data
        battery = reading.batteries.get(serial) if reading else None
        self._attr_device_info = battery_device_info(
            coordinator,
            serial,
            battery.position_label if battery else "",
            battery.model if battery else None,
        )

    @property
    def _battery(self) -> BatteryReading | None:
        reading = self.coordinator.data
        return reading.batteries.get(self._serial) if reading else None

    @property
    def available(self) -> bool:
        battery = self._battery
        return (
            self.coordinator.has_fresh_data
            and battery is not None
            and battery.alarm_bits is not None
        )

    @property
    def is_on(self) -> bool | None:
        battery = self._battery
        if battery is None or battery.alarm_bits is None:
            return None
        return bool(battery.alarms[self._slot.key])

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        battery = self._battery
        level = (
            battery.alarms[self._slot.key]
            if battery and battery.alarm_bits is not None
            else None
        )
        return {
            # The app renders 1/2/3 as "Level1/2/3 Fault" and says no more
            # about what separates them, so the number is passed through.
            "level": int(level) if level is not None else None,
            "category": self._slot.category,
        }


class TensiteClusterStatus(TensiteEntity, BinarySensorEntity):
    """Bank status: on when any battery reports a fault."""

    _attr_translation_key = "system_status"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: TensiteClusterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_system_status"
        self._attr_device_info = cluster_device_info(coordinator)

    @property
    def is_on(self) -> bool | None:
        reading = self.coordinator.data
        return reading.has_fault if reading else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        reading = self.coordinator.data
        if reading is None:
            return {"faulted_batteries": []}
        return {
            "faulted_batteries": list(reading.faulted_batteries),
            # Which battery, without opening each one.
            "active_alarms": {
                serial: [slot.name for slot, _ in battery.active_alarms]
                for serial, battery in sorted(reading.batteries.items())
                if battery.active_alarms
            },
        }


class TensiteBatteryStatus(TensiteEntity, BinarySensorEntity):
    """Status for one battery. See the module docstring on the off/on mapping."""

    _attr_translation_key = "system_status"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self, coordinator: TensiteClusterCoordinator, serial: str
    ) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._attr_unique_id = f"{serial}_system_status"
        reading = coordinator.data
        battery = reading.batteries.get(serial) if reading else None
        self._attr_device_info = battery_device_info(
            coordinator,
            serial,
            battery.position_label if battery else "",
            battery.model if battery else None,
        )

    @property
    def _battery(self) -> BatteryReading | None:
        reading = self.coordinator.data
        return reading.batteries.get(self._serial) if reading else None

    @property
    def available(self) -> bool:
        battery = self._battery
        return (
            self.coordinator.has_fresh_data
            and battery is not None
            and battery.has_fault is not None
        )

    @property
    def is_on(self) -> bool | None:
        battery = self._battery
        return battery.has_fault if battery else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        battery = self._battery
        if battery is None:
            return {}
        return {
            "active_alarms": [slot.name for slot, _ in battery.active_alarms],
            "alarm_level": (
                int(battery.alarm_level)
                if battery.alarm_level is not None
                else None
            ),
            # If a future firmware sets a bit outside the 29 the vendor app
            # parses, our table is incomplete and this is where it shows.
            "alarm_bits": battery.alarm_bits_hex,
            "unmapped_alarm_bits": battery.unmapped_alarm_bits or None,
        }
