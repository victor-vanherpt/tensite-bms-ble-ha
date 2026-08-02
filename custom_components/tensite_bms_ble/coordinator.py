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

from .const import (
    CONNECT_TIMEOUT,
    LISTEN_TIMEOUT,
    POLL_GRACE_FRACTION,
    STALE_AFTER_INTERVALS,
)

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
        #: Monotonic time the last poll *began*. See _needs_poll().
        self._last_poll_started: float | None = None
        #: Set for the duration of a poll. Home Assistant's debouncer already
        #: refuses to run two polls at once, but without this a poll that runs
        #: longer than the interval leaves _needs_poll saying "due" while it is
        #: still going, and the debouncer fires the queued one the instant it
        #: returns -- back-to-back connections that never let go of the
        #: gateway's single slot.
        self._poll_in_progress = False

        # --- diagnostics ---
        self._polls = 0
        self._poll_failures = 0
        self._polls_skipped_overlap = 0
        self._consecutive_failures = 0
        #: Wall time of the most recent poll, in seconds.
        self._last_poll_duration: float | None = None
        #: Gap between the last two poll starts. The honest answer to "is it
        #: actually polling every N seconds?", since the interval is a floor
        #: and advertisements decide when a poll may happen.
        self._last_poll_interval: float | None = None
        #: Why the last failure happened, for the diagnostics download.
        self._last_error: str | None = None

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
    def _due_after(self) -> float:
        """Seconds after a poll start before the next advertisement is taken.

        Slightly less than the interval; see POLL_GRACE_FRACTION.
        """
        return self.scan_interval * (1.0 - POLL_GRACE_FRACTION)

    @property
    def poll_health(self) -> dict[str, object]:
        """Everything needed to answer "is polling working, and if not why?".

        The interval is a floor, not a schedule: a poll can only start when an
        advertisement arrives, so the achieved interval is what actually
        matters and is reported separately from the configured one.
        """
        return {
            "configured_interval_s": self.scan_interval,
            "due_after_s": round(self._due_after, 1),
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
        """Advertisements ignored because a poll was still running."""
        return self._polls_skipped_overlap

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
        """Poll on the first advertisement, then once per interval.

        Timed from when the previous poll *started*, not from when it finished.
        The base class's *last_poll* measures the latter, and that quietly
        halves the polling rate: this gateway advertises on a fixed cadence, so
        if the interval is set to match it, the seconds a poll spends holding
        the connection push the next advertisement just under the threshold.
        It gets skipped, and the effective interval becomes two advertisements
        rather than one -- 10 minutes for a 5-minute setting.
        """
        # Deliberately not gated on hass.is_running. Config entries are set up
        # while Home Assistant is still CoreState.not_running, so that guard
        # rejects the very first advertisement -- and this gateway advertises
        # to registered callbacks rarely enough that the next chance can be
        # minutes away. The base class already refuses to poll while stopping,
        # which is the case that actually matters.
        if self._poll_in_progress:
            self._polls_skipped_overlap += 1
            _LOGGER.debug(
                "%s: advertisement ignored, a poll is still running",
                self.address,
            )
            return False

        since_start = (
            None
            if self._last_poll_started is None
            else monotonic_time_coarse() - self._last_poll_started
        )
        due = since_start is None or since_start >= self._due_after
        _LOGGER.debug(
            "%s: advertisement (rssi=%s connectable=%s) -> poll=%s "
            "(last_poll=%s due_after=%.0fs interval=%ss)",
            self.address,
            service_info.rssi,
            service_info.connectable,
            due,
            "never" if since_start is None else f"{since_start:.0f}s ago",
            self._due_after,
            self.scan_interval,
        )
        return due

    async def _async_poll_cluster(
        self, service_info: BluetoothServiceInfoBleak
    ) -> ClusterReading | None:
        """Time one poll, record how it went, and never leave the guard set."""
        started = monotonic_time_coarse()
        if self._last_poll_started is not None:
            self._last_poll_interval = started - self._last_poll_started
        self._last_poll_started = started
        self._poll_in_progress = True
        self._polls += 1
        try:
            return await self._async_read_cluster(service_info)
        except Exception as err:  # noqa: BLE001 -- recorded, then re-raised
            self._note_failure(f"{type(err).__name__}: {err}")
            raise
        finally:
            self._last_poll_duration = monotonic_time_coarse() - started
            self._poll_in_progress = False

    def _note_failure(self, reason: str) -> None:
        self._poll_failures += 1
        self._consecutive_failures += 1
        self._last_error = reason

    async def _async_read_cluster(
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
            self._note_failure("no connectable device available")
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
            "%s: read %d batteries, %s",
            self.address,
            reading.battery_count,
            ", ".join(
                f"{s[-5:]}={b.min_cell_mv}-{b.max_cell_mv}mV"
                for s, b in sorted(reading.batteries.items())
            ),
        )
        return reading
