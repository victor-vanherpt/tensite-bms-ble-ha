"""The connection switch -- a circuit breaker for the gateway's one slot.

The ESP32 gateway accepts a single BLE central at a time, and this integration
now holds that slot continuously. That is what makes readings arrive every few
seconds, and it is also what stops the Tensite app from connecting at all.

So the hold is made explicit and revocable. Turning this off disconnects
immediately and stays off: no advertisement reopens it, and the app can connect.
Turning it back on reconnects at the next opportunity.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TensiteConfigEntry
from .coordinator import TensiteClusterCoordinator
from .entity import TensiteEntity, cluster_device_info

CONNECTION_SWITCH = SwitchEntityDescription(
    key="connection",
    translation_key="connection",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TensiteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the connection switch."""
    async_add_entities([TensiteConnectionSwitch(entry.runtime_data)])


class TensiteConnectionSwitch(TensiteEntity, SwitchEntity):
    """Whether the integration holds the gateway's connection."""

    entity_description = CONNECTION_SWITCH

    def __init__(self, coordinator: TensiteClusterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_connection"
        self._attr_device_info = cluster_device_info(coordinator)

    @property
    def available(self) -> bool:
        """Always. This is the control that *fixes* an unavailable cluster.

        Every other entity reports unavailable when data stops arriving, which
        is the honest state for a reading. A switch that vanished whenever the
        connection was down would be unusable exactly when it is wanted, and
        would make turning the integration back on impossible.
        """
        return True

    @property
    def is_on(self) -> bool:
        """Whether the connection is meant to be held.

        Intent, not state: reporting the live connection would make the switch
        flick itself off during a reconnect and invite an automation to fight
        it. Whether the link is actually up is what the entities' availability
        and the connection diagnostics are for.
        """
        return self.coordinator.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Deliberately only values that hold still.

        Every attribute change writes a recorder row, and this entity is
        updated on every batch of frames -- so an uptime counter here would
        write one every few seconds for a switch nobody touched. Uptime is in
        the connection diagnostics instead.
        """
        return {
            "connected": self.coordinator.is_connected,
            "reconnects": self.coordinator.reconnects,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_enabled(False)
        self.async_write_ha_state()
