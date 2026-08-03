"""Sensor entities across the cluster / battery / cell hierarchy.

Batteries are not known until the first successful poll, so battery and cell
entities are added dynamically as the bank reports in.
"""

from __future__ import annotations

import logging
from datetime import datetime
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfTime,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from tensite_bms_ble import (
    BatteryReading,
    ClusterReading,
    is_sentinel_temperature,
)

from . import TensiteConfigEntry
from .const import DOMAIN
from .coordinator import TensiteClusterCoordinator
from .entity import TensiteEntity, battery_device_info, cluster_device_info

_LOGGER = logging.getLogger(__name__)

VOLT = UnitOfElectricPotential.VOLT
MILLIVOLT = UnitOfElectricPotential.MILLIVOLT


# --- Cluster-level ------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ClusterSensorDescription(SensorEntityDescription):
    value_fn: Callable[[ClusterReading], float | int | str | None]
    #: Optional extra attributes, for sensors whose state is a summary and
    #: whose detail belongs alongside it rather than in another entity.
    attrs_fn: Callable[[ClusterReading], dict[str, object]] | None = None
    #: For sensors that describe the *connection* rather than the battery data.
    #: These read from the coordinator and stay available with no data at all,
    #: which is the whole point -- they explain why data is missing.
    coordinator_fn: (
        Callable[["TensiteClusterCoordinator"], float | int | str | None] | None
    ) = None


