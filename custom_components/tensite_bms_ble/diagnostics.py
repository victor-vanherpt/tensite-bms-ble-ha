"""Downloadable diagnostics.

Settings -> Devices & Services -> Tensite BMS -> the three-dot menu ->
"Download diagnostics".

This is where the detail that would otherwise need a dozen entities lives:
connection health, frame parse statistics, and what each battery last
reported. The question it is meant to answer is "readings are not arriving as I
expect -- why?", so it deliberately includes the things that explain a *lack*
of data rather than only the data itself.

Serial numbers are redacted. They identify the hardware and appear in every
frame, so a diagnostics file pasted into an issue would otherwise carry them.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import TensiteConfigEntry
from .const import (
    CONF_MEMBER_SERIALS,
    CONF_SERIAL,
    CONNECT_TIMEOUT,
    UPDATE_THROTTLE,
)

TO_REDACT = {CONF_SERIAL, CONF_MEMBER_SERIALS, "serial", "master_serial"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TensiteConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a cluster."""
    coordinator = entry.runtime_data
    reading = coordinator.data

    data: dict[str, Any] = {
        "config": {
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "limits": {
            "connect_timeout_s": CONNECT_TIMEOUT,
            "update_throttle_s": UPDATE_THROTTLE,
        },
        "connection": {
            "address": coordinator.address,
            "has_fresh_data": coordinator.has_fresh_data,
            **coordinator.connection_health,
        },
    }

    if reading is None:
        data["reading"] = None
        return async_redact_data(data, TO_REDACT)

    data["reading"] = {
        "master_serial": reading.master_serial,
        "battery_count": reading.battery_count,
        # Non-null only if a type-0x32 topology frame arrived. Recorded to
        # find out whether these reach us at all outside the vendor app.
        "roster_count": reading.roster_count,
        "updated_at": reading.updated_at.isoformat(),
        # A climbing reject ratio means frames are arriving but failing CRC or
        # unstuffing, which looks identical to "not polling" from the outside.
        "frames": {
            "accepted": reading.stats.frames,
            "rejected": reading.stats.rejected,
            "crc_failures": reading.stats.crc_failures,
            "length_mismatches": reading.stats.length_mismatches,
            "bad_escapes": reading.stats.bad_escapes,
            "truncated": reading.stats.truncated,
            "reject_ratio": round(reading.stats.reject_ratio, 4),
            # Message ids the device sends that nothing here decodes. Not
            # an error -- the vendor app ignores some of these too -- but a
            # firmware that starts sending something new shows up here.
            "unhandled_ids": {
                f"0x{msg_id:04x}": count
                for msg_id, count in sorted(reading.stats.unhandled.items())
            },
        },
        # Keyed by position, not serial. async_redact_data replaces matching
        # *values*, so a dict keyed by serial would hand the serials straight
        # to anyone the file is sent to -- which is the one thing this is
        # supposed to prevent.
        "batteries": {
            battery.position_label: {
                "is_master": battery.is_master,
                "model": battery.model,
                "has_summary": battery.summary is not None,
                "cell_count": battery.cell_count,
                # Cells can lag the rest of a reading badly; this is how far.
                "cells_age_s": (
                    None
                    if battery.cells_age is None
                    else round(battery.cells_age, 1)
                ),
                "temperature_count": len(battery.temperatures),
                "alarm_bits": battery.alarm_bits_hex,
                "active_alarms": [
                    {"name": slot.name, "level": int(level)}
                    for slot, level in battery.active_alarms
                ],
                "unmapped_alarm_bits": battery.unmapped_alarm_bits or None,
                "relay_routes": list(battery.relay_routes),
                "switch_routes": list(battery.switch_routes),
            }
            for _, battery in sorted(reading.batteries.items())
        },
    }
    return async_redact_data(data, TO_REDACT)
