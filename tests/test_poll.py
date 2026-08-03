"""What a poll does with what it read.

The wiring between "a full scan is due" and "the bank size is now known" was
only ever verified by watching a log line go past. These drive it directly,
with the BLE client stubbed out, so the arithmetic is checked rather than
observed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from tensite_bms_ble import BatteryReading, ClusterReading, TensiteError

from custom_components.tensite_bms_ble.coordinator import TensiteClusterCoordinator

from .conftest import FakeServiceInfo

ADDRESS = "AA:BB:CC:DD:EE:FF"


def battery(serial: str, position: int) -> BatteryReading:
    return BatteryReading(serial=serial, position=position)


def reading(*serials: str) -> ClusterReading:
    return ClusterReading(
        address=ADDRESS,
        master_serial=serials[0] if serials else None,
        batteries={s: battery(s, 0x01A0 + i) for i, s in enumerate(serials)},
    )


@pytest.fixture
def coordinator(hass) -> TensiteClusterCoordinator:
    return TensiteClusterCoordinator(
        hass=hass, address=ADDRESS, serial=None, poll_delay=60
    )


def stub_client(result):
    """Patch the client so async_read returns *result* or raises it."""
    client = AsyncMock()
    if isinstance(result, Exception):
        client.async_read.side_effect = result
    else:
        client.async_read.return_value = result
    return patch(
        "custom_components.tensite_bms_ble.coordinator.TensiteClusterClient",
        return_value=client,
    ), client


async def run_poll(coordinator, result, device=object()):
    """Run one poll with a stubbed client, returning the client for assertions."""
    patcher, client = stub_client(result)
    with (
        patcher,
        patch(
            "custom_components.tensite_bms_ble.coordinator"
            ".async_ble_device_from_address",
            return_value=device,
        ),
    ):
        info = FakeServiceInfo(address=ADDRESS, device=device)
        coordinator.data = await coordinator._async_poll_cluster(info)
    return client


class TestFullScan:
    """Only a full-window poll may establish the bank size."""

    async def test_first_poll_is_a_full_scan_and_learns_the_count(self, coordinator):
        client = await run_poll(coordinator, reading("A", "B", "C", "D"))
        # expect=None is what makes it wait out the listening window.
        assert client.async_read.await_args.kwargs["expect"] is None
        assert coordinator.expected_batteries == 4
        assert coordinator.batteries_reported == 4

    async def test_later_polls_exit_early_on_the_learned_count(self, coordinator):
        await run_poll(coordinator, reading("A", "B", "C", "D"))
        client = await run_poll(coordinator, reading("A", "B", "C", "D"))
        assert client.async_read.await_args.kwargs["expect"] == 4

    async def test_an_early_exiting_poll_never_lowers_the_count(self, coordinator):
        """The trap, driven end to end.

        The gateway answers its batteries in rotation, so an ordinary poll can
        come back with fewer than the bank holds. If that were allowed to set
        the expected count it would latch lower and lower.
        """
        await run_poll(coordinator, reading("A", "B", "C", "D"))
        await run_poll(coordinator, reading("A", "B"))
        assert coordinator.expected_batteries == 4
        assert coordinator.batteries_reported == 2

    async def test_a_full_scan_does_lower_the_count(self, coordinator, monkeypatch):
        """A battery genuinely removed should stop being waited for."""
        await run_poll(coordinator, reading("A", "B", "C", "D"))
        now = 10_000.0
        monkeypatch.setattr(
            "custom_components.tensite_bms_ble.coordinator.monotonic_time_coarse",
            lambda: now,
        )
        coordinator._last_full_scan = None  # force the next poll to be full
        await run_poll(coordinator, reading("A", "B", "C"))
        assert coordinator.expected_batteries == 3



class TestFailureHandling:
    """A failed poll must not blank the last good reading."""

    async def test_keeps_previous_data_on_error(self, coordinator):
        await run_poll(coordinator, reading("A", "B"))
        good = coordinator.data
        await run_poll(coordinator, TensiteError("boom"))
        assert coordinator.data is good
        assert coordinator.consecutive_failures == 1
        assert coordinator._last_error == "boom"

    async def test_recovery_resets_the_failure_count(self, coordinator):
        await run_poll(coordinator, TensiteError("boom"))
        await run_poll(coordinator, reading("A"))
        assert coordinator.consecutive_failures == 0

    async def test_missing_device_is_a_failure_not_a_crash(self, coordinator):
        patcher, _ = stub_client(reading("A"))
        with (
            patcher,
            patch(
                "custom_components.tensite_bms_ble.coordinator"
                ".async_ble_device_from_address",
                return_value=None,
            ),
        ):
            info = FakeServiceInfo(address=ADDRESS, device=None, connectable=False)
            await coordinator._async_poll_cluster(info)
        assert coordinator.consecutive_failures == 1

    async def test_an_unexpected_exception_is_recorded_not_raised(self, coordinator):
        """_async_update_data must never propagate; see its docstring."""
        await run_poll(coordinator, RuntimeError("kaboom"))
        assert coordinator.consecutive_failures == 1
        assert "RuntimeError" in coordinator._last_error

    async def test_the_in_progress_guard_is_always_cleared(self, coordinator):
        await run_poll(coordinator, TensiteError("boom"))
        assert coordinator._poll_in_progress is False


class TestPollAccounting:
    """The diagnostics that explain a poll after the fact."""

    async def test_duration_and_counts_are_recorded(self, coordinator):
        await run_poll(coordinator, reading("A"))
        assert coordinator.last_poll_duration is not None
        assert coordinator.poll_health["polls"] == 1
        assert coordinator.poll_health["failures"] == 0

    async def test_achieved_interval_appears_from_the_second_poll(self, coordinator):
        await run_poll(coordinator, reading("A"))
        assert coordinator.last_poll_interval is None
        await run_poll(coordinator, reading("A"))
        assert coordinator.last_poll_interval is not None


class TestRoster:
    """A topology frame in the reading is adopted as the bank size."""

    async def test_roster_is_taken_from_the_reading(self, coordinator):
        reading_with_roster = ClusterReading(
            address=ADDRESS,
            master_serial="A",
            batteries={"A": battery("A", 0x01A0)},
            roster_count=4,
        )
        await run_poll(coordinator, reading_with_roster)
        assert coordinator.expected_batteries == 4
        assert coordinator.battery_count_source == "roster"

    async def test_a_poll_without_one_leaves_the_roster_alone(self, coordinator):
        await run_poll(
            coordinator,
            ClusterReading(
                address=ADDRESS,
                master_serial="A",
                batteries={"A": battery("A", 0x01A0)},
                roster_count=4,
            ),
        )
        await run_poll(coordinator, reading("A", "B"))
        assert coordinator.expected_batteries == 4
