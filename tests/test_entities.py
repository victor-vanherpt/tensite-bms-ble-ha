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
    kwargs.setdefault("poll_delay", 60)
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
        """A "why is polling failing" sensor is useless if it goes unavailable."""
        for key in ("poll_interval", "poll_duration", "consecutive_failures"):
            description = next(d for d in CLUSTER_SENSORS if d.key == key)
            assert description.coordinator_fn is not None


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
