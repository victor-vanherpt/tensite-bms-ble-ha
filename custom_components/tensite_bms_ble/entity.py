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

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo

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
    coordinator: TensiteClusterCoordinator, serial: str, position_label: str
) -> DeviceInfo:
    """Device entry for one battery, parented to its cluster."""
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=f"Battery {position_label.split('/')[-1]} {serial[-5:]}",
        manufacturer=MANUFACTURER,
        model=MODEL_BATTERY,
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


class TensiteEntity(PassiveBluetoothCoordinatorEntity[TensiteClusterCoordinator]):
    """Base for every entity in this integration.

    Built on ``PassiveBluetoothCoordinatorEntity`` rather than the general
    ``CoordinatorEntity``: Bluetooth coordinators track liveness through
    advertisements and expose ``available``, where ``CoordinatorEntity``
    expects a ``last_update_success`` flag that these coordinators do not have.

    The inherited ``available`` reflects only whether the gateway is being seen.
    Entities that render polled data additionally require a reading, since the
    device can be advertising happily while no poll has yet succeeded.
    """

    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        return self.coordinator.available and self.coordinator.data is not None