CLUSTER_SENSORS: tuple[ClusterSensorDescription, ...] = (
    ClusterSensorDescription(
        key="voltage",
        translation_key="voltage",
        value_fn=lambda r: r.total_voltage,
        native_unit_of_measurement=VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    ClusterSensorDescription(
        key="current",
        translation_key="current",
        value_fn=lambda r: r.current,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    ClusterSensorDescription(
        key="power",
        translation_key="power",
        value_fn=lambda r: r.power,
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    ClusterSensorDescription(
        key="soc",
        translation_key="soc",
        value_fn=lambda r: r.soc,
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    ClusterSensorDescription(
        key="max_temperature",
        translation_key="max_temperature",
        value_fn=lambda r: r.max_temperature,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    ClusterSensorDescription(
        key="min_temperature",
        translation_key="min_temperature",
        value_fn=lambda r: r.min_temperature,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    ClusterSensorDescription(
        key="status",
        translation_key="status",
        value_fn=lambda r: r.status,
        device_class=SensorDeviceClass.ENUM,
        options=["charging", "discharging", "idle"],
    ),
    ClusterSensorDescription(
        key="daily_charge",
        translation_key="daily_charge",
        value_fn=lambda r: r.daily_charge_kwh,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        # Resets to zero each day, which TOTAL_INCREASING handles: Home
        # Assistant treats a drop as a counter reset rather than a spike.
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
    ),
    ClusterSensorDescription(
        key="daily_discharge",
        translation_key="daily_discharge",
        value_fn=lambda r: r.daily_discharge_kwh,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
    ),
    # --- connection health ----------------------------------------------
    # Nothing here describes the batteries; they answer "is data flowing, and
    # if not, why?". They read from the coordinator so they survive a total
    # loss of data -- a diagnostic that goes unavailable with everything else
    # explains nothing.
    ClusterSensorDescription(
        key="batteries_expected",
        translation_key="batteries_expected",
        value_fn=lambda r: None,
        coordinator_fn=lambda c: c.batteries_expected,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # On a held connection every battery reports every ~5 s, so this should
    # equal the expected count within seconds of connecting. It falls to zero
    # when the connection drops, which is the point.
    ClusterSensorDescription(
        key="batteries_reported",
        translation_key="batteries_reported",
        value_fn=lambda r: None,
        coordinator_fn=lambda c: c.batteries_reported,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # The freshness number: near the update throttle while streaming, minutes
    # when the stream is down and waiting on an advertisement to reconnect.
    ClusterSensorDescription(
        key="update_interval",
        translation_key="update_interval",
        value_fn=lambda r: None,
        coordinator_fn=lambda c: c.update_interval_seconds,
        native_unit_of_measurement="s",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ClusterSensorDescription(
        key="reconnects",
        translation_key="reconnects",
        value_fn=lambda r: None,
        coordinator_fn=lambda c: c.reconnects,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ClusterSensorDescription(
        key="connection_failures",
        translation_key="connection_failures",
        value_fn=lambda r: None,
        coordinator_fn=lambda c: c.connection_failures,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ClusterSensorDescription(
        key="rejected_frames",
        translation_key="rejected_frames",
        value_fn=lambda r: r.stats.rejected,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ClusterSensorDescription(
        key="reject_ratio",
        translation_key="reject_ratio",
        value_fn=lambda r: round(r.stats.reject_ratio * 100, 1),
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ClusterSensorDescription(
        key="active_alarms",
        translation_key="active_alarms",
        value_fn=lambda r: sum(len(b.active_alarms) for b in r.batteries.values()),
        attrs_fn=lambda r: {
            # Keyed by serial so the state says how many and this says where.
            "by_battery": {
                serial: [slot.name for slot, _ in battery.active_alarms]
                for serial, battery in sorted(r.batteries.items())
                if battery.active_alarms
            },
            "alarms": sorted(
                {
                    slot.name
                    for battery in r.batteries.values()
                    for slot, _ in battery.active_alarms
                }
            ),
        },
        state_class=SensorStateClass.MEASUREMENT,
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
    value_fn: Callable[[BatteryReading], float | int | str | datetime | None]
    attrs_fn: Callable[[BatteryReading], dict[str, object]] | None = None


BATTERY_SENSORS: tuple[BatterySensorDescription, ...] = (
    BatterySensorDescription(
        key="voltage",
        translation_key="voltage",
        value_fn=lambda b: b.voltage,
        native_unit_of_measurement=VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    BatterySensorDescription(
        key="current",
        translation_key="current",
        value_fn=lambda b: b.current,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    BatterySensorDescription(
        key="power",
        translation_key="power",
        value_fn=lambda b: b.power,
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    BatterySensorDescription(
        key="soc",
        translation_key="soc",
        value_fn=lambda b: b.soc,
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    BatterySensorDescription(
        key="max_temperature",
        translation_key="max_temperature",
        value_fn=lambda b: b.max_temperature,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    BatterySensorDescription(
        key="min_temperature",
        translation_key="min_temperature",
        value_fn=lambda b: b.min_temperature,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    BatterySensorDescription(
        key="cell_sum_voltage",
        translation_key="cell_sum_voltage",
        value_fn=lambda b: b.cell_sum_voltage,
        native_unit_of_measurement=VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
    ),
    BatterySensorDescription(
        key="status",
        translation_key="status",
        value_fn=lambda b: b.status,
        device_class=SensorDeviceClass.ENUM,
        options=["charging", "discharging", "idle"],
    ),
    BatterySensorDescription(
        key="daily_charge",
        translation_key="daily_charge",
        value_fn=lambda b: b.daily_charge_kwh,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        # Resets to zero each day, which TOTAL_INCREASING handles: Home
        # Assistant treats a drop as a counter reset rather than a spike.
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
    ),
    BatterySensorDescription(
        key="daily_discharge",
        translation_key="daily_discharge",
        value_fn=lambda b: b.daily_discharge_kwh,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
    ),
    # Deliberately no state_class on these two: they are cell *positions*
    # (1-16), not measurements. With a state_class Home Assistant records
    # long-term statistics and charts them, which produces a meaningless graph
    # -- the mean of "cell 4" and "cell 11" is not a thing, and interpolating
    # between them is worse. They identify a cell; treat them as labels.
    # Tracks alarms firing without needing all 29 per-alarm entities enabled.
    # The state is a count so it graphs and triggers cleanly (> 0 is "something
    # is wrong", and the number rising says it got worse); which alarms are
    # firing rides along in the attributes.
    BatterySensorDescription(
        key="active_alarms",
        translation_key="active_alarms",
        value_fn=lambda b: (
            None if b.alarm_bits is None else len(b.active_alarms)
        ),
        attrs_fn=lambda b: {
            "alarms": [slot.name for slot, _ in b.active_alarms],
            "highest_level": (
                int(b.alarm_level) if b.alarm_level is not None else None
            ),
            "detail": [
                {
                    "name": slot.name,
                    "category": slot.category,
                    "label": slot.label,
                    "key": slot.key,
                    "level": int(level),
                }
                for slot, level in b.active_alarms
            ],
        },
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BatterySensorDescription(
        key="weakest_cell",
        translation_key="weakest_cell",
        value_fn=lambda b: b.weakest_cell,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BatterySensorDescription(
        key="strongest_cell",
        translation_key="strongest_cell",
        value_fn=lambda b: b.strongest_cell,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    # An age in seconds rather than the timestamp this used to be. Same
    # question -- have this battery's cells gone quiet while the others keep
    # reporting? -- but a timestamp sensor is *not continuous* to Home
    # Assistant, so every one of its ~585 changes an hour became a "Cell
    # voltages updated" line in the logbook. Four batteries made that 2340
    # entries an hour, burying the switch toggles and alarm changes the logbook
    # is for. A measurement with a unit is filtered out of it, and answers the
    # question more directly besides.
    BatterySensorDescription(
        key="cells_age",
        translation_key="cells_age",
        value_fn=lambda b: None if b.cells_age is None else round(b.cells_age),
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BatterySensorDescription(
        key="faulty_temperature_sensors",
        translation_key="faulty_temperature_sensors",
        value_fn=lambda b: b.faulty_temperature_sensors,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
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

    # Tracked per serial rather than as a plain "seen" set. A poll can return a
    # battery that sent its summary but not yet its cells -- the gateway
    # round-robins the bank -- and marking that serial as done would mean its
    # 16 cell entities were never created at all.
    known: set[str] = set()
    cells_added: dict[str, int] = {}
    temps_added: dict[str, int] = {}

    @callback
    def _add_new_batteries() -> None:
        reading = coordinator.data
        if reading is None:
            return
        entities: list[TensiteEntity] = []
        for serial, battery in reading.batteries.items():
            if serial not in known:
                known.add(serial)
                entities.extend(
                    TensiteBatterySensor(coordinator, serial, description)
                    for description in BATTERY_SENSORS
                )
                _LOGGER.debug(
                    "Adding battery %s (%s)", serial, battery.position_label
                )

            have = cells_added.get(serial, 0)
            if battery.cell_count > have:
                entities.extend(
                    TensiteCellSensor(coordinator, serial, index)
                    for index in range(have + 1, battery.cell_count + 1)
                )
                cells_added[serial] = battery.cell_count
                _LOGGER.debug(
                    "Adding %d cells for %s", battery.cell_count - have, serial
                )

            # Pack temperature sensors: 4 or 6 depending on the model. These
            # are pack-level, not per-cell, so they live on the battery device
            # rather than getting devices of their own.
            have = temps_added.get(serial, 0)
            count = len(battery.temperatures)
            if count > have:
                entities.extend(
                    TensitePackTemperatureSensor(coordinator, serial, index)
                    for index in range(have + 1, count + 1)
                )
                temps_added[serial] = count
                _LOGGER.debug(
                    "Adding %d temperature sensors for %s", count - have, serial
                )

        if entities:
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
    def available(self) -> bool:
        # Connection diagnostics must survive a total loss of data -- an
        # unavailable "why is nothing arriving" sensor is useless.
        if self.entity_description.coordinator_fn is not None:
            return True
        return super().available

    @property
    def native_value(self) -> float | int | str | None:
        if (coordinator_fn := self.entity_description.coordinator_fn) is not None:
            return coordinator_fn(self.coordinator)
        if (reading := self.coordinator.data) is None:
            return None
        return self.entity_description.value_fn(reading)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        if self.entity_description.coordinator_fn is not None:
            return self.coordinator.connection_health
        attrs_fn = self.entity_description.attrs_fn
        if attrs_fn is None or (reading := self.coordinator.data) is None:
            return None
        return attrs_fn(reading)


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
        # Deliberately not coordinator.available. A peripheral stops
        # advertising while a central is connected, so that flag drops for the
        # whole duration of every poll -- which would blink this entity out
        # once per cycle. The last known RSSI stays meaningful meanwhile.
        return self.coordinator.rssi is not None

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
        battery = self._battery
        self._attr_device_info = battery_device_info(
            coordinator,
            serial,
            battery.position_label if battery else "",
            battery.model if battery else None,
        )

    @property
    def _battery(self) -> BatteryReading | None:
        if (reading := self.coordinator.data) is None:
            return None
        return reading.batteries.get(self._serial)

    @property
    def available(self) -> bool:
        return self.coordinator.has_fresh_data and self._battery is not None

    @property
    def native_value(self) -> float | int | str | datetime | None:
        if (battery := self._battery) is None:
            return None
        return self.entity_description.value_fn(battery)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        attrs_fn = self.entity_description.attrs_fn
        if attrs_fn is None or (battery := self._battery) is None:
            return None
        return attrs_fn(battery)


class TensiteCellSensor(TensiteEntity, SensorEntity):
    """Voltage of one cell.

    Lives on the battery device alongside the pack temperature sensors, rather
    than on a device of its own. Giving each cell a device pushed all sixteen
    voltages a click away from the battery, which is the opposite of what you
    want when scanning a pack for a weak cell.
    """

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
        self._attr_translation_placeholders = {"index": f"{index:02d}"}
        self._attr_unique_id = f"{serial}_cell_{index:02d}_voltage"
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
        if (reading := self.coordinator.data) is None:
            return None
        return reading.batteries.get(self._serial)

    @property
    def available(self) -> bool:
        battery = self._battery
        return (
            self.coordinator.has_fresh_data
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


class TensitePackTemperatureSensor(TensiteEntity, SensorEntity):
    """One pack temperature sensor.

    Reports unavailable rather than a number when the BMS flags the sensor as
    faulty or absent. The raw protocol uses -50 C and -30 C as sentinels for
    that, and the vendor app displays them literally -- but they are not
    temperatures, and graphing them would wreck any history.
    """

    _attr_translation_key = "pack_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    # The BMS reports whole degrees, so anything past the decimal point is
    # noise from Home Assistant's own time-weighted statistics mean rather than
    # a real reading. Without this a *constant* sensor draws a spiky graph: the
    # mean lands on -49.999999999999996 for one bucket, and because the series
    # never varies the chart autoscales its y-axis to that 1e-13 range and
    # renders the rounding error as a full-height spike.
    _attr_suggested_display_precision = 0

    def __init__(
        self, coordinator: TensiteClusterCoordinator, serial: str, index: int
    ) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._index = index
        self._attr_translation_placeholders = {"index": str(index)}
        self._attr_unique_id = f"{serial}_pack_temperature_{index:02d}"
        reading = coordinator.data
        battery = reading.batteries.get(serial) if reading else None
        self._attr_device_info = battery_device_info(
            coordinator, serial, battery.position_label if battery else "", None
        )

    @property
    def _value(self) -> int | None:
        reading = self.coordinator.data
        if reading is None:
            return None
        battery = reading.batteries.get(self._serial)
        if battery is None or len(battery.temperatures) < self._index:
            return None
        return battery.temperatures[self._index - 1]

    @property
    def available(self) -> bool:
        if not self.coordinator.has_fresh_data or self._value is None:
            return False
        # By default the sentinel is shown as-is, matching the vendor app.
        # Hiding it is opt-in, because a sentinel is genuinely what the BMS
        # reports and some users would rather see it than lose the entity.
        if self.coordinator.hide_sentinel_temperatures:
            return not is_sentinel_temperature(self._value)
        return True

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "sensor_index": self._index,
            "battery_serial": self._serial,
            # A sentinel is not a measurement. What it *means* is unresolved
            # -- see is_sentinel_temperature() -- so no cause is claimed here.
            "sentinel": is_sentinel_temperature(self._value),
        }


    @property
    def native_value(self) -> int | None:
        return self._value
