"""The connection switch, through Home Assistant's service calls.

The coordinator's own release logic is covered in test_streaming; what matters
here is that the entity exists, that it stays usable when everything else has
gone unavailable, and that calling switch.turn_off really does let go of the
gateway.
"""

from __future__ import annotations

from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.helpers import entity_registry as er

from .conftest import ADDRESS


def switch_entity_id(hass, entry) -> str:
    registry = er.async_get(hass)
    return next(
        e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.unique_id == f"{ADDRESS}_connection"
    )


async def test_the_switch_exists_and_is_on(hass, entry):
    state = hass.states.get(switch_entity_id(hass, entry))
    assert state is not None
    assert state.state == "on"


async def test_turning_it_off_releases_the_connection(hass, entry, fake_stream):
    entity_id = switch_entity_id(hass, entry)
    await hass.services.async_call(
        "switch", SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "off"
    assert entry.runtime_data.is_connected is False
    assert fake_stream.instances[-1].stopped == 1


async def test_turning_it_back_on_reconnects(hass, entry, fake_stream):
    entity_id = switch_entity_id(hass, entry)
    for service in (SERVICE_TURN_OFF, SERVICE_TURN_ON):
        await hass.services.async_call(
            "switch", service, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )
        await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "on"
    assert entry.runtime_data.enabled is True


async def test_it_stays_available_with_no_data(hass, entry):
    """It is the control that *fixes* an unavailable cluster."""
    entry.runtime_data.data = None
    entry.runtime_data._last_data_at = None
    entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()

    assert hass.states.get(switch_entity_id(hass, entry)).state != "unavailable"


async def test_it_reports_the_live_connection_as_an_attribute(hass, entry):
    """The state is intent; whether the link is up belongs beside it."""
    attributes = hass.states.get(switch_entity_id(hass, entry)).attributes
    assert attributes["connected"] is True
    assert "reconnects" in attributes


async def test_unloading_releases_the_connection(hass, entry, fake_stream):
    """A reload that kept the old link open would lock out its replacement."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert fake_stream.instances[-1].stopped == 1
