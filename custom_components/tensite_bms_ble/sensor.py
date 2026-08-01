"""Sensor entities across the cluster / battery / cell hierarchy.

Batteries are not known until the first successful poll, so battery and cell
entities are added dynamically as the bank reports in.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfElectricPotential
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from tensite_bms_ble import BatteryReading, ClusterReading

from . import TensiteConfigEntry
from .const import DOMAIN
from .coordinator import TensiteClusterCoordinator
from .entity import (
    TensiteEntity,
    battery_device_info,
    cell_device_info,
    cluster_device_info,
)

_LOGGER = logging.getLogger(__name__)

VOLT = UnitOfElectricPotential.VOLT
MILLIVOLT = UnitOfElectricPotential.MILLIVOLT


# --- Cluster-level ------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ClusterSensorDescription(SensorEntityDescription):
    value_fn: Callable[[ClusterReading], float | int | None]


CLUSTER_SENSORS: tuple[ClusterSensorDescription, ...] = (
    ClusterSensorDescription(
        key="battery_count",
        translation_key="battery_count",
        value_fn=lambda r: r.battery_count,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ClusterSensorDescription(
        key="stack_voltage",
        translation_key="stack_voltage",
        value_fn=lambda r: r.total_voltage,
        native_unit_of_measurement=VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    ClusterSensorDescription(
        key="min_cell_voltage",
        translation_key="min_cell_voltage",
        value_fn=lambda r: r.min_cell_mv,
        native_unit_of_measurement=MILLIVOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ClusterSensorDescription(
        key="max_cell_voltage",
        translation_key="max_cell_voltage",
        value_fn=lambda r: r.max_cell_mv,
        native_unit_of_measurement=MILLIVOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ClusterSensorDescription(
        key="cell_delta",
        translation_key="cell_delta",
        value_fn=lambda r: r.delta_mv,
        native_unit_of_measurement=MILLIVOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


# --- Battery-level ------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class BatterySensorDescription(SensorEntityDescription):
    value_fn: Callable[[BatteryReading], float | int | None]


BATTERY_SENSORS: tuple[BatterySensorDescription, ...] = (
    BatterySensorDescription(
        key="stack_voltage",
        translation_key="stack_voltage",
        value_fn=lambda b: b.total_voltage,
        native_unit_of_measurement=VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    BatterySensorDescription(
        key="min_cell_voltage",
        translation_key="min_cell_voltage",
        value_fn=lambda b: b.min_cell_mv,
        native_unit_of_measurement=MILLIVOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BatterySensorDescription(
        key="max_cell_voltage",
        translation_key="max_cell_voltage",
        value_fn=lambda b: b.max_cell_mv,
        native_unit_of_measurement=MILLIVOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BatterySensorDescription(
        key="cell_delta",
        translation_key="cell_delta",
        value_fn=lambda b: b.delta_mv,
        native_unit_of_measurement=MILLIVOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BatterySensorDescription(
        key="cell_count",
        translation_key="cell_count",
        value_fn=lambda b: b.cell_count,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TensiteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up cluster sensors now, battery and cell sensors as they appear."""
    coordinator = entry.runtime_data

    async_add_entities(
        TensiteClusterSensor(coordinator, description)
        for description in CLUSTER_SENSORS
    )
    async_add_entities([TensiteRssiSensor(coordinator)])

    known: set[str] = set()

    @callback
    def _add_new_batteries() -> None:
        reading = coordinator.data
        if reading is None:
            return
        new = [s for s in reading.batteries if s not in known]
        if not new:
            return
        entities: list[TensiteEntity] = []
        for serial in new:
            known.add(serial)
            battery = reading.batteries[serial]
            entities.extend(
                TensiteBatterySensor(coordinator, serial, description)
                for description in BATTERY_SENSORS
            )
            entities.extend(
                TensiteCellSensor(coordinator, serial, index)
                for index in range(1, battery.cell_count + 1)
            )
            _LOGGER.debug(
                "Adding battery %s (%s) with %d cells",
                serial,
                battery.position_label,
                battery.cell_count,
            )
        async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_batteries))
    _add_new_batteries()  # cover a poll that landed before platform setup


class TensiteClusterSensor(TensiteEntity, SensorEntity):
    """A sensor describing the cluster as a whole."""

    entity_description: ClusterSensorDescription

    def __init__(
        self,
        coordinator: TensiteClusterCoordinator,
        description: ClusterSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = cluster_device_info(coordinator)

    @property
    def native_value(self) -> float | int | None:
        if (reading := self.coordinator.data) is None:
            return None
        return self.entity_description.value_fn(reading)


class TensiteRssiSensor(TensiteEntity, SensorEntity):
    """Signal strength of the gateway's advertisements."""

    _attr_translation_key = "rssi"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = "dBm"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: TensiteClusterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_rssi"
        self._attr_device_info = cluster_device_info(coordinator)

    @property
    def available(self) -> bool:
        # Depends only on advertisements, so it stays useful even when a poll
        # has never succeeded -- which is exactly when it is worth reading.
        return self.coordinator.available

    @property
    def native_value(self) -> int | None:
        return self.coordinator.rssi


class TensiteBatterySensor(TensiteEntity, SensorEntity):
    """A sensor describing one battery."""

    entity_description: BatterySensorDescription

    def __init__(
        self,
        coordinator: TensiteClusterCoordinator,
        serial: str,
        description: BatterySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"
        position = self._battery.position_label if self._battery else ""
        self._attr_device_info = battery_device_info(coordinator, serial, position)

    @property
    def _battery(self) -> BatteryReading | None:
        if (reading := self.coordinator.data) is None:
            return None
        return reading.batteries.get(self._serial)

    @property
    def available(self) -> bool:
        return self.coordinator.available and self._battery is not None

    @property
    def native_value(self) -> float | int | None:
        if (battery := self._battery) is None:
            return None
        return self.entity_description.value_fn(battery)


class TensiteCellSensor(TensiteEntity, SensorEntity):
    """Voltage of one cell, attached to that cell's own device."""

    _attr_translation_key = "cell_voltage"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = VOLT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3

    def __init__(
        self, coordinator: TensiteClusterCoordinator, serial: str, index: int
    ) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._index = index
        self._attr_unique_id = f"{serial}_cell_{index:02d}_voltage"
        self._attr_device_info = cell_device_info(serial, index)

    @property
    def _battery(self) -> BatteryReading | None:
        if (reading := self.coordinator.data) is None:
            return None
        return reading.batteries.get(self._serial)

    @property
    def available(self) -> bool:
        battery = self._battery
        return (
            self.coordinator.available
            and battery is not None
            and len(battery.cell_voltages_mv) >= self._index
        )

    @property
    def native_value(self) -> float | None:
        battery = self._battery
        if battery is None or len(battery.cell_voltages_mv) < self._index:
            return None
        return battery.cell_voltages_mv[self._index - 1] / 1000.0

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"cell_index": self._index, "battery_serial": self._serial}
