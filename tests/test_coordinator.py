"""Coordinator decision logic.

Every test here corresponds to something that actually broke in production, or
to a property the streaming design depends on, rather than restating the code.
"""

from __future__ import annotations

from custom_components.tensite_bms_ble.const import MIN_STALE_WINDOW
from custom_components.tensite_bms_ble.coordinator import TensiteClusterCoordinator

ADDRESS = "AA:BB:CC:DD:EE:FF"


def make_coordinator(hass, **kwargs) -> TensiteClusterCoordinator:
    return TensiteClusterCoordinator(
        hass=hass, address=ADDRESS, serial=None, **kwargs
    )


class TestConnectionGating:
    """When an advertisement should be used to open the connection."""

    def test_first_advertisement_opens_it(self, hass, service_info):
        coordinator = make_coordinator(hass)
        assert coordinator._needs_connection(service_info, None) is True

    def test_not_while_the_stream_is_already_running(self, hass, service_info):
        """The whole point of holding it: connecting again would be a second
        central on a gateway that accepts one."""
        coordinator = make_coordinator(hass)
        coordinator._stream = _RunningStream()
        assert coordinator._needs_connection(service_info, None) is False

    def test_not_once_the_connection_has_been_released(self, hass, service_info):
        """Turning the switch off must survive the next advertisement.

        Otherwise the release would last only until the gateway next spoke,
        which is the moment the user is trying to use the app.
        """
        coordinator = make_coordinator(hass)
        coordinator._enabled = False
        assert coordinator._needs_connection(service_info, None) is False

    def test_a_reconnect_uses_the_freshly_resolved_device(self, hass, service_info):
        """A stale BLEDevice points at an adapter that may no longer see the
        battery, and reconnects made with one fail as if the bank were gone."""
        coordinator = make_coordinator(hass)
        stream = _RunningStream()
        coordinator._stream = stream
        service_info.device = object()
        coordinator._needs_connection(service_info, None)
        assert stream.device is service_info.device


class TestStaleness:
    """When entities should give up on the last reading."""

    def test_window_covers_a_reconnect_wait(self, hass):
        """A drop may have to wait for the next advertisement to recover, and
        this hardware advertises every 245-300 s. A shorter window would blank
        every entity on a single routine drop."""
        assert make_coordinator(hass).stale_after == MIN_STALE_WINDOW
        assert MIN_STALE_WINDOW >= 300

    def test_no_data_is_never_fresh(self, hass):
        assert make_coordinator(hass).has_fresh_data is False

    def test_fresh_until_the_window_expires(self, hass, monkeypatch):
        coordinator = make_coordinator(hass)
        now = 10_000.0
        monkeypatch.setattr(
            "custom_components.tensite_bms_ble.coordinator.monotonic_time_coarse",
            lambda: now,
        )
        coordinator.data = object()
        coordinator._last_data_at = now - (coordinator.stale_after - 1)
        assert coordinator.has_fresh_data is True

        coordinator._last_data_at = now - (coordinator.stale_after + 1)
        assert coordinator.has_fresh_data is False

    def test_a_dropped_connection_keeps_the_last_reading(self, hass):
        """Frames stop; what they last said stays true until it goes stale."""
        coordinator = make_coordinator(hass)
        coordinator.data = object()
        coordinator._reported_batteries = 4
        coordinator._async_handle_connection_change(False)
        assert coordinator.data is not None
        assert coordinator.batteries_reported == 0, "nothing is reporting now"


class TestBatteryCount:
    """How the bank size is established.

    There is nothing to configure. The master states it in a header byte of its
    topology frame; counting who has reported is the fallback for before one
    arrives. That fallback is sound here and was not under polling: on a held
    connection every battery reports every ~5 s, so the tally is the bank.
    """

    def test_unknown_until_something_reports(self, hass):
        assert make_coordinator(hass).expected_batteries == 0
        assert make_coordinator(hass).battery_count_source == "unknown"

    def test_the_roster_is_used_when_present(self, hass):
        coordinator = make_coordinator(hass)
        coordinator._roster_batteries = 4
        assert coordinator.expected_batteries == 4
        assert coordinator.battery_count_source == "roster"

    def test_the_roster_beats_the_tally(self, hass):
        """It is a statement by the bank, not a count of who happened to reply."""
        coordinator = make_coordinator(hass)
        coordinator._reported_batteries = 3
        coordinator._roster_batteries = 4
        assert coordinator.expected_batteries == 4
        assert coordinator.battery_count_source == "roster"

    def test_the_tally_is_the_fallback(self, hass):
        coordinator = make_coordinator(hass)
        coordinator._reported_batteries = 4
        assert coordinator.expected_batteries == 4
        assert coordinator.battery_count_source == "stream"


class _RunningStream:
    """Enough of a stream for the gating tests."""

    is_running = True
    is_connected = True
    reconnects = 0
    last_error = None

    def __init__(self) -> None:
        self.device = None

    def update_device(self, device) -> None:
        self.device = device
