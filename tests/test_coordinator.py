"""Coordinator decision logic.

Every test here corresponds to something that actually broke in production, so
each one names the failure it prevents rather than restating the code.
"""

from __future__ import annotations

import pytest

from custom_components.tensite_bms_ble.const import (
    DEFAULT_SCAN_INTERVAL,
    FULL_SCAN_INTERVAL,
    LISTEN_TIMEOUT,
    MIN_SCAN_INTERVAL,
    MIN_STALE_WINDOW,
    POLL_GRACE_FRACTION,
    STALE_AFTER_INTERVALS,
)
from custom_components.tensite_bms_ble.coordinator import TensiteClusterCoordinator

ADDRESS = "AA:BB:CC:DD:EE:FF"


def make_coordinator(hass, **kwargs) -> TensiteClusterCoordinator:
    kwargs.setdefault("poll_delay", DEFAULT_SCAN_INTERVAL)
    return TensiteClusterCoordinator(
        hass=hass, address=ADDRESS, serial=None, **kwargs
    )


class TestPollGating:
    """When an advertisement should turn into a poll."""

    def test_first_advertisement_always_polls(self, hass, service_info):
        coordinator = make_coordinator(hass)
        assert coordinator._needs_poll(service_info, None) is True

    def test_timed_from_poll_start_not_poll_end(self, hass, service_info, monkeypatch):
        """The bug that halved the polling rate.

        The base class measures from when the previous poll *finished*, so the
        seconds a poll spends holding the connection pushed the next
        advertisement under the threshold and it was skipped -- 10 minutes for
        a 5-minute setting. Timing from the start is what fixes it, so a poll
        that began exactly one delay ago is due no matter how long it ran.
        """
        coordinator = make_coordinator(hass, poll_delay=300)
        now = 10_000.0
        monkeypatch.setattr(
            "custom_components.tensite_bms_ble.coordinator.monotonic_time_coarse",
            lambda: now,
        )
        coordinator._last_poll_started = now - 300
        assert coordinator._needs_poll(service_info, None) is True

    def test_grace_window_accepts_a_slightly_early_advertisement(
        self, hass, service_info, monkeypatch
    ):
        """Advertisements jitter; one arriving early is the only chance for minutes.

        Observed live: 289 s after the last poll with a 300 s delay. Rejecting
        it cost a further five minutes for the sake of 11 s.
        """
        coordinator = make_coordinator(hass, poll_delay=300)
        now = 10_000.0
        monkeypatch.setattr(
            "custom_components.tensite_bms_ble.coordinator.monotonic_time_coarse",
            lambda: now,
        )
        coordinator._last_poll_started = now - 289
        assert coordinator._needs_poll(service_info, None) is True

    def test_still_refuses_an_advertisement_well_inside_the_delay(
        self, hass, service_info, monkeypatch
    ):
        """The grace window must not swallow the delay entirely."""
        coordinator = make_coordinator(hass, poll_delay=300)
        now = 10_000.0
        monkeypatch.setattr(
            "custom_components.tensite_bms_ble.coordinator.monotonic_time_coarse",
            lambda: now,
        )
        coordinator._last_poll_started = now - 100
        assert coordinator._needs_poll(service_info, None) is False

    def test_grace_scales_with_the_delay(self, hass):
        """Expressed as a fraction so a short delay gets a short grace."""
        assert make_coordinator(hass, poll_delay=300)._due_after == pytest.approx(270)
        assert make_coordinator(hass, poll_delay=60)._due_after == pytest.approx(54)

    def test_never_starts_a_poll_while_one_is_running(self, hass, service_info):
        """Two connections to a gateway with one slot is the thing to avoid."""
        coordinator = make_coordinator(hass)
        coordinator._poll_in_progress = True
        assert coordinator._needs_poll(service_info, None) is False
        assert coordinator.polls_skipped_overlap == 1


