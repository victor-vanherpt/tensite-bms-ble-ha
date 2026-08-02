"""Cluster coordinator.

One coordinator per *cluster*, not per battery. This is forced by the hardware:
the ESP32 gateway accepts a single BLE central at a time, and connecting to the
cluster master relays frames for every battery in the bank. A coordinator per
battery would have them competing for the one connection slot.

**Advertisement-driven, and it has to be.** Polling on a timer was tried, to
escape the fact that this gateway only reaches Home Assistant's callbacks every
245-300 seconds and a poll can therefore only start that often. It fails: after
a few polls every connection raises "the proxy/adapter is out of connection
slots", and it never recovers until Home Assistant restarts.

That error is a red herring, and it was measured rather than guessed. With
habluetooth at INFO the adapter reports ``slots=5/5 free`` on every attempt
including the failing ones, and bleak_retry_connector never logs the
``No slots available`` rejection that real exhaustion produces. What actually
stops is the ``Found N connection path(s)`` line: once it disappears,
``async_scanner_devices_by_address(address, connectable=True)`` is returning
nothing, so no connection path exists at all and the code falls through to an
error that blames slots. Signal strength is not the discriminator either --
attempts logged at RSSI=-127 succeeded.

In short a connection can only be established while Home Assistant is holding a
live connectable scanner-device entry for the address, and that exists around
advertisements. Polling on their arrival is therefore not a workaround but the
only correct design, and the gateway's advertising cadence is a hard ceiling:
the poll delay is a *floor*, and setting it below roughly 280 seconds just
means "poll on every advertisement", which is as fast as this hardware allows.
"""

from __future__ import annotations

import logging

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_last_service_info,
    async_register_callback,
)
from homeassistant.components.bluetooth.match import BluetoothCallbackMatcher
from homeassistant.core import HomeAssistant, callback
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from tensite_bms_ble import (
    ClusterReading,
    TensiteClusterClient,
    TensiteError,
    TensiteNoDataError,
    merge_readings,
)

