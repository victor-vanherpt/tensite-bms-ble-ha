"""Entity behaviour that depends on decoded protocol detail.

These lean on the reverse-engineered facts rather than restating the code: the
alarm table comes from the vendor app's own parser, the relay route values from
a capture paired with a screenshot, and the temperature sentinels are values
the app itself attaches no meaning to.
"""

from __future__ import annotations

import pytest
from tensite_bms_ble import ALARM_SLOTS, BatteryReading, ClusterReading
from tensite_bms_ble.const import ROUTE_ACTIVE

from custom_components.tensite_bms_ble.coordinator import TensiteClusterCoordinator
from custom_components.tensite_bms_ble.sensor import (
    BATTERY_SENSORS,
    CLUSTER_SENSORS,
    TensitePackTemperatureSensor,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
SERIAL = "1417607SLKOPGG08051"

#: The one real fault ever captured: the app showed exactly
#: "Cell Faults -> Voltage Under: Fault" for these bytes.
FAULTY_BITS = bytes.fromhex("0000300000000000")


def make_coordinator(hass, **kwargs) -> TensiteClusterCoordinator:
    return TensiteClusterCoordinator(
        hass=hass, address=ADDRESS, serial=None, **kwargs
    )


def with_battery(coordinator, **fields) -> BatteryReading:
    battery = BatteryReading(serial=SERIAL, position=0x01A0, **fields)
    coordinator.data = ClusterReading(
        address=ADDRESS, master_serial=SERIAL, batteries={SERIAL: battery}
    )
    # Entities gate on freshness, not on data merely existing.
    coordinator._last_data_at = 10**9
    return battery


class TestPackTemperature:
    """The -50 / -30 sentinels, and the option to hide them."""

    def test_sentinel_shown_by_default_like_the_vendor_app(self, hass):
        """The app prints raw-50 unconditionally; so do we, by default."""
        coordinator = make_coordinator(hass)
        with_battery(coordinator, temperatures=(25, -50))
        sensor = TensitePackTemperatureSensor(coordinator, SERIAL, 2)
        assert sensor.available is True
        assert sensor.native_value == -50

    def test_option_reports_the_sentinel_as_unavailable(self, hass):
        coordinator = make_coordinator(hass, hide_sentinel_temperatures=True)
        with_battery(coordinator, temperatures=(25, -50))
        sensor = TensitePackTemperatureSensor(coordinator, SERIAL, 2)
        assert sensor.available is False

    def test_option_does_not_hide_a_genuinely_cold_pack(self, hass):
        """Only the two specific values are sentinels, not "anything negative".

        A pack really can sit below freezing, and -5 C in winter is a reading.
        """
        coordinator = make_coordinator(hass, hide_sentinel_temperatures=True)
        with_battery(coordinator, temperatures=(-5,))
        sensor = TensitePackTemperatureSensor(coordinator, SERIAL, 1)
        assert sensor.available is True
        assert sensor.native_value == -5

    @pytest.mark.parametrize("sentinel", [-50, -30])
    def test_both_sentinels_are_recognised(self, hass, sentinel):
        coordinator = make_coordinator(hass, hide_sentinel_temperatures=True)
        with_battery(coordinator, temperatures=(sentinel,))
        sensor = TensitePackTemperatureSensor(coordinator, SERIAL, 1)
        assert sensor.available is False

    def test_a_sensor_the_pack_does_not_have_is_unavailable(self, hass):
        """Four-sensor packs must not grow six entities."""
        coordinator = make_coordinator(hass)
        with_battery(coordinator, temperatures=(25, 26))
        assert TensitePackTemperatureSensor(coordinator, SERIAL, 5).available is False


class TestAvailability:
    """Freshness, not advertisement liveness, decides availability."""

    def test_unavailable_before_any_data(self, hass):
        coordinator = make_coordinator(hass)
        sensor = TensitePackTemperatureSensor(coordinator, SERIAL, 1)
        assert sensor.available is False

    def test_unavailable_once_data_goes_stale(self, hass, monkeypatch):
        coordinator = make_coordinator(hass)
        with_battery(coordinator, temperatures=(25,))
        now = 10**9 + coordinator.stale_after + 1
        monkeypatch.setattr(
            "custom_components.tensite_bms_ble.coordinator.monotonic_time_coarse",
            lambda: now,
        )
        assert TensitePackTemperatureSensor(coordinator, SERIAL, 1).available is False


class TestSensorDescriptions:
    """Guards on the descriptions themselves."""

    def test_keys_are_unique_within_each_level(self):
        for descriptions in (CLUSTER_SENSORS, BATTERY_SENSORS):
            keys = [d.key for d in descriptions]
            assert len(set(keys)) == len(keys)

    def test_cell_position_sensors_have_no_state_class(self):
        """They identify a cell; the mean of "cell 4" and "cell 11" is nothing."""
        for key in ("weakest_cell", "strongest_cell"):
            description = next(d for d in BATTERY_SENSORS if d.key == key)
            assert description.state_class is None

    def test_no_generic_battery_count_sensor_remains(self):
        """Replaced by batteries expected / reported, which mean something."""
        assert not any(d.key == "battery_count" for d in CLUSTER_SENSORS)

    def test_connection_diagnostics_survive_having_no_data(self):
        """A "why is nothing arriving" sensor is useless if it goes unavailable."""
        for key in ("update_interval", "reconnects", "connection_failures"):
            description = next(d for d in CLUSTER_SENSORS if d.key == key)
            assert description.coordinator_fn is not None


class TestLogbookNoise:
    """What lands in the Activity feed.

    Home Assistant shows a sensor in the logbook unless it counts as
    *continuous*, which means a unit, a state class, or a numeric device class
    (see `logbook.helpers.is_sensor_continuous`). Binary sensors are never
    filtered, which is why alarms belong there and do appear.

    On a held connection every reading is rewritten every few seconds, so a
    sensor that misses all three of those writes a logbook line every few
    seconds. "Cell voltages updated" did exactly that: ~585 changes an hour per
    battery, 2340 across the bank, burying the switch toggles and alarm changes
    the logbook exists for.
    """

    #: device classes Home Assistant does not treat as numeric.
    NON_NUMERIC = {"date", "enum", "timestamp"}

    def continuous(self, description) -> bool:
        device_class = description.device_class
        return bool(
            description.native_unit_of_measurement
            or description.state_class
            or (device_class is not None and str(device_class) not in self.NON_NUMERIC)
        )

    def test_no_new_sensor_quietly_starts_filling_the_logbook(self):
        """Deliberately an exact set, not a "nothing new" rule.

        Both survivors change rarely enough to be worth reading: charging state
        flips a few times a day, and the weakest/strongest cell only moves when
        the pack's balance does. Adding a third should be a decision, not an
        accident -- if this fails, either give the sensor a unit or state class,
        or add it here having checked how often it actually changes.
        """
        noisy = {
            f"{level}/{d.key}"
            for level, group in (("cluster", CLUSTER_SENSORS), ("battery", BATTERY_SENSORS))
            for d in group
            if not self.continuous(d)
        }
        assert noisy == {
            "cluster/status",
            "battery/status",
            "battery/weakest_cell",
            "battery/strongest_cell",
        }

    def test_cell_freshness_is_a_measurement_not_a_timestamp(self):
        """The one that had to change: it updated on every batch of frames."""
        description = next(d for d in BATTERY_SENSORS if d.key == "cells_age")
        assert self.continuous(description)
        assert not any(d.key == "cells_updated_at" for d in BATTERY_SENSORS)


class TestAlarmsAndRelays:
    """The reverse-engineered tables, seen through the entity layer."""

    def test_active_alarms_reports_the_captured_fault(self, hass):
        coordinator = make_coordinator(hass)
        battery = with_battery(coordinator, alarm_bits=FAULTY_BITS)
        description = next(d for d in BATTERY_SENSORS if d.key == "active_alarms")
        assert description.value_fn(battery) == 1
        assert description.attrs_fn(battery)["alarms"] == ["Cell Voltage Under"]

    def test_active_alarms_is_zero_not_unknown_when_healthy(self, hass):
        coordinator = make_coordinator(hass)
        battery = with_battery(coordinator, alarm_bits=bytes(8))
        description = next(d for d in BATTERY_SENSORS if d.key == "active_alarms")
        assert description.value_fn(battery) == 0

    def test_active_alarms_is_unknown_before_an_alarm_frame(self, hass):
        """Never having heard is not the same as everything being clear."""
        coordinator = make_coordinator(hass)
        battery = with_battery(coordinator)
        description = next(d for d in BATTERY_SENSORS if d.key == "active_alarms")
        assert description.value_fn(battery) is None

    def test_every_alarm_slot_gets_an_entity(self):
        assert len(ALARM_SLOTS) == 29

    def test_relay_active_state_matches_the_screenshot(self, hass):
        """Master relay frame 0x01 -> route 1 highlighted, 2-4 not."""
        coordinator = make_coordinator(hass)
        battery = with_battery(coordinator, relay_routes=(1, 0, 0, 0))
        assert battery.active_relays == (True, False, False, False)
        assert ROUTE_ACTIVE == 1


class TestEntityPlacement:
    """Where entities land on a device page.

    entity_category is the only control an integration has over this: Home
    Assistant supports exactly `config` and `diagnostic`, so a device page
    renders at most three buckets. Real grouping needs a dashboard -- see
    tools/make_dashboard.py.
    """

    def test_per_alarm_entities_are_diagnostic(self, hass):
        """29 per battery would otherwise be most of the page."""
        from homeassistant.const import EntityCategory

        from custom_components.tensite_bms_ble.binary_sensor import (
            TensiteBatteryAlarm,
        )

        coordinator = make_coordinator(hass)
        with_battery(coordinator, alarm_bits=FAULTY_BITS)
        alarm = TensiteBatteryAlarm(coordinator, SERIAL, ALARM_SLOTS[0])
        assert alarm.entity_category is EntityCategory.DIAGNOSTIC

    def test_diagnostic_does_not_cost_the_device_class(self, hass):
        """It is placement only -- automations and grouping still work."""
        from homeassistant.components.binary_sensor import BinarySensorDeviceClass

        from custom_components.tensite_bms_ble.binary_sensor import (
            TensiteBatteryAlarm,
        )

        coordinator = make_coordinator(hass)
        with_battery(coordinator, alarm_bits=FAULTY_BITS)
        alarm = TensiteBatteryAlarm(coordinator, SERIAL, ALARM_SLOTS[0])
        assert alarm.device_class is BinarySensorDeviceClass.PROBLEM

    def test_the_summary_sensors_stay_primary(self):
        """Active alarms and System status are what you put on a dashboard."""
        from custom_components.tensite_bms_ble.sensor import BATTERY_SENSORS

        active = next(d for d in BATTERY_SENSORS if d.key == "active_alarms")
        assert active.entity_category is None
