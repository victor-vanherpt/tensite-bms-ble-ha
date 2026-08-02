"""Cluster coordinator.

One coordinator per *cluster*, not per battery. This is forced by the hardware:
the ESP32 gateway accepts a single BLE central at a time, and connecting to the
cluster master relays frames for every battery in the bank. A coordinator per
battery would have them competing for the one connection slot.
"""

from __future__ import annotations

import logging

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
)
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.core import HomeAssistant
from tensite_bms_ble import (
    ClusterReading,
    TensiteClusterClient,
    TensiteError,
    TensiteNoDataError,
    merge_readings,
)

from .const import CONNECT_TIMEOUT, LISTEN_TIMEOUT, STALE_AFTER_INTERVALS

_LOGGER = logging.getLogger(__name__)


class TensiteClusterCoordinator(
    ActiveBluetoothDataUpdateCoordinator[ClusterReading | None]
):
    """Polls one cluster master and fans the result out to every battery."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        serial: str | None,
        scan_interval: float,
        expected_batteries: int = 0,
        hide_sentinel_temperatures: bool = False,
    ) -> None:
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            address=address,
            mode=BluetoothScanningMode.ACTIVE,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_poll_cluster,
            connectable=True,
        )
        self.serial = serial
        self.scan_interval = scan_interval
        #: See CONF_HIDE_SENTINEL_TEMPERATURES.
        self.hide_sentinel_temperatures = hide_sentinel_temperatures
        self._configured_expected = expected_batteries
        #: High-water mark of batteries seen, for diagnostics only.
        self._seen_batteries = 0
        #: Monotonic time of the last poll that actually returned data.
        self._last_data_at: float | None = None

    @property
    def has_fresh_data(self) -> bool:
        """Whether a recent poll produced data.

        Entities key their availability off this rather than off advertisement
        freshness -- see the note in ``TensiteEntity``. The window is generous
        because a single missed poll is routine on BLE; several consecutive
        failures are what should actually surface as unavailable.
        """
        if self.data is None or self._last_data_at is None:
            return False
        age = monotonic_time_coarse() - self._last_data_at
        return age <= self.scan_interval * STALE_AFTER_INTERVALS + LISTEN_TIMEOUT

    @property
    def data_age(self) -> float | None:
        """Seconds since the last poll that returned data."""
        if self._last_data_at is None:
            return None
        return monotonic_time_coarse() - self._last_data_at

    @property
    def cluster_name(self) -> str:
        """Human label for the cluster, preferring its serial over its address."""
        if self.serial:
            return f"Cluster {self.serial[-5:]}"
        return f"Cluster {self.address}"

    @property
    def rssi(self) -> int | None:
        """Signal strength from the most recent advertisement."""
        if self._last_service_info is not None:
            return self._last_service_info.rssi
        return None

    @property
    def expected_batteries(self) -> int:
        """How many batteries a poll waits for before returning early.

        Only ever the explicitly configured count. Deriving it from what a
        previous poll happened to see is a trap: the gateway round-robins the
        bank, so an early poll can easily catch three of four batteries. Wiring
        that back in as the exit condition would make every later poll stop at
        three and the fourth battery would never be discovered.
        """
        return self._configured_expected

    def _needs_poll(
        self, service_info: BluetoothServiceInfoBleak, last_poll: float | None
    ) -> bool:
        """Poll on the first advertisement, then no more often than the interval.

        *last_poll* is seconds elapsed since the previous poll attempt, or None
        if none has happened yet.
        """
        # Deliberately not gated on hass.is_running. Config entries are set up
        # while Home Assistant is still CoreState.not_running, so that guard
        # rejects the very first advertisement -- and this gateway advertises
        # to registered callbacks rarely enough that the next chance can be
        # minutes away. The base class already refuses to poll while stopping,
        # which is the case that actually matters.
        due = last_poll is None or last_poll >= self.scan_interval
        _LOGGER.debug(
            "%s: advertisement (rssi=%s connectable=%s) -> poll=%s "
            "(last_poll=%s interval=%ss)",
            self.address,
            service_info.rssi,
            service_info.connectable,
            due,
            "never" if last_poll is None else f"{last_poll:.0f}s ago",
            self.scan_interval,
        )
        return due

    async def _async_poll_cluster(
        self, service_info: BluetoothServiceInfoBleak
    ) -> ClusterReading | None:
        """Connect, read the whole bank, disconnect.

        Uses ``service_info.device`` where possible: per the Home Assistant
        Bluetooth docs this is the cheapest way to obtain a usable BLEDevice,
        with no scanner of our own involved.
        """
        ble_device = service_info.device
        if not service_info.connectable:
            # The advertisement reached us via a non-connectable proxy; ask
            # Home Assistant for a connectable device at the same address.
            ble_device = async_ble_device_from_address(
                self.hass, service_info.address, connectable=True
            )
        if ble_device is None:
            _LOGGER.debug(
                "%s: no connectable device available, keeping last reading",
                self.address,
            )
            return self.data

        client = TensiteClusterClient(
            ble_device,
            serial=self.serial,
            connect_timeout=CONNECT_TIMEOUT,
            listen_timeout=LISTEN_TIMEOUT,
            logger=_LOGGER,
        )
        try:
            reading = await client.async_read(expect=self.expected_batteries or None)
        except TensiteNoDataError as err:
            # Connected but the bank stayed quiet. Transient often enough that
            # dropping the last known reading would flap every entity.
            _LOGGER.debug("%s: %s", self.address, err)
            return self.data
        except TensiteError as err:
            _LOGGER.warning("%s: poll failed: %s", self.address, err)
            return self.data

        # Carry forward anything this poll did not observe. The gateway
        # round-robins the bank, so a poll can return a battery's summary
        # without its cells -- replacing wholesale would blank all 16 cell
        # entities for that battery until a later poll happened to catch them.
        reading = merge_readings(self.data, reading)

        self._last_data_at = monotonic_time_coarse()
        self._seen_batteries = max(self._seen_batteries, reading.battery_count)
        if self.serial is None and reading.master_serial:
            self.serial = reading.master_serial
        _LOGGER.debug(
            "%s: read %d batteries, %s",
            self.address,
            reading.battery_count,
            ", ".join(
                f"{s[-5:]}={b.min_cell_mv}-{b.max_cell_mv}mV"
                for s, b in sorted(reading.batteries.items())
            ),
        )
        return reading
