"""What the coordinator does with a held connection.

The stream itself is tested in the library against a faked GATT peer; what
matters here is the wiring: that a reading pushed from the connection reaches
the entities, that losing the connection is survivable, and that the switch
really does let go of the gateway's single slot.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from tensite_bms_ble import BatteryReading, ClusterReading

from custom_components.tensite_bms_ble.coordinator import TensiteClusterCoordinator

from .conftest import FakeServiceInfo

ADDRESS = "AA:BB:CC:DD:EE:FF"


def battery(serial: str, position: int) -> BatteryReading:
    return BatteryReading(serial=serial, position=position)


def reading(*serials: str, roster: int | None = None) -> ClusterReading:
    return ClusterReading(
        address=ADDRESS,
        master_serial=serials[0] if serials else None,
        batteries={s: battery(s, 0x01A0 + i) for i, s in enumerate(serials)},
        roster_count=roster,
    )


@pytest.fixture
def coordinator(hass) -> TensiteClusterCoordinator:
    return TensiteClusterCoordinator(hass=hass, address=ADDRESS, serial=None)


async def open_stream(coordinator, fake_stream, device=None):
    """Run the connect path with the stream class faked out."""
    device = device or object()
    with patch(
        "custom_components.tensite_bms_ble.coordinator"
        ".async_ble_device_from_address",
        return_value=device,
    ):
        info = FakeServiceInfo(address=ADDRESS, device=device)
        await coordinator._async_ensure_streaming(info)
    return fake_stream.instances[-1] if fake_stream.instances else None


class TestOpening:
    async def test_an_advertisement_opens_the_stream(self, coordinator, fake_stream):
        stream = await open_stream(coordinator, fake_stream)
        assert stream.started == 1
        assert coordinator.is_connected

    async def test_a_second_advertisement_does_not_open_another(
        self, coordinator, fake_stream
    ):
        """One central at a time; a second connection is the failure to avoid."""
        await open_stream(coordinator, fake_stream)
        await open_stream(coordinator, fake_stream)
        assert len(fake_stream.instances) == 1

    async def test_a_failed_start_is_recorded_not_raised(self, coordinator, fake_stream):
        """The next advertisement is the next chance; crashing helps nobody."""
        fake_stream.next_start_error = OSError("no connection slot")
        await open_stream(coordinator, fake_stream)

        assert coordinator.connection_failures == 1
        assert "no connection slot" in coordinator.connection_health["last_error"]
        assert coordinator.is_connected is False

    async def test_a_failure_leaves_the_next_advertisement_free_to_retry(
        self, coordinator, fake_stream, service_info
    ):
        coordinator._note_failure("boom")
        assert coordinator._needs_connection(service_info, None) is True

    async def test_no_connectable_device_is_a_failure_not_a_crash(
        self, coordinator, fake_stream
    ):
        with patch(
            "custom_components.tensite_bms_ble.coordinator"
            ".async_ble_device_from_address",
            return_value=None,
        ):
            info = FakeServiceInfo(address=ADDRESS, device=None, connectable=False)
            await coordinator._async_ensure_streaming(info)
        assert coordinator.connection_failures == 1
        assert fake_stream.instances == []


class TestReadings:
    async def test_a_pushed_reading_becomes_the_data(self, coordinator, fake_stream):
        stream = await open_stream(coordinator, fake_stream)
        stream.push(reading("A", "B", "C", "D"))
        assert coordinator.data.battery_count == 4
        assert coordinator.batteries_reported == 4
        assert coordinator.expected_batteries == 4

    async def test_the_roster_is_taken_from_the_reading(self, coordinator, fake_stream):
        stream = await open_stream(coordinator, fake_stream)
        stream.push(reading("A", roster=4))
        assert coordinator.expected_batteries == 4
        assert coordinator.battery_count_source == "roster"

    async def test_a_later_reading_without_a_roster_leaves_it_alone(
        self, coordinator, fake_stream
    ):
        stream = await open_stream(coordinator, fake_stream)
        stream.push(reading("A", roster=4))
        stream.push(reading("A", "B"))
        assert coordinator.expected_batteries == 4

    async def test_readings_are_merged_across_a_reconnect(
        self, coordinator, fake_stream
    ):
        """A battery that has not spoken yet in the new session must not blank."""
        first = await open_stream(coordinator, fake_stream)
        first.push(reading("A", "B", "C", "D"))
        first.drop()
        await coordinator.async_release()

        second = await open_stream(coordinator, fake_stream)
        second.push(reading("A"))
        assert set(coordinator.data.batteries) == {"A", "B", "C", "D"}
        assert coordinator.batteries_reported == 1, "only A is actually reporting"

    async def test_the_master_serial_is_adopted(self, coordinator, fake_stream):
        stream = await open_stream(coordinator, fake_stream)
        assert coordinator.serial is None
        stream.push(reading("1417725SLKOPGG08146"))
        assert coordinator.serial == "1417725SLKOPGG08146"

    async def test_the_achieved_cadence_appears_from_the_second_reading(
        self, coordinator, fake_stream
    ):
        stream = await open_stream(coordinator, fake_stream)
        stream.push(reading("A"))
        assert coordinator.update_interval_seconds is None
        stream.push(reading("A"))
        assert coordinator.update_interval_seconds is not None


class TestCircuitBreaker:
    """The switch has to actually free the gateway."""

    async def test_disabling_disconnects(self, coordinator, fake_stream):
        stream = await open_stream(coordinator, fake_stream)
        await coordinator.async_set_enabled(False)
        assert stream.stopped == 1
        assert coordinator.is_connected is False
        assert coordinator.enabled is False

    async def test_disabling_survives_the_next_advertisement(
        self, coordinator, fake_stream, service_info
    ):
        await open_stream(coordinator, fake_stream)
        await coordinator.async_set_enabled(False)
        assert coordinator._needs_connection(service_info, None) is False
        await open_stream(coordinator, fake_stream)
        assert len(fake_stream.instances) == 1, "reopened a released connection"

    async def test_re_enabling_reconnects_without_waiting(
        self, coordinator, fake_stream
    ):
        """Waiting for the next advertisement would be up to five minutes of
        looking broken after the user asked for it back."""
        await open_stream(coordinator, fake_stream)
        await coordinator.async_set_enabled(False)
        with patch(
            "custom_components.tensite_bms_ble.coordinator.async_last_service_info",
            return_value=FakeServiceInfo(address=ADDRESS, device=object()),
        ):
            await coordinator.async_set_enabled(True)
        assert len(fake_stream.instances) == 2
        assert coordinator.is_connected

    async def test_re_enabling_with_nothing_heard_yet_waits_for_one(
        self, coordinator, fake_stream
    ):
        await coordinator.async_set_enabled(False)
        with patch(
            "custom_components.tensite_bms_ble.coordinator.async_last_service_info",
            return_value=None,
        ):
            await coordinator.async_set_enabled(True)
        assert coordinator.enabled is True
        assert fake_stream.instances == []

    async def test_setting_the_same_value_twice_does_nothing(
        self, coordinator, fake_stream
    ):
        stream = await open_stream(coordinator, fake_stream)
        await coordinator.async_set_enabled(True)
        assert stream.stopped == 0

    async def test_release_is_safe_with_no_stream(self, coordinator):
        await coordinator.async_release()