from .const import (
    CONNECT_TIMEOUT,
    FULL_SCAN_INTERVAL,
    LISTEN_TIMEOUT,
    POLL_GRACE_FRACTION,
    MIN_STALE_WINDOW,
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
        poll_delay: float,
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
        self.poll_delay = poll_delay
        #: See CONF_HIDE_SENTINEL_TEMPERATURES.
        self.hide_sentinel_temperatures = hide_sentinel_temperatures
        #: Options as they were at setup. A reload is only worth doing when
        #: these change -- see _async_update_listener.
        self.options_snapshot: dict = {}
        self._configured_expected = expected_batteries
        #: What the last full-window poll counted. This is the auto-detected
        #: bank size: it is set only by polls that could not exit early, so it
        #: cannot latch onto an undercount, and it drops if a battery is
        #: removed.
        self._learned_batteries = 0
        #: How many batteries answered the most recent poll, before merging
        #: forward. Lower than expected on any given poll is normal; lower
        #: every poll is not.
        self._reported_batteries = 0
        #: Monotonic time of the last full-window poll.
        self._last_full_scan: float | None = None
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

    def _needs_poll(
        self, service_info: BluetoothServiceInfoBleak, last_poll: float | None
    ) -> bool:
        """Whether this advertisement should trigger a poll.

        Timed from when the previous poll *started*, not from when it finished:
        the base class measures the latter, and the seconds a poll spends
        holding the connection then push the next advertisement just under the
        threshold, so it is skipped and the effective rate halves.

        A grace window absorbs the rest. Advertisements jitter around their
        cadence rather than landing on a grid, and one arriving a few seconds
        early is the only chance for the next several minutes.
        """
        self._last_advertisement_at = monotonic_time_coarse()
        if self._poll_in_progress:
            self._polls_skipped_overlap += 1
            return False
        since_start = (
            None
            if self._last_poll_started is None
            else monotonic_time_coarse() - self._last_poll_started
        )
        due = since_start is None or since_start >= self._due_after
        _LOGGER.debug(
            "%s: advertisement (rssi=%s) -> poll=%s (last %s, due after %.0fs)",
            self.address,
            service_info.rssi,
            due,
            "never" if since_start is None else f"{since_start:.0f}s ago",
            self._due_after,
        )
        return due

    @property
    def _due_after(self) -> float:
        """Seconds after a poll start before an advertisement is acted on."""
        return self.poll_delay * (1.0 - POLL_GRACE_FRACTION)

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
        return age <= self.stale_after

    @property
    def stale_after(self) -> float:
        """Seconds without data before entities report unavailable."""
        return (
            max(self.poll_delay * STALE_AFTER_INTERVALS, MIN_STALE_WINDOW)
            + LISTEN_TIMEOUT
        )

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
            "configured_delay_s": self.poll_delay,
            "stale_after_s": round(self.stale_after, 1),
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
            "batteries_expected": self.expected_batteries,
            "batteries_reported": self._reported_batteries,
            "battery_count_forced": self.battery_count_is_forced,
            "seconds_since_full_scan": (
                None
                if self.seconds_since_full_scan is None
                else round(self.seconds_since_full_scan, 1)
            ),
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

        The configured value wins when set; otherwise it is what the last
        full-window poll counted. 0 means "not known yet", which makes a poll
        wait out the listening window rather than exit early.

        Learning this only from full-window polls is deliberate. An ordinary
        poll stops as soon as *expected* batteries have reported, so counting
        its results would just re-measure the exit condition -- and any
        undercount would latch permanently. See FULL_SCAN_INTERVAL.
        """
        return self._configured_expected or self._learned_batteries

    @property
    def batteries_expected(self) -> int:
        """Alias of expected_batteries, for the sensor of the same name."""
        return self.expected_batteries

    @property
    def batteries_reported(self) -> int:
        """How many batteries answered the most recent poll."""
        return self._reported_batteries

    @property
    def battery_count_is_forced(self) -> bool:
        """Whether the expected count comes from configuration, not detection."""
        return bool(self._configured_expected)

    @property
    def seconds_since_full_scan(self) -> float | None:
        """Time since the last poll that waited out the full window."""
        if self._last_full_scan is None:
            return None
        return monotonic_time_coarse() - self._last_full_scan

    def _is_full_scan_due(self) -> bool:
        """Whether this poll should ignore the expected count.

        Always true until one has run, so the bank size is established on the
        very first poll rather than after an hour of guessing.
        """
        since = self.seconds_since_full_scan
        return since is None or since >= FULL_SCAN_INTERVAL

    def _note_failure(self, reason: str) -> None:
        self._poll_failures += 1
        self._consecutive_failures += 1
        self._last_error = reason

    async def _async_poll_cluster(
        self, service_info: BluetoothServiceInfoBleak
    ) -> ClusterReading | None:
        """Time one poll, record how it went, and never leave the guard set.

        Never raises: a failed poll keeps the last good reading rather than
        blanking every entity, which on BLE would happen routinely. Staleness
        is judged by ``has_fresh_data`` instead.
        """
        started = monotonic_time_coarse()
        if self._last_poll_started is not None:
            self._last_poll_interval = started - self._last_poll_started
        self._last_poll_started = started
        self._poll_in_progress = True
        self._polls += 1
        try:
            return await self._async_read_cluster(service_info)
        except Exception as err:  # noqa: BLE001 -- recorded, never propagated
            self._note_failure(f"{type(err).__name__}: {err}")
            _LOGGER.debug("%s: poll raised: %s", self.address, err)
            return self.data
        finally:
            self._last_poll_duration = monotonic_time_coarse() - started
            self._poll_in_progress = False

    async def _async_read_cluster(
        self, service_info: BluetoothServiceInfoBleak
    ) -> ClusterReading | None:
        """Connect, read the whole bank, disconnect."""
        # Prefer the BLEDevice attached to the last advertisement, which is
        # exactly the object the advertisement-driven coordinator used to hand
        # to establish_connection. Resolving by address instead leaked a
        # connection slot on every successful poll -- three or four polls in,
        # every later one failed with "the proxy/adapter is out of connection
        # slots" while the adapter sat idle, until Home Assistant restarted.
        # Falling back to the address lookup is still right when nothing has
        # been heard yet.
        ble_device = service_info.device
        if not service_info.connectable:
            # Reached us via a non-connectable proxy; ask for a connectable
            # device at the same address instead.
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
        # A full scan cannot exit early, so it is the only poll whose count
        # can be trusted as the bank size. See FULL_SCAN_INTERVAL.
        full_scan = self._is_full_scan_due()
        expect = None if full_scan else (self.expected_batteries or None)
        try:
            reading = await client.async_read(expect=expect)
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

        # Count what *this* poll heard, before merge_readings folds in
        # batteries that were quiet this time round. Taking it afterwards makes
        # "batteries reported" report the merged total, which can never fall --
        # so a bank losing a battery would look perfectly healthy, and a full
        # scan could never revise the count downwards.
        reported = reading.battery_count
        self._reported_batteries = reported
        if full_scan:
            self._learned_batteries = reported
            self._last_full_scan = monotonic_time_coarse()
            _LOGGER.debug(
                "%s: full scan found %d batteries", self.address, reported
            )

        # Carry forward anything this poll did not observe. The gateway
        # round-robins the bank, so a poll can return a battery's summary
        # without its cells -- replacing wholesale would blank all 16 cell
        # entities for that battery until a later poll happened to catch them.
        reading = merge_readings(self.data, reading)

        self._last_data_at = monotonic_time_coarse()
        self._consecutive_failures = 0
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