class TestStaleness:
    """When entities should give up on the last reading."""

    def test_window_has_a_floor_so_fast_polling_does_not_flap(self, hass):
        """Scaling purely with the delay declares death absurdly quickly.

        At a 60 s delay three missed polls is three minutes, and a BLE device
        can be unreachable that long while perfectly healthy. The data is no
        more stale at a short delay -- only the sampling rate changed.
        """
        coordinator = make_coordinator(hass, poll_delay=60)
        assert coordinator.stale_after == MIN_STALE_WINDOW + LISTEN_TIMEOUT

    def test_window_scales_once_the_delay_exceeds_the_floor(self, hass):
        coordinator = make_coordinator(hass, poll_delay=600)
        assert coordinator.stale_after == 600 * STALE_AFTER_INTERVALS + LISTEN_TIMEOUT

    def test_no_data_is_never_fresh(self, hass):
        assert make_coordinator(hass).has_fresh_data is False

    def test_fresh_until_the_window_expires(self, hass, monkeypatch):
        coordinator = make_coordinator(hass, poll_delay=60)
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


class TestBatteryCount:
    """How the bank size is established.

    There is nothing to configure. The master states it in its topology frame;
    a full-window poll is only the fallback for before one has arrived. The
    trap the fallback avoids: an ordinary poll stops as soon as *expected*
    batteries have reported, so counting its results would just re-measure the
    exit condition, and an undercount would latch permanently.
    """

    def test_unknown_until_something_reports(self, hass):
        assert make_coordinator(hass).expected_batteries == 0
        assert make_coordinator(hass).battery_count_source == "unknown"

    def test_the_roster_is_used_when_present(self, hass):
        coordinator = make_coordinator(hass)
        coordinator._roster_batteries = 4
        assert coordinator.expected_batteries == 4
        assert coordinator.battery_count_source == "roster"

    def test_the_roster_beats_a_full_scan(self, hass):
        """It is a statement by the bank, not a count of who happened to reply."""
        coordinator = make_coordinator(hass)
        coordinator._learned_batteries = 3
        coordinator._roster_batteries = 4
        assert coordinator.expected_batteries == 4
        assert coordinator.battery_count_source == "roster"

    def test_full_scan_is_the_fallback(self, hass):
        coordinator = make_coordinator(hass)
        coordinator._learned_batteries = 4
        assert coordinator.expected_batteries == 4
        assert coordinator.battery_count_source == "full scan"

    def test_full_scan_due_before_any_has_run(self, hass):
        """So the bank size is established on the very first poll."""
        assert make_coordinator(hass)._is_full_scan_due() is True

    def test_full_scan_not_due_again_immediately(self, hass, monkeypatch):
        coordinator = make_coordinator(hass)
        now = 10_000.0
        monkeypatch.setattr(
            "custom_components.tensite_bms_ble.coordinator.monotonic_time_coarse",
            lambda: now,
        )
        coordinator._last_full_scan = now - 10
        assert coordinator._is_full_scan_due() is False

    def test_full_scan_due_again_after_the_interval(self, hass, monkeypatch):
        """The re-assert that finds a battery added and drops one removed."""
        coordinator = make_coordinator(hass)
        now = 10_000.0
        monkeypatch.setattr(
            "custom_components.tensite_bms_ble.coordinator.monotonic_time_coarse",
            lambda: now,
        )
        coordinator._last_full_scan = now - FULL_SCAN_INTERVAL
        assert coordinator._is_full_scan_due() is True


class TestConstants:
    """Guards on values whose justification is measured, not chosen."""

    def test_minimum_delay_matches_what_the_hardware_allows(self):
        """Below the advertising cadence the setting does nothing measurable."""
        assert MIN_SCAN_INTERVAL == DEFAULT_SCAN_INTERVAL

    def test_grace_leaves_most_of_the_delay_intact(self):
        assert 0 < POLL_GRACE_FRACTION <= 0.25


