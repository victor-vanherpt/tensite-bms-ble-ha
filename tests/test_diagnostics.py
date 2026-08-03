"""The downloadable diagnostics report.

This is what someone attaches to a bug report, so the things worth checking are
that it answers "why is nothing arriving", that it survives having no data at
all -- which is exactly when it is most likely to be requested -- and that it
does not leak the serial numbers that identify the hardware.
"""

from __future__ import annotations

import json

from custom_components.tensite_bms_ble.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .test_init import MASTER, SLAVE


async def test_reports_connection_health(hass, entry):
    data = await async_get_config_entry_diagnostics(hass, entry)
    connection = data["connection"]
    # The questions a stuck integration raises, in one place.
    for key in (
        "enabled",
        "connected",
        "connected_for_s",
        "reconnects",
        "update_interval_s",
        "connection_failures",
        "last_error",
        "seconds_since_data",
        "batteries_expected",
        "batteries_reported",
    ):
        assert key in connection, key


async def test_reports_frame_statistics(hass, entry):
    """A climbing reject ratio looks identical to "nothing arriving" outside."""
    frames = (await async_get_config_entry_diagnostics(hass, entry))["reading"][
        "frames"
    ]
    for key in ("accepted", "rejected", "crc_failures", "bad_escapes", "reject_ratio"):
        assert key in frames, key


async def test_serials_are_redacted(hass, entry):
    """A diagnostics file pasted into an issue would otherwise carry them."""
    blob = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    assert MASTER not in blob
    assert SLAVE not in blob


async def test_per_battery_detail_is_present(hass, entry):
    batteries = (await async_get_config_entry_diagnostics(hass, entry))["reading"][
        "batteries"
    ]
    assert len(batteries) == 2
    # Keyed by position label, which does not identify the hardware.
    assert set(batteries) == {"C01/PA0", "C01/P03"}
    detail = next(iter(batteries.values()))
    for key in ("cell_count", "alarm_bits", "relay_routes", "cells_age_s"):
        assert key in detail, key


async def test_a_faulted_battery_names_its_alarm(hass, entry):
    batteries = (await async_get_config_entry_diagnostics(hass, entry))["reading"][
        "batteries"
    ]
    firing = [
        alarm
        for detail in batteries.values()
        for alarm in detail["active_alarms"]
    ]
    assert [a["name"] for a in firing] == ["Cell Voltage Under"]


async def test_survives_having_no_data_at_all(hass, entry):
    """The state in which diagnostics are most likely to be asked for."""
    entry.runtime_data.data = None
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["reading"] is None
    assert data["connection"]["connected"] is not None
    assert data["limits"]["update_throttle_s"] > 0
