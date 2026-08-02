"""Cluster coordinator.

One coordinator per *cluster*, not per battery. This is forced by the hardware:
the ESP32 gateway accepts a single BLE central at a time, and connecting to the
cluster master relays frames for every battery in the bank. A coordinator per
battery would have them competing for the one connection slot.

**Timer-driven, not advertisement-driven.** The natural choice here is
``ActiveBluetoothDataUpdateCoordinator``, which polls when an advertisement
arrives, and that is what this used to be. The problem is that this gateway
only reaches Home Assistant's callbacks every 245-300 seconds -- measured, not
assumed -- and a poll can only start when one does. That capped polling at
roughly five minutes however the interval was configured, so any shorter
setting silently did nothing.

Polls therefore run on a timer, resolving the device through
``async_ble_device_from_address``, which reads Home Assistant's own cache and
needs no fresh advertisement. The cost is that we now connect without an
advertisement first proving the device is reachable, so failed polls become a
normal outcome rather than a surprise. That is what the poll diagnostics on
this class are for.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_last_service_info,
    async_register_callback,
)
from homeassistant.components.bluetooth.match import BluetoothCallbackMatcher
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from tensite_bms_ble import (
    ClusterReading,
    TensiteClusterClient,
    TensiteError,
    TensiteNoDataError,
    merge_readings,
)

from .const import CONNECT_TIMEOUT, LISTEN_TIMEOUT, STALE_AFTER_INTERVALS

_LOGGER = logging.getLogger(__name__)


class TensiteClusterCoordinator(DataUpdateCoordinator[ClusterReading | None]):
    """Polls one cluster master on a timer and fans the result out."""

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
            hass,
            _LOGGER,
            name=f"Tensite {address}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.address = address
        self.serial = serial
        self.scan_interval = scan_interval
        #: See CONF_HIDE_SENTINEL_TEMPERATURES.
        self.hide_sentinel_temperatures = hide_sentinel_temperatures
        self._configured_expected = expected_batteries
        #: High-water mark of batteries seen, for diagnostics only.
        self._seen_batteries = 0
        #: Monotonic time of the last poll that actually returned data.
        self._last_data_at: float | None = None
        #: Monotonic time the last poll began.
        self._last_poll_started: float | None = None
        #: Set for the duration of a poll. The base class waits for one refresh
        #: to finish before scheduling the next, but an explicit
        #: async_request_refresh can still land mid-poll, and two connections to
        #: a gateway with a single slot is the one thing worth preventing.
        self._poll_in_progress = False

        # --- diagnostics ---
        self._polls = 0
        self._poll_failures = 0
        self._polls_skipped_overlap = 0
        self._consecutive_failures = 0
        #: Wall time of the most recent poll, in seconds.
        self._last_poll_duration: float | None = None
        #: Gap between the last two poll starts, as achieved.
        self._last_poll_interval: float | None = None
        #: Why the last failure happened, for the diagnostics download.
        self._last_error: str | None = None
        #: Monotonic time of the last advertisement Home Assistant passed us.
        self._last_advertisement_at: float | None = None

    def async_start(self) -> Callable[[], None]:
        """Keep Home Assistant tracking this address, and return the unsubscribe.

        Polls are timed, so nothing here triggers one. The registration exists
        because it is what tells Home Assistant's Bluetooth manager that this
        address needs *connectable* access: it keeps the device in connectable
        history, keeps a connectable scanner assigned to it, and keeps its
        connection-slot bookkeeping alive.

        Dropping this when polling moved off advertisements is what broke
        connections -- every poll after the first failed with "the proxy/adapter
        is out of connection slots" while the adapter itself was idle and the
        device was advertising a second earlier.
        """
        return async_register_callback(
            self.hass,
            self._async_on_advertisement,
            BluetoothCallbackMatcher(address=self.address, connectable=True),
            BluetoothScanningMode.ACTIVE,
        )

    @callback
    def _async_on_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        """Note that the device is alive. Polling is on a timer, so no work."""
        self._last_advertisement_at = monotonic_time_coarse()

    @property
    def seconds_since_advertisement(self) -> float | None:
        """How long since Home Assistant last heard from the gateway."""
        if self._last_advertisement_at is None:
            return None
        return monotonic_time_coarse() - self._last_advertisement_at

    @property
    def has_fresh_data(self) -> bool:
        """Whether a recent poll produced data.

        Entities key their availability off this rather than off
        ``last_update_success``: a single failed poll is routine on BLE and
        should not blank every reading, whereas several in a row genuinely
        means the bank is out of touch. Being time-based, this does not care
        how many individual polls were missed.
        """
        if self.data is None or self._last_data_at is None:
            return False
        age = monotonic_time_coarse() - self._last_data_at
        return age <= self.scan_interval * STALE_AFTER_INTERVALS + LISTEN_TIMEOUT

    @property
    def available(self) -> bool:
        return self.has_fresh_data

    @property
    def data_age(self) -> float | None:
        """Seconds since the last poll that returned data."""
        if self._last_data_at is None:
            return None
        return monotonic_time_coarse() - self._last_data_at

    @property
    def poll_health(self) -> dict[str, object]:
        """Everything needed to answer "is polling working, and if not why?"."""
        return {
            "configured_interval_s": self.scan_interval,
            "achieved_interval_s": (
                None
                if self._last_poll_interval is None
                else round(self._last_poll_interval, 1)
            ),
            "last_duration_s": (
                None
                if self._last_poll_duration is None
                else round(self._last_poll_duration, 1)
            ),
            "polls": self._polls,
            "failures": self._poll_failures,
            "consecutive_failures": self._consecutive_failures,
            "skipped_still_polling": self._polls_skipped_overlap,
            "last_error": self._last_error,
            "seconds_since_data": (
                None if self.data_age is None else round(self.data_age, 1)
            ),
            "poll_in_progress": self._poll_in_progress,
            "batteries_seen": self._seen_batteries,
            "rssi": self.rssi,
            "seconds_since_advertisement": (
                None
                if self.seconds_since_advertisement is None
                else round(self.seconds_since_advertisement, 1)
            ),
        }

    @property
    def last_poll_duration(self) -> float | None:
        """Wall time of the most recent poll, in seconds."""
        return self._last_poll_duration

    @property
    def last_poll_interval(self) -> float | None:
        """Seconds between the last two poll starts, as achieved."""
        return self._last_poll_interval

    @property
    def consecutive_failures(self) -> int:
        """Failed polls since the last one that returned data."""
        return self._consecutive_failures

    @property
    def polls_skipped_overlap(self) -> int:
        """Refreshes declined because a poll was already running."""
        return self._polls_skipped_overlap

    @property
    def cluster_name(self) -> str:
        """Human label for the cluster, preferring its serial over its address."""
        if self.serial:
            return f"Cluster {self.serial[-5:]}"
        return f"Cluster {self.address}"

    @property
    def rssi(self) -> int | None:
        """Signal strength from the last advertisement Home Assistant saw.

        Nothing depends on this any more now that polling is timed rather than
        advertisement-driven; it is reported because it is the one cheap
        indicator of *why* a connection failed.
        """
        info = async_last_service_info(self.hass, self.address, connectable=True)
        return info.rssi if info else None

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

    def _note_failure(self, reason: str) -> None:
        self._poll_failures += 1
        self._consecutive_failures += 1
        self._last_error = reason

    async def _async_update_data(self) -> ClusterReading | None:
        """Time one poll, record how it went, and never leave the guard set.

        Deliberately never raises. Raising would set ``last_update_success``
        false and, through the base class, mark entities unavailable on a
        single missed poll -- which on BLE happens routinely. Staleness is
        judged by ``has_fresh_data`` instead.
        """
        if self._poll_in_progress:
            self._polls_skipped_overlap += 1
            _LOGGER.debug(
                "%s: refresh declined, a poll is already running", self.address
            )
            return self.data

        started = monotonic_time_coarse()
        if self._last_poll_started is not None:
            self._last_poll_interval = started - self._last_poll_started
        self._last_poll_started = started
        self._poll_in_progress = True
        self._polls += 1
        try:
            return await self._async_read_cluster()
        except Exception as err:  # noqa: BLE001 -- recorded, never propagated
            self._note_failure(f"{type(err).__name__}: {err}")
            _LOGGER.debug("%s: poll raised: %s", self.address, err)
            return self.data
        finally:
            self._last_poll_duration = monotonic_time_coarse() - started
            self._poll_in_progress = False

    async def _async_read_cluster(self) -> ClusterReading | None:
        """Connect, read the whole bank, disconnect."""
        # There is no advertisement in hand any more, so ask Home Assistant's
        # cache. This is the documented way to obtain a connectable device
        # without running a scanner of our own.
        ble_device = async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            self._note_failure("not in Home Assistant's Bluetooth cache")
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
            self._note_failure(f"no data: {err}")
            _LOGGER.debug("%s: %s", self.address, err)
            return self.data
        except TensiteError as err:
            self._note_failure(str(err))
            _LOGGER.warning("%s: poll failed: %s", self.address, err)
            return self.data

        # Carry forward anything this poll did not observe. The gateway
        # round-robins the bank, so a poll can return a battery's summary
        # without its cells -- replacing wholesale would blank all 16 cell
        # entities for that battery until a later poll happened to catch them.
        reading = merge_readings(self.data, reading)

        self._last_data_at = monotonic_time_coarse()
        self._consecutive_failures = 0
        self._seen_batteries = max(self._seen_batteries, reading.battery_count)
        if self.serial is None and reading.master_serial:
            self.serial = reading.master_serial
        _LOGGER.debug(
            "%s: read %d batteries in %.1fs, %s",
            self.address,
            reading.battery_count,
            monotonic_time_coarse() - started
            if (started := self._last_poll_started) is not None
            else 0.0,
            ", ".join(
                f"{s[-5:]}={b.min_cell_mv}-{b.max_cell_mv}mV"
                for s, b in sorted(reading.batteries.items())
            ),
        )
        return reading
