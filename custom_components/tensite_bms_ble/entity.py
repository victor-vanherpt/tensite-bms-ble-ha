"""Shared entity plumbing and the cluster -> battery -> cell device tree.

Home Assistant links devices with ``via_device``, which lets the registry
mirror the physical hierarchy:

    Cluster (the BLE gateway)
      └── Battery  (one per unit in the bank, keyed on its printed serial)
            └── Cell  (16 per battery)

Cell voltages therefore attach to cells, cells roll up to their battery, and
batteries roll up to the cluster.
"""

from __future__ import annotations

import re

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    MANUFACTURER,
    MODEL_BATTERY,
    MODEL_CELL,
    MODEL_CLUSTER,
)
from .coordinator import TensiteClusterCoordinator

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def cluster_device_id(address: str) -> str:
    return f"cluster_{address}"


def cell_device_id(serial: str, index: int) -> str:
    return f"{serial}_cell_{index:02d}"


def cluster_device_info(coordinator: TensiteClusterCoordinator) -> DeviceInfo:
    """Device entry for the BLE gateway that fronts the bank."""
    info = DeviceInfo(
        identifiers={(DOMAIN, cluster_device_id(coordinator.address))},
        name=coordinator.cluster_name,
        manufacturer=MANUFACTURER,
        model=MODEL_CLUSTER,
    )
    # Only Linux gives us a real MAC here; macOS hands out an opaque
    # CoreBluetooth UUID, which is not a valid Bluetooth connection value.
    if _MAC_RE.match(coordinator.address):
        info["connections"] = {(CONNECTION_BLUETOOTH, coordinator.address)}
    if coordinator.serial:
        info["serial_number"] = coordinator.serial
    return info


def battery_device_info(
    coordinator: TensiteClusterCoordinator,
    serial: str,
    position_label: str,
    model: str | None = None,
) -> DeviceInfo:
    """Device entry for one battery, parented to its cluster."""
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=f"Battery {position_label.split('/')[-1]} {serial[-5:]}",
        manufacturer=MANUFACTURER,
        # The BMS reports its own model string (e.g. AB4850/100_2.0) in a
        # type-0x24 frame, which does not always arrive in the first poll.
        model=model or MODEL_BATTERY,
        serial_number=serial,
        via_device=(DOMAIN, cluster_device_id(coordinator.address)),
    )


def cell_device_info(serial: str, index: int) -> DeviceInfo:
    """Device entry for one cell, parented to its battery."""
    return DeviceInfo(
        identifiers={(DOMAIN, cell_device_id(serial, index))},
        name=f"Cell {index:02d}",
        manufacturer=MANUFACTURER,
        model=MODEL_CELL,
        via_device=(DOMAIN, serial),
    )


class TensiteEntity(CoordinatorEntity[TensiteClusterCoordinator]):
    """Base for every entity in this integration.

    Availability is deliberately *not* the inherited ``last_update_success``.
    A single failed poll is routine on BLE, and letting one blank every reading
    made entities flap constantly while polls were in fact succeeding most of
    the time. What matters for a mains-powered device we actively poll is
    whether a recent poll produced data at all, which is time-based and
    indifferent to how many individual attempts were missed.
    """

    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        return self.coordinator.has_fresh_data
